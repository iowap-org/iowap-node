"""T-179: complete-by-script (opt-in) im Daemon (Phase 2 Stubs).

Plan Task 5: Capability-Flag ``complete_by_script: true`` (unter
``capabilities[].config``) → der Daemon versucht ``client.complete``
trotzdem als Fallback, ignoriert aber den 404 ("not claimed by this
node, or not in claimed status") sauber, wenn das Script sich selbst
completed hat. Ohne Flag verhält sich der Daemon exakt wie heute
(Regressionsschutz, stdout-Contract bleibt Default).

Jeder Test beginnt mit ``assert False  # TODO`` — Phase 3 (integrate)
füllt die Körper. Technik: FakeClient via MagicMock + ``Daemon._run_stage``
direkt aufrufen (wie node_daemon-Tests); ``nodes.common.node_daemon.run_handler``
wird gemockt. Exception-Form: exakt prüfen, wie RelayClient.complete
Fehler wirft (relay_client.py:472) — nicht raten.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nodes.common.node_daemon import SseDaemon as Daemon


@pytest.fixture
def client() -> MagicMock:
    c = MagicMock()
    c.base_url = "http://relay.test"
    return c


def _cap(complete_by_script: bool) -> dict:
    return {
        "name": "cap.x",
        "claimable": True,
        "handler": "noop",
        "config": {"complete_by_script": complete_by_script},
    }


def _stage() -> dict:
    return {"stage_id": "s_1", "task_id": "t_1", "payload": {}}


# ---------------------------------------------------------------------------
# Flag gesetzt: 404 beim Complete → als Erfolg gewertet
# ---------------------------------------------------------------------------


def test_flag_set_complete_404_counts_as_completed(client, monkeypatch):
    # client.complete wirft Exception mit 404-Muster → Flag gesetzt →
    # tasks_completed +1, tasks_failed unverändert, kein failure-Pfad.
    assert False  # TODO


def test_flag_set_404_does_not_increment_failed_tasks(client, monkeypatch):
    # Auch _failed_tasks[t_1] darf nicht wachsen (kein Reclaim-Budget-
    # Verbrauch für Script-completed Stages).
    assert False  # TODO


def test_flag_set_404_logs_treated_as_done(client, monkeypatch, caplog):
    # Plan-Wortlaut: "stage %s already completed by script — treating as done".
    assert False  # TODO


# ---------------------------------------------------------------------------
# Flag gesetzt, aber andere Exception → normaler Fehlerpfad
# ---------------------------------------------------------------------------


def test_flag_set_other_error_follows_normal_failure_path(client, monkeypatch):
    # z.B. 500/Netzwerkfehler → tasks_failed +1 wie bisher.
    assert False  # TODO


# ---------------------------------------------------------------------------
# Flag NICHT gesetzt → Regressionsschutz (Verhalten byte-identisch)
# ---------------------------------------------------------------------------


def test_flag_unset_404_follows_normal_failure_path(client, monkeypatch):
    # Ohne Flag bleibt der bisherige Fehlerpfad: tasks_failed +1.
    assert False  # TODO


def test_flag_unset_successful_complete_counts_as_completed(client, monkeypatch):
    # Happy Path unverändert: complete klappt → tasks_completed +1.
    assert False  # TODO


def test_flag_absent_key_treated_as_unset(client, monkeypatch):
    # cap ohne "config"-Key oder mit config ohne complete_by_script →
    # wie Flag nicht gesetzt (bool((cap.get("config") or {}).get(...))).
    assert False  # TODO


# ---------------------------------------------------------------------------
# Helper: _is_already_completed (Plan Task 5)
# ---------------------------------------------------------------------------


def test_is_already_completed_matches_server_404_pattern(client):
    # Server-Text (iowap-server/api/v2/scheduler.py:115):
    # "not claimed by this node, or not in claimed status" + Status 404.
    assert False  # TODO


def test_is_already_completed_rejects_other_errors(client):
    # 500 mit gleichem Text? Nein — 404 + Muster muss zusammen passen.
    assert False  # TODO