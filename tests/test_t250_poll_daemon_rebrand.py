"""T-250: poll-daemon move + daemon mode flag + iowap rebrand entry points.

Contract:
- The polling daemon (legacy claim/heartbeat loop) lives in
  ``nodes.common.poll_daemon``; ``node-cli daemon {start,stop,status,
  foreground,restart}`` stays byte-identical: same pid file (node-cli.pid),
  same log file (node-cli.log), same self-spawn command, same SIGHUP.
- ``node-daemon``/``iowap-daemon`` accepts ``--mode {sse,poll}`` (default
  ``sse`` = exactly the pre-T-250 behaviour). ``--mode poll`` runs the moved
  polling daemon without ever touching the SSE connection. Requesting both
  modes (or --mode twice) fails closed: loud error, exit != 0, no process,
  no pid file.
- pyproject [project.scripts] gains ``iowap-cli`` and ``iowap-daemon`` while
  ``node-cli``/``node-daemon`` remain unchanged shims. A built wheel exposes
  all four console scripts (verified via importlib.metadata).
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tomllib
import zipfile
from importlib.metadata import PathDistribution
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nodes.common import node_cli, node_daemon, poll_daemon

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def daemon_state(tmp_path, monkeypatch):
    """Isolate every pid/log/status path both modules touch."""
    for mod in (node_cli, node_daemon, poll_daemon):
        monkeypatch.setattr(mod, "BASE_DIR", tmp_path, raising=False)
        if hasattr(mod, "PID_PATH"):
            monkeypatch.setattr(
                mod, "PID_PATH", tmp_path / f"{mod.__name__.split('.')[-1]}.pid",
                raising=False,
            )
    monkeypatch.setattr(node_cli, "PID_PATH", tmp_path / "node-cli.pid")
    monkeypatch.setattr(node_daemon, "PID_PATH", tmp_path / "node-daemon.pid")
    monkeypatch.setattr(poll_daemon, "PID_PATH", tmp_path / "node-cli.pid")
    return tmp_path


class _FakeClient:
    """RelayClient stand-in: no network, no token, records construction."""

    instances: list[_FakeClient] = []

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

    runs: list[dict] = []

    def __init__(self, client, cfg):
        self.client = client
        self.cfg = cfg

    def run(self):
        _FakeSseDaemon.runs.append({
            "pid_file_content": node_daemon.PID_PATH.read_text(encoding="utf-8"),
        })


class _FakePollDaemon:
    """Records its run and verifies it owns the poll pid file."""

    runs: list[dict] = []

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
# Part 1: node-cli daemon regression — byte-identical surface
# ---------------------------------------------------------------------------


def test_pid_and_log_file_names_unchanged():
    assert poll_daemon.PID_PATH.name == node_cli.PID_PATH.name == "node-cli.pid"
    assert poll_daemon.LOG_PATH.name == node_cli.LOG_PATH.name == "node-cli.log"
    # single source of truth: node_cli re-exports the moved module's paths
    assert node_cli.PID_PATH is poll_daemon.PID_PATH


def test_daemon_start_spawns_the_same_inner_command(daemon_state, monkeypatch, capsys):
    """`node-cli daemon start` must self-spawn exactly as before T-250."""
    monkeypatch.setattr(
        poll_daemon, "subprocess", MagicMock(name="subprocess"),
    )
    proc = poll_daemon.subprocess.Popen.return_value
    proc.poll.return_value = None
    daemon_state.joinpath("node-cli.pid").write_text("12345\n", encoding="utf-8")
    rc = node_cli.main(["daemon", "start"])
    assert rc == 0
    argv = poll_daemon.subprocess.Popen.call_args[0][0]
    assert argv == [sys.executable, "-m", "nodes.common.node_cli", "--daemon-internal"]
    kwargs = poll_daemon.subprocess.Popen.call_args[1]
    assert kwargs["start_new_session"] is True
    assert "daemon started" in capsys.readouterr().out


def test_daemon_stop_reports_not_running(daemon_state, capsys):
    assert node_cli.main(["daemon", "stop"]) == 0
    assert "daemon not running" in capsys.readouterr().out


def test_daemon_status_empty_output_shape(daemon_state, capsys):
    rc = node_cli.main(["daemon", "status"])
    out = capsys.readouterr().out
    assert rc == 1
    assert out.startswith("pid: -\nrunning: False\n")
