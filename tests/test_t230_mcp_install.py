"""T-230: node-cli mcp install/status — client-neutral toolkit management."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import nodes.common.node_utils as nu
from nodes.common import node_cli
from nodes.common.cli import cli_mcp
from nodes.common.node_utils import install_mcp_toolkit

# ---------------------------------------------------------------------------
# install: venv-create + pip argv + verification
# ---------------------------------------------------------------------------


def test_install_creates_venv_and_installs_from_pypi(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def _run(argv, **kwargs):
        calls.append([str(x) for x in argv])
        # simulate the created venv + installed toolkit
        venv = Path(str(argv[-1]))
        (venv / "bin").mkdir(parents=True, exist_ok=True)
        (venv / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")
        exe = venv / "bin" / "iowap-mcp"
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(nu.subprocess, "run", _run)
    monkeypatch.setattr(nu, "_subprocess_dist_version", lambda p, dist=None: "0.1.2")
    out = install_mcp_toolkit(venv_path=str(tmp_path / "toolkit-venv"))
    assert out["success"] is True
    assert out["version"] == "0.1.2"
    assert calls[0][0] == nu.sys.executable and calls[0][1:3] == ["-m", "venv"]
    assert calls[1][0].endswith("/bin/python") and "-m" in calls[1]
    assert nu.MCP_SPEC in calls[1]  # iowap-client-mcp[mcp]
    assert "--upgrade" in calls[1]  # idempotent: upgrade = reinstall-safe


def test_install_reports_pip_failure_honestly(monkeypatch, tmp_path: Path) -> None:
    venv = tmp_path / "toolkit-venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")

    def _run(argv, **kwargs):
        if "venv" in argv:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise nu.subprocess.CalledProcessError(1, argv, stderr="net down")

    monkeypatch.setattr(nu.subprocess, "run", _run)
    out = install_mcp_toolkit(venv_path=str(venv))
    assert out["success"] is False and "pip install failed" in out["message"]


def test_default_venv_is_home_layout(monkeypatch) -> None:
    monkeypatch.setenv("HOME", "/home/fake")
    monkeypatch.delenv(cli_mcp.MCP_VENV_ENV, raising=False)
    assert cli_mcp._default_venv() == "/home/fake/iowap-client-mcp/.venv"
    monkeypatch.setenv(cli_mcp.MCP_VENV_ENV, "/custom/mcp-venv")
    assert cli_mcp._default_venv() == "/custom/mcp-venv"


# ---------------------------------------------------------------------------
# status (JSON shape, discovery as data)
# ---------------------------------------------------------------------------


def test_status_json_shape(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_mcp, "resolve_mcp_install", lambda: {
        "installed": True, "venv_python": Path("/venv/bin/python").resolve(), "venv": Path("/venv")})
    monkeypatch.setattr(cli_mcp, "_mcp_block", lambda **k: {
        "installed": True, "local_version": "0.1.2",
        "latest_version": "0.1.2", "update_available": False,
        "error": None, "venv": "/venv", "binary": "/venv/bin/iowap-mcp"})
    monkeypatch.setattr(cli_mcp, "get_pypi_latest_version", lambda d: {
        "latest_version": "0.1.2", "error": None})
    rc = cli_mcp._cmd_mcp_status(argparse.Namespace(json=True, log_level="ERROR"))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["toolkit"]["installed"] is True
    assert out["pypi_latest"] == "0.1.2"
    assert out["discovery"]["installed"] is True


def test_status_absent_is_data(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_mcp, "resolve_mcp_install", lambda: {"installed": False})
    monkeypatch.setattr(cli_mcp, "_mcp_block", lambda **k: {
        "installed": False, "discovered": False})
    monkeypatch.setattr(cli_mcp, "get_pypi_latest_version", lambda d: {
        "latest_version": "0.1.2", "error": None})
    rc = cli_mcp._cmd_mcp_status(argparse.Namespace(json=True, log_level="ERROR"))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["toolkit"]["installed"] is False


# ---------------------------------------------------------------------------
# parser wiring
# ---------------------------------------------------------------------------


def test_parser_mcp_subcommands_exist() -> None:
    parser = node_cli.build_parser()
    args = parser.parse_args(["mcp", "install", "--venv", "/x/venv"])
    assert args.mcp_command == "install" and args.venv == "/x/venv"
    args = parser.parse_args(["mcp", "status"])
    assert args.mcp_command == "status"


def test_mcp_group_is_cli_only() -> None:
    """Client-neutral proof: the CLI module has no MCP surface of its own.

    The toolkit's 7 tools live in iowap-client-mcp; this node-side group is
    a plain admin CLI (no MCP SDK import, no tool registration)."""
    import inspect

    src = inspect.getsource(cli_mcp)
    assert "MCPServer" not in src and "@tool" not in src
    assert "stdio_client" not in src  # no client-side MCP plumbing either