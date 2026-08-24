"""CLI route subcommands — register/unregister/list temp bridge routes (T-136).

Handler signatures are plain ``(client, args) -> int``; the
``node_cli.with_client`` decorator is applied at parser-registration time
in the facade. Errors are printed to stderr and the command returns 1 so
the caller (operator or script) sees a clear failure without a logger
dependency.
"""

from __future__ import annotations

import json
import sys

from nodes.common.relay_client import RelayClient


def _cmd_route_register(client: RelayClient, args) -> int:
    """node-cli route register — open a temporary bridge route (T-136)."""
    try:
        result = client.register_temp_route(
            path=args.path,
            method=args.method,
            upstream=args.upstream,
            ttl_seconds=args.ttl,
            channel_id=args.channel,
            description=args.description or "",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"failed to register route: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, default=str))
        return 0
    print("✅ Temp route registered")
    print(f"   Path:        {result.get('path')}")
    print(f"   Method:      {result.get('method')}")
    print(f"   Channel:     {result.get('channel_id')}")
    print(f"   Expires at:  {result.get('expires_at')}")
    return 0


def _cmd_route_unregister(client: RelayClient, args) -> int:
    """node-cli route unregister — revoke a temp route before its TTL (T-136)."""
    try:
        client.unregister_temp_route(path=args.path, method=args.method)
    except Exception as exc:  # noqa: BLE001
        print(f"failed to unregister route: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"status": "deleted", "path": args.path, "method": args.method}))
        return 0
    print("✅ Temp route deleted")
    print(f"   Path:   {args.path}")
    print(f"   Method: {args.method}")
    return 0


def _cmd_route_list(client: RelayClient, args) -> int:
    """node-cli route list — list this node's own routes (T-136)."""
    try:
        routes = client.list_temp_routes()
    except Exception as exc:  # noqa: BLE001
        print(f"failed to list routes: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(routes, default=str))
        return 0

    if not routes:
        print("(no routes registered for this node)")
        return 0

    print(f"Routes ({len(routes)} total):\n")
    for r in routes:
        path = r.get("path", "?")
        method = r.get("method", "?")
        auth = r.get("auth", "?")
        upstream = r.get("upstream", "-")
        expires = r.get("expires_at") or "permanent"
        channel = r.get("channel_id") or "-"
        kind = "temp" if r.get("expires_at") else "perm "
        print(f"  [{kind}] {method:6} {path}")
        print(f"          auth:     {auth}")
        print(f"          upstream: {upstream}")
        print(f"          channel:  {channel}")
        print(f"          expires:  {expires}")
        print()
    return 0
