"""CLI mcp subcommands — install / status (T-230, client-neutral).

Installs and verifies the MCP toolkit into its own venv from PyPI and
reports the stdio command path as DATA. No wiring, no per-client advice:
MCP hosts (Hermes, Codex, OpenCode, …) know how to register a stdio
server themselves.
"""

from __future__ import annotations

import json

from nodes.common.node_utils import (
    _mcp_block,
    get_pypi_latest_version,
    install_mcp_toolkit,
    resolve_mcp_install,
)
from nodes.common.relay_client import _setup_logging

MCP_VENV_ENV = "IOWAP_MCP_VENV"


def _default_venv() -> str:
    import os

    return os.environ.get(MCP_VENV_ENV) or str(__import__("pathlib").Path.home() / "iowap-client-mcp" / ".venv")


def _cmd_mcp_install(args) -> int:
    """node-cli mcp install — PyPI install into its own venv + verification."""
    _setup_logging("ERROR" if args.json else args.log_level)
    result = install_mcp_toolkit(venv_path=args.venv)
    if args.json:
        print(json.dumps(result, default=str))
        return 0 if result.get("success") else 1
    print(f"Venv:    {result['venv']}")
    print(f"Binary:  {result['binary'] or '-'}")
    print(f"Version: {result.get('version') or '-'}"
          + (f" (was {result['before_version']})" if result.get("before_version") and result.get("before_version") != result.get("version") else ""))
    print(f"Status:  {result['message']}")
    return 0 if result.get("success") else 1


def _cmd_mcp_status(args) -> int:
    """node-cli mcp status — discovery detail + versions, nothing mutated."""
    _setup_logging("ERROR" if args.json else args.log_level)
    resolve = resolve_mcp_install()
    block = _mcp_block(discovered=resolve, source_check=True)
    resolve_view = {
        k: (str(v) if hasattr(v, "as_posix") else v)
        for k, v in resolve.items()
    }
    pypi = get_pypi_latest_version("iowap-client-mcp")
    out = {
        "discovery": resolve_view,
        "toolkit": block,
        "pypi_latest": pypi["latest_version"],
        "pypi_error": pypi["error"],
    }
    if args.json:
        print(json.dumps(out, default=str))
        return 0
    if block.get("installed"):
        print(f"Installed: {block.get('local_version')} in {block.get('venv') or '?'}")
        print(f"PyPI:      {pypi['latest_version'] or '-'} → "
              f"{'update available' if block.get('update_available') else 'up to date'}")
        print(f"Command:   {block.get('binary') or resolve_view.get('venv_python', '-').replace('/python', '/iowap-mcp')}")
    else:
        print(f"Not installed (discovery: {resolve_view}) — install: node-cli mcp install")
    return 0


def build_parser(subparsers) -> None:
    """Attach the ``mcp`` group (install/status) — CLI admin surface only."""
    p_mcp = subparsers.add_parser(
        "mcp",
        help="Manage the local MCP toolkit (iowap-client-mcp).",
    )
    mcp_sub = p_mcp.add_subparsers(dest="mcp_command", required=True, metavar="<action>")
    p_inst = mcp_sub.add_parser(
        "install",
        help="Install/upgrade iowap-client-mcp from PyPI into its own venv "
             "(client-neutral — wires no MCP client itself).",
    )
    p_inst.add_argument(
        "--venv",
        default=None,
        help=f"Toolkit venv path (default: {MCP_VENV_ENV} env or ~/iowap-client-mcp/.venv).",
    )
    p_inst.set_defaults(func=_cmd_mcp_install)
    p_status = mcp_sub.add_parser(
        "status",
        help="Toolkit discovery + versions (read-only).",
    )
    p_status.set_defaults(func=_cmd_mcp_status)