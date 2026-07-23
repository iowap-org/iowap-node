#!/usr/bin/env python3
"""Shared utility functions for node-cli and related tools.

Extracted from the legacy poller.py to remove the dependency on the
old Poller class. These are thin wrappers around file I/O and config
loading used by node_cli.py and its RelayClient.
"""

import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger("node-utils")

BASE_DIR = Path.home() / ".relay"
META_PATH = BASE_DIR / "ai-relay-agent.json"
CONFIG_PATH = BASE_DIR / "relay_config.json"
TOKEN_PATH = BASE_DIR / "ai-relay-agent.token"
STATUS_PATH = BASE_DIR / "worker_status.json"

DEFAULT_CONFIG = {
    "base_url": None,
    "heartbeat_interval": 8,
    "claim_interval": 5,
    "status_interval": 7200,
    "rt_refresh_before_seconds": 86400,
    "rs_refresh_before_seconds": 3600,
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
    tmp.rename(path)


def load_config() -> dict:
    cfg = load_json(CONFIG_PATH, default=DEFAULT_CONFIG.copy())
    if cfg is None:
        cfg = DEFAULT_CONFIG.copy()
    for key, value in DEFAULT_CONFIG.items():
        cfg.setdefault(key, value)
    return cfg


def load_meta() -> dict:
    if not META_PATH.exists():
        raise FileNotFoundError(f"metadata missing: {META_PATH}")
    return json.loads(META_PATH.read_text())


def load_token():
    if TOKEN_PATH.exists():
        return TOKEN_PATH.read_text().strip()
    return None


def save_token(token: str):
    tmp = TOKEN_PATH.with_suffix(TOKEN_PATH.suffix + ".tmp")
    tmp.write_text(token + "\n")
    tmp.rename(TOKEN_PATH)


def save_meta(meta: dict):
    write_json_atomic(META_PATH, meta)


# ---------------------------------------------------------------------------
# T-062: git repo update helpers (update check / update apply)
# ---------------------------------------------------------------------------

# Location of the deployed repository on a node. Overridable via env var
# RELAY_REPO_DIR so tests (or non-standard installs) can point it elsewhere.
REPO_DIR = Path(os.environ.get("RELAY_REPO_DIR", str(Path.home() / "projects" / "ai-relay-service")))

# systemd user unit name restarted by `update apply`. Overridable via env
# RELAY_SERVICE_UNIT so tests can substitute a no-op service name.
SERVICE_UNIT = os.environ.get("RELAY_SERVICE_UNIT", "ai-relay-node-cli.service")


def _git(args: list[str], *, cwd: Path, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run a git command inside ``cwd`` and return the completed process.

    Raises CalledProcessError on non-zero exit so callers can distinguish a
    real failure from empty-but-valid output (e.g. ``git rev-list --count``
    before any upstream exists).
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )


def get_repo_info(repo_dir: Path | None = None) -> dict:
    """Return a snapshot of the local repo vs. its configured upstream.

    The dict contains:
      - ``local_commit``:    SHA of HEAD (or None if not a git repo)
      - ``local_branch``:    current branch name (or "" for detached HEAD)
      - ``remote_commit``:   SHA of the upstream tracking branch (or None)
      - ``behind_count``:    number of commits local is behind upstream
                             (0 when no upstream is configured)
      - ``has_upstream``:    bool whether an upstream is configured
    """
    repo = repo_dir or REPO_DIR
    info: dict = {
        "local_commit": None,
        "local_branch": "",
        "remote_commit": None,
        "behind_count": 0,
        "has_upstream": False,
    }
    if not (repo / ".git").exists() and not repo.exists():
        return info
    try:
        info["local_commit"] = _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return info
    try:
        info["local_branch"] = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        info["local_branch"] = ""
    try:
        info["remote_commit"] = _git(["rev-parse", "@{upstream}"], cwd=repo).stdout.strip()
        info["has_upstream"] = bool(info["remote_commit"])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        info["remote_commit"] = None
        info["has_upstream"] = False
    if info["has_upstream"]:
        try:
            count = _git(["rev-list", "--count", "HEAD..@{upstream}"], cwd=repo).stdout.strip()
            info["behind_count"] = int(count) if count.isdigit() else 0
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
            info["behind_count"] = 0
    return info


def check_for_updates(repo_dir: Path | None = None) -> dict:
    """Run ``git fetch origin`` and return ``get_repo_info()`` afterwards.

    The fetch is best-effort: network failures are logged and the current
    repo info is still returned so callers can display the last-known state.
    """
    repo = repo_dir or REPO_DIR
    try:
        _git(["fetch", "origin"], cwd=repo, timeout=60.0)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("git fetch failed: %s", exc)
    return get_repo_info(repo_dir=repo)


def apply_update(repo_dir: Path | None = None, *, service_unit: str | None = None) -> dict:
    """Pull the latest commits and restart the node-cli systemd service.

    Returns a dict with:
      - ``success``:        bool
      - ``message``:        human-readable summary
      - ``before_commit``:  SHA before the pull (or None)
      - ``after_commit``:   SHA after the pull (or None)
      - ``behind_before``:  behind_count before the pull
      - ``behind_after``:   behind_count after the pull
      - ``restarted``:      bool whether the service restart was attempted
    """
    repo = repo_dir or REPO_DIR
    unit = service_unit or SERVICE_UNIT
    before = get_repo_info(repo_dir=repo)
    result: dict = {
        "success": False,
        "message": "",
        "before_commit": before.get("local_commit"),
        "after_commit": None,
        "behind_before": before.get("behind_count", 0),
        "behind_after": 0,
        "restarted": False,
    }
    try:
        _git(["pull"], cwd=repo, timeout=120.0)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        result["message"] = f"git pull failed: {exc}"
        return result
    after = get_repo_info(repo_dir=repo)
    result["after_commit"] = after.get("local_commit")
    result["behind_after"] = after.get("behind_count", 0)
    # Restart the systemd user service so the new code is loaded.
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", unit],
            capture_output=True,
            text=True,
            timeout=60.0,
            check=True,
        )
        result["restarted"] = True
        result["success"] = True
        if result["before_commit"] == result["after_commit"]:
            result["message"] = f"already up to date ({result['after_commit']}); service restarted"
        else:
            result["message"] = (
                f"updated {result['before_commit']} -> {result['after_commit']}; "
                f"service restarted"
            )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        result["message"] = (
            f"pull ok ({result['before_commit']} -> {result['after_commit']}) but "
            f"service restart failed: {exc}"
        )
        result["success"] = False
    return result
