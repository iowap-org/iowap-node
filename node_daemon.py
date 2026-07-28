#!/usr/bin/env python3
"""node-daemon — SSE-driven reference daemon for the AI-Relay-Service.

A sibling to ``node-cli daemon`` that replaces the polling claim loop with
an SSE event stream. Instead of asking the scheduler for work every N
seconds, the daemon subscribes to ``/relay/v2/events/stream`` and reacts
to ``stage_claimed`` and ``task_created`` events:

* ``task_created``  — a new task is pending; the daemon attempts to
  claim a stage for any capability it advertises.
* ``stage_claimed`` — a stage was claimed (possibly by another node);
  the daemon checks whether the capability matches one of its own and,
  if so, claims + executes it.

``node-cli daemon`` remains unchanged; this module is a separate entry
point (``node-daemon``) that can run side-by-side without affecting the
existing daemon.

Architecture (threads)::

    Thread 1: Heartbeat  — identical to node-cli daemon (every 30s)
    Thread 2: SSE client — event stream with automatic reconnect
    Thread 3: Claim/Execute/Complete — triggered from SSE events
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from nodes.common.capability_loader import (
    current_profile_name,
    invalidate_active_cache,
    load_active_profile,
)
from nodes.common.handler_runner import run_handler
from nodes.common.node_cli import RelayClient, _effective_config, _setup_logging
from nodes.common.node_utils import (
    BASE_DIR,
    STATUS_PATH,
    TOKEN_PATH,
    load_meta,
    write_json_atomic,
)

# ---------------------------------------------------------------------------
# Paths (mirrors node_cli.py)
# ---------------------------------------------------------------------------

PID_PATH = BASE_DIR / "node-daemon.pid"
LOG_PATH = BASE_DIR / "node-daemon.log"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("node-daemon")


def _utcnow_str() -> str:
    return datetime.now(timezone.utc).isoformat()


# Event types the daemon is interested in.
_SUBSCRIBED_TYPES = "stage_claimed,task_created"

# Reconnect delay after a broken SSE connection.
_RECONNECT_DELAY = 5.0


class SseDaemon:
    """SSE-driven daemon: heartbeat thread + SSE event loop + execution.

    Mirrors ``node_cli.Daemon`` for the heartbeat and stage-execution
    logic but replaces the polling claim loop with an SSE client that
    reacts to ``stage_claimed`` / ``task_created`` events.
    """

    def __init__(self, client: RelayClient, cfg: dict[str, Any]) -> None:
        self.client = client
        self.cfg = cfg
        self._stop_event = threading.Event()
        self._sse_thread: threading.Thread | None = None
        self._hb_thread: threading.Thread | None = None
        self._in_flight: dict[str, int] = {}
        self._lock = threading.Lock()
        self._started_at = datetime.now(timezone.utc)
        self.tasks_completed = 0
        self.tasks_failed = 0
        self.last_heartbeat_status = "unknown"
        # T-060 mirror: per-task failure counter so the daemon stops
        # reclaiming stages for a task whose handler keeps failing.
        self._failed_tasks: dict[str, int] = {}

    # -- signal handling ---------------------------------------------------

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._on_term)
        signal.signal(signal.SIGINT, self._on_term)
        signal.signal(signal.SIGHUP, self._on_hup)

    def _on_term(self, *_: Any) -> None:
        log.info("received shutdown signal, stopping node-daemon …")
        self._stop_event.set()

    def _on_hup(self, *_: Any) -> None:
        log.info("received SIGHUP, invalidating capability cache")
        invalidate_active_cache()

    # -- status file -------------------------------------------------------

    def _write_status(self, error: str | None = None) -> None:
        caps = load_active_profile()
        status = {
            "pid": os.getpid(),
            "node_id": self.client.meta.get("node_id"),
            "daemon": "node-daemon",
            "started_at": self._started_at.isoformat(),
            "last_heartbeat": _utcnow_str(),
            "heartbeat_status": self.last_heartbeat_status,
            "active_profile": current_profile_name(),
            "token_present": bool(self.client.token),
            "capabilities": [
                {"name": c["name"], "claimable": c.get("claimable", False)} for c in caps
            ],
            "in_flight": dict(self._in_flight),
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "failed_tasks": dict(self._failed_tasks),
            "error": error,
        }
        try:
            write_json_atomic(STATUS_PATH, status)
        except OSError as exc:
            log.warning("could not write status file: %s", exc)

    # -- heartbeat thread --------------------------------------------------

    def _heartbeat_loop(self) -> None:
        """Heartbeat loop, identical to node-cli daemon."""
        interval = self.cfg["heartbeat_interval"]
        while not self._stop_event.is_set():
            error: str | None = None
            try:
                caps = load_active_profile()
                with self._lock:
                    inflight = dict(self._in_flight)
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
            for _ in range(max(1, interval)):
                if self._stop_event.is_set():
                    return
                time.sleep(1)

    def _start_heartbeat_thread(self) -> None:
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="heartbeat"
        )
        self._hb_thread.start()

    # -- SSE client --------------------------------------------------------

    def _stream_url(self) -> str:
        node_id = self.client.meta.get("node_id", "")
        return (
            f"{self.client.base_url}/relay/v2/events/stream"
            f"?node={node_id}&types={_SUBSCRIBED_TYPES}"
        )

    def _sse_loop(self) -> None:
        """Run the asyncio SSE client until ``_stop_event`` is set.

        The SSE client is async (httpx streaming), but the daemon is
        synchronous/threaded, so we run it inside ``asyncio.run`` in this
        dedicated thread.
        """
        try:
            asyncio.run(self._sse_loop_async())
        except Exception as exc:  # noqa: BLE001 — daemon must survive
            log.error("SSE loop crashed: %s", exc)

    async def _sse_loop_async(self) -> None:
        url = self._stream_url()
        headers = {"Authorization": f"Bearer {self.client.token}"}
        async with httpx.AsyncClient() as http:
            while not self._stop_event.is_set():
                try:
                    await self._consume_stream(http, url, headers)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — reconnect on any error
                    log.warning("SSE connection error: %s", exc)
                if self._stop_event.is_set():
                    return
                # Wait before reconnecting, but stay responsive to stop.
                log.info("SSE reconnecting in %.0fs …", _RECONNECT_DELAY)
                for _ in range(int(_RECONNECT_DELAY * 10)):
                    if self._stop_event.is_set():
                        return
                    await asyncio.sleep(0.1)

    async def _consume_stream(
        self,
        http: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
    ) -> None:
        async with http.stream(
            "GET", url, headers=headers, timeout=None
        ) as resp:
            resp.raise_for_status()
            log.info("SSE connected to %s", url)
            buffer: list[str] = []
            async for line in resp.aiter_lines():
                if self._stop_event.is_set():
                    return
                if line == "":
                    event = self._parse_sse(buffer)
                    buffer = []
                    if event is not None:
                        self._on_event(event)
                else:
                    buffer.append(line)

    @staticmethod
    def _parse_sse(lines: list[str]) -> dict[str, Any] | None:
        """Parse a single SSE message (one blank-line-delimited block)."""
        event_type = "message"
        data: str | None = None
        for line in lines:
            if line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: "):
                data = line[6:]
        if data is None:
            return None
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            payload = {"raw": data}
        return {"type": event_type, "data": payload}

    # -- event handling ----------------------------------------------------

    def _on_event(self, event: dict[str, Any]) -> None:
        """Dispatch an SSE event to the appropriate handler.

        Runs synchronously in the SSE thread. Stage execution may block,
        which pauses event consumption for its duration — this mirrors
        the sequential contract of ``node-cli daemon`` (see spec §15.2)
        and is intentional: max_parallel is enforced via ``_in_flight``.
        """
        event_type = event.get("type")
        payload = event.get("data") or {}
        if event_type == "stage_claimed":
            self._handle_stage_claimed(payload)
        elif event_type == "task_created":
            self._handle_task_created(payload)
        # Other event types are ignored (the stream is already filtered
        # server-side, but be defensive).

    def _handle_stage_claimed(self, payload: dict[str, Any]) -> None:
        """A stage was claimed somewhere in the cluster.

        If the capability matches one this node advertises, attempt to
        claim (and execute) it. The server atomically grants the claim
        to the first requester, so a race with another node is safe —
        the claim simply returns nothing.
        """
        capability = payload.get("capability")
        if not capability:
            return
        if not self._advertises_capability(capability):
            return
        self._try_claim_and_run(capability)

    def _handle_task_created(self, payload: dict[str, Any]) -> None:
        """A new task was created. Try to claim a stage for each
        claimable capability this node advertises."""
        caps = load_active_profile()
        for cap in caps:
            if not cap.get("claimable", False):
                continue
            name = cap["name"]
            with self._lock:
                inflight = self._in_flight.get(name, 0)
            if inflight >= int(cap.get("max_parallel", 1)):
                continue
            self._try_claim_and_run(name)

    def _try_claim_and_run(self, capability: str) -> None:
        max_retries = int(self.cfg.get("max_retries", 2))
        try:
            stage = self.client.claim(capability)
        except Exception as exc:  # noqa: BLE001 — never crash the SSE loop
            log.error("claim %s failed: %s", capability, exc)
            return
        if stage is None:
            return
        task_id = str(stage.get("task_id") or "")
        with self._lock:
            failures = self._failed_tasks.get(task_id, 0)
        if task_id and failures >= max_retries:
            log.warning(
                "skipping stage %s for task %s — %d failures >= max_retries %d",
                stage.get("stage_id"), task_id, failures, max_retries,
            )
            return
        cap = self._find_capability(capability)
        if cap is None:
            log.warning("claimed stage for unknown capability %s", capability)
            return
        self._run_stage(cap, stage)

    # -- claim/execute/complete (mirrors node-cli daemon) ------------------

    def _run_stage(self, cap: dict[str, Any], stage: dict[str, Any]) -> None:
        name = cap["name"]
        stage_id = stage.get("stage_id")
        task_id = stage.get("task_id")
        log.info("claimed %s stage %s (task %s)", name, stage_id, task_id)
        with self._lock:
            self._in_flight[name] = self._in_flight.get(name, 0) + 1
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
                self._in_flight[name] = max(0, self._in_flight.get(name, 1) - 1)

    # -- capability helpers ------------------------------------------------

    def _advertises_capability(self, name: str) -> bool:
        return self._find_capability(name) is not None

    def _find_capability(self, name: str) -> dict[str, Any] | None:
        for cap in load_active_profile():
            if cap.get("name") == name:
                return cap
        return None

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> None:
        self._install_signal_handlers()
        log.info(
            "node-daemon starting for node %s (base_url=%s)",
            self.client.meta.get("node_id"),
            self.client.base_url,
        )
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        self._write_status()
        self._start_heartbeat_thread()
        self._sse_thread = threading.Thread(
            target=self._sse_loop, daemon=True, name="sse"
        )
        self._sse_thread.start()
        try:
            while not self._stop_event.is_set():
                time.sleep(0.5)
        finally:
            self._stop_event.set()
            if self._sse_thread and self._sse_thread.is_alive():
                self._sse_thread.join(timeout=5)
            if self._hb_thread and self._hb_thread.is_alive():
                self._hb_thread.join(timeout=5)
            self._write_status()
            log.info("node-daemon stopped")

    def stop(self) -> None:
        """Request a graceful shutdown from outside the process."""
        self._stop_event.set()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="node-daemon",
        description="SSE-driven daemon for the AI-Relay-Service.",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="Log level (DEBUG/INFO/WARNING/ERROR). Default: env RELAY_LOG_LEVEL or INFO.",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Run in the foreground (default). Kept for parity with node-cli.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.log_level)
    try:
        meta = load_meta()
        cfg = _effective_config()
        client = RelayClient(meta, cfg)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()) + "\n", encoding="utf-8")
    try:
        SseDaemon(client, cfg).run()
    finally:
        PID_PATH.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())