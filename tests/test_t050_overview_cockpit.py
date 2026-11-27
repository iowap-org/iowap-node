"""T-050 companion (Ronny order): the OVERVIEW is the cockpit.

The dashboard overview (kv-grid) gains a ``Status`` card — busy/idle pill
plus the inline busy toggle — and a ``Budget`` card — used/budget (+%)
plus the inline reset. Contracts proven:

* both cards read the SAME data as the Budget&Busy section below
  (``node.yaml`` status via ``load_active_status()``; meter
  ``<relay-dir>/state/budget_used`` + ``node.yaml`` budget.total);
* both inline forms post to the EXISTING routes (``/busy/toggle``,
  ``/budget/reset``) — no new POST path, persistence + recompose + SIGHUP
  unchanged, and the route table stays byte-identical (drift guard);
* the overview reflects the current state (idle vs explicit busy) and
  still renders when the meter store degrades (missing/corrupt → 0);
* the Budget&Busy section below is untouched (config form + detail lines
  stay — no dedup, the overview is the cockpit).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nodes.common import (  # noqa: E402
    active_profile,
    budget,
    node_config,
    node_utils,
    onboarding,
)


# ---------------------------------------------------------------------------
# The route-table snapshot — the overview adds NO new POST route
# ---------------------------------------------------------------------------

_EXPECTED_POST_ROUTES = (
    "/caps/create",
    "/caps/update",
    "/caps/delete",
    "/caps/update-publish",
    "/caps/auto-publish",
    "/caps/script-upload",
    "/forward/save",
    "/forward/delete",
    "/budget/save",
    "/budget/reset",
    "/busy/toggle",
    "/slots/define",
    "/slots/delete",
    "/mcp/enable",
    "/mcp/disable",
    "/mcp/bearer/rotate",
    "/agents/create",
    "/agents/revoke",
    "/agents/caps-toggle",
)


def test_overview_adds_no_new_post_routes() -> None:
    assert onboarding._DASHBOARD_POST_ROUTES == _EXPECTED_POST_ROUTES


# ---------------------------------------------------------------------------
# Fixtures (t046 pattern — active node.yaml + meter state + render stubs)
# ---------------------------------------------------------------------------


class FakeSupervisor:
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


@pytest.fixture()
def cockpit_env(tmp_path, monkeypatch):
    """Configured node with an ACTIVE node.yaml — the state the overview
    cards read (status via load_active_status, meter via budget module)."""
    relay_dir = tmp_path / "relay"
    relay_dir.mkdir()
    monkeypatch.setattr(node_config, "BASE_DIR", relay_dir)
    monkeypatch.setattr(node_config, "ACTIVE_PATH", relay_dir / "node.yaml")
    monkeypatch.delenv(budget.BUDGET_STATE_ENV, raising=False)
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("IOWAP_MCP_BEARER", raising=False)
    for name in (
        "META_PATH", "TOKEN_PATH", "CONFIG_PATH",
        "LEGACY_META_PATH", "LEGACY_TOKEN_PATH", "STATUS_PATH",
    ):
        monkeypatch.setattr(node_utils, name, tmp_path / f"{name.lower()}.json")
    node_utils.META_PATH.write_text(json.dumps(
        {"node_id": "N1", "node_name": "win-node", "base_url": "http://r:8788"}))
    node_utils.TOKEN_PATH.write_text(json.dumps({"token": "tok-1"}))
    node_utils.CONFIG_PATH.write_text(json.dumps({"base_url": "http://r:8788"}))
    (relay_dir / "node.yaml").write_text("node_name: cockpit-node\n")
    _stub_render_side(monkeypatch)
    return relay_dir


# ---------------------------------------------------------------------------
# Overview cards
# ---------------------------------------------------------------------------


def test_overview_shows_status_toggle_and_budget_cockpit(cockpit_env) -> None:
    page = onboarding._render_dashboard().decode()

    # Status card: idle pill + the inline toggle posting the OPPOSITE state
    assert "<span class='pill ok'><span class='dot'></span>idle</span>" in page
    assert "action='/busy/toggle'" in page
    assert "<input type='hidden' name='busy' value='1'>" in page
    assert "Busy an" in page
    # Budget card: same meter data as the section below + inline reset
    assert "used 0 / budget 100 <strong>(0%)</strong>" in page
    assert "action='/budget/reset'" in page
    # the Budget&Busy section stays (no dedup): config form + reset + toggle
    assert "Budget &amp; Busy" in page
    assert "action='/budget/save'" in page
    assert "Budget speichern" in page


def test_overview_reflects_explicit_busy_state(cockpit_env) -> None:
    ok, _ = active_profile.write_busy_status(True)
    assert ok

    page = onboarding._render_dashboard().decode()

    assert "<span class='pill warn'><span class='dot'></span>busy (explicit)</span>" in page
    assert "<input type='hidden' name='busy' value='0'>" in page
    assert "Busy aus (→ idle)" in page
    assert node_config.load_active_status() == "busy"


def test_overview_toggle_roundtrips_over_the_existing_route(cockpit_env) -> None:
    """The overview button posts exactly the legacy payload — the existing
    /busy/toggle handler (persist → recompose + SIGHUP path) does the work."""
    status, body = onboarding._handle_dashboard_post("/busy/toggle", {"busy": ["1"]})
    text = body.decode()

    assert status == 200
    assert node_config.load_active_status() == "busy"
    assert "busy (explicit)" in text  # the rerendered page shows the flip
    assert "used 0 / budget 100" in text  # cockpit card on the error-free render

    status, _ = onboarding._handle_dashboard_post("/busy/toggle", {"busy": ["0"]})
    assert status == 200
    assert node_config.load_active_status() != "busy"


def test_overview_budget_degrades_with_a_corrupt_meter(cockpit_env) -> None:
    """The 'fallback store metric': unreadable meter → used 0, page fine."""
    used_path = budget.used_path()
    used_path.parent.mkdir(parents=True, exist_ok=True)
    used_path.write_text("definitely not json")

    page = onboarding._render_dashboard().decode()

    assert "used 0 / budget 100 <strong>(0%)</strong>" in page
    assert "action='/budget/reset'" in page  # the card still works
