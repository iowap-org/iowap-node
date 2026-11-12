"""T-248: node-cli ``task list`` — server-side task query filters.

Pins the T-248 contract: a new ``RelayClient.list_tasks()`` maps the CLI
options 1:1 onto the query params of ``GET /relay/v2/scheduler/tasks``
(``submitted_by``, ``owner_node_id``, ``status`` comma-separated verbatim,
``created_since``, ``created_until``) — only set options are sent. The CLI
handler prints ``--json`` full responses verbatim, renders human rows
``task_id  name  status  created_at  submitted_by -> owner_node_id``
newest-first, and routes HTTP error responses through ``_parse_relay_error``
(exit 1, code + detail on stderr). No server coupling: the transport is
mocked.
"""

from __future__ import annotations

import argparse
import json
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from nodes.common.cli import cli_task
from nodes.common.relay_client import RelayClient

META = {
    "node_id": "TESTNODE",
    "node_name": "task-list-test",
    "registration_secret": "rs_x",
    "base_url": "http://relay.test:8788",
}
CFG = {"base_url": None, "request_timeout": 5, "heartbeat_interval": 8}


def _args(**overrides: Any) -> argparse.Namespace:
    """argparse.Namespace for ``task list`` (fields used by _cmd_task_list)."""
    base: dict[str, Any] = {
        "submitted_by": None,
        "owner": None,
        "status": None,
        "since": None,
        "until": None,
        "json": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _list_via_fake_relay(**kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """list_tasks against a patched _get_with_retry; capture path + params."""
    captured: dict[str, Any] = {}

    def fake_get(path: str, **kw: Any) -> httpx.Response:
        captured["path"] = path
        captured["params"] = kw.get("params")
        return httpx.Response(
            200,
            json={"tasks": [], "viewer": {"node_id": "TESTNODE"}},
            request=httpx.Request("GET", "http://relay.test:8788" + path),
        )

    with patch.object(RelayClient, "_get_with_retry", side_effect=fake_get):
        client = RelayClient(dict(META), dict(CFG))
        resp = client.list_tasks(**kwargs)
    return resp, captured


# --------------------------------------------------------------------------
# Client level — list_tasks wire shape (query params, only set ones sent)
# --------------------------------------------------------------------------


def test_list_tasks_no_filter_sends_empty_params():
    resp, captured = _list_via_fake_relay()
    assert resp == {"tasks": [], "viewer": {"node_id": "TESTNODE"}}
    assert captured["path"] == "/relay/v2/scheduler/tasks"
    assert captured["params"] == {}


def test_list_tasks_maps_all_filters_one_to_one():
    _resp, captured = _list_via_fake_relay(
        submitted_by="NODE-A",
        owner_node_id="NODE-B",
        status="pending,claimed",
        created_since="2026-11-01T00:00:00Z",
        created_until="2026-11-12T00:00:00Z",
    )
    assert captured["path"] == "/relay/v2/scheduler/tasks"
    assert captured["params"] == {
        "submitted_by": "NODE-A",
        "owner_node_id": "NODE-B",
        "status": "pending,claimed",
        "created_since": "2026-11-01T00:00:00Z",
        "created_until": "2026-11-12T00:00:00Z",
    }


def test_list_tasks_sends_only_set_params():
    """Unset options must not appear as params (no None/empty entries)."""
    _resp, captured = _list_via_fake_relay(submitted_by="NODE-A")
    assert captured["params"] == {"submitted_by": "NODE-A"}


def test_list_tasks_status_comma_verbatim():
    """--status a,b,c rides through unvalidated as one comma string."""
    _resp, captured = _list_via_fake_relay(status="pending, completed,failed")
    assert captured["params"] == {"status": "pending, completed,failed"}


# --------------------------------------------------------------------------
# CLI level — _cmd_task_list json / human / error paths
# --------------------------------------------------------------------------


def _mk_client(payload: dict[str, Any]) -> MagicMock:
    client = MagicMock(spec=RelayClient)
    client.list_tasks.return_value = payload
    return client


def test_cmd_task_list_json_prints_full_response(capsys):
    payload = {
        "tasks": [
            {"task_id": "t-1", "task_name": "alpha", "status": "pending",
             "created_at": "2026-11-12T10:00:00Z", "submitted_by": "NODE-A",
             "owner_node_id": None},
        ],
        "viewer": {"node_id": "TESTNODE"},
    }
    rc = cli_task._cmd_task_list(_mk_client(payload), _args(json=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out) == payload


def test_cmd_task_list_human_row_contains_id_status_and_owner(capsys):
    payload = {
        "tasks": [
            {"task_id": "t-1", "task_name": "alpha", "status": "pending",
             "created_at": "2026-11-11T10:00:00Z", "submitted_by": "NODE-A",
             "owner_node_id": "NODE-B"},
        ],
        "viewer": {"node_id": "TESTNODE"},
    }
    rc = cli_task._cmd_task_list(_mk_client(payload), _args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "t-1" in out
    assert "pending" in out
    assert "NODE-A -> NODE-B" in out


def test_cmd_task_list_human_sorts_newest_first(capsys):
    payload = {
        "tasks": [
            {"task_id": "t-old", "task_name": "old", "status": "completed",
             "created_at": "2026-11-10T10:00:00Z", "submitted_by": "NODE-A",
             "owner_node_id": None},
            {"task_id": "t-new", "task_name": "new", "status": "claimed",
             "created_at": "2026-11-12T10:00:00Z", "submitted_by": "NODE-A",
             "owner_node_id": None},
        ],
        "viewer": {"node_id": "TESTNODE"},
    }
    cli_task._cmd_task_list(_mk_client(payload), _args())
    out = capsys.readouterr().out
    assert out.index("t-new") < out.index("t-old")


def test_cmd_task_list_human_empty(capsys):
    rc = cli_task._cmd_task_list(_mk_client({"tasks": [], "viewer": {}}), _args())
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip()


def test_cmd_task_list_filters_forwarded_to_client(capsys):
    client = _mk_client({"tasks": [], "viewer": {}})
    cli_task._cmd_task_list(
        client,
        _args(
            submitted_by="NODE-A",
            owner="NODE-B",
            status="pending,failed",
            since="2026-11-01T00:00:00Z",
            until="2026-11-12T00:00:00Z",
        ),
    )
    client.list_tasks.assert_called_once_with(
        submitted_by="NODE-A",
        owner_node_id="NODE-B",
        status="pending,failed",
        created_since="2026-11-01T00:00:00Z",
        created_until="2026-11-12T00:00:00Z",
    )


def _mk_status_error(status: int, detail: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://relay.test:8788/relay/v2/scheduler/tasks")
    response = httpx.Response(status, json={"detail": detail}, request=request)
    return httpx.HTTPStatusError(
        f"{status} error", request=request, response=response
    )


@pytest.mark.parametrize(
    ("status", "detail", "expect_code"),
    [(401, "auth_failed: token invalid", "auth_failed"),
     (423, "relay_locked: maintenance in progress", "relay_locked")],
)
def test_cmd_task_list_http_error_via_parse_relay_error(
    capsys, status, detail, expect_code
):
    """401/423 go through _parse_relay_error: exit 1, code+detail on stderr."""
    client = MagicMock(spec=RelayClient)
    client.list_tasks.side_effect = _mk_status_error(status, detail)
    rc = cli_task._cmd_task_list(client, _args())
    err = capsys.readouterr().err
    assert rc == 1
    assert f"HTTP {status}" in err
    assert f"{expect_code}:" in err
    assert "maintenance in progress" in err or "token invalid" in err


# --------------------------------------------------------------------------
# Parser registration — ``node-cli task list`` surface
# --------------------------------------------------------------------------


def _build_parser():
    from nodes.common import node_cli

    return node_cli.build_parser()


def test_parser_task_list_accepts_all_options():
    # --json is the global flag: node-cli --json task list ... (see main parser)
    parser = _build_parser()
    args = parser.parse_args(
        [
            "--json",
            "task", "list",
            "--submitted-by", "NODE-A",
            "--owner", "NODE-B",
            "--status", "pending,claimed",
            "--since", "2026-11-01T00:00:00Z",
            "--until", "2026-11-12T00:00:00Z",
        ]
    )
    assert args.task_command == "list"
    assert args.submitted_by == "NODE-A"
    assert args.owner == "NODE-B"
    assert args.status == "pending,claimed"
    assert args.since == "2026-11-01T00:00:00Z"
    assert args.until == "2026-11-12T00:00:00Z"
    assert args.json is True
    assert args.func is not None
