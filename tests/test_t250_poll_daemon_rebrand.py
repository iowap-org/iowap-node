"""T-250: poll-daemon move + daemon mode flag + iowap rebrand entry points.

Contract:
- The polling daemon (legacy claim/heartbeat loop) lives in
  ``nodes.common.poll_daemon``; the CLI carries NO daemon code (T-250):
  ``node-cli daemon {start,stop,status,foreground,restart}`` is an argparse
  usage error and node_cli re-exports none of the daemon names
  (PID_PATH/LOG_PATH/Daemon/_daemon_*). The poll daemon's self-spawn runs
  its own ``__main__``: ``python -m nodes.common.poll_daemon
  --daemon-internal`` (same pid file node-cli.pid, same log file
  node-cli.log, same SIGHUP).
- ``node-daemon``/``iowap-daemon`` accepts ``--mode {sse,poll}`` (default
  ``sse`` = exactly the pre-T-250 behaviour) and ``status`` / ``--status``
  (T-250: mode-aware status across both pid files — sees the running SSE
  daemon, rc 0, mode sse). ``--mode poll`` runs the moved polling daemon
  without ever touching the SSE connection. Requesting both modes (or
  --mode twice) fails closed: loud error, exit != 0, no process, no pid
  file.
- pyproject [project.scripts] gains ``iowap-cli`` and ``iowap-daemon`` while
  ``node-cli``/``node-daemon`` remain unchanged shims. A built wheel exposes
  all four console scripts (verified via importlib.metadata). The usage
  prog follows the invoked binary name (argv[0] basename): ``iowap-cli
  --help`` sights ``usage: iowap-cli …``, the legacy shim keeps ``node-cli``.
"""
from __future__ import annotations

import argparse
import importlib
import os
import subprocess
import sys
import tomllib
import zipfile
from importlib.metadata import PathDistribution
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nodes.common import node_cli, node_daemon, poll_daemon

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def daemon_state(tmp_path, monkeypatch):
    """Isolate every pid/log/status path the daemon modules touch."""
    monkeypatch.setattr(poll_daemon, "BASE_DIR", tmp_path, raising=False)
    monkeypatch.setattr(node_daemon, "BASE_DIR", tmp_path, raising=False)
    monkeypatch.setattr(node_daemon, "PID_PATH", tmp_path / "node-daemon.pid", raising=False)
    monkeypatch.setattr(poll_daemon, "PID_PATH", tmp_path / "node-cli.pid", raising=False)
    return tmp_path


class _FakeClient:
    """RelayClient stand-in: no network, no token, records construction."""

    instances: ClassVar[list[_FakeClient]] = []

    def __init__(self, meta, cfg):
        self.meta = meta
        self.cfg = cfg
        self.base_url = "http://127.0.0.1:1"
        _FakeClient.instances.append(self)


class _BoomDaemon:
    """Instantiating or running this means the wrong daemon mode ran."""

    def __init__(self, *a, **k):
        raise AssertionError(f"{type(self).__name__} must not be constructed")


class _FakeSseDaemon:
    """Records its run and verifies it owns the SSE pid file."""

    runs: ClassVar[list[dict]] = []

    def __init__(self, client, cfg):
        self.client = client
        self.cfg = cfg

    def run(self):
        _FakeSseDaemon.runs.append({
            "pid_file_content": node_daemon.PID_PATH.read_text(encoding="utf-8"),
        })


class _FakePollDaemon:
    """Records its run and verifies it owns the poll pid file."""

    runs: ClassVar[list[dict]] = []

    def __init__(self, client, cfg):
        self.client = client
        self.cfg = cfg

    def run(self):
        _FakePollDaemon.runs.append({
            "pid_file_content": poll_daemon.PID_PATH.read_text(encoding="utf-8"),
        })


@pytest.fixture(autouse=True)
def _reset_recorders():
    _FakeClient.instances.clear()
    _FakeSseDaemon.runs.clear()
    _FakePollDaemon.runs.clear()
    yield
    _FakeClient.instances.clear()
    _FakeSseDaemon.runs.clear()
    _FakePollDaemon.runs.clear()


def _wire_main(monkeypatch, tmp_path):
    """Fakes so node_daemon.main() never builds a real client / network."""
    monkeypatch.setattr(node_daemon, "load_meta", lambda: {"node_id": "t250"})
    monkeypatch.setattr(
        node_daemon, "_effective_config",
        lambda: {"heartbeat_interval": 1, "claim_interval": 1},
    )
    monkeypatch.setattr(node_daemon, "RelayClient", _FakeClient)
    return tmp_path


# ---------------------------------------------------------------------------
# Entry points (part 3: iowap rebrand shims)
# ---------------------------------------------------------------------------


def _scripts() -> dict[str, str]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return dict(data["project"]["scripts"])


def test_pyproject_declares_iowap_entrypoints():
    scripts = _scripts()
    assert scripts["iowap-cli"] == "nodes.common.node_cli:main"
    assert scripts["iowap-daemon"] == "nodes.common.node_daemon:main"


def test_legacy_shims_unchanged_in_pyproject():
    scripts = _scripts()
    assert scripts["node-cli"] == "nodes.common.node_cli:main"
    assert scripts["node-daemon"] == "nodes.common.node_daemon:main"


@pytest.mark.parametrize(
    "modname,fnname",
    [
        ("nodes.common.node_cli", "main"),
        ("nodes.common.node_daemon", "main"),
    ],
)
def test_entrypoint_callables_resolve(modname, fnname):
    mod = importlib.import_module(modname)
    assert callable(getattr(mod, fnname)), f"{modname}:{fnname} must be callable"


def test_wheel_exposes_all_four_console_scripts(tmp_path):
    """A wheel built from this tree must carry all four script entry points
    (read back through importlib.metadata)."""
    pytest.importorskip("hatchling", reason="wheel build needs hatchling")
    wheels_dir = tmp_path / "wheels"
    res = subprocess.run(
        [
            sys.executable, "-m", "pip", "wheel", str(REPO_ROOT),
            "--no-deps", "--no-build-isolation", "-w", str(wheels_dir),
        ],
        capture_output=True, text=True, timeout=300, check=False,
    )
    assert res.returncode == 0, f"wheel build failed:\n{res.stderr}"
    wheels = sorted(wheels_dir.glob("*.whl"))
    assert wheels, "no wheel produced"
    with zipfile.ZipFile(wheels[-1]) as zf:
        dist_info_dirs = {
            name.split(".dist-info/")[0] + ".dist-info/"
            for name in zf.namelist()
            if ".dist-info/" in name
        }
        assert len(dist_info_dirs) == 1
        info_dir = wheels_dir / dist_info_dirs.pop().rstrip("/")
        info_dir.mkdir()
        for name in zf.namelist():
            if name.startswith(info_dir.name + "/"):
                zf.extract(name, wheels_dir)
    dist = PathDistribution(info_dir)
    eps = {
        ep.name: ep.value for ep in dist.entry_points if ep.group == "console_scripts"
    }
    assert eps["iowap-cli"] == "nodes.common.node_cli:main"
    assert eps["iowap-daemon"] == "nodes.common.node_daemon:main"
    assert eps["node-cli"] == "nodes.common.node_cli:main"
    assert eps["node-daemon"] == "nodes.common.node_daemon:main"


def test_installed_dist_entrypoints_point_at_the_same_callables():
    """When the dist is installed (editable .venv), every console script it
    already exposes must keep pointing at the same callable."""
    try:
        from importlib.metadata import distribution
        dist = distribution("iowap-node")
    except Exception:  # noqa: BLE001 — CI installs no wheel; contract skips
        pytest.skip("iowap-node not installed in this environment")
    values = {
        ep.name: ep.value
        for ep in dist.entry_points
        if ep.group == "console_scripts"
    }
    expected = _scripts()
    for name, value in expected.items():
        if name in values:
            assert values[name] == value, (
                f"installed entry point {name} drifted from pyproject"
            )


# ---------------------------------------------------------------------------
# Part 2: --mode {sse,poll} on node-daemon (+ fail-closed double mode)
# ---------------------------------------------------------------------------


def test_mode_defaults_to_sse():
    args = node_daemon.build_parser().parse_args([])
    assert args.mode == "sse"


def test_mode_sse_explicit_parses():
    args = node_daemon.build_parser().parse_args(["--mode", "sse"])
    assert args.mode == "sse"


def test_mode_poll_parses():
    args = node_daemon.build_parser().parse_args(["--mode", "poll"])
    assert args.mode == "poll"


def test_mode_rejects_unknown_values():
    parser = node_daemon.build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--mode", "both"])
    assert exc.value.code != 0


# ---------------------------------------------------------------------------
# status mode (T-250): argv `status` / `--status` — mode-aware local status
# ---------------------------------------------------------------------------


def test_status_positional_parses():
    args = node_daemon.build_parser().parse_args(["status"])
    assert args.status == "status"
    assert args.status_flag is False


def test_status_flag_parses():
    args = node_daemon.build_parser().parse_args(["--status"])
    assert args.status is None
    assert args.status_flag is True


def test_status_positional_rejects_unknown_words():
    with pytest.raises(SystemExit) as exc:
        node_daemon.build_parser().parse_args(["bogus"])
    assert exc.value.code != 0


def test_status_mode_builds_no_client(daemon_state, monkeypatch):
    """`status` is a pure local read: it must never build a relay client —
    it works even when the client wiring would raise."""
    def _boom(*_a, **_k):
        raise AssertionError("status mode must not build a client")

    monkeypatch.setattr(node_daemon, "load_meta", _boom)
    assert node_daemon.main(["status"]) == 1  # nothing running (isolated state)


def test_double_mode_fails_closed(daemon_state, monkeypatch, capsys):
    """Two --mode flags = contradictory request: loud error, exit != 0, no
    process, no pid file, neither daemon constructed."""
    _wire_main(monkeypatch, daemon_state)
    monkeypatch.setattr(node_daemon, "SseDaemon", _BoomDaemon)
    monkeypatch.setattr(poll_daemon, "Daemon", _BoomDaemon)
    with pytest.raises(SystemExit) as exc:
        node_daemon.main(["--mode", "sse", "--mode", "poll"])
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "--mode" in err
    assert "more than once" in err, (
        f"fail-closed message missing for contradictory --mode: {err}"
    )
    assert not (daemon_state / "node-daemon.pid").exists()
    assert not (daemon_state / "node-cli.pid").exists()


def test_mode_poll_starts_poll_daemon_without_sse(daemon_state, monkeypatch):
    """--mode poll runs the moved polling daemon and never constructs the
    SSE daemon (no SSE connect, mocked away)."""
    _wire_main(monkeypatch, daemon_state)
    monkeypatch.setattr(node_daemon, "SseDaemon", _BoomDaemon)
    monkeypatch.setattr(poll_daemon, "Daemon", _FakePollDaemon)
    assert node_daemon.main(["--mode", "poll"]) == 0
    assert len(_FakePollDaemon.runs) == 1
    assert _FakePollDaemon.runs[0]["pid_file_content"] == f"{os.getpid()}\n"
    assert len(_FakeClient.instances) == 1
    assert not (daemon_state / "node-cli.pid").exists(), (
        "the poll process must unlink its own pid file on exit"
    )


def test_mode_poll_refuses_while_sse_daemon_runs(daemon_state, monkeypatch, capsys):
    """T-137b holds for the new surface: poll mode must not start while the
    SSE daemon is running."""
    _wire_main(monkeypatch, daemon_state)
    monkeypatch.setattr(node_daemon, "SseDaemon", _BoomDaemon)
    monkeypatch.setattr(poll_daemon, "Daemon", _BoomDaemon)
    (daemon_state / "node-daemon.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    rc = node_daemon.main(["--mode", "poll"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "refuses to start" in err
    assert "node-daemon is already running" in err
    assert not (daemon_state / "node-cli.pid").exists()


def test_default_and_explicit_sse_behaviour_unchanged(daemon_state, monkeypatch):
    """No --mode (and --mode sse) = exactly the pre-T-250 path: SseDaemon
    behind the node-daemon.pid lifecycle."""
    _wire_main(monkeypatch, daemon_state)
    monkeypatch.setattr(node_daemon, "SseDaemon", _FakeSseDaemon)
    monkeypatch.setattr(poll_daemon, "Daemon", _BoomDaemon)
    assert node_daemon.main([]) == 0
    assert len(_FakeSseDaemon.runs) == 1
    assert _FakeSseDaemon.runs[0]["pid_file_content"] == f"{os.getpid()}\n"


# ---------------------------------------------------------------------------
# Part 1: daemon-code removal from the CLI (T-250) — poll_daemon surface
# ---------------------------------------------------------------------------

_DAEMON_ACTIONS = ("start", "stop", "status", "restart", "foreground")


def test_pid_and_log_file_names_unchanged():
    assert poll_daemon.PID_PATH.name == "node-cli.pid"
    assert poll_daemon.LOG_PATH.name == "node-cli.log"


@pytest.mark.parametrize("name", ("PID_PATH", "LOG_PATH", "Daemon", "_daemon_internal"))
def test_node_cli_reexports_no_daemon_names(name):
    """T-250: the CLI is a pure tool — no daemon surface leaks through
    node_cli (nothing may import Daemon/PID_PATH/… from it anymore)."""
    assert not hasattr(node_cli, name), f"node_cli.{name} must not exist"


@pytest.mark.parametrize("action", _DAEMON_ACTIONS)
def test_node_cli_daemon_tree_is_gone(daemon_state, action, capsys):
    """``node-cli daemon <action>`` no longer exists: argparse usage error
    (invalid choice), never a daemon call, no pid file touched."""
    with pytest.raises(SystemExit) as exc:
        node_cli.main(["daemon", action])
    assert exc.value.code != 0
    assert "invalid choice: 'daemon'" in capsys.readouterr().err
    assert not (daemon_state / "node-cli.pid").exists()
    assert not (daemon_state / "node-daemon.pid").exists()


def test_node_cli_daemon_internal_flag_is_gone(daemon_state):
    """The CLI's hidden --daemon-internal self-spawn flag is gone with the
    daemon code — an unrecognized-argument usage error."""
    with pytest.raises(SystemExit) as exc:
        node_cli.main(["--daemon-internal"])
    assert exc.value.code != 0
    assert not (daemon_state / "node-cli.pid").exists()


def test_daemon_start_spawns_through_the_poll_daemon_module(
    daemon_state, monkeypatch, capsys
):
    """The poll daemon's self-spawn must go over its own module
    (``python -m nodes.common.poll_daemon --daemon-internal``) — the CLI no
    longer offers a --daemon-internal entry point to spawn through."""
    monkeypatch.setattr(
        poll_daemon, "subprocess", MagicMock(name="subprocess"),
    )
    proc = poll_daemon.subprocess.Popen.return_value
    proc.poll.return_value = None
    daemon_state.joinpath("node-cli.pid").write_text("12345\n", encoding="utf-8")
    rc = poll_daemon._daemon_start(argparse.Namespace(log_level=None))
    assert rc == 0
    argv = poll_daemon.subprocess.Popen.call_args[0][0]
    assert argv == [sys.executable, "-m", "nodes.common.poll_daemon", "--daemon-internal"]
    kwargs = poll_daemon.subprocess.Popen.call_args[1]
    assert kwargs["start_new_session"] is True
    assert "daemon started" in capsys.readouterr().out


def test_daemon_stop_reports_not_running(daemon_state, capsys):
    assert poll_daemon._daemon_stop(argparse.Namespace(log_level=None)) == 0
    assert "daemon not running" in capsys.readouterr().out


def test_daemon_status_empty_output_shape(daemon_state, capsys):
    rc = node_daemon.main(["status"])
    out = capsys.readouterr().out
    assert rc == 1
    assert out.startswith("pid: -\nmode: -\nrunning: False\n")


def test_daemon_status_flag_form_same_output_shape(daemon_state, capsys):
    """--status is the flag form of the positional status mode: same
    output, same exit-code contract."""
    rc = node_daemon.main(["--status"])
    out = capsys.readouterr().out
    assert rc == 1
    assert out.startswith("pid: -\nmode: -\nrunning: False\n")


def test_daemon_status_sees_running_sse_daemon(daemon_state, monkeypatch, capsys):
    """T-250: the consolidated daemon usually runs in SSE mode (pid file
    node-daemon.pid) — `iowap-daemon status` must report it as running with
    the mode (acceptance: rc 0, mode sse)."""
    sse_pid = node_daemon.PID_PATH
    sse_pid.write_text(str(os.getpid()) + "\n", encoding="utf-8")  # this test process is alive
    rc = node_daemon.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    lines = out.splitlines()
    assert f"pid: {os.getpid()}" in lines[0]
    assert "mode: sse" in lines[1]
    assert "running: True" in lines[2]


def test_daemon_status_prefers_poll_mode_when_both_pids_alive(
    daemon_state, monkeypatch, capsys
):
    """Both pid files alive → poll wins; T-137b forbids this state in
    production, status just reports honestly."""
    node_daemon.PID_PATH.write_text(str(os.getpid()) + "\n", encoding="utf-8")
    poll_pid = poll_daemon.PID_PATH
    poll_pid.write_text(str(os.getpid()) + "\n", encoding="utf-8")
    rc = node_daemon.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "mode: poll" in out.splitlines()[1]


# ---------------------------------------------------------------------------
# Part 1b: usage prog follows the invoked binary (T-250 cosmetics, owner
# addition) — argv[0] basename, fallback 'node-cli'
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv0", "expected"),
    [
        ("/usr/local/bin/iowap-cli", "iowap-cli"),
        ("/usr/bin/node-cli", "node-cli"),
        ("iowap-cli", "iowap-cli"),
    ],
)
def test_usage_prog_follows_the_invoked_binary(monkeypatch, argv0, expected):
    monkeypatch.setattr(sys, "argv", [argv0])
    assert node_cli.build_parser().prog == expected


def test_usage_prog_falls_back_to_node_cli(monkeypatch):
    monkeypatch.setattr(sys, "argv", [""])
    assert node_cli.build_parser().prog == "node-cli"


def test_usage_prog_can_be_forced_explicitly(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["/usr/bin/pytest"])
    assert node_cli.build_parser(prog="node-cli").prog == "node-cli"


def test_iowap_cli_help_sights_iowap_cli(monkeypatch, capsys):
    """End-to-end: `iowap-cli --help` prints `usage: iowap-cli …`."""
    monkeypatch.setattr(sys, "argv", ["/usr/local/bin/iowap-cli"])
    with pytest.raises(SystemExit) as exc:
        node_cli.main(["--help"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.lstrip().startswith("usage: iowap-cli")


def test_daemon_label_follows_invoked_binary(daemon_state, monkeypatch):
    """T-250: worker_status.json 'daemon' names the invoked binary —
    iowap-daemon for the rebranded entry point, node-daemon for legacy."""
    monkeypatch.setattr(sys, "argv", ["/venv/bin/iowap-daemon", "--foreground", "--mode", "sse"])
    assert node_daemon._daemon_label() == "iowap-daemon"
    monkeypatch.setattr(sys, "argv", ["/venv/bin/node-daemon", "--foreground"])
    assert node_daemon._daemon_label() == "node-daemon"
    monkeypatch.setattr(sys, "argv", ["/venv/bin/python", "-m", "nodes.common.node_daemon"])
    assert node_daemon._daemon_label() == "node-daemon"


def test_log_label_follows_invoked_binary(daemon_state, monkeypatch):
    """T-250: the journal label names the invoked binary, shims keep legacy."""
    from nodes.common.relay_client import _log_label
    monkeypatch.setattr(sys, "argv", ["/venv/bin/iowap-daemon", "--foreground", "--mode", "sse"])
    assert _log_label() == "iowap-daemon"
    monkeypatch.setattr(sys, "argv", ["/venv/bin/iowap-cli", "server", "health"])
    assert _log_label() == "iowap-cli"
    monkeypatch.setattr(sys, "argv", ["/venv/bin/node-daemon", "--foreground"])
    assert _log_label() == "node-daemon"
    monkeypatch.setattr(sys, "argv", ["/venv/bin/python", "-m", "nodes.common.node_cli"])
    assert _log_label() == "node-cli"


def test_daemon_parser_prog_follows_invoked_binary(daemon_state, monkeypatch):
    """iowap-daemon --help sights `usage: iowap-daemon …`; the legacy shim
    keeps `usage: node-daemon`."""
    monkeypatch.setattr(sys, "argv", ["/venv/bin/iowap-daemon", "status"])
    out = node_daemon.build_parser().format_help().splitlines()[0]
    assert "usage: iowap-daemon" in out
    monkeypatch.setattr(sys, "argv", ["/venv/bin/node-daemon", "--foreground"])
    out = node_daemon.build_parser().format_help().splitlines()[0]
    assert "usage: node-daemon" in out
