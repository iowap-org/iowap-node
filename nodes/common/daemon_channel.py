"""T-026: workspace channel in reverse — adopt ``<workspace>/bridge/daemon.json``.

The bridge daemon (Go, ``internal/daemonfile``, v0.3.2) writes
``{"url": ..., "token": ...}`` to ``<workspace>/bridge/daemon.json`` after a
successful bootstrap. This fixes the token desync of hand-recreated
containers: a container created via a plain ``docker compose up`` carries no
``IOWAP_BRIDGE_TOKEN`` env, and the daemon starts existing containers as-is
(DEC #6, never recreated) — so env injection can never repair such a
container. The shared workspace volume is the channel that survives every
recreate (same pattern as the T-022 providers.yaml watch, in reverse).

This module runs a small watcher thread (10 s poll, like the daemon's
T-022 watch) inside the node daemon(s). When the file exists and its
``url``/``token`` differ from the active node.yaml ``bridge:`` block, it
writes ONLY ``bridge.url`` and ``bridge.token`` (same atomic
load-mutate-dump pattern as the dashboard publish toggle, T-021 — every
other field and all non-bridge keys survive untouched) and SIGHUPs the
daemon pid files (the existing config-reload path) so the capability cache
is invalidated and the bridge manifest poller picks the new values up on
its next cycle.

**Precedence rule (board T-026): ``daemon.json`` always wins.** As long as
the file exists, a hand-configured ``bridge.url``/``bridge.token`` in
node.yaml is overwritten by the next poll cycle. Rationale: the file is
the daemon's own bootstrap fact (it knows port and token by construction)
and two writers fighting silently is worse than one obvious direction.
EVERY takeover is logged (url + token length, never the token itself).
To opt out, delete ``<workspace>/bridge/daemon.json`` — the watcher then
stops applying anything (a missing file is a no-op, never a crash).

The watcher only touches an EXISTING node.yaml: a node that has not yet
been registered has no use for a bridge block, and the registration
(onboarding POST) creates node.yaml itself — the watcher adopts the bridge
block on a later cycle.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import yaml

from nodes.common import node_config, node_utils

logger = logging.getLogger("daemon-channel")

# Poll cadence (board T-026: "Poll wie T-022, 10 s").
WATCH_INTERVAL = 10


# ---------------------------------------------------------------------------
# Reading the channel file
# ---------------------------------------------------------------------------


def daemon_json_path() -> Path:
    """``<workspace>/bridge/daemon.json`` (env ``IOWAP_WORKSPACE_DIR``, default /workspace)."""
    env = os.environ.get("IOWAP_WORKSPACE_DIR", "").strip()
    base = Path(env) if env else Path("/workspace")
    return base / "bridge" / "daemon.json"


def read_daemon_json(path: Path | None = None) -> tuple[str, str] | None:
    """Parse the channel file into ``(url, token)`` — ``None`` if unusable.

    A missing file is the normal no-op case; unparsable or incomplete
    content logs a warning and returns ``None`` (the watcher must never
    die on channel content — same discipline as ``load_bridge_config``).
    """
    path = path or daemon_json_path()
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("could not read %s: %s", path, exc)
        return None
    if not isinstance(doc, dict):
        logger.warning("%s is not a JSON object — ignoring", path)
        return None
    url, token = doc.get("url"), doc.get("token")
    if not (isinstance(url, str) and url.strip()) or not (
        isinstance(token, str) and token.strip()
    ):
        logger.warning("%s is missing url or token — ignoring", path)
        return None
    return url.rstrip("/"), token


# ---------------------------------------------------------------------------
# Current node.yaml bridge identity + adoption
# ---------------------------------------------------------------------------


def _current_identity(active_path: Path) -> tuple[str, str] | None:
    """The literal ``(url, token)`` of the active node.yaml bridge block (or None)."""
    if not active_path.exists():
        return None
    try:
        parsed = yaml.safe_load(active_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("bridge"), dict):
        return None
    block = parsed["bridge"]
    url = block.get("url")
    token = block.get("token")
    if not isinstance(url, str) or not url.strip():
        return None
    return url.rstrip("/"), token if isinstance(token, str) else ""


def _adopt(
    active_path: Path, url: str, token: str
) -> tuple[bool, str]:
    """Write ONLY ``bridge.url``/``bridge.token`` into node.yaml (atomically).

    Same contract as the dashboard's ``_update_bridge_block`` (T-021): the
    rest of the ``bridge:`` block and all non-bridge keys survive; a
    non-mapping block is replaced with a fresh mapping carrying the new
    url/token. Reload is the caller's job.
    """
    try:
        loaded = yaml.safe_load(active_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return False, f"could not parse {active_path}: {exc}"
    data: dict[str, Any] = loaded if isinstance(loaded, dict) else {}
    block = data.get("bridge")
    if not isinstance(block, dict):
        block = {}
    # T-045: the adoption write is a node.yaml bridge-block write path,
    # so the one-shot manual-publish migration runs here too — a VM whose
    # config is only ever touched by the daemon.json channel still gets
    # its blanket publish state conserved (auto_publish off, allowlist
    # seeded, active profile recomposed ONCE).
    from nodes.common import bridge as _bridge
    from nodes.common import dashboard as _dash

    migrated = _bridge.migrate_manual_publish_block(
        block,
        [p.get("name") for p in _dash.load_providers() if isinstance(p, dict)],
    )
    if migrated is not None:
        block = migrated
    block["url"] = url
    block["token"] = token
    data["bridge"] = block
    active_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = active_path.with_suffix(active_path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    tmp.replace(active_path)
    if migrated is not None:
        from nodes.common import active_profile

        active_profile.recompile_active_profile()
    return True, "node.yaml updated"


def apply_daemon_json(
    path: Path | None = None, active_path: Path | None = None
) -> tuple[bool, str]:
    """One adoption cycle: read the channel, compare, write, SIGHUP.

    Returns ``(changed, message)``. Idempotent: an unchanged identity is
    not rewritten and not re-signalled.
    """
    active = active_path or node_config.ACTIVE_PATH
    desired = read_daemon_json(path)
    if desired is None:
        return False, "no usable daemon.json"
    if not active.exists():
        # Unregistered node — node.yaml appears with the registration; the
        # bridge block is adopted on a later cycle.
        return False, "no active node.yaml yet"
    url, token = desired
    if _current_identity(active) == (url, token):
        return False, "unchanged"
    ok, message = _adopt(active, url, token)
    if not ok:
        logger.warning("daemon.json adoption failed: %s", message)
        return False, message
    logger.info(
        "bridge endpoint adopted from %s into node.yaml (url=%s, token=%d chars) — "
        "daemon.json wins while it exists (T-026)",
        path or daemon_json_path(), url, len(token),
    )
    _reload_daemons()
    return True, message


def _reload_daemons() -> None:
    """SIGHUP the daemon pid files (existing reload path, as dashboard.py)."""
    notified = node_utils.sighup_pid_files(
        [node_utils.BASE_DIR / "node-daemon.pid", node_utils.BASE_DIR / "node-cli.pid"]
    )
    if notified:
        logger.info("config reload: SIGHUPed daemon pids %s", notified)


# ---------------------------------------------------------------------------
# Watcher thread (wired next to the bridge manifest poller, T-003)
# ---------------------------------------------------------------------------


def _watch_loop(stop_event: threading.Event, interval: int) -> None:
    while not stop_event.is_set():
        try:
            apply_daemon_json()
        except Exception:  # noqa: BLE001 — the watcher must never kill the daemon
            logger.exception("daemon.json watcher cycle failed")
        for _ in range(max(1, int(interval))):
            if stop_event.is_set():
                return
            time.sleep(1)


def start_watcher(
    stop_event: threading.Event, interval: int = WATCH_INTERVAL
) -> threading.Thread:
    """Start the daemon.json watcher (daemon thread, stops with the daemon)."""
    thread = threading.Thread(
        target=_watch_loop, args=(stop_event, interval), daemon=True,
        name="daemon-json-watch",
    )
    thread.start()
    logger.info("daemon.json watcher started (poll %ss)", interval)
    return thread
