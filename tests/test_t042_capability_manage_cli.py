"""T-042: capability-store management CLI — create/update/delete/script-upload.

The subcommands wrap the dashboard store functions verbatim:

* ``capabilities create`` → ``dashboard.create_provider`` — identical
  validation (write-nothing-on-error, duplicate "delete it first"), the
  ``--publish`` chain additionally toggles the bridge allowlist via
  ``dashboard.set_capability_published`` after a successful create;
* ``capabilities update`` → ``dashboard.update_provider`` — name immutable,
  every edit auto-bumps (explicit differing ``--version`` wins); a
  field-less ``--publish``/``--unpublish`` invocation is the pure allowlist
  toggle (dashboard /caps/publish semantics, NO version bump);
* ``capabilities delete`` → ``dashboard.delete_provider``;
* ``capabilities script-upload`` → ``dashboard.save_script`` (+ slug +
  extension-aware default argv) via ``--file`` or ``--stdin --name``.

Errors go to stderr with exit 1; ``--json`` keeps stdout machine-readable
(the summary line fields name/version/published).
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nodes.common import dashboard, node_cli, node_config, node_utils
from nodes.common.cli import cli_capabilities
from nodes.common.node_cli import build_parser


@pytest.fixture()
def dash_env(tmp_path, monkeypatch):
    """Isolate every state path the dashboard touches (same fixture as T-036)."""
    for name in (
        "META_PATH", "TOKEN_PATH", "CONFIG_PATH",
        "LEGACY_META_PATH", "LEGACY_TOKEN_PATH", "STATUS_PATH",
    ):
        monkeypatch.setattr(node_utils, name, tmp_path / f"{name.lower()}.json")
    monkeypatch.setattr(node_config, "ACTIVE_PATH", tmp_path / "node.yaml")
    monkeypatch.setattr(node_config, "BASE_DIR", tmp_path / "relay")
    monkeypatch.setenv("IOWAP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    return tmp_path


def _args(**overrides) -> argparse.Namespace:
    """A default create/update/delete/script-upload Namespace."""
    base: dict = {
        "name": None, "type": None, "summary": None, "command": None,
        "args": None, "script": None, "max_parallel": None, "cost": None,
        "artifacts_glob": None, "version": None, "method": None,
        "url_template": None, "body_template": None, "result_path": None,
        "artifact_paths": None, "health_url": None,
        "publish": False, "unpublish": False, "file": None, "stdin": False,
        "json": False, "log_level": "ERROR",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _providers(dash_env) -> list[dict]:
    path = dashboard.providers_path()
    if not path.exists():
        return []
    return yaml.safe_load(path.read_text())["providers"]


def _allowlist(dash_env) -> list[str]:
    path = node_config.ACTIVE_PATH
    if not path.exists():
        return []
    block = (yaml.safe_load(path.read_text()) or {}).get("bridge") or {}
    return block.get("allowlist") or []


def _seed_bridge(dash_env, allowlist: list[str] | None = None) -> None:
    """A configured bridge (url set) — is_published reads it like the dashboard."""
    block = {"url": "http://relay.test:8788", "allowlist": allowlist or []}
    node_config.ACTIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    node_config.ACTIVE_PATH.write_text(yaml.safe_dump({"bridge": block}))


# ── create ──────────────────────────────────────────────────────────────────


def test_create_happy_writes_entry_and_summary(dash_env, capsys):
    args = _args(name="a.cap", type="process", command="run x", summary="s")
    rc = cli_capabilities._cmd_capabilities_create(args)

    assert rc == 0
    entry = _providers(dash_env)[0]
    assert entry["name"] == "a.cap"
    assert entry["command"] == ["run", "x"]  # dashboard parse_command rules
    assert entry["version"] == "1.0.0"
    out = capsys.readouterr().out
    assert "created 'a.cap'" in out
    assert "v1.0.0" in out
    assert "published=no" in out


def test_create_json_output_is_machine_readable(dash_env, capsys):
    args = _args(name="b.cap", type="process", command="run x", json=True)
    rc = cli_capabilities._cmd_capabilities_create(args)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": True, "name": "b.cap", "version": "1.0.0", "published": False}


def test_create_duplicate_fails_without_writing(dash_env, capsys):
    dash_env  # fixture
    ok, msg = dashboard.create_provider(
        {"name": "dup.cap", "type": "process", "command": "run x"}
    )
    assert ok, msg
    before = dashboard.providers_path().read_text()
    rc = cli_capabilities._cmd_capabilities_create(
        _args(name="dup.cap", type="process", command="run y")
    )

    assert rc == 1
    captured = capsys.readouterr()
    assert "delete it first" in captured.err
    assert dashboard.providers_path().read_text() == before  # nothing written


def test_create_garbage_field_fails_and_writes_nothing(dash_env, capsys):
    args = _args(name="bad.cap", type="process", command="run x", cost="not-a-number")
    rc = cli_capabilities._cmd_capabilities_create(args)

    assert rc == 1
    captured = capsys.readouterr()
    assert "cost" in captured.err
    assert dashboard.providers_path().exists() is False  # write-nothing-on-error


def test_create_invalid_type_uses_store_validation_message(dash_env, capsys):
    args = _args(name="t.cap", type="websocket", command="run x")
    rc = cli_capabilities._cmd_capabilities_create(args)

    assert rc == 1
    assert "invalid type 'websocket'" in capsys.readouterr().err
    assert dashboard.providers_path().exists() is False


def test_create_publish_chain_toggles_allowlist(dash_env, capsys):
    args = _args(name="pub.cap", type="process", command="run x", publish=True)
    rc = cli_capabilities._cmd_capabilities_create(args)

    assert rc == 0
    assert "pub.cap" in _allowlist(dash_env)
    out = capsys.readouterr().out
    assert "published=yes" in out


def test_create_http_type_via_url_template(dash_env, capsys):
    args = _args(name="web.cap", type="http", url_template="http://x/{{q}}", json=True)
    rc = cli_capabilities._cmd_capabilities_create(args)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    entry = _providers(dash_env)[0]
    assert entry["exec"]["url_template"] == "http://x/{{q}}"


def test_create_missing_command_is_store_error(dash_env, capsys):
    args = _args(name="nc.cap", type="process")
    rc = cli_capabilities._cmd_capabilities_create(args)

    assert rc == 1
    assert "require a command" in capsys.readouterr().err


def test_create_with_script_upload_prefills_default_argv(dash_env, tmp_path, capsys):
    script = tmp_path / "tool.bat"
    script.write_text("echo hi", encoding="utf-8")
    args = _args(name="script.cap", type="process", script=str(script), json=True)
    rc = cli_capabilities._cmd_capabilities_create(args)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["script_uploaded"] == "bridge/scripts/tool.bat"
    entry = _providers(dash_env)[0]
    assert entry["command"] == ["cmd", "/c", "scripts\\tool.bat"]
    assert (tmp_path / "workspace" / "bridge" / "scripts" / "tool.bat").is_file()


def test_create_script_upload_bad_extension_fails_clean(dash_env, tmp_path, capsys):
    script = tmp_path / "tool.exe.txt"
    script.write_text("evil", encoding="utf-8")
    args = _args(name="bad.cap", type="process", script=str(script))
    rc = cli_capabilities._cmd_capabilities_create(args)

    assert rc == 1
    assert "not allowed" in capsys.readouterr().err
    assert dashboard.providers_path().exists() is False


def test_create_missing_script_file_fails(dash_env, capsys):
    args = _args(name="ghost.cap", type="process", script="/nonexistent/tool.bat")
    rc = cli_capabilities._cmd_capabilities_create(args)

    assert rc == 1
    assert "script not found" in capsys.readouterr().err


# ── update ──────────────────────────────────────────────────────────────────


def _seed(dash_env, name: str = "a.cap", **extra) -> None:
    fields = {"name": name, "type": "process", "command": "run x", **extra}
    ok, msg = dashboard.create_provider(fields)
    assert ok, msg


def test_update_happy_edits_and_bumps(dash_env, capsys):
    dash_env  # fixture
    _seed(dash_env)
    rc = cli_capabilities._cmd_capabilities_update(
        _args(name="a.cap", type="process", command="run x", summary="NEW")
    )

    assert rc == 0
    entry = _providers(dash_env)[0]
    assert entry["summary"] == "NEW"
    assert entry["version"] == "1.0.1"  # auto-bump
    out = capsys.readouterr().out
    assert "update ok — a.cap v1.0.1" in out


def test_update_json_carries_version_and_publish_state(dash_env, capsys):
    dash_env  # fixture
    _seed(dash_env)
    rc = cli_capabilities._cmd_capabilities_update(
        _args(name="a.cap", type="process", command="run x", summary="N", json=True)
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["name"] == "a.cap"
    assert payload["version"] == "1.0.1"


def test_update_unknown_name_fails(dash_env, capsys):
    dash_env  # fixture
    rc = cli_capabilities._cmd_capabilities_update(
        _args(name="ghost.cap", type="process", command="run x")
    )

    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_update_explicit_version_override_wins(dash_env, capsys):
    dash_env  # fixture
    _seed(dash_env)
    rc = cli_capabilities._cmd_capabilities_update(
        _args(name="a.cap", type="process", command="run x", version="9.9.9")
    )

    assert rc == 0
    assert _providers(dash_env)[0]["version"] == "9.9.9"


def test_update_garbage_field_fails_and_file_is_untouched(dash_env, capsys):
    dash_env  # fixture
    _seed(dash_env)
    before = dashboard.providers_path().read_text()
    rc = cli_capabilities._cmd_capabilities_update(
        _args(name="a.cap", type="process", command="run x", max_parallel="zero")
    )

    assert rc == 1
    assert "max_parallel" in capsys.readouterr().err
    assert dashboard.providers_path().read_text() == before


def test_update_publish_with_fields_bumps_and_publishes(dash_env, capsys):
    dash_env  # fixture
    _seed(dash_env)
    rc = cli_capabilities._cmd_capabilities_update(
        _args(name="a.cap", type="process", command="run x", publish=True, json=True)
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["published"] is True
    assert "a.cap" in _allowlist(dash_env)
    assert _providers(dash_env)[0]["version"] == "1.0.1"  # the edit bumped


def test_update_publish_only_toggles_allowlist_without_bump(dash_env, capsys):
    dash_env  # fixture
    _seed(dash_env)
    _seed_bridge(dash_env)
    rc = cli_capabilities._cmd_capabilities_update(
        _args(name="a.cap", publish=True, json=True)
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["published"] is True
    assert payload["version"] == "1.0.0"  # pure toggle — no edit, no bump
    assert "a.cap" in _allowlist(dash_env)


def test_update_unpublish_only_removes_from_allowlist(dash_env, capsys):
    dash_env  # fixture
    _seed(dash_env)
    assert dashboard.set_capability_published("a.cap", True)[0]
    rc = cli_capabilities._cmd_capabilities_update(
        _args(name="a.cap", unpublish=True, json=True)
    )

    assert rc == 0
    assert "a.cap" not in _allowlist(dash_env)
    assert _providers(dash_env)[0]["version"] == "1.0.0"


def test_update_without_fields_or_publish_fails(dash_env, capsys):
    dash_env  # fixture
    _seed(dash_env)
    rc = cli_capabilities._cmd_capabilities_update(_args(name="a.cap"))

    assert rc == 1
    assert "at least one field" in capsys.readouterr().err


def test_update_args_tokens_append_to_command(dash_env, capsys):
    dash_env  # fixture
    _seed(dash_env)
    rc = cli_capabilities._cmd_capabilities_update(
        _args(name="a.cap", type="process", command="run", args=["--x", "1"])
    )

    assert rc == 0
    assert _providers(dash_env)[0]["command"] == ["run", "--x", "1"]


def test_update_args_without_command_fails(dash_env, capsys):
    dash_env  # fixture
    _seed(dash_env)
    rc = cli_capabilities._cmd_capabilities_update(
        _args(name="a.cap", args=["--x"])
    )

    assert rc == 1
    assert "--args requires --command" in capsys.readouterr().err


# ── delete ──────────────────────────────────────────────────────────────────


def test_delete_happy_removes_entry(dash_env, capsys):
    dash_env  # fixture
    _seed(dash_env)
    rc = cli_capabilities._cmd_capabilities_delete(_args(name="a.cap"))

    assert rc == 0
    assert _providers(dash_env) == []
    assert "deleted 'a.cap'" in capsys.readouterr().out


def test_delete_json_shape(dash_env, capsys):
    dash_env  # fixture
    _seed(dash_env)
    rc = cli_capabilities._cmd_capabilities_delete(_args(name="a.cap", json=True))

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True, "name": "a.cap", "deleted": True,
    }


def test_delete_unknown_name_fails(dash_env, capsys):
    dash_env  # fixture
    rc = cli_capabilities._cmd_capabilities_delete(_args(name="ghost.cap"))

    assert rc == 1
    assert "unknown capability" in capsys.readouterr().err


# ── script-upload ───────────────────────────────────────────────────────────


def test_script_upload_file_happy(dash_env, tmp_path, capsys):
    script = tmp_path / "cleanup.py"
    script.write_text("print('hi')", encoding="utf-8")
    rc = cli_capabilities._cmd_capabilities_script_upload(
        _args(file=str(script))
    )

    assert rc == 0
    stored = dash_workspace(dash_env) / "bridge" / "scripts" / "cleanup.py"
    assert stored.is_file()
    out = capsys.readouterr().out
    assert "bridge/scripts/cleanup.py" in out
    assert "python scripts\\cleanup.py" in out


def dash_workspace(dash_env) -> Path:
    return dash_env / "workspace"


def test_script_upload_file_json_shape(dash_env, tmp_path, capsys):
    script = tmp_path / "hello.bat"
    script.write_text("echo hi", encoding="utf-8")
    rc = cli_capabilities._cmd_capabilities_script_upload(
        _args(file=str(script), json=True)
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "ok": True,
        "stored": "bridge/scripts/hello.bat",
        "stored_name": "hello.bat",
        "name": "hello",  # slugify: the stem is already a valid name
        "command": ["cmd", "/c", "scripts\\hello.bat"],
    }


def test_script_upload_stdin_happy(dash_env, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"echo stdin")))
    rc = cli_capabilities._cmd_capabilities_script_upload(
        _args(stdin=True, name="run.cmd", json=True)
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stored"] == "bridge/scripts/run.cmd"
    assert payload["command"] == ["cmd", "/c", "scripts\\run.cmd"]
    assert (dash_workspace(dash_env) / "bridge" / "scripts" / "run.cmd").read_bytes() == b"echo stdin"


def test_script_upload_stdin_requires_name(dash_env, capsys):
    rc = cli_capabilities._cmd_capabilities_script_upload(_args(stdin=True))

    assert rc == 1
    assert "--stdin requires --name" in capsys.readouterr().err


def test_script_upload_bad_extension_fails(dash_env, tmp_path, capsys):
    script = tmp_path / "tool.txt"
    script.write_text("not allowed", encoding="utf-8")
    rc = cli_capabilities._cmd_capabilities_script_upload(_args(file=str(script)))

    assert rc == 1
    assert "not allowed" in capsys.readouterr().err


def test_script_upload_size_cap_fails(dash_env, capsys, monkeypatch):
    monkeypatch.setattr(dashboard, "MAX_SCRIPT_BYTES", 8)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"123456789")))
    rc = cli_capabilities._cmd_capabilities_script_upload(_args(stdin=True, name="big.sh"))

    assert rc == 1
    assert "script too large" in capsys.readouterr().err
    assert not (dash_workspace(dash_env) / "bridge" / "scripts").exists()


def test_script_upload_collision_gets_suffix(dash_env, tmp_path, capsys):
    script = tmp_path / "again.sh"
    script.write_text("first", encoding="utf-8")
    cli_capabilities._cmd_capabilities_script_upload(_args(file=str(script)))
    capsys.readouterr()  # flush the first (plain) run's output
    rc = cli_capabilities._cmd_capabilities_script_upload(_args(file=str(script), json=True))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stored_name"] == "again.1.sh"
    scripts = dash_workspace(dash_env) / "bridge" / "scripts"
    assert {p.name for p in scripts.iterdir()} == {"again.sh", "again.1.sh"}


def test_script_upload_missing_file_fails(dash_env, capsys):
    rc = cli_capabilities._cmd_capabilities_script_upload(_args(file="/nonexistent/x.sh"))

    assert rc == 1
    assert "script not found" in capsys.readouterr().err


# ── parser registration ─────────────────────────────────────────────────────


def test_parser_registers_create_with_field_slice():
    parser = build_parser("t")
    args = parser.parse_args(
        ["capabilities", "create", "--name", "x.cap", "--type", "process",
         "--command", "run x", "--cost", "2", "--publish"]
    )
    assert args.capabilities_command == "create"
    assert args.name == "x.cap"
    assert args.command == "run x"
    assert args.cost == "2"
    assert args.publish is True
    assert args.func is cli_capabilities._cmd_capabilities_create


def test_parser_create_field_slice_matches_update():
    parser = build_parser("t")
    create = parser.parse_args(
        ["capabilities", "create", "--name", "x.cap", "--summary", "s",
         "--command", "c", "--args", "a", "--script", "f", "--max-parallel", "2",
         "--cost", "1", "--artifacts-glob", "g", "--version", "1.2.3",
         "--url-template", "u", "--method", "GET", "--body-template", "b",
         "--result-path", "r", "--artifact-paths", "p", "--health-url", "h"]
    )
    update = parser.parse_args(
        ["capabilities", "update", "--name", "x.cap", "--summary", "s",
         "--command", "c", "--args", "a", "--script", "f", "--max-parallel", "2",
         "--cost", "1", "--artifacts-glob", "g", "--version", "1.2.3",
         "--url-template", "u", "--method", "GET", "--body-template", "b",
         "--result-path", "r", "--artifact-paths", "p", "--health-url", "h"]
    )
    for cli_key, _form_key in cli_capabilities._FIELD_FLAGS:  # identical slice
        attr = cli_key.replace("-", "_")
        assert getattr(create, attr) == getattr(update, attr)


def test_parser_registers_delete_and_script_upload():
    parser = build_parser("t")
    args = parser.parse_args(["capabilities", "delete", "--name", "x.cap"])
    assert args.func is cli_capabilities._cmd_capabilities_delete
    args = parser.parse_args(["capabilities", "script-upload", "--stdin", "--name", "t.sh"])
    assert args.stdin is True
    assert args.name == "t.sh"
    assert args.func is cli_capabilities._cmd_capabilities_script_upload
    with pytest.raises(SystemExit):
        parser.parse_args(["capabilities", "script-upload", "--file", "a", "--stdin"])


def test_json_global_flag_reaches_the_subcommands():
    parser = build_parser("t")
    args = parser.parse_args(["--json", "capabilities", "delete", "--name", "x.cap"])
    assert args.json is True
