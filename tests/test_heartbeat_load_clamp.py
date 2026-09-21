"""T-209: heartbeat load bleibt Prozent (0-100), auch bei loadavg > cores.

Regression: CT 902/906 fielen 20:30-20:53 am Relay auf 422, weil
load = (loadavg/cpu)*100 bei loadavg > cpu auf >100 stieg und der
Server-Schema-Field le=100 den Heartbeat ablehnte. Der Client clampet
jetzt auf 100 statt auf load_cap (T-081 auto-busy bleibt unberuehrt:
load_cap >= 100 schaltet auto-busy trotzdem).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from nodes.common.relay_client import RelayClient

META = {
    "node_id": "TESTNODE",
    "node_name": "clamp-test",
    "registration_secret": "rs_x",
    "base_url": "http://relay.test:8788",
}
CFG = {"base_url": None, "request_timeout": 5, "heartbeat_interval": 8}


@pytest.mark.parametrize("loadavg,cpu,expected", [
    (1.0, 2, 50.0),
    (2.0, 2, 100.0),
    (2.63, 2, 100.0),   # der CT-902/906-Fall
    (8.0, 1, 100.0),    # single-core spike
    (0.5, 4, 12.5),
])
def test_heartbeat_load_never_exceeds_100(loadavg, cpu, expected):
    client = RelayClient(dict(META), dict(CFG))
    with patch("nodes.common.relay_client.os.getloadavg", return_value=(loadavg, 0, 0)), \
         patch("nodes.common.relay_client.os.cpu_count", return_value=cpu), \
         patch("nodes.common.relay_client._read_cgroup_cpu_usage", return_value=None):
        payload = client._build_heartbeat_payload(caps=[], in_flight={})
    assert payload["load"] == pytest.approx(expected)
    assert payload["load"] <= 100.0
