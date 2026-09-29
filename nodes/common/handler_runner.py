#!/usr/bin/env python3
"""Handler runner: executes a capability handler as a subprocess.

A *handler* is an external executable (script, binary, or shell command)
that performs the actual work for a claimed stage. The runner sets up a
well-defined environment, feeds the stage ``payload`` as JSON on stdin,
captures stdout/stderr, enforces a timeout, and returns a result dict
ready to be POSTed to the ``/complete`` endpoint.

Contract (see NODE_CLI_SPEC.md §4 and docs/node/capabilities.md):

* Environment variables set before execution::

      RELAY_STAGE_ID      Stage ID from claim
      RELAY_TASK_ID       Task ID from claim
      RELAY_CAPABILITY    Capability name
      RELAY_NODE_ID       Assigned node ID
      RELAY_BASE_URL      Relay server URL
      RELAY_TOKEN_FILE    Path to runtime token file

* Stdin:  Request Envelope as a JSON string (T-005b Envelope-Contract):
  ``{"task_id": ..., "capability": ..., "input": {...payload...}}``.
  Since T-005d (strict phase) the envelope is sent alone; a legacy
  top-level mirror of the payload keys can be re-enabled per capability
  via ``config.envelope_request_mirror: true`` (rollback lever).
* Stdout: on exit 0 MUST be valid JSON — normalized to the Response
  Envelope (T-005b): a conforming ``{"status": "completed", "result":
  {...}, "error": null}`` passes through verbatim, ``status: "error"``
  fails the stage fail-fast via its ``error`` message, and bare result
  dicts are wrapped into the envelope (without an ``error`` key — the
  daemon counts failures via ``"error" in result`` key presence).
* Stderr: captured and included in the error result on non-zero exit.
  On exit 0 it is attached to the result under ``_handler.stderr`` for
  debugging but is never interpreted as the result.
* Exit codes:
      0          -> stdout parsed as result dict
      non-zero   -> {"error": "handler exited with code N", "stderr": ...}
  Handlers MUST exit non-zero whenever they could not produce a valid
  result. Exit 0 with non-JSON or an ``error``-keyed payload completes
  the stage silently and skips the scheduler's retry budget, losing the
  failed work.
* Timeout: kills the whole handler process tree (T-188) and returns
  ``{"error": "handler timeout after Ns"}``.
* Output cap (T-191): stdout/stderr are buffered up to
  ``MAX_HANDLER_STDOUT_BYTES``/``MAX_HANDLER_STDERR_BYTES``; exceeding the
  cap fails the stage instead of letting a runaway handler OOM the daemon.

Retry behaviour (T-060): non-zero exit, timeout, invalid-JSON-on-exit-0
and complete-endpoint failures all increment the daemon-side per-task
failure counter (and the server-side ``retry_count`` via the released
claim). Once ``max_retries`` (default 2 → 3 attempts total) is exceeded
the scheduler marks the stage as ``failed`` permanently.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from typing import Any

from nodes.common import node_config

# Environment variables passed to every handler.
HANDLER_ENV_KEYS = (
    "RELAY_STAGE_ID",
    "RELAY_TASK_ID",
    "RELAY_CAPABILITY",
    "RELAY_NODE_ID",
    "RELAY_BASE_URL",
    "RELAY_TOKEN_FILE",
)

# T-191 (F4): the handler shares the daemon process, so an unbounded stdout
# buffer is an OOM vector — a handler that accidentally dumps a huge file to
# stdout would take down the process holding the agent, file-serve and
# heartbeat threads. Cap what we buffer and fail the stage loudly instead.
MAX_HANDLER_STDOUT_BYTES = 10 * 1024 * 1024
MAX_HANDLER_STDERR_BYTES = 1 * 1024 * 1024


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill the handler and every process it spawned (T-188, F1).

    The handler runs through ``shell=True`` in its own session
    (``start_new_session=True`` at spawn), so the child is a process-group
    leader and ``os.killpg`` reaches the whole tree. ``proc.kill()`` alone
    only reaps the direct shell — the actual handler (a grandchild) would
    survive a timeout and keep burning CPU on the host.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


def _read_stream_bounded(stream: Any, limit: int) -> tuple[bytes, bool]:
    """Read a pipe up to ``limit`` bytes; return ``(data, overflowed)``.

    Stops reading as soon as the cap is hit — the caller kills the process
    tree on overflow, so the truncated remainder never matters. Returns
    ``overflowed=True`` exactly when more than ``limit`` bytes were offered.
    """
    buf = bytearray()
    while True:
        try:
            chunk = stream.read(65536)
        except (ValueError, OSError):
            break
        if not chunk:
            break
        remaining = limit - len(buf)
        if len(chunk) > remaining:
            buf += chunk[: max(0, remaining)]
            return bytes(buf), True
        buf += chunk
    return bytes(buf), False



def _build_env(stage: dict[str, Any], context: dict[str, Any]) -> dict[str, str]:
    """Build the environment dict for the handler subprocess.

    Inherits the current process environment and overlays the relay
    context variables. Stage-derived values (``RELAY_STAGE_ID``,
    ``RELAY_TASK_ID``, ``RELAY_CAPABILITY``) are populated from the
    claimed stage when not already supplied by ``context``; node-level
    values (``RELAY_NODE_ID``, ``RELAY_BASE_URL``, ``RELAY_TOKEN_FILE``)
    always come from ``context``. Missing values are set to the empty
    string so the handler can rely on the keys always being present.
    """
    env = dict(os.environ)
    # Stage-derived defaults (context may override).
    stage_defaults = {
        "RELAY_STAGE_ID": stage.get("stage_id"),
        "RELAY_TASK_ID": stage.get("task_id"),
        "RELAY_CAPABILITY": stage.get("capability"),
    }
    for key in HANDLER_ENV_KEYS:
        if key in stage_defaults and key not in context:
            value = stage_defaults[key]
        else:
            value = context.get(key)
        env[key] = "" if value is None else str(value)
    return env


def _stdin_payload(stage: dict[str, Any]) -> bytes:
    """Serialize the stage payload for handler stdin.

    T-005b (design.md §3.2): delegates to _build_stdin_payload with the
    per-capability rollout toggle (``config.envelope_request_mirror``).
    Signature kept for stability — daemon and tests call this unchanged.
    T-005d strict phase: the toggle defaults to False (envelope-only
    stdin); True re-enables the legacy top-level mirror as a rollback
    lever for a single capability.
    """
    return _build_stdin_payload(
        stage, mirrored=_envelope_request_mirror(stage)
    )


def _envelope_request_mirror(stage: dict[str, Any]) -> bool:
    """Resolve the per-capability request-mirror toggle for ``stage``.

    Reads the active profile's capability ``config.envelope_request_mirror``
    (T-005b design.md §3.2, D5: per-capability flip beats a global flag).

    T-005d (strict phase): the default is now **False** — the fleet has
    migrated to the envelope contract, so the strict envelope-only stdin
    is the normal case. The config key survives as the per-capability
    ROLLBACK lever: setting ``envelope_request_mirror: true`` re-enables
    the legacy top-level mirror for a single capability whose handler
    still reads flat keys.

    Any lookup problem (profile unreadable, capability unknown, config
    not a mapping) resolves to the default as well (strict) — a config
    hiccup must not silently re-enable the rollout mirror; a legacy
    handler reading flat keys then fails loudly instead of being kept
    alive by a stale profile.
    """
    try:
        for cap in node_config.load_active_profile():
            if cap.get("name") == stage.get("capability"):
                cfg = cap.get("config")
                if isinstance(cfg, dict):
                    return bool(cfg.get("envelope_request_mirror", False))
                break
    except Exception:  # noqa: BLE001 — resolve to strict default, no mirror
        return False
    return False


def _build_stdin_payload(stage: dict[str, Any], *, mirrored: bool) -> bytes:
    """Build the Request Envelope for handler stdin (T-005b, design.md §3.2).

    FROZEN contract: ``{"task_id": str, "capability": str, "input": dict}``
    — the capability-specific payload moves under ``input``.

    mirrored=True (rollout phase): payload keys are additionally placed at
    the top level so legacy handlers reading flat keys keep working.
    Payload keys are written FIRST and the contract keys LAST, so
    ``task_id``/``capability``/``input`` always win on collision.
    mirrored=False (strict phase, post-migration): envelope only.
    """
    payload = stage.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        payload = {"payload": payload}
    envelope: dict[str, Any] = {}
    if mirrored:
        envelope.update(payload)
    envelope["task_id"] = str(stage.get("task_id") or "")
    envelope["capability"] = str(stage.get("capability") or "")
    envelope["input"] = payload
    return json.dumps(envelope).encode("utf-8")


def _normalize_result(stdout_text: str) -> dict[str, Any]:
    """Normalize handler stdout to the Response Envelope (T-005b §3.1).

    Contract (FROZEN, T-005a design.md §3.1 + ## Deviations):

    - Parsed dict WITH ``status == "completed"``: requires a ``result``
      object; ``error`` must be None if the key exists. Passed through
      verbatim (conforming handlers are never rewritten). Note the
      verified live shape omits ``error`` entirely on success — the
      normalize layer does NOT inject ``error: None`` because the daemon
      counts failures via ``"error" in result`` (key presence).
    - ``status == "error"``: pass through verbatim — the daemon's
      ``"error" in result`` check then fails the stage (fail-fast,
      retry budget applies, no exit-code guessing).
    - Any other ``status`` value: error result.
    - Bare dict (no ``status``) WITH ``error`` key: pass through
      unchanged (legacy error convention, D3).
    - Bare dict otherwise: wrapped as ``{"status": "completed",
      "result": <d>}`` — deliberately WITHOUT an ``error`` key (daemon
      failure counting is key-presence-based; D4/D10 with the verified
      daemon read).
    - Non-object JSON: error result (bare scalars were always ambiguous;
      the contract requires an object).

    Raises nothing; returns an error-result dict on normalize failures.
    Idempotent by construction: envelopes are detected by the ``status``
    key and passed through verbatim, so double-wrapping is impossible.
    """
    try:
        parsed = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        return {"error": f"handler stdout is not valid JSON: {exc.msg}"}
    if not isinstance(parsed, dict):
        kind = type(parsed).__name__
        return {
            "error": (
                "handler stdout must be a JSON object (envelope or bare "
                f"result), got {kind}"
            )
        }
    if "status" not in parsed:
        # Bare result (legacy). Error-keyed bare dicts pass through
        # unchanged; everything else is wrapped into the envelope.
        if "error" in parsed:
            return parsed
        return {"status": "completed", "result": parsed}
    status = parsed.get("status")
    if status == "completed":
        if not isinstance(parsed.get("result"), dict):
            return {
                "error": (
                    "handler envelope status 'completed' missing 'result' "
                    "object"
                )
            }
        if "error" in parsed and parsed.get("error") is not None:
            return {
                "error": (
                    "handler envelope status 'completed' carries non-null "
                    "'error'"
                )
            }
        return parsed
    if status == "error":
        if not isinstance(parsed.get("error"), str) or not parsed.get("error"):
            return {
                "error": "handler envelope status 'error' missing 'error' message"
            }
        return parsed
    return {
        "error": (
            f"handler envelope has invalid status {status!r} "
            "(expected 'completed' or 'error')"
        )
    }


def run_handler(
    handler: str,
    stage: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    """Execute a handler subprocess and return a result dict.

    Parameters
    ----------
    handler:
        Executable path or shell command. Always run via the shell
        (``shell=True``) so profiles can use pipelines / env-aware
        commands. Profiles are trusted operator-only files.
    stage:
        The stage dict returned by the claim endpoint. Only its
        ``payload`` is forwarded to stdin; ``stage_id`` / ``task_id``
        are exposed via environment variables.
    context:
        Mapping with at least ``RELAY_NODE_ID``, ``RELAY_BASE_URL``,
        ``RELAY_TOKEN_FILE`` and (optionally) the other env vars. Any
        missing key is sent to the handler as the empty string.
    timeout:
        Subprocess timeout in seconds. Defaults to 300.

    Returns
    -------
    dict
        On success (exit 0): the parsed JSON from stdout, augmented with
        an ``_handler`` debug dict (``stderr``, ``stdout_length``,
        ``exit_code``). If the stdout is not valid JSON, an error dict is
        returned instead.
        On failure (non-zero exit or timeout): an error dict of the
        form ``{"error": "...", "stderr": "..."}``.
    """
    context = context or {}
    env = _build_env(stage, context)
    stdin_bytes = _stdin_payload(stage)

    try:
        proc = subprocess.Popen(  # noqa: S602 — shell=True by design
            handler,
            shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,  # T-188: own process group → killpg reaches the tree
        )
    except OSError as exc:
        return {"error": f"handler failed to start: {exc}", "stderr": ""}

    out_holder: list[tuple[bytes, bool]] = []
    err_holder: list[tuple[bytes, bool]] = []
    overflow_evt = threading.Event()

    def _drain(stream: Any, limit: int, holder: list[tuple[bytes, bool]]) -> None:
        data, overflowed = _read_stream_bounded(stream, limit)
        holder.append((data, overflowed))
        if overflowed:
            # Stop waiting for the handler to exit — it is blocked writing
            # into the now-unread pipe. The main loop kills the tree.
            overflow_evt.set()

    def _feed_stdin() -> None:
        stdin_pipe = proc.stdin
        if stdin_pipe is None:  # pragma: no cover — PIPE guarantees a handle
            return
        try:
            stdin_pipe.write(stdin_bytes)
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                stdin_pipe.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

    # Drain stdout/stderr on separate threads: a single-threaded read would
    # deadlock the moment one pipe fills while we block on the other.
    t_out = threading.Thread(
        target=_drain,
        args=(proc.stdout, MAX_HANDLER_STDOUT_BYTES, out_holder),
        daemon=True,
    )
    t_err = threading.Thread(
        target=_drain,
        args=(proc.stderr, MAX_HANDLER_STDERR_BYTES, err_holder),
        daemon=True,
    )
    t_in = threading.Thread(target=_feed_stdin, daemon=True)
    t_out.start()
    t_err.start()
    t_in.start()

    timed_out = False
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if overflow_evt.is_set():
            # T-191 (F4): kill now, the handler cannot finish (blocked write).
            _kill_process_tree(proc)
            break
        if time.monotonic() >= deadline:
            # T-188 (F1): kill the whole tree, not just the direct shell.
            timed_out = True
            _kill_process_tree(proc)
            break
        time.sleep(0.05)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover — SIGKILL is final
        pass

    t_out.join(timeout=5)
    t_err.join(timeout=5)
    t_in.join(timeout=5)

    stdout_bytes, stdout_overflowed = out_holder[0] if out_holder else (b"", False)
    stderr_bytes, stderr_overflowed = err_holder[0] if err_holder else (b"", False)

    stdout = _safe_decode(stdout_bytes)
    stderr = _safe_decode(stderr_bytes)

    if timed_out:
        return {"error": f"handler timeout after {timeout}s", "stderr": stderr}

    # T-191 (F4): a handler must not be able to OOM the daemon through stdout.
    if stdout_overflowed or stderr_overflowed:
        return {
            "error": (
                "handler output exceeded the size limit "
                f"(stdout>{MAX_HANDLER_STDOUT_BYTES}B / "
                f"stderr>{MAX_HANDLER_STDERR_BYTES}B)"
            ),
            "stdout": stdout,
            "stderr": stderr,
        }

    if proc.returncode != 0:
        return {
            "error": f"handler exited with code {proc.returncode}",
            "stderr": stderr,
            "stdout": stdout,
        }

    # Exit 0: stdout must be valid JSON result.
    if not stdout.strip():
        return {"error": "handler produced no stdout output", "stderr": stderr}

    try:
        # Guard kept separate from normalize so the non-JSON error keeps
        # its pre-existing shape (stdout/stderr attached) byte-identical.
        json.loads(stdout)
    except json.JSONDecodeError as exc:
        return {
            "error": f"handler stdout is not valid JSON: {exc.msg}",
            "stdout": stdout,
            "stderr": stderr,
        }

    # T-005b Envelope-Contract: normalize handler stdout at this single
    # choke-point — conforming envelopes pass through verbatim, bare
    # results are wrapped, error shapes fail the stage fail-fast. All
    # normalize failures return an error-result dict (daemon failure
    # accounting untouched).
    parsed = _normalize_result(stdout)

    # Always attach handler diagnostics so callers can debug empty
    # responses without having to download artifacts. The CLI surfaces
    # these in `node-cli task result` (see _print_task_result).
    # T-193 (F6): a handler may ship a non-dict ``_handler`` in its own
    # envelope — normalize it instead of raising a TypeError that would
    # abort the stage before ``client.complete()``.
    if not isinstance(parsed.get("_handler"), dict):
        parsed["_handler"] = {}
    parsed["_handler"]["stderr"] = stderr
    parsed["_handler"]["stdout_length"] = len(stdout)
    parsed["_handler"]["exit_code"] = proc.returncode
    return parsed


def _safe_decode(data: bytes | None) -> str:
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")
