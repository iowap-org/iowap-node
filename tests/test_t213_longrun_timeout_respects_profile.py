"""T-213: the long-run handler timeout must respect the capability profile.

Finding from the LTX render timeout (2026-10-04, task_L5Ol6JkOZHCDu2Os /
stage_tlwrvK4Ty4_QaoC6): the profile declares ``long_run: true`` +
``timeout: 10800``, but ``_handler_timeout()`` in ``node_daemon.py``
ignores the ``timeout`` field for long-run caps entirely and hard-caps
on the hardcoded 2h lease budget (``_LONGRUN_HANDLER_TIMEOUT``). A 3h
render dies at 2h with ``{"error": "handler timeout after 7200s"}``.

Semantics of the fix: ``max(_LONGRUN_HANDLER_TIMEOUT, cap.get("timeout"))``
— the lease budget is a FLOOR, not a cap. The operator can extend the
long-run budget via the profile (not shorten it — the 2h floor protects
against accidentally too-short profile values).
"""
from __future__ import annotations

from typing import Any

from nodes.common.node_daemon import _handler_timeout

_2H = 2 * 3600


def test_longrun_cap_without_timeout_gets_lease_floor() -> None:
    """Long-run cap without a timeout field: 2h floor (status quo)."""
    cap: dict[str, Any] = {"long_run": True}
    assert _handler_timeout(cap) == _2H


def test_longrun_cap_with_shorter_timeout_gets_lease_floor() -> None:
    """timeout < 2h: the floor wins, not the shorter profile value."""
    cap = {"long_run": True, "timeout": 600}
    assert _handler_timeout(cap) == _2H


def test_longrun_cap_with_timeout_equal_to_floor() -> None:
    """timeout == 2h: exactly the floor budget."""
    cap = {"long_run": True, "timeout": _2H}
    assert _handler_timeout(cap) == _2H


def test_longrun_cap_timeout_larger_than_floor_wins() -> None:
    """The actual T-213 case: 10800s (3h) profile value beats 2h."""
    cap = {"long_run": True, "timeout": 10800}
    assert _handler_timeout(cap) == 10800


def test_non_longrun_cap_keeps_configured_timeout() -> None:
    """Non-long-run caps: the profile timeout stays unchanged (as-is)."""
    cap = {"timeout": 120}
    assert _handler_timeout(cap) == 120


def test_non_longrun_cap_default_300() -> None:
    """Non-long-run cap without a timeout field: default 300 (as-is)."""
    cap: dict[str, Any] = {}
    assert _handler_timeout(cap) == 300


def test_longrun_cap_timeout_string_parses() -> None:
    """YAML may deliver timeout as a string — the int cast applies here too."""
    cap = {"long_run": True, "timeout": "10800"}
    assert _handler_timeout(cap) == 10800