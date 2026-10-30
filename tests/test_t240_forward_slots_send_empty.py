"""T-240: forward slots are sent with EVERY heartbeat after a loader
success — an empty list included, as an explicit "no slots".

Audit F4: ``_build_heartbeat_payload`` gated the field on truthiness
(``if self.forward_slots:``) — a loader success with an EMPTY
declaration dropped the field, server-side ``replace_forward_slots``
kept the stale rows, and forward routing kept sending traffic to a
target the node no longer serves.

Contract:

* loader succeeded + returned rows          → field carries the rows.
* loader succeeded + returned an empty list → field carries ``[]``
  (server semantics: empty list = clear all rows — noted SERVER-side
  in iowap-server by the companion agent; the node only guarantees the
  field is always present on the wire once the loader worked).
* no loader result yet (``None``)           → field omitted (legacy
  wire shape, e.g. the one-shot ``node-cli heartbeat``, which passes
  no slots and must keep producing the old body).
"""

from __future__ import annotations

import pytest

from nodes.common.relay_client import RelayClient

META = {
    "node_id": "240NODE",
    "node_name": "forward-slot-cleared",
    "registration_secret": "rs_x",
    "base_url": "http://relay.test:8788",
}
CFG = {"base_url": None, "request_timeout": 5, "heartbeat_interval": 1}

CAPS = [{"name": "probe.echo", "claimable": True}]
HEARTBEAT_PATH = "/relay/v2/discovery/worker-heartbeat"


class FakeResp:
    def __init__(self, payload) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


@pytest.fixture()
def client() -> RelayClient:
    return RelayClient(dict(META), dict(CFG))


def capture_post(client, monkeypatch) -> dict:
    captured: dict = {}

    def fake_post(path, body, timeout=None):
        captured["path"] = path
        captured["body"] = body
        return FakeResp({"status": "ok"})

    monkeypatch.setattr(client, "_post_with_auth_retry", fake_post)
    return captured


def test_heartbeat_clears_slots_with_empty_list(client, monkeypatch):
    """RED case: a loader success with [] must send the field as [] — the
    server replace then clears the stale rows (old truthiness kept them)."""
    captured = capture_post(client, monkeypatch)
    client.heartbeat(CAPS, {"probe.echo": 0}, forward_slots=[])

    assert captured["path"] == HEARTBEAT_PATH
    assert captured["body"]["forward_slots"] == []


def test_heartbeat_state_carry_keeps_sending_cleared_state(client, monkeypatch):
    """A later heartbeat WITHOUT an explicit argument keeps sending the
    last known (cleared) state instead of resurrecting it."""
    captured = capture_post(client, monkeypatch)
    client.heartbeat(CAPS, {}, forward_slots=[])
    captured.clear()

    client.heartbeat(CAPS, {})  # legacy-style call on the same client

    assert captured["body"]["forward_slots"] == []


# --- contract locks -----------------------------------------------------------


def test_heartbeat_carries_filled_rows(client, monkeypatch):
    captured = capture_post(client, monkeypatch)
    rows = [{"slot": "1", "label": "L", "description": "d"}]
    client.heartbeat(CAPS, {}, forward_slots=rows)

    assert captured["path"] == HEARTBEAT_PATH
    assert captured["body"]["forward_slots"] == rows


def test_heartbeat_without_loader_result_omits_field(client, monkeypatch):
    """Legacy wire shape: before any loader success the field stays
    omitted (fresh client, one-shot heartbeat call)."""
    captured = capture_post(client, monkeypatch)
    client.heartbeat(CAPS, {}, forward_slots=None)

    assert "forward_slots" not in captured["body"]