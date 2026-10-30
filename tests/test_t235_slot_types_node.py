"""T-235: typed forward-slot declarations in the node profile.

Covers four frozen contracts from ``.hermes/pipeline/t235-slot-types-node/design.md``:

* C1 — the client slot-type registry mirror in ``nodes/common/forward.py``
  (normalizer rules are word-exact mirrors of the deployed server's
  ``core/forward.py``; only the error class/code differ).
* C2 — ``load_forward_slots()`` rows carry the frozen 8-key form
  (``slot``, ``label``, ``description``, ``type``, ``value``, ``ref_slot``,
  ``ref_field``, ``input_schema``) and stay ``ValueError``-loud.
* C3 — the publish self-heal (Option A): the byte-exact copy loses the
  ``forward_slots`` section, so ``publish_profile`` restores it from the
  previous active file — never raising, never creating, source wins.
* C4 — the heartbeat transport carries the typed rows verbatim in BOTH
  daemon classes (parity is mandatory — T-233 finding).

RED phase: before green wiring, the non-guard tests fail on real error
paths (``NotImplementedError`` stubs, dict-shape/did-not-raise/heal-loss
assertions) — never on import or setup errors. No test touches ~/.relay.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest
import yaml

from nodes.common import node_cli, node_config, node_daemon
from nodes.common.forward import (
    DEFAULT_SLOT_TYPE,
    ForwardSlotError,
    SLOT_TYPES,
    SLOT_TYPE_BOOL,
    SLOT_TYPE_FORWARD,
    SLOT_TYPE_TEXT,
    normalize_slot_ref,
    normalize_slot_type,
    normalize_slot_value,
)
from nodes.common.node_config import load_forward_slots, publish_profile
from nodes.common.relay_client import RelayClient

META = {
    "node_id": "235NODE",
    "node_name": "typed-slots",
    "registration_secret": "rs_x",
    "base_url": "http://relay.test:8788",
}
CFG = {"base_url": None, "request_timeout": 5, "heartbeat_interval": 1}

# ---------------------------------------------------------------------------
# frozen fixtures (design §4)
# ---------------------------------------------------------------------------

TYPED_TEXT_ROW = {
    "slot": "1",
    "label": "Pump 1",
    "description": "d",
    "type": "text",
    "value": "probe",
    "ref_slot": None,
    "ref_field": None,
    "input_schema": None,
}

BOOL_ROW = {
    "slot": "s2",
    "label": None,
    "description": None,
    "type": "bool",
    "value": False,
    "ref_slot": None,
    "ref_field": None,
    "input_schema": None,
}

FORWARD_ROW = {
    "slot": "1",
    "label": None,
    "description": None,
    "type": "forward",
    "value": None,
    "ref_slot": None,
    "ref_field": None,
    "input_schema": None,
}

MAPPING_TYPED_SOURCE = """\
forward_slots:
  "1": {label: "Pump 1", description: "d", type: "text", value: "probe"}
"""

PLAIN_SOURCE = """\
# plain operator profile
node_name: T235NODE
capabilities: []
"""

TYPED_SOURCE = """\
# typed operator profile
node_name: T235NODE
capabilities: []
forward_slots:
  "1": {label: "Pump 1", description: "d", type: "text", value: "probe"}
"""


@pytest.fixture()
def relay_home(tmp_path, monkeypatch):
    """Isolate every relay state path; tests never touch ~/.relay."""
    monkeypatch.setattr(node_config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(node_config, "PROFILES_DIR", tmp_path / "profiles.d")
    monkeypatch.setattr(node_config, "ACTIVE_PATH", tmp_path / "node.yaml")
    monkeypatch.setattr(
        node_config, "ACTIVE_PROFILE_NAME_PATH", tmp_path / "node.profile"
    )
    monkeypatch.setattr(node_cli, "PID_PATH", tmp_path / "node-cli.pid")
    monkeypatch.setattr(node_daemon, "PID_PATH", tmp_path / "node-daemon.pid")
    monkeypatch.setattr(node_cli, "STATUS_PATH", tmp_path / "node-cli.status")
    monkeypatch.setattr(node_daemon, "STATUS_PATH", tmp_path / "node.status")
    return tmp_path


@pytest.fixture()
def no_stamp(monkeypatch):
    """Neutralize the T-226 stamp so publish assertions isolate the C3 heal.

    Stamp coexistence with the heal is pinned separately (test 35).
    """
    monkeypatch.setattr(node_config, "ensure_node_cli_stamp", lambda *a, **k: None)


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _write_profile(name: str, text: str) -> None:
    node_config.PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    _write(node_config.PROFILES_DIR / f"{name}.yaml", text)


def _run_one_heartbeat(daemon) -> None:
    """One heartbeat tick on a thread (t233:37-41 pattern)."""
    thread = threading.Thread(target=daemon._heartbeat_loop, daemon=True)
    thread.start()
    thread.join(timeout=3)
    assert not thread.is_alive()


# ---------------------------------------------------------------------------
# Group R — registry constants (guard, GREEN at stub)
# ---------------------------------------------------------------------------


def test_slot_type_registry_mirrors_server():
    """The 5 registry names mirror the server byte-exactly (T-245 C1)."""
    assert SLOT_TYPE_FORWARD == "forward"
    assert SLOT_TYPE_TEXT == "text"
    assert SLOT_TYPE_BOOL == "bool"
    assert DEFAULT_SLOT_TYPE is SLOT_TYPE_FORWARD
    assert SLOT_TYPES == frozenset({"forward", "text", "bool"})
    assert isinstance(SLOT_TYPES, frozenset)


# ---------------------------------------------------------------------------
# Group N — normalizers (RED: NotImplementedError out of the stubs)
# ---------------------------------------------------------------------------


def test_normalize_slot_type_missing_defaults_to_forward():
    """None/absent → DEFAULT_SLOT_TYPE (server core/forward.py:104-105)."""
    assert normalize_slot_type(None) == "forward"


def test_normalize_slot_type_exact_no_case_folding():
    """Known types pass exact (strip allowed); mixed case is an error."""
    assert normalize_slot_type("text") == "text"
    assert normalize_slot_type("bool") == "bool"
    assert normalize_slot_type("forward") == "forward"
    assert normalize_slot_type(" text ") == "text"
    for bad in ("Forward", "TEXT", "Text"):
        with pytest.raises(ForwardSlotError):
            normalize_slot_type(bad)


def test_normalize_slot_type_rejects_unknown_empty_and_mistyped():
    """Unknown/empty/mistyped declarations are errors (server :107-118)."""
    with pytest.raises(ForwardSlotError) as excinfo:
        normalize_slot_type("uri")
    assert "unknown slot type 'uri'" in excinfo.value.message
    assert "(known: bool, forward, text)" in excinfo.value.message
    for bad in ("", "   ", True, False, 1, []):
        with pytest.raises(ForwardSlotError):
            normalize_slot_type(bad)


def test_normalize_slot_type_error_code_forward_slot_invalid():
    """Declaration errors carry code forward_slot_invalid and stay ValueError."""
    with pytest.raises(ForwardSlotError) as excinfo:
        normalize_slot_type(1)
    assert excinfo.value.code == "forward_slot_invalid"
    assert str(excinfo.value).startswith("forward_slot_invalid: ")
    assert isinstance(excinfo.value, ValueError)


def test_normalize_slot_value_text_rules():
    """text slot values must be str (or None) (server :140-145)."""
    assert normalize_slot_value("text", "probe") == "probe"
    assert normalize_slot_value("text", None) is None
    with pytest.raises(ForwardSlotError):
        normalize_slot_value("text", 5)


def test_normalize_slot_value_bool_rules():
    """bool slot values must be a REAL bool — never coerce (server :146-152)."""
    assert normalize_slot_value("bool", True) is True
    assert normalize_slot_value("bool", False) is False
    assert normalize_slot_value("bool", None) is None
    for bad in ("true", "false", 1, 0):
        with pytest.raises(ForwardSlotError):
            normalize_slot_value("bool", bad)


def test_normalize_slot_value_forbidden_on_forward():
    """A forward slot must not carry a value (D-02, server :153-158)."""
    for bad in ("", "x", 0):
        with pytest.raises(ForwardSlotError) as excinfo:
            normalize_slot_value("forward", bad)
        assert "forward slots must not carry a 'value'" in excinfo.value.message
    assert normalize_slot_value("forward", None) is None


def test_normalize_slot_ref_missing_returns_none():
    """Absent refs normalize to None on every type (server :178-187/196)."""
    for slot_type in ("forward", "text", "bool"):
        assert normalize_slot_ref({}, slot_type) is None
        assert normalize_slot_ref({"ref_slot": None, "ref_field": None}, slot_type) is None


def test_normalize_slot_ref_requires_whole_pair():
    """Half refs are loud (server :188-195, non-str ref_field = absent)."""
    with pytest.raises(ForwardSlotError) as half_slot:
        normalize_slot_ref({"ref_slot": "1"}, "text")
    with pytest.raises(ForwardSlotError) as half_field:
        normalize_slot_ref({"ref_field": "name"}, "text")
    with pytest.raises(ForwardSlotError) as half_type:
        normalize_slot_ref({"ref_slot": "1", "ref_field": None}, "text")
    for excinfo in (half_slot, half_field, half_type):
        assert "a ref must declare both ref_slot and ref_field together" in excinfo.value.message
    assert normalize_slot_ref({"ref_slot": "1", "ref_field": "name"}, "text") == ("1", "name")


def test_normalize_slot_ref_forbidden_on_forward():
    """Forward slots carry no ref — the target lives in forward_mappings."""
    with pytest.raises(ForwardSlotError) as excinfo:
        normalize_slot_ref({"ref_slot": "1", "ref_field": "name"}, "forward")
    assert "forward slots must not carry a ref" in excinfo.value.message
    assert "forward_mappings" in excinfo.value.message
    assert normalize_slot_ref({"ref_slot": None, "ref_field": None}, "forward") is None


def test_normalize_slot_ref_slot_uses_slot_charset_rules():
    """ref_slot runs the normalize_forward_slot rules, remapped code (A5)."""
    with pytest.raises(ForwardSlotError) as dotted:
        normalize_slot_ref({"ref_slot": "a.b", "ref_field": "x"}, "text")
    assert dotted.value.code == "forward_slot_invalid"
    with pytest.raises(ForwardSlotError) as blank:
        normalize_slot_ref({"ref_slot": "  ", "ref_field": "x"}, "text")
    assert blank.value.code == "forward_slot_invalid"
    assert normalize_slot_ref({"ref_slot": 2, "ref_field": "x"}, "text") == ("2", "x")


def test_forward_slot_error_stays_valueerror():
    """ForwardSlotError stays a ValueError; default code unchanged (A5)."""
    err = ForwardSlotError("declaration broken", code="forward_slot_invalid")
    assert isinstance(err, ValueError)
    assert err.code == "forward_slot_invalid"
    assert str(err) == "forward_slot_invalid: declaration broken"
    default = ForwardSlotError("mapping broken")
    assert default.code == "forward_mapping_invalid"
    assert str(default) == "forward_mapping_invalid: mapping broken"


# ---------------------------------------------------------------------------
# Group P — parser row pipeline (RED: the old body ignores typed fields)
# ---------------------------------------------------------------------------


def test_load_forward_slots_mapping_form_text_slot_full_row(relay_home):
    """Mapping-form text slot → the exact frozen 8-key row.

    RED: the old body builds only slot/label/description (dict mismatch).
    """
    _write(node_config.ACTIVE_PATH, MAPPING_TYPED_SOURCE)
    assert load_forward_slots() == [TYPED_TEXT_ROW]


def test_load_forward_slots_list_form_bool_native(relay_home):
    """List-form bool slot → native False value (never the string)."""
    _write(
        node_config.ACTIVE_PATH,
        'forward_slots:\n  - slot: "s2"\n    type: "bool"\n    value: false\n',
    )
    rows = load_forward_slots()
    assert len(rows) == 1
    row = rows[0]
    assert row["type"] == "bool"
    assert row["value"] is False
    assert dict(row) == BOOL_ROW


def test_load_forward_slots_missing_type_resolves_forward(relay_home):
    """Bestandsschutz: a declaration without type resolves to forward."""
    _write(node_config.ACTIVE_PATH, 'forward_slots:\n  "1": {label: "X", description: "Y"}\n')
    rows = load_forward_slots()
    assert rows == [
        {
            "slot": "1",
            "label": "X",
            "description": "Y",
            "type": "forward",
            "value": None,
            "ref_slot": None,
            "ref_field": None,
            "input_schema": None,
        }
    ]


def test_load_forward_slots_mapping_scalar_legacy_form(relay_home):
    """Legacy mapping scalar (entry = description string) keeps its shape."""
    _write(node_config.ACTIVE_PATH, 'forward_slots:\n  "1": "some desc"\n')
    rows = load_forward_slots()
    row = rows[0]
    assert row["description"] == "some desc"
    assert row["label"] is None
    assert row["type"] == "forward"


def test_load_forward_slots_rejects_unknown_type(relay_home):
    """An unknown declared type must be loud (RED: did not raise)."""
    _write(node_config.ACTIVE_PATH, 'forward_slots:\n  "1": {type: "uri", label: "L"}\n')
    with pytest.raises(ForwardSlotError) as excinfo:
        load_forward_slots()
    assert "unknown slot type 'uri'" in excinfo.value.message
    assert "(known: bool, forward, text)" in excinfo.value.message


def test_load_forward_slots_rejects_mistyped_type(relay_home):
    """Non-registry type strings (incl. case-folds and non-str) are loud."""
    for value in ('"TEXT"', '""', '"   "', "true", "1"):
        _write(
            node_config.ACTIVE_PATH,
            f'forward_slots:\n  "1": {{type: {value}, label: "L"}}\n',
        )
        with pytest.raises(ValueError):
            load_forward_slots()


def test_load_forward_slots_rejects_value_on_forward(relay_home):
    """A value on an (explicit or implicit) forward slot is a D-02 error."""
    _write(node_config.ACTIVE_PATH, 'forward_slots:\n  "1": {value: "x"}\n')
    with pytest.raises(ForwardSlotError) as excinfo:
        load_forward_slots()
    assert "forward slots must not carry a 'value'" in excinfo.value.message
    assert str(excinfo.value).startswith("forward_slot_invalid: ")
    _write(node_config.ACTIVE_PATH, 'forward_slots:\n  "1": {type: "forward", value: "x"}\n')
    with pytest.raises(ForwardSlotError):
        load_forward_slots()


def test_load_forward_slots_rejects_bool_string_value(relay_home):
    """A bool slot rejects the string "true" and the int 1 (never coerce)."""
    for value in ('"true"', "1"):
        _write(
            node_config.ACTIVE_PATH,
            f'forward_slots:\n  "s": {{type: "bool", value: {value}}}\n',
        )
        with pytest.raises(ForwardSlotError):
            load_forward_slots()


def test_load_forward_slots_rejects_half_and_dangling_refs(relay_home):
    """Half refs, unknown targets, value-slot targets and refs on forward
    slots must all fail loud (RED: did not raise)."""
    cases = (
        # (a) half ref (ref_slot without ref_field)
        'forward_slots:\n  "s": {type: "text", ref_slot: "1"}\n',
        # (b) ref to an unknown slot
        'forward_slots:\n  "t": {type: "text", ref_slot: "ghost", ref_field: "name"}\n',
        # (c) ref targeting another VALUE slot (text → text)
        'forward_slots:\n  "1": {type: "text"}\n'
        '  "t": {type: "text", ref_slot: "1", ref_field: "name"}\n',
        # (d) ref carried by a forward slot
        'forward_slots:\n  "1": {type: "forward", ref_slot: "x", ref_field: "n"}\n',
    )
    for text in cases:
        _write(node_config.ACTIVE_PATH, text)
        with pytest.raises(ForwardSlotError):
            load_forward_slots()


def test_load_forward_slots_valid_ref_pair_roundtrip(relay_home):
    """A whole ref pair to a forward slot survives the roundtrip."""
    _write(
        node_config.ACTIVE_PATH,
        'forward_slots:\n'
        '  "1": {label: "Route"}\n'
        '  "t": {type: "text", ref_slot: "1", ref_field: "name"}\n',
    )
    rows = load_forward_slots()
    by_slot = {row["slot"]: row for row in rows}
    assert by_slot["t"]["ref_slot"] == "1"
    assert by_slot["t"]["ref_field"] == "name"
    assert by_slot["1"]["ref_slot"] is None
    assert by_slot["1"]["ref_field"] is None


def test_load_forward_slots_rejects_bad_input_schema(relay_home):
    """input_schema is forward-only and dict-only (server :414-430)."""
    # (a) input_schema on a text slot → forward-only violation
    _write(node_config.ACTIVE_PATH, 'forward_slots:\n  "s": {type: "text", input_schema: {a: 1}}\n')
    with pytest.raises(ForwardSlotError) as excinfo:
        load_forward_slots()
    assert "input_schema is only allowed on forward slots" in excinfo.value.message
    # (b) list-shaped input_schema on a forward slot → dict-only violation
    _write(node_config.ACTIVE_PATH, 'forward_slots:\n  "1": {input_schema: [a]}\n')
    with pytest.raises(ForwardSlotError) as excinfo2:
        load_forward_slots()
    assert "input_schema must be an object" in excinfo2.value.message


def test_load_forward_slots_input_schema_passthrough_native(relay_home):
    """A dict input_schema passes through native and unvalidated."""
    _write(
        node_config.ACTIVE_PATH,
        'forward_slots:\n  "1": {input_schema: {tags: ["a"], depth: 2}}\n',
    )
    rows = load_forward_slots()
    assert rows == [
        {
            "slot": "1",
            "label": None,
            "description": None,
            "type": "forward",
            "value": None,
            "ref_slot": None,
            "ref_field": None,
            "input_schema": {"tags": ["a"], "depth": 2},
        }
    ]


def test_load_forward_slots_duplicate_slot_last_wins(relay_home):
    """Duplicate slots dedupe last-wins, first-occurrence position (A3)."""
    _write(
        node_config.ACTIVE_PATH,
        'forward_slots:\n'
        '  - slot: "s"\n'
        '    type: "text"\n'
        '    value: "a"\n'
        '  - slot: "s"\n'
        '    type: "bool"\n'
        "    value: false\n",
    )
    rows = load_forward_slots()
    assert len(rows) == 1
    row = rows[0]
    assert row["type"] == "bool"
    assert row["value"] is False
    assert dict(row) == {
        "slot": "s",
        "label": None,
        "description": None,
        "type": "bool",
        "value": False,
        "ref_slot": None,
        "ref_field": None,
        "input_schema": None,
    }


def test_load_forward_slots_legacy_shapes_unchanged(relay_home):
    """(a) no file, (b) no section, (c) bad shape, (d) mapping key None."""
    # (a) file missing → []
    assert load_forward_slots() == []
    # (b) file without the section → []
    _write(node_config.ACTIVE_PATH, "node_name: X\n")
    assert load_forward_slots() == []
    # (c) non-mapping/non-list section → frozen message
    _write(node_config.ACTIVE_PATH, 'forward_slots: "x"\n')
    with pytest.raises(ValueError, match=r"^forward_slots must be a mapping or a list of objects$"):
        load_forward_slots()
    # (d) mapping key None → slot required (frozen message)
    _write(node_config.ACTIVE_PATH, 'forward_slots:\n  ~: {label: x}\n')
    with pytest.raises(ValueError, match=r"^forward_slots entries require a slot$"):
        load_forward_slots()


def test_load_forward_slots_explicit_empty_declaration(relay_home):
    """"forward_slots: []" is a legitimate removal marker → [] rows."""
    _write(node_config.ACTIVE_PATH, "forward_slots: []\n")
    assert load_forward_slots() == []


# ---------------------------------------------------------------------------
# Group S — publish self-heal, Option A (RED: the heal is missing)
# ---------------------------------------------------------------------------


def test_publish_source_with_section_stays_byte_exact(relay_home, no_stamp):
    """A source carrying the section is copied byte-exact (source wins)."""
    _write_profile("default", TYPED_SOURCE)
    publish_profile("default")
    active = node_config.ACTIVE_PATH
    assert active.read_text(encoding="utf-8") == TYPED_SOURCE
    assert load_forward_slots() == [TYPED_TEXT_ROW]


def test_publish_restores_section_missing_in_source(relay_home, no_stamp):
    """A section lost to the byte-exact copy is restored from the previous
    active file (heal), keeping the loader roundtrip and the source body."""
    _write_profile("default", PLAIN_SOURCE)
    _write(node_config.ACTIVE_PATH, TYPED_SOURCE)
    publish_profile("default")
    active = node_config.ACTIVE_PATH
    text = active.read_text(encoding="utf-8")
    # (a) the section is back in the active text
    assert "forward_slots" in text
    # (b) end-to-end: the healed file round-trips through the loader
    assert load_forward_slots() == [TYPED_TEXT_ROW]
    # (c) the copied source body is preserved verbatim
    assert "# plain operator profile" in text
    assert "capabilities: []" in text


def test_publish_heal_idempotent_across_publishes(relay_home, no_stamp):
    """Heal re-applies on every plain publish; exactly one section each."""
    _write_profile("typed", TYPED_SOURCE)
    _write_profile("plain", PLAIN_SOURCE)
    publish_profile("typed")
    publish_profile("plain")
    publish_profile("plain")
    text = node_config.ACTIVE_PATH.read_text(encoding="utf-8")
    assert text.count("forward_slots:") == 1
    assert load_forward_slots() == [TYPED_TEXT_ROW]


def test_publish_never_creates_section(relay_home, no_stamp):
    """No section anywhere → no heal write (never-create discipline)."""
    _write_profile("default", PLAIN_SOURCE)
    _write(node_config.ACTIVE_PATH, PLAIN_SOURCE)
    returned = publish_profile("default")
    active = node_config.ACTIVE_PATH
    assert "forward_slots" not in active.read_text(encoding="utf-8")
    assert returned == active
    # Edge: no previous active file at all (fresh register host).
    os.remove(active)
    publish_profile("default")
    assert "forward_slots" not in active.read_text(encoding="utf-8")


def test_publish_source_wins_explicit_empty_declaration(relay_home, no_stamp):
    """A source-side explicit [] (removal marker) is NOT re-healed."""
    _write_profile("default", "node_name: T235NODE\ncapabilities: []\nforward_slots: []\n")
    _write(node_config.ACTIVE_PATH, TYPED_SOURCE)
    publish_profile("default")
    parsed = yaml.safe_load(node_config.ACTIVE_PATH.read_text(encoding="utf-8"))
    assert parsed["forward_slots"] == []


def test_publish_heal_skips_malformed_or_broken_previous(relay_home, no_stamp):
    """A broken previous section is warn-and-skipped, never re-healed."""
    # (a) previously valid YAML, invalid TYPED section (unknown type)
    _write_profile("default", PLAIN_SOURCE)
    _write(
        node_config.ACTIVE_PATH,
        'node_name: T235NODE\ncapabilities: []\nforward_slots:\n  "1": {type: "uri"}\n',
    )
    returned = publish_profile("default")
    active = node_config.ACTIVE_PATH
    assert returned == active
    assert "forward_slots" not in active.read_text(encoding="utf-8")
    # (b) corrupted previous active YAML (syntax error)
    _write(node_config.ACTIVE_PATH, "forward_slots: [unclosed\n")
    publish_profile("default")
    parsed = yaml.safe_load(active.read_text(encoding="utf-8"))
    assert "forward_slots" not in parsed


def test_publish_heal_keeps_restamp_working(relay_home, tmp_path, monkeypatch):
    """Heal and the T-226 restamp coexist in one publish (stamp untested
    elsewhere in this file — this one keeps it REAL)."""
    _write_profile("default", PLAIN_SOURCE)
    _write(
        node_config.ACTIVE_PATH,
        'node_cli_path: "/stale/node-cli"\n' + TYPED_SOURCE,
    )
    venv = tmp_path / "fakevenv"
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True)
    script = bin_dir / "node-cli"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    py = bin_dir / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(py))
    monkeypatch.setattr(sys, "argv", [str(py), "node-cli"])

    publish_profile("default")

    active = node_config.ACTIVE_PATH
    assert load_forward_slots() == [TYPED_TEXT_ROW]
    expected = os.path.realpath(venv / "bin" / "node-cli")
    text = active.read_text(encoding="utf-8")
    stamp_line = text.splitlines(keepends=True)[0]
    assert stamp_line == f'node_cli_path: "{expected}"\n'


# ---------------------------------------------------------------------------
# Group H — heartbeat transport, BOTH daemon classes (real loader)
# ---------------------------------------------------------------------------


def test_cli_daemon_heartbeat_transports_typed_rows(relay_home, monkeypatch):
    """CLI daemon heartbeat carries the loader's typed 8-key rows verbatim."""
    _write(node_config.ACTIVE_PATH, TYPED_SOURCE)
    client = RelayClient(dict(META), dict(CFG))
    daemon = node_cli.Daemon(client, dict(CFG))
    monkeypatch.setattr(client, "maintenance_due", lambda: False)
    monkeypatch.setattr(node_cli, "load_active_profile", lambda: [])

    seen = []

    def fake_heartbeat(caps, inflight, forward_slots=None):
        seen.append(forward_slots)
        daemon._stop_event.set()
        return {"status": "ok"}

    monkeypatch.setattr(client, "heartbeat", fake_heartbeat)

    _run_one_heartbeat(daemon)

    assert seen == [[TYPED_TEXT_ROW]]


def test_sse_daemon_heartbeat_transports_typed_rows(relay_home, monkeypatch):
    """SSE daemon parity (T-233 finding): identical typed rows."""
    _write(node_config.ACTIVE_PATH, TYPED_SOURCE)
    client = RelayClient(dict(META), dict(CFG))
    daemon = node_daemon.SseDaemon(client, dict(CFG))
    monkeypatch.setattr(client, "maintenance_due", lambda: False)
    monkeypatch.setattr(node_daemon, "load_active_profile", lambda: [])

    seen = []

    def fake_heartbeat(caps, inflight, forward_slots=None):
        seen.append(forward_slots)
        daemon._stop_event.set()
        return {"status": "ok"}

    monkeypatch.setattr(client, "heartbeat", fake_heartbeat)

    _run_one_heartbeat(daemon)

    assert seen == [[TYPED_TEXT_ROW]]


def test_heartbeat_carries_forward_slots_when_loaded(relay_home):
    """C4 → updated by T-240: a LOADED EMPTY declaration now ships as an
    explicit ``[]`` (server treats an empty list as 'clear the rows');
    non-empty keeps shipping the rows verbatim. Before T-240 the
    truthiness gate dropped the empty key, so stale server rows kept
    routing to a target the node no longer serves."""
    client = RelayClient(dict(META), dict(CFG))
    client.forward_slots = []
    body = client._build_heartbeat_payload([], {})
    assert body["forward_slots"] == []
    row = dict(TYPED_TEXT_ROW)
    client.forward_slots = [row]
    body2 = client._build_heartbeat_payload([], {})
    assert body2["forward_slots"] == [row]