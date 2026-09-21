"""Shared Relay API client.

The ``RelayClient`` here is the common HTTP client that both the
``node-cli`` Swiss-army-knife command and the ``node-daemon`` realtime
daemon use to talk to an AI Relay. Extracted out of ``node_cli.py`` so
the daemon no longer depends on the whole CLI monolith (T-112).

Also carries the small helpers that built the client's config/logging:
``_setup_logging``, ``_effective_config``, ``_base_url`` and
``_filename_from_response``.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from nodes.common.node_config import load_active_status
from nodes.common.node_utils import load_config, load_token, save_meta, save_token

log = logging.getLogger("relay-client")

# ---------------------------------------------------------------------------
# T-210: load measurement with environment-appropriate sources
# ---------------------------------------------------------------------------

# T-210: previous state of /sys/fs/cgroup/cpu.stat (cgroup v2) resp.
# cpuacct.usage (v1). CPU consumption is a *rate*, so we diff against
# the last sample; on the very first call there is no previous state
# and the cgroup rung yields nothing (falls through to loadavg).
_CGROUP_PREV: dict[str, tuple[float, float]] = {}  # path -> (monotonic, usage)


def _read_cgroup_cpu_usage() -> tuple[str, float] | None:
    """Return (rung, cpu-seconds-used) from the process's own cgroup.

    cgroup v2 first (/sys/fs/cgroup/cpu.stat), then v1 (cpuacct.usage).
    Both report CPU time consumed by the cgroup, which inside an LXC
    container is exactly the CT's own usage (the container's cgroup
    root). On bare metal it equals the host's usage — also correct.
    Returns None when neither file is readable.
    """
    # cgroup v2
    try:
        with open("/sys/fs/cgroup/cpu.stat") as f:
            for line in f:
                if line.startswith("usage_usec"):
                    return "cgroup2", int(line.split()[1]) / 1_000_000.0
    except (OSError, ValueError):
        pass
    # cgroup v1 (cpuacct.usage is in NANOSECONDS, like v2's usage_usec*1000)
    for path in ("/sys/fs/cgroup/cpuacct/cpuacct.usage", "/sys/fs/cgroup/cpu,cpuacct/cpuacct.usage"):
        try:
            with open(path) as f:
                return "cgroup", int(f.read().strip()) / 1_000_000_000.0
        except (OSError, ValueError):
            continue
    return None


def _measure_load_pct(cpu_count: int) -> tuple[float, str]:
    """Load as percent (0-100) from the best available source.

    Rungs:
      1. cgroup CPU consumption (diffed against the previous heartbeat)
         — container-scoped, correct inside LXC/Docker.
      2. os.getloadavg() — host loadavg; correct on bare metal and
         macOS, but inside LXC it reports the HOST's load (all CTs
         share /proc/loadavg). Kept as fallback because it is the only
         source on macOS (NovaForge).
    """
    now = time.monotonic()
    sample = _read_cgroup_cpu_usage()
    if sample is not None:
        source, usage = sample
        prev = _CGROUP_PREV.get("current")
        _CGROUP_PREV["current"] = (now, usage)
        if prev is not None and now > prev[0]:
            dt = now - prev[0]
            cpu_used = max(usage - prev[1], 0.0)
            # utilization = used CPU-seconds / (elapsed * cores)
            pct = (cpu_used / (dt * max(cpu_count, 1))) * 100.0
            if 0.0 <= pct <= 1000.0:  # sanity: CPU can't exceed cores*100
                return min(pct, 100.0), source
    # rung 2: loadavg
    try:
        load_avg = os.getloadavg()[0]
        return min((load_avg / max(cpu_count, 1)) * 100.0, 100.0), "loadavg"
    except (OSError, AttributeError):
        return 0.0, "loadavg"


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
    return datetime.now(UTC).isoformat()


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
    # T-182: fixed maintenance intervals. rs refreshes on every start + once
    # per day; rt refreshes every 6 days. Both TTLs are 7 days, so both stay
    # comfortably ahead of expiry without any expiry math.
    rs_iv = os.environ.get("RELAY_RS_REFRESH_INTERVAL")
    if rs_iv is not None:
        try:
            cfg["rs_refresh_interval_seconds"] = int(rs_iv)
        except ValueError:
            log.warning("ignoring invalid RELAY_RS_REFRESH_INTERVAL=%r", rs_iv)
    rt_iv = os.environ.get("RELAY_RT_REFRESH_INTERVAL")
    if rt_iv is not None:
        try:
            cfg["rt_refresh_interval_seconds"] = int(rt_iv)
        except ValueError:
            log.warning("ignoring invalid RELAY_RT_REFRESH_INTERVAL=%r", rt_iv)
    return cfg


def _base_url(meta: dict[str, Any], cfg: dict[str, Any]) -> str:
    url = cfg.get("base_url") or meta.get("base_url")
    if not url:
        # T-152: mDNS fallback — discover the relay on the local network
        # when no base_url is configured. The relay advertises itself as
        # `AI Relay Service._http._tcp.local.` (see core/zeroconf.py).
        discovered = _discover_relay_mdns()
        if discovered:
            log.info("mDNS: discovered relay at %s", discovered)
            url = discovered
    if not url:
        raise SystemExit(
            "no base_url configured (set base_url in relay_config.json, RELAY_BASE_URL, "
            "or let the node discover the relay via mDNS)"
        )
    return url.rstrip("/")


def _discover_relay_mdns(timeout: float = 2.0) -> str | None:
    """Discover the relay via mDNS on the local network.

    Returns the relay base URL (e.g. ``http://192.168.1.50:8788``) or ``None``
    when no relay is found. Uses the ``zeroconf`` package (already a project
    dependency). The relay advertises ``AI Relay Service._http._tcp.local.``
    with a ``path`` property (default ``/health``) and the port.
    """
    try:
        from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf
    except ImportError:
        log.warning("mDNS discovery unavailable (zeroconf not installed)")
        return None

    found: dict[str, Any] = {}

    class _Listener:
        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name)
            if info:
                found["info"] = info

        def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

        def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

    zc = Zeroconf()
    try:
        listener = _Listener()
        browser = ServiceBrowser(zc, "_http._tcp.local.", listener)
        # Wait briefly for discovery.
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and "info" not in found:
            time.sleep(0.1)
        browser.cancel()
    finally:
        zc.close()

    info = found.get("info")
    if not info:
        return None
    # Build the base URL from the discovered address + port.
    try:
        addr = info.parsed_addresses()[0] if info.parsed_addresses() else None
    except Exception:  # noqa: BLE001
        addr = None
    if not addr:
        return None
    port = info.port or 8788
    return f"http://{addr}:{port}"


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
        # TLS (T-111): optional CA cert path for nodes connecting over https
        # to a relay using a private/self-signed CA. When set, httpx verifies
        # against it instead of the system trust store. Default True = system
        # trust store (public CA / Let's Encrypt).
        self._verify: str | bool = cfg.get("tls_ca_cert") or True
        data = load_token()
        self.token = data["token"] if data else None
        # T-088: track the token expiry so the daemon can refresh
        # proactively before it expires. ``None`` means unknown (e.g.
        # a migrated legacy token or a server that omits expires_at).
        self.token_expires_at: str | None = data.get("expires_at") if data else None
        # T-108: Auth-Failure-Streak für exponentiellen Backoff. Nach
        # wiederholten 401/403-Fehlschlägen erhöht der Daemon den
        # Heartbeat/Claim-Abstand, statt in einem engen Loop zu hämmern.
        self._auth_fail_streak = 0
        # T-182: fixed-interval credential maintenance. The daemons call
        # maybe_refresh_token() every heartbeat tick; the client tracks
        # per-credential timers here.
        # T-185 (Ronny's correction): rt is NOT due at start just because
        # the daemon (re)started — the 6-day cadence counts from the LAST
        # REFRESH, persisted as ``refreshed_at`` in the token envelope so
        # it survives restarts. A missing/unparseable stamp (legacy
        # envelope, fresh install) counts as due: the next maintenance
        # window rotates rt once and re-anchors it. rs IS due at start
        # (``_rs_last_refresh is None``): the daemons consume it
        # synchronously BEFORE any connection starts (quiescence model),
        # and a failed startup rotation stays due so the next maintenance
        # window retries it.
        self._rs_last_refresh: float | None = None
        self._rt_refreshed_at: str | None = (
            data.get("refreshed_at") if data else None
        )
        # T-183: claim pause during credential maintenance. The gate is
        # SET = claims allowed (open). During run_credential_maintenance()
        # it is cleared: claim() skips without HTTP and the 401 fallback
        # waits for the gate instead of refreshing against a token the
        # maintenance just invalidated (2026-09-13 restart race).
        self._maintenance_gate = threading.Event()
        # T-184: which thread owns the gate right now. The maintenance
        # thread itself must neither skip (maybe_refresh_token guard) nor
        # wait ( _refresh_token gate wait) on the gate it just closed —
        # both were silent no-ops/self-deadlocks before.
        self._maintenance_owner: int | None = None
        self._maintenance_gate.set()
        if not self.token:
            print(
                "no runtime token found, attempting recovery with registration secret",
                file=sys.stderr,
            )
            self.token = self._recover_runtime_token()
            if not self.token:
                raise SystemExit("no runtime token available and recovery failed")

    # -- T-108: backoff + self-healing -------------------------------------

    # Backoff begins after this many consecutive auth failures.
    _BACKOFF_THRESHOLD = 3
    # Base delay (seconds) once the threshold is reached.
    _BACKOFF_BASE = 10
    # Hard cap (seconds) so the backoff never grows unbounded.
    _BACKOFF_MAX = 300
    # T-183: max seconds a 401 fallback waits for the credential
    # maintenance gate before proceeding with its own refresh.
    _MAINTENANCE_WAIT_TIMEOUT = 120.0

    def _register_backoff_failure(self) -> None:
        """Record one consecutive auth failure (401/403)."""
        self._auth_fail_streak += 1

    def _register_backoff_success(self) -> None:
        """Reset the auth-failure streak after a successful auth/refresh."""
        self._auth_fail_streak = 0

    def _current_backoff(self) -> float:
        """Return the current auth-failure backoff in seconds (0 = none)."""
        if self._auth_fail_streak < self._BACKOFF_THRESHOLD:
            return 0.0
        # Exponential: 10s, 20s, 40s, 80s, 160s — capped at _BACKOFF_MAX.
        exp = min(self._auth_fail_streak - self._BACKOFF_THRESHOLD + 1, 5)
        return float(min(self._BACKOFF_BASE * (2 ** (exp - 1)), self._BACKOFF_MAX))

    def _reload_token_from_disk(self) -> None:
        """Re-read the token file. Helps when an external process (or a
        manual intervention) corrected the token after the daemon cached
        an invalid value. Reads only — overwrites nothing on disk.
        T-185: adopts the persisted ``refreshed_at`` cadence anchor too,
        so the rt timer follows whatever is actually on disk.
        """
        data = load_token()
        if data and data.get("token"):
            self.token = data["token"]
            self.token_expires_at = data.get("expires_at")
            self._rt_refreshed_at = data.get("refreshed_at")

    # -- low level ----------------------------------------------------------

    def _post(
        self, path: str, body: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> httpx.Response:
        return httpx.post(
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            json=body or {},
            timeout=timeout or self.cfg["request_timeout"],
            verify=self._verify,
        )

    def _get(
        self, path: str, *, timeout: float | None = None
    ) -> httpx.Response:
        return httpx.get(
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=timeout or self.cfg["request_timeout"],
            verify=self._verify,
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
        # T-183: a 401 fallback that raced the maintenance start waits for
        # the maintenance to finish, then adopts the token it persisted.
        # Refreshing against the old token would fail: the server already
        # invalidated it (2026-09-13 restart race).
        gate_was_closed = not self._maintenance_gate.is_set()
        if gate_was_closed and self._maintenance_owner != threading.get_ident():
            if not self._maintenance_gate.wait(timeout=self._MAINTENANCE_WAIT_TIMEOUT):
                log.warning(
                    "credential maintenance still running after %.0fs; refreshing anyway",
                    self._MAINTENANCE_WAIT_TIMEOUT,
                )
            else:
                # Maintenance finished while we waited: adopt its fresh token.
                self._reload_token_from_disk()
        try:
            r = httpx.post(
                f"{self.base_url}/relay/v2/auth/refresh",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"requested_credential": "runtime_token"},
                timeout=self.cfg["request_timeout"],
                verify=self._verify,
            )
            if r.status_code == 200:
                data = r.json()
                new = data.get("token")
                expires_at = data.get("expires_at")
                if new:
                    # T-185: re-anchor the persisted cadence stamp — the
                    # 6-day interval counts from THIS refresh.
                    self._rt_refreshed_at = _utcnow_str()
                    save_token(
                        new, expires_at=expires_at,
                        refreshed_at=self._rt_refreshed_at,
                    )
                    self.token = new
                    self.token_expires_at = expires_at
                    # T-182: if the server rotates the rs alongside, keep it.
                    self._persist_rotated_secret(data)
                    self._register_backoff_success()
                    return True
        except Exception as exc:
            log.warning("runtime-token refresh failed: %s", exc)
        recovered = self._recover_runtime_token()
        if recovered is not None:
            self._register_backoff_success()
            return True
        # Refresh + Recovery fehlgeschlagen: Datei neu lesen — ein externer
        # Prozess/manueller Eingriff könnte den Token inzwischen korrigiert
        # haben (T-108 Task 1). Backoff-Streak erhöhen, damit der Daemon
        # nicht in einem engen 401-Loop verharrt (T-108 Task 2).
        self._reload_token_from_disk()
        self._register_backoff_failure()
        return False

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
                verify=self._verify,
            )
            r.raise_for_status()
            data = r.json()
            new = data.get("token")
            expires_at = data.get("expires_at")
            if new:
                # T-185: the recovered token is brand-new — re-anchor the
                # persisted cadence stamp; the 6-day interval counts from
                # THIS recovery.
                self._rt_refreshed_at = _utcnow_str()
                save_token(
                    new, expires_at=expires_at,
                    refreshed_at=self._rt_refreshed_at,
                )
                self.token = new
                self.token_expires_at = expires_at
                # T-182 (Bug 4): the server rotates the rs on recovery and
                # returns it. Persisting it is the whole point — a node that
                # drops the rotated rs cannot recover the NEXT time.
                self._persist_rotated_secret(data)
            return new
        except Exception as exc:
            log.error("registration-secret recovery failed: %s", exc)
            return None

    # -- proactive refresh (T-118) + credential maintenance (T-182) ----------

    def _persist_rotated_secret(self, data: dict[str, Any]) -> None:
        """Keep a rotated registration secret from any refresh response.

        The 2026-09-13 death spiral happened because the server attached a
        rotated rs to the recovery response but the client threw it away
        (relay_client.py:316-323 pre-T-182). A node whose meta file holds a
        stale rs cannot recover once its rt dies — persist whatever the
        server hands us, in both response shapes.
        """
        new_secret = data.get("registration_secret")
        if not new_secret or new_secret == self.meta.get("registration_secret"):
            return
        self.meta["registration_secret"] = new_secret
        save_meta(self.meta)
        log.info("persisted rotated registration secret from refresh response")

    def _maybe_rotate_rs(self) -> None:
        """Rotate the registration secret on start + every rs interval (T-182).

        Due when ``_rs_last_refresh is None`` (never done since daemon
        start — T-185: the startup wrapper consumes this synchronously
        before any connection) or the 24h interval has elapsed. Uses the
        valid runtime token (server auth.py Case 3). A failed rotation is
        non-fatal: keep the old secret and try again next tick.
        """
        interval = float(self.cfg.get("rs_refresh_interval_seconds", 86400))
        due = self._rs_last_refresh is None or (
            time.monotonic() - self._rs_last_refresh
        ) >= interval
        if not due:
            return
        if not self.token:
            return
        try:
            r = httpx.post(
                f"{self.base_url}/relay/v2/auth/refresh",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"requested_credential": "registration_secret"},
                timeout=self.cfg["request_timeout"],
                verify=self._verify,
            )
            if r.status_code == 200:
                data = r.json()
                new = data.get("token")
                if new:
                    self.meta["registration_secret"] = new
                    save_meta(self.meta)
                    self._rs_last_refresh = time.monotonic()
                    log.info("registration secret rotated proactively")
                else:
                    log.warning("rs rotation response without token; retrying next tick")
            else:
                log.warning(
                    "rs rotation failed (http %s); retrying next tick", r.status_code
                )
        except Exception as exc:  # noqa: BLE001 — maintenance must not kill the daemon
            log.warning("rs rotation error: %s", exc)

    # -- T-185: rt cadence anchored at the persisted last refresh -----------

    @staticmethod
    def _parse_iso_ts(value: Any) -> datetime | None:
        """Parse an ISO-8601 timestamp; returns None when unusable."""
        if not isinstance(value, str) or not value:
            return None
        try:
            ts = datetime.fromisoformat(value)
        except ValueError:
            return None
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)

    def _rt_due(self) -> bool:
        """Whether the rt refresh interval has elapsed since the LAST
        REFRESH (persisted ``refreshed_at`` in the token envelope).

        T-185 correction (Ronny): the 6-day cadence must NOT count from
        daemon start — a restart would reset the timer of an aging token
        and it could silently run past its 7-day TTL. The stamp lives on
        disk, so restarts preserve it. Unknown/unparseable stamp → due
        (self-healing: the next window rotates once and re-anchors).

        Stamping: ``save_token()`` anchors ``refreshed_at`` to NOW on
        every token write, so the refresh/recovery paths re-anchor the
        cadence implicitly; only the RAM mirror is updated here.
        """
        interval = float(self.cfg.get("rt_refresh_interval_seconds", 6 * 86400))
        ts = self._parse_iso_ts(self._rt_refreshed_at)
        if ts is None:
            return True
        return (datetime.now(UTC) - ts).total_seconds() >= interval

    def maybe_refresh_token(self) -> None:
        """Fixed-interval credential maintenance (T-182).

        Replaces the T-118 expiry-margin logic. No expiry math anymore —
        the node acts on its own schedule:
          - rs: rotated on daemon start + every ``rs_refresh_interval_seconds``
            (default 24h)
          - rt: refreshed every ``rt_refresh_interval_seconds`` (default 6 days)
        Both credentials carry a 7-day TTL server-side, so fixed intervals
        keep them permanently fresh. A recovery that ran in ``__init__``
        counts as a fresh rt refresh (the recovered token is brand-new).

        T-183: claims must not run concurrently with the rotation — the
        daemons use :meth:`run_credential_maintenance`, which pauses the
        claim loop for the duration. This method only guards against a
        direct call while another maintenance is in flight.

        T-185 correction (Ronny): the rt cadence counts from the LAST
        REFRESH, persisted as ``refreshed_at`` in the token envelope —
        never from daemon start, so a restart cannot reset the timer of
        an aging token. A missing/unparseable stamp counts as due: the
        next window rotates rt once and re-anchors the stamp (legacy
        envelopes bootstrap cleanly on the rs-start rotation).
        """
        if self._maintenance_owner == threading.get_ident():
            # This thread IS the maintenance (T-184): never skip your own
            # rotation — the guard is only for other threads.
            pass
        elif not self._maintenance_gate.is_set():
            # Another thread is inside run_credential_maintenance().
            return
        rt_due = self._rt_due()
        if rt_due:
            self._refresh_token()
        self._maybe_rotate_rs()

    def run_credential_maintenance(self) -> None:
        """Run one maintenance tick with claims paused (T-183).

        Sequence model instead of locking: pause claims, rotate rt/rs,
        resume claims — which now inherit the fresh tokens. The ``finally``
        guarantees the gate reopens even if the maintenance raises (server
        down), so a wedged claim loop can never outlive one bad tick.
        """
        self._maintenance_gate.clear()
        self._maintenance_owner = threading.get_ident()
        try:
            self.maybe_refresh_token()
        finally:
            self._maintenance_owner = None
            self._maintenance_gate.set()

    def maintenance_due(self) -> bool:
        """Whether a rotation is due right now (T-185).

        The heartbeat loop opens the quiescence window ONLY when this is
        true — the window drops SSE/claims/heartbeat, so it must never run
        on a plain 8 s tick without a pending rotation.

        rt: counted from the persisted last refresh (T-185 correction) —
        see :meth:`_rt_due`. rs: start tick outstanding or 24h elapsed.
        """
        rt_due = self._rt_due()
        rs_interval = float(self.cfg.get("rs_refresh_interval_seconds", 86400))
        rs_due = self._rs_last_refresh is None or (
            time.monotonic() - self._rs_last_refresh
        ) >= rs_interval
        return rt_due or rs_due

    def refresh_registration_secret(self) -> None:
        """Synchronous rs rotation for daemon startup (T-185).

        The daemons call this BEFORE starting any connection thread, so the
        very first heartbeat/SSE request presents fresh credentials. A
        failure is non-fatal: rs stays due (``_rs_last_refresh is None``)
        and the next maintenance window retries (same semantics as
        ``_maybe_rotate_rs``).
        """
        self._maybe_rotate_rs()

    # -- public API ----------------------------------------------------------

    def _build_heartbeat_payload(
        self, caps: list[dict[str, Any]], in_flight: dict[str, int]
    ) -> dict[str, Any]:
        # T-210: load_source chain. os.getloadavg() reports the HOST
        # loadavg inside LXC containers (shared /proc/loadavg), so all
        # CTs on one Proxmox host reported the same number. Prefer
        # container-scoped sources, fall back to loadavg (correct on
        # bare metal / macOS). Each rung sets load_source so the server
        # (and dashboard) can tell which measurement it got.
        cpu_count = os.cpu_count() or 1
        load_pct, load_source = _measure_load_pct(cpu_count)
        load_cap = float(self.cfg.get("load_cap", cpu_count * 100.0))
        load = min(load_pct, load_cap, 100.0)

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
            # T-164: upload_modes (inline/artifact/bridge) an den Server
            # durchreichen, damit die node-capabilities-Tabelle sie speichert
            # und file send/file get sie für die Modus-Wahl nutzen kann.
            if cap.get("upload_modes"):
                entry["upload_modes"] = cap.get("upload_modes")
            cap_status.append(entry)

        queue_depth = sum(in_flight.values())
        body: dict[str, Any] = {
            "node_id": self.meta["node_id"],
            "status": "online",
            "available": True,
            "load": load,
            "queue_depth": queue_depth,
            "capabilities": cap_status,
        }
        # T-210: which rung of the load chain produced the value
        # (cgroup2/cgroup/loadavg), so the dashboard can show it.
        body["load_source"] = load_source

        # T-072: forward node-level node_name + description from the
        # meta file (iowap-agent.json) so the server can store and
        # surface them via `node list` / `node info`.
        node_name = self.meta.get("node_name")
        if node_name:
            body["node_name"] = node_name
        description = self.meta.get("description")
        if description:
            body["description"] = description

        # T-081: forward the node's requested status (busy/idle) from the
        # active YAML profile. The value is written into the YAML by
        # `node-cli node busy`/`idle` and persists until explicitly
        # changed. When no explicit status is set we send "online" so
        # the server can transition the node from approved/offline to
        # online.
        requested_status = load_active_status()
        if requested_status:
            body["status"] = requested_status
        # T-081: forward the per-node load cap so the server can run
        # its auto-busy logic against the operator-configured ceiling
        # rather than a server-wide default.
        load_cap = self.cfg.get("load_cap")
        if load_cap is not None:
            body["load_cap"] = float(load_cap)

        return body

    def heartbeat(self, caps: list[dict[str, Any]], in_flight: dict[str, int]) -> dict[str, Any]:
        body = self._build_heartbeat_payload(caps, in_flight)
        r = self._post_with_retry(
            # T-176: post to /worker-heartbeat (replace mode). The regular
            # /heartbeat endpoint merges capabilities union-only on the
            # server (core/discovery.py), so capabilities removed from the
            # active profile would linger as ghosts on the relay forever.
            # /worker-heartbeat hardcodes replace_capabilities=True, i.e.
            # the server REPLACES the stored set with this heartbeat's
            # list — removals propagate. Server-side this endpoint has
            # existed and shipped since T-081 (api/v2/discovery.py).
            "/relay/v2/discovery/worker-heartbeat",
            body,
        )
        r.raise_for_status()
        return r.json()

    def claim(self, capability: str) -> dict[str, Any] | None:
        # T-183: during credential maintenance the claim thread pauses —
        # decided locally, no HTTP against a token that is about to rotate.
        if not self._maintenance_gate.is_set():
            return None
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

    # -- T-164: transfer ladder --------------------------------------------

    def get_transfer_config(self) -> dict[str, Any]:
        """Fetch the server's transfer-ladder config (T-164).

        ``GET /relay/v2/discovery/transfer-config`` returns
        ``{max_inline_bytes, max_artifact_bytes, max_payload_bytes}``.
        """
        r = self._get_with_retry("/relay/v2/discovery/transfer-config")
        r.raise_for_status()
        return r.json()

    def get_capability_detail(self, name: str) -> dict[str, Any]:
        """Fetch a single capability's details incl. ``upload_modes`` (T-164).

        ``GET /relay/v2/discovery/capabilities/{name}`` returns the
        capability with ``input_schema`` and ``upload_modes`` so the
        ``file send``/``file get`` handler can pick the transfer mode.
        """
        r = self._get_with_retry(f"/relay/v2/discovery/capabilities/{name}")
        r.raise_for_status()
        return r.json()

    def add_task_note(self, task_id: str, message: str, kind: str = "info") -> dict[str, Any]:
        """Append a note to a task (T-052 mini-chat; T-154 kind drives Long-Run)."""
        r = self._post_with_retry(
            f"/relay/v2/scheduler/tasks/{task_id}/notes",
            {"message": message, "kind": kind},
        )
        r.raise_for_status()
        return r.json()

    # -- T-126: temporary bridge routes -------------------------------------

    def register_temp_route(
        self,
        path: str,
        method: str,
        upstream: str,
        *,
        ttl_seconds: int,
        channel_id: str,
        description: str = "",
    ) -> dict[str, Any]:
        """Register a temporary bridge route on the server (T-124/T-126).

        The route is owned by this node (the node_id comes from the
        Bearer token), lives for ``ttl_seconds`` and is tied to
        ``channel_id`` so the caller can revoke it later. Use this for
        large-file handoff (storage upload/download channels) where the
        regular heartbeat routes would be replaced too eagerly.

        Returns the server response dict containing ``expires_at``.
        """
        body = {
            "path": path,
            "method": method,
            "upstream": upstream,
            "ttl_seconds": ttl_seconds,
            "channel_id": channel_id,
            "description": description,
        }
        r = self._post_with_retry(
            "/relay/v2/dashboard/api/node-routes/register", body
        )
        r.raise_for_status()
        return r.json()

    def unregister_temp_route(self, path: str, method: str = "GET") -> None:
        """Delete a route owned by this node before its TTL expires (T-126).

        ``DELETE /api/node-routes/{node_id}/{path}?method=...``. The node
        is resolved from the Bearer token on the server side, so this
        client only needs the path + method it registered earlier. A 404
        (route already expired/reaped) is swallowed.
        """
        import urllib.parse

        # The path is matched verbatim by the server; keep the leading
        # slash the server expects and URL-encode any segment so a path
        # with special characters survives the routing layer.
        sub = path if path.startswith("/") else "/" + path
        url_path = urllib.parse.quote(sub, safe="/")
        r = httpx.delete(
            f"{self.base_url}/relay/v2/dashboard/api/node-routes/{self.meta['node_id']}{url_path}",
            params={"method": method},
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=self.cfg["request_timeout"],
            verify=self._verify,
        )
        if r.status_code in (401, 403):
            if self._refresh_token():
                r = httpx.delete(
                    f"{self.base_url}/relay/v2/dashboard/api/node-routes/{self.meta['node_id']}{url_path}",
                    params={"method": method},
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=self.cfg["request_timeout"],
                    verify=self._verify,
                )
        # 404 means the route already expired/reaped — fine.
        if r.status_code not in (200, 404):
            r.raise_for_status()

    def list_temp_routes(self) -> list[dict[str, Any]]:
        """List this node's own temp routes from the server (T-136).

        ``GET /api/node-routes?node_id=<own>`` (rt-Token). The server
        resolves the caller's node_id from the Bearer token and returns
        only routes owned by this node, including ``expires_at`` and
        ``channel_id`` for each row.
        """
        r = self._get_with_retry("/relay/v2/dashboard/api/node-routes")
        r.raise_for_status()
        data = r.json()
        # Server returns ``{"routes": [...]}``.
        if isinstance(data, list):
            return data
        return data.get("routes", [])

    # -- artifact download ---------------------------------------------------

    def download_artifact(
        self,
        artifact_id: str,
        output_path: Path | None = None,
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
        name: str | None = None,
        task_id: str | None = None,
        stage_id: str | None = None,
    ) -> dict[str, Any]:
        """Upload a local file to the relay as an artifact.

        Returns the server response dict containing ``artifact_id``,
        ``name``, ``size_bytes``, etc. Falls back to a token refresh
        on a 401/403, then retries once.
        """
        url = f"{self.base_url}/relay/v2/storage/upload"
        params: dict[str, str] = {}
        if task_id:
            params["task_id"] = task_id
        if stage_id:
            params["stage_id"] = stage_id

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
                    verify=self._verify,
                )

        resp = _do_upload()
        if resp.status_code in (401, 403):
            self._refresh_token()
            resp = _do_upload()
        resp.raise_for_status()
        return resp.json()

    # -- T-177: unauthenticated server-status probe ---------------------------

    def probe_server(self) -> dict[str, Any]:
        """GET /health, /ready and /metrics and reduce them to a dict.

        All three endpoints are public (no token) — this probe is read-only
        monitoring for dashboards/telemetry. Never raises: on any failure
        the probe degrades to ``{"ok": False, "error": ...}`` so callers
        (daemon probe thread, status file, telemetry push) can treat the
        server as unreachable without their own try/except.

        Shape::

            {"ok": True, "version": "2.0.0", "mode": "core",
             "database": "ok", "scheduler": "ok",
             "maintenance_age_seconds": 29.0,
             "nodes_total": 6, "nodes_online": 4,
             "queue_depth": 0,
             "tasks_completed": 311, "tasks_failed": 24,
             "tasks_cancelled": 7,
             "stages_total": 311, "stages_retry_ratio": 0.0322,
             "tasks_created_5m": 0, "tasks_completed_5m": 0,
             "node_load": {"E4W3CBWQ": 12.87, ...}}
        """
        result: dict[str, Any] = {"ok": False, "error": ""}
        timeout = self.cfg.get("request_timeout", 10)
        try:
            r = httpx.get(
                f"{self.base_url}/health",
                timeout=timeout,
                verify=self._verify,
            )
            r.raise_for_status()
            health = r.json()
            result["version"] = health.get("version")
            result["mode"] = health.get("mode")
        except Exception as exc:  # noqa: BLE001 — probe must never throw
            result["error"] = f"health: {exc}"
            return result

        try:
            r = httpx.get(
                f"{self.base_url}/ready",
                timeout=timeout,
                verify=self._verify,
            )
            r.raise_for_status()
            ready = r.json()
            result["database"] = ready.get("database")
            result["scheduler"] = ready.get("scheduler")
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"ready: {exc}"
            # /health OK but /ready down → server is up but degraded.

        try:
            r = httpx.get(
                f"{self.base_url}/metrics",
                timeout=timeout,
                verify=self._verify,
            )
            r.raise_for_status()
            result.update(_parse_prometheus_gauges(r.text))
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"metrics: {exc}"

        result["ok"] = not result["error"]
        return result


def _parse_prometheus_gauges(text: str) -> dict[str, Any]:
    """Pull the interesting gauges out of a Prometheus exposition dump.

    Only simple gauges (``name value``) are collected — histograms are
    skipped except for the pre-aggregated *_sum/_count lines. Labelled
    series are mapped: relay_tasks{status=…} → tasks_<status>,
    relay_node_load{node_id=…} → node_load[node_id].
    """
    gauges: dict[str, Any] = {}
    node_load: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, rest = line.partition(" ")
        value_str = rest.strip()
        try:
            value = float(value_str)
        except ValueError:
            continue
        # Labels? → map into structured fields, skip the raw name.
        if "{" in name:
            labels = dict(re.findall(r'(\w+)="([^"]*)"', name))
            bare = name[: name.index("{")]
            if bare == "relay_tasks":
                gauges[f"tasks_{labels.get('status', 'unknown')}"] = value
            elif bare == "relay_stages":
                gauges[f"stages_{labels.get('status', 'unknown')}"] = value
            elif bare == "relay_node_load":
                node_id = labels.get("node_id")
                if node_id:
                    node_load[node_id] = value
            continue
        name = name.removeprefix("relay_")
        gauges[name] = value
    if node_load:
        gauges["node_load"] = node_load
    return gauges


def _filename_from_response(response: httpx.Response, fallback: str) -> str:
    """Extract a filename from Content-Disposition, falling back to the id."""
    cd = response.headers.get("content-disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    return m.group(1) if m else fallback
