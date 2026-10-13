"""T-166: hp put/get bridge branches — contracts (phase 3 integrate).

Filled bodies of the phase-2 stubs; test names and intents unchanged
(plan tasks 4–6, FROZEN decisions from plan "Phase 1"):

* F5:  CLI→daemon hand-off (serve.json discovery, staging) — without
       a reachable serve: ``hp put`` exit 1 with the FROZEN message.
* F6:  ephemerality (max_downloads=1, the daemon cleans up + unregisters).
* F7:  the receiver pulls via the proxy URL (``file_serve.proxy_url``),
       Bearer, 64-KB stream — and NEVER calls ``unregister_temp_route``.
       Pull with POST + empty body (the proxy matches the registered
       method exactly).
* F8:  sha256 on the sender side in manifest AND envelope.
* F9:  cmd_put/cmd_get signatures, stdout formats, exit codes (0/1/2)
       stay FROZEN (T-179) — only the bridge branches get life.

Technique: FakeClient/MagicMock like tests/test_t179_cli_hp.py (only
sys.path bootstrap via conftest); isolation via module constants (D7):
SERVE_DIR/MANIFEST_PATH at call time → monkeypatch to tmp_path, without
a real ~/.relay. Happy paths mock ``file_serve.probe_serve`` (no real
daemon); get uses an ``httpx.MockTransport`` injected via a cli_hp-local
httpx proxy (spy on the request).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from nodes.common import file_serve
from nodes.common.cli import cli_hp

# --- Isolation (D7) ------------------------------------------------------------


@pytest.fixture
def client() -> MagicMock:
    """FakeClient-Basis: base_url + token + sender node_id (storage_ref F4)."""
    c = MagicMock()
    c.base_url = "http://relay.test"
    c.meta = {"node_id": "E4W3CBWQ"}
    c.token = "tok_test"
    return c


@pytest.fixture
def serve_dir(tmp_path: Path, monkeypatch) -> Path:
    """SERVE_DIR auf tmp_path umlenken (staged files + F6-Manifeste)."""
    d = tmp_path / "serve"
    monkeypatch.setattr(file_serve, "SERVE_DIR", d)
    return d


@pytest.fixture
def manifest_path(tmp_path: Path, monkeypatch) -> Path:
    """MANIFEST_PATH (serve.json) auf tmp_path umlenken (F5-Discovery)."""
    p = tmp_path / "serve.json"
    monkeypatch.setattr(file_serve, "MANIFEST_PATH", p)
    return p


def _modes(monkeypatch, modes: list[str]) -> None:
    """Patch cli_hp._load_capability_modes (F2.11 Monkeypatch-Surface)."""
    monkeypatch.setattr(
        cli_hp, "_load_capability_modes", lambda c, cap: {"upload_modes": modes}
    )


def _install_transport(monkeypatch, handler) -> list[httpx.Request]:
    """Repoint httpx.stream in the cli_hp path to a MockTransport.

    Returns the built requests (spy for the POST/URL/header contract).
    The proxy is set only for cli_hp (``cli_hp.httpx``) — the rest of the
    process keeps using the real httpx.
    """
    seen: list[httpx.Request] = []
    transport = httpx.MockTransport(handler)

    class _HttpxProxy:
        """Attribut-Proxy: alles echte httpx, nur ``stream`` gemockt."""

        def __getattr__(self, name: str):
            return getattr(httpx, name)

        def stream(self, method: str, url: str, **kwargs):
            c = httpx.Client(transport=transport)
            request = c.build_request(method, url, **kwargs)
            seen.append(request)

            class _CM:
                def __enter__(self):
                    self._response = c.send(request)
                    return self._response

                def __exit__(self, *exc):
                    self._response.close()
                    c.close()
                    return False

            return _CM()

    monkeypatch.setattr(cli_hp, "httpx", _HttpxProxy())
    return seen


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    """Sync with the serve request thread: counting happens AFTER the send."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# Task 4 — cmd_put bridge-Zweig
# ---------------------------------------------------------------------------


def test_put_bridge_happy_path_via_fake_client(
    tmp_path, client, serve_dir, manifest_path, monkeypatch, capsys
):
    payload = b"bridge-payload" * 10
    f = tmp_path / "big.bin"
    f.write_bytes(payload)
    file_serve.write_manifest(9123)  # F5: the daemon wrote serve.json
    monkeypatch.setattr(
        file_serve, "probe_serve", lambda port, timeout=2.0, host=None: True
    )
    _modes(monkeypatch, ["bridge"])
    client.get_transfer_config.return_value = {}  # Stufen-Konfig (ladder-frei)

    rc = cli_hp.cmd_put(client, cap="cap.x", path=f)

    assert rc == 0
    # register_temp_route: path (allowlist), POST, upstream points at the serve.
    client.register_temp_route.assert_called_once()
    args = client.register_temp_route.call_args.args
    kwargs = client.register_temp_route.call_args.kwargs
    route_path = args[0]
    assert route_path.startswith("/download/")
    transfer_id = route_path.removeprefix("/download/")
    assert len(transfer_id) == 22  # F2 — url-safe transfer id
    assert args[1] == "POST"  # the serve channel is POST-only
    assert args[2] == f"http://127.0.0.1:9123/transfer/{transfer_id}"  # upstream
    assert kwargs["ttl_seconds"] == 3600
    assert kwargs["channel_id"] == transfer_id
    assert kwargs["description"] == "iowap ephemeral file serve (T-166)"
    # Staged: echte Bytes, Quelle weg (Move-Semantik F5), F6-Manifest exakt.
    staged = serve_dir / transfer_id
    assert staged.read_bytes() == payload
    assert not f.exists()
    manifest = json.loads((serve_dir / f"{transfer_id}.json").read_text())
    assert manifest == {
        "file": transfer_id,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "max_downloads": 1,
    }
    # stdout EXACTLY ONE line: envelope src="bridge" with F4 ref + F8 sha256.
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    envelope = json.loads(out)
    ref = envelope["__iowap_ref__"]
    assert ref["src"] == "bridge"
    assert ref["sha256"] == hashlib.sha256(payload).hexdigest()
    assert ref["filename"] == "big.bin"
    storage = ref["storage_ref"]
    assert storage == {
        "type": "node_serve",
        "node_id": "E4W3CBWQ",
        "path": f"/download/{transfer_id}",
        "expires_at": storage["expires_at"],  # Struktur-Check unten
    }
    datetime.fromisoformat(storage["expires_at"])  # F4: ISO-parseable


def test_put_bridge_serve_json_missing_returns_exit_1(
    tmp_path, client, serve_dir, manifest_path, monkeypatch, capsys
):
    # F5 — serve.json missing (normal case without a daemon): exit 1, FROZEN wording,
    # nothing staged, no register, no envelope.
    f = tmp_path / "blob.bin"
    f.write_bytes(b"0123456789")
    _modes(monkeypatch, ["bridge"])

    rc = cli_hp.cmd_put(client, cap="cap.x", path=f)

    assert rc == 1
    ro = capsys.readouterr()
    assert ro.err.strip() == (
        "hp put: ephemeral serve not reachable (is the node daemon "
        "running?) — use artifact fallback"
    )
    assert ro.out == ""
    assert not serve_dir.exists()  # NOTHING staged
    client.register_temp_route.assert_not_called()
    assert f.exists()  # source untouched


def test_put_bridge_register_http_error_returns_exit_1_and_deletes_staged(
    tmp_path, client, serve_dir, manifest_path, monkeypatch, capsys
):
    payload = b"rollback-me"
    f = tmp_path / "blob.bin"
    f.write_bytes(payload)
    file_serve.write_manifest(9123)
    monkeypatch.setattr(
        file_serve, "probe_serve", lambda port, timeout=2.0, host=None: True
    )
    _modes(monkeypatch, ["bridge"])
    client.register_temp_route.side_effect = httpx.ConnectError("relay down")
    client.get_transfer_config.return_value = {}  # Stufen-Konfig (ladder-frei)

    rc = cli_hp.cmd_put(client, cap="cap.x", path=f)

    assert rc == 1
    ro = capsys.readouterr()
    assert ro.err.startswith("hp put: route registration failed:")
    assert ro.out == ""  # no envelope on a failed registration
    # no stale leftovers without the route: staged file + F6 manifest deleted (F6).
    assert list(serve_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# Task 5 — cmd_get bridge-Zweig
# ---------------------------------------------------------------------------


def _bridge_envelope(filename: str, sha256: str | None = None) -> dict:
    """node_serve envelope (F4/F8) for receiver tests."""
    ref = file_serve.build_storage_ref(
        "E4W3CBWQ", "/download/abcDEF-_123", "2026-09-06T18:00:00+00:00"
    )
    return cli_hp.make_envelope(
        src="bridge", filename=filename, storage_ref=ref, sha256=sha256
    )


def test_get_bridge_happy_path_via_mock_transport(
    tmp_path, client, manifest_path, monkeypatch, capsys
):
    payload = b"bridge-payload"  # 14 bytes
    env = _bridge_envelope(
        "recv.bin", sha256=hashlib.sha256(payload).hexdigest()
    )
    seen = _install_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            content=payload,
            headers={"Content-Length": str(len(payload))},
        ),
    )
    out_dir = tmp_path / "out"

    rc = cli_hp.cmd_get(client, envelope=env, out_dir=out_dir)

    assert rc == 0
    # request contract (F7/F3): POST (the proxy matches the method exactly), empty
    # body, proxy URL (F7 shape), Bearer token.
    assert len(seen) == 1
    req = seen[0]
    assert req.method == "POST"
    assert req.content == b""
    assert str(req.url) == file_serve.proxy_url(
        "http://relay.test", "E4W3CBWQ", "/download/abcDEF-_123"
    )
    assert req.headers["Authorization"] == f"Bearer {client.token}"
    # file byte-identical, the sha256 verify ran (F8), stdout FROZEN keys.
    target = out_dir / "recv.bin"
    assert target.read_bytes() == payload
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "path": str(target),
        "size_bytes": len(payload),
        "src": "bridge",
    }


def test_get_bridge_incomplete_storage_ref_returns_exit_1(
    tmp_path, client, manifest_path, capsys
):
    # guard BEFORE any HTTP (no pull attempt) + no unregister (F7).
    spy = MagicMock()
    client.unregister_temp_route = spy
    bad_refs = [
        {"type": "channel", "id": "ch_1"},  # fremder Typ
        {"type": "node_serve"},  # node_id/path fehlen
        {"type": "node_serve", "node_id": "N1"},  # path fehlt
    ]

    for bad in bad_refs:
        env = cli_hp.make_envelope(
            src="bridge", filename="x.bin", storage_ref=bad
        )
        rc = cli_hp.cmd_get(client, envelope=env, out_dir=tmp_path / "out")
        assert rc == 1, bad

    err = capsys.readouterr().err
    for line in err.strip().splitlines():
        assert line == (
            "hp get: bridge storage_ref missing node_serve fields "
            "(node_id/path)"
        )
    spy.assert_not_called()


def test_get_bridge_http_error_returns_exit_1_and_deletes_partial_file(
    tmp_path, client, manifest_path, monkeypatch, capsys
):
    env = _bridge_envelope("recv.bin")
    _install_transport(
        monkeypatch, lambda request: httpx.Response(500, text="boom")
    )
    out_dir = tmp_path / "out"

    rc = cli_hp.cmd_get(client, envelope=env, out_dir=out_dir)

    assert rc == 1
    assert capsys.readouterr().err.startswith("hp get: bridge pull failed:")
    assert not (out_dir / "recv.bin").exists()  # no half-written files
    client.unregister_temp_route.assert_not_called()  # the receiver is never the owner


def test_get_bridge_receiver_does_not_unregister(
    tmp_path, client, manifest_path, monkeypatch, capsys
):
    payload = b"no-owner-actions"
    env = _bridge_envelope(
        "keep.bin", sha256=hashlib.sha256(payload).hexdigest()
    )
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            content=payload,
            headers={"Content-Length": str(len(payload))},
        ),
    )
    spy = MagicMock()
    client.unregister_temp_route = spy

    rc = cli_hp.cmd_get(client, envelope=env, out_dir=tmp_path / "out")

    assert rc == 0
    spy.assert_not_called()  # F7: the route belongs to the sender daemon


# ---------------------------------------------------------------------------
# Task 6 — Roundtrip-Integrationstest (in-process e2e)
# ---------------------------------------------------------------------------


@pytest.fixture
def serve_globals(monkeypatch):
    """Serve-Laufzeit isolieren (wie tests/test_t166_file_serve.py)."""
    monkeypatch.setattr(file_serve, "_serve_thread", None)
    monkeypatch.setattr(file_serve, "_serve_server", None)
    monkeypatch.setattr(file_serve, "_ON_EXHAUSTED", None)
    monkeypatch.setattr(file_serve, "_last_sweep", 0.0)


def test_bridge_roundtrip_end_to_end(
    tmp_path, client, serve_dir, manifest_path, monkeypatch, capsys, serve_globals
):
    """Sender stages + registers → proxy pull → ephemerality proven.

    In-process e2e with the REAL ``EphemeralServeHandler`` (a
    ThreadingHTTPServer on port 0 instead of the daemon): cmd_put
    registers the route (capture), the MockTransport stand-in of the
    relay proxy forwards the receiver pull to the real serve server
    (POST, empty body — like the real proxy), and the on_exhausted hook
    spy proves the F6 chain (file gone + route unregistered — in the
    daemon ``unregister_after_transfer``).
    """
    payload = b"roundtrip-payload-" + bytes(range(64))
    f = tmp_path / "orig.bin"
    f.write_bytes(payload)
    _modes(monkeypatch, ["bridge"])  # bridge-Modus erzwingen (ladder-frei)

    # real serve server (the phase-3 handler) instead of the daemon.
    server = ThreadingHTTPServer(("127.0.0.1", 0), file_serve.EphemeralServeHandler)
    # D9 cache like in the daemon: loopback legitimate (the peer is loopback here).
    server._allow_ip = frozenset({"127.0.0.1", "::1"})  # type: ignore[attr-defined]
    serve_port = server.server_address[1]
    threading.Thread(
        target=server.serve_forever, daemon=True, name="serve-test"
    ).start()
    hook_calls: list[str] = []
    file_serve.set_on_exhausted(hook_calls.append)

    # sender side: serve.json points at the real serve server.
    file_serve.write_manifest(serve_port)
    try:
        captured: dict = {}

        def _register(route_path, method, upstream, **kwargs):
            captured["route_path"] = route_path
            captured["method"] = method
            captured["upstream"] = upstream
            captured.update(kwargs)
            return {"path": route_path, "method": method, "upstream": upstream}

        client.register_temp_route = _register
        rc_put = cli_hp.cmd_put(client, cap="cap.x", path=f)
        assert rc_put == 0
        put_out = capsys.readouterr().out
        envelope = json.loads(put_out)
        ref = envelope["__iowap_ref__"]["storage_ref"]
        assert ref["path"] == captured["route_path"]
        assert captured["upstream"] == (
            f"http://127.0.0.1:{serve_port}/transfer/"
            f"{captured['route_path'].removeprefix('/download/')}"
        )

        # receiver side: pull via a MockTransport that, like the relay proxy,
        # forwards the POST to the registered upstream.
        def _proxy(request: httpx.Request) -> httpx.Response:
            with httpx.Client() as upstream:
                resp = upstream.post(captured["upstream"])
                return httpx.Response(
                    resp.status_code,
                    content=resp.content,
                    headers={
                        "Content-Length": resp.headers.get(
                            "Content-Length", str(len(resp.content))
                        )
                    },
                )

        _install_transport(monkeypatch, _proxy)

        out_dir = tmp_path / "out"
        rc_get = cli_hp.cmd_get(client, envelope=envelope, out_dir=out_dir)
        assert rc_get == 0
        downloaded = (out_dir / "orig.bin").read_bytes()
        assert downloaded == payload  # byte-identisch (F5/F8-Kette)
        get_out = json.loads(capsys.readouterr().out)
        assert get_out["src"] == "bridge"
        assert get_out["size_bytes"] == len(payload)

        # F6 chain: exactly 1 download → staged file + manifest gone and the
        # hook (= daemon unregister) fired with the route.
        tid = captured["route_path"].removeprefix("/download/")
        assert _wait_until(lambda: not (serve_dir / tid).exists())
        assert _wait_until(lambda: hook_calls == [captured["route_path"]])
        assert not (serve_dir / f"{tid}.json").exists()
        # ephemeral: a second pull through the same proxy would be 404.
        probe = httpx.post(captured["upstream"])
        assert probe.status_code == 404
    finally:
        server.shutdown()
        server.server_close()