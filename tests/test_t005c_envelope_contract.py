"""T-005c: Envelope-Contract, Node-Seite (design.md T-005a section 4.2).

result_path_hints sind Pfade RELATIV ZUM INNERNEN result des Handler-Result-
Envelopes ({"status": "completed", "result": X}): flow unwrapt die Huelle
genau einmal, also navigiert ${ref.result.<hint>} direkt im enthuellten
Objekt. Der Validator durchreicht die Pfade unverändert (kein Präfix-Rewrite,
kein "result."-Pseudo-Segment), der Heartbeat veröffentlicht sie wie bisher
(T-004 pass-through, unverändert).
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
    """Validator akzeptiert result-relative Pfade und schreibt sie UNVERÄNDERT
    in die normalisierte Capability — kein Rewrite, kein Präfix-Zwang (D6)."""
    caps = validate_profile(_profile(["answer", "artifact_id", "meta.w"]))
    assert caps[0]["result_path_hints"] == ["answer", "artifact_id", "meta.w"]


def test_capability_dataclass_roundtrip_keeps_relative_hints():
    """Capability.from_dict/to_dict (T-004) erhalten die relativen Pfade —
    Contract gilt über alle drei Definitionstellen (dataclass, Validator,
    Heartbeat) hinweg."""
    cap = Capability.from_dict(
        {"name": "chat.ai", "type": "ai", "result_path_hints": ["answer"]}
    )
    assert cap.result_path_hints == ["answer"]
    restored = Capability.from_dict(cap.to_dict())
    assert restored.result_path_hints == ["answer"]


def test_hints_reach_heartbeat_payload_relative():
    """End-to-end Node-Seite: Profil → normalisierte Caps → Heartbeat-Payload —
    die Hints erscheinen result-relativ im cap_status-Eintrag (T-004
    pass-through, unverändert; nur die SEMANTIK der Pfade ändert sich)."""
    caps = validate_profile(_profile(["answer"]))
    client = RelayClient(dict(META), dict(CFG))
    with patch("nodes.common.relay_client.os.getloadavg", return_value=(0.5, 0, 0)), \
         patch("nodes.common.relay_client.os.cpu_count", return_value=2), \
         patch("nodes.common.relay_client._read_cgroup_cpu_usage", return_value=None):
        payload = client._build_heartbeat_payload(caps=caps, in_flight={})
    entry = next(c for c in payload["capabilities"] if c["name"] == "chat.ai")
    assert entry["result_path_hints"] == ["answer"]


def test_hint_only_profile_edit_is_diff_silent_accepted_deviation():
    """PINNED (akzeptierte Abweichung, design.md T-005a section 4.2): Ein
    reiner Hint-Edit zeigt in diff_profiles als 'unchanged', weil
    _NORMALIZED_KEYS result_path_hints nicht vergleicht. Akzeptiert, weil
    Hint-Änderungen reine Metadaten-Edits sind; falls das je auffällt,
    ist der Fix eine Zeile in _NORMALIZED_KEYS — bewusst NICHT Teil von
    T-005c (Verhaltensänderung an node-cli wäre Out-of-Scope)."""
    old = validate_profile(_profile(None))
    new = validate_profile(_profile(["answer"]))
    diff = diff_profiles(old, new)
    assert diff["added"] == []
    assert diff["removed"] == []
    assert diff["changed"] == []