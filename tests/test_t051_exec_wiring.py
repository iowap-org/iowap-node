"""T-051 (T-035 P3): exec wiring — input-artifact hydration + result-push binding.

Brief: ``references/t051-exec-wiring-brief.md`` (board iowap-windows-stack).
Two T-035 gaps become node-side wiring, both proven in this suite:

1. **Input hydration BEFORE exec** — artifact refs in the stage payload
   (T-179 envelope, ``__iowap_ref__`` with ``src: artifact``) download
   into ``<workspace>/inputs/<artifact_id>/<name>`` via the NODE creds
   and the payload fields rewrite to ``{"src": "path", "path": ...}`` —
   bridge exec (POST body) AND local-handler stages both see a path,
   never an id. Ownership is node-side (brief): the task submitter must
   own every referenced artifact — primary/legacy tasks (no binding)
   bypass, an unowned ref fails the stage at claim with the honest
   cause ``artifact '<id>' not available for this submitter`` (never
   not_found, never silent).
2. **Result-push → binding** — the three completion sites (poll
   ``_run_stage`` success, SSE ``_run_stage`` success, T-179
   script-complete) record the result's pushed artifact ids into the
   binding store for the bound submitter. The chained-files flow needs
   NO task_result fetch: t1 pushes → completion record → t2 references →
   hydration allows it; a foreign agent's identical reference refuses.

Store discipline: the binding-store fixture pins ``IOWAP_AGENTS_DIR``
and resets the T-050 probe-once cache (suite rule for every new pinned
path); binding writes are process-locked read-modify-write (budget.py
pattern, T-051) — atomic + last-writer-wins cross-process like
``budget_used``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nodes.common import (
    agent_tokens,
    artifact_hydration,
    bridge,
    envelope,
    node_config,
    node_daemon,
    poll_daemon,
)
from nodes.common.relay_client import RelayClient

META = {
    "node_id": "T051NODE",
    "node_name": "exec-wiring",
    "registration_secret": "rs_x",
    "base_url": "http://relay.test:8788",
}
CFG = {"base_url": None, "request_timeout": 5, "heartbeat_interval": 1}

BRIDGE_CFG_FMT = """
bridge:
  url: http://bridge.test:7780
  token: bridge-tok
  workspace: {ws}
"""


# ---------------------------------------------------------------------------
# Fixtures — store paths pinned ⇒ probe-once cache reset (T-050 suite rule)
# ---------------------------------------------------------------------------


@pytest.fixture()
def agents_env(tmp_path, monkeypatch):
    """Isolate the agent-token store behind IOWAP_AGENTS_DIR and reset the
    T-050 probe-once cache — a stale cache would freeze the first test's
    resolution for the rest of the suite."""
    agents_dir = tmp_path / "agents"
    monkeypatch.setenv(agent_tokens.AGENT_DIR_ENV, str(agents_dir))
    monkeypatch.setattr(agent_tokens, "_AGENTS_DIR_CACHE", None)
    yield agents_dir
    agent_tokens._reset_agents_dir_cache()


@pytest.fixture()
def node_env(tmp_path, monkeypatch):
    """RelayClient runtime isolation (the T-035/T-046 local-creds convention)."""
    relay_dir = tmp_path / "relay"
    relay_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(node_config, "BASE_DIR", relay_dir)
    monkeypatch.setattr(node_config, "ACTIVE_PATH", relay_dir / "node.yaml")
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("IOWAP_MCP_BEARER", raising=False)
    yield relay_dir


@pytest.fixture()
def bridge_env(node_env, monkeypatch):
    """The T-003 active-profile isolation + a bridge block pointing at a
    temp workspace (hydration must NEVER land under a real /workspace)."""
    active = node_env / "node.yaml"
    monkeypatch.setattr(node_config, "_active_cache", node_config.ActiveProfileCache(path=active))
    bridge.reset_manager()
    yield node_env
    bridge.reset_manager()


def _write_active(text: str) -> None:
    node_config.ACTIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    node_config.ACTIVE_PATH.write_text(text, encoding="utf-8")


def _workspace(node_env: Path, name: str = "ws") -> Path:
    ws = node_env.parent / name
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _seed_runtime(relay_dir: Path, monkeypatch) -> RelayClient:
    """Seed the meta/token/config files so RelayClient never walks into
    live recovery, then return a client with stubbed relays (T-046 shape)."""
    from nodes.common import onboarding

    for name in (
        "META_PATH", "TOKEN_PATH", "CONFIG_PATH",
        "LEGACY_META_PATH", "LEGACY_TOKEN_PATH", "STATUS_PATH",
    ):
        monkeypatch.setattr(onboarding.node_utils, name, relay_dir / f"{name.lower()}.json")
    onboarding.node_utils.META_PATH.write_text(json.dumps(dict(META)))
    onboarding.node_utils.TOKEN_PATH.write_text(json.dumps({"token": "tok-1"}))
    onboarding.node_utils.CONFIG_PATH.write_text(json.dumps({"base_url": META["base_url"]}))
    return RelayClient(dict(META), dict(CFG))


class CapturingRelay:
    """RelayClient surface for the hydration paths — records download/upload
    calls so a test can prove they never happened."""

    def __init__(self, files: dict[str, bytes] | None = None):
        self.meta: dict = dict(META)
        self.base_url = META["base_url"]
        self.files = dict(files or {})
        self.downloads: list[tuple[str, Path]] = []
        self.uploads: list[dict] = []
        self.completions: list = []

    def download_artifact(self, artifact_id, output_path=None, **_kw):
        data = self.files.get(artifact_id)
        if data is None:
            raise RuntimeError(f"HTTP 404 artifact '{artifact_id}' missing at the relay")
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.downloads.append((artifact_id, target))
        return target

    def upload_artifact(self, path, name=None, task_id=None, stage_id=None, **_kw):
        artifact_id = f"art-{len(self.uploads) + 1}"
        entry = {
            "artifact_id": artifact_id,
            "name": name or Path(path).name,
            "task_id": task_id,
            "stage_id": stage_id,
        }
        self.uploads.append(entry)
        return dict(entry)

    def complete(self, task_id=None, stage_id=None, result=None, **_kw):
        self.completions.append(result)
        return {"status": "ok"}


def _stub_exec_post(monkeypatch, responder):
    """The T-003 exec-POST stub — captures every bridge exec call."""
    posts: list[dict] = []

    def fake_post(url, *, headers=None, json=None, timeout=None):
        posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        resp = responder(posts[-1])
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(bridge.httpx, "post", fake_post)
    return posts


def _bridge_cap() -> dict:
    return {
        "name": "host.capsule",
        "bridge": True,
        "bridge_name": "capsule",
        "cost": 1,
        "timeout": 60,
    }


def _art_ref(artifact_id: str, filename: str) -> dict:
    """A T-179 artifact envelope (the sender-side shape)."""
    return envelope.make_envelope(
        src="artifact", filename=filename, artifact_id=artifact_id,
    )


def _completed_result(extra: dict | None = None) -> httpx.Response:
    body = {"status": "completed", "result": {}}
    if extra:
        body.update(extra)
    return body


def _no_call(*_a, **_k):
    raise AssertionError("must never run")


# ---------------------------------------------------------------------------
# Hydration: bridge exec body rewrite (happy / refusal / bypass / noop)
# ---------------------------------------------------------------------------


def test_bridge_hydration_happy_agent_owned(bridge_env, agents_env, monkeypatch, tmp_path):
    """Agent-owned ref → bytes land in the workspace, the POST body carries
    the rewritten path (never the id), the exec goes through."""
    ws = _workspace(tmp_path)
    _write_active(BRIDGE_CFG_FMT.format(ws=ws))
    agent_id, _token = agent_tokens.create("writer")
    agent_tokens.record_task(agent_id, "t1")
    # the submitter has SEEN the referenced artifact (completion record)
    agent_tokens.record_artifacts(agent_id, "t0", ["art-src"])

    relay = CapturingRelay(files={"art-src": b"hello"})
    stage = {
        "stage_id": "s1",
        "task_id": "t1",
        "payload": {"file": _art_ref("art-src", "input.txt"), "mode": "fast"},
    }

    def responder(post):
        assert post["url"] == "http://bridge.test:7780/v1/exec/capsule"
        # the exec contract carries the PATH convention, not the envelope
        assert post["json"]["params"] == {
            "file": {"src": "path", "path": "inputs/art-src/input.txt"},
            "mode": "fast",
        }
        return httpx.Response(
            200, json=_completed_result(), request=httpx.Request("POST", post["url"])
        )

    posts = _stub_exec_post(monkeypatch, responder)
    result = bridge.execute_bridge_task(relay, _bridge_cap(), stage)

    assert result["status"] == "completed"
    assert (ws / "inputs" / "art-src" / "input.txt").read_bytes() == b"hello"
    assert relay.downloads == [("art-src", ws / "inputs" / "art-src" / "input.txt")]
    assert posts, "the exec POST happened"


def test_bridge_hydration_foreign_ref_refused(bridge_env, agents_env, monkeypatch, tmp_path):
    """A foreign agent referencing an artifact it never saw → the stage
    FAILS AT CLAIM with the honest cause (no exec, no download, no lie)."""
    ws = _workspace(tmp_path)
    _write_active(BRIDGE_CFG_FMT.format(ws=ws))
    submitter, _ = agent_tokens.create("other")
    agent_tokens.record_task(submitter, "t2")  # submitter, but never saw art-src

    relay = CapturingRelay()
    monkeypatch.setattr(relay, "download_artifact", _no_call)
    stage = {
        "stage_id": "s1", "task_id": "t2",
        "payload": {"file": _art_ref("art-src", "in.txt")},
    }

    posts = _stub_exec_post(monkeypatch, lambda post: _no_call(post))
    result = bridge.execute_bridge_task(relay, _bridge_cap(), stage)

    assert result["status"] == "error"
    assert "not available for this submitter" in result["error"]
    assert "not found" not in result["error"]
    assert not posts
    assert relay.downloads == []
    assert agent_tokens.submitting_agent("t2") == submitter


def test_bridge_hydration_primary_bypass(bridge_env, agents_env, monkeypatch, tmp_path):
    """Primary/legacy task (no binding ever written) → full access: the ref
    hydrates exactly like the agent-owned happy case."""
    ws = _workspace(tmp_path)
    _write_active(BRIDGE_CFG_FMT.format(ws=ws))
    relay = CapturingRelay(files={"art-op": b"op"})
    stage = {
        "stage_id": "s1", "task_id": "t-primary",
        "payload": {"file": _art_ref("art-op", "in.bin")},
    }

    posts = _stub_exec_post(
        monkeypatch,
        lambda post: httpx.Response(
            200, json=_completed_result(), request=httpx.Request("POST", post["url"])
        ),
    )
    result = bridge.execute_bridge_task(relay, _bridge_cap(), stage)

    assert result["status"] == "completed"
    assert (ws / "inputs" / "art-op" / "in.bin").read_bytes() == b"op"
    assert posts[0]["json"]["params"]["file"] == {"src": "path", "path": "inputs/art-op/in.bin"}


def test_bridge_hydration_no_ref_noop(bridge_env, agents_env, monkeypatch, tmp_path):
    """No artifact refs in params → no download call, no rewrite, the body
    params pass through byte-identically."""
    ws = _workspace(tmp_path)
    _write_active(BRIDGE_CFG_FMT.format(ws=ws))
    relay = CapturingRelay()
    monkeypatch.setattr(relay, "download_artifact", _no_call)
    stage = {"stage_id": "s1", "task_id": "t1", "payload": {"prompt": "cat"}}

    posts = _stub_exec_post(
        monkeypatch,
        lambda post: httpx.Response(
            200, json=_completed_result(), request=httpx.Request("POST", post["url"])
        ),
    )
    result = bridge.execute_bridge_task(relay, _bridge_cap(), stage)

    assert result["status"] == "completed"
    assert posts[0]["json"]["params"] == {"prompt": "cat"}


def test_bridge_hydration_malformed_ref_terminal(bridge_env, agents_env, monkeypatch, tmp_path):
    """A malformed/unsupported envelope breaks the exec loudly (T-179
    rule) — never a silent passthrough into the provider."""
    ws = _workspace(tmp_path)
    _write_active(BRIDGE_CFG_FMT.format(ws=ws))
    relay = CapturingRelay()
    monkeypatch.setattr(relay, "download_artifact", _no_call)
    stage = {
        "stage_id": "s1",
        "task_id": "t1",
        "payload": {"file": {"__iowap_ref__": {"v": 99, "src": "artifact", "artifact_id": "x"}}},
    }
    posts = _stub_exec_post(monkeypatch, lambda post: _no_call(post))
    result = bridge.execute_bridge_task(relay, _bridge_cap(), stage)

    assert result["status"] == "error"
    assert "artifact ref invalid" in result["error"]
    assert not posts


def test_bridge_hydration_download_failure_terminal(bridge_env, agents_env, monkeypatch, tmp_path):
    """A real download failure (artifact gone at the relay) fails the stage
    with the exact cause — fail-fast at claim, not half-hydrated."""
    ws = _workspace(tmp_path)
    _write_active(BRIDGE_CFG_FMT.format(ws=ws))
    agent_id, _ = agent_tokens.create("owner")
    agent_tokens.record_task(agent_id, "t1")
    agent_tokens.record_artifacts(agent_id, "t0", ["art-gone"])
    relay = CapturingRelay()  # no such file → the fake raises
    stage = {
        "stage_id": "s1", "task_id": "t1",
        "payload": {"file": _art_ref("art-gone", "in.txt")},
    }
    posts = _stub_exec_post(monkeypatch, lambda post: _no_call(post))
    result = bridge.execute_bridge_task(relay, _bridge_cap(), stage)

    assert result["status"] == "error"
    assert "artifact 'art-gone' download failed" in result["error"]
    assert not posts


# ---------------------------------------------------------------------------
# Hydration module, unit level (workspace resolution, filename sanitizing)
# ---------------------------------------------------------------------------


def test_resolve_workspace_reads_bridge_cfg(bridge_env):
    _write_active(BRIDGE_CFG_FMT.format(ws="/custom/ws"))
    assert artifact_hydration.resolve_workspace() == "/custom/ws"


def test_resolve_workspace_defaults_without_bridge_block(node_env):
    # no active file, no bridge block → the bridge loader's default
    assert artifact_hydration.resolve_workspace() == "/workspace"


def test_safe_artifact_name_strips_traversal():
    assert "/" not in artifact_hydration._safe_artifact_name("a/b/c.txt", "art-1")
    assert artifact_hydration._safe_artifact_name("..", "art-1") == "art-1"
    assert artifact_hydration._safe_artifact_name("out.bin", "art-1") == "out.bin"


# ---------------------------------------------------------------------------
# Completion sites: result-push → binding (poll + SSE + script-complete)
# ---------------------------------------------------------------------------


def test_poll_daemon_completion_records_pushed_artifacts(bridge_env, agents_env, monkeypatch, tmp_path):
    """Bridge exec pushes artifacts → the completion record binds them to
    the submitter — WITHOUT any task_result fetch (the P3 win)."""
    ws = _workspace(tmp_path)
    _write_active(BRIDGE_CFG_FMT.format(ws=ws))
    _seed_runtime(node_config.BASE_DIR, monkeypatch)

    relay = CapturingRelay()
    agent_id, _ = agent_tokens.create("producer")
    agent_tokens.record_task(agent_id, "t1")

    # the workspace file the provider declares as an artifact
    (ws / "out.bin").write_bytes(b"payload")
    stage = {"stage_id": "s1", "task_id": "t1", "payload": {"p": 1}}
    _stub_exec_post(
        monkeypatch,
        lambda post: httpx.Response(
            200,
            json=_completed_result(
                {"artifacts": [{"name": "out.bin", "path": "out.bin"}]}
            ),
            request=httpx.Request("POST", post["url"]),
        ),
    )
    daemon = poll_daemon.Daemon(relay, dict(CFG))
    daemon._run_stage(_bridge_cap(), stage)

    assert daemon.tasks_completed == 1
    assert len(relay.uploads) == 1, "the pushed file uploaded"
    # the completion record: the submitter now OWNS art-1 — no
    # task_result fetch ever happened
    assert agent_tokens.can_read_artifact(agent_id, "art-1")
    entry = agent_tokens.load_bindings()[agent_id]
    assert entry["seen_artifacts"]["art-1"] == ["t1"]


def test_poll_daemon_completion_primary_no_record(bridge_env, agents_env, monkeypatch, tmp_path):
    """Primary/legacy task pushes artifacts → the binding store stays
    untouched (the primary path writes no binding ever, T-035 rule)."""
    ws = _workspace(tmp_path)
    _write_active(BRIDGE_CFG_FMT.format(ws=ws))
    _seed_runtime(node_config.BASE_DIR, monkeypatch)

    relay = CapturingRelay()
    (ws / "o").write_bytes(b"x")
    stage = {"stage_id": "s1", "task_id": "t-primary", "payload": {}}
    _stub_exec_post(
        monkeypatch,
        lambda post: httpx.Response(
            200,
            json=_completed_result({"artifacts": [{"name": "o", "path": "o"}]}),
            request=httpx.Request("POST", post["url"]),
        ),
    )
    daemon = poll_daemon.Daemon(relay, dict(CFG))
    daemon._run_stage(_bridge_cap(), stage)

    assert daemon.tasks_completed == 1
    assert agent_tokens.load_bindings() == {}, "no binding for the primary path"
    assert agent_tokens.submitting_agent("t-primary") is None


def test_poll_daemon_completion_error_envelope_no_record(bridge_env, agents_env, monkeypatch, tmp_path):
    """A stage completing with an error value carries no artifact ownership —
    the record lives on the success path only (T-046 meter discipline)."""
    ws = _workspace(tmp_path)
    _write_active(BRIDGE_CFG_FMT.format(ws=ws))
    _seed_runtime(node_config.BASE_DIR, monkeypatch)

    agent_id, _ = agent_tokens.create("worker")
    agent_tokens.record_task(agent_id, "t-fail")
    relay = CapturingRelay()
    monkeypatch.setattr(poll_daemon, "run_handler", lambda *a, **k: {
        "status": "completed",
        "result": {"artifacts": [{"artifact_id": "art-x"}]},
        "error": "boom",
    })
    cap = {"name": "local.risky", "handler": "h", "cost": 0}
    daemon = poll_daemon.Daemon(relay, dict(CFG))
    daemon._run_stage(dict(cap), {"stage_id": "s1", "task_id": "t-fail", "payload": {}})

    assert daemon.tasks_failed == 1
    bindings = agent_tokens.load_bindings()
    assert bindings[agent_id]["seen_artifacts"] == {}, "no record for an error result"
    assert agent_tokens.tasks_for(agent_id) == ["t-fail"], "the submit binding stays"


def test_sse_daemon_completion_records_pushed_artifacts(bridge_env, agents_env, monkeypatch, tmp_path):
    """The SSE daemon's complete path records too — same T-051 contract."""
    ws = _workspace(tmp_path)
    _write_active(BRIDGE_CFG_FMT.format(ws=ws))
    _seed_runtime(node_config.BASE_DIR, monkeypatch)

    relay = CapturingRelay()
    (ws / "o").write_bytes(b"sse")
    agent_id, _ = agent_tokens.create("sse-agent")
    agent_tokens.record_task(agent_id, "t-sse")
    _stub_exec_post(
        monkeypatch,
        lambda post: httpx.Response(
            200,
            json=_completed_result({"artifacts": [{"name": "o", "path": "o"}]}),
            request=httpx.Request("POST", post["url"]),
        ),
    )
    daemon = node_daemon.SseDaemon(relay, dict(CFG))
    daemon._run_stage(_bridge_cap(), {"stage_id": "s1", "task_id": "t-sse", "payload": {}})

    assert daemon.tasks_completed == 1
    assert agent_tokens.can_read_artifact(agent_id, "art-1")
    assert agent_tokens.load_bindings()[agent_id]["seen_artifacts"]["art-1"] == ["t-sse"]


def test_sse_daemon_script_complete_records_too(node_env, agents_env, monkeypatch):
    """T-179 script-complete IS a completed task — its result's artifact
    declarations bind as well (the third record site, next to T-046 meter)."""
    def complete_rejected_404(*_a, **_k):
        raise httpx.HTTPStatusError(
            "404 stage not claimed",
            request=httpx.Request("POST", "http://relay.test/stages/s1/complete"),
            response=httpx.Response(
                404,
                json={"detail": "stage not claimed by this node, or not in"
                                " claimed status"},
            ),
        )

    _seed_runtime(node_config.BASE_DIR, monkeypatch)
    agent_id, _ = agent_tokens.create("scripter")
    agent_tokens.record_task(agent_id, "t-script")
    monkeypatch.setattr(
        node_daemon, "run_handler",
        lambda *a, **k: {
            "status": "completed",
            "result": {"out": {"artifact_id": "art-script"}},
            "error": None,
        },
    )
    relay = CapturingRelay()
    relay.complete = complete_rejected_404
    daemon = node_daemon.SseDaemon(relay, dict(CFG))
    cap = {
        "name": "local.auto", "handler": "h", "cost": 0,
        "config": {"complete_by_script": True},
    }
    daemon._run_stage(dict(cap), {"stage_id": "s1", "task_id": "t-script"})

    assert daemon.tasks_completed == 1, "script-complete counts as done (T-179)"
    assert agent_tokens.can_read_artifact(agent_id, "art-script")
    assert agent_tokens.load_bindings()[agent_id]["seen_artifacts"]["art-script"] == ["t-script"]


# ---------------------------------------------------------------------------
# Local-handler hydration (both daemons)
# ---------------------------------------------------------------------------


def test_local_handler_hydration_happy_and_refused(bridge_env, agents_env, monkeypatch, tmp_path):
    """Local handler stage with an agent-owned ref: the payload rewrites to
    the workspace path and the handler runs; with a foreign ref the stage
    fails honestly and the handler never runs."""
    ws = _workspace(tmp_path)
    _write_active(BRIDGE_CFG_FMT.format(ws=ws))
    owner, _ = agent_tokens.create("owner")
    agent_tokens.record_task(owner, "t-l1")
    agent_tokens.record_artifacts(owner, "t0", ["art-local"])
    foreign, _ = agent_tokens.create("foreign")
    agent_tokens.record_task(foreign, "t-l2")

    _seed_runtime(node_config.BASE_DIR, monkeypatch)
    relay = CapturingRelay(files={"art-local": b"local"})
    seen_payloads: list[dict] = []

    def fake_run_handler(handler, stage, context=None, timeout=None):
        seen_payloads.append(stage["payload"])
        return {"status": "completed", "result": {"ran": True}, "error": None}

    monkeypatch.setattr(poll_daemon, "run_handler", fake_run_handler)
    daemon = poll_daemon.Daemon(relay, dict(CFG))
    cap = {"name": "local.ink", "handler": "h", "cost": 0}

    daemon._run_stage(
        dict(cap),
        {"stage_id": "s1", "task_id": "t-l1", "payload": {"file": _art_ref("art-local", "in.txt")}},
    )
    assert daemon.tasks_completed == 1
    assert seen_payloads[0]["file"] == {"src": "path", "path": "inputs/art-local/in.txt"}
    assert (ws / "inputs" / "art-local" / "in.txt").read_bytes() == b"local"

    # the foreign variant: same ref, NOT owned → the stage fails at claim,
    # the handler never runs, the completion envelope carries the cause
    daemon._run_stage(
        dict(cap),
        {"stage_id": "s2", "task_id": "t-l2", "payload": {"file": _art_ref("art-local", "in.txt")}},
    )
    assert daemon.tasks_completed == 1, "the refusal did not complete the stage"
    assert daemon.tasks_failed == 1
    assert relay.completions[-1] == {
        "status": "error",
        "error": "artifact 'art-local' not available for this submitter",
    }
    assert len(seen_payloads) == 1, "the handler never ran for the refusal"


def test_sse_daemon_local_hydration_primary_bypass(bridge_env, agents_env, monkeypatch, tmp_path):
    """The SSE daemon's local-handler path hydrates too — a primary task
    with a ref passes through to the handler as a workspace path."""
    ws = _workspace(tmp_path)
    _write_active(BRIDGE_CFG_FMT.format(ws=ws))
    _seed_runtime(node_config.BASE_DIR, monkeypatch)

    relay = CapturingRelay(files={"art-sse": b"sse-l"})
    seen: list[dict] = []
    monkeypatch.setattr(
        node_daemon, "run_handler",
        lambda handler, stage, context=None, timeout=None: (
            seen.append(stage["payload"]),
            {"status": "completed", "result": {}, "error": None},
        )[1],
    )
    daemon = node_daemon.SseDaemon(relay, dict(CFG))
    cap = {"name": "local.ink", "handler": "h", "cost": 0}
    daemon._run_stage(
        dict(cap),
        {"stage_id": "s1", "task_id": "t-p", "payload": {"file": _art_ref("art-sse", "in.txt")}},
    )
    assert daemon.tasks_completed == 1
    assert seen[0]["file"] == {"src": "path", "path": "inputs/art-sse/in.txt"}


# ---------------------------------------------------------------------------
# Chained artifact flows (the P3 value, brief §2)
# ---------------------------------------------------------------------------


def test_chained_flow_same_agent_ref_hydrates(bridge_env, agents_env, monkeypatch, tmp_path):
    """Submit A(t1) → exec pushes artifacts → completion record →
    A(t2, ref of t1's artifact) → hydration OK + exec runs (no
    task_result anywhere in between)."""
    ws = _workspace(tmp_path)
    _write_active(BRIDGE_CFG_FMT.format(ws=ws))
    _seed_runtime(node_config.BASE_DIR, monkeypatch)

    relay = CapturingRelay(files={"art-1": b"chain"})
    agent_id, _ = agent_tokens.create("chainer")
    agent_tokens.record_task(agent_id, "t1")

    (ws / "out.bin").write_bytes(b"chain-out")
    push_stage = {"stage_id": "s1", "task_id": "t1", "payload": {}}
    _stub_exec_post(
        monkeypatch,
        lambda post: httpx.Response(
            200,
            json=_completed_result(
                {"artifacts": [{"name": "out.bin", "path": "out.bin"}]}
            ),
            request=httpx.Request("POST", post["url"]),
        ),
    )
    daemon = poll_daemon.Daemon(relay, dict(CFG))
    daemon._run_stage(_bridge_cap(), push_stage)

    # the completion record bound the pushed artifact
    assert agent_tokens.can_read_artifact(agent_id, "art-1")

    # task2 references t1's artifact — owned → hydrates + execs
    agent_tokens.record_task(agent_id, "t2")
    posts = _stub_exec_post(
        monkeypatch,
        lambda post: httpx.Response(
            200,
            json=_completed_result({"result": {"saw": post["json"]["params"]}}),
            request=httpx.Request("POST", post["url"]),
        ),
    )
    ref_stage = {
        "stage_id": "s2", "task_id": "t2",
        "payload": {"file": _art_ref("art-1", "out.bin")},
    }
    result = bridge.execute_bridge_task(relay, _bridge_cap(), ref_stage)

    assert result["status"] == "completed"
    assert posts[0]["json"]["params"]["file"] == {"src": "path", "path": "inputs/art-1/out.bin"}
    assert (ws / "inputs" / "art-1" / "out.bin").read_bytes() == b"chain"
    # the binding store shows the chain: same agent, two tasks
    entry = agent_tokens.load_bindings()[agent_id]
    assert entry["task_ids"] == ["t1", "t2"]
    assert entry["seen_artifacts"]["art-1"] == ["t1"]


def test_chained_flow_foreign_agent_refused(bridge_env, agents_env, monkeypatch, tmp_path):
    """t1 pushed by agent A; agent2 references t1's artifact → REFUSED
    with the honest cause (ownership leaks nothing, T-035 matrix)."""
    ws = _workspace(tmp_path)
    _write_active(BRIDGE_CFG_FMT.format(ws=ws))
    _seed_runtime(node_config.BASE_DIR, monkeypatch)

    owner, _ = agent_tokens.create("owner")
    agent_tokens.record_task(owner, "t1")
    agent_tokens.record_artifacts(owner, "t1", ["art-x"])

    other, _ = agent_tokens.create("other")
    agent_tokens.record_task(other, "t2")
    relay = CapturingRelay()
    monkeypatch.setattr(relay, "download_artifact", _no_call)
    posts = _stub_exec_post(monkeypatch, lambda post: _no_call(post))
    result = bridge.execute_bridge_task(
        relay,
        _bridge_cap(),
        {"stage_id": "s2", "task_id": "t2", "payload": {"file": _art_ref("art-x", "f.bin")}},
    )

    assert result["status"] == "error"
    assert "not available for this submitter" in result["error"]
    assert not posts
    assert relay.downloads == []


# ---------------------------------------------------------------------------
# Store helpers (submitting_agent lookup, T-051 lock contract)
# ---------------------------------------------------------------------------


def test_submitting_agent_lookup(agents_env):
    assert agent_tokens.submitting_agent("") is None
    assert agent_tokens.submitting_agent("unknown") is None
    aid, _ = agent_tokens.create("a")
    agent_tokens.record_task(aid, "t9")
    assert agent_tokens.submitting_agent("t9") == aid
    # record_task is never written for primary names — the operator path
    assert agent_tokens.record_task("primary", "t-p") is False
    assert agent_tokens.submitting_agent("t-p") is None


def test_binding_records_never_corrupt_the_file(agents_env):
    """The tmp+os.replace discipline keeps the store valid under the
    process-locked read-modify-write (no partial JSON, no tmp leftovers)."""
    aid, _ = agent_tokens.create("writer")
    agent_tokens.record_task(aid, "t-a")
    agent_tokens.record_artifacts(aid, "t-a", ["one", "two"])
    agent_tokens.record_artifacts(aid, "t-a", ["two"])  # idempotent append
    path = agents_env / "bindings.json"
    data = json.loads(path.read_text())
    assert data["bindings"][aid]["seen_artifacts"] == {"one": ["t-a"], "two": ["t-a"]}
    assert path.with_suffix(path.suffix + ".tmp").exists() is False
