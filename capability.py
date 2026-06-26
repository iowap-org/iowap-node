#!/usr/bin/env python3
"""
Shared Capability-Modul fuer alle Node-Typen und den Server-CLI.

Dieses Modul wird sowohl von den Worker-Nodes als auch vom Relay-Server
verwendet. Es stellt die gemeinsamen Datenstrukturen und Hilfsfunktionen
fuer den Umgang mit Capabilities bereit:

    * ``CapabilityType``            – Enum der verfuegbaren Capability-Typen.
    * ``CapabilityInputField``     – Beschreibung eines einzelnen Eingabefelds.
    * ``CapabilityInputSchema``    – Schema ueber alle Eingabefelder mit
                                     Validierungsfunktion fuer Payloads.
    * ``Capability``               – Eine einzelne Capability eines Nodes.
    * ``CapabilitySet``            – Verwaltung des gesamten
                                     Capability-Vorrats eines Nodes.
    * ``load_capabilities_from_yaml`` – Laedt ein CapabilitySet aus YAML.
    * ``diff_capabilities``        – Vergleicht zwei CapabilitySets.

Alle Docstrings sind auf Deutsch verfasst.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml


class CapabilityType(str, Enum):
    """Typ einer Capability – bestimmt das Routing im Scheduler."""

    AI = "ai"
    TOOL = "tool"
    SCRIPT = "script"
    WORKFLOW = "workflow"
    RESOURCE = "resource"

    def __str__(self) -> str:
        """Gibt den String-Wert des Enums zurueck."""
        return self.value


# ---------------------------------------------------------------------------
# Eingabe-Schema
# ---------------------------------------------------------------------------


@dataclass
class CapabilityInputField:
    """
    Beschreibt ein einzelnes Eingabefeld einer Capability.

    Attribute:
        name:        Bezeichner des Felds.
        type:        Typ-Bezeichnung als String (z. B. ``"string"``,
                      ``"integer"``, ``"number"``, ``"boolean"``,
                      ``"object"``, ``"array"``). Standard ist ``"string"``.
        required:    Gibt an, ob das Feld zwingend erforderlich ist.
        default:     Standardwert, der verwendet wird, wenn das Feld im
                      Payload fehlt.
        enum:        Optionale Liste erlaubter Werte.
        ge:          Optionaler numerischer Untergrenzen-Wert (>=).
        le:          Optionaler numerischer Obergrenzen-Wert (<=).
        description: Mensch-lesbare Beschreibung des Felds.
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
        """Serialisiert das Feld in ein Dictionary."""
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
        """Erzeugt ein Feld aus einem Dictionary."""
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
        Validiert einen einzelnen Wert gegen das Feld-Schema.

        Rueckgabe:
            Liste von Fehlermeldungen (leer, wenn der Wert gueltig ist).
        """
        errors: list[str] = []

        if value is None:
            if self.required and self.default is None:
                errors.append(f"Feld '{self.name}' ist erforderlich.")
            return errors

        # Enum-Pruefung
        if self.enum is not None and value not in self.enum:
            errors.append(
                f"Feld '{self.name}': Wert {value!r} ist nicht in den "
                f"erlaubten Werten {self.enum!r}."
            )

        # Numerische Grenzen
        if self.ge is not None or self.le is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(
                    f"Feld '{self.name}': Wert {value!r} muss numerisch sein "
                    f"fuer ge/le-Pruefung."
                )
            else:
                if self.ge is not None and value < self.ge:
                    errors.append(
                        f"Feld '{self.name}': Wert {value!r} muss >= {self.ge} sein."
                    )
                if self.le is not None and value > self.le:
                    errors.append(
                        f"Feld '{self.name}': Wert {value!r} muss <= {self.le} sein."
                    )

        return errors


@dataclass
class CapabilityInputSchema:
    """
    Schema ueber alle Eingabefelder einer Capability.

    Attribute:
        fields: Mapping von Feldname auf das zugehoerige ``CapabilityInputField``.
    """

    fields: dict[str, CapabilityInputField] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialisiert das Schema in ein Dictionary."""
        return {
            "fields": {name: f.to_dict() for name, f in self.fields.items()}
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CapabilityInputSchema:
        """Erzeugt ein Schema aus einem Dictionary."""
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
        Validiert einen Payload gegen das Schema.

        Es werden folgende Regeln geprueft:
            * Alle als ``required`` markierten Felder muessen vorhanden sein.
            * Felder ohne Vorgabe erhalten ihren ``default``-Wert.
            * Enum-Beschraenkungen werden eingehalten.
            * Numerische Grenzen (``ge``/``le``) werden eingehalten.
            * Unerwartete, nicht im Schema definierte Felder werden als
              Fehler gemeldet.

        Rueckgabe:
            Tupel ``(gueltig, fehlermeldungen)``.
        """
        errors: list[str] = []
        if not isinstance(payload, dict):
            return False, ["Payload muss ein Dictionary sein."]

        # Unerwartete Felder
        for key in payload:
            if key not in self.fields:
                errors.append(f"Unerwartetes Feld '{key}' im Payload.")

        # Erwartete Felder pruefen
        for name, fld in self.fields.items():
            if name not in payload or payload[name] is None:
                if fld.required and fld.default is None:
                    errors.append(f"Feld '{name}' ist erforderlich.")
                    continue
                # default wird nicht in den Payload zurueckgeschrieben –
                # Validierung erfolgt mit default, falls vorhanden.
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
    Eine einzelne Capability eines Worker-Nodes.

    Attribute:
        name:         Eindeutiger Bezeichner der Capability.
        type:         Art der Capability (siehe ``CapabilityType``).
        description:  Mensch-lesbare Beschreibung.
        version:      Semantische Version der Capability.
        available:    Gibt an, ob die Capability aktuell nutzbar ist.
        input_schema: Optionales Eingabe-Schema fuer die Validierung von
                      Aufruf-Payloads.
        config:       Freie Konfigurationswerte der Capability.
        metadata:     Freie Metadaten der Capability.
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
        """Serialisiert die Capability in ein Dictionary."""
        d: dict[str, Any] = asdict(self)
        d["type"] = self.type.value if isinstance(self.type, CapabilityType) else self.type
        if self.input_schema is not None:
            d["input_schema"] = self.input_schema.to_dict()
        else:
            d["input_schema"] = None
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Capability:
        """Erzeugt eine Capability aus einem Dictionary."""
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
        Uebernimmt Felder aus ``other``, die nicht None/leer sind.

        ``name`` und ``type`` werden immer von ``self`` uebernommen.
        ``config`` und ``metadata`` werden gemischt (Werte aus ``other``
        ueberschreiben gleichnamige Schluessel aus ``self``).
        """
        merged = copy.deepcopy(self)
        if other.description:
            merged.description = other.description
        if other.version:
            merged.version = other.version
        # ``available`` wird auch dann uebernommen, wenn False – sonst
        # waere kein bewusstes Deaktivieren moeglich.
        merged.available = other.available
        if other.input_schema is not None:
            merged.input_schema = copy.deepcopy(other.input_schema)
        merged.config = {**merged.config, **(other.config or {})}
        merged.metadata = {**merged.metadata, **(other.metadata or {})}
        return merged

    def patch_config(self, updates: dict[str, Any]) -> None:
        """Schreibt einzelne Config-Werte nach, ohne die gesamte Config zu ersetzen."""
        self.config.update(updates)

    def matches(
        self,
        name: Optional[str] = None,
        ctype: Optional[CapabilityType | str] = None,
        available_only: bool = True,
    ) -> bool:
        """Prueft, ob die Capability den Filterkriterien entspricht."""
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
    Verwaltet den gesamten Capability-Vorrat eines Nodes.

    Attribute:
        _caps: Internes Mapping von Capability-Name auf ``Capability``.
    """

    _caps: dict[str, Capability] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Stellt sicher, dass ``_caps`` ein Dictionary ist."""
        if self._caps is None:
            self._caps = {}

    # ---- Registration ----------------------------------------------------

    def register(self, cap: Capability) -> None:
        """Registriert eine Capability. Vorhandene werden ueberschrieben."""
        self._caps[cap.name] = cap

    def deregister(self, name: str) -> bool:
        """Entfernt eine Capability am Namensschluessel. Gibt ``True`` zurueck, wenn entfernt."""
        if name in self._caps:
            del self._caps[name]
            return True
        return False

    # ---- Zugriff ---------------------------------------------------------

    def get(self, name: str) -> Optional[Capability]:
        """Liefert die Capability mit dem gegebenen Namen oder ``None``."""
        return self._caps.get(name)

    def update(self, name: str, **kwargs: Any) -> Optional[Capability]:
        """Aendert einzelne Felder einer registrierten Capability."""
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
        """Liefert alle Capabilities, die den Filterkriterien entsprechen."""
        return [
            c for c in self._caps.values()
            if c.matches(name, ctype, available_only)
        ]

    @property
    def names(self) -> list[str]:
        """Liefert die Liste aller registrierten Capability-Namen."""
        return list(self._caps.keys())

    # ---- Serialisierung --------------------------------------------------

    def to_list(self) -> list[dict[str, Any]]:
        """Serialisiert alle Capabilities in eine Liste von Dictionaries."""
        return [c.to_dict() for c in self._caps.values()]

    @classmethod
    def from_list(cls, data: list[dict[str, Any]]) -> CapabilitySet:
        """Erzeugt ein CapabilitySet aus einer Liste von Dictionaries."""
        return cls(
            _caps={c["name"]: Capability.from_dict(c) for c in data}
        )


# ---------------------------------------------------------------------------
# YAML-Ladefunktion
# ---------------------------------------------------------------------------


def load_capabilities_from_yaml(path: Path) -> CapabilitySet:
    """
    Laedt Capabilities aus einer YAML-Datei.

    Erwartete Struktur der YAML-Datei::

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

    Wenn die Datei nicht existiert, wird ein leeres ``CapabilitySet``
    zurueckgegeben.
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
# Diff-Funktion
# ---------------------------------------------------------------------------


def diff_capabilities(old: CapabilitySet, new: CapabilitySet) -> dict[str, Any]:
    """
    Vergleicht zwei CapabilitySets und liefert die Aenderungen.

    Rueckgabe-Struktur::

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