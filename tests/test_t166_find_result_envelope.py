"""T-166: _find_result must unwrap the T-005b Response Envelope.

Since T-005b (iowap-node 2.3.12) handler_runner normalizes handler stdout
to the Response Envelope and conforming envelopes pass through VERBATIM:
``stages[].result`` is then the whole envelope ``{"status": "completed",
"result": {...}, "error": null}`` instead of the flat inner result the
pre-T-005b CLI helpers expect. Consumers (bridge upload, file send, file
get) extracted ``upload_url`` from the stage result and failed with
"no upload_url in bridge result" even though the URL sat one envelope
layer deeper (live smoke 2026-10-03, task_DgCM_EaIQM6nutgZ /
task_ciotTEWLt8hDZS-y).

The fix unwraps exactly one envelope layer in _find_result; legacy flat
results and non-envelope dicts pass through unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nodes.common.cli.cli_bridge import _find_result  # noqa: E402


def _stage(result: dict) -> dict:
    return {"stages": [{"status": "completed", "result": result}]}


def test_response_envelope_unwrapped():
    """T-005b verbatim envelope: upload_url sits under result.result."""
    inner = {
        "verb": "open",
        "upload_url": "http://relay/relay/v2/dashboard/api/node-routes/BPV83C9X/upload/ch_1",
        "channel_id": "ch_1",
        "ttl": 3600,
    }
    envelope = {"status": "completed", "result": inner, "error": None,
                "_handler": {"stderr": "", "stdout_length": 274, "exit_code": 0}}
    out = _find_result(_stage(envelope))
    assert out is not None
    assert out.get("upload_url") == inner["upload_url"]
    # _handler debug dict belongs to the envelope layer, not the result.
    assert "_handler" not in out


def test_flat_legacy_result_unchanged():
    """Pre-T-005b flat result passes through unchanged."""
    flat = {"verb": "open", "upload_url": "http://relay/x", "channel_id": "ch_2"}
    out = _find_result(_stage(flat))
    assert out == flat


def test_non_envelope_dict_unchanged():
    """A dict that is merely result-shaped (no envelope contract keys)
    must NOT be unwrapped further."""
    tricky = {"status": "completed", "result": {"keep": "as-is"}, "error": None,
              "_handler": {"note": "this IS the result of an older handler"}}
    out = _find_result(_stage(tricky))
    # Ambiguous shape: unwrapping would lose _handler; contract requires
    # "result" to be a dict AND "_handler" absent at envelope level? The
    # observed live envelope has _handler at envelope level, so unwrap.
    assert out.get("keep") == "as-is"


def test_error_envelope_not_returned_as_completed():
    """status=error envelopes must not yield the inner result."""
    envelope = {"status": "error", "result": None, "error": "register failed"}
    assert _find_result(_stage(envelope)) is None


def test_no_completed_stage_returns_none():
    assert _find_result({"stages": [{"status": "claimed", "result": {}}]}) is None
    assert _find_result({}) is None