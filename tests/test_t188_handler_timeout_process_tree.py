"""T-188 (F1, HIGH): Handler-Timeout muss den ganzen Prozessbaum beenden.

Beleg aus dem Senior-Review 2026-09-30: ``subprocess.run(handler,
shell=True, timeout=…)`` beendet bei Timeout nur die direkte Shell — der
eigentliche Handler (Enkelprozess) läuft weiter. Bei ``long_run`` +
Claim-Retry bedeutet das überlappende Handler und Ressourcen-Leaks auf
fremden Hosts.

Der bestehende Test ``test_envelope_contract.py::test_run_handler_timeout_fails``
prüft nur den Fehlerstring, nie den Prozessbaum — dieser Test schließt
genau die Lücke.
"""
from __future__ import annotations

import os
import sys
import time
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


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover — pid exists, not ours
        return True
    return True


def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_run_handler_timeout_kills_grandchild_process(tmp_path: Path) -> None:
    """A grandchild spawned by the handler must not outlive the timeout."""
    pid_file = tmp_path / "grandchild.pid"
    script = tmp_path / "handler.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c',"
        " 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    handler = f"{sys.executable} {script}"

    result = handler_runner.run_handler(handler, _stage(), context={}, timeout=1)

    assert result["error"] == "handler timeout after 1s"
    # The grandchild pid must have been written before the timeout hit.
    deadline = time.time() + 5
    while not pid_file.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert pid_file.exists(), "handler never reported its grandchild pid"

    grandchild_pid = int(pid_file.read_text().strip())
    assert _wait_gone(grandchild_pid), (
        f"grandchild pid {grandchild_pid} survived the handler timeout "
        "(process tree not killed)"
    )
