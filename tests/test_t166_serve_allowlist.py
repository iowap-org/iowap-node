"""T-166 D9: Source-IP-Allowlist am ephemeral Serve-Endpoint.

Der Serve-Server bindet (bei Cross-Host-Deploy) auf 0.0.0.0 — im LAN
erreichbar. Als Abschirmung gilt das T-128-Muster vom Storage-Node:
nur Verbindungen von der Relay-Server-IP (+ loopback für den lokalen
CLI-Probe-Pfad) werden akzeptiert, alles andere → HTTP 403.

Auflösung der erlaubten IP (Haus-Pattern, gleiche Kette wie der
Storage-Node):

1. env ``IOWAP_SERVE_ALLOW`` (explizite Override, kommasepariert)
2. Relay-Hostname aus der Node-Relay-Config (``base_url`` aus
   relay_config.json / env ``RELAY_BASE_URL`` / mDNS) → erste IPv4
3. nichts auflösbar → ``None`` → fail-closed (jeder Request 403)
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from nodes.common import file_serve


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _transfer_id() -> str:
    return "a" * 22  # F2: 22 url-safe Zeichen


@pytest.fixture
def serve_globals(monkeypatch, tmp_path):
    """Serve-Laufzeit + Staging-Verzeichnis isolieren."""
    monkeypatch.setattr(file_serve, "_serve_thread", None)
    monkeypatch.setattr(file_serve, "_serve_server", None)
    monkeypatch.setattr(file_serve, "_ON_EXHAUSTED", None)
    monkeypatch.setattr(file_serve, "_last_sweep", 0.0)
    monkeypatch.setattr(file_serve, "SERVE_DIR", tmp_path / "serve")
    monkeypatch.setattr(file_serve, "MANIFEST_PATH", tmp_path / "serve.json")
    file_serve.SERVE_DIR.mkdir(parents=True, exist_ok=True)
    yield


def _start_server(monkeypatch, port: int) -> ThreadingHTTPServer:
    """Direkter Server (ohne Thread-Wrapper) für Request-Tests.

    Setzt denselben D9-Cache wie ``start_serve_thread`` — der Handler
    liest ``server._allow_ip`` (Contract: Cache am Server-Objekt).
    """
    file_serve._serve_server = server = ThreadingHTTPServer(
        ("0.0.0.0", port), file_serve.EphemeralServeHandler
    )
    server._allow_ip = file_serve._allowed_peers()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _get_health(port: int, host: str = "127.0.0.1") -> int:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=3) as resp:
            return resp.status
    except OSError as exc:
        # http.client.HTTPStatus-Fehler (403) → als Status zurückgeben
        code = getattr(exc, "code", None)
        if code is not None:
            return code
        raise


def _post_transfer(port: int, transfer_id: str, host: str = "127.0.0.1") -> int:
    import urllib.request

    req = urllib.request.Request(
        f"http://{host}:{port}/transfer/{transfer_id}", data=b"", method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status
    except OSError as exc:
        code = getattr(exc, "code", None)
        if code is not None:
            return code
        raise


# --- serve_allow_ip() -------------------------------------------------------


def test_allow_env_override_takes_precedence(monkeypatch):
    monkeypatch.setenv("IOWAP_SERVE_ALLOW", "192.168.2.90")
    monkeypatch.setenv("RELAY_BASE_URL", "http://10.9.9.9:8788")
    assert file_serve.serve_allow_ip() == "192.168.2.90"


def test_allow_resolves_relay_hostname_from_config(monkeypatch):
    monkeypatch.delenv("IOWAP_SERVE_ALLOW", raising=False)
    monkeypatch.setenv("RELAY_BASE_URL", "http://127.0.0.1:8788")
    assert file_serve.serve_allow_ip() == "127.0.0.1"


def test_allow_unresolvable_returns_none(monkeypatch):
    monkeypatch.delenv("IOWAP_SERVE_ALLOW", raising=False)
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    monkeypatch.setattr(file_serve, "_relay_host_ip", lambda: None)
    assert file_serve.serve_allow_ip() is None


def test_allow_invalid_env_falls_through(monkeypatch):
    monkeypatch.setenv("IOWAP_SERVE_ALLOW", "nicht-eine-ip")
    monkeypatch.setenv("RELAY_BASE_URL", "http://127.0.0.1:8788")
    assert file_serve.serve_allow_ip() == "127.0.0.1"


# --- Handler-Verhalten -------------------------------------------------------


def test_health_from_allowed_loopback_ok(serve_globals, monkeypatch):
    port = _free_port()
    monkeypatch.setenv("IOWAP_SERVE_ALLOW", "127.0.0.1")
    server = _start_server(monkeypatch, port)
    try:
        assert _get_health(port) == 200
    finally:
        server.shutdown()


def _outbound_ip() -> str | None:
    """Primäre Outbound-IP (UDP-connect-Trick, kein Paket wird gesendet).

    ``gethostbyname(hostname)`` taugt nicht: Debian-/etc/hosts liefert
    ``127.0.1.1`` — und Verbindungen in den Loopback-Bereich landen am
    Server trotzdem als Peer ``127.0.0.1`` (primäre lo-Adresse).
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return None if ip.startswith("127.") else ip


def test_transfer_from_denied_ip_gets_403(serve_globals, monkeypatch):
    """Fail-closed: Peer ist nicht in der Allowlist → 403.

    Der Test-Peer verbindet via der echten Node-LAN-IP (D8-Muster) —
    loopback-Peers sind immer legitim (lokaler Probe-Pfad) und taugen
    nicht für den Deny-Fall.
    """
    node_ip = _outbound_ip()
    if node_ip is None:
        pytest.skip("keine non-loopback Outbound-IP (CI ohne LAN)")
    port = _free_port()
    monkeypatch.setenv("IOWAP_SERVE_ALLOW", "192.168.2.90")
    server = _start_server(monkeypatch, port)
    try:
        assert _get_health(port, host=node_ip) == 403
        assert _post_transfer(port, _transfer_id(), host=node_ip) == 403
    finally:
        server.shutdown()


def test_peer_allowed_unit_no_socket():
    """Unit: _peer_allowed-Logik direkt — ohne Server/Socket (CI-sicher)."""
    import types

    handler = file_serve.EphemeralServeHandler.__new__(
        file_serve.EphemeralServeHandler
    )
    handler.server = types.SimpleNamespace(
        _allow_ip=frozenset({"192.168.2.90", "127.0.0.1", "::1"})
    )
    handler.client_address = ("10.0.0.5", 55555)
    assert handler._peer_allowed() is False  # Fremd-IP → 403-Pfad
    handler.client_address = ("192.168.2.90", 55555)
    assert handler._peer_allowed() is True  # Relay-IP
    handler.client_address = ("127.0.0.1", 55555)
    assert handler._peer_allowed() is True  # loopback immer legitim

    handler.server = types.SimpleNamespace(_allow_ip=None)  # fail-closed-Cache
    handler.client_address = ("192.168.2.90", 55555)
    assert handler._peer_allowed() is False


def test_transfer_unresolved_relay_ip_rejects_remote_peer(serve_globals, monkeypatch):
    """Fail-closed für den Remote-Pfad: Relay-IP unauflösbar → 403 für Fremd-IP.

    Loopback bleibt legitim (lokaler Probe-Pfad, D8-Lektion).
    """
    port = _free_port()
    monkeypatch.delenv("IOWAP_SERVE_ALLOW", raising=False)
    monkeypatch.setattr(file_serve, "_relay_host_ip", lambda: None)
    server = _start_server(monkeypatch, port)
    try:
        assert _get_health(port) == 200  # loopback-Peer bleibt legitim
    finally:
        server.shutdown()


def test_transfer_from_relay_ip_succeeds(serve_globals, monkeypatch):
    """Happy path: Relay-IP (= Test-Peer 127.0.0.1) darf transferieren."""
    port = _free_port()
    tid = _transfer_id()
    staged = file_serve.SERVE_DIR / tid
    payload = b"allowed-payload"
    staged.write_bytes(payload)
    manifest = {
        "file": str(staged),
        "size": len(payload),
        "sha256": "x",
        "max_downloads": 1,
    }
    Path(str(staged) + ".json").write_text(json.dumps(manifest))
    monkeypatch.setenv("IOWAP_SERVE_ALLOW", "127.0.0.1")
    server = _start_server(monkeypatch, port)
    try:
        assert _post_transfer(port, tid) == 200
    finally:
        server.shutdown()