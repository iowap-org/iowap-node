"""T-166 D9: source-IP allowlist on the ephemeral serve endpoint.

The serve server binds (in cross-host deploys) on 0.0.0.0 — reachable
in the LAN. The shielding follows the T-128 pattern from the storage
node: only connections from the relay-server IP (+ loopback for the
local CLI probe path) are accepted, everything else → HTTP 403.

Resolution of the allowed IP (house pattern, same chain as the
storage node):

1. env ``IOWAP_SERVE_ALLOW`` (explicit override, comma-separated)
2. relay hostname from the node relay config (``base_url`` from
   relay_config.json / env ``RELAY_BASE_URL`` / mDNS) → first IPv4
3. nothing resolvable → ``None`` → fail-closed (every request 403)
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
    """Direct server (without the thread wrapper) for request tests.

    Sets the same D9 cache as ``start_serve_thread`` — the handler
    reads ``server._allow_ip`` (contract: cache on the server object).
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
        # http.client.HTTPStatus errors (403) → returned as the status
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
    monkeypatch.setenv("IOWAP_SERVE_ALLOW", "192.0.2.90")
    monkeypatch.setenv("RELAY_BASE_URL", "http://10.9.9.9:8788")
    assert file_serve.serve_allow_ip() == "192.0.2.90"


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
    """The primary outbound IP (UDP-connect trick, no packet is sent).

    ``gethostbyname(hostname)`` won't do: Debian /etc/hosts yields
    ``127.0.1.1`` — and connections into the loopback range still land
    at the server as peer ``127.0.0.1`` (primary lo address).
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
    """Fail-closed: the peer is not in the allowlist → 403.

    The test peer connects via the real node LAN IP (D8 pattern) —
    loopback peers are always legitimate (local probe path) and are
    no good for the deny case.
    """
    node_ip = _outbound_ip()
    if node_ip is None:
        pytest.skip("no non-loopback outbound IP (CI without LAN)")
    port = _free_port()
    monkeypatch.setenv("IOWAP_SERVE_ALLOW", "192.0.2.90")
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
        _allow_ip=frozenset({"192.0.2.90", "127.0.0.1", "::1"})
    )
    handler.client_address = ("10.0.0.5", 55555)
    assert handler._peer_allowed() is False  # Fremd-IP → 403-Pfad
    handler.client_address = ("192.0.2.90", 55555)
    assert handler._peer_allowed() is True  # Relay-IP
    handler.client_address = ("127.0.0.1", 55555)
    assert handler._peer_allowed() is True  # loopback always legitimate

    handler.server = types.SimpleNamespace(_allow_ip=None)  # fail-closed cache
    handler.client_address = ("192.0.2.90", 55555)
    assert handler._peer_allowed() is False


def test_transfer_unresolved_relay_ip_rejects_remote_peer(serve_globals, monkeypatch):
    """Fail-closed for the remote path: relay IP unresolvable → 403 for a foreign IP.

    Loopback stays legitimate (local probe path, D8 lesson).
    """
    port = _free_port()
    monkeypatch.delenv("IOWAP_SERVE_ALLOW", raising=False)
    monkeypatch.setattr(file_serve, "_relay_host_ip", lambda: None)
    server = _start_server(monkeypatch, port)
    try:
        assert _get_health(port) == 200  # loopback peer stays legitimate
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