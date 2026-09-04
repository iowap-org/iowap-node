"""T-178: `node-cli node register` and `node-cli server health|metrics`.

Covers the first-class registration flow (POST /relay/v2/auth/register +
state-file persistence) and the unauthenticated server status probe CLI.
"""

from __future__ import annotations

import json
import socket

import httpx
import pytest

from nodes.common import node_cli, node_utils
from nodes.common.cli import cli_node, cli_server
from nodes.common.cli.cli_server import normalize_base_url

REG_RESPONSE = {
    "node_id": "ABCD1234",
    "node_name": "test-node",
    "status": "pending",
    "token_type": "temporary",
    "token": "tp_abc123",
    "expires_at": "2026-09-03T12:00:00Z",
    "registration_secret": "rs_xyz",
}


@pytest.fixture()
def isolated_relay_dir(tmp_path, monkeypatch):
    """Point every state path at a tmp dir so tests never touch ~/.relay."""
    for name in (
        "META_PATH", "TOKEN_PATH", "CONFIG_PATH",
        "LEGACY_META_PATH", "LEGACY_TOKEN_PATH", "STATUS_PATH",
    ):
        monkeypatch.setattr(node_utils, name, tmp_path / f"{name.lower()}.json")
    return tmp_path


# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------


def test_normalize_base_url_shapes():
    assert normalize_base_url("192.168.2.60") == "http://192.168.2.60:8788"
    assert normalize_base_url("192.168.2.60:9000") == "http://192.168.2.60:9000"
    assert normalize_base_url("https://relay.example.com") == "https://relay.example.com"
    assert normalize_base_url("http://host:8788/") == "http://host:8788"


# ---------------------------------------------------------------------------
# node register
# ---------------------------------------------------------------------------


def test_register_persists_state_and_token(isolated_relay_dir, monkeypatch, capsys):
    calls: dict = {}

    def fake_post(url, json=None, **kw):  # noqa: A002 — mirrors httpx signature
        calls["url"] = url
        calls["body"] = json
        return httpx.Response(200, json=REG_RESPONSE, request=httpx.Request("POST", url))

    monkeypatch.setattr(cli_server.httpx, "post", fake_post)
    rc = node_cli.main(["--json", "node", "register", "192.168.2.60", "--name", "test-node"])
    assert rc == 0
    assert calls["url"] == "http://192.168.2.60:8788/relay/v2/auth/register"
    assert calls["body"]["node_name"] == "test-node"
    assert calls["body"]["endpoint"] is None
    assert calls["body"]["capabilities"] == []

    meta = json.loads(node_utils.META_PATH.read_text())
    assert meta["node_id"] == "ABCD1234"
    assert meta["registration_secret"] == "rs_xyz"
    assert meta["base_url"] == "http://192.168.2.60:8788"

    tok = json.loads(node_utils.TOKEN_PATH.read_text())
    assert tok["token"] == "tp_abc123"
    assert tok["expires_at"] == "2026-09-03T12:00:00Z"


def test_register_default_name_is_hostname(isolated_relay_dir, monkeypatch):
    calls: dict = {}

    def fake_post(url, json=None, **kw):  # noqa: A002
        calls["body"] = json
        return httpx.Response(200, json=REG_RESPONSE, request=httpx.Request("POST", url))

    monkeypatch.setattr(cli_server.httpx, "post", fake_post)
    rc = node_cli.main(["--json", "node", "register", "host1"])
    assert rc == 0
    assert calls["body"]["node_name"] == socket.gethostname()


def test_register_refuses_existing_state(isolated_relay_dir, monkeypatch):
    node_utils.META_PATH.write_text("{}")
    called = False

    def fake_post(url, **kw):
        nonlocal called
        called = True
        return httpx.Response(200, json=REG_RESPONSE, request=httpx.Request("POST", url))

    monkeypatch.setattr(cli_server.httpx, "post", fake_post)
    rc = node_cli.main(["--json", "node", "register", "host1"])
    assert rc == 1
    assert not called


def test_register_force_overrides_guard(isolated_relay_dir, monkeypatch):
    node_utils.META_PATH.write_text('{"node_id": "OLD00000"}')

    def fake_post(url, json=None, **kw):  # noqa: A002
        return httpx.Response(200, json=REG_RESPONSE, request=httpx.Request("POST", url))

    monkeypatch.setattr(cli_server.httpx, "post", fake_post)
    rc = node_cli.main(["--json", "node", "register", "host1", "--force"])
    assert rc == 0
    meta = json.loads(node_utils.META_PATH.read_text())
    assert meta["node_id"] == "ABCD1234"


def test_register_409_writes_nothing(isolated_relay_dir, monkeypatch):
    def fake_post(url, **kw):
        return httpx.Response(
            409, json={"detail": "node_name already exists"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(cli_server.httpx, "post", fake_post)
    rc = node_cli.main(["--json", "node", "register", "host1"])
    assert rc == 1
    assert not node_utils.META_PATH.exists()
    assert not node_utils.TOKEN_PATH.exists()


def test_register_network_error(isolated_relay_dir, monkeypatch):
    def fake_post(url, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(cli_server.httpx, "post", fake_post)
    rc = node_cli.main(["--json", "node", "register", "host1"])
    assert rc == 1
    assert not node_utils.META_PATH.exists()


def test_register_missing_server_arg(isolated_relay_dir):
    with pytest.raises(SystemExit):
        node_cli.main(["node", "register"])


# ---------------------------------------------------------------------------
# server health / metrics
# ---------------------------------------------------------------------------

PROBE_OK = {
    "ok": True, "version": "2.0.0", "mode": "core",
    "database": "ok", "scheduler": "ok",
    "nodes_total": 6, "nodes_online": 4, "queue_depth": 0,
    "tasks_completed": 311, "tasks_failed": 24, "tasks_cancelled": 7,
    "stages_total": 311, "stages_retry_ratio": 0.0322,
    "node_load": {"E4W3CBWQ": 12.87},
}


def test_server_health_json(isolated_relay_dir, monkeypatch, capsys):
    monkeypatch.setattr(cli_server, "probe_server_endpoint", lambda base, **kw: dict(PROBE_OK))
    rc = node_cli.main(["--json", "server", "health", "http://192.168.2.60:8788"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["nodes_online"] == 4


def test_server_health_pretty_print(isolated_relay_dir, monkeypatch, capsys):
    monkeypatch.setattr(cli_server, "probe_server_endpoint", lambda base, **kw: dict(PROBE_OK))
    rc = node_cli.main(["server", "health", "http://h:8788"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "4/6 online" in out
    assert "311" in out


def test_server_health_unreachable(isolated_relay_dir, monkeypatch):
    monkeypatch.setattr(
        cli_server, "probe_server_endpoint",
        lambda base, **kw: {"ok": False, "error": "health: connection refused"},
    )
    rc = node_cli.main(["--json", "server", "health", "http://h:8788"])
    assert rc == 1


def test_server_health_default_base_from_config(isolated_relay_dir, monkeypatch):
    node_utils.CONFIG_PATH.write_text(json.dumps({"base_url": "http://cfg-host:8788"}))
    seen: dict = {}
    monkeypatch.setattr(
        cli_server, "probe_server_endpoint", lambda base, **kw: (seen.update(base=base), dict(PROBE_OK))[1]
    )
    rc = node_cli.main(["--json", "server", "health"])
    assert rc == 0
    assert seen["base"] == "http://cfg-host:8788"


def test_server_metrics_lists_node_load(isolated_relay_dir, monkeypatch, capsys):
    monkeypatch.setattr(cli_server, "probe_server_endpoint", lambda base, **kw: dict(PROBE_OK))
    rc = node_cli.main(["server", "metrics", "http://h:8788"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "E4W3CBWQ" in out
    assert "12.87" in out


def test_server_metrics_no_server_configured(isolated_relay_dir):
    rc = node_cli.main(["--json", "server", "metrics"])
    assert rc == 1