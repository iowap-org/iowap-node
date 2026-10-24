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

# T-235 C1: slot-type registry — exact client mirror of the server's
# core/forward.py:35-41 (T-245; server is the source of truth).
SLOT_TYPE_FORWARD: Final[str] = "forward"
SLOT_TYPE_TEXT: Final[str] = "text"
SLOT_TYPE_BOOL: Final[str] = "bool"
SLOT_TYPES: Final[frozenset[str]] = frozenset(
    {SLOT_TYPE_FORWARD, SLOT_TYPE_TEXT, SLOT_TYPE_BOOL}
)
DEFAULT_SLOT_TYPE: Final[str] = SLOT_TYPE_FORWARD

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

    def __init__(self, message: str, code: str = "forward_mapping_invalid") -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


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


def normalize_slot_type(value: Any) -> str:
    """Resolve the declared slot type against the client registry (T-235 C1).

    Client mirror of the server's ``core/forward.py::normalize_slot_type``
    (core/forward.py:90-119): ``None``/absent → ``DEFAULT_SLOT_TYPE``
    (Bestandsschutz: old declarations carry no type); explicit values are
    matched EXACTLY (registry match after ``str``-strip, NO case-folding —
    "Forward"/"TEXT" are errors); bools and non-str values are mis-typed
    declarations. Raises ``ForwardSlotError`` (code ``forward_slot_invalid``).
    """
    def _invalid(msg: str) -> ForwardSlotError:
        return ForwardSlotError(msg, code="forward_slot_invalid")

    if value is None:
        return DEFAULT_SLOT_TYPE
    # Bools are an int subclass and must never coerce; anything non-str
    # is a mis-typed declaration (loud, node-owned metadata error).
    if isinstance(value, bool) or not isinstance(value, str):
        raise _invalid(f"slot type must be a string, got {type(value).__name__}")
    slot_type = value.strip()
    if not slot_type:
        raise _invalid("slot type must not be empty when declared")
    if slot_type not in SLOT_TYPES:
        # Exact registry match — no case-folding ("Forward"/"TEXT" are errors).
        raise _invalid(
            f"unknown slot type '{slot_type}'"
            f" (known: {', '.join(sorted(SLOT_TYPES))})"
        )
    return slot_type


def normalize_slot_value(slot_type: str, value: Any) -> str | bool | None:
    """Validate the typed ``value`` field of a declaration (T-235 C1).

    Client mirror of the server's ``core/forward.py::normalize_slot_value``
    (core/forward.py:122-158): ``None`` → ``None`` for every type; ``text``
    → ``value`` must be ``str``; ``bool`` → a REAL bool ("true"/``1``/``0``
    are errors — bools are an int subclass and never coerce); ``type:
    forward`` FORBIDS any non-``None`` value. Returns the native value
    unchanged. Raises ``ForwardSlotError`` (code ``forward_slot_invalid``).
    """
    def _invalid(msg: str) -> ForwardSlotError:
        return ForwardSlotError(msg, code="forward_slot_invalid")

    if value is None:
        return None
    if slot_type == SLOT_TYPE_TEXT:
        if not isinstance(value, str):
            raise _invalid(
                f"text slot value must be a string, got {type(value).__name__}"
            )
        return value
    if slot_type == SLOT_TYPE_BOOL:
        # Bools are an int subclass: "true" and 1 must never coerce.
        if not isinstance(value, bool):
            raise _invalid(
                f"bool slot value must be a real bool, got {type(value).__name__}"
            )
        return value
    # slot_type == SLOT_TYPE_FORWARD — a value is forbidden there (D-02):
    # the routing target lives in forward_mappings, not on the slot row.
    raise _invalid(
        "forward slots must not carry a 'value'"
        " — the routing target lives in forward_mappings"
    )


def normalize_slot_ref(entry: Mapping[str, Any], slot_type: str) -> tuple[str, str] | None:
    """Normalize ``ref_slot``/``ref_field`` of a declaration (T-235 C1).

    Client mirror of the server's ``core/forward.py::normalize_slot_ref``
    (core/forward.py:161-204): a ref is legal ONLY on value slots
    (``text``/``bool``) and must be given WHOLE (``ref_slot`` XOR
    ``ref_field`` → error; a non-``str`` ``ref_field`` counts as absent);
    on ``type: forward`` any ref is an error. ``ref_slot`` goes through the
    ``normalize_forward_slot`` charset/length rules with the error code
    REMAPPED to ``forward_slot_invalid``. Whether ``ref_field`` exists
    inside the referenced slot's ``input_schema`` is NOT validated here
    (server DEFERRED D-05/D-09 → T-247). Returns ``(ref_slot, ref_field)``
    or ``None``. Raises ``ForwardSlotError`` (code ``forward_slot_invalid``).
    """
    def _invalid(msg: str) -> ForwardSlotError:
        return ForwardSlotError(msg, code="forward_slot_invalid")

    raw_slot = entry.get("ref_slot")
    raw_field = entry.get("ref_field")
    if slot_type == SLOT_TYPE_FORWARD:
        # Routing handles carry no refs — the target lives in forward_mappings.
        if raw_slot is not None or raw_field is not None:
            raise _invalid(
                "forward slots must not carry a ref"
                " — the routing target lives in forward_mappings"
            )
        return None
    # Value slots accept a ref, but only as a WHOLE pair (no half refs);
    # ref_slot goes through normalize_forward_slot (int coercion allowed
    # there) and a non-str ref_field counts as absent — the pair is half.
    has_slot = raw_slot is not None
    has_field = isinstance(raw_field, str)
    if has_slot != has_field:
        raise _invalid("a ref must declare both ref_slot and ref_field together")
    if not has_slot:
        return None
    try:
        ref_slot = normalize_forward_slot(raw_slot)
    except ForwardSlotError as exc:
        # Same charset/length rules as normalize_forward_slot, remapped to
        # the declaration error code (client mirror of server :198-203).
        raise ForwardSlotError(exc.message, code="forward_slot_invalid") from exc
    # has_slot == has_field == True here — raw_field IS a str; the
    # conditional keeps the frozen ``tuple[str, str]`` return type honest.
    ref_field = raw_field if isinstance(raw_field, str) else ""
    return (ref_slot, ref_field)
