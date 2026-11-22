#!/usr/bin/env python3
"""T-017: node-side onboarding UI served from the blank-start container.

In the blank-start state (T-011 — the daemon idles because no relay
configuration exists) this module serves a small HTTP UI on port 7783 so
the user can register the node from the host browser without any
daemon-side exec indirection (DEC 2026-11-22: the QEMU-VM node never
reached the relay through the old `docker exec` onboarding path).

``GET /`` IS the page (no landing page / redirect):

* unconfigured → registration form: node name + relay URL. The relay
  field is prefilled from in-process mDNS discovery
  (:func:`nodes.common.relay_client._discover_relay_mdns`); a discovery
  failure never breaks the page — the form simply starts empty.
* configured → status (node identity, relay URL) + the four SPEC §9
  MCP-Bearer snippets, rendered from the node's own ``IOWAP_MCP_BEARER``
  env. Without the env the section shows "Bearer not provisioned".

``POST /configure`` writes the config files the blank-start node expects
(``relay_config.json`` + ``node.yaml`` — an existing node.yaml's
``bridge:`` block is preserved untouched) and then calls the in-process
register logic (:func:`nodes.common.cli.cli_server.register_node`, the
same code path as ``iowap-cli node register`` — no CLI subprocess). The
result (node_id + status ok/pending) is shown inline; errors as plain
text. POST is refused once the node is configured.

Config pickup needs no signal: the T-011 idle loop polls every 2 s and
starts the daemon on its own (see ``node_utils.wait_for_relay_config``).

Security: the server runs ONLY while the node is blank (no relay
traffic, no credentials exist yet) and POSTs are refused afterwards; the
compose profile maps the port to 127.0.0.1 on the host (loopback-only,
mirroring the MCP port 7781).
"""

from __future__ import annotations

import html
import json
import logging
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

from nodes.common import node_config, node_utils
from nodes.common.cli.cli_server import normalize_base_url, register_node
from nodes.common.relay_client import _discover_relay_mdns

logger = logging.getLogger("node-onboarding")

ONBOARDING_PORT = 7783
DEFAULT_MCP_PORT = 7781
_MAX_BODY_BYTES = 16 * 1024

_PAGE_TITLE = "IOWAP node setup"

_STYLE = """
  body { font-family: system-ui, sans-serif; max-width: 640px; margin: 3rem auto; padding: 0 1rem; color: #1c2430; }
  h1 { font-size: 1.4rem; } h2 { font-size: 1.05rem; margin-top: 2rem; }
  label { display: block; margin: 1rem 0 0.25rem; font-weight: 600; }
  input { width: 100%; box-sizing: border-box; padding: 0.5rem; font-size: 1rem; }
  button { margin-top: 1.25rem; padding: 0.55rem 1.4rem; font-size: 1rem; cursor: pointer; }
  .note { color: #5a6472; font-size: 0.9rem; }
  .error { background: #fdecea; border: 1px solid #e5b4ae; padding: 0.75rem; margin: 1rem 0; }
  .ok { background: #e8f5ec; border: 1px solid #b4dfc1; padding: 0.75rem; margin: 1rem 0; }
  pre { background: #f4f6f8; border: 1px solid #dde3ea; padding: 0.75rem; overflow-x: auto; font-size: 0.85rem; }
  dt { font-weight: 600; margin-top: 0.75rem; } dd { margin: 0.15rem 0 0; }
"""


def onboarding_port() -> int:
    """Port to serve on (env override ``IOWAP_ONBOARDING_PORT``, default 7783)."""
    try:
        return int(os.environ.get("IOWAP_ONBOARDING_PORT") or ONBOARDING_PORT)
    except ValueError:
        return ONBOARDING_PORT


def mcp_port() -> int:
    """Host-side MCP port for the snippets (env ``IOWAP_MCP_PORT``, default 7781)."""
    try:
        return int(os.environ.get("IOWAP_MCP_PORT") or DEFAULT_MCP_PORT)
    except ValueError:
        return DEFAULT_MCP_PORT


_mcp_port = mcp_port  # internal alias usable while the parameter shadows the name


# ---------------------------------------------------------------------------
# MCP-Bearer snippets (SPEC §9 — the four golden formats)
# ---------------------------------------------------------------------------


def mcp_snippets(bearer: str, mcp_port: int | None = None) -> dict[str, str]:
    """The four SPEC §9 config snippets for the node's HTTP-MCP endpoint.

    ``bearer`` is the node's ``IOWAP_MCP_BEARER`` env value. Callers render
    a "Bearer not provisioned" section when the env is missing (see
    :func:`_bearer_status`) — this function always formats, never checks.
    """
    port = mcp_port if mcp_port is not None else _mcp_port()
    url = f"http://127.0.0.1:{port}/mcp"
    return {
        "hermes": (
            f"hermes mcp add iowap --url {url} "
            f'--header "Authorization: Bearer {bearer}"'
        ),
        "claude": json.dumps(
            {
                "mcpServers": {
                    "iowap": {
                        "type": "http",
                        "url": url,
                        "headers": {"Authorization": f"Bearer {bearer}"},
                    }
                }
            },
            indent=2,
        ),
        "codex": (
            "[mcp_servers.iowap]\n"
            f'url = "{url}"\n'
            f'http_headers = {{ "Authorization" = "Bearer {bearer}" }}'
        ),
        "curl": f'curl -H "Authorization: Bearer {bearer}" {url}',
    }


# ---------------------------------------------------------------------------
# Configuration write + in-process register (the POST /configure core)
# ---------------------------------------------------------------------------


def _write_relay_config(base_url: str) -> None:
    """Write/refresh relay_config.json with the new base_url (atomically)."""
    cfg = node_utils.load_config()
    cfg["base_url"] = base_url
    node_utils.write_json_atomic(node_utils.CONFIG_PATH, cfg)


def _update_node_yaml(node_name: str) -> None:
    """Set ``node_name`` in node.yaml without touching anything else.

    The entrypoint (iowap-docker) seeds node.yaml even in blank-start and
    the SPEC §4 ``bridge:`` block may already live there — the file is
    loaded, only the ``node_name`` key is set, and everything else
    survives the round-trip. Missing file → minimal fresh document.
    """
    import yaml

    path = node_config.ACTIVE_PATH
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception as exc:  # noqa: BLE001 — keep the block, warn only
            logger.warning("could not parse %s (%s) — rewriting node_name only", path, exc)
    data["node_name"] = node_name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


def configure_node(name: str, relay: str) -> dict[str, Any]:
    """Handle a form submission: write config, register in-process.

    Never raises. Returns a result dict:
    ``{"ok": bool, "http_status": int, "node_id"?, "node_name", "status"?, "error"?}``.
    Nothing is written when validation fails; on a failed registration the
    config files stay (harmless — the node stays blank without meta/token)
    so the user can simply retry.
    """
    name = (name or "").strip()
    relay = (relay or "").strip()
    if node_utils.relay_config_ready() is not None:
        return {
            "ok": False,
            "http_status": 409,
            "configured": True,
            "error": "This node is already configured — the form is disabled. "
            "The page below shows its status.",
        }
    if not name:
        return {"ok": False, "http_status": 400, "error": "node name is required"}
    if not relay:
        return {"ok": False, "http_status": 400, "error": "relay URL is required"}
    base_url = normalize_base_url(relay)
    _write_relay_config(base_url)
    _update_node_yaml(name)
    result = register_node(base_url, name=name, force=True, timeout=15.0)
    if not result.get("ok"):
        return {
            "ok": False,
            "http_status": 502,
            "node_name": name,
            "base_url": base_url,
            "error": result.get("error") or "registration failed",
        }
    return {
        "ok": True,
        "http_status": 200,
        "node_id": result.get("node_id"),
        "node_name": result.get("node_name") or name,
        "status": result.get("status") or "pending",
        "base_url": base_url,
    }


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def _discover_prefill() -> str:
    """mDNS prefill for the relay field — failure-safe, never raises."""
    try:
        return _discover_relay_mdns(timeout=2.0) or ""
    except Exception as exc:  # noqa: BLE001 — the form must stay functional
        logger.warning("mDNS prefill failed: %s", exc)
        return ""


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _page(body: str) -> bytes:
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{_esc(_PAGE_TITLE)}</title>"
        f"<style>{_STYLE}</style></head><body>{body}</body></html>"
    )
    return doc.encode("utf-8")


def _render_form(error: str | None = None, name: str = "", relay: str = "") -> bytes:
    prefill = relay if relay else _discover_prefill()
    err = f"<div class='error'>{_esc(error)}</div>" if error else ""
    body = f"""
<h1>{_esc(_PAGE_TITLE)}</h1>
<p>This node container is not registered yet. Give the node a name and
point it at your IOWAP relay — the daemon picks the configuration up
automatically (within a few seconds) and starts working.</p>
{err}
<form method="post" action="/configure">
  <label for="name">Node name</label>
  <input id="name" name="name" value="{_esc(name)}" required
         placeholder="e.g. windows-vm-node" autofocus>
  <label for="relay">Relay URL</label>
  <input id="relay" name="relay" value="{_esc(prefill)}"
         placeholder="http://192.0.2.60:8788">
  <p class="note">Prefilled via mDNS discovery when a relay was found —
  just edit it if the field is empty or wrong.</p>
  <button type="submit">Register node</button>
</form>
"""
    return _page(body)


def _render_status(
    *,
    banner: str | None = None,
    node_id: str | None = None,
    register_status: str | None = None,
) -> bytes:
    try:
        meta = node_utils.load_meta()
    except FileNotFoundError:
        meta = {}
    cfg = node_utils.load_config()
    node_id = node_id or meta.get("node_id") or "unknown"
    node_name = meta.get("node_name") or "(unnamed)"
    base_url = (
        os.environ.get("RELAY_BASE_URL")
        or cfg.get("base_url")
        or meta.get("base_url")
        or "(unknown)"
    )
    token = node_utils.load_token()
    banner_html = f"<div class='ok'>{_esc(banner)}</div>" if banner else ""
    bearer = os.environ.get("IOWAP_MCP_BEARER") or ""
    if bearer:
        snips = mcp_snippets(bearer)
        mcp_section = f"""
<h2>Agent access (HTTP-MCP)</h2>
<p class="note">Bearer read from this container's <code>IOWAP_MCP_BEARER</code>.
Keep it secret — it grants full tool access to this node.</p>
<dl>
  <dt>Hermes</dt><dd><pre>{_esc(snips['hermes'])}</pre></dd>
  <dt>Claude Desktop / Claude Code</dt><dd><pre>{_esc(snips['claude'])}</pre></dd>
  <dt>Codex (config.toml)</dt><dd><pre>{_esc(snips['codex'])}</pre></dd>
  <dt>Live probe (curl)</dt><dd><pre>{_esc(snips['curl'])}</pre></dd>
</dl>"""
    else:
        mcp_section = f"""
<h2>Agent access (HTTP-MCP)</h2>
<div class='error'>Bearer not provisioned — the environment variable
<code>IOWAP_MCP_BEARER</code> is not set in this container, so no agent
config snippets can be rendered. Recreate the container with the
variable set (the bridge daemon injects it at bootstrap).</div>"""
    body = f"""
<h1>{_esc(_PAGE_TITLE)}</h1>
{banner_html}
<dl>
  <dt>Node</dt><dd>{_esc(node_name)} (ID {_esc(node_id)})</dd>
  <dt>Relay</dt><dd>{_esc(base_url)}</dd>
  <dt>Status</dt><dd>{_esc(register_status or "registered")}</dd>
  <dt>Runtime token</dt><dd>{"present" if token and token.get("token") else "missing"}</dd>
</dl>
<p class="note">The daemon picks up this configuration automatically
(2&nbsp;s poll) and starts heartbeating. If the relay shows the node as
<em>pending</em>, an admin must approve it first.</p>
{mcp_section}
"""
    return _page(body)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class OnboardingHandler(BaseHTTPRequestHandler):
    """Serves the onboarding page; POST only while unconfigured."""

    server_version = "iowap-onboarding/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — stdlib name
        logger.debug("onboarding: " + format, *args)

    def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — stdlib naming
        path = self.path.split("?", 1)[0]
        if path != "/":
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
            return
        if node_utils.relay_config_ready() is not None:
            self._send(200, _render_status())
        else:
            self._send(200, _render_form())

    def do_POST(self) -> None:  # noqa: N802 — stdlib naming
        path = self.path.split("?", 1)[0]
        if path != "/configure":
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_BODY_BYTES:
            self._send(413, b"form body too large or missing\n", "text/plain; charset=utf-8")
            return
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        form = parse_qs(raw, keep_blank_values=True)
        name = (form.get("name") or [""])[0]
        relay = (form.get("relay") or [""])[0]
        result = configure_node(name, relay)
        if result["ok"]:
            banner = (
                f"Node registered: {result['node_name']} "
                f"(ID {result['node_id']}) — relay status: {result['status']}"
            )
            self._send(
                200,
                _render_status(
                    banner=banner,
                    node_id=result.get("node_id"),
                    register_status=result.get("status"),
                ),
            )
            return
        if result.get("configured"):
            # after configuration the form is gone for good — status page only
            self._send(result["http_status"], _render_status(banner=result["error"]))
            return
        if result["http_status"] == 400:
            # validation — re-show the form with the user's input preserved
            self._send(400, _render_form(error=result["error"], name=name, relay=relay))
            return
        self._send(result["http_status"], _render_form(error=result["error"], name=name, relay=relay))


def start_if_blank(*, host: str = "0.0.0.0", port: int | None = None) -> ThreadingHTTPServer | None:
    """Start the onboarding server when the node is blank; else ``None``.

    Never raises — a broken onboarding server must not take the daemon
    down (the T-011 idle path keeps working without it). The server runs
    in a daemon thread; stop it with :func:`shutdown`.
    """
    try:
        if node_utils.relay_config_ready() is not None:
            return None
        bind_port = onboarding_port() if port is None else port
        server = ThreadingHTTPServer((host, bind_port), OnboardingHandler)
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever,
            name="onboarding-ui",
            daemon=True,
            kwargs={"poll_interval": 0.2},
        )
        thread.start()
        logger.info(
            "onboarding UI serving on http://%s:%d (blank-start node setup)",
            host,
            server.server_address[1],
        )
        return server
    except Exception as exc:  # noqa: BLE001 — never break the daemon
        logger.warning("onboarding UI could not start: %s", exc)
        return None


def shutdown(server: ThreadingHTTPServer | None) -> None:
    """Stop a server created by :func:`start_if_blank` (idempotent)."""
    if server is None:
        return
    try:
        server.shutdown()
        server.server_close()
    except Exception as exc:  # noqa: BLE001 — best-effort teardown
        logger.warning("onboarding UI shutdown failed: %s", exc)
