"""T-003: generic bridge handler — manifest pull, host.* publish gate, exec forward.

Contract (SPEC.md, board iowap-windows-stack):

* Pull model: the node polls ``GET /v1/manifest`` at the configured
  ``bridge.url`` (node.yaml ``bridge:`` block: url, token, workspace,
  auto_publish=false default, allowlist=[], poll_interval=60). The node
  exposes NO inbound endpoint.
* Manifest capabilities are merged into the local registry with a
  ``host.`` prefix. ``input_schema`` passes through 1:1 (no client-side
  validation — client-mcp T-009 convention); ``long_run`` is forwarded.
* Publish gate (DEC 2026-11-20, default-off): a capability is published
  only when ``auto_publish: true`` (with an empty allowlist), the name
  is listed in ``allowlist``, or the allowlist contains ``"*"``.
  auto_publish=true with a non-empty allowlist publishes ONLY allowlisted
  names. Gate-blocked caps are logged, never published.
* Collision with an already existing capability name → skip + log.
* ``health: "down"`` → cap is not published / gets unpublished.
* 3 consecutive poll errors → all ``host.`` caps unpublished; recovery →
  republish. Manifest ``version`` major != 1 → manifest ignored + logged.
* Exec forward: claimed ``host.`` tasks → ``POST /v1/exec/<name>`` with
  Bearer. SPEC §5 mapping — terminal errors (``capability.failed``,
  ``invalid_params``, 404 unknown cap, 401 auth, other 4xx) complete the
  stage with an error result; retryable errors (``provider.unavailable``,
  ``timeout``, 503 ``provider.busy``, other 5xx, transport errors) raise
  :class:`BridgeRetryableError` so the claim expires and the relay
  requeues (existing retry budget semantics).
* Artifact return path: workspace-relative paths from the exec response
  are resolved against the ``bridge.workspace`` volume root and uploaded
  via the existing artifact-upload API.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nodes.common import bridge, node_config, poll_daemon

MANIFEST_OK = {
    "version": 1,
    "provider": {"name": "windows-bridge", "host_os": "windows", "version": "0.1.0"},
    "capabilities": [
        {
            "name": "comfy.generate",
            "summary": "Stable-Diffusion via local ComfyUI",
            "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}}},
            "long_run": True,
            "health": "ok",
        }
    ],
}

BRIDGE_CFG_AUTO_ON = """
bridge:
  url: http://bridge.test:7780
  token: bridge-tok
  workspace: /workspace
  auto_publish: true
"""

BRIDGE_CFG_DEFAULT = """
bridge:
  url: http://bridge.test:7780
  token: bridge-tok
  workspace: /workspace
"""


@pytest.fixture()
def bridge_env(tmp_path, monkeypatch):
    """Isolate the active-profile paths and give every test a fresh manager."""
    active = tmp_path / "node.yaml"
    monkeypatch.setattr(node_config, "ACTIVE_PATH", active)
    monkeypatch.setattr(node_config, "_active_cache", node_config.ActiveProfileCache(path=active))
    bridge.reset_manager()
    yield tmp_path
    bridge.reset_manager()


def _write_active(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _active_with_bridge(bridge_block: str, caps: str = "") -> str:
    text = bridge_block
    if caps:
        text += caps
    else:
        text += "capabilities: []\n"
    return text


def _stub_manifest(monkeypatch, manifest):
    calls: list[str] = []

    def fake_fetch(url: str, token: str, timeout: float = 10.0):
        calls.append(url)
        if isinstance(manifest, Exception):
            raise manifest
        return manifest

    monkeypatch.setattr(bridge, "fetch_manifest", fake_fetch)
    return calls


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def test_load_bridge_config_none_without_block(bridge_env, monkeypatch):
    _write_active(node_config.ACTIVE_PATH, "capabilities: []\n")
    assert bridge.load_bridge_config() is None


def test_load_bridge_config_defaults(bridge_env, monkeypatch):
    _write_active(node_config.ACTIVE_PATH, BRIDGE_CFG_DEFAULT)
    cfg = bridge.load_bridge_config()
    assert cfg["url"] == "http://bridge.test:7780"
    assert cfg["token"] == "bridge-tok"
    assert cfg["workspace"] == "/workspace"
    # DEC 2026-11-20: auto_publish default-off
    assert cfg["auto_publish"] is False
    assert cfg["allowlist"] == []
    assert cfg["poll_interval"] == 60


def test_load_bridge_config_token_env_fallback(bridge_env, monkeypatch):
    monkeypatch.delenv("IOWAP_BRIDGE_TOKEN", raising=False)
    _write_active(
        node_config.ACTIVE_PATH,
        "bridge:\n  url: http://bridge.test:7780\n",
    )
    cfg = bridge.load_bridge_config()
    assert cfg["token"] == ""


# ---------------------------------------------------------------------------
# Manifest pull / merge / prefix
# ---------------------------------------------------------------------------


def test_manifest_pull_merge_prefix(bridge_env, monkeypatch):
    _write_active(node_config.ACTIVE_PATH, BRIDGE_CFG_AUTO_ON)
    calls = _stub_manifest(monkeypatch, MANIFEST_OK)

    bridge.get_manager().poll_once()

    assert calls == ["http://bridge.test:7780"]
    merged = node_config.load_active_profile()
    assert "host.comfy.generate" in [c["name"] for c in merged]
    cap = next(c for c in merged if c["name"] == "host.comfy.generate")
    assert cap["claimable"] is True
    assert cap["bridge"] is True
    assert cap["bridge_name"] == "comfy.generate"
    # input_schema passes through 1:1 — no client-side validation
    assert cap["input_schema"] == MANIFEST_OK["capabilities"][0]["input_schema"]
    assert cap["long_run"] is True
    # the base profile itself stays untouched
    base = node_config.load_active_profile_unmerged()
    assert "host.comfy.generate" not in [c["name"] for c in base]


def test_auto_publish_gate_default_off(bridge_env, monkeypatch, caplog):
    _write_active(node_config.ACTIVE_PATH, BRIDGE_CFG_DEFAULT)
    _stub_manifest(monkeypatch, MANIFEST_OK)

    with caplog.at_level(logging.INFO, logger="bridge"):
        bridge.get_manager().poll_once()

    merged = node_config.load_active_profile()
    assert "host.comfy.generate" not in [c["name"] for c in merged]
    assert bridge.get_manager().published == {}
    # detected, but only logged — never published
    assert any("comfy.generate" in r.message for r in caplog.records)


def test_allowlist_publishes_named_cap_only(bridge_env, monkeypatch):
    _write_active(
        node_config.ACTIVE_PATH,
        BRIDGE_CFG_DEFAULT.replace(
            "workspace: /workspace", "workspace: /workspace\n  allowlist: [comfy.generate]"
        ),
    )
    manifest = {
        "version": 1,
        "capabilities": [
            {"name": "comfy.generate", "health": "ok"},
            {"name": "other.thing", "health": "ok"},
        ],
    }
    _stub_manifest(monkeypatch, manifest)

    bridge.get_manager().poll_once()

    assert list(bridge.get_manager().published) == ["host.comfy.generate"]


def test_allowlist_star_publishes_all(bridge_env, monkeypatch):
    _write_active(
        node_config.ACTIVE_PATH,
        BRIDGE_CFG_DEFAULT.replace(
            "workspace: /workspace", "workspace: /workspace\n  allowlist: ['*']"
        ),
    )
    _stub_manifest(monkeypatch, MANIFEST_OK)

    bridge.get_manager().poll_once()

    assert list(bridge.get_manager().published) == ["host.comfy.generate"]


def test_collision_with_existing_cap_skipped(bridge_env, monkeypatch, caplog):
    _write_active(
        node_config.ACTIVE_PATH,
        BRIDGE_CFG_AUTO_ON
        + "capabilities:\n  - name: host.comfy.generate\n    handler: /bin/true\n    claimable: true\n",
    )
    _stub_manifest(monkeypatch, MANIFEST_OK)

    with caplog.at_level(logging.INFO, logger="bridge"):
        bridge.get_manager().poll_once()

    assert list(bridge.get_manager().published) == []
    assert any("collision" in r.message.lower() for r in caplog.records)


def test_invalid_manifest_name_skipped(bridge_env, monkeypatch, caplog):
    _write_active(node_config.ACTIVE_PATH, BRIDGE_CFG_AUTO_ON)
    manifest = {
        "version": 1,
        "capabilities": [{"name": "Bad Cap!", "health": "ok"}],
    }
    _stub_manifest(monkeypatch, manifest)

    with caplog.at_level(logging.INFO, logger="bridge"):
        bridge.get_manager().poll_once()

    assert bridge.get_manager().published == {}
    assert any("Bad Cap!" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Health / error-driven unpublish
# ---------------------------------------------------------------------------


def test_health_down_unpublishes_and_recovers(bridge_env, monkeypatch):
    _write_active(node_config.ACTIVE_PATH, BRIDGE_CFG_AUTO_ON)
    _stub_manifest(monkeypatch, MANIFEST_OK)
    mgr = bridge.get_manager()
    mgr.poll_once()
    assert list(mgr.published) == ["host.comfy.generate"]

    down = {
        "version": 1,
        "capabilities": [{"name": "comfy.generate", "health": "down"}],
    }
    _stub_manifest(monkeypatch, down)
    mgr.poll_once()
    assert mgr.published == {}

    _stub_manifest(monkeypatch, MANIFEST_OK)
    mgr.poll_once()
    assert list(mgr.published) == ["host.comfy.generate"]


def test_three_consecutive_poll_errors_unpublish(bridge_env, monkeypatch):
    _write_active(node_config.ACTIVE_PATH, BRIDGE_CFG_AUTO_ON)
    _stub_manifest(monkeypatch, MANIFEST_OK)
    mgr = bridge.get_manager()
    mgr.poll_once()
    assert list(mgr.published) == ["host.comfy.generate"]

    _stub_manifest(monkeypatch, httpx.ConnectError("bridge down"))
    mgr.poll_once()
    assert mgr.published != {}  # 1 error: keep publishing
    mgr.poll_once()
    assert mgr.published != {}  # 2 errors: keep publishing
    mgr.poll_once()
    assert mgr.published == {}  # 3 errors: unpublish everything

    _stub_manifest(monkeypatch, MANIFEST_OK)
    mgr.poll_once()
    assert list(mgr.published) == ["host.comfy.generate"]  # recovery → republish


def test_empty_manifest_unpublishes_all_host_caps(bridge_env, monkeypatch):
    _write_active(node_config.ACTIVE_PATH, BRIDGE_CFG_AUTO_ON)
    _stub_manifest(monkeypatch, MANIFEST_OK)
    mgr = bridge.get_manager()
    mgr.poll_once()
    assert list(mgr.published) == ["host.comfy.generate"]

    _stub_manifest(monkeypatch, {"version": 1, "capabilities": []})
    mgr.poll_once()
    assert mgr.published == {}


def test_manifest_major_version_mismatch_ignored(bridge_env, monkeypatch, caplog):
    _write_active(node_config.ACTIVE_PATH, BRIDGE_CFG_AUTO_ON)
    _stub_manifest(monkeypatch, MANIFEST_OK)
    mgr = bridge.get_manager()
    mgr.poll_once()
    assert list(mgr.published) == ["host.comfy.generate"]

    _stub_manifest(monkeypatch, {"version": 2, "capabilities": []})
    with caplog.at_level(logging.WARNING, logger="bridge"):
        mgr.poll_once()

    # ignored entirely — current published state is kept
    assert list(mgr.published) == ["host.comfy.generate"]
    assert any("version" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Exec forward (SPEC §5)
# ---------------------------------------------------------------------------


@pytest.fixture()
def exec_env(bridge_env, monkeypatch):
    _write_active(node_config.ACTIVE_PATH, BRIDGE_CFG_DEFAULT)
    fake_client = MagicMock()
    fake_client.upload_artifact.return_value = {"artifact_id": "a1"}
    return fake_client


def _stage() -> dict:
    return {"stage_id": "s1", "task_id": "t1", "payload": {"prompt": "cat"}}


def _bridge_cap() -> dict:
    return bridge.build_bridge_cap("comfy.generate", MANIFEST_OK["capabilities"][0])


def _stub_exec_post(monkeypatch, responder):
    posts: list[dict] = []

    def fake_post(url, *, headers=None, json=None, timeout=None):
        posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        resp = responder(posts[-1])
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(bridge.httpx, "post", fake_post)
    return posts


def test_exec_forward_completed_with_artifacts(exec_env, monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    out = ws / "runs" / "x"
    out.mkdir(parents=True)
    (out / "output.png").write_bytes(b"png")
    _write_active(
        node_config.ACTIVE_PATH,
        BRIDGE_CFG_DEFAULT.replace("workspace: /workspace", f"workspace: {ws}"),
    )

    def responder(post):
        assert post["url"] == "http://bridge.test:7780/v1/exec/comfy.generate"
        assert post["headers"]["Authorization"] == "Bearer bridge-tok"
        assert post["json"] == {
            "task_id": "t1",
            "params": {"prompt": "cat"},
            "timeout_s": bridge.DEFAULT_BRIDGE_TIMEOUT,
        }
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "result": {"image": "ok"},
                "artifacts": [{"name": "output.png", "path": "runs/x/output.png"}],
            },
            request=httpx.Request("POST", post["url"]),
        )

    posts = _stub_exec_post(monkeypatch, responder)
    result = bridge.execute_bridge_task(exec_env, _bridge_cap(), _stage())

    assert result["status"] == "completed"
    assert result["result"]["image"] == "ok"
    assert result["result"]["artifacts"] == [{"artifact_id": "a1"}]
    exec_env.upload_artifact.assert_called_once()
    args, kwargs = exec_env.upload_artifact.call_args
    assert args[0] == ws / "runs" / "x" / "output.png"
    assert kwargs["task_id"] == "t1"
    assert kwargs["stage_id"] == "s1"
    assert posts


def test_exec_forward_terminal_errors_complete_with_error(exec_env, monkeypatch):
    # 200 failed + retryable=false (invalid_params, capability.failed) → terminal
    for code, retryable in (("invalid_params", False), ("capability.failed", False)):

        def responder(post, code=code, retryable=retryable):
            return httpx.Response(
                200,
                json={
                    "status": "failed",
                    "error": {"code": code, "message": "boom", "retryable": retryable},
                },
                request=httpx.Request("POST", post["url"]),
            )

        _stub_exec_post(monkeypatch, responder)
        result = bridge.execute_bridge_task(exec_env, _bridge_cap(), _stage())
        assert result["status"] == "error", code
        assert code in str(result["error"]), code

    # 404 unknown cap at the provider → terminal
    _stub_exec_post(
        monkeypatch,
        lambda post: httpx.Response(
            404, json={"detail": "unknown"}, request=httpx.Request("POST", post["url"])
        ),
    )
    result = bridge.execute_bridge_task(exec_env, _bridge_cap(), _stage())
    assert result["status"] == "error"

    # 401 auth → terminal (token misconfiguration, retrying cannot help)
    _stub_exec_post(
        monkeypatch,
        lambda post: httpx.Response(
            401, json={"detail": "unauthorized"}, request=httpx.Request("POST", post["url"])
        ),
    )
    result = bridge.execute_bridge_task(exec_env, _bridge_cap(), _stage())
    assert result["status"] == "error"


def test_exec_forward_retryable_errors_raise(exec_env, monkeypatch):
    # 200 failed + retryable=true → retryable
    def failed_retryable(post):
        return httpx.Response(
            200,
            json={
                "status": "failed",
                "error": {"code": "provider.unavailable", "message": "busy", "retryable": True},
            },
            request=httpx.Request("POST", post["url"]),
        )

    _stub_exec_post(monkeypatch, failed_retryable)
    with pytest.raises(bridge.BridgeRetryableError):
        bridge.execute_bridge_task(exec_env, _bridge_cap(), _stage())

    # 503 provider.busy → retryable
    _stub_exec_post(
        monkeypatch,
        lambda post: httpx.Response(
            503, json={"detail": "provider.busy"}, request=httpx.Request("POST", post["url"])
        ),
    )
    with pytest.raises(bridge.BridgeRetryableError):
        bridge.execute_bridge_task(exec_env, _bridge_cap(), _stage())

    # other 5xx → retryable
    _stub_exec_post(
        monkeypatch,
        lambda post: httpx.Response(
            500, json={"detail": "boom"}, request=httpx.Request("POST", post["url"])
        ),
    )
    with pytest.raises(bridge.BridgeRetryableError):
        bridge.execute_bridge_task(exec_env, _bridge_cap(), _stage())

    # transport error → retryable (claim expires, relay requeues)
    _stub_exec_post(monkeypatch, lambda post: httpx.ConnectError("refused"))
    with pytest.raises(bridge.BridgeRetryableError):
        bridge.execute_bridge_task(exec_env, _bridge_cap(), _stage())


def test_exec_forward_missing_bridge_config_is_terminal_error(exec_env):
    # the exec_env fixture writes a bridge config — remove it again
    node_config.ACTIVE_PATH.unlink()
    result = bridge.execute_bridge_task(exec_env, _bridge_cap(), _stage())
    assert result["status"] == "error"


def test_exec_forward_artifact_outside_workspace_rejected(exec_env, monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")

    _write_active(
        node_config.ACTIVE_PATH,
        BRIDGE_CFG_DEFAULT.replace("workspace: /workspace", f"workspace: {ws}"),
    )

    def responder(post):
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "result": {},
                "artifacts": [{"name": "secret.txt", "path": "../secret.txt"}],
            },
            request=httpx.Request("POST", post["url"]),
        )

    _stub_exec_post(monkeypatch, responder)
    result = bridge.execute_bridge_task(exec_env, _bridge_cap(), _stage())

    assert result["status"] == "completed"
    assert result["result"].get("artifacts") == []
    assert result["result"]["artifact_errors"]
    exec_env.upload_artifact.assert_not_called()


# ---------------------------------------------------------------------------
# Daemon integration: bridge caps route to the bridge, not to run_handler
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self):
        self.meta = {"node_id": "N1"}
        self.base_url = "http://relay.test:8788"
        self.completed: list[tuple] = []

    def complete(self, task_id, stage_id, result):
        self.completed.append((task_id, stage_id, result))
        return {}


def test_poll_daemon_run_stage_routes_bridge_cap(monkeypatch):
    client = _FakeClient()
    daemon = poll_daemon.Daemon(client, {"max_retries": 2})
    called = {}

    def fake_exec(cl, cap, stage):
        called["cap"] = cap
        return {"status": "completed", "result": {"ok": True}}

    monkeypatch.setattr(bridge, "execute_bridge_task", fake_exec)
    monkeypatch.setattr(
        poll_daemon,
        "run_handler",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("run_handler must not run for bridge caps")
        ),
    )

    daemon._run_stage(_bridge_cap(), _stage())

    assert called["cap"]["bridge"] is True
    assert client.completed == [("t1", "s1", {"status": "completed", "result": {"ok": True}})]


def test_sse_daemon_run_stage_routes_bridge_cap(monkeypatch):
    from nodes.common import node_daemon

    client = _FakeClient()
    daemon = node_daemon.SseDaemon(client, {"max_retries": 2})

    def fake_exec(cl, cap, stage):
        return {"status": "completed", "result": {"ok": True}}

    monkeypatch.setattr(bridge, "execute_bridge_task", fake_exec)
    monkeypatch.setattr(
        node_daemon,
        "run_handler",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("run_handler must not run for bridge caps")
        ),
    )

    daemon._run_stage(_bridge_cap(), _stage())
    assert client.completed == [("t1", "s1", {"status": "completed", "result": {"ok": True}})]


# ---------------------------------------------------------------------------
# Poller thread
# ---------------------------------------------------------------------------


def test_poller_thread_runs_and_stops(bridge_env, monkeypatch):
    _write_active(node_config.ACTIVE_PATH, BRIDGE_CFG_AUTO_ON)
    _stub_manifest(monkeypatch, MANIFEST_OK)
    stop = threading.Event()
    thread = bridge.start_poller(stop)
    # give the first poll a moment, then stop — the loop must exit promptly
    deadline = __import__("time").monotonic() + 5
    while not bridge.get_manager().published and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.05)
    assert bridge.get_manager().published
    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_poller_thread_without_config_polls_nothing(bridge_env):
    stop = threading.Event()
    thread = bridge.start_poller(stop)
    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert bridge.get_manager().published == {}
