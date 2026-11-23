"""T-026: reverse workspace channel — adopt ``<workspace>/bridge/daemon.json``.

Contract (board T-026):

* The bridge daemon writes ``{"url", "token"}`` to
  ``<workspace>/bridge/daemon.json`` after a successful bootstrap; the
  node's watcher (10 s poll, like the daemon's T-022 watch) adopts the
  values into the active node.yaml ``bridge:`` block — ONLY ``url`` and
  ``token``, every other field and all non-bridge keys survive — and
  SIGHUPs the daemon pid files (the existing reload path).
* Precedence: ``daemon.json`` ALWAYS wins while it exists — a
  hand-configured bridge url/token in node.yaml is overwritten (logged).
* Robustness: a missing channel file is a no-op, an unparsable/incomplete
  one is ignored with a warning; the watcher never crashes and never
  writes anything when the node has no node.yaml yet.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nodes.common import daemon_channel, node_config, node_utils

NODE_YAML = (
    "node_name: win-node\n"
    "relay: {}\n"
)


@pytest.fixture()
def chan_env(tmp_path, monkeypatch):
    """Isolate every path the watcher touches (dashboard-fixture discipline)."""
    monkeypatch.setattr(node_config, "ACTIVE_PATH", tmp_path / "node.yaml")
    monkeypatch.setattr(node_config, "BASE_DIR", tmp_path / "relay")
    monkeypatch.setattr(node_utils, "BASE_DIR", tmp_path / "relay")
    monkeypatch.setenv("IOWAP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    reloads: list[list[int]] = []
    monkeypatch.setattr(
        daemon_channel, "_reload_daemons", lambda: reloads.append([1])
    )
    return {"tmp": tmp_path, "reloads": reloads}


def write_daemon_json(env, url="http://host.docker.internal:7780", token="tok-123"):
    tmp_path = env["tmp"]
    path = tmp_path / "workspace" / "bridge" / "daemon.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": url, "token": token}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# read_daemon_json
# ---------------------------------------------------------------------------


def test_read_valid_channel(chan_env):
    write_daemon_json(chan_env)
    assert daemon_channel.read_daemon_json() == (
        "http://host.docker.internal:7780", "tok-123",
    )


def test_read_missing_channel_is_none(chan_env):
    assert daemon_channel.read_daemon_json() is None


def test_read_broken_channel_is_none_not_crash(chan_env):
    path = chan_env["tmp"] / "workspace" / "bridge" / "daemon.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert daemon_channel.read_daemon_json() is None
    path.write_text(json.dumps({"url": "http://x"}), encoding="utf-8")
    assert daemon_channel.read_daemon_json() is None  # token missing


# ---------------------------------------------------------------------------
# apply_daemon_json — node.yaml adoption
# ---------------------------------------------------------------------------


def test_apply_creates_bridge_block_preserving_rest(chan_env):
    active = chan_env["tmp"] / "node.yaml"
    active.write_text(NODE_YAML, encoding="utf-8")
    write_daemon_json(chan_env)

    changed, message = daemon_channel.apply_daemon_json()

    assert changed and message == "node.yaml updated"
    doc = yaml.safe_load(active.read_text(encoding="utf-8"))
    assert doc["node_name"] == "win-node"
    assert doc["relay"] == {}
    assert doc["bridge"]["url"] == "http://host.docker.internal:7780"
    assert doc["bridge"]["token"] == "tok-123"
    # only url/token were written — nothing else in the bridge block
    assert set(doc["bridge"]) == {"url", "token"}
    assert chan_env["reloads"] == [[1]]  # reload triggered


def test_apply_updates_existing_block_other_fields_intact(chan_env):
    active = chan_env["tmp"] / "node.yaml"
    active.write_text(
        NODE_YAML
        + "bridge:\n"
        "  url: http://stale:9\n"
        "  token: old\n"
        "  workspace: /workspace\n"
        "  auto_publish: true\n"
        "  allowlist: ['comfy.generate']\n"
        "  poll_interval: 30\n",
        encoding="utf-8",
    )
    write_daemon_json(chan_env)

    changed, _ = daemon_channel.apply_daemon_json()

    assert changed
    block = yaml.safe_load(active.read_text(encoding="utf-8"))["bridge"]
    assert block["url"] == "http://host.docker.internal:7780"
    assert block["token"] == "tok-123"
    # the publish-toggle fields (T-021) and the rest survive untouched
    assert block["workspace"] == "/workspace"
    assert block["auto_publish"] is True
    assert block["allowlist"] == ["comfy.generate"]
    assert block["poll_interval"] == 30


def test_apply_is_idempotent_no_rewrite_no_reload(chan_env):
    active = chan_env["tmp"] / "node.yaml"
    active.write_text(NODE_YAML, encoding="utf-8")
    write_daemon_json(chan_env)
    assert daemon_channel.apply_daemon_json()[0]
    before = active.read_text(encoding="utf-8")

    changed, message = daemon_channel.apply_daemon_json()

    assert not changed and message == "unchanged"
    assert active.read_text(encoding="utf-8") == before
    assert chan_env["reloads"] == [[1]]  # no additional reload


def test_apply_overrides_hand_configured_bridge_url(chan_env):
    """Precedence rule: daemon.json ALWAYS wins while it exists (T-026)."""
    active = chan_env["tmp"] / "node.yaml"
    active.write_text(
        NODE_YAML + "bridge:\n  url: http://manually-configured:1\n  token: hand\n",
        encoding="utf-8",
    )
    write_daemon_json(chan_env)

    changed, _ = daemon_channel.apply_daemon_json()

    assert changed
    block = yaml.safe_load(active.read_text(encoding="utf-8"))["bridge"]
    assert block["url"] == "http://host.docker.internal:7780"
    assert block["token"] == "tok-123"


def test_apply_without_channel_file_is_noop(chan_env):
    active = chan_env["tmp"] / "node.yaml"
    active.write_text(NODE_YAML, encoding="utf-8")

    changed, message = daemon_channel.apply_daemon_json()

    assert not changed and message == "no usable daemon.json"
    assert "bridge" not in yaml.safe_load(active.read_text(encoding="utf-8"))
    assert chan_env["reloads"] == []


def test_apply_without_node_yet_writes_nothing(chan_env):
    write_daemon_json(chan_env)

    changed, message = daemon_channel.apply_daemon_json()

    assert not changed and message == "no active node.yaml yet"
    assert not (chan_env["tmp"] / "node.yaml").exists()
    assert chan_env["reloads"] == []


# ---------------------------------------------------------------------------
# Watcher thread
# ---------------------------------------------------------------------------


def test_watcher_adopts_channel_and_stops_cleanly(chan_env):
    active = chan_env["tmp"] / "node.yaml"
    active.write_text(NODE_YAML, encoding="utf-8")
    stop = threading.Event()
    thread = daemon_channel.start_watcher(stop, interval=1)
    try:
        # the channel file appears only AFTER the watcher started — proves
        # the loop notices an appearing file, not just an existing one
        write_daemon_json(chan_env)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if "bridge" in active.read_text(encoding="utf-8"):
                break
            time.sleep(0.1)
        assert "bridge" in active.read_text(encoding="utf-8")
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_watcher_survives_broken_channel_content(chan_env):
    active = chan_env["tmp"] / "node.yaml"
    active.write_text(NODE_YAML, encoding="utf-8")
    path = chan_env["tmp"] / "workspace" / "bridge" / "daemon.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("garbage", encoding="utf-8")

    # direct cycle (what _watch_loop catches exceptions around anyway)
    changed, _ = daemon_channel.apply_daemon_json()
    assert not changed
    assert active.exists() and "bridge" not in active.read_text(encoding="utf-8")
