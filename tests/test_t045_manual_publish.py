"""T-045: publish wird manuell — cap-Flag off, one-shot migration, auto-recompile,
checkbox batch UX.

The manual model (Ronny 25.11):

* ``bridge.allowlist`` is the ONLY publish state ("manuell = add, zurück
  = remove"); ``build_bridge_cap`` stamps ``auto_publish: False`` so a
  Layer-1-published cap can never blanket-ride the heartbeat wire again
  (Layer 2 — the per-cap flag — keeps the merged registry dicts off the
  wire).
* The node.yaml bridge-block write paths run the one-shot migration:
  ``auto_publish: true`` + empty allowlist → allowlist := the current
  providers.yaml names, flag off, logged ONCE (live state conserved).
* Create paths (dashboard/MCP/CLI) never touch the allowlist — new caps
  start unpublished.
* ``recompile_active_profile()`` recomposes the active node.yaml after
  EVERY mutation: local entries ride along, allowlisted provider entries
  are baked as routing-complete ``host.*`` entries, non-capability
  blocks survive, validation failures write NOTHING, both daemon pid
  files get a best-effort SIGHUP afterwards.
* The dashboard replaces the per-row instant toggles with a checkbox per
  row (desired state; the badge stays the actual state) and one
  „Update Capabilities" button → ``POST /caps/update-publish`` — the
  server diffs the posted (checked) names against the allowlist view,
  applies the delta without per-cap recompiles, recomposes ONCE and
  flashes the counter „published: N, unpublished: M, unchanged: K".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nodes.common import (
    active_profile,
    bridge,
    dashboard,
    node_config,
    node_daemon,
    node_utils,
    onboarding,
    poll_daemon,
    relay_client,
)
from nodes.common.cli import cli_capabilities
from tests.test_cli_capabilities_publish import kill_probe  # noqa: F401  (SIGHUP probe fixture)

BRIDGE_YAML = (
    "node_name: win-node\n"
    "relay:\n"
    "  base_url: http://r:8788\n"
    "bridge:\n"
    "  url: http://host.docker.internal:7780\n"
    "  token: bridging\n"
    "  workspace: /workspace\n"
    "auto_publish_flag_note: t045-fixture-marks-non-capability-blocks\n"
    "agents:\n"
    "  allow: []\n"
    "forward_slots: {}\n"
    "budget: 8\n"
)

BRIDGE_YAML_AUTO_ON = (
    "node_name: win-node\n"
    "bridge:\n"
    "  url: http://host.docker.internal:7780\n"
    "  token: bridging\n"
    "  auto_publish: true\n"
    "  allowlist: []\n"
    "mcp:\n"
    "  port: 7781\n"
)

LOCAL_CAPS = (
    "capabilities:\n"
    "- name: local.chat\n"
    "  version: 1.0.0\n"
    "  claimable: true\n"
    "  handler: /bin/true\n"
)


@pytest.fixture()
def t045_env(tmp_path, monkeypatch):
    """Isolate every state path: node.yaml, store workspace, daemon pids."""
    for name in (
        "META_PATH", "TOKEN_PATH", "CONFIG_PATH",
        "LEGACY_META_PATH", "LEGACY_TOKEN_PATH", "STATUS_PATH",
    ):
        monkeypatch.setattr(node_utils, name, tmp_path / f"{name.lower()}.json")
    active = tmp_path / "node.yaml"
    monkeypatch.setattr(node_config, "ACTIVE_PATH", active)
    monkeypatch.setattr(node_config, "BASE_DIR", tmp_path / "relay")
    monkeypatch.setattr(node_config, "_active_cache", node_config.ActiveProfileCache(path=active))
    monkeypatch.setattr(node_daemon, "PID_PATH", tmp_path / "node-daemon.pid")
    monkeypatch.setattr(poll_daemon, "PID_PATH", tmp_path / "node-cli.pid")
    monkeypatch.setenv("IOWAP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    bridge.reset_manager()
    yield tmp_path
    bridge.reset_manager()


def _seed_providers(*names: str) -> None:
    for name in names:
        ok, msg = dashboard.create_provider(
            {"name": name, "type": "process", "command": f"run {name}"}
        )
        assert ok, msg


def _allowlist() -> list[str]:
    doc = yaml.safe_load(node_config.ACTIVE_PATH.read_text(encoding="utf-8"))
    block = doc.get("bridge") or {}
    return block.get("allowlist") or []


def _post_form(server, data, path):
    """POST a multi-value checkbox form (urlencoded, doseq)."""
    import urllib.error
    import urllib.parse
    import urllib.request

    body = urllib.parse.urlencode(data, doseq=True).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}{path}", data=body, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return _Resp(resp.status, resp.read().decode())
    except urllib.error.HTTPError as exc:
        return _Resp(exc.code, exc.read().decode())


class _Resp:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


# ---------------------------------------------------------------------------
# Cap flag + gate trace (Layer 1 vs Layer 2)
# ---------------------------------------------------------------------------


def test_build_bridge_cap_no_blanket_flag():
    """T-045: build_bridge_cap stamps auto_publish False — no blanket wire."""
    cap = bridge.build_bridge_cap("comfy.generate", {"summary": "s"})
    assert cap["auto_publish"] is False
    assert cap["bridge"] is True


def test_gate_trace_layering(tmp_path, t045_env, monkeypatch):
    """The T-045 layering, traced end-to-end.

    Layer 1 (bridge._publish_allowed — allowlist/auto_publish) admits a
    manifest cap into the runtime REGISTRY (claim routing). Layer 2 (the
    cap-level auto_publish flag, now False) keeps the merged dict OFF
    the heartbeat wire. A provider cap reaches the relay only as a
    recomposed allowlist-filtered entry of the active profile — i.e.
    after an allowlist-seeding recompile, never by blanket flag.
    """
    from tests.test_t003_bridge import MANIFEST_OK

    node_config.ACTIVE_PATH.write_text(
        BRIDGE_YAML.replace("auto_publish_flag_note: t045-fixture-marks-non-capability-blocks\n", "")
        + "capabilities: []\n"
    )
    # (a) Layer 2 off alone: legacy blanket config (auto_publish ON) — the
    #     merged dict exists (Layer 1 passes) but carries auto_publish=False.
    doc = yaml.safe_load(node_config.ACTIVE_PATH.read_text())
    doc["bridge"]["auto_publish"] = True
    node_config.ACTIVE_PATH.write_text(yaml.safe_dump(doc, sort_keys=False))
    monkeypatch.setattr(
        bridge, "fetch_manifest", lambda *a, **k: dict(MANIFEST_OK)
    )
    bridge.get_manager().poll_once()
    merged = node_config.load_active_profile()
    cap = next(c for c in merged if c["name"] == "host.comfy.generate")
    assert cap["bridge"] is True and cap["auto_publish"] is False

    # a runtime token file so the RelayClient constructor succeeds
    node_utils.TOKEN_PATH.write_text(json.dumps({"token": "tok-1"}))
    client = relay_client.RelayClient(
        {"node_id": "n1"}, {"base_url": "http://r:8788"}
    )
    payload = client._build_heartbeat_payload(node_config.load_active_profile(), {})
    # (b) Layer 2 keeps the merged dict OFF the wire — kein blanket
    #     publish ohne allowlist-Eintrag, even with auto_publish ON.
    assert "host.comfy.generate" not in [e["name"] for e in payload["capabilities"]]

    # (c) The manual wire: allowlist entry + recompose → the baked entry
    #     rides the heartbeat (available=True), the merged dict still
    #     never rides it. The compose source is the workspace store —
    #     seed the operator's entry first.
    assert dashboard.create_provider(
        {"name": "comfy.generate", "type": "http", "url_template": "http://x/generate"}
    )[0]
    assert dashboard.set_capability_published("comfy.generate", True)[0]
    payload = client._build_heartbeat_payload(node_config.load_active_profile(), {})
    names = [e["name"] for e in payload["capabilities"]]
    assert "host.comfy.generate" in names
    entry = next(e for e in payload["capabilities"] if e["name"] == "host.comfy.generate")
    assert entry["available"] is True


# ---------------------------------------------------------------------------
# One-shot migration
# ---------------------------------------------------------------------------


def test_migration_seeds_allowlist_then_disables_flag(t045_env, caplog):
    node_config.ACTIVE_PATH.write_text(BRIDGE_YAML_AUTO_ON)
    _seed_providers("b.cap", "a.cap")
    block = {"auto_publish": True, "allowlist": [], "url": "http://x"}
    migrated = bridge.migrate_manual_publish_block(block, ["b.cap", "a.cap", "b.cap"])
    assert migrated is not None
    assert migrated["auto_publish"] is False
    assert migrated["allowlist"] == ["b.cap", "a.cap"]  # provider order, deduped
    with caplog.at_level("INFO", logger="bridge"):
        bridge.migrate_manual_publish_block(
            {"auto_publish": True, "allowlist": [], "url": "http://x"}, ["z.cap"]
        )
    assert any("T-045 one-shot migration" in r.message for r in caplog.records)


def test_migration_idempotent_and_guards():
    assert bridge.migrate_manual_publish_block("nope", ["a"]) is None
    assert bridge.migrate_manual_publish_block({"auto_publish": False}, ["a"]) is None
    assert (
        bridge.migrate_manual_publish_block({"auto_publish": True, "allowlist": ["x"]}, ["a"])
        is None
    )
    block = {"auto_publish": True, "allowlist": [], "url": "u"}
    once = bridge.migrate_manual_publish_block(block, ["a"])
    assert once == {"auto_publish": False, "allowlist": ["a"], "url": "u"}
    # second run on the MIGRATED block: flag off → condition gone → None
    assert bridge.migrate_manual_publish_block(once, ["a"]) is None


def test_migration_on_write_path_seeds_then_unpublish_stays_removed(t045_env):
    """Seed-then-mutate order: pulling a cap back right after the upgrade
    on a blanket config is a REAL remove, never re-seeded."""
    node_config.ACTIVE_PATH.write_text(BRIDGE_YAML_AUTO_ON)
    _seed_providers("a.cap", "b.cap")
    ok, _ = dashboard.set_capability_published("a.cap", False, recompile=False)
    assert ok
    doc = yaml.safe_load(node_config.ACTIVE_PATH.read_text())
    assert doc["bridge"]["auto_publish"] is False
    # seed-then-mutate: BOTH names were seeded (live state), then the
    # user's pull-back of a.cap applied — b.cap stays published:
    assert doc["bridge"]["allowlist"] == ["b.cap"]


def test_daemon_channel_adoption_runs_migration_and_bakes(t045_env):
    node_config.ACTIVE_PATH.write_text(BRIDGE_YAML_AUTO_ON.replace("mcp:\n  port: 7781\n", ""))
    _seed_providers("a.cap")
    channel = t045_env / "bridge" / "daemon.json"
    channel.parent.mkdir(parents=True, exist_ok=True)
    channel.write_text(json.dumps({"url": "http://host.docker.internal:7790", "token": "t2"}))
    from nodes.common import daemon_channel

    ok, _ = daemon_channel.apply_daemon_json(path=channel)
    assert ok
    doc = yaml.safe_load(node_config.ACTIVE_PATH.read_text())
    assert doc["bridge"]["url"] == "http://host.docker.internal:7790"
    assert doc["bridge"]["auto_publish"] is False
    assert doc["bridge"]["allowlist"] == ["a.cap"]
    # the bake: the live state rides the active profile immediately
    names = [c["name"] for c in doc["capabilities"]]
    assert "host.a.cap" in names


# ---------------------------------------------------------------------------
# Create paths never touch the allowlist
# ---------------------------------------------------------------------------


def test_all_create_paths_leave_allowlist_and_state_unpublished(t045_env):
    node_config.ACTIVE_PATH.write_text(BRIDGE_YAML + LOCAL_CAPS)
    _seed_providers("a.cap")

    # dashboard create
    ok, _ = dashboard.create_provider({"name": "new.cap", "type": "process", "command": "x"})
    assert ok
    assert _allowlist() == []

    # CLI wrap (MCP capability_create drives exactly this — T-042)
    args = SimpleNamespace(
        name="cli.cap", type="process", command="run x", args=None, summary=None,
        script=None, max_parallel=None, cost=None, artifacts_glob=None,
        version=None, method=None, url_template=None, body_template=None,
        result_path=None, artifact_paths=None, health_url=None,
        publish=False, unpublish=False, json=True, log_level="ERROR",
    )
    assert cli_capabilities._cmd_capabilities_create(args) == 0
    assert _allowlist() == []

    # the new cap starts unpublished in the recomposed profile
    doc = yaml.safe_load(node_config.ACTIVE_PATH.read_text())
    names = [c["name"] for c in doc["capabilities"]]
    assert "host.new.cap" not in names
    assert "host.cli.cap" not in names
    assert "host.a.cap" not in names  # a.cap exists but is NOT allowlisted
    assert dashboard.is_published("new.cap", bridge.load_bridge_config()) is False


# ---------------------------------------------------------------------------
# recompile_active_profile pipeline
# ---------------------------------------------------------------------------


def _seed_bridge_block(allowlist: list[str] | None = None, auto_publish: bool = False) -> None:
    text = BRIDGE_YAML + LOCAL_CAPS
    node_config.ACTIVE_PATH.write_text(text)
    doc = yaml.safe_load(node_config.ACTIVE_PATH.read_text())
    doc["bridge"]["allowlist"] = allowlist or []
    doc["bridge"]["auto_publish"] = auto_publish
    node_config.ACTIVE_PATH.write_text(yaml.safe_dump(doc, sort_keys=False))


def test_recompose_happy_bakes_only_published(t045_env):
    _seed_bridge_block(allowlist=["a.cap"])
    _seed_providers("a.cap", "b.cap")
    ok, msg = active_profile.recompile_active_profile()
    assert ok and "1 published bridge cap(s) baked" in msg
    doc = yaml.safe_load(node_config.ACTIVE_PATH.read_text())
    caps = [c for c in doc["capabilities"] if c.get("bridge")]
    assert [c["name"] for c in caps] == ["host.a.cap"]
    # routing-complete baked entry (bridge pair survives the schema)
    baked = caps[0]
    assert baked["bridge"] is True and baked["bridge_name"] == "a.cap"
    assert baked["name"] == "host.a.cap"
    assert baked["claimable"] is False
    assert "auto_publish" not in baked  # offered-state lives in the allowlist
    assert baked["description"] == "run a.cap" or baked["description"] is None


def test_recompose_locals_ride_and_unpublished_missing(t045_env):
    _seed_bridge_block(allowlist=[])
    _seed_providers("a.cap", "b.cap")
    ok, msg = active_profile.recompile_active_profile()
    assert ok
    doc = yaml.safe_load(node_config.ACTIVE_PATH.read_text())
    bridged = [c["name"] for c in doc["capabilities"] if c.get("bridge")]
    assert bridged == []  # unpublished missing
    locals_ = [c["name"] for c in doc["capabilities"] if not c.get("bridge")]
    assert locals_ == ["local.chat"]  # local entries ride along untouched


def test_recompose_other_blocks_survive_byte_equal(t045_env):
    """Load-mutate-dump: with an EMPTY published set the recomposed file
    must be byte-identical to a pure load→dump round trip — only
    ``capabilities`` is ever replaced."""
    _seed_bridge_block(allowlist=[])
    _seed_providers("a.cap", "b.cap")
    before_text = node_config.ACTIVE_PATH.read_text()
    before_doc = yaml.safe_load(before_text)
    ok, _ = active_profile.recompile_active_profile()
    assert ok
    after_text = node_config.ACTIVE_PATH.read_text()
    assert after_text == yaml.safe_dump(before_doc, sort_keys=False, allow_unicode=True)

    # now publish a.cap: every NON-capability block keeps its dump shape,
    # capabilities gains exactly the baked entry
    ok, _ = dashboard.set_capability_published("a.cap", True)
    assert ok
    after_text = node_config.ACTIVE_PATH.read_text()
    doc_after = yaml.safe_load(after_text)
    # the toggle writes bridge.allowlist (its own job) and the recompose
    # writes capabilities — EVERY OTHER block stays dump-identical:
    expected = yaml.safe_dump(
        {**before_doc, "bridge": doc_after["bridge"], "capabilities": doc_after["capabilities"]},
        sort_keys=False,
        allow_unicode=True,
    )
    assert after_text == expected  # byte-equal: only bridge+capabilities moved
    doc = yaml.safe_load(after_text)
    assert doc["relay"]["base_url"] == "http://r:8788"
    assert doc["bridge"]["token"] == "bridging"
    assert doc["agents"]["allow"] == []
    assert doc["budget"] == 8
    assert doc["forward_slots"] == {}
    assert doc["node_name"] == "win-node"


def test_recompose_validation_error_write_nothing(t045_env):
    """A composed profile that does not validate never reaches disk.

    The compose sanitizes provider fields itself (build_bridge_cap
    coerces), so true validation failures come from the OTHER side of
    the file: a broken local entry (e.g. a hand-edited profile) must
    keep the WHOLE recompose from writing — node.yaml stays
    byte-identical and carries the failure into the flash note.
    """
    _seed_bridge_block(allowlist=["a.cap"])
    _seed_providers("a.cap")
    # break a LOCAL entry in the file (hand-edit class of failure)
    doc = yaml.safe_load(node_config.ACTIVE_PATH.read_text())
    for entry in doc["capabilities"]:
        if entry.get("name") == "local.chat":
            entry["max_parallel"] = 0  # violates the profile schema
    node_config.ACTIVE_PATH.write_text(yaml.safe_dump(doc, sort_keys=False))
    before = node_config.ACTIVE_PATH.read_bytes()
    ok, msg = active_profile.recompile_active_profile()
    assert ok is False
    assert "node.yaml untouched" in msg
    assert node_config.ACTIVE_PATH.read_bytes() == before  # write-nothing
    assert active_profile.last_recompile_note().startswith("recompile failed")


def test_recompose_wildcard_publishes_everything(t045_env):
    _seed_bridge_block(allowlist=["*"])
    _seed_providers("a.cap", "b.cap")
    ok, _ = active_profile.recompile_active_profile()
    assert ok
    doc = yaml.safe_load(node_config.ACTIVE_PATH.read_text())
    bridged = sorted(c["name"] for c in doc["capabilities"] if c.get("bridge"))
    assert bridged == ["host.a.cap", "host.b.cap"]


def test_recompose_no_active_file_write_nothing(t045_env):
    assert not node_config.ACTIVE_PATH.exists()
    _seed_providers("a.cap")
    ok, msg = active_profile.recompile_active_profile()
    assert ok and "no active node.yaml" in msg
    assert not node_config.ACTIVE_PATH.exists()


def test_recompose_collision_with_local_entry_skipped(t045_env):
    text = (
        BRIDGE_YAML
        + "capabilities:\n"
        + "- name: host.a.cap\n"
        + "  version: 1.0.0\n"
        + "  claimable: true\n"
        + "  handler: /bin/true\n"
    )
    node_config.ACTIVE_PATH.write_text(text)
    doc = yaml.safe_load(text)
    doc["bridge"]["allowlist"] = ["a.cap"]
    node_config.ACTIVE_PATH.write_text(yaml.safe_dump(doc, sort_keys=False))
    _seed_providers("a.cap")
    ok, msg = active_profile.recompile_active_profile()
    assert ok and "local collision" in msg
    doc = yaml.safe_load(node_config.ACTIVE_PATH.read_text())
    # the operator's local entry keeps the name — no double bake
    matches = [c for c in doc["capabilities"] if c["name"] == "host.a.cap"]
    assert len(matches) == 1
    assert matches[0].get("handler") == "/bin/true"


def test_recompose_sighup_best_effort_changed_and_unchanged(t045_env, kill_probe):  # noqa: F811
    sent, alive = kill_probe
    _seed_bridge_block(allowlist=["a.cap"])
    _seed_providers("a.cap")
    alive.update({4242, 4243})
    node_daemon.PID_PATH.write_text("4242\n")
    poll_daemon.PID_PATH.write_text("4243\n")
    ok, msg = active_profile.recompile_active_profile()
    assert ok and "SIGHUP 4242, 4243" in msg  # readable pid note
    assert [(4242, 1), (4243, 1)] == [s for s in sent if s[1] == 1]
    sent.clear()
    # unchanged state → STILL recompiled + SIGHUPed (deterministic)
    ok, _ = active_profile.recompile_active_profile()
    assert ok
    assert sorted(p for p, _ in sent) == [4242, 4243]

    sent.clear()
    # no live daemons → best-effort: no signal, ok=True
    PID_A = node_daemon.PID_PATH  # noqa: N806 — test-local clarity
    PID_B = poll_daemon.PID_PATH  # noqa: N806
    PID_A.unlink(); PID_B.unlink()  # noqa: E702
    ok, _ = active_profile.recompile_active_profile()
    assert ok and sent == []


# ---------------------------------------------------------------------------
# Mutation-path hooks (dashboard / MCP-via-CLI / CLI)
# ---------------------------------------------------------------------------


def test_store_mutations_each_recompose(t045_env, monkeypatch):
    """Dashboard create/update/delete + the allowlist toggle each fire the
    recompile exactly once — MCP drives the same functions (T-042 CLI
    wrap), so one hook covers all three paths."""
    node_config.ACTIVE_PATH.write_text(BRIDGE_YAML + LOCAL_CAPS)
    calls: list[str] = []
    real = active_profile.recompile_active_profile

    def counting(**kwargs):
        calls.append("recompile")
        return real(**kwargs)

    monkeypatch.setattr(active_profile, "recompile_active_profile", counting)
    # rebind the dashboard-level hook (it imports the module lazily but
    # resolves the attribute at call time → the patch applies)
    _seed_providers("a.cap")
    assert len(calls) == 1
    ok, _ = dashboard.update_provider(
        "a.cap", {"name": "a.cap", "type": "process", "command": "run a.cap", "summary": "edited"}
    )
    assert ok and len(calls) == 2
    ok, _ = dashboard.delete_provider("a.cap")
    assert ok and len(calls) == 3
    ok, _ = dashboard.set_capability_published("x", True)  # even for unknown names
    assert ok and len(calls) == 4


def test_set_capability_published_recompile_kwarg(t045_env, monkeypatch):
    node_config.ACTIVE_PATH.write_text(BRIDGE_YAML + LOCAL_CAPS)
    calls: list[str] = []
    monkeypatch.setattr(
        active_profile,
        "recompile_active_profile",
        lambda **kwargs: calls.append("recompile") or (True, "n"),
    )
    ok, _ = dashboard.set_capability_published("a.cap", True, recompile=False)
    assert ok
    assert _allowlist() == ["a.cap"]
    assert calls == []  # batch mode: no per-cap recompile


def test_cli_wraps_recompose_via_store(t045_env):
    """CLI create/update/delete/--publish all end in the recomposed profile
    (the MCP capability_* tools call exactly these handlers)."""
    node_config.ACTIVE_PATH.write_text(BRIDGE_YAML + LOCAL_CAPS)
    base = dict(
        name=None, type=None, summary=None, command=None, args=None, script=None,
        max_parallel=None, cost=None, artifacts_glob=None, version=None,
        method=None, url_template=None, body_template=None, result_path=None,
        artifact_paths=None, health_url=None, publish=False, unpublish=False,
        json=True, log_level="ERROR",
    )
    create_args = SimpleNamespace(**{**base, "name": "cli.cap", "type": "process", "command": "run x"})
    assert cli_capabilities._cmd_capabilities_create(create_args) == 0
    names = [c["name"] for c in yaml.safe_load(node_config.ACTIVE_PATH.read_text())["capabilities"]]
    assert "local.chat" in names and "host.cli.cap" not in names

    publish_args = SimpleNamespace(**{**base, "name": "cli.cap", "publish": True})
    assert cli_capabilities._cmd_capabilities_update(publish_args) == 0
    doc = yaml.safe_load(node_config.ACTIVE_PATH.read_text())
    names = [c["name"] for c in doc["capabilities"]]
    assert "host.cli.cap" in names  # recomposed: published → baked
    assert doc["bridge"]["allowlist"] == ["cli.cap"]

    delete_args = SimpleNamespace(**{**base, "name": "cli.cap"})
    assert cli_capabilities._cmd_capabilities_delete(delete_args) == 0
    doc = yaml.safe_load(node_config.ACTIVE_PATH.read_text())
    names = [c["name"] for c in doc["capabilities"]]
    assert "host.cli.cap" not in names


# ---------------------------------------------------------------------------
# Dashboard checkbox-batch UX (1c)
# ---------------------------------------------------------------------------


def test_render_checkboxes_match_actual_state(t045_env, monkeypatch):
    import tests.test_t021_dashboard as t021

    t021._configure(t045_env)
    node_config.ACTIVE_PATH.write_text(BRIDGE_YAML + LOCAL_CAPS)
    _seed_providers("a.cap", "b.cap")
    assert dashboard.set_capability_published("a.cap", True)[0]
    srv = t021._start_server(t045_env, monkeypatch)
    try:
        resp = t021._get(srv, "/")
        html = resp.text
        # Ist → checked: a.cap checked, b.cap unchecked
        assert "value='a.cap' form='caps-publish-form' checked>" in html
        assert "value='b.cap' form='caps-publish-form'>" in html
        # the batch form + button
        assert "action='/caps/update-publish'" in html
        assert ">Update Capabilities</button>" in html
        # the per-row instant toggles are GONE
        assert "action='/caps/publish'" not in html
        assert "action='/caps/unpublish'" not in html
    finally:
        onboarding.shutdown(srv)


def test_batch_update_publish_diff_and_counter(t045_env, monkeypatch):
    import tests.test_t021_dashboard as t021

    t021._configure(t045_env)
    node_config.ACTIVE_PATH.write_text(BRIDGE_YAML + LOCAL_CAPS)
    _seed_providers("a.cap", "b.cap", "c.cap")
    assert dashboard.set_capability_published("a.cap", True)[0]
    srv = t021._start_server(t045_env, monkeypatch)
    try:
        resp = _post_form(srv, {"caps": ["a.cap", "b.cap"]}, "/caps/update-publish")
        assert resp.status_code == 200
        assert (
            "published: 1, unpublished: 0, unchanged: 1" in resp.text
        )
        assert _allowlist() == ["a.cap", "b.cap"]
        # EINMAL recompile für den Batch → the recomposed profile carries
        # both (allowlist state == wire state in one write)
        doc = yaml.safe_load(node_config.ACTIVE_PATH.read_text())
        bridged = [c["name"] for c in doc["capabilities"] if c.get("bridge")]
        assert bridged == ["host.a.cap", "host.b.cap"]
    finally:
        onboarding.shutdown(srv)


def test_batch_update_publish_unpublishes_removed_and_recompiles_once(
    t045_env, monkeypatch
):
    import tests.test_t021_dashboard as t021

    t021._configure(t045_env)
    node_config.ACTIVE_PATH.write_text(BRIDGE_YAML + LOCAL_CAPS)
    _seed_providers("a.cap", "c.cap")
    assert dashboard.set_capability_published("a.cap", True)[0]
    calls: list[str] = []
    real = active_profile.recompile_active_profile

    def counting(**kwargs):
        calls.append("recompile")
        return real(**kwargs)

    monkeypatch.setattr(active_profile, "recompile_active_profile", counting)
    srv = t021._start_server(t045_env, monkeypatch)
    try:
        # checked = c only → a pulled back, c newly published
        resp = _post_form(srv, {"caps": ["c.cap"]}, "/caps/update-publish")
        assert resp.status_code == 200
        assert "published: 1, unpublished: 1, unchanged: 0" in resp.text
        assert _allowlist() == ["c.cap"]
        doc = yaml.safe_load(node_config.ACTIVE_PATH.read_text())
        bridged = [c["name"] for c in doc["capabilities"] if c.get("bridge")]
        assert bridged == ["host.c.cap"]
        # the two toggles ran WITHOUT recompiles; ONE recompose total
        assert calls == ["recompile"]
    finally:
        onboarding.shutdown(srv)


def test_batch_ignores_unknown_names(t045_env, monkeypatch):
    import tests.test_t021_dashboard as t021

    t021._configure(t045_env)
    node_config.ACTIVE_PATH.write_text(BRIDGE_YAML + LOCAL_CAPS)
    _seed_providers("a.cap")
    assert dashboard.set_capability_published("a.cap", True)[0]
    srv = t021._start_server(t045_env, monkeypatch)
    try:
        resp = _post_form(srv, {"caps": ["ghost.cap", "a.cap"]}, "/caps/update-publish")
        assert resp.status_code == 200
        assert "published: 0, unpublished: 0, unchanged: 1" in resp.text
        assert _allowlist() == ["a.cap"]  # the checked cap was already published
        # a no-delta batch still recomposes (+ SIGHUP) — deterministic
        assert "Update Capabilities saved —" in resp.text
        assert "recompiled active profile" in resp.text
    finally:
        onboarding.shutdown(srv)


def test_broadcast_routes_replaced(t045_env, monkeypatch):
    assert "/caps/update-publish" in onboarding._DASHBOARD_POST_ROUTES
    assert "/caps/publish" not in onboarding._DASHBOARD_POST_ROUTES
    assert "/caps/unpublish" not in onboarding._DASHBOARD_POST_ROUTES
    import tests.test_t021_dashboard as t021

    t021._configure(t045_env)
    node_config.ACTIVE_PATH.write_text(BRIDGE_YAML + LOCAL_CAPS)
    _seed_providers("a.cap")
    srv = t021._start_server(t045_env, monkeypatch)
    try:
        resp = t021._post(srv, {"name": "a.cap"}, "/caps/publish")
        assert resp.status_code == 404  # the route is gone (unknown dashboard route)
    finally:
        onboarding.shutdown(srv)
