"""T-178: first-class node registration + unauthenticated server status CLI.

``_cmd_node_register`` needs no RelayClient (registration happens *before*
any state files exist); the server status commands reuse the T-177
``probe_server()`` logic at module level so they work without meta/token.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import httpx

import nodes.common.node_cli as _cli
from nodes.common import node_utils
from nodes.common.node_utils import save_meta, save_token
from nodes.common.relay_client import _parse_prometheus_gauges

REGISTER_PATH = "/relay/v2/auth/register"
DEFAULT_PORT = 8788


def normalize_base_url(server: str) -> str:
    """Normalize a server argument to a usable base URL.

    ``192.168.2.60`` → ``http://192.168.2.60:8788``; a ``host:port`` keeps
    its port; a full URL is kept as-is (trailing slash stripped).
    """
    server = server.strip().rstrip("/")
    if server.startswith(("http://", "https://")):
        return server
    host, sep, port = server.rpartition(":")
    if sep and port.isdigit():
        return f"http://{host}:{port}"
    return f"http://{server}:{DEFAULT_PORT}"


# ---------------------------------------------------------------------------
# node register (T-178)
# ---------------------------------------------------------------------------


def _cmd_node_register(args) -> int:
    """node-cli node register <server> — register this node and persist state.

    Chicken-and-egg-free: runs without meta/token files and creates them.
    Never writes anything on failure.
    """
    import socket

    base_url = normalize_base_url(args.server)
    name = args.name or socket.gethostname()

    # Guard: refuse to clobber an existing node identity unless --force.
    meta_path = node_utils.META_PATH
    if meta_path.exists() and not args.force:
        print(
            f"node state already exists at {meta_path} "
            f"(node already registered?). Use --force to re-register.",
            file=sys.stderr,
        )
        return 1

    body = {
        "node_name": name,
        "endpoint": None,
        "role": "node",
        "capabilities": [],
    }
    try:
        resp = httpx.post(f"{base_url}{REGISTER_PATH}", json=body, timeout=args.timeout)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        print(
            f"registration failed: HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"registration failed: {exc}", file=sys.stderr)
        return 1

    node_id = data.get("node_id")
    token = data.get("token")
    if not node_id or not token:
        print(f"unexpected registration response: {json.dumps(data)[:200]}", file=sys.stderr)
        return 1

    meta = {
        "node_id": node_id,
        "node_name": name,
        "endpoint": None,
        "registration_secret": data.get("registration_secret"),
        "capabilities": [],
        "base_url": base_url,
    }
    save_meta(meta)
    save_token(token, data.get("expires_at"))

    if args.json:
        print(json.dumps({"ok": True, **meta, "status": data.get("status", "pending")}, default=str))
        return 0

    print(f"✅ Node registered: {name} (ID={node_id})")
    print(f"   Server:  {base_url}")
    print(f"   Status:  {data.get('status', 'pending')}")
    if data.get("expires_at"):
        print(f"   Token:   temporary tp_-token, expires {data['expires_at']}")
    print()
    print("   ⏳ Node is pending — an admin must approve it (dashboard or admin API)")
    print("      before it can claim work.")
    print("   Next: node-cli capabilities publish <profile> && node-daemon")
    return 0


# ---------------------------------------------------------------------------
# server health / metrics (T-178; probe logic from T-177)
# ---------------------------------------------------------------------------


def probe_server_endpoint(
    base_url: str,
    *,
    timeout: float = 10.0,
    verify: str | bool = True,
) -> dict[str, Any]:
    """GET /health + /ready + /metrics without client state.

    Module-level mirror of ``RelayClient.probe_server()`` (T-177) so the CLI
    can probe a server *before* the node has meta/token files. Never raises;
    returns ``{"ok": False, "error": ...}`` on failure.
    """
    result: dict[str, Any] = {"ok": False, "error": ""}
    try:
        r = httpx.get(f"{base_url}/health", timeout=timeout, verify=verify)
        r.raise_for_status()
        health = r.json()
        result["version"] = health.get("version")
        result["mode"] = health.get("mode")
    except Exception as exc:  # noqa: BLE001 — probe must never throw
        result["error"] = f"health: {exc}"
        return result
    try:
        r = httpx.get(f"{base_url}/ready", timeout=timeout, verify=verify)
        r.raise_for_status()
        ready = r.json()
        result["database"] = ready.get("database")
        result["scheduler"] = ready.get("scheduler")
    except Exception as exc:  # noqa: BLE001
        # /health OK but /ready down → server is up but degraded.
        result["error"] = f"ready: {exc}"
    try:
        r = httpx.get(f"{base_url}/metrics", timeout=timeout, verify=verify)
        r.raise_for_status()
        result.update(_parse_prometheus_gauges(r.text))
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"metrics: {exc}"
    result["ok"] = not result["error"]
    return result


def resolve_server(server: str | None, as_json: bool = False) -> str | None:
    """Resolve the server argument: arg → config file / node state → error."""
    if server:
        return normalize_base_url(server)
    cfg = _cli.load_json(node_utils.CONFIG_PATH, default={}) or {}
    base = cfg.get("base_url")
    if not base and node_utils.META_PATH.exists():
        meta = _cli.load_json(node_utils.META_PATH, default={}) or {}
        base = meta.get("base_url")
    if base:
        return str(base).rstrip("/")
    msg = "no server given and no base_url in relay_config.json or node state"
    if as_json:
        print(json.dumps({"ok": False, "error": msg}))
    else:
        print(msg, file=sys.stderr)
    return None


def _fmt_num(val: Any) -> str:
    """Display helper: 4.0 → '4', 12.87 → '12.87'."""
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val)


def _cmd_server_health(args) -> int:
    """node-cli server health [server] — one-shot server status probe."""
    base_url = resolve_server(args.server, getattr(args, "json", False))
    if base_url is None:
        return 1
    data = probe_server_endpoint(base_url)
    if args.json:
        print(json.dumps(data, default=str))
        return 0 if data["ok"] else 1
    if not data["ok"]:
        print(f"❌ Server unreachable: {data['error']}", file=sys.stderr)
        return 1
    print(f"✅ {base_url} — IOWAP {data.get('version', '?')} ({data.get('mode', '?')})")
    print(f"   Database:  {data.get('database', '?')}")
    print(f"   Scheduler: {data.get('scheduler', '?')}")
    total = data.get("nodes_total")
    online = data.get("nodes_online")
    if total is not None:
        print(f"   Nodes:     {_fmt_num(online)}/{_fmt_num(total)} online")
    if data.get("queue_depth") is not None:
        print(f"   Queue:     {_fmt_num(data.get('queue_depth'))} tasks waiting")
    completed = data.get("tasks_completed")
    failed = data.get("tasks_failed")
    if completed is not None:
        print(f"   Tasks:     {_fmt_num(completed)} completed, {_fmt_num(failed)} failed")
    return 0


def _cmd_server_metrics(args) -> int:
    """node-cli server metrics [server] — merged probe dict, Prometheus fields."""
    base_url = resolve_server(args.server, getattr(args, "json", False))
    if base_url is None:
        return 1
    data = probe_server_endpoint(base_url)
    if not data["ok"]:
        msg = f"server unreachable: {data['error']}"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(f"❌ {msg}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(data, default=str))
        return 0
    print(f"Server metrics ({base_url}):")
    for key in sorted(data):
        if key in ("ok", "error"):
            continue
        val = data[key]
        if isinstance(val, dict):
            print(f"  {key}:")
            for sub, sv in sorted(val.items()):
                print(f"    {sub:24} {sv}")
        elif isinstance(val, float) and val.is_integer():
            print(f"  {key:28} {int(val)}")
        else:
            print(f"  {key:28} {val}")
    return 0