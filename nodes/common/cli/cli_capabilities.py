"""CLI capabilities subcommands — list/validate/publish/diff/current/server/info +
T-042 workspace-store management (create/update/delete/script-upload).

Constants ``ACTIVE_PATH`` and ``PROFILES_DIR`` are referenced lazily via
``node_config`` (``_nc.ACTIVE_PATH`` / ``_nc.PROFILES_DIR``) so the
``cl.ACTIVE_PATH`` / ``cl.PROFILES_DIR`` monkeypatches applied by the test
fixture take effect. ``PID_PATH`` (polling daemon, lives in poll_daemon
since T-250 — the CLI no longer re-exports it) is referenced lazily via
``poll_daemon.PID_PATH`` at call time. ``node_daemon.PID_PATH`` (SSE
daemon) is resolved the same lazy way inside the publish handler
(t_c214ca25: the SSE daemon's pid file must be probed for hot-reload).

The ``with_client``-decorated handlers (server/info) keep the plain
``(client, args) -> int`` signature; the decorator is applied at
parser-registration time in the facade.

T-042: the ``create``/``update``/``delete``/``script-upload`` subcommands
manage the WORKSPACE capability store (``<workspace>/bridge/providers.yaml``)
and wrap the dashboard write functions verbatim — ``dashboard.create_provider``
/ ``update_provider`` / ``delete_provider`` / ``save_script`` — so every
validation rule (write-nothing-on-error, duplicate "delete it first",
name regex, version shape, auto-bump) is the dashboard's rule, not a copy.
They are deliberately separate from the profile commands above
(list/validate/publish/diff operate on ``profiles.d/`` entries). Client-free:
the relay is never contacted; the daemon picks changes up on its own
``providers.yaml`` watch (T-022), and ``--publish`` toggles the allowlist via
``set_capability_published`` which recomposes the active profile and
SIGHUPs live daemons itself.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from nodes.common import node_config as _nc
from nodes.common import node_utils as _nu
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
              f"max_parallel={c['max_parallel']} cost={_nc.capability_cost(c)} "
              f"timeout={c['timeout']}")
    return 0


def _cmd_capabilities_publish(args) -> int:
    _setup_logging(args.log_level)
    try:
        active = publish_profile(args.profile)
    except CapabilityValidationError as exc:
        print(f"publish FAILED: {exc}", file=sys.stderr)
        return 1
    # Best-effort SIGHUP to any running daemon. The SSE daemon writes
    # node-daemon.pid; the legacy polling daemon writes node-cli.pid —
    # both install SIGHUP handlers, so notify every live pid file
    # (t_c214ca25: the old code only probed node-cli.pid and always took
    # the "daemon not running" branch on SSE-only nodes).
    from nodes.common.node_daemon import PID_PATH as _DAEMON_PID_PATH
    from nodes.common import poll_daemon as _pd

    pids = _nu.sighup_pid_files([_DAEMON_PID_PATH, _pd.PID_PATH])
    if pids:
        print(f"published '{args.profile}' -> {active} (sent SIGHUP to pid {', '.join(str(p) for p in pids)})")
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
    keys = ("version", "auto_publish", "claimable", "handler", "max_parallel", "cost", "timeout")
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
    # T-247: node-local capacity overlay — the server never sees cost or
    # budget (the heartbeat payload builder whitelists fields); these
    # lines reflect THIS node's active profile and its points budget.
    local = None
    try:
        if _nc.ACTIVE_PATH.exists():
            for c in validate_profile(_nc.ACTIVE_PATH):
                if c.get("name") == args.name:
                    local = c
                    break
    except CapabilityValidationError:
        local = None  # broken active profile → degrade, never crash info
    if local is not None:
        print(
            f"Local node:  cost={_nc.capability_cost(local)}"
            f", max_parallel={int(local.get('max_parallel', 1))}"
        )
    else:
        print("Local node:  (not in this node's active profile)")
    budget = _nc.load_active_budget()
    if budget is not None:
        print(f"Node budget: {budget} points")
    else:
        print("Node budget: (unset — points axis off)")
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


# ---------------------------------------------------------------------------
# T-042: workspace capability management (dashboard store wrap)
# ---------------------------------------------------------------------------


# T-042: the create/update form-field slice — argparse flag name → store
# field name. One mapping for both subcommands (the update field set must
# stay identical to the create set) and for the parser registration test.
_FIELD_FLAGS: tuple[tuple[str, str], ...] = (
    ("name", "name"),
    ("type", "type"),
    ("summary", "summary"),
    ("command", "command"),
    ("args", "args"),
    ("script", "script"),
    ("max-parallel", "max_parallel"),
    ("cost", "cost"),
    ("artifacts-glob", "artifacts_glob"),
    ("version", "version"),
    ("method", "method"),
    ("url-template", "url_template"),
    ("body-template", "body_template"),
    ("result-path", "result_path"),
    ("artifact-paths", "artifact_paths"),
    ("health-url", "health_url"),
)


def _field_args(args) -> dict[str, Any]:
    """Collect the capability form fields from a create/update Namespace.

    Only flags the user actually passed land in the dict — the dashboard
    validation decides what is mandatory (identical error messages). Values
    stay raw (strings / lists / None) because ``_parse_capacity`` and the
    version regex validate them client-side of the store.
    """
    fields: dict[str, Any] = {}
    for cli_key, form_key in _FIELD_FLAGS:
        value = getattr(args, cli_key.replace("-", "_"), None)
        if value:
            fields[form_key] = value
    return fields


def _entry_summary(name: str) -> dict[str, Any]:
    """One workspace entry + its publish state for the success report."""
    from nodes.common import bridge as _bridge
    from nodes.common import dashboard as _dash

    entry = _dash.find_provider(name) or {}
    published = _dash.is_published(name, _bridge.load_bridge_config())
    return {
        "name": name,
        "version": entry.get("version"),
        "published": published,
    }


def _print_summary_line(prefix: str, summary: dict[str, Any]) -> None:
    print(
        f"{prefix}{summary['name']} v{summary['version']} "
        f"published={'yes' if summary['published'] else 'no'}"
    )


def _publish_chain(args, name: str, summary: dict[str, Any]) -> dict[str, Any] | None:
    """Apply a ``--publish`` flag after a successful create/update.

    Returns a publish-failure result dict (``publish_error``) when the
    allowlist toggle fails AFTER the entry write (the entry stays — the
    caller reports both, exit 1), or None when nothing failed / no flag.
    """
    from nodes.common import dashboard as _dash

    if not getattr(args, "publish", False):
        return None
    ok, msg = _dash.set_capability_published(name, True)
    if ok:
        summary["published"] = True
        return None
    return {"publish_error": msg}


def _merge_command_and_args(fields: dict[str, Any]) -> str | None:
    """Fold ``--args`` (raw tokens) into the ``--command`` text in place.

    Returns the error message when ``args`` came without a command, else
    None: the merged string is then split by the store's own parse_command,
    so both create and update build the argv list identically.
    """
    extra = fields.pop("args", None)
    command = fields.get("command")
    if command and extra:
        fields["command"] = f"{command} {' '.join(str(a) for a in extra)}"
    elif extra:
        return "--args requires --command"
    return None


def _cmd_capabilities_create(args) -> int:
    """T-042: create one workspace capability (dashboard create_provider wrap)."""
    from nodes.common import dashboard as _dash

    _setup_logging(args.log_level)
    fields = _field_args(args)

    script = fields.pop("script", None)
    if script:
        # T-042: dashboard-form flow — store the script first, then prefill
        # the command with the extension-aware default argv (the --command
        # flag, when given, wins over the prefill). A failed create leaves
        # the stored script behind, exactly as the dashboard form does.
        script_path = Path(script)
        if not script_path.is_file():
            print(f"script not found: {script}", file=sys.stderr)
            return 1
        ok, msg, stored = _dash.save_script(script_path.name, script_path.read_bytes())
        if not ok:
            print(f"script upload FAILED: {msg}", file=sys.stderr)
            return 1
        fields["command"] = fields.get("command") or json.dumps(_dash.script_command_argv(stored))
        summary_extra = {"script_uploaded": f"bridge/scripts/{stored}", "script_name": stored}
    else:
        summary_extra = {}

    merge_error = _merge_command_and_args(fields)
    if merge_error:
        print(merge_error, file=sys.stderr)
        return 1

    ok, msg = _dash.create_provider(fields)
    if not ok:
        print(f"create FAILED: {msg}", file=sys.stderr)
        return 1
    summary = {**_entry_summary(msg), **summary_extra}
    publish_err = _publish_chain(args, msg, summary)
    if args.json:
        print(json.dumps({"ok": True, **summary, **(publish_err or {})}))
        return 1 if publish_err else 0
    _print_summary_line(f"created '{msg}' — ", summary)
    if publish_err:
        print(f"publish FAILED: {publish_err['publish_error']}", file=sys.stderr)
        return 1
    return 0


def _cmd_capabilities_update(args) -> int:
    """T-042: edit one workspace capability in place (dashboard update_provider wrap).

    A field-less invocation with only --publish/--unpublish is the pure
    allowlist toggle (dashboard /caps/publish semantics — ``set_capability_published``
    only, NO version bump); a full field edit keeps the update contract
    (every edit bumps unless an explicit differing version is passed).
    """
    from nodes.common import dashboard as _dash

    _setup_logging(args.log_level)
    name = (args.name or "").strip()
    if not name:
        print("update requires --name", file=sys.stderr)
        return 1
    fields = _field_args(args)
    fields.pop("name", None)  # the routing key rides --name only
    merge_error = _merge_command_and_args(fields)
    if merge_error:
        print(merge_error, file=sys.stderr)
        return 1
    publish_only = not fields
    if publish_only and not (args.publish or args.unpublish):
        print(
            "update requires at least one field or --publish/--unpublish",
            file=sys.stderr,
        )
        return 1
    if publish_only:
        # Pure allowlist toggle — the manual publish set (T-045: the
        # dashboard batch form is the UX, MCP/CLI stay per-call; NO
        # version bump — update_provider is never called).
        ok, msg = _dash.set_capability_published(name, bool(args.publish))
        if not ok:
            print(f"publish FAILED: {msg}", file=sys.stderr)
            return 1
        summary = _entry_summary(name)
        if args.json:
            print(json.dumps({"ok": True, **summary}))
            return 0
        verb = "published" if args.publish else "unpublished"
        _print_summary_line(f"{verb} — ", summary)
        return 0

    ok, msg = _dash.update_provider(name, fields)
    if not ok:
        print(f"update FAILED: {msg}", file=sys.stderr)
        return 1
    summary = _entry_summary(name)
    publish_err = _publish_chain(args, name, summary)
    if args.json:
        print(json.dumps({"ok": True, **summary, "update_message": msg, **(publish_err or {})}))
        return 1 if publish_err else 0
    _print_summary_line(f"{msg} — ", summary)
    if publish_err:
        print(f"publish FAILED: {publish_err['publish_error']}", file=sys.stderr)
        return 1
    return 0


def _cmd_capabilities_delete(args) -> int:
    """T-042: remove one workspace capability (dashboard delete_provider wrap)."""
    from nodes.common import dashboard as _dash

    _setup_logging(args.log_level)
    name = (args.name or "").strip()
    if not name:
        print("delete requires --name", file=sys.stderr)
        return 1
    ok, msg = _dash.delete_provider(name)
    if not ok:
        print(f"delete FAILED: {msg}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"ok": True, "name": msg, "deleted": True}))
        return 0
    print(f"deleted '{msg}'")
    return 0


def _cmd_capabilities_script_upload(args) -> int:
    """T-042: store one capability script (dashboard save_script wrap).

    ``--file <path>`` (or ``--stdin`` with ``--name <filename>``) →
    ``<workspace>/bridge/scripts/<stored>`` with the extension allowlist,
    2 MB size cap and collision suffixing of the dashboard. Output is the
    workspace-relative stored path, the slugified capability-name default
    and the extension-aware default argv template per extension.
    """
    from nodes.common import dashboard as _dash

    _setup_logging(args.log_level)
    name = (args.name or "").strip()
    if args.stdin:
        if not name:
            print("--stdin requires --name (the filename, e.g. tool.bat)", file=sys.stderr)
            return 1
        filename = name
        content = sys.stdin.buffer.read()
    else:
        file_path = Path(args.file or "")
        if not file_path.is_file():
            print(f"script not found: {args.file}", file=sys.stderr)
            return 1
        filename = name or file_path.name
        content = file_path.read_bytes()
    ok, msg, stored = _dash.save_script(filename, content)
    if not ok:
        print(f"script upload FAILED: {msg}", file=sys.stderr)
        return 1
    result = {
        "ok": True,
        "stored": f"bridge/scripts/{stored}",
        "stored_name": stored,
        "name": _dash.slugify_capability_name(stored),
        "command": _dash.script_command_argv(stored),
    }
    if args.json:
        print(json.dumps(result))
        return 0
    print(f"stored '{result['stored']}'")
    print(f"name: {result['name']}")
    print(f"command: {' '.join(result['command'])}")
    return 0