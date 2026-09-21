"""CLI update subcommands — check / apply (wheel-based, successor of T-062).

The git-based update path was removed: wheel-deployed nodes have no git
checkout, and a pull would never touch the package running from
site-packages. The source of truth is now the newest ``wheel-vX.Y.Z``
release on GitHub (iowap-org/iowap-node); the installed distribution
version (importlib.metadata) is compared against it.
"""

from __future__ import annotations

import json

from nodes.common.node_utils import apply_wheel_update, check_wheel_updates
from nodes.common.relay_client import _setup_logging


def _cmd_update_check(args) -> int:
    """node-cli update check — compare installed version vs. newest release."""
    _setup_logging("ERROR" if args.json else args.log_level)
    info = check_wheel_updates()
    if args.json:
        print(json.dumps(info, default=str))
        return 0
    print(f"Local version:  {info.get('local_version') or '-'}")
    print(f"Latest release: {info.get('latest_version') or '-'} ({info.get('tag') or 'no wheel release'})")
    if info.get("error"):
        print(f"Error:          {info['error']}")
        return 1
    if info.get("update_available"):
        print("Status:         update available")
        return 0
    print("Status:         up to date")
    return 0


def _cmd_update_apply(args) -> int:
    """node-cli update apply — download wheel, pip reinstall, restart unit."""
    _setup_logging("ERROR" if args.json else args.log_level)
    result = apply_wheel_update(service_unit=args.service_unit)
    if args.json:
        print(json.dumps(result, default=str))
        return 0 if result.get("success") else 1
    print(f"Before: {result.get('before_version') or '-'}")
    print(f"After:  {result.get('after_version') or '-'}")
    print(f"Restarted: {'yes' if result.get('restarted') else 'no'}")
    print(f"Result:  {result.get('message')}")
    return 0 if result.get("success") else 1