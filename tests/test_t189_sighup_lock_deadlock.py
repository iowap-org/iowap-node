"""T-189 (F2, HIGH): ``invalidate()`` darf nie den Load-Lock nehmen.

Beleg aus dem Senior-Review 2026-09-30: ``ActiveProfileCache._lock`` ist ein
nicht-reentranter ``threading.Lock``; ``get()`` hält ihn WÄHREND
``validate_profile()`` (YAML-Parse). Der SIGHUP-Handler ruft
``invalidate_active_cache()`` → nimmt denselben Lock → Self-Deadlock, weil
Signal-Handler auf dem Main-Thread laufen und der Polling-Daemon
``_claim_loop`` ebenfalls dort fährt. ``node-cli capabilities publish``
feuert genau dieses Signal.

Der Test bildet das nach: ein Thread ist mitten im Load, der Main-Thread
ruft ``invalidate()``. Ohne Fix blockiert der Invalidierungs-Aufruf.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from nodes.common import node_config


def _write_profile(path: Path, name: str) -> None:
    path.write_text(
        "capabilities:\n"
        f"  - name: {name}\n"
        "    handler: \"echo hi\"\n"
        "    claimable: true\n",
        encoding="utf-8",
    )


def test_invalidate_does_not_block_while_get_holds_load_lock(
    tmp_path: Path, monkeypatch: Any
) -> None:
    profile = tmp_path / "active.yaml"
    _write_profile(profile, "chat.ai")
    cache = node_config.ActiveProfileCache(profile)

    entered = threading.Event()
    release = threading.Event()
    real_validate = node_config.validate_profile

    def slow_validate(path: Path) -> list[dict[str, Any]]:
        entered.set()
        # Hold the load "in progress" like a slow YAML parse would.
        release.wait(timeout=5)
        return real_validate(path)

    monkeypatch.setattr(node_config, "validate_profile", slow_validate)

    loader = threading.Thread(target=cache.get, daemon=True)
    loader.start()
    assert entered.wait(timeout=5), "get() never entered the load path"

    # The SIGHUP handler path — must return while the load is still running.
    done = threading.Event()

    def _invalidate() -> None:
        cache.invalidate()
        done.set()

    watchdog = threading.Thread(target=_invalidate, daemon=True)
    watchdog.start()
    assert done.wait(timeout=3), "invalidate() blocked on the load lock (deadlock)"

    release.set()
    loader.join(timeout=5)


def test_invalidate_during_load_wins_against_stale_store(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """An invalidate() mid-load must not be overwritten by the in-flight load."""
    profile = tmp_path / "active.yaml"
    _write_profile(profile, "chat.ai")
    cache = node_config.ActiveProfileCache(profile)

    entered = threading.Event()
    release = threading.Event()
    real_validate = node_config.validate_profile

    def slow_validate(path: Path) -> list[dict[str, Any]]:
        entered.set()
        release.wait(timeout=5)
        return real_validate(path)

    monkeypatch.setattr(node_config, "validate_profile", slow_validate)

    loader = threading.Thread(target=cache.get, daemon=True)
    loader.start()
    assert entered.wait(timeout=5)

    # Replace the profile on disk AND invalidate while the old load runs.
    _write_profile(profile, "terminal.ai")
    cache.invalidate()
    release.set()
    loader.join(timeout=5)

    # Next get() must re-read and return the NEW profile, not the stale one.
    monkeypatch.setattr(node_config, "validate_profile", real_validate)
    caps = cache.get()
    assert [c["name"] for c in caps] == ["terminal.ai"]


def test_invalidate_then_get_reloads_unchanged_mtime(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """invalidate() clears the cache even when the mtime did not change."""
    profile = tmp_path / "active.yaml"
    _write_profile(profile, "chat.ai")
    cache = node_config.ActiveProfileCache(profile)

    assert [c["name"] for c in cache.get()] == ["chat.ai"]

    # Same-second rewrite: mtime may be identical on coarse filesystems.
    _write_profile(profile, "terminal.ai")
    cache.invalidate()
    assert [c["name"] for c in cache.get()] == ["terminal.ai"]
