"""T-166 Task 3: Daemon startet Ephemeral-Serve (F1) — Lifecycle-Tests.

Verträge:

* start()/run() verdrahtet den on_exhausted-Hook (F6: nach erschöpftem
  Transfer ruft der Daemon ``unregister_after_transfer(client, route_path)``
  — Best-Effort, HTTP-Fehler werden geloggt, nie geworfen) und startet den
  Serve-Thread; serve.json wird mit dem tatsächlich gebundenen Port
  geschrieben (F5-Discovery für ``hp put``).
* Bind-Fehl (Port belegt, F1): WARNING + kein Crash — SseDaemon.run()
  läuft regulär weiter, ``hp put bridge`` meldet später den F5-Fehler
  statt dass der Daemon stirbt.
* Idempotenz: zweiter start_serve_thread()-Aufruf → derselbe Thread.

run() blockiert im 0.5-s-Poll-Loop; der Test beendet es via ``stop()``
aus einem Timer-Thread. Signal-Handler werden durch monkeypatch der
Methode neutralisiert (kein Hauptthread-Zwang im Test).
"""
from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from nodes.common import file_serve
from nodes.common.node_daemon import SseDaemon


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def serve_globals(monkeypatch):
    """Serve-Laufzeit isolieren: Threads/Server/Hook/Sweep-Zeitstempel."""
    monkeypatch.setattr(file_serve, "_serve_thread", None)
    monkeypatch.setattr(file_serve, "_serve_server", None)
    monkeypatch.setattr(file_serve, "_ON_EXHAUSTED", None)
    monkeypatch.setattr(file_serve, "_last_sweep", 0.0)


@pytest.fixture
def daemon(tmp_path, monkeypatch) -> SseDaemon:
    """SseDaemon mit gemocktem Client; Status-Pfad auf tmp_path umgelenkt."""
    c = MagicMock()
    c.base_url = "http://relay.test"
    c.meta = {"node_id": "N1"}
    c.token = "tok"
    c._current_backoff.return_value = 0

    import nodes.common.node_daemon as nd

    monkeypatch.setattr(nd, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(nd, "load_active_profile", list, raising=False)
    monkeypatch.setattr(nd, "current_profile_name", lambda: "test")
    monkeypatch.setattr(
        nd, "write_json_atomic", lambda p, d: Path(p).write_text("{}")
    )
    monkeypatch.setattr(
        SseDaemon, "_install_signal_handlers", lambda self: None
    )
    return SseDaemon(c, {"heartbeat_interval": 3600})


def _stop_later(d: SseDaemon, delay: float) -> threading.Timer:
    t = threading.Timer(delay, d.stop)
    t.daemon = True
    t.start()
    return t


def _shutdown_serve() -> None:
    if file_serve._serve_server is not None:
        file_serve._serve_server.shutdown()
        file_serve._serve_server.server_close()


def test_daemon_start_starts_serve_and_writes_manifest(
    daemon, tmp_path, monkeypatch, serve_globals
):
    port = _free_port()
    monkeypatch.setenv("IOWAP_SERVE_PORT", str(port))
    monkeypatch.setattr(file_serve, "MANIFEST_PATH", tmp_path / "serve.json")
    try:
        timer = _stop_later(daemon, 0.4)
        daemon.run()

        # Serve-Thread läuft, Manifest mit gebundenem Port (F5-Discovery).
        assert file_serve._serve_thread is not None
        assert file_serve._serve_thread.is_alive()
        assert file_serve.read_manifest() == {"port": port}
        assert file_serve.probe_serve(port) is True
    finally:
        timer.cancel()
        _shutdown_serve()


def test_daemon_bind_failure_does_not_crash(
    daemon, tmp_path, monkeypatch, serve_globals
):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        monkeypatch.setenv("IOWAP_SERVE_PORT", str(port))
        monkeypatch.setattr(
            file_serve, "MANIFEST_PATH", tmp_path / "serve.json"
        )
        _stop_later(daemon, 0.4)
        daemon.run()  # darf nicht raisen
    assert file_serve._serve_thread is None  # Serve nie gestartet
    assert not (tmp_path / "serve.json").exists()  # kein Manifest bei Bind-Fehl


def test_daemon_hook_unregisters_after_exhausted_transfer(
    daemon, tmp_path, monkeypatch, serve_globals
):
    """F6-Kette: erschöpfter Transfer → Daemon unregistriert (Best-Effort).

    Startet den Daemon (echter Hook-Verdrahtung + Serve-Thread), staged per
    HTTP-POST einen Download bis ``max_downloads`` erreicht ist und prüft,
    dass der verdrahtete Hook ``unregister_after_transfer`` → Client aufruft.
    """
    serve_dir = tmp_path / "serve"
    serve_dir.mkdir()
    monkeypatch.setattr(file_serve, "SERVE_DIR", serve_dir)
    monkeypatch.setattr(file_serve, "MANIFEST_PATH", tmp_path / "serve.json")
    port = _free_port()
    monkeypatch.setenv("IOWAP_SERVE_PORT", str(port))

    staged = serve_dir / ("b" * 22)
    staged.write_bytes(b"hook-payload")
    manifest_file = serve_dir / ("b" * 22 + ".json")
    manifest_file.write_text(
        json.dumps(
            {
                "file": "b" * 22,
                "size": 12,
                "sha256": "0" * 64,
                "max_downloads": 1,
            }
        )
    )

    try:
        timer = _stop_later(daemon, 0.5)
        daemon.run()

        r = httpx.post(f"http://127.0.0.1:{port}/transfer/{'b' * 22}")
        assert r.status_code == 200
        # Zählen/Unregister passiert nach dem Senden → poll-sync.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not staged.exists() and not manifest_file.exists():
                break
            time.sleep(0.01)
        assert not staged.exists()
        assert not manifest_file.exists()
        # Hook-Kette: Daemon hat unregister_temp_route am Client aufgerufen.
        daemon.client.unregister_temp_route.assert_called_once_with(
            f"/download/{'b' * 22}", method="POST"
        )
    finally:
        timer.cancel()
        _shutdown_serve()


def test_daemon_hook_survives_unregister_error(
    daemon, tmp_path, monkeypatch, serve_globals
):
    """Best-Effort (F6): Unregister-Fehler → nur Log, kein Daemon-Crash."""
    daemon.client.unregister_temp_route.side_effect = httpx.ConnectError("x")
    hook_calls: list[str] = []
    file_serve.set_on_exhausted(
        lambda route_path: (
            hook_calls.append(route_path),
            file_serve.unregister_after_transfer(daemon.client, route_path),
        )[-1]
    )

    hook = file_serve._ON_EXHAUSTED
    assert hook is not None
    hook("/download/whatever")  # darf nicht raisen
    assert hook_calls == ["/download/whatever"]


def test_start_serve_thread_idempotent(serve_globals, tmp_path, monkeypatch):
    port = _free_port()
    monkeypatch.setenv("IOWAP_SERVE_PORT", str(port))
    monkeypatch.setattr(file_serve, "MANIFEST_PATH", tmp_path / "serve.json")
    try:
        t1 = file_serve.start_serve_thread()
        t2 = file_serve.start_serve_thread()
        assert t2 is t1
        assert t1.is_alive()
    finally:
        _shutdown_serve()