"""T-041: the dashboard MCP supervisor spawns a transport-appropriate binary.

The universal image (iowap-docker/universal-node) renamed the MCP HTTP
entry to ``iowap-node-mcp`` — the legacy ``iowap-mcp`` console script no
longer knows ``--http`` (client-mcp 0.2.0 is stdio-only), so the fixed
``iowap-mcp --http`` spawn died with a usage error before answering the
first probe. Resolution order since T-041: ``iowap-node-mcp`` first,
legacy ``iowap-mcp --http`` fallback, fail-closed (spawn NOTHING) when
neither is resolvable. The bearer fail-closed guard is untouched — the
spawn still runs with the bearer injected (T-016d/e contract).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nodes.common import dashboard


@pytest.fixture(autouse=True)
def _clean_spawn_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the exotic-install override out of every resolution here."""
    monkeypatch.delenv("IOWAP_MCP_COMMAND", raising=False)
    monkeypatch.delenv("IOWAP_MCP_BEARER", raising=False)


@pytest.fixture()
def sup(tmp_path: Path) -> dashboard.McpSupervisor:
    """Supervisor WITHOUT an explicit command — exercises the resolver."""
    return dashboard.McpSupervisor(
        port=7781,
        pid_path=tmp_path / "mcp.pid",
        bearer_path=tmp_path / "mcp_bearer",
        log_path=tmp_path / "mcp.log",
    )


def _which(mapping: dict[str, str]):
    """``shutil.which`` double driven by a name → fake exe path mapping."""

    def fake(name, *args, **kwargs):
        return mapping.get(name)

    return fake


def _capturing_spawner(calls: list):
    """Spawner double that records the argv handed to the spawn."""

    def fake_popen(argv, stdout=None, stderr=None, env=None):
        calls.append({"argv": list(argv), "env": env})
        return SimpleNamespace(pid=4242, poll=lambda: None)

    return fake_popen


def test_resolver_prefers_node_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    # (a) universal image: iowap-node-mcp wins even when the legacy
    # script is ALSO resolvable, and the server flags stay identical.
    monkeypatch.setattr(
        dashboard.shutil,
        "which",
        _which(
            {
                "iowap-node-mcp": "/usr/local/bin/iowap-node-mcp",
                "iowap-mcp": "/usr/local/bin/iowap-mcp",
            }
        ),
    )
    argv = dashboard.resolve_mcp_spawn(7781)
    assert argv[0] == "/usr/local/bin/iowap-node-mcp"
    assert argv[1:] == ["--http", "--host", "0.0.0.0", "--port", "7781"]


def test_resolver_falls_back_to_legacy_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    # (b) classic/legacy images: no iowap-node-mcp → iowap-mcp with the
    # same --http/--host/--port flags as before T-041.
    monkeypatch.setattr(
        dashboard.shutil, "which", _which({"iowap-mcp": "/usr/local/bin/iowap-mcp"})
    )
    argv = dashboard.resolve_mcp_spawn(7781)
    assert argv[0] == "/usr/local/bin/iowap-mcp"
    assert argv[1:] == ["--http", "--host", "0.0.0.0", "--port", "7781"]


def test_neither_binary_fail_closed_no_start(
    sup: dashboard.McpSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (c) nothing resolvable → clear failure, NOTHING spawned, no pid file.
    monkeypatch.setattr(dashboard.shutil, "which", _which({}))
    calls: list = []
    sup._spawner = _capturing_spawner(calls)
    ok, msg = sup.start()
    assert ok is False
    assert calls == []
    assert not sup.pid_path.exists()
    assert "iowap-node-mcp" in msg and "iowap-mcp" in msg  # clear error
    # the resolver itself fails closed, not silently
    with pytest.raises(FileNotFoundError, match="iowap-node-mcp"):
        dashboard.resolve_mcp_spawn(7781)


def test_spawn_args_carry_http_host_port(
    sup: dashboard.McpSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (d) through the real supervisor path: the spawned argv carries
    # --http and --host/--port exactly as before T-041, and the bearer
    # fail-closed contract still injects the token (T-016d/e).
    monkeypatch.setattr(
        dashboard.shutil, "which", _which({"iowap-node-mcp": "/usr/local/bin/iowap-node-mcp"})
    )
    sup.bearer_path.write_text("iw_mcp_tok", encoding="utf-8")
    calls: list = []
    sup._spawner = _capturing_spawner(calls)
    ok, msg = sup.start()
    assert ok is True
    assert sup.pid_path.exists()
    assert calls[0]["argv"] == [
        "/usr/local/bin/iowap-node-mcp",
        "--http",
        "--host",
        "0.0.0.0",
        "--port",
        "7781",
    ]
    assert sup._child_env()["IOWAP_MCP_BEARER"] == "iw_mcp_tok"
