"""T-246: the slot reserve must precede the claim POST.

The SSE daemon now claims from SEVERAL threads (T-244/T-245): the woken
ticker/one-shot sweep AND the SSE-thread backlog handler. Checking
``in_flight`` separately from the HTTP POST left a race window where two
callers both saw a free slot and claimed past the capability's
``max_parallel`` — measured live 2026-11-02 (two hermes-test stages
claimed in the same second at max_parallel=1). Harmless for a ping,
breeder for RAM-heavy caps (mflux/flux3-hq, related to T-236).

Fix contract: check + increment happen in ONE lock section BEFORE the
``claim`` HTTP call; every non-run exit releases the slot; the run exit
releases it in the ``_run_stage`` finally.
"""

import threading
import time
from collections import deque
from datetime import UTC, datetime

from nodes.common import node_daemon

CAPS = [
    {"name": "chat.ai", "claimable": True, "max_parallel": 1, "handler": "h"},
    {"name": "code.ai", "claimable": True, "max_parallel": 1, "handler": "h"},
]


class FakeClient:
    """Like the T-245 claim sink, with optional POST latency + stages."""

    def __init__(
        self, stages: list[dict] | None = None, claim_delay: float = 0.0
    ) -> None:
        self._mutex = threading.Lock()
        self._stages = deque(stages or [])
        self.claim_delay = claim_delay
        self.claim_attempts: list[str] = []
        self.meta = {"node_id": "NODE"}
        self.base_url = "http://127.0.0.1:1"

    def claim(self, capability: str):
        if self.claim_delay:
            time.sleep(self.claim_delay)
        with self._mutex:
            self.claim_attempts.append(capability)
            try:
                return self._stages.popleft()
            except IndexError:
                return None

    def complete(self, task_id: str, stage_id: str, result: dict) -> dict:
        return {}

    def add_task_note(self, task_id: str, text: str, kind: str = "note") -> dict:
        return {}

    def _current_backoff(self) -> int:
        return 0


def make_daemon(fake: FakeClient) -> node_daemon.SseDaemon:
    d = node_daemon.SseDaemon.__new__(node_daemon.SseDaemon)
    d.client = fake
    d.cfg = {"heartbeat_interval": 30, "backfill_interval": 60, "max_retries": 2}
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
    d._claim_wake = threading.Event()
    d._claim_hint = None
    return d


def test_parallel_claim_paths_cannot_exceed_max_parallel(monkeypatch):
    """Two concurrent task_created handlers for the same capability at
    max_parallel=1 → exactly ONE claim attempt. This is the live race
    from the T-245 re-probe (two claims in the same second)."""
    fake = FakeClient(claim_delay=0.4)
    d = make_daemon(fake)
    monkeypatch.setattr(node_daemon, "load_active_profile", lambda: CAPS)

    def fire():
        d._on_event(
            {
                "type": "task_created",
                "data": {"payload": {"task_id": "t", "capabilities": ["chat.ai"]}},
            }
        )

    a = threading.Thread(target=fire, daemon=True)
    a.start()
    time.sleep(0.1)  # A is inside its (slow) claim POST now
    b = threading.Thread(target=fire, daemon=True)
    b.start()
    a.join(timeout=5)
    b.join(timeout=5)

    assert fake.claim_attempts == ["chat.ai"], (
        "second concurrent claimer posted although max_parallel=1 — "
        f"attempts={fake.claim_attempts}, in_flight={d._in_flight}"
    )
    with d._lock:
        assert d._in_flight.get("chat.ai") in (None, 0), (
            "slot must be released after the empty claim"
        )


def test_empty_claim_releases_reserved_slot(monkeypatch):
    """Claim returns nothing → the reserved slot is released immediately
    (otherwise the capability would look busy until a stage runs)."""
    fake = FakeClient()  # no stages
    d = make_daemon(fake)
    monkeypatch.setattr(node_daemon, "load_active_profile", lambda: CAPS)

    d._targeted_claim_sweep(["chat.ai"])

    assert fake.claim_attempts == ["chat.ai"]
    with d._lock:
        assert d._in_flight.get("chat.ai", 0) == 0


def test_retry_budget_skip_releases_reserved_slot(monkeypatch):
    """A stage for an exhausted-retries task is skipped AFTER the claim —
    the reserved slot must be released, and the stage must never run."""
    fake = FakeClient([{"task_id": "t-fail", "stage_id": "s1"}])
    d = make_daemon(fake)
    monkeypatch.setattr(node_daemon, "load_active_profile", lambda: CAPS)
    d._failed_tasks["t-fail"] = 2  # >= max_retries

    d._targeted_claim_sweep(["chat.ai"])

    assert fake.claim_attempts == ["chat.ai"]  # claimed, then skipped
    assert d.tasks_completed == 0 and d.tasks_failed == 0
    with d._lock:
        assert d._in_flight.get("chat.ai", 0) == 0


def test_unknown_capability_never_claims(monkeypatch):
    """A capability this node doesn't know must not produce a claim POST
    (the old order claimed first and dropped the stage — lease waste)."""
    fake = FakeClient()
    d = make_daemon(fake)
    monkeypatch.setattr(node_daemon, "load_active_profile", lambda: CAPS)

    d._try_claim_and_run("ghost.cap")

    assert fake.claim_attempts == []