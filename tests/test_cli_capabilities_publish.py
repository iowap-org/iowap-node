"""Regression tests: `capabilities publish` / `node-cli reload` SIGHUP target.

t_c214ca25: the publisher resolved the polling CLI daemon's pid file
(``node-cli.pid``) only, while the active SSE daemon writes
``node-daemon.pid`` — hot-reload after a real profile publish never reached
the running daemon. The fix notifies every known daemon pid file whose pid
is alive (SSE first, polling legacy fallback; both install SIGHUP handlers).
"""

from __future__ import annotations

import argparse
import os
import signal
import types

import pytest

from nodes.common import node_cli, node_config, node_daemon
from nodes.common.cli import cli_capabilities

PROFILE_YAML = """\
capabilities:
  - name: test.cap
    version: "1.0.0"
    handler: /bin/true
"""


@pytest.fixture()
def relay_home(tmp_path, monkeypatch):
    """Isolate every relay state path; tests never touch ~/.relay."""
    monkeypatch.setattr(node_config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(node_config, "PROFILES_DIR", tmp_path / "profiles.d")
    monkeypatch.setattr(node_config, "ACTIVE_PATH", tmp_path / "node.yaml")
    monkeypatch.setattr(
        node_config, "ACTIVE_PROFILE_NAME_PATH", tmp_path / "node.profile"
    )
    monkeypatch.setattr(node_cli, "PID_PATH", tmp_path / "node-cli.pid")
    monkeypatch.setattr(node_daemon, "PID_PATH", tmp_path / "node-daemon.pid")
    return tmp_path


@pytest.fixture()
def kill_probe(monkeypatch):
    """Replace os.kill: sig 0 probes an `alive` set, real signals recorded."""
    sent: list[tuple[int, int]] = []
    alive: set[int] = set()

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            # Liveness probe: alive -> no-op, dead -> ProcessLookupError
            # (subclass of OSError, matches pid_running() expectations).
            if pid not in alive:
                raise ProcessLookupError(pid)
            return
        sent.append((pid, sig))

    monkeypatch.setattr(os, "kill", fake_kill)
    return sent, alive


def _write_pid(path, pid: int) -> None:
    path.write_text(f"{pid}\n", encoding="utf-8")


def _publish_args(profile: str = "default") -> types.SimpleNamespace:
    return types.SimpleNamespace(profile=profile, log_level="ERROR", json=False)


# ---------------------------------------------------------------------------
# capabilities publish
# ---------------------------------------------------------------------------


def test_publish_sighups_sse_daemon(tmp_path, relay_home, kill_probe, capsys):
    """The active SSE daemon's node-daemon.pid must receive the SIGHUP."""
    sent, alive = kill_probe
    profiles = tmp_path / "profiles.d"
    profiles.mkdir()
    (profiles / "default.yaml").write_text(PROFILE_YAML, encoding="utf-8")
    alive.add(os.getpid())
    _write_pid(tmp_path / "node-daemon.pid", os.getpid())

    rc = cli_capabilities._cmd_capabilities_publish(_publish_args())

    assert rc == 0
    assert sent == [(os.getpid(), signal.SIGHUP)]
    out = capsys.readouterr().out
    assert f"(sent SIGHUP to pid {os.getpid()})" in out
    assert "daemon not running" not in out


def test_publish_sighups_legacy_polling_daemon(
    tmp_path, relay_home, kill_probe, capsys
):
    """Fallback: a running polling daemon (node-cli.pid) is notified too."""
    sent, alive = kill_probe
    profiles = tmp_path / "profiles.d"
    profiles.mkdir()
    (profiles / "default.yaml").write_text(PROFILE_YAML, encoding="utf-8")
    alive.add(os.getpid())
    _write_pid(tmp_path / "node-cli.pid", os.getpid())

    rc = cli_capabilities._cmd_capabilities_publish(_publish_args())

    assert rc == 0
    assert sent == [(os.getpid(), signal.SIGHUP)]
    out = capsys.readouterr().out
    assert f"(sent SIGHUP to pid {os.getpid()})" in out


def test_publish_sighups_both_daemons(tmp_path, relay_home, kill_probe, capsys):
    """Belt & suspenders: both pid files live -> both get SIGHUP, one line."""
    sent, alive = kill_probe
    profiles = tmp_path / "profiles.d"
    profiles.mkdir()
    (profiles / "default.yaml").write_text(PROFILE_YAML, encoding="utf-8")
    other_pid = 424242
    alive.update({os.getpid(), other_pid})
    _write_pid(tmp_path / "node-daemon.pid", os.getpid())
    _write_pid(tmp_path / "node-cli.pid", other_pid)

    rc = cli_capabilities._cmd_capabilities_publish(_publish_args())

    assert rc == 0
    assert sent == [
        (os.getpid(), signal.SIGHUP),
        (other_pid, signal.SIGHUP),
    ]
    out = capsys.readouterr().out
    assert f"(sent SIGHUP to pid {os.getpid()}, {other_pid})" in out


def test_publish_stale_pid_reports_no_daemon(
    tmp_path, relay_home, kill_probe, capsys
):
    """A pid file whose process is dead must take the no-daemon branch."""
    sent, _alive = kill_probe
    profiles = tmp_path / "profiles.d"
    profiles.mkdir()
    (profiles / "default.yaml").write_text(PROFILE_YAML, encoding="utf-8")
    # alive stays empty -> pid in the file is stale.
    _write_pid(tmp_path / "node-daemon.pid", os.getpid())

    rc = cli_capabilities._cmd_capabilities_publish(_publish_args())

    assert rc == 0
    assert sent == []
    out = capsys.readouterr().out
    assert "daemon not running" in out


def test_publish_without_pid_files_reports_no_daemon(
    tmp_path, relay_home, kill_probe, capsys
):
    sent, _alive = kill_probe
    profiles = tmp_path / "profiles.d"
    profiles.mkdir()
    (profiles / "default.yaml").write_text(PROFILE_YAML, encoding="utf-8")

    rc = cli_capabilities._cmd_capabilities_publish(_publish_args())

    assert rc == 0
    assert sent == []
    assert "daemon not running" in capsys.readouterr().out


def test_publish_garbage_pid_file_reports_no_daemon(
    tmp_path, relay_home, kill_probe, capsys
):
    """An unparsable pid file must not crash the publish."""
    sent, _alive = kill_probe
    profiles = tmp_path / "profiles.d"
    profiles.mkdir()
    (profiles / "default.yaml").write_text(PROFILE_YAML, encoding="utf-8")
    (tmp_path / "node-daemon.pid").write_text("not-a-pid\n", encoding="utf-8")

    rc = cli_capabilities._cmd_capabilities_publish(_publish_args())

    assert rc == 0
    assert sent == []
    assert "daemon not running" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# node-cli reload (same wrong-pidfile class, fixed for consistency)
# ---------------------------------------------------------------------------


def test_reload_sighups_sse_daemon(relay_home, kill_probe, capsys):
    sent, alive = kill_probe
    alive.add(os.getpid())
    _write_pid(relay_home / "node-daemon.pid", os.getpid())

    rc = node_cli._cmd_reload(argparse.Namespace())

    assert rc == 0
    assert sent == [(os.getpid(), signal.SIGHUP)]
    assert f"SIGHUP sent to daemon (pid {os.getpid()})" in capsys.readouterr().out


def test_reload_falls_back_to_polling_daemon(relay_home, kill_probe, capsys):
    sent, alive = kill_probe
    alive.add(os.getpid())
    _write_pid(relay_home / "node-cli.pid", os.getpid())

    rc = node_cli._cmd_reload(argparse.Namespace())

    assert rc == 0
    assert sent == [(os.getpid(), signal.SIGHUP)]


def test_reload_without_daemon_fails(relay_home, kill_probe, capsys):
    sent, _alive = kill_probe
    _write_pid(relay_home / "node-daemon.pid", os.getpid())  # stale

    rc = node_cli._cmd_reload(argparse.Namespace())

    assert rc == 1
    assert sent == []
    assert "daemon not running" in capsys.readouterr().err