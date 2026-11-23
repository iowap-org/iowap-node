"""T-017: node-side onboarding UI inside the container (blank-start HTTP server).

Contract:

* In the blank-start state the node serves a small HTTP UI on port 7783
  (bind 0.0.0.0 inside the container — the compose mapping keeps it
  loopback-only on the host, mirroring the MCP port 7781).
* ``GET /`` IS the page (no landing page / redirect — Ronny 2026-11-22):
  unconfigured → registration form (node name + relay URL), configured →
  status + MCP-Bearer snippets. Title is self-explanatory
  ("IOWAP node setup") — it is the user's first contact point after the
  daemon bootstrap opens the browser.
* The relay field is prefilled from in-process mDNS discovery
  (``_discover_relay_mdns``); discovery failure leaves the form
  functional with an empty prefill.
* ``POST /configure`` writes the config files the blank-start node
  expects (relay_config.json + node.yaml — node.yaml's ``bridge:`` block
  is never touched) and then registers in-process (the register logic of
  ``iowap-cli node register`` — no CLI subprocess). Result (node_id +
  status ok/pending) is shown inline; errors as plain text.
* POST is refused once the node is configured (status-only page after
  that). No SIGHUP is needed: the T-011 2s config poll picks the new
  files up on its own (integration-tested below).
* MCP snippets: the four SPEC §9 formats (Hermes one-liner, Claude
  mcpServers-JSON, Codex config.toml, curl probe) rendered from the
  node's own ``IOWAP_MCP_BEARER`` env. Missing env → "Bearer not
  provisioned" section instead of a crash.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nodes.common import node_config, node_utils, onboarding
from nodes.common.cli import cli_server

REG_RESPONSE = {
    "node_id": "ABCD1234",
    "node_name": "win-vm-node",
    "status": "pending",
    "token_type": "temporary",
    "token": "tp_abc123",
    "expires_at": "2026-12-03T12:00:00Z",
    "registration_secret": "rs_xyz",
}


@pytest.fixture()
def onb_env(tmp_path, monkeypatch):
    """Isolate every state path the onboarding server touches."""
    for name in (
        "META_PATH", "TOKEN_PATH", "CONFIG_PATH",
        "LEGACY_META_PATH", "LEGACY_TOKEN_PATH", "STATUS_PATH",
    ):
        monkeypatch.setattr(node_utils, name, tmp_path / f"{name.lower()}.json")
    monkeypatch.setattr(node_config, "ACTIVE_PATH", tmp_path / "node.yaml")
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("IOWAP_MCP_BEARER", raising=False)
    # mDNS is never real in tests — default: discovery finds nothing.
    monkeypatch.setattr(onboarding, "_discover_relay_mdns", lambda timeout=2.0: None)
    return tmp_path


def _fake_register(monkeypatch, response=REG_RESPONSE, calls=None):
    def fake_post(url, json=None, **kw):  # noqa: A002 — mirrors httpx signature
        if calls is not None:
            calls.append({"url": url, "body": json})
        return httpx.Response(200, json=response, request=httpx.Request("POST", url))

    monkeypatch.setattr(cli_server.httpx, "post", fake_post)


class _Resp:
    """Minimal response stand-in (status_code + text) for urllib answers."""

    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


def _post(server, data, path="/configure"):
    import urllib.error
    import urllib.parse
    import urllib.request

    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}{path}", data=body, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return _Resp(resp.status, resp.read().decode())
    except urllib.error.HTTPError as exc:
        return _Resp(exc.code, exc.read().decode())


def _get(server, path="/"):
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_address[1]}{path}", timeout=10
        ) as resp:
            return _Resp(resp.status, resp.read().decode())
    except urllib.error.HTTPError as exc:
        return _Resp(exc.code, exc.read().decode())


def _start(onb_env, monkeypatch):
    monkeypatch.setattr(onboarding, "_discover_relay_mdns", lambda timeout=2.0: None)
    srv = onboarding.start_if_blank(port=0)
    assert srv is not None
    return srv


# ---------------------------------------------------------------------------
# GET / — the page itself
# ---------------------------------------------------------------------------


def test_get_root_blank_serves_registration_form(onb_env, monkeypatch):
    srv = _start(onb_env, monkeypatch)
    try:
        resp = _get(srv)
        assert resp.status_code == 200
        assert "IOWAP node setup" in resp.text
        assert 'name="name"' in resp.text
        assert 'name="relay"' in resp.text
        assert "/configure" in resp.text
        # no discovery hit → empty prefill, form still fully functional
        assert 'value=""' in resp.text
    finally:
        onboarding.shutdown(srv)


def test_get_root_prefills_relay_from_mdns(onb_env, monkeypatch):
    monkeypatch.setattr(
        onboarding, "_discover_relay_mdns", lambda timeout=2.0: "http://192.0.2.60:8788"
    )
    srv = onboarding.start_if_blank(port=0)
    try:
        resp = _get(srv)
        assert 'value="http://192.0.2.60:8788"' in resp.text
    finally:
        onboarding.shutdown(srv)


def test_get_root_survives_mdns_failure(onb_env, monkeypatch):
    def boom(timeout=2.0):
        raise OSError("zeroconf exploded")

    monkeypatch.setattr(onboarding, "_discover_relay_mdns", boom)
    srv = onboarding.start_if_blank(port=0)
    try:
        resp = _get(srv)
        assert resp.status_code == 200
        assert 'name="relay"' in resp.text
    finally:
        onboarding.shutdown(srv)


def test_get_root_configured_shows_status_and_snippets(onb_env, monkeypatch):
    monkeypatch.setenv("IOWAP_MCP_BEARER", "iw_mcp_secret")
    srv = onboarding.start_if_blank(port=0)
    assert srv is not None
    # configuration arrives while the server is already up (race window)
    node_utils.META_PATH.write_text(
        json.dumps({"node_id": "N1", "node_name": "win-vm-node",
                    "base_url": "http://192.0.2.60:8788"})
    )
    node_utils.TOKEN_PATH.write_text(json.dumps({"token": "tok-1"}))
    node_utils.CONFIG_PATH.write_text(json.dumps({"base_url": "http://192.0.2.60:8788"}))
    try:
        resp = _get(srv)
        assert resp.status_code == 200
        assert "N1" in resp.text
        assert "iw_mcp_secret" in resp.text
        # all four SPEC §9 snippet formats (html-escaped in the page)
        assert "hermes mcp add iowap" in resp.text
        assert "mcpServers" in resp.text
        assert "[mcp_servers.iowap]" in resp.text
        assert "curl -i -X POST" in resp.text
        # configured → the form is gone, POST is refused
        assert 'name="relay"' not in resp.text
        resp_post = _post(srv, {"name": "x", "relay": "http://192.0.2.60:8788"})
        assert resp_post.status_code == 409
    finally:
        onboarding.shutdown(srv)


def test_get_root_configured_without_bearer_shows_provision_hint(onb_env, monkeypatch):
    srv = onboarding.start_if_blank(port=0)
    assert srv is not None
    node_utils.META_PATH.write_text(
        json.dumps({"node_id": "N1", "node_name": "n", "base_url": "http://r:8788"})
    )
    node_utils.TOKEN_PATH.write_text(json.dumps({"token": "tok-1"}))
    node_utils.CONFIG_PATH.write_text(json.dumps({"base_url": "http://r:8788"}))
    try:
        resp = _get(srv)
        assert resp.status_code == 200
        assert "Bearer not provisioned" in resp.text
    finally:
        onboarding.shutdown(srv)


# ---------------------------------------------------------------------------
# POST /configure
# ---------------------------------------------------------------------------


def test_post_configure_writes_config_and_registers(onb_env, monkeypatch):
    calls: list = []
    _fake_register(monkeypatch, calls=calls)
    srv = _start(onb_env, monkeypatch)
    try:
        resp = _post(srv, {"name": "win-vm-node", "relay": "192.0.2.60:8788"})
        assert resp.status_code == 200
        assert "ABCD1234" in resp.text
        assert "pending" in resp.text

        # register happened in-process against the normalized URL
        assert calls[0]["url"] == "http://192.0.2.60:8788/relay/v2/auth/register"
        assert calls[0]["body"]["node_name"] == "win-vm-node"

        # config files the blank-start node expects
        cfg = json.loads(node_utils.CONFIG_PATH.read_text())
        assert cfg["base_url"] == "http://192.0.2.60:8788"
        meta = json.loads(node_utils.META_PATH.read_text())
        assert meta["node_id"] == "ABCD1234"
        tok = json.loads(node_utils.TOKEN_PATH.read_text())
        assert tok["token"] == "tp_abc123"
        node_yaml = node_config.ACTIVE_PATH.read_text()
        assert "node_name: win-vm-node" in node_yaml
    finally:
        onboarding.shutdown(srv)


def test_post_configure_preserves_node_yaml_bridge_block(onb_env, monkeypatch):
    _fake_register(monkeypatch)
    node_config.ACTIVE_PATH.write_text(
        "node_name: old-name\nbridge:\n  url: http://host.docker.internal:7780\n"
        "  token: bridging\n  workspace: /workspace\n  auto_publish: false\n"
        "  allowlist: []\n  poll_interval: 60\n"
    )
    srv = _start(onb_env, monkeypatch)
    try:
        resp = _post(srv, {"name": "renamed", "relay": "http://192.0.2.60:8788"})
        assert resp.status_code == 200
        text = node_config.ACTIVE_PATH.read_text()
        assert "node_name: renamed" in text
        assert "http://host.docker.internal:7780" in text
        assert "auto_publish: false" in text
    finally:
        onboarding.shutdown(srv)


def test_post_configure_rejects_empty_name(onb_env, monkeypatch):
    _fake_register(monkeypatch)
    srv = _start(onb_env, monkeypatch)
    try:
        resp = _post(srv, {"name": "   ", "relay": "http://192.0.2.60:8788"})
        assert resp.status_code == 400
        assert "node name" in resp.text.lower()
        assert not node_utils.META_PATH.exists()
        assert not node_utils.CONFIG_PATH.exists()
    finally:
        onboarding.shutdown(srv)


def test_post_configure_rejects_missing_relay(onb_env, monkeypatch):
    _fake_register(monkeypatch)
    srv = _start(onb_env, monkeypatch)
    try:
        resp = _post(srv, {"name": "n", "relay": ""})
        assert resp.status_code == 400
        assert not node_utils.META_PATH.exists()
    finally:
        onboarding.shutdown(srv)


def test_post_configure_register_failure_is_plaintext(onb_env, monkeypatch):
    def failing_post(url, json=None, **kw):  # noqa: A002
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(cli_server.httpx, "post", failing_post)
    srv = _start(onb_env, monkeypatch)
    try:
        resp = _post(srv, {"name": "n", "relay": "http://192.0.2.99:8788"})
        assert resp.status_code == 502
        assert "registration failed" in resp.text
        assert "connection refused" in resp.text
        # no node identity written — the node stays blank and retryable
        assert not node_utils.META_PATH.exists()
    finally:
        onboarding.shutdown(srv)


def test_post_configure_unknown_path_404(onb_env, monkeypatch):
    srv = _start(onb_env, monkeypatch)
    try:
        assert _get(srv, "/nope").status_code == 404
        assert _post(srv, {}, path="/other").status_code == 404
    finally:
        onboarding.shutdown(srv)


# ---------------------------------------------------------------------------
# Config pickup — the T-011 2s poll is enough (no SIGHUP needed)
# ---------------------------------------------------------------------------


def test_blank_pickup_after_configure_without_sighup(onb_env, monkeypatch):
    _fake_register(monkeypatch)
    srv = _start(onb_env, monkeypatch)
    try:
        resp = _post(srv, {"name": "win-vm-node", "relay": "http://192.0.2.60:8788"})
        assert resp.status_code == 200
        # wait_for_relay_config (T-011) picks the files up on its own
        ready = node_utils.wait_for_relay_config(poll_seconds=0.05)
        assert ready is not None
        meta, cfg = ready
        assert meta["node_id"] == "ABCD1234"
        assert cfg["base_url"] == "http://192.0.2.60:8788"
    finally:
        onboarding.shutdown(srv)


def test_start_if_blank_is_noop_when_configured(onb_env):
    node_utils.META_PATH.write_text(json.dumps({"node_id": "N1"}))
    node_utils.TOKEN_PATH.write_text(json.dumps({"token": "t"}))
    node_utils.CONFIG_PATH.write_text(json.dumps({"base_url": "http://r:8788"}))
    assert onboarding.start_if_blank(port=0) is None


# ---------------------------------------------------------------------------
# Snippet builder (SPEC §9)
# ---------------------------------------------------------------------------


def test_mcp_snippets_exact_formats():
    snips = onboarding.mcp_snippets("iw_mcp_secret", mcp_port=7781)
    assert snips["hermes"] == (
        'hermes mcp add iowap --url http://127.0.0.1:7781/mcp '
        '--header "Authorization: Bearer iw_mcp_secret"'
    )
    claude = json.loads(snips["claude"])
    assert claude == {
        "mcpServers": {
            "iowap": {
                "type": "http",
                "url": "http://127.0.0.1:7781/mcp",
                "headers": {"Authorization": "Bearer iw_mcp_secret"},
            }
        }
    }
    assert snips["codex"] == (
        '[mcp_servers.iowap]\n'
        'url = "http://127.0.0.1:7781/mcp"\n'
        'http_headers = { "Authorization" = "Bearer iw_mcp_secret" }'
    )
    # T-016f: the probe is the two-step MCP handshake (initialize -> session
    # id -> further calls with Mcp-Session-Id); a single-shot curl answered
    # -32600 "Missing session ID" and looked like a server bug.
    assert "Mcp-Session-Id: <ID aus Schritt 1>" in snips["curl"]
    assert '"method":"initialize"' in snips["curl"]
    assert '"method":"tools/list"' in snips["curl"]
    assert "Bearer iw_mcp_secret" in snips["curl"]


def test_mcp_snippets_port_env_override(onb_env, monkeypatch):
    monkeypatch.setenv("IOWAP_MCP_PORT", "7999")
    snips = onboarding.mcp_snippets("b")
    assert "http://127.0.0.1:7999/mcp" in snips["hermes"]
