"""T-245: targeted claims — SSE payloads actually reach the handlers.

Node side of the pair (iowap-server T-245 adds capability fields):

- The server's SSE ``data`` line is the FULL envelope
  ``{"type", "timestamp", "payload"}``; the real payload sits one level
  deeper. The historical reader treated the envelope AS the payload —
  ``stage_claimed`` saw no capability (dead trigger) and
  ``task_created`` stayed cap-blind (broadside sweep over every
  advertised capability per event: N × 0.2–1.4 s claim latency;
  11–12 s measured on felix, 5–30 s on NovaForge).
- With ``payload.capabilities`` (T-245 server) ``task_created`` claims
  only the capabilities the task actually needs (advertised ∩ claimable
  ∩ capacity). No caps field → broadside fallback (contract lock, for
  servers that predate the field).
- ``stage_claimed`` with ``payload.capability`` → one targeted claim.
- The post-completion wake (T-244) carries the completed capability as
  a hint — the woken sweep claims only the hint (broadside stays the
  periodic/fallback path).
"""

import threading
import time
from datetime import UTC, datetime

from nodes.common import node_daemon

CAPS = [
    {"name": "chat.ai", "claimable": True, "max_parallel": 1, "handler": "h"},
    {"name": "code.ai", "claimable": True, "max_parallel": 1, "handler": "h"},
]


class FakeClient:
    """Claim sink: records claim attempts, never hands out a stage."""

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self.claim_attempts: list[str] = []
        self._completed: list[tuple[str, str]] = []
        self.meta = {"node_id": "NODE"}
        self.base_url = "http://127.0.0.1:1"

    def claim(self, capability: str):
        with self._mutex:
            self.claim_attempts.append(capability)

    def complete(self, task_id: str, stage_id: str, result: dict) -> dict:
        with self._mutex:
            self._completed.append((str(task_id), str(stage_id)))
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


def _pin_profile(monkeypatch) -> None:
    monkeypatch.setattr(node_daemon, "load_active_profile", lambda: CAPS)


def _wait_until(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_task_created_with_capabilities_claims_only_them(monkeypatch):
    fake = FakeClient()
    d = make_daemon(fake)
    _pin_profile(monkeypatch)

    d._on_event(
        {
            "type": "task_created",
            "data": {"payload": {"task_id": "t1", "capabilities": ["chat.ai"]}},
        }
    )

    # code.ai was NOT tried — no broadside beside the targeted claim.
    assert fake.claim_attempts == ["chat.ai"]


def test_task_created_keeps_payload_order_skips_unknown(monkeypatch):
    fake = FakeClient()
    d = make_daemon(fake)
    _pin_profile(monkeypatch)

    d._on_event(
        {
            "type": "task_created",
            "data": {
                "payload": {
                    "task_id": "t1",
                    "capabilities": ["code.ai", "not.advertised", "chat.ai"],
                }
            },
        }
    )

    assert fake.claim_attempts == ["code.ai", "chat.ai"]


def test_task_created_without_caps_falls_back_to_broadside(monkeypatch):
    # CONTRACT LOCK: a task_created without the T-245 capabilities field
    # (server predating the payload enrichment) still claims — the node
    # must never go deaf against an older relay, so this is expected to
    # pass before the implementation too.
    fake = FakeClient()
    d = make_daemon(fake)
    _pin_profile(monkeypatch)

    d._on_event({"type": "task_created", "data": {"payload": {"task_id": "t1"}}})

    assert fake.claim_attempts == ["chat.ai", "code.ai"]


def test_stage_claimed_payload_targets_single_cap(monkeypatch):
    fake = FakeClient()
    d = make_daemon(fake)
    _pin_profile(monkeypatch)

    d._on_event(
        {
            "type": "stage_claimed",
            "data": {
                "payload": {
                    "task_id": "t1",
                    "stage_id": "s1",
                    "node_id": "SOMEOTHER",
                    "capability": "code.ai",
                }
            },
        }
    )

    assert fake.claim_attempts == ["code.ai"]


def test_wake_hint_claims_only_the_hint(monkeypatch):
    fake = FakeClient()
    d = make_daemon(fake)
    _pin_profile(monkeypatch)
    # One-shot fallback context: ticker absent, connect-context armed.
    d._backfill_armed.set()

    d._wake_backfill("chat.ai")

    swept = _wait_until(lambda: len(fake.claim_attempts) >= 1, timeout=5.0)
    assert swept, "wake did not run the hint sweep"
    assert fake.claim_attempts == ["chat.ai"], (
        "hint wake must not broadside other capabilities"
    )