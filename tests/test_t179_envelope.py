"""T-179: Envelope-Format v1 — bauen, validieren, auflösen.

Der Envelope ist der FROZEN Contract zwischen zwei node-clis (Phase 1):
Der Sender ersetzt ein Payload-Feld durch ``{"__iowap_ref__": {...}}``,
der Empfänger erkennt den Key und löst auf. Scripts sehen nur
Pfad rein / Pfad raus.
"""
import base64
import json

import pytest

from nodes.common.envelope import EnvelopeError, make_envelope, parse_envelope


def test_make_envelope_inline_minimal():
    env = make_envelope(src="inline", filename="a.txt",
                        data=bytes([1, 2, 3]))
    assert env["__iowap_ref__"]["v"] == 1
    assert env["__iowap_ref__"]["src"] == "inline"
    assert env["__iowap_ref__"]["data_base64"] == base64.b64encode(bytes([1, 2, 3])).decode()
    assert env["__iowap_ref__"]["filename"] == "a.txt"
    assert env["__iowap_ref__"]["size_bytes"] == 3


def test_parse_envelope_roundtrip_inline():
    env = make_envelope(src="inline", filename="a.txt", data=b"hi")
    src, data = parse_envelope(env)
    assert src == "inline"
    assert data == b"hi"


def test_parse_envelope_artifact_requires_id():
    env = make_envelope(src="artifact", filename="a.bin",
                        artifact_id="art_1")
    src, data = parse_envelope(env)
    assert src == "artifact"
    assert data == "art_1"          # artifact: id statt Bytes


def test_parse_envelope_rejects_unknown_version():
    with pytest.raises(EnvelopeError):
        parse_envelope({"__iowap_ref__": {"v": 99, "src": "inline"}})


def test_parse_envelope_rejects_unknown_src():
    with pytest.raises(EnvelopeError):
        parse_envelope({"__iowap_ref__": {"v": 1, "src": "carrier-pigeon"}})


def test_parse_envelope_rejects_non_envelope():
    with pytest.raises(EnvelopeError):
        parse_envelope({"something": "else"})


# --- vertragliche Präzisierungen (Phase 1, FROZEN) ---------------------------


def test_make_envelope_rejects_unknown_src():
    # Sender-Seite validiert genauso hart wie parse (fail-fast am Erzeuger).
    with pytest.raises(EnvelopeError):
        make_envelope(src="carrier-pigeon", filename="a.txt", data=b"x")


def test_make_envelope_artifact_without_data_has_size_zero():
    # Nicht-inline-Umschläge kennen size_bytes erst nach Auflösung → 0 als
    # Platzhalter. Sender-Duplikat-Check: data nur bei inline erlaubt.
    env = make_envelope(src="artifact", filename="a.bin", artifact_id="art_1")
    assert env["__iowap_ref__"]["size_bytes"] == 0
    assert "data_base64" not in env["__iowap_ref__"]
    assert env["__iowap_ref__"]["artifact_id"] == "art_1"


def test_make_envelope_inline_rejects_data_with_artifact_id():
    # Widersprüchlicher Umschlag: inline-Bytes + artifact_id ist ein
    # Protokollfehler, kein Datenfehler.
    with pytest.raises(EnvelopeError):
        make_envelope(src="inline", filename="a.txt", data=b"x",
                      artifact_id="art_1")


def test_make_envelope_bridge_stores_storage_ref():
    env = make_envelope(src="bridge", filename="a.bin",
                        storage_ref={"type": "channel", "id": "ch_1"})
    ref = env["__iowap_ref__"]
    assert ref["src"] == "bridge"
    assert ref["storage_ref"] == {"type": "channel", "id": "ch_1"}
    assert "data_base64" not in ref and "artifact_id" not in ref
    # Und parse liefert das storage_ref-Dict zurück:
    src, val = parse_envelope(env)
    assert src == "bridge"
    assert val == {"type": "channel", "id": "ch_1"}


def test_make_envelope_inline_computes_sha256():
    import hashlib
    env = make_envelope(src="inline", filename="a.txt", data=b"hi")
    assert env["__iowap_ref__"]["sha256"] == hashlib.sha256(b"hi").hexdigest()
    # Known-answer check gegen den dokumentierten Wert für b"hi":
    assert env["__iowap_ref__"]["sha256"] == \
        "8f434346648f6b96df89dda901c5176b10a6d83961dd3c1ac88b59b2dc327aa4"


def test_parse_envelope_rejects_inline_without_data():
    with pytest.raises(EnvelopeError):
        parse_envelope({"__iowap_ref__": {"v": 1, "src": "inline",
                                          "filename": "a.txt"}})


def test_parse_envelope_rejects_artifact_without_id():
    with pytest.raises(EnvelopeError):
        parse_envelope({"__iowap_ref__": {"v": 1, "src": "artifact",
                                          "filename": "a.bin"}})


def test_parse_envelope_rejects_bridge_without_storage_ref():
    with pytest.raises(EnvelopeError):
        parse_envelope({"__iowap_ref__": {"v": 1, "src": "bridge",
                                          "filename": "a.bin"}})


def test_parse_envelope_rejects_non_dict_payload():
    with pytest.raises(EnvelopeError):
        parse_envelope({"__iowap_ref__": "not-a-dict"})


def test_is_envelope_and_dump_roundtrip():
    from nodes.common.envelope import dump, is_envelope
    env = make_envelope(src="inline", filename="a.txt", data=b"hi")
    assert is_envelope(env) is True
    assert is_envelope({"something": "else"}) is False
    assert is_envelope([env]) is False                    # keine Liste
    assert is_envelope(None) is False                      # kein None
    # dump → compact JSON → parse → identische Bytes
    round = json.loads(dump(env))
    assert round == env
    assert parse_envelope(round) == ("inline", b"hi")