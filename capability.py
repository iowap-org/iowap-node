#!/usr/bin/env python3
"""
Shared Capability-Modul fuer alle Node-Typen.

Capabilities sind strukturierte Objekte mit Name, Typ, Beschreibung,
Version, Konfiguration und Availability-Flag. Sie werden im Heartbeat
an den Relay-Server gesendet und koennen zur Laufzeit geaendert werden.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class CapabilityType(str, Enum):
    """Typ einer Capability – bestimmt das Routing im Scheduler."""
    AI = "ai"
    TOOL = "tool"
    SCRIPT = "script"
    WORKFLOW = "workflow"
    RESOURCE = "resource"

    def __str__(self) -> str:
        return self.value


@dataclass
class Capability:
    """Eine einzelne Capability eines Worker-Nodes."""
    name: str
    type: CapabilityType
    description: str = ""
    version: str = "1.0.0"
    available: bool = True
    config: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d: dict[str, Any] = asdict(self)
        d["type"] = self.type.value if isinstance(self.type, CapabilityType) else self.type
        return d

    @classmethod
    def from_dict(cls, data: dict) -> Capability:
        raw_type = data.get("type", "tool")
        return cls(
            name=data["name"],
            type=CapabilityType(raw_type) if isinstance(raw_type, str) else raw_type,
            description=data.get("description", ""),
            version=data.get("version", "1.0.0"),
            available=data.get("available", True),
            config=data.get("config", {}),
            metadata=data.get("metadata", {}),
        )

    def merge(self, other: Capability) -> Capability:
        """Uebernimmt Felder aus ``other``, die nicht None/leer sind."""
        merged = copy.deepcopy(self)
        for f_name in ("description", "version", "available", "config", "metadata"):
            val = getattr(other, f_name)
            if val not in (None, {}, ""):
                setattr(merged, f_name, copy.deepcopy(val) if isinstance(val, dict) else val)
        return merged

    def patch_config(self, updates: dict[str, Any]) -> None:
        """Schreibt einzelne Config-Werte nach, ohne die gesamte Config zu ersetzen."""
        self.config.update(updates)

    def matches(self,
                name: Optional[str] = None,
                ctype: Optional[CapabilityType | str] = None,
                available_only: bool = True) -> bool:
        """Prueft ob die Capability den Filterkriterien entspricht."""
        if name and self.name != name:
            return False
        if ctype is not None:
            check = ctype.value if isinstance(ctype, CapabilityType) else str(ctype)
            self_val = self.type.value if isinstance(self.type, CapabilityType) else str(self.type)
            if self_val != check:
                return False
        if available_only and not self.available:
            return False
        return True


class CapabilitySet:
    """Verwaltet den gesamten Capability-Vorrat eines Nodes."""

    def __init__(self, capabilities: Optional[list[Capability]] = None):
        self._caps: dict[str, Capability] = {}
        if capabilities:
            for cap in capabilities:
                self.register(cap)

    def register(self, cap: Capability) -> None:
        self._caps[cap.name] = cap

    def deregister(self, name: str) -> bool:
        if name in self._caps:
            del self._caps[name]
            return True
        return False

    def get(self, name: str) -> Optional[Capability]:
        return self._caps.get(name)

    def update(self, name: str, **kwargs: Any) -> Optional[Capability]:
        """Aendert einzelne Felder einer Capability."""
        cap = self._caps.get(name)
        if not cap:
            return None
        for key in ("description", "version", "available", "config", "metadata"):
            if key in kwargs:
                setattr(cap, key, kwargs[key])
        return cap

    def patch_config(self, name: str, updates: dict[str, Any]) -> Optional[Capability]:
        cap = self._caps.get(name)
        if cap:
            cap.patch_config(updates)
        return cap

    def filter(self,
               name: Optional[str] = None,
               ctype: Optional[CapabilityType | str] = None,
               available_only: bool = True) -> list[Capability]:
        return [c for c in self._caps.values() if c.matches(name, ctype, available_only)]

    @property
    def names(self) -> list[str]:
        return list(self._caps.keys())

    def to_list(self) -> list[dict]:
        return [c.to_dict() for c in self._caps.values()]

    @classmethod
    def from_list(cls, data: list[dict]) -> CapabilitySet:
        return cls([Capability.from_dict(d) for d in data])


def load_capabilities_from_yaml(path: Path) -> CapabilitySet:
    """Laedt Capabilities aus einer YAML-Datei."""
    try:
        import yaml
    except ImportError:
        raise ImportError("PyYAML ist erforderlich: pip install pyyaml")

    if not path.exists():
        return CapabilitySet()
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    caps = raw.get("capabilities", [])
    return CapabilitySet([Capability.from_dict(c) for c in caps])


def diff_capabilities(old: CapabilitySet, new: CapabilitySet) -> dict[str, Any]:
    """Vergleicht zwei CapabilitySets und gibt Aenderungen zurueck."""
    result: dict[str, Any] = {"added": [], "removed": [], "changed": []}
    old_names = set(old.names)
    new_names = set(new.names)

    for name in new_names - old_names:
        cap = new.get(name)
        if cap is not None:
            result["added"].append(cap.to_dict())
    for name in old_names - new_names:
        result["removed"].append(name)
    for name in old_names & new_names:
        old_cap = old.get(name)
        new_cap = new.get(name)
        if old_cap is not None and new_cap is not None and old_cap.to_dict() != new_cap.to_dict():
            result["changed"].append({
                "name": name,
                "old": old_cap.to_dict(),
                "new": new_cap.to_dict(),
            })
    return result