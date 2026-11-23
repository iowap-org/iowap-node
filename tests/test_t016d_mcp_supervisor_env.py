"""T-016d: the dashboard's MCP supervisor must inject the bearer.

Windows-VM finding (2026-11-22): a container recreated via plain
``docker compose up`` carries no ``IOWAP_MCP_BEARER`` env. The dashboard's
Enable/Generate-Bearer wrote the bearer file but the spawned
``iowap-mcp --http`` inherited a bearer-less environment, hit its
fail-closed guard and exited immediately — port 7781 answered curl 52
while the dashboard claimed success.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from nodes.common import dashboard


@pytest.fixture()
def sup(tmp_path: Path, monkeypatch) -> dashboard.McpSupervisor:
    # rotate_bearer/write_bearer/current_bearer use the module-level path —
    # pin it into the tmp dir so tests never touch the real ~/.relay.
    monkeypatch.setattr(dashboard, "mcp_bearer_path", lambda: tmp_path / "mcp_bearer")
    return dashboard.McpSupervisor(
        port=7781,
        pid_path=tmp_path / "mcp.pid",
        bearer_path=tmp_path / "mcp_bearer",
        log_path=tmp_path / "mcp.log",
        command=["true"],  # fake binary for real (non-mocked) spawns
    )


def _capturing_spawner(calls: list):
    """Spawner double that records argv and the caller-provided env."""

    def fake_popen(argv, stdout=None, stderr=None, env=None):
        calls.append({"argv": argv, "env": env})
        return SimpleNamespace(pid=4242, poll=lambda: None)

    return fake_popen


def test_child_env_injects_bearer_from_file(sup: dashboard.McpSupervisor, tmp_path, monkeypatch) -> None:
    sup.bearer_path.write_text("iw_mcp_filetoken\n", encoding="utf-8")
    monkeypatch.delenv("IOWAP_MCP_BEARER", raising=False)
    env = sup._child_env()
    assert env["IOWAP_MCP_BEARER"] == "iw_mcp_filetoken"


def test_child_env_file_wins_over_stale_env(sup: dashboard.McpSupervisor, tmp_path, monkeypatch) -> None:
    # T-016e: after a dashboard rotation the FILE is the new truth — the
    # container env may still carry the pre-rotation bootstrap token, and a
    # restart with that stale env token caused invalid_token on every probe.
    sup.bearer_path.write_text("iw_mcp_filetoken", encoding="utf-8")
    monkeypatch.setenv("IOWAP_MCP_BEARER", "iw_mcp_envtoken")
    env = sup._child_env()
    assert env["IOWAP_MCP_BEARER"] == "iw_mcp_filetoken"


def test_real_spawn_passes_bearer_env(sup: dashboard.McpSupervisor, tmp_path, monkeypatch) -> None:
    """The actual Popen path must carry the injected env (the VM bug)."""
    calls: list = []
    sup._spawner = None
    monkeypatch.setattr(dashboard.subprocess, "Popen", lambda *a, **kw: calls.append(kw) or SimpleNamespace(pid=1, poll=lambda: None))
    sup.bearer_path.write_text("iw_mcp_filetoken", encoding="utf-8")
    monkeypatch.delenv("IOWAP_MCP_BEARER", raising=False)
    ok, msg = sup.start()
    assert ok, msg
    assert calls, "Popen was never called"
    assert calls[0]["env"]["IOWAP_MCP_BEARER"] == "iw_mcp_filetoken"


def test_rotate_on_stopped_process_starts_it(sup: dashboard.McpSupervisor, monkeypatch) -> None:
    monkeypatch.delenv("IOWAP_MCP_BEARER", raising=False)
    started: list = []
    sup._spawner = lambda argv: started.append(argv) or SimpleNamespace(pid=7, poll=lambda: None)
    bearer, restarted = dashboard.rotate_bearer(sup)
    assert bearer
    assert sup.bearer_path.read_text(encoding="utf-8").strip() == bearer
    assert restarted is True
    assert started, "rotate on a stopped process must start the MCP process"


def test_rotate_result_bearer_matches_running_process_env(sup: dashboard.McpSupervisor, monkeypatch) -> None:
    monkeypatch.delenv("IOWAP_MCP_BEARER", raising=False)
    calls: list = []
    sup._spawner = None
    monkeypatch.setattr(dashboard.subprocess, "Popen", lambda *a, **kw: calls.append(kw) or SimpleNamespace(pid=2, poll=lambda: None))
    bearer, _ = dashboard.rotate_bearer(sup)
    env = sup._child_env()
    # after rotate, the file has the new bearer and the child env matches it
    assert env["IOWAP_MCP_BEARER"] == bearer
