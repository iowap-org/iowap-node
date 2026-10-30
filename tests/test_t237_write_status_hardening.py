"""T-237: ``_write_status`` must never kill the heartbeat thread.

Audit F1: both daemons call ``_write_status`` OUTSIDE the heartbeat
loop's ``try`` (node_cli.py:266 / node_daemon.py:354) and the first
statement of ``_write_status`` itself was the unguarded
``load_active_profile()``. A broken active profile therefore raised
``CapabilityValidationError`` into the thread body → the heartbeat
thread died silently, the process stayed alive, the node sat offline
at the relay and the status file kept reporting the stale last tick.
``node-cli daemon run`` also wrote the status file before the thread
start (node_cli.py:457) — the same broken profile aborted the whole
daemon startup with a traceback.

Fix contract (pinned here):

* ``_write_status`` survives a broken active profile: capabilities
  fall back to ``[]`` and the error is reported in the status file.
* the heartbeat loop survives even when ``_write_status`` itself
  raises — the write is guarded and the loop keeps ticking.
* the startup status write before the thread start survives too.

Thread-survival tests follow the real ``_run_one_heartbeat`` pattern
from tests/test_t233_cli_daemon_forward_slots.py (thread started and
joined — deliberately NOT the vacuum-green stop-before-loop variant
of tests/test_t233_cli_daemon_forward_slots.py:111-114, which T-238
repairs).
"""

from __future__ import annotations

import threading
import time

import pytest

from nodes.common import node_cli, node_daemon
from nodes.common.node_config import CapabilityValidationError
from nodes.common.relay_client import RelayClient

META = {
    "node_id": "237NODE",
    "node_name": "write-status-hardening",
    "registration_secret": "rs_x",
    "base_url": "http://relay.test:8788",
}
CFG = {"base_url": None, "request_timeout": 5, "heartbeat_interval": 1}
PROFILE_MSG = "capability 'x': missing required field 'handler'"


def _broken_profile():
    def boom() -> list[dict]:
        raise CapabilityValidationError(PROFILE_MSG)

    return boom


@pytest.fixture()
def client() -> RelayClient:
    return RelayClient(dict(META), dict(CFG))


def _run_until_n_tick_errors(daemon, monkeypatch, caplog, want: int) -> None:
    """Run the real heartbeat loop thread until it has ticked `want` times.

    A tick is observed via the loop's per-tick error log (the broken
    profile aborts the tick BEFORE the heartbeat POST — capabilities stay
    unsent, the relay never receives half data; the loop logs
    ``heartbeat error``). Not vacuum-green: pre-fix the thread dies inside
    the first tick's ``_write_status`` (call site sits outside the loop's
    try), so the second tick never happens.
    """
    monkeypatch.setattr(daemon.client, "heartbeat", lambda *a, **k: {"status": "ok"})
    monkeypatch.setattr(daemon.client, "maintenance_due", lambda: False)
    thread = threading.Thread(target=daemon._heartbeat_loop, daemon=True)
    thread.start()

    def tick_errors() -> int:
        return sum(
            "heartbeat error" in rec.getMessage() and PROFILE_MSG in rec.getMessage()
            for rec in caplog.records
        )

    deadline = time.monotonic() + 20
    while tick_errors() < want and time.monotonic() < deadline:
        time.sleep(0.2)
    daemon._stop_event.set()
    thread.join(timeout=5)

    ticks = tick_errors()
    assert not thread.is_alive()
    assert ticks >= want, (
        f"only {ticks}/{want} ticks completed — the heartbeat thread died "
        "before its next tick (_write_status killed it, T-237)"
    )


# --- node-cli daemon ----------------------------------------------------------


def test_cli_heartbeat_thread_survives_invalid_active_profile(
    client, monkeypatch, tmp_path, caplog
):
    """A broken active profile must not take the heartbeat thread down."""
    monkeypatch.setattr(node_cli, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(node_cli, "load_forward_slots", list)
    daemon = node_cli.Daemon(client, dict(CFG))
    monkeypatch.setattr(node_cli, "load_active_profile", _broken_profile())

    _run_until_n_tick_errors(daemon, monkeypatch, caplog, want=2)


def test_cli_status_file_reports_profile_error_with_empty_caps(
    client, monkeypatch, tmp_path
):
    """With a broken profile the status snapshot still lands on disk —
    capabilities fall back to ``[]`` and the error is reported."""
    monkeypatch.setattr(node_cli, "STATUS_PATH", tmp_path / "status.json")
    daemon = node_cli.Daemon(client, dict(CFG))
    monkeypatch.setattr(node_cli, "load_active_profile", _broken_profile())

    daemon._write_status()  # must not raise (T-237)

    import json

    status = json.loads((tmp_path / "status.json").read_text())
    assert status["capabilities"] == []
    assert "active profile invalid" in (status["error"] or "")
    assert "missing required field" in (status["error"] or "")


def test_cli_heartbeat_loop_survives_status_write_failure(
    client, monkeypatch, tmp_path
):
    """Even when _write_status itself raises, the loop keeps ticking."""

    def explode(error=None):
        raise RuntimeError("status disk gone")

    monkeypatch.setattr(node_cli, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(node_cli, "load_forward_slots", list)
    daemon = node_cli.Daemon(client, dict(CFG))
    monkeypatch.setattr(daemon, "_write_status", explode)

    ticks: list[int] = []
    reached = threading.Event()

    def fake_heartbeat(caps, inflight, forward_slots=None):
        ticks.append(1)
        if len(ticks) >= 2:
            reached.set()
            daemon._stop_event.set()
        return {"status": "ok"}

    monkeypatch.setattr(client, "heartbeat", fake_heartbeat)
    monkeypatch.setattr(client, "maintenance_due", lambda: False)

    thread = threading.Thread(target=daemon._heartbeat_loop, daemon=True)
    thread.start()
    assert reached.wait(timeout=15), (
        "heartbeat loop died after the first _write_status failure (T-237)"
    )
    daemon._stop_event.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(ticks) >= 2


# --- node-daemon (SSE) parity ---------------------------------------------------


def test_sse_heartbeat_thread_survives_invalid_active_profile(
    client, monkeypatch, tmp_path, caplog
):
    """Same contract for the SSE daemon's heartbeat loop."""
    monkeypatch.setattr(node_daemon, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(node_daemon, "load_forward_slots", list)
    daemon = node_daemon.SseDaemon(client, dict(CFG))
    monkeypatch.setattr(node_daemon, "load_active_profile", _broken_profile())

    _run_until_n_tick_errors(daemon, monkeypatch, caplog, want=2)


def test_sse_status_file_reports_profile_error_with_empty_caps(
    client, monkeypatch, tmp_path
):
    monkeypatch.setattr(node_daemon, "STATUS_PATH", tmp_path / "status.json")
    daemon = node_daemon.SseDaemon(client, dict(CFG))
    monkeypatch.setattr(node_daemon, "load_active_profile", _broken_profile())

    daemon._write_status()  # must not raise (T-237)

    import json

    status = json.loads((tmp_path / "status.json").read_text())
    assert status["capabilities"] == []
    assert "active profile invalid" in (status["error"] or "")