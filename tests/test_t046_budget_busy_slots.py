"""T-046: Budget-Config + Prozent + Busy-Schalter + Forward-Slots-DEFINE.

Ronny-Order 25.11 (Board: „Node-Dashboard: Budget-Config + Prozent +
Busy-Schalter + Forward-Slots-Nachzug"):

1. **Budget**: node.yaml ``budget.total`` (Zahl, Default 100) — the
   accounting ceiling NEXT TO the T-247 admission axis (the legacy int
   form keeps its admission meaning and serves both); the meter accumulates
   ``cost`` per COMPLETED capability task, ONLY bridge caps with cost>0
   count, persists in ``<relay-dir>/state/budget_used`` atomic
   (T-035 pattern); dashboard shows ``used N / budget B (P%)`` + Reset;
   NO auto-busy at 100% (v1 deliberately simple).
2. **Busy-Schalter**: dashboard toggle → persists in node.yaml (the
   T-081 ``status`` field — the exact heartbeat-field form the relay
   honours, ``core/discovery.py`` explicit-request path) → heartbeat
   carries the explicit-busy request; toggle-off reverts to ``idle``.
3. **Forward-Slots DEFINE**: the /forward/save + /forward/delete routes
   maintain only the relay-side MAPPINGS (T-030, admin-blocked T-246 —
   the 403 blende stays); the DEFINE flow (label/type/value/ref,
   T-245-typed) writes node.yaml ``forward_slots`` LOCALLY via
   /slots/define + /slots/delete — no admin role involved. Happy path:
   slot defined → node.yaml ``forward_slots`` erzeugt.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from nodes.common import active_profile, budget, node_config, poll_daemon, node_daemon
from nodes.common import onboarding
from nodes.common.relay_client import RelayClient

META = {
    "node_id": "T046NODE",
    "node_name": "budget-busy-slots",
    "registration_secret": "rs_x",
    "base_url": "http://relay.test:8788",
}
CFG = {"base_url": None, "request_timeout": 5, "heartbeat_interval": 1}


@pytest.fixture()
def node_env(tmp_path, monkeypatch):
    """Isolate the active node.yaml + the budget state dir (T-035 pattern)
    and provision meta/token/config so RelayClient never walks into the
    registration-recovery path (same shape as the T-035 _configure)."""
    relay_dir = tmp_path / "relay"
    monkeypatch.setattr(node_config, "BASE_DIR", relay_dir)
    monkeypatch.setattr(node_config, "ACTIVE_PATH", relay_dir / "node.yaml")
    monkeypatch.delenv(budget.BUDGET_STATE_ENV, raising=False)
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("IOWAP_MCP_BEARER", raising=False)
    for name in (
        "META_PATH", "TOKEN_PATH", "CONFIG_PATH",
        "LEGACY_META_PATH", "LEGACY_TOKEN_PATH", "STATUS_PATH",
    ):
        monkeypatch.setattr(onboarding.node_utils, name, tmp_path / f"{name.lower()}.json")
    onboarding.node_utils.META_PATH.write_text(json.dumps(dict(META)))
    onboarding.node_utils.TOKEN_PATH.write_text(json.dumps({"token": "tok-1"}))
    onboarding.node_utils.CONFIG_PATH.write_text(json.dumps({"base_url": META["base_url"]}))
    return relay_dir


def _write_node_yaml(relay_dir: Path, doc: dict) -> Path:
    path = relay_dir / "node.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _read_node_yaml(relay_dir: Path) -> dict:
    return yaml.safe_load((relay_dir / "node.yaml").read_text(encoding="utf-8"))


def _slot_rows(doc: dict) -> dict:
    """Normalize both documented section shapes to ``{slot: entry}``."""
    section = doc.get("forward_slots")
    if isinstance(section, list):
        return {r["slot"]: r for r in section if isinstance(r, dict) and r.get("slot")}
    return dict(section) if isinstance(section, dict) else {}


# ---------------------------------------------------------------------------
# Budget config read (budget_total)
# ---------------------------------------------------------------------------


def test_budget_total_defaults_to_100_without_active_file(node_env) -> None:
    assert node_env.exists() is False
    assert budget.budget_total() == budget.DEFAULT_BUDGET_TOTAL == 100


def test_budget_total_reads_mapping_form(node_env) -> None:
    _write_node_yaml(node_env, {"budget": {"total": 42}})
    assert budget.budget_total() == 42


def test_budget_total_legacy_int_form_serves_both_axes(node_env) -> None:
    """T-247 keeps the admission meaning of ``budget: <int>``; the same
    int is the T-046 accounting ceiling (one number, two axes)."""
    _write_node_yaml(node_env, {"budget": 7})
    assert node_config.load_active_budget() == 7
    assert budget.budget_total() == 7


def test_budget_total_mapping_form_also_feeds_admission(node_env) -> None:
    """The dashboard writes the mapping form — it must never silently
    disarm the T-247 admission axis: budget.total is BOTH axes."""
    _write_node_yaml(node_env, {"budget": {"total": 42}})
    assert budget.budget_total() == 42
    assert node_config.load_active_budget() == 42


def test_budget_total_mapping_without_total_disarms_admission(node_env) -> None:
    """A hand-edited broken mapping validates as an error at publish time
    (schema) and degrades to axis-off here (never die on YAML content)."""
    _write_node_yaml(node_env, {"budget": {}})
    assert budget.budget_total() == 100
    assert node_config.load_active_budget() is None


def test_budget_total_mapping_without_total_degrades_to_default(node_env) -> None:
    _write_node_yaml(node_env, {"budget": {}})
    assert budget.budget_total() == 100


def test_budget_total_negative_total_degrades_to_default(node_env) -> None:
    _write_node_yaml(node_env, {"budget": {"total": -1}, "capabilities": []})
    assert budget.budget_total() == 100


def test_validate_with_schema_accepts_budget_mapping_and_int() -> None:
    assert node_config.validate_with_schema({"budget": {"total": 10}}) == []
    assert node_config.validate_with_schema({"budget": 10}) == []
    assert node_config.validate_with_schema({}) == []


def test_validate_with_schema_rejects_broken_budget_forms() -> None:
    assert node_config.validate_with_schema({"budget": {"total": -1}})
    assert node_config.validate_with_schema({"budget": {"other": 1}})
    assert node_config.validate_with_schema({"budget": {"total": 1, "extra": 2}})
    assert node_config.validate_with_schema({"budget": True})
    assert node_config.validate_with_schema({"budget": -3})


# ---------------------------------------------------------------------------
# Meter (record/reset/read)
# ---------------------------------------------------------------------------


def test_meter_state_dir_defaults_to_relay_dir_state(node_env) -> None:
    assert budget.state_dir() == node_env / "state"
    assert budget.used_path() == node_env / "state" / "budget_used"


def test_meter_state_dir_env_override(node_env, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(budget.BUDGET_STATE_ENV, str(tmp_path / "custom"))
    assert budget.state_dir() == tmp_path / "custom"


def test_record_stage_cost_gate_non_bridge_and_free_caps(node_env) -> None:
    assert budget.record_stage_cost({"name": "x", "cost": 5}, 5) is None
    assert budget.record_stage_cost({"name": "x", "bridge": False}, 5) is None
    assert budget.record_stage_cost({"name": "h", "bridge": True, "cost": 0}, 0) is None
    assert budget.record_stage_cost({"name": "h", "bridge": True}, 0) is None
    assert not budget.used_path().exists(), "the gate must never touch the file"
    assert budget.read_used() == 0


def test_record_stage_cost_accumulates_bridge_caps(node_env) -> None:
    cap = {"name": "host.comfy", "bridge": True, "cost": 3}
    assert budget.record_stage_cost(cap, 3) == 3
    assert budget.record_stage_cost(cap, 3) == 6
    node = json.loads(budget.used_path().read_text(encoding="utf-8"))
    assert node["used"] == 6


def test_record_stage_cost_accepts_cost_argument_over_cap_default(node_env) -> None:
    """The daemons pass the captured admission cost (the T-247 reserve)."""
    cap = {"name": "host.comfy", "bridge": True, "cost": 2}
    assert budget.record_stage_cost(cap, 5) == 5


def test_record_stage_cost_never_raises_on_stale_state(node_env) -> None:
    budget.state_dir().mkdir(parents=True)
    budget.used_path().write_text("{ not json", encoding="utf-8")
    assert budget.read_used() == 0, "corrupt meter degrades to 0"
    cap = {"name": "host.comfy", "bridge": True, "cost": 2}
    assert budget.record_stage_cost(cap, 2) == 2, "a corrupt file must not block metering"


def test_reset_used_zeroes_meter_not_budget(node_env) -> None:
    cap = {"name": "host.comfy", "bridge": True, "cost": 4}
    budget.record_stage_cost(cap, 4)
    assert budget.reset_used() == 0
    assert budget.read_used() == 0
    _write_node_yaml(node_env, {"budget": {"total": 50}})
    assert budget.budget_total() == 50, "reset touches the meter, not the budget"


def test_percent_math(node_env) -> None:
    assert budget.percent(50, 100) == 50
    assert budget.percent(0, 100) == 0
    assert budget.percent(7, 100) == 7
    assert budget.percent(150, 100) == 150  # over-budget shows the real P%
    assert budget.percent(5, 0) == 0, "no division by zero"


# ---------------------------------------------------------------------------
# Active-profile mutations (budget.total + busy + slots)
# ---------------------------------------------------------------------------


def test_write_budget_total_creates_mapping_form(node_env) -> None:
    _write_node_yaml(node_env, {"node_name": "n", "capabilities": []})
    ok, msg = active_profile.write_budget_total(42)
    assert ok is True
    doc = _read_node_yaml(node_env)
    assert doc["budget"] == {"total": 42}
    assert doc["node_name"] == "n"
    assert "budget total" in msg


def test_write_budget_total_rejects_negative_untouched(node_env) -> None:
    path = _write_node_yaml(node_env, {"node_name": "n", "capabilities": []})
    before = path.read_text(encoding="utf-8")
    ok, _ = active_profile.write_budget_total(-1)
    assert ok is False
    assert path.read_text(encoding="utf-8") == before


def test_write_budget_total_fails_loud_on_broken_profile(node_env) -> None:
    """A compose that does not validate writes NOTHING (byte-identical)."""
    path = _write_node_yaml(
        node_env, {"capabilities": [{"name": "x", "handler": 5}]}
    )
    before = path.read_text(encoding="utf-8")
    ok, msg = active_profile.write_budget_total(10)
    assert ok is False
    assert "untouched" in msg
    assert path.read_text(encoding="utf-8") == before


def test_write_busy_status_text_edit_keeps_comments(node_env) -> None:
    path = _write_node_yaml(node_env, {"node_name": "n", "capabilities": []})
    path.write_text("# operator comment\ncapabilities: []\nstatus: online\n", encoding="utf-8")
    ok, msg = active_profile.write_busy_status(True)
    assert ok and "busy" in msg
    raw = path.read_text(encoding="utf-8")
    assert "# operator comment" in raw, "the text edit preserves unrelated lines"
    assert "status: busy\n" in raw
    loaded = yaml.safe_load(raw)
    assert loaded["status"] == "busy"
    ok2, _ = active_profile.write_busy_status(False)
    assert ok2
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert loaded["status"] == "idle"


def test_write_busy_status_dump_fallback_when_no_status_line(node_env) -> None:
    """No ``status:`` line yet → load-mutate-dump + full revalidation."""
    _write_node_yaml(node_env, {"node_name": "n", "capabilities": []})
    ok, _ = active_profile.write_busy_status(True)
    assert ok
    assert node_config.load_active_status() == "busy"


def test_write_busy_status_missing_file_refused(node_env) -> None:
    ok, msg = active_profile.write_busy_status(True)
    assert ok is False
    assert "no active node.yaml" in msg


def test_upsert_forward_slot_happy_path(node_env) -> None:
    _write_node_yaml(node_env, {"node_name": "n", "capabilities": []})
    ok, msg = active_profile.upsert_forward_slot(
        "video-gen", {"type": "forward", "label": "Video-Gen"}
    )
    assert ok, msg
    doc = _read_node_yaml(node_env)
    assert _slot_rows(doc)["video-gen"] == {"slot": "video-gen", "type": "forward", "label": "Video-Gen"}
    rows = node_config.validate_profile(doc)  # whole profile still validates
    assert rows == []


def test_upsert_forward_slot_replaces_and_keeps_others(node_env) -> None:
    _write_node_yaml(
        node_env,
        {
            "capabilities": [],
            "forward_slots": {
                "a": {"type": "forward"},
                "old": "legacy description",
            },
        },
    )
    ok, _ = active_profile.upsert_forward_slot("b", {"type": "text", "value": "hi"})
    assert ok
    doc = _read_node_yaml(node_env)
    section = doc["forward_slots"]
    assert section["a"] == {"type": "forward"}
    assert section["b"] == {"type": "text", "value": "hi"}, (
        "the mapping form keys the slot outside the entry (no stray key)"
    )
    assert section["old"] == "legacy description", (
        "legacy description entries are preserved untouched"
    )


def test_upsert_forward_slot_list_form_roundtrip(node_env) -> None:
    """node/config.md documents the LIST form (``- slot: ...``) — the
    define flow mutates the matching row in place, appends otherwise and
    preserves the row order."""
    _write_node_yaml(
        node_env,
        {
            "capabilities": [],
            "forward_slots": [
                {"slot": "a", "type": "forward"},
                {"slot": "c", "type": "bool", "value": True},
            ],
        },
    )
    ok, _ = active_profile.upsert_forward_slot("a", {"type": "forward", "label": "New"})
    assert ok
    ok, _ = active_profile.upsert_forward_slot("b", {"type": "text", "value": "hi"})
    assert ok
    doc = _read_node_yaml(node_env)
    section = doc["forward_slots"]
    assert section[0] == {"slot": "a", "type": "forward", "label": "New"}, (
        "the replaced row keeps its position"
    )
    assert section[1] == {"slot": "c", "type": "bool", "value": True}
    assert section[2] == {"slot": "b", "type": "text", "value": "hi"}
    node_config.validate_profile(doc)

    ok, _ = active_profile.remove_forward_slot("a")
    assert ok
    doc = _read_node_yaml(node_env)
    section = doc["forward_slots"]
    assert [row["slot"] for row in section] == ["c", "b"]
    ok, _ = active_profile.remove_forward_slot("b")
    assert ok
    doc = _read_node_yaml(node_env)
    assert doc["forward_slots"] == [{"slot": "c", "type": "bool", "value": True}], (
        "the last dict row STAYS (emptying by removal stops at the last row)"
    )
    ok, _ = active_profile.remove_forward_slot("c")
    assert ok
    doc = _read_node_yaml(node_env)
    assert "forward_slots" not in doc


def test_upsert_forward_slot_first_define_starts_list_form(node_env) -> None:
    """The first define writes the DOCUMENTED form (node/config.md) —
    the list form."""
    _write_node_yaml(node_env, {"capabilities": []})
    ok, _ = active_profile.upsert_forward_slot("1", {"type": "forward"})
    assert ok
    doc = _read_node_yaml(node_env)
    assert doc["forward_slots"] == [{"slot": "1", "type": "forward"}]


def test_upsert_forward_slot_validates_whole_section_fail_fast(node_env) -> None:
    """T-239: the row pipeline runs over the WHOLE section — a BROKEN
    pre-existing row blocks the define loudly, file untouched."""
    _write_node_yaml(
        node_env,
        {
            "capabilities": [],
            "forward_slots": {
                "a": {"type": "text", "ref_slot": "ghost", "ref_field": "f"},
            },
        },
    )
    before = (node_env / "node.yaml").read_text(encoding="utf-8")
    ok, msg = active_profile.upsert_forward_slot("b", {"type": "forward"})
    assert ok is False
    assert "untouched" in msg
    assert (node_env / "node.yaml").read_text(encoding="utf-8") == before


def test_remove_forward_slot_and_empty_section_removal(node_env) -> None:
    _write_node_yaml(
        node_env,
        {"capabilities": [], "forward_slots": {"a": {"type": "forward"}}},
    )
    ok, _ = active_profile.remove_forward_slot("a")
    assert ok
    doc = _read_node_yaml(node_env)
    assert "forward_slots" not in doc, "an empty section is dropped"


def test_remove_forward_slot_unknown_slot_refused(node_env) -> None:
    _write_node_yaml(
        node_env,
        {"capabilities": [], "forward_slots": {"a": {"type": "forward"}}},
    )
    ok, _ = active_profile.remove_forward_slot("zz")
    assert ok is False


# ---------------------------------------------------------------------------
# Dashboard render + POST routes
# ---------------------------------------------------------------------------


def test_dashboard_renders_budget_busy_and_define_card(node_env) -> None:
    _write_node_yaml(node_env, {"node_name": "n", "capabilities": []})
    budget.record_stage_cost({"name": "h", "bridge": True, "cost": 7}, 7)
    page = onboarding._render_dashboard().decode()
    # used/budget/percent display
    assert "used 7 / budget 100" in page
    assert "(7%)" in page
    assert "/budget/save" in page and "/budget/reset" in page
    # busy switch present, current state rendered
    assert "/busy/toggle" in page
    assert "Busy an" in page
    # define card present with the T-245-typed form fields
    assert "/slots/define" in page
    for field in ("name='slot'", "name='label'", "name='type'", "name='value'",
                  "name='ref_slot'", "name='ref_field'"):
        assert field in page


def test_post_budget_save_writes_node_yaml(node_env) -> None:
    _write_node_yaml(node_env, {"node_name": "n", "capabilities": []})
    status, page = onboarding._handle_dashboard_post(
        "/budget/save", {"budget_total": ["42"]}
    )
    assert status == 200
    assert _read_node_yaml(node_env)["budget"] == {"total": 42}
    assert "budget total" in page.decode()


def test_post_budget_save_rejects_junk(node_env) -> None:
    path = _write_node_yaml(node_env, {"node_name": "n", "capabilities": []})
    for junk in ("-5", "abc", ""):
        status, page = onboarding._handle_dashboard_post(
            "/budget/save", {"budget_total": [junk]}
        )
        assert status == 400
    assert "budget" not in _read_node_yaml(node_env), "no write for junk"
    assert path.exists()


def test_post_budget_reset_zeroes_meter(node_env) -> None:
    budget.record_stage_cost({"name": "h", "bridge": True, "cost": 7}, 7)
    status, page = onboarding._handle_dashboard_post("/budget/reset", {})
    assert status == 200
    assert budget.read_used() == 0
    assert "zurückgesetzt" in page.decode()


def test_post_busy_toggle_persisted_and_heartbeat_carries_it(node_env) -> None:
    _write_node_yaml(node_env, {"node_name": "n", "capabilities": []})
    status, _ = onboarding._handle_dashboard_post("/busy/toggle", {"busy": ["1"]})
    assert status == 200
    assert node_config.load_active_status() == "busy"
    client = RelayClient(dict(META), dict(CFG))
    body = client._build_heartbeat_payload(
        [{"name": "host.x", "bridge": True, "cost": 1}], {}
    )
    assert body["status"] == "busy", "the explicit-busy request rides the heartbeat"

    status, _ = onboarding._handle_dashboard_post("/busy/toggle", {"busy": ["0"]})
    assert status == 200
    assert node_config.load_active_status() == "idle"
    body = client._build_heartbeat_payload([], {})
    assert body["status"] == "idle", "toggle-off reverts the node to idle"


def test_post_busy_toggle_noop_when_already_in_state(node_env) -> None:
    _write_node_yaml(
        node_env, {"node_name": "n", "capabilities": [], "status": "busy"}
    )
    status, page = onboarding._handle_dashboard_post("/busy/toggle", {"busy": ["1"]})
    assert status == 200
    assert "unchanged" in page.decode()


def test_post_slots_define_happy_path(node_env) -> None:
    """THE Ronny test: DEFINE klickt → node.yaml forward_slots erzeugt."""
    _write_node_yaml(node_env, {"node_name": "n", "capabilities": []})
    status, page = onboarding._handle_dashboard_post(
        "/slots/define",
        {"slot": ["video-gen"], "label": ["Video"], "type": ["forward"]},
    )
    assert status == 200, page.decode()
    doc = _read_node_yaml(node_env)
    rows = _slot_rows(doc)
    assert rows["video-gen"]["type"] == "forward"
    assert rows["video-gen"]["label"] == "Video"
    node_config.validate_profile(doc)


def test_post_slots_define_value_slot_with_ref(node_env) -> None:
    _write_node_yaml(node_env, {"node_name": "n", "capabilities": []})
    onboarding._handle_dashboard_post(  # the ref TARGET arrives first
        "/slots/define", {"slot": ["routing"], "type": ["forward"]},
    )
    status, _ = onboarding._handle_dashboard_post(
        "/slots/define",
        {
            "slot": ["model"],
            "type": ["text"],
            "value": ["flux-pro"],
            "ref_slot": ["routing"],
            "ref_field": ["prompt"],
        },
    )
    assert status == 200
    doc = _read_node_yaml(node_env)
    assert _slot_rows(doc)["model"] == {
        "slot": "model",
        "type": "text",
        "value": "flux-pro",
        "ref_slot": "routing",
        "ref_field": "prompt",
    }
    node_config.validate_profile(doc)


def test_post_slots_define_forward_with_value_refused(node_env) -> None:
    _write_node_yaml(node_env, {"node_name": "n", "capabilities": []})
    status, page = onboarding._handle_dashboard_post(
        "/slots/define",
        {"slot": ["s1"], "type": ["forward"], "value": ["not-routable"]},
    )
    assert status == 400
    assert "forward slots must not carry a value" in page.decode()
    assert "forward_slots" not in _read_node_yaml(node_env)


def test_post_slots_define_bool_value(node_env) -> None:
    _write_node_yaml(node_env, {"node_name": "n", "capabilities": []})
    status, _ = onboarding._handle_dashboard_post(
        "/slots/define", {"slot": ["flag"], "type": ["bool"], "value": ["true"]},
    )
    assert status == 200
    assert _slot_rows(_read_node_yaml(node_env))["flag"]["value"] is True


def test_post_slots_delete_removes_defined_slot(node_env) -> None:
    _write_node_yaml(node_env, {"node_name": "n", "capabilities": []})
    onboarding._handle_dashboard_post(
        "/slots/define", {"slot": ["s1"], "type": ["forward"]},
    )
    status, _ = onboarding._handle_dashboard_post("/slots/delete", {"slot": ["s1"]})
    assert status == 200
    assert "forward_slots" not in _read_node_yaml(node_env)


def test_forward_section_define_card_survives_403_panel(node_env) -> None:
    """The mapping PUT is relay-admin-blocked (T-246/T-030 403 blende) —
    the local DEFINE card stays available regardless (no admin role
    involved in writing node.yaml)."""
    _write_node_yaml(node_env, {"node_name": "n", "capabilities": []})
    html = onboarding._forward_section({"forbidden": True})
    assert "keine Admin-Rolle" in html, "the mapping blende stays"
    assert "/slots/define" in html, "the DEFINE card must still render"
    html = onboarding._forward_section({"ok": False, "error": "relay down"})
    assert "nicht abrufbar" in html
    assert "/slots/define" in html


# ---------------------------------------------------------------------------
# Daemons: the completion path meters (poll + SSE)
# ---------------------------------------------------------------------------


def _bridge_result_ok() -> dict:
    return {"status": "completed", "result": {"done": True}, "error": None}


def test_poll_daemon_completion_meters_bridge_cost(node_env, monkeypatch) -> None:
    """A completed bridge task increments the persisted meter (cost>0)."""
    from nodes.common import bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "execute_bridge_task", lambda *a, **k: _bridge_result_ok())
    monkeypatch.setattr(poll_daemon, "run_handler", lambda *a, **k: _bridge_result_ok())
    client = RelayClient(dict(META), dict(CFG))
    client.complete = lambda *a, **k: {"status": "ok"}
    daemon = poll_daemon.Daemon(client, dict(CFG))

    cap = {"name": "host.comfy", "bridge": True, "cost": 3}
    daemon._run_stage(dict(cap), {"stage_id": "s1", "task_id": "t1"})
    assert daemon.tasks_completed == 1
    assert budget.read_used() == 3, "the completed bridge task meters cost 3"

    # the T-046 gate: a non-bridge cap and a free cap do not meter
    from nodes.common.handler_runner import run_handler as _rh  # noqa: F401

    free = {"name": "local.x", "handler": "h", "cost": 9}
    daemon._run_stage(dict(free), {"stage_id": "s2", "task_id": "t2"})
    zero_cost = {"name": "host.free", "bridge": True, "cost": 0}
    daemon._run_stage(dict(zero_cost), {"stage_id": "s3", "task_id": "t3"})
    assert daemon.tasks_completed == 3
    assert budget.read_used() == 3, "only bridge caps with cost>0 count"


def test_sse_daemon_completion_meters_bridge_cost(node_env, monkeypatch) -> None:
    """The SSE daemon's complete path meters too — same T-046 contract."""
    from nodes.common import bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "execute_bridge_task", lambda *a, **k: _bridge_result_ok())
    client = RelayClient(dict(META), dict(CFG))
    client.complete = lambda *a, **k: {"status": "ok"}
    daemon = node_daemon.SseDaemon(client, dict(CFG))

    cap = {"name": "host.comfy", "bridge": True, "cost": 2}
    daemon._run_stage(dict(cap), {"stage_id": "s1", "task_id": "t1"})
    assert daemon.tasks_completed == 1
    assert budget.read_used() == 2


def test_sse_daemon_script_complete_meters_too(node_env, monkeypatch) -> None:
    """T-179 opt-in: a stage completed BY THE SCRIPT counts as a completed
    capability task — it meters as well (T-046: pro Task-Abschluss).
    Non-bridge caps still never meter (the T-046 gate decides)."""
    import httpx

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

    monkeypatch.setattr(
        node_daemon, "run_handler",
        lambda *a, **k: {"status": "completed", "result": {}, "error": None},
    )
    client = RelayClient(dict(META), dict(CFG))
    client.complete = complete_rejected_404
    daemon = node_daemon.SseDaemon(client, dict(CFG))
    cap = {
        "name": "local.auto", "handler": "h", "cost": 5,
        "config": {"complete_by_script": True},
    }
    daemon._run_stage(dict(cap), {"stage_id": "s1", "task_id": "t1"})
    assert daemon.tasks_completed == 1, "script-complete counts as done (T-179)"
    assert budget.read_used() == 0, "non-bridge caps never meter (T-046 gate)"

