"""T-190 (F3, MED): ``download_artifact`` muss TLS verifizieren wie alle anderen Calls.

Beleg aus dem Senior-Review 2026-09-30: ``download_artifact`` war der
EINZIGE HTTP-Call ohne ``verify=self._verify`` (13/13 andere setzen es). Ein
Node mit ``tls_ca_cert`` (private CA) verliert genau auf diesem Pfad die
Verifikation gegen die gepinnte CA.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nodes.common import node_utils, relay_client
from nodes.common.relay_client import RelayClient

META = {
    "node_id": "E4W3CBWQ",
    "node_name": "test-node",
    "base_url": "http://relay.test:8788",
    "registration_secret": "rs_test",
}
CA_PATH = "/etc/ssl/private-relay-ca.pem"


@pytest.fixture()
def isolated_relay_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in (
        "META_PATH",
        "TOKEN_PATH",
        "CONFIG_PATH",
        "LEGACY_META_PATH",
        "LEGACY_TOKEN_PATH",
        "STATUS_PATH",
    ):
        monkeypatch.setattr(node_utils, name, tmp_path / f"{name.lower()}.json")
    node_utils.save_token("rt_current", expires_at=None)
    return tmp_path


class _FakeResponse:
    status_code = 200
    headers: dict[str, str] = {"Content-Disposition": 'attachment; filename="a.bin"'}

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self, chunk_size: int = 65536):
        yield self._payload

    def close(self) -> None:
        return None


class _FakeStream:
    def __init__(self, resp: _FakeResponse) -> None:
        self._resp = resp

    def __enter__(self) -> _FakeResponse:
        return self._resp

    def __exit__(self, *exc: Any) -> bool:
        return False


def _patch_stream(monkeypatch: pytest.MonkeyPatch, seen: list[dict]) -> None:
    def _stream(method: str, url: str, **kwargs: Any) -> _FakeStream:
        seen.append({"method": method, "url": url, **kwargs})
        return _FakeStream(_FakeResponse(b"artifact-bytes"))

    class _HttpxProxy:
        def __getattr__(self, name: str):
            import httpx as _real

            return getattr(_real, name)

        stream = staticmethod(_stream)

    monkeypatch.setattr(relay_client, "httpx", _HttpxProxy())


def test_download_artifact_passes_verify(
    isolated_relay_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[dict] = []
    _patch_stream(monkeypatch, seen)
    client = RelayClient(dict(META), {"tls_ca_cert": CA_PATH, "request_timeout": 5})

    target = client.download_artifact("art_1", tmp_path / "out.bin")

    assert target.read_bytes() == b"artifact-bytes"
    assert seen, "download_artifact issued no httpx.stream call"
    assert seen[0].get("verify") == CA_PATH, (
        "download_artifact must pass verify=self._verify (tls_ca_cert)"
    )


def test_download_artifact_defaults_to_system_trust(
    isolated_relay_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[dict] = []
    _patch_stream(monkeypatch, seen)
    client = RelayClient(dict(META), {"request_timeout": 5})

    client.download_artifact("art_1", tmp_path / "out.bin")

    # Default True == system trust store, same as every other call.
    assert seen[0].get("verify") is True
