"""T-179: Envelope format v1 for handler primitives (put/get).

Contract: the sending node-cli replaces a payload field with the
envelope; the receiving node-cli recognizes ``__iowap_ref__`` and
resolves it. Scripts see only a path in / a path out.

FROZEN (Phase 1, T-179) — every signature in this module is immutable
for Phase 2 (scaffold) and Phase 3 (integrate). Field semantics v1:

* ``v``           — version, must be exactly 1 (anything else: hard
                    error; forward-compat never silently breaks old
                    receivers).
* ``src``         — ``inline`` | ``artifact`` | ``bridge``; unknown → error.
* ``filename``    — display name inside the envelope (not trustworthy
                    as a path — receivers sanitize).
* ``size_bytes``  — inline: exact byte length; otherwise 0 (placeholder,
                    the size only becomes fixed after resolution).
* ``sha256``      — inline: always set (computed); otherwise optional; the
                    sender may know it when it saw the bytes.
* ``data_base64`` — only for src=inline.
* ``artifact_id`` — only for src=artifact.
* ``storage_ref`` — only for src=bridge (``{type, id, ...}``).

The envelope replaces the payload field completely (no wrapper object).
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

ENVELOPE_KEY = "__iowap_ref__"
SUPPORTED_VERSION = 1
SUPPORTED_SOURCES = ("inline", "artifact", "bridge")


class EnvelopeError(ValueError):
    """Envelope missing, foreign, or carrying an unsupported format."""


def make_envelope(
    *,
    src: str,
    filename: str,
    data: bytes | None = None,
    artifact_id: str | None = None,
    storage_ref: dict | None = None,
    sha256: str | None = None,
    size_bytes: int | None = None,
) -> dict[str, Any]:
    """Build a v1 envelope. ``data`` only for src=inline (base64-encoded).

    Raise: EnvelopeError on unknown ``src`` or a contradicting field
    combination (inline+artifact_id, inline without data, bridge+data …).
    Fail fast at the producer — an invalid envelope should never leave
    the repo, so the receiver only sees transport-related errors.

    ``size_bytes``: actual file size for artifact/bridge (there the
    ``data`` is not inside the envelope). Default ``0`` — the original
    T-166 delivery erroneously always had ``0`` for non-inline
    (bugfix after the live E2E). Negative or smaller than a set
    ``data`` length → EnvelopeError.
    """
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool):
        if size_bytes is not None:
            raise EnvelopeError("size_bytes must be an int")
        size_bytes = None
    if size_bytes is not None and size_bytes < 0:
        raise EnvelopeError("size_bytes must be >= 0")
    if src not in SUPPORTED_SOURCES:
        raise EnvelopeError(f"unsupported src {src!r}")
    if not filename:
        raise EnvelopeError("envelope requires a filename")
    is_inline = src == "inline"
    if is_inline and data is None:
        raise EnvelopeError("inline envelope requires data")
    if not is_inline and data is not None:
        raise EnvelopeError(f"{src!r} envelope must not carry data")
    if src == "artifact" and not artifact_id:
        raise EnvelopeError("artifact envelope requires artifact_id")
    if src == "bridge" and storage_ref is None:
        raise EnvelopeError("bridge envelope requires storage_ref")
    if is_inline and artifact_id is not None:
        raise EnvelopeError("inline envelope must not carry artifact_id")
    if src == "bridge" and artifact_id is not None:
        raise EnvelopeError("bridge envelope must not carry artifact_id")

    ref: dict[str, Any] = {
        "v": SUPPORTED_VERSION,
        "src": src,
        "filename": filename,
        # inline: always derived from data; artifact/bridge: explicit
        # size (the file is not inside the envelope), default 0.
        "size_bytes": len(data) if data is not None else (
            size_bytes if size_bytes is not None else 0
        ),
    }
    if data is not None:
        ref["data_base64"] = base64.b64encode(data).decode("ascii")
    # T-166 (F8): sha256 in the envelope — inline computes it from the
    # data, bridge/artifact carry the sender hash explicitly (F8
    # verification at the receiver even without inline payload).
    if sha256 is not None:
        ref["sha256"] = sha256
    elif data is not None:
        ref["sha256"] = hashlib.sha256(data).hexdigest()
    if artifact_id is not None:
        ref["artifact_id"] = artifact_id
    if storage_ref is not None:
        ref["storage_ref"] = storage_ref
    return {ENVELOPE_KEY: ref}


def parse_envelope(envelope: dict[str, Any]) -> tuple[str, Any]:
    """Validate an envelope, return ``(src, raw reference)``.

    inline   → second value is the decoded bytes (``bytes``)
    artifact → second value is the artifact_id (``str``)
    bridge   → second value is the storage_ref dict

    Raise: EnvelopeError on a missing/contradicting envelope —
    unknown ``v`` or unknown ``src`` breaks the receiver loudly
    (forward-compat: new modes never break old receivers silently).
    """
    if not isinstance(envelope, dict) or ENVELOPE_KEY not in envelope:
        raise EnvelopeError(f"no {ENVELOPE_KEY} key — not an envelope")
    ref = envelope[ENVELOPE_KEY]
    if not isinstance(ref, dict):
        raise EnvelopeError("envelope payload must be an object")
    version = ref.get("v")
    if version != SUPPORTED_VERSION:
        raise EnvelopeError(f"unsupported envelope version {version!r}")
    src = ref.get("src")
    if src not in SUPPORTED_SOURCES:
        raise EnvelopeError(f"unsupported envelope src {src!r}")
    if src == "inline":
        raw = ref.get("data_base64")
        if not raw:
            raise EnvelopeError("inline envelope without data_base64")
        try:
            return src, base64.b64decode(raw)
        except (ValueError, TypeError) as exc:
            # binascii.Error is a ValueError subclass.
            raise EnvelopeError(f"invalid base64 in inline envelope: {exc}") from exc
    if src == "artifact":
        artifact_id = ref.get("artifact_id")
        if not artifact_id:
            raise EnvelopeError("artifact envelope without artifact_id")
        return src, artifact_id
    storage_ref = ref.get("storage_ref")
    if not isinstance(storage_ref, dict):
        raise EnvelopeError("bridge envelope without storage_ref")
    return src, storage_ref


def is_envelope(value: Any) -> bool:
    """True if ``value`` is a dict with the ``__iowap_ref__`` key.

    Pure shape check without validation — for "is this payload field an
    envelope?" decisions on the receiver side.
    """
    return isinstance(value, dict) and ENVELOPE_KEY in value


def dump(envelope: dict[str, Any]) -> str:
    """Serialize an envelope as compact JSON (no separator lines)."""
    return json.dumps(envelope, separators=(",", ":"))