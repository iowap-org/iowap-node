"""T-179: handler primitives — ``node-cli hp put/get``.

Scripts talk exclusively to the local node-cli (subprocess contract,
board decision ①): token, ladder negotiation and the transfer formats
stay inside the CLI process. Scripts see only a path in / a path out.

FROZEN CONTRACT (Phase 1, T-179)
================================
Every public signature in this module is immutable across Phase 2
(scaffold: test stubs + parser boilerplate) and Phase 3 (integrate:
implementation). Specifically FROZEN:

* parameter names + order (the Phase 2/3 tests call them positionally
  and by keyword — both must remain stable),
* return types (``int`` exit codes; ``0`` ok / ``1`` error / ``2`` usage),
* stdout contracts (exactly ONE compact JSON line each, see cmd_put/cmd_get),
* the error message strings of ``decide_mode`` (Task 2b needs them
  byte-identical to ``cli_file._choose_mode``).

House convention (like cli_file/cli_task, T-117 split): CLI handlers are
``(client, args) -> int`` and get the ``with_client(...)`` decorator at
parser registration time in ``node_cli.py``. This module does NOT import
``node_cli`` (no circular import; the RelayClient monkeypatch stays
functional).

Phase 1 delivers only signatures + contract documentation; the bodies
are implemented in Phase 3 (Phase 2 produces ``NotImplementedError``
test stubs that are already prepared here).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

import httpx

from nodes.common import file_serve
from nodes.common.cli.cli_file import _load_capability_modes
from nodes.common.envelope import (  # noqa: F401
    ENVELOPE_KEY,
    EnvelopeError,
    dump,
    is_envelope,
    make_envelope,
    parse_envelope,
)
from nodes.common.relay_client import RelayClient

# Order = preference; the first allowed rung whose threshold fits.
# FROZEN: the order is part of the contract (inline < artifact < bridge).
_LADDER = ("inline", "artifact", "bridge")


def decide_mode(
    size_bytes: int,
    upload_modes: list[str],
    thresholds: dict[str, int] | None = None,
    force: str | None = None,
) -> str:
    """Pick the ladder rung for a file — the shared decision point.

    FROZEN SIGNATURE (Phase 1) — Task 2b routes ``cli_file._choose_mode``
    through this function; that is why the error messages are FROZEN
    too (see below).

    Semantics
    ---------
    * Ladder preference: the first rung of ``_LADDER`` that (a) is
      allowed in ``upload_modes`` and (b) whose threshold carries the
      size.
    * ``thresholds``: keys ``max_inline_bytes`` / ``max_artifact_bytes``
      (from the server, ``RelayClient.get_transfer_config()``).
      - Missing key or ``None`` → the rung counts as **unlimited**
        (the safety net is the server payload limit, not a silent
        failure).
      - Explicit ``0`` → rung **forbidden** (cli_file semantics when
        the server response is missing).
      - ``thresholds=None`` → all rungs unlimited → pure preference
        choice according to ``upload_modes``.
    * ``bridge`` has no threshold key (the server defines none) →
      unlimited; chosen as soon as inline/artifact do not fit.
    * ``force``: overrides the choice but must be in ``upload_modes``.
      Checked BEFORE the size logic (like cli_file).

    Errors (FROZEN, byte-identical to ``cli_file._choose_mode``)
    ------------------------------------------------------------
    Raise ``ValueError`` (library level; the CLI adapter converts it
    to SystemExit/exit code):

    * force not allowed::

        --force {force!r} not supported by capability (upload_modes={upload_modes})

    * no rung fits::

        file too big: {size_bytes} bytes, capability supports only {upload_modes} (server ladder: inline<={max_inline}, artifact<={max_artifact})

      with ``max_inline = (thresholds or {}).get("max_inline_bytes", 0)`` etc.

    Task-2b mapping (FROZEN, behavior-identical) in ``cli_file.py``::

        thresholds = {
            "max_inline_bytes": int(transfer_cfg.get("max_inline_bytes", 0)),
            "max_artifact_bytes": int(transfer_cfg.get("max_artifact_bytes", 0)),
        }
        try:
            return decide_mode(size, upload_modes, thresholds, force=force)
        except ValueError as exc:
            raise SystemExit(str(exc))

    (cli_file materializes the 0-defaults explicitly — this keeps the
    "missing key = unlimited" behavior of decide_mode without any
    effect on the cli_file path.)
    """
    if force is not None:
        if force not in upload_modes:
            raise ValueError(
                f"--force {force!r} not supported by capability "
                f"(upload_modes={upload_modes})"
            )
        return force

    th = thresholds or {}
    max_inline = th.get("max_inline_bytes")
    max_artifact = th.get("max_artifact_bytes")

    if "inline" in upload_modes and (max_inline is None or size_bytes <= max_inline):
        return "inline"
    if "artifact" in upload_modes and (
        max_artifact is None or size_bytes <= max_artifact
    ):
        return "artifact"
    if "bridge" in upload_modes:
        return "bridge"
    raise ValueError(
        f"file too big: {size_bytes} bytes, capability supports only {upload_modes} "
        f"(server ladder: inline<={(th or {}).get('max_inline_bytes', 0)}, "
        f"artifact<={(th or {}).get('max_artifact_bytes', 0)})"
    )


def _serve_count_from_env() -> int:
    """``--serve-count`` equivalent from ``IOWAP_SERVE_COUNT`` (1–10, F6).

    The FROZEN ``cmd_put`` signature (F9) has no ``serve_count`` param —
    the F6 multi-download runs through the env (0/invalid/out-of-range →
    1, no WARNING spam for the normal case). A value ``< 1`` would fail
    fast in ``stage_file`` anyway — here we normalize cleanly to 1.
    """
    raw = os.environ.get("IOWAP_SERVE_COUNT")
    if not raw:
        return 1
    try:
        value = int(raw)
    except ValueError:
        return 1
    return value if 1 <= value <= 10 else 1


def cmd_put(
    client: RelayClient,
    *,
    cap: str,
    path: Path,
    name: str | None = None,
    out: TextIO | None = None,
) -> int:
    """``node-cli hp put <path> --cap <cap>`` → envelope JSON on stdout.

    FROZEN SIGNATURE (Phase 1).

    Contract
    --------
    * Loads the capability details via ``_load_capability_modes(client,
      cap)`` (``upload_modes``; its ``SystemExit`` on HTTP/lookup errors
      propagates — ``main()`` turns it into the exit code).
    * Thresholds via ``client.get_transfer_config()``; extract defensively
      (take only real ints, anything else = omit the key → the rung is
      unlimited). HTTP errors here → ``return 1`` with a clear message.
    * ``mode = decide_mode(size, upload_modes, thresholds)`` with
      ``size = path.stat().st_size`` (do NOT read the entire file first —
      artifact/bridge are meant to handle big files without a RAM peak;
      inline reads the bytes only after the decision). ``ValueError``
      from decide_mode → stderr + ``return 1``.
    * inline  → ``make_envelope(src="inline", filename=name or path.name,
      data=<bytes>)``.
    * artifact → ``client.upload_artifact(path, name=name or path.name)``;
      a missing ``artifact_id`` in the response → stderr + ``return 1``;
      otherwise ``make_envelope(src="artifact", ..., artifact_id=aid)``.
    * bridge → ``raise NotImplementedError("bridge-put follows in Task
      3")`` (deliberate MVP boundary — the envelope can carry bridge, the
      channel-open flow cannot; see plan T-179).
    * stdout: EXACTLY ONE line — compact JSON of the envelope
      (``envelope.dump``) + ``"\n"``. ``out=None`` → resolve
      ``sys.stdout`` at CALL time (not import time!) so that pytest-capsys
      works. No other stdout output; errors → stderr.
    * Exit codes: ``0`` ok, ``1`` transfer/protocol error,
      ``2`` usage (file missing — checked in dispatch_put).

    ``name`` overrides the filename inside the envelope (default:
    path.name).
    """
    if not path.is_file():
        print(f"file not found: {path}", file=sys.stderr)
        return 2

    # 1. Capability details (upload_modes); SystemExit on lookup errors
    #    propagates — main() turns it into the exit code.
    cap_detail = _load_capability_modes(client, cap)
    upload_modes = list(
        cap_detail.get("upload_modes") or ["inline", "artifact", "bridge"]
    )

    # 2. Load the server ladder config; defensively: take only real ints,
    #    anything else = omit the key → rung unlimited (F2.5 semantics).
    try:
        transfer_cfg = client.get_transfer_config()
    except httpx.HTTPError as exc:
        print(f"failed to load transfer config: {exc}", file=sys.stderr)
        return 1
    thresholds: dict[str, int] = {}
    if isinstance(transfer_cfg, dict):
        for key in ("max_inline_bytes", "max_artifact_bytes"):
            value = transfer_cfg.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                thresholds[key] = value

    # 3. Size via stat (F2.9) — read the bytes only after the decision.
    size = path.stat().st_size
    try:
        mode = decide_mode(size, upload_modes, thresholds)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # 4. Build the envelope per mode.
    if mode == "inline":
        envelope = make_envelope(
            src="inline", filename=name or path.name, data=path.read_bytes()
        )
    elif mode == "artifact":
        try:
            resp = client.upload_artifact(path, name=name or path.name)
        except httpx.HTTPError as exc:
            print(f"artifact upload failed: {exc}", file=sys.stderr)
            return 1
        artifact_id = resp.get("artifact_id") if isinstance(resp, dict) else None
        if not artifact_id:
            print(
                "artifact upload response without artifact_id", file=sys.stderr
            )
            return 1
        envelope = make_envelope(
            src="artifact",
            filename=name or path.name,
            artifact_id=str(artifact_id),
            size_bytes=size,
        )
    else:  # bridge — ephemeral serve handoff (T-166, F1/F5/F6/F8)
        manifest = file_serve.read_manifest()
        if manifest is None or not file_serve.probe_serve(
            manifest["port"], host=manifest.get("host")
        ):
            print(
                "hp put: ephemeral serve not reachable (is the node daemon "
                "running?) — use artifact fallback",
                file=sys.stderr,
            )
            return 1
        transfer_id, _staged, sha_hex = file_serve.stage_file(
            path, max_downloads=_serve_count_from_env()
        )
        route_path = f"/download/{transfer_id}"
        # D8: advertise host from the manifest (the daemon writes the
        # serve_host() address) — the proxy dials the address VERBATIM,
        # so it must be resolvable from the relay machine, not only from
        # here (live: LXC 903 ↔ Hermes host).
        upstream = (
            f"http://{manifest.get('host', file_serve.SERVE_HOST)}:{manifest['port']}"
            f"/transfer/{transfer_id}"
        )
        node_id = str((client.meta or {}).get("node_id") or "unknown")
        expires = (
            datetime.now(UTC) + timedelta(seconds=3600)
        ).isoformat()
        try:
            client.register_temp_route(
                route_path,
                "POST",
                upstream,
                ttl_seconds=3600,
                channel_id=transfer_id,
                description="iowap ephemeral file serve (T-166)",
            )
        except httpx.HTTPError as exc:
            # No stale left-over without a route (F6): staged file + manifest gone.
            file_serve.discard_staged(transfer_id)
            print(f"hp put: route registration failed: {exc}", file=sys.stderr)
            return 1
        envelope = make_envelope(
            src="bridge",
            filename=name or path.name,
            storage_ref=file_serve.build_storage_ref(node_id, route_path, expires),
            sha256=sha_hex,
            size_bytes=size,  # read before stage_file — move semantics (F5)
        )

    # 5. stdout: EXACTLY ONE compact JSON line; out=None → sys.stdout at
    #    CALL time (capsys-compatible).
    stream = out if out is not None else sys.stdout
    stream.write(dump(envelope) + "\n")
    return 0


def cmd_get(
    client: RelayClient,
    *,
    envelope: dict[str, Any],
    out_dir: Path,
    output: Path | None = None,
    out: TextIO | None = None,
) -> int:
    """``node-cli hp get`` → resolve the envelope, store the file locally.

    FROZEN SIGNATURE (Phase 1).

    Contract
    --------
    * ``parse_envelope(envelope)`` → ``(src, ref)``; ``EnvelopeError`` →
      ``"hp get: {exc}"`` on stderr + ``return 1``.
    * Filename: ``envelope["__iowap_ref__"].get("filename") or
      "download"`` (the filename from the envelope is NOT trustworthy
      as a path — do not accept ``..``/absolute paths, sanitize).
    * ``out_dir`` is created when needed (``mkdir(parents=True,
      exist_ok=True)``) — also for the ``output`` override, so the
      default-path branch stays consistent.
    * inline  → ``target = output or out_dir / filename``;
      ``target.write_bytes(ref)``.
    * artifact → ``target = client.download_artifact(ref, output or
      out_dir / filename)`` (RelayClient takes care of the streaming).
    * bridge  → stderr ``"hp get: bridge resolution not supported by
      this node-cli version"`` + ``return 1`` (MVP boundary; the exact
      message string is FROZEN — plan T-179 Task 3).
    * sha256 verification when the envelope carries one (inline always
      does): mismatch → stderr
      ``"hp get: sha256 mismatch ({digest} != {expected})"`` + ``return 1``.
    * stdout: EXACTLY ONE line — compact JSON
      ``{"path": str(target), "size_bytes": target.stat().st_size,
      "src": <mode>}`` + ``"\n"`` (keys FROZEN). ``out=None`` →
      ``sys.stdout`` at CALL time (capsys-compatible).
    * Exit codes: ``0`` ok, ``1`` resolution/verification error,
      ``2`` usage.
    """
    try:
        src, ref = parse_envelope(envelope)
    except EnvelopeError as exc:
        print(f"hp get: {exc}", file=sys.stderr)
        return 1

    # The filename from the envelope is NOT trustworthy (no
    # ../absolute paths) — sanitize.
    raw_name = (envelope.get(ENVELOPE_KEY) or {}).get("filename")
    if not isinstance(raw_name, str) or not raw_name:
        raw_name = "download"
    safe_name = Path(raw_name).name
    if safe_name in ("", ".", ".."):
        safe_name = "download"

    out_dir.mkdir(parents=True, exist_ok=True)

    if src == "bridge":
        serve_ref = ref if isinstance(ref, dict) else {}
        if (
            serve_ref.get("type") != "node_serve"
            or not serve_ref.get("node_id")
            or not serve_ref.get("path")
        ):
            print(
                "hp get: bridge storage_ref missing node_serve fields "
                "(node_id/path)",
                file=sys.stderr,
            )
            return 1
        url = file_serve.proxy_url(
            client.base_url, str(serve_ref["node_id"]), str(serve_ref["path"])
        )
        target = output or out_dir / safe_name
        headers: dict[str, str] = {}
        if client.token:
            headers["Authorization"] = f"Bearer {client.token}"
        try:
            # POST with an empty body — the proxy matches the registered
            # method exactly (route_registry.py:194-200); GET on a POST
            # route would be 404. The serve handler ignores the body.
            with httpx.stream(
                "POST",
                url,
                headers=headers,
                content=b"",
                timeout=httpx.Timeout(30.0, read=None),
            ) as r:
                r.raise_for_status()
                with target.open("wb") as f:
                    for chunk in r.iter_bytes(chunk_size=64 * 1024):
                        f.write(chunk)
        except httpx.HTTPError as exc:
            print(f"hp get: bridge pull failed: {exc}", file=sys.stderr)
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            return 1
        # F7: the receiver NEVER unregisters (it is not the owner) —
        # ephemerality/TTL is the sender daemon's job.

    elif src == "inline":
        target = output or out_dir / safe_name
        target.write_bytes(ref)
    else:  # artifact — RelayClient takes care of the streaming.
        try:
            target = client.download_artifact(ref, output or out_dir / safe_name)
        except httpx.HTTPError as exc:
            print(f"hp get: artifact download failed: {exc}", file=sys.stderr)
            return 1

    # sha256 verification when the envelope carries one (inline always does).
    expected = (envelope.get(ENVELOPE_KEY) or {}).get("sha256")
    if expected:
        # T-192 (F5): hash chunkwise (64 KiB) — no RAM peak on the
        # big bridge/artifact path, which the rest of the flow streams
        # deliberately.
        digest = file_serve._sha256_file(target)
        if digest != expected:
            print(
                f"hp get: sha256 mismatch ({digest} != {expected})",
                file=sys.stderr,
            )
            try:
                target.unlink()
            except OSError:
                pass
            return 1

    stream = out if out is not None else sys.stdout
    stream.write(
        json.dumps(
            {
                "path": str(target),
                "size_bytes": target.stat().st_size,
                "src": src,
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    return 0


def default_out_dir() -> Path:
    """Default drop directory for ``hp get``: ``~/.relay/tmp/<task_id>/``.

    FROZEN SIGNATURE (Phase 1). Task ID from ``RELAY_TASK_ID`` (assigned
    by the handler_runner to every handler, see
    ``handler_runner.HANDLER_ENV_KEYS``); without handler context →
    ``adhoc``. Mirrors ``node_config.BASE_DIR`` (``~/.relay``) — the same
    root as profile/meta, no new root folder.
    """
    task_id = __import__("os").environ.get("RELAY_TASK_ID") or "adhoc"
    return Path.home() / ".relay" / "tmp" / task_id


def dispatch_put(client: RelayClient, args: argparse.Namespace) -> int:
    """Argparse adapter: ``node-cli hp put <path> --cap <cap> [--name N]``.

    FROZEN SIGNATURE (Phase 1). House convention ``(client, args) -> int``;
    registration in ``node_cli.py`` (Phase 2)::

        p_hp_put.set_defaults(func=with_client(cli_hp.dispatch_put))

    Namespace attributes (FROZEN, Phase 2 registers exactly these):

    * ``args.path``   (str, positional)   — file, existence-checked here:
      missing → stderr + ``return 2`` (like cli_file._cmd_file_send).
    * ``args.cap``    (str, --cap, required)
    * ``args.name``   (str | None, --name, default None)

    Delegates to ``cmd_put(client, cap=args.cap, path=Path(args.path),
    name=args.name)``.
    """
    path = Path(args.path)
    if not path.is_file():
        print(f"file not found: {path}", file=sys.stderr)
        return 2
    return cmd_put(client, cap=args.cap, path=path, name=args.name)


def dispatch_get(client: RelayClient, args: argparse.Namespace) -> int:
    """Argparse adapter: ``node-cli hp get [--file F] [--output P]``.

    FROZEN SIGNATURE (Phase 1). House convention ``(client, args) -> int``;
    registration in ``node_cli.py`` (Phase 2)::

        p_hp_get.set_defaults(func=with_client(cli_hp.dispatch_get))

    Namespace attributes (FROZEN, Phase 2 registers exactly these):

    * ``args.file``    (str | None, --file, default None) — envelope JSON
      as a file; without --file stdin is read (``sys.stdin``, call time).
    * ``args.output``  (Path | None, --output/-o, default None)
    * ``args.out_dir`` (Path | None, --out-dir, default None) — override,
      otherwise ``default_out_dir()``.

    The input must be a JSON object with ``__iowap_ref__``; invalid
    JSON → stderr + ``return 2`` (usage). Delegates to ``cmd_get``.
    """
    if args.file:
        try:
            raw = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"hp get: cannot read {args.file}: {exc}", file=sys.stderr)
            return 2
    else:
        raw = sys.stdin.read()

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"hp get: invalid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(envelope, dict) or ENVELOPE_KEY not in envelope:
        print(
            f"hp get: no {ENVELOPE_KEY} key — not an envelope",
            file=sys.stderr,
        )
        return 2

    out_dir = args.out_dir if args.out_dir is not None else default_out_dir()
    return cmd_get(
        client, envelope=envelope, out_dir=out_dir, output=args.output
    )
