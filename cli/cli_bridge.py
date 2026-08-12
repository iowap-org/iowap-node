"""CLI bridge subcommands — upload/download large files via temp bridge routes (T-153).

The bridge way (T-129) streams big files directly storage<->caller instead
of buffering them through the relay or the artifact store. A caller opens
a channel by submitting a bridge task (``storage.upload_channel`` /
``storage.download_channel``, or the backup variants ``backup.create``
``mode=bridge`` / ``backup.restore``), waits for the stage to complete,
extracts the public ``upload_url`` / ``download_url``, then streams the
file against that URL chunkwise.

Handler signatures are plain ``(client, args) -> int``; the
``node_cli.with_client`` decorator is applied at parser-registration time
in the facade.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from nodes.common.relay_client import RelayClient

# Chunk size for streaming upload/download (64 KB, same as the storage
# node writes with).
_CHUNK = 64 * 1024


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _find_result(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the first completed stage's result from a task fetch."""
    for s in data.get("stages", []):
        if s.get("status") == "completed" and s.get("result"):
            return s["result"]
    return None


def _wait_for_result(client: RelayClient, task_id: str, interval: int) -> dict[str, Any] | None:
    """Poll a task until it reaches a terminal state; return the completed
    stage result (or None on failure/not-found)."""
    while True:
        data = client.get_task(task_id)
        if "error" in data:
            print(f"Task {task_id}: {data['error']}", file=sys.stderr)
            return None
        task = data.get("task", {})
        status = task.get("status", "unknown")
        if status in ("completed", "failed", "timed_out", "cancelled"):
            if status != "completed":
                print(f"Task {task_id} — {status}", file=sys.stderr)
                return None
            return _find_result(data)
        print(f"\r⏳ {status}...", end="", flush=True)
        time.sleep(max(1, interval))


def _bearer(client: RelayClient) -> dict[str, str]:
    """Authorization header for reaching the caller-authenticated bridge route."""
    if not client.token:
        print("no runtime token available", file=sys.stderr)
        raise SystemExit(1)
    return {"Authorization": f"Bearer {client.token}"}


# ---------------------------------------------------------------------------
# bridge upload
# ---------------------------------------------------------------------------


def _cmd_bridge_upload(client: RelayClient, args) -> int:
    file_path = Path(args.file)
    if not file_path.is_file():
        print(f"file not found: {file_path}", file=sys.stderr)
        return 2

    # Choose the bridge task to open the channel: storage or backup.
    if args.backup:
        payload: dict[str, Any] = {
            "source": args.source or file_path.name,
            "type": args.type or "full",
            "mode": "bridge",
        }
        capability = "backup.create"
    else:
        payload = {"channel_id": args.channel} if args.channel else {}
        capability = "storage.upload_channel"

    resp = client.submit_simple_task(capability, payload, name=args.name or "",
                                     priority=args.priority)
    task_id = resp.get("task_id") or resp.get("id")
    if not task_id:
        print(f"no task_id in submit response: {resp}", file=sys.stderr)
        return 1

    result = _wait_for_result(client, str(task_id), args.interval)
    if result is None:
        return 1

    url = result.get("upload_url")
    if not url:
        print(f"no upload_url in result: {result}", file=sys.stderr)
        return 1

    # Stream the file chunkwise to the bridge URL (relay proxies it).
    # T-162: send the original filename so the storage node preserves it
    # (instead of storing under the opaque channel id / data.bin).
    headers = _bearer(client)
    headers["X-Filename"] = file_path.name
    try:
        with file_path.open("rb") as f, httpx.stream(
            "POST", url, headers=headers, content=f,
            timeout=httpx.Timeout(30.0, read=None),
        ) as r:
            r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        print(f"bridge upload failed ({exc.response.status_code}): "
              f"{exc.response.text}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"bridge upload failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"status": "uploaded", "path": str(file_path),
                          "size_bytes": file_path.stat().st_size,
                          "channel_id": result.get("channel_id"),
                          "backup_id": result.get("backup_id")}, default=str))
    else:
        tag = result.get("backup_id") or result.get("channel_id") or ""
        suffix = f" ({tag})" if tag else ""
        print(f"✅ Uploaded {file_path.stat().st_size} bytes via bridge{suffix}")
    return 0


# ---------------------------------------------------------------------------
# bridge download
# ---------------------------------------------------------------------------


def _cmd_bridge_download(client: RelayClient, args) -> int:
    if args.backup:
        capability = "backup.restore"
        payload: dict[str, Any] = {"backup_id": args.backup}
    elif args.channel:
        capability = "storage.download_channel"
        payload = {"channel_id": args.channel}
    else:
        print("bridge download requires --channel <id> or --backup <id>", file=sys.stderr)
        return 2

    resp = client.submit_simple_task(capability, payload, name=args.name or "",
                                     priority=args.priority)
    task_id = resp.get("task_id") or resp.get("id")
    if not task_id:
        print(f"no task_id in submit response: {resp}", file=sys.stderr)
        return 1

    result = _wait_for_result(client, str(task_id), args.interval)
    if result is None:
        return 1

    out_path = args.output
    # NOTE: when out_path is None we do NOT derive a default here — the
    # storage node echoes the original filename in the X-Filename response
    # header (T-162), which is only known after the stream starts. We set
    # the default inside the stream block below, falling back to the
    # channel/backup id.

    # Small backups may come back inline as data_base64 — write that directly.
    if "data_base64" in result and not result.get("download_url"):
        if out_path is None:
            # Inline path: no X-Filename header, but the backup manifest
            # carries the original filename (T-162).
            out_path = Path(
                result.get("filename")
                or result.get("channel_id")
                or result.get("backup_id")
                or "download"
            )
        try:
            out_path.write_bytes(base64.b64decode(result["data_base64"]))
        except Exception as exc:  # noqa: BLE001
            print(f"failed to write inline data: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({"status": "downloaded", "path": str(out_path),
                              "size_bytes": out_path.stat().st_size}, default=str))
        else:
            print(f"✅ Downloaded {out_path.stat().st_size} bytes to {out_path}")
        return 0

    url = result.get("download_url")
    if not url:
        print(f"no download_url in result: {result}", file=sys.stderr)
        return 1

    # Stream the response chunkwise to the output file. For a channel
    # download the storage node echoes the original filename in the
    # X-Filename header (T-162) — use it as the default when no --output
    # was given.
    try:
        with httpx.stream(
            "GET", url, headers=_bearer(client), timeout=httpx.Timeout(30.0, read=None)
        ) as r:
            r.raise_for_status()
            if out_path is None:
                header_name = r.headers.get("x-filename")
                if header_name:
                    out_path = Path(header_name)
                else:
                    out_path = Path(result.get("channel_id") or result.get("backup_id") or "download")
            total = 0
            with out_path.open("wb") as f:
                for chunk in r.iter_bytes(chunk_size=_CHUNK):
                    f.write(chunk)
                    total += len(chunk)
    except httpx.HTTPStatusError as exc:
        print(f"bridge download failed ({exc.response.status_code}): "
              f"{exc.response.text}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"bridge download failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"status": "downloaded", "path": str(out_path),
                          "size_bytes": out_path.stat().st_size}, default=str))
    else:
        print(f"✅ Downloaded {out_path.stat().st_size} bytes to {out_path}")
    return 0
