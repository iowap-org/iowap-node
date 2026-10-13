"""T-179: envelope format v1 — build, validate, resolve.

The envelope is the FROZEN contract between two node-clis (phase 1):
the sender replaces a payload field with ``{"__iowap_ref__": {...}}``,
the receiver recognizes the key and resolves it. Scripts see only
path in / path out.
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
    assert data == "art_1"          # artifact: id instead of bytes


def test_parse_envelope_rejects_unknown_version():
    with pytest.raises(EnvelopeError):
        parse_envelope({"__iowap_ref__": {"v": 99, "src": "inline"}})


def test_parse_envelope_rejects_unknown_src():
    with pytest.raises(EnvelopeError):
        parse_envelope({"__iowap_ref__": {"v": 1, "src": "carrier-pigeon"}})


def test_parse_envelope_rejects_non_envelope():
    with pytest.raises(EnvelopeError):
        parse_envelope({"something": "else"})


# --- contract-level clarifications (phase 1, FROZEN) -------------------------


def test_make_envelope_rejects_unknown_src():
    # the sender side validates just as strictly as parse (fail-fast at the producer).
    with pytest.raises(EnvelopeError):
        make_envelope(src="carrier-pigeon", filename="a.txt", data=b"x")


def test_make_envelope_artifact_without_data_has_size_zero():
    # non-inline envelopes only learn size_bytes after resolution → 0 as
    # the placeholder. sender duplicate check: data allowed only for inline.
    env = make_envelope(src="artifact", filename="a.bin", artifact_id="art_1")
    assert env["__iowap_ref__"]["size_bytes"] == 0
    assert "data_base64" not in env["__iowap_ref__"]
    assert env["__iowap_ref__"]["artifact_id"] == "art_1"


def test_make_envelope_inline_rejects_data_with_artifact_id():
    # contradictory envelope: inline bytes + artifact_id is a
    # protocol error, not a data error.
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
    # and parse returns the storage_ref dict:
    src, val = parse_envelope(env)
    assert src == "bridge"
    assert val == {"type": "channel", "id": "ch_1"}


def test_make_envelope_inline_computes_sha256():
    import hashlib
    env = make_envelope(src="inline", filename="a.txt", data=b"hi")
    assert env["__iowap_ref__"]["sha256"] == hashlib.sha256(b"hi").hexdigest()
    # known-answer check against the documented value for b"hi":
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
    assert is_envelope([env]) is False                    # not a list
    assert is_envelope(None) is False                      # not None
    # dump → compact JSON → parse → identical bytes
    round = json.loads(dump(env))
    assert round == env
    assert parse_envelope(round) == ("inline", b"hi")


# --- T-166 bugfix: size_bytes for artifact/bridge (used to be 0) ----------


def test_make_envelope_bridge_size_bytes():
    env = make_envelope(src="bridge", filename="big.bin",
                        storage_ref={"type": "node_serve"}, size_bytes=53477376)
    assert env["__iowap_ref__"]["size_bytes"] == 53477376


def test_make_envelope_artifact_size_bytes():
    env = make_envelope(src="artifact", filename="a.bin",
                        artifact_id="art_1", size_bytes=7)
    assert env["__iowap_ref__"]["size_bytes"] == 7


def test_make_envelope_inline_ignores_wrong_size():
    # inline derives it from data; an explicit size does not take effect
    env = make_envelope(src="inline", filename="a.txt", data=b"hi",
                        size_bytes=99)
    assert env["__iowap_ref__"]["size_bytes"] == 2


def test_make_envelope_rejects_negative_size():
    with pytest.raises(EnvelopeError):
        make_envelope(src="bridge", filename="a.bin",
                      storage_ref={"type": "node_serve"}, size_bytes=-1)


def test_make_envelope_rejects_non_int_size():
    with pytest.raises(EnvelopeError):
        make_envelope(src="artifact", filename="a.bin",
                      artifact_id="art_1",
                      size_bytes="7")  # type: ignore[arg-type]