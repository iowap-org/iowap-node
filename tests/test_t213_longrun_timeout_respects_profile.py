"""T-213: Long-Run-Handler-Timeout muss das Capability-Profil respektieren.

Befund aus dem LTX-Render-Timeout (2026-10-04, task_L5Ol6JkOZHCDu2Os /
stage_tlwrvK4Ty4_QaoC6): das Profil deklariert
``long_run: true`` + ``timeout: 10800``, aber ``_handler_timeout()`` in
``node_daemon.py`` ignoriert das ``timeout``-Feld für Long-Run-Caps
komplett und cappt hart auf den hardcoded 2h-Lease-Budget
(``_LONGRUN_HANDLER_TIMEOUT``). Ein 3h-Render stirbt bei 2h mit
``{"error": "handler timeout after 7200s"}``.

Semantik des Fixs: ``max(_LONGRUN_HANDLER_TIMEOUT, cap.get("timeout"))`` —
der Lease-Budget ist ein FLOOR, kein Cap. Der Operator kann damit das
Long-Run-Budget per Profil verlängern (nicht verkürzen — der 2h-Floor
schützt vor versehentlich zu kurzen Profil-Werten).
"""
from __future__ import annotations

from typing import Any

from nodes.common.node_daemon import _handler_timeout

_2H = 2 * 3600


def test_longrun_cap_without_timeout_gets_lease_floor() -> None:
    """Long-Run-Cap ohne timeout-Feld: 2h Floor (Status quo)."""
    cap: dict[str, Any] = {"long_run": True}
    assert _handler_timeout(cap) == _2H


def test_longrun_cap_with_shorter_timeout_gets_lease_floor() -> None:
    """timeout < 2h: Floor gewinnt, nicht der kürzere Profil-Wert."""
    cap = {"long_run": True, "timeout": 600}
    assert _handler_timeout(cap) == _2H


def test_longrun_cap_with_timeout_equal_to_floor() -> None:
    """timeout == 2h: exakt das Floor-Budget."""
    cap = {"long_run": True, "timeout": _2H}
    assert _handler_timeout(cap) == _2H


def test_longrun_cap_timeout_larger_than_floor_wins() -> None:
    """Der eigentliche T-213-Fall: 10800s (3h) Profil-Wert schlägt 2h."""
    cap = {"long_run": True, "timeout": 10800}
    assert _handler_timeout(cap) == 10800


def test_non_longrun_cap_keeps_configured_timeout() -> None:
    """Nicht-Long-Run-Caps: Profil-Timeout unverändert (Bestand)."""
    cap = {"timeout": 120}
    assert _handler_timeout(cap) == 120


def test_non_longrun_cap_default_300() -> None:
    """Nicht-Long-Run-Cap ohne timeout-Feld: Default 300 (Bestand)."""
    cap: dict[str, Any] = {}
    assert _handler_timeout(cap) == 300


def test_longrun_cap_timeout_string_parses() -> None:
    """YAML kannTimeout als String liefern — int-Cast greift auch hier."""
    cap = {"long_run": True, "timeout": "10800"}
    assert _handler_timeout(cap) == 10800