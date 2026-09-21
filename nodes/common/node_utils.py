#!/usr/bin/env python3
"""Shared utility functions for node-cli and related tools.

Extracted from the legacy poller.py to remove the dependency on the
old Poller class. These are thin wrappers around file I/O and config
loading used by node_cli.py and its RelayClient.
"""

import json
import logging
import os
import re
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("node-utils")


def _utcnow_str() -> str:
    """Current UTC time as an ISO-8601 string (token cadence anchor)."""
    return datetime.now(UTC).isoformat()


BASE_DIR = Path.home() / ".relay"
LEGACY_META_PATH = BASE_DIR / "ai-relay-agent.json"
LEGACY_TOKEN_PATH = BASE_DIR / "ai-relay-agent.token"
META_PATH = BASE_DIR / "iowap-agent.json"
CONFIG_PATH = BASE_DIR / "relay_config.json"
TOKEN_PATH = BASE_DIR / "iowap-agent.token"
STATUS_PATH = BASE_DIR / "worker_status.json"

DEFAULT_CONFIG = {
    "base_url": None,
    "heartbeat_interval": 8,
    "claim_interval": 5,
    # T-c51219ee: periodic backfill claim sweep for the SSE daemon
    # (rescues tasks whose one-shot task_created event was missed).
    # 0 disables the ticker. Env override: RELAY_BACKFILL_INTERVAL.
    "backfill_interval": 60,
    "status_interval": 7200,
    # T-182: fixed maintenance intervals instead of expiry-margin math.
    # rt (TTL 7d): refresh every 6 days. rs (TTL 7d): rotate on every
    # start + every 24h. Env overrides: RELAY_RS_REFRESH_INTERVAL /
    # RELAY_RT_REFRESH_INTERVAL (seconds).
    "rs_refresh_interval_seconds": 86400,
    "rt_refresh_interval_seconds": 518400,
    "request_timeout": 10,
    "task_timeout": 600,
    "log_level": "INFO",
    "background_heartbeat": True,
    # T-060: how many times the daemon retries a task before refusing
    # to claim more stages for it (3 attempts total with the default of 2).
    "max_retries": 2,
}


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        logger.warning("failed to read %s: %s", path, exc)
        return default


def write_json_atomic(path: Path, data: dict):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def load_config() -> dict:
    cfg = load_json(CONFIG_PATH, default=DEFAULT_CONFIG.copy())
    if cfg is None:
        cfg = DEFAULT_CONFIG.copy()
    for key, value in DEFAULT_CONFIG.items():
        cfg.setdefault(key, value)
    return cfg


def load_meta() -> dict:
    path = META_PATH if META_PATH.exists() else LEGACY_META_PATH
    if not path.exists():
        raise FileNotFoundError(f"metadata missing: {META_PATH} (legacy: {LEGACY_META_PATH})")
    return json.loads(path.read_text())


def load_token() -> dict | None:
    """Load the persisted runtime token as a dict.

    Returns ``{"token": "...", "expires_at": "..." | None}`` or ``None``
    when no token file exists. Legacy plaintext token files (pre-T-088)
    are detected — a single non-JSON line is treated as the token value
    with an unknown expiry — and migrated to the JSON format on the next
    ``save_token()`` call.
    """
    if not TOKEN_PATH.exists() and not LEGACY_TOKEN_PATH.exists():
        return None
    token_path = TOKEN_PATH if TOKEN_PATH.exists() else LEGACY_TOKEN_PATH
    raw = token_path.read_text().strip()
    if not raw:
        return None
    # T-088: prefer the JSON envelope. Fall back to the legacy
    # plaintext format so existing installs keep working.
    if raw.lstrip().startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("token file %s is not valid JSON, treating as plaintext", TOKEN_PATH)
            return {"token": raw, "expires_at": None, "refreshed_at": None}
        if isinstance(data, dict) and data.get("token"):
            return {
                "token": data["token"],
                "expires_at": data.get("expires_at"),
                # T-185: cadence anchor for the 6-day rt interval; None
                # means "unknown" (legacy envelope) — the daemon then
                # stamps it on the next rotation.
                "refreshed_at": data.get("refreshed_at"),
            }
        return None
    return {"token": raw, "expires_at": None, "refreshed_at": None}


def save_token(
    token: str, expires_at: str | None = None, refreshed_at: str | None = None
) -> None:
    """Persist the runtime token plus its expiry as a JSON envelope.

    The envelope is
    ``{"token": "...", "expires_at": "...|None", "refreshed_at": "...|None"}``.
    ``refreshed_at`` (T-185) is the cadence anchor for the 6-day rt
    refresh interval: it counts from the LAST REFRESH, not from daemon
    start, so the stamp must survive restarts on disk. ``None`` stamps
    the current UTC time — every token write IS a refresh of the
    credential. Writing is atomic (tmp file + rename) so a crash
    mid-write never leaves a truncated token file. The file is chmod
    0o600 so other local users cannot read the token (security
    hardening, T-171).
    """
    if refreshed_at is None:
        refreshed_at = _utcnow_str()
    tmp = TOKEN_PATH.with_suffix(TOKEN_PATH.suffix + ".tmp")
    tmp.write_text(
        json.dumps(
            {"token": token, "expires_at": expires_at, "refreshed_at": refreshed_at}
        )
        + "\n"
    )
    os.chmod(tmp, 0o600)
    tmp.replace(TOKEN_PATH)
    os.chmod(TOKEN_PATH, 0o600)
    # Migrate: remove legacy token file after successful write
    if LEGACY_TOKEN_PATH.exists() and LEGACY_TOKEN_PATH != TOKEN_PATH:
        LEGACY_TOKEN_PATH.unlink(missing_ok=True)


def save_meta(meta: dict):
    write_json_atomic(META_PATH, meta)
    # Migrate: remove legacy file after successful write
    if LEGACY_META_PATH.exists() and LEGACY_META_PATH != META_PATH:
        LEGACY_META_PATH.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# PID helpers (T-117): parameterised so node_cli, node_daemon and a future
# federation_node can share them. Each caller passes its own pid_path.
# ---------------------------------------------------------------------------

def read_pid(pid_path: Path) -> int | None:
    """Read a PID file and return the pid, or ``None`` if missing/invalid."""
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return None


def pid_running(pid: int) -> bool:
    """Return True if a process with the given pid is currently alive."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Wheel-based self-update (successor of the T-062 git helpers)
# ---------------------------------------------------------------------------
# Source of truth for "is there a newer version" is the GitHub release with
# tag ``wheel-v<X.Y.Z>`` on iowap-org/iowap-node — the Wheel-CI publishes its
# built artifact as release asset ``iowap_node-<X.Y.Z>-py3-none-any.whl``.
# The locally installed distribution version (importlib.metadata) is compared
# against the newest release tag; apply = download the asset, pip
# force-reinstall it into the running venv, restart the systemd unit.

UPDATE_REPO = os.environ.get("RELAY_UPDATE_REPO", "iowap-org/iowap-node")
UPDATE_ASSET_RE = re.compile(r"^iowap_node-(\d+\.\d+\.\d+)-py3-none-any\.whl$")

# systemd user unit name restarted by `update apply`. Overridable via env
# RELAY_SERVICE_UNIT so tests can substitute a no-op service name.
SERVICE_UNIT = os.environ.get("RELAY_SERVICE_UNIT", "iowap-node-daemon.service")

# Restart command used by `update apply` instead of the systemd default —
# needed on hosts without systemd (macOS/launchd, manual setups). When set
# (env RELAY_RESTART_COMMAND) the value is split with shlex and run in place
# of `systemctl --user restart <unit>`; the string `{unit}` (if present) is
# replaced with the unit/service name. Example for launchd:
#   RELAY_RESTART_COMMAND='/usr/bin/launchctl kickstart -k gui/$UID/{unit}'
RESTART_COMMAND = os.environ.get("RELAY_RESTART_COMMAND")


def get_local_wheel_version() -> str | None:
    """Return the installed ``iowap-node`` distribution version (or None)."""
    try:
        from importlib import metadata

        return metadata.version("iowap-node")
    except Exception:  # noqa: BLE001 — not installed / metadata unreadable
        return None


def get_latest_release_version(repo: str | None = None) -> dict:
    """Query GitHub for the newest ``wheel-vX.Y.Z`` release.

    Returns a dict with:
      - ``latest_version``: parsed version str of the newest wheel release
                            (None when no matching release exists)
      - ``tag``:            full tag name (e.g. ``wheel-v2.3.8``)
      - ``asset_name``:     release asset filename of the wheel
      - ``asset_url``:      browser_download_url for the asset
      - ``error``:          human-readable failure reason (when lookup failed)
    """
    repo = repo or UPDATE_REPO
    url = f"https://api.github.com/repos/{repo}/releases"
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            releases = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — network / HTTP / parse errors
        return {"latest_version": None, "tag": None, "asset_name": None,
                "asset_url": None, "error": f"release lookup failed: {exc}"}
    for rel in releases:
        tag = rel.get("tag_name") or ""
        m = re.match(r"^wheel-v(\d+\.\d+\.\d+)$", tag)
        if not m:
            continue
        for asset in rel.get("assets") or []:
            name = asset.get("name") or ""
            if UPDATE_ASSET_RE.match(name):
                return {
                    "latest_version": m.group(1),
                    "tag": tag,
                    "asset_name": name,
                    "asset_url": asset.get("browser_download_url"),
                    "error": None,
                }
    return {"latest_version": None, "tag": None, "asset_name": None,
            "asset_url": None, "error": "no wheel-vX.Y.Z release found"}


def check_wheel_updates(*, repo: str | None = None) -> dict:
    """Compare the installed wheel version against the newest GitHub release.

    Returns get_latest_release_version() plus:
      - ``local_version``: installed version (None if not installed)
      - ``update_available``: bool (True only when both versions parse and
        latest > local)
    """
    local = get_local_wheel_version()
    info = get_latest_release_version(repo=repo)
    info["local_version"] = local
    latest = info.get("latest_version")
    try:
        info["update_available"] = bool(
            local and latest and tuple(int(x) for x in latest.split(".")) > tuple(int(x) for x in local.split("."))
        )
    except ValueError:
        info["update_available"] = False
    return info


def apply_wheel_update(
    *,
    repo: str | None = None,
    service_unit: str | None = None,
    wheel_dir: Path | None = None,
    restart_command: str | None = None,
) -> dict:
    """Download the newest wheel release, reinstall it and restart the unit.

    Returns a dict with:
      - ``success``:        bool
      - ``message``:        human-readable summary
      - ``before_version``: installed version before the update (or None)
      - ``after_version``:  installed version after the update (or None)
      - ``restarted``:      bool whether the service restart was attempted
      - ``wheel_path``:     local path of the downloaded wheel (or None)
    """
    unit = service_unit or SERVICE_UNIT
    target_dir = wheel_dir or (Path.home() / ".relay" / "wheels")
    before = get_local_wheel_version()
    result: dict = {
        "success": False,
        "message": "",
        "before_version": before,
        "after_version": None,
        "restarted": False,
        "wheel_path": None,
    }
    info = check_wheel_updates(repo=repo)
    if not info.get("update_available"):
        result["after_version"] = before
        result["message"] = (
            f"already up to date ({before}); no wheel release newer than the "
            f"installed version"
        )
        return result
    url = info.get("asset_url")
    name = info.get("asset_name")
    if not url or not name:
        result["message"] = info.get("error") or "no wheel asset in newest release"
        return result
    target_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = target_dir / name
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=120.0) as resp, open(wheel_path, "wb") as fh:
            fh.write(resp.read())
    except Exception as exc:  # noqa: BLE001 — network / HTTP errors
        result["message"] = f"wheel download failed: {exc}"
        return result
    result["wheel_path"] = str(wheel_path)
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet",
             "--force-reinstall", "--no-deps", str(wheel_path)],
            capture_output=True, text=True, timeout=300.0, check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        tail = (getattr(exc, "stderr", "") or "").strip().splitlines()[-3:]
        result["message"] = f"pip install failed: {exc}" + (f" | {tail}" if tail else "")
        return result
    after = get_local_wheel_version()
    result["after_version"] = after
    restart_argv: list[str] | None = None
    restart_cmd = restart_command or RESTART_COMMAND
    if restart_cmd:
        import shlex

        argv = shlex.split(os.path.expanduser(restart_cmd))
        restart_argv = [tok.replace("{unit}", unit) for tok in argv]
    else:
        restart_argv = ["systemctl", "--user", "restart", unit]
    try:
        subprocess.run(
            restart_argv,
            capture_output=True, text=True, timeout=60.0, check=True,
        )
        result["restarted"] = True
        result["success"] = True
        result["message"] = f"updated {before} -> {after}; service restarted"
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        result["message"] = (
            f"pip ok ({before} -> {after}) but service restart failed: {exc}"
        )
    return result
