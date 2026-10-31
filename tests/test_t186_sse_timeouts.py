"""T-186: SSE stream needs read/connect timeouts — no more ``timeout=None``.

Regression: ``_consume_stream`` opened the stream with ``timeout=None``.
(a) With the server down, every reconnect attempt hung ~2min in SYN
retransmits (connect timeout disabled too) — this dominated the deploy
reconnect gap. (b) On a hard server kill (no EOF) the connection became
a TCP zombie: the node read forever, the reconnect path never fired.

Fix: ``httpx.Timeout(connect=5.0, read=60.0)`` — read timeout is 3× the
server's SSE ping interval (T-207, iowap-server, default 20s), so a
silent/dead stream ends with ``httpx.ReadTimeout`` and the normal
reconnect path takes over.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from nodes.common import node_utils
from nodes.common.node_daemon import SseDaemon
from nodes.common.relay_client import RelayClient

META = {
    "node_id": "337NU4U9",
    "node_name": "test-node",
    "base_url": "http://relay.test:8788",
    "registration_secret": "rs_old",
}
CFG = {"base_url": None, "request_timeout": 5, "heartbeat_interval": 8}


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


def _daemon() -> SseDaemon:
    return SseDaemon(RelayClient(dict(META), dict(CFG)), dict(CFG))


def test_t186_stream_timeout_contract(isolated_relay_dir, monkeypatch):
    """_consume_stream must pass a Timeout with connect=5/read=60."""
    daemon = _daemon()
    captured: dict = {}

    class FakeResp:
        def raise_for_status(self) -> None:
            return None

        async def aiter_lines(self):  # pragma: no cover — never reached
            raise RuntimeError("probe-end")
            yield ""  # generator marker (line is unreachable — noqa removed)

    class FakeStreamCM:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def __aenter__(self):
            return FakeResp()

        async def __aexit__(self, *exc) -> None:
            return None

    class FakeHttp:
        def stream(self, *args, **kwargs):
            return FakeStreamCM(**kwargs)

    with pytest.raises(RuntimeError, match="probe-end"):
        asyncio.run(daemon._consume_stream(FakeHttp(), "http://x/stream", {}))

    timeout = captured.get("timeout")
    assert timeout is not None, "stream must set a timeout (was None)"
    assert timeout.connect == 5.0
    assert timeout.read == 60.0


def test_t186_silent_stream_ends_with_read_timeout(isolated_relay_dir, monkeypatch):
    """A server that accepts but never sends must be dropped by the read
    timeout (behavioral proof with a real httpx client + real socket)."""
    monkeypatch.setattr("nodes.common.node_daemon._SSE_READ_TIMEOUT", 0.3)
    daemon = _daemon()

    async def scenario() -> Exception | None:
        server = await asyncio.start_server(
            lambda r, w: None, host="127.0.0.1", port=0
        )
        port = server.sockets[0].getsockname()[1]
        outcome: Exception | None = None
        try:
            # T-242 CI finding: the httpx client must be CLOSED BEFORE the
            # server's wait_closed() — since Python 3.12 wait_closed waits
            # for the open client connection to drain, and the silent SSE
            # connection never drains by itself (the old order hung the
            # CI 3.12 leg forever; the 10s wait_for bounds the stream, not
            # the teardown).
            http = httpx.AsyncClient()
            try:
                try:
                    await asyncio.wait_for(
                        daemon._consume_stream(
                            http, f"http://127.0.0.1:{port}/stream", {}
                        ),
                        timeout=10.0,
                    )
                except Exception as exc:  # noqa: BLE001 — expected path
                    outcome = exc
            finally:
                await http.aclose()
        finally:
            server.close()
            # T-242 finding: since Python 3.12 wait_closed() waits for ALL
            # transports — the silent handler never closes its writer, so
            # the drained connection would block forever; bound the drain.
            # The test's assertion is about the STREAM ending (outcome),
            # not client teardown.
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=3.0)
            except (asyncio.TimeoutError, TimeoutError):
                pass
        return outcome

    outcome = asyncio.run(scenario())
    assert outcome is not None, (
        "silent stream never ended — zombie connection (timeout=None)"
    )
    # T-242 sharpened: the read timeout itself (0.3 via the retuned
    # _SSE_READ_TIMEOUT) must end the silent stream — NOT the outer
    # wait_for(10s) cancellation (the cancel of a silent aiter_lines
    # deadlocks the httpx/anyio close on Python 3.12; it also masked this
    # test's intent: a wait_for TimeoutError proved nothing about the
    # read budget).
    assert isinstance(outcome, httpx.ReadTimeout), outcome