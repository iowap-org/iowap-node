"""T-166: Ephemeral file serve — CLI→daemon handoff for ``hp put`` bridge.

The sending node stages the file to ``~/.relay/serve/{transfer_id}``
and registers a temp route whose upstream points at the serve server
inside the DAEMON (F1 — the CLI process dies after the envelope, so
the endpoint must live in the long-running service). The receiver
pulls through the relay proxy (F7). After ``max_downloads`` successful
downloads (F6, default 1) the daemon deletes file + manifest and
unregisters the route (the TTL sweep T-125 is the backstop).

FROZEN (Phase 1, T-166) — every signature in this module is immutable
for Phase 2 (scaffold) and Phase 3 (integrate):

* ``serve_port()``            — env ``IOWAP_SERVE_PORT``, else 8792.
* ``read_manifest()``        — read serve.json; ``None`` when missing/
                                invalid (the normal case without a daemon).
* ``write_manifest(port)``    — write atomically (tmp + os.replace).
* ``new_transfer_id()``      — ``secrets.token_urlsafe(16)`` → 22 chars
                                (≤64-char limit of the route registry).
* ``stage_file(src)``        — stage to ``SERVE_DIR``, compute sha256
                                (F8), write the F6 manifest
                                ``{file, size, sha256, max_downloads}``.
* ``build_storage_ref(...)``  — F4 shape ``{type, node_id, path, expires_at}``
                                (envelope v1 unchanged).
* ``proxy_url(...)``         — F7 URL shape, identical to the
                                ``unregister_temp_route`` path (T-126).
* ``probe_serve(port)``      — ``GET /health``; ``True`` iff HTTP 200.
* ``EphemeralServeHandler``  — skeleton; Phase 3 (plan task 2) fills
                                POST ``/transfer/{id}`` + GET ``/health``.
* ``start_serve_thread()``   — skeleton; Phase 3 (plan task 3) fills the
                                bind + manifest write + daemon thread.
* ``unregister_after_transfer`` — best-effort route deletion (F6),
                                log errors only (TTL is the backstop).

Phase-1 refinements (FROZEN, documented in the plan under "Phase 1"):

* ``stage_file`` gets the keyword-only argument ``max_downloads=1``
  (F6) — call-compatible with the plan snippet ``stage_file(path)``.
* ``read_manifest()`` validates the port strictly (int 1–65535, bool
  explicitly excluded) — cmd_put (task 4) gets a typed guarantee.
* ``serve_port()`` tolerates invalid env values with WARNING + default
  (house pattern: relay_client._effective_config) instead of SystemExit.
* ``probe_serve`` counts only HTTP 200 as "reachable".
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from nodes.common.relay_client import RelayClient

log = logging.getLogger("file-serve")

SERVE_PORT_DEFAULT = 8792
SERVE_HOST = "127.0.0.1"          # Default — the relay proxy is the only client.
# T-166 live-smoke deviation (D8): the relay and the node run on different
# machines (LXC 903 vs. Hermes host). The proxy dials the registered
# upstream address VERBATIM — ``127.0.0.1`` points into the void there (502).
# ``IOWAP_SERVE_HOST`` overrides the bind AND advertise address (e.g. the
# LAN IP); the default remains the plan's single-host behavior.

# T-166 D9: source-IP allowlist (T-128 pattern from the storage node). When
# the serve binds to 0.0.0.0, the transfer endpoints are reachable in the
# LAN — only connections from the relay server IP (T-128: "the relay proxy
# is the only legitimate dialer") plus loopback for the local CLI probe
# path are accepted. Fail closed when the IP is not resolvable.
# X-Forwarded-For is never evaluated — the socket peer is
# authoritative (otherwise spoofable).


def serve_host() -> str:
    """Bind/advertise host: env ``IOWAP_SERVE_HOST``, else ``SERVE_HOST``.

    House pattern like ``serve_port()``: a nonsensical env value (empty,
    whitespace, >255 chars) → WARNING + default, no hard crash.
    Evaluated in the daemon AND in the CLI — both run on the same
    machine, so the same env set yields the same address.
    """
    raw = os.environ.get("IOWAP_SERVE_HOST")
    if raw is None:
        return SERVE_HOST
    host = raw.strip()
    if not host or len(host) > 255:
        log.warning("ignoring invalid IOWAP_SERVE_HOST=%r", raw)
        return SERVE_HOST
    return host


def _relay_host_ip() -> str | None:
    """First IPv4 of the relay hostname from the node relay config (D9).

    Order like ``_effective_config()``/storage-node T-128: env
    ``RELAY_BASE_URL`` → relay_config.json ``base_url`` → mDNS discovery.
    ``None`` when none of these is resolvable (the caller then decides
    to fail closed).
    """
    import socket
    from urllib.parse import urlparse

    from nodes.common.relay_client import (
        _discover_relay_mdns,
        _effective_config,
    )

    cfg = _effective_config()
    relay_url = str(cfg.get("base_url") or "")
    if not relay_url:
        discovered = _discover_relay_mdns()
        if discovered:
            relay_url = discovered
    if not relay_url:
        return None
    host = urlparse(relay_url).hostname
    if not host:
        return None
    try:
        infos = socket.getaddrinfo(
            host, None, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
    except socket.gaierror:
        return None
    for info in infos:
        if info[0] == socket.AF_INET:
            return str(info[4][0])
    return None


def serve_allow_ip() -> str | None:
    """The single allowed client IP at the serve endpoint (D9).

    Chain: env ``IOWAP_SERVE_ALLOW`` (explicit override) → relay IP from
    the node config. ``None`` = not resolvable → LAN peers are rejected
    (fail closed for the remote path); the local probe path
    (loopback/advertise IP) stays servable regardless (D8 lesson:
    ``hp put`` probes the advertise address — the peer is the node's
    own LAN IP). Cached at server bind time (T-128 semantics: relay IP
    change → daemon restart).
    """
    raw = os.environ.get("IOWAP_SERVE_ALLOW")
    if raw and raw.strip():
        ip = raw.strip().split(",")[0].strip()
        try:
            import ipaddress

            ipaddress.IPv4Address(ip)
        except ValueError:
            log.warning("ignoring invalid IOWAP_SERVE_ALLOW=%r", raw)
        else:
            return ip
    return _relay_host_ip()


def _allowed_peers() -> frozenset[str]:
    """Complete peer allowlist (D9): relay + advertise + loopback.

    Legitimate clients at the serve endpoint:

    * the relay proxy (relay IP — ``serve_allow_ip()``),
    * the node CLI on the same host (loopback; disguised via the
      advertise address as the node's own LAN IP — D8: ``probe_serve``
      targets the advertise address, so the node's own advertise IP is
      allowed too),
    * loopback in general (probe, local tools).

    A relay-IP outage degrades only the remote path (WARNING at bind),
    never the local one.
    """
    import socket

    peers = {"127.0.0.1", "::1"}
    relay_ip = serve_allow_ip()
    if relay_ip:
        peers.add(relay_ip)
    host = serve_host()
    if host not in ("", "0.0.0.0", "127.0.0.1", "localhost"):
        try:
            peers.add(socket.gethostbyname(host))
        except OSError:
            log.debug("cannot resolve advertise host %r — skipping in allowlist", host)
    return frozenset(peers)


SERVE_DIR = Path.home() / ".relay" / "serve"
MANIFEST_PATH = Path.home() / ".relay" / "serve.json"

_ROUTE_BASE = "/relay/v2/dashboard/api/node-routes"

# --- Runtime state (task 2/3) -------------------------------------------------
# TTL for staged leftovers without a route (sweep backstop, F6); sweep
# interval inside the serve thread. transfer_id: exactly 22 url-safe chars (F2).
_SERVE_TTL_SECONDS = 3600.0
_SWEEP_INTERVAL = 60.0
_TRANSFER_RE = re.compile(r"^/transfer/([A-Za-z0-9_-]{22})$")

# Daemon runtime: tests patch these module attributes (call-time lookup).
_serve_thread: threading.Thread | None = None
_serve_server: ThreadingHTTPServer | None = None
_ON_EXHAUSTED: Callable[[str], None] | None = None
_last_sweep = 0.0
_transfer_lock = threading.Lock()


def set_on_exhausted(hook: Callable[[str], None] | None) -> None:
    """Hook ``(route_path) -> None`` after an exhausted transfer (F6).

    The daemon wires ``unregister_after_transfer`` here (a closure over
    its RelayClient); tests set a spy. Invocation happens in the request
    thread AFTER the fully sent response — hook errors are logged and
    never break the download.
    """
    global _ON_EXHAUSTED
    _ON_EXHAUSTED = hook


def discard_staged(transfer_id: str) -> None:
    """Remove the staged file + transfer manifest (rollback after a register failure).

    ``hp put`` (task 4) calls this when the route registration fails —
    no stale leftover without a route in SERVE_DIR (the sweep is the
    backstop).
    """
    (SERVE_DIR / transfer_id).unlink(missing_ok=True)
    (SERVE_DIR / (transfer_id + ".json")).unlink(missing_ok=True)


def _sweep_stale_transfers() -> None:
    """Delete SERVE_DIR leftovers older than ``_SERVE_TTL_SECONDS`` (rate-limited).

    Covers aborts between staging and register (a file without a
    route). Runs in the request path, but at most every
    ``_SWEEP_INTERVAL`` seconds (``_last_sweep``); every file operation
    is best-effort.
    """
    global _last_sweep
    now = time.time()
    if now - _last_sweep < _SWEEP_INTERVAL:
        return
    _last_sweep = now
    cutoff = now - _SERVE_TTL_SECONDS
    try:
        entries = list(SERVE_DIR.iterdir())
    except OSError:
        return
    for p in entries:
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
        except OSError:
            continue


def serve_port() -> int:
    """Serve port: env ``IOWAP_SERVE_PORT``, else ``SERVE_PORT_DEFAULT``.

    Invalid env values (not an int, out of range) → WARNING + default —
    a typo in the env does not break the daemon (house pattern like
    ``relay_client._effective_config``).
    """
    raw = os.environ.get("IOWAP_SERVE_PORT")
    if raw is None:
        return SERVE_PORT_DEFAULT
    try:
        port = int(raw)
    except ValueError:
        log.warning("ignoring invalid IOWAP_SERVE_PORT=%r", raw)
        return SERVE_PORT_DEFAULT
    if not 1 <= port <= 65535:
        log.warning("ignoring out-of-range IOWAP_SERVE_PORT=%r", raw)
        return SERVE_PORT_DEFAULT
    return port


def read_manifest() -> dict | None:
    """Read serve.json; ``None`` when missing or invalid.

    "Invalid" explicitly includes: not JSON, not a dict, ``port``
    missing/not an int/not a valid port value. The normal case without
    a daemon is ``None`` (the CLI then reports the F5 error).

    D8: the return additionally contains ``host`` (the advertise
    address for the upstream URL). Manifests without a ``host`` key
    (before D8) get the default ``SERVE_HOST`` — an invalid ``host``
    value likewise falls back to the default instead of discarding the
    entire manifest (the port remains the relevant part of the
    contract).
    """
    try:
        raw = MANIFEST_PATH.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("cannot read serve manifest %s: %s", MANIFEST_PATH, exc)
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        log.warning("serve manifest %s is not valid JSON: %s", MANIFEST_PATH, exc)
        return None
    if not isinstance(data, dict):
        return None
    port = data.get("port")
    # bool is an int subclass — otherwise True would be "port 1".
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return None
    host = data.get("host", SERVE_HOST)
    if not isinstance(host, str) or not host or len(host) > 255:
        log.warning("serve manifest has invalid host %r — using default", host)
        host = SERVE_HOST
    return {"port": port, "host": host}


def write_manifest(port: int, host: str | None = None) -> None:
    """Write ``{"port": port, "host": host}`` to ``MANIFEST_PATH`` — atomically.

    The daemon writes the manifest at serve start (F5 port discovery);
    a CLI reading midway through the write never sees a half-written
    JSON. ``host`` is the D8 advertise address (``serve_host()``);
    ``None`` writes the default ``SERVE_HOST`` — old manifests without
    a ``host`` key stay readable (read_manifest adds it, see there).
    """
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_name(MANIFEST_PATH.name + ".tmp")
    tmp.write_text(
        json.dumps({"port": port, "host": host if host is not None else SERVE_HOST})
    )
    os.replace(tmp, MANIFEST_PATH)


def new_transfer_id() -> str:
    """``secrets.token_urlsafe(16)`` → 22 url-safe chars (F2).

    22 ≤ the route registry's 64-char limit; collisions are impossible
    with 128 bits of entropy.
    """
    return secrets.token_urlsafe(16)


def stage_file(src: Path, *, max_downloads: int = 1) -> tuple[str, Path, str]:
    """Stage ``src`` to ``SERVE_DIR/{transfer_id}``, return (id, staged, sha256).

    Move semantics (F5): ``os.replace`` = O(1) on the same filesystem,
    with a ``shutil.copy`` + unlink fallback across filesystem
    boundaries (EXDEV). Writes the F6 manifest
    ``SERVE_DIR/{transfer_id}.json`` with
    ``{file, size, sha256, max_downloads}`` — the daemon counts along
    it and cleans up after ``max_downloads`` downloads.

    Raise ``ValueError`` for ``max_downloads < 1`` — a 0-download
    transfer would be a dead route (fail fast at the producer).
    """
    if max_downloads < 1:
        raise ValueError(f"max_downloads must be >= 1, got {max_downloads}")
    SERVE_DIR.mkdir(parents=True, exist_ok=True)
    transfer_id = new_transfer_id()
    staged = SERVE_DIR / transfer_id
    try:
        os.replace(src, staged)
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.ENOTSUP):
            raise
        shutil.copy(src, staged)
        src.unlink(missing_ok=True)
    sha_hex = _sha256_file(staged)
    _write_transfer_manifest(staged, sha_hex, max_downloads)
    return transfer_id, staged, sha_hex


def _sha256_file(path: Path) -> str:
    """sha256 of a file, chunkwise (64 KiB) — no RAM peak (F8)."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_transfer_manifest(staged: Path, sha_hex: str, max_downloads: int) -> None:
    """F6 manifest next to the staged file (atomic via tmp+replace)."""
    manifest = Path(str(staged) + ".json")
    tmp = Path(str(manifest) + ".tmp")
    payload = {
        "file": staged.name,
        "size": staged.stat().st_size,
        "sha256": sha_hex,
        "max_downloads": max_downloads,
    }
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, manifest)


def build_storage_ref(node_id: str, route_path: str, expires_at: str) -> dict:
    """F4 — ``storage_ref`` for envelope v1 (shape FROZEN):

    ``{"type": "node_serve", "node_id": ..., "path": ..., "expires_at": ...}``

    ``parse_envelope`` returns the dict generically (T-179) — no
    envelope change, no contract break for old receivers.
    """
    return {
        "type": "node_serve",
        "node_id": node_id,
        "path": route_path,
        "expires_at": expires_at,
    }


def proxy_url(base_url: str, node_id: str, route_path: str) -> str:
    """F7 — proxy URL for the receiver's pull.

    Path shape identical to ``unregister_temp_route``
    (``relay_client.py``): ``{base_url}{_ROUTE_BASE}/{node_id}{path}``.
    ``base_url`` loses a trailing slash; ``route_path`` is guaranteed
    its leading slash (defensively against both spellings).
    """
    path = route_path if route_path.startswith("/") else "/" + route_path
    return f"{base_url.rstrip('/')}{_ROUTE_BASE}/{node_id}{path}"


def probe_serve(port: int, timeout: float = 2.0, host: str | None = None) -> bool:
    """``GET http://{host|SERVE_HOST}:{port}/health`` — ``True`` iff 200.

    Everything else (connection refused, timeout, 4xx/5xx, invalid
    response) is "unreachable" → ``False`` without raising. The CLI
    (task 4) derives the F5 error from it. ``host`` (D8) allows probing
    against the advertise address from the manifest; the default stays
    ``SERVE_HOST`` (localhost, plan behavior).
    """
    try:
        r = httpx.get(f"http://{host or SERVE_HOST}:{port}/health", timeout=timeout)
    except httpx.HTTPError as exc:
        log.debug("serve probe on port %d failed: %s", port, exc)
        return False
    return r.status_code == 200


class EphemeralServeHandler(BaseHTTPRequestHandler):
    """POST ``/transfer/{id}`` + GET ``/health`` (T-166, F2/F6/F7).

    Contracts (FROZEN from Phase 1, now implemented):

    * POST ``/transfer/{transfer_id}`` (body is ignored/drained):
      streams the staged file chunkwise (``shutil.copyfileobj``, 64 KiB)
      with ``Content-Length``; counts the download only once the body
      was fully sent (HTTP 200, no abort); deletes file + manifest
      after ``max_downloads`` (F6) and triggers the ``on_exhausted``
      hook (the daemon wires ``unregister_after_transfer`` there);
      aborts do NOT count → retry until the TTL. Side effect:
      rate-limited sweep.
    * GET ``/health`` → 200 ``{"ok": true}`` (contract for ``probe_serve``).
    * Unknown/foreign paths or a wrong ID form → 404. GET on
      ``/transfer/...`` → 404 (POST-only channel — the proxy matches
      the registered method exactly).
    * Logging quiet (debug level, no stderr spam per request in the
      daemon).
    """

    def log_message(self, format: str, *args) -> None:
        log.debug("serve: " + format, *args)

    def _drain_body(self) -> None:
        """Consume the request body (F7 sends it empty; the proxy may send more)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        remaining = max(0, length)
        while remaining > 0:
            chunk = self.rfile.read(min(64 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _send_json(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _peer_allowed(self) -> bool:
        """D9: check the socket peer against the cached allowlist.

        Cached on the server object (T-128 semantics: resolved once at
        start, ``_allowed_peers``). Fail closed applies to the remote
        path: no relay IP resolvable → LAN peers rejected; loopback and
        the advertise IP stay legitimate (local probe/CLI path, D8).
        """
        allowed: frozenset[str] | None = getattr(self.server, "_allow_ip", None)
        peer = self.client_address[0]
        if not allowed:
            log.warning(
                "serve allowlist unresolved — rejecting remote request from %s",
                peer,
            )
            return False
        if peer in allowed:
            return True
        log.warning("rejected serve request from %s (allowed: %s)", peer, sorted(allowed))
        return False

    def do_GET(self) -> None:
        self._drain_body()
        if not self._peer_allowed():
            self._send_json(403, b'{"error": "forbidden"}')
            return
        if self.path == "/health":
            self._send_json(200, b'{"ok": true}')
        else:
            self._send_json(404, b'{"error": "not found"}')

    def do_POST(self) -> None:
        self._drain_body()
        if not self._peer_allowed():
            self._send_json(403, b'{"error": "forbidden"}')
            return
        m = _TRANSFER_RE.match(self.path)
        if not m:
            self._send_json(404, b'{"error": "not found"}')
            return
        transfer_id = m.group(1)
        staged = SERVE_DIR / transfer_id
        manifest_file = Path(str(staged) + ".json")
        with _transfer_lock:
            try:
                fh = staged.open("rb")
            except OSError:
                self._send_json(404, b'{"error": "unknown transfer"}')
                return
            size = staged.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        try:
            with fh:
                shutil.copyfileobj(fh, self.wfile, length=64 * 1024)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            # Aborted download does NOT count (F6 — retry possible until the TTL).
            log.warning("transfer %s aborted mid-stream: %s", transfer_id, exc)
            return
        self._record_download(transfer_id, staged, manifest_file)

    def _record_download(
        self, transfer_id: str, staged: Path, manifest_file: Path
    ) -> None:
        """Count the download; at ``max_downloads`` clean up + fire the hook (F6)."""
        with _transfer_lock:
            try:
                data = json.loads(manifest_file.read_text())
            except (OSError, ValueError):
                # A parallel request has already exhausted it — nothing left to count.
                return
            downloads = int(data.get("downloads", 0)) + 1
            max_downloads = int(data.get("max_downloads", 1))
            if downloads >= max_downloads:
                staged.unlink(missing_ok=True)
                manifest_file.unlink(missing_ok=True)
                exhausted = True
            else:
                data["downloads"] = downloads
                tmp = Path(str(manifest_file) + ".tmp")
                tmp.write_text(json.dumps(data))
                os.replace(tmp, manifest_file)
                exhausted = False
        if exhausted:
            hook = _ON_EXHAUSTED
            if hook is not None:
                try:
                    hook(f"/download/{transfer_id}")
                except Exception:
                    log.exception("on_exhausted hook failed for %s", transfer_id)
        _sweep_stale_transfers()


def start_serve_thread() -> threading.Thread:
    """Start the serve thread in the daemon (F1) — bind, manifest, daemon thread.

    * bind ``SERVE_HOST:serve_port()`` (``ThreadingHTTPServer``),
      ``write_manifest(port)`` after a successful bind (F5 port
      discovery).
    * Thread ``daemon=True`` → dies with the daemon process.
    * ``RuntimeError`` on bind failure — the daemon wrapper catches
      OSError/RuntimeError → WARNING, the node keeps running without
      serve (F1: ``hp put`` bridge then reports the F5 error instead
      of crashing).
    * Idempotency: an already-running serve thread → the same thread
      is returned (no double bind on re-run).
    """
    global _serve_server, _serve_thread
    existing = _serve_thread
    if existing is not None and existing.is_alive():
        return existing
    port = serve_port()
    host = serve_host()
    # D9: resolve the peer allowlist once at bind time and cache it on
    # the server object (T-128 semantics). Without a relay IP the serve
    # runs fail-closed for remote peers — the local probe path stays
    # servable.
    allow_peers = _allowed_peers()
    if serve_allow_ip() is None:
        log.warning(
            "serve allowlist: relay IP unresolved (set IOWAP_SERVE_ALLOW) "
            "— remote serve requests will be rejected until daemon restart"
        )
    try:
        server = ThreadingHTTPServer((host, port), EphemeralServeHandler)
    except OSError as exc:
        raise RuntimeError(
            f"ephemeral serve bind failed on {host}:{port}: {exc}"
        ) from exc
    server._allow_ip = allow_peers  # type: ignore[attr-defined]
    _serve_server = server
    write_manifest(port, host=host)
    t = threading.Thread(
        target=server.serve_forever, daemon=True, name="file-serve"
    )
    t.start()
    _serve_thread = t
    return t


def unregister_after_transfer(
    client: RelayClient, route_path: str, method: str = "POST"
) -> None:
    """Best-effort route deletion after an exhausted transfer (F6).

    The daemon calls this after the last successful download —
    HTTP errors are only logged (never re-raised): the download itself
    already succeeded; the TTL sweep (T-125) is the backstop.
    """
    try:
        client.unregister_temp_route(route_path, method=method)
    except httpx.HTTPError as exc:
        log.warning("route unregister failed for %s (%s): %s", route_path, method, exc)