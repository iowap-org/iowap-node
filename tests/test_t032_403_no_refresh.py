"""T-032: 403 is a permission answer — no token-refresh spiral, no poll loop.

Windows-VM log (bug.txt): the forward panel's 403 (wintest is not an
admin node) triggered the client's 401/403-refresh branch — spinning the
registration-secret rotation and colliding with live calls (spurious
401s on heartbeat/SSE), while every dashboard render re-fetched the
endpoint.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".."))

from nodes.common import dashboard  # noqa: E402


class _Resp:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _Client:
    """Records GETs, answers 403 always (like the relay for non-admins)."""

    def __init__(self) -> None:
        self.gets = 0

    def _get_with_retry(self, path: str, *, params=None, timeout: float = 8.0):
        self.gets += 1
        return _Resp(403)


def test_fetch_forward_panel_403_sets_forbidden(monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "_FORBIDDEN_LATCH", {})
    client = _Client()
    panel = dashboard.fetch_forward_panel(
        client=client, node_id="XB7J43T3"
    )
    assert panel["forbidden"] is True
    assert panel["ok"] is False
    assert client.gets == 1


def test_fetch_forward_panel_403_latches(monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "_FORBIDDEN_LATCH", {})
    client = _Client()
    for _ in range(3):
        panel = dashboard.fetch_forward_panel(
            client=client, node_id="XB7J43T3"
        )
        assert panel["forbidden"] is True
    # after the first 403 the latch answers from memory — no further GETs
    assert client.gets == 1
