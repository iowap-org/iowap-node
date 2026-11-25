"""T-035 P1a: agent-token store API — the node manages its MCP agents.

The universal node's HTTP MCP endpoint (``iowap-node-mcp``, T-037) serves
agents that used to share one operator Bearer (``IOWAP_MCP_BEARER``). T-035
makes the node own per-agent tokens: the dashboard (HTTP, onboarding.py)
mints them show-once, and the MCP process enforces them multi-bearer
(DEC 2026-11-25, "Node-owned Enforcement" — enforcement point is the
Node-MCP-layer, the relay is untouched).

Storage lives in a DEDICATED named volume (DEC 2026-11-25, "not a folder on
the Windows host"): ``iowap-agent-tokens`` mounted at ``/data/agents`` —
Explorer on the VM never sees it. Both processes (dashboard + MCP server)
read these JSON files directly; the module is transport-agnostic per the
T-037 split rule — everything under ``nodes/common`` knows nothing about
MCP transports, and the MCP-side readers keep the same JSON contracts
without importing this package.

Files (both written atomically — tmp + os.replace, T-021 write pattern):

``agents.json``
    ``{"version": 1, "agents": [entry, ...]}`` where an entry is
    ``{"id": "agt-<8hex>", "name", "token_sha256", "created_at",
    "last_used", "caps_manage", "revoked"}``. The raw token is NEVER
    stored — ``create()`` returns it exactly once (show-once, caller
    renders it), only its SHA-256 hash rides the file.

``bindings.json``
    ``{"version": 1, "bindings": {agent_id: {"task_ids": [...],
    "seen_artifacts": {artifact_id: [task_id, ...]}}}}`` — which agent
    submitted which task and saw which artifact (T-035 P2 scoping view).

Primary/legacy: the operator's ``IOWAP_MCP_BEARER`` bearer is NOT recorded
here at all; every ``can_read_task("primary", ...)`` path returns full
access (read-any / manage). Old tasks stay readable for everyone and no
binding is ever written for the primary path.

Fail-closed: a corrupt/invalid ``agents.json`` degrades to primary-only
(log a warning, never crash, never rewrite from the read path). Store
reads happen fresh per call — no cache — because the dashboard and the
MCP process both write here (last-writer-wins per file is documented and
accepted by DEC; writes are one entry large).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nodes.common import node_config

logger = logging.getLogger("agent-tokens")

AGENT_DIR_ENV = "IOWAP_AGENTS_DIR"
VOLUME_AGENT_DIR = Path("/data/agents")
AGENTS_FILE = "agents.json"
BINDINGS_FILE = "bindings.json"

_TOKEN_PREFIX = "iw_agt_"
_TOKEN_ENTROPY_BYTES = 32
_PRIMARY = "primary"


# ---------------------------------------------------------------------------
# Path resolution (Volume > relay-dir fallback, env override for tests)
# ---------------------------------------------------------------------------


def agents_dir() -> Path:
    """The directory holding ``agents.json`` / ``bindings.json``.

    ``IOWAP_AGENTS_DIR`` overrides everything (tests, exotic installs).
    Otherwise the dedicated token volume is used when its directory
    exists (``/data/agents`` — compose/daemon create the mount); fresh or
    volume-less containers (and every pre-T-035 install) fall back to
    ``<relay-dir>/agents`` so old containers stay runnable.
    """
    override = os.environ.get(AGENT_DIR_ENV, "").strip()
    if override:
        return Path(override)
    if VOLUME_AGENT_DIR.is_dir():
        return VOLUME_AGENT_DIR
    return Path(node_config.BASE_DIR) / "agents"


def agents_path() -> Path:
    """:py:attr:`agents_dir` / ``agents.json``."""
    return agents_dir() / AGENTS_FILE


def bindings_path() -> Path:
    """:py:attr:`agents_dir` / ``bindings.json``."""
    return agents_dir() / BINDINGS_FILE


# ---------------------------------------------------------------------------
# Atomic JSON I/O (fresh read per call — no cache, two writers by design)
# ---------------------------------------------------------------------------


def _read_json(path: Path, default: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Read a store JSON file; ``() -> (data, ok)`` — ``ok=False`` on any error."""
    if not path.exists():
        return default, True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("agent-token store %s unreadable (%s) — degrading", path, exc)
        return default, False
    if not isinstance(data, dict):
        logger.warning("agent-token store %s is not an object — degrading", path)
        return default, False
    return data, True


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic write (tmp + chmod 0600 + os.replace). Never partial."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# agents.json
# ---------------------------------------------------------------------------


def _empty_agents() -> dict[str, Any]:
    return {"version": 1, "agents": []}


def load_agents() -> list[dict[str, Any]]:
    """List the known agents; corrupt/missing file → ``[]`` (primary-only)."""
    data, _ = _read_json(agents_path(), _empty_agents())
    agents = data.get("agents")
    if not isinstance(agents, list):
        return []
    return [a for a in agents if isinstance(a, dict)]


def _save_agents(agents: list[dict[str, Any]]) -> None:
    _write_json(agents_path(), {"version": 1, "agents": agents})


def _new_agent_id() -> str:
    return f"agt-{secrets.token_hex(4)}"


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# tokens + bindings JSON
# ---------------------------------------------------------------------------


def _empty_bindings() -> dict[str, Any]:
    return {"version": 1, "bindings": {}}


def load_bindings() -> dict[str, dict[str, Any]]:
    """The binding map ``agent_id -> {task_ids, seen_artifacts}``.

    Corrupt/missing file → ``{}`` — an unreadable binding store never
    widens access (the MCP layer treats an agent with no binding entries
    as ownerless), and never crashes it either.
    """
    data, _ = _read_json(bindings_path(), _empty_bindings())
    bindings = data.get("bindings")
    if not isinstance(bindings, dict):
        return {}
    return {k: v for k, v in bindings.items() if isinstance(v, dict)}


def _save_bindings(bindings: dict[str, dict[str, Any]]) -> None:
    _write_json(bindings_path(), {"version": 1, "bindings": bindings})


def _binding_entry(bindings: dict[str, dict[str, Any]], agent_id: str) -> dict[str, Any]:
    entry = bindings.get(agent_id)
    if not isinstance(entry, dict):
        entry = {"task_ids": [], "seen_artifacts": {}}
    task_ids = [t for t in entry.get("task_ids", []) if isinstance(t, str)]
    seen = entry.get("seen_artifacts")
    seen_map: dict[str, list[str]] = {}
    if isinstance(seen, dict):
        for art_id, tasks in seen.items():
            if isinstance(art_id, str) and isinstance(tasks, list):
                seen_map[art_id] = [t for t in tasks if isinstance(t, str)]
    return {"task_ids": task_ids, "seen_artifacts": seen_map}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def hash_token(raw: str) -> str:
    """SHA-256 hex digest of a raw token — the only value stored at rest."""
    if not isinstance(raw, str) or not raw:
        raise ValueError("raw token must be a non-empty string")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify(raw: str) -> dict[str, Any] | None:
    """Resolve a raw agent token to its entry — ``None`` for anything else.

    ``None`` covers: unknown token, malformed input, and revoked agents
    (revoked == invalid, never resurrected). The primary/legacy bearer
    never lives in this store and therefore never verifies against it.
    """
    if not isinstance(raw, str) or not raw:
        return None
    digest = hash_token(raw.strip())
    for entry in load_agents():
        if entry.get("revoked"):
            continue
        if isinstance(entry.get("token_sha256"), str) and entry["token_sha256"] == digest:
            return entry
    return None


def create(name: str) -> tuple[str, str]:
    """Mint one agent token → ``(agent_id, raw_token)``.

    ``raw_token`` is returned EXACTLY ONCE — the caller renders it in the
    show-once banner and then only the SHA-256 hash is kept anywhere.
    """
    clean = str(name or "").strip()
    if not clean:
        raise ValueError("agent name must be a non-empty string")
    raw_token = _TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
    entry = {
        "id": _new_agent_id(),
        "name": clean,
        "token_sha256": hash_token(raw_token),
        "created_at": _utcnow(),
        "last_used": None,
        "caps_manage": False,
        "revoked": False,
    }
    agents = [a for a in load_agents()]
    agents.append(entry)
    _save_agents(agents)
    return entry["id"], raw_token


def revoke(agent_id: str) -> dict[str, Any] | None:
    """Mark one agent revoked (verify() stops accepting it). Returns the entry."""
    agents = load_agents()
    for entry in agents:
        if entry.get("id") == agent_id:
            entry["revoked"] = True
            _save_agents(agents)
            return entry
    return None


def set_caps_manage(agent_id: str, enabled: bool) -> dict[str, Any] | None:
    """Toggle an agent's ``caps_manage`` flag (T-035 P2 capability gate)."""
    agents = load_agents()
    for entry in agents:
        if entry.get("id") == agent_id:
            entry["caps_manage"] = bool(enabled)
            _save_agents(agents)
            return entry
    return None


def touch(agent_id: str) -> bool:
    """Update ``last_used`` for one agent (best-effort, never fatal)."""
    agents = load_agents()
    for entry in agents:
        if entry.get("id") == agent_id:
            entry["last_used"] = _utcnow()
            _save_agents(agents)
            return True
    return False


def record_task(agent_id: str, task_id: str) -> bool:
    """Bind a submitted task to its agent (never for the primary path)."""
    if agent_id == _PRIMARY or not agent_id or not task_id:
        return False
    bindings = load_bindings()
    entry = _binding_entry(bindings, agent_id)
    if task_id not in entry["task_ids"]:
        entry["task_ids"].append(task_id)
    bindings[agent_id] = entry
    _save_bindings(bindings)
    return True


def record_artifacts(agent_id: str, task_id: str, artifact_ids: list[str]) -> bool:
    """Record the artifacts an agent HAS SEEN via one task (scoping view)."""
    if agent_id == _PRIMARY or not agent_id or not task_id:
        return False
    clean = [a for a in (artifact_ids or []) if isinstance(a, str) and a]
    if not clean:
        return False  # nothing seen — no binding entry at all
    bindings = load_bindings()
    entry = _binding_entry(bindings, agent_id)
    for art_id in clean:
        tasks = entry["seen_artifacts"].get(art_id)
        if not isinstance(tasks, list) or task_id not in tasks:
            entry["seen_artifacts"][art_id] = [
                *entry["seen_artifacts"].get(art_id, []), task_id]
    bindings[agent_id] = entry
    _save_bindings(bindings)
    return True


def tasks_for(agent_id: str) -> list[str]:
    """Tasks bound to one agent (empty for primary — primary has no view)."""
    if agent_id == _PRIMARY:
        return []
    entry = load_bindings().get(agent_id, {})
    task_ids = entry.get("task_ids") if isinstance(entry, dict) else []
    return [t for t in (task_ids or []) if isinstance(t, str)]


def can_read_task(agent_id: str, task_id: str) -> bool:
    """May this identity read one task?

    Primary/legacy: full access — the operator bearer predates binding and
    legacy tasks must stay readable (DEC: "Migration without a break").
    An agent may read exactly its own bound tasks; anything else is False
    (the MCP layer then serves the T-227/T-005g not_found semantics so
    foreign tasks stay indistinguishable from unknown ones).
    """
    if agent_id == _PRIMARY:
        return True
    if not agent_id or not task_id:
        return False
    return task_id in tasks_for(agent_id)


def can_read_artifact(agent_id: str, artifact_id: str) -> bool:
    """May this identity read one artifact?

    Primary: full access. An agent: only artifacts it has seen (recorded
    by a wrapped ``task_result``, T-035 P2) — fail-closed otherwise.
    """
    if agent_id == _PRIMARY:
        return True
    if not agent_id or not artifact_id:
        return False
    entry = load_bindings().get(agent_id, {})
    seen = entry.get("seen_artifacts") if isinstance(entry, dict) else {}
    return isinstance(seen, dict) and artifact_id in seen
