"""T-036: capability EDIT in place + version bump (create → edit, not
delete→recreate).

Contract (board T-036, brief 2026-11-25):

* providers.yaml entries carry an optional ``version``: ``X.Y.Z`` (regex
  ``^\\d+\\.\\d+\\.\\d+$``), empty/missing → the entry carries ``1.0.0``
  (the default is VISIBLE in the YAML). Garbage of any shape is a
  validation error and writes NOTHING (T-021 rule: the whole write is
  discarded on error — same for update).
* ``dashboard.update_provider(name, fields)``: entry per name, missing →
  ``capability '...' not found``. The name is the IMMUTABLE routing key
  (relay identity + forward mappings — rename stays delete+create);
  a mismatched ``fields["name"]`` is rejected with the file untouched.
  Field validation is the create ``_validate_provider_fields`` contract,
  reused (no duplicate validation code). After a successful merge the
  version AUTO-BUMPS on the patch level — the only exception is an
  explicit valid version that DIFFERS from the stored one (it applies
  verbatim); an explicit version equal to the stored one still bumps
  (the contract is "every edit changes the version"). The write is
  atomic, in place (same list position), and the message carries the
  new version (``update ok — <name> vX.Y.Z``).
* ``bridge.build_bridge_cap`` passes the entry version through (default
  1.0.0) instead of hardcoding it — version flows manifest → heartbeat →
  relay upsert (relay-side already handles ``capability_version``).
* Publish state lives in node.yaml ``bridge.allowlist`` (NOT per entry)
  — an update must not rip the entry's bridge-block counterpart apart.
* UI: GET ``/caps/edit?name=...`` renders the existing create form
  prefilled (name readonly, hidden ``__name__``); POST ``/caps/update``
  follows the create flash pattern and the route list carries it.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nodes.common import bridge, dashboard, node_config, node_utils, onboarding


@pytest.fixture()
def dash_env(tmp_path, monkeypatch):
    """Isolate every state path the dashboard touches (see test_t030)."""
    for name in (
        "META_PATH", "TOKEN_PATH", "CONFIG_PATH",
        "LEGACY_META_PATH", "LEGACY_TOKEN_PATH", "STATUS_PATH",
    ):
        monkeypatch.setattr(node_utils, name, tmp_path / f"{name.lower()}.json")
    monkeypatch.setattr(node_config, "ACTIVE_PATH", tmp_path / "node.yaml")
    monkeypatch.setattr(node_config, "BASE_DIR", tmp_path / "relay")
    monkeypatch.setenv("IOWAP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    return tmp_path


_BASE = {"type": "process", "command": "run x", "summary": "old summary"}


def _fresh_entry(dash_env, name: str = "a.cap", **extra) -> Path:
    """Create one capability and return the providers.yaml path."""
    fields = {"name": name, **_BASE, **extra}
    ok, msg = dashboard.create_provider(fields)
    assert ok, msg
    return dashboard.providers_path()


# 1 — update happy: field change lands in providers.yaml + auto-bump 1.0.0→1.0.1


def test_update_happy_changes_field_and_bumps_version(dash_env):
    path = _fresh_entry(dash_env)
    ok, msg = dashboard.update_provider(
        "a.cap", {"name": "a.cap", "type": "process", "command": "run x",
                  "summary": "NEW TEXT"})
    assert ok, msg
    assert msg == "update ok — a.cap v1.0.1"
    entry = yaml.safe_load(path.read_text())["providers"][0]
    assert entry["summary"] == "NEW TEXT"
    assert entry["version"] == "1.0.1"
    assert entry["name"] == "a.cap"  # position/name stable (edit in place)


# 2 — auto-bump accumulates: two edits without explicit version → 1.0.2


def test_update_bumps_version_on_every_edit(dash_env):
    path = _fresh_entry(dash_env)
    fields = {"name": "a.cap", "type": "process", "command": "run x"}
    ok, msg = dashboard.update_provider("a.cap", fields)
    assert ok and msg.endswith("v1.0.1"), msg
    ok, msg = dashboard.update_provider("a.cap", fields)
    assert ok and msg.endswith("v1.0.2"), msg  # never falls back to 1.0.0
    entry = yaml.safe_load(path.read_text())["providers"][0]
    assert entry["version"] == "1.0.2"


# 3 — explicit version in the form (different) → exactly that, no bump


def test_update_explicit_version_wins_without_bump(dash_env):
    path = _fresh_entry(dash_env)
    ok, msg = dashboard.update_provider(
        "a.cap", {"name": "a.cap", "type": "process", "command": "run x",
                  "version": "1.2.3"})
    assert ok and msg.endswith("v1.2.3"), msg
    entry = yaml.safe_load(path.read_text())["providers"][0]
    assert entry["version"] == "1.2.3"


# 4 — explicit IDENTICAL version still bumps ("every edit changes the version")


def test_update_explicit_identical_version_still_bumps(dash_env):
    path = _fresh_entry(dash_env)  # entry version = 1.0.0
    ok, msg = dashboard.update_provider(
        "a.cap", {"name": "a.cap", "type": "process", "command": "run x",
                  "version": "1.0.0"})
    assert ok and msg.endswith("v1.0.1"), msg
    entry = yaml.safe_load(path.read_text())["providers"][0]
    assert entry["version"] == "1.0.1"


# 5 — name immutable: reject, file byte-identical, rename hint in the msg


def test_update_name_is_immutable_and_file_untouched(dash_env):
    path = _fresh_entry(dash_env)
    before = path.read_bytes()
    ok, msg = dashboard.update_provider(
        "a.cap", {"name": "b.cap", "type": "process", "command": "run x"})
    assert not ok
    assert "immutable" in msg and "delete" in msg, msg
    assert path.read_bytes() == before


# 6 — validation garbage (max_parallel -5) → error, write discarded entirely


def test_update_validation_error_writes_nothing(dash_env):
    path = _fresh_entry(dash_env)
    before = path.read_bytes()
    ok, msg = dashboard.update_provider(
        "a.cap", {"name": "a.cap", "type": "process", "command": "run x",
                  "max_parallel": "-5"})
    assert not ok
    assert "max_parallel" in msg, msg
    assert path.read_bytes() == before, "failed validation must write NOTHING"


# 7 — unknown name → not-found error


def test_update_unknown_name_not_found(dash_env):
    path = _fresh_entry(dash_env)
    ok, msg = dashboard.update_provider(
        "nope.cap", {"name": "nope.cap", "type": "process", "command": "x"})
    assert not ok
    assert "not found" in msg and "nope.cap" in msg, msg


# 8 — create with empty version → entry carries version: 1.0.0 (visible in
#     YAML) and the manifest passthrough receives 1.0.0


def test_create_empty_version_defaults_to_1_0_0_visible_and_passthrough(dash_env):
    path = _fresh_entry(dash_env, "v.cap", type="http",
                        url_template="http://127.0.0.1:9000/x", version="")
    raw = path.read_text()
    assert "version: 1.0.0" in raw, "default must be visible in the YAML"
    entry = yaml.safe_load(raw)["providers"][0]
    assert entry["version"] == "1.0.0"
    cap = bridge.build_bridge_cap("v.cap", entry)
    assert cap["version"] == "1.0.0"


# 9 — bridge passthrough: entry version 2.5.7 → manifest entry "2.5.7"
#     (no hardcode; garbage falls back to the default, T-030 style)


def test_bridge_passthrough_version_from_entry():
    cap = bridge.build_bridge_cap("sysinfo", {"summary": "s", "version": "2.5.7"})
    assert cap["version"] == "2.5.7"
    # garbage shapes never poison the heartbeat
    for bad in (1.5, True, "2.5", "v2.5.7", "2.5.7.8", None, 257):
        assert bridge.build_bridge_cap("sysinfo", {"version": bad})["version"] == "1.0.0"


# 10 — publish state (node.yaml bridge.allowlist, not per-entry) survives
#      an update; the bridge:-block is not touched by update_provider


def test_published_state_and_bridge_block_survive_update(dash_env):
    _fresh_entry(dash_env)
    # a REAL bridge block (url set) so the publish gate is live, not disabled
    node_config.ACTIVE_PATH.write_text(yaml.safe_dump({
        "bridge": {"url": "http://relay:8788", "token": "t0k", "workspace": "/ws"},
    }))
    ok, msg = dashboard.set_auto_publish(True)
    assert ok, msg
    ok, msg = dashboard.set_capability_published("a.cap", True)
    assert ok, msg
    node_before = (node_config.ACTIVE_PATH).read_bytes()
    doc_before = yaml.safe_load(node_before)
    ok, msg = dashboard.update_provider(
        "a.cap", {"name": "a.cap", "type": "process", "command": "run x",
                  "summary": "edited"})
    assert ok, msg
    entry = yaml.safe_load(dashboard.providers_path().read_text())["providers"][0]
    assert entry["summary"] == "edited"
    assert entry["version"] == "1.0.1"
    # T-045: the store edit NOW recomposes the active profile — the
    # published cap is re-baked with the bumped version and the bridge
    # block (allowlist/auto_publish/url/token/workspace) survives:
    doc_after = yaml.safe_load((node_config.ACTIVE_PATH).read_text())
    assert doc_after["bridge"] == doc_before["bridge"]
    assert doc_after["bridge"]["allowlist"] == ["a.cap"]
    assert doc_after["bridge"]["auto_publish"] is False  # one-shot migrated
    baked = [c for c in doc_after["capabilities"] if c.get("bridge")]
    assert [(c["name"], c["version"]) for c in baked] == [("host.a.cap", "1.0.1")]
    assert dashboard.is_published(
        "a.cap", bridge.load_bridge_config()) is True


# ---------------------------------------------------------------------------
# UI wiring: GET /caps/edit (prefill) + POST /caps/update (flash pattern)
# ---------------------------------------------------------------------------


def _configure(dash_env):
    """Make the node look configured (meta + token + relay config)."""
    node_utils.META_PATH.write_text(json.dumps(
        {"node_id": "N1", "node_name": "win-node", "base_url": "http://r:8788"}))
    node_utils.TOKEN_PATH.write_text(json.dumps({"token": "tok-1"}))
    node_utils.CONFIG_PATH.write_text(json.dumps({"base_url": "http://r:8788"}))


def _get(server, path: str):
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_address[1]}{path}", timeout=10
        ) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _post(server, path: str, form: dict[str, str]):
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}{path}",
        data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_caps_edit_route_renders_prefilled_readonly_form(dash_env):
    _configure(dash_env)
    _fresh_entry(dash_env, "a.cap", summary="Edit me", max_parallel="3", cost="2")
    srv = onboarding.start_dashboard(port=0)
    try:
        status, html = _get(srv, "/caps/edit?name=a.cap")
        assert status == 200, html
        assert "Edit capability a.cap" in html
        assert "name='__name__' value='a.cap'" in html
        assert "readonly value='a.cap'" in html
        assert "action='/caps/update'" in html
        assert "value='Edit me'" in html  # summary prefilled
        assert "Update capability" in html
        assert "value='3'" in html  # max_parallel prefilled
        assert "value='2'" in html  # cost prefilled
        # plain dashboard still renders the CREATE form
        status, html2 = _get(srv, "/")
        assert status == 200
        assert "action='/caps/create'" in html2
        assert "name='__name__'" not in html2
        # unknown name → dashboard rendering an error, not a crash
        status, html3 = _get(srv, "/caps/edit?name=ghost.cap")
        assert status == 404
        assert "ghost.cap" in html3
    finally:
        onboarding.shutdown(srv)


def test_caps_update_post_bumps_and_flashes(dash_env):
    _configure(dash_env)
    _fresh_entry(dash_env, "a.cap", summary="old")
    path = dashboard.providers_path()
    before = path.read_bytes()
    srv = onboarding.start_dashboard(port=0)
    try:
        form = {
            "__name__": "a.cap", "name": "a.cap", "type": "process",
            "command": "run x", "summary": "flash me",
        }
        status, html = _post(srv, "/caps/update", form)
        assert status == 200, html
        assert "update ok — a.cap v1.0.1" in html  # create-style flash
        entry = yaml.safe_load(path.read_text())["providers"][0]
        assert entry["summary"] == "flash me" and entry["version"] == "1.0.1"
        # a name mismatch (readonly bypass) is rejected and writes NOTHING
        before2 = path.read_bytes()
        bad = dict(form, name="evil.cap")
        status, html = _post(srv, "/caps/update", bad)
        assert status == 400
        assert "immutable" in html
        assert path.read_bytes() == before2
    finally:
        onboarding.shutdown(srv)
