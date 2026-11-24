#!/usr/bin/env python3
"""T-021: node-dashboard backend — capability store, publish toggle, MCP supervisor.

The write-side of the node dashboard served by
:mod:`nodes.common.onboarding` on port 7783. The dashboard is deliberately
narrow about what it writes (board T-021, DEC #8):

* ``<workspace>/bridge/providers.yaml`` — the capability registry shared
  with the bridge daemon (the daemon watches this file, board T-022).
  Schema mirrors the daemon's ``providers.example.yaml`` (T-019):
  ``providers:`` list with ``process`` / ``http`` entries.
* ``node.yaml`` — ONLY the ``bridge.allowlist`` / ``bridge.auto_publish``
  fields of the existing ``bridge:`` block; everything else (url, token,
  workspace, poll_interval, and all non-bridge keys) survives untouched.
  Afterwards the daemon pid files are SIGHUPed (the existing reload path,
  same channel ``iowap-cli reload`` uses).
* ``~/.relay/mcp_bearer`` — the HTTP-MCP bearer token.

**MCP supervisor contract** (agreed with the T-020 entrypoint supervisor):
the entrypoint starts ``iowap-mcp --http --port <IOWAP_MCP_PORT>`` when
``IOWAP_MCP_BEARER`` is set. With T-021 the dashboard owns the runtime
bearer: the supervisor loop reads ``~/.relay/mcp_bearer`` when
``IOWAP_MCP_BEARER`` is unset in the environment and (re)starts the
process with that token. The dashboard therefore ALWAYS writes the bearer
file BEFORE starting (or restarting) the process, so a running MCP always
serves exactly the bearer shown in the dashboard snippets. The pid file
``~/.relay/mcp.pid`` marks the running process.

Bearer generation mirrors the daemon's ``internal/provision`` (Go):
``iw_mcp_`` prefix + 32 bytes of crypto-random entropy as unpadded
base64url (43 chars, 256 bit).
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import json
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import yaml

from nodes.common import node_config, node_utils

logger = logging.getLogger("node-dashboard")

# Capability names — same rule the daemon enforces (providers.example.yaml).
CAP_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
CAP_TYPES = ("process", "http")
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

BEARER_PREFIX = "iw_mcp_"
BEARER_ENTROPY_BYTES = 32

DEFAULT_MCP_PORT = 7781

# Spawner signature: takes the argv list, returns a Popen-like object.
Spawner = Callable[[list[str]], Any]


# ---------------------------------------------------------------------------
# Workspace paths (the shared channel to the bridge daemon, T-022)
# ---------------------------------------------------------------------------


def workspace_dir() -> Path:
    """The node's workspace volume (env ``IOWAP_WORKSPACE_DIR``, default /workspace)."""
    env = os.environ.get("IOWAP_WORKSPACE_DIR", "").strip()
    return Path(env) if env else Path("/workspace")


def providers_path() -> Path:
    """``<workspace>/bridge/providers.yaml`` — the capability registry file."""
    return workspace_dir() / "bridge" / "providers.yaml"


def _atomic_write_yaml(path: Path, doc: dict[str, Any]) -> None:
    """Atomically (temp + rename) write a YAML document, keeping it valid."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Capability store
# ---------------------------------------------------------------------------


def load_providers(path: Path | None = None) -> list[dict[str, Any]]:
    """The provider entries from ``<workspace>/bridge/providers.yaml``.

    A missing file is an empty registry; unparsable content logs a warning
    and returns [] (the daemon fails loud on its side — the dashboard must
    keep rendering).
    """
    path = path or providers_path()
    if not path.exists():
        return []
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("could not read %s: %s", path, exc)
        return []
    if not isinstance(doc, dict) or not isinstance(doc.get("providers"), list):
        return []
    return [entry for entry in doc["providers"] if isinstance(entry, dict)]


def _validate_provider_fields(fields: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Validate one create form into a providers.yaml entry (or an error)."""
    name = (fields.get("name") or "").strip()
    if not CAP_NAME_RE.match(name):
        return None, (
            f"invalid capability name {name!r} — must match "
            "^[a-z][a-z0-9_.-]{1,63}$ (lowercase, 2–64 chars)"
        )
    ctype = (fields.get("type") or "").strip()
    if ctype not in CAP_TYPES:
        return None, f"invalid type {ctype!r} — must be one of {', '.join(CAP_TYPES)}"

    entry: dict[str, Any] = {"name": name, "type": ctype}
    summary = (fields.get("summary") or "").strip()
    if summary:
        entry["summary"] = summary
    if fields.get("long_run") in ("on", "1", "true", True):
        entry["long_run"] = True

    if ctype == "process":
        raw_command = (fields.get("command") or "").strip()
        if not raw_command:
            return None, "process capabilities require a command (argv template)"
        argv = parse_command(raw_command)
        if not argv:
            return None, "command could not be parsed into an argv list"
        entry["command"] = argv
        glob = (fields.get("artifacts_glob") or "").strip()
        if glob:
            entry["artifacts_glob"] = glob
        return entry, ""

    # http provider — the exec mapping (daemon schema, T-019)
    url_template = (fields.get("url_template") or "").strip()
    if not url_template:
        return None, "http capabilities require a url_template"
    method = (fields.get("method") or "POST").strip().upper()
    if method not in HTTP_METHODS:
        return None, f"invalid HTTP method {method!r}"
    exec_block: dict[str, Any] = {"method": method, "url_template": url_template}
    body = (fields.get("body_template") or "").strip()
    if body:
        exec_block["body_template"] = body
    result_path = (fields.get("result_path") or "").strip()
    if result_path:
        exec_block["result_path"] = result_path
    raw_paths = (fields.get("artifact_paths") or "").strip()
    if raw_paths:
        paths = [p.strip() for p in raw_paths.split(",") if p.strip()]
        if paths:
            exec_block["artifact_paths"] = paths
    entry["exec"] = exec_block
    health = (fields.get("health_url") or "").strip()
    if health:
        entry["health_url"] = health
    return entry, ""


def parse_command(raw: str) -> list[str]:
    """Parse the dashboard's free-text command into an argv list (T-016g).

    Users type all kinds of shapes: a JSON array (["cmd", "/c", "sysinfo"]),
    comma-separated (cmd, /c, systeminfo), or plain whitespace. shlex alone
    keeps commas attached to tokens ("cmd,"), which made command[0]
    unresolvable and the provider permanently health=down.
    """
    raw = raw.strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed if str(x).strip()]
        except ValueError:
            pass
    argv = shlex.split(raw)
    # comma-separated input: shlex keeps the commas glued to the tokens —
    # strip a single leading/trailing comma per token (csv-like arguments
    # with inner commas survive).
    cleaned = [t.lstrip(",").rstrip(",") for t in argv]
    cleaned = [t for t in cleaned if t]
    return cleaned if cleaned else argv


def create_provider(fields: dict[str, Any], path: Path | None = None) -> tuple[bool, str]:
    """Validate + append one capability to the workspace registry (atomically).

    Returns ``(ok, message)`` — the message is an error for ``ok=False``
    (validation failures write NOTHING; duplicates leave the file as-is)
    and the capability name otherwise.
    """
    entry, error = _validate_provider_fields(fields)
    if entry is None:
        return False, error
    path = path or providers_path()
    existing = load_providers(path)
    if any(p.get("name") == entry["name"] for p in existing):
        return False, (
            f"capability {entry['name']!r} already exists — delete it first "
            "or pick another name"
        )
    existing.append(entry)
    _atomic_write_yaml(path, {"providers": existing})
    return True, entry["name"]


def delete_provider(name: str, path: Path | None = None) -> tuple[bool, str]:
    """Remove one capability from the workspace registry (atomically)."""
    name = (name or "").strip()
    path = path or providers_path()
    existing = load_providers(path)
    kept = [p for p in existing if p.get("name") != name]
    if len(kept) == len(existing):
        return False, f"unknown capability {name!r}"
    _atomic_write_yaml(path, {"providers": kept})
    return True, name


# ---------------------------------------------------------------------------
# Publish toggle (node.yaml bridge.allowlist / bridge.auto_publish)
# ---------------------------------------------------------------------------


def _update_bridge_block(mutate: Callable[[dict[str, Any]], None]) -> tuple[bool, str]:
    """Load node.yaml, mutate ONLY its ``bridge:`` block, write atomically.

    A missing ``bridge:`` block is created (allowlist/auto_publish only —
    without a url the bridge stays disabled, the fields simply wait for
    the operator to configure one). A non-mapping block is replaced with
    a fresh mapping.
    """
    path = node_config.ACTIVE_PATH
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, yaml.YAMLError) as exc:
            return False, f"could not parse {path}: {exc}"
    block = data.get("bridge")
    if not isinstance(block, dict):
        block = {}
    mutate(block)
    data["bridge"] = block
    _atomic_write_yaml(path, data)
    reload_daemons()
    return True, "node.yaml updated"


def set_capability_published(name: str, published: bool) -> tuple[bool, str]:
    """Add/remove ``name`` (bare, no ``host.`` prefix) from bridge.allowlist."""

    def mutate(block: dict[str, Any]) -> None:
        allowlist = block.get("allowlist")
        if not isinstance(allowlist, list) or not all(isinstance(e, str) for e in allowlist):
            allowlist = []
        allowlist = [e for e in allowlist if e != name]
        if published:
            allowlist.append(name)
        block["allowlist"] = allowlist

    return _update_bridge_block(mutate)


def set_auto_publish(enabled: bool) -> tuple[bool, str]:
    """Set ``bridge.auto_publish`` (publishes all caps while the allowlist is empty)."""

    def mutate(block: dict[str, Any]) -> None:
        block["auto_publish"] = bool(enabled)

    return _update_bridge_block(mutate)


def is_published(name: str, bridge_cfg: dict[str, Any] | None) -> bool:
    """Dashboard mirror of the bridge publish gate (bridge._publish_allowed).

    Published when the allowlist names the cap (or contains ``*``), or
    when ``auto_publish`` is true AND the allowlist is empty.
    """
    if not bridge_cfg:
        return False
    allowlist = bridge_cfg.get("allowlist") or []
    if "*" in allowlist or name in allowlist:
        return True
    return bool(bridge_cfg.get("auto_publish")) and not allowlist


def reload_daemons() -> bool:
    """SIGHUP the node daemon pid files (the existing config-reload path).

    Best-effort: a not-running daemon simply picks the node.yaml change up
    on its next start. Returns True when at least one live daemon was
    signalled.
    """
    base = node_utils.BASE_DIR
    notified = node_utils.sighup_pid_files(
        [base / "node-daemon.pid", base / "node-cli.pid"]
    )
    if notified:
        logger.info("config reload: SIGHUPed daemon pids %s", notified)
    return bool(notified)


# ---------------------------------------------------------------------------
# MCP bearer + supervisor
# ---------------------------------------------------------------------------


def mcp_port() -> int:
    """Container-side MCP HTTP port (env ``IOWAP_MCP_PORT``, default 7781)."""
    try:
        return int(os.environ.get("IOWAP_MCP_PORT") or DEFAULT_MCP_PORT)
    except ValueError:
        return DEFAULT_MCP_PORT


def mcp_pid_path() -> Path:
    """Pid file of the running MCP process (``~/.relay/mcp.pid``)."""
    return node_config.BASE_DIR / "mcp.pid"


def mcp_bearer_path() -> Path:
    """The runtime bearer file the supervisor reads (``~/.relay/mcp_bearer``)."""
    return node_config.BASE_DIR / "mcp_bearer"


def mcp_log_path() -> Path:
    """Where the MCP process stdout/stderr lands (``~/.relay/mcp.log``)."""
    return node_config.BASE_DIR / "mcp.log"


def generate_bearer() -> str:
    """``iw_mcp_`` + 32 crypto-random bytes as unpadded base64url.

    Parity with the daemon's ``internal/provision.GenerateBearer`` (Go).
    """
    return BEARER_PREFIX + secrets.token_urlsafe(BEARER_ENTROPY_BYTES)


def current_bearer() -> str:
    """The node's MCP bearer: the file wins, then ``IOWAP_MCP_BEARER``, then ''."""
    try:
        path = mcp_bearer_path()
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
    except OSError as exc:
        logger.warning("could not read %s: %s", mcp_bearer_path(), exc)
    return os.environ.get("IOWAP_MCP_BEARER", "").strip()


def write_bearer(bearer: str) -> Path:
    """Atomically write the bearer file, readable only by the owner (0600)."""
    path = mcp_bearer_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(bearer + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path


def rotate_bearer(supervisor: "McpSupervisor | None" = None) -> tuple[str, bool]:
    """Generate a bearer, write the file, restart a RUNNING MCP process.

    The write always happens BEFORE the restart (supervisor contract — see
    module docstring). Returns ``(bearer, restarted)``.
    """
    bearer = generate_bearer()
    write_bearer(bearer)
    restarted = False
    if supervisor is not None:
        if supervisor.is_running():
            supervisor.restart()
            restarted = True
        else:
            # T-016d: rotate on a stopped process = rotate + start — otherwise
            # a fresh bearer is written but the endpoint stays dead.
            ok, msg = supervisor.start()
            restarted = ok
            if not ok:
                logger.warning("rotate_bearer: start after rotate failed: %s", msg)
    return bearer, restarted


def default_mcp_command(port: int) -> list[str]:
    """Resolve the MCP server argv (same shape the T-020 supervisor uses).

    Resolution: ``IOWAP_MCP_COMMAND`` (shell string, for exotic installs),
    then the ``iowap-mcp`` console script on PATH, then
    ``python -m iowap_client_mcp`` from the running interpreter.
    """
    env_cmd = os.environ.get("IOWAP_MCP_COMMAND", "").strip()
    if env_cmd:
        return shlex.split(env_cmd)
    # --host 0.0.0.0 (T-016d, same reason the T-020 entrypoint needs it):
    # the process defaults to container-internal loopback, but the compose
    # publish 127.0.0.1:7781 -> container eth0 never reaches that bind —
    # the endpoint would answer curl 52 while looking perfectly alive.
    exe = shutil.which("iowap-mcp")
    if exe:
        return [exe, "--http", "--host", "0.0.0.0", "--port", str(port)]
    return [sys.executable, "-m", "iowap_client_mcp", "--http", "--host", "0.0.0.0", "--port", str(port)]


class McpSupervisor:
    """Starts/stops the in-container HTTP-MCP process (``iowap-mcp --http``).

    The dashboard's enable/disable/rotate route through this class so the
    process interface is one seam tests can mock (``spawner`` kwarg).
    State on disk: pid file :func:`mcp_pid_path`, log :func:`mcp_log_path`,
    bearer :func:`mcp_bearer_path` (always written before start/restart —
    see the module docstring for the T-020 supervisor contract).
    """

    def __init__(
        self,
        *,
        port: int | None = None,
        pid_path: Path | None = None,
        bearer_path: Path | None = None,
        log_path: Path | None = None,
        command: list[str] | None = None,
        spawner: Spawner | None = None,
    ) -> None:
        self.port = mcp_port() if port is None else port
        self.pid_path = pid_path or mcp_pid_path()
        self.bearer_path = bearer_path or mcp_bearer_path()
        self.log_path = log_path or mcp_log_path()
        self._command = list(command) if command else None
        # None → the real spawn (subprocess.Popen with log redirect);
        # injected spawners (tests) fully replace the spawn step.
        self._spawner = spawner
        self._proc: Any = None

    # -- internals ----------------------------------------------------------

    def _argv(self) -> list[str]:
        if self._command:
            return list(self._command)
        return default_mcp_command(self.port)

    def _read_pid(self) -> int | None:
        return node_utils.read_pid(self.pid_path)

    # -- public interface ----------------------------------------------------

    def is_running(self) -> bool:
        """True when the pid file names a live process (or we own one)."""
        if self._proc is not None and self._proc.poll() is None:
            return True
        pid = self._read_pid()
        return pid is not None and node_utils.pid_running(pid)

    def _child_env(self) -> dict:
        """Child environment with the bearer injected (T-016d).

        The dashboard writes the bearer to :func:`mcp_bearer_path`, but a
        container recreated via plain ``docker compose up`` carries no
        ``IOWAP_MCP_BEARER`` env — without this injection the spawned
        ``iowap-mcp`` hit its fail-closed guard and exited immediately
        (curl 52 on the published port).

        T-016e: the FILE wins over the env. The dashboard always writes the
        file before start/restart and renders its snippets from the same
        file — with the old "env wins" rule a rotation on a
        bootstrap-created container restarted the process with the STALE
        env token while the UI showed the new file token
        (``invalid_token`` on every probe).
        """
        env = dict(os.environ)
        try:
            value = self.bearer_path.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if value:
            env["IOWAP_MCP_BEARER"] = value
        return env

    def start(self) -> tuple[bool, str]:
        """Spawn the MCP process (no-op when already running)."""
        if self.is_running():
            return True, f"MCP already running (pid {self._read_pid()})"
        argv = self._argv()
        try:
            if self._spawner is not None:
                # injected spawner (tests) — full replacement, no log redirect
                self._proc = self._spawner(argv)
            else:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("ab") as log:
                    self._proc = subprocess.Popen(
                        argv, stdout=log, stderr=subprocess.STDOUT,
                        env=self._child_env(),
                    )
        except OSError as exc:
            logger.warning("MCP start failed: %s", exc)
            return False, f"MCP start failed: {exc}"
        self.pid_path.parent.mkdir(parents=True, exist_ok=True)
        self.pid_path.write_text(str(self._proc.pid) + "\n", encoding="utf-8")
        logger.info("MCP process started (pid %d): %s", self._proc.pid, " ".join(argv))
        return True, f"MCP started (pid {self._proc.pid})"

    def stop(self) -> tuple[bool, str]:
        """Terminate the MCP process (graceful SIGTERM, SIGKILL fallback)."""
        stopped = False
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5)
            stopped = True
        pid = self._read_pid()
        if pid is not None and node_utils.pid_running(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError as exc:
                logger.warning("MCP stop: could not signal pid %d: %s", pid, exc)
                return False, f"MCP stop failed: {exc}"
            stopped = True
        self._proc = None
        self.pid_path.unlink(missing_ok=True)
        return (True, "MCP stopped") if stopped else (True, "MCP was not running")

    def restart(self) -> tuple[bool, str]:
        """Stop (when running) then start — picks up a rotated bearer."""
        self.stop()
        return self.start()


def build_supervisor() -> McpSupervisor:
    """The supervisor for the current container (factory — tests replace it)."""
    return McpSupervisor()
