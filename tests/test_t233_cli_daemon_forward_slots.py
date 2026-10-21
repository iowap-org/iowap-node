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


def test_cli_daemon_heartbeat_failure_is_survivable(client, monkeypatch):
    """A broken forward_slot loader must not kill the heartbeat loop — the
    daemon survives and just flags the error (same resilience as the SSE
    daemon)."""
    daemon = node_cli.Daemon(client, dict(CFG))
    monkeypatch.setattr(client, "maintenance_due", lambda: False)

    def boom():
        raise ValueError("bad forward_slots")

    monkeypatch.setattr(node_cli, "load_forward_slots", boom)

    calls: list[int] = []

    def fake_heartbeat(caps, inflight, forward_slots=None):
        calls.append(1)
        return {"status": "ok"}

    monkeypatch.setattr(client, "heartbeat", fake_heartbeat)
    daemon._stop_event.set()  # single tick
    daemon._heartbeat_loop()

    assert calls == []
    assert daemon.last_heartbeat_status == "unknown"