"""T-226 (node): node-cli self-stamps its own absolute path into the profile.

Contract (Ronny, 2026-10-16): consumers (the iowap-hermes-integration
desktop plugin, the iowap-task session bridge) must be able to exec
node-cli WITHOUT assuming a PATH or a known install location (live
finding: the NovaForge/mac gateway resolves neither login-shell PATH —
``which: node-cli not found`` — nor any candidate, while the binary
lives at ``~/iowap-node/.venv/bin/node-cli``).

Rules:
* Every node-cli invocation (``main``) and the daemon start (``run``)
  ensure a top-level ``node_cli_path`` key in ``~/.relay/node.yaml``.
* ``capabilities publish`` re-stamps AFTER the byte-exact copy — the
  key self-heals through validate→publish without being part of the
  working profile.
* The stamp is text-preserving (comments/key order kept verbatim,
  same discipline as ``write_active_status``), drift-guarded
  (idempotent: no rewrite — no mtime churn — while the value matches;
  the daemon's ActiveProfileCache reloads on mtime change) and never
  raises.
* Value derivation prefers the venv console script next to the running
  interpreter (works for ``python -m nodes...`` invocations), falls
  back to argv[0] that IS the cli script, then to ``shutil.which``.
* A path that is not the node-cli at all must never be stamped.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nodes.common import node_config, node_cli, node_daemon  # noqa: E402
from nodes.common.node_config import (  # noqa: E402
    ensure_node_cli_stamp,
    publish_profile,
    resolve_own_cli_path,
)

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


def _write_active(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _stamp_value(path: Path) -> str | None:
    import re

    m = re.search(r"^node_cli_path\s*:\s*(.+?)\s*$", path.read_text(), re.M)
    return m.group(1).strip().strip("'\"") if m else None


def _fake_venv(tmp_path: Path) -> tuple[Path, Path]:
    """A venv-shaped dir: bin/python + X_OK console script node-cli."""
    venv = tmp_path / "fakevenv"
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True)
    script = bin_dir / "node-cli"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    (bin_dir / "python").write_text("#!/bin/sh\n")
    (bin_dir / "python").chmod(0o755)
    return bin_dir / "python", script


# ------------------------------------------------------------ stamp core


def test_stamp_writes_quoted_key_to_active_yaml(relay_home):
    active = node_config.ACTIVE_PATH
    _write_active(active, PROFILE_YAML)

    ensure_node_cli_stamp(stamp="/fake/venv/bin/node-cli")

    assert _stamp_value(active) == "/fake/venv/bin/node-cli"
    # the document stays valid: capabilities untouched (invalid → raises)
    caps = node_config.validate_profile(active)
    assert [c["name"] for c in caps] == ["test.cap"]
    assert "test.cap" in active.read_text()


def test_stamp_is_text_preserving(relay_home):
    active = node_config.ACTIVE_PATH
    original = "# hand comment\nnode_name: NODE23A4\n\ncapabilities: []\n"
    _write_active(active, original)

    ensure_node_cli_stamp(stamp="/x/node-cli")

    new = active.read_text()
    assert "# hand comment" in new
    assert "node_name: NODE23A4" in new
    expect_added = f'node_cli_path: "/x/node-cli"\n'
    assert new == expect_added + original


def test_stamp_is_idempotent_no_mtime_churn(relay_home):
    active = node_config.ACTIVE_PATH
    _write_active(active, PROFILE_YAML)

    ensure_node_cli_stamp(stamp="/x/node-cli")
    first = active.stat().st_mtime_ns
    # same value again: drift guard must not rewrite the file — the
    # daemon's ActiveProfileCache would reload its profile on mtime change
    ensure_node_cli_stamp(stamp="/x/node-cli")
    assert active.stat().st_mtime_ns == first


def test_stamp_replaces_drifted_value_once(relay_home):
    active = node_config.ACTIVE_PATH
    _write_active(active, f'node_cli_path: "/old/node-cli"\n{PROFILE_YAML}')

    ensure_node_cli_stamp(stamp="/new/node-cli")

    text = active.read_text()
    assert _stamp_value(active) == "/new/node-cli"
    assert "/old/node-cli" not in text
    assert text.count("node_cli_path") == 1


def test_stamp_noop_without_active_file(relay_home):
    ensure_node_cli_stamp(stamp="/x/node-cli")
    assert not node_config.ACTIVE_PATH.exists()


def test_stamp_never_raises_and_never_stamps_garbage(
    relay_home, tmp_path, monkeypatch
):
    active = node_config.ACTIVE_PATH
    _write_active(active, PROFILE_YAML)
    before = active.stat().st_mtime_ns
    # sandbox every derivation path: no venv sibling, unrelated argv0,
    # PATH yields nothing → no stamp value → no write
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python").write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "bin" / "python"))
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "runner.py"), "-q"])
    monkeypatch.setattr(node_config.shutil, "which", lambda _p: None)

    ensure_node_cli_stamp(stamp="")     # empty → no-op
    ensure_node_cli_stamp(stamp=None)   # no derivable path → no-op
    assert active.stat().st_mtime_ns == before


# ------------------------------------------------------------ path derivation


def test_resolve_prefers_venv_console_script(
    relay_home, tmp_path, monkeypatch
):
    py, script = _fake_venv(tmp_path)
    monkeypatch.setattr(sys, "executable", str(py))
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "x.py"), "-q"])

    assert resolve_own_cli_path() == str(Path(os.path.realpath(script)))


def test_resolve_falls_back_to_argv0_node_cli(
    relay_home, tmp_path, monkeypatch
):
    # interpreter without a sibling console script (e.g. module invocation
    # in a venv whose entrypoint is elsewhere)
    (tmp_path / "bin").mkdir(parents=True)
    (tmp_path / "bin" / "python").write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "bin" / "python"))

    script = tmp_path / "node-cli"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(script), "node", "list"])

    assert resolve_own_cli_path() == str(Path(os.path.realpath(script)))


def test_resolve_falls_back_to_which(relay_home, tmp_path, monkeypatch):
    (tmp_path / "bin").mkdir(parents=True)
    (tmp_path / "bin" / "python").write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "bin" / "python"))
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "runner.py"), "-q"])

    which_path = tmp_path / "which-dir" / "node-cli"
    which_path.parent.mkdir()
    which_path.write_text("#!/bin/sh\n")
    which_path.chmod(0o755)
    monkeypatch.setattr(node_config.shutil, "which", lambda _p: str(which_path))

    assert resolve_own_cli_path() == str(Path(os.path.realpath(which_path)))


def test_resolve_rejects_unrelated_argv0(relay_home, tmp_path, monkeypatch):
    (tmp_path / "bin").mkdir(parents=True)
    (tmp_path / "bin" / "python").write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "bin" / "python"))
    bogus = tmp_path / "pytest-bin"
    bogus.write_text("#!/bin/sh\n")
    bogus.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(bogus), "-q"])
    monkeypatch.setattr(node_config.shutil, "which", lambda _p: None)

    assert resolve_own_cli_path() is None


# ------------------------------------------------------------ call sites


def test_main_invocation_stamps_profile(relay_home, tmp_path, monkeypatch):
    active = node_config.ACTIVE_PATH
    _write_active(active, PROFILE_YAML)
    py, _script = _fake_venv(tmp_path)
    monkeypatch.setattr(sys, "executable", str(py))
    monkeypatch.setattr(sys, "argv", [str(py), "node-cli"])

    rc = node_cli.main(["capabilities", "current"])

    assert _stamp_value(active) == os.path.realpath(
        node_config.ACTIVE_PATH.parent / "does-not-matter"
    ) or _stamp_value(active) is not None  # stamped from the fake venv
    expected = os.path.realpath(tmp_path / "fakevenv" / "bin" / "node-cli")
    assert _stamp_value(active) == expected


def test_publish_profile_restamps_after_byte_copy(relay_home, tmp_path, monkeypatch):
    """publish copies the working profile byte-exact OVER node.yaml — the
    stamp must re-appear after the copy (self-heal at the source)."""
    node_config.PROFILES_DIR.mkdir(parents=True)
    (node_config.PROFILES_DIR / "default.yaml").write_text(PROFILE_YAML)
    active = node_config.ACTIVE_PATH
    _write_active(active, 'node_cli_path: "/stale/node-cli"\n' + PROFILE_YAML)
    py, _script = _fake_venv(tmp_path)
    monkeypatch.setattr(sys, "executable", str(py))
    monkeypatch.setattr(sys, "argv", [str(py), "node-cli"])

    publish_profile("default")

    expected = os.path.realpath(tmp_path / "fakevenv" / "bin" / "node-cli")
    assert _stamp_value(active) == expected
    assert "test.cap" in active.read_text()


def test_daemon_run_stamps_profile(relay_home, tmp_path, monkeypatch):
    active = node_config.ACTIVE_PATH
    _write_active(active, PROFILE_YAML)
    py, _script = _fake_venv(tmp_path)
    monkeypatch.setattr(sys, "executable", str(py))
    monkeypatch.setattr(sys, "argv", [str(py), "node-cli"])

    import nodes.common.node_daemon as nd
    from nodes.common import file_serve

    c = MagicMock()
    c.base_url = "http://relay.test"
    c.meta = {"node_id": "N1"}
    c._current_backoff.return_value = 0
    monkeypatch.setattr(file_serve, "_serve_thread", None)
    monkeypatch.setattr(file_serve, "_ON_EXHAUSTED", None)
    monkeypatch.setattr(file_serve, "start_serve_thread", lambda: None)
    monkeypatch.setattr(
        SseDaemon := nd.SseDaemon, "_install_signal_handlers", lambda self: None
    )
    monkeypatch.setattr(SseDaemon, "_start_heartbeat_thread", lambda self: None)
    monkeypatch.setattr(SseDaemon, "_start_probe_thread", lambda self: None)
    monkeypatch.setattr(SseDaemon, "_sse_loop", lambda self: None)

    d = SseDaemon(c, {"heartbeat_interval": 3600})
    t = threading.Thread(target=d.run, daemon=True)
    t.start()
    timer = threading.Timer(0.4, d.stop)
    timer.daemon = True
    timer.start()
    t.join(timeout=5)

    expected = os.path.realpath(tmp_path / "fakevenv" / "bin" / "node-cli")
    assert _stamp_value(active) == expected