"""T-233: node-cli daemon must heartbeat forward_slots like the SSE daemon.

Before T-233 ``node_cli.Daemon._heartbeat_loop`` called
``client.heartbeat(caps, inflight)`` without forward_slots, so every
daemon started via ``node-cli daemon start`` (the fleet default) silently
reported **no** declared forward slots even when ``node.yaml`` carried a
``forward_slots`` block. Server-side the table stayed empty and forward
routing for that node failed at ``submit`` time (422 forward_slot_invalid).

These tests pin the CLI daemon heartbeat payload so it matches the SSE
daemon contract from ``node_daemon.py`` (T-Forward-Node-2).
"""

from __future__ import annotations

import threading
import time

import pytest

from nodes.common import node_cli
from nodes.common.relay_client import RelayClient

META = {
    "node_id": "233NODE",
    "node_name": "forward-slot-parity",
    "registration_secret": "rs_x",
    "base_url": "http://relay.test:8788",
}
CFG = {"base_url": None, "request_timeout": 5, "heartbeat_interval": 1}


@pytest.fixture()
def client() -> RelayClient:
    return RelayClient(dict(META), dict(CFG))


def _run_one_heartbeat(daemon: node_cli.Daemon) -> None:
    thread = threading.Thread(target=daemon._heartbeat_loop, daemon=True)
    thread.start()
    thread.join(timeout=3)
    assert not thread.is_alive()


def test_cli_daemon_heartbeat_loads_and_sends_forward_slots(client, monkeypatch):
    """node-cli daemon heartbeat passes the declared forward_slots from
    node.yaml into RelayClient.heartbeat — same contract as the SSE daemon."""
    daemon = node_cli.Daemon(client, dict(CFG))
    monkeypatch.setattr(client, "maintenance_due", lambda: False)
    monkeypatch.setattr(
        node_cli,
        "load_forward_slots",
        lambda: [{"slot": "1", "label": "Parity Slot", "description": "t233"}],
    )

    seen: list[list[dict[str, str]] | None] = []

    def fake_heartbeat(caps, inflight, forward_slots=None):
        seen.append(forward_slots)
        daemon._stop_event.set()
        return {"status": "ok"}

    monkeypatch.setattr(client, "heartbeat", fake_heartbeat)

    _run_one_heartbeat(daemon)

    assert seen == [[{"slot": "1", "label": "Parity Slot", "description": "t233"}]]


def test_cli_daemon_heartbeat_without_slots_reports_empty_forward_slots(
    client, monkeypatch
):
    """Missing node.yaml / no forward_slots block still reaches the client
    as an empty list — never as ``None`` — so both daemon paths produce the
    identical heartbeat envelope."""
    daemon = node_cli.Daemon(client, dict(CFG))
    monkeypatch.setattr(client, "maintenance_due", lambda: False)
    monkeypatch.setattr(node_cli, "load_forward_slots", list)

    seen: list[list[dict[str, str]] | None] = []

    def fake_heartbeat(caps, inflight, forward_slots=None):
        seen.append(forward_slots)
        daemon._stop_event.set()
        return {"status": "ok"}

    monkeypatch.setattr(client, "heartbeat", fake_heartbeat)

    _run_one_heartbeat(daemon)

    assert seen == [[]]


def test_cli_daemon_heartbeat_failure_is_survivable(
    client, monkeypatch, tmp_path, caplog
):
    """A broken forward_slot loader must not kill the heartbeat loop — the
    daemon survives and just flags the error (same resilience as the SSE
    daemon).

    Repaired during T-238: the predecessor set the stop event BEFORE
    invoking ``_heartbeat_loop()`` directly — the loop body never ran and
    the assertions ("no heartbeat call, status unknown") passed
    vacuum-green. This version runs a real thread: tick 1 heartbeats
    successfully, the loader breaks from tick 2 on and the loop must keep
    ticking (heartbeat stops while the loader is broken — capabilities are
    never sent half-known — and the error is surfaced, T-237-hardened).
    """
    daemon = node_cli.Daemon(client, dict(CFG))
    monkeypatch.setattr(node_cli, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(client, "maintenance_due", lambda: False)

    loader_calls: list[int] = []
    heartbeat_calls: list[int] = []

    def flaky_loader():
        loader_calls.append(1)
        if len(loader_calls) == 1:
            # First tick declares a slot — the heartbeat fires with it.
            return [{"slot": "1", "label": "ok-once"}]
        raise ValueError("bad forward_slots")

    monkeypatch.setattr(node_cli, "load_forward_slots", flaky_loader)

    def fake_heartbeat(caps, inflight, forward_slots=None):
        heartbeat_calls.append(1)
        return {"status": "ok"}

    monkeypatch.setattr(client, "heartbeat", fake_heartbeat)

    thread = threading.Thread(target=daemon._heartbeat_loop, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while len(loader_calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.2)
    daemon._stop_event.set()
    thread.join(timeout=5)
    assert not thread.is_alive()

    assert heartbeat_calls == [1]  # one real heartbeat pass happened
    assert len(loader_calls) >= 3, (
        "heartbeat loop died at the first broken loader call instead of "
        "continuing (test must drive a real pass-through)"
    )
    assert any(
        "bad forward_slots" in rec.getMessage() for rec in caplog.records
    )
    assert daemon.last_heartbeat_status == "ok"  # from the successful tick