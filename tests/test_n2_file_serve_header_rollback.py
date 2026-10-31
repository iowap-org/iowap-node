"""N2 (audit 2026-10-31, F12 regression): client abort BEFORE the response
headers must roll back the download reservation.

The F12 reservation is written BEFORE streaming — but the header block
(``send_response``/``send_header``/``end_headers``) writes to ``wfile``
too. Before the fix it sat OUTSIDE the abort handler, so a client that
disconnected before the first byte raised there: the reservation stayed
consumed, the rollback never ran, and the retry ran into 404 "download
allowance exhausted" without receiving a byte.

Test mirrors the test_t243 raced/allowance pattern (real
ThreadingHTTPServer, real sockets). Determinism: the handler subclass
gates the FIRST ``send_header`` until the client has RST-closed its
socket (SO_LINGER 0 close), so the first real header write hits a dead
peer — the pre-header abort of the audit repro, no timing luck.

Pre-fix this test is RED (reservation stuck at 1 → wait times out);
post-fix it is GREEN (rollback → retry gets 200 + payload).
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from nodes.common import file_serve


# --- isolation (test_t243 pattern) -------------------------------------------


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
        return -1  # cleaned up = request completed/cleaned (caller judges)
    return int(json.loads(manifest.read_text()).get("downloads", 0))


# --- the gated handler --------------------------------------------------------


class AbortBeforeHeadersHandler(file_serve.EphemeralServeHandler):
    """Gates the FIRST ``send_header`` call until the client aborted.

    The first ``send_header`` happens INSIDE ``send_response`` (Server
    header) — before the gate releases, NO response byte was written;
    after it, the real write goes to the RST-closed socket and raises.
    The gate rides on the server instance, so the retry request (same
    server, ``first_attempt`` already set) passes through untouched.
    """

    def send_header(self, keyword, value):  # noqa: D102 — test-local override
        gate = getattr(self.server, "_abort_gate", None)
        if gate is not None and not gate["first_attempt"].is_set():
            gate["first_attempt"].set()
            gate["client_aborted"].wait(timeout=5)
        super().send_header(keyword, value)


def _spawn_abort_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), AbortBeforeHeadersHandler)
    # D9 allowlist cache like the daemon; the test peer is loopback.
    server._allow_ip = frozenset({"127.0.0.1", "::1"})  # type: ignore[attr-defined]
    server._abort_gate = {  # type: ignore[attr-defined]
        "first_attempt": threading.Event(),
        "client_aborted": threading.Event(),
    }
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _rst_close(sock: socket.socket) -> None:
    """Close with SO_LINGER=0 → immediate RST (no FIN handshake)."""
    sock.setsockopt(
        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
    )
    sock.close()


def test_abort_before_headers_rolls_back_reservation_retries_fresh(
    serve_dir, serve_globals
):
    """Abort pre-headers → reservation rolled back → retry gets the bytes.

    Pre-fix (RED): send block outside the abort handler → reservation
    stuck at 1 → the rollback wait times out. Post-fix (GREEN): rollback
    → the retry streams the payload instead of 404 "allowance exhausted".
    """
    payload = b"n2-abort-before-headers"
    (serve_dir.parent / "src.bin").write_bytes(payload)
    tid, staged, _ = file_serve.stage_file(serve_dir.parent / "src.bin", max_downloads=1)
    server, port = _spawn_abort_server()
    try:
        # 1. the aborted attempt: full request, socket closed BEFORE any
        #    response byte (gated against the first header write).
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        request = (
            f"POST /transfer/{tid} HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Length: 0\r\n"
            "\r\n"
        ).encode("ascii")
        sock.sendall(request)
        gate = server._abort_gate  # type: ignore[attr-defined]
        assert gate["first_attempt"].wait(timeout=5), "handler never reached send_header"
        _rst_close(sock)
        gate["client_aborted"].set()

        # 2. the fix: the reservation is rolled back (manifest shows 0).
        #    Pre-fix this never happens — downloads stays 1 → RED here.
        assert _wait_until(
            lambda: _manifest_downloads(serve_dir, tid) == 0
        ), f"reservation not rolled back (downloads={_manifest_downloads(serve_dir, tid)})"

        # 3. the retry transfers for real: 200 + payload + counted + cleanup.
        r = httpx.post(f"http://127.0.0.1:{port}/transfer/{tid}", content=b"")
        assert r.status_code == 200, r.status_code
        assert r.content == payload
        assert r.headers["Content-Length"] == str(len(payload))
        assert _wait_until(lambda: _manifest_downloads(serve_dir, tid) == -1)
        assert not (serve_dir / f"{tid}.json").exists()
        assert not staged.exists()
    finally:
        server.shutdown()
        server.server_close()