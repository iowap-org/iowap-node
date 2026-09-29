"""T-192 (F5, MED): ``hp get``-sha256 muss chunkwise prüfen, nicht in RAM.

Beleg aus dem Senior-Review 2026-09-30 (``cli_hp.py:434``): die
sha256-Verifikation las die gesamte Datei mit ``read_bytes()`` in den RAM —
genau auf dem Bridge-/Artifact-Pfad, für den der Rest der Datei bewusst in
64-KiB-Chunks streamt. Ein großer Transfer erzeugte so denselben
RAM-Peak, den der Chunk-Stream gerade vermeiden soll.

Fix: ``file_serve._sha256_file`` wiederverwenden (existiert, chunkweise).
Der Test spioniert ``read_bytes`` auf der Zieldatei aus.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nodes.common import file_serve
from nodes.common.cli import cli_hp
from nodes.common.envelope import make_envelope


@pytest.fixture
def client() -> MagicMock:
    c = MagicMock()
    c.base_url = "http://relay.test"
    c.meta = {"node_id": "E4W3CBWQ"}
    c.token = "tok_test"
    return c


def test_hp_get_sha256_does_not_read_whole_file(
    tmp_path: Path, client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"x" * 4096
    digest = hashlib.sha256(payload).hexdigest()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    target = out_dir / "recv.bin"
    target.write_bytes(payload)

    envelope = make_envelope(
        src="inline", filename="recv.bin", data=payload, sha256=digest
    )

    # Poison read_bytes on Path: any whole-file read during verify fails loudly.
    real_read_bytes = Path.read_bytes

    def _boom(self: Path):
        raise AssertionError(
            "hp get used Path.read_bytes() for sha256 — must stream chunkwise"
        )

    monkeypatch.setattr(Path, "read_bytes", _boom)
    # The inline writer itself uses write_bytes, which is fine — keep it real.
    monkeypatch.setattr(cli_hp, "_load_capability_modes", lambda c, cap: {"upload_modes": ["inline"]})

    rc = cli_hp.cmd_get(client, envelope=envelope, out_dir=out_dir)

    monkeypatch.setattr(Path, "read_bytes", real_read_bytes)
    assert rc == 0
    assert target.read_bytes() == payload


def test_hp_get_sha256_mismatch_still_detected_chunked(
    tmp_path: Path, client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"y" * 2048
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    target = out_dir / "recv.bin"
    target.write_bytes(payload)

    envelope = make_envelope(
        src="inline", filename="recv.bin", data=payload, sha256="0" * 64
    )
    monkeypatch.setattr(cli_hp, "_load_capability_modes", lambda c, cap: {"upload_modes": ["inline"]})

    rc = cli_hp.cmd_get(client, envelope=envelope, out_dir=out_dir)

    assert rc == 1
    assert not target.exists(), "mismatching file must be removed"
