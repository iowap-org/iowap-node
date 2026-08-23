#!/usr/bin/env python3
"""node-cli — generic capability-driven daemon & CLI for the AI-Relay-Service.

The CLI is fully capability-agnostic: all behaviour is driven by an
external YAML profile (see NODE_CLI_SPEC.md). Subcommands:

    Daemon control:
        node-cli daemon start | stop | status | restart | foreground

    One-shot operations:
        node-cli heartbeat
        node-cli claim <capability>
        node-cli complete <stage_id> --task <task_id> --result-file <path>
        node-cli task submit --name <name> --stage <cap>:<json_payload> [--priority N] [--owner <node_id>]
        node-cli artifact download <artifact_id> [--output <path>]
        node-cli artifact upload <file> [--name <name>] [--task-id <id>] [--stage-id <id>]

    Capability profile management:
        node-cli capabilities list | validate [profile] | publish <profile>
        node-cli capabilities diff [profile] | current

    Status:
        node-cli status
        node-cli reload
        node-cli node busy     — mark this node as busy (T-084)
        node-cli node idle     — mark this node as idle (T-084)
        node-cli node status   — show local + server node status (T-084)
        node-cli node clear-status — restore automatic status handling (T-084)

    Updates (T-062):
        node-cli update check     — fetch origin, compare local vs. upstream
        node-cli update apply     — git pull + restart the node-cli service
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from nodes.common.handler_runner import run_handler
from nodes.common.node_config import (
    ACTIVE_PATH,  # noqa: F401 — re-exported so the test fixture's cli.ACTIVE_PATH patch keeps working (T-117)
    BASE_DIR,
    PROFILES_DIR,  # noqa: F401 — re-exported for the test fixture's cli.PROFILES_DIR patch
    current_profile_name,
    invalidate_active_cache,
    load_active_profile,
)
from nodes.common.node_utils import (
    REPO_DIR,  # noqa: F401 — re-exported for the test fixture's cli.REPO_DIR patch
    SERVICE_UNIT,
    STATUS_PATH,
    TOKEN_PATH,
    load_json,
    load_meta,
    write_json_atomic,
)
from nodes.common.node_utils import (
    pid_running as _nu_pid_running,
)
from nodes.common.node_utils import (
    read_pid as _nu_read_pid,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PID_PATH = BASE_DIR / "node-cli.pid"
LOG_PATH = BASE_DIR / "node-cli.log"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("node-cli")



# ---------------------------------------------------------------------------
# Shared relay client — extracted to relay_client.py (T-112)
# Re-exported here so node_daemon.py and existing tests that reference
# node_cli.RelayClient / node_cli._effective_config / etc. keep working.
# The underscore helpers are re-exported for downstream consumers; they
# are not used directly in this module, hence the noqa.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# @with_client decorator — eliminates boilerplate from _cmd_* functions
# ---------------------------------------------------------------------------
# T-117: cli submodules are imported for build_parser() registration. They
# must NOT import node_cli (avoids circular import + keeps the cli.RelayClient
# monkeypatch working). Handlers are plain (client, args) -> int and wrapped
# with @with_client here at registration time.
from nodes.common.cli import (
    cli_artifact,
    cli_bridge,
    cli_capabilities,
    cli_docs,
    cli_file,
    cli_node,
    cli_route,
    cli_task,
    cli_update,
)
from nodes.common.relay_client import (  # noqa: F401
    RelayClient,
    _base_url,
    _effective_config,
    _filename_from_response,
    _setup_logging,
    _utcnow_str,
)


def with_client(func):
    """Decorator: injects (client, args) into _cmd_* functions.

    Handles _setup_logging, load_meta, _effective_config, RelayClient
    so every command doesn't repeat the same 4 lines.
    When --json is set, forces log level to ERROR so INFO/WARNING
    messages don't pollute the JSON output on stdout.
    """
    @functools.wraps(func)
    def wrapper(args):
        log_level = "ERROR" if getattr(args, "json", False) else args.log_level
        _setup_logging(log_level)
        meta = load_meta()
        cfg = _effective_config()
        client = RelayClient(meta, cfg)
        return func(client, args)
    return wrapper


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class Daemon:
    """The long-running daemon: heartbeat thread + claim/execute/complete loop."""

    def __init__(self, client: RelayClient, cfg: dict[str, Any]) -> None:
        self.client = client
        self.cfg = cfg
        self.in_flight: dict[str, int] = {}
        self.tasks_completed = 0
        self.tasks_failed = 0
        self.last_heartbeat_status = "unknown"
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._hb_thread: threading.Thread | None = None
        self._started_at = datetime.now(timezone.utc)
        # T-060: per-task failure counter so the daemon stops reclaiming
        # stages for a task whose handler keeps failing (exit 0 with an
        # ``error`` result or an exception). Mirrors the server-side
        # retry_count guard so both sides converge on the same budget.
        self._failed_tasks: dict[str, int] = {}

    # -- signal handling -----------------------------------------------------

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._on_term)
        signal.signal(signal.SIGINT, self._on_term)
        signal.signal(signal.SIGHUP, self._on_hup)

    def _on_term(self, *_: Any) -> None:
        log.info("received shutdown signal, stopping daemon …")
        self._stop_event.set()

    def _on_hup(self, *_: Any) -> None:
        log.info("received SIGHUP, invalidating capability cache")
        invalidate_active_cache()

    # -- status file ---------------------------------------------------------

    def _write_status(self, error: str | None = None) -> None:
        caps = load_active_profile()
        # T-108: Loop-Detection — wenn der Daemon in einem Auth-Fehler-Loop
        # festhängt (Backoff > 0 + 401/403), reichern wir den Fehler-String
        # an und markieren den Status als degraded, so dass `node info`/
        # Dashboard und Log klar signalisieren, dass die Token-Datei
        # geprüft oder der Daemon neu gestartet werden muss.
        backoff = self.client._current_backoff()
        auth_loop = backoff > 0 and "401" in (error or "")
        if auth_loop:
            error = (error or "") + " | AUTH-LOOP: token invalid — Datei prüfen oder Daemon neu starten"
        status = {
            "pid": os.getpid(),
            "node_id": self.client.meta.get("node_id"),
            "started_at": self._started_at.isoformat(),
            "last_heartbeat": _utcnow_str(),
            "heartbeat_status": self.last_heartbeat_status,
            "active_profile": current_profile_name(),
            "token_present": bool(self.client.token),
            "capabilities": [
                {"name": c["name"], "claimable": c.get("claimable", False)} for c in caps
            ],
            "in_flight": dict(self.in_flight),
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "failed_tasks": dict(self._failed_tasks),
            "error": error,
            "auth_loop": auth_loop,
            "auth_backoff_seconds": backoff,
        }
        try:
            write_json_atomic(STATUS_PATH, status)
        except OSError as exc:
            log.warning("could not write status file: %s", exc)

    # -- heartbeat thread ----------------------------------------------------

    def _heartbeat_loop(self) -> None:
        interval = self.cfg["heartbeat_interval"]
        while not self._stop_event.is_set():
            error: str | None = None
            try:
                # T-118: proactively refresh the runtime token before it
                # expires (centralized in RelayClient.maybe_refresh_token).
                # Uses rt_refresh_before_seconds (24h) margin and also
                # refreshes legacy tokens with unknown expiry.
                self.client.maybe_refresh_token()
                caps = load_active_profile()
                with self._lock:
                    inflight = dict(self.in_flight)
                hb = self.client.heartbeat(caps, inflight)
                self.last_heartbeat_status = hb.get("status", "ok")
                log.info("heartbeat %s", self.last_heartbeat_status)
            except httpx.HTTPStatusError as exc:
                error = f"http {exc.response.status_code}"
                log.error("heartbeat http error %s", error)
            except Exception as exc:  # noqa: BLE001 — daemon must survive
                error = str(exc)
                log.error("heartbeat error: %s", exc)
            self._write_status(error=error)
            # T-108: Backoff nach wiederholten Auth-Fehlschlägen, damit
            # der Daemon nicht alle ~8s hämmert. Nach Erfolg sofort 0.
            sleep_interval = interval + self.client._current_backoff()
            for _ in range(max(1, int(sleep_interval))):
                if self._stop_event.is_set():
                    return
                time.sleep(1)

    def _start_heartbeat_thread(self) -> None:
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="heartbeat"
        )
        self._hb_thread.start()

    # -- claim/execute/complete loop ----------------------------------------

    def _run_stage(self, cap: dict[str, Any], stage: dict[str, Any]) -> None:
        name = cap["name"]
        stage_id = stage.get("stage_id")
        task_id = stage.get("task_id")
        log.info("claimed %s stage %s (task %s)", name, stage_id, task_id)
        with self._lock:
            self.in_flight[name] = self.in_flight.get(name, 0) + 1
        try:
            context = {
                "RELAY_STAGE_ID": str(stage_id or ""),
                "RELAY_TASK_ID": str(task_id or ""),
                "RELAY_CAPABILITY": name,
                "RELAY_NODE_ID": str(self.client.meta.get("node_id", "")),
                "RELAY_BASE_URL": self.client.base_url,
                "RELAY_TOKEN_FILE": str(TOKEN_PATH),
            }
            result = run_handler(
                cap.get("handler", ""),
                stage,
                context=context,
                timeout=int(cap.get("timeout", 300)),
            )
            try:
                self.client.complete(str(task_id), str(stage_id), result)
                with self._lock:
                    if "error" in result:
                        self.tasks_failed += 1
                        # T-060: count handler-reported errors so the
                        # claim loop can stop reclaiming this task.
                        if task_id is not None:
                            self._failed_tasks[str(task_id)] = (
                                self._failed_tasks.get(str(task_id), 0) + 1
                            )
                    else:
                        self.tasks_completed += 1
                log.info("completed stage %s", stage_id)
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self.tasks_failed += 1
                    if task_id is not None:
                        self._failed_tasks[str(task_id)] = (
                            self._failed_tasks.get(str(task_id), 0) + 1
                        )
                log.error("failed to report result for stage %s: %s", stage_id, exc)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.tasks_failed += 1
                if task_id is not None:
                    self._failed_tasks[str(task_id)] = (
                        self._failed_tasks.get(str(task_id), 0) + 1
                    )
            log.error("stage %s execution failed: %s", stage_id, exc)
        finally:
            with self._lock:
                self.in_flight[name] = max(0, self.in_flight.get(name, 1) - 1)

    def _claim_loop(self) -> None:
        interval = self.cfg["claim_interval"]
        max_retries = int(self.cfg.get("max_retries", 2))
        while not self._stop_event.is_set():
            try:
                caps = load_active_profile()
                for cap in caps:
                    if not cap.get("claimable", False):
                        continue
                    name = cap["name"]
                    with self._lock:
                        inflight = self.in_flight.get(name, 0)
                    if inflight >= int(cap.get("max_parallel", 1)):
                        continue
                    stage = self.client.claim(name)
                    if stage is None:
                        continue
                    # T-060: skip stages for tasks that have already
                    # exceeded the retry budget on this daemon. The
                    # claim will expire on the server side where
                    # retry_count > max_retries fails the stage
                    # permanently. This prevents the daemon from
                    # burning CPU on a handler that keeps failing.
                    task_id = str(stage.get("task_id") or "")
                    with self._lock:
                        failures = self._failed_tasks.get(task_id, 0)
                    if task_id and failures >= max_retries:
                        log.warning(
                            "skipping stage %s for task %s — %d failures >= max_retries %d",
                            stage.get("stage_id"), task_id, failures, max_retries,
                        )
                        continue
                    # Run synchronously; the heartbeat thread keeps the
                    # node alive. max_parallel is enforced per-capability
                    # via in_flight and sequential iteration. Spawning
                    # threads here is intentionally avoided to keep the
                    # contract simple (see spec §15.2).
                    self._run_stage(cap, stage)
            except httpx.HTTPStatusError as exc:
                log.error("claim loop http error %s", exc.response.status_code)
            except Exception as exc:  # noqa: BLE001
                log.error("claim loop error: %s", exc)
            # T-108: Backoff nach wiederholten Auth-Fehlschlägen.
            sleep_interval = interval + self.client._current_backoff()
            for _ in range(max(1, int(sleep_interval))):
                if self._stop_event.is_set():
                    return
                time.sleep(1)

    # -- lifecycle ----------------------------------------------------------

    def run(self) -> None:
        self._install_signal_handlers()
        log.info(
            "node-cli daemon starting for node %s (base_url=%s)",
            self.client.meta.get("node_id"),
            self.client.base_url,
        )
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        self._write_status()
        self._start_heartbeat_thread()
        try:
            self._claim_loop()
        finally:
            self._stop_event.set()
            if self._hb_thread and self._hb_thread.is_alive():
                self._hb_thread.join(timeout=5)
            self._write_status()
            log.info("daemon stopped")


# ---------------------------------------------------------------------------
# Daemon control (start / stop / status)
# ---------------------------------------------------------------------------

def _read_pid() -> int | None:
    # T-117: thin wrapper over node_utils.read_pid(pid_path) so callers and
    # tests keep the original no-arg signature (PID_PATH bound at call time).
    return _nu_read_pid(PID_PATH)


def _pid_running(pid: int) -> bool:
    # T-117: re-export wrapper over node_utils.pid_running(pid).
    return _nu_pid_running(pid)


def _daemon_start(args: argparse.Namespace) -> int:
    _setup_logging(args.log_level)
    pid = _read_pid()
    if pid is not None and _pid_running(pid):
        print(f"daemon already running (pid {pid})")
        return 0
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    log_fd = open(LOG_PATH, "a", encoding="utf-8")  # noqa: SIM115 — kept open by child
    proc = subprocess.Popen(  # noqa: S603 — intentional self-spawn
        [sys.executable, "-m", "nodes.common.node_cli", "--daemon-internal"],
        stdout=log_fd,
        stderr=log_fd,
        start_new_session=True,
    )
    # Inner process writes the PID file itself; give it a moment.
    for _ in range(50):
        if PID_PATH.exists():
            break
        if proc.poll() is not None:
            print("daemon inner process exited early — see log:", LOG_PATH, file=sys.stderr)
            return 1
        time.sleep(0.1)
    pid = _read_pid() or proc.pid
    print(f"daemon started (pid {pid}); log: {LOG_PATH}")
    return 0


def _daemon_stop(args: argparse.Namespace) -> int:  # noqa: ARG001
    _setup_logging(args.log_level)
    pid = _read_pid()
    if pid is None or not _pid_running(pid):
        print("daemon not running")
        if PID_PATH.exists():
            PID_PATH.unlink(missing_ok=True)
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"failed to signal pid {pid}: {exc}", file=sys.stderr)
        return 1
    for _ in range(100):
        if not _pid_running(pid):
            break
        time.sleep(0.1)
    if _pid_running(pid):
        print("daemon did not stop, sending SIGKILL", file=sys.stderr)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    PID_PATH.unlink(missing_ok=True)
    print("daemon stopped")
    return 0


def _daemon_status(args: argparse.Namespace) -> int:  # noqa: ARG001
    pid = _read_pid()
    running = pid is not None and _pid_running(pid)
    active_profile = current_profile_name()
    status_file: dict[str, Any] = {}
    if STATUS_PATH.exists():
        try:
            status_file = load_json(STATUS_PATH, default={}) or {}
        except Exception:
            status_file = {}
    print(f"pid: {pid if pid is not None else '-'}")
    print(f"running: {running}")
    print(f"active_profile: {active_profile or '-'}")
    print(f"last_heartbeat: {status_file.get('last_heartbeat', '-')}")
    print(f"heartbeat_status: {status_file.get('heartbeat_status', '-')}")
    print(f"tasks_completed: {status_file.get('tasks_completed', 0)}")
    print(f"tasks_failed: {status_file.get('tasks_failed', 0)}")
    failed_tasks = status_file.get("failed_tasks", {})
    if failed_tasks:
        print(f"failed_tasks: {failed_tasks}")
    inflight = status_file.get("in_flight", {})
    if inflight:
        print(f"in_flight: {inflight}")
    return 0 if running else 1


def _daemon_restart(args: argparse.Namespace) -> int:
    _daemon_stop(args)
    return _daemon_start(args)


def _daemon_foreground(args: argparse.Namespace) -> int:  # noqa: ARG001
    _setup_logging(os.environ.get("RELAY_LOG_LEVEL"))
    try:
        meta = load_meta()
        cfg = _effective_config()
        client = RelayClient(meta, cfg)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1
    # Write our own PID file so `status`/`stop` work for foreground too.
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()) + "\n", encoding="utf-8")
    try:
        Daemon(client, cfg).run()
    finally:
        PID_PATH.unlink(missing_ok=True)
    return 0


def _check_other_daemon(
    other_pid_path: Path | None = None,
    own_name: str = "node-cli daemon",
    other_name: str = "node-daemon",
) -> str | None:
    """T-137b-Guard: return an error message if the SSE daemon is running."""
    pid_file = other_pid_path or (BASE_DIR / "node-daemon.pid")
    pid = _nu_read_pid(pid_file)
    if pid is not None and _nu_pid_running(pid):
        return (
            f"{own_name} refuses to start: {other_name} is already running "
            f"(pid {pid}, {pid_file}). Stop it first — running both as the "
            f"same node causes token fights and duplicate claims (T-137b)."
        )
    return None


def _daemon_internal() -> int:
    """Inner entry point for the self-spawned daemon process."""
    _setup_logging(os.environ.get("RELAY_LOG_LEVEL"))
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    # T-137b-Guard: refuse to start while the SSE daemon is running.
    conflict = _check_other_daemon()
    if conflict:
        print(conflict, file=sys.stderr)
        return 1
    try:
        meta = load_meta()
        cfg = _effective_config()
        client = RelayClient(meta, cfg)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1
    PID_PATH.write_text(str(os.getpid()) + "\n", encoding="utf-8")
    try:
        Daemon(client, cfg).run()
    finally:
        PID_PATH.unlink(missing_ok=True)
    return 0


# ---------------------------------------------------------------------------
# One-shot subcommands
# ---------------------------------------------------------------------------

def _parse_stage_arg(stage: str) -> tuple[str, dict[str, Any]]:
    """Parse ``<cap>:<json_payload>`` into (capability, payload).

    Re-exported from ``cli.cli_task`` (T-117 split) so existing tests that
    call ``cli._parse_stage_arg`` keep resolving.
    """
    return cli_task._parse_stage_arg(stage)


@with_client
def _cmd_heartbeat(client: RelayClient, args: argparse.Namespace) -> int:
    caps = load_active_profile()
    hb = client.heartbeat(caps, {})
    if args.json:
        print(json.dumps(hb, default=str))
        return 0
    print(json.dumps(hb, indent=2, default=str))
    return 0


@with_client
def _cmd_claim(client: RelayClient, args: argparse.Namespace) -> int:
    stage = client.claim(args.capability)
    if stage is None:
        print(json.dumps({"claimed": False}))
        return 0
    result = {"claimed": True, "stage": stage}
    if args.json:
        print(json.dumps(result, default=str))
        return 0
    print(json.dumps(result, indent=2, default=str))
    cd = stage.get("capability_details")
    if cd:
        print()
        print(f"  Capability: {cd.get('name', '?')}")
        if cd.get("description"):
            print(f"  Description: {cd['description']}")
        if cd.get("type"):
            print(f"  Type:        {cd['type']}")
        if cd.get("input_schema"):
            print(f"  Input Schema: {json.dumps(cd['input_schema'], indent=2)}")
    return 0


@with_client
def _cmd_complete(client: RelayClient, args: argparse.Namespace) -> int:
    if not Path(args.result_file).exists():
        print(f"result file not found: {args.result_file}", file=sys.stderr)
        return 2
    try:
        result = json.loads(Path(args.result_file).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"result file is not valid JSON: {exc}", file=sys.stderr)
        return 2
    resp = client.complete(args.task, args.stage_id, result)
    if args.json:
        print(json.dumps(resp, default=str))
        return 0
    print(json.dumps(resp, indent=2, default=str))
    return 0


# ---------------------------------------------------------------------------
# capabilities subcommands — moved to cli/cli_capabilities (T-117)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# node subcommands — moved to cli/cli_node (T-117)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# status / reload
# ---------------------------------------------------------------------------

def _cmd_status(args: argparse.Namespace) -> int:  # noqa: ARG001
    if not STATUS_PATH.exists():
        print("(no status file — daemon not started yet)")
        return 1
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"status file is not valid JSON: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(data, indent=2, default=str))
    return 0


def _cmd_reload(args: argparse.Namespace) -> int:  # noqa: ARG001
    pid = _read_pid()
    if pid is None or not _pid_running(pid):
        print("daemon not running", file=sys.stderr)
        return 1
    try:
        os.kill(pid, signal.SIGHUP)
    except OSError as exc:
        print(f"failed to send SIGHUP: {exc}", file=sys.stderr)
        return 1
    print(f"SIGHUP sent to daemon (pid {pid})")
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="node-cli",
        description="Generic capability-driven daemon & CLI for the AI-Relay-Service.",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="Log level (DEBUG/INFO/WARNING/ERROR). Default: env RELAY_LOG_LEVEL or INFO.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of formatted text (for scripting).",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    # daemon
    p_daemon = sub.add_parser("daemon", help="Control the background daemon.")
    p_daemon_sub = p_daemon.add_subparsers(dest="daemon_command", required=True, metavar="<action>")
    for action in ("start", "stop", "restart", "foreground"):
        p = p_daemon_sub.add_parser(action, help=f"daemon {action}")
        p.set_defaults(func=_daemon_dispatch)
    p_status = p_daemon_sub.add_parser("status", help="show daemon status")
    p_status.set_defaults(func=_daemon_dispatch)

    # heartbeat
    p_hb = sub.add_parser("heartbeat", help="Send a single heartbeat and exit.")
    p_hb.set_defaults(func=_cmd_heartbeat)

    # claim
    p_claim = sub.add_parser("claim", help="Claim one stage for a capability.")
    p_claim.add_argument("capability", help="Capability name to claim.")
    p_claim.set_defaults(func=_cmd_claim)

    # complete
    p_complete = sub.add_parser("complete", help="Complete a claimed stage.")
    p_complete.add_argument("stage_id", help="Stage ID to complete.")
    p_complete.add_argument("--task", required=True, help="Task ID of the stage.")
    p_complete.add_argument(
        "--result-file",
        required=True,
        help="Path to a JSON file containing the result dict.",
    )
    p_complete.set_defaults(func=_cmd_complete)

    # task submit
    p_task = sub.add_parser("task", help="Task operations.")
    p_task_sub = p_task.add_subparsers(dest="task_command", required=True, metavar="<action>")
    p_submit = p_task_sub.add_parser("submit", help="Submit a single-stage task.")
    p_submit.add_argument("--name", default="", help="Task name (default: auto).")
    p_submit.add_argument(
        "--stage",
        required=True,
        help="Stage as <capability>:<json-payload>.",
    )
    p_submit.add_argument("--priority", type=int, default=0, help="Task priority 0-10.")
    p_submit.add_argument(
        "--owner",
        default=None,
        help="Node ID that must claim this task (owner_node_id).",
    )
    p_submit.set_defaults(func=with_client(cli_task._cmd_task_submit))

    p_result = p_task_sub.add_parser("result", help="Show task result (status, stages, artifacts).")
    p_result.add_argument("task_id", help="Task ID to query.")
    p_result.set_defaults(func=with_client(cli_task._cmd_task_result))

    p_wait = p_task_sub.add_parser("wait", help="Wait for a task to complete and show result.")
    p_wait.add_argument("task_id", help="Task ID to wait for.")
    p_wait.add_argument("--interval", type=int, default=5, help="Poll interval in seconds (default: 5).")
    p_wait.set_defaults(func=with_client(cli_task._cmd_task_wait))

    p_note = p_task_sub.add_parser("note", help="Append a free-form note to a task (T-052 mini-chat).")
    p_note.add_argument("task_id", help="Task ID to add a note to.")
    p_note.add_argument("message", help="Note text (1..2000 characters).")
    p_note.set_defaults(func=with_client(cli_task._cmd_task_note))

    # capabilities
    p_caps = sub.add_parser("capabilities", help="Capability profile management.")
    p_caps_sub = p_caps.add_subparsers(
        dest="capabilities_command", required=True, metavar="<action>"
    )

    p_list = p_caps_sub.add_parser("list", help="List profiles in profiles.d/.")
    p_list.set_defaults(func=cli_capabilities._cmd_capabilities_list)

    p_validate = p_caps_sub.add_parser("validate", help="Validate a profile (default: active).")
    p_validate.add_argument(
        "profile", nargs="?", default=None, help="Profile name (default: active)."
    )
    p_validate.set_defaults(func=cli_capabilities._cmd_capabilities_validate)

    p_publish = p_caps_sub.add_parser(
        "publish", help="Validate + atomically copy profile to active."
    )
    p_publish.add_argument("profile", help="Profile name to publish.")
    p_publish.set_defaults(func=cli_capabilities._cmd_capabilities_publish)

    p_diff = p_caps_sub.add_parser("diff", help="Diff working profile vs active.")
    p_diff.add_argument("profile", nargs="?", default=None, help="Profile name (default: active).")
    p_diff.set_defaults(func=cli_capabilities._cmd_capabilities_diff)

    p_current = p_caps_sub.add_parser("current", help="Show active profile name.")
    p_current.set_defaults(func=cli_capabilities._cmd_capabilities_current)

    p_server = p_caps_sub.add_parser(
        "server", help="Query capabilities registered on the relay server (all nodes)."
    )
    p_server.set_defaults(func=with_client(cli_capabilities._cmd_capabilities_server))

    p_info = p_caps_sub.add_parser(
        "info", help="Show detailed info for a single capability registered on the relay."
    )
    p_info.add_argument("name", help="Capability name to query.")
    p_info.set_defaults(func=with_client(cli_capabilities._cmd_capabilities_info))

    # node
    p_node = sub.add_parser("node", help="Node operations (list, info, busy/idle/status).")
    p_node_sub = p_node.add_subparsers(dest="node_command", required=True, metavar="<action>")

    p_node_list = p_node_sub.add_parser("list", help="List all nodes registered on the relay server.")
    p_node_list.set_defaults(func=with_client(cli_node._cmd_node_list))

    p_node_info = p_node_sub.add_parser("info", help="Show detailed info for a single node.")
    p_node_info.add_argument("node_id", help="Node ID to query.")
    p_node_info.set_defaults(func=with_client(cli_node._cmd_node_info))

    # T-084: node busy / idle / status / clear-status
    p_node_busy = p_node_sub.add_parser(
        "busy", help="Mark this node as busy (stops it from claiming new stages)."
    )
    p_node_busy.add_argument(
        "--once", action="store_true",
        help="Send a single heartbeat carrying the new status immediately.",
    )
    p_node_busy.set_defaults(func=with_client(cli_node._cmd_node_busy))

    p_node_idle = p_node_sub.add_parser(
        "idle", help="Mark this node as idle (available for claiming stages again)."
    )
    p_node_idle.add_argument(
        "--once", action="store_true",
        help="Send a single heartbeat carrying the new status immediately.",
    )
    p_node_idle.set_defaults(func=with_client(cli_node._cmd_node_idle))

    p_node_clear_status = p_node_sub.add_parser(
        "clear-status",
        help="Remove an explicit busy/idle request and restore automatic status handling.",
    )
    p_node_clear_status.add_argument(
        "--once", action="store_true",
        help="Send a single heartbeat after clearing the requested status.",
    )
    p_node_clear_status.set_defaults(func=with_client(cli_node._cmd_node_clear_status))

    p_node_status = p_node_sub.add_parser(
        "status", help="Show the local + server-side node status."
    )
    p_node_status.set_defaults(func=with_client(cli_node._cmd_node_status))

    # T-136: route register/unregister/list — temp bridge route management
    p_route = sub.add_parser("route", help="Manage temporary bridge routes (T-136).")
    p_route_sub = p_route.add_subparsers(dest="route_command", required=True, metavar="<action>")

    p_route_register = p_route_sub.add_parser("register", help="Register a temporary bridge route.")
    p_route_register.add_argument("--path", required=True, help="Route path (e.g. /upload/abc123).")
    p_route_register.add_argument("--method", required=True, help="HTTP method (GET/POST/PUT/...).")
    p_route_register.add_argument("--upstream", required=True, help="Upstream URL the relay proxies to.")
    p_route_register.add_argument("--ttl", type=int, required=True, help="Time-to-live in seconds.")
    p_route_register.add_argument("--channel", required=True, help="channel_id tying this route to a session.")
    p_route_register.add_argument("--description", default="", help="Optional human-readable note.")
    p_route_register.set_defaults(func=with_client(cli_route._cmd_route_register))

    p_route_unregister = p_route_sub.add_parser("unregister", help="Revoke a temp route before its TTL.")
    p_route_unregister.add_argument("--path", required=True, help="Route path to delete.")
    p_route_unregister.add_argument("--method", required=True, help="HTTP method of the route.")
    p_route_unregister.set_defaults(func=with_client(cli_route._cmd_route_unregister))

    p_route_list = p_route_sub.add_parser("list", help="List this node's own routes (temp + permanent).")
    p_route_list.set_defaults(func=with_client(cli_route._cmd_route_list))

    # T-153: bridge upload/download — stream large files via temp bridge routes
    p_bridge = sub.add_parser(
        "bridge", help="Upload/download large files via a temp bridge route (T-129/T-153)."
    )
    p_bridge_sub = p_bridge.add_subparsers(dest="bridge_command", required=True, metavar="<action>")

    p_bridge_upload = p_bridge_sub.add_parser(
        "upload", help="Open a bridge channel and stream a local file TO the storage node."
    )
    p_bridge_upload.add_argument("file", type=str, help="Path to the file to upload.")
    p_bridge_upload.add_argument(
        "--channel", default=None,
        help="Channel id (storage.upload_channel); minted when omitted.",
    )
    p_bridge_upload.add_argument(
        "--backup", action="store_true",
        help="Use backup.create (mode=bridge) instead of storage.upload_channel.",
    )
    p_bridge_upload.add_argument("--source", default=None, help="Backup source (default: filename).")
    p_bridge_upload.add_argument("--type", default="full", choices=["full", "incremental"],
                                 help="Backup type (default: full).")
    p_bridge_upload.add_argument("--name", default="", help="Optional task name.")
    p_bridge_upload.add_argument("--priority", type=int, default=0, help="Optional task priority.")
    p_bridge_upload.add_argument("--interval", type=int, default=5,
                                 help="Poll interval in seconds (default: 5).")
    p_bridge_upload.set_defaults(func=with_client(cli_bridge._cmd_bridge_upload))

    p_bridge_download = p_bridge_sub.add_parser(
        "download", help="Open a bridge channel and stream a large file FROM the storage node."
    )
    p_bridge_download.add_argument(
        "--channel", default=None,
        help="Channel id (storage.download_channel); required unless --backup is set.",
    )
    p_bridge_download.add_argument(
        "--backup", default=None,
        help="Backup id (backup.restore). When set, --channel is ignored.",
    )
    p_bridge_download.add_argument(
        "--output", "-o", type=Path, default=None,
        help="Output path (default: <channel_id> or <backup_id>).",
    )
    p_bridge_download.add_argument("--name", default="", help="Optional task name.")
    p_bridge_download.add_argument("--priority", type=int, default=0, help="Optional task priority.")
    p_bridge_download.add_argument("--interval", type=int, default=5,
                                   help="Poll interval in seconds (default: 5).")
    p_bridge_download.set_defaults(func=with_client(cli_bridge._cmd_bridge_download))

    # T-164: file send/get — generic transfer-ladder wrapper.
    p_file = sub.add_parser(
        "file",
        help="Send/get files via the generic transfer ladder (T-164).",
    )
    p_file_sub = p_file.add_subparsers(dest="file_command", required=True, metavar="<action>")

    p_file_send = p_file_sub.add_parser(
        "send", help="Send a file via inline/artifact/bridge (auto-chosen)."
    )
    p_file_send.add_argument("file", type=str, help="Path to the file to send.")
    p_file_send.add_argument("--cap", required=True, help="Capability that accepts the file.")
    p_file_send.add_argument(
        "--force", choices=["inline", "artifact", "bridge"], default=None,
        help="Force a transfer mode (must be in the capability's upload_modes).",
    )
    p_file_send.add_argument("--name", default="", help="Target filename / task name.")
    p_file_send.add_argument("--priority", type=int, default=0, help="Task priority.")
    p_file_send.add_argument("--owner", default=None, help="Node ID that must claim the task.")
    p_file_send.add_argument("--source", default=None, help="Backup source (backup.create bridge).")
    p_file_send.add_argument(
        "--type", default="full", choices=["full", "incremental"],
        help="Backup type (backup.create bridge).",
    )
    p_file_send.add_argument(
        "--interval", type=int, default=5, help="Poll interval in seconds (bridge mode).",
    )
    p_file_send.set_defaults(func=with_client(cli_file._cmd_file_send))

    p_file_get = p_file_sub.add_parser(
        "get", help="Get a file via inline/bridge (auto-chosen by capability)."
    )
    p_file_get.add_argument(
        "ref", type=str,
        help="Generic file reference (path for storage.fetch, backup_id for backup.restore, "
             "channel_id for storage.download_channel).",
    )
    p_file_get.add_argument("--cap", required=True, help="Capability that serves the file.")
    p_file_get.add_argument(
        "--output", "-o", type=Path, default=None,
        help="Output path (default: filename from server/channel).",
    )
    p_file_get.add_argument("--name", default="", help="Task name.")
    p_file_get.add_argument("--priority", type=int, default=0, help="Task priority.")
    p_file_get.add_argument("--owner", default=None, help="Node ID that must claim the task.")
    p_file_get.add_argument(
        "--interval", type=int, default=5, help="Poll interval in seconds (bridge mode).",
    )
    p_file_get.set_defaults(func=with_client(cli_file._cmd_file_get))

    # status / reload
    p_status = sub.add_parser("status", help="Print worker_status.json content.")
    p_status.set_defaults(func=_cmd_status)

    p_reload = sub.add_parser("reload", help="Send SIGHUP to running daemon.")
    p_reload.set_defaults(func=_cmd_reload)

    # artifact operations
    p_artifact = sub.add_parser("artifact", help="Artifact operations.")
    p_artifact_sub = p_artifact.add_subparsers(
        dest="artifact_command", required=True, metavar="<action>"
    )
    p_artifact_download = p_artifact_sub.add_parser(
        "download", help="Download an artifact by id from the relay."
    )
    p_artifact_download.add_argument("artifact_id", help="The artifact ID to download.")
    p_artifact_download.add_argument(
        "--output", "-o", type=Path, default=None,
        help="Output path (default: <artifact name from server>).",
    )
    p_artifact_download.set_defaults(func=with_client(cli_artifact._cmd_artifact_download))

    p_artifact_upload = p_artifact_sub.add_parser(
        "upload", help="Upload a local file as an artifact to the relay."
    )
    p_artifact_upload.add_argument("file", type=str, help="Path to the file to upload.")
    p_artifact_upload.add_argument(
        "--name", default=None, help="Artifact name (default: filename)."
    )
    p_artifact_upload.add_argument(
        "--task-id", default=None, help="Optional task ID to associate with."
    )
    p_artifact_upload.add_argument(
        "--stage-id", default=None, help="Optional stage ID to associate with."
    )
    p_artifact_upload.set_defaults(func=with_client(cli_artifact._cmd_artifact_upload))

    # docs (T-059)
    p_docs = sub.add_parser(
        "docs",
        help="Read relay documentation from the server (list all, or print one).",
    )
    p_docs.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Document name (omit to list all available documents).",
    )
    p_docs.set_defaults(func=with_client(cli_docs._cmd_docs))

    # update (T-062)
    p_update = sub.add_parser(
        "update",
        help="Check for and apply git-based node-cli updates.",
    )
    p_update_sub = p_update.add_subparsers(
        dest="update_command", required=True, metavar="<action>"
    )
    p_update_check = p_update_sub.add_parser(
        "check",
        help="Fetch origin and report whether the local branch is behind.",
    )
    p_update_check.set_defaults(func=cli_update._cmd_update_check)

    p_update_apply = p_update_sub.add_parser(
        "apply",
        help="Pull the latest commits and restart the node-cli service.",
    )
    p_update_apply.add_argument(
        "--service-unit",
        default=SERVICE_UNIT,
        help=f"systemd user unit to restart (default: {SERVICE_UNIT}).",
    )
    p_update_apply.set_defaults(func=cli_update._cmd_update_apply)

    return parser


def _daemon_dispatch(args: argparse.Namespace) -> int:
    action = args.daemon_command
    if action == "start":
        return _daemon_start(args)
    if action == "stop":
        return _daemon_stop(args)
    if action == "restart":
        return _daemon_restart(args)
    if action == "status":
        return _daemon_status(args)
    if action == "foreground":
        return _daemon_foreground(args)
    print(f"unknown daemon action: {action}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    # Hidden internal flag used by the self-spawned daemon process.
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "--daemon-internal":
        return _daemon_internal()

    parser = build_parser()
    args = parser.parse_args(raw)
    try:
        return int(args.func(args))
    except SystemExit as exc:
        if exc.code is None:
            return 0
        print(exc, file=sys.stderr)
        return int(exc.code) if isinstance(exc.code, int) else 1
    except KeyboardInterrupt:
        return 130
    except httpx.HTTPError as exc:
        print(f"network error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
