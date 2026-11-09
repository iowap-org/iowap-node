"""T-247: node capacity points — budget admission alongside max_parallel.

Ronny's point model (2026-11-09): each capability may declare a ``cost``
in points, the node declares a top-level ``budget`` in the active YAML.
A claim reserves only when BOTH admission axes pass:

    (1) running_points + cost <= budget        (points axis, T-247)
    (2) running_cap_count + 1 <= max_parallel  (parallel axis, T-081/229)

Both live in the T-246 atomic reserve (check + ledger update in ONE lock
section BEFORE the claim POST). Without ``budget`` the points axis is
off (default-off: a profile without budget behaves exactly as before).
The ledger stores the reserve-time cost so a profile reload between
reserve and release cannot corrupt the accounting.

Board example (mac-node): budget 10, mflux image-gen cost 9, clef
classify cost 1 — one mflux running leaves exactly one clef slot, a
second mflux waits although max_parallel would still allow it.
"""

from __future__ import annotations

import argparse
import threading
import time
from collections import deque
from datetime import UTC, datetime

import pytest

from nodes.common import node_cli, node_config, node_daemon
from nodes.common.cli import cli_capabilities
from nodes.common.node_config import CapabilityValidationError, validate_profile

# ---------------------------------------------------------------------------
# Part 1 — profile schema: `cost` (per capability) + `budget` (node level)
# ---------------------------------------------------------------------------


def test_cost_declared_normalizes_and_ships_in_profile():
    """A declared `cost` survives normalization unchanged (T-247)."""
    caps = validate_profile(
        {
            "capabilities": [
                {
                    "name": "image.gen.mflux",
                    "claimable": True,
                    "handler": "h",
                    "cost": 9,
                }
            ]
        }
    )
    assert caps[0]["cost"] == 9


def test_undeclared_cost_absent_from_normalized_cap():
    """The normalized dict stays byte-compatible for undeclared caps —
    `cost` only appears when the operator declares it."""
    caps = validate_profile(
        {
            "capabilities": [
                {"name": "doc.clef", "claimable": True, "handler": "h"}
            ]
        }
    )
    assert "cost" not in caps[0]
    assert node_config.capability_cost(caps[0]) == 1


@pytest.mark.parametrize("bad", [-1, "9", True, 1.5])
def test_cost_rejects_non_points_values(bad):
    """cost must be a non-negative integer — anything else fails loud at
    validate/publish time (never a silent admission surprise)."""
    with pytest.raises(CapabilityValidationError) as err:
        validate_profile(
            {
                "capabilities": [
                    {
                        "name": "x.cap",
                        "claimable": True,
                        "handler": "h",
                        "cost": bad,
                    }
                ]
            }
        )
    assert "cost" in str(err.value)


@pytest.mark.parametrize("bad", [-1, "ten", True, 2.5])
def test_budget_rejects_non_points_values(bad):
    """The top-level `budget` must be a non-negative integer."""
    with pytest.raises(CapabilityValidationError) as err:
        validate_profile(
            {
                "budget": bad,
                "capabilities": [
                    {"name": "x.cap", "claimable": True, "handler": "h"}
                ],
            }
        )
    assert "'budget'" in str(err.value)


def test_budget_valid_profile_passes():
    """budget: 10 (the board's mac-node example) validates fine."""
    caps = validate_profile(
        {
            "budget": 10,
            "capabilities": [
                {"name": "doc.clef", "claimable": True, "handler": "h"}
            ],
        }
    )
    assert len(caps) == 1


def test_capability_cost_defaults_and_tolerates_hand_built_dicts():
    """Default cost is 1 point; declared values pass through; junk in a
    hand-built dict falls back to the default instead of crashing the
    admission hot path (profiles are pre-validated; this is a belt)."""
    assert node_config.capability_cost({}) == 1
    assert node_config.capability_cost({"cost": 9}) == 9
    assert node_config.capability_cost({"cost": 0}) == 0  # explicit free cap
    assert node_config.capability_cost({"cost": True}) == 1
    assert node_config.capability_cost({"cost": "9"}) == 1
    assert node_config.capability_cost({"cost": -3}) == 1


# ---------------------------------------------------------------------------
# Part 2 — `load_active_budget()` (fresh read, load_active_status pattern)
# ---------------------------------------------------------------------------


@pytest.fixture()
def relay_home(tmp_path, monkeypatch):
    """Isolate every relay state path; tests never touch ~/.relay."""
    monkeypatch.setattr(node_config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(node_config, "PROFILES_DIR", tmp_path / "profiles.d")
    monkeypatch.setattr(node_config, "ACTIVE_PATH", tmp_path / "node.yaml")
    return tmp_path


def _write_active(home, text: str) -> None:
    (home / "node.yaml").write_text(text, encoding="utf-8")


def test_load_active_budget_roundtrip(relay_home):
    _write_active(
        relay_home,
        "node_cli_path: '/x'\nstatus: online\nbudget: 10\ncapabilities: []\n",
    )
    assert node_config.load_active_budget() == 10


def test_load_active_budget_absent_is_none(relay_home):
    _write_active(relay_home, "capabilities: []\n")
    assert node_config.load_active_budget() is None
    # No file at all → also None.
    assert node_config.load_active_budget() is None


@pytest.mark.parametrize("bad", ["ten", -5, True, 2.5])
def test_load_active_budget_tolerates_invalid_hand_edits(relay_home, bad):
    """A hand-edited active file carries an invalid budget silently:
    the loader must degrade to None (axis off), not kill the daemon."""
    _write_active(relay_home, f"budget: {str(bad).lower()}\n")
    assert node_config.load_active_budget() is None


# ---------------------------------------------------------------------------
# Part 3 — SSE daemon: the T-246 atomic reserve gains the points axis
# ---------------------------------------------------------------------------

FLUX = {
    "name": "image.gen.mflux",
    "claimable": True,
    "max_parallel": 3,
    "cost": 9,
    "handler": "h",
}
CLEF = {
    "name": "doc.clef",
    "claimable": True,
    "max_parallel": 2,
    "cost": 1,
    "handler": "h",
}
CAPS = [FLUX, CLEF]


class FakeClient:
    """Like the T-246/T-244 claim sink, with optional POST latency."""

    def __init__(self, stages: list[dict] | None = None, claim_delay: float = 0.0):
        self._mutex = threading.Lock()
        self._stages = deque(stages or [])
        self.claim_delay = claim_delay
        self.claim_attempts: list[str] = []
        self.completed: list[tuple[str, str]] = []
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
        with self._mutex:
            self.completed.append((str(task_id), str(stage_id)))
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
    # T-247: the points ledger is part of the reserve state.
    d._in_flight_points = 0
    d._started_at = datetime.now(UTC)
    d._lock = threading.Lock()
    d._stop_event = threading.Event()
    d._backfill_thread = None
    d._backfill_armed = threading.Event()
    d._claim_wake = threading.Event()
    d._claim_hint = None
    return d


def test_second_heavy_claim_refused_at_budget_edge(monkeypatch):
    """Board example: mflux (cost 9) runs. A second mflux claim is
    refused although max_parallel=3 still has room — points serialize."""
    fake = FakeClient()
    d = make_daemon(fake)
    monkeypatch.setattr(node_daemon, "load_active_profile", lambda: CAPS)
    monkeypatch.setattr(node_daemon, "load_active_budget", lambda: 10)
    with d._lock:
        d._in_flight["image.gen.mflux"] = 1
        d._in_flight_points = 9

    d._try_claim_and_run("image.gen.mflux")

    assert fake.claim_attempts == [], (
        "second heavy claim must wait until budget frees up (T-247 points axis)"
    )


def test_light_claim_admitted_within_budget(monkeypatch):
    """mflux (9) runs, one clef (1) fits into the remaining point
    (9+1=10) even before considering max_parallel."""
    fake = FakeClient()
    d = make_daemon(fake)
    monkeypatch.setattr(node_daemon, "load_active_profile", lambda: CAPS)
    monkeypatch.setattr(node_daemon, "load_active_budget", lambda: 10)
    with d._lock:
        d._in_flight["image.gen.mflux"] = 1
        d._in_flight_points = 9

    d._try_claim_and_run("doc.clef")

    assert fake.claim_attempts == ["doc.clef"]


def test_points_axis_off_without_budget(monkeypatch):
    """No `budget` in node.yaml → the points axis is off (default-off):
    any cost is admitted, behaviour unchanged from pre-T-247."""
    fake = FakeClient()
    d = make_daemon(fake)
    heavy = [{"name": "heavy.x", "claimable": True, "max_parallel": 10, "cost": 99, "handler": "h"}]
    monkeypatch.setattr(node_daemon, "load_active_profile", lambda: heavy)
    monkeypatch.setattr(node_daemon, "load_active_budget", lambda: None)  # no budget
    with d._lock:
        d._in_flight["heavy.x"] = 7
        d._in_flight_points = 42

    d._try_claim_and_run("heavy.x")

    assert fake.claim_attempts == ["heavy.x"], (
        "without a budget the points axis must not refuse claims"
    )


def test_max_parallel_axis_still_blocks(monkeypatch):
    """The existing parallel axis stays intact: clef at max_parallel=2 is
    refused even though budget has room."""
    fake = FakeClient()
    d = make_daemon(fake)
    monkeypatch.setattr(node_daemon, "load_active_profile", lambda: CAPS)
    monkeypatch.setattr(node_daemon, "load_active_budget", lambda: 10)
    with d._lock:
        d._in_flight["doc.clef"] = 2
        d._in_flight_points = 2

    d._try_claim_and_run("doc.clef")

    assert fake.claim_attempts == []


def test_empty_claim_release_returns_points(monkeypatch):
    """An empty claim releases slot AND points at once (no sticky budget)."""
    fake = FakeClient()  # no stages
    d = make_daemon(fake)
    monkeypatch.setattr(node_daemon, "load_active_profile", lambda: CAPS)
    monkeypatch.setattr(node_daemon, "load_active_budget", lambda: 10)

    d._targeted_claim_sweep(["doc.clef"])

    assert fake.claim_attempts == ["doc.clef"]
    with d._lock:
        assert d._in_flight.get("doc.clef", 0) == 0
        assert d._in_flight_points == 0


def test_two_reserve_threads_cannot_exceed_budget(monkeypatch):
    """T-246 race, now on both axes: budget 3 + cost 2 → the second
    concurrent reserve is refused while the first POST is in flight."""
    cap = {"name": "chat.ai", "claimable": True, "max_parallel": 5, "cost": 2, "handler": "h"}
    fake = FakeClient(claim_delay=0.4)
    d = make_daemon(fake)
    monkeypatch.setattr(node_daemon, "load_active_profile", lambda: [cap])
    monkeypatch.setattr(node_daemon, "load_active_budget", lambda: 3)

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
        "second concurrent reserve posted although the budget forbids it "
        f"(attempts={fake.claim_attempts})"
    )


def test_completed_stage_release_returns_points(monkeypatch):
    """The run exit releases the reserved points in the `_run_stage`
    finally — the ledger is empty again right after the stage completes
    (T-244 wake then re-admits with the freed budget)."""
    stage = {"task_id": "t1", "stage_id": "s1", "payload": {}}
    fake = FakeClient([stage])
    d = make_daemon(fake)
    monkeypatch.setattr(node_daemon, "load_active_profile", lambda: CAPS)
    monkeypatch.setattr(node_daemon, "load_active_budget", lambda: 10)
    monkeypatch.setattr(node_daemon, "run_handler", lambda *a, **k: {"status": "completed"})

    d._try_claim_and_run("doc.clef")

    assert d.tasks_completed == 1
    with d._lock:
        assert d._in_flight.get("doc.clef", 0) == 0
        assert d._in_flight_points == 0, (
            "the reserved points must be released when the stage finishes"
        )


# ---------------------------------------------------------------------------
# Part 4 — CLI daemon (fleet default): the claim loop honours the budget
# ---------------------------------------------------------------------------

CLI_CAP = {
    "name": "doc.clef",
    "claimable": True,
    "max_parallel": 1,
    "cost": 2,
    "handler": "stub",
}


def _wait_until(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def _start_loop(daemon):
    worker = threading.Thread(
        target=daemon._claim_loop, daemon=True, name="claim-loop"
    )
    worker.start()
    return worker


def test_cli_daemon_loop_blocks_claim_over_budget(monkeypatch):
    """budget 1 + cost 2 → the claim POST must never fire."""
    fake = FakeClient()
    daemon = node_cli.Daemon(
        fake, cfg={"claim_interval": 300, "heartbeat_interval": 60, "max_retries": 2}
    )
    passes: list[int] = []

    def profile():
        passes.append(1)
        return [dict(CLI_CAP)]

    monkeypatch.setattr(node_cli, "load_active_profile", profile)
    monkeypatch.setattr(node_cli, "load_active_budget", lambda: 1)

    daemon.in_flight_points = 0
    worker = _start_loop(daemon)
    try:
        assert _wait_until(lambda: len(passes) >= 1, timeout=5.0)
        time.sleep(0.2)  # let the pass walk its (single) capability
        assert fake.claim_attempts == [], (
            "claim posted although cost exceeds the budget (T-247)"
        )
    finally:
        daemon._stop_event.set()
        worker.join(timeout=10)


def test_cli_daemon_loop_claims_within_budget_and_releases(monkeypatch):
    """budget 5 + cost 2 → claimed; with the stage run the reserved
    points return to 0 afterwards (the loop is ready for the next pass)."""
    stage = {"task_id": "t1", "stage_id": "s1", "payload": {}}
    fake = FakeClient([stage])
    daemon = node_cli.Daemon(
        fake, cfg={"claim_interval": 300, "heartbeat_interval": 60, "max_retries": 2}
    )
    monkeypatch.setattr(node_cli, "load_active_profile", lambda: [dict(CLI_CAP)])
    monkeypatch.setattr(node_cli, "load_active_budget", lambda: 5)
    monkeypatch.setattr(node_cli, "run_handler", lambda *a, **k: {"status": "completed"})
    daemon.in_flight_points = 0

    worker = _start_loop(daemon)
    try:
        assert _wait_until(lambda: len(fake.completed) >= 1, timeout=5.0)
    finally:
        daemon._stop_event.set()
        worker.join(timeout=10)

    # The stage ran through; later passes may re-attempt against the
    # (now empty) queue — only the first attempt is the admission proof.
    assert fake.completed == [("t1", "s1")]
    assert fake.claim_attempts[0] == "doc.clef"
    assert daemon.in_flight_points == 0, (
        "the points ledger must be released after the stage completes"
    )


# ---------------------------------------------------------------------------
# Part 5 — display: node-cli capabilities validate / diff / info
# ---------------------------------------------------------------------------


def test_validate_output_lists_cost(relay_home, monkeypatch, capsys):
    """`capabilities validate` shows the admission parameters per cap —
    cost falls back to 1 for undeclared caps."""
    (relay_home / "profiles.d").mkdir()
    (relay_home / "profiles.d" / "t247.yaml").write_text(
        "capabilities:\n"
        "  - name: image.gen.mflux\n"
        "    claimable: true\n"
        "    handler: h\n"
        "    cost: 9\n"
        "  - name: doc.clef\n"
        "    claimable: true\n"
        "    handler: h\n",
        encoding="utf-8",
    )
    rc = cli_capabilities._cmd_capabilities_validate(
        argparse.Namespace(profile="t247", log_level="error")
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "cost=9" in out and "cost=1" in out


def test_diff_flags_cost_change(capsys):
    """A cost change is a real operator decision — the diff must show it."""
    cli_capabilities._print_cap_diff(
        {"version": "1.0.0", "cost": 9}, {"version": "1.0.0"}
    )
    out = capsys.readouterr().out
    assert "cost: 9 -> None" in out


class FakeResp:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "name": "image.gen.mflux",
            "version": "1.0.0",
            "available": True,
        }


class FakeInfoClient:
    def __init__(self) -> None:
        self.meta = {"node_id": "NODE"}
        self.base_url = "http://127.0.0.1:1"

    def _get_with_retry(self, path: str) -> FakeResp:
        return FakeResp()


def test_info_prints_local_capacity_overlay(relay_home, capsys):
    """`capabilities info` gains the node-local overlay: the local cost /
    max_parallel from the active profile and the top-level budget."""
    _write_active(
        relay_home,
        "budget: 10\ncapabilities:\n"
        "  - name: image.gen.mflux\n    claimable: true\n"
        "    handler: h\n    cost: 9\n    max_parallel: 3\n",
    )
    rc = cli_capabilities._cmd_capabilities_info(
        FakeInfoClient(), argparse.Namespace(name="image.gen.mflux", json=False)
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "cost=9" in out
    assert "max_parallel=3" in out
    assert "budget: 10 points" in out.lower()


def test_info_prints_unset_budget_and_unknown_local_cap(relay_home, capsys):
    _write_active(relay_home, "capabilities: []\n")
    rc = cli_capabilities._cmd_capabilities_info(
        FakeInfoClient(), argparse.Namespace(name="image.gen.mflux", json=False)
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "(not in this node's active profile)" in out
    assert "(unset" in out
