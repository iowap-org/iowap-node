"""T-182: Fixed credential-maintenance intervals (Ronny, 2026-09-13).

Model: no more expiry math. Nodes act on their own:
- rs (registration secret): refreshed on daemon start + every 24h
- rt (runtime token): refreshed every 6 days
- TTL stays 7 days on both (server config.py, unchanged)

Bug context: the server never rotates the rs on rt-refresh (Case 1), the
/auth/status polling contract was never implemented in the client (0 calls
all-time), and _recover_runtime_token() threw away the rotated rs from the
recovery response (relay_client.py:316-323). Together that produced the
2026-09-13 401 death spiral on node 337NU4U9.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from nodes.common import node_utils, relay_client
from nodes.common.relay_client import RelayClient

META = {
    "node_id": "337NU4U9",
    "node_name": "test-node",
    "base_url": "http://relay.test:8788",
    "registration_secret": "rs_old",
}
CFG = {"base_url": None, "request_timeout": 5, "heartbeat_interval": 8}

RS_NEW = "rs_fresh_from_server"
RT_NEW = "rt_fresh_from_server"


@pytest.fixture()
def isolated_relay_dir(tmp_path, monkeypatch):
    """Point every state path at a tmp dir so tests never touch ~/.relay."""
    for name in (
        "META_PATH", "TOKEN_PATH", "CONFIG_PATH",
        "LEGACY_META_PATH", "LEGACY_TOKEN_PATH", "STATUS_PATH",
    ):
        monkeypatch.setattr(node_utils, name, tmp_path / f"{name.lower()}.json")
    # Seed a valid runtime token so __init__ does not attempt recovery.
    node_utils.save_token("rt_current", expires_at=None)
    return tmp_path


def _make_client() -> RelayClient:
    return RelayClient(dict(META), dict(CFG))


def _fake_http(responses: dict[str, list], calls: list):
    """Build an httpx.post stub routing on requested_credential.

    responses maps requested_credential -> list of (status, payload) tuples.
    Recovery calls (runtime_token via rs, no bearer) need their own key:
    ``recovery``.
    """

    def fake_post(url, json=None, headers=None, **kw):
        body = json or {}
        req = body.get("requested_credential")
        if req == "runtime_token" and headers is None:
            req = "recovery"
        kind = req if isinstance(req, str) else "unknown"
        calls.append({"url": url, "body": body, "headers": headers, "kind": kind})
        status, payload = responses[kind].pop(0)
        return httpx.Response(status, json=payload, request=httpx.Request("POST", url))

    return fake_post


# ---------------------------------------------------------------------------
# rs: on start + every 24h
# ---------------------------------------------------------------------------


def test_rs_rotated_on_first_maintenance(isolated_relay_dir, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"registration_secret": [(200, {"token": RS_NEW})],
                    "runtime_token": [(200, {"token": RT_NEW})],
                    "recovery": []}, calls),
    )
    client = _make_client()
    client.maybe_refresh_token()

    kinds = [c["kind"] for c in calls]
    assert "registration_secret" in kinds
    rs_call = next(c for c in calls if c["kind"] == "registration_secret")
    assert rs_call["body"] == {"requested_credential": "registration_secret"}
    # rs rotation runs AFTER the rt refresh in the same tick — it must use
    # the freshly rotated runtime token (the old one is already invalidated).
    assert rs_call["headers"]["Authorization"] == f"Bearer {RT_NEW}"

    # The fresh rs must be persisted in the meta file (the whole point of T-182).
    meta = json.loads(node_utils.META_PATH.read_text())
    assert meta["registration_secret"] == RS_NEW
    assert client.meta["registration_secret"] == RS_NEW


def test_rs_not_rotated_again_within_24h(isolated_relay_dir, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"registration_secret": [(200, {"token": RS_NEW})],
                    "runtime_token": [(200, {"token": RT_NEW})],
                    "recovery": []}, calls),
    )
    client = _make_client()
    client.maybe_refresh_token()
    rs_calls_first = sum(1 for c in calls if c["kind"] == "registration_secret")
    assert rs_calls_first == 1

    # Second tick shortly after: no second rs rotation.
    client.maybe_refresh_token()
    assert sum(1 for c in calls if c["kind"] == "registration_secret") == 1


def test_rs_rotated_again_after_24h(isolated_relay_dir, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"registration_secret": [(200, {"token": RS_NEW}),
                                            (200, {"token": "rs_newer"})],
                    "runtime_token": [(200, {"token": RT_NEW})],
                    "recovery": []}, calls),
    )
    client = _make_client()
    client.maybe_refresh_token()

    # Simulate 24h of daemon uptime.
    client._rs_last_refresh = time.monotonic() - (24 * 3600 + 1)
    client.maybe_refresh_token()
    assert sum(1 for c in calls if c["kind"] == "registration_secret") == 2
    meta = json.loads(node_utils.META_PATH.read_text())
    assert meta["registration_secret"] == "rs_newer"


# ---------------------------------------------------------------------------
# rt: every 6 days
# ---------------------------------------------------------------------------


def test_rt_refreshed_on_first_maintenance(isolated_relay_dir, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"registration_secret": [(200, {"token": RS_NEW})],
                    "runtime_token": [(200, {"token": RT_NEW})],
                    "recovery": []}, calls),
    )
    client = _make_client()
    client.maybe_refresh_token()

    rt_call = next(c for c in calls if c["kind"] == "runtime_token")
    assert rt_call["headers"]["Authorization"] == "Bearer rt_current"
    tok = json.loads(node_utils.TOKEN_PATH.read_text())
    assert tok["token"] == RT_NEW
    assert client.token == RT_NEW


def test_rt_not_refreshed_again_within_6_days(isolated_relay_dir, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"registration_secret": [(200, {"token": RS_NEW})],
                    "runtime_token": [(200, {"token": RT_NEW})],
                    "recovery": []}, calls),
    )
    client = _make_client()
    client.maybe_refresh_token()
    assert sum(1 for c in calls if c["kind"] == "runtime_token") == 1

    client.maybe_refresh_token()
    assert sum(1 for c in calls if c["kind"] == "runtime_token") == 1


def test_rt_refreshed_after_6_days(isolated_relay_dir, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"registration_secret": [(200, {"token": RS_NEW})],
                    "runtime_token": [(200, {"token": RT_NEW}),
                                      (200, {"token": "rt_newer"})],
                    "recovery": []}, calls),
    )
    client = _make_client()
    client.maybe_refresh_token()

    client._rt_last_refresh = time.monotonic() - (6 * 86400 + 1)
    client.maybe_refresh_token()
    assert sum(1 for c in calls if c["kind"] == "runtime_token") == 2
    assert json.loads(node_utils.TOKEN_PATH.read_text())["token"] == "rt_newer"


# ---------------------------------------------------------------------------
# Bug 4 fix: rotated rs from responses must be persisted
# ---------------------------------------------------------------------------


def test_recovery_persists_rotated_rs(isolated_relay_dir, monkeypatch):
    """Recovery response carries a fresh rs — must land in the meta file."""
    # Remove the seeded token so __init__ takes the recovery path.
    node_utils.TOKEN_PATH.unlink()
    calls: list = []
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"recovery": [(200, {"token": "rt_recovered",
                                        "registration_secret": RS_NEW,
                                        "expires_at": "2026-09-20T00:00:00+00:00"})]},
                   calls),
    )
    client = RelayClient(dict(META), dict(CFG))  # no token on disk -> recovery
    assert client.token == "rt_recovered"

    meta = json.loads(node_utils.META_PATH.read_text())
    assert meta["registration_secret"] == RS_NEW
    assert client.meta["registration_secret"] == RS_NEW
    # Recovery counts as an rt refresh for the interval timer.
    assert client._rt_last_refresh is not None


def test_refresh_response_rs_is_persisted_when_present(isolated_relay_dir, monkeypatch):
    """If the server ever attaches a rotated rs to the rt-refresh response,
    the client must keep it (fail-safe for future server behaviour)."""
    calls: list = []
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"registration_secret": [(200, {"token": RS_NEW})],
                    "runtime_token": [(200, {"token": RT_NEW,
                                             "registration_secret": "rs_alongside"})],
                    "recovery": []}, calls),
    )
    client = _make_client()
    # Isolate the Bug-4 path: suppress the proactive rs rotation so the
    # only rs source is the registration_secret field on the rt response.
    client._rs_due_on_start = False
    client._rs_last_refresh = time.monotonic()
    client.maybe_refresh_token()
    meta = json.loads(node_utils.META_PATH.read_text())
    assert meta["registration_secret"] == "rs_alongside"


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_rs_refresh_failure_is_not_fatal(isolated_relay_dir, monkeypatch):
    """Server down during a maintenance tick: warn, keep old rs, retry later."""
    node_utils.save_meta(dict(META))  # seed the meta file with the old rs
    calls: list = []
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"registration_secret": [(500, {"detail": "boom"})],
                    "runtime_token": [(200, {"token": RT_NEW})],
                    "recovery": []}, calls),
    )
    client = _make_client()
    client.maybe_refresh_token()  # must not raise

    meta = json.loads(node_utils.META_PATH.read_text())
    assert meta["registration_secret"] == "rs_old"
    assert client._rs_last_refresh is None  # retry on the next tick


def test_interval_env_overrides(isolated_relay_dir, monkeypatch):
    monkeypatch.setenv("RELAY_RS_REFRESH_INTERVAL", "60")
    monkeypatch.setenv("RELAY_RT_REFRESH_INTERVAL", "120")
    cfg = relay_client._effective_config()
    assert cfg["rs_refresh_interval_seconds"] == 60
    assert cfg["rt_refresh_interval_seconds"] == 120