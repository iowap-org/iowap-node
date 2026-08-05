"""CLI update subcommands — check / apply (T-117 split).

These handlers have no RelayClient; they call local git helpers in
node_utils. ``REPO_DIR`` is referenced lazily via ``node_utils.REPO_DIR``
so the ``node_utils.REPO_DIR`` monkeypatch (applied by the test fixture)
takes effect — a ``cli.REPO_DIR`` patch becomes a harmless no-op.
"""

from __future__ import annotations

import json

from nodes.common import node_utils as _nu
from nodes.common.relay_client import _setup_logging
from nodes.common.node_utils import apply_update, check_for_updates


def _cmd_update_check(args) -> int:
    """node-cli update check — fetch origin and compare local vs. upstream."""
    _setup_logging("ERROR" if args.json else args.log_level)
    info = check_for_updates()
    if args.json:
        print(json.dumps(info, default=str))
        return 0
    print(f"Repo:           {_nu.REPO_DIR}")
    print(f"Local commit:   {info.get('local_commit') or '-'}")
    print(f"Local branch:   {info.get('local_branch') or '-'}")
    print(f"Upstream:       {'yes' if info.get('has_upstream') else 'no (not configured)'}")
    print(f"Remote commit:  {info.get('remote_commit') or '-'}")
    behind = info.get("behind_count", 0)
    if not info.get("has_upstream"):
        print("Status:         no upstream configured — cannot determine updates")
        return 1
    if behind > 0:
        print(f"Status:         {behind} commit{'s' if behind != 1 else ''} behind — update available")
        return 0
    print("Status:         up to date")
    return 0


def _cmd_update_apply(args) -> int:
    """node-cli update apply — git pull + restart the systemd service."""
    _setup_logging("ERROR" if args.json else args.log_level)
    result = apply_update(service_unit=args.service_unit)
    if args.json:
        print(json.dumps(result, default=str))
        return 0 if result.get("success") else 1
    print(f"Before: {result.get('before_commit') or '-'}")
    print(f"After:  {result.get('after_commit') or '-'}")
    print(f"Restarted: {'yes' if result.get('restarted') else 'no'}")
    print(f"Result:  {result.get('message')}")
    return 0 if result.get("success") else 1