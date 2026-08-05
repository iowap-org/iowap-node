"""CLI node subcommands — list/info/busy/idle/clear-status/status (T-117 split).

Handler signatures are plain ``(client, args) -> int``; the
``node_cli.with_client`` decorator is applied at parser-registration time
in the facade. ``log`` is referenced lazily via ``node_cli.log`` (the
facade logger) so this module does not need its own logger instance and
stays decoupled.
"""

from __future__ import annotations

import json
import sys
from typing import Optional

import nodes.common.node_cli as _cli
from nodes.common.node_config import load_active_profile, load_active_status, write_active_status
from nodes.common.node_utils import load_meta
from nodes.common.relay_client import RelayClient


def _cmd_node_list(client: RelayClient, args) -> int:
    """List all nodes registered on the relay server."""
    try:
        resp = client._get_with_retry("/relay/v2/discovery/nodes")
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"failed to query nodes: {exc}", file=sys.stderr)
        return 1

    nodes = data.get("nodes", [])
    if args.json:
        print(json.dumps(nodes, default=str))
        return 0
    if not nodes:
        print("(no nodes registered on the server)")
        return 0

    print(f"Nodes ({len(nodes)} total):\n")
    for n in nodes:
        nid = n.get("node_id", "?")
        name = n.get("node_name", "?")
        status = n.get("status", "?")
        avail = "✅" if n.get("available", False) else "❌"
        endpoint = n.get("endpoint", "-")
        role = n.get("role", "-")
        caps_raw = n.get("capabilities", "")
        caps = ", ".join(c.get("name", "?") for c in caps_raw) if isinstance(caps_raw, list) else str(caps_raw)[:60]
        last_seen = n.get("last_seen", "?")[:19] if n.get("last_seen") else "?"
        print(f"  {avail} {name:20}  ID={nid}")
        print(f"      Status:   {status:10} Role: {role}")
        print(f"      Endpoint: {endpoint}")
        print(f"      Last:     {last_seen}")
        print(f"      Caps:     {caps}")
        # T-072: show node-level description (truncated for the list view).
        desc = n.get("description", "")
        if desc:
            print(f"      Desc:     {desc[:60]}{'...' if len(desc) > 60 else ''}")
        print()
    return 0


def _cmd_node_info(client: RelayClient, args) -> int:
    """Show detailed info for a single node."""
    try:
        resp = client._get_with_retry("/relay/v2/discovery/nodes?status=all")
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"failed to query nodes: {exc}", file=sys.stderr)
        return 1

    nodes = data.get("nodes", [])
    # Some older server versions treat ``status=all`` as a literal status filter
    # (returning an empty list). Fall back to the unfiltered endpoint so the
    # command still works for offline/pending nodes.
    if not nodes:
        try:
            resp = client._get_with_retry("/relay/v2/discovery/nodes")
            resp.raise_for_status()
            nodes = resp.json().get("nodes", [])
        except Exception:
            pass
    node = None
    for n in nodes:
        if n.get("node_id") == args.node_id:
            node = n
            break

    if not node:
        print(f"Node '{args.node_id}' not found.")
        return 1

    if args.json:
        print(json.dumps(node, default=str))
        return 0
    print(f"Node:        {node.get('node_name', '?')}")
    print(f"ID:          {node.get('node_id', '?')}")
    print(f"Status:      {node.get('status', '?')}")
    print(f"Role:        {node.get('role', '-')}")
    print(f"Available:   {'yes' if node.get('available', False) else 'no'}")
    print(f"Endpoint:    {node.get('endpoint', '-')}")
    print(f"Load:        {node.get('load', 0):.1f}")
    print(f"Queue Depth: {node.get('queue_depth', 0)}")
    print(f"Last Seen:   {node.get('last_seen', '?')}")
    print(f"Registered:  {node.get('registered_at', '?')}")
    # T-072: show the full node-level description.
    desc = node.get("description", "")
    if desc:
        print(f"Description: {desc}")

    caps_raw = node.get("capabilities", "")
    if isinstance(caps_raw, list) and caps_raw:
        print(f"\nCapabilities ({len(caps_raw)}):")
        for c in caps_raw:
            cname = c.get("name", "?")
            cver = c.get("version", "?")
            cavail = "✅" if c.get("available", False) else "❌"
            print(f"  {cavail} {cname:25} v{cver}")
    elif caps_raw:
        print(f"\nCapabilities: {caps_raw}")

    return 0


# ---------------------------------------------------------------------------
# node busy / idle / status (T-084) — status stored in active YAML profile
# ---------------------------------------------------------------------------


def _save_requested_status(status: str) -> None:
    """Persist the operator-requested node status into the active YAML profile.

    The heartbeat loop reads the YAML ``status`` field and forwards it
    to the server on the next heartbeat, where the transition is validated
    against the central registry. The YAML file is the only source for the
    daemon so the requested status survives restarts.
    """
    write_active_status(status)


def _clear_requested_status() -> None:
    """Remove the operator-requested status so the node returns to auto."""
    write_active_status(None)


def _cmd_node_busy(client: RelayClient, args) -> int:
    """node-cli node busy — mark this node as busy.

    Persists ``status: busy`` into the meta file so the next heartbeat
    forwards it to the server. The server validates the transition
    (online/idle → busy) via the central status registry; an invalid
    transition is silently ignored on the server side.

    With ``--once`` the daemon is not affected and a single heartbeat
    carrying the new status is sent immediately.
    """
    _save_requested_status("busy")
    if args.once:
        caps = load_active_profile()
        client.heartbeat(caps, {})
    if args.json:
        print(json.dumps({"status": "busy", "persisted": True, "once": bool(args.once)}))
        return 0
    print("✅ Node marked busy — next heartbeat will forward the new status.")
    if not args.once:
        print("   Send SIGHUP or restart the daemon to pick up the change immediately.")
    return 0


def _cmd_node_idle(client: RelayClient, args) -> int:
    """node-cli node idle — mark this node as idle (available for claims)."""
    _save_requested_status("idle")
    if args.once:
        caps = load_active_profile()
        client.heartbeat(caps, {})
    if args.json:
        print(json.dumps({"status": "idle", "persisted": True, "once": bool(args.once)}))
        return 0
    print("✅ Node marked idle — next heartbeat will forward the new status.")
    if not args.once:
        print("   Send SIGHUP or restart the daemon to pick up the change immediately.")
    return 0


def _cmd_node_clear_status(client: RelayClient, args) -> int:
    """node-cli node clear-status — remove an explicit status request.

    After this the node returns to automatic status handling (the
    server sets it to ``online`` on the next heartbeat and may later
    flip it to ``busy`` via the auto-busy load tracking).
    """
    _clear_requested_status()
    if args.once:
        caps = load_active_profile()
        client.heartbeat(caps, {})
    if args.json:
        print(json.dumps({"status": None, "persisted": True, "once": bool(args.once)}))
        return 0
    print("✅ Explicit node status cleared — auto handling restored.")
    return 0


def _cmd_node_status(client: RelayClient, args) -> int:
    """node-cli node status — show the local + server-side node status.

    Reports the operator-requested status from the active YAML profile
    (if any) and queries the server for the authoritative current
    status of this node.
    """
    meta = load_meta()
    requested = load_active_status()
    server_status: Optional[str] = None
    server_load = None
    server_queue = None
    try:
        resp = client._get_with_retry("/relay/v2/discovery/nodes?status=all")
        if resp.status_code == 200:
            nodes = resp.json().get("nodes", [])
            if not nodes:
                resp = client._get_with_retry("/relay/v2/discovery/nodes")
                nodes = resp.json().get("nodes", []) if resp.status_code == 200 else []
            for n in nodes:
                if n.get("node_id") == meta.get("node_id"):
                    server_status = n.get("status")
                    server_load = n.get("load")
                    server_queue = n.get("queue_depth")
                    break
    except Exception as exc:  # noqa: BLE001
        _cli.log.warning("could not query server-side node status: %s", exc)

    if args.json:
        print(json.dumps({
            "node_id": meta.get("node_id"),
            "requested_status": requested,
            "server_status": server_status,
            "load": server_load,
            "queue_depth": server_queue,
        }, default=str))
        return 0
    print(f"Node:             {meta.get('node_name', meta.get('node_id', '?'))}")
    print(f"Requested status: {requested or '(auto)'}")
    print(f"Server status:    {server_status or '-'}")
    if server_load is not None:
        print(f"Server load:      {server_load}")
    if server_queue is not None:
        print(f"Server queue:     {server_queue}")
    return 0