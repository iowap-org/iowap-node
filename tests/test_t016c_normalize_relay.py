"""T-016c: normalize_base_url rejects unparseable relay hosts with friendly errors.

Windows-VM finding (2026-11-22): a relay host with an empty label
(trailing/double dot) hit httpx < 0.28's ``str.encode('idna')`` inside the
registration call and surfaced as a raw
``registration failed: encoding with 'idna' codec failed (UnicodeError:
label empty or too long)``. normalize_base_url now validates the parsed
URL and raises a friendly ValueError instead.
"""

import pytest

from nodes.common.cli.cli_server import normalize_base_url


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [
        ("http://192.168.2.60:8788", "http://192.168.2.60:8788"),
        ("http://192.168.2.60:8788/", "http://192.168.2.60:8788"),
        ("192.168.2.60", "http://192.168.2.60:8788"),
        ("192.0.2.60:9000", "http://192.0.2.60:9000"),
        ("http://iowap-relay:8788", "http://iowap-relay:8788"),
    ],
)
def test_valid_relay_inputs_pass_through(submitted: str, expected: str) -> None:
    assert normalize_base_url(submitted) == expected


@pytest.mark.parametrize(
    "submitted",
    [
        "http://192.168.2.60.:8788",  # trailing dot on the host → empty idna label
        "http://192.168.2.60..:8788",  # double dot → empty label
        "http://iowap-relay.local.:8788",  # zeroconf-style trailing dot
        ":8788",  # missing host entirely
        "http://:8788",  # missing host, full-URL form
    ],
)
def test_empty_label_hosts_raise_friendly_valueerror(submitted: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        normalize_base_url(submitted)
    msg = str(excinfo.value)
    assert "relay URL" in msg
    # The raw codec error must never leak: the friendly message replaces it.
    assert "idna" not in msg


def test_error_names_the_submitted_value() -> None:
    with pytest.raises(ValueError) as excinfo:
        normalize_base_url("http://iowap-relay.local.:8788")
    assert "iowap-relay.local.:8788" in str(excinfo.value)
