"""T-005c: envelope contract, node side (design.md T-005a section 4.2).

result_path_hints are paths RELATIVE TO THE INNER result of the
handler-result envelope ({"status": "completed", "result": X}): flow
unwraps the envelope exactly once, so ${ref.result.<hint>} navigates
directly inside the unveiled object. The validator passes the paths
through unchanged (no prefix rewrite, no "result." pseudo-segment);
the heartbeat publishes them as before (T-004 pass-through, unchanged).
"""
from __future__ import annotations

from unittest.mock import patch

from nodes.common.capability import Capability
from nodes.common.node_config import diff_profiles, validate_profile
from nodes.common.relay_client import RelayClient

META = {
    "node_id": "TESTNODE",
    "node_name": "hints-test",
    "registration_secret": "rs_x",
    "base_url": "http://relay.test:8788",
}
CFG = {"base_url": None, "request_timeout": 5, "heartbeat_interval": 8}


def _profile(hints):
    cap = {
        "name": "chat.ai",
        "type": "ai",
        "claimable": True,
        "handler": "/usr/bin/true",
    }
    if hints is not None:
        cap["result_path_hints"] = hints
    return {"capabilities": [cap]}


def test_validator_passes_relative_hints_through_unchanged():
    """Validator accepts result-relative paths and writes them UNCHANGED
    into the normalized capability — no rewrite, no prefix mandate (D6)."""
    caps = validate_profile(_profile(["answer", "artifact_id", "meta.w"]))
    assert caps[0]["result_path_hints"] == ["answer", "artifact_id", "meta.w"]


def test_capability_dataclass_roundtrip_keeps_relative_hints():
    """Capability.from_dict/to_dict (T-004) preserve the relative paths —
    the contract holds across all three definition sites (dataclass,
    validator, heartbeat)."""
    cap = Capability.from_dict(
        {"name": "chat.ai", "type": "ai", "result_path_hints": ["answer"]}
    )
    assert cap.result_path_hints == ["answer"]
    restored = Capability.from_dict(cap.to_dict())
    assert restored.result_path_hints == ["answer"]


def test_hints_reach_heartbeat_payload_relative():
    """End-to-end node side: profile → normalized caps → heartbeat payload —
    the hints appear result-relative in the cap_status entry (T-004
    pass-through, unchanged; only the SEMANTICS of the paths change)."""
    caps = validate_profile(_profile(["answer"]))
    client = RelayClient(dict(META), dict(CFG))
    with patch("nodes.common.relay_client.os.getloadavg", return_value=(0.5, 0, 0)), \
         patch("nodes.common.relay_client.os.cpu_count", return_value=2), \
         patch("nodes.common.relay_client._read_cgroup_cpu_usage", return_value=None):
        payload = client._build_heartbeat_payload(caps=caps, in_flight={})
    entry = next(c for c in payload["capabilities"] if c["name"] == "chat.ai")
    assert entry["result_path_hints"] == ["answer"]


def test_hint_only_profile_edit_is_diff_silent_accepted_deviation():
    """PINNED (accepted deviation, design.md T-005a section 4.2): A
    pure hint edit shows up in diff_profiles as 'unchanged' because
    _NORMALIZED_KEYS does not compare result_path_hints. Accepted
    because hint changes are pure metadata edits; if this ever shows
    up, the fix is one line in _NORMALIZED_KEYS — deliberately NOT
    part of T-005c (changing node-cli behavior would be out of scope)."""
    old = validate_profile(_profile(None))
    new = validate_profile(_profile(["answer"]))
    diff = diff_profiles(old, new)
    assert diff["added"] == []
    assert diff["removed"] == []
    assert diff["changed"] == []