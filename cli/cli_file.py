"""CLI file subcommands — generic transfer-ladder send/get (T-164).

``file send`` and ``file get`` are capability-agnostic wrappers that pick
the right transfer mode (inline / artifact / bridge) based on the server's
ladder config (``max_inline_bytes`` / ``max_artifact_bytes``) and the
capability's declared ``upload_modes``. The three modes are:

* **inline**  — file is base64-encoded and carried in the task payload
  (small files, no storage node needed, RAM-heavy on the handler).
* **artifact** — file is uploaded to the transient relay artifact store,
  the task payload carries an ``artifact_id`` reference (medium files).
* **bridge**  — file is streamed to/from a storage node through a temp
  bridge route; the task payload carries a ``storage_ref`` so the
  receiving node fetches it directly (large files, RAM-bounded).

Handler signatures are plain ``(client, args) -> int``; the
``node_cli.with_client`` decorator is applied at parser-registration time.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any, Optional

import httpx

from nodes.common.cli import cli_bridge
from nodes.common.relay_client import RelayClient

# Reuse the bridge helpers (wait-for-result, bearer header, chunk size).
_wait_for_result = cli_bridge._wait_for_result
_bearer = cli_bridge._bearer
_CHUNK = cli_bridge._CHUNK

# Capabilities that accept a file via the bridge two-step flow: a
# ``storage_ref`` in the task payload points the handler at a channel or
# backup on a storage node. The node-cli opens the channel by submitting
# a bridge-capability task, then puts the ref into the actual task.
_BRIDGE_OPEN_CAPS = {
    "storage.store": "storage.upload_channel",
    "backup.create": "backup.create",
}


def _choose_mode(
    size: int,
    transfer_cfg: dict[str, Any],
    upload_modes: list[str],
    force: Optional[str] = None,
) -> str:
    """Pick the smallest transfer mode the capability supports.

    The server ladder defines the upper bounds (``max_inline_bytes`` /
    ``max_artifact_bytes``); the capability's ``upload_modes`` restricts
    which rungs are available. We pick the lowest rung that fits both.
    ``force`` overrides the choice but must be in ``upload_modes``.
    """
    if force is not None:
        if force not in upload_modes:
            raise SystemExit(
                f"--force {force!r} not supported by capability "
                f"(upload_modes={upload_modes})"
            )
        return force

    max_inline = int(transfer_cfg.get("max_inline_bytes", 0))
    max_artifact = int(transfer_cfg.get("max_artifact_bytes", 0))

    if "inline" in upload_modes and size <= max_inline:
        return "inline"
    if "artifact" in upload_modes and size <= max_artifact:
        return "artifact"
    if "bridge" in upload_modes:
        return "bridge"
    # No supported mode fits → caller surfaces a "file too big" error.
    raise SystemExit(
        f"file too big: {size} bytes, capability supports only {upload_modes} "
        f"(server ladder: inline<={max_inline}, artifact<={max_artifact})"
    )


def _load_capability_modes(client: RelayClient, cap: str) -> dict[str, Any]:
    """Load capability details (upload_modes + input_schema) from the server."""
    try:
        detail = client.get_capability_detail(cap)
    except httpx.HTTPError as exc:
        print(f"failed to load capability details for {cap!r}: {exc}", file=sys.stderr)
        raise SystemExit(1)
    if not detail:
        print(f"capability {cap!r} not found on the relay", file=sys.stderr)
        raise SystemExit(1)
    return detail


# ---------------------------------------------------------------------------
# file send
# ---------------------------------------------------------------------------


def _bridge_open_channel(
    client: RelayClient, cap: str, file_path: Path, args
) -> dict[str, Any]:
    """Open a bridge channel for ``cap`` and stream ``file_path`` into it.

    Returns the completed stage result (carrying ``channel_id`` /
    ``backup_id`` + the upload URL was already consumed). The caller
    builds a ``storage_ref`` from the returned id.
    """
    # Map the file-bearing capability to the bridge-opener capability.
    if cap == "backup.create":
        payload: dict[str, Any] = {
            "source": args.source or file_path.name,
            "type": args.type or "full",
            "mode": "bridge",
            "filename": file_path.name,
        }
        open_cap = "backup.create"
    else:
        # Default: storage.upload_channel (storage.store and others).
        payload = {}
        open_cap = "storage.upload_channel"

    resp = client.submit_simple_task(
        open_cap, payload, name=args.name or "", priority=args.priority
    )
    task_id = resp.get("task_id") or resp.get("id")
    if not task_id:
        print(f"no task_id in submit response: {resp}", file=sys.stderr)
        raise SystemExit(1)

    result = _wait_for_result(client, str(task_id), args.interval)
    if result is None:
        raise SystemExit(1)
    url = result.get("upload_url")
    if not url:
        print(f"no upload_url in bridge result: {result}", file=sys.stderr)
        raise SystemExit(1)

    headers = _bearer(client)
    headers["X-Filename"] = file_path.name
    try:
        with file_path.open("rb") as f, httpx.stream(
            "POST", url, headers=headers, content=f,
            timeout=httpx.Timeout(30.0, read=None),
        ) as r:
            r.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"bridge upload failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
    return result


def _cmd_file_send(client: RelayClient, args) -> int:
    file_path = Path(args.file)
    if not file_path.is_file():
        print(f"file not found: {file_path}", file=sys.stderr)
        return 2
    size = file_path.stat().st_size

    # 1. Server-Treppen-Konfig laden.
    try:
        transfer_cfg = client.get_transfer_config()
    except httpx.HTTPError as exc:
        print(f"failed to load transfer config: {exc}", file=sys.stderr)
        return 1

    # 2. Capability-Details laden (upload_modes).
    cap_detail = _load_capability_modes(client, args.cap)
    upload_modes = list(cap_detail.get("upload_modes") or ["inline", "artifact", "bridge"])

    # 3. Modus wählen.
    mode = _choose_mode(size, transfer_cfg, upload_modes, force=args.force)

    # 4. Datei-Feld aus dem Capability-input_schema ableiten (wie file get):
    #    storage.store → path, backup.create → source, sonst path.
    schema = cap_detail.get("input_schema") or {}
    fields = (schema.get("fields") or {}) if isinstance(schema, dict) else {}
    if "source" in fields:
        file_field = "source"
    else:
        file_field = "path"

    # 5. Orchestrieren.
    if mode == "inline":
        data_b64 = base64.b64encode(file_path.read_bytes()).decode("ascii")
        payload: dict[str, Any] = {
            file_field: args.name or file_path.name,
            "data_base64": data_b64,
        }
        if file_field == "source":
            payload["type"] = args.type or "full"
        resp = client.submit_simple_task(
            args.cap, payload, name=args.name or "", priority=args.priority,
            owner_node_id=args.owner,
        )
        _print_send_result(args, resp, mode, size)
        return 0

    if mode == "artifact":
        art = client.upload_artifact(file_path, name=args.name or file_path.name)
        artifact_id = art.get("artifact_id")
        if not artifact_id:
            print(f"artifact upload returned no artifact_id: {art}", file=sys.stderr)
            return 1
        payload = {
            file_field: args.name or file_path.name,
            "artifact_id": artifact_id,
        }
        if file_field == "source":
            payload["type"] = args.type or "full"
        resp = client.submit_simple_task(
            args.cap, payload, name=args.name or "", priority=args.priority,
            owner_node_id=args.owner,
        )
        _print_send_result(args, resp, mode, size, artifact_id=artifact_id)
        return 0

    # mode == "bridge": Zwei-Schritt-Flow.
    result = _bridge_open_channel(client, args.cap, file_path, args)
    ref_id = result.get("backup_id") or result.get("channel_id")
    ref_type = "backup" if result.get("backup_id") else "channel"
    if not ref_id:
        print(f"bridge channel returned no id: {result}", file=sys.stderr)
        return 1
    storage_ref = {
        "type": ref_type,
        "id": ref_id,
        "filename": file_path.name,
    }
    payload = {
        "path": args.name or file_path.name,
        "storage_ref": storage_ref,
    }
    resp = client.submit_simple_task(
        args.cap, payload, name=args.name or "", priority=args.priority,
        owner_node_id=args.owner,
    )
    _print_send_result(args, resp, mode, size, ref=storage_ref)
    return 0


def _print_send_result(
    args, resp: dict[str, Any], mode: str, size: int, *,
    artifact_id: Optional[str] = None, ref: Optional[dict] = None,
) -> None:
    if args.json:
        out = {"status": "sent", "mode": mode, "size_bytes": size, "submit": resp}
        if artifact_id:
            out["artifact_id"] = artifact_id
        if ref:
            out["storage_ref"] = ref
        print(json.dumps(out, default=str))
        return
    tag = f" (artifact={artifact_id})" if artifact_id else ""
    tag = tag or (f" (bridge={ref['type']}:{ref['id']})" if ref else "")
    print(f"✅ Sent {size} bytes via {mode}{tag}")
    print(json.dumps(resp, default=str))


# ---------------------------------------------------------------------------
# file get
# ---------------------------------------------------------------------------


def _cmd_file_get(client: RelayClient, args) -> int:
    # Capability-Details laden → upload_modes + input_schema bestimmen die
    # Feld-Interpretation von <ref>.
    cap_detail = _load_capability_modes(client, args.cap)
    upload_modes = list(cap_detail.get("upload_modes") or ["inline", "artifact", "bridge"])
    schema = cap_detail.get("input_schema") or {}
    fields = (schema.get("fields") or {}) if isinstance(schema, dict) else {}

    # Welches Payload-Feld trägt <ref>? Aus input_schema ableiten.
    if "path" in fields:
        ref_field = "path"
    elif "backup_id" in fields:
        ref_field = "backup_id"
    elif "channel_id" in fields:
        ref_field = "channel_id"
    else:
        print(
            f"capability {args.cap!r} has no file-reference field "
            f"(path/backup_id/channel_id) in its input_schema",
            file=sys.stderr,
        )
        return 1

    # Bridge-Download bevorzugt, wenn die Capability bridge anbietet —
    # sonst inline (Task abschicken, Result enthält data_base64).
    if "bridge" in upload_modes and ref_field in ("backup_id", "channel_id"):
        return _file_get_bridge(client, args, ref_field)
    return _file_get_inline(client, args, ref_field)


def _file_get_inline(client: RelayClient, args, ref_field: str) -> int:
    payload = {ref_field: args.ref}
    resp = client.submit_simple_task(
        args.cap, payload, name=args.name or "", priority=args.priority,
        owner_node_id=args.owner,
    )
    task_id = resp.get("task_id") or resp.get("id")
    if not task_id:
        print(f"no task_id in submit response: {resp}", file=sys.stderr)
        return 1
    result = _wait_for_result(client, str(task_id), args.interval)
    if result is None:
        return 1
    data_b64 = result.get("data_base64")
    if not data_b64:
        # Maybe a bridge download_url slipped through — surface it.
        if result.get("download_url"):
            print(
                "capability returned a download_url but bridge is not in "
                "upload_modes; use `bridge download` instead",
                file=sys.stderr,
            )
            return 1
        print(f"no data_base64 in result: {result}", file=sys.stderr)
        return 1
    out_path = args.output or Path(
        result.get("filename") or result.get("channel_id")
        or result.get("backup_id") or "download"
    )
    try:
        out_path.write_bytes(base64.b64decode(data_b64))
    except Exception as exc:  # noqa: BLE001
        print(f"failed to write inline data: {exc}", file=sys.stderr)
        return 1
    _print_get_result(args, out_path, "inline")
    return 0


def _file_get_bridge(client: RelayClient, args, ref_field: str) -> int:
    """Open a bridge download channel and stream the file to disk."""
    if ref_field == "backup_id":
        payload = {"backup_id": args.ref}
    else:
        payload = {"channel_id": args.ref}
    resp = client.submit_simple_task(
        args.cap, payload, name=args.name or "", priority=args.priority,
        owner_node_id=args.owner,
    )
    task_id = resp.get("task_id") or resp.get("id")
    if not task_id:
        print(f"no task_id in submit response: {resp}", file=sys.stderr)
        return 1
    result = _wait_for_result(client, str(task_id), args.interval)
    if result is None:
        return 1

    out_path = args.output
    # Inline fallback: small files may come back as data_base64.
    if "data_base64" in result and not result.get("download_url"):
        if out_path is None:
            out_path = Path(
                result.get("filename") or result.get("channel_id")
                or result.get("backup_id") or "download"
            )
        try:
            out_path.write_bytes(base64.b64decode(result["data_base64"]))
        except Exception as exc:  # noqa: BLE001
            print(f"failed to write inline data: {exc}", file=sys.stderr)
            return 1
        _print_get_result(args, out_path, "inline")
        return 0

    url = result.get("download_url")
    if not url:
        print(f"no download_url in result: {result}", file=sys.stderr)
        return 1
    try:
        with httpx.stream(
            "GET", url, headers=_bearer(client),
            timeout=httpx.Timeout(30.0, read=None),
        ) as r:
            r.raise_for_status()
            if out_path is None:
                header_name = r.headers.get("x-filename")
                out_path = Path(
                    result.get("filename") or header_name
                    or result.get("channel_id")
                    or result.get("backup_id") or "download"
                )
            total = 0
            with out_path.open("wb") as f:
                for chunk in r.iter_bytes(chunk_size=_CHUNK):
                    f.write(chunk)
                    total += len(chunk)
    except httpx.HTTPError as exc:
        print(f"bridge download failed: {exc}", file=sys.stderr)
        return 1
    _print_get_result(args, out_path, "bridge")
    return 0


def _print_get_result(args, out_path: Path, mode: str) -> None:
    size = out_path.stat().st_size if out_path.exists() else 0
    if args.json:
        print(json.dumps(
            {"status": "downloaded", "mode": mode, "path": str(out_path),
             "size_bytes": size}, default=str,
        ))
        return
    print(f"✅ Downloaded {size} bytes to {out_path} via {mode}")