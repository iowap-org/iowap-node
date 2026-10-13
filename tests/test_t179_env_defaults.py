"""T-179 Task 4: complete/note default to RELAY_* env ids (phase 2 stubs).

Every test starts with ``assert False  # TODO`` — phase 3 (integrate)
fills the bodies. The contract comes from the plan (Task 4):

``node-cli complete <stage_id>`` and ``node-cli task note <id> <msg>``
work without explicit ``--task``/``task_id`` when
``RELAY_TASK_ID``/``RELAY_STAGE_ID`` are set (handler_runner sets them
for every handler). Explicit flags win; without either → stderr +
exit 1. ``--task`` becomes ``required=False``.
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
    # plan: ``_cmd_complete`` with env — RELAY_TASK_ID/RELAY_STAGE_ID
    # set, no --task → client.complete receives the env ids.
    monkeypatch.setenv("RELAY_TASK_ID", "t_env")
    monkeypatch.setenv("RELAY_STAGE_ID", "s_env")
    args = _ns(stage_id="s_env", task=None, result_file=str(result_file))
    rc = node_cli._cmd_complete(fake_client, args)
    assert rc == 0
    fake_client.complete.assert_called_once_with("t_env", "s_env", {"status": "ok"})


def test_complete_explicit_flags_win_over_env(fake_client, result_file, monkeypatch):
    # explicit flags win over env (plan logic:
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
    # stage_id is positional-required; the env fallback applies to --task.
    # (here: stage positional, task via env).
    monkeypatch.setenv("RELAY_TASK_ID", "t_env")
    monkeypatch.delenv("RELAY_STAGE_ID", raising=False)
    args = _ns(stage_id="s_pos", task=None, result_file=str(result_file))
    rc = node_cli._cmd_complete(fake_client, args)
    assert rc == 0
    fake_client.complete.assert_called_once_with("t_env", "s_pos", {"status": "ok"})


def test_complete_without_ids_exits_1_with_message(fake_client, result_file, monkeypatch, capsys):
    # plan: without --task and without RELAY_TASK_ID → stderr + return 1.
    monkeypatch.delenv("RELAY_TASK_ID", raising=False)
    monkeypatch.delenv("RELAY_STAGE_ID", raising=False)
    args = _ns(stage_id="", task=None, result_file=str(result_file))
    rc = node_cli._cmd_complete(fake_client, args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "RELAY_TASK_ID" in err
    # the client was NEVER called.
    fake_client.complete.assert_not_called()


def test_complete_parser_task_flag_now_optional():
    # Plan: ``p_complete.add_argument("--task", required=True)`` →
    # ``required=False``. Parser contract: complete without --task parses.
    parser = node_cli.build_parser()
    args = parser.parse_args(["complete", "s_1", "--result-file", "/tmp/r.json"])
    assert args.task is None
    assert args.stage_id == "s_1"


def test_note_env_id_used_without_explicit_task(fake_client, monkeypatch, capsys):
    # plan: ``_cmd_note`` with env — RELAY_TASK_ID set, task_id None.
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
    # plan: without task_id and without RELAY_TASK_ID → stderr + return 1.
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
    # regression: the existing 404 path stays (plan: previous failure path
    # unchanged).
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