"""T-243: hygiene series (audit F8, F9, F10, F12, F13, F14 — LOW).

* F8  update fallback: the wheel path installs with ``--no-deps`` BY
  DESIGN (protect the venv from dependency churn) — so a new wheel that
  needs a NEW dependency would pass "pip ok" and crash the daemon at the
  restart. Contract: an import smoke with the TARGET interpreter runs
  after the install and BEFORE the restart; a failed smoke = honest
  failure result, no restart, no fake success.
* F9  token writes set 0o600 AT CREATION (``os.open(O_CREAT|O_WRONLY|
  O_TRUNC, 0o600)``), not via a later chmod — the old write_text+chmod
  sequence published the plaintext token through a crash window with
  umask-default permissions. Same treatment for ``write_json_atomic``.
* F10 install.sh must not carry the real LAN relay IP in a public
  repo (TEST-NET rule) — example URL becomes 192.0.2.60.
* F12 download reservation is atomic BEFORE the stream: parallel
  requests must not all pass the ``max_downloads`` gate (the old flow
  counted only after the send — two racing readers could stream a
  single-download transfer; the aborted path rolls the reservation back
  so F6's "retry possible until TTL" stays true).
* F13 ``hp put`` help documents the MOVE semantics (bridge rung: the
  file moves into the serve dir and the staged copy is deleted after
  transfer).
* F14 ``_post_with_retry`` → ``_post_with_auth_retry`` — the name
  claimed network-retry semantics that don't exist (POSTs here are not
  idempotent: only the 401/403 token refresh repeats a request once).

RED evidence: F12's parallel gate + F8's smoke tests + F13/F14/F9/F10
locks fail against the pre-fix tree. The F12 abort-rollback test is a
contract lock that pins the ROLLBACK of the new reservation (a naive
reservation without rollback fails it post-reservation).
"""

from __future__ import annotations

import json
import stat as stat_mod
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from nodes.common import file_serve, node_utils

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- F9: token/json writes are 0600 from creation -------------------------


@pytest.fixture()
def relay_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(node_utils, "TOKEN_PATH", tmp_path / "token.json")
    monkeypatch.setattr(node_utils, "LEGACY_TOKEN_PATH", tmp_path / "leg.token")
    return tmp_path


def _mode(path: Path) -> int:
    return stat_mod.S_IMODE(path.stat().st_mode)


def test_save_token_permissions_survive_missing_chmod(relay_paths, monkeypatch):
    """Simulate the crash window: chmod is unavailable — the file must
    STILL be 0600 because the mode is set at os.open time (F9)."""

    def no_chmod(*_a, **_k):
        raise AssertionError("chmod must not be needed")

    monkeypatch.setattr(node_utils.os, "chmod", no_chmod)
    node_utils.save_token("tp_new_token")

    assert _mode(relay_paths / "token.json") == 0o600


def test_write_json_atomic_permissions_survive_missing_chmod(
    relay_paths, monkeypatch
):
    def no_chmod(*_a, **_k):
        raise AssertionError("chmod must not be needed")

    monkeypatch.setattr(node_utils.os, "chmod", no_chmod)
    node_utils.write_json_atomic(relay_paths / "meta.json", {"node_id": "X"})

    assert _mode(relay_paths / "meta.json") == 0o600
    assert json.loads((relay_paths / "meta.json").read_text()) == {"node_id": "X"}


# --- F10: install.sh carries no real LAN IP --------------------------------


def test_install_sh_uses_testnet_relay_ip():
    text = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
    assert "192.168.2.60" not in text
    assert "http://192.0.2.60:8788" in text


# --- F13: hp put help carries the move semantics ---------------------------


def test_hp_put_help_documents_move_semantics():
    from nodes.common import node_cli
    from nodes.common.node_cli import build_parser

    parser = build_parser()
    hp_parser = parser._subparsers._group_actions[0].choices.get("hp")  # type: ignore[union-attr]
    assert hp_parser is not None
    put_parser = hp_parser._subparsers._group_actions[0].choices["put"]
    assert put_parser is not None

    doc = node_cli.__doc__ or ""
    assert "moves the file into the serve dir" in doc
    # Surface 1: `node-cli hp --help` shows the put subcommand help.
    flat_hp = " ".join(hp_parser.format_help().split())
    assert (
        "moves the file into the serve dir and deletes it after transfer"
        in flat_hp
    )
    # Surface 2: `node-cli hp put --help` shows the path argument's move
    # warning (argparse wraps lines — compare whitespace-normalized).
    flat_put = " ".join(put_parser.format_help().split())
    assert "moves the file into the serve dir" in flat_put.lower() or (
        "bridge rung moves the file" in flat_put
    )


# --- F14: the auth-retry rename + honest docstring --------------------------


def test_post_with_retry_renamed_and_no_network_retry():
    from nodes.common import relay_client as rc

    assert hasattr(rc.RelayClient, "_post_with_auth_retry")
    assert not hasattr(rc.RelayClient, "_post_with_retry")
    doc = rc.RelayClient._post_with_auth_retry.__doc__ or ""
    assert "not idempotent" in doc.lower() or "non-idempotent" in doc.lower()
    # The old name is gone everywhere the wheel ships it.
    source = (REPO_ROOT / "nodes/common/relay_client.py").read_text(encoding="utf-8")
    assert "_post_with_retry" not in source


def test_post_with_auth_retry_refreshes_once_on_401(monkeypatch):
    """Behavior lock after the rename: 401 → one token refresh → one
    repeat — the ONLY replay this method does (no network retries)."""
    from nodes.common.relay_client import RelayClient

    META = {
        "node_id": "243NODE",
        "registration_secret": "rs_x",
        "base_url": "http://relay.test:8788",
    }
    client = RelayClient(dict(META), {"base_url": None, "request_timeout": 5})
    posts: list[dict] = []

    def fake_post(_self, path, body=None, timeout=None, **_kw):
        posts.append(1)
        resp = MagicMock()
        resp.status_code = 401 if len(posts) == 1 else 200
        return resp

    monkeypatch.setattr(RelayClient, "_post", fake_post)
    monkeypatch.setattr(RelayClient, "_refresh_token", lambda self: True)

    client._post_with_auth_retry("/relay/v2/test", {"x": 1})

    assert len(posts) == 2, "exactly the auth replay — no network retry (T-243 F14)"


# --- F8: import smoke after the pip install ---------------------------------


def _fake_wheel_machinery(monkeypatch, tmp_path, smoke_outcome, version="2.3.31"):
    """Wire apply_wheel_update without network/real pip: record the
    subprocess calls, control the smoke outcome."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if any(tok == "-c" for tok in cmd) and smoke_outcome == "fail":
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=cmd,
                stderr="ModuleNotFoundError: new_dependency",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(node_utils.subprocess, "run", fake_run)
    monkeypatch.setattr(
        node_utils,
        "check_wheel_updates",
        lambda **kw: {
            "update_available": True,
            "asset_url": "https://example.invalid/iowap_node-X-py3-none-any.whl",
            "asset_name": "iowap_node-X-py3-none-any.whl",
        },
    )
    fake_resp = MagicMock()
    fake_resp.__enter__.return_value.read.return_value = b"wheel-bytes"
    monkeypatch.setattr(node_utils.urllib.request, "urlopen", lambda *a, **k: fake_resp)
    monkeypatch.setattr(node_utils, "get_local_wheel_version", lambda: version)
    return calls


def test_wheel_update_runs_import_smoke_before_restart(monkeypatch, tmp_path):
    """The GitHub-wheel fallback installs with --no-deps BY DESIGN — the
    import smoke with the target interpreter guards against a wheel whose
    new dependency is missing (pip 'ok', daemon dead at restart)."""
    calls = _fake_wheel_machinery(monkeypatch, tmp_path, smoke_outcome="ok")
    restarts: list[str] = []
    monkeypatch.setattr(
        node_utils,
        "_restart_service",
        lambda *a, **k: (restarts.append("r"), (True, ""))[1],
    )

    result = node_utils.apply_wheel_update(wheel_dir=tmp_path / "wheels")

    assert result["success"] is True
    smoke_calls = [cmd for cmd in calls if "-c" in cmd]
    assert smoke_calls, "no import smoke ran before the restart (F8)"
    assert "import nodes.common.node_cli" in " ".join(smoke_calls[-1])
    # The smoke runs BEFORE the restart.
    pip_call_idx = next(i for i, c in enumerate(calls) if "pip" in " ".join(c))
    assert calls.index(smoke_calls[-1]) == pip_call_idx + 1
    assert len(restarts) == 1


def test_wheel_update_reports_failed_import_smoke(monkeypatch, tmp_path):
    """A broken (e.g. missing-dependency) install fails loudly — no
    restart behind it, no fake success."""
    calls = _fake_wheel_machinery(monkeypatch, tmp_path, smoke_outcome="fail")
    monkeypatch.setattr(
        node_utils, "_restart_service", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("no restart behind a failed import smoke")
        )
    )

    result = node_utils.apply_wheel_update(wheel_dir=tmp_path / "wheels")

    assert result["success"] is False
    assert "import smoke failed" in result["message"]
    assert "new_dependency" in result["message"]
    assert any(tok == "-c" for cmd in calls for tok in cmd)


# --- F12: download reservation is atomic before the stream -----------------


@pytest.fixture
def serve_dir(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "serve"
    monkeypatch.setattr(file_serve, "SERVE_DIR", d)
    return d


@pytest.fixture
def serve_globals(monkeypatch):
    monkeypatch.setattr(file_serve, "_serve_thread", None)
    monkeypatch.setattr(file_serve, "_serve_server", None)
    monkeypatch.setattr(file_serve, "_ON_EXHAUSTED", None)
    monkeypatch.setattr(file_serve, "_last_sweep", 0.0)


def _stage(serve_dir: Path, payload: bytes, max_downloads: int = 1):
    src = serve_dir.parent / "src.bin"
    src.write_bytes(payload)
    return file_serve.stage_file(src, max_downloads=max_downloads)


def _spawn(handler_cls) -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    # D9 allowlist cache like the daemon (contract: handler reads
    # server._allow_ip); the test peer is loopback.
    server._allow_ip = frozenset({"127.0.0.1", "::1"})  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except (OSError, FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(0.01)
    return False


def _manifest_downloads(serve_dir: Path, tid: str) -> int:
    manifest = serve_dir / f"{tid}.json"
    if not manifest.exists():
        return -1  # cleaned up = allowance exhausted (caller judges)
    return int(json.loads(manifest.read_text()).get("downloads", 0))


def test_parallel_downloads_cannot_breach_max_downloads(
    serve_dir, serve_globals, monkeypatch
):
    """max_downloads=1 + two racing readers: the LATE one is refused at
    admission (the winner reserved the allowance BEFORE streaming). Pre-
    fix both streamed — counting happened only after the send."""
    payload = b"single-download-transfer"
    tid, staged, _ = _stage(serve_dir, payload, max_downloads=1)
    server, port = _spawn(file_serve.EphemeralServeHandler)
    url = f"http://127.0.0.1:{port}/transfer/{tid}"

    release = threading.Event()
    orig_copy = file_serve.shutil.copyfileobj

    def gated_copy(fh, wfile, length=None):
        # Only the admitted winner reaches the stream — hold it until the
        # late request has been checked.
        release.wait(timeout=5)
        orig_copy(fh, wfile, length=length)

    monkeypatch.setattr(file_serve.shutil, "copyfileobj", gated_copy)

    first_result: list[httpx.Response] = []

    def first_download() -> None:
        first_result.append(httpx.post(url, content=b""))

    th = threading.Thread(target=first_download)
    th.start()
    try:
        # Wait until the winner's reservation is on disk.
        assert _wait_until(
            lambda: _manifest_downloads(serve_dir, tid) >= 1
        ), "the reservation must be written before the stream (F12)"

        late = httpx.post(url, content=b"")
        assert late.status_code == 404, (
            "the second racing reader must not pass the max_downloads gate"
        )

        release.set()
        th.join(timeout=5)
        first = first_result[0]
        assert first.status_code == 200
        assert first.content == payload
        # F6 completion: file + manifest gone after the reserved download.
        assert _wait_until(lambda: _manifest_downloads(serve_dir, tid) == -1)
    finally:
        release.set()
        server.shutdown()
        server.server_close()


def test_aborted_download_rolls_back_reservation(serve_dir, serve_globals, monkeypatch):
    """Contract lock on the rollback: after the aborted stream the
    allowance is back to 0 — a retry stays possible until the TTL (F6).
    A naive F12 reservation WITHOUT rollback fails this (downloads stays
    1 + the retry is refused)."""
    payload = b"retry-after-abort"
    tid, staged, _ = _stage(serve_dir, payload, max_downloads=2)
    server, port = _spawn(file_serve.EphemeralServeHandler)
    url = f"http://127.0.0.1:{port}/transfer/{tid}"

    def aborting_copy(fh, wfile, length=None):
        raise ConnectionResetError("client hung up mid-stream")

    monkeypatch.setattr(file_serve.shutil, "copyfileobj", aborting_copy)

    # Server cuts the connection mid-response — the client may see an
    # HTTP error OR a broken body; both are the abort path.
    try:
        httpx.post(url, content=b"")
    except httpx.HTTPError:
        pass

    # The reservation must be rolled back under the lock.
    assert _wait_until(
        lambda: _manifest_downloads(serve_dir, tid) == 0
    ), "an aborted download must roll its reservation back (retry budget intact)"
    assert staged.exists(), "the transfer survives the abort (retry until TTL)"