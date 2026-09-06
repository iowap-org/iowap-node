"""T-179: Envelope-Format v1 für Handler-Primitives (put/get).

Vertrag: Sender-node-cli ersetzt ein Payload-Feld durch den Umschlag,
Empfänger-node-cli erkennt __iowap_ref__ und löst auf. Scripts sehen
nur Pfad rein / Pfad raus.

FROZEN (Phase 1, T-179) — jede Signatur in diesem Modul ist unveränderlich
für Phase 2 (scaffold) und Phase 3 (integrate). Feld-Semantik v1:

* ``v``           — Version, muss exakt 1 sein (alles andere: harter Fehler,
                    forward-compat bricht alte Empfänger nie still).
* ``src``         — ``inline`` | ``artifact`` | ``bridge``; unbekannt → Fehler.
* ``filename``    — Anzeigename im Umschlag (nicht trustbar als Pfad —
                    Empfänger sanitize).
* ``size_bytes``  — inline: exakte Byte-Länge; sonst 0 (Platzhalter, Größe
                    steht erst nach Auflösung fest).
* ``sha256``      — inline: immer gesetzt (berechnet); sonst optional, der
                    Sender kann ihn kennt, wenn er die Bytes gesehen hat.
* ``data_base64`` — nur bei src=inline.
* ``artifact_id`` — nur bei src=artifact.
* ``storage_ref`` — nur bei src=bridge (``{type, id, ...}``).

Der Umschlag ersetzt das Payload-Feld komplett (kein Wrapper-Objekt).
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
    """Umschlag fehlt, ist fremd oder trägt ein nicht unterstütztes Format."""


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
    """Baue einen v1-Umschlag. ``data`` nur bei src=inline (wird base64-kodiert).

    Raise: EnvelopeError bei unbekanntem ``src`` oder widersprüchlicher
    Feld-Kombination (inline+artifact_id, inline ohne data, bridge+data …).
    Fail-fast am Erzeuger — ein ungültiger Umschlag soll nie das Repo
    verlassen, damit der Empfänger nur noch transport-bedingte Fehler sieht.

    ``size_bytes``: tatsächliche Dateigröße für artifact/bridge (dort ist
    ``data`` nicht im Umschlag). Default ``0`` — T-166-Lieferung hatte
    fälschlich immer ``0`` für Nicht-Inline (Bugfix nach Live-E2E).
    Negativ oder kleiner als eine gesetzte ``data``-Länge → EnvelopeError.
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
        # inline: immer aus data hergeleitet; artifact/bridge: explizite
        # Größe (Datei liegt nicht im Umschlag), Default 0.
        "size_bytes": len(data) if data is not None else (
            size_bytes if size_bytes is not None else 0
        ),
    }
    if data is not None:
        ref["data_base64"] = base64.b64encode(data).decode("ascii")
    # T-166 (F8): sha256 im Envelope — inline berechnet sie aus den Daten,
    # bridge/artifact tragen den Sender-Hash explizit (F8-Verifizierung beim
    # Empfänger auch ohne inline-Payload).
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
    """Validiere einen Umschlag, gib ``(src, rohe Referenz)`` zurück.

    inline   → zweiter Wert sind die dekodierten Bytes (``bytes``)
    artifact → zweiter Wert ist die artifact_id (``str``)
    bridge   → zweiter Wert ist das storage_ref-Dict

    Raise: EnvelopeError bei fehlendem/widersprüchlichem Umschlag —
    unbekannte ``v`` oder unbekanntes ``src`` brechen den Empfänger
    laut (forward-compat: neue Modi brechen alte Empfänger nicht still).
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
            # binascii.Error ist eine ValueError-Unterklasse.
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
    """True, wenn ``value`` ein dict mit dem ``__iowap_ref__``-Key ist.

    Reine Shape-Prüfung ohne Validierung — für "ist dieses Payload-Feld
    ein Umschlag?"-Entscheidungen auf Empfängerseite.
    """
    return isinstance(value, dict) and ENVELOPE_KEY in value


def dump(envelope: dict[str, Any]) -> str:
    """Serialisiere einen Umschlag als compactes JSON (Trennzeilen-frei)."""
    return json.dumps(envelope, separators=(",", ":"))