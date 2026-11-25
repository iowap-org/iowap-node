"""T-046: node-local budget accounting (meter + config read).

Ronny's budget model (T-046, „Node-Dashboard: Budget-Config + Prozent“):

* the node.yaml ``budget.total`` field (mapping form ``budget: {total: N}``,
  default **100** when the block is absent) sets the accounting ceiling —
  both budget forms feed BOTH axes: the legacy ``budget: <int>`` (T-247
  admission) and the mapping form's ``total`` are also the number the
  admission axis caps at (``load_active_budget``), so writing the
  dashboard field can never silently disarm admission;
* every **completed** capability task increments the counter by the
  capability's admission-points ``cost`` (T-247), but ONLY bridge
  capabilities with ``cost > 0`` count (Ronny: „nur bridge-Caps mit
  cost>0 zählen“ — non-bridge/local handler work and free caps are not
  metered);
* the counter persists in ``<relay-dir>/state/budget_used`` written
  ATOMICALLY (tmp + os.replace, the T-035 agent-token store pattern);
* the dashboard shows ``used N / budget B (P%)`` plus a Reset button;
  there is NO auto-busy at 100% (v1 deliberately simple — Ronny tests
  whether auto-busy is wanted before we wire any enforcement).

The module is transport-agnostic (the T-037 split rule): daemons call
:func:`record_stage_cost` at the completion site, the dashboard renders
:func:`read_used`/:func:`budget_total` and calls the active-profile
mutation in ``active_profile.write_budget_total`` (config write lives
there because it shares the validate+atomic+SIGHUP recompose pattern).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from nodes.common import node_config

logger = logging.getLogger("budget")

DEFAULT_BUDGET_TOTAL = 100
BUDGET_STATE_ENV = "IOWAP_BUDGET_DIR"
USED_FILE = "budget_used"

_METER_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Path resolution (env override > <relay-dir>/state)
# ---------------------------------------------------------------------------


def state_dir() -> Path:
    """``IOWAP_BUDGET_DIR`` overrides everything (tests, exotic installs);
    default ``<relay-dir>/state`` (``~/.relay/state`` locally, the
    container mounts the relay dir there)."""
    override = os.environ.get(BUDGET_STATE_ENV, "").strip()
    if override:
        return Path(override)
    return Path(node_config.BASE_DIR) / "state"


def used_path() -> Path:
    """:py:attr:`state_dir` / ``budget_used``."""
    return state_dir() / USED_FILE


# ---------------------------------------------------------------------------
# Accounting counter (atomic JSON, fresh read per call)
# ---------------------------------------------------------------------------


def read_used() -> int:
    """The accumulated meter value; missing/corrupt file → ``0``.

    Reads freshly per call — two writer processes (SSE + poll daemon are
    never both, but the dashboard resets independently) rely on
    read-modify-write under :py:data:`_METER_LOCK` per process; a
    cross-process race is last-writer-wins, accepted like the T-035
    agent-token store (writes are one number large).
    """
    path = used_path()
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("budget meter %s unreadable (%s) — degrading to 0", path, exc)
        return 0
    if not isinstance(data, dict):
        logger.warning("budget meter %s is not an object — degrading to 0", path)
        return 0
    used = data.get("used")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        logger.warning("budget meter %s has a non-numeric used — degrading to 0", path)
        return 0
    return int(used)


def _write_used(used: int) -> None:
    """Atomic write (tmp + os.replace, T-035 pattern). Never partial."""
    path = used_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(
            {"version": 1, "used": used, "updated_at": _utcnow()}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def record_stage_cost(cap: dict[str, Any] | None, cost: Any) -> int | None:
    """Meter ONE completed capability task; returns the new value or ``None``.

    The gate lives HERE so the daemon completion sites stay dumb:
    only a **bridge** capability (``cap.get("bridge")``) with
    ``cost > 0`` increments — everything else returns ``None`` without
    touching the file. Never raises (a broken meter must not kill the
    completion path); write failures degrade to ``None`` with a log.
    """
    if not isinstance(cap, dict) or not cap.get("bridge"):
        return None
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return None
    if cost <= 0:
        return None
    # admission points are T-247 integers; tolerate a float provider
    # cost by rounding DOWN (never overcharge the operator).
    points = max(0, int(cost))
    if points <= 0:
        return None
    with _METER_LOCK:
        used = read_used() + points
        try:
            _write_used(used)
        except OSError as exc:
            logger.warning("budget meter write failed: %s", exc)
            return None
    return used


def reset_used() -> int:
    """Set the meter back to ``0`` (Reset button). Returns the new value."""
    with _METER_LOCK:
        try:
            _write_used(0)
        except OSError as exc:
            logger.warning("budget meter reset failed: %s", exc)
            return read_used()
    return 0


# ---------------------------------------------------------------------------
# Budget config read (node.yaml ``budget.total``, default 100)
# ---------------------------------------------------------------------------


def budget_total(path: Path | None = None) -> int:
    """The accounting ceiling from the active YAML profile.

    * ``budget: {total: N}`` (mapping form, T-046) → ``N``;
    * ``budget: <int>`` (legacy T-247 admission form) → the same int (a
      node that only set the admission axis gets one ceiling for both);
    * absent / unreadable / invalid → :py:data:`DEFAULT_BUDGET_TOTAL`
      (100). Read fresh from disk — the dashboard and the daemons share
      the no-cache discipline of ``load_active_status``.
    """
    try:
        active = path or node_config.ACTIVE_PATH
    except AttributeError:  # pragma: no cover — defensive, never in tests
        return DEFAULT_BUDGET_TOTAL
    if not active.exists():
        return DEFAULT_BUDGET_TOTAL
    try:
        parsed = yaml.safe_load(active.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:  # pragma: no cover — logged path
        logger.warning("budget_total: cannot read %s (%s) — default", active, exc)
        return DEFAULT_BUDGET_TOTAL
    if not isinstance(parsed, dict):
        return DEFAULT_BUDGET_TOTAL
    total = _budget_total_from(parsed.get("budget"))
    if total is None:
        return DEFAULT_BUDGET_TOTAL
    return total


def _budget_total_from(raw: Any) -> int | None:
    """The accounting total from the raw ``budget`` YAML value; ``None`` → default."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, dict):
        total = raw.get("total")
        if isinstance(total, bool) or not isinstance(total, int):
            return None
        return total if total >= 0 else None
    return None


def percent(used: int, total: int) -> int:
    """``used / total * 100`` floored; ``total <= 0`` → 0 (never divide by zero)."""
    if total <= 0:
        return 0
    return int(used * 100 / total)
