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
import secrets
import shutil
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import httpx

from nodes.common.relay_client import RelayClient

log = logging.getLogger("file-serve")

SERVE_PORT_DEFAULT = 8792
SERVE_HOST = "127.0.0.1"          # NUR localhost — der Relay-Proxy ist der einzige Client
SERVE_DIR = Path.home() / ".relay" / "serve"
MANIFEST_PATH = Path.home() / ".relay" / "serve.json"

_ROUTE_BASE = "/relay/v2/dashboard/api/node-routes"


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
    return {"port": port}


def write_manifest(port: int) -> None:
    """``{"port": port}`` nach ``MANIFEST_PATH`` — atomar (tmp+replace).

    Der Daemon schreibt das Manifest beim Serve-Start (F5 Port-Discovery);
    ein CLI, das mitten im Write liest, sieht nie ein halbes JSON.
    """
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_name(MANIFEST_PATH.name + ".tmp")
    tmp.write_text(json.dumps({"port": port}))
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


def probe_serve(port: int, timeout: float = 2.0) -> bool:
    """``GET http://127.0.0.1:{port}/health`` — ``True`` gdw. HTTP 200.

    Alles andere (Connection refused, Timeout, 4xx/5xx, ungültige
    Antwort) ist „nicht erreichbar“ → ``False`` ohne Raise. Der CLI
    (Task 4) leitet daraus den F5-Fehler ab.
    """
    try:
        r = httpx.get(f"http://{SERVE_HOST}:{port}/health", timeout=timeout)
    except httpx.HTTPError as exc:
        log.debug("serve probe on port %d failed: %s", port, exc)
        return False
    return r.status_code == 200


class EphemeralServeHandler(BaseHTTPRequestHandler):
    """Skeleton (Phase 3 / Plan Task 2): POST ``/transfer/{id}`` + GET ``/health``.

    FROZEN Verträge für Phase 3:

    * POST ``/transfer/{transfer_id}`` (leerer Body, Body wird ignoriert):
      streamt die staged Datei chunkwise (``shutil.copyfileobj``, 64 KiB),
      setzt ``Content-Length``; zählt den Download erst bei vollständigem
      Senden (HTTP 200); löscht Datei + Manifest nach ``max_downloads``
      und triggert den ``on_exhausted(route_path)``-Hook (Daemon verdrahtet
      dort ``unregister_after_transfer``); räumt Staged-Files ohne Route
      im Idle nach TTL auf (Plan Task 2).
    * GET ``/health`` → 200 ``{"ok": true}`` (Contract für ``probe_serve``).
    * Unbekannte transfer_id → 404. Abgebrochene/fehlerhafte Downloads
      zählen NICHT (F6 — Retry möglich bis TTL).
    * Logging quiet (kein Stderr-Spam pro Request im Daemon).
    """

    def do_GET(self) -> None:  # pragma: no cover — Phase 3 (Plan Task 2)
        raise NotImplementedError("serve handler folgt in Task 2")

    def do_POST(self) -> None:  # pragma: no cover — Phase 3 (Plan Task 2)
        raise NotImplementedError("serve handler folgt in Task 2")


def start_serve_thread() -> threading.Thread:
    """Skeleton (Phase 3 / Plan Task 3): Serve-Thread im Daemon starten.

    FROZEN Verträge für Phase 3:

    * bind ``SERVE_HOST:serve_port()`` (``ThreadingHTTPServer``);
      ``write_manifest(port)`` nach erfolgreichem Bind.
    * Thread ``daemon=True`` → stirbt mit dem Daemon-Prozess.
    * ``RuntimeError`` bei Bind-Fehl — der Daemon-Wrapper (FROZEN-Snippet
      im Plan) fängt OSError/RuntimeError → WARNING, Node läuft ohne
      Serve weiter (F1: bridge put meldet dann den F5-Fehler).
    * Idempotenz: bereits laufender Serve-Thread → denselben Thread
      zurückgeben (kein Doppel-Bind beim Re-Run).
    """
    raise NotImplementedError("serve thread folgt in Task 3")


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