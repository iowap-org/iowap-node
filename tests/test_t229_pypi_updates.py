"""T-229: PyPI-first update checks + MCP co-update (offline, mocked network)."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch as mock_patch  # noqa: F401 — parity with t212 style

import nodes.common.node_utils as nu


class _FakeResp:
    """Context-manager + read — proper binding (SimpleNamespace lambdas fail)."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._payload


def _pypi_json(version: str) -> _FakeResp:
    return _FakeResp(json.dumps({"info": {"version": version}}).encode())


# ---------------------------------------------------------------------------
# PyPI lookup + source resolution
# ---------------------------------------------------------------------------


def test_pypi_latest_version_success(monkeypatch) -> None:
    monkeypatch.setattr(nu.urllib.request, "urlopen", lambda *a, **k: _pypi_json("2.3.21"))
    out = nu.get_pypi_latest_version("iowap-node")
    assert out == {"latest_version": "2.3.21", "source": "pypi", "error": None}


def test_pypi_latest_version_network_error(monkeypatch) -> None:
    def _boom(*a, **k):
        raise ConnectionError("offline")

    monkeypatch.setattr(nu.urllib.request, "urlopen", _boom)
    out = nu.get_pypi_latest_version("iowap-node")
    assert out["latest_version"] is None and "PyPI lookup failed" in out["error"]


def test_resolve_source_auto_prefers_pypi(monkeypatch) -> None:
    monkeypatch.setattr(nu, "get_pypi_latest_version", lambda d: {"latest_version": "2.3.21", "source": "pypi", "error": None})
    out = nu.resolve_update_source("iowap-node", source="auto")
    assert out["source"] == "pypi" and out["latest_version"] == "2.3.21"


def test_resolve_source_auto_falls_back_to_github(monkeypatch) -> None:
    monkeypatch.setattr(nu, "get_pypi_latest_version", lambda d: {"latest_version": None, "source": "pypi", "error": "PyPI lookup failed: offline"})
    monkeypatch.setattr(nu, "get_latest_release_version", lambda repo=None: {
        "latest_version": "2.3.20", "tag": "wheel-v2.3.20", "asset_name": "a.whl",
        "asset_url": "http://x", "error": None})
    out = nu.resolve_update_source("iowap-node", source="auto")
    assert out["source"] == "github" and out["pypi_error"].startswith("PyPI lookup failed")


def test_resolve_source_pypi_strict_no_fallback(monkeypatch) -> None:
    monkeypatch.setattr(nu, "get_pypi_latest_version", lambda d: {"latest_version": None, "source": "pypi", "error": "offline"})
    out = nu.resolve_update_source("iowap-node", source="pypi")
    assert out["source"] == "pypi" and out["latest_version"] is None and "wheel-v" not in json.dumps(out)


def test_resolve_source_github_pinned(monkeypatch) -> None:
    monkeypatch.setattr(nu, "get_latest_release_version", lambda repo=None: {"latest_version": "9.9.9"})
    out = nu.resolve_update_source("iowap-node", source="github")
    assert out["source"] == "github" and out["latest_version"] == "9.9.9"


# ---------------------------------------------------------------------------
# check: node + MCP blocks
# ---------------------------------------------------------------------------


def test_bump_relevant_version_semantics() -> None:
    assert nu.bump_relevant_version("2.3.21", "2.3.20") is True
    assert nu.bump_relevant_version("2.3.20", "2.3.20") is False
    assert nu.bump_relevant_version("2.10.0", "2.9.9") is True
    assert nu.bump_relevant_version("bad", "2.3.20") is False
    assert nu.bump_relevant_version("2.3.21", None) is False


def test_check_updates_with_mcp_installed_and_outdated(monkeypatch) -> None:
    monkeypatch.setattr(nu, "get_local_dist_version", lambda d: "0.1.1" if d == nu.MCP_DIST else "2.3.20")
    monkeypatch.setattr(nu, "resolve_update_source", lambda d, source=None, repo=None: {"latest_version": "2.3.20", "source": "pypi", "error": None})
    monkeypatch.setattr(nu, "get_pypi_latest_version", lambda d: {"latest_version": "0.1.2", "source": "pypi", "error": None})
    monkeypatch.setattr(nu, "get_dist_url_kind", lambda d: None)
    out = nu.check_updates()
    assert out["update_available"] is False  # node current
    assert out["mcp"] == {"installed": True, "local_version": "0.1.1",
                          "latest_version": "0.1.2", "update_available": True, "error": None}
    assert out["source"] == "pypi"


def test_check_updates_mcp_not_installed_is_not_an_error(monkeypatch) -> None:
    state = {"mcp": False}
    def _local(dist):
        if state["mcp"] and dist == nu.MCP_DIST:
            return "0.1.2"
        return "2.3.20" if dist == nu.NODE_DIST else None
    monkeypatch.setattr(nu, "get_local_dist_version", _local)
    monkeypatch.setattr(nu, "resolve_update_source", lambda d, source=None, repo=None: {"latest_version": "2.3.20", "source": "pypi", "error": None})
    monkeypatch.setattr(nu, "get_dist_url_kind", lambda d: None)
    monkeypatch.setattr(nu, "get_pypi_latest_version", lambda d: {"latest_version": "0.1.2", "source": "pypi", "error": None})
    out = nu.check_updates()
    assert out["mcp"] == {"installed": False}
    assert out["update_available"] is False
    state["mcp"] = True  # same setup with the toolkit present: no exception either
    out = nu.check_updates()
    assert out["mcp"]["update_available"] is False


# ---------------------------------------------------------------------------
# apply: guard, pip argv, MCP venv targeting
# ---------------------------------------------------------------------------


def test_apply_refuses_dev_install(monkeypatch) -> None:
    monkeypatch.setattr(nu, "check_updates", lambda **k: {
        "local_version": "2.3.20", "local_kind": "dev", "latest_version": "2.3.21",
        "source": "pypi", "update_available": True, "error": None})
    out = nu.apply_updates(include_mcp=False)
    assert out["success"] is False and "refusing" in out["message"] and "dev" in out["message"]


def test_apply_pypi_runs_full_upgrade_in_its_own_venv(monkeypatch) -> None:
    calls: list[list[str]] = []

    def _run(argv, **kwargs):
        calls.append([str(x) for x in argv])
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(nu.subprocess, "run", _run)
    monkeypatch.setattr(nu, "check_updates", lambda **k: {
        "local_version": "2.3.20", "local_kind": None, "latest_version": "2.3.21",
        "source": "pypi", "update_available": True, "error": None,
        "mcp": {"installed": True, "local_version": "0.1.1",
                "latest_version": "0.1.2", "update_available": True, "error": None}})
    monkeypatch.setattr(nu, "get_local_dist_version", lambda d: "2.3.21")
    monkeypatch.setattr(nu, "resolve_mcp_install", lambda: {
        "installed": True, "venv_python": Path("/tmp/mcp/.venv/bin/python")})
    monkeypatch.setattr(nu, "_venv_dist_url_kind", lambda p, dist=None: None)
    monkeypatch.setattr(nu, "_subprocess_dist_version", lambda p, dist=None: "0.1.2")

    out = nu.apply_updates()
    assert out["success"] is True
    assert calls[0] == [nu.sys.executable, "-m", "pip", "install", "--quiet",
                        "--upgrade", "iowap-node"]
    assert "--no-deps" not in calls[0]  # full upgrade, deps may move
    # order: node pip-upgrade, service restart, mcp pip-upgrade, version probe
    assert calls[1] == ["systemctl", "--user", "restart", "iowap-node-daemon.service"]
    assert out["node"]["restarted"] is True  # same restart contract as legacy
    assert calls[2][:3] == ["/tmp/mcp/.venv/bin/python", "-m", "pip"]
    assert "iowap-client-mcp[mcp]" in calls[2]
    out_mcp = out["mcp"]
    assert out_mcp["before"] == "0.1.1" and out_mcp["after"] == "0.1.2"


def test_apply_mcp_updated_needs_no_restart(monkeypatch) -> None:
    monkeypatch.setattr(nu.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(nu, "check_updates", lambda **k: {
        "local_version": "2.3.20", "local_kind": None, "latest_version": "2.3.20",
        "source": "pypi", "update_available": False, "error": None,
        "mcp": {"installed": True, "local_version": "0.1.1",
                "latest_version": "0.1.2", "update_available": True, "error": None}})
    monkeypatch.setattr(nu, "resolve_mcp_install", lambda: {
        "installed": True, "venv_python": Path("/tmp/mcp/.venv/bin/python")})
    monkeypatch.setattr(nu, "_venv_dist_url_kind", lambda p, dist=None: None)
    monkeypatch.setattr(nu, "_subprocess_dist_version", lambda p, dist=None: "0.1.2")
    out = nu.apply_updates()
    assert out["node"] is None  # node up to date → no action
    assert out["mcp"]["after"] == "0.1.2"
    assert "next client spawn" in out["mcp"]["status"]


def test_apply_mcp_refused_in_dev_venv(monkeypatch) -> None:
    monkeypatch.setattr(nu, "check_updates", lambda **k: {
        "local_version": "2.3.20", "local_kind": None, "latest_version": "2.3.20",
        "source": "pypi", "update_available": False, "error": None,
        "mcp": {"installed": True, "local_version": "0.1.1",
                "latest_version": "0.1.2", "update_available": True, "error": None}})
    monkeypatch.setattr(nu, "resolve_mcp_install", lambda: {
        "installed": True, "venv_python": Path("/tmp/mcp/.venv/bin/python")})
    monkeypatch.setattr(nu, "_venv_dist_url_kind", lambda p, dist=None: "dev")
    out = nu.apply_updates()
    assert out["success"] is True
    assert "refused" in out["mcp"]["status"] and "dev" in out["mcp"]["status"]


def test_apply_mcp_skipped_via_no_mcp_flag(monkeypatch) -> None:
    monkeypatch.setattr(nu, "check_updates", lambda **k: {
        "local_version": "2.3.20", "local_kind": None, "latest_version": "2.3.20",
        "source": "pypi", "update_available": False, "error": None,
        "mcp": {"installed": False}})
    out = nu.apply_updates(include_mcp=False)
    assert "already up to date" in out["message"]
    assert out["mcp"] is None  # skipped entirely


# ---------------------------------------------------------------------------
# MCP binary discovery
# ---------------------------------------------------------------------------


def test_resolve_mcp_install_via_which(monkeypatch, tmp_path: Path) -> None:
    venv_bin = tmp_path / "myvenv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\nexit 0\n")
    exe = venv_bin / "iowap-mcp"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    monkeypatch.setattr(nu.shutil, "which", lambda name: str(exe))
    out = nu.resolve_mcp_install()
    assert out["installed"] is True
    assert out["venv_python"] == venv_bin / "python"


def test_resolve_mcp_install_fallback_home_local(monkeypatch, tmp_path: Path) -> None:
    """PATH misses → ~/.local/bin fallback (HOME-scoped, macOS layout)."""
    monkeypatch.setattr(nu.shutil, "which", lambda *_: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    candidate = tmp_path / ".local" / "bin" / "iowap-mcp"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("#!/bin/sh\nexit 0\n")
    candidate.chmod(0o755)
    out = nu.resolve_mcp_install()
    # ~/.local/bin holds the binary without a venv-python sibling — the
    # venv gate refuses it deliberately (a bare script is not an install).
    assert out.get("installed") is False


def test_resolve_mcp_install_hermes_config_layout(monkeypatch, tmp_path: Path) -> None:
    """Hermes MCP registration is the authoritative hint (absolute command)."""
    monkeypatch.setattr(nu.shutil, "which", lambda *_: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    venv_bin = tmp_path / "projects" / "iowap-client-mcp" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\nexit 0\n")
    exe = venv_bin / "iowap-mcp"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    (hermes_dir / "config.yaml").write_text(
        "mcp_servers:\n"
        "  iowap:\n"
        f"    command: {exe}\n"
        "    enabled: true\n"
    )
    out = nu.resolve_mcp_install()
    assert out["installed"] is True
    assert out["venv_python"] == venv_bin / "python"


def test_hermes_mcp_config_commands_tolerates_garbage(monkeypatch, tmp_path: Path) -> None:
    """A missing/unreadable/unparseable config yields no candidates, no raise."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / "config.yaml").write_text(":]\nnot: [valid\n")
    assert nu._hermes_mcp_config_commands() == []


def test_resolve_mcp_install_venv_layout(monkeypatch, tmp_path: Path) -> None:
    """PATH misses → dedicated home-layout venv (~/iowap-client-mcp/.venv)."""
    monkeypatch.setattr(nu.shutil, "which", lambda *_: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    venv_bin = tmp_path / "iowap-client-mcp" / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    (venv_bin / "python").write_text("#!/bin/sh\nexit 0\n")
    exe = venv_bin / "iowap-mcp"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    out = nu.resolve_mcp_install()
    assert out["installed"] is True
    assert out["venv"] == tmp_path / "iowap-client-mcp" / ".venv"


def test_suite_namespaces_importable() -> None:
    """Sanity: the new API is reachable via the public module namespace."""
    mod = importlib.import_module("nodes.common.node_utils")
    for name in ("check_updates", "apply_updates", "resolve_update_source",
                 "get_pypi_latest_version", "resolve_mcp_install"):
        assert getattr(mod, name)