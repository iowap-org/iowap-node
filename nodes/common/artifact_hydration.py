"""T-051 (T-035 P3): exec wiring — input-artifact hydration + result-push binding.

Brief: ``references/t051-exec-wiring-brief.md`` (board iowap-windows-stack).
Two T-035 gaps are wired here, node-side — the relay and the bridge exec
contract stay untouched:

1. **Input hydration, BEFORE exec** — stage payloads may carry artifact
   references in the T-179 envelope format (``__iowap_ref__``,
   ``nodes/common/envelope.py``, Phase-1 FROZEN; ``src: artifact`` +
   ``artifact_id``). Until P3 the exec (bridge daemon AND local handler)
   never saw the bytes — only the raw ID. This module walks the params
   tree, downloads each referenced artifact via
   ``relay_client.download_artifact`` (NODE credentials) into
   ``<workspace>/inputs/<artifact_id>/<name>``, and rewrites the payload
   field to the P3 hydration convention ``{"src": "path",
   "path": "inputs/<artifact_id>/<name>"}`` (workspace-relative, modeled
   after T-179's ``src`` field). The exec contract v1.1 body is
   unchanged — the provider/handler sees a path, never an ID.

2. **Result-push → binding** — the completion sites (poll daemon
   ``_run_stage``, SSE daemon ``_run_stage``, T-179 script-complete
   branch — exactly where ``budget_meter`` meters) call
   :func:`record_result_artifacts`: artifact ids echoed by the pushed
   result are recorded into the T-035 binding store via
   ``agent_tokens.record_artifacts`` for the bound submitter, so the
   owning agent may reference them from completion ON — without a prior
   ``task_result`` fetch. This is the chained-files win: task1 pushes →
   record at completion → task2 references task1's artifact → hydration
   allows it.

Ownership (node-owned enforcement, T-035 rule): hydration happens ONLY
when the TASK SUBMITTER owns the artifact. The submitter is resolved
from the binding store (``agent_tokens.submitting_agent`` —
``task_submit`` binds at MCP time); an unbound task is a primary/legacy
task → full access (the operator bearer predates binding and is never
recorded). An agent submitter may reference only artifacts it has SEEN
(recorded at completion — this module — or at the ``task_result`` fetch,
T-035 P2 ``mcp/binding.py``); anything else FAILS the stage at claim
time with the honest cause ``artifact '<id>' not available for this
submitter`` (deliberately NOT a ``not_found`` shape: the submitter knows
the id from its own context — the refusal must say why, not lie).

Concurrency: ``bindings.json`` is written by the MCP process (submit/
result wrappers) AND the node daemons (completion records, here).
Store writes run read-modify-write under the process-level
``agent_tokens._BINDINGS_LOCK`` plus atomic tmp+os.replace — the
deliberate budget.py variant (T-046): cross-process remains
last-writer-wins exactly like ``budget_used`` (documented; writes are a
few entries large, never corrupt under the atomic replace). No
fcntl/flock was added — the budget pattern is the established one for
this node's multi-writer JSON stores and the record is best-effort by
design (a lost record costs one hydration refusal, never integrity).

Never raises: hydration failures return the error message, result
recording swallows store failures (log + degrade) — the completion path
must not die on bookkeeping (same discipline as the T-046 meter).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from nodes.common import agent_tokens
from nodes.common import envelope as envelope_mod

logger = logging.getLogger("artifact-hydration")

# Workspace-relative hydration convention (brief): where hydrated bytes
# land, and the rewritten payload-field shape the exec receives.
INPUTS_DIR = "inputs"
_HYDRATED_SRC = "path"

# Sanitized artifact filename (T-179: ``filename`` is a display name,
# never a trustworthy path — receivers sanitize). Only safe path atoms
# survive; anything else maps to the artifact id.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_NAME_LEN = 120


def _safe_artifact_name(filename: Any, artifact_id: str) -> str:
    """Sanitize an envelope ``filename`` into a safe file atom.

    The raw display name may carry separators/traversal (``../etc/pwd``,
    ``C:\\...``) — strip everything outside ``[A-Za-z0-9._-]``, clamp the
    length, and fall back to the artifact id when nothing survives.
    """
    raw = str(filename or "").replace("\\", "_")
    cleaned = _SAFE_NAME_RE.sub("_", raw).strip("._")
    cleaned = cleaned[:_MAX_NAME_LEN].strip("._")
    if not cleaned or cleaned in (".", ".."):
        return artifact_id
    return cleaned


def resolve_workspace() -> str:
    """The workspace hydrated inputs land in (the bridge one, or its default).

    The bridge ``workspace`` (node.yaml ``bridge.workspace``) is the
    node's one shared exec workspace — local handler stages hydrate into
    the same tree. Fresh read per call, mirroring the bridge config
    discipline (no cache — the dashboard edits it live).
    """
    from nodes.common import bridge  # local import: bridge imports this module

    cfg = bridge.load_bridge_config()
    if cfg and cfg.get("workspace"):
        return str(cfg["workspace"])
    # Same default the bridge config loader applies (SPEC §4).
    return "/workspace"


def collect_artifact_ids(result: Any) -> list[str]:
    """Collect artifact ids a pushed result envelope says were pushed.

    Mirror of the MCP-side walker (``mcp/iowap_node_mcp/binding.py
    extract_artifact_ids``, T-035 P2 — deliberately NOT imported per the
    T-037 split rule): bridge-capability stage results carry the upload
    echoes under an ``artifacts`` key (``[{"artifact_id": ...}, ...]`` —
    ``nodes/common/bridge.py``). The walk stays defensive: any dict with
    a non-empty str ``artifact_id`` anywhere in the envelope, task-level
    and stage-level shapes, exactly-once ids, unknown shapes ignored.
    """
    ids: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("artifact_id"), str) and node["artifact_id"]:
                ids.append(node["artifact_id"])
            for value in node.values():
                if isinstance(value, (dict, list)):
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(result)
    seen: set[str] = set()
    ordered = [eid for eid in ids if not (eid in seen or seen.add(eid))]
    return ordered


def record_result_artifacts(result: Any, task_id: str) -> list[str]:
    """Bind pushed artifacts to the completed task's submitter — best effort.

    The THREE completion sites (poll ``_run_stage`` success, SSE
    ``_run_stage`` success, T-179 script-complete) call this with the
    result envelope and the task id. No artifacts, no bound submitter
    (primary/legacy path) or a failed store write: no-op with a log —
    the completion path never raises here (the T-046 meter discipline).

    Returns the ids that were recorded (empty for every no-op).
    """
    ids = collect_artifact_ids(result)
    if not ids:
        return []
    submitter = agent_tokens.submitting_agent(task_id or "")
    if submitter is None:
        # Primary/legacy task: the operator path writes no binding ever
        # (T-035 rule) — the record is deliberately skipped.
        logger.debug(
            "result carries %d artifact(s) but task %s has no agent "
            "binding (primary/legacy) — no record",
            len(ids),
            task_id or "?",
        )
        return []
    try:
        ok = agent_tokens.record_artifacts(submitter, task_id or "", ids)
    except Exception as exc:  # noqa: BLE001 — bookkeeping must not kill completion
        logger.warning(
            "record_artifacts(%s, %s, %s) failed: %s", submitter, task_id, ids, exc
        )
        return []
    if not ok:
        logger.debug("record_artifacts(%s, %s) wrote nothing", submitter, task_id)
    logger.info(
        "completion record: artifact(s) %s bound to submitter %s via task %s",
        ids,
        submitter,
        task_id or "?",
    )
    return ids


def hydrate_stage_params(
    client: Any,
    params: Any,
    task_id: str,
    workspace: str,
) -> tuple[Any, str | None]:
    """Download artifact refs in the params tree and rewrite them to paths.

    Walks ``params`` for T-179 envelope fields (``__iowap_ref__``).
    Only ``src: artifact`` fields are hydrated; ``inline`` envelopes
    carry their bytes already and pass through untouched, ``bridge`` refs
    are out of scope (documented). For each artifact ref:

    * the binding guard runs (brief): the task submitter must own the
      artifact — primary/legacy tasks bypass (no binding — operator
      path), an unowned ref refuses with the honest cause;
    * the bytes download via ``client.download_artifact`` (NODE
      credentials) into ``<workspace>/inputs/<artifact_id>/<name>``;
    * the field is rewritten in a copy to ``{"src": "path",
      "path": "inputs/<artifact_id>/<name>"}``.

    Returns ``(params, None)`` — the ORIGINAL object when the tree held
    no artifact ref at all (no-copy no-op), a rewritten copy otherwise —
    or ``(params, error)`` with ``error`` an honest, terminal cause:
    ownership refusal, malformed envelope, or a failed download. The
    exec must not start with half-hydrated refs (fail-fast at claim).
    """
    submitter = agent_tokens.submitting_agent(task_id or "")
    ws_root = Path(workspace) if workspace else Path("/")
    state: dict[str, str | None] = {"error": None}
    hydrated: list[tuple[str, str]] = []  # (artifact_id, workspace-relative path)

    def _guard_and_download(artifact_id: str, ref: dict[str, Any]) -> dict[str, Any]:
        """Guard + download ONE artifact ref → the rewritten field value."""
        artifact_id = str(artifact_id or "")
        # Binding guard (brief): primary/legacy tasks bypass (the operator
        # bearer never binds); agent submitters fail CLOSED.
        if submitter is not None and not agent_tokens.can_read_artifact(
            submitter, artifact_id
        ):
            state["error"] = f"artifact '{artifact_id}' not available for this submitter"
            return dict(ref)
        path_art = _safe_artifact_name(artifact_id, artifact_id)
        name = _safe_artifact_name(ref.get("filename"), path_art)
        rel = f"{INPUTS_DIR}/{path_art}/{name}"
        target = ws_root / INPUTS_DIR / path_art / name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            client.download_artifact(artifact_id, target)
        except Exception as exc:  # noqa: BLE001 — terminal with the honest cause
            state["error"] = f"artifact '{artifact_id}' download failed: {exc}"
            return dict(ref)
        hydrated.append((artifact_id, rel))
        # P3 hydration convention (brief, modeled after T-179's src field):
        # the envelope field is REPLACED by the workspace-relative path —
        # the provider/handler never sees an artifact id.
        return {"src": _HYDRATED_SRC, "path": rel}

    def _rewrite(node: Any) -> Any:
        if state["error"] is not None:
            return node
        if isinstance(node, dict):
            if envelope_mod.is_envelope(node):
                try:
                    src, raw = envelope_mod.parse_envelope(node)
                except envelope_mod.EnvelopeError as exc:
                    # T-179 rule: an unsupported envelope breaks the receiver
                    # loudly — never a silent passthrough into the exec.
                    state["error"] = f"artifact ref invalid: {exc}"
                    return node
                if src != "artifact":
                    return node  # inline carries bytes, bridge refs out of scope
                return _guard_and_download(raw, node[envelope_mod.ENVELOPE_KEY])
            return {key: _rewrite(value) for key, value in node.items()}
        if isinstance(node, list):
            return [_rewrite(item) for item in node]
        return node

    rewritten = _rewrite(params)
    if state["error"] is not None:
        logger.warning(
            "hydration refused for task %s: %s", task_id or "?", state["error"]
        )
        return params, state["error"]
    if hydrated:
        logger.info(
            "hydration for task %s: %d artifact ref(s) → %s",
            task_id or "?",
            len(hydrated),
            [f"{art} → {rel}" for art, rel in hydrated],
        )
    else:
        # envelopes existed but only non-artifact sources — params pass
        # through (rewritten copy without changes, downloads never ran).
        logger.debug("hydration for task %s: no artifact refs to hydrate", task_id or "?")
    return rewritten, None
