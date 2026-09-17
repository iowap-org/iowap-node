#!/usr/bin/env python3
"""node-daemon — SSE-driven reference daemon for IOWAP.

A sibling to ``node-cli daemon`` that replaces the polling claim loop with
an SSE event stream. Instead of asking the scheduler for work every N
seconds, the daemon subscribes to ``/relay/v2/events/stream`` and reacts
to ``stage_claimed`` and ``task_created`` events:

* ``task_created``  — a new task is pending; the daemon attempts to
  claim a stage for any capability it advertises.
* ``stage_claimed`` — a stage was claimed (possibly by another node);
  the daemon checks whether the capability matches one of its own and,
  if so, claims + executes it.

Backfill (T-c51219ee): ``task_created`` is a one-shot event, so a task
that became pending before the SSE connection existed — or that lost
its initial claim race — would stay ``pending`` forever. After every
successful SSE connect the daemon sweeps all claimable capabilities
once, and a periodic ticker (``backfill_interval``, default 60s,
``RELAY_BACKFILL_INTERVAL``) repeats the sweep as a safety net. The
sweep reuses the ``max_parallel``/``_in_flight`` guard from the event
handler; claims are idempotent (the server grants them atomically).

``node-cli daemon`` remains unchanged; this module is a separate entry
point (``node-daemon``) that can run side-by-side without affecting the
existing daemon.

Architecture (threads)::

    Thread 1: Heartbeat  — identical to node-cli daemon (every 30s)
    Thread 2: SSE client — event stream with automatic reconnect
    Thread 3: Claim/Execute/Complete — triggered from SSE events
    Thread 4: Backfill ticker — periodic claim sweep (T-c51219ee)
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from nodes.common import file_serve
from nodes.common.handler_runner import run_handler
from nodes.common.node_config import (
    current_profile_name,
    invalidate_active_cache,
    load_active_profile,
)
from nodes.common.node_utils import (
    BASE_DIR,
    STATUS_PATH,
    TOKEN_PATH,
    load_meta,
    write_json_atomic,
)
from nodes.common.relay_client import RelayClient, _effective_config, _setup_logging

# ---------------------------------------------------------------------------
# Paths (mirrors node_cli.py)
# ---------------------------------------------------------------------------

PID_PATH = BASE_DIR / "node-daemon.pid"
LOG_PATH = BASE_DIR / "node-daemon.log"

# T-137b-Guard: the polling daemon's PID file. Both daemons must never run
# at the same time as the same node (token fight + task race).
_POLLING_PID_PATH = BASE_DIR / "node-cli.pid"


def _check_other_daemon(
    base_dir: Path | None = None,
    other_pid_path: Path | None = None,
    own_name: str = "node-daemon",
    other_name: str = "node-cli daemon",
) -> str | None:
    """Return an error message if the polling daemon is running, else None."""
    from nodes.common import node_utils as _nu

    pid_file = other_pid_path or (base_dir or BASE_DIR) / "node-cli.pid"
    pid = _nu.read_pid(pid_file)
    if pid is not None and _nu.pid_running(pid):
        return (
            f"{own_name} refuses to start: {other_name} is already running "
            f"(pid {pid}, {pid_file}). Stop it first — running both as the "
            f"same node causes token fights and duplicate claims (T-137b)."
        )
    return None

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("node-daemon")


def _utcnow_str() -> str:
    return datetime.now(timezone.utc).isoformat()


# Long-Run lease budget on the relay (T-154): an accepted stage stays
# alive for this long as long as progress notes keep resetting the TTL.
# A long_run handler process gets the same budget so it is not killed by
# the short per-stage timeout (T-163).
_LONGRUN_HANDLER_TIMEOUT = 2 * 3600  # 2h


def _handler_timeout(cap: dict[str, Any]) -> int:
    """Return the subprocess timeout for a capability's handler.

    A ``long_run`` capability (archive/extract) must not be killed by the
    short per-stage timeout (default 300s) — the relay's Long-Run lease
    keeps the stage ``accepted`` for up to 2h as long as progress notes
    keep arriving, so the handler gets the same 2h budget. Non-long-run
    capabilities keep their configured timeout.
    """
    if cap.get("long_run"):
        return _LONGRUN_HANDLER_TIMEOUT
    return int(cap.get("timeout", 300))


# Event types the daemon is interested in.
_SUBSCRIBED_TYPES = "stage_claimed,task_created"

# Backfill (T-c51219ee): the daemon is purely SSE-event-driven and
# ``task_created`` is a one-shot event — a task that became pending
# before the SSE connection existed (daemon start, reload, network
# blip) or that lost its initial claim race never triggers another
# event and stays ``pending`` forever. Two safety nets fix this:
#
# * a claim sweep over every claimable capability right after a
#   successful SSE connect (``_consume_stream``), and
# * a periodic ticker that repeats the sweep every
#   ``backfill_interval`` seconds while connected.
#
# A claim attempt is idempotent and cheap (the server atomically
# grants the claim to the first requester; a null claim is a 204), so
# an extra sweep cannot hurt. Default: 60s (override via
# RELAY_BACKFILL_INTERVAL env or ``backfill_interval`` in
# relay_config.json; 0 disables the ticker).
_DEFAULT_BACKFILL_INTERVAL = 60


def _cfg_int(cfg: dict[str, Any], key: str, default: int) -> int:
    """Read an integer from cfg, falling back to ``default`` on garbage."""
    raw = cfg.get(key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        log.warning("ignoring invalid %s=%r — using %d", key, raw, default)
        return default


def _backfill_interval(cfg: dict[str, Any]) -> int:
    """Effective backfill sweep interval in seconds (0 = ticker off)."""
    env = os.environ.get("RELAY_BACKFILL_INTERVAL")
    if env is not None:
        try:
            return int(env)
        except ValueError:
            log.warning(
                "ignoring invalid RELAY_BACKFILL_INTERVAL=%r", env
            )
    return _cfg_int(cfg, "backfill_interval", _DEFAULT_BACKFILL_INTERVAL)

# Reconnect delay after a broken SSE connection.
_RECONNECT_DELAY = 5.0

# T-186: SSE timeouts — timeout=None disabled BOTH connect and read
# timeouts: with the server down, each reconnect attempt hung ~2min in
# SYN retransmits; after a hard server kill (no EOF) the connection
# became a TCP zombie and never reconnected. Read timeout is 3× the
# server's SSE ping interval (T-207 iowap-server, default 20s).
_SSE_CONNECT_TIMEOUT = 5.0
_SSE_READ_TIMEOUT = 60.0
_SSE_STREAM_TIMEOUT = httpx.Timeout(
    connect=_SSE_CONNECT_TIMEOUT,
    write=5.0,
    read=_SSE_READ_TIMEOUT,
    pool=5.0,
)

# T-179 Task 5: the relay's 404 detail when a stage is already completed
# (iowap-server api/v2/scheduler.py, complete endpoint).
_ALREADY_COMPLETED_DETAIL = "not claimed by this node, or not in claimed status"


def _is_already_completed(exc: BaseException) -> bool:
    """Return True if ``exc`` is the relay's 404 'already completed' answer.

    Used by the ``complete_by_script`` opt-in (T-179 Task 5): when a
    handler script completes the stage itself, the daemon's fallback
    ``complete`` call is rejected with this exact 404 detail — that is a
    success, not a failure.
    """
    response = getattr(exc, "response", None)
    if response is None or getattr(response, "status_code", None) != 404:
        return False
    try:
        detail = response.json().get("detail", "")
    except Exception:  # noqa: BLE001 — non-JSON body can never match
        return False
    return _ALREADY_COMPLETED_DETAIL in detail


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
        # T-185: quiescence window. SET = normal operation, CLEAR = a
        # credential-maintenance window is open: the SSE loop drops its
        # active connection (the server sends no keepalive —
        # core/events.py) and holds reconnects; the heartbeat thread is
        # the window owner and pauses itself while rotating.
        self._quiesce = threading.Event()
        self._quiesce.set()
        # T-177: last server probe result (health/ready/metrics), written by
        # the probe thread and merged into the status file by _write_status.
        self.server_probe: dict[str, Any] = {"ok": False, "error": "probe pending"}
        self._probe_thread: threading.Thread | None = None
        self._in_flight: dict[str, int] = {}
        self._lock = threading.Lock()
        self._started_at = datetime.now(timezone.utc)
        self.tasks_completed = 0
        self.tasks_failed = 0
        self.last_heartbeat_status = "unknown"
        # T-060 mirror: per-task failure counter so the daemon stops
        # reclaiming stages for a task whose handler keeps failing.
        self._failed_tasks: dict[str, int] = {}
        # Backfill ticker (T-c51219ee): periodic claim sweep while the
        # SSE stream is connected. Started once by the first successful
        # SSE connect; the sweep itself must NOT fire while the ticker
        # thread races a live stream on another thread — therefore the
        # ticker is armed only from the SSE thread.
        self._backfill_thread: threading.Thread | None = None
        self._backfill_armed = threading.Event()

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
        # T-108: Loop-Detection — wenn der Daemon in einem Auth-Fehler-Loop
        # festhängt (Backoff > 0 + 401/403), reichern wir den Fehler-String
        # an und markieren den Status als degraded. Zentrale Logik liegt
        # im geteilten RelayClient (greift für beide Daemons).
        backoff = self.client._current_backoff()
        auth_loop = backoff > 0 and "401" in (error or "")
        if auth_loop:
            error = (error or "") + " | AUTH-LOOP: token invalid — Datei prüfen oder Daemon neu starten"
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
            "auth_loop": auth_loop,
            "auth_backoff_seconds": backoff,
            "server": self.server_probe,
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
                # T-182/T-183: fixed-interval credential maintenance with
                # claims paused. T-185: the rotation runs inside a
                # quiescence window (SSE dropped + reconnects held) and
                # ONLY when a rotation is actually due — an idle window on
                # every 8 s tick would drop SSE for nothing.
                if self.client.maintenance_due():
                    self._run_maintenance_window()
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
            # T-108: Backoff nach wiederholten Auth-Fehlschlägen (zentral
            # im RelayClient). SSE-Reconnect bleibt separat via
            # _RECONNECT_DELAY, da er andere Fehlerursachen abdeckt.
            sleep_interval = interval + self.client._current_backoff()
            for _ in range(max(1, int(sleep_interval))):
                if self._stop_event.is_set():
                    return
                time.sleep(1)

    def _run_maintenance_window(self) -> None:
        """Open the quiescence window around one maintenance tick (T-185).

        Window owner = the heartbeat thread. While the window is open the
        SSE loop cancels its active stream and holds reconnects, so the
        rotation never runs against a live connection presenting the old
        token. The window ALWAYS re-opens (finally), even on failure —
        a wedged daemon must not outlive one bad tick.
        """
        log.info("maintenance window open — dropping SSE, pausing rotation")
        self._quiesce.clear()
        try:
            self.client.run_credential_maintenance()
        except Exception as exc:  # noqa: BLE001 — the window must always close
            log.error("maintenance window: rotation failed: %s", exc)
        finally:
            self._quiesce.set()
            log.info("maintenance window closed — connections rebuilt with fresh token")

    def _start_heartbeat_thread(self) -> None:
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="heartbeat"
        )
        self._hb_thread.start()

    # -- T-177: server probe thread -------------------------------------------

    def _probe_loop(self) -> None:
        """Probe relay /health + /ready + /metrics on a fixed cadence.

        Mirror of the node-cli daemon probe loop: own daemon thread so a
        hanging HTTP call can never stall heartbeats or the SSE loop.
        Failures are recorded (never raised): the next tick retries.
        30 s cadence → worst-case server-outage visibility latency is
        probe + telemetry interval (~90 s end-to-end).
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
        async with httpx.AsyncClient() as http:
            while not self._stop_event.is_set():
                # T-185: while a maintenance window is open, hold the loop —
                # no connection attempt presents a token about to rotate.
                await self._wait_until_normal()
                if self._stop_event.is_set():
                    return
                # T-184: rebuild the auth header on EVERY attempt. A token
                # rotation (credential maintenance) must not leave the SSE
                # loop presenting a stale snapshot forever — that caused a
                # permanent 401 reconnect cycle (2026-09-13).
                headers = {"Authorization": f"Bearer {self.client.token}"}
                consume = asyncio.ensure_future(
                    self._consume_stream(http, url, headers)
                )
                watch = asyncio.ensure_future(self._watch_quiesce())
                try:
                    await asyncio.wait(
                        {consume, watch}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    # T-185: if the window opened (or we are stopping), the
                    # active stream is cancelled — the server sends no
                    # keepalive, so quiescence must drop it actively.
                    for task in (consume, watch):
                        if not task.done():
                            task.cancel()
                    # Await both so nothing is left dangling; swallow every
                    # outcome — a consume error is handled below, a cancel
                    # is expected here.
                    for task in (consume, watch):
                        try:
                            await task
                        except BaseException as task_exc:  # noqa: BLE001 — cleanup only
                            log.debug("SSE task cleanup: %r", task_exc)
                exc = (
                    consume.exception()
                    if consume.done() and not consume.cancelled()
                    else None
                )
                if exc is not None:
                    log.warning("SSE connection error: %s", exc)
                if (
                    watch.done()
                    and not watch.cancelled()
                    and not watch.exception()
                ):
                    log.info("quiescence window: SSE connection dropped")
                if self._stop_event.is_set():
                    return
                if not self._quiesce.is_set():
                    # Window still open: hold (no reconnect attempts).
                    continue
                # Wait before reconnecting, but stay responsive to stop.
                log.info("SSE reconnecting in %.0fs …", _RECONNECT_DELAY)
                for _ in range(int(_RECONNECT_DELAY * 10)):
                    if self._stop_event.is_set():
                        return
                    await asyncio.sleep(0.1)

    async def _wait_until_normal(self) -> None:
        """Block until the quiescence window closes (T-185)."""
        while not self._quiesce.is_set() and not self._stop_event.is_set():
            await asyncio.sleep(0.05)

    async def _watch_quiesce(self) -> None:
        """Return when a maintenance window opens (T-185)."""
        while self._quiesce.is_set() and not self._stop_event.is_set():
            await asyncio.sleep(0.05)

    async def _consume_stream(
        self,
        http: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
    ) -> None:
        async with http.stream(
            "GET", url, headers=headers, timeout=_SSE_STREAM_TIMEOUT
        ) as resp:
            resp.raise_for_status()
            log.info("SSE connected to %s", url)
            # Backfill (T-c51219ee): every successful SSE connect first
            # sweeps already-pending work — one-shot ``task_created``
            # events that fired before this connection exist can never
            # arrive anymore. Best-effort: a sweep error must not kill
            # the stream. Also arms the periodic claim ticker exactly
            # once (this runs in the SSE thread).
            try:
                self._on_sse_connected()
            except Exception as exc:  # noqa: BLE001 — stream must survive
                log.warning("SSE connect backfill failed: %s", exc)
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
        self._backfill_sweep()

    # -- backfill (T-c51219ee) ----------------------------------------------

    def _on_sse_connected(self) -> None:
        """First reaction after a successful SSE connect.

        Runs in the SSE thread (called from ``_consume_stream`` before
        any event is consumed). Two jobs:

        1. Immediately sweep for already-pending work — a task created
           before this connection will never re-emit its one-shot
           ``task_created`` event.
        2. Arm the periodic backfill ticker (exactly once per daemon
           lifetime); the ticker repeats the sweep so tasks that became
           pending mid-run without an event (claim-race loss, event
           hole) are still picked up.
        """
        self._backfill_sweep()
        self._arm_backfill_ticker()

    def _backfill_sweep(self) -> None:
        """One claim attempt per claimable capability.

        Same contract as the ``task_created`` handler: respects
        ``max_parallel`` via the ``_in_flight`` counters (stage
        execution is sequential in the SSE thread — never claim more
        than the capability can run) and lets the server decide
        atomically whether a stage exists (a null claim is a cheap
        204).
        """
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

    def _arm_backfill_ticker(self) -> None:
        """Start the periodic backfill ticker thread (once)."""
        if self._backfill_armed.is_set():
            return
        self._backfill_armed.set()
        interval = _backfill_interval(self.cfg)
        if interval <= 0:
            log.info("backfill ticker disabled (backfill_interval=%d)", interval)
            return
        self._backfill_thread = threading.Thread(
            target=self._backfill_ticker_loop,
            daemon=True,
            name="backfill-ticker",
        )
        self._backfill_thread.start()
        log.info(
            "backfill ticker armed (every %ds) — catches tasks that became "
            "pending without a task_created event",
            interval,
        )

    def _backfill_ticker_loop(self) -> None:
        """Repeat the claim sweep every ``backfill_interval`` seconds.

        Pure safety net for the live-connection case: while the SSE
        stream is up, ``task_created`` events normally trigger claims;
        the ticker only rescues tasks that were missed (claim race,
        event hole, dropped event). Stops with the daemon.
        """
        interval = _backfill_interval(self.cfg)
        while not self._stop_event.is_set():
            # Sleep in small slices so shutdown stays responsive.
            for _ in range(max(1, interval)):
                if self._stop_event.is_set():
                    return
                time.sleep(1)
            if self._stop_event.is_set():
                return
            try:
                self._backfill_sweep()
            except Exception as exc:  # noqa: BLE001 — ticker must survive
                log.warning("backfill sweep failed: %s", exc)

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
        # T-154: if the capability declares long_run, signal the relay
        # immediately so the stage is switched to `accepted` and the
        # 2h lease starts (instead of the 300s claim timeout killing it).
        if task_id and cap.get("long_run"):
            try:
                self.client.add_task_note(
                    task_id,
                    f"long-running {capability} started",
                    kind="longrun",
                )
            except Exception as exc:  # noqa: BLE001 — best-effort; never block the claim
                log.warning("longrun note failed for %s: %s", task_id, exc)
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
                # T-154/T-163: a long_run capability must not be killed by
                # the short per-stage timeout (default 300s). The relay's
                # Long-Run lease keeps the stage `accepted` for up to 2h as
                # long as progress notes keep arriving, so the handler
                # process gets the same 2h budget. Without this, a big
                # archive/extract is killed mid-run and the stage is
                # re-claimed to start over.
                timeout=_handler_timeout(cap),
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
                # T-179 Task 5: complete_by_script — wenn das Handler-Script
                # den Stage selbst completed hat, antwortet das Relay mit
                # dem 404 "not claimed"-Detail. Bei Opt-in zählt das als
                # Erfolg, nicht als Fehler.
                script_complete = bool((cap.get("config") or {}).get(
                    "complete_by_script"
                ))
                if script_complete and _is_already_completed(exc):
                    with self._lock:
                        self.tasks_completed += 1
                    log.info(
                        "stage %s already completed by script — counted as done",
                        stage_id,
                    )
                    return
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
        # T-185: refresh the registration secret synchronously BEFORE any
        # connection thread starts — the very first heartbeat/SSE request
        # presents fresh credentials (no startup 401 race). Non-fatal on
        # failure: rs stays due and the next maintenance window retries.
        try:
            self.client.refresh_registration_secret()
            log.info("startup: registration secret refreshed before connections")
        except Exception as exc:  # noqa: BLE001 — startup must survive
            log.warning("startup rs refresh failed: %s", exc)
        self._write_status()
        self._start_heartbeat_thread()
        self._start_probe_thread()
        # T-166 (F1): ephemeral file serve im Daemon — der CLI-Prozess stirbt
        # nach dem stdout-Envelope, der Serve-Endpoint muss überleben. Bind-
        # Fehler (Port belegt) = WARNING, der Node läuft ohne Serve weiter
        # (hp put bridge meldet dann den F5-Fehler statt zu crashen).
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
            # Backfill ticker (T-c51219ee): exits on _stop_event; join it
            # so a mid-sweep claim does not outlive the shutdown log line.
            if self._backfill_thread and self._backfill_thread.is_alive():
                self._backfill_thread.join(timeout=5)
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
        description="SSE-driven daemon for IOWAP.",
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
    # T-137b-Guard: refuse to start while the polling daemon is running.
    conflict = _check_other_daemon(base_dir=BASE_DIR)
    if conflict:
        print(conflict, file=sys.stderr)
        return 1
    PID_PATH.write_text(str(os.getpid()) + "\n", encoding="utf-8")
    try:
        SseDaemon(client, cfg).run()
    finally:
        PID_PATH.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())