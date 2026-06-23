#!/usr/bin/env python3
"""Generic AI Relay agent poller: heartbeat + task claim loop.

This is a reference implementation for both KI-capable worker nodes and
KI-less service nodes. It handles authentication, heartbeats, task claiming,
execution dispatch, and status reporting.

Usage:

    from poller import Poller

    poller = Poller()
    poller.register("chat", handle_chat)
    poller.register("storage.archive", handle_archive)
    poller.run()

Handlers receive a stage dict and must return a result dict.

Improvements over older pollers:
- Config lives in ~/.relay/relay_config.json instead of hardcoded constants.
- Status is written to ~/.relay/worker_status.json for external monitoring.
- Token files are written atomically via rename().
- 401/403 triggers immediate credential refresh instead of blind retry/backoff.
- Runtime token is the primary credential; registration secret is recovery.
- Periodic /auth/status calls check credential lifetimes and refresh them
  proactively.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

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
    "load_cap": 1.0,
    "log_level": "INFO",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_str() -> str:
    return _utcnow().isoformat()


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        print(f"failed to read {path}: {exc}", file=sys.stderr)
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
        print(f"metadata missing: {META_PATH}", file=sys.stderr)
        sys.exit(1)
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


class Poller:
    config: dict

    def __init__(self):
        self.meta = load_meta()
        self.config = load_config()
        if self.config.get("base_url"):
            self.meta["base_url"] = self.config["base_url"].rstrip("/")

        self.token = load_token()
        self.handlers: dict[str, Callable] = {}
        self.tasks_completed = 0
        self.tasks_failed = 0
        self.last_claim = 0
        self.last_status_check = 0
        self._worker_start = _utcnow()

        if not self.token:
            print("no runtime token found, attempting recovery with registration secret", file=sys.stderr)
            self.token = self._recover_runtime_token_with_rs()
            if not self.token:
                print("no runtime token available and recovery failed", file=sys.stderr)
                sys.exit(1)

    def _api_post(self, path: str, headers: dict = None, json_body=None, timeout=None):
        url = f"{self.meta['base_url']}{path}"
        return httpx.post(
            url,
            headers=headers or {},
            json=json_body,
            timeout=timeout or self.config["request_timeout"],
        )

    def _refresh_runtime_token_with_rt(self):
        r = self._api_post(
            "/relay/v2/auth/refresh",
            headers={"Authorization": f"Bearer {self.token}"},
            json_body={"requested_credential": "runtime_token"},
        )
        r.raise_for_status()
        data = r.json()
        new_token = data.get("token")
        if new_token:
            save_token(new_token)
        return new_token

    def _refresh_registration_secret_with_rt(self):
        r = self._api_post(
            "/relay/v2/auth/refresh",
            headers={"Authorization": f"Bearer {self.token}"},
            json_body={"requested_credential": "registration_secret"},
        )
        r.raise_for_status()
        data = r.json()
        new_secret = data.get("token")
        if new_secret:
            self.meta["registration_secret"] = new_secret
            save_meta(self.meta)
        return new_secret

    def _recover_runtime_token_with_rs(self):
        r = self._api_post(
            "/relay/v2/auth/refresh",
            json_body={
                "node_id": self.meta["node_id"],
                "requested_credential": "runtime_token",
                "registration_secret": self.meta["registration_secret"],
            },
        )
        r.raise_for_status()
        data = r.json()
        new_token = data.get("token")
        new_secret = data.get("registration_secret")
        if new_token:
            save_token(new_token)
        if new_secret:
            self.meta["registration_secret"] = new_secret
            save_meta(self.meta)
        return new_token

    def _fetch_credential_status(self):
        r = self._api_post(
            "/relay/v2/auth/status",
            headers={"Authorization": f"Bearer {self.token}"},
            json_body={"node_id": self.meta["node_id"]},
        )
        r.raise_for_status()
        return r.json()

    def _iso_to_timestamp(self, value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value).timestamp()
        except Exception:
            return None

    def _ensure_credentials_fresh(self):
        status = self._fetch_credential_status()
        now = time.time()
        rt_valid = self._iso_to_timestamp(status.get("rt_valid_until"))
        rs_valid = self._iso_to_timestamp(status.get("rs_valid_until"))

        if rt_valid and (rt_valid - now) < self.config["rt_refresh_before_seconds"]:
            print("runtime token close to expiry, refreshing", file=sys.stderr)
            new_token = self._refresh_runtime_token_with_rt()
            if new_token:
                self.token = new_token

        if rs_valid and (rs_valid - now) < self.config["rs_refresh_before_seconds"]:
            print("registration secret close to expiry, refreshing", file=sys.stderr)
            self._refresh_registration_secret_with_rt()

        self.token = load_token() or self.token

    def _handle_auth_error(self):
        print("token invalid, forcing refresh", file=sys.stderr)
        new_token = None
        try:
            new_token = self._refresh_runtime_token_with_rt()
        except Exception as exc:
            print(f"runtime-token refresh failed: {exc}", file=sys.stderr)

        if not new_token:
            print("trying registration-secret recovery", file=sys.stderr)
            try:
                new_token = self._recover_runtime_token_with_rs()
            except Exception as exc:
                print(f"registration-secret recovery failed: {exc}", file=sys.stderr)

        if not new_token:
            print("all credential refresh attempts failed, exiting", file=sys.stderr)
            sys.exit(1)

        self.token = new_token

    def heartbeat(self):
        url = f"{self.meta['base_url']}/relay/v2/discovery/heartbeat"
        raw_caps = self.meta.get("capabilities", [])
        caps = []
        for c in raw_caps:
            if isinstance(c, str):
                caps.append({"name": c, "available": True})
            elif isinstance(c, dict):
                caps.append({"name": c.get("name", c), "available": c.get("available", True)})
            else:
                caps.append({"name": str(c), "available": True})

        try:
            load = os.getloadavg()[0]
        except (OSError, AttributeError):
            load = 0.0
        load = min(load, float(self.config.get("load_cap", 1.0)))

        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {self.token}"},
            json={
                "node_id": self.meta["node_id"],
                "status": "online",
                "load": load,
                "queue_depth": 0,
                "capabilities": caps,
            },
            timeout=self.config["request_timeout"],
        )
        r.raise_for_status()
        return r.json()

    def claim(self, capability):
        url = f"{self.meta['base_url']}/relay/v2/scheduler/claim"
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {self.token}"},
            json={"capability": capability},
            timeout=self.config["request_timeout"],
        )
        if r.status_code == 204:
            return None
        r.raise_for_status()
        data = r.json()
        if not data.get("claimed") or not data.get("stage"):
            return None
        return data["stage"]

    def complete(self, task_id, stage_id, result):
        url = f"{self.meta['base_url']}/relay/v2/scheduler/stages/{stage_id}/complete"
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {self.token}"},
            json={
                "node_id": self.meta["node_id"],
                "task_id": task_id,
                "result": result,
            },
            timeout=self.config["task_timeout"],
        )
        r.raise_for_status()
        return r.json()

    def submit_task(self, task_name, stages, priority=0):
        url = f"{self.meta['base_url']}/relay/v2/scheduler/tasks"
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {self.token}"},
            json={
                "task_name": task_name,
                "stages": stages,
                "priority": priority,
            },
            timeout=self.config["request_timeout"],
        )
        r.raise_for_status()
        return r.json()

    def register(self, capability, handler):
        self.handlers[capability] = handler

    def _write_status(self, heartbeat_status, error=None):
        status = {
            "pid": os.getpid(),
            "node_id": self.meta.get("node_id"),
            "started_at": self._worker_start.isoformat(),
            "last_heartbeat": _utcnow_str(),
            "heartbeat_status": heartbeat_status,
            "token_present": bool(self.token),
            "capabilities": [c.get("name") if isinstance(c, dict) else c for c in self.meta.get("capabilities", [])],
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "error": error,
        }
        write_json_atomic(STATUS_PATH, status)

    def run(self):
        capabilities = []
        for c in self.meta.get("capabilities", []):
            if isinstance(c, dict):
                capabilities.append(c.get("name"))
            else:
                capabilities.append(c)
        print(f"poller started for node {self.meta['node_id']} with caps {capabilities}")

        while True:
            heartbeat_status = "unknown"
            error = None
            try:
                self.token = load_token() or self.token
                hb = self.heartbeat()
                heartbeat_status = hb.get("status", "ok")
                print(f"heartbeat {heartbeat_status} at {time.strftime('%H:%M:%S')}")

                if time.time() - self.last_status_check > self.config["status_interval"]:
                    self._ensure_credentials_fresh()
                    self.last_status_check = time.time()

                if time.time() - self.last_claim > self.config["claim_interval"]:
                    for cap in capabilities:
                        stage = self.claim(cap)
                        if stage:
                            print(f"claimed {cap} stage: {stage.get('stage_id')}")
                            handler = self.handlers.get(cap)
                            if handler:
                                try:
                                    result = handler(stage)
                                    self.complete(stage["task_id"], stage["stage_id"], result)
                                    self.tasks_completed += 1
                                    print(f"completed {stage.get('stage_id')}")
                                except Exception as exc:
                                    self.tasks_failed += 1
                                    print(f"task execution failed: {exc}", file=sys.stderr)
                                    self.complete(stage["task_id"], stage["stage_id"], {"error": str(exc)})
                            break
                    self.last_claim = time.time()

            except httpx.HTTPStatusError as exc:
                error = f"http {exc.response.status_code}"
                if exc.response.status_code in (401, 403):
                    self._handle_auth_error()
                    continue
                print(f"http error: {exc}", file=sys.stderr)
            except Exception as exc:
                error = str(exc)
                print(f"error: {exc}", file=sys.stderr)

            self._write_status(heartbeat_status, error=error)
            time.sleep(self.config["heartbeat_interval"])


def main():
    poller = Poller()

    def dummy_handler(stage):
        return {"status": "ok", "message": f"stage {stage.get('stage_id')} acknowledged"}

    for cap in ["chat"]:
        poller.register(cap, dummy_handler)
    poller.run()


if __name__ == "__main__":
    main()
