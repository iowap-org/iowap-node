"""T-210: heartbeat load misst CT-eigene Last statt Host-Load.

Regression: mc-node (AMKJA9AE) und bot-desktop (XMYTFTA7) meldeten am
Relay exakt denselben Load (64.11/70.87), weil os.getloadavg() in LXC
das HOST-loadavg liefert (/proc/loadavg ist im CT shared). Die Kette
misst jetzt zuerst die CPU-Consume aus dem eigenen cgroup (v2, dann
v1) und faellt nur zurueck auf loadavg (bare metal/macOS korrekt).
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from nodes.common import relay_client
from nodes.common.relay_client import RelayClient, _measure_load_pct, _read_cgroup_cpu_usage

META = {
    "node_id": "TESTNODE",
    "node_name": "load-source-test",
    "registration_secret": "rs_x",
    "base_url": "http://relay.test:8788",
}
CFG = {"base_url": None, "request_timeout": 5, "heartbeat_interval": 8}

CgroupSample = tuple[str, float] | None


def _with_cgroup_samples(samples: list, loadavg: float = 0.0, cpu: int = 2):
    """Drive _build_heartbeat_payload through N calls with staged cgroup reads."""
    client = RelayClient(dict(META), dict(CFG))
    seq = iter(samples)
    with patch.object(relay_client, "_read_cgroup_cpu_usage", side_effect=lambda: next(seq)), \
         patch("nodes.common.relay_client.os.getloadavg", return_value=(loadavg, 0, 0)), \
         patch("nodes.common.relay_client.os.cpu_count", return_value=cpu):
        payloads = [client._build_heartbeat_payload(caps=[], in_flight={}) for _ in samples]
    return payloads


def test_cgroup_v2_diff_between_two_heartbeats():
    # 1st heartbeat: seeds prev sample (100.0s at t=1000)
    # 2nd heartbeat: 60 CPU-seconds used in 120s wall time on 1 core -> 50%
    client_calls = [None, ("cgroup2", 100.0), ("cgroup2", 160.0)]
    times = iter([999.0, 1000.0, 1120.0])
    with patch.object(relay_client.time, "monotonic", side_effect=lambda: next(times)):
        payloads = _with_cgroup_samples(client_calls, loadavg=0.0, cpu=1)
    assert payloads[0]["load_source"] == "loadavg"
    assert payloads[0]["load"] == 0.0
    # 2nd call seeds prev (no earlier cgroup sample) -> still loadavg;
    # 3rd call diffs against the seed and yields the rate
    assert payloads[1]["load_source"] == "loadavg"
    assert payloads[2]["load_source"] == "cgroup2"
    assert payloads[2]["load"] == pytest.approx(50.0)


def test_cgroup_v1_usage_nanoseconds():
    # cpuacct.usage reports ns -> 1e9 = 1 CPU-second; 1st call seeds,
    # 2nd call diffs: 1 CPU-second over 10s on 1 core = 10%
    times = iter([0.0, 0.0, 10.0])
    calls = [None, ("cgroup", 0.0), ("cgroup", 1.0)]
    with patch.object(relay_client.time, "monotonic", side_effect=lambda: next(times)):
        payloads = _with_cgroup_samples(calls, cpu=1)
    assert payloads[1]["load_source"] == "loadavg"
    assert payloads[2]["load_source"] == "cgroup"
    assert payloads[2]["load"] == pytest.approx(10.0)


def test_no_cgroup_file_falls_back_to_loadavg():
    payloads = _with_cgroup_samples([None, None], loadavg=1.0, cpu=2)
    assert payloads[0]["load_source"] == "loadavg"
    assert payloads[0]["load"] == pytest.approx(50.0)


def test_cgroup_counter_reset_falls_back():
    # usage went backwards (cgroup recreated) -> fallback to loadavg
    times = iter([0.0, 0.0, 10.0])
    calls = [("cgroup2", 500.0), ("cgroup2", 100.0)]
    with patch.object(relay_client.time, "monotonic", side_effect=lambda: next(times)):
        payloads = _with_cgroup_samples(calls, loadavg=0.5, cpu=1)
    assert payloads[1]["load_source"] == "loadavg"
    assert payloads[1]["load"] == pytest.approx(50.0)


def test_read_cgroup_v2_parses_real_file_format(tmp_path):
    f = tmp_path / "cpu.stat"
    f.write_text("usage_usec 76403678571\nuser_usec 1\nsystem_usec 2\n")
    with patch("builtins.open", return_value=open(f)):
        rung, usage = _read_cgroup_cpu_usage()
    assert rung == "cgroup2"
    assert usage == pytest.approx(76403.678571)


def test_load_pct_clamped_at_100():
    # 2 cores, 200% consumption -> clamped; 1st call seeds, 2nd measures
    times = iter([0.0, 0.0, 10.0])
    calls = [None, ("cgroup2", 0.0), ("cgroup2", 40.0)]  # 40s over 10s on 2 cores = 200%
    with patch.object(relay_client.time, "monotonic", side_effect=lambda: next(times)):
        payloads = _with_cgroup_samples(calls, cpu=2)
    assert payloads[2]["load"] == 100.0


# ── T-209 regression: loadavg path still clamps at 100 ───────────────

@pytest.mark.parametrize("loadavg,cpu,expected", [
    (1.0, 2, 50.0),
    (2.0, 2, 100.0),
    (2.63, 2, 100.0),   # der CT-902/906-Fall
    (8.0, 1, 100.0),    # single-core spike
    (0.5, 4, 12.5),
])
def test_heartbeat_loadavg_fallback_never_exceeds_100(loadavg, cpu, expected):
    payloads = _with_cgroup_samples([None, None], loadavg=loadavg, cpu=cpu)
    assert payloads[0]["load"] == pytest.approx(expected)
    assert payloads[0]["load"] <= 100.0