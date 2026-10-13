"""T-193 (F6, MED): ``_handle`` as a non-dict must not abort the stage run.

Evidence from the senior review 2026-09-30: if a conforming handler
returns a ``completed`` envelope WITH ``_handler`` as a non-dict,
``parsed["_handler"]["stderr"] = …`` raises a ``TypeError``. In the
daemon ``_run_stage`` then breaks BEFORE ``client.complete()`` → the
stage runs into the lease timeout instead of being reported.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from nodes.common import handler_runner


def _stage() -> dict[str, Any]:
    return {
        "task_id": "task_1",
        "stage_id": "stage_1",
        "capability": "chat.ai",
        "payload": {"prompt": "hi"},
    }


def _write_handler(tmp_path: Path, body: str) -> str:
    script = tmp_path / "handler.py"
    script.write_text(body, encoding="utf-8")
    return f"{sys.executable} {script}"


def test_handler_non_dict_diagnostic_does_not_raise(tmp_path: Path) -> None:
    handler = _write_handler(
        tmp_path,
        "import json\n"
        "print(json.dumps({'status': 'completed', 'result': {'answer': 'ok'},"
        " '_handler': 'oops-not-a-dict'}))\n",
    )
    result = handler_runner.run_handler(handler, _stage(), context={}, timeout=30)

    # Result stays a success — the diagnostic block is normalized, not fatal.
    assert result["status"] == "completed"
    assert result["result"] == {"answer": "ok"}
    assert isinstance(result["_handler"], dict)
    assert result["_handler"]["exit_code"] == 0
    assert "stderr" in result["_handler"]


def test_handler_list_diagnostic_does_not_raise(tmp_path: Path) -> None:
    handler = _write_handler(
        tmp_path,
        "import json\n"
        "print(json.dumps({'status': 'completed', 'result': {},"
        " '_handler': [1, 2, 3]}))\n",
    )
    result = handler_runner.run_handler(handler, _stage(), context={}, timeout=30)
    assert result["status"] == "completed"
    assert isinstance(result["_handler"], dict)
    assert result["_handler"]["exit_code"] == 0
