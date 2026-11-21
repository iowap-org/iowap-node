"""T-011: blank-start mode — the daemon idles without relay configuration.

Contract:

* With no relay configuration (no iowap-agent.json / no runtime token /
  no explicit base_url), ``iowap-daemon`` does NOT exit with the previous
  "no base_url configured" SystemExit. It idles: no relay traffic, no
  crash, and a worker_status.json that marks the blank start.
* As soon as the configuration exists (register writes the files, or the
  operator runs ``iowap-cli reload`` → SIGHUP), the idle loop picks it up
  and the daemon proceeds with its normal startup path.
* SIGTERM/SIGINT stop the idle daemon cleanly (exit 0). SIGHUP triggers
  an immediate re-check instead of killing the process.
* The idle check only accepts an EXPLICIT base_url (env, relay_config.json
  or the state file) — mDNS discovery never engages from the idle loop,
  so a blank node sends no network traffic at all.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nodes.common import node_daemon, node_utils, poll_daemon


@pytest.fixture()
def blank_env(tmp_path, monkeypatch):
    """Isolate every state path the idle loop touches."""
    monkeypatch.setattr(node_utils, "META_PATH", tmp_path / "iowap-agent.json")
    monkeypatch.setattr(node_utils, "LEGACY_META_PATH", tmp_path / "legacy.json")
    monkeypatch.setattr(node_utils, "TOKEN_PATH", tmp_path / "iowap-agent.token")
    monkeypatch.setattr(node_utils, "LEGACY_TOKEN_PATH", tmp_path / "legacy.token")
    monkeypatch.setattr(node_utils, "CONFIG_PATH", tmp_path / "relay_config.json")
    monkeypatch.setattr(node_utils, "STATUS_PATH", tmp_path / "worker_status.json")
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    # T-137b guard: both daemons probe the OTHER daemon's pid file under
    # BASE_DIR — isolate so a live host daemon cannot fail the test.
    monkeypatch.setattr(node_daemon, "BASE_DIR", tmp_path, raising=False)
    monkeypatch.setattr(poll_daemon, "BASE_DIR", tmp_path, raising=False)
    monkeypatch.setattr(node_daemon, "PID_PATH", tmp_path / "node-daemon.pid", raising=False)
    monkeypatch.setattr(poll_daemon, "PID_PATH", tmp_path / "node-cli.pid", raising=False)
    return tmp_path


def _write_full_config(tmp_path: Path) -> None:
    (tmp_path / "iowap-agent.json").write_text(
        json.dumps({"node_id": "N1", "registration_secret": "s3cret"}), encoding="utf-8"
    )
    (tmp_path / "iowap-agent.token").write_text(
        json.dumps({"token": "tok-123", "expires_at": "2099-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    (tmp_path / "relay_config.json").write_text(
        json.dumps({"base_url": "http://relay.test:8788"}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# wait_for_relay_config
# ---------------------------------------------------------------------------


def test_blank_start_idles_and_writes_status(blank_env):
    stop = threading.Event()
    timer = threading.Timer(0.3, stop.set)
    timer.start()
    try:
        ready = node_utils.wait_for_relay_config(poll_seconds=0.05, stop_event=stop)
    finally:
        timer.join()

    assert ready is None
    status = json.loads(node_utils.STATUS_PATH.read_text(encoding="utf-8"))
    assert status.get("blank_start") is True
    assert "configuration" in status.get("error", "").lower()


def test_blank_start_picks_up_config_when_it_appears(blank_env):
    stop = threading.Event()
    timer = threading.Timer(0.15, lambda: _write_full_config(blank_env))
    timer.start()
    try:
        ready = node_utils.wait_for_relay_config(poll_seconds=0.05, stop_event=stop)
    finally:
        timer.join()
        stop.set()

    assert ready is not None
    meta, cfg = ready
    assert meta["node_id"] == "N1"
    assert cfg["base_url"] == "http://relay.test:8788"


def test_blank_start_idle_sends_no_traffic(blank_env, monkeypatch):
    """No HTTP from the idle loop: mDNS discovery never engages."""

    def boom(*a, **k):
        raise AssertionError("idle loop must not touch the network")

    monkeypatch.setattr("httpx.get", boom)
    monkeypatch.setattr("httpx.post", boom)
    stop = threading.Event()
    timer = threading.Timer(0.3, stop.set)
    timer.start()
    try:
        assert node_utils.wait_for_relay_config(poll_seconds=0.05, stop_event=stop) is None
    finally:
        timer.join()


def test_blank_start_meta_without_base_url_stays_idle(blank_env):
    _write_full_config(blank_env)
    # remove the explicit base_url → NOT ready (discovery must not engage)
    (blank_env / "relay_config.json").write_text("{}", encoding="utf-8")
    import json as _json

    meta = json.loads((blank_env / "iowap-agent.json").read_text())
    meta.pop("base_url", None)
    (blank_env / "iowap-agent.json").write_text(_json.dumps(meta), encoding="utf-8")

    stop = threading.Event()
    timer = threading.Timer(0.2, stop.set)
    timer.start()
    try:
        assert node_utils.wait_for_relay_config(poll_seconds=0.05, stop_event=stop) is None
    finally:
        timer.join()


def test_blank_start_sighup_wakes_the_idle_loop(blank_env):
    import os
    import signal

    def appear():
        _write_full_config(blank_env)
        os.kill(os.getpid(), signal.SIGHUP)

    timer = threading.Timer(0.15, appear)
    timer.start()
    try:
        ready = node_utils.wait_for_relay_config(poll_seconds=30.0)
    finally:
        timer.join()

    assert ready is not None  # SIGHUP triggered an immediate re-check


# ---------------------------------------------------------------------------
# main() wiring — SSE daemon + poll mode both go through the idle gate
# ---------------------------------------------------------------------------


def test_main_blank_start_returns_zero_without_client(blank_env, monkeypatch):
    def no_client(*a, **k):
        raise AssertionError("RelayClient must not be constructed while blank")

    monkeypatch.setattr(node_daemon, "RelayClient", no_client)
    monkeypatch.setattr(node_daemon, "SseDaemon", no_client)

    # no config on disk → idle forever unless stopped; stop via event
    stop = threading.Event()

    def fake_wait(**kwargs):
        ev = kwargs.get("stop_event")
        if ev is not None:
            ev.wait(5)

    monkeypatch.setattr(node_daemon, "wait_for_relay_config", fake_wait)

    result: list[int] = []

    def run_main():
        result.append(node_daemon.main(["--log-level", "ERROR"]))

    thread = threading.Thread(target=run_main, daemon=True)
    thread.start()
    time.sleep(0.2)
    stop.set()
    thread.join(timeout=5)

    assert result == [0]


def test_main_starts_normally_after_blank_start(blank_env, monkeypatch):
    started = {}
    meta = {"node_id": "N1", "registration_secret": "s"}
    cfg = {"base_url": "http://relay.test:8788", "heartbeat_interval": 30}

    class FakeClient:
        def __init__(self, m, c):
            started["client"] = (m, c)

    class FakeSseDaemon:
        def __init__(self, client, cfg):
            started["daemon"] = True

        def run(self):
            started["ran"] = True

    monkeypatch.setattr(node_daemon, "RelayClient", FakeClient)
    monkeypatch.setattr(node_daemon, "SseDaemon", FakeSseDaemon)
    monkeypatch.setattr(
        node_daemon,
        "wait_for_relay_config",
        lambda **k: (meta, cfg),
    )

    rc = node_daemon.main(["--log-level", "ERROR"])

    assert rc == 0
    assert started["ran"] is True
    assert started["client"][0] == meta
    # pid file cleaned up after the run
    assert not node_daemon.PID_PATH.exists()


def test_poll_mode_goes_through_idle_gate(blank_env, monkeypatch):
    meta = {"node_id": "N1", "registration_secret": "s"}
    cfg = {"base_url": "http://relay.test:8788", "heartbeat_interval": 30}

    class FakeClient:
        def __init__(self, m, c):
            self.meta, self.cfg = m, c

    class FakePollDaemon:
        def __init__(self, client, cfg):
            started["daemon"] = True

        def run(self):
            started["ran"] = True

    started: dict = {}
    monkeypatch.setattr(node_daemon, "RelayClient", FakeClient)
    monkeypatch.setattr(poll_daemon, "Daemon", FakePollDaemon)
    monkeypatch.setattr(
        node_daemon,
        "wait_for_relay_config",
        lambda **k: (meta, cfg),
    )

    rc = node_daemon.main(["--log-level", "ERROR", "--mode", "poll"])

    assert rc == 0
    assert started["ran"] is True


def test_poll_daemon_internal_blank_start_idles(blank_env, monkeypatch):
    def no_client(*a, **k):
        raise AssertionError("RelayClient must not be constructed while blank")

    monkeypatch.setattr(poll_daemon, "RelayClient", no_client)
    monkeypatch.setattr(
        poll_daemon,
        "wait_for_relay_config",
        lambda **k: None,
    )
    rc = poll_daemon._daemon_internal()
    assert rc == 0
