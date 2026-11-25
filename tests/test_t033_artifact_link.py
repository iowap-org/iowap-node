"""T-033: ``artifact link`` — relay client + CLI acceptance tests.

Covers the brief's node-repo requirements:

* ``RelayClient.create_artifact_link`` POSTs
  ``/relay/v2/storage/files/{artifact_id}/link`` via ``_post_with_auth_retry``
  and re-aches the response dict (incl. ``url``).
* Both optional clamps ride the wire exactly as passed; omitted keys stay
  absent (the server applies its own defaults).
* Relay 404/5xx surface as ``httpx.HTTPStatusError`` (raise_for_status),
  which the CLI turns into ``artifact link failed: HTTP <code>: <detail>``
  on stderr — the T-274-shape classification of the MCP runner keys off
  exactly this text.
* CLI output shape (both plain and ``--json``) and exit codes.
"""

from __future__ import annotations

import argparse
import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from nodes.common.node_cli import build_parser
from nodes.common.cli import cli_artifact
from nodes.common.relay_client import RelayClient

META = {
    "node_id": "E4W3CBWQ",
    "node_name": "test-node",
    "base_url": "http://relay.test:8788",
    "registration_secret": "rs_test",
}
CFG = {"base_url": None, "request_timeout": 5, "heartbeat_interval": 8}


def _stub_response(status_code: int, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("POST", "http://relay.test:8788/link"),
    )


# ── relay_client.create_artifact_link ──────────────────────────────────────


def test_create_artifact_link_happy_path_posts_and_returns_dict() -> None:
    captured: dict[str, Any] = {}

    def fake_post(path: str, body: dict[str, Any], **_kw: Any) -> httpx.Response:
        captured["path"] = path
        captured["body"] = body
        return _stub_response(
            200,
            {
                "artifact_id": "artifact_x",
                "url": "http://relay.test:8788/relay/v2/storage/files/artifact_x/dl/tok",
                "expires_at": "2026-11-25T20:00:00+00:00",
                "max_downloads": 1,
                "ttl_seconds": 900,
                "max_downloads_cap": 10,
            },
        )
    with patch.object(
        RelayClient, "_post_with_auth_retry", side_effect=fake_post
    ):
        client = RelayClient(dict(META), dict(CFG))
        result = client.create_artifact_link("artifact_x")

    assert captured["path"] == "/relay/v2/storage/files/artifact_x/link"
    # Both optionals absent → server defaults (empty body, no zero-coercion).
    assert captured["body"] == {}
    assert result["url"].endswith("/dl/tok")
    assert result["artifact_id"] == "artifact_x"
    assert result["max_downloads"] == 1


def test_create_artifact_link_sends_both_clamps_as_set() -> None:
    captured: dict[str, Any] = {}

    def fake_post(path: str, body: dict[str, Any], **_kw: Any) -> httpx.Response:
        captured["path"] = path
        captured["body"] = body
        return _stub_response(
            200,
            {
                "artifact_id": "artifact_x",
                "url": "u",
                "expires_at": "2026-11-25T20:00:00+00:00",
                "max_downloads": 10,
                "ttl_seconds": 60,
            },
        )
    with patch.object(
        RelayClient, "_post_with_auth_retry", side_effect=fake_post
    ):
        client = RelayClient(dict(META), dict(CFG))
        result = client.create_artifact_link(
            "artifact_x", ttl_seconds=60, max_downloads=10
        )

    assert captured["body"] == {"ttl_seconds": 60, "max_downloads": 10}
    assert result["ttl_seconds"] == 60 and result["max_downloads"] == 10


def test_create_artifact_link_partial_clamp_only_sends_given_key() -> None:
    captured: dict[str, Any] = {}

    def fake_post(_path: str, body: dict[str, Any], **_kw: Any) -> httpx.Response:
        captured["body"] = body
        return _stub_response(200, {"url": "u"})
    with patch.object(
        RelayClient, "_post_with_auth_retry", side_effect=fake_post
    ):
        client = RelayClient(dict(META), dict(CFG))
        client.create_artifact_link("artifact_x", max_downloads=5)

    assert captured["body"] == {"max_downloads": 5}


def test_create_artifact_link_404_surfaces_http_status_error() -> None:
    def fake_post(path: str, body: dict[str, Any], **_kw: Any) -> httpx.Response:
        return _stub_response(404, {"detail": "Artifact not found"})
    with patch.object(
        RelayClient, "_post_with_auth_retry", side_effect=fake_post
    ):
        client = RelayClient(dict(META), dict(CFG))
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            client.create_artifact_link("artifact_missing")

    assert excinfo.value.response.status_code == 404


def test_create_artifact_link_5xx_surfaces_http_status_error() -> None:
    def fake_post(_path: str, _body: dict[str, Any], **_kw: Any) -> httpx.Response:
        return _stub_response(500, {"detail": "boom"})
    with patch.object(
        RelayClient, "_post_with_auth_retry", side_effect=fake_post
    ):
        client = RelayClient(dict(META), dict(CFG))
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            client.create_artifact_link("artifact_x")

    assert excinfo.value.response.status_code == 500


# ── CLI artifact link ──────────────────────────────────────────────────────

_LINK_RESULT = {
    "artifact_id": "artifact_x",
    "url": "http://relay.test:8788/relay/v2/storage/files/artifact_x/dl/tok",
    "expires_at": "2026-11-25T20:00:00+00:00",
    "max_downloads": 1,
    "ttl_seconds": 900,
}


class _StubClient:
    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self._result = result or _LINK_RESULT
        self.calls: list[tuple[str, int | None, int | None]] = []

    def create_artifact_link(
        self,
        artifact_id: str,
        *,
        ttl_seconds: int | None = None,
        max_downloads: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append((artifact_id, ttl_seconds, max_downloads))
        return self._result


class _FailClient:
    def __init__(self, status_code: int, detail: str) -> None:
        request = httpx.Request("POST", "http://relay.test:8788/link")
        response = httpx.Response(status_code, json={"detail": detail}, request=request)
        self._exc = httpx.HTTPStatusError(
            f"HTTP {status_code}: {detail}", request=request, response=response
        )

    def create_artifact_link(self, artifact_id: str, **_kw: Any) -> dict[str, Any]:
        raise self._exc


def _cmd_args(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "artifact_id": "artifact_x",
        "ttl_seconds": None,
        "max_downloads": None,
        "json": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_cli_link_plain_output_carries_url_and_expiry(capsys: pytest.CaptureFixture) -> None:
    stub = _StubClient()
    rc = cli_artifact._cmd_artifact_link(stub, _cmd_args())  # type: ignore[arg-type]
    out = capsys.readouterr().out

    assert rc == 0
    assert _LINK_RESULT["url"] in out
    assert _LINK_RESULT["expires_at"] in out
    assert "max 1 download" in out
    assert stub.calls == [("artifact_x", None, None)]


def test_cli_link_json_output_is_the_pure_response(capsys: pytest.CaptureFixture) -> None:
    stub = _StubClient()
    rc = cli_artifact._cmd_artifact_link(stub, _cmd_args(json=True))  # type: ignore[arg-type]
    out = capsys.readouterr().out

    assert rc == 0
    assert json.loads(out) == _LINK_RESULT


def test_cli_link_forwards_ttl_and_cap(capsys: pytest.CaptureFixture) -> None:
    stub = _StubClient()
    rc = cli_artifact._cmd_artifact_link(
        stub, _cmd_args(ttl_seconds=60, max_downloads=3)
    )  # type: ignore[arg-type]

    assert rc == 0
    assert stub.calls == [("artifact_x", 60, 3)]
    capsys.readouterr()


def test_cli_link_404_prints_http_status_and_exits_1(capsys: pytest.CaptureFixture) -> None:
    fail = _FailClient(404, "Artifact not found")
    rc = cli_artifact._cmd_artifact_link(fail, _cmd_args())  # type: ignore[arg-type]
    err = capsys.readouterr().err

    assert rc == 1
    assert "artifact link failed: HTTP 404: Artifact not found" in err


def test_cli_link_5xx_prints_http_status_and_exits_1(capsys: pytest.CaptureFixture) -> None:
    fail = _FailClient(500, "boom")
    rc = cli_artifact._cmd_artifact_link(fail, _cmd_args())  # type: ignore[arg-type]
    err = capsys.readouterr().err

    assert rc == 1
    assert "artifact link failed: HTTP 500: boom" in err


def test_cli_link_parser_registers_subcommand() -> None:
    """The registered ``artifact link`` parser parses into the handler args."""
    parser = build_parser()
    args = parser.parse_args(
        ["artifact", "link", "artifact_x", "--ttl-seconds", "120", "--max-downloads", "2"]
    )
    assert args.ttl_seconds == 120
    assert args.max_downloads == 2
    assert callable(args.func)
