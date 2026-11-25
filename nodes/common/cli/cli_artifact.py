"""CLI artifact subcommands — download / upload / link (T-117 split, T-033 link).

Handler signatures are plain ``(client, args) -> int``; the
``node_cli.with_client`` decorator is applied at parser-registration time
in the facade.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx

from nodes.common.relay_client import RelayClient


def _cmd_artifact_link(client: RelayClient, args: Any) -> int:
    """Create a short-lived signed download URL for an artifact (T-033).

    pretty print URL + expires_at + max_downloads; exit 0. Relay
    rejections (404 artifact, 422 out-of-range) exit 1 with the status on
    stderr — the caller (CLI, MCP runner) classifies from there.
    """
    try:
        result = client.create_artifact_link(
            args.artifact_id,
            ttl_seconds=args.ttl_seconds,
            max_downloads=args.max_downloads,
        )
    except httpx.HTTPStatusError as exc:
        resp = exc.response
        detail = ""
        try:
            detail = resp.json().get("detail", "")
        except Exception:  # noqa: BLE001 — body may not be JSON
            detail = ""
        if detail:
            print(
                f"artifact link failed: HTTP {resp.status_code}: {detail}",
                file=sys.stderr,
            )
        else:
            print(
                f"artifact link failed: HTTP {resp.status_code}: {resp.text.strip()}",
                file=sys.stderr,
            )
        return 1
    if args.json:
        print(json.dumps(result, default=str))
        return 0
    print(
        "Download link created — valid until {expires_at}, max {max_downloads} download(s):".format(
            expires_at=result.get("expires_at", "?"),
            max_downloads=result.get("max_downloads", "?"),
        )
    )
    print(result.get("url", ""))
    return 0


def _cmd_artifact_download(client: RelayClient, args) -> int:
    target = client.download_artifact(args.artifact_id, output_path=args.output)
    size = target.stat().st_size if target.exists() else 0
    if args.json:
        print(json.dumps({"path": str(target), "size_bytes": size}, default=str))
        return 0
    print(f"Downloaded {size} bytes to {target}")
    return 0


def _cmd_artifact_upload(client: RelayClient, args) -> int:
    file_path = Path(args.file)
    if not file_path.exists():
        print(f"file not found: {file_path}", file=sys.stderr)
        return 2
    result = client.upload_artifact(
        file_path,
        name=args.name,
        task_id=args.task_id,
        stage_id=args.stage_id,
    )
    if args.json:
        print(json.dumps(result, default=str))
        return 0
    print(json.dumps(result, indent=2, default=str))
    return 0