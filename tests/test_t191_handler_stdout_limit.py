"""T-191 (F4, MED): stdout des Handlers braucht ein Größenlimit.

Beleg aus dem Senior-Review 2026-09-30: ``subprocess.run(capture_output=
True)`` puffert unbounded; ein Handler, der versehentlich eine Riesendatei
nach stdout schreibt, killt den Node-Daemon (OOM) — denselben Prozess, der
Agent/Serve/Heartbeat hält.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

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


def test_oversized_stdout_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handler_runner, "MAX_HANDLER_STDOUT_BYTES", 1024)
    handler = _write_handler(
        tmp_path,
        "import sys\n"
        "sys.stdout.write('x' * 200000)\n",
    )
    result = handler_runner.run_handler(handler, _stage(), context={}, timeout=30)
    assert "error" in result
    assert "stdout" in result["error"].lower()


def test_within_limit_stdout_still_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handler_runner, "MAX_HANDLER_STDOUT_BYTES", 1_000_000)
    handler = _write_handler(
        tmp_path,
        "import json, sys\n"
        "print(json.dumps({'status': 'completed', 'result': {'answer': 'ok'}}))\n",
    )
    result = handler_runner.run_handler(handler, _stage(), context={}, timeout=30)
    assert result["status"] == "completed"
    assert result["result"] == {"answer": "ok"}
