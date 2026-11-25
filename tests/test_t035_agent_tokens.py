"""T-035: multi-bearer agent tokens — node-side store + dashboard (P1a/P1c).

Contracts proven:

* Store API (:mod:`nodes.common.agent_tokens`): create (show-once raw token,
  hash-only at rest), verify (unknown/revoked/malformed → None), revoke,
  caps-manage toggle, touch, record_task/record_artifacts/tasks_for,
  can_read_task/can_read_artifact with the primary sentinel (full access —
  the IOWAP_MCP_BEARER bearer is never stored, legacy tasks stay readable);
* store resolution: ``IOWAP_AGENTS_DIR`` env override > dedicated Volume dir
  ``/data/agents`` (simulated) > ``<relay-dir>/agents`` fallback; writes are
  atomic (tmp + os.replace, no .tmp leftovers);
* fail-closed: corrupt/invalid agents.json degrades to primary-only
  (verify → None, load → [], never a crash, read path never rewrites);
* dashboard (7783): the Agent-Tokens section lists agents, create renders
  the raw token EXACTLY ONCE (later renders show the store list only), the
  primary bearer never appears in the section, revoke stops verify()
  immediately, and the caps-manage toggle persists;
* the ``capabilities_*`` CLI surface (T-042) is untouched by this slice.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nodes.common import agent_tokens, node_config, onboarding  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store_env(tmp_path, monkeypatch):
    """Isolate the store: relay-dir fallback + no volume + no env override."""
    monkeypatch.setattr(node_config, "BASE_DIR", tmp_path / "relay")
    monkeypatch.delenv(agent_tokens.AGENT_DIR_ENV, raising=False)
    # simulate "no dedicated volume exists" on the test host
    monkeypatch.setattr(agent_tokens, "VOLUME_AGENT_DIR", tmp_path / "definitely-absent")
    return tmp_path


@pytest.fixture()
def dash_env(store_env, tmp_path, monkeypatch):
    """Everything the dashboard renders/reads (T-021 pattern, + agent store)."""
    for name in (
        "META_PATH", "TOKEN_PATH", "CONFIG_PATH",
        "LEGACY_META_PATH", "LEGACY_TOKEN_PATH", "STATUS_PATH",
    ):
        monkeypatch.setattr(onboarding.node_utils, name, tmp_path / f"{name.lower()}.json")
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("IOWAP_MCP_BEARER", raising=False)
    return tmp_path


def _configure(dash_env):
    """Make the node look configured (meta + token + relay config)."""
    onboarding.node_utils.META_PATH.write_text(json.dumps(
        {"node_id": "N1", "node_name": "win-node", "base_url": "http://r:8788"}))
    onboarding.node_utils.TOKEN_PATH.write_text(json.dumps({"token": "tok-1"}))
    onboarding.node_utils.CONFIG_PATH.write_text(json.dumps({"base_url": "http://r:8788"}))


# ---------------------------------------------------------------------------
# Resolver order
# ---------------------------------------------------------------------------


def test_agents_dir_prefers_env_override(store_env, monkeypatch) -> None:
    monkeypatch.setenv(agent_tokens.AGENT_DIR_ENV, str(store_env / "custom"))
    agent_tokens.create("env-agent")
    assert (store_env / "custom" / "agents.json").exists()
    assert agent_tokens.agents_dir() == store_env / "custom"


def test_agents_dir_uses_volume_when_present(store_env, monkeypatch) -> None:
    volume = store_env / "data" / "agents"
    volume.mkdir(parents=True)
    monkeypatch.setattr(agent_tokens, "VOLUME_AGENT_DIR", volume)
    agent_tokens.create("volume-agent")
    assert (volume / "agents.json").exists()
    assert agent_tokens.agents_dir() == volume


def test_agents_dir_falls_back_to_relay_dir(store_env, monkeypatch) -> None:
    assert agent_tokens.agents_dir() == store_env / "relay" / "agents"
    agent_tokens.create("fallback-agent")
    assert (store_env / "relay" / "agents" / "agents.json").exists()


# ---------------------------------------------------------------------------
# Store API
# ---------------------------------------------------------------------------


def test_create_returns_raw_once_and_stores_hash_only(store_env) -> None:
    agent_id, raw = agent_tokens.create("hermes")

    assert agent_id.startswith("agt-") and len(agent_id) == 12
    assert raw.startswith("iw_agt_") and len(raw) > 32
    agents = agent_tokens.load_agents()
    assert len(agents) == 1
    entry = agents[0]
    assert entry["id"] == agent_id
    assert entry["name"] == "hermes"
    assert entry["token_sha256"] == agent_tokens.hash_token(raw)
    assert raw not in agent_tokens.agents_path().read_text()  # hash-only at rest
    assert entry["created_at"]
    assert entry["last_used"] is None
    assert entry["caps_manage"] is False
    assert entry["revoked"] is False
    mode = agent_tokens.agents_path().stat().st_mode & 0o777
    assert mode == 0o600


def test_verify_resolves_only_the_matching_unrevoked_token(store_env) -> None:
    _, raw_a = agent_tokens.create("a")
    agent_id_b, raw_b = agent_tokens.create("b")

    entry = agent_tokens.verify(raw_b)
    assert entry is not None and entry["id"] == agent_id_b
    entry_a = agent_tokens.verify(raw_a)
    assert entry_a is not None
    assert entry_a["id"] != agent_id_b
    assert agent_tokens.verify("iw_agt_unknown_token") is None
    assert agent_tokens.verify("") is None
    assert agent_tokens.verify(None) is None  # type: ignore[arg-type]

    agent_tokens.revoke(agent_id_b)
    assert agent_tokens.verify(raw_b) is None  # revoked == invalid
    entry_a2 = agent_tokens.verify(raw_a)
    assert entry_a2 is not None  # others keep working


def test_hash_token_rejects_non_strings(store_env) -> None:
    with pytest.raises(ValueError):
        agent_tokens.hash_token("")
    assert agent_tokens.hash_token("x") != agent_tokens.hash_token("y")


def test_revoke_and_set_caps_manage(store_env) -> None:
    agent_id, raw = agent_tokens.create("ops-agent")

    assert agent_tokens.set_caps_manage(agent_id, True)["caps_manage"] is True
    assert agent_tokens.load_agents()[0]["caps_manage"] is True
    assert agent_tokens.set_caps_manage(agent_id, False)["caps_manage"] is False

    revoked = agent_tokens.revoke(agent_id)
    assert revoked["revoked"] is True
    assert revoked["id"] == agent_id
    assert agent_tokens.revoke("agt-nope") is None
    assert agent_tokens.set_caps_manage("agt-nope", True) is None


def test_touch_updates_last_used(store_env) -> None:
    agent_id, _ = agent_tokens.create("t")
    assert agent_tokens.load_agents()[0]["last_used"] is None
    assert agent_tokens.touch(agent_id) is True
    assert agent_tokens.load_agents()[0]["last_used"]
    assert agent_tokens.touch("agt-nope") is False


def test_binding_flow_record_task_and_read_scope(store_env) -> None:
    agent_id, raw = agent_tokens.create("scoped-agent")
    _, other_raw = agent_tokens.create("other-agent")
    other_entry = agent_tokens.verify(other_raw)
    other_id = other_entry["id"] if other_entry else "?"

    assert agent_tokens.record_task(agent_id, "task-A") is True
    assert agent_tokens.record_task(agent_id, "task-A") is True  # idempotent
    assert agent_tokens.record_task(agent_id, "task-B") is True
    assert agent_tokens.tasks_for(agent_id) == ["task-A", "task-B"]

    assert agent_tokens.can_read_task(agent_id, "task-A") is True
    assert agent_tokens.can_read_task(agent_id, "task-C") is False
    assert agent_tokens.can_read_task(other_id, "task-A") is False  # fremd
    # primary/legacy: full access, no binding ever written
    assert agent_tokens.can_read_task("primary", "task-C") is True
    assert agent_tokens.tasks_for("primary") == []
    assert agent_tokens.record_task("primary", "task-X") is False
    assert agent_tokens.record_task("", "task-X") is False


def test_binding_flow_record_artifacts_and_read_scope(store_env) -> None:
    agent_id, _ = agent_tokens.create("art-agent")

    assert agent_tokens.record_artifacts(agent_id, "t1", ["art-1", "art-2"]) is True
    assert agent_tokens.record_artifacts(agent_id, "t2", []) is False  # nothing seen
    assert agent_tokens.can_read_artifact(agent_id, "art-1") is True
    assert agent_tokens.can_read_artifact(agent_id, "art-9") is False
    assert agent_tokens.can_read_artifact("primary", "art-anything") is True
    # artifact-to-task map survives: art-1 seen via t1
    entry = agent_tokens.load_bindings()[agent_id]
    assert entry["seen_artifacts"]["art-1"] == ["t1"]


def test_atomic_writes_leave_no_tmp_files(store_env) -> None:
    agent_id, _ = agent_tokens.create("atomic")
    agent_tokens.set_caps_manage(agent_id, True)
    agent_tokens.record_task(agent_id, "task-1")
    d = store_env / "relay" / "agents"
    assert sorted(p.name for p in d.iterdir()) == ["agents.json", "bindings.json"]


def test_corrupt_agents_json_degrades_to_primary_only(store_env, caplog) -> None:
    d = store_env / "relay" / "agents"
    d.mkdir(parents=True)
    (d / "agents.json").write_text("{not json at all")

    assert agent_tokens.load_agents() == []
    assert agent_tokens.verify("iw_agt_whatever") is None  # no agent verifies
    # ...and a subsequent create REPAIRS the store instead of crashing
    agent_id, raw = agent_tokens.create("repaired")
    assert agent_tokens.verify(raw)["id"] == agent_id
    assert any("unreadable" in r.message.lower() for r in caplog.records)


def test_corrupt_bindings_json_degrades_to_empty(store_env) -> None:
    agent_id, _ = agent_tokens.create("b-agent")
    d = store_env / "relay" / "agents"
    (d / "bindings.json").write_text("[1, 2, 3")

    assert agent_tokens.load_bindings() == {}
    assert agent_tokens.can_read_task(agent_id, "any") is False  # fail-closed
    assert agent_tokens.can_read_task("primary", "any") is True
    # write path rebuilds the binding map
    assert agent_tokens.record_task(agent_id, "t9") is True


def test_store_schema_is_stable(store_env) -> None:
    """The JSON contracts shared with the MCP process must not drift."""
    agent_id, raw = agent_tokens.create("schema-check")
    agent_tokens.record_task(agent_id, "t1")
    agents_store = json.loads(agent_tokens.agents_path().read_text())
    bindings_store = json.loads(agent_tokens.bindings_path().read_text())

    assert set(agents_store) == {"version", "agents"}
    assert agents_store["version"] == 1
    assert set(agents_store["agents"][0]) == {
        "id", "name", "token_sha256", "created_at", "last_used",
        "caps_manage", "revoked",
    }
    assert set(bindings_store) == {"version", "bindings"}
    assert bindings_store["version"] == 1
    assert set(bindings_store["bindings"][agent_id]) == {"task_ids", "seen_artifacts"}
    assert bindings_store["bindings"][agent_id]["task_ids"] == ["t1"]


# ---------------------------------------------------------------------------
# Dashboard section (P1c)
# ---------------------------------------------------------------------------


def _get(server, path="/"):
    import urllib.request

    with urllib.request.urlopen(
        f"http://127.0.0.1:{server.server_address[1]}{path}", timeout=10
    ) as resp:
        return resp.status, resp.read().decode()


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
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


class FakeSupervisor:
    """Process-interface stand-in: records calls, never spawns (T-021)."""

    def __init__(self, running=False):
        self.running = running

    def is_running(self):
        return self.running


def _stub_render_side(monkeypatch):
    monkeypatch.setattr(onboarding.dashboard, "build_supervisor",
                        lambda: FakeSupervisor(running=False))
    monkeypatch.setattr(onboarding.dashboard, "fetch_forward_panel",
                        lambda *a, **k: {"ok": True, "forbidden": False, "error": None,
                                         "node_id": "N1", "slots": [], "mappings": {}})


def test_dashboard_agents_create_show_once_then_hidden(dash_env, monkeypatch) -> None:
    _configure(dash_env)
    _stub_render_side(monkeypatch)
    srv = onboarding.start_dashboard(port=0)
    try:
        status, page = _post(srv, {"name": "hermes-laptop"}, "/agents/create")
        assert status == 200
        assert "hermes-laptop" in page
        assert "iw_agt_" in page  # show-once render
        assert "shown only this once" in page.lower()
        import re as _re
        raw = [w for w in _re.split(r"[\s<>\"']", page) if w.startswith("iw_agt_")][0]

        # the store holds only the hash — and the NEXT render never shows raw
        status, page2 = _get(srv)
        assert status == 200
        assert "Agent-Tokens" in page2
        assert raw not in page2
        assert "hermes-laptop" in page2
        assert agent_tokens.verify(raw) is not None
    finally:
        onboarding.shutdown(srv)


def test_dashboard_agents_create_requires_name(dash_env, monkeypatch) -> None:
    _configure(dash_env)
    _stub_render_side(monkeypatch)
    srv = onboarding.start_dashboard(port=0)
    try:
        status, page = _post(srv, {}, "/agents/create")
        assert status == 400
        assert "agent name is required" in page
    finally:
        onboarding.shutdown(srv)


def test_dashboard_revoke_and_caps_toggle(dash_env, monkeypatch) -> None:
    _configure(dash_env)
    _stub_render_side(monkeypatch)
    agent_id, raw = agent_tokens.create("toggle-agent")
    srv = onboarding.start_dashboard(port=0)
    try:
        status, page = _post(srv, {"agent_id": agent_id, "enable": "1"}, "/agents/caps-toggle")
        assert status == 200
        assert "granted" in page
        assert agent_tokens.load_agents()[0]["caps_manage"] is True

        status, page = _post(srv, {"agent_id": agent_id}, "/agents/revoke")
        assert status == 200
        assert "revoked" in page
        assert agent_tokens.load_agents()[0]["revoked"] is True
        assert agent_tokens.verify(raw) is None  # enforcement cut-off

        status, page = _post(srv, {"agent_id": "agt-unknown0"}, "/agents/revoke")
        assert status == 400
        assert "unknown" in page
    finally:
        onboarding.shutdown(srv)


def test_dashboard_never_renders_the_primary_bearer(dash_env, monkeypatch) -> None:
    """The Agent-Tokens section shows hashes only — the primary bearer stays
    in the T-016e MCP-snippet flow, never in this section."""
    _configure(dash_env)
    monkeypatch.setenv("IOWAP_MCP_BEARER", "iw_mcp_primary_secret_token")
    agent_tokens.create("quiet-agent")

    section = onboarding._agents_section(None)

    assert "iw_mcp_primary_secret_token" not in section
    assert "quiet-agent" in section
    assert "iw_agt_" not in section  # hashes live in the store, never rendered
    # and the full render keeps the two concerns separated
    full = onboarding._render_dashboard().decode()
    assert "<h2>Agent-Tokens</h2>" in full
    token_in_agents_section = full.split("<h2>Agent-Tokens</h2>")[1]
    assert "iw_mcp_primary_secret_token" not in token_in_agents_section


def test_dashboard_routes_registered(dash_env) -> None:
    assert "/agents/create" in onboarding._DASHBOARD_POST_ROUTES
    assert "/agents/revoke" in onboarding._DASHBOARD_POST_ROUTES
    assert "/agents/caps-toggle" in onboarding._DASHBOARD_POST_ROUTES


# ---------------------------------------------------------------------------
# Untouched surfaces
# ---------------------------------------------------------------------------


def test_capabilities_cli_imports_unchanged(store_env) -> None:
    """T-042's CLI surface rides agent_tokens in parallel — no interference."""
    from nodes.common import dashboard as dash_mod

    assert hasattr(dash_mod, "create_provider")  # T-042 wraps these
    assert hasattr(dash_mod, "update_provider")
    assert hasattr(dash_mod, "delete_provider")
    assert hasattr(dash_mod, "save_script")
    # the store module stays transport-agnostic (T-037 rule): no MCP imports
    src = Path(agent_tokens.__file__).read_text()
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "iowap_client_mcp" not in stripped
            assert not stripped.startswith("import mcp")
            assert "node_mcp" not in stripped


# ---------------------------------------------------------------------------
# Contract shared with the MCP process — the example files live byte-identical
# in iowap-universal-node/mcp/tests/fixtures/agent_tokens/ (same file names).
# The mcp package cannot import this repo; the pinned hashes make any JSON
# contract drift fail BOTH suites instead of live traffic.
# ---------------------------------------------------------------------------

_AGENTS_EXAMPLE_SHA256 = "b920cc5b5d0bc22ed419760a5127ac70d99011988e3c506031cd32428a5c4e72"
_BINDINGS_EXAMPLE_SHA256 = "5a90e3a5a155f432ecba9b3a5613bafad4aa77dd5007c964bab8898fc30e73c8"




def test_shared_example_files_roundtrip(store_env) -> None:
    fixtures = Path(__file__).resolve().parent / "fixtures" / "agent_tokens"

    # byte-identity with the mcp-package copies is the drift guard
    assert (hashlib.sha256((fixtures / "agents.json").read_bytes()).hexdigest()
            == _AGENTS_EXAMPLE_SHA256)
    assert (hashlib.sha256((fixtures / "bindings.json").read_bytes()).hexdigest()
            == _BINDINGS_EXAMPLE_SHA256)

    # the STORE reads them natively (env override → fixtures dir)
    import os as _os

    _os.environ[agent_tokens.AGENT_DIR_ENV] = str(fixtures)
    try:
        agents = agent_tokens.load_agents()
        assert [a["id"] for a in agents] == [
            "agt-00001234", "agt-5678abcd", "agt-deadbeef"]

        bindings = agent_tokens.load_bindings()
        assert set(bindings["agt-00001234"]["seen_artifacts"]) == {"art-100", "art-101"}

        # a revoked agent's token never verifies; an active one resolves
        assert agent_tokens.verify("iw_agt_raw_of_old_scoped_agent") is None  # revoked
        entry = agent_tokens.verify("iw_agt_raw_of_hermes_laptop")
        assert entry is not None and entry["name"] == "hermes-laptop"

        # binding-view semantics against the shared example
        assert agent_tokens.can_read_task("agt-00001234", "TASK-1A")
        assert agent_tokens.can_read_artifact("agt-00001234", "art-101")
        assert not agent_tokens.can_read_artifact("agt-00001234", "art-999")
        # caps_manage rides the example too
        assert agent_tokens.load_agents()[1]["caps_manage"] is True
    finally:
        _os.environ.pop(agent_tokens.AGENT_DIR_ENV, None)
