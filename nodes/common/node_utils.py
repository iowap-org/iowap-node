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
import shutil
import signal
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


def sighup_pid_files(pid_paths: list[Path]) -> list[int]:
    """Send SIGHUP to every pid file whose process is alive (t_c214ca25).

    The node ships two daemons — the SSE daemon (``node-daemon.pid``) and
    the legacy polling daemon (``node-cli.pid``). Both install SIGHUP
    handlers that invalidate the capability cache, so profile publishers
    notify every known pid file instead of guessing which daemon runs.
    Missing, unparsable and stale pid files are skipped silently; OSError
    on the actual kill is swallowed (best-effort notification).

    Returns the list of pids that received the signal, in the order the
    paths were given (first entry wins as "the" daemon in messages).
    """
    notified: list[int] = []
    for pid_path in pid_paths:
        pid = read_pid(pid_path)
        if pid is None or not pid_running(pid):
            continue
        try:
            os.kill(pid, signal.SIGHUP)
        except OSError:
            continue
        notified.append(pid)
    return notified


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

# T-229: update sources — pypi-first with GitHub fallback for the node wheel.
UPDATE_SOURCE_DEFAULT = os.environ.get("IOWAP_UPDATE_SOURCE", "auto")  # auto|pypi|github
NODE_DIST = "iowap-node"
MCP_DIST = "iowap-client-mcp"
MCP_SPEC = "iowap-client-mcp[mcp]"


def get_local_dist_version(dist: str) -> str | None:
    """Return the installed distribution version of ``dist`` (or None)."""
    try:
        from importlib import metadata

        return metadata.version(dist)
    except Exception:  # noqa: BLE001 — not installed / metadata unreadable
        return None


def get_local_wheel_version() -> str | None:
    """Return the installed ``iowap-node`` distribution version (or None)."""
    return get_local_dist_version("iowap-node")


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


def _restart_service(service_unit: str | None, restart_command: str | None) -> tuple[bool, str]:
    """Restart the daemon unit after an update (shared by both apply paths).

    Returns (restarted, error_note). Honors RELAY_RESTART_COMMAND for hosts
    without systemd (``{unit}`` placeholder replaced).
    """
    unit = service_unit or SERVICE_UNIT
    restart_cmd = restart_command or RESTART_COMMAND
    if restart_cmd:
        import shlex

        argv = [tok.replace("{unit}", unit) for tok in shlex.split(os.path.expanduser(restart_cmd))]
    else:
        argv = ["systemctl", "--user", "restart", unit]
    try:
        subprocess.run(argv, capture_output=True, text=True, timeout=60.0, check=True)
        return True, ""
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"


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
    restarted, restart_err = _restart_service(unit, restart_command)
    result["restarted"] = restarted
    if restarted:
        result["success"] = True
        result["message"] = f"updated {before} -> {after}; service restarted"
    else:
        result["message"] = (
            f"pip ok ({before} -> {after}) but service restart failed: {restart_err}"
        )
    return result


def bump_relevant_version(latest: str | None, local: str | None) -> bool:
    """True when ``latest`` is strictly newer than ``local`` (semver trio)."""
    try:
        return tuple(int(x) for x in latest.split(".")) > tuple(int(x) for x in local.split("."))
    except (ValueError, AttributeError):
        return False


def _direct_url_kind(raw: str | None) -> str | None:
    """Classify a ``direct_url.json`` payload — 'dev' ONLY for editable installs.

    Editable installs carry ``dir_info.editable: true``. A LOCAL WHEEL file
    install (legacy `update apply` path) also records a ``file://`` URL —
    that is a registry-grade install and must NOT be refused.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    dir_info = data.get("dir_info") if isinstance(data, dict) else None
    if isinstance(dir_info, dict) and dir_info.get("editable") is True:
        return "dev"
    return None


def get_dist_url_kind(dist: str) -> str | None:
    """``"dev"`` when the dist was installed EDITABLE (not a local wheel).

    Reads ``direct_url.json`` of the installed distribution: registry and
    local-wheel installs must be updatable, editable (pip -e .) installs are
    dev-land and refuse ``apply``. Runs the check inside the RUNNING
    interpreter's environment — for another venv, pass the payload reader.
    """
    try:
        from importlib import metadata

        return _direct_url_kind(metadata.distribution(dist).read_text("direct_url.json"))
    except Exception:  # noqa: BLE001 — unreadable metadata = not a dev install
        return None


def get_pypi_latest_version(dist: str) -> dict:
    """Newest published version of ``dist`` on PyPI (JSON API, no pip call)."""
    url = f"https://pypi.org/pypi/{dist}/json"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latest = str(data.get("info", {}).get("version") or "").strip() or None
        return {"latest_version": latest, "source": "pypi", "error": None}
    except Exception as exc:  # noqa: BLE001 — network / HTTP / parse errors
        return {"latest_version": None, "source": "pypi",
                "error": f"PyPI lookup failed: {exc}"}


def resolve_update_source(
    dist: str = "iowap-node",
    *,
    source: str = "auto",
    repo: str | None = None,
) -> dict:
    """Resolve the newest upstream version: PyPI first, GitHub as fallback.

    ``source``: ``auto`` (PyPI, fall back to GitHub on failure) | ``pypi``
    (strict — no fallback) | ``github`` (legacy wheel-release path).
    """
    if source == "github":
        info = get_latest_release_version(repo=repo)
        info["source"] = "github"
        return info
    pypi = get_pypi_latest_version(dist)
    if pypi.get("latest_version"):
        return pypi
    if source == "pypi":
        return {
            "latest_version": None, "tag": None, "asset_name": None,
            "asset_url": None, "source": "pypi", "error": pypi["error"],
        }
    fallback = get_latest_release_version(repo=repo)
    fallback["source"] = "github"
    fallback["pypi_error"] = pypi["error"]
    return fallback


def resolve_mcp_install() -> dict:
    """Locate a potentially installed iowap-client-mcp on this host.

    Chain: PATH (shutil.which) → ~/.local/bin → ~/iowap-client-mcp/.venv →
    ~/projects/iowap-client-mcp/.venv → the Hermes MCP registration
    (~/.hermes/config.yaml: any mcp_servers entry whose command basename is
    ``iowap-mcp`` — the authoritative hint on Hermes hosts).
    Returns ``{"installed": True, "venv_python": Path}`` when the binary
    resolves, ``{"installed": False}`` otherwise (not an error — the toolkit
    is optional on a node host).
    """
    candidates = [
        shutil.which("iowap-mcp"),
        os.path.expanduser("~/.local/bin/iowap-mcp"),
        os.path.expanduser("~/iowap-client-mcp/.venv/bin/iowap-mcp"),
        os.path.expanduser("~/projects/iowap-client-mcp/.venv/bin/iowap-mcp"),
    ]
    candidates.extend(_hermes_mcp_config_commands())
    for found in candidates:
        if not found:
            continue
        binary = Path(found).expanduser().resolve()
        if binary.is_file() and os.access(binary, os.X_OK):
            venv_root = binary.parents[1]  # <venv>/bin/<binary>
            if (venv_root / "bin" / "python").exists():
                return {"installed": True, "venv_python": venv_root / "bin" / "python",
                        "venv": venv_root}
    return {"installed": False}


def _hermes_mcp_config_commands() -> list[str]:
    """Commands from ~/.hermes/config.yaml whose basename is ``iowap-mcp``.

    Best-effort YAML-less-safe: parses via pyyaml when available, falls back
    to a line-regex; a missing/unreadable config yields no candidates.
    """
    import glob

    results: list[str] = []
    for cfg in (*glob.glob(os.path.expanduser("~/.hermes/config.yaml")),):
        try:
            with open(cfg, "r", encoding="utf-8") as fh:
                raw = fh.read()
            try:
                import yaml

                servers = (yaml.safe_load(raw) or {}).get("mcp_servers") or {}
            except Exception:  # noqa: BLE001 — no pyyaml → line-scan fallback
                servers = {}
            found: list[str] = []
            if servers and isinstance(servers, dict):
                for server in servers.values():
                    cmd = (server or {}).get("command") if isinstance(server, dict) else None
                    if cmd and os.path.basename(str(cmd)) == "iowap-mcp":
                        found.append(str(cmd))
            if not found:
                found = re.findall(
                    r"command:\s*(\S*/iowap-mcp)\b", raw
                )
            results.extend(found)
        except Exception:  # noqa: S112 — config probe never breaks updates
            continue
    return results


def _venv_dist_url_kind(venv_python: Path, dist: str = MCP_DIST) -> str | None:
    """Read ``direct_url.json`` of ``dist`` inside ANOTHER venv (dev-guard)."""
    try:
        out = subprocess.run(
            [str(venv_python), "-c",
             "import importlib.metadata as m; print(m.distribution('" + dist
             + "').read_text('direct_url.json') or '')"],
            capture_output=True, text=True, timeout=30.0, check=True,
        ).stdout.strip()
        if not out or out == "None":
            return None
        return _direct_url_kind(out)
    except Exception:  # noqa: BLE001 — unreadable = treat as registry install
        return None


def check_updates(
    *,
    dist: str = "iowap-node",
    source: str | None = None,
    repo: str | None = None,
    include_mcp: bool = True,
) -> dict:
    """Full update-check: node block (PyPI-first) + MCP toolkit block.

    The MCP block reports the curated toolkit's state WITHOUT it being an
    error when absent — every node host simply may not run one.
    """
    source = source or UPDATE_SOURCE_DEFAULT
    local = get_local_dist_version(dist)
    info = resolve_update_source(dist, source=source, repo=repo)
    info["local_version"] = local
    info["local_kind"] = get_dist_url_kind(dist)
    info["update_available"] = bool(
        local and info.get("latest_version")
        and bump_relevant_version(info["latest_version"], local)
    )
    if include_mcp:
        mcp_local = get_local_dist_version(MCP_DIST)
        if mcp_local is None:
            info["mcp"] = {"installed": False}
        else:
            mcp_info = get_pypi_latest_version(MCP_DIST)
            info["mcp"] = {
                "installed": True,
                "local_version": mcp_local,
                "latest_version": mcp_info["latest_version"],
                "update_available": bool(
                    mcp_info["latest_version"]
                    and bump_relevant_version(mcp_info["latest_version"], mcp_local)
                ),
                "error": mcp_info["error"],
            }
    return info


def apply_updates(
    *,
    source: str | None = None,
    repo: str | None = None,
    service_unit: str | None = None,
    restart_command: str | None = None,
    include_mcp: bool = True,
) -> dict:
    """Apply self-updates: node always (PyPI pip path / GitHub fallback as
    resolved), the MCP toolkit upgrade in ITS OWN venv when installed.

    Never touches a dev/editable install (refusal with hint — the "apply not
    from dev venv" rule made machine-checkable). The MCP server has no unit
    to restart: a running iowap-mcp keeps its code until the next client
    spawn (the result carries that note).
    """
    source = source or UPDATE_SOURCE_DEFAULT
    result: dict = {
        "success": False,
        "message": "",
        "node": None,
        "mcp": None,
    }
    check = check_updates(source=source, repo=repo, include_mcp=include_mcp)
    result["check"] = {k: v for k, v in check.items() if k not in ("mcp",)}

    node_local_kind = check.get("local_kind")
    if check.get("update_available") and node_local_kind == "dev":
        result["message"] = (
            "refusing: installed iowap-node is a dev/editable install — "
            "apply is for registry (PyPI/GitHub) installs only"
        )
        return result

    if check.get("update_available"):
        if check.get("source") == "pypi":
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--quiet",
                     "--upgrade", NODE_DIST],
                    capture_output=True, text=True, timeout=600.0, check=True,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                tail = (getattr(exc, "stderr", "") or "").strip().splitlines()[-3:]
                result["message"] = f"pip upgrade failed: {exc}" + (f" | {tail}" if tail else "")
                return result
            after = get_local_dist_version(NODE_DIST)
            result["node"] = {"before": check["local_version"],
                              "after": after,
                              "source": "pypi"}
            # Same contract as the legacy path: the daemon only runs new code
            # after a restart — the wheel path must never skip that.
            restarted, restart_err = _restart_service(service_unit, restart_command)
            result["node"]["restarted"] = restarted
            if not restarted:
                result["message"] = (
                    f"pip ok ({check['local_version']} -> {after}) but service restart failed: {restart_err}"
                )
                return result
        else:
            legacy = apply_wheel_update(
                repo=repo, service_unit=service_unit, restart_command=restart_command,
            )
            result["node"] = {
                "before": legacy.get("before_version"),
                "after": legacy.get("after_version"),
                "source": "github",
                "restarted": legacy.get("restarted"),
                "wheel_path": legacy.get("wheel_path"),
            }
            if not legacy.get("success"):
                result["message"] = legacy.get("message") or "github apply failed"
                return result

    mcp_part = None
    if include_mcp:
        mcp_check = check.get("mcp") or {}
        if not mcp_check.get("installed"):
            mcp_part = {"installed": False}
        elif not mcp_check.get("update_available"):
            mcp_part = {"installed": True, "status": "up to date",
                        "version": mcp_check.get("local_version")}
        else:
            resolve = resolve_mcp_install()
            if not resolve.get("installed"):
                mcp_part = {"installed": True, "status": "update available but "
                            "iowap-mcp binary not resolvable (skipped)"}
            else:
                venv_python = resolve["venv_python"]
                if _venv_dist_url_kind(venv_python) == "dev":
                    mcp_part = {"installed": True, "status": "refused — toolkit "
                                "venv holds a dev/editable install"}
                else:
                    try:
                        subprocess.run(
                            [str(venv_python), "-m", "pip", "install", "--quiet",
                             "--upgrade", MCP_SPEC],
                            capture_output=True, text=True, timeout=600.0, check=True,
                        )
                        mcp_after = _subprocess_dist_version(venv_python)
                        mcp_part = {
                            "installed": True,
                            "before": mcp_check.get("local_version"),
                            "after": mcp_after,
                            "status": "updated (server restarts implicitly on the next client spawn)",
                        }
                    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                        tail = (getattr(exc, "stderr", "") or "").strip().splitlines()[-3:]
                        mcp_part = {
                            "installed": True,
                            "status": f"pip upgrade failed: {exc}" + (f" | {tail}" if tail else ""),
                        }
        result["mcp"] = mcp_part

    node_after = result["node"]["after"] if result["node"] else None
    ok = (node_after is not None) if check.get("update_available") else True
    if not check.get("update_available"):
        result["message"] = (
            f"node already up to date ({check.get('local_version')}); source={check.get('source')}"
        )
    elif ok and result["node"]:
        restarted_note = ", service restarted" if result["node"].get("restarted") else ""
        result["message"] = (
            f"node updated {result['node']['before']} -> {node_after} "
            f"(source={result['node']['source']}{restarted_note})"
        )
    result["success"] = ok
    return result


def _subprocess_dist_version(python: Path, dist: str = MCP_DIST) -> str | None:
    """Read the installed version of ``dist`` inside the venv of ``python``."""
    try:
        out = subprocess.run(
            [str(python), "-c",
             f"import importlib.metadata as m; print(m.version('{dist}'))"],
            capture_output=True, text=True, timeout=30.0, check=True,
        ).stdout.strip()
        return out or None
    except Exception:  # noqa: BLE001 — probe only
        return None


def post_result_rescue_note(
    client,
    task_id: str | None,
    stage_id: str,
    result,
    *,
    reason: str = "complete rejected",
    char_cap: int = 4000,
) -> bool:
    """T-001c-c: rescue a handler result whose ``complete`` call was rejected.

    A 404 from ``/scheduler/stages/{id}/complete`` means the stage is gone
    server-side (expired/re-queued/failed while the handler was running).
    The handler had already done the work — posting the result as an info
    note on the task (T-052 note channel, rides in TaskView) keeps it out
    of the bit bucket. Best effort: ``False`` when the task is gone or the
    note call fails; never raises.
    """
    if not task_id:
        return False
    try:
        payload = json.dumps(result, default=str)
    except (TypeError, ValueError):
        payload = repr(result)
    if len(payload) > char_cap:
        payload = payload[: char_cap - 3] + "..."
    try:
        client.add_task_note(
            str(task_id),
            f"[result-rescue] stage {stage_id} complete failed ({reason}); "
            f"result preserved:\n{payload}",
            kind="info",
        )
        return True
    except Exception:  # noqa: BLE001 — best effort, the failure is logged below
        logger.error(
            "result-rescue note for task %s stage %s failed — result not recoverable",
            task_id,
            stage_id,
        )
        return False
