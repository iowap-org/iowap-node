"""Forward-routing client support (T-Forward-Node) — slot rules + request encoding.

Pure client-side mirror of the relay's forward contract: a task submitted with
capability ``forward`` carries the node-local slot as ``payload["forward_slot"]``;
the relay resolves ``(submitter, slot) -> target_capability`` and redirects
submit-time (core/forward.py on the server). The client never names the target
capability, never converts slots, and never advertises ``forward`` (profile
validation rejects it — node_config._normalize_caps_list).

Contracts: ``.hermes/pipeline/t-forward-node/design.md`` §3.1/§3.2.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

FORWARD_CAPABILITY: Final[str] = "forward"
SLOT_MAX_CHARS: Final[int] = 64
_SLOT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")

# Actionable suggestions for the relay's forward-error codes (design.md §6).
# Keys are the exact codes from the deployed relay (api/v2/scheduler.py:70-72,
# core/forward.py); values are single-line operator guidance, docs-facing.
FORWARD_ERROR_HINTS: Final[dict[str, str]] = {
    "forward_mapping_missing": (
        "open the relay dashboard (node profile → Forward mappings) and "
        "configure the mapping for this node and slot"
    ),
    "forward_mapping_invalid": (
        "do not retry — fix the slot value or the mapping first"
    ),
    "forward_node_unknown": (
        "this node lost its registration — re-register with 'node-cli node register'"
    ),
    "target_unknown_capability": (
        "stale mapping: the configured target is not in the capability "
        "catalog — ask the operator to update the mapping"
    ),
    "redirect_self": (
        "mapping points at the forward pseudo-capability itself — operator "
        "must fix the mapping target"
    ),
}


class ForwardSlotError(ValueError):
    """Client-side forward-slot validation failure (server-mirrored rules)."""

    def __init__(self, message: str) -> None:
        self.code = "forward_mapping_invalid"
        self.message = message
        super().__init__(f"{self.code}: {message}")


def normalize_forward_slot(value: Any) -> str:
    """Normalize a forward slot — exact client mirror of the server's
    ``core/forward.py::normalize_slot`` (design.md D-3).

    Returns the normalized slot string (trimmed, int-coerced). Raises
    ``ForwardSlotError`` (code ``forward_mapping_invalid``) for bools,
    non-str/int values, empty or whitespace-only strings, inner whitespace,
    oversized (>{SLOT_MAX_CHARS} chars) or disallowed characters (anything
    outside ``[A-Za-z0-9_-]``, e.g. dots).
    """
    if isinstance(value, bool):
        raise ForwardSlotError("forward slot must be a string, got a bool")
    if isinstance(value, int):
        value = str(value)
    elif not isinstance(value, str):
        raise ForwardSlotError(f"forward slot must be a string, got {type(value).__name__}")

    slot = value.strip()
    if not slot:
        raise ForwardSlotError("forward slot must not be empty")
    if re.search(r"\s", slot):
        raise ForwardSlotError("forward slot must not contain whitespace")
    if not 1 <= len(slot) <= SLOT_MAX_CHARS:
        raise ForwardSlotError(f"forward slot must be 1..{SLOT_MAX_CHARS} chars")
    if not _SLOT_PATTERN.fullmatch(slot):
        raise ForwardSlotError("forward slot allows only letters, digits, '_' and '-'")
    return slot


def apply_forward_slot(
    capability: str,
    payload: Mapping[str, Any],
    forward_slot: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Pure request-encoding for forward submission (design.md §3.2/D-2/D-16).

    * ``forward_slot is None`` → ``(capability, dict(payload))`` — shallow
      copy, no key injected; a payload-carried ``forward_slot`` passes
      through unchanged (the server validates it).
    * otherwise → normalize the slot first (raises ``ForwardSlotError``),
      then ``("forward", {**dict(payload), "forward_slot": slot})`` — the
      capability is FORCED to the pseudo-capability and an explicit slot
      wins over a payload-carried key.

    NEVER mutates the caller's payload dict (test-pinned).
    """
    payload_dict = dict(payload)
    if forward_slot is None:
        return capability, payload_dict
    slot = normalize_forward_slot(forward_slot)
    return FORWARD_CAPABILITY, {**payload_dict, "forward_slot": slot}


def forward_hint(code: str | None) -> str | None:
    """Actionable suggestion for a relay forward-error code (design.md §6).

    Returns the FORWARD_ERROR_HINTS entry for ``code``; ``None`` for unknown,
    empty, or non-str codes. Pure lookup, never raises.
    """
    if not isinstance(code, str):
        return None
    return FORWARD_ERROR_HINTS.get(code)