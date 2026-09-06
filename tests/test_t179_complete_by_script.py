"""T-179 Task 5: complete-by-script (opt-in) im Daemon (Phase 2 Stubs).

Jeder Test beginnt mit ``assert False  # TODO`` — Phase 3 (integrate)
füllt die Körper. Der Contract kommt aus dem Plan (Task 5):

Capability-Flag ``config.complete_by_script: true`` → der Daemon versucht
complete trotzdem (Fallback), ignoriert aber 404 mit dem Server-Muster
``"not claimed by this node, or not in claimed status"`` sauber als
success (tasks_completed + 1). Ohne Flag: failure wie bisher
(Regressionsschutz für den stdout-Contract).

Realer Contract von ``SseDaemon._run_stage``: ``(cap, stage)`` — das Flag
liegt in ``cap["config"]["complete_by_script"]``.
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
    """Exception mit httpx.HTTPStatusError-Fläche für _is_already_completed."""

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
    # Server-Text (iowap-server/api/v2/scheduler.py:115): 404 + Detail-Muster
    # "not claimed by this node, or not in claimed status".
    assert node_daemon._is_already_completed(
        FakeExc(404, "stage s1 not claimed by this node, or not in claimed status")
    )


def test_is_already_completed_rejects_other_status_or_detail():
    # 404 ohne das Muster → NEIN (echter 404 bleibt ein Fehler).
    assert not node_daemon._is_already_completed(
        FakeExc(404, "stage not found")
    )
    # Muster, aber kein 404 → NEIN.
    assert not node_daemon._is_already_completed(
        FakeExc(409, "not claimed by this node, or not in claimed status")
    )
    # Andere Exception-Klasse ohne response-Attribut → NEIN.
    assert not node_daemon._is_already_completed(ValueError("boom"))


def test_script_complete_404_treated_as_done(daemon, monkeypatch, caplog):
    # Flag gesetzt + 404-Muster → tasks_completed + 1, kein failure.
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
    assert rc is None  # _run_stage ist void — der Zähler ist der Contract
    assert daemon.tasks_completed == 1
    assert daemon.tasks_failed == 0
    assert "already completed by script" in caplog.text


def test_no_flag_404_failure_as_before(daemon, monkeypatch):
    # Flag NICHT gesetzt + 404 → failure-Pfad wie bisher (Regression).
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
    # Regression: stdout-Contract ohne Flag — normaler complete funktioniert
    # exakt wie heute.
    daemon.client.complete = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(
        node_daemon, "run_handler",
        MagicMock(return_value={"status": "ok", "stdout": "{}"}))
    daemon._run_stage(_cap({}), _stage())
    assert daemon.tasks_completed == 1
    assert daemon.tasks_failed == 0


def test_flag_with_real_error_still_fails(daemon, monkeypatch):
    # Flag gesetzt, aber der Fehler ist KEIN 404-Muster (z. B. 500) →
    # normaler Fehlerpfad (Plan: "bisheriger Fehlerpfad unverändert").
    daemon.client.complete = MagicMock(side_effect=FakeExc(500, "internal server error"))
    monkeypatch.setattr(
        node_daemon, "run_handler",
        MagicMock(return_value={"status": "ok", "stdout": "{}"}))
    daemon._run_stage(_cap({"complete_by_script": True}), _stage())
    assert daemon.tasks_completed == 0
    assert daemon.tasks_failed == 1