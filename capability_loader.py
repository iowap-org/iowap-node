#!/usr/bin/env python3
"""Capability profile loader for the node-cli daemon.

This module is responsible for loading, validating, publishing, and
diffing capability profiles stored as YAML files under
``~/.relay/capabilities.d/``. The daemon only ever reads the *active*
profile (``~/.relay/capabilities.active.yaml``); working profiles in
``capabilities.d/`` are never touched by the daemon at runtime.

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
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# JSON Schema for capability profiles (Draft 2020-12)
# ---------------------------------------------------------------------------

CAPABILITY_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["capabilities"],
    "properties": {
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
                    "dashboard_page": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        }
    },
    "additionalProperties": False,
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
    if "capabilities" not in data:
        errors.append("'capabilities' key is required")
        return errors
    if not isinstance(data["capabilities"], list):
        errors.append("'capabilities' must be a list")
        return errors

    for i, entry in enumerate(data["capabilities"]):
        prefix = f"capabilities[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix}: must be a mapping, got {type(entry).__name__}")
            continue
        if "name" not in entry or not isinstance(entry.get("name"), str) or not entry["name"].strip():
            errors.append(f"{prefix}: 'name' is required and must be a non-empty string")
        # Check for unknown keys
        allowed = {"name", "version", "type", "description", "input_schema", "auto_publish", "claimable", "handler", "max_parallel", "timeout", "dashboard_page"}
        extra = set(entry.keys()) - allowed
        if extra:
            errors.append(f"{prefix}: unknown keys: {', '.join(sorted(extra))}")
        # Type checks for optional fields
        for key, expected_type in [
            ("version", str),
            ("auto_publish", bool),
            ("claimable", bool),
            ("handler", str),
            ("max_parallel", int),
            ("timeout", int),
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
    os.environ.get("RELAY_PROFILES_DIR", str(BASE_DIR / "capabilities.d"))
)
ACTIVE_PATH = BASE_DIR / "capabilities.active.yaml"
ACTIVE_PROFILE_NAME_PATH = BASE_DIR / "capabilities.active.profile"

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
    "dashboard_page",
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

    dashboard_page = raw.get("dashboard_page", False)
    if not isinstance(dashboard_page, bool):
        raise CapabilityValidationError(
            "'dashboard_page' must be a boolean",
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
        "dashboard_page": dashboard_page,
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
        seen.add(cap["name"])
        normalized.append(cap)
    return normalized


def validate_profile(
    source: str | os.PathLike[str] | dict[str, Any] | Path,
) -> list[dict[str, Any]]:
    """Validate a profile and return the normalized capabilities list.

    Accepts either a path to a YAML file or an already-parsed dict.
    Raises :class:`CapabilityValidationError` on any problem.
    """
    if isinstance(source, dict):
        # Schema validation first
        schema_errors = validate_with_schema(source)
        if schema_errors:
            raise CapabilityValidationError(
                "schema validation failed:\n  " + "\n  ".join(schema_errors)
            )
        if "capabilities" not in source:
            raise CapabilityValidationError("'capabilities' key missing")
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
            "profile root must be a mapping with a 'capabilities' key",
            file=file_label,
        )

    # Schema validation
    schema_errors = validate_with_schema(parsed)
    if schema_errors:
        raise CapabilityValidationError(
            "schema validation failed:\n  " + "\n  ".join(schema_errors),
            file=file_label,
        )

    if "capabilities" not in parsed:
        raise CapabilityValidationError(
            "'capabilities' key missing or not a list",
            file=file_label,
        )
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
    """Resolve a profile name to a path inside ``capabilities.d/``.

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

    Also records the profile name in ``capabilities.active.profile``.
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
    shutil.copyfile(src, tmp)
    os.replace(tmp, ACTIVE_PATH)
    write_current_profile_name(name)
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
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or ACTIVE_PATH
        self._lock = threading.Lock()
        self._cached_mtime: float | None = None
        self._cached_caps: list[dict[str, Any]] | None = None

    def invalidate(self) -> None:
        with self._lock:
            self._cached_mtime = None
            self._cached_caps = None

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
            if self._cached_caps is None or mtime != self._cached_mtime:
                caps = validate_profile(self.path)
                self._cached_caps = caps
                self._cached_mtime = mtime
            return self._cached_caps


# Module-level singleton used by the daemon.
_active_cache = ActiveProfileCache()


def load_active_profile() -> list[dict[str, Any]]:
    """Convenience accessor for the module-level active profile cache."""
    return _active_cache.get()


def invalidate_active_cache() -> None:
    """Force the next :func:`load_active_profile` call to re-read disk."""
    _active_cache.invalidate()


# ---------------------------------------------------------------------------
# Serialization helpers (for `capabilities diff` output)
# ---------------------------------------------------------------------------

def caps_to_json(caps: list[dict[str, Any]]) -> str:
    """Pretty-print normalized caps as JSON with stable key order."""
    return json.dumps(caps, indent=2, sort_keys=False, default=str)
