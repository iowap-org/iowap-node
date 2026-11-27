"""T-050: agent-token store WRITE probe — /data/agents → relay-dir fallback.

Contracts proven (brief: references/t050-agent-store-permissions-brief.md):

* the resolver gains a WRITE probe (``mkdir`` + tmp write + unlink) on the
  volume candidate — ``os.access()`` alone misses exactly the root-owned
  named volume that surfaced as ``PermissionError [Errno 13]:
  /data/agents/agents.json.tmp`` (Ronny-Log 2026-11-27, container as
  appuser);
* ``IOWAP_AGENTS_DIR`` env override wins unprobed, per call (tests/
  exotic installs keep full control);
* an unwritable volume falls back to ``<relay-dir>/agents`` and logs the
  fallback warning EXACTLY ONCE — the resolution is cached on first call
  (probe-once per process, cache reset is a test seam);
* the dashboard create path works against the resolved fallback store
  (the entry lands there — the Ronny-VM state after the fix), and a still
  failing store write degrades to a 200 flash with the cause instead of
  the generic exception dump;
* companion COPY fix (same onboarding terrain): the MCP section speaks
  primary/operator semantics — agents are pointed at their own tokens in
  the Agent-Tokens section, the old single-bearer invitation is gone, and
  the service controls are byte-identical.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nodes.common import agent_tokens, node_config, onboarding  # noqa: E402

# chmod 0o555 proves "unwritable" only for real (non-root) POSIX users.
_NON_ROOT_POSIX = os.name == "posix" and os.geteuid() != 0


# ---------------------------------------------------------------------------
# Fixtures (t035 pattern — plus the T-050 probe-once cache reset)
# ---------------------------------------------------------------------------


@pytest.fixture()
def probe_env(tmp_path, monkeypatch):
    """Isolate the store: relay-dir fallback + no volume + no env override."""
    monkeypatch.setattr(node_config, "BASE_DIR", tmp_path / "relay")
    monkeypatch.delenv(agent_tokens.AGENT_DIR_ENV, raising=False)
    # simulate "no dedicated volume exists" on the test host
    monkeypatch.setattr(agent_tokens, "VOLUME_AGENT_DIR", tmp_path / "definitely-absent")
    # probe-once cache is process state — every test gets a fresh probe
    monkeypatch.setattr(agent_tokens, "_AGENTS_DIR_CACHE", None)
    return tmp_path


@pytest.fixture()
def dash_env(probe_env, tmp_path, monkeypatch):
    """Everything the dashboard renders/reads (t035 pattern, + store probe)."""
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
    import json

    onboarding.node_utils.META_PATH.write_text(json.dumps(
        {"node_id": "N1", "node_name": "win-node", "base_url": "http://r:8788"}))
    onboarding.node_utils.TOKEN_PATH.write_text(json.dumps({"token": "tok-1"}))
    onboarding.node_utils.CONFIG_PATH.write_text(json.dumps({"base_url": "http://r:8788"}))


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


def _root_volume(tmp_path, monkeypatch):
    """A volume candidate that EXISTS but rejects writes (root-owned mount)."""
    volume = tmp_path / "data" / "agents"
    volume.mkdir(parents=True)
    monkeypatch.setattr(agent_tokens, "VOLUME_AGENT_DIR", volume)
    volume.chmod(0o555)
    return volume


# ---------------------------------------------------------------------------
# Resolver order + write probe
# ---------------------------------------------------------------------------


def test_env_override_wins_unprobed(probe_env, monkeypatch) -> None:
    """``IOWAP_AGENTS_DIR`` short-circuits — even a failing probe stays out."""
    override = probe_env / "explicit"
    monkeypatch.setenv(agent_tokens.AGENT_DIR_ENV, str(override))
    assert agent_tokens.agents_dir() == override


def test_writable_volume_is_chosen_and_probe_leaves_no_litter(
    probe_env, tmp_path, monkeypatch
) -> None:
    volume = tmp_path / "data" / "agents"
    volume.mkdir(parents=True)
    monkeypatch.setattr(agent_tokens, "VOLUME_AGENT_DIR", volume)
    assert agent_tokens.agents_dir() == volume
    assert list(volume.iterdir()) == []  # the write probe cleans up after itself


def test_volume_absent_still_falls_back_to_relay_dir(probe_env) -> None:
    """Volume-less containers keep the established contract (no probe on a
    directory that does not exist)."""
    assert agent_tokens.agents_dir() == probe_env / "relay" / "agents"


@pytest.mark.skipif(not _NON_ROOT_POSIX, reason="chmod-based unwritable test")
def test_unwritable_volume_falls_back_and_logs_exactly_once(
    probe_env, tmp_path, monkeypatch, caplog
) -> None:
    volume = _root_volume(tmp_path, monkeypatch)
    fallback = probe_env / "relay" / "agents"
    with caplog.at_level(logging.WARNING, logger="agent-tokens"):
        first = agent_tokens.agents_dir()
        second = agent_tokens.agents_dir()

    assert first == second == fallback
    warnings = [r for r in caplog.records
                if r.name == "agent-tokens" and r.levelno == logging.WARNING]
    assert len(warnings) == 1  # probe-once — the second call is a cache hit
    assert "not writable" in warnings[0].getMessage()
    assert "/data/agents" in warnings[0].getMessage()
    volume.chmod(0o755)  # leave tmp_path deletable


@pytest.mark.skipif(not _NON_ROOT_POSIX, reason="chmod-based unwritable test")
def test_probe_result_is_cached_even_if_access_changes_later(
    probe_env, tmp_path, monkeypatch
) -> None:
    """Probe-once per process: a later permission change does not flip the
    resolution mid-process (restart picks it up — logged contract)."""
    volume = tmp_path / "data" / "agents"
    volume.mkdir(parents=True)
    monkeypatch.setattr(agent_tokens, "VOLUME_AGENT_DIR", volume)
    first = agent_tokens.agents_dir()
    assert first == volume
    volume.chmod(0o555)  # "volume became root-owned after boot"
    assert agent_tokens.agents_dir() == volume  # cache, no re-probe
    volume.chmod(0o755)


# ---------------------------------------------------------------------------
# Dashboard paths (the Ronny-VM state)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _NON_ROOT_POSIX, reason="chmod-based unwritable test")
def test_dashboard_create_lands_in_the_fallback_store(dash_env, tmp_path, monkeypatch):
    """Root-owned volume → resolver falls back → POST /agents/create writes
    the entry into ``<relay-dir>/agents`` (the fix for the existing VM)."""
    _root_volume(tmp_path, monkeypatch)
    _configure(dash_env)
    _stub_render_side(monkeypatch)

    status, body = onboarding._handle_dashboard_post(
        "/agents/create", {"name": ["vm-agent"]})
    text = body.decode()

    assert status == 200
    assert "vm-agent" in text
    assert "shown only this once" in text
    entry = agent_tokens.load_agents()[0]
    assert entry["name"] == "vm-agent"
    assert agent_tokens.agents_dir() == dash_env / "relay" / "agents"
    assert agent_tokens.agents_path().exists()
    (dash_env / "data" / "agents").chmod(0o755)


def _store_boom(*_args, **_kwargs):
    """Raising PermissionError shaped like the Ronny-Log entry."""
    raise PermissionError(13, "Permission denied", "/data/agents/agents.json.tmp")


@pytest.mark.parametrize("path,form,fn_name", [
    ("/agents/create", {"name": ["any"]}, "create"),
    ("/agents/revoke", {"agent_id": ["agt-12345678"]}, "revoke"),
    ("/agents/caps-toggle",
     {"agent_id": ["agt-12345678"], "enable": ["1"]}, "set_caps_manage"),
])
def test_agent_routes_flash_the_cause_instead_of_a_crash_dump(
    dash_env, monkeypatch, path, form, fn_name
) -> None:
    """Store-write failures on /agents/* → 200 + error flash with the cause
    (path + strerror + resolver hint) — never the generic exception dump."""
    _configure(dash_env)
    _stub_render_side(monkeypatch)
    monkeypatch.setattr(agent_tokens, fn_name, _store_boom)

    status, body = onboarding._handle_dashboard_post(path, form)
    text = body.decode()

    assert status == 200
    assert "agent token store not writable" in text
    assert "/data/agents/agents.json.tmp" in text
    assert "Permission denied" in text
    assert "falls back automatically" in text
    assert "Traceback" not in text


# ---------------------------------------------------------------------------
# Companion COPY fix (T-050): MCP section speaks primary/operator language
# ---------------------------------------------------------------------------


def test_mcp_section_points_agents_at_their_own_tokens(dash_env, monkeypatch) -> None:
    """The rendered MCP card names the bearer as PRIMARY (operator), invites
    agents to the Agent-Tokens section — and drops the old single-bearer
    invitation; service controls/snippets stay exactly as they were."""
    _configure(dash_env)
    _stub_render_side(monkeypatch)
    monkeypatch.setenv("IOWAP_MCP_BEARER", "iw_mcp_primary_operator_bearer")

    section = onboarding._mcp_section()

    # the operator hint + snippets label are in
    assert "primary (operator)" in section
    assert "Agent-Tokens" in section
    assert "Primary bearer (operator) — config snippets:" in section
    assert "Service status + primary (operator) access" in section
    # the old single-bearer agent invitation is gone
    assert "configure your agents (Hermes, Claude, Codex) against this" not in section
    # behaviour unchanged: the same controls and snippet surfaces remain
    assert "action='/mcp/enable'" in section
    assert "action='/mcp/disable'" in section
    assert "action='/mcp/bearer/rotate'" in section
    assert "iw_mcp_primary_operator_bearer" in section
