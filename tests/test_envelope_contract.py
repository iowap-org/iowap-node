"""T-005b: handler_runner Normalize-Choke-Point (Envelope-Contract, T-005a design.md §3).

Jeder Test-Contract kommt aus design.md §3.1–§3.4:
- Response-Normalize: conforming pass-through, bare-wrap, bare-error
  pass-through, fail-fast bei status:error, Fehlerfälle.
- Request-Hülle: {task_id, capability, input:{...}} + tolerante Spiegelung
  (config.envelope_request_mirror).
- Kein Verhalten-Change für konforme Handler (hermes-handler.py Form).

Deviation-Tests (dokumentiert in design.md ## Deviations):
- Wrap omits the "error" key (daemon counts failures by key presence).
- node_config forwards a per-capability "config" mapping (validator +
  normalizer), the carrier of config.envelope_request_mirror.
"""
from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from nodes.common import handler_runner, node_config

# --- §3.1 Response-Normalize -------------------------------------------------

def test_normalize_conforming_envelope_passes_through() -> None:
    text = json.dumps(
        {"status": "completed", "result": {"answer": "ok"}, "error": None}
    )
    assert handler_runner._normalize_result(text) == {
        "status": "completed",
        "result": {"answer": "ok"},
        "error": None,
    }


def test_normalize_conforming_envelope_extra_keys_pass_through() -> None:
    # hermes-handler.py shape: extra "debug" key, no "error" key at all.
    envelope = {
        "status": "completed",
        "result": {"answer": "ok"},
        "debug": {"stderr": "", "stdout_length": 2},
    }
    text = json.dumps(envelope)
    assert handler_runner._normalize_result(text) == envelope


def test_normalize_bare_result_is_wrapped() -> None:
    text = json.dumps({"answer": 42})
    assert handler_runner._normalize_result(text) == {
        "status": "completed",
        "result": {"answer": 42},
    }


def test_normalize_bare_error_keyed_result_passes_through() -> None:
    text = json.dumps({"error": "disk full"})
    assert handler_runner._normalize_result(text) == {"error": "disk full"}


def test_normalize_bare_dict_with_result_key_but_no_status_is_wrapped() -> None:
    # design.md §3.1 edge: no "status" key means bare — wrap (D4).
    text = json.dumps({"result": {"nested": True}})
    assert handler_runner._normalize_result(text) == {
        "status": "completed",
        "result": {"result": {"nested": True}},
    }


def test_normalize_error_status_fails_fast_message() -> None:
    text = json.dumps({"status": "error", "error": "Hermes timed out after 280s"})
    assert handler_runner._normalize_result(text) == {
        "status": "error",
        "error": "Hermes timed out after 280s",
    }


def test_normalize_invalid_status_value_is_error() -> None:
    text = json.dumps({"status": "ok", "stdout": "{}"})
    result = handler_runner._normalize_result(text)
    assert result["error"] == (
        "handler envelope has invalid status 'ok' (expected 'completed' or 'error')"
    )


def test_normalize_completed_without_result_is_error() -> None:
    text = json.dumps({"status": "completed"})
    result = handler_runner._normalize_result(text)
    assert result["error"] == (
        "handler envelope status 'completed' missing 'result' object"
    )


def test_normalize_completed_with_non_dict_result_is_error() -> None:
    # "result" present but not an object violates §2.2 (result must be object).
    text = json.dumps({"status": "completed", "result": "bare text"})
    result = handler_runner._normalize_result(text)
    assert result["error"] == (
        "handler envelope status 'completed' missing 'result' object"
    )


def test_normalize_completed_with_non_null_error_is_error() -> None:
    # §3.1: completed requires error to be None if the key exists.
    text = json.dumps({"status": "completed", "result": {}, "error": "oops"})
    result = handler_runner._normalize_result(text)
    assert result["error"] == (
        "handler envelope status 'completed' carries non-null 'error'"
    )


def test_normalize_error_without_error_string_is_error() -> None:
    for bad in ([None, {}]):
        text = json.dumps({"status": "error", "error": bad})
        result = handler_runner._normalize_result(text)
        assert result["error"] == (
            "handler envelope status 'error' missing 'error' message"
        )
    text = json.dumps({"status": "error"})
    result = handler_runner._normalize_result(text)
    assert result["error"] == (
        "handler envelope status 'error' missing 'error' message"
    )


def test_normalize_non_object_stdout_is_error() -> None:
    for raw in ('[1, 2]', '"text"', '5', 'null', 'true'):
        result = handler_runner._normalize_result(raw)
        expected_type = {
            "[1, 2]": "list",
            '"text"': "str",
            "5": "int",
            "null": "NoneType",
            "true": "bool",
        }[raw]
        assert result["error"] == (
            f"handler stdout must be a JSON object (envelope or bare result), "
            f"got {expected_type}"
        )


def test_normalize_non_json_stdout_is_error() -> None:
    result = handler_runner._normalize_result("this is not json")
    assert result["error"].startswith("handler stdout is not valid JSON:")


def test_normalize_idempotent() -> None:
    samples: list[str] = [
        json.dumps({"status": "completed", "result": {"answer": "ok"}, "error": None}),
        json.dumps({"status": "completed", "result": {"answer": "ok"}}),
        json.dumps({"status": "error", "error": "boom"}),
        json.dumps({"answer": 42}),
        json.dumps({"error": "legacy"}),
        json.dumps({"result": {"nested": True}}),
    ]
    for text in samples:
        once = handler_runner._normalize_result(text)
        twice = handler_runner._normalize_result(json.dumps(once))
        assert twice == once


# --- §3.2 Request-Hülle ------------------------------------------------------

def _stage(**overrides: Any) -> dict[str, Any]:
    stage: dict[str, Any] = {
        "stage_id": "st_1",
        "task_id": "task_1",
        "capability": "chat.ai",
        "payload": {"prompt": "hi"},
    }
    stage.update(overrides)
    return stage


def test_stdin_payload_envelope_shape() -> None:
    data = json.loads(handler_runner._build_stdin_payload(_stage(), mirrored=False))
    assert data == {"task_id": "task_1", "capability": "chat.ai", "input": {"prompt": "hi"}}


def test_stdin_payload_mirror_legacy_keys_top_level() -> None:
    data = json.loads(handler_runner._build_stdin_payload(_stage(), mirrored=True))
    # Legacy flat reads keep working…
    assert data["prompt"] == "hi"
    # …and the contract keys are present.
    assert data["task_id"] == "task_1"
    assert data["capability"] == "chat.ai"
    assert data["input"] == {"prompt": "hi"}


def test_stdin_payload_mirror_contract_keys_win_on_collision() -> None:
    # Payload keys are written first, contract keys last — contract always wins.
    stage = _stage(payload={"task_id": "evil", "input": "evil", "capability": "evil"})
    data = json.loads(handler_runner._build_stdin_payload(stage, mirrored=True))
    assert data["task_id"] == "task_1"
    assert data["capability"] == "chat.ai"
    assert data["input"] == {"task_id": "evil", "input": "evil", "capability": "evil"}


def test_stdin_payload_mirror_disabled_strict() -> None:
    data = json.loads(handler_runner._build_stdin_payload(_stage(), mirrored=False))
    assert set(data) == {"task_id", "capability", "input"}
    assert "prompt" not in data


def test_stdin_payload_empty_payload_becomes_empty_input() -> None:
    stage = _stage(payload=None)
    strict = json.loads(handler_runner._build_stdin_payload(stage, mirrored=False))
    assert strict["input"] == {}
    mirrored = json.loads(handler_runner._build_stdin_payload(stage, mirrored=True))
    assert mirrored["input"] == {}
    assert set(mirrored) == {"task_id", "capability", "input"}


def test_stdin_payload_resolves_mirror_from_capability_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caps = [
        {
            "name": "chat.ai",
            "config": {"envelope_request_mirror": False},
        }
    ]
    monkeypatch.setattr(node_config, "load_active_profile", lambda: caps)
    data = json.loads(handler_runner._stdin_payload(_stage()))
    assert set(data) == {"task_id", "capability", "input"}


def test_stdin_payload_defaults_to_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    # No capability config at all → rollout default mirrored=True.
    monkeypatch.setattr(node_config, "load_active_profile", lambda: [{"name": "chat.ai"}])
    data = json.loads(handler_runner._stdin_payload(_stage()))
    assert data["prompt"] == "hi"


def test_envelope_request_mirror_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    stage = _stage()

    def _boom() -> list[dict[str, Any]]:
        raise RuntimeError("profile unreadable")

    monkeypatch.setattr(node_config, "load_active_profile", _boom)
    assert handler_runner._envelope_request_mirror(stage) is True

    # Unknown capability / no config / non-dict config → all fail open.
    monkeypatch.setattr(node_config, "load_active_profile", list)
    assert handler_runner._envelope_request_mirror(stage) is True
    monkeypatch.setattr(
        node_config, "load_active_profile", lambda: [{"name": "chat.ai"}]
    )
    assert handler_runner._envelope_request_mirror(stage) is True
    monkeypatch.setattr(
        node_config,
        "load_active_profile",
        lambda: [{"name": "chat.ai", "config": "not-a-dict"}],
    )
    assert handler_runner._envelope_request_mirror(stage) is True
    monkeypatch.setattr(
        node_config,
        "load_active_profile",
        lambda: [{"name": "chat.ai", "config": {"envelope_request_mirror": True}}],
    )
    assert handler_runner._envelope_request_mirror(stage) is True
    monkeypatch.setattr(
        node_config,
        "load_active_profile",
        lambda: [{"name": "chat.ai", "config": {"envelope_request_mirror": False}}],
    )
    assert handler_runner._envelope_request_mirror(stage) is False


# --- §3.4 run_handler End-to-End (subprocess) --------------------------------


def _write_handler(tmp_path: Any, body: str) -> str:
    script = tmp_path / "handler.py"
    script.write_text(body, encoding="utf-8")
    return f"{sys.executable} {script}"


def test_run_handler_end_to_end_conforming(tmp_path: Any) -> None:
    handler = _write_handler(
        tmp_path,
        'import json, sys\n'
        'print(json.dumps({"status": "completed", "result": {"answer": "ok"},'
        ' "error": None, "debug": {"x": 1}}))\n',
    )
    result = handler_runner.run_handler(handler, _stage(), context={}, timeout=30)
    assert result["status"] == "completed"
    assert result["result"] == {"answer": "ok"}
    assert result["error"] is None
    assert result["debug"] == {"x": 1}
    assert result["_handler"]["exit_code"] == 0


def test_run_handler_end_to_end_legacy_wrapped(tmp_path: Any) -> None:
    handler = _write_handler(tmp_path, 'import json, sys\nprint(json.dumps({"answer": 42}))\n')
    result = handler_runner.run_handler(handler, _stage(), context={}, timeout=30)
    assert result["status"] == "completed"
    assert result["result"] == {"answer": 42}
    # Deviation 1: wrap must NOT carry an "error" key — the daemon counts
    # failures via `"error" in result` (key presence).
    assert "error" not in result
    assert result["_handler"]["exit_code"] == 0


def test_run_handler_receives_envelope_and_mirror_on_stdin(tmp_path: Any) -> None:
    handler = _write_handler(
        tmp_path,
        "import json, sys\n"
        "d = json.load(sys.stdin)\n"
        'print(json.dumps({"status": "completed", "result": {'
        '"flat_prompt": d.get("prompt"), "input": d.get("input"),'
        ' "task_id": d.get("task_id"), "capability": d.get("capability")}}))\n',
    )
    result = handler_runner.run_handler(handler, _stage(), context={}, timeout=30)
    inner = result["result"]
    assert inner["flat_prompt"] == "hi"  # mirrored legacy read
    assert inner["input"] == {"prompt": "hi"}
    assert inner["task_id"] == "task_1"
    assert inner["capability"] == "chat.ai"


def test_run_handler_envelope_error_fails_stage(tmp_path: Any) -> None:
    handler = _write_handler(
        tmp_path,
        'import json, sys\n'
        'print(json.dumps({"status": "error", "error": "Hermes timed out"}))\n',
    )
    result = handler_runner.run_handler(handler, _stage(), context={}, timeout=30)
    # Exactly the daemon gate condition (node_daemon.py "error" in result).
    assert "error" in result
    assert result["error"] == "Hermes timed out"
    assert result["status"] == "error"


def test_run_handler_non_json_fails(tmp_path: Any) -> None:
    handler = _write_handler(tmp_path, 'print("not json")\n')
    result = handler_runner.run_handler(handler, _stage(), context={}, timeout=30)
    assert result["error"].startswith("handler stdout is not valid JSON:")


def test_run_handler_nonzero_exit_fails(tmp_path: Any) -> None:
    handler = _write_handler(tmp_path, 'import sys\nprint("boom", file=sys.stderr)\nsys.exit(3)\n')
    result = handler_runner.run_handler(handler, _stage(), context={}, timeout=30)
    assert result["error"] == "handler exited with code 3"
    assert "boom" in result["stderr"]


def test_run_handler_timeout_fails(tmp_path: Any) -> None:
    handler = _write_handler(tmp_path, "import time\ntime.sleep(5)\n")
    result = handler_runner.run_handler(handler, _stage(), context={}, timeout=1)
    assert result["error"] == "handler timeout after 1s"


# --- Validator/Normalizer: per-capability "config" carrier (Deviation 2) -----


def test_profile_validator_accepts_capability_config() -> None:
    caps = node_config.validate_profile(
        {
            "capabilities": [
                {
                    "name": "chat.ai",
                    "handler": "h.py",
                    "claimable": True,
                    "config": {"envelope_request_mirror": False},
                }
            ]
        }
    )
    assert caps[0]["config"] == {"envelope_request_mirror": False}


def test_profile_validator_rejects_non_mapping_config() -> None:
    with pytest.raises(node_config.CapabilityValidationError, match="'config' must be a mapping"):
        node_config.validate_profile(
            {
                "capabilities": [
                    {"name": "chat.ai", "handler": "h.py", "claimable": True, "config": "nope"}
                ]
            }
        )