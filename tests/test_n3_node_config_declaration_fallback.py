"""N3 (audit 2026-10-31, F3 degradation): a broken active-file
``forward_slots`` declaration degrades with a WARNING + the last valid
list instead of killing the heartbeat silently.

The T-239 TODO left the degradation openly designed (audit report one-
liner option: loader ValueError → warn + last valid list — the client
carries rows since T-240). Implemented in ``load_forward_slots``:
ValueError from the row pipeline → warn loud + re-serve the last valid
declaration for the active path; without a prior valid load the
ValueError contract stays loud (pinned by test_t235), and a removed
section carries nothing forward (never a stale resurrection).
"""

from __future__ import annotations

import logging

import pytest

from nodes.common import node_config

VALID_TEXT_DECL = (
    'forward_slots:\n  "1": {label: "Pump 1", type: "text", value: "probe"}\n'
)
VALID_BOOL_DECL = 'forward_slots:\n  "2": {label: "Other", type: "bool", value: true}\n'
BROKEN_DECL = 'forward_slots:\n  "1": {type: "uri", label: "L"}\n'
CLEARED_DECL = "forward_slots: []\n"
NO_SECTION_DECL = "node_name: X\n"

TEXT_ROW = {
    "slot": "1",
    "label": "Pump 1",
    "description": None,
    "type": "text",
    "value": "probe",
    "ref_slot": None,
    "ref_field": None,
    "input_schema": None,
}
BOOL_ROW = {
    "slot": "2",
    "label": "Other",
    "description": None,
    "type": "bool",
    "value": True,
    "ref_slot": None,
    "ref_field": None,
    "input_schema": None,
}


@pytest.fixture()
def active_path(tmp_path, monkeypatch):
    """Isolate just the path the loader reads (loader-test shape of
    test_t235's relay_home)."""
    p = tmp_path / "node.yaml"
    monkeypatch.setattr(node_config, "ACTIVE_PATH", p)
    return p


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    """N3 tests must not see fallback entries from other tests in the same
    pytest process (t235/t239/t240 run alongside). ``raising=False`` keeps
    the pre-fix RED honest: on unfixed code the fixture is a no-op and the
    fallback tests fail at their behavior assertions, not at setup."""
    monkeypatch.setattr(node_config, "_LAST_VALID_FORWARD_SLOTS", {}, raising=False)


def test_broken_declaration_warns_and_serves_last_valid_list(active_path, caplog):
    """The finding case: valid load → file breaks → warn + carried rows,
    not a silent omission and not a dead heartbeat tick."""
    active_path.write_text(VALID_TEXT_DECL)
    assert node_config.load_forward_slots() == [TEXT_ROW]

    active_path.write_text(BROKEN_DECL)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="relay.node_config"):
        rows = node_config.load_forward_slots()

    assert rows == [TEXT_ROW]  # the LAST VALID declaration, not the broken one
    assert any(
        rec.levelno == logging.WARNING
        and "forward_slots" in rec.message
        and "falling" in rec.message
        for rec in caplog.records
    ), caplog.record_tuples


def test_broken_declaration_without_prior_valid_load_stays_loud(active_path):
    """No prior valid load → ValueError contract unchanged (fresh paths,
    publish-time checks and test_t235 keep their loud behavior)."""
    active_path.write_text(BROKEN_DECL)
    with pytest.raises(ValueError):
        node_config.load_forward_slots()


def test_fallback_follows_the_current_valid_declaration(active_path, caplog):
    """After a FIXED (re-declared) declaration loads, the fallback serves
    the NEW list — never a stale first-load resurrection."""
    active_path.write_text(VALID_TEXT_DECL)
    assert node_config.load_forward_slots() == [TEXT_ROW]
    active_path.write_text(VALID_BOOL_DECL)
    assert node_config.load_forward_slots() == [BOOL_ROW]

    active_path.write_text(BROKEN_DECL)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        rows = node_config.load_forward_slots()
    assert rows == [BOOL_ROW]


def test_cleared_declaration_carries_empty_not_stale_rows(active_path, caplog):
    """``forward_slots: []`` is a legitimate removal marker (T-240): after
    it, a broken file degrades to [] — the server replace keeps the rows
    cleared instead of resurrecting the old list."""
    active_path.write_text(VALID_TEXT_DECL)
    assert node_config.load_forward_slots() == [TEXT_ROW]
    active_path.write_text(CLEARED_DECL)
    assert node_config.load_forward_slots() == []

    active_path.write_text(BROKEN_DECL)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        rows = node_config.load_forward_slots()
    assert rows == []
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


def test_removed_section_has_no_stale_fallback(active_path):
    """A profile WITHOUT the section carries nothing forward: a later
    broken declaration is loud again — no resurrection of removed slots."""
    active_path.write_text(VALID_TEXT_DECL)
    assert node_config.load_forward_slots() == [TEXT_ROW]
    active_path.write_text(NO_SECTION_DECL)
    assert node_config.load_forward_slots() == []

    active_path.write_text(BROKEN_DECL)
    with pytest.raises(ValueError):
        node_config.load_forward_slots()


def test_fallback_returns_copies_not_cached_cells(active_path):
    """The fallback returns copies — callers cannot poison the carried
    cache (the same rows must survive consecutive fallback loads)."""
    active_path.write_text(VALID_TEXT_DECL)
    first = node_config.load_forward_slots()
    active_path.write_text(BROKEN_DECL)
    warned = node_config.load_forward_slots()
    warned[0]["label"] = "mutated"
    again = node_config.load_forward_slots()
    assert again == first
    assert again[0]["label"] == "Pump 1"