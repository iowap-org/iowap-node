"""T-021: node dashboard on port 7783 (capability management + MCP control).

Contract (board T-021, DEC #8 — extends the T-017 onboarding UI into a
full node dashboard in the relay-dashboard design family):

* ``GET /`` serves the registration FORM while the node is blank and the
  full DASHBOARD once configured — same server, same port, rendering is
  state-based, so the server keeps running across the blank→configured
  transition (``wait_for_relay_config(serve_dashboard=True)``).
* Capability management writes ``<workspace>/bridge/providers.yaml``
  (the shared workspace channel to the bridge daemon, T-022): create
  (name ``^[a-z][a-z0-9_.-]{1,63}$``, type ``process|http`` with
  typ-specific fields), delete, and the YAML stays valid + duplicate
  names are refused.
* Publish toggle writes ``bridge.allowlist`` / ``bridge.auto_publish``
  into node.yaml — the rest of the ``bridge:`` block (url, token,
  poll_interval …) is preserved untouched — and SIGHUPs the daemon pid
  files (the existing reload path).
* MCP: enable/disable drive the in-container MCP process via the
  ``McpSupervisor`` (pid file ``~/.relay/mcp.pid``); bearer rotation
  generates ``iw_mcp_``-prefixed tokens (crypto/rand parity with the
  daemon's internal/provision), writes ``~/.relay/mcp_bearer`` (0600)
  and restarts a running MCP process. The SPEC §9 snippets re-render
  with the NEW bearer.
* The dashboard only ever writes: workspace ``bridge/providers.yaml``,
  node.yaml (bridge.allowlist/auto_publish) and ``~/.relay/mcp_bearer``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nodes.common import dashboard, node_config, node_utils, onboarding

BRIDGE_YAML = (
    "node_name: win-node\n"
    "bridge:\n"
    "  url: http://host.docker.internal:7780\n"
    "  token: bridging\n"
    "  workspace: /workspace\n"
    "  auto_publish: false\n"
    "  allowlist: []\n"
    "  poll_interval: 60\n"
)


@pytest.fixture()
def dash_env(tmp_path, monkeypatch):
    """Isolate every state path the dashboard touches."""
    for name in (
        "META_PATH", "TOKEN_PATH", "CONFIG_PATH",
        "LEGACY_META_PATH", "LEGACY_TOKEN_PATH", "STATUS_PATH",
    ):
        monkeypatch.setattr(node_utils, name, tmp_path / f"{name.lower()}.json")
    monkeypatch.setattr(node_config, "ACTIVE_PATH", tmp_path / "node.yaml")
    monkeypatch.setattr(node_config, "BASE_DIR", tmp_path / "relay")
    monkeypatch.setenv("IOWAP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("IOWAP_MCP_BEARER", raising=False)
    monkeypatch.setattr(onboarding, "_discover_relay_mdns", lambda timeout=2.0: None)
    return tmp_path


def _configure(dash_env):
    """Make the node look configured (meta + token + relay config)."""
    node_utils.META_PATH.write_text(json.dumps(
        {"node_id": "N1", "node_name": "win-node", "base_url": "http://r:8788"}))
    node_utils.TOKEN_PATH.write_text(json.dumps({"token": "tok-1"}))
    node_utils.CONFIG_PATH.write_text(json.dumps({"base_url": "http://r:8788"}))


class _Resp:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


def _post(server, data, path):
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


def _start_server(dash_env, monkeypatch):
    srv = onboarding.start_dashboard(port=0)
    assert srv is not None
    return srv


class FakeSupervisor:
    """Process-interface stand-in: records calls, never spawns."""

    def __init__(self, running=False):
        self.running = running
        self.started = 0
        self.stopped = 0
        self.restarts = 0

    def is_running(self):
        return self.running

    def start(self):
        self.started += 1
        self.running = True
        return True, "started"

    def stop(self):
        self.stopped += 1
        self.running = False
        return True, "stopped"

    def restart(self):
        self.restarts += 1
        return True, "restarted"


# ---------------------------------------------------------------------------
# Bearer generation / rotation
# ---------------------------------------------------------------------------


def test_generate_bearer_format():
    token = dashboard.generate_bearer()
    assert token.startswith("iw_mcp_")
    # 32 bytes → 43 unpadded base64url chars, matching the daemon's Go generator.
    assert len(token) == 7 + 43
    assert all(c.isalnum() or c in "-_" for c in token[7:])
    assert dashboard.generate_bearer() != token


def test_bearer_file_roundtrip_and_env_fallback(dash_env, monkeypatch):
    assert dashboard.current_bearer() == ""
    monkeypatch.setenv("IOWAP_MCP_BEARER", "iw_mcp_fromenv")
    assert dashboard.current_bearer() == "iw_mcp_fromenv"
    # a written bearer file wins over the env
    path = dashboard.mcp_bearer_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("iw_mcp_fromfile\n")
    assert dashboard.current_bearer() == "iw_mcp_fromfile"


def test_rotate_bearer_writes_file_and_restarts(dash_env):
    fake = FakeSupervisor(running=True)
    bearer, restarted = dashboard.rotate_bearer(supervisor=fake)
    assert restarted is True
    assert fake.restarts == 1
    path = dashboard.mcp_bearer_path()
    assert path.exists()
    assert path.read_text().strip() == bearer
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_rotate_bearer_without_running_process_skips_restart(dash_env):
    fake = FakeSupervisor(running=False)
    _, restarted = dashboard.rotate_bearer(supervisor=fake)
    assert restarted is False
    assert fake.restarts == 0


# ---------------------------------------------------------------------------
# Capability store (workspace bridge/providers.yaml)
# ---------------------------------------------------------------------------


def test_workspace_dir_default_is_slash_workspace(monkeypatch):
    monkeypatch.delenv("IOWAP_WORKSPACE_DIR", raising=False)
    assert str(dashboard.workspace_dir()) == "/workspace"


def test_create_process_provider_writes_valid_yaml(dash_env):
    ok, msg = dashboard.create_provider({
        "name": "script.cleanup",
        "type": "process",
        "summary": "Run the cleanup script",
        "command": 'python C:/scripts/cleanup.py --target "{{target}}"',
        "artifacts_glob": "out/*.png",
    })
    assert ok, msg
    doc = yaml.safe_load(dashboard.providers_path().read_text())
    entry = doc["providers"][0]
    assert entry["name"] == "script.cleanup"
    assert entry["type"] == "process"
    assert entry["command"] == ["python", "C:/scripts/cleanup.py", "--target", "{{target}}"]
    assert entry["artifacts_glob"] == "out/*.png"
    assert entry["summary"] == "Run the cleanup script"


def test_create_http_provider_builds_exec_block(dash_env):
    ok, msg = dashboard.create_provider({
        "name": "render.doc",
        "type": "http",
        "health_url": "http://127.0.0.1:9000/health",
        "method": "POST",
        "url_template": "http://127.0.0.1:9000/api/{{task_id}}/render",
        "body_template": '{"file": "{{file}}"}',
        "result_path": "result",
        "artifact_paths": "result.output_path, result.log",
    })
    assert ok, msg
    entry = yaml.safe_load(dashboard.providers_path().read_text())["providers"][0]
    assert entry["health_url"] == "http://127.0.0.1:9000/health"
    assert entry["exec"]["method"] == "POST"
    assert entry["exec"]["url_template"].endswith("/render")
    assert entry["exec"]["artifact_paths"] == ["result.output_path", "result.log"]


def test_create_rejects_invalid_name_without_writing(dash_env):
    for bad in ("UPPER", "1start", "has space", "", "a", "x" * 65):
        ok, _ = dashboard.create_provider({"name": bad, "type": "process", "command": "run"})
        assert not ok
    assert not dashboard.providers_path().exists()


def test_create_rejects_duplicate_name(dash_env):
    assert dashboard.create_provider({"name": "a.cap", "type": "process", "command": "run"})[0]
    before = dashboard.providers_path().read_text()
    ok, msg = dashboard.create_provider({"name": "a.cap", "type": "http", "url_template": "u"})
    assert not ok
    assert "a.cap" in msg
    assert dashboard.providers_path().read_text() == before


def test_create_rejects_process_without_command(dash_env):
    ok, msg = dashboard.create_provider({"name": "a.cap", "type": "process"})
    assert not ok
    assert "command" in msg.lower()


def test_create_rejects_unknown_type(dash_env):
    ok, _ = dashboard.create_provider({"name": "a.cap", "type": "comfyui", "command": "run"})
    assert not ok


def test_delete_provider_removes_entry(dash_env):
    dashboard.create_provider({"name": "a.cap", "type": "process", "command": "run"})
    dashboard.create_provider({"name": "b.cap", "type": "process", "command": "run"})
    ok, _ = dashboard.delete_provider("a.cap")
    assert ok
    names = [p["name"] for p in dashboard.load_providers()]
    assert names == ["b.cap"]
    ok, msg = dashboard.delete_provider("missing")
    assert not ok
    assert "missing" in msg


def test_load_providers_missing_file_is_empty_list(dash_env):
    assert dashboard.load_providers() == []


# ---------------------------------------------------------------------------
# Publish toggle (node.yaml bridge block)
# ---------------------------------------------------------------------------


def test_set_capability_published_updates_allowlist_only(dash_env):
    node_config.ACTIVE_PATH.write_text(BRIDGE_YAML)
    ok, _ = dashboard.set_capability_published("script.cleanup", True)
    assert ok
    doc = yaml.safe_load(node_config.ACTIVE_PATH.read_text())
    assert doc["bridge"]["allowlist"] == ["script.cleanup"]
    assert doc["bridge"]["url"] == "http://host.docker.internal:7780"
    assert doc["bridge"]["token"] == "bridging"
    assert doc["bridge"]["poll_interval"] == 60
    assert doc["node_name"] == "win-node"
    # toggle back off
    dashboard.set_capability_published("script.cleanup", False)
    doc = yaml.safe_load(node_config.ACTIVE_PATH.read_text())
    assert doc["bridge"]["allowlist"] == []


def test_set_capability_published_creates_missing_bridge_block(dash_env):
    node_config.ACTIVE_PATH.write_text("node_name: win-node\n")
    ok, _ = dashboard.set_capability_published("a.cap", True)
    assert ok
    doc = yaml.safe_load(node_config.ACTIVE_PATH.read_text())
    assert doc["bridge"]["allowlist"] == ["a.cap"]
    assert doc["node_name"] == "win-node"


def test_set_auto_publish(dash_env):
    node_config.ACTIVE_PATH.write_text(BRIDGE_YAML)
    dashboard.set_auto_publish(True)
    doc = yaml.safe_load(node_config.ACTIVE_PATH.read_text())
    assert doc["bridge"]["auto_publish"] is True
    assert doc["bridge"]["url"] == "http://host.docker.internal:7780"
    dashboard.set_auto_publish(False)
    assert yaml.safe_load(node_config.ACTIVE_PATH.read_text())["bridge"]["auto_publish"] is False


def test_is_published_mirrors_bridge_gate():
    cfg = {"auto_publish": False, "allowlist": []}
    assert dashboard.is_published("a.cap", cfg) is False
    assert dashboard.is_published("a.cap", {**cfg, "allowlist": ["a.cap"]}) is True
    assert dashboard.is_published("a.cap", {**cfg, "allowlist": ["*"]}) is True
    # auto_publish only applies while the allowlist is empty
    assert dashboard.is_published("a.cap", {**cfg, "auto_publish": True}) is True
    assert (
        dashboard.is_published("a.cap", {**cfg, "auto_publish": True, "allowlist": ["b"]})
        is False
    )


def test_is_published_handles_missing_config():
    assert dashboard.is_published("a.cap", None) is False


# ---------------------------------------------------------------------------
# McpSupervisor (process interface — mocked in tests)
# ---------------------------------------------------------------------------


def test_supervisor_start_stop_with_fake_spawner(dash_env, monkeypatch):
    created = []

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_spawner(argv):
        created.append(argv)
        return FakeProc()

    sup = dashboard.McpSupervisor(
        port=7781, spawner=fake_spawner, command=["iowap-mcp", "--http", "--port", "7781"]
    )
    assert sup.is_running() is False
    ok, _ = sup.start()
    assert ok
    assert created == [["iowap-mcp", "--http", "--port", "7781"]]
    assert sup.is_running() is True
    pid_path = dashboard.mcp_pid_path()
    assert pid_path.exists()
    assert pid_path.read_text().strip() == "4242"
    # start is idempotent while running
    ok, _ = sup.start()
    assert ok
    assert len(created) == 1
    ok, _ = sup.stop()
    assert ok
    assert not pid_path.exists()
    assert sup.is_running() is False


def test_supervisor_restart(dash_env):
    fake_spawner_calls = []

    class FakeProc:
        pid = 7

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    sup = dashboard.McpSupervisor(
        port=7781, spawner=lambda argv: (fake_spawner_calls.append(argv) or FakeProc()),
        command=["iowap-mcp", "--http"],
    )
    sup.start()
    ok, _ = sup.restart()
    assert ok
    assert len(fake_spawner_calls) == 2
    assert sup.is_running()


# ---------------------------------------------------------------------------
# HTTP surface — dashboard render + POST routes
# ---------------------------------------------------------------------------


def test_get_root_configured_renders_dashboard(dash_env, monkeypatch):
    _configure(dash_env)
    node_config.ACTIVE_PATH.write_text(BRIDGE_YAML)
    dashboard.create_provider({"name": "script.cleanup", "type": "process",
                               "summary": "cleanup", "command": "python clean.py"})
    monkeypatch.setenv("IOWAP_MCP_BEARER", "iw_mcp_envtoken")
    srv = _start_server(dash_env, monkeypatch)
    try:
        resp = _get(srv)
        assert resp.status_code == 200
        assert "N1" in resp.text                # node id
        assert "win-node" in resp.text          # node name
        assert "http://r:8788" in resp.text     # relay url
        assert "script.cleanup" in resp.text    # capability table
        assert "host.docker.internal:7780" in resp.text  # bridge url
        assert "hermes mcp add iowap" in resp.text       # snippets present
        assert "/caps/create" in resp.text
    finally:
        onboarding.shutdown(srv)


def test_get_root_blank_still_renders_form(dash_env, monkeypatch):
    srv = _start_server(dash_env, monkeypatch)
    try:
        resp = _get(srv)
        assert resp.status_code == 200
        assert 'name="name"' in resp.text
        assert 'name="relay"' in resp.text
        assert "/configure" in resp.text
    finally:
        onboarding.shutdown(srv)


def test_caps_create_via_http(dash_env, monkeypatch):
    _configure(dash_env)
    srv = _start_server(dash_env, monkeypatch)
    try:
        resp = _post(srv, {
            "name": "script.cleanup", "type": "process", "summary": "cleanup",
            "command": "python clean.py", "artifacts_glob": "out/*.png",
        }, "/caps/create")
        assert resp.status_code == 200
        assert "script.cleanup" in resp.text
        doc = yaml.safe_load(dashboard.providers_path().read_text())
        assert doc["providers"][0]["name"] == "script.cleanup"

        # invalid name → 400 + message, nothing written
        resp = _post(srv, {"name": "Bad Name", "type": "process", "command": "x"},
                     "/caps/create")
        assert resp.status_code == 400
        assert "Bad Name" in resp.text
        # duplicate → 400
        resp = _post(srv, {"name": "script.cleanup", "type": "process", "command": "x"},
                     "/caps/create")
        assert resp.status_code == 400
    finally:
        onboarding.shutdown(srv)


def test_caps_delete_via_http(dash_env, monkeypatch):
    _configure(dash_env)
    dashboard.create_provider({"name": "gone.cap", "type": "process", "command": "x"})
    srv = _start_server(dash_env, monkeypatch)
    try:
        resp = _post(srv, {"name": "gone.cap"}, "/caps/delete")
        assert resp.status_code == 200
        assert dashboard.load_providers() == []
        resp = _post(srv, {"name": "nope.cap"}, "/caps/delete")
        assert resp.status_code == 400
    finally:
        onboarding.shutdown(srv)


def test_caps_publish_unpublish_via_http(dash_env, monkeypatch):
    _configure(dash_env)
    node_config.ACTIVE_PATH.write_text(BRIDGE_YAML)
    srv = _start_server(dash_env, monkeypatch)
    try:
        resp = _post(srv, {"name": "script.cleanup"}, "/caps/publish")
        assert resp.status_code == 200
        doc = yaml.safe_load(node_config.ACTIVE_PATH.read_text())
        assert doc["bridge"]["allowlist"] == ["script.cleanup"]
        assert doc["bridge"]["url"] == "http://host.docker.internal:7780"
        resp = _post(srv, {"name": "script.cleanup"}, "/caps/unpublish")
        assert resp.status_code == 200
        doc = yaml.safe_load(node_config.ACTIVE_PATH.read_text())
        assert doc["bridge"]["allowlist"] == []
    finally:
        onboarding.shutdown(srv)


def test_caps_auto_publish_via_http(dash_env, monkeypatch):
    _configure(dash_env)
    node_config.ACTIVE_PATH.write_text(BRIDGE_YAML)
    srv = _start_server(dash_env, monkeypatch)
    try:
        assert _post(srv, {"enable": "1"}, "/caps/auto-publish").status_code == 200
        assert yaml.safe_load(node_config.ACTIVE_PATH.read_text())["bridge"]["auto_publish"] is True
        assert _post(srv, {}, "/caps/auto-publish").status_code == 200
        assert yaml.safe_load(node_config.ACTIVE_PATH.read_text())["bridge"]["auto_publish"] is False
    finally:
        onboarding.shutdown(srv)


def test_mcp_enable_disable_via_http(dash_env, monkeypatch):
    _configure(dash_env)
    fake = FakeSupervisor(running=False)
    monkeypatch.setattr(dashboard, "build_supervisor", lambda: fake)
    srv = _start_server(dash_env, monkeypatch)
    try:
        resp = _post(srv, {}, "/mcp/enable")
        assert resp.status_code == 200
        assert fake.started == 1
        assert "started" in resp.text.lower() or "running" in resp.text.lower()
        resp = _post(srv, {}, "/mcp/disable")
        assert resp.status_code == 200
        assert fake.stopped == 1
    finally:
        onboarding.shutdown(srv)


def test_bearer_rotate_via_http_shows_new_bearer_in_snippets(dash_env, monkeypatch):
    _configure(dash_env)
    monkeypatch.setenv("IOWAP_MCP_BEARER", "iw_mcp_oldtoken")
    fake = FakeSupervisor(running=True)
    monkeypatch.setattr(dashboard, "build_supervisor", lambda: fake)
    srv = _start_server(dash_env, monkeypatch)
    try:
        resp = _post(srv, {}, "/mcp/bearer/rotate")
        assert resp.status_code == 200
        assert fake.restarts == 1
        new_bearer = dashboard.mcp_bearer_path().read_text().strip()
        assert new_bearer.startswith("iw_mcp_")
        assert new_bearer != "iw_mcp_oldtoken"
        # snippets re-render with the NEW bearer (claude/codex show it)
        assert new_bearer in resp.text
        # the old bearer is gone from the page
        assert "iw_mcp_oldtoken" not in resp.text
    finally:
        onboarding.shutdown(srv)


def test_dashboard_posts_refused_while_blank(dash_env, monkeypatch):
    srv = _start_server(dash_env, monkeypatch)
    try:
        resp = _post(srv, {"name": "x", "type": "process", "command": "x"}, "/caps/create")
        assert resp.status_code == 409
        resp = _post(srv, {}, "/mcp/enable")
        assert resp.status_code == 409
    finally:
        onboarding.shutdown(srv)


def test_dashboard_unknown_post_path_404(dash_env, monkeypatch):
    _configure(dash_env)
    srv = _start_server(dash_env, monkeypatch)
    try:
        assert _post(srv, {}, "/caps/frobnicate").status_code == 404
    finally:
        onboarding.shutdown(srv)


# ---------------------------------------------------------------------------
# Server lifecycle — the dashboard keeps serving after configuration
# ---------------------------------------------------------------------------


def test_start_dashboard_serves_when_configured(dash_env, monkeypatch):
    _configure(dash_env)
    monkeypatch.delenv("IOWAP_ONBOARDING_PORT", raising=False)
    srv = onboarding.start_dashboard(port=0)
    try:
        assert srv is not None
        assert _get(srv).text  # configured → dashboard, no exception
    finally:
        onboarding.shutdown(srv)


def test_wait_for_relay_config_serves_dashboard_when_configured(
    dash_env, monkeypatch
):
    _configure(dash_env)
    monkeypatch.setenv("IOWAP_ONBOARDING_PORT", "0")
    onboarding._active_server = None
    ready = node_utils.wait_for_relay_config(poll_seconds=0.01, serve_dashboard=True)
    try:
        assert ready is not None
        srv = onboarding._active_server
        assert srv is not None
        assert "N1" in _get(srv).text
    finally:
        onboarding._active_server = None
