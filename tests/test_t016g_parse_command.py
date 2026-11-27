"""T-016g: the dashboard's free-text command must parse into a clean argv.

Windows-VM finding: the operator typed "cmd" "/c" "systeminfo" / comma
separated forms; shlex keeps commas glued to tokens ("cmd,"), making
command[0] unresolvable on the daemon — the provider reported
health=down forever and the node never published it
("bridge cap host.sysinfo is down — not published").
(Pre-T-048 wording: since T-048 health is advisory and the allowlist
decides publishing; an unresolvable command then reports its honest
exec error at run time instead of vanishing.)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".."))

from nodes.common.dashboard import parse_command  # noqa: E402


def test_json_array() -> None:
    assert parse_command('["cmd", "/c", "systeminfo"]') == ["cmd", "/c", "systeminfo"]


def test_comma_separated() -> None:
    assert parse_command("cmd, /c, systeminfo") == ["cmd", "/c", "systeminfo"]


def test_quoted_comma_separated() -> None:
    assert parse_command('"cmd" "/c" "systeminfo"') == ["cmd", "/c", "systeminfo"]


def test_plain_whitespace() -> None:
    assert parse_command("cmd /c systeminfo") == ["cmd", "/c", "systeminfo"]


def test_inner_comma_argument_survives() -> None:
    # csv-like values keep their inner commas
    assert parse_command("tool --format csv --cols a,b") == [
        "tool", "--format", "csv", "--cols", "a,b"
    ]


def test_trailing_comma_tolerated() -> None:
    assert parse_command("cmd, /c, systeminfo,") == ["cmd", "/c", "systeminfo"]


def test_invalid_json_array_falls_back() -> None:
    # starts with '[' but is not valid JSON → shlex fallback, not a crash
    assert parse_command("[cmd]") == ["[cmd]"]


def test_windows_backslash_paths_survive() -> None:
    # T-016i: POSIX shlex ate backslashes as escapes (c:scripteshello.bat)
    assert parse_command("cmd /c c:\\scripts\\hello.bat") == [
        "cmd", "/c", "c:\\scripts\\hello.bat"
    ]


def test_quoted_windows_path() -> None:
    assert parse_command('"c:\\scripts\\tool.exe" --run') == [
        "c:\\scripts\\tool.exe", "--run"
    ]
