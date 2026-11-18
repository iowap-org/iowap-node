#!/usr/bin/env python3
"""Poll-mode daemon (T-250) — the legacy claim/heartbeat polling daemon.

Holds the ``Daemon`` class and its control surface
(start/stop/status/restart/foreground + the self-spawn inner entry point)
moved byte-exact out of ``node_cli`` (T-250). Two consumers delegate here:

* ``node-cli daemon {start,stop,status,foreground,restart}`` — must stay
  byte-identical: pid file ``node-cli.pid``, log ``node-cli.log``, same
  self-spawn command (``python -m nodes.common.node_cli --daemon-internal``),
  same SIGHUP behaviour (invalidate the capability cache).
* ``node-daemon``/``iowap-daemon`` ``--mode poll`` — runs the same daemon
  in the foreground of the caller; the mode default stays ``sse``
  (``node_daemon.SseDaemon``), requesting both modes fails closed.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from nodes.common import file_serve
from nodes.common.handler_runner import run_handler
from nodes.common.node_config import (
    BASE_DIR,
    capability_cost,
    current_profile_name,
    invalidate_active_cache,
    load_active_budget,
    load_active_profile,
    load_forward_slots,
)
from nodes.common.node_utils import (
    STATUS_PATH,
    TOKEN_PATH,
    load_json,
    load_meta,
    post_result_rescue_note,
    write_json_atomic,
)
from nodes.common.node_utils import (
    pid_running as _nu_pid_running,
)
from nodes.common.node_utils import (
    read_pid as _nu_read_pid,
)
from nodes.common.relay_client import (
    RelayClient,
    _effective_config,
    _setup_logging,
    _utcnow_str,
)

# ---------------------------------------------------------------------------
# Paths — identical to the pre-T-250 node_cli definitions (T-137b guard in
# node_daemon reads node-cli.pid; consumers re-export them from node_cli).
# ---------------------------------------------------------------------------

PID_PATH = BASE_DIR / "node-cli.pid"
LOG_PATH = BASE_DIR / "node-cli.log"

# ---------------------------------------------------------------------------
# Logging — same logger name as the pre-move class so log-file output is
# unchanged (the message format itself is fixed in relay_client._setup_logging).
# ---------------------------------------------------------------------------

log = logging.getLogger("node-cli")


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class Daemon:
    """The long-running daemon: heartbeat thread + claim/execute/complete loop."""

    def __init__(self, client: RelayClient, cfg: dict[str, Any]) -> None:
        self.client = client
        self.cfg = cfg
        self.in_flight: dict[str, int] = {}
        # T-247: sum of the admission points of the stages running (or
        # reserving) right now — checked by the claim loop alongside
        # max_parallel, released in _release_reservation.
        self.in_flight_points = 0
        self.tasks_completed = 0
        self.tasks_failed = 0
        self.last_heartbeat_status = "unknown"
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._hb_thread: threading.Thread | None = None
        # T-177: last server probe result (health/ready/metrics), written by
        # the probe thread and merged into the status file by _write_status.
        self.server_probe: dict[str, Any] = {"ok": False, "error": "probe pending"}
        self._probe_thread: threading.Thread | None = None
        self._started_at = datetime.now(UTC)
        # T-060: per-task failure counter so the daemon stops reclaiming
        # stages for a task whose handler keeps failing (exit 0 with an
        # ``error`` result or an exception). Mirrors the server-side
        # retry_count guard so both sides converge on the same budget.
        self._failed_tasks: dict[str, int] = {}
        # T-244: set by the stage runner when a stage completes — the
        # claim loop wakes immediately instead of sitting out the full
        # claim interval while queued work may be waiting.
        self._claim_wake = threading.Event()

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
        # T-237: the profile load used to be the first unguarded statement —
        # a broken active profile raised CapabilityValidationError straight
        # into the heartbeat thread (the call site sits OUTSIDE the loop's
        # try) and out of the startup path. The thread died silently, the
        # process lived on, the node went offline at the relay and the
        # status file kept reporting the stale last tick. Now: degrade —
        # empty capabilities + the profile error in the status snapshot.
        profile_error: str | None = None
        try:
            caps = load_active_profile()
        except Exception as exc:
            log.exception("active profile load failed while writing status")
            caps = []
            profile_error = f"active profile invalid: {exc}"
        # T-108: loop detection — when the daemon is stuck in an auth
        # error loop (backoff > 0 + 401/403), we enrich the error string
        # and mark the status as degraded so that `node info`/dashboard
        # and the log clearly signal that the token file must be checked
        # or the daemon restarted.
        backoff = self.client._current_backoff()
        auth_loop = backoff > 0 and "401" in (error or "")
        if auth_loop:
            error = (error or "") + " | AUTH-LOOP: token invalid — check the token file or restart the daemon"
        if profile_error:
            error = f"{error} | {profile_error}" if error else profile_error
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
            "server": self.server_probe,
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
                # T-182/T-183: fixed-interval credential maintenance with
                # claims paused. T-185: only when a rotation is actually
                # due — the heartbeat tick must not rotate without need.
                if self.client.maintenance_due():
                    self.client.run_credential_maintenance()
                caps = load_active_profile()
                with self._lock:
                    inflight = dict(self.in_flight)
                forward_slots = load_forward_slots()
                hb = self.client.heartbeat(caps, inflight, forward_slots=forward_slots)
                self.last_heartbeat_status = hb.get("status", "ok")
                log.info("heartbeat %s", self.last_heartbeat_status)
            except httpx.HTTPStatusError as exc:
                error = f"http {exc.response.status_code}"
                log.error("heartbeat http error %s", error)
            except Exception as exc:  # noqa: BLE001 — daemon must survive
                error = str(exc)
                log.error("heartbeat error: %s", exc)
            try:
                # T-237: the status snapshot itself must never take this
                # thread down — a failing write is logged with traceback
                # and the loop continues with the next tick.
                self._write_status(error=error)
            except Exception:
                log.exception("status file write failed")
            # T-108: back off after repeated auth failures so the daemon
            # does not hammer every ~8s. Immediately 0 after success.
            sleep_interval = interval + self.client._current_backoff()
            for _ in range(max(1, int(sleep_interval))):
                if self._stop_event.is_set():
                    return
                time.sleep(1)

    # -- T-177: server probe thread -------------------------------------------

    def _probe_loop(self) -> None:
        """Probe relay /health + /ready + /metrics on a fixed cadence.

        Runs in its own daemon thread so a hanging HTTP call can never
        stall heartbeats or claims. Failures are recorded (never raised):
        the next tick retries. 30 s cadence → worst-case server-outage
        visibility latency is probe + telemetry interval (~90 s end-to-end).
        """
        interval = 30
        while not self._stop_event.is_set():
            try:
                self.server_probe = self.client.probe_server()
            except Exception as exc:  # noqa: BLE001 — probe must survive
                log.warning("server probe failed: %s", exc)
                self.server_probe = {"ok": False, "error": str(exc)}
            for _ in range(interval):
                if self._stop_event.is_set():
                    return
                time.sleep(1)

    def _start_probe_thread(self) -> None:
        self._probe_thread = threading.Thread(
            target=self._probe_loop, daemon=True, name="server-probe"
        )
        self._probe_thread.start()

    def _start_heartbeat_thread(self) -> None:
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="heartbeat"
        )
        self._hb_thread.start()

    # -- claim/execute/complete loop ----------------------------------------

    def _release_reservation(self, capability: str, cost: int) -> None:
        """Release an in-flight slot together with its T-247 points.

        Mirror of the SSE daemon's helper. ``cost`` is the admission
        cost captured by the caller (the same cap dict that was checked
        in the claim loop, so no repricing skew is possible).
        """
        with self._lock:
            self.in_flight[capability] = max(
                0, self.in_flight.get(capability, 1) - 1
            )
            self.in_flight_points = max(0, self.in_flight_points - cost)

    def _run_stage(self, cap: dict[str, Any], stage: dict[str, Any]) -> None:
        name = cap["name"]
        stage_id = stage.get("stage_id")
        task_id = stage.get("task_id")
        log.info("claimed %s stage %s (task %s)", name, stage_id, task_id)
        # T-247: the run reserves the stage's admission points for its
        # lifetime (released in the finally via _release_reservation).
        cost = capability_cost(cap)
        with self._lock:
            self.in_flight[name] = self.in_flight.get(name, 0) + 1
            self.in_flight_points += cost
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
                    # T-238: count by error VALUE, not key presence —
                    # handler_runner passes conforming completed envelopes
                    # with "error": null through verbatim; null is not a
                    # failure (it used to bump tasks_failed/_failed_tasks
                    # and starve later stages of a multi-stage task via the
                    # max_retries skip).
                    if result.get("error") is not None:
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
                # T-001c-c: the stage is gone server-side (404: expired/
                # re-queued/failed) — the handler's result is rescued as a
                # task note instead of being dropped. Other failures stay
                # plain (retryable); there is no complete_by_script
                # fallthrough here.
                cli_resp = getattr(exc, "response", None)
                if (
                    getattr(cli_resp, "status_code", None) == 404
                    and task_id
                ):
                    cli_saved = post_result_rescue_note(
                        self.client,
                        str(task_id),
                        str(stage_id),
                        result,
                        reason=str(exc),
                    )
                    if cli_saved:
                        log.info(
                            "stage %s complete rejected (404) — result rescued as note",
                            stage_id,
                        )
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
            self._release_reservation(name, cost)
            # T-244: the completed stage may leave queued work behind —
            # wake the claim loop immediately (the loop clears the event).
            self._claim_wake.set()

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
                        running_points = self.in_flight_points
                    if inflight >= int(cap.get("max_parallel", 1)):
                        continue
                    # T-247: the points axis alongside max_parallel —
                    # admit only when the running points plus this
                    # capability's cost stay at/below the node budget;
                    # the claim POST is skipped entirely otherwise. No
                    # budget → axis off (default-off). The loop is
                    # sequential today (in_flight drains before the
                    # next check) but the ledger check keeps parity
                    # with the SSE daemon's atomic reserve.
                    budget = load_active_budget()
                    if (
                        budget is not None
                        and running_points + capability_cost(cap) > budget
                    ):
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
            # T-108: back off after repeated auth failures.
            sleep_interval = interval + self.client._current_backoff()
            for _ in range(max(1, int(sleep_interval))):
                if self._stop_event.is_set():
                    return
                # T-244: wake-aware slices — a completed stage (or any
                # external signal) shortens the wait to a re-check.
                if self._claim_wake.wait(1):
                    self._claim_wake.clear()
                    break

    # -- lifecycle ----------------------------------------------------------

    def run(self) -> None:
        self._install_signal_handlers()
        log.info(
            "node-cli daemon starting for node %s (base_url=%s)",
            self.client.meta.get("node_id"),
            self.client.base_url,
        )
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        # T-185: refresh the registration secret synchronously BEFORE any
        # connection thread starts (same quiescence model as the SSE
        # daemon). Non-fatal on failure: rs stays due and the next
        # maintenance window retries.
        try:
            self.client.refresh_registration_secret()
            log.info("startup: registration secret refreshed before connections")
        except Exception as exc:  # noqa: BLE001 — startup must survive
            log.warning("startup rs refresh failed: %s", exc)
        try:
            # T-237: the startup snapshot must not abort the daemon start
            # (a broken active profile used to raise through here).
            self._write_status()
        except Exception:
            log.exception("startup status write failed")
        self._start_heartbeat_thread()
        self._start_probe_thread()
        # T-166 (F1): ephemeral file serve inside the daemon — the CLI
        # process dies after the stdout envelope, the serve endpoint must
        # survive. Mirrors the wiring from node_daemon.py (SSE daemon);
        # bind errors (port in use) = WARNING, the node keeps running
        # without serve (hp put bridge then reports the F5 error instead
        # of crashing).
        try:
            file_serve.set_on_exhausted(
                lambda route_path: file_serve.unregister_after_transfer(
                    self.client, route_path
                )
            )
            file_serve.start_serve_thread()
            log.info(
                "ephemeral file serve listening on %s:%d",
                file_serve.serve_host(),
                file_serve.serve_port(),
            )
        except (OSError, RuntimeError) as exc:
            log.warning("ephemeral file serve unavailable: %s", exc)
        try:
            self._claim_loop()
        finally:
            self._stop_event.set()
            if self._hb_thread and self._hb_thread.is_alive():
                self._hb_thread.join(timeout=5)
            try:
                self._write_status()
            except Exception:
                log.exception("final status write failed")
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
    proc = subprocess.Popen(
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


def _daemon_stop(args: argparse.Namespace) -> int:
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


def _daemon_status(args: argparse.Namespace) -> int:
    pid = _read_pid()
    running = pid is not None and _pid_running(pid)
    active_profile = current_profile_name()
    status_file: dict[str, Any] = {}
    if STATUS_PATH.exists():
        try:
            status_file = load_json(STATUS_PATH, default={}) or {}
        except Exception:  # noqa: BLE001 — fail-soft: corrupt status file renders as "-"/0
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


def _daemon_foreground(args: argparse.Namespace) -> int:
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
# node-daemon --mode poll entry (T-250)
# ---------------------------------------------------------------------------

def start_poll_daemon(
    client: RelayClient, cfg: dict[str, Any], own_name: str = "node-cli daemon"
) -> int:
    """Run the poll daemon in the caller's foreground (node-daemon --mode poll).

    Mirrors the guard/pid/run tail of ``_daemon_internal`` — the client is
    pre-built by ``node_daemon.main()``. The T-137b guard refuses to start
    while the SSE daemon (node-daemon.pid) is running; never write the SSE
    pid file from here.
    """
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    conflict = _check_other_daemon(own_name=own_name)
    if conflict:
        print(conflict, file=sys.stderr)
        return 1
    PID_PATH.write_text(str(os.getpid()) + "\n", encoding="utf-8")
    try:
        Daemon(client, cfg).run()
    finally:
        PID_PATH.unlink(missing_ok=True)
    return 0
