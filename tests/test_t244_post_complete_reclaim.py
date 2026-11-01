"""T-244: post-completion work check — the daemons must not sit out the
full claim/backfill interval when a stage just completed.

Ronny's request (2026-11-02): "wann immer ein task completed wird soll der
node daemon automatisch schauen ob noch weitere arbeit auf ihn wartet".

Three observable slices of one mechanic (``_claim_wake`` event):

1. CLI daemon claim loop (``node_cli.Daemon._claim_loop``): a claimed
   stage is executed synchronously inside the loop pass; when the pass
   ends, the loop used to sleep the full claim interval before re-checking
   its own queue.  With the wake, the next pass happens immediately.
2. SSE daemon backfill ticker (``node_daemon.SseDaemon._backfill_ticker_loop``):
   the periodic sweep used to fire only on the fixed ticker tick.  With
   the wake, a completed stage triggers the sweep instantly.
3. SSE daemon with the ticker disabled (``backfill_interval: 0``): the
   post-completion check must still happen — a one-shot sweep thread.
"""

from __future__ import annotations

import threading
import time
from collections import deque

import pytest

from nodes.common import node_cli, node_daemon

CAP = "a.cap"

CAP_DEF = {
    "name": CAP,
    "claimable": True,
    "max_parallel": 1,
    "handler": "stub",
}


def _stage(n: int) -> dict:
    return {
        "task_id": f"t-{n}",
        "stage_id": f"st-{n}",
        "capability": CAP,
        "payload": {},
    }


class FakeClient:
    """Claim/complete sink: hands out queued stages once each."""

    def __init__(self, stages: list[dict] | None = None) -> None:
        self._stages: deque[dict] = deque(stages or [])
        self._mutex = threading.Lock()
        self.claim_attempts: list[str] = []
        self.completed: list[tuple[str, str]] = []
        self.meta = {"node_id": "NODE"}
        self.base_url = "http://127.0.0.1:1"

    def claim(self, capability: str) -> dict | None:
        with self._mutex:
            self.claim_attempts.append(capability)
            try:
                return self._stages.popleft()
            except IndexError:
                return None

    def complete(self, task_id: str, stage_id: str, result: dict) -> dict:
        with self._mutex:
            self.completed.append((str(task_id), str(stage_id)))
        return {}

    def add_task_note(self, task_id: str, text: str, kind: str = "note") -> dict:
        return {}

    def _current_backoff(self) -> int:
        return 0


def _wait_until(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


@pytest.fixture()
def daemon_module_stubs(monkeypatch):
    """Stub the profile loader and the handler runner so a stage run
    completes instantly with no external server."""

    def install(mod):
        monkeypatch.setattr(mod, "load_active_profile", lambda: [dict(CAP_DEF)])
        monkeypatch.setattr(
            mod, "run_handler", lambda *a, **k: {"status": "completed"}
        )

    return install


def test_cli_daemon_reclaims_immediately_after_complete(
    daemon_module_stubs, monkeypatch
):
    """After a completed stage the claim loop must start the next pass
    right away — NOT after the full claim interval (300s here)."""
    daemon_module_stubs(node_cli)
    fake = FakeClient([_stage(1), _stage(2)])
    daemon = node_cli.Daemon(
        fake,
        cfg={
            "claim_interval": 300,
            "heartbeat_interval": 60,
            "max_retries": 2,
        },
    )
    worker = threading.Thread(
        target=daemon._claim_loop, daemon=True, name="claim-loop"
    )
    worker.start()
    try:
        got_two = _wait_until(
            lambda: len(fake.completed) >= 2, timeout=10.0
        )
    finally:
        daemon._stop_event.set()
        worker.join(timeout=10)

    assert got_two, (
        "claim loop did not reach the second stage within 10s despite "
        f"claim_interval=300 — post-complete wake missing "
        f"(completed={fake.completed}, attempts={len(fake.claim_attempts)})"
    )


def test_sse_daemon_backfill_ticker_sweeps_immediately_after_complete(
    daemon_module_stubs,
):
    """A completed stage must wake the backfill ticker: the sweep fires
    instantly instead of waiting the full backfill interval (300s)."""
    daemon_module_stubs(node_daemon)
    fake = FakeClient([_stage(1), _stage(2)])
    daemon = node_daemon.SseDaemon(
        fake,
        cfg={
            "heartbeat_interval": 60,
            "backfill_interval": 300,
            "max_retries": 2,
        },
    )
    ticker = threading.Thread(
        target=daemon._backfill_ticker_loop, daemon=True, name="backfill-ticker"
    )
    # Register the ticker the way _arm_backfill_ticker does, so the
    # wake must travel through the ticker (no one-shot fallback).
    daemon._backfill_armed.set()
    daemon._backfill_thread = ticker
    ticker.start()
    try:
        # One completed stage, synchronously (mirrors the SSE-thread run).
        daemon._run_stage(dict(CAP_DEF), _stage(1))
        got_second = _wait_until(
            lambda: len(fake.completed) >= 2, timeout=10.0
        )
    finally:
        daemon._stop_event.set()
        ticker.join(timeout=10)

    assert got_second, (
        "backfill ticker did not sweep for the queued stage within 10s "
        f"despite backfill_interval=300 — post-complete wake missing "
        f"(completed={fake.completed}, attempts={len(fake.claim_attempts)})"
    )


def test_sse_daemon_post_complete_sweep_without_ticker(daemon_module_stubs):
    """With backfill_interval=0 (ticker disabled) the post-completion
    check must still happen: a one-shot sweep claims for the capability."""
    daemon_module_stubs(node_daemon)
    fake = FakeClient()  # no queued stages — the sweep is still observable
    daemon = node_daemon.SseDaemon(
        fake,
        cfg={
            "heartbeat_interval": 60,
            "backfill_interval": 0,  # ticker disabled
            "max_retries": 2,
        },
    )
    # The one-shot fallback requires an SSE-connect context (in
    # production every stage run implies a prior connect that armed the
    # ticker; with interval=0 the arm leaves no thread behind).
    daemon._backfill_armed.set()

    daemon._run_stage(dict(CAP_DEF), _stage(1))

    swept = _wait_until(
        lambda: len(fake.claim_attempts) >= 1, timeout=5.0
    )
    assert swept, (
        "post-completion sweep did not run with the ticker disabled "
        f"(claim_attempts={fake.claim_attempts})"
    )