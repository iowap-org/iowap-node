"""CLI task subcommands — submit / result / note / wait (T-117 split).

Handler signatures are plain ``(client, args) -> int``; the
``node_cli.with_client`` decorator is applied at parser-registration time
in the facade, so this module never imports ``node_cli`` (avoids a
circular import and keeps the ``cli.RelayClient`` monkeypatch working).
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

import httpx

from nodes.common.relay_client import RelayClient


def _parse_stage_arg(stage: str) -> tuple[str, dict[str, Any]]:
    """Parse ``<cap>:<json_payload>`` into (capability, payload)."""
    if ":" not in stage:
        raise SystemExit(f"invalid --stage value {stage!r}; expected <capability>:<json-payload>")
    cap, _, payload_str = stage.partition(":")
    cap = cap.strip()
    if not cap:
        raise SystemExit(f"invalid --stage value {stage!r}; empty capability")
    try:
        payload = json.loads(payload_str) if payload_str.strip() else {}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid --stage payload JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("--stage payload must be a JSON object")
    return cap, payload


def _cmd_task_submit(client: RelayClient, args) -> int:
    cap, payload = _parse_stage_arg(args.stage)
    resp = client.submit_simple_task(
        cap,
        payload,
        name=args.name or "",
        priority=args.priority,
        owner_node_id=args.owner,
    )
    if args.json:
        print(json.dumps(resp, default=str))
        return 0
    print(json.dumps(resp, indent=2, default=str))
    return 0


def _cmd_task_result(client: RelayClient, args) -> int:
    data = client.get_task(args.task_id)
    if "error" in data:
        print(f"Task {args.task_id}: {data['error']}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(data, default=str))
        return 0
    _print_task_result(data)
    return 0


def _cmd_task_note(client: RelayClient, args) -> int:
    """node-cli task note <task_id> <message> — append a note to a task."""
    try:
        data = client.add_task_note(args.task_id, args.message)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            print(f"Task {args.task_id} not found", file=sys.stderr)
            return 1
        print(f"Error: {exc.response.status_code} {exc.response.text}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(data, default=str))
        return 0
    print(f"✅ Note added to task {data.get('task_id', args.task_id)}")
    print(f"   {data.get('message', '')} ({data.get('created_at', '')})")
    return 0


def _cmd_task_wait(client: RelayClient, args) -> int:
    task_id = args.task_id
    interval = max(1, args.interval)

    last_note_count = 0
    while True:
        data = client.get_task(task_id)
        if "error" in data:
            print(f"Task {task_id}: {data['error']}", file=sys.stderr)
            return 1

        task = data.get("task", {})
        status = task.get("status", "unknown")

        # T-052: surface any new notes that arrived since the last poll.
        notes = data.get("notes", [])
        if len(notes) > last_note_count:
            for n in notes[last_note_count:]:
                print(f"\n💬 [{n.get('node_id', '?')}] {n.get('message', '')} ({n.get('created_at', '?')})")
            last_note_count = len(notes)

        if status in ("completed", "failed", "timed_out"):
            if args.json:
                print(json.dumps(data, default=str))
                return 0 if status == "completed" else 1
            print(f"\n✅ Task {task_id} — {status}\n")
            _print_task_result(data)
            return 0 if status == "completed" else 1

        # Show spinner / progress
        stages = data.get("stages", [])
        done = sum(1 for s in stages if s.get("status") == "completed")
        total = len(stages)
        print(f"\r⏳ {status} — {done}/{total} stages completed...", end="", flush=True)
        time.sleep(interval)


def _print_task_result(data: dict[str, Any]) -> None:
    task = data.get("task", {})
    stages = data.get("stages", [])
    artifacts = data.get("artifacts", [])
    notes = data.get("notes", [])

    print(f"  Task:    {task.get('task_name', '?')} ({task.get('task_id', '?')})")
    print(f"  Status:  {task.get('status', '?')}")
    print(f"  Created: {task.get('created_at', '?')}")
    print(f"  Updated: {task.get('updated_at', '?')}")
    print()

    if stages:
        print("  Stages:")
        for s in stages:
            status_icon = "✅" if s.get("status") == "completed" else "⏳" if s.get("status") == "claimed" else "⬜"
            result_str = ""
            if s.get("result"):
                result_str = f"  result={json.dumps(s['result'])}"
            print(f"    {status_icon} {s.get('stage_name','?')} [{s.get('capability','?')}] — {s.get('status','?')}{result_str}")
            # T-053: show resolved capability metadata when present.
            cd = s.get("capability_details")
            if cd:
                if cd.get("description"):
                    print(f"       description: {cd['description']}")
                if cd.get("type"):
                    print(f"       type:        {cd['type']}")
                if cd.get("input_schema"):
                    print(f"       input_schema: {json.dumps(cd['input_schema'])}")
            # Surface handler diagnostics (exit code, stdout size,
            # stderr snippet) so callers can debug empty responses
            # without downloading artifacts. Populated by
            # handler_runner.run_handler() on success (exit 0).
            handler_info = (s.get("result") or {}).get("_handler")
            if handler_info:
                stderr_snippet = (handler_info.get("stderr") or "")[:200]
                print(
                    f"      [handler] exit={handler_info.get('exit_code')} "
                    f"stdout={handler_info.get('stdout_length','?')}B "
                    f"stderr={stderr_snippet!r}"
                )
        print()

    if artifacts:
        print("  Artifacts:")
        for a in artifacts:
            size = a.get("size_bytes", 0)
            size_str = f"{size/1024:.0f} KB" if size < 1024*1024 else f"{size/1024/1024:.1f} MB"
            print(f"    📄 {a.get('name','?')} ({a.get('artifact_id','?')}) — {size_str}")
    else:
        print("  (no artifacts linked to this task)")

    if notes:
        print()
        print(f"  Notes ({len(notes)}):")
        for n in notes:
            print(f"    💬 [{n.get('node_id', '?')}] {n.get('message', '')} ({n.get('created_at', '?')})")