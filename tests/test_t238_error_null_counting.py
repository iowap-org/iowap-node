"""T-238: a conforming ``{"status": "completed", "error": null}`` envelope
must not be counted as a stage failure by the daemons.

Audit F2: ``handler_runner._normalize_result`` passes a conforming
completed envelope with ``error`` present-but-``null`` through verbatim
(handler_runner.py:287-293) — and that is the documented, conforming
form (docstring :24-31). But both daemons counted failures by KEY
PRESENCE (``if "error" in result:`` at node_cli.py:357 /
node_daemon.py:776): ``error: null`` incremented ``tasks_failed`` and
``_failed_tasks[task_id]``, and the claim loop (node_daemon.py) then
skipped every stage of that task once ``failures >= max_retries`` — a
3-stage flow died at stage 3 for nothing.

Fix (both daemons): count a failure only when the error VALUE is
non-null — ``result.get("error") is not None``. The verbatim
pass-through semantics of the runner are preserved (no payload
rewriting); only the counting detection changes. The runner docstring
contradictions (handler_runner.py:26-31 and :237-252) are corrected in
the same commit: the failure counting is value-based (T-238), which
means ``error: null`` in a completed envelope now completes the stage.
"""

from __future__ import annotations

import pytest

from nodes.common import node_cli, node_daemon
from nodes.common.relay_client import RelayClient

META = {
    "node_id": "238NODE",
    "node_name": "error-null-counting",
    "registration_secret": "rs_x",
    "base_url": "http://relay.test:8788",
}
CFG = {"base_url": None, "request_timeout": 5, "heartbeat_interval": 1}

CAP = {"name": "probe.echo", "handler": "/nonexistent", "claimable": True}
STAGE = {"stage_id": "stg-1", "task_id": "task-1"}


@pytest.fixture()
def client() -> RelayClient:
    return RelayClient(dict(META), dict(CFG))


def rh_ok(handler, stage, *, context=None, timeout=None):
    """Canned conforming envelope: completed + ``error: null``."""
    return {"status": "completed", "result": {"echo": 1}, "error": None}


# --- node-cli daemon ----------------------------------------------------------


def test_cli_completed_with_null_error_counts_as_completed(client, monkeypatch):
    """{status: completed, result, error: null} → tasks_completed++, NO
    tasks_failed/_failed_tasks bump (the stage-3 flow stays alive)."""
    daemon = node_cli.Daemon(client, dict(CFG))
    monkeypatch.setattr(node_cli, "run_handler", rh_ok)
    daemon.client.complete = lambda *_a, **_k: {"status": "ok"}

    daemon._run_stage(dict(CAP), dict(STAGE))

    assert daemon.tasks_completed == 1
    assert daemon.tasks_failed == 0
    assert daemon._failed_tasks == {}


def test_cli_non_null_error_still_counts_as_failure(client, monkeypatch):
    """Contract lock: real errors (non-null, flat and envelope form) keep
    incrementing tasks_failed and the per-task budget."""
    daemon = node_cli.Daemon(client, dict(CFG))
    daemon.client.complete = lambda *_a, **_k: {"status": "ok"}

    cases = [
        {"status": "completed", "result": {}, "error": "boom"},
        {"error": "boom"},
        {"status": "error", "error": "boom"},
    ]
    for result in cases:
        monkeypatch.setattr(node_cli, "run_handler", lambda *a, _r=result, **k: _r)
        daemon._run_stage(dict(CAP), dict(STAGE))

    assert daemon.tasks_completed == 0
    assert daemon.tasks_failed == 3
    assert daemon._failed_tasks["task-1"] == 3


# --- node-daemon (SSE) parity ---------------------------------------------------


def test_sse_completed_with_null_error_counts_as_completed(client, monkeypatch):
    daemon = node_daemon.SseDaemon(client, dict(CFG))
    monkeypatch.setattr(node_daemon, "run_handler", rh_ok)
    daemon.client.complete = lambda *_a, **_k: {"status": "ok"}

    daemon._run_stage(dict(CAP), dict(STAGE))

    assert daemon.tasks_completed == 1
    assert daemon.tasks_failed == 0
    assert daemon._failed_tasks == {}


def test_sse_non_null_error_still_counts_as_failure(client, monkeypatch):
    daemon = node_daemon.SseDaemon(client, dict(CFG))
    daemon.client.complete = lambda *_a, **_k: {"status": "ok"}

    cases = [
        {"status": "completed", "result": {}, "error": "boom"},
        {"error": "boom"},
        {"status": "error", "error": "boom"},
    ]
    for result in cases:
        monkeypatch.setattr(node_daemon, "run_handler", lambda *a, _r=result, **k: _r)
        daemon._run_stage(dict(CAP), dict(STAGE))

    assert daemon.tasks_completed == 0
    assert daemon.tasks_failed == 3
    assert daemon._failed_tasks["task-1"] == 3