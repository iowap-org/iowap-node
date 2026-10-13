"""T-179 Task 5: complete-by-script (opt-in) in the daemon (phase 2 stubs).

Every test starts with ``assert False  # TODO`` — phase 3 (integrate)
fills the bodies. The contract comes from the plan (Task 5):

Capability flag ``config.complete_by_script: true`` → the daemon attempts
complete anyway (fallback), but cleanly treats a 404 carrying the server
pattern ``"not claimed by this node, or not in claimed status"`` as
success (tasks_completed + 1). Without the flag: failure as before
(regression protection for the stdout contract).

Real contract of ``SseDaemon._run_stage``: ``(cap, stage)`` — the flag
lives in ``cap["config"]["complete_by_script"]``.
"""
from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from unittest.mock import MagicMock

import httpx
import pytest

from nodes.common import node_daemon


class FakeExc(Exception):
    """Exception with an httpx.HTTPStatusError surface for _is_already_completed."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.response = httpx.Response(status, json={"detail": detail})


@pytest.fixture
def daemon() -> node_daemon.SseDaemon:
    d = node_daemon.SseDaemon.__new__(node_daemon.SseDaemon)
    d.client = MagicMock()
    d.cfg = {"heartbeat_interval": 30}
    d.server_probe = {"ok": False, "error": "probe pending"}
    d.tasks_completed = 0
    d.tasks_failed = 0
    d._failed_tasks = {}
    d._in_flight = {}
    d._started_at = datetime.now(UTC)
    d._lock = threading.Lock()
    d._stop_event = threading.Event()
    return d


def _cap(config: dict | None = None) -> dict:
    return {"name": "chat.ai", "handler": "chat.py", "config": config or {}}


def _stage() -> dict:
    return {"stage_id": "s_1", "task_id": "t_1"}


def test_is_already_completed_matches_404_claim_pattern():
    # server text (iowap-server/api/v2/scheduler.py:115): 404 + detail pattern
    # "not claimed by this node, or not in claimed status".
    assert node_daemon._is_already_completed(
        FakeExc(404, "stage s1 not claimed by this node, or not in claimed status")
    )


def test_is_already_completed_rejects_other_status_or_detail():
    # 404 without the pattern → NO (a real 404 stays an error).
    assert not node_daemon._is_already_completed(
        FakeExc(404, "stage not found")
    )
    # pattern but not a 404 → NO.
    assert not node_daemon._is_already_completed(
        FakeExc(409, "not claimed by this node, or not in claimed status")
    )
    # different exception class without a response attribute → NO.
    assert not node_daemon._is_already_completed(ValueError("boom"))


def test_script_complete_404_treated_as_done(daemon, monkeypatch, caplog):
    # flag set + 404 pattern → tasks_completed + 1, no failure.
    daemon.client.complete = MagicMock(
        side_effect=FakeExc(
            404, "stage s1 not claimed by this node, or not in claimed status"
        )
    )
    monkeypatch.setattr(
        node_daemon, "run_handler",
        MagicMock(return_value={"status": "ok", "stdout": "{}"}))
    with caplog.at_level(logging.INFO):
        rc = daemon._run_stage(_cap({"complete_by_script": True}), _stage())
    assert rc is None  # _run_stage is void — the counter is the contract
    assert daemon.tasks_completed == 1
    assert daemon.tasks_failed == 0
    assert "already completed by script" in caplog.text


def test_no_flag_404_failure_as_before(daemon, monkeypatch):
    # flag NOT set + 404 → failure path as before (regression).
    daemon.client.complete = MagicMock(
        side_effect=FakeExc(
            404, "stage s1 not claimed by this node, or not in claimed status"
        )
    )
    monkeypatch.setattr(
        node_daemon, "run_handler",
        MagicMock(return_value={"status": "ok", "stdout": "{}"}))
    daemon._run_stage(_cap({}), _stage())
    assert daemon.tasks_completed == 0
    assert daemon.tasks_failed == 1


def test_no_flag_normal_complete_unaffected(daemon, monkeypatch):
    # regression: stdout contract without the flag — a normal complete works
    # exactly as it does today.
    daemon.client.complete = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(
        node_daemon, "run_handler",
        MagicMock(return_value={"status": "ok", "stdout": "{}"}))
    daemon._run_stage(_cap({}), _stage())
    assert daemon.tasks_completed == 1
    assert daemon.tasks_failed == 0


def test_flag_with_real_error_still_fails(daemon, monkeypatch):
    # flag set, but the error is NOT a 404 pattern (e.g. 500) →
    # normal failure path (plan: "previous failure path unchanged").
    daemon.client.complete = MagicMock(side_effect=FakeExc(500, "internal server error"))
    monkeypatch.setattr(
        node_daemon, "run_handler",
        MagicMock(return_value={"status": "ok", "stdout": "{}"}))
    daemon._run_stage(_cap({"complete_by_script": True}), _stage())
    assert daemon.tasks_completed == 0
    assert daemon.tasks_failed == 1