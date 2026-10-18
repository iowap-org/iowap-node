"""T-Forward-Node: forward-slot submit surface (client prep).

Pins the frozen contracts of ``.hermes/pipeline/t-forward-node/design.md``
(§3.1 wire shape, §3.2 module contract, §3.4 CLI behavior; test map §8):
slot normalization mirrors the deployed relay (core/forward.py), a slot
forces capability ``forward`` and rides inside ``payload["forward_slot"]``,
the no-slot submit body stays identical to the pre-T-Forward shape, the
profile validator refuses the ``forward`` pseudo-capability (never
advertised), and relay refusal errors surface verbatim with actionable
hints (no retry). RED→GREEN per stubs.md §3.
"""

from __future__ import annotations

import argparse
import json
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from nodes.common.cli import cli_task
from nodes.common.forward import (
    FORWARD_CAPABILITY,
    FORWARD_ERROR_HINTS,
    ForwardSlotError,
    apply_forward_slot,
    forward_hint,
    normalize_forward_slot,
)
from nodes.common.node_config import CapabilityValidationError, validate_profile
from nodes.common.relay_client import RelayClient

META = {
    "node_id": "TESTNODE",
    "node_name": "forward-test",
    "registration_secret": "rs_x",
    "base_url": "http://relay.test:8788",
}
CFG = {"base_url": None, "request_timeout": 5, "heartbeat_interval": 8}


def _args(stage: str, forward_slot: Any = None, **overrides: Any) -> argparse.Namespace:
    """argparse.Namespace for ``task submit`` (fields used by _cmd_task_submit)."""
    base: dict[str, Any] = {
        "stage": stage,
        "forward_slot": forward_slot,
        "name": "",
        "priority": 0,
        "owner": None,
        "json": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _submit_via_fake_relay(
    cap: str, payload: dict[str, Any], **kwargs: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """submit_simple_task against a patched _post_with_retry; capture the wire body."""
    captured: dict[str, Any] = {}

    def fake_post(path: str, body: dict[str, Any], **_kw: Any) -> httpx.Response:
        captured["path"] = path
        captured["body"] = body
        return httpx.Response(
            200,
            json={"task_id": "t1", "stage_id": "s1", "status": "pending"},
            request=httpx.Request("POST", "http://relay.test:8788" + path),
        )

    with patch.object(RelayClient, "_post_with_retry", side_effect=fake_post):
        client = RelayClient(dict(META), dict(CFG))
        resp = client.submit_simple_task(cap, payload, **kwargs)
    return resp, captured


# --------------------------------------------------------------------------
# Client level — submit_simple_task wire shape (design.md §3.1/§3.3)
# --------------------------------------------------------------------------


def test_submit_with_forward_slot_sends_forward_capability_and_payload_slot():
    resp, captured = _submit_via_fake_relay(
        "artifact-processing", {"x": 1}, forward_slot="2"
    )
    assert resp == {"task_id": "t1", "stage_id": "s1", "status": "pending"}
    assert captured["path"] == "/relay/v2/scheduler/task-simple"
    assert captured["body"]["capability"] == "forward"
    assert captured["body"]["payload"]["forward_slot"] == "2"
    assert captured["body"]["payload"]["x"] == 1


def test_submit_without_forward_slot_body_unchanged():
    _resp, captured = _submit_via_fake_relay("chat.ai", {"p": "v"})
    assert captured["body"] == {
        "capability": "chat.ai",
        "payload": {"p": "v"},
        "name": "",
        "priority": 0,
    }
    assert "forward_slot" not in captured["body"]["payload"]


def test_forward_flag_wins_over_payload_carried_key():
    _resp, captured = _submit_via_fake_relay(
        "agent.ai", {"forward_slot": "old"}, forward_slot="2"
    )
    assert captured["body"]["capability"] == "forward"
    assert captured["body"]["payload"]["forward_slot"] == "2"


def test_submit_does_not_mutate_caller_payload():
    with_slot = {"a": 1}
    _submit_via_fake_relay("agent.ai", with_slot, forward_slot="2")
    assert with_slot == {"a": 1}
    without_slot = {"b": 2}
    _submit_via_fake_relay("agent.ai", without_slot)
    assert without_slot == {"b": 2}


# --------------------------------------------------------------------------
# Pure helpers — nodes.common.forward (design.md §3.2)
# --------------------------------------------------------------------------


def test_normalize_forward_slot_mirrors_server_rules():
    assert normalize_forward_slot(" 1 ") == "1"
    assert normalize_forward_slot(2) == "2"
    assert normalize_forward_slot(0) == "0"
    assert normalize_forward_slot("s-2_x") == "s-2_x"
    bad_values = [True, False, None, [], {}, "", "  ", "a b", "x" * 65, "a.b", "ä"]
    for bad in bad_values:
        with pytest.raises(ForwardSlotError):
            normalize_forward_slot(bad)


def test_forward_slot_error_carries_invalid_code():
    with pytest.raises(ForwardSlotError) as ei:
        normalize_forward_slot("a b")
    assert ei.value.code == "forward_mapping_invalid"
    assert str(ei.value).startswith("forward_mapping_invalid: ")


def test_apply_forward_slot_forces_pseudo_capability():
    cap, payload = apply_forward_slot("agent.ai", {"q": 1}, "3")
    assert cap == FORWARD_CAPABILITY
    assert payload == {"q": 1, "forward_slot": "3"}


def test_apply_forward_slot_noop_without_slot():
    caller = {"q": 1}
    cap, returned = apply_forward_slot("agent.ai", caller)
    assert cap == "agent.ai"
    assert returned == {"q": 1}
    assert returned is not caller
    returned["extra"] = True
    assert caller == {"q": 1}


# --------------------------------------------------------------------------
# CLI level — _cmd_task_submit (design.md §3.4)
# --------------------------------------------------------------------------


def test_cmd_submit_local_encoding_via_flag():
    client = MagicMock()
    client.submit_simple_task.return_value = {
        "task_id": "t1", "stage_id": "s1", "status": "pending",
    }
    args = _args(f"artifact-processing:{json.dumps({'prompt': 'hi'})}", forward_slot="2")
    rc = cli_task._cmd_task_submit(client, args)
    assert rc == 0
    client.submit_simple_task.assert_called_once()
    call = client.submit_simple_task.call_args
    assert call.args[0] == "forward"
    assert call.args[1] == {"prompt": "hi", "forward_slot": "2"}
    assert call.kwargs == {"name": "", "priority": 0, "owner_node_id": None}


def test_cmd_submit_refuses_forward_without_slot():
    client = MagicMock()
    with pytest.raises(SystemExit) as ei:
        cli_task._cmd_task_submit(client, _args("forward:{}", forward_slot=None))
    client.submit_simple_task.assert_not_called()
    assert "refusing to submit capability 'forward' without a slot" in str(ei.value)


def test_cmd_submit_refuses_malformed_slot_locally():
    client = MagicMock()
    with pytest.raises(SystemExit) as ei:
        cli_task._cmd_task_submit(client, _args("agent.ai:{}", forward_slot="a b"))
    client.submit_simple_task.assert_not_called()
    assert "invalid --forward-slot 'a b'" in str(ei.value)
    assert "allowed: 1..64 chars of A-Za-z0-9_-" in str(ei.value)


def test_cmd_submit_renders_relay_error_verbatim_with_hint(capsys):
    client = MagicMock()
    detail = "forward_mapping_missing: no mapping for node 'TESTNODE' slot '1'"
    request = httpx.Request("POST", "http://relay.test:8788/relay/v2/scheduler/task-simple")
    response = httpx.Response(422, json={"detail": detail}, request=request)
    client.submit_simple_task.side_effect = httpx.HTTPStatusError(
        "Client error '422 Unprocessable Entity'", request=request, response=response
    )
    rc = cli_task._cmd_task_submit(client, _args("agent.ai:{}", forward_slot="1"))
    assert rc == 1
    err = capsys.readouterr().err
    assert detail in err
    assert f"hint: {FORWARD_ERROR_HINTS['forward_mapping_missing']}" in err


def test_cmd_submit_no_hint_for_unknown_error_code(capsys):
    client = MagicMock()
    request = httpx.Request("POST", "http://relay.test:8788/relay/v2/scheduler/task-simple")
    response = httpx.Response(404, json={"detail": "Task not found"}, request=request)
    client.submit_simple_task.side_effect = httpx.HTTPStatusError(
        "Client error '404 Not Found'", request=request, response=response
    )
    rc = cli_task._cmd_task_submit(client, _args("agent.ai:{}", forward_slot=None))
    assert rc == 1
    err = capsys.readouterr().err
    assert "Task not found" in err
    assert "hint:" not in err


def test_parse_relay_error_shapes():
    request = httpx.Request("POST", "http://relay.test:8788/x")
    body_resp = httpx.Response(
        422,
        json={"detail": "forward_mapping_missing: no mapping for node 'X' slot '1'"},
        request=request,
    )
    assert cli_task._parse_relay_error(body_resp) == (
        "forward_mapping_missing",
        "no mapping for node 'X' slot '1'",
    )
    raw_resp = httpx.Response(500, text="boom", request=request)
    assert cli_task._parse_relay_error(raw_resp) == (None, "boom")
    nonmatch = httpx.Response(404, json={"detail": "Task not found"}, request=request)
    assert cli_task._parse_relay_error(nonmatch) == (None, "Task not found")
    empty = httpx.Response(503, text="", request=request)
    assert cli_task._parse_relay_error(empty) == (None, "")


def test_forward_hint_table_covers_plan_codes():
    for code in (
        "forward_mapping_missing",
        "forward_mapping_invalid",
        "forward_node_unknown",
        "target_unknown_capability",
        "redirect_self",
    ):
        hint = forward_hint(code)
        assert hint == FORWARD_ERROR_HINTS[code]
        assert hint  # non-empty operator guidance
    assert forward_hint("unknown_code") is None
    assert forward_hint("") is None


# --------------------------------------------------------------------------
# Profile guard + heartbeat invariant (design.md §3.5, D-11)
# --------------------------------------------------------------------------


def test_profile_validation_refuses_forward_capability():
    with pytest.raises(CapabilityValidationError) as ei:
        validate_profile(
            {"capabilities": [{"name": "forward", "type": "task", "handler": "/bin/true"}]}
        )
    assert "'forward' is the relay's forward-routing pseudo-capability" in str(ei.value)
    caps = validate_profile(
        {
            "capabilities": [
                {"name": "chat.ai", "type": "ai", "claimable": True, "handler": "/bin/true"}
            ]
        }
    )
    assert [c["name"] for c in caps] == ["chat.ai"]


def test_heartbeat_never_advertises_forward():
    client = RelayClient(dict(META), dict(CFG))
    caps = validate_profile(
        {
            "capabilities": [
                {"name": "chat.ai", "type": "ai", "claimable": True, "handler": "/bin/true"}
            ]
        }
    )
    payload = client._build_heartbeat_payload(caps, {})
    names = [entry["name"] for entry in payload["capabilities"]]
    assert "forward" not in names
    assert names == ["chat.ai"]