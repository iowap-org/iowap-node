"""T-166: file_serve — Manifest/Staging/ID-Kontrakte (Phase 1, TDD-RED-first).

Phase 1 (architect) liefert Task 1 komplett UND pinnt alle FROZEN
Signaturen aus dem Plan via ``inspect.signature`` — Phase 2 (scaffold)
verifiziert gegen diese Pins, Phase 3 (integrate) füllt nur noch.

Test-Isolation: ``SERVE_DIR``/``MANIFEST_PATH`` sind Module-Konstanten,
die Funktionen lesen sie zur CALL-Zeit → per monkeypatch austauschbar,
ohne echtes ``~/.relay`` anzufassen.

Task-2/3-Skeletons (``EphemeralServeHandler``, ``start_serve_thread``)
werden nur auf Existenz + Signatur + NotImplementedError gepinnt —
echte HTTP-Tests kommen mit Phase 3 (Plan Task 2 modifiziert dieses
File).
"""
from __future__ import annotations

import errno
import hashlib
import inspect
import json
import os
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from nodes.common import file_serve

# --- Isolation ---------------------------------------------------------------


@pytest.fixture
def serve_dir(tmp_path: Path, monkeypatch) -> Path:
    """SERVE_DIR auf tmp_path umlenken (wird von stage_file selbst angelegt)."""
    d = tmp_path / "serve"
    monkeypatch.setattr(file_serve, "SERVE_DIR", d)
    return d


@pytest.fixture
def manifest_path(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "serve.json"
    monkeypatch.setattr(file_serve, "MANIFEST_PATH", p)
    return p


# --- FROZEN pins (Phase 2/3-Gate verifiziert gegen diese) ----------------------


def test_frozen_constants():
    # FROZEN-Block Plan T-166, byte-identisch.
    assert file_serve.SERVE_PORT_DEFAULT == 8792
    assert file_serve.SERVE_HOST == "127.0.0.1"
    assert file_serve.SERVE_DIR == Path.home() / ".relay" / "serve"
    assert file_serve.MANIFEST_PATH == Path.home() / ".relay" / "serve.json"


def test_frozen_signatures():
    # Annotationen zeigen gequotet (from __future__ import annotations) —
    # Haus-Konvention wie test_t179_cli_hp.py::test_cli_hp_signatures_unchanged.
    pins = {
        "serve_port": "() -> 'int'",
        "read_manifest": "() -> 'dict | None'",
        "write_manifest": "(port: 'int', host: 'str | None' = None) -> 'None'",
        "new_transfer_id": "() -> 'str'",
        # Phase-1-Präzisierung D5: max_downloads keyword-only, Default 1 —
        # call-kompatibel zum Plan-Snippet stage_file(path), s. Plan Phase 1.
        "stage_file": (
            "(src: 'Path', *, max_downloads: 'int' = 1) -> 'tuple[str, Path, str]'"
        ),
        "build_storage_ref": (
            "(node_id: 'str', route_path: 'str', expires_at: 'str') -> 'dict'"
        ),
        "proxy_url": "(base_url: 'str', node_id: 'str', route_path: 'str') -> 'str'",
        "probe_serve": (
            "(port: 'int', timeout: 'float' = 2.0, host: 'str | None' = None) -> 'bool'"
        ),
        "start_serve_thread": "() -> 'threading.Thread'",
        "unregister_after_transfer": (
            "(client: 'RelayClient', route_path: 'str', method: 'str' = 'POST')"
            " -> 'None'"
        ),
    }
    for name, expected in pins.items():
        assert str(inspect.signature(getattr(file_serve, name))) == expected, name


# --- Serve-Handler + start_serve_thread (Task 2/3, Phase 3) ---------------------
#
# Der Skeleton-Pin-Test (Phase 1) ist hier ersetzt: Phase 3 füllt die Körper,
# die Verträge stehen im Handler-/start_serve_thread-Docstring.


import time


@pytest.fixture
def serve_globals(monkeypatch):
    """Serve-Laufzeit isolieren: Threads/Server/Hook/Sweep-Zeitstempel."""
    monkeypatch.setattr(file_serve, "_serve_thread", None)
    monkeypatch.setattr(file_serve, "_serve_server", None)
    monkeypatch.setattr(file_serve, "_ON_EXHAUSTED", None)
    monkeypatch.setattr(file_serve, "_last_sweep", 0.0)


def _stage(serve_dir: Path, payload: bytes, max_downloads: int = 1):
    src = serve_dir.parent / "src.bin"
    src.write_bytes(payload)
    return file_serve.stage_file(src, max_downloads=max_downloads)


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    """Sync mit dem Request-Thread: Zählen/aufräumen passiert NACH dem Senden."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_serve_post_streams_staged_file(serve_dir, serve_globals):
    payload = b"bridge-payload-123"
    tid, _, _ = _stage(serve_dir, payload)
    server, port = _spawn(file_serve.EphemeralServeHandler)
    try:
        r = httpx.post(f"http://127.0.0.1:{port}/transfer/{tid}", content=b"")
        assert r.status_code == 200
        assert r.content == payload
        assert r.headers["Content-Length"] == str(len(payload))
        assert r.headers["Content-Type"] == "application/octet-stream"
    finally:
        server.shutdown()
        server.server_close()


def test_serve_post_unknown_or_malformed_id_404(serve_dir, serve_globals):
    server, port = _spawn(file_serve.EphemeralServeHandler)
    try:
        unknown = httpx.post(
            f"http://127.0.0.1:{port}/transfer/{'a' * 22}", content=b""
        )
        assert unknown.status_code == 404
        malformed = httpx.post(
            f"http://127.0.0.1:{port}/transfer/too-short", content=b""
        )
        assert malformed.status_code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_serve_max_downloads_exhausts_and_fires_hook(serve_dir, serve_globals):
    payload = b"once-only"
    tid, staged, _ = _stage(serve_dir, payload)
    hook_calls: list[str] = []
    file_serve.set_on_exhausted(hook_calls.append)
    server, port = _spawn(file_serve.EphemeralServeHandler)
    url = f"http://127.0.0.1:{port}/transfer/{tid}"
    try:
        r = httpx.post(url, content=b"")
        assert r.status_code == 200
        assert r.content == payload
        # F6: Datei + Manifest weg, Hook mit route_path gefeuert — Zählen
        # passiert nach dem Senden → poll-sync mit dem Request-Thread.
        assert _wait_until(lambda: not staged.exists())
        assert _wait_until(lambda: hook_calls == [f"/download/{tid}"])
        assert not (serve_dir / f"{tid}.json").exists()
        # Ephemeral bewiesen: zweiter Pull ist 404.
        r2 = httpx.post(url, content=b"")
        assert r2.status_code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_serve_health_and_post_only_channel(serve_globals):
    server, port = _spawn(file_serve.EphemeralServeHandler)
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/health")
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert httpx.get(f"http://127.0.0.1:{port}/other").status_code == 404
        # GET auf /transfer → 404 (POST-only; Proxy matcht Methode exakt).
        assert (
            httpx.get(f"http://127.0.0.1:{port}/transfer/{'a' * 22}").status_code
            == 404
        )
    finally:
        server.shutdown()
        server.server_close()


def test_serve_post_body_ignored(serve_dir, serve_globals):
    payload = b"body-must-not-matter"
    tid, _, _ = _stage(serve_dir, payload)
    server, port = _spawn(file_serve.EphemeralServeHandler)
    try:
        r = httpx.post(
            f"http://127.0.0.1:{port}/transfer/{tid}", content=b"garbage"
        )
        assert r.status_code == 200
        assert r.content == payload
    finally:
        server.shutdown()
        server.server_close()


def test_serve_count_persists_between_downloads(serve_dir, serve_globals):
    payload = b"two-downloads"
    tid, staged, _ = _stage(serve_dir, payload, max_downloads=2)
    server, port = _spawn(file_serve.EphemeralServeHandler)
    url = f"http://127.0.0.1:{port}/transfer/{tid}"
    try:
        r1 = httpx.post(url, content=b"")
        assert r1.status_code == 200
        # Download-Zählen passiert nach dem Senden (Request-Thread) → poll-sync.
        manifest_file = serve_dir / f"{tid}.json"
        assert _wait_until(
            lambda: manifest_file.exists()
            and json.loads(manifest_file.read_text()).get("downloads") == 1
        )
        manifest = json.loads(manifest_file.read_text())
        assert manifest["downloads"] == 1
        r2 = httpx.post(url, content=b"")
        assert r2.status_code == 200
        assert _wait_until(lambda: not staged.exists())
        r3 = httpx.post(url, content=b"")
        assert r3.status_code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_sweep_removes_stale_files(serve_dir, serve_globals):
    serve_dir.mkdir(parents=True, exist_ok=True)
    old_ts = time.time() - 7200
    stale_file = serve_dir / "stalefile"
    stale_file.write_bytes(b"x")
    os.utime(stale_file, (old_ts, old_ts))
    stale_manifest = serve_dir / ("d" * 22 + ".json")
    stale_manifest.write_text("{}")
    os.utime(stale_manifest, (old_ts, old_ts))
    fresh = serve_dir / "fresh.bin"
    fresh.write_bytes(b"y")

    file_serve._sweep_stale_transfers()

    assert not stale_file.exists()
    assert not stale_manifest.exists()
    assert fresh.exists()


def test_sweep_rate_limited(serve_dir, serve_globals):
    serve_dir.mkdir(parents=True, exist_ok=True)
    file_serve._last_sweep = time.time()  # gerade gesweept
    stale_file = serve_dir / "stale.bin"
    stale_file.write_bytes(b"x")
    old_ts = time.time() - 7200
    os.utime(stale_file, (old_ts, old_ts))

    file_serve._sweep_stale_transfers()

    assert stale_file.exists()  # Intervall nicht erreicht → nichts getan


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_start_serve_thread_binds_writes_manifest_idempotent(
    serve_dir, serve_globals, manifest_path, monkeypatch
):
    port = _free_port()
    monkeypatch.setenv("IOWAP_SERVE_PORT", str(port))
    try:
        t1 = file_serve.start_serve_thread()
        assert t1.is_alive() and t1.daemon
        assert file_serve.read_manifest() == {"port": port, "host": "127.0.0.1"}
        assert file_serve.probe_serve(port) is True
        t2 = file_serve.start_serve_thread()
        assert t2 is t1  # Idempotenz — kein Doppel-Bind
    finally:
        if file_serve._serve_server is not None:
            file_serve._serve_server.shutdown()
            file_serve._serve_server.server_close()


def test_start_serve_thread_bind_fail_raises_no_manifest(
    serve_dir, serve_globals, manifest_path, monkeypatch
):
    port = _free_port()
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        monkeypatch.setenv("IOWAP_SERVE_PORT", str(port))
        with pytest.raises(RuntimeError, match="bind failed"):
            file_serve.start_serve_thread()
    assert not manifest_path.exists()  # kein Manifest bei Bind-Fehl


# --- serve_port ---------------------------------------------------------------


def test_serve_port_default_without_env(monkeypatch):
    monkeypatch.delenv("IOWAP_SERVE_PORT", raising=False)
    assert file_serve.serve_port() == 8792


def test_serve_port_env_override(monkeypatch):
    monkeypatch.setenv("IOWAP_SERVE_PORT", "9123")
    assert file_serve.serve_port() == 9123


def test_serve_port_invalid_env_falls_back(monkeypatch):
    # Haus-Pattern wie relay_client._effective_config: WARNING + Default,
    # kein harter Absturz wegen eines Env-Tippfehlers.
    monkeypatch.setenv("IOWAP_SERVE_PORT", "not-a-port")
    assert file_serve.serve_port() == 8792


def test_serve_port_out_of_range_env_falls_back(monkeypatch):
    monkeypatch.setenv("IOWAP_SERVE_PORT", "70000")
    assert file_serve.serve_port() == 8792
    monkeypatch.setenv("IOWAP_SERVE_PORT", "0")
    assert file_serve.serve_port() == 8792


# --- D8: advertise-/bind-Host (IOWAP_SERVE_HOST) --------------------------------


def test_serve_host_default_without_env(monkeypatch):
    monkeypatch.delenv("IOWAP_SERVE_HOST", raising=False)
    assert file_serve.serve_host() == "127.0.0.1"


def test_serve_host_env_override(monkeypatch):
    # Live-Fall: Relay (LXC 903) dialt die LAN-IP des Nodes.
    monkeypatch.setenv("IOWAP_SERVE_HOST", "192.168.2.168")
    assert file_serve.serve_host() == "192.168.2.168"


def test_serve_host_invalid_env_falls_back(monkeypatch):
    # Haus-Pattern wie serve_port(): WARNING + Default, kein Absturz.
    monkeypatch.setenv("IOWAP_SERVE_HOST", "   ")
    assert file_serve.serve_host() == "127.0.0.1"
    monkeypatch.setenv("IOWAP_SERVE_HOST", "x" * 256)
    assert file_serve.serve_host() == "127.0.0.1"


def test_read_manifest_invalid_host_falls_back_to_default(manifest_path: Path):
    # Kaputter host-Wert im Manifest: Port bleibt verwertbar, host → Default.
    manifest_path.write_text('{"port": 8792, "host": 42}')
    assert file_serve.read_manifest() == {"port": 8792, "host": "127.0.0.1"}


# --- manifest (serve.json) roundtrip --------------------------------------------


def test_manifest_roundtrip(manifest_path: Path):
    file_serve.write_manifest(8792)
    assert manifest_path.exists()
    assert file_serve.read_manifest() == {"port": 8792, "host": "127.0.0.1"}


def test_manifest_roundtrip_with_host(manifest_path: Path):
    # D8: advertise-Host im Manifest — Daemon schreibt serve_host().
    file_serve.write_manifest(8792, host="192.168.2.168")
    assert file_serve.read_manifest() == {
        "port": 8792,
        "host": "192.168.2.168",
    }


def test_write_manifest_leaves_no_tmp(manifest_path: Path):
    # Atomar via tmp + os.replace: kein .tmp-Rest, kein torn read.
    file_serve.write_manifest(9123)
    assert not (manifest_path.parent / (manifest_path.name + ".tmp")).exists()
    assert file_serve.read_manifest() == {"port": 9123, "host": "127.0.0.1"}


def test_read_manifest_without_host_key_falls_back(manifest_path: Path):
    # Vor-D8-Manifeste ohne host-Key bleiben lesbar → Default.
    manifest_path.write_text('{"port": 8792}')
    assert file_serve.read_manifest() == {"port": 8792, "host": "127.0.0.1"}


def test_read_manifest_missing_returns_none(manifest_path: Path):
    # Der NORMALFALL ohne Daemon: serve.json fehlt → None (kein Raise).
    assert file_serve.read_manifest() is None


def test_read_manifest_invalid_json_returns_none(manifest_path: Path):
    manifest_path.write_text("{not json")
    assert file_serve.read_manifest() is None


def test_read_manifest_invalid_shape_returns_none(manifest_path: Path):
    # Getypte Garantie für cmd_put (Task 4): port muss int 1–65535 sein.
    for bad in (
        "[1, 2]",                  # kein dict
        '{"other": 1}',            # port fehlt
        '{"port": "8792"}',        # kein int
        '{"port": 0}',             # out of range
        '{"port": 70000}',         # out of range
        '{"port": true}',          # bool ist kein port (isinstance-Falle)
    ):
        manifest_path.write_text(bad)
        assert file_serve.read_manifest() is None, bad


# --- new_transfer_id ------------------------------------------------------------


def test_new_transfer_id_shape_and_uniqueness():
    ids = {file_serve.new_transfer_id() for _ in range(50)}
    assert len(ids) == 50  # kollisionsfrei über 50 Züge (Plan: min. 2×)
    for tid in ids:
        assert len(tid) == 22  # token_urlsafe(16) → 22 Zeichen (≤64-Registry-Limit)
        assert re.fullmatch(r"[A-Za-z0-9_-]{22}", tid)  # URL-safe


# --- stage_file ------------------------------------------------------------------


def test_stage_file_moves_hashes_and_manifest(serve_dir: Path, tmp_path: Path):
    payload = bytes(range(256)) * 4  # 1 KiB pseudo-random
    src = tmp_path / "big.bin"
    src.write_bytes(payload)

    transfer_id, staged, sha_hex = file_serve.stage_file(src)

    assert len(transfer_id) == 22
    assert staged == serve_dir / transfer_id
    assert staged.read_bytes() == payload
    assert not src.exists()  # Move-Semantik (F5) — Quelle ist weg
    assert sha_hex == hashlib.sha256(payload).hexdigest()
    # F6-Manifest, exakter Key-Set:
    manifest = json.loads((serve_dir / f"{transfer_id}.json").read_text())
    assert manifest == {
        "file": transfer_id,
        "size": len(payload),
        "sha256": sha_hex,
        "max_downloads": 1,
    }


def test_stage_file_copy_fallback_cross_fs(serve_dir: Path, tmp_path: Path, monkeypatch):
    # os.replace → OSError(EXDEV) NUR für den Quelldatei-Move (Quelle auf
    # fremdem FS); das atomare Manifest-Rename im SERVE_DIR bleibt echt.
    # shutil.copy + unlink-Fallback muss liefern — inkl. Move-Semantik.
    payload = b"cross-fs-payload" * 100
    src = tmp_path / "src.bin"
    src.write_bytes(payload)

    real_replace = os.replace

    def _exdev_replace(src_path, dst_path):
        if str(src_path) == str(src):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(src_path, dst_path)

    monkeypatch.setattr(os, "replace", _exdev_replace)

    transfer_id, staged, sha_hex = file_serve.stage_file(src)

    assert staged.read_bytes() == payload
    assert not src.exists()
    assert sha_hex == hashlib.sha256(payload).hexdigest()
    assert (serve_dir / f"{transfer_id}.json").exists()


def test_stage_file_max_downloads_param(serve_dir: Path, tmp_path: Path):
    # F6 --serve-count: Manifest trägt N (CLI validiert 1–10, Task 4).
    src = tmp_path / "n.bin"
    src.write_bytes(b"n")
    transfer_id, _, _ = file_serve.stage_file(src, max_downloads=5)
    manifest = json.loads((serve_dir / f"{transfer_id}.json").read_text())
    assert manifest["max_downloads"] == 5


def test_stage_file_rejects_max_downloads_below_one(serve_dir: Path, tmp_path: Path):
    # Ein 0-Download-Transfer wäre eine tote Route — fail-fast.
    src = tmp_path / "n.bin"
    src.write_bytes(b"n")
    with pytest.raises(ValueError):
        file_serve.stage_file(src, max_downloads=0)


# --- storage_ref / proxy_url Shapes ----------------------------------------------


def test_build_storage_ref_shape():
    # F4 FROZEN — Envelope v1 unverändert; parse_envelope gibt das dict roh zurück.
    ref = file_serve.build_storage_ref(
        "E4W3CBWQ", "/download/abcDEF-_123", "2026-09-06T18:00:00+00:00"
    )
    assert ref == {
        "type": "node_serve",
        "node_id": "E4W3CBWQ",
        "path": "/download/abcDEF-_123",
        "expires_at": "2026-09-06T18:00:00+00:00",
    }


def test_proxy_url_shape():
    # F7 — Pfad-Shape identisch zu unregister_temp_route (relay_client.py:594).
    url = file_serve.proxy_url("http://relay.test", "E4W3CBWQ", "/download/abcDEF-_123")
    assert url == (
        "http://relay.test/relay/v2/dashboard/api/node-routes/E4W3CBWQ/download/abcDEF-_123"
    )


def test_proxy_url_normalizes_trailing_slash_and_missing_leading_slash():
    assert file_serve.proxy_url("http://relay.test/", "N1", "/download/x") == (
        "http://relay.test/relay/v2/dashboard/api/node-routes/N1/download/x"
    )
    assert file_serve.proxy_url("http://relay.test", "N1", "download/x") == (
        "http://relay.test/relay/v2/dashboard/api/node-routes/N1/download/x"
    )


# --- probe_serve ------------------------------------------------------------------


class _HealthHandler(BaseHTTPRequestHandler):
    """Minimaler /health-Endpoint — derselbe Vertrag, den Task 2 liefert."""

    def do_GET(self):
        if self.path == "/health":
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):  # Test-Output stummhalten
        pass


class _Always404Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


def _spawn(handler_cls) -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def test_probe_serve_true_on_health_ok():
    server, port = _spawn(_HealthHandler)
    try:
        assert file_serve.probe_serve(port) is True
    finally:
        server.shutdown()
        server.server_close()


def test_probe_serve_false_on_non_200():
    # Contract: True gdw. HTTP 200 — alles andere ist „nicht erreichbar“.
    server, port = _spawn(_Always404Handler)
    try:
        assert file_serve.probe_serve(port) is False
    finally:
        server.shutdown()
        server.server_close()


def test_probe_serve_false_on_refused():
    # Freien Port besorgen und schließen → Connection refused → False, kein Raise.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert file_serve.probe_serve(port) is False


# --- unregister_after_transfer ------------------------------------------------------


def test_unregister_after_transfer_calls_client():
    client = MagicMock()
    file_serve.unregister_after_transfer(client, "/download/abc")
    client.unregister_temp_route.assert_called_once_with("/download/abc", method="POST")


def test_unregister_after_transfer_method_override():
    client = MagicMock()
    file_serve.unregister_after_transfer(client, "/download/abc", method="GET")
    client.unregister_temp_route.assert_called_once_with("/download/abc", method="GET")


def test_unregister_after_transfer_swallows_http_error():
    # Best-Effort (Plan F6/TTL-Backstop): HTTP-Fehler nur loggen, kein Raise —
    # ein gescheiterter Unregister darf den Download-Erfolg nicht brechen.
    client = MagicMock()
    client.unregister_temp_route.side_effect = httpx.ConnectError("boom")
    file_serve.unregister_after_transfer(client, "/download/abc")