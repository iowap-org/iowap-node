#!/usr/bin/env python3
"""Node configuration & capability profile loader for the node-cli daemon.

This module is responsible for loading, validating, publishing, and
diffing capability profiles stored as YAML files under
``~/.relay/profiles.d/``. The daemon only ever reads the *active*
profile (``~/.relay/node.yaml``); working profiles in ``profiles.d/``
are never touched by the daemon at runtime.

The active file is also the home of node-level configuration: the
optional ``status`` field (operator-requested busy/idle state, managed
by ``node-cli node busy``/``idle``/``clear-status``) and the optional
``node_name``/``description`` overrides. The ``capabilities`` array
itself is optional — a node may publish a profile that carries only
node-level configuration.

A *profile* is a YAML document of the form::

    capabilities:
      - name: chat.ai
        version: "1.0.0"
        auto_publish: true
        claimable: true
        handler: /opt/relay/handlers/chat-ai.sh
        max_parallel: 2
        timeout: 300

The loader normalizes each capability into a dict with the keys
``name``, ``version``, ``auto_publish``, ``claimable``, ``handler``,
``max_parallel`` and ``timeout``. Defaults are applied for missing
optional fields. Environment-variable overrides
(``RELAY_CAPABILITY_<NAME>_HANDLER`` / ``..._MAX_PARALLEL``) are applied
on read so that operators can patch a profile without editing the YAML.

For backward compatibility a one-shot migration (``_migrate_old_paths``)
runs at import time: legacy ``capabilities.active.yaml`` /
``capabilities.active.profile`` / ``capabilities.d/`` artifacts are
copied to their new names if the new files do not yet exist. The old
files are left in place so a rolling deploy can fall back.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

import yaml

from nodes.common.forward import (
    FORWARD_CAPABILITY,
    SLOT_TYPE_FORWARD,
    ForwardSlotError,
    normalize_forward_slot,
    normalize_slot_ref,
    normalize_slot_type,
    normalize_slot_value,
)

# ---------------------------------------------------------------------------
# JSON Schema for capability profiles (Draft 2020-12)
# ---------------------------------------------------------------------------

CAPABILITY_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["busy", "idle", "online"]},
        "node_name": {"type": "string"},
        "description": {"type": "string"},
        "capabilities": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "version": {"type": "string", "minLength": 1},
                    "type": {"type": "string"},
                    "description": {"type": "string"},
                    "auto_publish": {"type": "boolean"},
                    "claimable": {"type": "boolean"},
                    "handler": {"type": "string"},
                    "max_parallel": {"type": "integer", "minimum": 1},
                    "timeout": {"type": "integer", "minimum": 1},
                    "input_schema": {"type": "object"},
                },
                "additionalProperties": False,
            },
        }
    },
    "additionalProperties": True,
}


def validate_with_schema(data: dict[str, Any]) -> list[str]:
    """Validate parsed YAML data against CAPABILITY_SCHEMA.

    Returns a list of human-readable error messages. An empty list means
    the data is structurally valid. Uses ``jsonschema`` if available,
    otherwise falls back to a basic structural check.
    """
    errors: list[str] = []

    # Basic structural check (works without jsonschema dependency).
    if not isinstance(data, dict):
        errors.append("profile root must be a mapping")
        return errors
    # 'capabilities' is optional — a profile may carry only node-level
    # configuration (status, node_name, description). When present it
    # must be a list.
    caps = data.get("capabilities")
    if caps is not None:
        if not isinstance(caps, list):
            errors.append("'capabilities' must be a list")
            return errors
        for i, entry in enumerate(caps):
            prefix = f"capabilities[{i}]"
            if not isinstance(entry, dict):
                errors.append(f"{prefix}: must be a mapping, got {type(entry).__name__}")
                continue
            if "name" not in entry or not isinstance(entry.get("name"), str) or not entry["name"].strip():
                errors.append(f"{prefix}: 'name' is required and must be a non-empty string")
            # Check for unknown keys
            allowed = {"name", "version", "type", "description", "input_schema", "auto_publish", "claimable", "handler", "max_parallel", "timeout", "routes", "long_run", "upload_modes", "result_path_hints", "config"}
            extra = set(entry.keys()) - allowed
            if extra:
                errors.append(f"{prefix}: unknown keys: {', '.join(sorted(extra))}")
            # T-005b: per-capability config mapping (carrier of
            # envelope_request_mirror). Must be a mapping when present.
            if "config" in entry and not isinstance(entry.get("config"), dict):
                errors.append(f"{prefix}: 'config' must be a mapping")
            # Type checks for optional fields
            for key, expected_type in [
                ("version", str),
                ("auto_publish", bool),
                ("claimable", bool),
                ("handler", str),
                ("max_parallel", int),
                ("timeout", int),
                ("long_run", bool),
            ]:
                val = entry.get(key)
                if val is not None and not isinstance(val, expected_type):
                    errors.append(f"{prefix}.{key}: expected {expected_type.__name__}, got {type(val).__name__}")
            # Range checks
            for key in ("max_parallel", "timeout"):
                val = entry.get(key)
                if isinstance(val, int) and val < 1:
                    errors.append(f"{prefix}.{key}: must be >= 1, got {val}")

    return errors

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = Path.home() / ".relay"
PROFILES_DIR = Path(
    os.environ.get("RELAY_PROFILES_DIR", str(BASE_DIR / "profiles.d"))
)
ACTIVE_PATH = BASE_DIR / "node.yaml"
ACTIVE_PROFILE_NAME_PATH = BASE_DIR / "node.profile"


def _migrate_old_paths() -> None:
    """Migrate old capability-named files to new node-named files.

    Called once at module import time. Detects legacy paths and copies
    them to the new locations. Old files are NOT deleted — they are
    left in place for backward compat during a rolling deploy.
    """
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    old_active = BASE_DIR / "capabilities.active.yaml"
    new_active = ACTIVE_PATH
    if old_active.exists() and not new_active.exists():
        try:
            shutil.copy2(old_active, new_active)
        except OSError:
            pass
    old_profile = BASE_DIR / "capabilities.active.profile"
    new_profile = ACTIVE_PROFILE_NAME_PATH
    if old_profile.exists() and not new_profile.exists():
        try:
            shutil.copy2(old_profile, new_profile)
        except OSError:
            pass
    old_profiles_dir = BASE_DIR / "capabilities.d"
    new_profiles_dir = PROFILES_DIR
    if old_profiles_dir.exists() and not new_profiles_dir.exists():
        try:
            shutil.copytree(old_profiles_dir, new_profiles_dir, dirs_exist_ok=True)
        except OSError:
            pass


_migrate_old_paths()

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_VERSION = "1.0.0"
DEFAULT_MAX_PARALLEL = 1
DEFAULT_TIMEOUT = 300

# Required normalized keys in order (for stable output / diffs).
_NORMALIZED_KEYS = (
    "name",
    "version",
    "description",
    "auto_publish",
    "claimable",
    "handler",
    "max_parallel",
    "timeout",
    "routes",
)


class CapabilityValidationError(ValueError):
    """Raised when a capability profile fails validation.

    The message is intended for human consumption and always includes
    enough context (file path, offending capability name) to locate the
    problem. The optional ``file`` and ``line`` attributes allow callers
    to render structured error messages.
    """

    def __init__(
        self,
        message: str,
        *,
        file: str | None = None,
        line: int | None = None,
        capability: str | None = None,
    ) -> None:
        location = ""
        if file:
            location = file
            if line is not None:
                location += f":{line}"
            if capability:
                location += f" (capability '{capability}')"
        full = f"{location}: {message}" if location else message
        super().__init__(full)
        self.message = message
        self.file = file
        self.line = line
        self.capability = capability


# ---------------------------------------------------------------------------
# Env-var override helpers
# ---------------------------------------------------------------------------

def _env_var_name(capability_name: str, suffix: str) -> str:
    """Build the environment-variable name for a capability override.

    ``<NAME>`` is uppercased with dots and hyphens normalized to
    underscores, e.g. ``chat.ai`` -> ``RELAY_CAPABILITY_CHAT_AI_HANDLER``.
    """
    normalized = re.sub(r"[^A-Za-z0-9]", "_", capability_name).upper()
    return f"RELAY_CAPABILITY_{normalized}_{suffix}"


def _apply_env_overrides(cap: dict[str, Any]) -> dict[str, Any]:
    """Apply per-capability env-var overrides in-place and return the cap."""
    name = cap["name"]
    handler = os.environ.get(_env_var_name(name, "HANDLER"))
    if handler:
        cap["handler"] = handler
    max_parallel_env = os.environ.get(_env_var_name(name, "MAX_PARALLEL"))
    if max_parallel_env is not None:
        try:
            cap["max_parallel"] = int(max_parallel_env)
        except ValueError:
            raise CapabilityValidationError(
                f"env var {_env_var_name(name, 'MAX_PARALLEL')!r}="
                f"{max_parallel_env!r} is not an integer",
                capability=name,
            ) from None
    return cap


# ---------------------------------------------------------------------------
# Normalization & validation
# ---------------------------------------------------------------------------

def _normalize_capability(
    raw: Any,
    *,
    file: str | None = None,
    index: int | None = None,
) -> dict[str, Any]:
    """Normalize and validate a single capability entry.

    Raises :class:`CapabilityValidationError` on any structural problem.
    """
    if not isinstance(raw, dict):
        where = f"#{index}" if index is not None else "entry"
        raise CapabilityValidationError(
            f"capability entry {where} is not a mapping",
            file=file,
        )

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise CapabilityValidationError(
            "capability is missing required 'name' (must be non-empty string)",
            file=file,
            line=None,
            capability=name if isinstance(name, str) else None,
        )

    auto_publish = raw.get("auto_publish", True)
    if not isinstance(auto_publish, bool):
        raise CapabilityValidationError(
            "'auto_publish' must be a boolean",
            file=file,
            capability=name,
        )

    claimable = raw.get("claimable", False)
    if not isinstance(claimable, bool):
        raise CapabilityValidationError(
            "'claimable' must be a boolean",
            file=file,
            capability=name,
        )

    handler = raw.get("handler")
    if claimable and (not isinstance(handler, str) or not handler.strip()):
        raise CapabilityValidationError(
            "'handler' is required when claimable is true",
            file=file,
            capability=name,
        )
    if handler is not None and not isinstance(handler, str):
        raise CapabilityValidationError(
            "'handler' must be a string (path or shell command)",
            file=file,
            capability=name,
        )

    max_parallel = raw.get("max_parallel", DEFAULT_MAX_PARALLEL)
    if not isinstance(max_parallel, int) or isinstance(max_parallel, bool):
        raise CapabilityValidationError(
            "'max_parallel' must be a positive integer",
            file=file,
            capability=name,
        )
    if max_parallel < 1:
        raise CapabilityValidationError(
            "'max_parallel' must be a positive integer (>= 1)",
            file=file,
            capability=name,
        )

    timeout = raw.get("timeout", DEFAULT_TIMEOUT)
    if not isinstance(timeout, int) or isinstance(timeout, bool):
        raise CapabilityValidationError(
            "'timeout' must be a positive integer",
            file=file,
            capability=name,
        )
    if timeout < 1:
        raise CapabilityValidationError(
            "'timeout' must be a positive integer (>= 1)",
            file=file,
            capability=name,
        )

    version = raw.get("version", DEFAULT_VERSION)
    if not isinstance(version, str) or not version.strip():
        raise CapabilityValidationError(
            "'version' must be a non-empty string",
            file=file,
            capability=name,
        )

    cap: dict[str, Any] = {
        "name": name,
        "version": version,
        "auto_publish": auto_publish,
        "claimable": claimable,
        "handler": handler if isinstance(handler, str) else "",
        "max_parallel": max_parallel,
        "timeout": timeout,
    }
    # T-053/T-056: forward optional metadata fields so the heartbeat
    # can populate node_capabilities.{type,description,input_schema}
    # and the server can resolve capability_details on claim/task-view.
    if raw.get("type") is not None:
        cap["type"] = raw["type"]
    if raw.get("description") is not None:
        cap["description"] = raw["description"]
    if raw.get("input_schema") is not None:
        cap["input_schema"] = raw["input_schema"]
    # T-075: forward routes so the heartbeat can register dynamic API endpoints.
    if raw.get("routes") is not None:
        cap["routes"] = raw["routes"]
    # T-154: forward long_run so the daemon can apply the 2h accepted-TTL
    # instead of the default 300s claimed-TTL for long-running capabilities.
    if raw.get("long_run") is not None:
        cap["long_run"] = raw["long_run"]
    # T-164: forward upload_modes so the heartbeat can populate
    # node_capabilities.upload_modes and the server can tell node-cli
    # which transfer modes (inline/artifact/bridge) this capability supports.
    if raw.get("upload_modes") is not None:
        cap["upload_modes"] = raw["upload_modes"]
    # T-004 (iowap-flow): forward result_path_hints so the heartbeat can
    # publish them via node_capabilities and the flow planner can use
    # valid ${ref.result.path} template paths.
    if raw.get("result_path_hints") is not None:
        cap["result_path_hints"] = raw["result_path_hints"]
    # T-005b: per-capability config mapping (carrier of the
    # envelope_request_mirror rollout toggle; design.md §3.2). The
    # daemon's complete_by_script gate already reads cap["config"].
    if raw.get("config") is not None:
        cap["config"] = raw["config"]
    # Apply env-var overrides (may raise CapabilityValidationError).
    _apply_env_overrides(cap)
    # Re-validate handler after overrides: an override could clear it.
    if cap["claimable"] and not cap["handler"].strip():
        raise CapabilityValidationError(
            "'handler' is required when claimable is true "
            "(env override may have cleared it)",
            file=file,
            capability=name,
        )
    return cap


def _normalize_caps_list(
    raw_caps: Any,
    *,
    file: str | None,
) -> list[dict[str, Any]]:
    """Validate the ``capabilities`` list and return normalized caps."""
    if not isinstance(raw_caps, list):
        raise CapabilityValidationError(
            "'capabilities' key missing or not a list",
            file=file,
        )

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw_caps):
        cap = _normalize_capability(entry, file=file, index=i)
        if cap["name"] in seen:
            raise CapabilityValidationError(
                f"duplicate capability name {cap['name']!r}",
                file=file,
                capability=cap["name"],
            )
        if cap["name"] == FORWARD_CAPABILITY:
            raise CapabilityValidationError(
                "'forward' is the relay's forward-routing pseudo-capability and must not be a node "
                "capability — it is never advertised (submit with --forward-slot instead; see "
                "the IOWAP docs, concepts/forward-routing.md)",
                file=file,
                capability=cap["name"],
            )
        seen.add(cap["name"])
        normalized.append(cap)
    return normalized


def validate_profile(
    source: str | os.PathLike[str] | dict[str, Any] | Path,
) -> list[dict[str, Any]]:
    """Validate a profile and return the normalized capabilities list.

    Accepts either a path to a YAML file or an already-parsed dict.
    Raises :class:`CapabilityValidationError` on any problem — including
    a declared ``forward_slots`` section the daemon's row pipeline
    cannot parse (T-239: fail-fast at validate/publish time instead of
    the first heartbeat tick).
    """
    if isinstance(source, dict):
        # Schema validation first
        schema_errors = validate_with_schema(source)
        if schema_errors:
            raise CapabilityValidationError(
                "schema validation failed:\n  " + "\n  ".join(schema_errors)
            )
        if source.get("forward_slots") is not None:
            # T-239: the source's own forward_slots declaration is
            # validated through the same row pipeline the daemon
            # heartbeat uses.
            _validate_forward_slots_section(source["forward_slots"])
        if "capabilities" not in source:
            return []
        return _normalize_caps_list(source["capabilities"], file=None)

    path = Path(source)
    file_label = str(path)
    if not path.exists():
        raise CapabilityValidationError(f"profile file not found: {file_label}")
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CapabilityValidationError(f"cannot read profile {file_label}: {exc}") from exc
    try:
        parsed = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        # yaml.MarkedYAMLError carries a problem line; surface it if present.
        line = getattr(exc, "problem_mark", None)
        line_no = line.line + 1 if line is not None else None
        raise CapabilityValidationError(
            f"YAML syntax error: {exc}",
            file=file_label,
            line=line_no,
        ) from exc

    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise CapabilityValidationError(
            "profile root must be a mapping",
            file=file_label,
        )

    # Schema validation
    schema_errors = validate_with_schema(parsed)
    if schema_errors:
        raise CapabilityValidationError(
            "schema validation failed:\n  " + "\n  ".join(schema_errors),
            file=file_label,
        )

    if parsed.get("forward_slots") is not None:
        # T-239: the source's own forward_slots declaration is validated
        # through the same row pipeline the daemon heartbeat uses — a
        # declaration the daemon cannot parse never reaches the active
        # file ("publish OK" followed by silent nodes is fixed).
        _validate_forward_slots_section(parsed["forward_slots"], file_label)
    if "capabilities" not in parsed:
        return []
    return _normalize_caps_list(parsed["capabilities"], file=file_label)


def load_profile(path: str | os.PathLike[str] | Path) -> list[dict[str, Any]]:
    """Load and validate a profile from disk. Alias for :func:`validate_profile`."""
    return validate_profile(path)


# ---------------------------------------------------------------------------
# Profile discovery / publish / active profile management
# ---------------------------------------------------------------------------

def list_profiles() -> list[Path]:
    """Return sorted list of ``*.yaml`` / ``*.yml`` profiles in PROFILES_DIR."""
    if not PROFILES_DIR.exists():
        return []
    profiles = [
        p for p in PROFILES_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in (".yaml", ".yml")
    ]
    return sorted(profiles, key=lambda p: p.name)


def profile_path(name: str) -> Path:
    """Resolve a profile name to a path inside ``profiles.d/``.

    Accepts either a bare name (``default``) or a full filename
    (``default.yaml``). Does not check existence.
    """
    if name.endswith((".yaml", ".yml")):
        return PROFILES_DIR / name
    return PROFILES_DIR / f"{name}.yaml"


def current_profile_name() -> str | None:
    """Return the name of the active profile, or ``None`` if unset."""
    if not ACTIVE_PROFILE_NAME_PATH.exists():
        return None
    text = ACTIVE_PROFILE_NAME_PATH.read_text(encoding="utf-8").strip()
    return text or None


def write_current_profile_name(name: str) -> None:
    ACTIVE_PROFILE_NAME_PATH.write_text(name + "\n", encoding="utf-8")


def publish_profile(name: str) -> Path:
    """Validate a working profile then atomically copy it to the active file.

    Also records the profile name in ``node.profile``.
    Returns the path of the active file. Never touches the active file
    if validation fails.
    """
    src = profile_path(name)
    if not src.exists():
        raise CapabilityValidationError(f"profile not found: {name} ({src})")
    # Validate first — raises on any problem and leaves active untouched.
    validate_profile(src)
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic write to active file via temp + rename.
    tmp = ACTIVE_PATH.with_suffix(ACTIVE_PATH.suffix + ".tmp")
    # T-235 C3: the byte-exact copy would drop the active file's
    # forward_slots section — read the previous active text first and let
    # the heal restore the section into tmp before the single replace
    # (source wins; never raises, never creates).
    try:
        previous_text = ACTIVE_PATH.read_text(encoding="utf-8")
    except OSError:
        previous_text = None
    shutil.copyfile(src, tmp)
    _heal_forward_slots_section(tmp, previous_text)
    os.replace(tmp, ACTIVE_PATH)
    write_current_profile_name(name)
    # T-226: the byte-exact copy replaced the active file — re-stamp the
    # node_cli_path so the key self-heals through validate→publish even
    # when it is not part of the working profile.
    ensure_node_cli_stamp()
    return ACTIVE_PATH


def diff_profiles(
    old: list[dict[str, Any]] | None,
    new: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Compare two normalized capability lists.

    Returns ``{"added": [...], "removed": [...], "changed": [...]}``.
    Each ``added``/``changed`` entry is the full normalized cap dict;
    each ``removed`` entry is just the capability name.
    """
    old = old or []
    new = new or []
    old_map = {c["name"]: c for c in old}
    new_map = {c["name"]: c for c in new}
    old_names = set(old_map)
    new_names = set(new_map)

    added = [new_map[n] for n in sorted(new_names - old_names)]
    removed = sorted(old_names - new_names)
    changed: list[dict[str, Any]] = []
    for n in sorted(old_names & new_names):
        a = old_map[n]
        b = new_map[n]
        # Compare only the canonical keys (ignore key ordering).
        if {k: a.get(k) for k in _NORMALIZED_KEYS} != {
            k: b.get(k) for k in _NORMALIZED_KEYS
        }:
            changed.append({"name": n, "old": a, "new": b})
    return {"added": added, "removed": removed, "changed": changed}


# ---------------------------------------------------------------------------
# mtime-cached active profile loader
# ---------------------------------------------------------------------------

class ActiveProfileCache:
    """Thread-safe mtime-cached loader for the active profile.

    The daemon calls :meth:`get` before every heartbeat / claim-loop
    iteration. A changed mtime triggers a re-read and re-validation. A
    SIGHUP handler calls :meth:`invalidate` to force the next read.

    T-189 (F2): ``invalidate`` is called from a signal handler, which runs
    on the main thread — the same thread the polling daemon runs its claim
    loop on. It must therefore NEVER take the load lock: ``get`` holds it
    across the whole YAML parse, so a signal handler blocking on it would
    self-deadlock the daemon (only SIGKILL). Invalidation is a lock-free
    generation bump; a load that started before the bump refuses to store
    its (now stale) result.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or ACTIVE_PATH
        self._lock = threading.Lock()
        self._cached_mtime: float | None = None
        self._cached_caps: list[dict[str, Any]] | None = None
        self._cached_generation = -1
        # Monotonic invalidation counter. Bumped (atomically under the GIL)
        # by invalidate() WITHOUT the lock; a loader captures it before the
        # read and only commits if the generation is unchanged afterwards.
        self._generation = 0

    def invalidate(self) -> None:
        """Drop the cache and force the next :meth:`get` to re-read disk.

        Lock-free by contract (T-189): safe to call from a signal handler.
        """
        self._generation += 1

    def get(self) -> list[dict[str, Any]]:
        """Return the active profile, reloading if mtime changed.

        If the active file does not exist, returns an empty list (the
        daemon treats this as "no capabilities published yet").
        """
        with self._lock:
            if not self.path.exists():
                self._cached_mtime = None
                self._cached_caps = None
                return []
            try:
                mtime = self.path.stat().st_mtime
            except OSError:
                return self._cached_caps or []
            # A load that raced with an invalidate() must not commit its
            # stale result — re-read whenever the generation moved.
            generation = self._generation
            if (
                self._cached_caps is None
                or mtime != self._cached_mtime
                or generation != self._cached_generation
            ):
                caps = validate_profile(self.path)
                self._cached_caps = caps
                self._cached_mtime = mtime
                self._cached_generation = generation
            return self._cached_caps


# Module-level singleton used by the daemon.
_active_cache = ActiveProfileCache()


def load_active_profile() -> list[dict[str, Any]]:
    """Convenience accessor for the module-level active profile cache."""
    return _active_cache.get()


def load_forward_slots() -> list[dict[str, Any]]:
    """Read the node's declared forward slots (T-Forward-2, typed — T-235).

    The active YAML profile may carry a ``forward_slots`` section —
    either as a mapping (``"1": {label, description, ...}``) or as a list of
    the same objects. Rows are the frozen 8-key form: ``slot``, ``label``,
    ``description``, ``type`` (resolved via the client registry, never
    ``None``), ``value`` (native str/bool/None), ``ref_slot``, ``ref_field``
    and ``input_schema`` (dict, forward slots only). label/description are
    stripped (empty → ``None``); the empty list means "no declared slots".
    Malformed rows raise ``ValueError`` so operator mistakes are loud.
    """
    path = ACTIVE_PATH
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw)
    if not isinstance(parsed, dict):
        return []
    section = parsed.get("forward_slots")
    if section is None:
        return []
    items = _forward_slot_items(section)
    rows = [_build_forward_slot_row(raw_slot, entry) for raw_slot, entry in items]
    return _crosscheck_forward_slot_refs(rows)


# ---------------------------------------------------------------------------
# T-235 forward-slot typed declarations (parser row pipeline + publish heal)
# ---------------------------------------------------------------------------

def _forward_slot_items(section: Any) -> list[tuple[Any, Any]]:
    """Split a ``forward_slots`` section into ``(raw_slot, entry)`` pairs.

    Mapping form → ``list(section.items())``; list form → ``(entry["slot"],
    entry)`` for dict entries and ``(None, entry)`` for non-dict entries
    (the caller's slot-required contract turns those into a loud
    ``ValueError`` — the old list branch crashed on scalars with an
    incidental ``AttributeError``, node_config.py:731 — Decision A4).
    Any other shape → ``ValueError("forward_slots must be a mapping or a
    list of objects")`` (frozen message, node_config.py:733). Shared by
    the parser and the publish heal (T-235 C2/C3).
    """
    if isinstance(section, dict):
        return list(section.items())
    if isinstance(section, list):
        return [
            (entry.get("slot"), entry) if isinstance(entry, dict) else (None, entry)
            for entry in section
        ]
    raise ValueError("forward_slots must be a mapping or a list of objects")


def _build_forward_slot_row(raw_slot: Any, entry: Any) -> dict[str, Any]:
    """Normalize ONE forward_slots declaration row via the client registry.

    ValueError-loud (Bestandsvertrag, node_config.py:715). Entry forms:
    dict (full — typed fields via ``normalize_slot_type``/``normalize_slot_value``/
    ``normalize_slot_ref`` + ``input_schema`` dict-only-on-forward); str
    (legacy mapping-form description, node_config.py:742-743); other
    (label/description None). ``raw_slot is None`` → ``ValueError
    ("forward_slots entries require a slot")`` (frozen, :735-736). Returns
    the frozen 8-key row (``slot``, ``label``, ``description``, ``type``,
    ``value``, ``ref_slot``, ``ref_field``, ``input_schema`` — design §2.2).
    """
    if raw_slot is None:
        raise ValueError("forward_slots entries require a slot")
    slot = normalize_forward_slot(raw_slot)
    if isinstance(entry, dict):
        raw_label = entry.get("label")
        raw_description = entry.get("description")
        slot_type = normalize_slot_type(entry.get("type"))
        value = normalize_slot_value(slot_type, entry.get("value"))
        ref = normalize_slot_ref(entry, slot_type)
        raw_schema = entry.get("input_schema")
        if raw_schema is not None:
            if slot_type != SLOT_TYPE_FORWARD:
                raise ForwardSlotError(
                    f"slot '{slot}': input_schema is only allowed on forward slots",
                    code="forward_slot_invalid",
                )
            # D-05 structure-only: dict-or-loud, the schema's CONTENT is not
            # validated here (server DEFERRED D-05/D-09 → T-247).
            if not isinstance(raw_schema, dict):
                raise ForwardSlotError(
                    f"slot '{slot}': input_schema must be an object,"
                    f" got {type(raw_schema).__name__}",
                    code="forward_slot_invalid",
                )
    else:
        raw_label = None
        raw_description = entry if isinstance(entry, str) else None
        slot_type = SLOT_TYPE_FORWARD
        value = None
        ref = None
        raw_schema = None
    ref_slot, ref_field = ref if ref is not None else (None, None)
    label_value = raw_label.strip() if isinstance(raw_label, str) else None
    description_value = (
        raw_description.strip() if isinstance(raw_description, str) else None
    )
    return {
        "slot": slot,
        "label": label_value or None,
        "description": description_value or None,
        "type": slot_type,
        "value": value,
        "ref_slot": ref_slot,
        "ref_field": ref_field,
        "input_schema": raw_schema,
    }


def _crosscheck_forward_slot_refs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe last-wins + ref-target check across one declaration (in-memory).

    Mirror of the server's phase A2 (core/forward.py:446-460): every
    ``ref_slot`` must exist in the same declaration and target a
    ``forward`` slot; refs are checked against the LAST-WINS type map,
    per-row over ALL built rows (duplicate occurrences included). Returns
    deduped rows: one per slot, first-occurrence position, last value wins
    (server upsert mirror, core/forward.py:483-488 — Decision A3). Raises
    ``ValueError`` (via ``ForwardSlotError``) loud.
    """
    type_map: dict[str, str] = {row["slot"]: row["type"] for row in rows}
    for row in rows:
        if row["ref_slot"] is None:
            continue
        if type_map.get(row["ref_slot"]) != SLOT_TYPE_FORWARD:
            raise ForwardSlotError(
                f"slot '{row['slot']}': ref_slot '{row['ref_slot']}' must"
                " reference a forward slot declared in the same request",
                code="forward_slot_invalid",
            )
    # Dedupe last-wins (server upsert mirror): one emitted row per slot,
    # first-occurrence position, last value wins.
    last: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        if row["slot"] not in last:
            order.append(row["slot"])
        last[row["slot"]] = row
    return [last[slot] for slot in order]


def _validate_forward_slots_section(section: Any, file_label: str | None = None) -> None:
    """Run the load_forward_slots row pipeline over ONE declaration section.

    T-239 (audit F3): ``validate_profile`` used to leave ``forward_slots``
    unchecked (schema root ``additionalProperties: true``) and the publish
    heal only validated the PREVIOUS block — the first checker for the
    source section was ``load_forward_slots()`` in the daemon heartbeat.
    The result: ``capabilities publish`` said OK, then every heartbeat
    tick died BEFORE the POST and the node sat silent at the relay.
    Now the same row pipeline runs at validate/publish time — a
    declaration the daemon cannot parse never reaches the active file.

    Raises ``CapabilityValidationError`` (wrapping the row pipeline's
    ``ValueError``/``ForwardSlotError``). Callers that want raw errors
    keep using the pipeline directly (load_forward_slots keeps its
    ``ValueError`` contract, pinned by test_t235).

    TODO (T-239, intentionally NOT implemented): a degradation path —
    keep heartbeating with the LAST VALID slot list plus a warning when
    the new declaration is invalid. That needs a last-known-good slot
    cache next to the active file and a policy answer for "profile says
    X, node sends Y-1" at the replace-based server; fail-fast at publish
    plus the T-237 status-file error surface is the cheaper honest
    contract for now.
    """
    try:
        rows = [
            _build_forward_slot_row(raw_slot, entry)
            for raw_slot, entry in _forward_slot_items(section)
        ]
        _crosscheck_forward_slot_refs(rows)
    except ValueError as exc:
        raise CapabilityValidationError(
            f"forward_slots: {exc}", file=file_label
        ) from exc


def _extract_forward_slots_block(text: str) -> str | None:
    """Text-based extraction of a top-level ``forward_slots`` block.

    Returns the block from the ``forward_slots:`` key line through the line
    before the next top-level key (``^[A-Za-z_][A-Za-z0-9_-]*\\s*:(\\s|$)``
    at column 0) or EOF; ``None`` when the key is absent. Col-0 ``- ``
    list items belong to the block. Pure text scan — never raises, never
    modifies. Caller applies the YAML round-trip pin (design §2.3 step 5).
    """
    key_line = re.compile(r"^forward_slots\s*:(\s|$)")
    boundary = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*\s*:(\s|$)")
    lines = text.splitlines(keepends=True)
    start = None
    for idx, line in enumerate(lines):
        if key_line.match(line):
            start = idx
            break
    if start is None:
        return None
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        if boundary.match(lines[idx]):
            end = idx
            break
    return "".join(lines[start:end])


def _heal_forward_slots_section(tmp: Path, previous_text: str | None) -> None:
    """C3 Option A: restore the forward_slots section missing from the source.

    Source-wins rule (design §2.3 guard matrix): a source whose parsed dict
    carries a non-``None`` ``forward_slots`` (non-empty OR explicit empty
    ``[]``/``{}``) is authoritative — no write. Only when the section is
    ABSENT and the previous active file carried one, the previous section
    is validated via the shared row pipeline (``_forward_slot_items`` →
    ``_build_forward_slot_row`` → ``_crosscheck_forward_slot_refs``), its
    text extracted (``_extract_forward_slots_block`` + YAML round-trip pin,
    ``yaml.safe_dump`` fallback) and appended at EOF with the join rule
    ``text.rstrip("\\n") + "\\n\\n" + block.rstrip("\\n") + "\\n"``. Never
    raises (warn-and-skip over ``(OSError, yaml.YAMLError, ValueError)``,
    logger ``relay.node_config`` — same channel as ensure_node_cli_stamp,
    node_config.py:901-904); never creates a section.
    """
    if previous_text is None:
        return
    try:
        if not tmp.exists():
            return
        tmp_text = tmp.read_text(encoding="utf-8")
        doc = yaml.safe_load(tmp_text)
        # Source wins: a source that carries the section (non-empty OR the
        # explicit []/{} removal marker) is authoritative — A6 includes
        # explicit ``forward_slots: null`` in the HEAL branch (absent-like).
        if not isinstance(doc, dict) or doc.get("forward_slots") is not None:
            return
        previous_doc = yaml.safe_load(previous_text)
        if not isinstance(previous_doc, dict) or previous_doc.get("forward_slots") is None:
            return
        previous_section = previous_doc.get("forward_slots")
        # Step 4: validate the previous section through the shared row
        # pipeline, so a restored section is guaranteed parser-readable.
        rows = [
            _build_forward_slot_row(raw_slot, entry)
            for raw_slot, entry in _forward_slot_items(previous_section)
        ]
        _crosscheck_forward_slot_refs(rows)
        # Step 5: extract the block text with the YAML round-trip pin;
        # a pin failure (e.g. duplicate top-level keys) falls back to a
        # normalized, data-preserving safe_dump block. An absent block
        # (extractor None → "") can never pin — it goes to the fallback.
        block = _extract_forward_slots_block(previous_text) or ""
        pinned = False
        try:
            parsed_block = yaml.safe_load(block)
            pinned = (
                isinstance(parsed_block, dict)
                and parsed_block.get("forward_slots") == previous_section
            )
        except yaml.YAMLError:
            pinned = False
        if not pinned:
            block = yaml.safe_dump(
                {"forward_slots": previous_section},
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
            )
        # Step 6: single EOF insertion into tmp — the publish os.replace
        # below stays the ONLY write transaction.
        new_text = tmp_text.rstrip("\n") + "\n\n" + block.rstrip("\n") + "\n"
        tmp.write_text(new_text, encoding="utf-8")
    except (OSError, yaml.YAMLError, ValueError) as exc:
        __import__("logging").getLogger("relay.node_config").warning(
            "could not restore forward_slots section in %s: %s", tmp, exc
        )


def load_active_status() -> str | None:
    """Read the ``status`` field from the active YAML profile.

    Returns ``None`` when no explicit status is set (the node defaults
    to ``online``). The value is read fresh from disk every call so
    ``node-cli node busy`` / ``idle`` (which write directly into the
    active YAML) are picked up on the next heartbeat without a cache
    invalidation.
    """
    path = ACTIVE_PATH
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(raw)
        if isinstance(parsed, dict):
            val = parsed.get("status")
            if isinstance(val, str) and val in ("busy", "idle", "online"):
                return val
    except (OSError, yaml.YAMLError):
        pass
    return None


_STATUS_RE = re.compile(r'^(status:\s*).*', re.MULTILINE)


def write_active_status(status: str | None) -> None:
    """Write (or remove) the ``status`` field in the active YAML profile.

    When ``status`` is ``None`` the field is removed from the YAML so
    the node returns to its default ``online`` state. The file is
    rewritten in place via temp + rename, preserving the rest of the
    document verbatim (the YAML is edited as text so comments, key
    ordering and formatting are kept intact). If the active profile
    does not exist yet, this is a no-op.
    """
    path = ACTIVE_PATH
    if not path.exists():
        return
    try:
        raw = path.read_text(encoding="utf-8")
        if status is None:
            # Remove the status: line.
            new_text = _STATUS_RE.sub("", raw)
            # Clean up double blank lines from removal.
            new_text = re.sub(r'\n{3,}', '\n\n', new_text)
        else:
            if _STATUS_RE.search(raw):
                new_text = _STATUS_RE.sub(rf"\1{status}", raw)
            else:
                # Insert after the first line (usually node_name or the
                # capabilities: key) so the field sits at the top of the
                # document.
                first_newline = raw.index("\n") + 1 if "\n" in raw else len(raw)
                new_text = raw[:first_newline] + f"status: {status}\n" + raw[first_newline:]
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, yaml.YAMLError) as exc:
        log = __import__("logging").getLogger("relay.node_config")
        log.warning("could not write status to YAML: %s", exc)


def invalidate_active_cache() -> None:
    """Force the next :func:`load_active_profile` call to re-read disk."""
    _active_cache.invalidate()


# ---------------------------------------------------------------------------
# node_cli_path stamp (T-226)
# ---------------------------------------------------------------------------

_NODE_CLI_STAMP_KEY = "node_cli_path"
# Top-level only: 'node_cli_path:' at column 0. MULTILINE '^' matches
# line starts, and capability-level keys are indented — those never match.
_NODE_CLI_STAMP_RE = re.compile(
    rf"^({_NODE_CLI_STAMP_KEY}):(.*)$", re.MULTILINE
)


def resolve_own_cli_path() -> str | None:
    """Best-effort absolute path of the node-cli console script.

    Preference order: the ``node-cli`` sibling of the running interpreter
    (covers ``python -m nodes.common.node_cli`` AND console-script runs —
    both exec from the same venv), then argv[0] when it *is* the cli
    script (symlink-resolved, e.g. pipx), then ``shutil.which``. Returns
    ``None`` when nothing derivable exists so callers stamp nothing.
    """
    exe = str(sys.executable or "")
    if exe:
        cand = Path(exe).with_name("node-cli")
        if cand.is_file() and os.access(cand, os.X_OK):
            return os.path.realpath(cand)
    argv0 = sys.argv[0] if sys.argv else ""
    if os.path.basename(argv0) in ("node-cli", "node_cli.py"):
        try:
            resolved = Path(argv0).resolve()
        except (OSError, ValueError):
            resolved = None
        if resolved and resolved.is_file() and os.access(resolved, os.X_OK):
            return os.path.realpath(resolved)
    which = shutil.which("node-cli")
    if which:
        return os.path.realpath(which)
    return None


def ensure_node_cli_stamp(stamp: str | None = None) -> None:
    """Ensure the active profile carries the node_cli_path stamp (T-226).

    Consumers of node-cli that cannot rely on PATH (the Hermes desktop
    gateway does not inherit the login-shell PATH) read the top-level
    ``node_cli_path`` key from the active profile. The stamp:

    * never raises and never creates node.yaml (register-only hosts stay
      untouched — the consumer falls back to PATH/known locations);
    * is drift-guarded: rewrites ONLY when the recorded value differs,
      so the steady state is mtime-silent (the daemon's
      ActiveProfileCache reloads on mtime change);
    * edits the YAML as text — comments, key order and formatting are
      preserved verbatim (same discipline as ``write_active_status``).

    ``stamp`` overrides the derived path (tests, callers that know
    better); pass an empty string to make the call a no-op.
    """
    value = stamp if stamp is not None else resolve_own_cli_path()
    if not value:
        return
    path = ACTIVE_PATH
    if not path.exists():
        return
    try:
        raw = path.read_text(encoding="utf-8")
        m = _NODE_CLI_STAMP_RE.search(raw)
        current = m.group(2).strip().strip("'\"") if m else None
        if current == value:
            return
        quoted = '"' + value.replace('"', "") + '"'
        if m:
            new_text = raw[: m.start(2)] + " " + quoted + raw[m.end(2):]
        else:
            new_text = f"{_NODE_CLI_STAMP_KEY}: {quoted}\n" + raw
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, yaml.YAMLError) as exc:
        __import__("logging").getLogger("relay.node_config").warning(
            "could not record node_cli_path in %s: %s", path, exc
        )


# ---------------------------------------------------------------------------
# Serialization helpers (for `capabilities diff` output)
# ---------------------------------------------------------------------------

def caps_to_json(caps: list[dict[str, Any]]) -> str:
    """Pretty-print normalized caps as JSON with stable key order."""
    return json.dumps(caps, indent=2, sort_keys=False, default=str)
