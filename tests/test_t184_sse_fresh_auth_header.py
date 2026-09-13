"""T-184: SSE reconnect must use the CURRENT runtime token.

Regression: ``_sse_loop_async`` built the Authorization header once before
the reconnect loop. Once maintenance rotated the runtime token (possible
again since the T-184 maintenance fix), the SSE loop kept presenting the
stale snapshot and 401-looped forever (observed live 2026-09-13 14:21).
"""

from __future__ import annotations

import asyncio
import threading

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


def test_sse_reconnect_uses_current_token(isolated_relay_dir, monkeypatch):
    """After a reconnect, the Authorization header must be rebuilt from the
    client's current token — not the snapshot taken before the loop."""
    client = RelayClient(dict(META), dict(CFG))
    daemon = SseDaemon(client, dict(CFG))
    seen: list[str] = []

    async def fake_consume(self, http, url, headers):
        seen.append(headers["Authorization"])
        if len(seen) >= 2:
            daemon._stop_event.set()
            return
        # Token rotation happens between connection attempts (maintenance).
        client.token = RT_NEW
        raise RuntimeError("force reconnect")

    monkeypatch.setattr(SseDaemon, "_consume_stream", fake_consume)
    monkeypatch.setattr("nodes.common.node_daemon._RECONNECT_DELAY", 0.2)

    asyncio.run(daemon._sse_loop_async())

    assert seen[0] == "Bearer rt_current"
    assert seen[1] == f"Bearer {RT_NEW}"