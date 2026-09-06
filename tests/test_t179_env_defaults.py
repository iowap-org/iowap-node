"""T-179 Task 4: complete/note default to RELAY_* env ids (Phase 2 Stubs).

Jeder Test beginnt mit ``assert False  # TODO`` — Phase 3 (integrate)
füllt die Körper. Der Contract kommt aus dem Plan (Task 4):

``node-cli complete <stage_id>`` und ``node-cli task note <id> <msg>``
funktionieren ohne explizite ``--task``/``task_id``, wenn
``RELAY_TASK_ID``/``RELAY_STAGE_ID`` gesetzt sind (handler_runner setzt
sie für jeden Handler). Explizite Flags win; ohne beide → stderr +
exit 1. ``--task`` wird ``required=False``.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from nodes.common import node_cli
from nodes.common.cli import cli_task


def _ns(**kw) -> argparse.Namespace:
    """Namespace mit komplettem Set an complete-/note-Attributen."""
    base = {
        "stage_id": "",
        "task": None,
        "result_file": "",
        "json": True,
        "task_id": None,
        "message": "",
    }
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def fake_client() -> MagicMock:
    c = MagicMock()
    c.complete = MagicMock(return_value={"ok": True})
    c.add_task_note = MagicMock(
        return_value={"task_id": "t_1", "message": "m", "created_at": "now"}
    )
    return c


@pytest.fixture
def result_file(tmp_path: Path) -> Path:
    f = tmp_path / "result.json"
    f.write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# node-cli complete — env defaults (Plan Task 4)
# ---------------------------------------------------------------------------


def test_complete_env_ids_used_without_flags(fake_client, result_file, monkeypatch, capsys):
    # Plan: ``_cmd_complete`` mit env — RELAY_TASK_ID/RELAY_STAGE_ID
    # gesetzt, kein --task → client.complete bekommt die env-IDs.
    monkeypatch.setenv("RELAY_TASK_ID", "t_env")
    monkeypatch.setenv("RELAY_STAGE_ID", "s_env")
    args = _ns(stage_id="s_env", task=None, result_file=str(result_file))
    rc = node_cli._cmd_complete(fake_client, args)
    assert rc == 0
    fake_client.complete.assert_called_once_with("t_env", "s_env", {"status": "ok"})


def test_complete_explicit_flags_win_over_env(fake_client, result_file, monkeypatch):
    # Explizite Flags gewinnen gegen env (Plan-Logik:
    # ``args.task or os.environ.get(...)``).
    monkeypatch.setenv("RELAY_TASK_ID", "t_env")
    monkeypatch.setenv("RELAY_STAGE_ID", "s_env")
    args = _ns(stage_id="s_explicit", task="t_explicit",
               result_file=str(result_file))
    rc = node_cli._cmd_complete(fake_client, args)
    assert rc == 0
    fake_client.complete.assert_called_once_with(
        "t_explicit", "s_explicit", {"status": "ok"}
    )


def test_complete_stage_id_env_fallback(fake_client, result_file, monkeypatch):
    # stage_id ist positional-required; der env-Fallback gilt für --task.
    # (Hier: stage über positional, task über env.)
    monkeypatch.setenv("RELAY_TASK_ID", "t_env")
    monkeypatch.delenv("RELAY_STAGE_ID", raising=False)
    args = _ns(stage_id="s_pos", task=None, result_file=str(result_file))
    rc = node_cli._cmd_complete(fake_client, args)
    assert rc == 0
    fake_client.complete.assert_called_once_with("t_env", "s_pos", {"status": "ok"})


def test_complete_without_ids_exits_1_with_message(fake_client, result_file, monkeypatch, capsys):
    # Plan: ohne --task und ohne RELAY_TASK_ID → stderr + return 1.
    monkeypatch.delenv("RELAY_TASK_ID", raising=False)
    monkeypatch.delenv("RELAY_STAGE_ID", raising=False)
    args = _ns(stage_id="", task=None, result_file=str(result_file))
    rc = node_cli._cmd_complete(fake_client, args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "RELAY_TASK_ID" in err
    # Client wurde NIE gerufen.
    fake_client.complete.assert_not_called()


def test_complete_parser_task_flag_now_optional():
    # Plan: ``p_complete.add_argument("--task", required=True)`` →
    # ``required=False``. Parser-Vertrag: complete ohne --task parst.
    parser = node_cli.build_parser()
    args = parser.parse_args(["complete", "s_1", "--result-file", "/tmp/r.json"])
    assert args.task is None
    assert args.stage_id == "s_1"


def test_note_env_id_used_without_explicit_task(fake_client, monkeypatch, capsys):
    # Plan: ``_cmd_note`` mit env — RELAY_TASK_ID gesetzt, task_id None.
    monkeypatch.setenv("RELAY_TASK_ID", "t_env")
    fake_client.add_task_note = MagicMock(
        return_value={"task_id": "t_env", "message": "hello", "created_at": "now"}
    )
    args = _ns(task_id=None, message="hello")
    rc = cli_task._cmd_task_note(fake_client, args)
    assert rc == 0
    fake_client.add_task_note.assert_called_once_with("t_env", "hello")
    out = capsys.readouterr().out
    assert "t_env" in out


def test_note_without_ids_exits_1_with_message(fake_client, monkeypatch, capsys):
    # Plan: ohne task_id und ohne RELAY_TASK_ID → stderr + return 1.
    monkeypatch.delenv("RELAY_TASK_ID", raising=False)
    args = _ns(task_id=None, message="hello")
    rc = cli_task._cmd_task_note(fake_client, args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "RELAY_TASK_ID" in err
    fake_client.add_task_note.assert_not_called()


def test_note_explicit_id_wins_over_env(fake_client, monkeypatch):
    monkeypatch.setenv("RELAY_TASK_ID", "t_env")
    args = _ns(task_id="t_explicit", message="hello")
    rc = cli_task._cmd_task_note(fake_client, args)
    assert rc == 0
    fake_client.add_task_note.assert_called_once_with("t_explicit", "hello")


def test_note_404_maps_to_exit_1(fake_client, monkeypatch, capsys):
    # Regression: bestehender 404-Pfad bleibt (Plan: bisheriger Fehlerpfad
    # unverändert).
    monkeypatch.setenv("RELAY_TASK_ID", "t_missing")
    fake_client.add_task_note = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "404",
            request=httpx.Request("POST", "http://x"),
            response=httpx.Response(404),
        )
    )
    args = _ns(task_id=None, message="hello")
    rc = cli_task._cmd_task_note(fake_client, args)
    assert rc == 1
    assert "not found" in capsys.readouterr().err