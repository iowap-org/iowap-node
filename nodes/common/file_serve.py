"""T-166: Ephemeral file serve — CLI→Daemon Handoff für ``hp put`` bridge.

Der Sende-Node staged die Datei nach ``~/.relay/serve/{transfer_id}``
und registriert eine temp Route, deren Upstream auf den Serve-Server im
DAEMON zeigt (F1 — der CLI-Prozess stirbt nach dem Envelope, deshalb
lebt der Endpoint im Dauerdienst). Der Empfänger pullt über den
Relay-Proxy (F7). Nach ``max_downloads`` erfolgreichen Downloads (F6,
Default 1) löscht der Daemon Datei + Manifest und unregistriert die
Route (TTL-Sweep T-125 ist der Backstop).

FROZEN (Phase 1, T-166) — jede Signatur in diesem Modul ist unveränderlich
für Phase 2 (scaffold) und Phase 3 (integrate):

* ``serve_port()``            — env ``IOWAP_SERVE_PORT``, sonst 8792.
* ``read_manifest()``        — serve.json lesen; ``None`` wenn fehlend/
                                ungültig (der Normalfall ohne Daemon).
* ``write_manifest(port)``    — atomar (tmp + os.replace) schreiben.
* ``new_transfer_id()``      — ``secrets.token_urlsafe(16)`` → 22 Zeichen
                                (≤64-Zeichen-Limit der Route-Registry).
* ``stage_file(src)``        — staged nach ``SERVE_DIR``, berechnet sha256
                                (F8), schreibt das F6-Manifest
                                ``{file, size, sha256, max_downloads}``.
* ``build_storage_ref(...)``  — F4-Shape ``{type, node_id, path, expires_at}``
                                (Envelope v1 unverändert).
* ``proxy_url(...)``         — F7-URL-Shape, identisch zum
                                ``unregister_temp_route``-Pfad (T-126).
* ``probe_serve(port)``      — ``GET /health``; ``True`` gdw. HTTP 200.
* ``EphemeralServeHandler``  — Skeleton; Phase 3 (Plan Task 2) füllt
                                POST ``/transfer/{id}`` + GET ``/health``.
* ``start_serve_thread()``   — Skeleton; Phase 3 (Plan Task 3) füllt den
                                Bind + Manifest-Write + daemon-Thread.
* ``unregister_after_transfer`` — best-effort Route-Löschung (F6), Fehler
                                nur loggen (TTL ist der Backstop).

Phase-1-Präzisierungen (FROZEN, im Plan unter „Phase 1" dokumentiert):

* ``stage_file`` bekommt das keyword-only-Argument ``max_downloads=1``
  (F6) — call-kompatibel zum Plan-Snippet ``stage_file(path)``.
* ``read_manifest()`` validiert port streng (int 1–65535, bool ausdrücklich
  ausgeschlossen) — cmd_put (Task 4) bekommt eine getypte Garantie.
* ``serve_port()`` toleriert ungültige Env-Werte mit WARNING + Default
  (Haus-Pattern: relay_client._effective_config) statt SystemExit.
* ``probe_serve`` wertet nur HTTP 200 als „erreichbar“.
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
SERVE_HOST = "127.0.0.1"          # Default — der Relay-Proxy ist der einzige Client.
# T-166 Live-Smoke-Deviation (D8): Relay und Node laufen auf verschiedenen
# Maschinen (LXC 903 vs. Hermes-Host). Der Proxy dialt die registrierte
# upstream-Adresse WÖRTLICH — ``127.0.0.1`` zeigt dort ins Leere (502).
# ``IOWAP_SERVE_HOST`` überschreibt Bind- UND Advertise-Adresse (z. B. die
# LAN-IP); Default bleibt das Single-Host-Verhalten des Plans.

# T-166 D9: Source-IP-Allowlist (T-128-Muster vom Storage-Node). Bindet
# der Serve auf 0.0.0.0, sind die Transfer-Endpunkte im LAN erreichbar —
# akzeptiert werden nur Verbindungen von der Relay-Server-IP (T-128:
# "der Relay-Proxy ist der einzige legitime Dialer") plus loopback für
# den lokalen CLI-Probe-Pfad. Fail-closed, wenn die IP nicht auflösbar
# ist. X-Forwarded-For wird nie ausgewertet — der Socket-Peer ist
# autoritativ (sonst spoofbar).


def serve_host() -> str:
    """Bind-/Advertise-Host: env ``IOWAP_SERVE_HOST``, sonst ``SERVE_HOST``.

    Haus-Pattern wie ``serve_port()``: ein unsinniger Env-Wert (leer,
    Whitespace, >255 Zeichen) → WARNING + Default, kein harter Absturz.
    Wird im Daemon UND im CLI ausgewertet — beide laufen auf derselben
    Maschine, also liefert derselbe Env-Satz dieselbe Adresse.
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
    """Erste IPv4 des Relay-Hostnamens aus der Node-Relay-Config (D9).

    Reihenfolge wie ``_effective_config()``/Storage-Node-T-128: env
    ``RELAY_BASE_URL`` → relay_config.json ``base_url`` → mDNS-Discovery.
    ``None``, wenn nichts davon auflösbar ist (Caller entscheidet dann
    fail-closed).
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
    """Die eine erlaubte Client-IP am Serve-Endpoint (D9).

    Kette: env ``IOWAP_SERVE_ALLOW`` (explizite Override) → Relay-IP aus
    der Node-Config. ``None`` = nicht auflösbar → LAN-Peers werden
    abgewiesen (fail-closed für den Remote-Pfad); der lokale Probe-Pfad
    (loopback/advertise-IP) bleibt unabhängig davon bedienbar (D8-Lektion:
    ``hp put`` probt die advertise-Adresse — Peer ist die eigene LAN-IP).
    Wird beim Server-Bind gecacht (T-128-Semantik: Relay-IP-Änderung →
    Daemon-Restart).
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
    """Vollständige Peer-Allowlist (D9): Relay + advertise + loopback.

    Legitime Clients am Serve-Endpoint:

    * der Relay-Proxy (Relay-IP — ``serve_allow_ip()``),
    * die Node-CLI auf demselben Host (loopback; via advertise-Adresse
      getarnt als die eigene LAN-IP — D8: ``probe_serve`` zielt auf die
      advertise-Adresse, daher ist die eigene advertise-IP ebenfalls
      erlaubt),
    * loopback generell (Probe, lokale Tools).

    Ein Relay-IP-Ausfall degradiert nur den Remote-Pfad (WARNING beim
    Bind), nie den lokalen.
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

# --- Laufzeitzustand (Task 2/3) ------------------------------------------------
# TTL für staged Reste ohne Route (Sweep-Backstop, F6); Sweep-Intervall im
# Serve-Thread. transfer_id: genau 22 url-safe Zeichen (F2).
_SERVE_TTL_SECONDS = 3600.0
_SWEEP_INTERVAL = 60.0
_TRANSFER_RE = re.compile(r"^/transfer/([A-Za-z0-9_-]{22})$")

# Daemon-Laufzeit: Tests patchen diese Modul-Attribute (Call-Zeit-Lookup).
_serve_thread: threading.Thread | None = None
_serve_server: ThreadingHTTPServer | None = None
_ON_EXHAUSTED: Callable[[str], None] | None = None
_last_sweep = 0.0
_transfer_lock = threading.Lock()


def set_on_exhausted(hook: Callable[[str], None] | None) -> None:
    """Hook ``(route_path) -> None`` nach erschöpftem Transfer (F6).

    Der Daemon verdrahtet hier ``unregister_after_transfer`` (Closure über
    seinen RelayClient); Tests setzen einen Spy. Aufruf erfolgt im
    Request-Thread NACH vollständig gesendeter Antwort — Hook-Fehler
    werden geloggt, brechen den Download nie.
    """
    global _ON_EXHAUSTED
    _ON_EXHAUSTED = hook


def discard_staged(transfer_id: str) -> None:
    """Staged Datei + Transfer-Manifest entfernen (Rollback nach Register-Fail).

    ``hp put`` (Task 4) ruft das, wenn die Route-Registrierung scheitert —
    kein stale Rest ohne Route im SERVE_DIR (der Sweep ist der Backstop).
    """
    (SERVE_DIR / transfer_id).unlink(missing_ok=True)
    (SERVE_DIR / (transfer_id + ".json")).unlink(missing_ok=True)


def _sweep_stale_transfers() -> None:
    """Lösche SERVE_DIR-Reste älter als ``_SERVE_TTL_SECONDS`` (rate-limited).

    Deckt Abbrüche zwischen Staging und Register ab (Datei ohne Route).
    Läuft im Request-Pfad, aber höchstens alle ``_SWEEP_INTERVAL`` Sekunden
    (``_last_sweep``); jede Datei-Operation ist best-effort.
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
    """Serv-Port: env ``IOWAP_SERVE_PORT``, sonst ``SERVE_PORT_DEFAULT``.

    Ungültige Env-Werte (nicht-int, out of range) → WARNING + Default —
    ein Tippfehler in der Env bricht den Daemon nicht (Haus-Pattern wie
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
    """serve.json lesen; ``None`` wenn fehlend oder ungültig.

    „Ungültig“ schließt explizit ein: kein JSON, kein dict, ``port``
    fehlt/kein int/kein gültiger Port-Wert. Der Normalfall ohne Daemon
    ist ``None`` (CLI meldet dann den F5-Fehler).

    D8: Rückgabe enthält zusätzlich ``host`` (Advertise-Adresse für die
    upstream-URL). Manifeste ohne ``host``-Key (vor D8) erhalten den
    Default ``SERVE_HOST`` — ein ungültiger ``host``-Wert fällt ebenso
    auf den Default zurück, statt das Manifest komplett zu verwerfen
    (Port bleibt der relevante Teil des Contracts).
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
    # bool ist eine int-Unterklasse — True wäre sonst „Port 1“.
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return None
    host = data.get("host", SERVE_HOST)
    if not isinstance(host, str) or not host or len(host) > 255:
        log.warning("serve manifest has invalid host %r — using default", host)
        host = SERVE_HOST
    return {"port": port, "host": host}


def write_manifest(port: int, host: str | None = None) -> None:
    """``{"port": port, "host": host}`` nach ``MANIFEST_PATH`` — atomar.

    Der Daemon schreibt das Manifest beim Serve-Start (F5 Port-Discovery);
    ein CLI, das mitten im Write liest, sieht nie ein halbes JSON.
    ``host`` ist die D8-Advertise-Adresse (``serve_host()``); ``None``
    schreibt den Default ``SERVE_HOST`` — alte Manifeste ohne ``host``-Key
    bleiben lesbar (read_manifest ergänzt ihn, siehe dort).
    """
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_name(MANIFEST_PATH.name + ".tmp")
    tmp.write_text(
        json.dumps({"port": port, "host": host if host is not None else SERVE_HOST})
    )
    os.replace(tmp, MANIFEST_PATH)


def new_transfer_id() -> str:
    """``secrets.token_urlsafe(16)`` → 22 url-safe Zeichen (F2).

    22 ≤ 64-Zeichen-Limit der Route-Registry; Kollisionen sind bei 128
    Bit Entropie ausgeschlossen.
    """
    return secrets.token_urlsafe(16)


def stage_file(src: Path, *, max_downloads: int = 1) -> tuple[str, Path, str]:
    """Stage ``src`` nach ``SERVE_DIR/{transfer_id}``, gib (id, staged, sha256).

    Move-Semantik (F5): ``os.replace`` = O(1) auf gleichem FS, mit
    ``shutil.copy`` + unlink-Fallback über FS-Grenzen (EXDEV). Schreibt
    das F6-Manifest ``SERVE_DIR/{transfer_id}.json`` mit
    ``{file, size, sha256, max_downloads}`` — der Daemon zählt daran
    entlang und räumt nach ``max_downloads`` Downloads ab.

    Raise ``ValueError`` für ``max_downloads < 1`` — ein 0-Download-
    Transfer wäre eine tote Route (fail-fast am Erzeuger).
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
    """sha256 einer Datei chunkwise (64 KiB) — kein RAM-Peak (F8)."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_transfer_manifest(staged: Path, sha_hex: str, max_downloads: int) -> None:
    """F6-Manifest neben der staged Datei (atomar via tmp+replace)."""
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
    """F4 — ``storage_ref`` für den Envelope v1 (Shape FROZEN):

    ``{"type": "node_serve", "node_id": ..., "path": ..., "expires_at": ...}``

    ``parse_envelope`` gibt das dict generisch zurück (T-179) — kein
    Envelope-Change, kein Contract-Break für alte Empfänger.
    """
    return {
        "type": "node_serve",
        "node_id": node_id,
        "path": route_path,
        "expires_at": expires_at,
    }


def proxy_url(base_url: str, node_id: str, route_path: str) -> str:
    """F7 — Proxy-URL für den Empfänger-Pull.

    Pfad-Shape identisch zu ``unregister_temp_route``
    (``relay_client.py``): ``{base_url}{_ROUTE_BASE}/{node_id}{path}``.
    ``base_url`` verliert einen trailing slash; ``route_path`` bekommt
    seinen führenden Slash garantiert (defensiv gegen beide Schreibweisen).
    """
    path = route_path if route_path.startswith("/") else "/" + route_path
    return f"{base_url.rstrip('/')}{_ROUTE_BASE}/{node_id}{path}"


def probe_serve(port: int, timeout: float = 2.0, host: str | None = None) -> bool:
    """``GET http://{host|SERVE_HOST}:{port}/health`` — ``True`` gdw. 200.

    Alles andere (Connection refused, Timeout, 4xx/5xx, ungültige
    Antwort) ist „nicht erreichbar“ → ``False`` ohne Raise. Der CLI
    (Task 4) leitet daraus den F5-Fehler ab. ``host`` (D8) erlaubt das
    Probe gegen die advertise-Adresse aus dem Manifest; Default bleibt
    ``SERVE_HOST`` (localhost, Plan-Verhalten).
    """
    try:
        r = httpx.get(f"http://{host or SERVE_HOST}:{port}/health", timeout=timeout)
    except httpx.HTTPError as exc:
        log.debug("serve probe on port %d failed: %s", port, exc)
        return False
    return r.status_code == 200


class EphemeralServeHandler(BaseHTTPRequestHandler):
    """POST ``/transfer/{id}`` + GET ``/health`` (T-166, F2/F6/F7).

    Verträge (FROZEN aus Phase 1, jetzt implementiert):

    * POST ``/transfer/{transfer_id}`` (Body wird ignoriert/drainiert):
      streamt die staged Datei chunkwise (``shutil.copyfileobj``, 64 KiB)
      mit ``Content-Length``; zählt den Download erst bei vollständig
      gesendetem Body (HTTP 200, kein Abbruch); löscht Datei + Manifest
      nach ``max_downloads`` (F6) und triggert den ``on_exhausted``-Hook
      (Daemon verdrahtet dort ``unregister_after_transfer``); Abbrüche
      zählen NICHT → Retry bis TTL. Nebeneffekt: rate-limited Sweep.
    * GET ``/health`` → 200 ``{"ok": true}`` (Contract für ``probe_serve``).
    * Unbekannte/fremde Pfade oder falsche ID-Form → 404. GET auf
      ``/transfer/...`` → 404 (POST-only-Kanal — der Proxy matcht die
      registrierte Methode exakt).
    * Logging quiet (Debug-Level, kein Stderr-Spam pro Request im Daemon).
    """

    def log_message(self, format: str, *args) -> None:
        log.debug("serve: " + format, *args)

    def _drain_body(self) -> None:
        """Request-Body konsumieren (F7 sendet leer; Proxy kann mehr schicken)."""
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
        """D9: Socket-Peer gegen die gecachte Allowlist prüfen.

        Gecacht am Server-Objekt (T-128-Semantik: einmalig beim Start,
        ``_allowed_peers``). Fail-closed gilt für den Remote-Pfad: keine
        Relay-IP auflösbar → LAN-Peers abgewiesen; loopback und die
        advertise-IP bleiben legitim (lokaler Probe-/CLI-Pfad, D8).
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
            # Abgebrochener Download zählt NICHT (F6 — Retry möglich bis TTL).
            log.warning("transfer %s aborted mid-stream: %s", transfer_id, exc)
            return
        self._record_download(transfer_id, staged, manifest_file)

    def _record_download(
        self, transfer_id: str, staged: Path, manifest_file: Path
    ) -> None:
        """Download zählen; bei ``max_downloads`` abräumen + Hook feuern (F6)."""
        with _transfer_lock:
            try:
                data = json.loads(manifest_file.read_text())
            except (OSError, ValueError):
                # Parallel-Request hat bereits erschöpft — nichts mehr zu zählen.
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
    """Serve-Thread im Daemon starten (F1) — bind, Manifest, daemon-Thread.

    * bind ``SERVE_HOST:serve_port()`` (``ThreadingHTTPServer``),
      ``write_manifest(port)`` nach erfolgreichem Bind (F5 Port-Discovery).
    * Thread ``daemon=True`` → stirbt mit dem Daemon-Prozess.
    * ``RuntimeError`` bei Bind-Fehl — der Daemon-Wrapper fängt
      OSError/RuntimeError → WARNING, Node läuft ohne Serve weiter (F1:
      ``hp put`` bridge meldet dann den F5-Fehler).
    * Idempotenz: bereits laufender Serve-Thread → derselbe Thread
      zurückgegeben (kein Doppel-Bind beim Re-Run).
    """
    global _serve_server, _serve_thread
    existing = _serve_thread
    if existing is not None and existing.is_alive():
        return existing
    port = serve_port()
    host = serve_host()
    # D9: Peer-Allowlist einmalig beim Bind auflösen und am Server-Objekt
    # cachen (T-128-Semantik). Ohne Relay-IP läuft der Serve fail-closed
    # für Remote-Peers — der lokale Probe-Pfad bleibt bedienbar.
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
    """Best-effort Route-Löschung nach erschöpftem Transfer (F6).

    Der Daemon ruft das nach dem letzten erfolgreichen Download —
    HTTP-Fehler werden nur geloggt (niemals gereraised): der Download
    selbst war schon erfolgreich, der TTL-Sweep (T-125) ist der Backstop.
    """
    try:
        client.unregister_temp_route(route_path, method=method)
    except httpx.HTTPError as exc:
        log.warning("route unregister failed for %s (%s): %s", route_path, method, exc)