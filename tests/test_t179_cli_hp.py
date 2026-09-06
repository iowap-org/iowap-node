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


# ---------------------------------------------------------------------------
# decide_mode (Task 2 + Task 2b — geteilter Entscheid-Punkt)
# ---------------------------------------------------------------------------


def test_decide_mode_prefers_inline_when_allowed():
    assert False  # TODO


def test_decide_mode_falls_back_to_artifact_when_inline_missing():
    assert False  # TODO


def test_decide_mode_falls_back_to_bridge_when_sizes_exceed():
    assert False  # TODO


def test_decide_mode_respects_threshold_inline_over_limit():
    # F2.5: explizite 0 → Stufe verboten; fehlender Key → unbegrenzt.
    assert False  # TODO


def test_decide_mode_respects_threshold_artifact_over_limit():
    assert False  # TODO


def test_decide_mode_no_mode_fits_raises_valueerror():
    # FROZEN Meldung (F2.4): "file too big: {size} bytes, capability
    # supports only {modes} (server ladder: inline<={..}, artifact<={..})"
    assert False  # TODO


def test_decide_mode_force_not_allowed_raises_frozen_message():
    # FROZEN Meldung (F2.4): "--force {force!r} not supported by capability
    # (upload_modes={modes})"
    assert False  # TODO


def test_decide_mode_force_overrides_size_logic():
    assert False  # TODO


# ---------------------------------------------------------------------------
# cmd_put (Task 2)
# ---------------------------------------------------------------------------


def test_put_small_file_prefers_inline(tmp_path, client, monkeypatch, capsys):
    # Plan-Snippet (Task 2): Datei b"hello" in tmp_path; _load_capability_modes
    # gepatcht → {"upload_modes": ["inline"]}; rc = cli_hp.cmd_put(client,
    # cap="cap.x", path=f, name=None); stdout (capsys) → json.loads →
    # __iowap_ref__.src == "inline", size_bytes == 5, rc == 0.
    assert False  # TODO


def test_put_artifact_when_inline_not_allowed(tmp_path, client, monkeypatch, capsys):
    # Plan-Snippet (Task 2): 10-Byte-Datei; modes ["artifact", "bridge"];
    # client.upload_artifact → {"artifact_id": "art_9"}; rc == 0; stdout →
    # src == "artifact", artifact_id == "art_9".
    assert False  # TODO


def test_put_missing_file_returns_exit_2(tmp_path, client):
    # Exit-Code-Konvention F2.7: 2 = usage (Datei fehlt — im dispatch_put);
    # hier: cmd_put/dispatch_put mit nicht existierendem Pfad in tmp_path.
    assert False  # TODO


def test_put_decide_mode_valueerror_returns_exit_1(
        tmp_path, client, monkeypatch, capsys):
    # Datei zu groß für jede Stufe → stderr-Meldung + return 1 (F2.7/8).
    assert False  # TODO


def test_put_artifact_without_artifact_id_returns_exit_1(
        tmp_path, client, monkeypatch):
    # upload_artifact liefert kein artifact_id → stderr + return 1 (F2-Vertrag).
    assert False  # TODO


def test_put_bridge_raises_notimplementederror(tmp_path, client, monkeypatch):
    # FROZEN (F2.10): NotImplementedError("bridge-put folgt in Task 3").
    assert False  # TODO


def test_put_name_override_sets_envelope_filename(tmp_path, client, monkeypatch):
    assert False  # TODO


def test_put_prints_exactly_one_compact_json_line(
        tmp_path, client, monkeypatch, capsys):
    # F2.8: stdout-Vertragsform — eine Zeile, envelope.dump + "\n".
    assert False  # TODO


def test_put_out_none_resolves_sys_stdout_at_call_time(
        tmp_path, client, monkeypatch, capsys):
    # F2.1: out=None → sys.stdout zur CALL-Zeit (capsys-Kompatibilität).
    assert False  # TODO


def test_put_reads_size_via_stat_not_full_file(
        tmp_path, client, monkeypatch):
    # F2.9: size via path.stat().st_size, Bytes erst nach der Entscheidung.
    assert False  # TODO


# ---------------------------------------------------------------------------
# dispatch_put (Task 2, F2.2: func=with_client(cli_hp.dispatch_put))
# ---------------------------------------------------------------------------


def test_dispatch_put_missing_file_returns_exit_2(client, tmp_path, capsys):
    assert False  # TODO


def test_dispatch_put_delegates_to_cmd_put(client, tmp_path, monkeypatch):
    assert False  # TODO


# ---------------------------------------------------------------------------
# cmd_get (Task 3)
# ---------------------------------------------------------------------------


def test_get_inline_roundtrip_writes_file(client, tmp_path, capsys):
    assert False  # TODO


def test_get_artifact_downloads_via_client(client, tmp_path, capsys):
    assert False  # TODO


def test_get_bridge_not_supported_returns_exit_1(client, tmp_path, capsys):
    # FROZEN Meldung (F2.10): "hp get: bridge resolution not supported by
    # this node-cli version"
    assert False  # TODO


def test_get_invalid_envelope_returns_exit_1(client, tmp_path, capsys):
    assert False  # TODO


def test_get_filename_sanitized_no_traversal(client, tmp_path, capsys):
    # F2-Vertrag: filename aus dem Umschlag ist NICHT trustbar (keine
    # ".."/absoluten Pfade).
    assert False  # TODO


def test_get_sha256_mismatch_returns_exit_1(client, tmp_path, capsys):
    assert False  # TODO


def test_get_out_dir_created_when_missing(client, tmp_path, capsys):
    # mkdir(parents=True, exist_ok=True) — auch bei output-Override.
    assert False  # TODO


def test_get_prints_path_size_bytes_src_json_line(client, tmp_path, capsys):
    # F2.8: {"path", "size_bytes", "src"} — Keys FROZEN.
    assert False  # TODO


def test_get_output_override_writes_to_given_path(client, tmp_path, capsys):
    assert False  # TODO


# ---------------------------------------------------------------------------
# default_out_dir (F2.6)
# ---------------------------------------------------------------------------


def test_default_out_dir_uses_relay_task_id(monkeypatch):
    assert False  # TODO


def test_default_out_dir_falls_back_to_adhoc(monkeypatch):
    assert False  # TODO


# ---------------------------------------------------------------------------
# dispatch_get (Task 3, F2.12: args.file / args.output / args.out_dir)
# ---------------------------------------------------------------------------


def test_dispatch_get_reads_envelope_from_file(client, tmp_path, monkeypatch):
    assert False  # TODO


def test_dispatch_get_reads_envelope_from_stdin(client, tmp_path, monkeypatch):
    assert False  # TODO


def test_dispatch_get_invalid_json_returns_exit_2(client, tmp_path, capsys):
    # usage-Fehler: ungültiges JSON → return 2 (F2.7).
    assert False  # TODO


def test_dispatch_get_delegates_to_cmd_get(client, tmp_path, monkeypatch):
    assert False  # TODO


# ---------------------------------------------------------------------------
# Signaturen-Vertrag (Phase 1 FROZEN — inspect-basiert, wie F4 verifiziert)
# ---------------------------------------------------------------------------


def test_cli_hp_signatures_unchanged():
    # F2: inspect.signature-Output ist verbindlich — Phase 2/3 dürfen die
    # Signaturen nicht ändern. Erwartet: decide_mode(size_bytes, upload_modes,
    # thresholds=None, force=None), cmd_put(client, *, cap, path, name=None,
    # out=None), cmd_get(client, *, envelope, out_dir, output=None, out=None),
    # dispatch_put(client, args), dispatch_get(client, args),
    # default_out_dir().
    assert False  # TODO


def test_module_has_no_node_cli_import():
    # F2.2: cli_hp importiert node_cli NICHT (kein circular import).
    assert False  # TODO