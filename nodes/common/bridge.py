"""T-003: generic bridge handler — pull the provider manifest and forward tasks.

Implements the bridge-provider contract v1 (SPEC §3–§5, board
``iowap-windows-stack``). The node polls ``GET /v1/manifest`` at the
configured ``bridge.url`` (the ``bridge:`` block of the active
``node.yaml``) and merges the reported capabilities into the local
capability registry under a ``host.`` prefix. Publishing is GATED:
default-off (DEC 2026-11-20) — without ``auto_publish`` or an explicit
allowlist entry, provider caps are detected and logged but never
published to the relay.

Design decision (T-003): the bridge is its own poller thread NEXT TO the
heartbeat thread, NOT an extension of it. The heartbeat cadence is the
relay's property, the manifest cadence is the bridge's — coupling them
would change relay-visible timing for every existing node. The merge
happens through a capability-provider hook installed in
``node_config.load_active_profile`` so both daemons (heartbeat, claim
loops, backfill sweeps, status snapshot) see the ``host.`` caps without
any change to their loop code; claimed ``host.`` stages are routed to
:func:`execute_bridge_task` by a single guard in each daemon's
``_run_stage``.

Error semantics (SPEC §5): terminal errors complete the stage with an
error result (the relay's retry budget then fails the task);
retryable errors raise :class:`BridgeRetryableError` — the daemon logs
and leaves the stage uncompleted, so the claim expires and the relay
requeues it (identical to a local handler crash).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from nodes.common import node_config

log = logging.getLogger("bridge")

BRIDGE_PREFIX = "host."
# SPEC §3: provider capability names arrive WITHOUT the prefix.
_CAP_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
# Default per-exec budget (SPEC §5 example request); the manifest carries
# no timeout of its own. Long-run caps inherit this as their stage budget.
DEFAULT_BRIDGE_TIMEOUT = 3600
# Poll cadence when no bridge config exists yet (config-appearance latency).
_IDLE_POLL_INTERVAL = 5
# Consecutive manifest-poll failures before every host.* cap is unpublished.
_MAX_POLL_ERRORS = 3

_MANIFEST_TIMEOUT = 10.0


class BridgeRetryableError(RuntimeError):
    """A retryable bridge failure — the stage is left to expire and requeue."""


# ---------------------------------------------------------------------------
# Configuration (node.yaml ``bridge:`` block, SPEC §4)
# ---------------------------------------------------------------------------


def load_bridge_config() -> dict[str, Any] | None:
    """Read and normalize the ``bridge:`` block of the active node.yaml.

    Returns ``None`` when the node has no bridge configured (no active
    file, no block, or no ``url``). Malformed fields degrade to their
    defaults with a warning — the poller must never die on YAML content
    (same discipline as ``load_active_budget``).
    """
    path = node_config.ACTIVE_PATH
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(parsed, dict):
        return None
    block = parsed.get("bridge")
    if not isinstance(block, dict):
        return None
    url = block.get("url")
    if not isinstance(url, str) or not url.strip():
        log.warning("bridge block present but url missing/invalid — bridge disabled")
        return None
    token = block.get("token")
    if not isinstance(token, str) or not token.strip():
        # SPEC §2: the token is injected as IOWAP_BRIDGE_TOKEN by the
        # container creator; the config value (if any) wins.
        token = os.environ.get("IOWAP_BRIDGE_TOKEN", "")
    workspace = block.get("workspace")
    if not isinstance(workspace, str) or not workspace.strip():
        workspace = "/workspace"
    auto_publish = block.get("auto_publish", False)
    if not isinstance(auto_publish, bool):
        log.warning("bridge.auto_publish must be a boolean — using default (false)")
        auto_publish = False
    allowlist = block.get("allowlist", [])
    if not isinstance(allowlist, list) or not all(isinstance(e, str) for e in allowlist):
        log.warning("bridge.allowlist must be a list of strings — using default ([])")
        allowlist = []
    poll_interval = block.get("poll_interval", 60)
    if not isinstance(poll_interval, int) or isinstance(poll_interval, bool) or poll_interval < 5:
        log.warning("bridge.poll_interval must be an integer >= 5 — using 60")
        poll_interval = 60
    return {
        "url": url.rstrip("/"),
        "token": token,
        "workspace": workspace,
        "auto_publish": auto_publish,
        "allowlist": allowlist,
        "poll_interval": poll_interval,
    }


# ---------------------------------------------------------------------------
# Manifest (SPEC §3)
# ---------------------------------------------------------------------------


def fetch_manifest(url: str, token: str, timeout: float = _MANIFEST_TIMEOUT) -> dict[str, Any]:
    """GET {url}/v1/manifest with Bearer auth; returns the parsed body."""
    r = httpx.get(
        f"{url}/v1/manifest",
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise TypeError("bridge manifest is not a JSON object")
    return data


def build_bridge_cap(name: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize ONE manifest capability into a registry cap dict.

    The cap rides the normal heartbeat/claim machinery: claimable,
    auto_publish already gated (the bridge only publishes allowed caps),
    and the ``bridge`` marker routes claimed stages to
    :func:`execute_bridge_task` instead of the local handler runner.
    ``input_schema`` passes through 1:1 (client-mcp T-009 convention —
    the node never re-validates).
    """
    cap: dict[str, Any] = {
        "name": f"{BRIDGE_PREFIX}{name}",
        "version": "1.0.0",
        "description": raw.get("summary") if isinstance(raw.get("summary"), str) else None,
        "auto_publish": True,
        "claimable": True,
        "handler": "",  # never executed locally
        "max_parallel": 1,
        "cost": 1,
        "timeout": DEFAULT_BRIDGE_TIMEOUT,
        "long_run": bool(raw.get("long_run")),
        "bridge": True,
        "bridge_name": name,
    }
    if isinstance(raw.get("input_schema"), dict):
        cap["input_schema"] = raw["input_schema"]
    return cap


def _publish_allowed(name: str, cfg: dict[str, Any]) -> bool:
    """The T-003 publish gate (DEC 2026-11-20, default-off).

    A cap publishes when the allowlist names it (or contains ``"*"``),
    or when ``auto_publish`` is true AND the allowlist is empty.
    ``auto_publish: true`` with a non-empty allowlist publishes ONLY the
    allowlisted names — the explicit list always wins.
    """
    allowlist = cfg["allowlist"]
    if "*" in allowlist or name in allowlist:
        return True
    return bool(cfg["auto_publish"]) and not allowlist


class BridgeManager:
    """Holds the published ``host.`` caps and the poll-failure streak."""

    def __init__(self) -> None:
        self.published: dict[str, dict[str, Any]] = {}
        self.consecutive_errors = 0
        self._lock = threading.Lock()

    # -- unpublish helpers -------------------------------------------------

    def _unpublish_all(self, reason: str) -> None:
        with self._lock:
            if not self.published:
                return
            log.warning("unpublishing all %d host.* caps: %s", len(self.published), reason)
            self.published = {}

    # -- polling ------------------------------------------------------------

    def poll_once(self) -> None:
        """One manifest pull + registry reconciliation. Never raises."""
        cfg = load_bridge_config()
        if cfg is None:
            # Bridge removed from the config → drop any published caps.
            self._unpublish_all("bridge config removed")
            with self._lock:
                self.consecutive_errors = 0
            return
        try:
            manifest = fetch_manifest(cfg["url"], cfg["token"])
        except Exception as exc:  # noqa: BLE001 — the poller must survive
            with self._lock:
                self.consecutive_errors += 1
                errors = self.consecutive_errors
            if errors >= _MAX_POLL_ERRORS:
                self._unpublish_all(f"{errors} consecutive poll errors (last: {exc})")
            else:
                log.warning(
                    "bridge manifest poll failed (%d/%d): %s", errors, _MAX_POLL_ERRORS, exc
                )
            return
        self._apply_manifest(cfg, manifest)

    def _apply_manifest(self, cfg: dict[str, Any], manifest: dict[str, Any]) -> None:
        version = manifest.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or int(version) != 1:
            # SPEC §3: never best-effort-parse across major boundaries.
            log.warning(
                "bridge manifest version %r unsupported (need major 1) — manifest ignored",
                version,
            )
            return
        with self._lock:
            self.consecutive_errors = 0

        base_names = {c["name"] for c in node_config.load_active_profile_unmerged()}
        new_published: dict[str, dict[str, Any]] = {}
        gated: list[str] = []
        skipped: list[str] = []
        for raw in manifest.get("capabilities") or []:
            if not isinstance(raw, dict):
                continue
            name = raw.get("name")
            if not isinstance(name, str) or not _CAP_NAME_RE.match(name):
                log.warning("bridge cap with invalid name %r skipped", name)
                skipped.append(str(name))
                continue
            health = raw.get("health", "ok")
            if health == "down":
                # SPEC §3: down → not published / unpublished.
                log.info("bridge cap %s is down — not published", name)
                continue
            if not _publish_allowed(name, cfg):
                gated.append(name)
                continue
            prefixed = f"{BRIDGE_PREFIX}{name}"
            if prefixed in base_names:
                # Namespace rule: a local capability keeps the name.
                log.warning(
                    "bridge cap %s skipped — collision with existing capability '%s'",
                    name,
                    prefixed,
                )
                skipped.append(name)
                continue
            new_published[prefixed] = build_bridge_cap(name, raw)

        with self._lock:
            old = set(self.published)
            new = set(new_published)
            added = sorted(new - old)
            removed = sorted(old - new)
            self.published = new_published
        if added:
            log.info("bridge: published host caps: %s", ", ".join(added))
        if removed:
            log.info("bridge: unpublished host caps: %s", ", ".join(removed))
        log.debug(
            "bridge manifest: %d published, %d gate-blocked, %d skipped",
            len(new_published),
            len(gated),
            len(skipped),
        )
        if gated:
            log.info(
                "bridge: %d provider cap(s) detected but not published "
                "(auto_publish off, not allowlisted): %s",
                len(gated),
                ", ".join(sorted(gated)),
            )


# ---------------------------------------------------------------------------
# Registry singleton + node_config merge hook
# ---------------------------------------------------------------------------

_manager: BridgeManager | None = None
_manager_lock = threading.Lock()


def get_manager() -> BridgeManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = BridgeManager()
        return _manager


def reset_manager() -> None:
    """Test helper: drop the singleton (the next get_manager() is fresh)."""
    global _manager
    with _manager_lock:
        _manager = None


def published_caps() -> list[dict[str, Any]]:
    """The currently published bridge caps (node_config merge provider)."""
    return list(get_manager().published.values())


def _provider() -> list[dict[str, Any]]:
    try:
        return published_caps()
    except Exception:
        log.exception("bridge caps provider failed")
        return []


node_config.register_bridge_caps_provider(_provider)


# ---------------------------------------------------------------------------
# Poller thread
# ---------------------------------------------------------------------------


def _poll_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        cfg = load_bridge_config()
        try:
            get_manager().poll_once()
        except Exception:
            log.exception("bridge poll crashed")
        interval = cfg["poll_interval"] if cfg else _IDLE_POLL_INTERVAL
        for _ in range(max(1, int(interval))):
            if stop_event.is_set():
                return
            time.sleep(1)


def start_poller(stop_event: threading.Event) -> threading.Thread:
    """Start the manifest poller thread (daemon thread, stops with the daemon)."""
    thread = threading.Thread(
        target=_poll_loop, args=(stop_event,), daemon=True, name="bridge-poll"
    )
    thread.start()
    log.info("bridge manifest poller started")
    return thread


# ---------------------------------------------------------------------------
# Exec forward (SPEC §5)
# ---------------------------------------------------------------------------


def _resolve_artifact_path(workspace: str, rel: Any) -> Path | None:
    """Resolve a workspace-relative artifact path, rejecting traversal."""
    if not isinstance(rel, str) or not rel.strip():
        return None
    try:
        ws = Path(workspace).resolve()
        candidate = (ws / rel).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(ws):
        return None
    return candidate


def _upload_artifacts(
    client: Any,
    cfg: dict[str, Any],
    artifacts: Any,
    task_id: str,
    stage_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Upload workspace artifacts via the existing artifact API.

    Returns (uploaded_responses, errors). A missing/unresolvable file is
    an error entry, never a task failure — the provider may stream the
    file after answering.
    """
    uploaded: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if not isinstance(artifacts, list):
        return uploaded, errors
    for entry in artifacts:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        path = _resolve_artifact_path(cfg["workspace"], entry.get("path"))
        if path is None or not path.is_file():
            errors.append({"name": name, "error": "artifact file not found in workspace"})
            log.warning(
                "bridge artifact %r not found under workspace %s",
                entry.get("path"),
                cfg["workspace"],
            )
            continue
        try:
            resp = client.upload_artifact(
                path,
                name=name if isinstance(name, str) and name else path.name,
                task_id=task_id or None,
                stage_id=stage_id or None,
            )
            uploaded.append(resp)
        except Exception as exc:  # noqa: BLE001 — best-effort upload
            errors.append({"name": name, "error": str(exc)})
            log.warning("bridge artifact upload failed for %s: %s", name, exc)
    return uploaded, errors


def execute_bridge_task(
    client: Any,
    cap: dict[str, Any],
    stage: dict[str, Any],
) -> dict[str, Any]:
    """Forward a claimed ``host.*`` stage to the bridge provider.

    Returns a Response-Envelope-shaped result for
    ``RelayClient.complete`` on SUCCESS and on TERMINAL failure.
    Retryable failures raise :class:`BridgeRetryableError` — the caller
    (daemon ``_run_stage``) logs and leaves the stage uncompleted so the
    relay's claim expiry requeues it.
    """
    cfg = load_bridge_config()
    if cfg is None or not cfg.get("url"):
        return {"status": "error", "error": "bridge: no bridge configuration on this node"}
    name = cap.get("bridge_name") or str(cap.get("name", "")).removeprefix(BRIDGE_PREFIX)
    task_id = str(stage.get("task_id") or "")
    stage_id = str(stage.get("stage_id") or "")
    params = stage.get("payload")
    if not isinstance(params, dict):
        params = {} if params is None else {"payload": params}
    timeout_s = int(cap.get("timeout", DEFAULT_BRIDGE_TIMEOUT) or DEFAULT_BRIDGE_TIMEOUT)
    body = {"task_id": task_id, "params": params, "timeout_s": timeout_s}
    url = f"{cfg['url']}/v1/exec/{name}"
    try:
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {cfg['token']}"},
            json=body,
            # HTTP budget = the provider's exec budget + a small grace.
            timeout=httpx.Timeout(timeout_s + 30.0),
        )
    except httpx.HTTPError as exc:
        raise BridgeRetryableError(f"bridge exec transport error ({name}): {exc}") from exc

    if r.status_code == 200:
        try:
            data = r.json()
        except ValueError as exc:
            raise BridgeRetryableError(f"bridge exec returned non-JSON 200 ({name})") from exc
        if not isinstance(data, dict):
            raise BridgeRetryableError(f"bridge exec returned a non-object 200 body ({name})")
        status = data.get("status")
        if status == "completed":
            result = data.get("result")
            if not isinstance(result, dict):
                result = {} if result is None else {"value": result}
            # Echo the artifacts key whenever the provider sent one —
            # even empty (the key's presence is part of the contract).
            had_artifacts = isinstance(data.get("artifacts"), list)
            uploaded, errors = _upload_artifacts(
                client, cfg, data.get("artifacts"), task_id, stage_id
            )
            if had_artifacts:
                result = {**result, "artifacts": uploaded}
            if errors:
                result = {**result, "artifact_errors": errors}
            return {"status": "completed", "result": result}
        if status == "failed":
            err = data.get("error")
            err = err if isinstance(err, dict) else {}
            code = err.get("code", "capability.failed")
            message = err.get("message", "")
            if err.get("retryable"):
                raise BridgeRetryableError(f"bridge exec {name}: {code}: {message}")
            # SPEC §5 terminal: capability.failed / invalid_params.
            return {"status": "error", "error": f"bridge {code}: {message}"}
        raise BridgeRetryableError(f"bridge exec returned unknown status {status!r} ({name})")

    if r.status_code == 404:
        return {
            "status": "error",
            "error": f"bridge: capability '{name}' unknown at the provider (404)",
        }
    if r.status_code in (401, 403):
        return {
            "status": "error",
            "error": f"bridge auth failed ({r.status_code}) — check the bridge token",
        }
    if r.status_code == 503:
        raise BridgeRetryableError(f"bridge busy (503 provider.busy) for {name}")
    if 500 <= r.status_code < 600:
        raise BridgeRetryableError(f"bridge exec server error {r.status_code} for {name}")
    return {"status": "error", "error": f"bridge exec failed with http {r.status_code} for {name}"}
