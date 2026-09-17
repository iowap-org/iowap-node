"""Backfill logic for pending tasks (T-c51219ee) — SSE-connect sweep + ticker.

Root cause (report REPORT_hangende_tasks_node_daemon_2026-09-16.md):
the node-daemon is purely SSE-event-driven and ``task_created`` is a
one-shot event. A task that became pending BEFORE the SSE connection
existed (daemon start, reload, network blip) — or that lost its initial
claim race — never re-triggers an event and stays ``pending`` forever.

Fix contract:

* After every successful SSE connect (``_consume_stream`` reaches the
  connected state), the daemon sweeps all claimable capabilities once
  (``_on_sse_connected`` → ``_backfill_sweep``).
* A periodic ticker repeats the sweep every ``backfill_interval``
  seconds (default 60; env ``RELAY_BACKFILL_INTERVAL``; 0 = off).
* The sweep reuses the ``max_parallel``/``_in_flight`` guard from the
  ``task_created`` handler — it must never violate ``max_parallel``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from nodes.common import node_daemon
from nodes.common.node_daemon import (
    SseDaemon,
    _backfill_interval,
    _cfg_int,
)


class FakeResp:
    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self):  # pragma: no cover — generator marker
        raise RuntimeError("probe-end")
        yield ""  # noqa: unreachable - generator marker


class FakeStreamCM:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    async def __aenter__(self):
        return FakeResp()

    async def __aexit__(self, *exc) -> None:
        return None


class FakeHttp:
    def stream(self, *args, **kwargs):
        return FakeStreamCM(**kwargs)


def make_daemon(cfg: dict | None = None) -> SseDaemon:
    """Real SseDaemon with a MagicMock client (no network, no ~/.relay)."""
    d = SseDaemon.__new__(SseDaemon)
    d.client = MagicMock()
    d.cfg = cfg or {"heartbeat_interval": 30}
    d.server_probe = {"ok": False, "error": "probe pending"}
    d.tasks_completed = 0
    d.tasks_failed = 0
    d._failed_tasks = {}
    d._in_flight = {}
    d._started_at = datetime.now(UTC)
    d._lock = threading.Lock()
    d._stop_event = threading.Event()
    d._backfill_thread = None
    d._backfill_armed = threading.Event()
    return d


@pytest.fixture()
def cap_profile(monkeypatch):
    """Pin the active capability profile used by the sweep."""
    calls = {"profile": [
        {"name": "image.generate.mflux", "claimable": True, "max_parallel": 1},
        {"name": "chat.ai", "claimable": False},
        {"name": "file.ai", "claimable": True, "max_parallel": 1},
    ]}
    monkeypatch.setattr(
        node_daemon, "load_active_profile", lambda: calls["profile"]
    )
    return calls


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def test_default_backfill_interval_is_60(cap_profile):
    assert _backfill_interval({}) == 60


def test_env_overrides_config(monkeypatch, cap_profile):
    monkeypatch.setenv("RELAY_BACKFILL_INTERVAL", "120")
    assert _backfill_interval({"backfill_interval": 15}) == 120


def test_config_key_respected_without_env(monkeypatch, cap_profile):
    monkeypatch.delenv("RELAY_BACKFILL_INTERVAL", raising=False)
    assert _backfill_interval({"backfill_interval": 30}) == 30


def test_garbage_values_fall_back_to_default(cap_profile):
    assert _backfill_interval({"backfill_interval": "nonsense"}) == 60
    assert _cfg_int({}, "missing", 7) == 7


# ---------------------------------------------------------------------------
# SSE-connect sweep
# ---------------------------------------------------------------------------


def test_connect_sweep_claims_pending_task(cap_profile, monkeypatch):
    """Acceptance 1: daemon (re)connects while a pending task for its
    capability exists → the sweep claims it."""
    daemon = make_daemon()
    claimed: list[str] = []
    monkeypatch.setattr(
        daemon, "_try_claim_and_run",
        lambda name: claimed.append(name),
    )
    daemon._on_sse_connected()
    # Both claimable capabilities were tried; chat.ai (not claimable) skipped.
    assert claimed == ["image.generate.mflux", "file.ai"]


def test_consume_stream_triggers_backfill_on_connect(
    cap_profile, monkeypatch
):
    """The real stream path: reaching the connected state fires the sweep
    exactly once, before any event line is consumed."""
    daemon = make_daemon()
    swept = []
    monkeypatch.setattr(
        daemon, "_backfill_sweep", lambda: swept.append(1)
    )
    monkeypatch.setattr(
        daemon, "_arm_backfill_ticker", lambda: swept.append("arm")
    )
    with pytest.raises(RuntimeError, match="probe-end"):
        asyncio.run(
            daemon._consume_stream(FakeHttp(), "http://x/stream", {})
        )
    assert swept == [1, "arm"]


def test_consume_stream_survives_backfill_error(cap_profile, monkeypatch):
    """A backfill failure must not kill the SSE stream (best-effort)."""
    daemon = make_daemon()

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(daemon, "_backfill_sweep", boom)
    # The in-stream connect hook also catches: a sweep crash must not
    # propagate out of _consume_stream.
    monkeypatch.setattr(
        daemon, "_on_sse_connected",
        lambda: (_ for _ in ()).throw(RuntimeError("boom-arm")),
        raising=False,
    )
    lines: list[str] = []

    resp = FakeResp()

    async def aiter_lines():
        # An ignored event type — the ONLY error source in this stream
        # is the connect hook, which must be swallowed.
        for line in ["event: node_online", "data: {}", ""]:
            lines.append(line)
            yield line

    resp.aiter_lines = aiter_lines  # type: ignore[method-assign]

    class CM:
        async def __aenter__(self):
            return resp

        async def __aexit__(self, *exc) -> None:
            return None

    class HTTP:
        def stream(self, *args, **kwargs):
            return CM()

    asyncio.run(daemon._consume_stream(HTTP(), "http://x/stream", {}))
    # The whole stream (incl. the swallowed connect-hook error) was consumed.
    assert lines == ["event: node_online", "data: {}", ""]


def test_max_parallel_guard_respected(cap_profile, monkeypatch):
    """Acceptance pitfall: the sweep must not violate max_parallel."""
    daemon = make_daemon()
    # mflux already at max_parallel=1 → must be skipped entirely.
    with daemon._lock:
        daemon._in_flight["image.generate.mflux"] = 1
    claimed: list[str] = []
    monkeypatch.setattr(
        daemon, "_try_claim_and_run",
        lambda name: claimed.append(name),
    )
    daemon._backfill_sweep()
    assert claimed == ["file.ai"]


def test_sweep_respects_max_retries(cap_profile, monkeypatch):
    """A task whose handler keeps failing is not reclaimed by backfill."""
    daemon = make_daemon(cfg={"heartbeat_interval": 30, "max_retries": 2})
    with daemon._lock:
        daemon._failed_tasks["task_dead"] = 2
    claimed: list[str] = []

    def fake_claim(capability: str):
        if capability == "image.generate.mflux":
            return {"stage_id": "s_1", "task_id": "task_dead"}
        return None

    monkeypatch.setattr(daemon.client, "claim", fake_claim)
    monkeypatch.setattr(
        daemon, "_run_stage",
        lambda cap, stage: claimed.append((cap["name"], stage)),
    )
    daemon._backfill_sweep()
    # The failing task's stage must NOT have been executed.
    assert claimed == []


def test_sweep_claims_and_runs_when_healthy(cap_profile, monkeypatch):
    """Positive path: sweep claims a stage and runs it (long_run note
    path included)."""
    daemon = make_daemon(cfg={"heartbeat_interval": 30})
    stage = {"stage_id": "s_1", "task_id": "task_ok"}
    monkeypatch.setattr(
        daemon.client, "claim", lambda capability: stage
        if capability == "image.generate.mflux" else None,
    )
    monkeypatch.setattr(daemon.client, "add_task_note", lambda *a, **k: None)
    monkeypatch.setattr(
        daemon, "_find_capability",
        lambda name: {"name": name, "handler": "h.sh"},
    )
    executed = []
    monkeypatch.setattr(
        daemon, "_run_stage",
        lambda cap, s: executed.append((cap["name"], s["stage_id"])),
    )
    daemon._backfill_sweep()
    assert executed == [("image.generate.mflux", "s_1")]


# ---------------------------------------------------------------------------
# Ticker
# ---------------------------------------------------------------------------


def test_ticker_starts_once_and_sweeps_periodically(
    cap_profile, monkeypatch
):
    daemon = make_daemon()
    monkeypatch.setattr(node_daemon, "_DEFAULT_BACKFILL_INTERVAL", 60)
    monkeypatch.setattr(node_daemon, "_backfill_interval", lambda cfg: 1)
    monkeypatch.setenv("RELAY_BACKFILL_INTERVAL", "1")
    swept = []
    monkeypatch.setattr(
        daemon, "_backfill_sweep", lambda: swept.append(time.monotonic())
    )
    daemon._arm_backfill_ticker()
    assert daemon._backfill_armed.is_set()
    # Second arm must be a no-op (exactly one ticker).
    thread_before = daemon._backfill_thread
    daemon._arm_backfill_ticker()
    assert daemon._backfill_thread is thread_before
    # Ticker fires after ~1s.
    deadline = time.monotonic() + 5
    while len(swept) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(swept) >= 1, "ticker must sweep periodically"
    daemon._stop_event.set()
    daemon._backfill_thread.join(timeout=5)
    assert not daemon._backfill_thread.is_alive()


def test_ticker_disabled_with_interval_zero(
    cap_profile, monkeypatch
):
    daemon = make_daemon()
    monkeypatch.setattr(node_daemon, "_backfill_interval", lambda cfg: 0)
    daemon._arm_backfill_ticker()
    assert daemon._backfill_armed.is_set()
    assert daemon._backfill_thread is None


def test_reconnect_does_not_spawn_second_ticker(
    cap_profile, monkeypatch
):
    """Multiple SSE reconnects must arm the ticker exactly once."""
    daemon = make_daemon()
    monkeypatch.setattr(node_daemon, "_backfill_interval", lambda cfg: 60)
    for _ in range(3):
        daemon._on_sse_connected()
    assert len(
        [t for t in threading.enumerate()
         if t is daemon._backfill_thread and t.is_alive()]
    ) == 1
    daemon._stop_event.set()
    daemon._backfill_thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Regression: task_created handler keeps its contract
# ---------------------------------------------------------------------------


def test_task_created_still_sweeps(cap_profile, monkeypatch):
    """The factored sweep preserves the original event-handler contract."""
    daemon = make_daemon()
    claimed = []
    monkeypatch.setattr(
        daemon, "_try_claim_and_run",
        lambda name: claimed.append(name),
    )
    daemon._handle_task_created({"task_id": "t_x"})
    assert claimed == ["image.generate.mflux", "file.ai"]