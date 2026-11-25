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
import time
import shlex
import shutil
import signal
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import yaml

from nodes.common import node_config, node_utils

if TYPE_CHECKING:
    from nodes.common.relay_client import RelayClient

logger = logging.getLogger("node-dashboard")

# Capability names — same rule the daemon enforces (providers.example.yaml).
CAP_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
CAP_TYPES = ("process", "http")
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

# T-036: optional entry version — strict X.Y.Z, empty/missing → 1.0.0.
CAP_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
DEFAULT_CAP_VERSION = "1.0.0"

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


def find_provider(name: str, path: Path | None = None) -> dict[str, Any] | None:
    """One provider entry by name (``None`` when missing) — T-036 edit path."""
    name = (name or "").strip()
    return next((p for p in load_providers(path) if p.get("name") == name), None)


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

    # T-036: optional ``version`` — empty/missing defaults to 1.0.0, the
    # default is carried INTO the entry (visible in providers.yaml). Any
    # other garbage shape is a validation error (nothing is written).
    raw_version = fields.get("version")
    if raw_version is None or (isinstance(raw_version, str) and not raw_version.strip()):
        entry["version"] = DEFAULT_CAP_VERSION
    else:
        version = str(raw_version).strip()
        if not CAP_VERSION_RE.match(version):
            return None, (
                f"invalid version {version!r} — must be X.Y.Z (e.g. 1.0.0)"
            )
        entry["version"] = version

    # T-030: scheduler capacity — max_parallel (≥1, default 1) and cost
    # (admission points, ≥0, default 1). Empty field = default; anything
    # non-integer / below the minimum is a validation error (nothing is
    # written — create_provider discards the whole entry on error).
    max_parallel, mp_err = _parse_capacity(fields.get("max_parallel"), default=1, minimum=1)
    if mp_err:
        return None, f"max_parallel: {mp_err}"
    cost, cost_err = _parse_capacity(fields.get("cost"), default=1, minimum=0)
    if cost_err:
        return None, f"cost: {cost_err}"
    entry["max_parallel"] = max_parallel
    entry["cost"] = cost

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


def _parse_capacity(value: Any, *, default: int, minimum: int) -> tuple[int, str]:
    """Parse one capacity form field (T-030) → ``(int, error)``.

    Empty/None → ``(default, "")``. Accepts ints and digit strings;
    floats, negatives below ``minimum`` and garbage are errors.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return default, ""
    if isinstance(value, bool):
        return default, "must be an integer"
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default, f"invalid value {value!r} — must be an integer"
    if parsed < minimum:
        return default, f"must be ≥ {minimum} (got {parsed})"
    return parsed, ""


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
    # T-016i: posix=False — POSIX shlex eats backslashes as escape chars and
    # turned "c:\\scripts\\hello.bat" into "c:scripteshello.bat". Windows
    # paths must survive untouched; quotes and separator commas are stripped
    # per token afterwards.
    argv = shlex.split(raw, posix=False)
    cleaned = []
    for t in argv:
        if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
            t = t[1:-1]
        t = t.lstrip(",").rstrip(",")
        if t:
            cleaned.append(t)
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


def _bump_version(version: str) -> str:
    """Patch-bump one ``X.Y.Z`` version string (T-036 auto-bump)."""
    major, minor, patch = (int(p) for p in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def update_provider(name: str, fields: dict[str, Any], path: Path | None = None) -> tuple[bool, str]:
    """Replace ONE capability entry in place, atomically (T-036 edit path).

    Contract:

    * the name is the immutable routing key: ``fields["name"]`` (when set)
      must equal ``name`` — renames stay delete + create-legitimate;
    * field validation is the create ``_validate_provider_fields``
      contract — any validation error writes NOTHING (the file is left
      byte-identical, same T-021 rule as ``create_provider``);
    * every successful edit patch-bumps the entry version, UNLESS the form
      carries an explicit valid ``version`` that differs from the stored
      one — that one applies verbatim. An explicit version equal to the
      stored one still bumps (the contract is "every edit changes the
      version").

    Returns ``(ok, message)`` — ``update ok — <name> vX.Y.Z`` on success
    (the message feeds the dashboard flash).
    """
    name = (name or "").strip()
    path = path or providers_path()
    fields = dict(fields)
    new_name = (fields.get("name") or "").strip()
    if new_name and new_name != name:
        return False, (
            f"name is immutable — {name!r} cannot be renamed to {new_name!r} "
            "(rename stays delete + create legitimate)"
        )
    fields.setdefault("name", name)
    existing = load_providers(path)
    idx = next((i for i, p in enumerate(existing) if p.get("name") == name), -1)
    if idx < 0:
        return False, f"capability {name!r} not found"
    entry, error = _validate_provider_fields(fields)
    if entry is None:
        return False, error
    entry["name"] = name  # belt & braces: the routing key never moves
    old_version = str(existing[idx].get("version") or DEFAULT_CAP_VERSION)
    if not CAP_VERSION_RE.match(old_version):
        old_version = DEFAULT_CAP_VERSION  # hand-edited garbage → sane base
    explicit = fields.get("version")
    explicit = explicit.strip() if isinstance(explicit, str) else ""
    if explicit and entry["version"] != old_version:
        # an explicit valid version DIFFERENT from the stored one → verbatim
        pass
    else:
        # no explicit version, or explicit-but-equal → every edit bumps
        entry["version"] = _bump_version(old_version)
    existing[idx] = entry
    _atomic_write_yaml(path, {"providers": existing})
    return True, f"update ok — {name} v{entry['version']}"


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


# ---------------------------------------------------------------------------
# MCP spawn resolution (T-041): transport-dependent binary selection.
#
# The universal image (iowap-docker/universal-node) ships the MCP HTTP
# server as the ``iowap-node-mcp`` console script — the legacy ``iowap-mcp``
# entry no longer knows ``--http`` (client-mcp 0.2.0 is stdio-only), so the
# old ``iowap-mcp --http`` spawn in a universal image died with a usage
# error before answering the first probe (curl 52 again). Classic node
# images only know ``iowap-mcp --http``. The resolver picks the binary by
# transport; nothing resolvable → FileNotFoundError, the supervisor start
# turns that into a fail-closed (False, msg) and spawns NOTHING.
#
# Untouched channels: the entrypoint-started MCP (separate supervisor) and
# the bearer fail-closed guard (start never without the bearer — the
# spawned process still enforces it itself via _child_env()).
# ---------------------------------------------------------------------------

# http transport (the McpSupervisor / dashboard HTTP-MCP start): universal
# image first, then the legacy console script — both take the same server
# flags (--http --host 0.0.0.0 --port <port>); only the name changed.
MCP_HTTP_BINARY = "iowap-node-mcp"
MCP_HTTP_LEGACY_BINARY = "iowap-mcp"
# stdio transport (client-context entry — NOT the dashboard HTTP path):
# the legacy console script without transport flags; client-mcp 0.2.0 is
# stdio-only.
MCP_STDIO_BINARY = "iowap-mcp"


def resolve_mcp_spawn(port: int, *, transport: str = "http") -> list[str]:
    """The MCP process argv, transport-dependent (T-041).

    ``transport="http"`` — the supervisor/dashboard HTTP-MCP spawn:
    ``IOWAP_MCP_COMMAND`` override (exotic installs), the
    ``iowap-node-mcp`` console script (universal images), then the legacy
    ``iowap-mcp --http`` argv (classic images). Raises FileNotFoundError
    when nothing is resolvable — fail-closed, same rule the spawned
    process enforces on a missing bearer: never start what cannot serve
    the endpoint.

    ``transport="stdio"`` — the stdio client entry (``iowap-mcp``, no
    transport flags); not exercised by the dashboard's HTTP spawn path.

    The ``--host 0.0.0.0`` (T-016d, same reason the T-020 entrypoint
    needs it): the process defaults to container-internal loopback, but
    the compose publish 127.0.0.1:7781 -> container eth0 never reaches
    that bind — the endpoint would answer curl 52 while looking perfectly
    alive.
    """
    env_cmd = os.environ.get("IOWAP_MCP_COMMAND", "").strip()
    if env_cmd:
        return shlex.split(env_cmd)
    if transport == "stdio":
        exe = shutil.which(MCP_STDIO_BINARY)
        if exe:
            return [exe]
        raise FileNotFoundError(
            f"no {MCP_STDIO_BINARY!r} on PATH (T-041) — set IOWAP_MCP_COMMAND to override"
        )
    # http: universal image first (T-041), legacy images fallback, then a
    # clear failure — no silent stdio-only last resort (client-mcp 0.2.0
    # is stdio-only; the old ``python -m iowap_client_mcp --http`` spawn
    # just died with a usage error).
    exe = shutil.which(MCP_HTTP_BINARY)
    if exe:
        return [exe, "--http", "--host", "0.0.0.0", "--port", str(port)]
    exe = shutil.which(MCP_HTTP_LEGACY_BINARY)
    if exe:
        return [exe, "--http", "--host", "0.0.0.0", "--port", str(port)]
    raise FileNotFoundError(
        f"no MCP binary on PATH: tried {MCP_HTTP_BINARY!r} (universal image) and "
        f"{MCP_HTTP_LEGACY_BINARY!r} (legacy) — set IOWAP_MCP_COMMAND to override (T-041)"
    )


def default_mcp_command(port: int) -> list[str]:
    """The MCP server argv for the HTTP spawn (same shape the T-020 supervisor uses).

    T-041: thin alias of :func:`resolve_mcp_spawn` (http transport) —
    ``iowap-node-mcp`` first (universal images), legacy ``iowap-mcp
    --http`` fallback, FileNotFoundError fail-closed when neither is
    resolvable.
    """
    return resolve_mcp_spawn(port)


class McpSupervisor:
    """Starts/stops the in-container HTTP-MCP process (T-041 spawn).

    The binary is transport-dependent (HTTP-MCP start): ``iowap-node-mcp``
    on universal images, legacy ``iowap-mcp --http`` elsewhere,
    fail-closed when neither is resolvable. The entrypoint-started MCP
    is a separate channel and unaffected.

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
        # T-041: this spawn IS the HTTP-MCP start — http transport resolves
        # ``iowap-node-mcp`` (universal image) with the legacy ``iowap-mcp
        # --http`` fallback, or raises FileNotFoundError (fail-closed).
        return resolve_mcp_spawn(self.port, transport="http")

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
        try:
            argv = self._argv()
        except FileNotFoundError as exc:
            # T-041 fail-closed: no resolvable MCP binary → spawn NOTHING
            # (the same rule the bearer guard enforces on the process
            # side: the endpoint must be able to come up, or not start).
            logger.warning("MCP start failed: %s", exc)
            return False, f"MCP start failed: {exc}"
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


# ---------------------------------------------------------------------------
# Script upload (T-030, Ronny): form picks a file → stored under
# <workspace>/bridge/scripts/ → the process provider's argv references it
# workspace-relative (the Go daemon execs with cmd.Dir = workspaceDir,
# process.go — relative paths resolve).
# ---------------------------------------------------------------------------

MAX_SCRIPT_BYTES = 2 * 1024 * 1024
SCRIPT_EXTENSIONS = (".bat", ".cmd", ".ps1", ".py", ".sh", ".exe")
SCRIPTS_DIRNAME = "scripts"  # under <workspace>/bridge/


def scripts_dir(workspace: Path | None = None) -> Path:
    """``<workspace>/bridge/scripts`` — the upload target directory."""
    base = workspace if workspace is not None else workspace_dir()
    return base / "bridge" / SCRIPTS_DIRNAME


def sanitize_script_filename(filename: str) -> str:
    """Basename + safe characters only — no traversal, no hidden files."""
    name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).lstrip("._")
    return name


def script_command_argv(filename: str) -> list[str]:
    """The default argv template for an uploaded script, per extension (T-030).

    Windows extensions use backslashes (cmd/powershell resolve them),
    ``.sh`` uses the POSIX separator. The path is workspace-relative —
    the daemon runs every process command with cwd = workspace
    (iowap-bridge-daemon process.go: ``cmd.Dir = workspaceDir``).
    """
    ext = Path(filename).suffix.lower()
    if ext in (".bat", ".cmd"):
        return ["cmd", "/c", f"scripts\\{filename}"]
    if ext == ".ps1":
        return [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", f"scripts\\{filename}",
        ]
    if ext == ".py":
        return ["python", f"scripts\\{filename}"]
    if ext == ".sh":
        return ["bash", f"scripts/{filename}"]
    # .exe (and anything else the allowlist may grow): direct relative path
    return [f"scripts\\{filename}"]


def slugify_capability_name(filename: str) -> str:
    """Filename stem → a CAP_NAME_RE-valid capability name (T-030).

    Lowercase, invalid chars → ``-``; a result that would not match
    ``^[a-z][a-z0-9_.-]{1,63}$`` (empty, digit-first, too long) is
    prefixed/clamped so the form always gets a valid default.
    """
    stem = Path(sanitize_script_filename(filename)).stem
    slug = re.sub(r"[^a-z0-9_.-]+", "-", stem.lower()).strip("-.")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug or not CAP_NAME_RE.match(slug):
        slug = f"cap-{slug}" if slug else "cap-script"
    return slug[:64]


def save_script(
    filename: str,
    content: bytes,
    workspace: Path | None = None,
) -> tuple[bool, str, str]:
    """Store one uploaded script atomically (T-030) → ``(ok, message, stored_name)``.

    Rules: extension allowlist (:data:`SCRIPT_EXTENSIONS`, case-insensitive),
    size cap :data:`MAX_SCRIPT_BYTES`, collisions get a numeric suffix
    (``tool.1.bat``) instead of overwriting. The write is temp+rename, so
    a concurrent daemon read never sees a partial file.
    """
    name = sanitize_script_filename(filename)
    if not name or "." not in name:
        return False, "filename missing or has no extension", ""
    ext = Path(name).suffix.lower()
    if ext not in SCRIPT_EXTENSIONS:
        return False, (
            f"extension {ext!r} not allowed — one of "
            f"{', '.join(SCRIPT_EXTENSIONS)}"
        ), ""
    if len(content) > MAX_SCRIPT_BYTES:
        return False, (
            f"script too large ({len(content)} bytes) — cap is "
            f"{MAX_SCRIPT_BYTES // (1024 * 1024)} MB"
        ), ""
    target_dir = scripts_dir(workspace)
    target_dir.mkdir(parents=True, exist_ok=True)
    stem, suffix = Path(name).stem, Path(name).suffix
    candidate = target_dir / name
    counter = 0
    while candidate.exists():
        counter += 1
        candidate = target_dir / f"{stem}.{counter}{suffix}"
    tmp = candidate.with_suffix(candidate.suffix + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(candidate)
    return True, str(candidate.name), candidate.name


# ---------------------------------------------------------------------------
# Forward-Slots panel (T-030): GET slots + mappings, PUT/DELETE mappings.
# Slots themselves are declared in node.yaml (T-053/075) and transported
# by the heartbeat — the dashboard only maintains the (node, slot) →
# target mappings via the relay's /relay/v2/forward-mappings API.
# ---------------------------------------------------------------------------

FORWARD_MAPPINGS_PATH = "/relay/v2/forward-mappings"


# T-032: node_id -> monotonic deadline until which the forward panel is
# muted after a 403 (permission answers don't change render-to-render).
_FORBIDDEN_LATCH: dict[str, float] = {}
_FORWARD_FORBIDDEN_TTL = 600.0


def build_forward_client() -> "RelayClient":
    """A RelayClient for the current node (meta/config/token from disk).

    Raises (SystemExit from _base_url / registration gaps) when the node
    has no usable relay address — callers must catch and render a note.
    """
    from nodes.common import relay_client

    meta = node_utils.load_meta()
    cfg = relay_client._effective_config()
    return relay_client.RelayClient(meta, cfg)  # type: ignore[return-value]


def fetch_forward_panel(
    client: Any = None,
    *,
    node_id: str | None = None,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Fetch this node's forward slots + mappings (T-030). Never raises.

    Returns ``{"ok", "forbidden", "error", "node_id", "slots", "mappings"}``
    where ``mappings`` maps slot → mapping entry. ``forbidden`` is True on
    403 (the runtime token is not an admin — the UI hides the editor).
    """
    panel: dict[str, Any] = {
        "ok": False,
        "forbidden": False,
        "error": None,
        "node_id": node_id or "",
        "slots": [],
        "mappings": {},
    }
    # T-032: after a 403 the role will not change within seconds — latch the
    # forbidden state per node for 10 minutes so repeated dashboard renders
    # stop hammering an endpoint that answers permission-denied.
    if not node_id:
        try:
            node_id = node_utils.load_meta().get("node_id") or ""
        except Exception:  # noqa: BLE001 — latch is best-effort
            node_id = ""
    latch = _FORBIDDEN_LATCH.get(node_id)
    if latch and time.monotonic() < latch:
        panel["forbidden"] = True
        panel["node_id"] = node_id
        return panel
    try:
        if client is None:
            client = build_forward_client()
        if not node_id:
            meta = node_utils.load_meta()
            node_id = meta.get("node_id") or ""
        panel["node_id"] = node_id
        if not node_id:
            panel["error"] = "no node_id in meta"
            return panel

        r_slots = client._get_with_retry(
            f"{FORWARD_MAPPINGS_PATH}/slots", params={"node": node_id}, timeout=timeout
        )
        if r_slots.status_code == 403:
            panel["forbidden"] = True
            _FORBIDDEN_LATCH[node_id] = time.monotonic() + _FORWARD_FORBIDDEN_TTL
            return panel
        if r_slots.status_code != 200:
            panel["error"] = f"slots request failed (HTTP {r_slots.status_code})"
            return panel
        slots = r_slots.json().get("slots", [])
        panel["slots"] = slots if isinstance(slots, list) else []

        r_maps = client._get_with_retry(
            FORWARD_MAPPINGS_PATH, params={"node": node_id}, timeout=timeout
        )
        if r_maps.status_code == 403:
            panel["forbidden"] = True
            return panel
        if r_maps.status_code == 200:
            rows = r_maps.json().get("mappings", [])
            panel["mappings"] = {
                m.get("slot"): m for m in rows if isinstance(m, dict) and m.get("slot")
            }
        else:
            # slots are readable but mappings are not — still render slots
            panel["error"] = f"mappings request failed (HTTP {r_maps.status_code})"
        panel["ok"] = True
        return panel
    except Exception as exc:  # noqa: BLE001 — the dashboard must keep rendering
        logger.warning("forward panel fetch failed: %s", exc)
        panel["error"] = f"{type(exc).__name__}: {exc}"
        return panel


def save_forward_mapping(
    client: Any,
    node_id: str,
    slot: str,
    target_capability: str,
    target_node_id: str | None = None,
    *,
    timeout: float = 8.0,
) -> tuple[bool, str]:
    """PUT one (node, slot) → target mapping (T-030). Never raises.

    Contract (iowap-server api/v2/forward.py): ``PUT
    /relay/v2/forward-mappings/{node_id}/{slot}`` with JSON body
    ``{"target_capability": "…"}`` (upsert — create AND update).
    """
    slot = (slot or "").strip()
    target = (target_capability or "").strip()
    if not slot or not target:
        return False, "slot and target_capability are required"
    path = f"{FORWARD_MAPPINGS_PATH}/{node_id}/{slot}"
    body: dict[str, Any] = {"target_capability": target}
    if target_node_id and target_node_id.strip():
        body["target_node_id"] = target_node_id.strip()
    try:
        r = client._put(path, body, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if r.status_code == 403:
        return False, "forbidden — admin role required"
    if r.status_code in (200, 201):
        return True, f"mapping for slot {slot} saved"
    detail = ""
    try:
        detail = r.json().get("detail", "")
    except Exception:  # noqa: BLE001
        pass
    return False, f"HTTP {r.status_code}: {detail or 'upsert failed'}"


def delete_forward_mapping(
    client: Any,
    node_id: str,
    slot: str,
    *,
    timeout: float = 8.0,
) -> tuple[bool, str]:
    """DELETE one (node, slot) mapping (T-030). Never raises."""
    slot = (slot or "").strip()
    if not slot:
        return False, "slot is required"
    path = f"{FORWARD_MAPPINGS_PATH}/{node_id}/{slot}"
    try:
        r = client._delete(path, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if r.status_code == 403:
        return False, "forbidden — admin role required"
    if r.status_code == 404:
        return False, f"no mapping for slot {slot}"
    if r.status_code == 200:
        return True, f"mapping for slot {slot} deleted"
    return False, f"HTTP {r.status_code}: delete failed"
