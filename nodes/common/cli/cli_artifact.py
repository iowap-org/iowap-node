"""CLI artifact subcommands — download / upload (T-117 split).

Handler signatures are plain ``(client, args) -> int``; the
``node_cli.with_client`` decorator is applied at parser-registration time
in the facade.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from nodes.common.relay_client import RelayClient


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