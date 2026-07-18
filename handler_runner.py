#!/usr/bin/env python3
"""Handler runner: executes a capability handler as a subprocess.

A *handler* is an external executable (script, binary, or shell command)
that performs the actual work for a claimed stage. The runner sets up a
well-defined environment, feeds the stage ``payload`` as JSON on stdin,
captures stdout/stderr, enforces a timeout, and returns a result dict
ready to be POSTed to the ``/complete`` endpoint.

Contract (see NODE_CLI_SPEC.md §4):

* Environment variables set before execution::

      RELAY_STAGE_ID      Stage ID from claim
      RELAY_TASK_ID       Task ID from claim
      RELAY_CAPABILITY    Capability name
      RELAY_NODE_ID       Assigned node ID
      RELAY_BASE_URL      Relay server URL
      RELAY_TOKEN_FILE    Path to runtime token file

* Stdin:  stage ``payload`` as a JSON string.
* Stdout: must be valid JSON — parsed and returned as the result dict.
* Stderr: captured and included in the error result on non-zero exit.
* Exit codes:
      0          -> stdout parsed as result dict
      non-zero   -> {"error": "handler exited with code N", "stderr": ...}
* Timeout: terminates the subprocess and returns
  ``{"error": "handler timeout after Ns"}``.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

# Environment variables passed to every handler.
HANDLER_ENV_KEYS = (
    "RELAY_STAGE_ID",
    "RELAY_TASK_ID",
    "RELAY_CAPABILITY",
    "RELAY_NODE_ID",
    "RELAY_BASE_URL",
    "RELAY_TOKEN_FILE",
)


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
    """Serialize the stage payload to JSON bytes for stdin.

    The spec mandates that handlers receive *only* the payload on
    stdin (not the full stage). An absent payload is serialized as the
    empty object so handlers always see valid JSON.
    """
    payload = stage.get("payload")
    if payload is None:
        payload = {}
    return json.dumps(payload).encode("utf-8")


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
        proc = subprocess.run(  # noqa: S602 — shell=True by design
            handler,
            shell=True,
            input=stdin_bytes,
            capture_output=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {"error": f"handler timeout after {timeout}s", "stderr": _safe_decode(exc.stderr)}

    stdout = _safe_decode(proc.stdout)
    stderr = _safe_decode(proc.stderr)

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
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return {
            "error": f"handler stdout is not valid JSON: {exc.msg}",
            "stdout": stdout,
            "stderr": stderr,
        }

    if not isinstance(parsed, dict):
        # Wrap non-dict JSON (e.g. a bare string/number) into a result.
        parsed = {"result": parsed}

    # Always attach handler diagnostics so callers can debug empty
    # responses without having to download artifacts. The CLI surfaces
    # these in `node-cli task result` (see _print_task_result).
    parsed.setdefault("_handler", {})
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
