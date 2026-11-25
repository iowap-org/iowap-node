#!/usr/bin/env python3
"""node-cli — generic capability-driven CLI for IOWAP.

The CLI is fully capability-agnostic: all behaviour is driven by an
external YAML profile (see NODE_CLI_SPEC.md). Subcommands:

    One-shot operations:
        node-cli heartbeat
        node-cli claim <capability>
        node-cli complete <stage_id> --task <task_id> --result-file <path>
        node-cli task submit --name <name> --stage <cap>:<json_payload> [--priority N] [--owner <node_id>]
        node-cli artifact download <artifact_id> [--output <path>]
        node-cli artifact upload <file> [--name <name>] [--task-id <id>] [--stage-id <id>]
        node-cli artifact link <artifact_id> [--ttl-seconds N] [--max-downloads N]

    Handler primitives (T-179):
        node-cli hp put <path> --cap <capability> [--name <name>]
            — the bridge rung moves the file into the serve dir and
              deletes it after transfer
        node-cli hp get [--file <envelope.json>] [--output <path>] [--out-dir <dir>]

    Capability profile management:
        node-cli capabilities list | validate [profile] | publish <profile>
        node-cli capabilities diff [profile] | current

    Capability store management (T-042, dashboard providers.yaml):
        node-cli capabilities create --name N [--type process|http] [--summary S]
            [--command C] [--args AR...] [--script F] [--max-parallel N]
            [--cost N] [--artifacts-glob G] [--version V] [--publish]
        node-cli capabilities update --name N [same field set] [--publish|--unpublish]
        node-cli capabilities delete --name N
        node-cli capabilities script-upload --file F | --stdin --name FILENAME

    Status:
        node-cli status
        node-cli reload — SIGHUP notification to a running daemon (ops
            signal only; daemon lifecycle lives in iowap-daemon /
            nodes.common.poll_daemon, T-250)
        node-cli node busy     — mark this node as busy (T-084)
        node-cli node idle     — mark this node as idle (T-084)
        node-cli node status   — show local + server node status (T-084)
        node-cli node clear-status — restore automatic status handling (T-084)

    Updates (T-062):
        node-cli update check     — fetch origin, compare local vs. upstream
        node-cli update apply     — git pull + restart the node-cli service
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from nodes.common import node_utils as _nu
from nodes.common.handler_runner import (
    run_handler,  # noqa: F401 — re-exported (T-250: tests patch node_cli.run_handler)
)
from nodes.common.node_config import (
    ACTIVE_PATH,  # noqa: F401 — re-exported so the test fixture's cli.ACTIVE_PATH patch keeps working (T-117)
    PROFILES_DIR,  # noqa: F401 — re-exported for the test fixture's cli.PROFILES_DIR patch
    load_active_profile,
)
from nodes.common.node_utils import (
    RESTART_COMMAND,
    SERVICE_UNIT,
    STATUS_PATH,
    load_json,  # noqa: F401 — re-exported (T-250: cli_server resolves via _cli.load_json)
    load_meta,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("node-cli")



# ---------------------------------------------------------------------------
# Shared relay client — extracted to relay_client.py (T-112)
# Re-exported here so node_daemon.py and existing tests that reference
# node_cli.RelayClient / node_cli._effective_config / etc. keep working.
# The underscore helpers are re-exported for downstream consumers; they
# are not used directly in this module, hence the noqa.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# @with_client decorator — eliminates boilerplate from _cmd_* functions
# ---------------------------------------------------------------------------
# T-117: cli submodules are imported for build_parser() registration. They
# must NOT import node_cli (avoids circular import + keeps the cli.RelayClient
# monkeypatch working). Handlers are plain (client, args) -> int and wrapped
# with @with_client here at registration time.
from nodes.common.cli import (
    cli_artifact,
    cli_bridge,
    cli_capabilities,
    cli_docs,
    cli_file,
    cli_hp,  # T-179: handler primitives (hp put/get) — subcommand registration below
    cli_mcp,  # T-230: toolkit install/status (client-neutral)
    cli_node,
    cli_route,
    cli_server,
    cli_task,
    cli_update,
)
from nodes.common.relay_client import (  # noqa: F401
    RelayClient,
    _base_url,
    _cmd_relay_discover,
    _cmd_relay_set,
    _effective_config,
    _filename_from_response,
    _setup_logging,
    _utcnow_str,
)


def with_client(func):
    """Decorator: injects (client, args) into _cmd_* functions.

    Handles _setup_logging, load_meta, _effective_config, RelayClient
    so every command doesn't repeat the same 4 lines.
    When --json is set, forces log level to ERROR so INFO/WARNING
    messages don't pollute the JSON output on stdout.
    """
    @functools.wraps(func)
    def wrapper(args):
        log_level = "ERROR" if getattr(args, "json", False) else args.log_level
        _setup_logging(log_level)
        meta = load_meta()
        cfg = _effective_config()
        client = RelayClient(meta, cfg)
        return func(client, args)
    return wrapper


# ---------------------------------------------------------------------------
# Daemon — no longer part of the CLI (T-250): the daemon implementation and
# its lifecycle control live in nodes.common.poll_daemon (poll mode) and
# nodes.common.node_daemon (SSE mode, `iowap-daemon`). `reload` above and
# the SIGHUP in `capabilities publish` remain as ops signalisation only.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# One-shot subcommands
# ---------------------------------------------------------------------------

def _parse_stage_arg(stage: str) -> tuple[str, dict[str, Any]]:
    """Parse ``<cap>:<json_payload>`` into (capability, payload).

    Re-exported from ``cli.cli_task`` (T-117 split) so existing tests that
    call ``cli._parse_stage_arg`` keep resolving.
    """
    return cli_task._parse_stage_arg(stage)


@with_client
def _cmd_heartbeat(client: RelayClient, args: argparse.Namespace) -> int:
    caps = load_active_profile()
    hb = client.heartbeat(caps, {})
    if args.json:
        print(json.dumps(hb, default=str))
        return 0
    print(json.dumps(hb, indent=2, default=str))
    return 0


@with_client
def _cmd_claim(client: RelayClient, args: argparse.Namespace) -> int:
    stage = client.claim(args.capability)
    if stage is None:
        print(json.dumps({"claimed": False}))
        return 0
    result = {"claimed": True, "stage": stage}
    if args.json:
        print(json.dumps(result, default=str))
        return 0
    print(json.dumps(result, indent=2, default=str))
    cd = stage.get("capability_details")
    if cd:
        print()
        print(f"  Capability: {cd.get('name', '?')}")
        if cd.get("description"):
            print(f"  Description: {cd['description']}")
        if cd.get("type"):
            print(f"  Type:        {cd['type']}")
        if cd.get("input_schema"):
            print(f"  Input Schema: {json.dumps(cd['input_schema'], indent=2)}")
    return 0


def _cmd_complete(client: RelayClient, args: argparse.Namespace) -> int:
    """Complete a claimed stage (T-179 Task 4: env-defaults for IDs).

    ``--task``/``stage_id`` fallen back to ``RELAY_TASK_ID``/
    ``RELAY_STAGE_ID`` (set by handler_runner for every handler) —
    handler scripts can complete without repeating their IDs. Explicit
    flags win over env. Without any ID → stderr message + exit 1.
    """
    task_id = getattr(args, "task", None) or os.environ.get("RELAY_TASK_ID")
    stage_id = args.stage_id or os.environ.get("RELAY_STAGE_ID")
    if not task_id or not stage_id:
        print(
            "complete: no --task/--task-id and no RELAY_TASK_ID/"
            "RELAY_STAGE_ID in env",
            file=sys.stderr,
        )
        return 1
    if not Path(args.result_file).exists():
        print(f"result file not found: {args.result_file}", file=sys.stderr)
        return 2
    try:
        result = json.loads(Path(args.result_file).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"result file is not valid JSON: {exc}", file=sys.stderr)
        return 2
    resp = client.complete(task_id, stage_id, result)
    if args.json:
        print(json.dumps(resp, default=str))
        return 0
    print(json.dumps(resp, indent=2, default=str))
    return 0


# ---------------------------------------------------------------------------
# capabilities subcommands — moved to cli/cli_capabilities (T-117)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# node subcommands — moved to cli/cli_node (T-117)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# status / reload
# ---------------------------------------------------------------------------

def _cmd_status(args: argparse.Namespace) -> int:
    if not STATUS_PATH.exists():
        print("(no status file — daemon not started yet)")
        return 1
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"status file is not valid JSON: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(data, indent=2, default=str))
    return 0


def _cmd_reload(args: argparse.Namespace) -> int:
    # t_c214ca25: notify every live daemon pid file (SSE node-daemon.pid
    # first, legacy polling node-cli.pid fallback). The SSE daemon installs
    # a SIGHUP handler (capability-cache invalidation) — probing only the
    # polling pid file left SSE-only nodes without hot-reload. PID_PATH is
    # read off poll_daemon lazily at call time so the monkeypatched pid
    # file fixtures take effect (T-250: the CLI no longer re-exports it).
    from nodes.common import node_daemon as _nd
    from nodes.common import poll_daemon as _pd

    pids = _nu.sighup_pid_files([_nd.PID_PATH, _pd.PID_PATH])
    if not pids:
        print("daemon not running", file=sys.stderr)
        return 1
    print(f"SIGHUP sent to daemon (pid {', '.join(str(p) for p in pids)})")
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _parser_prog() -> str:
    """usage prog = invoked binary name (argv[0] basename, fallback 'iowap-cli').

    argv[0] stays the deciding surface: the ``iowap-cli`` script and the
    legal module entry ``python -m nodes.common.node_cli`` (argv0 basename
    ``node_cli.py``) both mirror their invocation. T-250 sunset (2.3.43):
    the empty-argv0 fallback names the one remaining canonical binary —
    the legacy ``node-cli`` shim no longer exists.
    """
    argv0 = sys.argv[0] if sys.argv and sys.argv[0] else ""
    return os.path.basename(argv0) or "iowap-cli"


def _add_capability_field_args(parser: argparse.ArgumentParser) -> None:
    """T-042: the shared capability form-field slice for create AND update.

    Identical flags on both subcommands (the brief's "same field set"):
    only flags the user passed are collected by cli_capabilities._field_args,
    so the dashboard store validation governs what is mandatory.
    """
    parser.add_argument(
        "--type", default=None, help="Capability type: process or http (validated by the store)."
    )
    parser.add_argument("--summary", default=None, help="Short human description.")
    parser.add_argument(
        "--command", default=None, help="Process command text (argv template; parsed like the dashboard form)."
    )
    parser.add_argument(
        "--args", nargs="*", default=None, metavar="ARG", help="Extra tokens appended to --command."
    )
    parser.add_argument(
        "--script", default=None,
        help="Store this script (dashboard upload rules) and prefill --command with its default argv.",
    )
    parser.add_argument("--max-parallel", default=None, help="Scheduler parallelism (≥1, default 1).")
    parser.add_argument("--cost", default=None, help="Admission points (≥0, default 1).")
    parser.add_argument("--artifacts-glob", default=None, help="Glob of artifacts the run may emit.")
    parser.add_argument(
        "--version", default=None, help="Entry version X.Y.Z (default 1.0.0; update auto-bumps unless set)."
    )
    parser.add_argument("--url-template", default=None, help="http type: URL template (exec mapping).")
    parser.add_argument(
        "--method", default=None, help="http type: HTTP method (GET/POST/PUT/PATCH/DELETE; default POST)."
    )
    parser.add_argument("--body-template", default=None, help="http type: request body template.")
    parser.add_argument("--result-path", default=None, help="http type: JSON path of the stage result.")
    parser.add_argument(
        "--artifact-paths", default=None, help="http type: comma-separated artifact JSON paths."
    )
    parser.add_argument("--health-url", default=None, help="http type: health probe URL.")


def build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog or _parser_prog(),
        description="Generic capability-driven CLI for IOWAP.",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="Log level (DEBUG/INFO/WARNING/ERROR). Default: env RELAY_LOG_LEVEL or INFO.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of formatted text (for scripting).",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    # heartbeat
    p_hb = sub.add_parser("heartbeat", help="Send a single heartbeat and exit.")
    p_hb.set_defaults(func=_cmd_heartbeat)

    # claim
    p_claim = sub.add_parser("claim", help="Claim one stage for a capability.")
    p_claim.add_argument("capability", help="Capability name to claim.")
    p_claim.set_defaults(func=_cmd_claim)

    # complete
    p_complete = sub.add_parser("complete", help="Complete a claimed stage.")
    p_complete.add_argument("stage_id", help="Stage ID to complete.")
    p_complete.add_argument(
        "--task",
        required=False,
        default=None,
        help="Task ID of the stage (default: RELAY_TASK_ID env, T-179).",
    )
    p_complete.add_argument(
        "--result-file",
        required=True,
        help="Path to a JSON file containing the result dict.",
    )
    p_complete.set_defaults(func=with_client(_cmd_complete))

    # task submit
    p_task = sub.add_parser("task", help="Task operations.")
    p_task_sub = p_task.add_subparsers(dest="task_command", required=True, metavar="<action>")
    p_submit = p_task_sub.add_parser("submit", help="Submit a single-stage task.")
    p_submit.add_argument("--name", default="", help="Task name (default: auto).")
    p_submit.add_argument(
        "--stage",
        required=True,
        help="Stage as <capability>:<json-payload>.",
    )
    p_submit.add_argument("--priority", type=int, default=0, help="Task priority 0-10.")
    p_submit.add_argument(
        "--owner",
        default=None,
        help="Node ID that must claim this task (owner_node_id).",
    )
    p_submit.add_argument(
        "--forward-slot",
        default=None,
        metavar="slot",
        help="Submit against the relay's 'forward' pseudo-capability: replaces the "
        "--stage capability with 'forward' and carries the slot in the payload; "
        "the relay resolves (this node, slot) -> target capability via its "
        "forward mappings (slots are per-node, 1..64 chars of A-Za-z0-9_-).",
    )
    p_submit.set_defaults(func=with_client(cli_task._cmd_task_submit))

    p_result = p_task_sub.add_parser("result", help="Show task result (status, stages, artifacts).")
    p_result.add_argument("task_id", help="Task ID to query.")
    p_result.set_defaults(func=with_client(cli_task._cmd_task_result))

    p_wait = p_task_sub.add_parser("wait", help="Wait for a task to complete and show result.")
    p_wait.add_argument("task_id", help="Task ID to wait for.")
    p_wait.add_argument("--interval", type=int, default=5, help="Poll interval in seconds (default: 5).")
    p_wait.set_defaults(func=with_client(cli_task._cmd_task_wait))

    p_note = p_task_sub.add_parser("note", help="Append a free-form note to a task (T-052 mini-chat).")
    p_note.add_argument("task_id", help="Task ID to add a note to.")
    p_note.add_argument("message", help="Note text (1..2000 characters).")
    p_note.set_defaults(func=with_client(cli_task._cmd_task_note))

    # T-248: task list — server-side filtered query of scheduler tasks.
    # --json is the existing global flag (node-cli --json task list ...).
    p_task_list = p_task_sub.add_parser(
        "list", help="List/query tasks on the relay (server-side filters)."
    )
    p_task_list.add_argument(
        "--submitted-by",
        default=None,
        help="Filter: node ID that submitted the task (submitted_by).",
    )
    p_task_list.add_argument(
        "--owner",
        default=None,
        help="Filter: node ID that must claim the task (owner_node_id).",
    )
    p_task_list.add_argument(
        "--status",
        default=None,
        help=(
            "Filter: comma-separated status values, e.g. pending,claimed "
            "(passed through unvalidated; the relay validates)."
        ),
    )
    p_task_list.add_argument(
        "--since",
        default=None,
        help="Filter: tasks created at/after this ISO-8601 timestamp (created_since).",
    )
    p_task_list.add_argument(
        "--until",
        default=None,
        help="Filter: tasks created at/before this ISO-8601 timestamp (created_until).",
    )
    p_task_list.set_defaults(func=with_client(cli_task._cmd_task_list))

    # capabilities
    p_caps = sub.add_parser("capabilities", help="Capability profile management.")
    p_caps_sub = p_caps.add_subparsers(
        dest="capabilities_command", required=True, metavar="<action>"
    )

    p_list = p_caps_sub.add_parser("list", help="List profiles in profiles.d/.")
    p_list.set_defaults(func=cli_capabilities._cmd_capabilities_list)

    p_validate = p_caps_sub.add_parser("validate", help="Validate a profile (default: active).")
    p_validate.add_argument(
        "profile", nargs="?", default=None, help="Profile name (default: active)."
    )
    p_validate.set_defaults(func=cli_capabilities._cmd_capabilities_validate)

    p_publish = p_caps_sub.add_parser(
        "publish", help="Validate + atomically copy profile to active."
    )
    p_publish.add_argument("profile", help="Profile name to publish.")
    p_publish.set_defaults(func=cli_capabilities._cmd_capabilities_publish)

    p_diff = p_caps_sub.add_parser("diff", help="Diff working profile vs active.")
    p_diff.add_argument("profile", nargs="?", default=None, help="Profile name (default: active).")
    p_diff.set_defaults(func=cli_capabilities._cmd_capabilities_diff)

    p_current = p_caps_sub.add_parser("current", help="Show active profile name.")
    p_current.set_defaults(func=cli_capabilities._cmd_capabilities_current)

    p_server = p_caps_sub.add_parser(
        "server", help="Query capabilities registered on the relay server (all nodes)."
    )
    p_server.set_defaults(func=with_client(cli_capabilities._cmd_capabilities_server))

    p_info = p_caps_sub.add_parser(
        "info", help="Show detailed info for a single capability registered on the relay."
    )
    p_info.add_argument("name", help="Capability name to query.")
    p_info.set_defaults(func=with_client(cli_capabilities._cmd_capabilities_info))

    # T-042: workspace capability management — wraps the dashboard store
    # (create_provider/update_provider/delete_provider/save_script), NOT the
    # profile commands above (list/validate/publish/diff stay profile-based).
    p_create = p_caps_sub.add_parser(
        "create", help="Create one workspace capability (dashboard store)."
    )
    p_create.add_argument("--name", required=True, help="Capability name (T-042: dashboard name regex).")
    _add_capability_field_args(p_create)
    p_create.add_argument(
        "--publish", action="store_true",
        help="Also add the capability to the bridge allowlist (set_capability_published).",
    )
    p_create.set_defaults(func=cli_capabilities._cmd_capabilities_create)

    p_update = p_caps_sub.add_parser(
        "update",
        help="Edit one workspace capability in place (name immutable, auto-bump) — or, field-less with --publish/--unpublish, toggle the bridge allowlist only.",
    )
    p_update.add_argument("--name", required=True, help="Capability name (the immutable routing key).")
    _add_capability_field_args(p_update)
    p_update_group = p_update.add_mutually_exclusive_group()
    p_update_group.add_argument(
        "--publish", action="store_true",
        help="After the edit (or alone, field-less): add to the bridge allowlist.",
    )
    p_update_group.add_argument(
        "--unpublish", action="store_true",
        help="Field-less only: remove the capability from the bridge allowlist.",
    )
    p_update.set_defaults(func=cli_capabilities._cmd_capabilities_update)

    p_delete = p_caps_sub.add_parser(
        "delete", help="Remove one workspace capability (destructive)."
    )
    p_delete.add_argument("--name", required=True, help="Capability name to delete.")
    p_delete.set_defaults(func=cli_capabilities._cmd_capabilities_delete)

    p_script_upload = p_caps_sub.add_parser(
        "script-upload",
        help="Store a capability script under <workspace>/bridge/scripts (extension allowlist, 2 MB cap).",
    )
    p_script_upload_source = p_script_upload.add_mutually_exclusive_group(required=True)
    p_script_upload_source.add_argument(
        "--file", default=None, help="Path of the script to store (basename becomes the stored name)."
    )
    p_script_upload_source.add_argument(
        "--stdin", action="store_true", help="Read the script bytes from stdin (requires --name)."
    )
    p_script_upload.add_argument(
        "--name", default=None,
        help="Override the filename (required with --stdin; must carry an allowed extension).",
    )
    p_script_upload.set_defaults(func=cli_capabilities._cmd_capabilities_script_upload)

    # node
    p_node = sub.add_parser("node", help="Node operations (list, info, busy/idle/status).")
    p_node_sub = p_node.add_subparsers(dest="node_command", required=True, metavar="<action>")

    p_node_list = p_node_sub.add_parser("list", help="List all nodes registered on the relay server.")
    p_node_list.set_defaults(func=with_client(cli_node._cmd_node_list))

    p_node_info = p_node_sub.add_parser("info", help="Show detailed info for a single node.")
    p_node_info.add_argument("node_id", help="Node ID to query.")
    p_node_info.set_defaults(func=with_client(cli_node._cmd_node_info))

    # T-084: node busy / idle / status / clear-status
    p_node_busy = p_node_sub.add_parser(
        "busy", help="Mark this node as busy (stops it from claiming new stages)."
    )
    p_node_busy.add_argument(
        "--once", action="store_true",
        help="Send a single heartbeat carrying the new status immediately.",
    )
    p_node_busy.set_defaults(func=with_client(cli_node._cmd_node_busy))

    p_node_idle = p_node_sub.add_parser(
        "idle", help="Mark this node as idle (available for claiming stages again)."
    )
    p_node_idle.add_argument(
        "--once", action="store_true",
        help="Send a single heartbeat carrying the new status immediately.",
    )
    p_node_idle.set_defaults(func=with_client(cli_node._cmd_node_idle))

    p_node_clear_status = p_node_sub.add_parser(
        "clear-status",
        help="Remove an explicit busy/idle request and restore automatic status handling.",
    )
    p_node_clear_status.add_argument(
        "--once", action="store_true",
        help="Send a single heartbeat after clearing the requested status.",
    )
    p_node_clear_status.set_defaults(func=with_client(cli_node._cmd_node_clear_status))

    p_node_status = p_node_sub.add_parser(
        "status", help="Show the local + server-side node status."
    )
    p_node_status.set_defaults(func=with_client(cli_node._cmd_node_status))

    # T-178: node register — first-class registration (no client needed).
    p_node_register = p_node_sub.add_parser(
        "register",
        help="Register this node on a relay server (creates local state files).",
    )
    p_node_register.add_argument(
        "server",
        help="Relay server: IP/host, IP:port, or full URL (default port 8788).",
    )
    p_node_register.add_argument("--name", default=None, help="Node name (default: hostname).")
    p_node_register.add_argument(
        "--force", action="store_true",
        help="Re-register even if local node state already exists.",
    )
    p_node_register.add_argument(
        "--timeout", type=float, default=15.0,
        help="HTTP timeout for the registration request (default: 15s).",
    )
    p_node_register.set_defaults(func=cli_server._cmd_node_register)

    # T-178: server status commands — unauthenticated health/metrics probe.
    p_server_cmd = sub.add_parser(
        "server", help="Inspect a relay server (health, metrics) without node credentials."
    )
    p_server_sub = p_server_cmd.add_subparsers(dest="server_command", required=True, metavar="<action>")
    p_server_health = p_server_sub.add_parser("health", help="One-shot health probe (/health + /ready).")
    p_server_health.add_argument(
        "server", nargs="?", default=None,
        help="Relay server: IP/host, IP:port, or full URL (default port 8788).",
    )
    p_server_health.set_defaults(func=cli_server._cmd_server_health)
    p_server_metrics = p_server_sub.add_parser(
        "metrics", help="Show Prometheus metrics (gauges) from the relay server."
    )
    p_server_metrics.add_argument(
        "server", nargs="?", default=None,
        help="Relay server: IP/host, IP:port, or full URL (default port 8788).",
    )
    p_server_metrics.set_defaults(func=cli_server._cmd_server_metrics)

    # T-187: relay set/discover — pin base_url or targeted mDNS discovery
    p_relay = sub.add_parser(
        "relay",
        help="Relay connection management (pin URL or discover via mDNS).",
    )
    p_relay_sub = p_relay.add_subparsers(
        dest="relay_command", required=True, metavar="<action>"
    )
    p_relay_set = p_relay_sub.add_parser(
        "set", help="Pin the relay base_url, or remove the pin (--discover)."
    )
    p_relay_set.add_argument(
        "--server-url", default=None,
        help="Pin this relay URL (discovery disabled).",
    )
    p_relay_set.add_argument(
        "--discover", action="store_true",
        help="Remove the pinned URL and re-enable mDNS discovery.",
    )
    p_relay_set.set_defaults(func=_cmd_relay_set)

    p_relay_discover = p_relay_sub.add_parser(
        "discover", help="Targeted mDNS lookup for the relay service name."
    )
    p_relay_discover.add_argument(
        "--name", default=None,
        help="Expected service name (default: mdns_service_name from "
        "relay_config.json, then 'IOWAP Relay Service').",
    )
    p_relay_discover.add_argument(
        "--timeout", type=float, default=2.0,
        help="Seconds to watch the network (default 2.0).",
    )
    p_relay_discover.set_defaults(func=_cmd_relay_discover)

    # T-136: route register/unregister/list — temp bridge route management
    p_route = sub.add_parser("route", help="Manage temporary bridge routes (T-136).")
    p_route_sub = p_route.add_subparsers(dest="route_command", required=True, metavar="<action>")

    p_route_register = p_route_sub.add_parser("register", help="Register a temporary bridge route.")
    p_route_register.add_argument("--path", required=True, help="Route path (e.g. /upload/abc123).")
    p_route_register.add_argument("--method", required=True, help="HTTP method (GET/POST/PUT/...).")
    p_route_register.add_argument("--upstream", required=True, help="Upstream URL the relay proxies to.")
    p_route_register.add_argument("--ttl", type=int, required=True, help="Time-to-live in seconds.")
    p_route_register.add_argument("--channel", required=True, help="channel_id tying this route to a session.")
    p_route_register.add_argument("--description", default="", help="Optional human-readable note.")
    p_route_register.set_defaults(func=with_client(cli_route._cmd_route_register))

    p_route_unregister = p_route_sub.add_parser("unregister", help="Revoke a temp route before its TTL.")
    p_route_unregister.add_argument("--path", required=True, help="Route path to delete.")
    p_route_unregister.add_argument("--method", required=True, help="HTTP method of the route.")
    p_route_unregister.set_defaults(func=with_client(cli_route._cmd_route_unregister))

    p_route_list = p_route_sub.add_parser("list", help="List this node's own routes (temp + permanent).")
    p_route_list.set_defaults(func=with_client(cli_route._cmd_route_list))

    # T-153: bridge upload/download — stream large files via temp bridge routes
    p_bridge = sub.add_parser(
        "bridge", help="Upload/download large files via a temp bridge route (T-129/T-153)."
    )
    p_bridge_sub = p_bridge.add_subparsers(dest="bridge_command", required=True, metavar="<action>")

    p_bridge_upload = p_bridge_sub.add_parser(
        "upload", help="Open a bridge channel and stream a local file TO the storage node."
    )
    p_bridge_upload.add_argument("file", type=str, help="Path to the file to upload.")
    p_bridge_upload.add_argument(
        "--channel", default=None,
        help="Channel id (storage.upload_channel); minted when omitted.",
    )
    p_bridge_upload.add_argument(
        "--backup", action="store_true",
        help="Use backup.create (mode=bridge) instead of storage.upload_channel.",
    )
    p_bridge_upload.add_argument("--source", default=None, help="Backup source (default: filename).")
    p_bridge_upload.add_argument("--type", default="full", choices=["full", "incremental"],
                                 help="Backup type (default: full).")
    p_bridge_upload.add_argument("--name", default="", help="Optional task name.")
    p_bridge_upload.add_argument("--priority", type=int, default=0, help="Optional task priority.")
    p_bridge_upload.add_argument("--interval", type=int, default=5,
                                 help="Poll interval in seconds (default: 5).")
    p_bridge_upload.set_defaults(func=with_client(cli_bridge._cmd_bridge_upload))

    p_bridge_download = p_bridge_sub.add_parser(
        "download", help="Open a bridge channel and stream a large file FROM the storage node."
    )
    p_bridge_download.add_argument(
        "--channel", default=None,
        help="Channel id (storage.download_channel); required unless --backup is set.",
    )
    p_bridge_download.add_argument(
        "--backup", default=None,
        help="Backup id (backup.restore). When set, --channel is ignored.",
    )
    p_bridge_download.add_argument(
        "--output", "-o", type=Path, default=None,
        help="Output path (default: <channel_id> or <backup_id>).",
    )
    p_bridge_download.add_argument("--name", default="", help="Optional task name.")
    p_bridge_download.add_argument("--priority", type=int, default=0, help="Optional task priority.")
    p_bridge_download.add_argument("--interval", type=int, default=5,
                                   help="Poll interval in seconds (default: 5).")
    p_bridge_download.set_defaults(func=with_client(cli_bridge._cmd_bridge_download))

    # T-164: file send/get — generic transfer-ladder wrapper.
    p_file = sub.add_parser(
        "file",
        help="Send/get files via the generic transfer ladder (T-164).",
    )
    p_file_sub = p_file.add_subparsers(dest="file_command", required=True, metavar="<action>")

    p_file_send = p_file_sub.add_parser(
        "send", help="Send a file via inline/artifact/bridge (auto-chosen)."
    )
    p_file_send.add_argument("file", type=str, help="Path to the file to send.")
    p_file_send.add_argument("--cap", required=True, help="Capability that accepts the file.")
    p_file_send.add_argument(
        "--force", choices=["inline", "artifact", "bridge"], default=None,
        help="Force a transfer mode (must be in the capability's upload_modes).",
    )
    p_file_send.add_argument("--name", default="", help="Target filename / task name.")
    p_file_send.add_argument("--priority", type=int, default=0, help="Task priority.")
    p_file_send.add_argument("--owner", default=None, help="Node ID that must claim the task.")
    p_file_send.add_argument("--source", default=None, help="Backup source (backup.create bridge).")
    p_file_send.add_argument(
        "--type", default="full", choices=["full", "incremental"],
        help="Backup type (backup.create bridge).",
    )
    p_file_send.add_argument(
        "--interval", type=int, default=5, help="Poll interval in seconds (bridge mode).",
    )
    p_file_send.set_defaults(func=with_client(cli_file._cmd_file_send))

    p_file_get = p_file_sub.add_parser(
        "get", help="Get a file via inline/bridge (auto-chosen by capability)."
    )
    p_file_get.add_argument(
        "ref", type=str,
        help="Generic file reference (path for storage.fetch, backup_id for backup.restore, "
             "channel_id for storage.download_channel).",
    )
    p_file_get.add_argument("--cap", required=True, help="Capability that serves the file.")
    p_file_get.add_argument(
        "--output", "-o", type=Path, default=None,
        help="Output path (default: filename from server/channel).",
    )
    p_file_get.add_argument("--name", default="", help="Task name.")
    p_file_get.add_argument("--priority", type=int, default=0, help="Task priority.")
    p_file_get.add_argument("--owner", default=None, help="Node ID that must claim the task.")
    p_file_get.add_argument(
        "--interval", type=int, default=5, help="Poll interval in seconds (bridge mode).",
    )
    p_file_get.set_defaults(func=with_client(cli_file._cmd_file_get))

    # T-179: hp put/get — handler primitives for handler scripts.
    # Scripts talk exclusively to the local node-cli (subprocess
    # contract); token/ladder/envelope negotiation stays in the CLI
    # process.
    p_hp = sub.add_parser(
        "hp", help="Handler primitives for handler scripts (T-179)."
    )
    p_hp_sub = p_hp.add_subparsers(dest="hp_cmd", required=True, metavar="<action>")

    p_hp_put = p_hp_sub.add_parser(
        "put",
        help="Wrap a local file as an envelope (ladder-negotiated; the "
        "bridge rung moves the file into the serve dir and deletes it "
        "after transfer).",
    )
    p_hp_put.add_argument(
        "path",
        help="Path to the local file to wrap (the bridge rung moves the "
        "file into the serve dir — the source is gone after a successful "
        "bridge put).",
    )
    p_hp_put.add_argument(
        "--cap", required=True, help="Receiving capability (decides upload modes)."
    )
    p_hp_put.add_argument(
        "--name", default=None, help="Override filename inside the envelope."
    )
    p_hp_put.set_defaults(func=with_client(cli_hp.dispatch_put))

    p_hp_get = p_hp_sub.add_parser(
        "get", help="Resolve an __iowap_ref__ envelope (inline/artifact) to a local file."
    )
    p_hp_get.add_argument(
        "--file", default=None,
        help="Read the envelope JSON from this file (default: stdin).",
    )
    p_hp_get.add_argument(
        "--output", "-o", type=Path, default=None,
        help="Output path (default: <out_dir>/<filename from envelope>).",
    )
    p_hp_get.add_argument(
        "--out-dir", type=Path, default=None,
        help="Default drop directory (default: ~/.relay/tmp/${RELAY_TASK_ID:-adhoc}/).",
    )
    p_hp_get.set_defaults(func=with_client(cli_hp.dispatch_get))

    # status / reload
    p_status = sub.add_parser("status", help="Print worker_status.json content.")
    p_status.set_defaults(func=_cmd_status)

    p_reload = sub.add_parser("reload", help="Send SIGHUP to running daemon.")
    p_reload.set_defaults(func=_cmd_reload)

    # artifact operations
    p_artifact = sub.add_parser("artifact", help="Artifact operations.")
    p_artifact_sub = p_artifact.add_subparsers(
        dest="artifact_command", required=True, metavar="<action>"
    )
    p_artifact_download = p_artifact_sub.add_parser(
        "download", help="Download an artifact by id from the relay."
    )
    p_artifact_download.add_argument("artifact_id", help="The artifact ID to download.")
    p_artifact_download.add_argument(
        "--output", "-o", type=Path, default=None,
        help="Output path (default: <artifact name from server>).",
    )
    p_artifact_download.set_defaults(func=with_client(cli_artifact._cmd_artifact_download))

    p_artifact_upload = p_artifact_sub.add_parser(
        "upload", help="Upload a local file as an artifact to the relay."
    )
    p_artifact_upload.add_argument("file", type=str, help="Path to the file to upload.")
    p_artifact_upload.add_argument(
        "--name", default=None, help="Artifact name (default: filename)."
    )
    p_artifact_upload.add_argument(
        "--task-id", default=None, help="Optional task ID to associate with."
    )
    p_artifact_upload.add_argument(
        "--stage-id", default=None, help="Optional stage ID to associate with."
    )
    p_artifact_upload.set_defaults(func=with_client(cli_artifact._cmd_artifact_upload))

    # T-033: short-lived signed download link
    p_artifact_link = p_artifact_sub.add_parser(
        "link", help="Create a short-lived signed download URL for an artifact."
    )
    p_artifact_link.add_argument("artifact_id", help="The artifact ID to link.")
    p_artifact_link.add_argument(
        "--ttl-seconds", type=int, default=None,
        help="Link TTL in seconds (server default: 900; accepted 30..86400).",
    )
    p_artifact_link.add_argument(
        "--max-downloads", type=int, default=None,
        help="Download cap (server default: 1; accepted 1..10).",
    )
    p_artifact_link.set_defaults(func=with_client(cli_artifact._cmd_artifact_link))

    # docs (T-059)
    p_docs = sub.add_parser(
        "docs",
        help="Read relay documentation from the server (list all, or print one).",
    )
    p_docs.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Document name (omit to list all available documents).",
    )
    p_docs.set_defaults(func=with_client(cli_docs._cmd_docs))

    # update (wheel-based self-update)
    p_update = sub.add_parser(
        "update",
        help="Check for and apply self-updates (PyPI-first, GitHub fallback).",
    )
    p_update_sub = p_update.add_subparsers(
        dest="update_command", required=True, metavar="<action>"
    )
    p_update_check = p_update_sub.add_parser(
        "check",
        help="Compare installed versions (node + MCP toolkit) against upstream.",
    )
    p_update_check.add_argument(
        "--source",
        default=None,
        choices=["auto", "pypi", "github"],
        help="Upstream: auto (PyPI→GitHub fallback, default; IOWAP_UPDATE_SOURCE), "
             "pypi (strict, no fallback) or github (legacy wheel releases).",
    )
    p_update_check.set_defaults(func=cli_update._cmd_update_check)

    p_update_apply = p_update_sub.add_parser(
        "apply",
        help="Update node (PyPI pip upgrade / GitHub fallback) and the MCP toolkit.",
    )
    p_update_apply.add_argument(
        "--source",
        default=None,
        choices=["auto", "pypi", "github"],
        help="Upstream for the node update: auto (PyPI→GitHub, default), "
             "pypi (strict) or github (legacy wheel download path).",
    )
    p_update_apply.add_argument(
        "--no-mcp",
        action="store_true",
        help="Skip the iowap-client-mcp check/upgrade in this run.",
    )
    p_update_apply.add_argument(
        "--service-unit",
        default=SERVICE_UNIT,
        help=f"systemd user unit to restart (default: {SERVICE_UNIT}).",
    )
    p_update_apply.add_argument(
        "--restart-command",
        default=RESTART_COMMAND,
        help=(
            "Command to run instead of `systemctl --user restart` (for hosts "
            "without systemd, e.g. macOS launchd). '{unit}' is replaced with "
            "the service name. Default: RELAY_RESTART_COMMAND env or systemd."
        ),
    )
    p_update_apply.set_defaults(func=cli_update._cmd_update_apply)

    # mcp (T-230): toolkit install/status — client-neutral admin surface
    cli_mcp.build_parser(sub)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        # T-226: every invocation records its own absolute path as the
        # top-level node_cli_path stamp in the active profile — consumers
        # without PATH knowledge (desktop gateway) resolve the binary from
        # there. Drift-guarded; never raises.
        from nodes.common.node_config import ensure_node_cli_stamp

        ensure_node_cli_stamp()
        return int(args.func(args))
    except SystemExit as exc:
        if exc.code is None:
            return 0
        print(exc, file=sys.stderr)
        return int(exc.code) if isinstance(exc.code, int) else 1
    except KeyboardInterrupt:
        return 130
    except httpx.HTTPError as exc:
        print(f"network error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
