"""Wheel-based `node-cli update check/apply` — successor of the git helpers.

Contract:
- check compares importlib.metadata version('iowap-node') against the newest
  wheel-vX.Y.Z GitHub release; `update_available` is True only when the
  release version parses strictly greater.
- apply downloads the release asset, pip force-reinstalls it (--no-deps) and
  restarts the systemd unit; every failure path returns success=False with a
  message instead of raising.
- GitHub lookups are injectable via monkeypatched get_latest_release_version;
  no test touches the network.
"""
from __future__ import annotations

import argparse
import json
import sys
from unittest.mock import MagicMock

import pytest

from nodes.common import node_utils
from nodes.common.node_utils import (
    apply_wheel_update,
    check_wheel_updates,
    get_latest_release_version,
    get_local_wheel_version,
)


# ---------------------------------------------------------------------------
# get_local_wheel_version
# ---------------------------------------------------------------------------

def test_get_local_wheel_version_returns_string(monkeypatch):
    monkeypatch.setattr(
        node_utils, "get_local_wheel_version",
        lambda: "2.3.8",
    )
    assert node_utils.get_local_wheel_version() == "2.3.8"


def test_get_local_wheel_version_none_when_uninstalled(monkeypatch):
    from importlib import metadata
    monkeypatch.setattr(
        metadata, "version",
        lambda _name: (_ for _ in ()).throw(metadata.PackageNotFoundError()),
    )
    assert node_utils.get_local_wheel_version() is None


# ---------------------------------------------------------------------------
# get_latest_release_version — parsing of the GitHub release list
# ---------------------------------------------------------------------------

def _release(tag: str, asset: str | None):
    assets = [{"name": asset, "browser_download_url": f"https://x/{asset}"}] if asset else []
    return {"tag_name": tag, "assets": assets}


def test_latest_release_picks_newest_wheel_release(monkeypatch):
    releases = [
        _release("wheel-v2.3.8", "iowap_node-2.3.8-py3-none-any.whl"),
        _release("v1.0.0", "foo.tar.gz"),           # non-wheel tag, skipped
        _release("wheel-v2.3.7", "iowap_node-2.3.7-py3-none-any.whl"),
    ]

    def fake_urlopen(req, **kw):
        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return __import__("json").dumps(releases).encode()

        return R()

    monkeypatch.setattr(node_utils.urllib.request, "urlopen", fake_urlopen)
    info = get_latest_release_version()
    assert info["latest_version"] == "2.3.8"
    assert info["tag"] == "wheel-v2.3.8"
    assert info["asset_name"] == "iowap_node-2.3.8-py3-none-any.whl"
    assert info["asset_url"] == "https://x/iowap_node-2.3.8-py3-none-any.whl"
    assert info["error"] is None


def test_latest_release_network_error_returns_error_dict(monkeypatch):
    def boom(req, **kw):
        raise OSError("network down")

    monkeypatch.setattr(node_utils.urllib.request, "urlopen", boom)
    info = get_latest_release_version()
    assert info["latest_version"] is None
    assert "network down" in info["error"]


# ---------------------------------------------------------------------------
# check_wheel_updates — version comparison
# ---------------------------------------------------------------------------

def _patch_lookup(monkeypatch, latest: str | None, error: str | None = None):
    monkeypatch.setattr(
        node_utils, "get_latest_release_version",
        lambda repo=None: {
            "latest_version": latest, "tag": f"wheel-v{latest}" if latest else None,
            "asset_name": None, "asset_url": None, "error": error,
        },
    )


def test_check_update_available(monkeypatch):
    _patch_lookup(monkeypatch, "2.4.0")
    monkeypatch.setattr(node_utils, "get_local_wheel_version", lambda: "2.3.8")
    info = check_wheel_updates()
    assert info["update_available"] is True
    assert info["local_version"] == "2.3.8"


def test_check_up_to_date(monkeypatch):
    _patch_lookup(monkeypatch, "2.3.8")
    monkeypatch.setattr(node_utils, "get_local_wheel_version", lambda: "2.3.8")
    assert check_wheel_updates()["update_available"] is False


def test_check_older_release_not_an_update(monkeypatch):
    _patch_lookup(monkeypatch, "2.3.7")
    monkeypatch.setattr(node_utils, "get_local_wheel_version", lambda: "2.3.8")
    assert check_wheel_updates()["update_available"] is False


def test_check_no_release_no_crash(monkeypatch):
    _patch_lookup(monkeypatch, None, error="no wheel-vX.Y.Z release found")
    monkeypatch.setattr(node_utils, "get_local_wheel_version", lambda: "2.3.8")
    assert check_wheel_updates()["update_available"] is False


def test_check_not_installed(monkeypatch):
    _patch_lookup(monkeypatch, "2.4.0")
    monkeypatch.setattr(node_utils, "get_local_wheel_version", lambda: None)
    assert check_wheel_updates()["update_available"] is False


# ---------------------------------------------------------------------------
# apply_wheel_update — orchestration with mocked subprocess
# ---------------------------------------------------------------------------

def _apply_args(**kw):
    return kw


def test_apply_already_up_to_date(monkeypatch, tmp_path):
    _patch_lookup(monkeypatch, "2.3.8")
    monkeypatch.setattr(node_utils, "get_local_wheel_version", lambda: "2.3.8")
    result = apply_wheel_update(service_unit="noop.service", wheel_dir=tmp_path)
    assert result["success"] is False
    assert "already up to date" in result["message"]
    assert result["restarted"] is False
    assert result["wheel_path"] is None


def test_apply_success_path(monkeypatch, tmp_path):
    _patch_lookup(monkeypatch, "2.4.0")
    versions = iter(["2.3.8", "2.3.8", "2.4.0"])
    monkeypatch.setattr(node_utils, "get_local_wheel_version", lambda: next(versions))
    monkeypatch.setattr(node_utils, "get_latest_release_version", lambda repo=None: {
        "latest_version": "2.4.0", "tag": "wheel-v2.4.0",
        "asset_name": "iowap_node-2.4.0-py3-none-any.whl",
        "asset_url": "https://x/iowap_node-2.4.0-py3-none-any.whl", "error": None,
    })

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        r = MagicMock()
        r.stdout = ""
        r.stderr = ""
        return r

    def fake_open_write(url, **kw):
        ctx = MagicMock()
        ctx.__enter__ = lambda s: MagicMock(read=lambda: b"wheel-bytes")
        return ctx

    monkeypatch.setattr(node_utils.subprocess, "run", fake_run)
    monkeypatch.setattr(node_utils.urllib.request, "urlopen", fake_open_write)
    result = apply_wheel_update(service_unit="noop.service", wheel_dir=tmp_path)
    assert result["success"] is True
    assert result["message"] == "updated 2.3.8 -> 2.4.0; service restarted"
    assert result["restarted"] is True
    assert result["before_version"] == "2.3.8"
    assert result["after_version"] == "2.4.0"
    # cmd order: pip install, then systemctl restart
    assert "pip" in calls[0] and "install" in calls[0]
    assert calls[1][:3] == ["systemctl", "--user", "restart"]
    wheel = tmp_path / "iowap_node-2.4.0-py3-none-any.whl"
    assert wheel.read_bytes() == b"wheel-bytes"


def test_apply_download_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(node_utils, "get_latest_release_version", lambda repo=None: {
        "latest_version": "2.4.0", "tag": "wheel-v2.4.0",
        "asset_name": "iowap_node-2.4.0-py3-none-any.whl",
        "asset_url": "https://x/iowap_node-2.4.0-py3-none-any.whl", "error": None,
    })
    monkeypatch.setattr(node_utils, "get_local_wheel_version", lambda: "2.3.8")

    def fake_open(url, **kw):
        raise OSError("cdn down")

    monkeypatch.setattr(node_utils.urllib.request, "urlopen", fake_open)
    result = apply_wheel_update(service_unit="noop.service", wheel_dir=tmp_path)
    assert result["success"] is False
    assert "wheel download failed" in result["message"]
    assert result["restarted"] is False


def test_apply_pip_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(node_utils, "get_latest_release_version", lambda repo=None: {
        "latest_version": "2.4.0", "tag": "wheel-v2.4.0",
        "asset_name": "iowap_node-2.4.0-py3-none-any.whl",
        "asset_url": "https://x/iowap_node-2.4.0-py3-none-any.whl", "error": None,
    })
    monkeypatch.setattr(node_utils, "get_local_wheel_version", lambda: "2.3.8")

    def fake_open(url, **kw):
        ctx = MagicMock()
        ctx.__enter__ = lambda s: MagicMock(read=lambda: b"wheel-bytes")
        return ctx

    def fake_run(cmd, **kw):
        raise node_utils.subprocess.CalledProcessError(1, cmd, stderr="pip error")

    monkeypatch.setattr(node_utils.urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(node_utils.subprocess, "run", fake_run)
    result = apply_wheel_update(service_unit="noop.service", wheel_dir=tmp_path)
    assert result["success"] is False
    assert "pip install failed" in result["message"]
    assert result["restarted"] is False


def test_apply_restart_failure_after_pip(monkeypatch, tmp_path):
    _patch_lookup(monkeypatch, "2.4.0")
    versions = iter(["2.3.8", "2.3.8", "2.4.0"])
    monkeypatch.setattr(node_utils, "get_local_wheel_version", lambda: next(versions))
    monkeypatch.setattr(node_utils, "get_latest_release_version", lambda repo=None: {
        "latest_version": "2.4.0", "tag": "wheel-v2.4.0",
        "asset_name": "iowap_node-2.4.0-py3-none-any.whl",
        "asset_url": "https://x/iowap_node-2.4.0-py3-none-any.whl", "error": None,
    })

    def fake_run(cmd, **kw):
        if cmd[:3] == ["systemctl", "--user", "restart"]:
            raise node_utils.subprocess.CalledProcessError(1, cmd, stderr="unit missing")
        r = MagicMock()
        r.stdout = ""
        r.stderr = ""
        return r

    def fake_open(url, **kw):
        ctx = MagicMock()
        ctx.__enter__ = lambda s: MagicMock(read=lambda: b"wheel-bytes")
        return ctx

    monkeypatch.setattr(node_utils.subprocess, "run", fake_run)
    monkeypatch.setattr(node_utils.urllib.request, "urlopen", fake_open)
    result = apply_wheel_update(service_unit="noop.service", wheel_dir=tmp_path)
    assert result["success"] is False
    assert "service restart failed" in result["message"]
    assert result["restarted"] is False
    assert result["after_version"] == "2.4.0"


# ---------------------------------------------------------------------------
# CLI layer — _cmd_update_check / _cmd_update_apply (JSON output)
# ---------------------------------------------------------------------------

def test_cli_check_json(monkeypatch, capsys):
    from nodes.common.cli import cli_update
    _patch_lookup(monkeypatch, "2.4.0")
    monkeypatch.setattr(node_utils, "get_local_wheel_version", lambda: "2.3.8")
    rc = cli_update._cmd_update_check(argparse.Namespace(json=True, log_level="ERROR"))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["update_available"] is True


def test_cli_apply_json(monkeypatch, capsys):
    from nodes.common.cli import cli_update
    _patch_lookup(monkeypatch, "2.3.8")
    monkeypatch.setattr(node_utils, "get_local_wheel_version", lambda: "2.3.8")
    rc = cli_update._cmd_update_apply(
        argparse.Namespace(json=True, log_level="ERROR", service_unit="noop.service")
    )
    assert rc == 1  # already up to date -> not "success"
    out = json.loads(capsys.readouterr().out)
    assert "already up to date" in out["message"]


# ---------------------------------------------------------------------------
# Parser wiring — update subcommands still parse after the rebuild
# ---------------------------------------------------------------------------

def test_parser_update_subcommands_exist():
    from nodes.common import node_cli
    parser = node_cli.build_parser()
    args = parser.parse_args(["update", "check"])
    assert args.update_command == "check"
    args = parser.parse_args(["update", "apply"])
    assert args.update_command == "apply"
    assert args.service_unit == node_cli.SERVICE_UNIT
