#!/usr/bin/env python3
"""Shared Capability module for all node types and the server CLI.

This module is used by both worker nodes and the relay server.
It provides the common data structures and helper functions for
working with capabilities:

    * ``CapabilityType``            - Enum of available capability types.
    * ``CapabilityInputField``     - Description of a single input field.
    * ``CapabilityInputSchema``    - Schema over all input fields with
                                     payload validation.
    * ``Capability``               - A single capability of a node.
    * ``CapabilitySet``            - Management of a node's full
                                     capability inventory.
    * ``load_capabilities_from_yaml`` - Loads a CapabilitySet from YAML.
    * ``diff_capabilities``        - Compares two CapabilitySets.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml


class CapabilityType(str, Enum):
    """Type of a capability - determines routing in the scheduler."""

    AI = "ai"
    TOOL = "tool"
    SCRIPT = "script"
    WORKFLOW = "workflow"
    RESOURCE = "resource"

    def __str__(self) -> str:
        """Return the string value of this enum member."""
        return self.value


# ---------------------------------------------------------------------------
# Input Schema
# ---------------------------------------------------------------------------


@dataclass
class CapabilityInputField:
    """
    Describes a single input field of a capability.

    Attributes:
        name:        Identifier of the field.
        type:        Type identifier as string (e.g. ``"string"``,
                     ``"integer"``, ``"number"``, ``"boolean"``,
                     ``"object"``, ``"array"``). Default is ``"string"``.
        required:    Whether the field is mandatory.
        default:     Default value used when the field is missing from
                     the payload.
        enum:        Optional list of allowed values.
        ge:          Optional numeric lower bound (>=).
        le:          Optional numeric upper bound (<=).
        description: Human-readable description of the field.
    """

    name: str
    type: str = "string"
    required: bool = False
    default: Any = None
    enum: Optional[list[Any]] = None
    ge: Optional[float] = None
    le: Optional[float] = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize this field to a dictionary."""
        d: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "default": self.default,
            "enum": self.enum,
            "ge": self.ge,
            "le": self.le,
            "description": self.description,
        }
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CapabilityInputField:
        """Create a field from a dictionary."""
        return cls(
            name=data["name"],
            type=data.get("type", "string"),
            required=data.get("required", False),
            default=data.get("default", None),
            enum=data.get("enum"),
            ge=data.get("ge"),
            le=data.get("le"),
            description=data.get("description", ""),
        )

    def validate(self, value: Any) -> list[str]:
        """
        Validate a single value against this field schema.

        Returns:
            List of error messages (empty if the value is valid).
        """
        errors: list[str] = []

        if value is None:
            if self.required and self.default is None:
                errors.append(f"Field '{self.name}' is required.")
            return errors

        # Enum check
        if self.enum is not None and value not in self.enum:
            errors.append(
                f"Field '{self.name}': value {value!r} is not in "
                f"allowed values {self.enum!r}."
            )

        # Numeric bounds
        if self.ge is not None or self.le is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(
                    f"Field '{self.name}': value {value!r} must be numeric "
                    f"for ge/le validation."
                )
            else:
                if self.ge is not None and value < self.ge:
                    errors.append(
                        f"Field '{self.name}': value {value!r} must >= {self.ge}."
                    )
                if self.le is not None and value > self.le:
                    errors.append(
                        f"Field '{self.name}': value {value!r} must <= {self.le}."
                    )

        return errors


@dataclass
class CapabilityInputSchema:
    """
    Schema over all input fields of a capability.

    Attributes:
        fields: Mapping of field name to the corresponding \
                ``CapabilityInputField``.
    """

    fields: dict[str, CapabilityInputField] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize this schema to a dictionary."""
        return {
            "fields": {name: f.to_dict() for name, f in self.fields.items()}
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CapabilityInputSchema:
        """Create a schema from a dictionary."""
        if not data:
            return cls()
        raw_fields = data.get("fields", data) if isinstance(data, dict) else {}
        fields: dict[str, CapabilityInputField] = {}
        for raw in raw_fields.values() if isinstance(raw_fields, dict) else raw_fields:
            fld = CapabilityInputField.from_dict(raw)
            fields[fld.name] = fld
        return cls(fields=fields)

    def validate_payload(self, payload: dict[str, Any]) -> tuple[bool, list[str]]:
        """
        Validate a payload against this schema.

        Rules checked:
            * All fields marked ``required`` must be present.
            * Fields without a value receive their ``default``.
            * Enum constraints are enforced.
            * Numeric bounds (``ge`` / ``le``) are enforced.
            * Unexpected fields not defined in the schema are reported as
              errors.

        Returns:
            Tuple ``(is_valid, error_messages)``.
        """
        errors: list[str] = []
        if not isinstance(payload, dict):
            return False, ["Payload must be a dictionary."]

        # Check for unexpected fields
        for key in payload:
            if key not in self.fields:
                errors.append(f"Unexpected field '{key}' in payload.")

        # Check expected fields
        for name, fld in self.fields.items():
            if name not in payload or payload[name] is None:
                if fld.required and fld.default is None:
                    errors.append(f"Field '{name}' is required.")
                    continue
                # default is not written back into the payload -
                # validation uses the default if present.
                value = fld.default
            else:
                value = payload[name]

            if value is None:
                continue

            errors.extend(fld.validate(value))
        return (len(errors) == 0), errors


# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------


@dataclass
class Capability:
    """
    A single capability of a worker node.

    Attributes:
        name:         Unique identifier of the capability.
        type:         Type of capability (see ``CapabilityType``).
        description:  Human-readable description.
        version:      Semantic version of the capability.
        available:    Whether the capability is currently usable.
        input_schema: Optional input schema for validating call payloads.
        config:       Free-form configuration values of the capability.
        metadata:     Free-form metadata of the capability.
    """

    name: str
    type: CapabilityType
    description: str = ""
    version: str = "1.0.0"
    available: bool = True
    input_schema: Optional[CapabilityInputSchema] = None
    config: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize this capability to a dictionary."""
        d: dict[str, Any] = asdict(self)
        d["type"] = self.type.value if isinstance(self.type, CapabilityType) else self.type
        if self.input_schema is not None:
            d["input_schema"] = self.input_schema.to_dict()
        else:
            d["input_schema"] = None
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Capability:
        """Create a capability from a dictionary."""
        raw_type = data.get("type", "tool")
        raw_schema = data.get("input_schema")
        input_schema: Optional[CapabilityInputSchema] = None
        if raw_schema:
            input_schema = CapabilityInputSchema.from_dict(raw_schema)
        return cls(
            name=data["name"],
            type=CapabilityType(raw_type) if isinstance(raw_type, str) else raw_type,
            description=data.get("description", ""),
            version=data.get("version", "1.0.0"),
            available=data.get("available", True),
            input_schema=input_schema,
            config=data.get("config", {}) or {},
            metadata=data.get("metadata", {}) or {},
        )

    def merge(self, other: Capability) -> Capability:
        """
        Merge fields from ``other`` into this capability.

        Fields that are None or empty in ``other`` are skipped.
        ``name`` and ``type`` are always taken from ``self``.
        ``config`` and ``metadata`` are merged (values from ``other``
        override same-named keys from ``self``).
        """
        merged = copy.deepcopy(self)
        if other.description:
            merged.description = other.description
        if other.version:
            merged.version = other.version
        # ``available`` is also taken when False - otherwise there
        # would be no way to intentionally disable a capability.
        merged.available = other.available
        if other.input_schema is not None:
            merged.input_schema = copy.deepcopy(other.input_schema)
        merged.config = {**merged.config, **(other.config or {})}
        merged.metadata = {**merged.metadata, **(other.metadata or {})}
        return merged

    def patch_config(self, updates: dict[str, Any]) -> None:
        """Patch individual config values without replacing the entire config."""
        self.config.update(updates)

    def matches(
        self,
        name: Optional[str] = None,
        ctype: Optional[CapabilityType | str] = None,
        available_only: bool = True,
    ) -> bool:
        """Check if this capability matches the given filter criteria."""
        if name is not None and self.name != name:
            return False
        if ctype is not None:
            check = ctype.value if isinstance(ctype, CapabilityType) else str(ctype)
            self_val = self.type.value if isinstance(self.type, CapabilityType) else str(self.type)
            if self_val != check:
                return False
        if available_only and not self.available:
            return False
        return True


# ---------------------------------------------------------------------------
# CapabilitySet
# ---------------------------------------------------------------------------


@dataclass
class CapabilitySet:
    """
    Manage the full inventory of capabilities for a node.

    Attributes:
        _caps: Internal mapping of capability name to ``Capability``.
    """

    _caps: dict[str, Capability] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Ensure ``_caps`` is a dictionary."""
        if self._caps is None:
            self._caps = {}

    # ---- Registration ----------------------------------------------------

    def register(self, cap: Capability) -> None:
        """Register a capability. Existing entries are overwritten."""
        self._caps[cap.name] = cap

    def deregister(self, name: str) -> bool:
        """Remove a capability by name. Returns True if removed."""
        if name in self._caps:
            del self._caps[name]
            return True
        return False

    # ---- Access -----------------------------------------------------------

    def get(self, name: str) -> Optional[Capability]:
        """Get the capability with the given name or ``None``."""
        return self._caps.get(name)

    def update(self, name: str, **kwargs: Any) -> Optional[Capability]:
        """Update individual fields of a registered capability."""
        cap = self._caps.get(name)
        if cap is None:
            return None
        for key in ("description", "version", "available", "input_schema",
                    "config", "metadata"):
            if key in kwargs and kwargs[key] is not None:
                setattr(cap, key, kwargs[key])
        return cap

    def filter(
        self,
        name: Optional[str] = None,
        ctype: Optional[CapabilityType | str] = None,
        available_only: bool = True,
    ) -> list[Capability]:
        """Return all capabilities matching the filter criteria."""
        return [
            c for c in self._caps.values()
            if c.matches(name, ctype, available_only)
        ]

    @property
    def names(self) -> list[str]:
        """Return the list of all registered capability names."""
        return list(self._caps.keys())

    # ---- Serialization ----------------------------------------------------

    def to_list(self) -> list[dict[str, Any]]:
        """Serialize all capabilities to a list of dictionaries."""
        return [c.to_dict() for c in self._caps.values()]

    @classmethod
    def from_list(cls, data: list[dict[str, Any]]) -> CapabilitySet:
        """Create a CapabilitySet from a list of dictionaries."""
        return cls(
            _caps={c["name"]: Capability.from_dict(c) for c in data}
        )


# ---------------------------------------------------------------------------
# YAML Loader
# ---------------------------------------------------------------------------


def load_capabilities_from_yaml(path: Path) -> CapabilitySet:
    """
    Load capabilities from a YAML file.

    Expected YAML structure::

        capabilities:
          - name: vault
            type: tool
            description: "..."
            version: "1.2.0"
            available: true
            input_schema:
              fields:
                query:
                  name: query
                  type: string
                  required: true
            config: {}
            metadata: {}

    If the file does not exist, an empty ``CapabilitySet`` is returned.
    """
    path = Path(path)
    if not path.exists():
        return CapabilitySet()

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    raw_caps = raw.get("capabilities", []) if isinstance(raw, dict) else []
    caps: dict[str, Capability] = {}
    for entry in raw_caps:
        if not isinstance(entry, dict) or "name" not in entry:
            continue
        cap = Capability.from_dict(entry)
        caps[cap.name] = cap
    return CapabilitySet(_caps=caps)


# ---------------------------------------------------------------------------
# Diff Function
# ---------------------------------------------------------------------------


def diff_capabilities(old: CapabilitySet, new: CapabilitySet) -> dict[str, Any]:
    """
    Compare two CapabilitySets and return the differences.

    Return structure::

        {
            "added":   [<Capability.to_dict>, ...],
            "removed": [<name:str>, ...],
            "changed": [
                {"name": <str>, "old": <dict>, "new": <dict>},
                ...
            ],
        }
    """
    result: dict[str, Any] = {"added": [], "removed": [], "changed": []}
    old_names = set(old.names)
    new_names = set(new.names)

    for name in sorted(new_names - old_names):
        cap = new.get(name)
        if cap is not None:
            result["added"].append(cap.to_dict())

    for name in sorted(old_names - new_names):
        result["removed"].append(name)

    for name in sorted(old_names & new_names):
        old_cap = old.get(name)
        new_cap = new.get(name)
        if old_cap is not None and new_cap is not None:
            if old_cap.to_dict() != new_cap.to_dict():
                result["changed"].append({
                    "name": name,
                    "old": old_cap.to_dict(),
                    "new": new_cap.to_dict(),
                })

    return result