"""T-016h: bridge prefix must be idempotent — no host.host.* capabilities.

The relay stored the capability as "host.host.sysinfo": the operator
typed the name WITH the host. prefix in the dashboard (the published
name is all they ever see), and _apply_manifest unconditionally added
BRIDGE_PREFIX again. Names are normalized to bare form before
publishing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".."))

from nodes.common import bridge  # noqa: E402


def _manager() -> bridge.BridgeManager:
    return bridge.BridgeManager()


def test_prefixed_manifest_name_not_doubled() -> None:
    cfg = {"url": "http://x", "token": "t", "allowlist": ["sysinfo"], "auto_publish": False}
    m = _manager()
    manifest = {"version": 1, "capabilities": [
        {"name": "host.sysinfo", "health": "ok", "type": "process",
         "input_schema": {"type": "object"}},
    ]}
    m._apply_manifest(cfg, manifest)
    assert "host.sysinfo" in m.published
    assert not any(n.startswith("host.host.") for n in m.published)


def test_bare_manifest_name_gets_prefix() -> None:
    cfg = {"url": "http://x", "token": "t", "allowlist": ["sysinfo"], "auto_publish": False}
    m = _manager()
    manifest = {"version": 1, "capabilities": [
        {"name": "sysinfo", "health": "ok", "type": "process",
         "input_schema": {"type": "object"}},
    ]}
    m._apply_manifest(cfg, manifest)
    assert "host.sysinfo" in m.published


def test_allowlist_matches_bare_name_for_prefixed_manifest() -> None:
    # the dashboard allowlist stores the bare name as the operator typed it
    cfg = {"url": "http://x", "token": "t", "allowlist": ["sysinfo"], "auto_publish": False}
    m = _manager()
    manifest = {"version": 1, "capabilities": [
        {"name": "host.sysinfo", "health": "ok", "type": "process",
         "input_schema": {"type": "object"}},
    ]}
    m._apply_manifest(cfg, manifest)
    assert "host.sysinfo" in m.published
