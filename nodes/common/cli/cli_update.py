"""CLI update subcommands — check / apply (T-229: PyPI-first + MCP co-update).

Source resolution: PyPI JSON API by default (``--source auto``) with the
GitHub wheel-v release as fallback; ``--source pypi``/``--source github``
pin strictly. Both the node wheel AND the curated MCP toolkit
(``iowap-client-mcp``, when installed) are checked and updated in the same
run — the toolkit upgrade targets its own venv and needs no service restart
(client-spawned servers reload on the next session)."""

from __future__ import annotations

import json

from nodes.common.node_utils import apply_updates, check_updates
from nodes.common.relay_client import _setup_logging


def _cmd_update_check(args) -> int:
    """node-cli update check — compare installed versions vs. upstream.

    PyPI-first (GitHub fallback); reports the node wheel AND the MCP
    toolkit when it is installed on this host.
    """
    _setup_logging("ERROR" if args.json else args.log_level)
    info = check_updates(source=getattr(args, "source", None), include_mcp=True)
    if args.json:
        print(json.dumps(info, default=str))
        return 0
    mcp = info.get("mcp") or {}
    print(f"Local version:  {info.get('local_version') or '-'}")
    print(f"Latest:         {info.get('latest_version') or '-'} "
          f"(source: {info.get('source') or '-'}"
          + (f", tag {info['tag']}" if info.get("tag") else "") + ")")
    if info.get("pypi_error"):
        print(f"PyPI note:      {info['pypi_error']} — fell back to GitHub")
    print(f"Status:         {'update available' if info.get('update_available') else 'up to date'}")
    if not mcp.get("installed"):
        print("MCP toolkit:    not installed (optional — iowap-client-mcp)")
    else:
        print(f"MCP toolkit:    {mcp.get('local_version')} vs {mcp.get('latest_version')} "
              f"-> {'update available' if mcp.get('update_available') else 'up to date'}")
    if info.get("error"):
        print(f"Error:          {info['error']}")
        return 1
    return 0


def _cmd_update_apply(args) -> int:
    """node-cli update apply — PyPI pip upgrade (default), GitHub fallback."""
    _setup_logging("ERROR" if args.json else args.log_level)
    result = apply_updates(
        source=getattr(args, "source", None),
        include_mcp=not args.no_mcp,
        service_unit=args.service_unit,
        restart_command=args.restart_command,
    )
    if args.json:
        print(json.dumps(result, default=str))
        return 0 if result.get("success") else 1
    node = result.get("node") or {}
    mcp = result.get("mcp") or {}
    print(f"Node: {'->'.join(filter(None, [node.get('before'), node.get('after')])) or result.get('message')}")
    if isinstance(mcp, str):
        print(f"MCP toolkit: {mcp}")
    elif mcp.get("installed"):
        pair = '->'.join(filter(None, [mcp.get('before'), mcp.get('after')]))
        print(f"MCP toolkit: {pair or mcp.get('version')} ({mcp.get('status', '')})")
    else:
        print("MCP toolkit: not installed")
    print(f"Result: {result.get('message')}")
    return 0 if result.get("success") else 1