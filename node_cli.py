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

    Updates (T-062):
        node-cli update check     — fetch origin, compare local vs. upstream
        node-cli update apply     — git pull + restart the node-cli service
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from nodes.common.capability_loader import (
    ACTIVE_PATH,
    BASE_DIR,
    PROFILES_DIR,
    CapabilityValidationError,
    current_profile_name,
    diff_profiles,
    invalidate_active_cache,
    list_profiles,
    load_active_profile,
    load_profile,
    profile_path,
    publish_profile,
    validate_profile,
)
from nodes.common.handler_runner import run_handler
from nodes.common.node_utils import (
    REPO_DIR,
    SERVICE_UNIT,
    STATUS_PATH,
    TOKEN_PATH,
    apply_update,
    check_for_updates,
    get_repo_info,
    load_config,
    load_json,
    load_meta,
    load_token,
    save_token,
    write_json_atomic,
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


def _setup_logging(level: str | None = None) -> None:
    if level is None:
        level = os.environ.get("RELAY_LOG_LEVEL", "INFO")
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s | %(levelname)-7s | node-cli | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _utcnow_str() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Config helpers (env-var aware)
# ---------------------------------------------------------------------------

def _effective_config() -> dict[str, Any]:
    """Return relay_config.json merged with env-var overrides."""
    cfg = load_config()
    base_url = os.environ.get("RELAY_BASE_URL")
    if base_url:
        cfg["base_url"] = base_url
    hb = os.environ.get("RELAY_HEARTBEAT_INTERVAL")
    if hb is not None:
        try:
            cfg["heartbeat_interval"] = int(hb)
        except ValueError:
            log.warning("ignoring invalid RELAY_HEARTBEAT_INTERVAL=%r", hb)
    ci = os.environ.get("RELAY_CLAIM_INTERVAL")
    if ci is not None:
        try:
            cfg["claim_interval"] = int(ci)
        except ValueError:
            log.warning("ignoring invalid RELAY_CLAIM_INTERVAL=%r", ci)
    mr = os.environ.get("RELAY_MAX_RETRIES")
    if mr is not None:
        try:
            cfg["max_retries"] = int(mr)
        except ValueError:
            log.warning("ignoring invalid RELAY_MAX_RETRIES=%r", mr)
    return cfg


def _base_url(meta: dict[str, Any], cfg: dict[str, Any]) -> str:
    url = cfg.get("base_url") or meta.get("base_url")
    if not url:
        raise SystemExit(
            "no base_url configured (set base_url in relay_config.json or RELAY_BASE_URL)"
        )
    return url.rstrip("/")


# ---------------------------------------------------------------------------
# Auth helpers (thin wrappers around poller functions)
# ---------------------------------------------------------------------------

class RelayClient:
    """Minimal HTTP client wrapping heartbeat / claim / complete / submit.

    Encapsulates token handling so the CLI subcommands and the daemon
    can share a single implementation. On 401/403 it attempts a token
    refresh exactly once.
    """

    def __init__(self, meta: dict[str, Any], cfg: dict[str, Any]) -> None:
        self.meta = meta
        self.cfg = cfg
        self.base_url = _base_url(meta, cfg)
        self.token = load_token()
        if not self.token:
            print(
                "no runtime token found, attempting recovery with registration secret",
                file=sys.stderr,
            )
            self.token = self._recover_runtime_token()
            if not self.token:
                raise SystemExit("no runtime token available and recovery failed")

    # -- low level ----------------------------------------------------------

    def _post(
        self, path: str, body: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> httpx.Response:
        return httpx.post(
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            json=body or {},
            timeout=timeout or self.cfg["request_timeout"],
        )

    def _get(
        self, path: str, *, timeout: float | None = None
    ) -> httpx.Response:
        return httpx.get(
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=timeout or self.cfg["request_timeout"],
        )

    def _get_with_retry(
        self, path: str, *, timeout: float | None = None
    ) -> httpx.Response:
        r = self._get(path, timeout=timeout)
        if r.status_code in (401, 403):
            log.warning("auth error %s on %s, refreshing token", r.status_code, path)
            if self._refresh_token():
                r = self._get(path, timeout=timeout)
        return r

    def _post_with_retry(
        self, path: str, body: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> httpx.Response:
        r = self._post(path, body, timeout=timeout)
        if r.status_code in (401, 403):
            log.warning("auth error %s on %s, refreshing token", r.status_code, path)
            if self._refresh_token():
                r = self._post(path, body, timeout=timeout)
        return r

    # -- token refresh -------------------------------------------------------

    def _refresh_token(self) -> bool:
        try:
            r = httpx.post(
                f"{self.base_url}/relay/v2/auth/refresh",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"requested_credential": "runtime_token"},
                timeout=self.cfg["request_timeout"],
            )
            if r.status_code == 200:
                new = r.json().get("token")
                if new:
                    save_token(new)
                    self.token = new
                    return True
        except Exception as exc:
            log.warning("runtime-token refresh failed: %s", exc)
        return self._recover_runtime_token() is not None

    def _recover_runtime_token(self) -> str | None:
        try:
            r = httpx.post(
                f"{self.base_url}/relay/v2/auth/refresh",
                json={
                    "node_id": self.meta["node_id"],
                    "requested_credential": "runtime_token",
                    "registration_secret": self.meta.get("registration_secret"),
                },
                timeout=self.cfg["request_timeout"],
            )
            r.raise_for_status()
            data = r.json()
            new = data.get("token")
            if new:
                save_token(new)
                self.token = new
            return new
        except Exception as exc:
            log.error("registration-secret recovery failed: %s", exc)
            return None

    # -- public API ----------------------------------------------------------

    def heartbeat(self, caps: list[dict[str, Any]], in_flight: dict[str, int]) -> dict[str, Any]:
        try:
            load_avg = os.getloadavg()[0]
            cpu_count = os.cpu_count() or 1
            load_pct = (load_avg / cpu_count) * 100.0
        except (OSError, AttributeError):
            cpu_count = 1
            load_pct = 0.0
        load_cap = float(self.cfg.get("load_cap", cpu_count * 100.0))
        load = min(load_pct, load_cap)

        cap_status: list[dict[str, Any]] = []
        for cap in caps:
            if not cap.get("auto_publish", True):
                continue
            name = cap["name"]
            inflight = in_flight.get(name, 0)
            entry: dict[str, Any] = {
                "name": name,
                "version": cap.get("version", "1.0.0"),
                "available": inflight < cap.get("max_parallel", 1),
                "dashboard_page": bool(cap.get("dashboard_page", False)),
            }
            # T-053: forward capability metadata so the server can
            # populate node_capabilities.{description,input_schema}
            # and resolve capability_details on claim/task-view
            # without an extra discovery round-trip. Omit fields that
            # are absent or falsy to keep the heartbeat payload small.
            if cap.get("type"):
                entry["type"] = cap.get("type")
            if cap.get("description"):
                entry["description"] = cap.get("description")
            if cap.get("input_schema"):
                entry["input_schema"] = cap.get("input_schema")
            cap_status.append(entry)

        queue_depth = sum(in_flight.values())
        r = self._post_with_retry(
            "/relay/v2/discovery/heartbeat",
            {
                "node_id": self.meta["node_id"],
                "status": "online",
                "available": True,
                "load": load,
                "queue_depth": queue_depth,
                "capabilities": cap_status,
            },
        )
        r.raise_for_status()
        return r.json()

    def claim(self, capability: str) -> dict[str, Any] | None:
        r = self._post_with_retry(
            "/relay/v2/scheduler/claim",
            {"capability": capability},
        )
        if r.status_code == 204:
            return None
        r.raise_for_status()
        data = r.json()
        if not data.get("claimed") or not data.get("stage"):
            return None
        return data["stage"]

    def complete(self, task_id: str, stage_id: str, result: dict[str, Any]) -> dict[str, Any]:
        r = self._post_with_retry(
            f"/relay/v2/scheduler/stages/{stage_id}/complete",
            {"node_id": self.meta["node_id"], "task_id": task_id, "result": result},
            timeout=self.cfg.get("task_timeout", 600),
        )
        r.raise_for_status()
        return r.json()

    def submit_simple_task(
        self,
        capability: str,
        payload: dict[str, Any],
        *,
        name: str = "",
        priority: int = 0,
        owner_node_id: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "capability": capability,
            "payload": payload,
            "name": name,
            "priority": priority,
        }
        if owner_node_id:
            body["owner_node_id"] = owner_node_id
        r = self._post_with_retry("/relay/v2/scheduler/task-simple", body)
        r.raise_for_status()
        return r.json()

    def get_task(self, task_id: str) -> dict[str, Any]:
        """Fetch task details including stages, artifacts, and notes."""
        r = self._get_with_retry(f"/relay/v2/scheduler/tasks/{task_id}")
        if r.status_code == 404:
            return {"error": "not found", "task_id": task_id}
        r.raise_for_status()
        return r.json()

    def add_task_note(self, task_id: str, message: str) -> dict[str, Any]:
        """Append a free-form note to a task (T-052 mini-chat)."""
        r = self._post_with_retry(
            f"/relay/v2/scheduler/tasks/{task_id}/notes",
            {"message": message},
        )
        r.raise_for_status()
        return r.json()

    # -- artifact download ---------------------------------------------------

    def download_artifact(
        self,
        artifact_id: str,
        output_path: Optional[Path] = None,
        *,
        chunk_size: int = 64 * 1024,
    ) -> Path:
        """Download an artifact by id, streaming it to disk chunkwise.

        Falls back to a token refresh on a 401/403, then retries once. The
        output filename is derived from the Content-Disposition header when
        no ``output_path`` is supplied.
        """
        url = f"{self.base_url}/relay/v2/storage/files/{artifact_id}"
        timeout = self.cfg.get("request_timeout", 30)

        cm = httpx.stream(
            "GET",
            url,
            headers={"Authorization": f"Bearer {self.token}"},
            follow_redirects=True,
            timeout=timeout,
        )
        resp = cm.__enter__()
        try:
            if resp.status_code in (401, 403):
                # Close this attempt and retry once after refreshing the token.
                cm.__exit__(None, None, None)
                refreshed = self._refresh_token()
                cm = httpx.stream(
                    "GET",
                    url,
                    headers={"Authorization": f"Bearer {self.token}"},
                    follow_redirects=True,
                    timeout=timeout,
                )
                resp = cm.__enter__()
                if not refreshed:
                    resp.raise_for_status()  # surface the auth error
            resp.raise_for_status()

            target = output_path or Path(_filename_from_response(resp, artifact_id))
            with target.open("wb") as f:
                for chunk in resp.iter_bytes(chunk_size=chunk_size):
                    f.write(chunk)
            return target
        finally:
            cm.__exit__(None, None, None)

    # -- artifact upload -----------------------------------------------------

    def upload_artifact(
        self,
        file_path: Path,
        *,
        name: Optional[str] = None,
        task_id: Optional[str] = None,
        stage_id: Optional[str] = None,
        capability: Optional[str] = None,
    ) -> dict[str, Any]:
        """Upload a local file to the relay as an artifact.

        Returns the server response dict containing ``artifact_id``,
        ``name``, ``size_bytes``, etc. Falls back to a token refresh
        on a 401/403, then retries once.

        When ``capability`` is set, the server stores the file as the
        dashboard page for that capability (no artifact DB entry); the
        response then contains ``status`` and ``path`` instead.
        """
        url = f"{self.base_url}/relay/v2/storage/upload"
        params: dict[str, str] = {}
        if task_id:
            params["task_id"] = task_id
        if stage_id:
            params["stage_id"] = stage_id
        if capability:
            params["capability"] = capability

        file_path = Path(file_path)
        upload_name = name or file_path.name

        def _do_upload() -> httpx.Response:
            with file_path.open("rb") as f:
                return httpx.post(
                    url,
                    headers={"Authorization": f"Bearer {self.token}"},
                    files={"file": (upload_name, f, "application/octet-stream")},
                    params=params or None,
                    timeout=self.cfg.get("request_timeout", 30),
                )

        resp = _do_upload()
        if resp.status_code in (401, 403):
            self._refresh_token()
            resp = _do_upload()
        resp.raise_for_status()
        return resp.json()


def _filename_from_response(response: httpx.Response, fallback: str) -> str:
    """Extract a filename from Content-Disposition, falling back to the id."""
    cd = response.headers.get("content-disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    return m.group(1) if m else fallback


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
            # Sleep in 1s increments so shutdown is responsive.
            for _ in range(max(1, interval)):
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
            for _ in range(max(1, interval)):
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
    if not PID_PATH.exists():
        return None
    try:
        return int(PID_PATH.read_text().strip())
    except (ValueError, OSError):
        return None


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


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


def _daemon_internal() -> int:
    """Inner entry point for the self-spawned daemon process."""
    _setup_logging(os.environ.get("RELAY_LOG_LEVEL"))
    BASE_DIR.mkdir(parents=True, exist_ok=True)
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

def _cmd_heartbeat(args: argparse.Namespace) -> int:
    _setup_logging(args.log_level)
    meta = load_meta()
    cfg = _effective_config()
    client = RelayClient(meta, cfg)
    caps = load_active_profile()
    hb = client.heartbeat(caps, {})
    print(json.dumps(hb, indent=2, default=str))
    return 0


def _cmd_claim(args: argparse.Namespace) -> int:
    _setup_logging(args.log_level)
    meta = load_meta()
    cfg = _effective_config()
    client = RelayClient(meta, cfg)
    stage = client.claim(args.capability)
    if stage is None:
        print(json.dumps({"claimed": False}))
        return 0
    # T-053: surface resolved capability metadata when the server
    # included capability_details on the claim response.
    print(json.dumps({"claimed": True, "stage": stage}, indent=2, default=str))
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


def _cmd_complete(args: argparse.Namespace) -> int:
    _setup_logging(args.log_level)
    meta = load_meta()
    cfg = _effective_config()
    client = RelayClient(meta, cfg)
    if not Path(args.result_file).exists():
        print(f"result file not found: {args.result_file}", file=sys.stderr)
        return 2
    try:
        result = json.loads(Path(args.result_file).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"result file is not valid JSON: {exc}", file=sys.stderr)
        return 2
    resp = client.complete(args.task, args.stage_id, result)
    print(json.dumps(resp, indent=2, default=str))
    return 0


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


def _cmd_task_submit(args: argparse.Namespace) -> int:
    _setup_logging(args.log_level)
    meta = load_meta()
    cfg = _effective_config()
    client = RelayClient(meta, cfg)
    cap, payload = _parse_stage_arg(args.stage)
    resp = client.submit_simple_task(
        cap,
        payload,
        name=args.name or "",
        priority=args.priority,
        owner_node_id=args.owner,
    )
    print(json.dumps(resp, indent=2, default=str))
    return 0


def _cmd_task_result(args: argparse.Namespace) -> int:
    _setup_logging(args.log_level)
    meta = load_meta()
    cfg = _effective_config()
    client = RelayClient(meta, cfg)
    data = client.get_task(args.task_id)
    if "error" in data:
        print(f"Task {args.task_id}: {data['error']}", file=sys.stderr)
        return 1
    _print_task_result(data)
    return 0


def _cmd_task_note(args: argparse.Namespace) -> int:
    """node-cli task note <task_id> <message> — append a note to a task."""
    _setup_logging(args.log_level)
    meta = load_meta()
    cfg = _effective_config()
    client = RelayClient(meta, cfg)
    try:
        data = client.add_task_note(args.task_id, args.message)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            print(f"Task {args.task_id} not found", file=sys.stderr)
            return 1
        print(f"Error: {exc.response.status_code} {exc.response.text}", file=sys.stderr)
        return 1
    print(f"✅ Note added to task {data.get('task_id', args.task_id)}")
    print(f"   {data.get('message', '')} ({data.get('created_at', '')})")
    return 0


def _cmd_task_wait(args: argparse.Namespace) -> int:
    _setup_logging(args.log_level)
    meta = load_meta()
    cfg = _effective_config()
    client = RelayClient(meta, cfg)
    task_id = args.task_id
    interval = max(1, args.interval)

    import time

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


def _cmd_artifact_download(args: argparse.Namespace) -> int:
    _setup_logging(args.log_level)
    meta = load_meta()
    cfg = _effective_config()
    client = RelayClient(meta, cfg)
    target = client.download_artifact(args.artifact_id, output_path=args.output)
    size = target.stat().st_size if target.exists() else 0
    print(f"Downloaded {size} bytes to {target}")
    return 0


def _cmd_artifact_upload(args: argparse.Namespace) -> int:
    _setup_logging(args.log_level)
    meta = load_meta()
    cfg = _effective_config()
    client = RelayClient(meta, cfg)
    file_path = Path(args.file)
    if not file_path.exists():
        print(f"file not found: {file_path}", file=sys.stderr)
        return 2
    result = client.upload_artifact(
        file_path,
        name=args.name,
        task_id=args.task_id,
        stage_id=args.stage_id,
        capability=args.capability,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


# ---------------------------------------------------------------------------
# docs (T-059)
# ---------------------------------------------------------------------------

def _html_to_text(html: str) -> str:
    """Best-effort conversion of an HTML document to terminal-friendly text.

    The docs endpoint serves rendered Markdown as HTML. On a headless node
    we want something readable in the terminal, so we strip tags, expand
    block elements to newlines, decode the common entities, and collapse
    excess blank lines. This is intentionally simple — it does not aim to
    reproduce a full browser.
    """
    import html as html_mod
    import re as _re

    # Drop <head>…</head> (style/title) entirely.
    html = _re.sub(r"<head\b.*?</head>", "", html, flags=_re.S | _re.I)
    # Drop <style>…</style> and <script>…</script>.
    html = _re.sub(r"<(style|script)\b.*?</\1>", "", html, flags=_re.S | _re.I)
    # Block-level elements → surrounding newlines.
    block_tags = (
        "p", "br", "div", "section", "article", "header", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "ul", "ol", "li", "pre", "blockquote", "table", "tr",
    )
    html = _re.sub(
        rf"</?({ '|'.join(block_tags) })\b[^>]*>",
        "\n",
        html,
        flags=_re.I,
    )
    # <td>/<th> → tab separator, <hr> → rule.
    html = _re.sub(r"</?(td|th)\b[^>]*>", "\t", html, flags=_re.I)
    html = _re.sub(r"<hr\b[^>]*/?>", "\n----\n", html, flags=_re.I)
    # Code spans keep their text only.
    html = _re.sub(r"</?code\b[^>]*>", "", html, flags=_re.I)
    # Strip all remaining tags.
    html = _re.sub(r"<[^>]+>", "", html)
    # Decode HTML entities (&amp; &lt; …).
    html = html_mod.unescape(html)
    # Collapse runs of whitespace inside lines, keep newlines.
    html = _re.sub(r"[ \t]+", " ", html)
    html = _re.sub(r" *\n *", "\n", html)
    # Trim leading whitespace per line.
    html = "\n".join(line.rstrip() for line in html.splitlines())
    # Collapse 3+ blank lines to 2.
    html = _re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def _cmd_docs(args: argparse.Namespace) -> int:
    """node-cli docs [<name>] — read relay documentation from the server.

    Without an argument, lists all public documents (name + URL).
    With a name, fetches the document and prints it as readable text.
    """
    _setup_logging(args.log_level)
    meta = load_meta()
    cfg = _effective_config()
    client = RelayClient(meta, cfg)
    try:
        if args.name:
            resp = client._get_with_retry(f"/relay/v2/docs/{args.name}")
            if resp.status_code == 404:
                print(f"Document '{args.name}' not found.", file=sys.stderr)
                return 1
            resp.raise_for_status()
            body = resp.text
            ctype = resp.headers.get("content-type", "")
            if "html" in ctype.lower() or body.lstrip().lower().startswith("<!doctype"):
                print(_html_to_text(body))
            else:
                # Server returned JSON with content/markdown, or raw text.
                try:
                    data = resp.json()
                    if isinstance(data, dict):
                        text = data.get("content") or data.get("markdown")
                        if text:
                            print(text)
                            return 0
                    print(json.dumps(data, indent=2, default=str))
                except (json.JSONDecodeError, ValueError):
                    print(body)
            return 0

        # List all docs.
        resp = client._get_with_retry("/relay/v2/docs")
        resp.raise_for_status()
        data = resp.json()
        docs = data if isinstance(data, list) else data.get("docs", [])
        print(f"Relay documentation ({len(docs)} pages):\n")
        for doc in docs:
            name = doc.get("name") or doc.get("title", "?")
            url = doc.get("url", "")
            available = doc.get("available", True)
            marker = "📄" if available else "🚫"
            print(f"  {marker} {name}")
            if url:
                print(f"     {url}")
            print()
        return 0
    except httpx.HTTPStatusError as exc:
        print(
            f"docs request failed: {exc.response.status_code} {exc.response.text}",
            file=sys.stderr,
        )
        return 1


# ---------------------------------------------------------------------------
# update (T-062): check for and apply git-based updates
# ---------------------------------------------------------------------------

def _cmd_update_check(args: argparse.Namespace) -> int:
    """node-cli update check — fetch origin and compare local vs. upstream."""
    _setup_logging(args.log_level)
    info = check_for_updates()
    print(f"Repo:           {REPO_DIR}")
    print(f"Local commit:   {info.get('local_commit') or '-'}")
    print(f"Local branch:   {info.get('local_branch') or '-'}")
    print(f"Upstream:       {'yes' if info.get('has_upstream') else 'no (not configured)'}")
    print(f"Remote commit:  {info.get('remote_commit') or '-'}")
    behind = info.get("behind_count", 0)
    if not info.get("has_upstream"):
        print("Status:         no upstream configured — cannot determine updates")
        return 1
    if behind > 0:
        print(f"Status:         {behind} commit{'s' if behind != 1 else ''} behind — update available")
        return 0
    print("Status:         up to date")
    return 0


def _cmd_update_apply(args: argparse.Namespace) -> int:
    """node-cli update apply — git pull + restart the systemd service."""
    _setup_logging(args.log_level)
    result = apply_update(service_unit=args.service_unit)
    print(f"Before: {result.get('before_commit') or '-'}")
    print(f"After:  {result.get('after_commit') or '-'}")
    print(f"Restarted: {'yes' if result.get('restarted') else 'no'}")
    print(f"Result:  {result.get('message')}")
    return 0 if result.get("success") else 1


# ---------------------------------------------------------------------------
# capabilities subcommands
# ---------------------------------------------------------------------------

def _cmd_capabilities_list(args: argparse.Namespace) -> int:  # noqa: ARG001
    profiles = list_profiles()
    if not profiles:
        print("(no profiles in %s)" % PROFILES_DIR)
        return 0
    active = current_profile_name()
    for p in profiles:
        marker = "*" if active and (p.stem == active or p.name == active) else " "
        print(f"{marker} {p.stem}")
    return 0


def _cmd_capabilities_validate(args: argparse.Namespace) -> int:
    _setup_logging(args.log_level)
    target = args.profile
    if target is None:
        if not ACTIVE_PATH.exists():
            print("no active profile and no profile name given", file=sys.stderr)
            return 1
        path = ACTIVE_PATH
        target = "active"
    else:
        path = profile_path(target)
    try:
        caps = validate_profile(path)
    except CapabilityValidationError as exc:
        print(f"INVALID {target}: {exc}", file=sys.stderr)
        return 1
    print(f"OK {target} ({len(caps)} capability{'ies' if len(caps) != 1 else ''})")
    for c in caps:
        print(f"  - {c['name']} v{c['version']} "
              f"auto_publish={c['auto_publish']} claimable={c['claimable']} "
              f"max_parallel={c['max_parallel']} timeout={c['timeout']}")
    return 0


def _cmd_capabilities_publish(args: argparse.Namespace) -> int:
    _setup_logging(args.log_level)
    try:
        active = publish_profile(args.profile)
    except CapabilityValidationError as exc:
        print(f"publish FAILED: {exc}", file=sys.stderr)
        return 1
    # Best-effort SIGHUP to running daemon.
    pid = _read_pid()
    if pid is not None and _pid_running(pid):
        try:
            os.kill(pid, signal.SIGHUP)
            print(f"published '{args.profile}' -> {active} (sent SIGHUP to pid {pid})")
        except OSError as exc:
            print(f"published '{args.profile}' -> {active} (SIGHUP failed: {exc})", file=sys.stderr)
    else:
        print(f"published '{args.profile}' -> {active} (daemon not running)")
    return 0


def _cmd_capabilities_diff(args: argparse.Namespace) -> int:
    _setup_logging(args.log_level)
    if args.profile is None:
        if not ACTIVE_PATH.exists():
            print("no active profile and no profile name given", file=sys.stderr)
            return 1
        working_path = ACTIVE_PATH
        working_label = "active"
    else:
        working_path = profile_path(args.profile)
        working_label = args.profile
    try:
        working = load_profile(working_path)
    except CapabilityValidationError as exc:
        print(f"working profile invalid: {exc}", file=sys.stderr)
        return 1
    if ACTIVE_PATH.exists():
        try:
            active = load_profile(ACTIVE_PATH)
        except CapabilityValidationError as exc:
            print(f"active profile invalid: {exc}", file=sys.stderr)
            return 1
    else:
        active = []
    diff = diff_profiles(active, working)
    if not diff["added"] and not diff["removed"] and not diff["changed"]:
        print(f"no differences between active and {working_label}")
        return 0
    print(f"diff active -> {working_label}:")
    for c in diff["added"]:
        print(f"+ {c['name']} v{c['version']}")
    for name in diff["removed"]:
        print(f"- {name}")
    for ch in diff["changed"]:
        print(f"~ {ch['name']}:")
        _print_cap_diff(ch["old"], ch["new"])
    return 0


def _print_cap_diff(old: dict[str, Any], new: dict[str, Any]) -> None:
    keys = ("version", "auto_publish", "claimable", "handler", "max_parallel", "timeout")
    for k in keys:
        ov, nv = old.get(k), new.get(k)
        if ov != nv:
            print(f"    {k}: {ov!r} -> {nv!r}")


def _cmd_capabilities_current(args: argparse.Namespace) -> int:  # noqa: ARG001
    name = current_profile_name()
    if name is None:
        print("(no active profile set)")
        return 1
    print(name)
    return 0


def _cmd_capabilities_server(args: argparse.Namespace) -> int:
    """Query capabilities from the relay server (all registered nodes)."""
    _setup_logging(args.log_level)
    meta = load_meta()
    cfg = _effective_config()
    client = RelayClient(meta, cfg)
    try:
        resp = client._get("/relay/v2/discovery/capabilities")
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"failed to query server capabilities: {exc}", file=sys.stderr)
        return 1

    caps = data.get("capabilities", data) if isinstance(data, dict) else data
    if not caps:
        print("(no capabilities registered on the server)")
        return 0

    print(f"Server capabilities ({len(caps)} total):\n")
    for c in caps:
        name = c.get("name", "?")
        ver = c.get("version", "?")
        avail = c.get("available", False)
        nodes = c.get("nodes", [])
        status = "✅" if avail else "❌"
        node_names = ", ".join(
            n.get("node_name", n.get("node_id", "?")) for n in nodes
        ) if nodes else "(no nodes)"
        print(f"  {status} {name:20} v{ver:8}  [{node_names}]")
        desc = c.get("description")
        if desc:
            print(f"     {desc}")
        schema = c.get("input_schema")
        if schema:
            print(f"     Input: {json.dumps(schema, indent=6)}")
        print()
    return 0


def _cmd_capabilities_info(args: argparse.Namespace) -> int:
    """Show detailed info for a single capability registered on the relay."""
    _setup_logging(args.log_level)
    meta = load_meta()
    cfg = _effective_config()
    client = RelayClient(meta, cfg)
    try:
        resp = client._get(f"/relay/v2/discovery/capabilities/{args.name}")
        if resp.status_code == 404:
            print(f"Capability '{args.name}' not found.")
            return 1
        resp.raise_for_status()
        cap = resp.json()
    except Exception as exc:
        print(f"failed to query capability: {exc}", file=sys.stderr)
        return 1

    print(f"Name:        {cap.get('name', '?')}")
    print(f"Type:        {cap.get('type', '-')}")
    print(f"Version:     {cap.get('version', '?')}")
    print(f"Available:   {'yes' if cap.get('available', False) else 'no'}")
    desc = cap.get("description")
    if desc:
        print(f"Description: {desc}")
    schema = cap.get("input_schema")
    if schema:
        print("Input Schema:")
        print(json.dumps(schema, indent=2))
    nodes = cap.get("nodes", [])
    if nodes:
        print(f"\nNodes ({len(nodes)}):")
        for n in nodes:
            print(
                f"  - {n.get('node_name', n.get('node_id', '?'))} "
                f"(load={n.get('load', 0):.1f}, "
                f"queue={n.get('queue_depth', 0)})"
            )
    return 0


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
    p_submit.set_defaults(func=_cmd_task_submit)

    p_result = p_task_sub.add_parser("result", help="Show task result (status, stages, artifacts).")
    p_result.add_argument("task_id", help="Task ID to query.")
    p_result.set_defaults(func=_cmd_task_result)

    p_wait = p_task_sub.add_parser("wait", help="Wait for a task to complete and show result.")
    p_wait.add_argument("task_id", help="Task ID to wait for.")
    p_wait.add_argument("--interval", type=int, default=5, help="Poll interval in seconds (default: 5).")
    p_wait.set_defaults(func=_cmd_task_wait)

    p_note = p_task_sub.add_parser("note", help="Append a free-form note to a task (T-052 mini-chat).")
    p_note.add_argument("task_id", help="Task ID to add a note to.")
    p_note.add_argument("message", help="Note text (1..2000 characters).")
    p_note.set_defaults(func=_cmd_task_note)

    # capabilities
    p_caps = sub.add_parser("capabilities", help="Capability profile management.")
    p_caps_sub = p_caps.add_subparsers(
        dest="capabilities_command", required=True, metavar="<action>"
    )

    p_list = p_caps_sub.add_parser("list", help="List profiles in capabilities.d/.")
    p_list.set_defaults(func=_cmd_capabilities_list)

    p_validate = p_caps_sub.add_parser("validate", help="Validate a profile (default: active).")
    p_validate.add_argument(
        "profile", nargs="?", default=None, help="Profile name (default: active)."
    )
    p_validate.set_defaults(func=_cmd_capabilities_validate)

    p_publish = p_caps_sub.add_parser(
        "publish", help="Validate + atomically copy profile to active."
    )
    p_publish.add_argument("profile", help="Profile name to publish.")
    p_publish.set_defaults(func=_cmd_capabilities_publish)

    p_diff = p_caps_sub.add_parser("diff", help="Diff working profile vs active.")
    p_diff.add_argument("profile", nargs="?", default=None, help="Profile name (default: active).")
    p_diff.set_defaults(func=_cmd_capabilities_diff)

    p_current = p_caps_sub.add_parser("current", help="Show active profile name.")
    p_current.set_defaults(func=_cmd_capabilities_current)

    p_server = p_caps_sub.add_parser(
        "server", help="Query capabilities registered on the relay server (all nodes)."
    )
    p_server.set_defaults(func=_cmd_capabilities_server)

    p_info = p_caps_sub.add_parser(
        "info", help="Show detailed info for a single capability registered on the relay."
    )
    p_info.add_argument("name", help="Capability name to query.")
    p_info.set_defaults(func=_cmd_capabilities_info)

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
    p_artifact_download.set_defaults(func=_cmd_artifact_download)

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
    p_artifact_upload.add_argument(
        "--capability",
        default=None,
        help="If set, store the file as the dashboard page for this capability "
        "(in ~/.relay/capability-pages/<name>/dashboard.html on the server). "
        "No artifact DB entry is created.",
    )
    p_artifact_upload.set_defaults(func=_cmd_artifact_upload)

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
    p_docs.set_defaults(func=_cmd_docs)

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
    p_update_check.set_defaults(func=_cmd_update_check)

    p_update_apply = p_update_sub.add_parser(
        "apply",
        help="Pull the latest commits and restart the node-cli service.",
    )
    p_update_apply.add_argument(
        "--service-unit",
        default=SERVICE_UNIT,
        help=f"systemd user unit to restart (default: {SERVICE_UNIT}).",
    )
    p_update_apply.set_defaults(func=_cmd_update_apply)

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
