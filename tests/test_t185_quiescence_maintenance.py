"""T-185: Credential maintenance as a quiescence window (Ronny, 2026-09-13).

Model: token rotation suspends ALL server connections (SSE, heartbeat,
claims), rotates cleanly, and every connection is rebuilt with the fresh
token afterwards. Intervals (Ronny, fixed cadences):
- rs: refreshed synchronously at daemon start BEFORE any connection starts,
  + every 24h inside the window.
- rt: NOT refreshed at start (Ronny's correction) — only every 6 days
  inside the window.

Server finding: the SSE endpoint sends no keepalive (core/events.py:
naked ``await queue.get()``), so quiescence must ACTIVELY cancel the
streaming GET instead of waiting between lines.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest

from nodes.common import node_utils, relay_client
from nodes.common.node_daemon import SseDaemon
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
    node_utils.save_token("rt_current", expires_at=None)
    return tmp_path


def _make_client() -> RelayClient:
    return RelayClient(dict(META), dict(CFG))


def _fake_http(responses: dict[str, list], calls: list):
    """httpx.post stub routing on requested_credential (T-182 pattern)."""

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
# rs: synchronous startup rotation (before any connection starts)
# ---------------------------------------------------------------------------


def test_startup_wrapper_rotates_rs_immediately(isolated_relay_dir, monkeypatch):
    """refresh_registration_secret() must rotate NOW (start tick), not wait
    for the first heartbeat tick, so the very first connection already
    presents fresh credentials."""
    calls: list = []
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"registration_secret": [(200, {"token": RS_NEW})],
                    "runtime_token": [], "recovery": []}, calls),
    )
    client = _make_client()
    client.refresh_registration_secret()

    assert [c["kind"] for c in calls] == ["registration_secret"]
    meta = json.loads(node_utils.META_PATH.read_text())
    assert meta["registration_secret"] == RS_NEW
    # The 24h cadence starts from this success.
    assert client._rs_last_refresh is not None


def test_startup_wrapper_failure_retries_on_next_maintenance(
    isolated_relay_dir, monkeypatch
):
    """Server down at startup: no crash, rs stays due (retry in the next
    maintenance window)."""
    calls: list = []
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"registration_secret": [(500, {"detail": "boom"})],
                    "runtime_token": [], "recovery": []}, calls),
    )
    client = _make_client()
    client.refresh_registration_secret()  # must not raise

    node_utils.save_meta(dict(META))  # seed meta so the file exists for reads
    meta = json.loads(node_utils.META_PATH.read_text())
    assert meta["registration_secret"] == "rs_old"
    assert client.maintenance_due() is True  # rs still outstanding


# ---------------------------------------------------------------------------
# rt: NOT refreshed at start, only on the 6-day cadence
# ---------------------------------------------------------------------------


def test_rt_not_refreshed_at_start(isolated_relay_dir, monkeypatch):
    """Ronny's correction: a daemon start must NOT rotate rt — it is fresh
    enough at startup; rotation happens only inside the 6-day window.
    (rs rotates on its own start cadence — separate test.)"""
    calls: list = []
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"registration_secret": [(200, {"token": RS_NEW})],
                    "runtime_token": [], "recovery": []}, calls),
    )
    client = _make_client()
    client.maybe_refresh_token()

    assert [c["kind"] for c in calls] == ["registration_secret"]


def test_rt_refreshed_after_backdating_6d(isolated_relay_dir, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"registration_secret": [(200, {"token": RS_NEW})],
                    "runtime_token": [(200, {"token": RT_NEW})],
                    "recovery": []}, calls),
    )
    client = _make_client()
    # rs already consumed at startup (fresh client state here: rs due once).
    client.maybe_refresh_token()
    rt_calls = [c for c in calls if c["kind"] == "runtime_token"]
    assert rt_calls == []

    client._rt_last_refresh = time.monotonic() - (6 * 86400 + 1)
    client.maybe_refresh_token()
    rt_calls = [c for c in calls if c["kind"] == "runtime_token"]
    assert len(rt_calls) == 1
    assert json.loads(node_utils.TOKEN_PATH.read_text())["token"] == RT_NEW


# ---------------------------------------------------------------------------
# maintenance_due(): the heartbeat loop must only open the window when a
# rotation is actually due — otherwise it would drop SSE every 8 s tick.
# ---------------------------------------------------------------------------


def test_maintenance_due_reflects_cadences(isolated_relay_dir, monkeypatch):
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"registration_secret": [(200, {"token": RS_NEW})],
                    "runtime_token": [], "recovery": []}, []),
    )
    client = _make_client()
    # rs start rotation is outstanding → due.
    assert client.maintenance_due() is True

    client.refresh_registration_secret()  # consumes the start tick
    assert client.maintenance_due() is False

    # A failed startup rotation keeps rs outstanding.
    calls_fail: list = []
    monkeypatch.setattr(
        relay_client.httpx, "post",
        _fake_http({"registration_secret": [(500, {"detail": "boom"})],
                    "runtime_token": [], "recovery": []}, calls_fail),
    )
    client2 = _make_client()
    client2.refresh_registration_secret()
    assert client2.maintenance_due() is True

    # rt due again after backdating past the 6-day cadence.
    client._rt_last_refresh = time.monotonic() - (6 * 86400 + 1)
    assert client.maintenance_due() is True


# ---------------------------------------------------------------------------
# SSE loop: quiescence window
# ---------------------------------------------------------------------------


def _run_sse_in_thread(daemon: SseDaemon) -> threading.Thread:
    t = threading.Thread(
        target=lambda: asyncio.run(daemon._sse_loop_async()), daemon=True
    )
    t.start()
    return t


def test_sse_loop_holds_connections_during_window(
    isolated_relay_dir, monkeypatch
):
    """While the window is open the SSE loop must not open any connection;
    after it closes it connects fresh (current token, T-184 header)."""
    client = _make_client()
    daemon = SseDaemon(client, dict(CFG))
    attempts: list[str] = []

    async def fake_consume(self, http, url, headers):
        attempts.append(headers["Authorization"])
        if len(attempts) >= 2:
            daemon._stop_event.set()
            return
        client.token = RT_NEW  # rotation "happened" during the window
        raise RuntimeError("force reconnect")

    monkeypatch.setattr(SseDaemon, "_consume_stream", fake_consume)
    monkeypatch.setattr("nodes.common.node_daemon._RECONNECT_DELAY", 0.2)

    daemon._quiesce.clear()  # window open BEFORE the loop starts
    t = _run_sse_in_thread(daemon)
    time.sleep(0.4)
    assert attempts == []  # no connection attempt while window is open

    daemon._quiesce.set()  # window closed → connect fresh
    t.join(timeout=3)
    assert not t.is_alive()
    assert attempts == ["Bearer rt_current", f"Bearer {RT_NEW}"]


def test_quiesce_cancels_active_stream_and_holds(
    isolated_relay_dir, monkeypatch
):
    """The server sends no SSE keepalive — opening the window must ACTIVELY
    cancel the streaming GET mid-flight, then hold until it closes."""
    client = _make_client()
    daemon = SseDaemon(client, dict(CFG))
    attempts: list[str] = []
    cancelled: list[bool] = []

    async def fake_consume(self, http, url, headers):
        attempts.append(headers["Authorization"])
        try:
            if len(attempts) == 1:
                await asyncio.Event().wait()  # stream idle, never returns
            daemon._stop_event.set()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(SseDaemon, "_consume_stream", fake_consume)
    monkeypatch.setattr("nodes.common.node_daemon._RECONNECT_DELAY", 0.2)

    t = _run_sse_in_thread(daemon)
    time.sleep(0.4)
    assert len(attempts) == 1  # connected, stream idle

    client.token = RT_NEW  # rotation happens during the window
    daemon._quiesce.clear()  # window opens MID-STREAM
    time.sleep(0.5)
    assert cancelled == [True]  # active stream was cancelled
    assert len(attempts) == 1  # and NO reconnect while window is open

    daemon._quiesce.set()  # window closed → fresh connection
    t.join(timeout=3)
    assert not t.is_alive()
    assert len(attempts) == 2
    assert attempts[1] == f"Bearer {RT_NEW}"


# ---------------------------------------------------------------------------
# Heartbeat thread: the window owner
# ---------------------------------------------------------------------------


def test_maintenance_window_wraps_rotation(isolated_relay_dir, monkeypatch):
    """_run_maintenance_window() clears _quiesce for the duration of the
    rotation and re-opens it afterwards — even if the rotation raises."""
    client = _make_client()
    daemon = SseDaemon(client, dict(CFG))
    seen_during: list[bool] = []

    def spy():
        seen_during.append(daemon._quiesce.is_set())

    monkeypatch.setattr(client, "run_credential_maintenance", spy)
    daemon._run_maintenance_window()

    assert seen_during == [False]  # window was OPEN during rotation
    assert daemon._quiesce.is_set() is True  # closed again afterwards

    def boom():
        raise RuntimeError("rotation failed")

    monkeypatch.setattr(client, "run_credential_maintenance", boom)
    daemon._run_maintenance_window()  # must not raise
    assert daemon._quiesce.is_set() is True


def test_heartbeat_loop_opens_window_only_when_due(
    isolated_relay_dir, monkeypatch
):
    """The heartbeat loop runs the maintenance window only when a rotation
    is actually due — not on every 8 s tick (that would drop SSE forever)."""
    client = _make_client()
    daemon = SseDaemon(client, dict(CFG))
    windows_run: list[bool] = []

    def fake_window():
        windows_run.append(True)
        daemon._stop_event.set()  # end the loop after one tick

    monkeypatch.setattr(client, "maintenance_due", lambda: True)
    monkeypatch.setattr(daemon, "_run_maintenance_window", fake_window)
    monkeypatch.setattr(client, "heartbeat", lambda caps, inflight: {"status": "ok"})

    t = threading.Thread(target=daemon._heartbeat_loop, daemon=True)
    t.start()
    t.join(timeout=3)
    assert not t.is_alive()
    assert windows_run == [True]

    # Not due → no window (and no rotation) on the next tick.
    daemon2 = SseDaemon(client, dict(CFG))
    windows_run2: list[bool] = []
    hb_calls: list[int] = []

    monkeypatch.setattr(client, "maintenance_due", lambda: False)
    monkeypatch.setattr(daemon2, "_run_maintenance_window",
                        lambda: windows_run2.append(True))
    monkeypatch.setattr(
        client, "heartbeat",
        lambda caps, inflight: hb_calls.append(1) or daemon2._stop_event.set()
        or {"status": "ok"},
    )

    t2 = threading.Thread(target=daemon2._heartbeat_loop, daemon=True)
    t2.start()
    t2.join(timeout=3)
    assert not t2.is_alive()
    assert windows_run2 == []
    assert hb_calls == [1]  # heartbeat itself still ran normally


# ---------------------------------------------------------------------------
# Startup order: rs rotation BEFORE any connection thread starts
# ---------------------------------------------------------------------------


def test_startup_rs_before_threads(isolated_relay_dir, monkeypatch):
    """SseDaemon.run() must rotate rs synchronously before starting the
    heartbeat/probe/SSE threads."""
    client = _make_client()
    daemon = SseDaemon(client, dict(CFG))
    order: list[str] = []

    def fake_rs():
        order.append("rs")

    def fake_hb():
        order.append("hb")
        daemon._stop_event.set()  # end run()'s main loop right away

    def fake_probe():
        order.append("probe")

    monkeypatch.setattr(client, "refresh_registration_secret", fake_rs)
    monkeypatch.setattr(daemon, "_start_heartbeat_thread", fake_hb)
    monkeypatch.setattr(daemon, "_start_probe_thread", fake_probe)
    monkeypatch.setattr(SseDaemon, "_sse_loop", lambda self: None)

    daemon.run()

    assert order[0] == "rs"
    assert order[1] == "hb"
    assert order[2] == "probe"