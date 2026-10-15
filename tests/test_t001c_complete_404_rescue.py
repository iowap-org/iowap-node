"""T-001c-c: node-side complete-404 rescue — preserve handler results.

Before this change, a ``complete`` call rejected with 404 (stage expired,
re-queued, or failed server-side while the handler was still running)
incremented the failure counters and LOGGED ONLY — the handler result
landed in the bit bucket (T-001c Störfall 2026-09-05: "Result verworfen
→ Flow failed").

Now: both call sites (``nodes/common/node_daemon.py`` SseDaemon._run_stage
and ``nodes/common/node_cli.py`` Daemon._run_stage) rescue the result by
posting it as an ``[result-rescue]`` info-note on the task (T-052/
T-154 note channel, note rides in TaskView) before counting the failure.
The T-179 already-completed branch (opt-in ``complete_by_script``) stays
strictly a success with NO rescue note; non-404 failures (timeouts,
network blips) stay retryable and rescue NOTHING.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from unittest.mock import MagicMock

import httpx
import pytest

from nodes.common import node_cli, node_daemon, node_utils
from nodes.common.relay_client import RelayClient


class FakeExc(Exception):
    """Exception with an httpx.HTTPStatusError surface (404 from relay)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.response = httpx.Response(status, json={"detail": detail})


CLAIM_404 = "stage s_1 not claimed by this node, or not in claimed status"
GONE_404 = "Stage not found, not claimed by this node, or not in claimable/completable status"


def _daemon(module, clsname: str):
    d = getattr(module, clsname).__new__(getattr(module, clsname))
    d.client = MagicMock()
    d.tasks_completed = 0
    d.tasks_failed = 0
    d._failed_tasks = {}
    # both naming conventions: SseDaemon._in_flight, node_cli.Daemon.in_flight
    d._in_flight = {}
    d.in_flight = {}
    d._lock = threading.Lock()
    d._stop_event = threading.Event()
    return d


def _cap(config: dict | None = None) -> dict:
    return {"name": "chat.ai", "handler": "chat.py", "config": config or {}}


def _stage() -> dict:
    return {"stage_id": "s_1", "task_id": "t_1"}


# ── Rescue helper unit ──────────────────────────────────────────


class TestRescueNoteHelper:
    def test_builds_info_note_with_result_payload(self):
        client = MagicMock()
        ok = node_utils.post_result_rescue_note(
            client, "t_1", "s_1", {"stdout": "hi"}, reason="404"
        )
        assert ok is True
        args, kwargs = client.add_task_note.call_args
        assert args[0] == "t_1"
        assert "[result-rescue]" in args[1]
        assert "s_1" in args[1]
        assert '"stdout": "hi"' in args[1]
        assert kwargs.get("kind") == "info"

    def test_caps_oversized_result(self):
        client = MagicMock()
        node_utils.post_result_rescue_note(
            client, "t_1", "s_1", {"blob": "x" * 50_000}, reason="404"
        )
        msg = client.add_task_note.call_args[0][1]
        assert len(msg) <= 4_500, "long results are capped, not truncated silently"
        assert msg.endswith("...")

    def test_non_serializable_falls_back_to_repr(self):
        client = MagicMock()

        class Weird:
            def __str__(self):
                return "<weird>"

        node_utils.post_result_rescue_note(client, "t_1", "s_1", {"x": Weird()}, reason="404")
        msg = client.add_task_note.call_args[0][1]
        assert "weird" in msg

    def test_no_task_id_is_noop_false(self):
        client = MagicMock()
        assert node_utils.post_result_rescue_note(client, None, "s_1", {"a": 1}) is False
        client.add_task_note.assert_not_called()

    def test_note_failure_swallowed_false(self):
        client = MagicMock()
        client.add_task_note.side_effect = httpx.HTTPStatusError(
            "boom", request=MagicMock(), response=MagicMock()
        )
        assert node_utils.post_result_rescue_note(client, "t_1", "s_1", {}) is False


# ── Daemon rescue branch ────────────────────────────────────────


class TestDaemonRescue:
    def test_complete_404_rescues_result_as_note(self, monkeypatch):
        d = _daemon(node_daemon, "SseDaemon")
        d.client.complete = MagicMock(
            side_effect=FakeExc(404, GONE_404),
        )
        monkeypatch.setattr(
            node_daemon, "run_handler", MagicMock(return_value={"status": "completed", "result": {"answer": "worked"}})
        )
        d._run_stage(_cap(), _stage())
        assert d.client.add_task_note.call_count == 1, (
            "T-001c-c: a lost stage must rescue its handler result as a note"
        )
        assert d.tasks_failed == 1 and d.tasks_completed == 0

    def test_already_completed_script_success_has_no_note(self, monkeypatch):
        # T-179 regression: opt-in + already-completed 404 = success, no rescue
        d = _daemon(node_daemon, "SseDaemon")
        d.client.complete = MagicMock(side_effect=FakeExc(404, CLAIM_404))
        monkeypatch.setattr(
            node_daemon, "run_handler", MagicMock(return_value={"status": "completed", "result": {}})
        )
        d._run_stage(_cap({"complete_by_script": True}), _stage())
        assert d.tasks_completed == 1 and d.tasks_failed == 0
        d.client.add_task_note.assert_not_called()

    def test_non_404_failure_calls_no_note(self, monkeypatch):
        d = _daemon(node_daemon, "SseDaemon")
        d.client.complete = MagicMock(
            side_effect=httpx.ReadTimeout("handler timeout")
        )
        monkeypatch.setattr(
            node_daemon, "run_handler", MagicMock(return_value={"status": "completed", "result": {}})
        )
        d._run_stage(_cap(), _stage())
        assert d.client.add_task_note.call_count == 0, (
            "timeouts are retryable — rescue is for lost stages (404) only"
        )
        assert d.tasks_failed == 1


# ── CLI Daemon rescue branch ────────────────────────────────────


class TestCliDaemonRescue:
    def test_complete_404_rescues_result_as_note(self, monkeypatch):
        d = _daemon(node_cli, "Daemon")
        d.client.complete = MagicMock(side_effect=FakeExc(404, GONE_404))
        monkeypatch.setattr(
            node_cli, "run_handler", MagicMock(return_value={"status": "completed", "result": {"answer": "x"}})
        )
        d._run_stage(_cap(), _stage())
        assert d.client.add_task_note.call_count == 1
        assert d.tasks_failed == 1 and d.tasks_completed == 0

    def test_non_404_failure_calls_no_note(self, monkeypatch):
        d = _daemon(node_cli, "Daemon")
        d.client.complete = MagicMock(side_effect=httpx.ConnectError("net down"))
        monkeypatch.setattr(
            node_cli, "run_handler", MagicMock(return_value={"status": "completed", "result": {}})
        )
        d._run_stage(_cap(), _stage())
        assert d.client.add_task_note.call_count == 0
        assert d.tasks_failed == 1