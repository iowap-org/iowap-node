"""T-239: ``forward_slots`` must be validated when a profile is
validated/published — fail-fast at the operator, not at the first
heartbeat tick.

Audit F3: ``validate_profile`` (node_config.py:478-530) never touched
``forward_slots`` (the schema root is ``additionalProperties: true``)
and ``publish_profile`` (:581-613) runs the row pipeline only over the
PREVIOUS block (the heal, T-235 C3). The first checker for the SOURCE
section was ``load_forward_slots()`` inside the daemon heartbeat —
``node-cli capabilities publish`` therefore returned OK, and afterwards
every heartbeat tick died BEFORE the POST (node_cli.py:255-256 /
node_daemon.py:344-345): the node sat silent at the relay while the
operator had just seen "OK".

Fix: the shared row pipeline (
``_forward_slot_items`` → ``_build_forward_slot_row`` →
``_crosscheck_forward_slot_refs``) now runs inside ``validate_profile``
for any declared ``forward_slots`` section — malformed or
typed-invalid rows raise ``CapabilityValidationError`` there, so
``validate`` and ``publish`` fail loudly at the operator.

DESIGN DECISION (degradation path): NO. The alternative — keep
heartbeating with the LAST VALID slot list plus a warning — would
require a last-known-good cache next to the active file plus a policy
for "profile says X, node sends Y-1" at the relay (the server replaces
its declaration rows from the heartbeat). The simple fail-fast at
publish + the T-237 status-file error surface give the operator the
honest state for a fraction of the complexity. Degradation is recorded
as a TODO concept in node_config (T-239-note), not implemented.
"""

from __future__ import annotations

import pytest

from nodes.common import node_cli, node_config, node_daemon
from nodes.common.node_config import (
    CapabilityValidationError,
    publish_profile,
    validate_profile,
)

WORKING_PROFILE = """\
version: 1
profile: default
capabilities:
  - name: probe.echo
    handler: /usr/bin/env
    max_parallel: 1
    timeout: 60
    claimable: true
    input_schema: {}
forward_slots:
  "1":
    type: forward
    label: Wartungsslot
"""


def _write_broken(yaml_text: str, path) -> None:
    path.write_text(yaml_text, encoding="utf-8")


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
    monkeypatch.setattr(node_config, "ensure_node_cli_stamp", lambda *a, **k: None)
    return tmp_path


# --- fail-fast validation -----------------------------------------------------


def test_validate_profile_rejects_broken_forward_slots(relay_home):
    """An unknown slot_type in the declaration is a CapabilityValidationError
    with a forward_slots message — not an invisible OK."""
    doc = {
        "capabilities": [
            {
                "name": "probe.echo",
                "handler": "/usr/bin/env",
                "input_schema": {},
            }
        ],
        "forward_slots": {"1": {"type": "Forward"}},
    }
    with pytest.raises(CapabilityValidationError) as excinfo:
        validate_profile(doc)
    assert "forward_slots" in str(excinfo.value)


def test_validate_profile_file_rejects_broken_forward_slots(relay_home):
    """Same failure for the file source (the publish path's input)."""
    src = relay_home / "profiles.d" / "default.yaml"
    src.parent.mkdir()
    _write_broken(
        WORKING_PROFILE + '  "2":\n    type: Forward\n    label: broken\n', src
    )
    with pytest.raises(CapabilityValidationError) as excinfo:
        publish_profile("default")
    message = str(excinfo.value)
    assert "forward_slots" in message
    assert "unknown slot type 'Forward'" in message


def test_publish_with_broken_forward_slots_leaves_active_untouched(relay_home):
    """publish must not activate a profile it could not validate — the
    active file and the profile pointer stay at the previous state."""
    active = relay_home / "node.yaml"
    active.write_text(WORKING_PROFILE, encoding="utf-8")
    pointer = relay_home / "node.profile"
    pointer.write_text("default\n", encoding="utf-8")

    src = relay_home / "profiles.d" / "next.yaml"
    src.parent.mkdir()
    _write_broken(
        WORKING_PROFILE.replace('name: probe.echo', 'name: probe.echo2') + '  "9":\n    type: Nonsense\n',
        src,
    )
    with pytest.raises(CapabilityValidationError):
        publish_profile("next")

    assert active.read_text(encoding="utf-8") == WORKING_PROFILE
    assert pointer.read_text(encoding="utf-8") == "default\n"


def test_publish_cli_reports_failed_instead_of_ok(relay_home, monkeypatch, capsys):
    """End-to-end through the CLI: ``capabilities publish`` with a broken
    declaration prints 'publish FAILED' and exits 1 — not OK."""
    src = relay_home / "profiles.d" / "default.yaml"
    src.parent.mkdir()
    _write_broken(WORKING_PROFILE + '  "2":\n    type: Forward\n', src)

    from nodes.common.cli import cli_capabilities

    ns = type("NS", (), {"profile": "default", "log_level": "WARNING"})()
    rc = cli_capabilities._cmd_capabilities_publish(ns)

    out = capsys.readouterr()
    assert out.err.startswith("publish FAILED:")
    assert "forward_slots" in out.err
    assert rc == 1
    assert not (relay_home / "node.yaml").exists()


# --- contract locks (valid declaration keeps working) -------------------------


def test_validate_and_publish_with_valid_forward_slots_ok(relay_home):
    """A valid typed declaration validates and publishes WITHOUT regression
    (T-235 C3 heal + parser round-trip stay intact)."""
    src = relay_home / "profiles.d" / "default.yaml"
    src.parent.mkdir()
    src.write_text(WORKING_PROFILE, encoding="utf-8")

    active = publish_profile("default")

    assert active == relay_home / "node.yaml"
    assert node_config.load_forward_slots() == [
        {
            "slot": "1",
            "label": "Wartungsslot",
            "description": None,
            "type": "forward",
            "value": None,
            "ref_slot": None,
            "ref_field": None,
            "input_schema": None,
        }
    ]


def test_validate_profile_without_forward_slots_stays_ok(relay_home):
    """Profiles without the section validate unchanged."""
    doc = {"capabilities": [{"name": "probe.echo", "handler": "/usr/bin/env"}]}
    assert validate_profile(doc) != []
    assert node_config.validate_profile({}) == []