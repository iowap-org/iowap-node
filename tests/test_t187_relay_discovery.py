"""T-187 (node): mDNS discovery — name-filtered, configurable, CLI-controllable.

Contract (Ronny, 2026-10-03):
- Discovery accepts ONLY the relay service name (default
  ``IOWAP Relay Service``), never the first-best _http._tcp hit.
- Service name is configurable (``mdns_service_name`` in relay_config.json,
  env ``RELAY_MDNS_SERVICE_NAME``), so a server with a custom
  ``mdns_service_name`` is discoverable.
- ``node-cli relay discover`` runs a targeted lookup and prints the URL.
- ``node-cli relay set --server-url <url>`` pins the URL (discovery off).
- ``node-cli relay set --discover`` removes the pin (discovery on).
- ``_base_url`` fallback uses the filtered discovery.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nodes.common.relay_client import (  # noqa: E402
    DEFAULT_SERVICE_NAME,
    _base_url,
    discover_relay,
)


def _conf_path(tmp_path: Path) -> Path:
    return tmp_path / "relay_config.json"


def _patch_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data: dict | None
) -> Path:
    """Point node_utils.CONFIG_PATH at a fresh config file."""
    import nodes.common.node_utils as nu

    conf = _conf_path(tmp_path)
    if data is not None:
        conf.write_text(json.dumps(data))
    monkeypatch.setattr(nu, "CONFIG_PATH", conf)
    return conf


# ------------------------------------------------------------ discovery core

def test_default_service_name_is_iowap() -> None:
    assert DEFAULT_SERVICE_NAME == "IOWAP Relay Service"


def test_discover_skips_foreign_services() -> None:
    """QNAP/Brother-style _http._tcp hits are never accepted."""
    fake = {"http://192.168.2.200:8080": "QNAP-nas._http._tcp.local."}
    with patch(
        "nodes.common.relay_client._probe_mdns_services", return_value=fake
    ):
        assert discover_relay() is None


def test_discover_accepts_iowap_service() -> None:
    fake = {
        "http://192.168.2.200:8080": "QNAP-nas._http._tcp.local.",
        "http://192.168.2.60:8788": "IOWAP Relay Service._http._tcp.local.",
    }
    with patch(
        "nodes.common.relay_client._probe_mdns_services", return_value=fake
    ):
        assert discover_relay() == "http://192.168.2.60:8788"


def test_discover_respects_explicit_service_name() -> None:
    """A server with custom mdns_service_name, passed as argument."""
    fake = {"http://10.0.0.9:8788": "Keller-Relay._http._tcp.local."}
    with patch(
        "nodes.common.relay_client._probe_mdns_services", return_value=fake
    ):
        assert discover_relay(service_name="Keller-Relay") == "http://10.0.0.9:8788"


def test_discover_reads_configured_service_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """relay_config.json ``mdns_service_name`` overrides the default."""
    _patch_config(
        monkeypatch, tmp_path, {"mdns_service_name": "Hobbykeller"}
    )
    fake = {"http://10.0.0.5:8788": "Hobbykeller._http._tcp.local."}
    with patch(
        "nodes.common.relay_client._probe_mdns_services", return_value=fake
    ):
        assert discover_relay() == "http://10.0.0.5:8788"


def test_discover_env_overrides_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env RELAY_MDNS_SERVICE_NAME beats relay_config.json."""
    _patch_config(
        monkeypatch, tmp_path, {"mdns_service_name": "AusDerConfig"}
    )
    monkeypatch.setenv("RELAY_MDNS_SERVICE_NAME", "AusDemEnv")
    fake = {"http://10.0.0.7:8788": "AusDemEnv._http._tcp.local."}
    with patch(
        "nodes.common.relay_client._probe_mdns_services", return_value=fake
    ):
        assert discover_relay() == "http://10.0.0.7:8788"


# ------------------------------------------------------------ _base_url wiring

def test_base_url_prefers_pin_over_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = {"http://10.0.0.9:8788": "IOWAP Relay Service._http._tcp.local."}
    monkeypatch.setattr(
        "nodes.common.node_utils.CONFIG_PATH", _conf_path(tmp_path)
    )
    with patch(
        "nodes.common.relay_client._probe_mdns_services", return_value=fake
    ):
        assert _base_url({"base_url": "http://192.168.2.60:8788"}, {}) == (
            "http://192.168.2.60:8788"
        )


def test_base_url_falls_back_to_filtered_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = {"http://192.168.2.60:8788": "IOWAP Relay Service._http._tcp.local."}
    monkeypatch.setattr(
        "nodes.common.node_utils.CONFIG_PATH", _conf_path(tmp_path)
    )
    with patch(
        "nodes.common.relay_client._probe_mdns_services", return_value=fake
    ):
        assert _base_url({}, {}) == "http://192.168.2.60:8788"


def test_base_url_finds_custom_named_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured mdns_service_name makes a custom-named server discoverable.

    _base_url receives the merged cfg from _effective_config() — the name
    arrives via the cfg parameter (production shape), not by re-reading
    the file.
    """
    fake = {"http://10.0.0.9:8788": "Keller-Relay._http._tcp.local."}
    with patch(
        "nodes.common.relay_client._probe_mdns_services", return_value=fake
    ):
        assert _base_url({}, {"mdns_service_name": "Keller-Relay"}) == (
            "http://10.0.0.9:8788"
        )


# ------------------------------------------------------------ CLI handlers

def test_relay_set_pins_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from nodes.common.relay_client import _cmd_relay_set

    conf = _patch_config(monkeypatch, tmp_path, {"heartbeat_interval": 8})
    ns = __import__("types").SimpleNamespace(
        server_url="http://192.168.2.60:8788", discover=False, name=None
    )
    assert _cmd_relay_set(ns) == 0
    data = json.loads(conf.read_text())
    assert data["base_url"] == "http://192.168.2.60:8788"
    assert data["heartbeat_interval"] == 8  # existing keys preserved


def test_relay_set_discover_clears_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from nodes.common.relay_client import _cmd_relay_set

    conf = _patch_config(
        monkeypatch, tmp_path, {"base_url": "http://192.168.2.60:8788"}
    )
    ns = __import__("types").SimpleNamespace(
        server_url=None, discover=True, name=None
    )
    assert _cmd_relay_set(ns) == 0
    data = json.loads(conf.read_text())
    assert "base_url" not in data


def test_relay_set_rejects_no_args() -> None:
    from nodes.common.relay_client import _cmd_relay_set

    ns = __import__("types").SimpleNamespace(
        server_url=None, discover=False, name=None
    )
    with pytest.raises(SystemExit):
        _cmd_relay_set(ns)


def test_relay_set_rejects_conflicting_flags() -> None:
    from nodes.common.relay_client import _cmd_relay_set

    ns = __import__("types").SimpleNamespace(
        server_url="http://x:1", discover=True, name=None
    )
    with pytest.raises(SystemExit):
        _cmd_relay_set(ns)


def test_relay_discover_prints_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from nodes.common.relay_client import _cmd_relay_discover

    _patch_config(monkeypatch, tmp_path, {})
    fake = {"http://192.168.2.60:8788": "IOWAP Relay Service._http._tcp.local."}
    ns = __import__("types").SimpleNamespace(name=None, timeout=2.0)
    with patch(
        "nodes.common.relay_client._probe_mdns_services", return_value=fake
    ):
        assert _cmd_relay_discover(ns) == 0
    out = capsys.readouterr().out
    assert "http://192.168.2.60:8788" in out
    assert "IOWAP Relay Service" in out


def test_relay_discover_prints_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from nodes.common.relay_client import _cmd_relay_discover

    _patch_config(monkeypatch, tmp_path, {})
    ns = __import__("types").SimpleNamespace(name=None, timeout=2.0)
    with patch(
        "nodes.common.relay_client._probe_mdns_services", return_value={}
    ):
        assert _cmd_relay_discover(ns) == 1
    out = capsys.readouterr().out
    assert "not found" in out.lower()


# ------------------------------------------------------------ parser wiring

def test_parser_relay_subcommand_wired() -> None:
    from nodes.common.node_cli import build_parser

    parser = build_parser()
    ns = parser.parse_args(["relay", "set", "--server-url", "http://x:1"])
    assert ns.server_url == "http://x:1"
    ns2 = parser.parse_args(["relay", "discover"])
    assert hasattr(ns2, "timeout")
    with pytest.raises(SystemExit):
        parser.parse_args(["relay", "nonsense-sub"])