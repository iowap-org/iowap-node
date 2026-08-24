"""CLI capabilities subcommands — list/validate/publish/diff/current/server/info (T-117 split).

Constants ``ACTIVE_PATH`` and ``PROFILES_DIR`` are referenced lazily via
``node_config`` (``_nc.ACTIVE_PATH`` / ``_nc.PROFILES_DIR``) so the
``cl.ACTIVE_PATH`` / ``cl.PROFILES_DIR`` monkeypatches applied by the test
fixture take effect. ``PID_PATH`` (CLI-specific, lives in the facade) is
referenced lazily via ``node_cli.PID_PATH`` at call time — this is the
one tolerated circular access because it only resolves inside a handler
body, after ``node_cli`` is fully initialised.

The ``with_client``-decorated handlers (server/info) keep the plain
``(client, args) -> int`` signature; the decorator is applied at
parser-registration time in the facade.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from typing import Any

import nodes.common.node_cli as _cli
from nodes.common import node_config as _nc
from nodes.common.node_config import (
    CapabilityValidationError,
    current_profile_name,
    diff_profiles,
    list_profiles,
    load_profile,
    profile_path,
    publish_profile,
    validate_profile,
)
from nodes.common.node_utils import pid_running, read_pid
from nodes.common.relay_client import RelayClient, _setup_logging


def _cmd_capabilities_list(args) -> int:  # noqa: ARG001
    profiles = list_profiles()
    if not profiles:
        print("(no profiles in %s)" % _nc.PROFILES_DIR)
        return 0
    active = current_profile_name()
    for p in profiles:
        marker = "*" if active and (p.stem == active or p.name == active) else " "
        print(f"{marker} {p.stem}")
    return 0


def _cmd_capabilities_validate(args) -> int:
    _setup_logging(args.log_level)
    target = args.profile
    if target is None:
        if not _nc.ACTIVE_PATH.exists():
            print("no active profile and no profile name given", file=sys.stderr)
            return 1
        path = _nc.ACTIVE_PATH
        target = "active"
    else:
        path = profile_path(target)
    try:
        caps = validate_profile(path)
    except CapabilityValidationError as exc:
        print(f"INVALID {target}: {exc}", file=sys.stderr)
        return 1
    print(f"OK {target} ({len(caps)} capability{'ies' if len(caps) != 1 else ''})")
    for c in caps:
        print(f"  - {c['name']} v{c['version']} "
              f"auto_publish={c['auto_publish']} claimable={c['claimable']} "
              f"max_parallel={c['max_parallel']} timeout={c['timeout']}")
    return 0


def _cmd_capabilities_publish(args) -> int:
    _setup_logging(args.log_level)
    try:
        active = publish_profile(args.profile)
    except CapabilityValidationError as exc:
        print(f"publish FAILED: {exc}", file=sys.stderr)
        return 1
    # Best-effort SIGHUP to running daemon.
    pid = read_pid(_cli.PID_PATH)
    if pid is not None and pid_running(pid):
        try:
            os.kill(pid, signal.SIGHUP)
            print(f"published '{args.profile}' -> {active} (sent SIGHUP to pid {pid})")
        except OSError as exc:
            print(f"published '{args.profile}' -> {active} (SIGHUP failed: {exc})", file=sys.stderr)
    else:
        print(f"published '{args.profile}' -> {active} (daemon not running)")
    return 0


def _cmd_capabilities_diff(args) -> int:
    _setup_logging(args.log_level)
    if args.profile is None:
        if not _nc.ACTIVE_PATH.exists():
            print("no active profile and no profile name given", file=sys.stderr)
            return 1
        working_path = _nc.ACTIVE_PATH
        working_label = "active"
    else:
        working_path = profile_path(args.profile)
        working_label = args.profile
    try:
        working = load_profile(working_path)
    except CapabilityValidationError as exc:
        print(f"working profile invalid: {exc}", file=sys.stderr)
        return 1
    if _nc.ACTIVE_PATH.exists():
        try:
            active = load_profile(_nc.ACTIVE_PATH)
        except CapabilityValidationError as exc:
            print(f"active profile invalid: {exc}", file=sys.stderr)
            return 1
    else:
        active = []
    diff = diff_profiles(active, working)
    if not diff["added"] and not diff["removed"] and not diff["changed"]:
        print(f"no differences between active and {working_label}")
        return 0
    print(f"diff active -> {working_label}:")
    for c in diff["added"]:
        print(f"+ {c['name']} v{c['version']}")
    for name in diff["removed"]:
        print(f"- {name}")
    for ch in diff["changed"]:
        print(f"~ {ch['name']}:")
        _print_cap_diff(ch["old"], ch["new"])
    return 0


def _print_cap_diff(old: dict[str, Any], new: dict[str, Any]) -> None:
    keys = ("version", "auto_publish", "claimable", "handler", "max_parallel", "timeout")
    for k in keys:
        ov, nv = old.get(k), new.get(k)
        if ov != nv:
            print(f"    {k}: {ov!r} -> {nv!r}")


def _cmd_capabilities_current(args) -> int:  # noqa: ARG001
    name = current_profile_name()
    if name is None:
        print("(no active profile set)")
        return 1
    print(name)
    return 0


def _cmd_capabilities_server(client: RelayClient, args) -> int:
    """Query capabilities from the relay server (all registered nodes)."""
    try:
        resp = client._get_with_retry("/relay/v2/discovery/capabilities")
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"failed to query server capabilities: {exc}", file=sys.stderr)
        return 1

    caps = data.get("capabilities", data) if isinstance(data, dict) else data
    if args.json:
        print(json.dumps(caps, default=str))
        return 0
    if not caps:
        print("(no capabilities registered on the server)")
        return 0

    print(f"Server capabilities ({len(caps)} total):\n")
    for c in caps:
        name = c.get("name", "?")
        ver = c.get("version", "?")
        avail = c.get("available", False)
        nodes = c.get("nodes", [])
        status = "✅" if avail else "❌"
        node_names = ", ".join(
            f"{n.get('node_name', '?')} ({n.get('node_id', '?')})" for n in nodes
        ) if nodes else "(no nodes)"
        print(f"  {status} {name:20} v{ver:8}  [{node_names}]")
        desc = c.get("description")
        if desc:
            print(f"     {desc}")
        schema = c.get("input_schema")
        if schema:
            print(f"     Input: {json.dumps(schema, indent=6)}")
        print()
    return 0


def _cmd_capabilities_info(client: RelayClient, args) -> int:
    """Show detailed info for a single capability registered on the relay."""
    try:
        resp = client._get_with_retry(f"/relay/v2/discovery/capabilities/{args.name}")
        if resp.status_code == 404:
            print(f"Capability '{args.name}' not found.")
            return 1
        resp.raise_for_status()
        cap = resp.json()
    except Exception as exc:
        print(f"failed to query capability: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(cap, default=str))
        return 0
    print(f"Name:        {cap.get('name', '?')}")
    print(f"Type:        {cap.get('type', '-')}")
    print(f"Version:     {cap.get('version', '?')}")
    print(f"Available:   {'yes' if cap.get('available', False) else 'no'}")
    desc = cap.get("description")
    if desc:
        print(f"Description: {desc}")
    schema = cap.get("input_schema")
    if schema:
        print("Input Schema:")
        print(json.dumps(schema, indent=2))
    nodes = cap.get("nodes", [])
    if nodes:
        print(f"\nNodes ({len(nodes)}):")
        for n in nodes:
            print(
                f"  - {n.get('node_name', '?')} ({n.get('node_id', '?')}) "
                f"(load={n.get('load', 0):.1f}, "
                f"queue={n.get('queue_depth', 0)})"
            )
    return 0