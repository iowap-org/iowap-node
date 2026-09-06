"""T-179: hp put/get — Script-Primitives über node-cli (Phase 2 Stubs).

Jeder Test beginnt mit ``assert False  # TODO`` — Phase 3 (integrate)
füllt die Körper. Die Test-Namen und -Intents sind bereits die
Vertrags-Checkliste aus dem Plan (Task 2/2b/3) und F2 (Phase 1):
Signaturen, Exit-Codes (0 ok / 1 Fehler / 2 usage) und stdout-Form
(GENAU EINE compacte JSON-Zeile) werden hier eingespielt.

Technik: FakeClient/MagicMock wie tests/conftest.py (nur sys.path-Bootstrap);
``cli_hp`` importiert node_cli NICHT (F2.2), Tests patchen
``cli_hp._load_capability_modes`` (F2.11 Monkeypatch-Surface).
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nodes.common.cli import cli_hp


@pytest.fixture
def client() -> MagicMock:
    c = MagicMock()
    c.base_url = "http://relay.test"
    return c


def _modes(monkeypatch, modes: list[str]) -> None:
    """Patch cli_hp._load_capability_modes (F2.11 Monkeypatch-Surface)."""
    monkeypatch.setattr(
        cli_hp, "_load_capability_modes", lambda c, cap: {"upload_modes": modes}
    )


# ---------------------------------------------------------------------------
# decide_mode (Task 2 + Task 2b — geteilter Entscheid-Punkt)
# ---------------------------------------------------------------------------


def test_decide_mode_prefers_inline_when_allowed():
    assert cli_hp.decide_mode(10, ["inline", "artifact", "bridge"], {}) == "inline"


def test_decide_mode_falls_back_to_artifact_when_inline_missing():
    assert cli_hp.decide_mode(10, ["artifact", "bridge"], {}) == "artifact"


def test_decide_mode_falls_back_to_bridge_when_sizes_exceed():
    th = {"max_inline_bytes": 5, "max_artifact_bytes": 8}
    assert cli_hp.decide_mode(10, ["inline", "artifact", "bridge"], th) == "bridge"


def test_decide_mode_respects_threshold_inline_over_limit():
    # F2.5: explizite 0 → Stufe verboten; fehlender Key → unbegrenzt.
    assert (
        cli_hp.decide_mode(10, ["inline", "artifact"], {"max_inline_bytes": 0})
        == "artifact"
    )
    # Fehlender artifact-Key = unbegrenzt.
    assert (
        cli_hp.decide_mode(10**9, ["artifact"], {"max_inline_bytes": 0})
        == "artifact"
    )


def test_decide_mode_respects_threshold_artifact_over_limit():
    # artifact über explizites Limit → Stufe wird übersprungen (inline
    # passt); explizite 0 verbietet artifact komplett.
    th = {"max_inline_bytes": 100, "max_artifact_bytes": 50}
    assert cli_hp.decide_mode(60, ["inline", "artifact"], th) == "inline"
    assert (
        cli_hp.decide_mode(60, ["inline", "artifact"], {"max_artifact_bytes": 0})
        == "inline"
    )
    # artifact über Limit, inline nicht angeboten, bridge nicht angeboten
    # → ValueError.
    with pytest.raises(ValueError):
        cli_hp.decide_mode(60, ["artifact"], {"max_artifact_bytes": 50})


def test_decide_mode_no_mode_fits_raises_valueerror():
    # FROZEN Meldung (F2.4): beide Stufen über Limit + keine bridge.
    with pytest.raises(ValueError) as excinfo:
        cli_hp.decide_mode(
            100,
            ["inline", "artifact"],
            {"max_inline_bytes": 5, "max_artifact_bytes": 50},
        )
    msg = str(excinfo.value)
    assert msg.startswith("file too big: 100 bytes")
    assert "capability supports only ['inline', 'artifact']" in msg
    assert "server ladder: inline<=5" in msg
    assert "artifact<=50" in msg


def test_decide_mode_force_not_allowed_raises_frozen_message():
    # FROZEN Meldung (F2.4).
    with pytest.raises(ValueError) as excinfo:
        cli_hp.decide_mode(10, ["inline"], None, force="bridge")
    msg = str(excinfo.value)
    assert msg.startswith("--force 'bridge' not supported by capability")
    assert "upload_modes=['inline']" in msg


def test_decide_mode_force_overrides_size_logic():
    th = {"max_inline_bytes": 1}
    assert (
        cli_hp.decide_mode(10**9, ["inline", "artifact", "bridge"], th, force="bridge")
        == "bridge"
    )
    assert (
        cli_hp.decide_mode(10**9, ["inline", "artifact"], th, force="artifact")
        == "artifact"
    )


# ---------------------------------------------------------------------------
# Task 2b — cli_file._choose_mode delegiert an decide_mode (byte-identisch)
# ---------------------------------------------------------------------------


def test_cli_file_choose_mode_delegates_and_keeps_zero_default_semantics():
    # cli_file materialisiert die 0-Defaults: fehlender Server-Key = Stufe
    # verboten (anders als decide_mode allein, wo fehlend = unbegrenzt).
    from nodes.common.cli import cli_file

    # Beide Schwellwerte fehlen (transfer_cfg leer) → nichts passt.
    with pytest.raises(SystemExit) as excinfo:
        cli_file._choose_mode(10, {}, ["inline", "artifact"])
    msg = str(excinfo.value)
    assert msg.startswith("file too big: 10 bytes")
    assert "server ladder: inline<=0, artifact<=0" in msg


def test_cli_file_choose_mode_identical_ladder_decisions():
    from nodes.common.cli import cli_file

    cfg = {"max_inline_bytes": 100, "max_artifact_bytes": 200}
    assert cli_file._choose_mode(10, cfg, ["inline", "artifact", "bridge"]) == "inline"
    # 500 > artifact threshold → artifact-Rung verboten → bridge.
    assert cli_file._choose_mode(500, cfg, ["artifact", "bridge"]) == "bridge"
    assert cli_file._choose_mode(150, cfg, ["artifact", "bridge"]) == "artifact"
    assert cli_file._choose_mode(500, cfg, ["bridge"]) == "bridge"
    # force-Pfad bleibt unverändert (SystemExit, nicht ValueError).
    with pytest.raises(SystemExit) as excinfo:
        cli_file._choose_mode(10, cfg, ["inline"], force="bridge")
    assert "--force 'bridge' not supported by capability" in str(excinfo.value)
    assert cli_file._choose_mode(10, cfg, ["inline", "bridge"], force="bridge") == "bridge"


# ---------------------------------------------------------------------------
# cmd_put (Task 2)
# ---------------------------------------------------------------------------


def test_put_small_file_prefers_inline(tmp_path, client, monkeypatch, capsys):
    # Plan-Snippet (Task 2): Datei b"hello" in tmp_path; _load_capability_modes
    # gepatcht → {"upload_modes": ["inline"]}; rc = cli_hp.cmd_put(client,
    # cap="cap.x", path=f, name=None); stdout (capsys) → json.loads →
    # __iowap_ref__.src == "inline", size_bytes == 5, rc == 0.
    f = tmp_path / "hello.txt"
    f.write_bytes(b"hello")
    _modes(monkeypatch, ["inline"])
    rc = cli_hp.cmd_put(client, cap="cap.x", path=f, name=None)
    assert rc == 0
    envelope = json.loads(capsys.readouterr().out)
    ref = envelope["__iowap_ref__"]
    assert ref["src"] == "inline"
    assert ref["size_bytes"] == 5


def test_put_artifact_when_inline_not_allowed(tmp_path, client, monkeypatch, capsys):
    # Plan-Snippet (Task 2): 10-Byte-Datei; modes ["artifact", "bridge"];
    # client.upload_artifact → {"artifact_id": "art_9"}; rc == 0; stdout →
    # src == "artifact", artifact_id == "art_9".
    f = tmp_path / "blob.bin"
    f.write_bytes(b"0123456789")
    _modes(monkeypatch, ["artifact", "bridge"])
    client.upload_artifact = MagicMock(return_value={"artifact_id": "art_9"})
    rc = cli_hp.cmd_put(client, cap="cap.x", path=f, name=None)
    assert rc == 0
    envelope = json.loads(capsys.readouterr().out)
    ref = envelope["__iowap_ref__"]
    assert ref["src"] == "artifact"
    assert ref["artifact_id"] == "art_9"


def test_put_missing_file_returns_exit_2(tmp_path, client):
    # Exit-Code-Konvention F2.7: 2 = usage (Datei fehlt).
    missing = tmp_path / "nope.txt"
    assert cli_hp.cmd_put(client, cap="cap.x", path=missing) == 2


def test_put_decide_mode_valueerror_returns_exit_1(
        tmp_path, client, monkeypatch, capsys):
    # Datei zu groß für jede Stufe → stderr-Meldung + return 1 (F2.7/8).
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 10)
    _modes(monkeypatch, ["inline", "artifact"])
    client.get_transfer_config = MagicMock(
        return_value={"max_inline_bytes": 5, "max_artifact_bytes": 8}
    )
    rc = cli_hp.cmd_put(client, cap="cap.x", path=f)
    assert rc == 1
    err = capsys.readouterr().err
    assert "file too big" in err


def test_put_artifact_without_artifact_id_returns_exit_1(
        tmp_path, client, monkeypatch):
    # upload_artifact liefert kein artifact_id → stderr + return 1 (F2-Vertrag).
    f = tmp_path / "blob.bin"
    f.write_bytes(b"0123456789")
    _modes(monkeypatch, ["artifact"])
    client.upload_artifact = MagicMock(return_value={})
    assert cli_hp.cmd_put(client, cap="cap.x", path=f) == 1


# (T-166 Phase 3: die MVP-Grenz-Pins test_put_bridge_raises_notimplementederror
# und test_get_bridge_not_supported_returns_exit_1 sind ersetzt — bridge-put/get
# sind implementiert, Verträge jetzt in tests/test_t166_hp_bridge.py.)


def test_put_name_override_sets_envelope_filename(tmp_path, client, monkeypatch, capsys):
    f = tmp_path / "real.txt"
    f.write_bytes(b"hi")
    _modes(monkeypatch, ["inline"])
    rc = cli_hp.cmd_put(client, cap="cap.x", path=f, name="renamed.txt")
    assert rc == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["__iowap_ref__"]["filename"] == "renamed.txt"


def test_put_prints_exactly_one_compact_json_line(
        tmp_path, client, monkeypatch, capsys):
    # F2.8: stdout-Vertragsform — eine Zeile, envelope.dump + "\n".
    f = tmp_path / "hello.txt"
    f.write_bytes(b"hello")
    _modes(monkeypatch, ["inline"])
    cli_hp.cmd_put(client, cap="cap.x", path=f)
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert out.endswith("\n")
    # compact: keine Leerzeichen nach Trennern.
    assert ": " not in out and ", " not in out


def test_put_out_none_resolves_sys_stdout_at_call_time(
        tmp_path, client, monkeypatch, capsys):
    # F2.1: out=None → sys.stdout zur CALL-ZEIT (capsys-Kompatibilität).
    f = tmp_path / "hello.txt"
    f.write_bytes(b"hello")
    _modes(monkeypatch, ["inline"])
    rc = cli_hp.cmd_put(client, cap="cap.x", path=f, out=None)
    assert rc == 0
    assert capsys.readouterr().out.startswith('{"__iowap_ref__"')


def test_put_reads_size_via_stat_not_full_file(
        tmp_path, client, monkeypatch):
    # F2.9: size via path.stat().st_size, Bytes erst nach der Entscheidung.
    f = tmp_path / "blob.bin"
    f.write_bytes(b"0" * 10)
    _modes(monkeypatch, ["artifact"])
    client.upload_artifact = MagicMock(return_value={"artifact_id": "art_1"})
    read_calls: list = []
    orig_read_bytes = Path.read_bytes

    def spy(self):
        read_calls.append(str(self))
        return orig_read_bytes(self)

    import pathlib

    monkeypatch.setattr(pathlib.Path, "read_bytes", spy)
    rc = cli_hp.cmd_put(client, cap="cap.x", path=f)
    assert rc == 0
    # artifact-Pfad: Bytes NIE ganz gelesen (nur der Upload-Stream des
    # Clients — hier MagicMock, also kein read_bytes).
    assert read_calls == []


# ---------------------------------------------------------------------------
# dispatch_put (Task 2, F2.2: func=with_client(cli_hp.dispatch_put))
# ---------------------------------------------------------------------------


def test_dispatch_put_missing_file_returns_exit_2(client, tmp_path, capsys):
    import argparse

    args = argparse.Namespace(
        path=str(tmp_path / "nope.txt"), cap="cap.x", name=None
    )
    assert cli_hp.dispatch_put(client, args) == 2
    assert "file not found" in capsys.readouterr().err


def test_dispatch_put_delegates_to_cmd_put(client, tmp_path, monkeypatch):
    import argparse

    f = tmp_path / "hello.txt"
    f.write_bytes(b"hello")
    args = argparse.Namespace(path=str(f), cap="cap.x", name="override.txt")
    seen: dict = {}

    def fake_cmd_put(c, *, cap, path, name=None):
        seen.update(cap=cap, path=path, name=name)
        return 0

    monkeypatch.setattr(cli_hp, "cmd_put", fake_cmd_put)
    assert cli_hp.dispatch_put(client, args) == 0
    assert seen == {"cap": "cap.x", "path": f, "name": "override.txt"}


# ---------------------------------------------------------------------------
# cmd_get (Task 3)
# ---------------------------------------------------------------------------


def _inline_envelope(data: bytes, filename: str) -> dict:
    return cli_hp.make_envelope(src="inline", filename=filename, data=data)


def test_get_inline_roundtrip_writes_file(client, tmp_path, capsys):
    env = _inline_envelope(b"roundtrip", "doc.txt")
    out_dir = tmp_path / "out"
    rc = cli_hp.cmd_get(client, envelope=env, out_dir=out_dir)
    assert rc == 0
    assert (out_dir / "doc.txt").read_bytes() == b"roundtrip"


def test_get_artifact_downloads_via_client(client, tmp_path, capsys):
    env = cli_hp.make_envelope(
        src="artifact", filename="big.bin", artifact_id="art_7"
    )
    target = tmp_path / "out" / "big.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"downloaded")
    client.download_artifact = MagicMock(return_value=target)
    rc = cli_hp.cmd_get(client, envelope=env, out_dir=tmp_path / "out")
    assert rc == 0
    client.download_artifact.assert_called_once()


def test_get_invalid_envelope_returns_exit_1(client, tmp_path, capsys):
    rc = cli_hp.cmd_get(
        client, envelope={"payload": "nope"}, out_dir=tmp_path / "out"
    )
    assert rc == 1
    assert "hp get:" in capsys.readouterr().err


def test_get_filename_sanitized_no_traversal(client, tmp_path, capsys):
    # F2-Vertrag: filename aus dem Umschlag ist NICHT trustbar (keine
    # ".."/absoluten Pfade).
    env = _inline_envelope(b"evil", "../../etc/passwd")
    out_dir = tmp_path / "out"
    rc = cli_hp.cmd_get(client, envelope=env, out_dir=out_dir)
    assert rc == 0
    written = list(out_dir.iterdir())
    assert len(written) == 1
    assert written[0] == out_dir / "passwd"
    assert not (tmp_path / "etc").exists()


def test_get_sha256_mismatch_returns_exit_1(client, tmp_path, capsys):
    env = _inline_envelope(b"honest", "f.txt")
    ref = env["__iowap_ref__"]
    ref["sha256"] = "0" * 64  # manipuliert
    rc = cli_hp.cmd_get(client, envelope=env, out_dir=tmp_path / "out")
    assert rc == 1
    assert "sha256 mismatch" in capsys.readouterr().err


def test_get_out_dir_created_when_missing(client, tmp_path, capsys):
    # mkdir(parents=True, exist_ok=True) — auch bei output-Override.
    deep = tmp_path / "a" / "b" / "c"
    env = _inline_envelope(b"data", "f.txt")
    rc = cli_hp.cmd_get(client, envelope=env, out_dir=deep)
    assert rc == 0
    assert deep.is_dir()

    deep2 = tmp_path / "x" / "y"
    env2 = _inline_envelope(b"data", "f.txt")
    rc2 = cli_hp.cmd_get(
        client, envelope=env2, out_dir=deep2, output=deep2 / "target.bin"
    )
    assert rc2 == 0
    assert (deep2 / "target.bin").read_bytes() == b"data"


def test_get_prints_path_size_bytes_src_json_line(client, tmp_path, capsys):
    # F2.8: {"path", "size_bytes", "src"} — Keys FROZEN.
    env = _inline_envelope(b"12345", "f.txt")
    out_dir = tmp_path / "out"
    rc = cli_hp.cmd_get(client, envelope=env, out_dir=out_dir)
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    data = json.loads(out)
    assert set(data.keys()) == {"path", "size_bytes", "src"}
    assert data["size_bytes"] == 5
    assert data["src"] == "inline"
    assert data["path"] == str(out_dir / "f.txt")


def test_get_output_override_writes_to_given_path(client, tmp_path, capsys):
    env = _inline_envelope(b"payload", "orig.txt")
    # output-Override im out_dir-Unterordner: cmd_get legt out_dir an
    # (Vertrag), target darunter ist neu.
    out_dir = tmp_path / "out"
    target = out_dir / "renamed.bin"
    rc = cli_hp.cmd_get(
        client, envelope=env, out_dir=out_dir, output=target
    )
    assert rc == 0
    assert target.read_bytes() == b"payload"
    # Orig-Filename wird NICHT angelegt (output hat Vorrang).
    assert not (out_dir / "orig.txt").exists()


# ---------------------------------------------------------------------------
# default_out_dir (F2.6)
# ---------------------------------------------------------------------------


def test_default_out_dir_uses_relay_task_id(monkeypatch):
    monkeypatch.setenv("RELAY_TASK_ID", "t_42")
    d = cli_hp.default_out_dir()
    assert d == Path.home() / ".relay" / "tmp" / "t_42"


def test_default_out_dir_falls_back_to_adhoc(monkeypatch):
    monkeypatch.delenv("RELAY_TASK_ID", raising=False)
    assert cli_hp.default_out_dir() == Path.home() / ".relay" / "tmp" / "adhoc"


# ---------------------------------------------------------------------------
# dispatch_get (Task 3, F2.12: args.file / args.output / args.out_dir)
# ---------------------------------------------------------------------------


def test_dispatch_get_reads_envelope_from_file(client, tmp_path, monkeypatch):
    import argparse

    env = _inline_envelope(b"fromfile", "doc.txt")
    envelope_file = tmp_path / "env.json"
    envelope_file.write_text(json.dumps(env), encoding="utf-8")
    args = argparse.Namespace(
        file=str(envelope_file), output=None, out_dir=tmp_path / "out"
    )
    seen: dict = {}

    def fake_cmd_get(c, *, envelope, out_dir, output=None, out=None):
        seen.update(envelope=envelope, out_dir=out_dir, output=output)
        return 0

    monkeypatch.setattr(cli_hp, "cmd_get", fake_cmd_get)
    assert cli_hp.dispatch_get(client, args) == 0
    assert seen["envelope"] == env
    assert seen["out_dir"] == tmp_path / "out"
    assert seen["output"] is None


def test_dispatch_get_reads_envelope_from_stdin(
        client, tmp_path, monkeypatch):
    import argparse
    import io

    env = _inline_envelope(b"fromstdin", "doc.txt")
    args = argparse.Namespace(file=None, output=None, out_dir=tmp_path / "out")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(env)))
    assert cli_hp.dispatch_get(client, args) == 0


def test_dispatch_get_invalid_json_returns_exit_2(client, tmp_path, capsys):
    # usage-Fehler: ungültiges JSON → return 2 (F2.7).
    import argparse

    args = argparse.Namespace(
        file=str(tmp_path / "broken.json"), output=None, out_dir=tmp_path / "out"
    )
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    assert cli_hp.dispatch_get(client, args) == 2


def test_dispatch_get_delegates_to_cmd_get(client, tmp_path, monkeypatch):
    import argparse

    env = _inline_envelope(b"payload", "f.txt")
    envelope_file = tmp_path / "env.json"
    envelope_file.write_text(json.dumps(env), encoding="utf-8")
    args = argparse.Namespace(
        file=str(envelope_file), output=tmp_path / "t.bin", out_dir=None
    )
    seen: dict = {}

    def fake_cmd_get(c, *, envelope, out_dir, output=None, out=None):
        seen.update(envelope=envelope, out_dir=out_dir, output=output)
        return 0

    monkeypatch.setattr(cli_hp, "cmd_get", fake_cmd_get)
    monkeypatch.setenv("RELAY_TASK_ID", "t_99")
    assert cli_hp.dispatch_get(client, args) == 0
    assert seen["envelope"] == env
    assert seen["output"] == tmp_path / "t.bin"
    # out_dir=None → default_out_dir() mit RELAY_TASK_ID (F2.6).
    assert seen["out_dir"] == Path.home() / ".relay" / "tmp" / "t_99"


# ---------------------------------------------------------------------------
# Signaturen-Vertrag (Phase 1 FROZEN — inspect-basiert, wie F4 verifiziert)
# ---------------------------------------------------------------------------


def test_cli_hp_signatures_unchanged():
    # F2: inspect.signature-Output ist verbindlich — Phase 2/3 dürfen die
    # Signaturen nicht ändern. (Annotationen zeigen als Strings, weil das
    # Modul `from __future__ import annotations` nutzt.)
    assert str(inspect.signature(cli_hp.decide_mode)) == (
        "(size_bytes: 'int', upload_modes: 'list[str]', "
        "thresholds: 'dict[str, int] | None' = None, force: 'str | None' = None) -> 'str'"
    )
    assert str(inspect.signature(cli_hp.cmd_put)) == (
        "(client: 'RelayClient', *, cap: 'str', path: 'Path', "
        "name: 'str | None' = None, out: 'TextIO | None' = None) -> 'int'"
    )
    assert str(inspect.signature(cli_hp.cmd_get)) == (
        "(client: 'RelayClient', *, envelope: 'dict[str, Any]', out_dir: 'Path', "
        "output: 'Path | None' = None, out: 'TextIO | None' = None) -> 'int'"
    )
    assert str(inspect.signature(cli_hp.dispatch_put)) == (
        "(client: 'RelayClient', args: 'argparse.Namespace') -> 'int'"
    )
    assert str(inspect.signature(cli_hp.dispatch_get)) == (
        "(client: 'RelayClient', args: 'argparse.Namespace') -> 'int'"
    )
    assert str(inspect.signature(cli_hp.default_out_dir)) == "() -> 'Path'"


def test_module_has_no_node_cli_import():
    # F2.2: cli_hp importiert node_cli NICHT (kein circular import).
    src = inspect.getsource(cli_hp)
    assert "import node_cli" not in src
    assert "from nodes.common import node_cli" not in src
    assert "from nodes.common.node_cli" not in src
    assert "nodes.common.node_cli" not in src.replace(
        "node_cli import", ""
    ) or "node_cli" not in src