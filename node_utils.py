#!/usr/bin/env python3
"""Shared utility functions for node-cli and related tools.

Extracted from the legacy poller.py to remove the dependency on the
old Poller class. These are thin wrappers around file I/O and config
loading used by node_cli.py and its RelayClient.
"""

import json
import logging
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
