"""Shared Relay API client.

The ``RelayClient`` here is the common HTTP client that both the
``node-cli`` Swiss-army-knife command and the ``node-daemon`` realtime
daemon use to talk to an AI Relay. Extracted out of ``node_cli.py`` so
the daemon no longer depends on the whole CLI monolith (T-112).

Also carries the small helpers that built the client's config/logging:
``_setup_logging``, ``_effective_config``, ``_base_url`` and
``_filename_from_response``.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from nodes.common.node_config import load_active_status
from nodes.common.node_utils import load_config, load_token, save_token

log = logging.getLogger("relay-client")

def _setup_logging(level: str | None = None) -> None:
    if level is None:
        level = os.environ.get("RELAY_LOG_LEVEL", "INFO")
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s | %(levelname)-7s | node-cli | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _utcnow_str() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Config helpers (env-var aware)
# ---------------------------------------------------------------------------

def _effective_config() -> dict[str, Any]:
    """Return relay_config.json merged with env-var overrides."""
    cfg = load_config()
    base_url = os.environ.get("RELAY_BASE_URL")
    if base_url:
        cfg["base_url"] = base_url
    hb = os.environ.get("RELAY_HEARTBEAT_INTERVAL")
    if hb is not None:
        try:
            cfg["heartbeat_interval"] = int(hb)
        except ValueError:
            log.warning("ignoring invalid RELAY_HEARTBEAT_INTERVAL=%r", hb)
    ci = os.environ.get("RELAY_CLAIM_INTERVAL")
    if ci is not None:
        try:
            cfg["claim_interval"] = int(ci)
        except ValueError:
            log.warning("ignoring invalid RELAY_CLAIM_INTERVAL=%r", ci)
    mr = os.environ.get("RELAY_MAX_RETRIES")
    if mr is not None:
        try:
            cfg["max_retries"] = int(mr)
        except ValueError:
            log.warning("ignoring invalid RELAY_MAX_RETRIES=%r", mr)
    return cfg


def _base_url(meta: dict[str, Any], cfg: dict[str, Any]) -> str:
    url = cfg.get("base_url") or meta.get("base_url")
    if not url:
        # T-152: mDNS fallback — discover the relay on the local network
        # when no base_url is configured. The relay advertises itself as
        # `AI Relay Service._http._tcp.local.` (see core/zeroconf.py).
        discovered = _discover_relay_mdns()
        if discovered:
            log.info("mDNS: discovered relay at %s", discovered)
            url = discovered
    if not url:
        raise SystemExit(
            "no base_url configured (set base_url in relay_config.json, RELAY_BASE_URL, "
            "or let the node discover the relay via mDNS)"
        )
    return url.rstrip("/")


def _discover_relay_mdns(timeout: float = 2.0) -> str | None:
    """Discover the relay via mDNS on the local network.

    Returns the relay base URL (e.g. ``http://192.168.1.50:8788``) or ``None``
    when no relay is found. Uses the ``zeroconf`` package (already a project
    dependency). The relay advertises ``AI Relay Service._http._tcp.local.``
    with a ``path`` property (default ``/health``) and the port.
    """
    try:
        from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf  # noqa: PLC0415
    except ImportError:
        log.warning("mDNS discovery unavailable (zeroconf not installed)")
        return None

    found: dict[str, Any] = {}

    class _Listener:
        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name)
            if info:
                found["info"] = info

        def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

        def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

    zc = Zeroconf()
    try:
        listener = _Listener()
        browser = ServiceBrowser(zc, "_http._tcp.local.", listener)
        # Wait briefly for discovery.
        import time  # noqa: PLC0415

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and "info" not in found:
            time.sleep(0.1)
        browser.cancel()
    finally:
        zc.close()

    info = found.get("info")
    if not info:
        return None
    # Build the base URL from the discovered address + port.
    try:
        addr = info.parsed_addresses()[0] if info.parsed_addresses() else None
    except Exception:  # noqa: BLE001
        addr = None
    if not addr:
        return None
    port = info.port or 8788
    return f"http://{addr}:{port}"


# ---------------------------------------------------------------------------
# Auth helpers (thin wrappers around poller functions)
# ---------------------------------------------------------------------------

class RelayClient:
    """Minimal HTTP client wrapping heartbeat / claim / complete / submit.

    Encapsulates token handling so the CLI subcommands and the daemon
    can share a single implementation. On 401/403 it attempts a token
    refresh exactly once.
    """

    def __init__(self, meta: dict[str, Any], cfg: dict[str, Any]) -> None:
        self.meta = meta
        self.cfg = cfg
        self.base_url = _base_url(meta, cfg)
        # TLS (T-111): optional CA cert path for nodes connecting over https
        # to a relay using a private/self-signed CA. When set, httpx verifies
        # against it instead of the system trust store. Default True = system
        # trust store (public CA / Let's Encrypt).
        self._verify: "str | bool" = cfg.get("tls_ca_cert") or True
        data = load_token()
        self.token = data["token"] if data else None
        # T-088: track the token expiry so the daemon can refresh
        # proactively before it expires. ``None`` means unknown (e.g.
        # a migrated legacy token or a server that omits expires_at).
        self.token_expires_at: str | None = data.get("expires_at") if data else None
        # T-108: Auth-Failure-Streak für exponentiellen Backoff. Nach
        # wiederholten 401/403-Fehlschlägen erhöht der Daemon den
        # Heartbeat/Claim-Abstand, statt in einem engen Loop zu hämmern.
        self._auth_fail_streak = 0
        if not self.token:
            print(
                "no runtime token found, attempting recovery with registration secret",
                file=sys.stderr,
            )
            self.token = self._recover_runtime_token()
            if not self.token:
                raise SystemExit("no runtime token available and recovery failed")

    # -- T-108: backoff + self-healing -------------------------------------

    # Backoff begins after this many consecutive auth failures.
    _BACKOFF_THRESHOLD = 3
    # Base delay (seconds) once the threshold is reached.
    _BACKOFF_BASE = 10
    # Hard cap (seconds) so the backoff never grows unbounded.
    _BACKOFF_MAX = 300

    def _register_backoff_failure(self) -> None:
        """Record one consecutive auth failure (401/403)."""
        self._auth_fail_streak += 1

    def _register_backoff_success(self) -> None:
        """Reset the auth-failure streak after a successful auth/refresh."""
        self._auth_fail_streak = 0

    def _current_backoff(self) -> float:
        """Return the current auth-failure backoff in seconds (0 = none)."""
        if self._auth_fail_streak < self._BACKOFF_THRESHOLD:
            return 0.0
        # Exponential: 10s, 20s, 40s, 80s, 160s — capped at _BACKOFF_MAX.
        exp = min(self._auth_fail_streak - self._BACKOFF_THRESHOLD + 1, 5)
        return float(min(self._BACKOFF_BASE * (2 ** (exp - 1)), self._BACKOFF_MAX))

    def _reload_token_from_disk(self) -> None:
        """Re-read the token file. Helps when an external process (or a
        manual intervention) corrected the token after the daemon cached
        an invalid value. Reads only — overwrites nothing on disk.
        """
        data = load_token()
        if data and data.get("token"):
            self.token = data["token"]
            self.token_expires_at = data.get("expires_at")

    # -- low level ----------------------------------------------------------

    def _post(
        self, path: str, body: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> httpx.Response:
        return httpx.post(
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            json=body or {},
            timeout=timeout or self.cfg["request_timeout"],
            verify=self._verify,
        )

    def _get(
        self, path: str, *, timeout: float | None = None
    ) -> httpx.Response:
        return httpx.get(
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=timeout or self.cfg["request_timeout"],
            verify=self._verify,
        )

    def _get_with_retry(
        self, path: str, *, timeout: float | None = None
    ) -> httpx.Response:
        r = self._get(path, timeout=timeout)
        if r.status_code in (401, 403):
            log.warning("auth error %s on %s, refreshing token", r.status_code, path)
            if self._refresh_token():
                r = self._get(path, timeout=timeout)
        return r

    def _post_with_retry(
        self, path: str, body: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> httpx.Response:
        r = self._post(path, body, timeout=timeout)
        if r.status_code in (401, 403):
            log.warning("auth error %s on %s, refreshing token", r.status_code, path)
            if self._refresh_token():
                r = self._post(path, body, timeout=timeout)
        return r

    # -- token refresh -------------------------------------------------------

    def _refresh_token(self) -> bool:
        try:
            r = httpx.post(
                f"{self.base_url}/relay/v2/auth/refresh",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"requested_credential": "runtime_token"},
                timeout=self.cfg["request_timeout"],
                verify=self._verify,
            )
            if r.status_code == 200:
                data = r.json()
                new = data.get("token")
                expires_at = data.get("expires_at")
                if new:
                    save_token(new, expires_at=expires_at)
                    self.token = new
                    self.token_expires_at = expires_at
                    self._register_backoff_success()
                    return True
        except Exception as exc:
            log.warning("runtime-token refresh failed: %s", exc)
        recovered = self._recover_runtime_token()
        if recovered is not None:
            self._register_backoff_success()
            return True
        # Refresh + Recovery fehlgeschlagen: Datei neu lesen — ein externer
        # Prozess/manueller Eingriff könnte den Token inzwischen korrigiert
        # haben (T-108 Task 1). Backoff-Streak erhöhen, damit der Daemon
        # nicht in einem engen 401-Loop verharrt (T-108 Task 2).
        self._reload_token_from_disk()
        self._register_backoff_failure()
        return False

    def _recover_runtime_token(self) -> str | None:
        try:
            r = httpx.post(
                f"{self.base_url}/relay/v2/auth/refresh",
                json={
                    "node_id": self.meta["node_id"],
                    "requested_credential": "runtime_token",
                    "registration_secret": self.meta.get("registration_secret"),
                },
                timeout=self.cfg["request_timeout"],
                verify=self._verify,
            )
            r.raise_for_status()
            data = r.json()
            new = data.get("token")
            expires_at = data.get("expires_at")
            if new:
                save_token(new, expires_at=expires_at)
                self.token = new
                self.token_expires_at = expires_at
            return new
        except Exception as exc:
            log.error("registration-secret recovery failed: %s", exc)
            return None

    # -- proactive refresh (T-118) ------------------------------------------

    def maybe_refresh_token(self) -> None:
        """Proactively refresh the runtime token before it expires.

        Centralized here so both daemons (node-cli and node-daemon) share one
        implementation. Refreshes when the token expires within
        ``rt_refresh_before_seconds`` (default 24h). When ``expires_at`` is
        unknown (legacy plaintext token), refresh immediately — an unknown
        expiry is treated as risky rather than trusted.
        """
        margin = float(self.cfg.get("rt_refresh_before_seconds", 86400))
        if self.token_expires_at:
            try:
                exp = datetime.fromisoformat(self.token_expires_at)
                if exp - datetime.now(timezone.utc) < timedelta(seconds=margin):
                    log.info(
                        "token expires soon (%s), refreshing proactively",
                        self.token_expires_at,
                    )
                    self._refresh_token()
            except (ValueError, TypeError):
                # Malformed expires_at — fall through to the unknown-expiry
                # path below so a bad value can't silently kill the node.
                self._refresh_token()
        else:
            # No known expiry (legacy token): refresh to be safe.
            log.info("token expiry unknown, refreshing proactively")
            self._refresh_token()

    # -- public API ----------------------------------------------------------

    def heartbeat(self, caps: list[dict[str, Any]], in_flight: dict[str, int]) -> dict[str, Any]:
        try:
            load_avg = os.getloadavg()[0]
            cpu_count = os.cpu_count() or 1
            load_pct = (load_avg / cpu_count) * 100.0
        except (OSError, AttributeError):
            cpu_count = 1
            load_pct = 0.0
        load_cap = float(self.cfg.get("load_cap", cpu_count * 100.0))
        load = min(load_pct, load_cap)

        cap_status: list[dict[str, Any]] = []
        for cap in caps:
            if not cap.get("auto_publish", True):
                continue
            name = cap["name"]
            inflight = in_flight.get(name, 0)
            entry: dict[str, Any] = {
                "name": name,
                "version": cap.get("version", "1.0.0"),
                "available": inflight < cap.get("max_parallel", 1),
            }
            # T-053: forward capability metadata so the server can
            # populate node_capabilities.{description,input_schema}
            # and resolve capability_details on claim/task-view
            # without an extra discovery round-trip. Omit fields that
            # are absent or falsy to keep the heartbeat payload small.
            if cap.get("type"):
                entry["type"] = cap.get("type")
            if cap.get("description"):
                entry["description"] = cap.get("description")
            if cap.get("input_schema"):
                entry["input_schema"] = cap.get("input_schema")
            cap_status.append(entry)

        queue_depth = sum(in_flight.values())
        body: dict[str, Any] = {
            "node_id": self.meta["node_id"],
            "status": "online",
            "available": True,
            "load": load,
            "queue_depth": queue_depth,
            "capabilities": cap_status,
        }
        # T-072: forward node-level node_name + description from the
        # meta file (ai-relay-agent.json) so the server can store and
        # surface them via `node list` / `node info`.
        node_name = self.meta.get("node_name")
        if node_name:
            body["node_name"] = node_name
        description = self.meta.get("description")
        if description:
            body["description"] = description

        # T-081: forward the node's requested status (busy/idle) from the
        # active YAML profile. The value is written into the YAML by
        # `node-cli node busy`/`idle` and persists until explicitly
        # changed. When no explicit status is set we send "online" so
        # the server can transition the node from approved/offline to
        # online.
        requested_status = load_active_status()
        if requested_status:
            body["status"] = requested_status
        # T-081: forward the per-node load cap so the server can run
        # its auto-busy logic against the operator-configured ceiling
        # rather than a server-wide default.
        load_cap = self.cfg.get("load_cap")
        if load_cap is not None:
            body["load_cap"] = float(load_cap)

        # T-075: collect routes from all capabilities in the active profile.
        routes: list[dict[str, Any]] = []
        for cap in caps:
            cap_routes = cap.get("routes")
            if cap_routes and isinstance(cap_routes, list):
                routes.extend(cap_routes)
        if routes:
            body["routes"] = routes

        r = self._post_with_retry(
            "/relay/v2/discovery/heartbeat",
            body,
        )
        r.raise_for_status()
        return r.json()

    def claim(self, capability: str) -> dict[str, Any] | None:
        r = self._post_with_retry(
            "/relay/v2/scheduler/claim",
            {"capability": capability},
        )
        if r.status_code == 204:
            return None
        r.raise_for_status()
        data = r.json()
        if not data.get("claimed") or not data.get("stage"):
            return None
        return data["stage"]

    def complete(self, task_id: str, stage_id: str, result: dict[str, Any]) -> dict[str, Any]:
        r = self._post_with_retry(
            f"/relay/v2/scheduler/stages/{stage_id}/complete",
            {"node_id": self.meta["node_id"], "task_id": task_id, "result": result},
            timeout=self.cfg.get("task_timeout", 600),
        )
        r.raise_for_status()
        return r.json()

    def submit_simple_task(
        self,
        capability: str,
        payload: dict[str, Any],
        *,
        name: str = "",
        priority: int = 0,
        owner_node_id: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "capability": capability,
            "payload": payload,
            "name": name,
            "priority": priority,
        }
        if owner_node_id:
            body["owner_node_id"] = owner_node_id
        r = self._post_with_retry("/relay/v2/scheduler/task-simple", body)
        r.raise_for_status()
        return r.json()

    def get_task(self, task_id: str) -> dict[str, Any]:
        """Fetch task details including stages, artifacts, and notes."""
        r = self._get_with_retry(f"/relay/v2/scheduler/tasks/{task_id}")
        if r.status_code == 404:
            return {"error": "not found", "task_id": task_id}
        r.raise_for_status()
        return r.json()

    def add_task_note(self, task_id: str, message: str) -> dict[str, Any]:
        """Append a free-form note to a task (T-052 mini-chat)."""
        r = self._post_with_retry(
            f"/relay/v2/scheduler/tasks/{task_id}/notes",
            {"message": message},
        )
        r.raise_for_status()
        return r.json()

    # -- T-126: temporary bridge routes -------------------------------------

    def register_temp_route(
        self,
        path: str,
        method: str,
        upstream: str,
        *,
        ttl_seconds: int,
        channel_id: str,
        description: str = "",
    ) -> dict[str, Any]:
        """Register a temporary bridge route on the server (T-124/T-126).

        The route is owned by this node (the node_id comes from the
        Bearer token), lives for ``ttl_seconds`` and is tied to
        ``channel_id`` so the caller can revoke it later. Use this for
        large-file handoff (storage upload/download channels) where the
        regular heartbeat routes would be replaced too eagerly.

        Returns the server response dict containing ``expires_at``.
        """
        body = {
            "path": path,
            "method": method,
            "upstream": upstream,
            "ttl_seconds": ttl_seconds,
            "channel_id": channel_id,
            "description": description,
        }
        r = self._post_with_retry(
            "/relay/v2/dashboard/api/node-routes/register", body
        )
        r.raise_for_status()
        return r.json()

    def unregister_temp_route(self, path: str, method: str = "GET") -> None:
        """Delete a route owned by this node before its TTL expires (T-126).

        ``DELETE /api/node-routes/{node_id}/{path}?method=...``. The node
        is resolved from the Bearer token on the server side, so this
        client only needs the path + method it registered earlier. A 404
        (route already expired/reaped) is swallowed.
        """
        import urllib.parse

        # The path is matched verbatim by the server; keep the leading
        # slash the server expects and URL-encode any segment so a path
        # with special characters survives the routing layer.
        sub = path if path.startswith("/") else "/" + path
        url_path = urllib.parse.quote(sub, safe="/")
        r = httpx.delete(
            f"{self.base_url}/relay/v2/dashboard/api/node-routes/{self.meta['node_id']}{url_path}",
            params={"method": method},
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=self.cfg["request_timeout"],
            verify=self._verify,
        )
        if r.status_code in (401, 403):
            if self._refresh_token():
                r = httpx.delete(
                    f"{self.base_url}/relay/v2/dashboard/api/node-routes/{self.meta['node_id']}{url_path}",
                    params={"method": method},
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=self.cfg["request_timeout"],
                    verify=self._verify,
                )
        # 404 means the route already expired/reaped — fine.
        if r.status_code not in (200, 404):
            r.raise_for_status()

    def list_temp_routes(self) -> list[dict[str, Any]]:
        """List this node's own temp routes from the server (T-136).

        ``GET /api/node-routes?node_id=<own>`` (rt-Token). The server
        resolves the caller's node_id from the Bearer token and returns
        only routes owned by this node, including ``expires_at`` and
        ``channel_id`` for each row.
        """
        r = self._get_with_retry("/relay/v2/dashboard/api/node-routes")
        r.raise_for_status()
        data = r.json()
        # Server returns ``{"routes": [...]}``.
        if isinstance(data, list):
            return data
        return data.get("routes", [])

    # -- artifact download ---------------------------------------------------

    def download_artifact(
        self,
        artifact_id: str,
        output_path: Optional[Path] = None,
        *,
        chunk_size: int = 64 * 1024,
    ) -> Path:
        """Download an artifact by id, streaming it to disk chunkwise.

        Falls back to a token refresh on a 401/403, then retries once. The
        output filename is derived from the Content-Disposition header when
        no ``output_path`` is supplied.
        """
        url = f"{self.base_url}/relay/v2/storage/files/{artifact_id}"
        timeout = self.cfg.get("request_timeout", 30)

        cm = httpx.stream(
            "GET",
            url,
            headers={"Authorization": f"Bearer {self.token}"},
            follow_redirects=True,
            timeout=timeout,
        )
        resp = cm.__enter__()
        try:
            if resp.status_code in (401, 403):
                # Close this attempt and retry once after refreshing the token.
                cm.__exit__(None, None, None)
                refreshed = self._refresh_token()
                cm = httpx.stream(
                    "GET",
                    url,
                    headers={"Authorization": f"Bearer {self.token}"},
                    follow_redirects=True,
                    timeout=timeout,
                )
                resp = cm.__enter__()
                if not refreshed:
                    resp.raise_for_status()  # surface the auth error
            resp.raise_for_status()

            target = output_path or Path(_filename_from_response(resp, artifact_id))
            with target.open("wb") as f:
                for chunk in resp.iter_bytes(chunk_size=chunk_size):
                    f.write(chunk)
            return target
        finally:
            cm.__exit__(None, None, None)

    # -- artifact upload -----------------------------------------------------

    def upload_artifact(
        self,
        file_path: Path,
        *,
        name: Optional[str] = None,
        task_id: Optional[str] = None,
        stage_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Upload a local file to the relay as an artifact.

        Returns the server response dict containing ``artifact_id``,
        ``name``, ``size_bytes``, etc. Falls back to a token refresh
        on a 401/403, then retries once.
        """
        url = f"{self.base_url}/relay/v2/storage/upload"
        params: dict[str, str] = {}
        if task_id:
            params["task_id"] = task_id
        if stage_id:
            params["stage_id"] = stage_id

        file_path = Path(file_path)
        upload_name = name or file_path.name

        def _do_upload() -> httpx.Response:
            with file_path.open("rb") as f:
                return httpx.post(
                    url,
                    headers={"Authorization": f"Bearer {self.token}"},
                    files={"file": (upload_name, f, "application/octet-stream")},
                    params=params or None,
                    timeout=self.cfg.get("request_timeout", 30),
                    verify=self._verify,
                )

        resp = _do_upload()
        if resp.status_code in (401, 403):
            self._refresh_token()
            resp = _do_upload()
        resp.raise_for_status()
        return resp.json()


def _filename_from_response(response: httpx.Response, fallback: str) -> str:
    """Extract a filename from Content-Disposition, falling back to the id."""
    cd = response.headers.get("content-disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    return m.group(1) if m else fallback
