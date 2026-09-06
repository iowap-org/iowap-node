"""T-179: complete/note mit env-defaults (Phase 2 Stubs).

Plan Task 4: ``node-cli complete`` und ``node-cli task note`` funktionieren
ohne explizite IDs, wenn ``RELAY_TASK_ID``/``RELAY_STAGE_ID`` gesetzt sind
(werden vom handler_runner an jeden Handler vergeben). Ohne Env und ohne
Flags → stderr-Meldung + exit 1.

Jeder Test beginnt mit ``assert False  # TODO`` — Phase 3 (integrate)
füllt die Körper. Beachte: Die ``required=True``-Änderung an
``p_complete.add_argument("--task", ...)`` ist Teil von Phase 3
(Plan-Task 4); dieser Testfile ruft die Command-Handler direkt auf
(FakeClient-Technik wie tests/test_t178_register_and_server.py).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nodes.common.cli import cli_task
from nodes.common.node_cli import _cmd_complete


@pytest.fixture
def client() -> MagicMock:
    c = MagicMock()
    c.base_url = "http://relay.test"
    return c


@pytest.fixture
def result_file(tmp_path: Path) -> Path:
    f = tmp_path / "result.json"
    f.write_text(json.dumps({"answer": "ok"}), encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# _cmd_complete (node_cli) — env-fallback RELAY_TASK_ID / RELAY_STAGE_ID
# ---------------------------------------------------------------------------


def test_complete_uses_env_ids_when_flags_missing(client, result_file, monkeypatch):
    assert False  # TODO


def test_complete_explicit_flags_win_over_env(client, result_file, monkeypatch):
    assert False  # TODO


def test_complete_without_env_or_flags_returns_exit_1(
        client, result_file, monkeypatch, capsys):
    # Plan-Logik: "complete: no --task/--task-id and no RELAY_TASK_ID/
    # RELAY_STAGE_ID in env" auf stderr + return 1.
    assert False  # TODO


def test_complete_partial_env_returns_exit_1(
        client, result_file, monkeypatch, capsys):
    # Nur RELAY_TASK_ID gesetzt, RELAY_STAGE_ID fehlt (oder umgekehrt) →
    # weiterhin exit 1, kein Halbwissen.
    assert False  # TODO


def test_complete_env_only_json_output(client, result_file, monkeypatch, capsys):
    # --json-Flag: kompakte JSON-Ausgabe der complete-Antwort.
    assert False  # TODO


# ---------------------------------------------------------------------------
# p_complete parser-Vertrag (Phase 3 macht --task optional; parser-Kontakt
# hier nur als Vertrags-Check, kein Verhalten)
# ---------------------------------------------------------------------------


def test_complete_parser_task_flag_becomes_optional():
    # Plan Task 4: p_complete.add_argument("--task", required=True) →
    # required=False. Parser bauen und die Argument-Option prüfen.
    assert False  # TODO


# ---------------------------------------------------------------------------
# cli_task._cmd_note (cli_task.py:67) — env-fallback für task_id
# ---------------------------------------------------------------------------


def test_note_uses_env_task_id_when_arg_missing(client, monkeypatch):
    assert False  # TODO


def test_note_explicit_task_id_wins_over_env(client, monkeypatch):
    assert False  # TODO


def test_note_without_env_or_task_id_returns_error(client, monkeypatch, capsys):
    assert False  # TODO


def test_note_env_stage_id_fallback(client, monkeypatch):
    # RELAY_STAGE_ID als optionale Anreicherung (Plan-Task-4-Kontext).
    assert False  # TODO


def test_note_env_fallback_sends_correct_payload(client, monkeypatch):
    # FakeClient-Check: complete/note-Call enthält die env-stammenden IDs.
    assert False  # TODO