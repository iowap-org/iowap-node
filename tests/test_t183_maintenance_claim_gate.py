"""T-183: Claims pause during credential maintenance (Ronny, 2026-09-13).

Sequence model instead of locking: the maintenance thread pauses claims,
rotates rt/rs, and resumes claims with fresh tokens. A claim request that
was already in flight when the pause started waits in the 401 fallback,
then adopts the fresh token from disk instead of refreshing against the
token the maintenance just invalidated (the 2026-09-13 restart race).
"""

from __future__ import annotations

import threading
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
    """httpx.post stub routing on requested_credential (see T-182 tests)."""

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
# Gate basics + claim pause
# ---------------------------------------------------------------------------


def test_gate_is_open_after_init(isolated_relay_dir):
    client = _make_client()
    assert client._maintenance_gate.is_set()


def test_claim_skips_without_http_while_paused(isolated_relay_dir, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"registration_secret": [], "runtime_token": [], "recovery": []}, calls),
    )
    client = _make_client()
    client._maintenance_gate.clear()

    assert client.claim("demo") is None
    # No claim HTTP call happened — the pause is decided locally.
    assert calls == []


def test_claim_resumes_after_maintenance(isolated_relay_dir, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"recovery": [], "unknown": [(204, {})]}, calls),
    )
    client = _make_client()
    client._maintenance_gate.clear()
    client._maintenance_gate.set()

    client.claim("demo")  # 204 -> None, but the call must have gone out
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/scheduler/claim")


def test_run_credential_maintenance_pauses_and_resumes(isolated_relay_dir, monkeypatch):
    """The gate is closed exactly for the duration of the maintenance."""
    calls: list = []
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"registration_secret": [(200, {"token": RS_NEW})],
                    "runtime_token": [(200, {"token": RT_NEW})],
                    "recovery": []}, calls),
    )
    client = _make_client()
    observed: list[bool] = []
    original = client.maybe_refresh_token

    def spy():
        observed.append(client._maintenance_gate.is_set())
        original()

    monkeypatch.setattr(client, "maybe_refresh_token", spy)
    client.run_credential_maintenance()
    assert observed == [False]  # gate closed DURING maintenance
    assert client._maintenance_gate.is_set()  # open again afterwards


def test_run_credential_maintenance_resumes_on_error(isolated_relay_dir, monkeypatch):
    client = _make_client()

    def boom():
        raise RuntimeError("maintenance exploded")

    monkeypatch.setattr(client, "maybe_refresh_token", boom)
    with pytest.raises(RuntimeError):
        client.run_credential_maintenance()
    assert client._maintenance_gate.is_set()  # finally must have fired


# ---------------------------------------------------------------------------
# 401 fallback: wait for maintenance, adopt the fresh token
# ---------------------------------------------------------------------------


def test_fallback_refresh_waits_and_adopts_fresh_token(isolated_relay_dir, monkeypatch):
    """A claim 401 during maintenance waits, then refreshes with the token
    the maintenance just persisted — not with the invalidated old one."""
    calls: list = []
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"runtime_token": [(200, {"token": "rt_after_fallback"})],
                    "registration_secret": [], "recovery": []}, calls),
    )
    client = _make_client()
    client._maintenance_gate.clear()

    def finish_maintenance():
        time.sleep(0.2)
        # What the maintenance thread does on a successful rt refresh:
        node_utils.save_token(RT_NEW, expires_at=None)
        client._maintenance_gate.set()

    worker = threading.Thread(target=finish_maintenance, daemon=True)
    worker.start()
    assert client._refresh_token() is True
    worker.join()

    rt_calls = [c for c in calls if c["kind"] == "runtime_token"]
    assert len(rt_calls) == 1
    # The fallback's own refresh ran with the maintenance's fresh token.
    assert rt_calls[0]["headers"]["Authorization"] == f"Bearer {RT_NEW}"
    assert client.token == "rt_after_fallback"


def test_fallback_refresh_times_out_and_proceeds(isolated_relay_dir, monkeypatch):
    """Maintenance hangs (gate never reopens): the fallback must not block
    forever — after the timeout it proceeds with its own refresh."""
    calls: list = []
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"runtime_token": [(401, {"detail": "stale"})],
                    "registration_secret": [], "recovery": []}, calls),
    )
    client = _make_client()
    client._maintenance_gate.clear()
    monkeypatch.setattr(client, "_MAINTENANCE_WAIT_TIMEOUT", 0.1)

    started = time.monotonic()
    assert client._refresh_token() is False  # 401 stub -> recovery-less failure
    assert time.monotonic() - started < 2.0
    assert client._maintenance_gate.is_set() is False


# ---------------------------------------------------------------------------
# maybe_refresh_token refuses to race a running maintenance
# ---------------------------------------------------------------------------


def test_maybe_refresh_skips_while_gate_closed(isolated_relay_dir, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"registration_secret": [], "runtime_token": [], "recovery": []}, calls),
    )
    client = _make_client()
    client._maintenance_gate.clear()

    client.maybe_refresh_token()
    assert calls == []  # no rt/rs calls — maintenance already in flight elsewhere


def test_maybe_refresh_works_when_gate_open(isolated_relay_dir, monkeypatch):
    """Sanity: the direct call (used by tests/older flows) is unchanged."""
    calls: list = []
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"registration_secret": [(200, {"token": RS_NEW})],
                    "runtime_token": [(200, {"token": RT_NEW})],
                    "recovery": []}, calls),
    )
    client = _make_client()
    client.maybe_refresh_token()
    kinds = {c["kind"] for c in calls}
    assert kinds == {"runtime_token", "registration_secret"}