#!/usr/bin/env python3
"""T-017 + T-021: node UI served on port 7783 — onboarding form + full dashboard.

In the blank-start state (T-011 — the daemon idles because no relay
configuration exists) this module serves a small HTTP UI on port 7783 so
the user can register the node from the host browser without any
daemon-side exec indirection (DEC 2026-11-22: the QEMU-VM node never
reached the relay through the old `docker exec` onboarding path).

``GET /`` IS the page (no landing page / redirect); rendering is
state-based, so the SAME server survives the blank→configured
transition and becomes the full node dashboard (T-021):

* unconfigured → registration form: node name + relay URL. The relay
  field is prefilled from in-process mDNS discovery
  (:func:`nodes.common.relay_client._discover_relay_mdns`); a discovery
  failure never breaks the page — the form simply starts empty.
* configured → the node dashboard (design language of the relay
  dashboard in iowap-server: dark tokens, topnav, status pills, tables,
  badges) with three sections:
  - Overview: node identity, relay URL, status, bridge connection, version.
  - Capability management (via :mod:`nodes.common.dashboard`): the
    workspace ``bridge/providers.yaml`` registry — create (process/http),
    delete, and the publish toggle (node.yaml ``bridge.allowlist`` /
    ``auto_publish`` + daemon SIGHUP).
  - MCP: enable/disable of the in-container MCP process, bearer rotation
    (``~/.relay/mcp_bearer`` + supervisor restart) and the four SPEC §9
    snippets rendered with the CURRENT bearer.

``POST /configure`` writes the config files the blank-start node expects
(``relay_config.json`` + ``node.yaml`` — an existing node.yaml's
``bridge:`` block is preserved untouched) and then calls the in-process
register logic (:func:`nodes.common.cli.cli_server.register_node`, the
same code path as ``iowap-cli node register`` — no CLI subprocess). The
result (node_id + status ok/pending) is shown inline; errors as plain
text. POST is refused once the node is configured. All other POST
routes (``/caps/*``, ``/mcp/*``, ``/agents/*``) are refused while blank (409).

Config pickup needs no signal: the T-011 idle loop polls every 2 s and
starts the daemon on its own (see ``node_utils.wait_for_relay_config``).

T-035 P1c adds the fourth section, "Agent-Tokens": create (show-once raw
token rendered exactly in that POST's response), revoke (immediate MCP
verification cut-off) and the caps-manage toggle — all backed by
:mod:`nodes.common.agent_tokens` on the dedicated ``/data/agents``
volume. The primary bearer itself is never rendered here (T-016e keeps
its rotate flow).

Security: the server is loopback-mapped on the host (compose profile —
mirroring the MCP port 7781) and unauthenticated by design: it can only
be reached from the node host itself, the same trust level as the T-017
registration form. The dashboard writes only the three T-021 files
(workspace providers.yaml, node.yaml bridge allowlist/auto_publish,
~/.relay/mcp_bearer) — no bridge logic, no relay API, no daemon code.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

from nodes.common import agent_tokens, bridge, dashboard, node_config, node_utils
from nodes.common.cli.cli_server import normalize_base_url, register_node
from nodes.common.relay_client import _discover_relay_mdns

logger = logging.getLogger("node-onboarding")

ONBOARDING_PORT = 7783
DEFAULT_MCP_PORT = 7781
_MAX_BODY_BYTES = 16 * 1024

_PAGE_TITLE = "IOWAP node setup"

# Design tokens + components borrowed from the relay dashboard
# (iowap-server static/admin.css — same palette, same component shapes;
# no copy-paste, one condensed stylesheet for this single page).
_STYLE = """
  :root { --bg:#0b0d14; --nav:#12141e; --panel:#181b26; --panel2:#1c2030;
    --text:#e4e8f0; --muted:#848aa0; --accent:#6c8cff; --accent2:#a78bfa;
    --ok:#34d399; --warn:#fbbf24; --bad:#f87171; --border:#262a38; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font-family: system-ui, -apple-system, sans-serif; font-size:15px; }
  .topnav { background:var(--nav); border-bottom:1px solid var(--border);
    padding:0 1.5rem; display:flex; align-items:center; height:56px; gap:.5rem;
    position:sticky; top:0; }
  .topnav .logo { font-weight:700; color:var(--accent); letter-spacing:-.02em; }
  .topnav .logo span { color:var(--muted); font-weight:400; }
  .topnav .spacer { flex:1; }
  .wrap { max-width:1100px; margin:0 auto; padding:1.5rem 1.5rem 3rem; }
  h1 { font-size:1.5rem; margin:.5rem 0 1rem; }
  h2 { font-size:1.1rem; margin:2rem 0 .75rem; color:var(--text); }
  a { color:var(--accent); }
  .card { background:var(--panel); border:1px solid var(--border);
    border-radius:.75rem; padding:1.25rem; margin-bottom:1.25rem; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
    gap:1rem; }
  .kv { background:var(--panel); border:1px solid var(--border);
    border-radius:.6rem; padding:.85rem 1rem; }
  .kv .k { color:var(--muted); font-size:.78rem; text-transform:uppercase;
    letter-spacing:.05em; margin-bottom:.3rem; }
  .kv .v { font-size:.95rem; word-break:break-all; }
  .mono { font-family:ui-monospace,Menlo,Consolas,monospace; }
  .pill { display:inline-flex; align-items:center; gap:.45rem; padding:.3rem .85rem;
    border-radius:2rem; font-size:.82rem; border:1px solid var(--border);
    background:var(--panel2); color:var(--muted); }
  .pill .dot { width:8px; height:8px; border-radius:50%; background:var(--muted); }
  .pill.ok { color:var(--ok); border-color:rgba(52,211,153,.3);
    background:rgba(52,211,153,.08); }
  .pill.ok .dot { background:var(--ok); animation:pulse 2s infinite; }
  .pill.warn { color:var(--warn); border-color:rgba(251,191,36,.3);
    background:rgba(251,191,36,.08); }
  .pill.warn .dot { background:var(--warn); }
  .pill.bad { color:var(--bad); border-color:rgba(248,113,113,.3);
    background:rgba(248,113,113,.08); }
  .pill.bad .dot { background:var(--bad); }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.4; } }
  table { width:100%; border-collapse:collapse; font-size:.9rem; }
  th { text-align:left; color:var(--muted); font-size:.75rem; text-transform:uppercase;
    letter-spacing:.05em; padding:.5rem .6rem; border-bottom:1px solid var(--border); }
  td { padding:.6rem; border-bottom:1px solid var(--border); vertical-align:top; }
  tr:last-child td { border-bottom:none; }
  .badge { display:inline-block; padding:.15rem .6rem; border-radius:.8rem;
    font-size:.75rem; border:1px solid var(--border); color:var(--muted); }
  .badge.ok { color:var(--ok); border-color:rgba(52,211,153,.35); }
  .badge.muted { color:var(--muted); }
  .badge.accent { color:var(--accent); border-color:rgba(108,140,255,.4); }
  .btn { padding:.45rem .9rem; border:1px solid var(--border); background:var(--panel2);
    color:var(--text); border-radius:.4rem; cursor:pointer; font-size:.85rem;
    transition:border-color .15s; }
  .btn:hover { border-color:var(--accent); }
  .btn-primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  .btn-danger { background:transparent; border-color:var(--bad); color:var(--bad); }
  .btn-danger:hover { background:var(--bad); color:#0b0d14; }
  form.inline { display:inline; }
  label { display:block; margin:.9rem 0 .25rem; font-weight:600; font-size:.9rem; }
  input[type=text], input:not([type]), select, textarea { width:100%; padding:.5rem;
    font-size:.95rem; border-radius:.4rem; border:1px solid var(--border);
    background:var(--panel2); color:var(--text); }
  input:focus, select:focus { outline:none; border-color:var(--accent); }
  .fields { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
    gap:.25rem 1rem; }
  .note { color:var(--muted); font-size:.82rem; }
  .flash { padding:.75rem 1rem; border-radius:.5rem; margin:1rem 0; font-size:.9rem; }
  .flash.ok { background:rgba(52,211,153,.08); border:1px solid rgba(52,211,153,.3);
    color:var(--ok); }
  .flash.error { background:rgba(248,113,113,.08); border:1px solid rgba(248,113,113,.35);
    color:var(--bad); }
  .error { background:rgba(248,113,113,.08); border:1px solid rgba(248,113,113,.35);
    padding:.75rem; margin:1rem 0; border-radius:.5rem; color:var(--bad); }
  .ok { background:rgba(52,211,153,.08); border:1px solid rgba(52,211,153,.3);
    padding:.75rem; margin:1rem 0; border-radius:.5rem; color:var(--ok); }
  pre { background:var(--panel2); border:1px solid var(--border); border-radius:.5rem;
    padding:.75rem; overflow-x:auto; font-size:.8rem; }
  code { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:.85em;
    background:var(--panel2); padding:.1rem .3rem; border-radius:.25rem; }
  dl { margin:.5rem 0; } dt { font-weight:600; margin-top:.75rem; font-size:.9rem; }
  dd { margin:.15rem 0 0; }
  @media (max-width:640px) { .wrap { padding:1rem .75rem 2rem; } }
"""


def onboarding_port() -> int:
    """Port to serve on (env override ``IOWAP_ONBOARDING_PORT``, default 7783)."""
    try:
        return int(os.environ.get("IOWAP_ONBOARDING_PORT") or ONBOARDING_PORT)
    except ValueError:
        return ONBOARDING_PORT


def mcp_port() -> int:
    """Host-side MCP port for the snippets (env ``IOWAP_MCP_PORT``, default 7781)."""
    return dashboard.mcp_port()


_mcp_port = mcp_port  # internal alias usable while the parameter shadows the name


# ---------------------------------------------------------------------------
# MCP-Bearer snippets (SPEC §9 — the four golden formats)
# ---------------------------------------------------------------------------


def mcp_snippets(bearer: str, mcp_port: int | None = None) -> dict[str, str]:
    """The four SPEC §9 config snippets for the node's HTTP-MCP endpoint.

    ``bearer`` is the node's CURRENT runtime bearer (``~/.relay/mcp_bearer``
    since T-021, falling back to the ``IOWAP_MCP_BEARER`` env). Callers
    render a "no bearer yet" note when neither exists — this function
    always formats, never checks.
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
        # T-016f: the streamable-HTTP transport requires the MCP handshake —
        # initialize first (the response carries an Mcp-Session-Id header),
        # then further calls WITH that session id. A single-shot curl gets
        # "-32600 Missing session ID" and looks like a server bug.
        "curl": (
            "# 1) Handshake: initialize - die Antwort hat einen Mcp-Session-Id Header\n"
            "curl -i -X POST " + url + " -H \"Authorization: Bearer " + bearer + "\" "
            "-H \"Content-Type: application/json\" "
            "-H \"Accept: application/json, text/event-stream\" "
            "-d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\","
            "\"params\":{\"protocolVersion\":\"2025-03-26\",\"capabilities\":{},"
            "\"clientInfo\":{\"name\":\"probe\",\"version\":\"1.0\"}}}'\n"
            "# 2) Weitere Calls MIT der Session-ID (z.B. tools/list):\n"
            "curl -X POST " + url + " -H \"Authorization: Bearer " + bearer + "\" "
            "-H \"Mcp-Session-Id: <ID aus Schritt 1>\" "
            "-H \"Content-Type: application/json\" "
            "-H \"Accept: application/json, text/event-stream\" "
            "-d '{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\"}'"
        ),
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
    try:
        base_url = normalize_base_url(relay)
    except ValueError as exc:
        return {
            "ok": False,
            "http_status": 400,
            "node_name": name,
            "error": f"{exc} — submitted relay value: {relay!r}",
        }
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
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_esc(_PAGE_TITLE)}</title>"
        f"<style>{_STYLE}</style></head><body>{body}</body></html>"
    )
    return doc.encode("utf-8")


def node_version() -> str:
    """The installed node version (metadata; falls back to 'dev')."""
    try:
        from importlib.metadata import version

        return version("iowap-node")
    except Exception:  # noqa: BLE001 — never break the page on metadata
        return "dev"


def _nav() -> str:
    return (
        "<nav class='topnav'><div class='logo'>⟐ node<span>.dashboard</span></div>"
        "<div class='spacer'></div>"
        f"<span class='pill'><span class='dot'></span>v{_esc(node_version())}</span>"
        "</nav>"
    )


def _render_form(error: str | None = None, name: str = "", relay: str = "") -> bytes:
    prefill = relay if relay else _discover_prefill()
    err = f"<div class='flash error'>{_esc(error)}</div>" if error else ""
    body = f"""
{_nav()}
<div class='wrap' style='max-width:640px'>
<h1>{_esc(_PAGE_TITLE)}</h1>
<div class='card'>
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
  <div style="margin-top:1.25rem"><button class="btn btn-primary" type="submit">Register node</button></div>
</form>
</div>
</div>
"""
    return _page(body)


# ---------------------------------------------------------------------------
# Dashboard rendering (T-021)
# ---------------------------------------------------------------------------


def _status_pill(token: dict | None) -> str:
    """Relay-status pill: temporary token = pending approval, else registered."""
    if (token or {}).get("token_type") == "temporary":
        return "<span class='pill warn'><span class='dot'></span>pending approval</span>"
    return "<span class='pill ok'><span class='dot'></span>registered</span>"


def _mcp_section(banner_bearer: str | None = None) -> str:
    """MCP status + control buttons + SPEC §9 snippets (current bearer)."""
    sup = dashboard.build_supervisor()
    running = sup.is_running()
    if running:
        pid = (
            dashboard.mcp_pid_path().read_text().strip()
            if dashboard.mcp_pid_path().exists()
            else "?"
        )
        status = f"<span class='pill ok'><span class='dot'></span>running (pid {_esc(pid)})</span>"
    else:
        status = "<span class='pill'><span class='dot'></span>stopped</span>"

    bearer = banner_bearer if banner_bearer is not None else dashboard.current_bearer()
    if bearer:
        snips = mcp_snippets(bearer)
        snippets = f"""
<dl>
  <dt>Hermes</dt><dd><pre>{_esc(snips['hermes'])}</pre></dd>
  <dt>Claude Desktop / Claude Code</dt><dd><pre>{_esc(snips['claude'])}</pre></dd>
  <dt>Codex (config.toml)</dt><dd><pre>{_esc(snips['codex'])}</pre></dd>
  <dt>Live probe (curl)</dt><dd><pre>{_esc(snips['curl'])}</pre></dd>
</dl>"""
    else:
        snippets = (
            "<p class='note'>Bearer not provisioned yet — generate one to "
            "configure your agents (Hermes, Claude, Codex) against this "
            "node's MCP endpoint.</p>"
        )

    return f"""
<div class='card'>
  <div style='display:flex;align-items:center;gap:1rem;flex-wrap:wrap'>
    {status}
    <form class='inline' method='post' action='/mcp/enable'>
      <button class='btn' type='submit'>Enable</button></form>
    <form class='inline' method='post' action='/mcp/disable'>
      <button class='btn' type='submit'>Disable</button></form>
    <form class='inline' method='post' action='/mcp/bearer/rotate'>
      <button class='btn btn-primary' type='submit'>Generate new Bearer</button></form>
  </div>
  <p class='note'>The MCP endpoint serves tools over HTTP on port
  <code>{_esc(dashboard.mcp_port())}</code> (loopback-mapped to the host).
  The bearer is stored in <code>~/.relay/mcp_bearer</code>; generating a
  new one restarts the MCP process so it serves the new token immediately.</p>
  {snippets}
</div>"""


def _agent_rows(agents: list[dict[str, Any]]) -> str:
    """One table row per known agent token (T-035: id, created, used, caps)."""
    if not agents:
        return (
            "<tr><td colspan='5' class='note'>No agent tokens yet — every "
            "MCP client currently uses the primary bearer above; create one "
            "per agent to scope it to its own tasks.</td></tr>"
        )
    rows = []
    for entry in agents:
        agent_id = str(entry.get("id", "?"))
        name = str(entry.get("name", "?"))
        created = str(entry.get("created_at") or "—").split("T")[0]
        used = str(entry.get("last_used") or "never").split(".")[0]
        revoked = bool(entry.get("revoked"))
        caps = bool(entry.get("caps_manage"))
        rev_note = " <span class='note'>(revoked)</span>" if revoked else ""
        caps_html = (
            "<span class='badge ok'>manage</span>"
            if caps
            else "<span class='badge muted'>restricted</span>"
        )
        state_html = (
            "<span class='badge muted'>revoked</span>"
            if revoked
            else "<span class='badge ok'>active</span>"
        )
        toggle_action = "0" if caps else "1"
        toggle_label = "Revoke manage-right" if caps else "Allow caps-manage"
        disabled = " disabled" if revoked else ""
        rows.append(f"""
<tr>
  <td>{_esc(name)}{rev_note}</td>
  <td class='mono'>{_esc(agent_id)}</td>
  <td class='note'>{_esc(created)}</td>
  <td class='note'>{_esc(used)}</td>
  <td>{state_html} {caps_html}</td>
  <td>
    <form class='inline' method='post' action='/agents/caps-toggle'{disabled}>
      <input type='hidden' name='agent_id' value='{_esc(agent_id)}'>
      <input type='hidden' name='enable' value='{toggle_action}'>
      <button class='btn' type='submit'{disabled}>{toggle_label}</button></form>
    <form class='inline' method='post' action='/agents/revoke'{disabled}>
      <input type='hidden' name='agent_id' value='{_esc(agent_id)}'>
      <button class='btn btn-danger' type='submit'{disabled}>Revoke</button></form>
  </td>
</tr>"""
        )
    return "".join(rows)


def _agents_section(agent_token: str | None = None) -> str:
    """Agent-token management (T-035 P1c): list, show-once create, revoke, toggle.

    The raw token is rendered EXACTLY ONCE — the caller passes it right
    after ``create``; every later render shows only the id list. The
    PRIMARY bearer is never displayed here (T-016e rotate flow keeps that
    job).
    """
    try:
        agents = agent_tokens.load_agents()
    except Exception as exc:  # noqa: BLE001 — a broken store must not kill the page
        logger.warning("agent token section: store unreadable (%s)", exc)
        agents = []

    show_once = ""
    if agent_token is not None:
        show_once = f"""
  <div class='flash ok mono' style='word-break:break-all;font-size:.9rem'>
    {_esc(agent_token)}
  </div>
  <p class='note'>Copy it now — this token is <strong>shown only this
  once</strong>; the store keeps only its SHA-256 hash. Give it to exactly
  one agent (its MCP header <code>Authorization: Bearer &lt;token&gt;</code>
  replaces the shared primary bearer in that agent's URL config).</p>"""

    return f"""
<div class='card'>
  {show_once}
  <div style='overflow-x:auto'>
  <table style='width:100%;border-collapse:collapse'>
    <thead><tr>
      <th style='text-align:left;padding:.4rem'>Agent</th>
      <th style='text-align:left;padding:.4rem'>ID</th>
      <th style='text-align:left;padding:.4rem'>Created</th>
      <th style='text-align:left;padding:.4rem'>Last used</th>
      <th style='text-align:left;padding:.4rem'>State</th>
      <th style='text-align:left;padding:.4rem'>Actions</th>
    </tr></thead>
    <tbody>{_agent_rows(agents)}</tbody>
  </table>
  </div>
  <form method='post' action='/agents/create' style='margin-top:1rem'>
    <input type='text' name='name' placeholder='agent name (e.g. hermes-laptop)'
           required style='padding:.5rem .7rem;border-radius:.5rem;
           border:1px solid var(--border);background:var(--panel2);color:var(--text)'>
    <button class='btn btn-primary' type='submit'>Create token</button>
  </form>
  <p class='note'>Tokens live on the dedicated <code>/data/agents</code>
  volume (hashes only, 0600). Agents see their submitted tasks only
  (primary bearer = full access, unchanged); <em>Allow caps-manage</em>
  additionally grants an agent the capability_* MCP tools.</p>
</div>"""


def _capability_rows(providers: list[dict[str, Any]], bridge_cfg: dict[str, Any] | None) -> str:
    if not providers:
        return (
            "<tr><td colspan='6' class='note'>No capabilities defined yet — "
            "add one below (the script lives on this host, the capability "
            "definition goes here).</td></tr>"
        )
    rows = []
    for entry in providers:
        name = str(entry.get("name", "?"))
        ctype = str(entry.get("type", "?"))
        summary = str(entry.get("summary") or "")
        published = dashboard.is_published(name, bridge_cfg)
        badge = (
            "<span class='badge ok'>published</span>"
            if published
            else "<span class='badge muted'>unpublished</span>"
        )
        # T-045: the per-row instant toggles are replaced by the batch
        # checkbox UX — the checkbox is the DESIRED state, the badge the
        # CURRENT (actual) state; posting the form diffs the two.
        checkbox = (
            f"<input type='checkbox' name='caps' value='{_esc(name)}'"
            " form='caps-publish-form'"
            f"{' checked' if published else ''}>"
        )
        # T-030: scheduler capacity (defaults 1× / 1 pt)
        limits = (
            f"{_esc(entry.get('max_parallel', 1))}&times; / "
            f"{_esc(entry.get('cost', 1))} pt"
        )
        rows.append(f"""
<tr>
  <td class='mono'>{_esc(name)}</td>
  <td><span class='badge accent'>{_esc(ctype)}</span></td>
  <td class='note'>{_esc(summary)}</td>
  <td class='note'>{limits}</td>
  <td>{badge} {checkbox}</td>
  <td>
    <form class='inline' method='get' action='/caps/edit'>
      <input type='hidden' name='name' value='{_esc(name)}'>
      <button class='btn' type='submit'>Edit</button></form>
    <form class='inline' method='post' action='/caps/delete'
          onsubmit="return confirm('Delete capability {_esc(name)}?');">
      <input type='hidden' name='name' value='{_esc(name)}'>
      <button class='btn btn-danger' type='submit'>Delete</button></form>
  </td>
</tr>""")
    return "".join(rows)


def _capability_form(entry: dict[str, Any] | None) -> str:
    """The add/edit capability form card (T-036: edit mode prefills the entry).

    ``entry is None`` renders the plain create form (action
    ``/caps/create``) — no behavior change for create. An entry renders
    the SAME form prefilled: name readonly (the immutable routing key,
    also carried by the hidden ``__name__``), every field per the
    ``_validate_provider_fields`` contract (version included) and action
    ``POST /caps/update``. Unchecked checkboxes simply send nothing, so
    the prefill is fully round-trip safe.
    """
    edit = entry is not None
    vals = entry or {}
    exec_block = vals.get("exec")
    if not isinstance(exec_block, dict):
        exec_block = {}
    name = str(vals.get("name") or "")
    heading = f"Edit capability {_esc(name)}" if edit else "Add capability"
    action = "/caps/update" if edit else "/caps/create"
    button = "Update capability" if edit else "Create capability"
    name_field = (
        f"""<div><label>Name (immutable)</label>
      <input type='hidden' name='__name__' value='{_esc(name)}'>
      <input name='name' id='cap_name' required pattern='[a-z][a-z0-9_.-]{{1,63}}'
             readonly value='{_esc(name)}'></div>"""
        if edit
        else """<div><label>Name</label>
      <input name='name' id='cap_name' required pattern='[a-z][a-z0-9_.-]{{1,63}}'
             placeholder='script.cleanup'></div>"""
    )
    sel_proc = " selected" if vals.get("type") == "process" else ""
    sel_http = " selected" if vals.get("type") == "http" else ""
    v_summary = _esc(vals.get("summary") or "")
    v_version = _esc(vals.get("version") or "")
    checked_long_run = " checked" if vals.get("long_run") else ""
    v_command = (
        _esc(json.dumps(vals["command"]))
        if edit and isinstance(vals.get("command"), list)
        else ""
    )
    v_glob = _esc(vals.get("artifacts_glob") or "")
    v_health = _esc(vals.get("health_url") or "")
    current_method = str(exec_block.get("method") or "").upper()
    method_options = "".join(
        f"<option{' selected' if m == current_method else ''}>{m}</option>"
        for m in ("POST", "GET", "PUT", "PATCH", "DELETE")
    )
    v_url = _esc(exec_block.get("url_template") or "")
    v_body = _esc(exec_block.get("body_template") or "")
    v_result = _esc(exec_block.get("result_path") or "")
    v_paths = _esc(",".join(exec_block.get("artifact_paths") or []))
    v_max_parallel = _esc(vals.get("max_parallel", 1))
    v_cost = _esc(vals.get("cost", 1))
    return f"""
<div class='card'>
<h2 style='margin-top:0'>{heading}</h2>
<form method='post' action='{action}'>
  <div class='fields'>
    {name_field}
    <div><label>Type</label>
      <select name='type'>
        <option value='process'{sel_proc}>process (run a host command)</option>
        <option value='http'{sel_http}>http (call a REST endpoint)</option>
      </select></div>
    <div><label>Summary (optional)</label>
      <input name='summary' value='{v_summary}' placeholder='Run the cleanup script'></div>
    <div><label>Version (optional X.Y.Z, default 1.0.0)</label>
      <input name='version' class='mono' value='{v_version}' placeholder='1.0.0'></div>
  </div>
  <label style='margin-top:1rem'>
    <input type='checkbox' name='long_run' style='width:auto'{checked_long_run}> Long-running task (minutes+)</label>
  <h2>process fields</h2>
  <div class='fields'>
    <div><label>Command (argv template)</label>
      <input name='command' id='cap_command' class='mono' value='{v_command}'
             placeholder="python C:/scripts/cleanup.py --target {{{{target}}}}"></div>
    <div><label>Artifacts glob (workspace-relative, optional)</label>
      <input name='artifacts_glob' class='mono' value='{v_glob}' placeholder='out/*.png'></div>
  </div>
  <p class='note' style='margin-top:.5rem'>
    <input type='file' id='cap_script' accept='{_esc(",".join(dashboard.SCRIPT_EXTENSIONS))}'
           style='display:none'>
    <button class='btn' type='button'
            onclick="document.getElementById('cap_script').click()">Script w&auml;hlen&hellip;</button>
    <span id='cap_script_status' class='note'></span><br>
    Datei-Upload nach <code>&lt;workspace&gt;/bridge/scripts/</code> (max.
    {_esc(dashboard.MAX_SCRIPT_BYTES // (1024 * 1024))} MB; .bat .cmd .ps1 .py .sh .exe).
    Name + Command werden automatisch vorbelegt und bleiben editierbar —
    der Daemon f&uuml;hrt Prozesse mit cwd = Workspace aus, relative
    <code>scripts/...</code>-Pfade funktionieren.
  </p>
  <script>
  (function () {{
    var input = document.getElementById('cap_script');
    if (!input) return;
    input.addEventListener('change', function () {{
      var file = input.files && input.files[0];
      var status = document.getElementById('cap_script_status');
      if (!file) return;
      status.textContent = 'Lade ' + file.name + ' hoch\\u2026';
      var reader = new FileReader();
      reader.onload = function () {{
        var body = new FormData();
        body.append('file', file);
        fetch('/caps/script-upload', {{ method: 'POST', body: body }})
          .then(function (r) {{ return r.json(); }})
          .then(function (data) {{
            if (!data.ok) {{ status.textContent = 'Fehler: ' + data.error; return; }}
            var nameInput = document.getElementById('cap_name');
            if (nameInput && !nameInput.readOnly) nameInput.value = data.name;
            document.getElementById('cap_command').value = JSON.stringify(data.command);
            status.textContent = 'Gespeichert als ' + data.stored;
          }})
          .catch(function (exc) {{ status.textContent = 'Upload fehlgeschlagen: ' + exc; }});
      }};
      reader.readAsArrayBuffer(file);
    }});
  }})();
  </script>
  <h2>http fields</h2>
  <div class='fields'>
    <div><label>Health URL (optional)</label>
      <input name='health_url' class='mono' value='{v_health}' placeholder='http://127.0.0.1:9000/health'></div>
    <div><label>Method</label>
      <select name='method'>{method_options}</select></div>
    <div><label>URL template</label>
      <input name='url_template' class='mono' value='{v_url}'
             placeholder='http://127.0.0.1:9000/api/{{{{task_id}}}}/render'></div>
    <div><label>Body template (optional)</label>
      <input name='body_template' class='mono' value='{v_body}' placeholder='{{{{"file": "{{{{file}}}}"}}}}'></div>
    <div><label>Result path (optional dot path)</label>
      <input name='result_path' class='mono' value='{v_result}' placeholder='result'></div>
    <div><label>Artifact paths (comma-separated dot paths)</label>
      <input name='artifact_paths' class='mono' value='{v_paths}' placeholder='result.output_path'></div>
  </div>
  <h2>scheduler</h2>
  <div class='fields'>
    <div><label>Max parallel (default 1)</label>
      <input name='max_parallel' type='number' min='1' value='{v_max_parallel}'></div>
    <div><label>Cost — admission points (default 1)</label>
      <input name='cost' type='number' min='0' value='{v_cost}'></div>
  </div>
  <div style='margin-top:1.25rem'>
    <button class='btn btn-primary' type='submit'>{button}</button>
  </div>
</form>
</div>"""


def _capabilities_section(
    bridge_cfg: dict[str, Any] | None, edit_entry: dict[str, Any] | None = None
) -> str:
    providers = dashboard.load_providers()
    auto = bool(bridge_cfg and bridge_cfg.get("auto_publish"))
    auto_badge = (
        "<span class='badge ok'>on</span>" if auto else "<span class='badge muted'>off</span>"
    )
    auto_form = f"""
<form class='inline' method='post' action='/caps/auto-publish'>
  <input type='hidden' name='enable' value='{"0" if auto else "1"}'>
  <button class='btn' type='submit'>{"Turn off" if auto else "Turn on"}</button>
</form>"""
    return f"""
<div class='card'>
<table>
  <thead><tr><th>Capability</th><th>Type</th><th>Summary</th><th>Parallel / Cost</th>
  <th>Status</th><th>Actions</th></tr></thead>
  <tbody>{_capability_rows(providers, bridge_cfg)}</tbody>
</table>
<p class='note'>Definitions live in
<code>{_esc(dashboard.providers_path())}</code> (the bridge daemon picks
them up automatically). T-045: tick the checkboxes for the
capabilities that should be published and press <em>Update
Capabilities</em> — the checkmarks are the desired set, the badge shows
the current allowlist state; the save diffs both and reloads the node
(recomposed active profile). <em>Parallel / Cost</em> feed the relay
scheduler (max parallel claims / admission points per claim).
<em>Edit</em> reuses this form prefilled; every save bumps the entry
version (patch level) unless an explicit different X.Y.Z is set.</p>
<form id='caps-publish-form' method='post' action='/caps/update-publish'
      style='margin-top:1rem'>
  <button class='btn btn-primary' type='submit'>Update Capabilities</button>
</form>
<p style='margin-top:1rem'>Auto-publish (all capabilities while the
allowlist is empty): {auto_badge} {auto_form}</p>
</div>
{_capability_form(edit_entry)}"""


def _forward_section(panel: dict[str, Any]) -> str:
    """The Forward-Slots section (T-030) — read-only for non-admin tokens.

    Slots themselves are declared in node.yaml (``forward_slots``, T-053)
    and transported by the heartbeat; this panel maintains only the
    (node, slot) → target mappings via the relay's forward-mappings API.
    A 403 (runtime token without admin role) hides the section behind a
    quiet note instead of showing a dead editor.
    """
    if panel.get("forbidden"):
        return """
<div class='card'>
<p class='note'>Forward-Slots: nur f&uuml;r Admin-Nodes konfigurierbar —
dieser Node-Token hat keine Admin-Rolle. Slots und Mappings werden im
Relay-Dashboard gepflegt.</p>
</div>"""
    if not panel.get("ok"):
        err = panel.get("error") or "unbekannter Fehler"
        return f"""
<div class='card'>
<p class='note'>Forward-Slots nicht abrufbar: {_esc(err)}</p>
</div>"""

    mappings = panel.get("mappings") or {}
    rows = []
    for slot in panel.get("slots") or []:
        if not isinstance(slot, dict):
            continue
        sid = str(slot.get("slot", "?"))
        label = str(slot.get("label") or "")
        description = str(slot.get("description") or "")
        slot_type = str(slot.get("slot_type") or "forward")
        mapping = mappings.get(sid) or {}
        target = str(mapping.get("target_capability") or "")
        pin = str(mapping.get("target_node_id") or "")
        pin_note = f" <span class='badge accent'>pin: {_esc(pin)}</span>" if pin else ""
        rows.append(f"""
<tr>
  <td class='mono'>{_esc(sid)}</td>
  <td>{_esc(label)}<div class='note'>{_esc(description)}</div></td>
  <td><span class='badge'>{_esc(slot_type)}</span></td>
  <td>
    <form class='inline' method='post' action='/forward/save'
          style='display:flex;gap:.4rem;flex-wrap:wrap'>
      <input type='hidden' name='slot' value='{_esc(sid)}'>
      <input name='target_capability' class='mono' value='{_esc(target)}'
             placeholder='target_capability' required style='min-width:14rem'>
      <button class='btn btn-primary' type='submit'>Speichern</button>
    </form>
    <form class='inline' method='post' action='/forward/delete'>
      <input type='hidden' name='slot' value='{_esc(sid)}'>
      <button class='btn btn-danger' type='submit'>Mapping l&ouml;schen</button>
    </form>
  </td>
  <td>{pin_note}</td>
</tr>""")
    if not rows:
        rows.append(
            "<tr><td colspan='5' class='note'>Keine Forward-Slots deklariert — "
            "Slots werden in node.yaml (<code>forward_slots</code>) angelegt "
            "und per Heartbeat &uuml;bertragen.</td></tr>"
        )
    return f"""
<div class='card'>
<table>
  <thead><tr><th>Slot</th><th>Label / Beschreibung</th><th>Typ</th>
  <th>Ziel (target_capability)</th><th>Pin</th></tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
<p class='note'>Nodes mit Admin-Rolle d&uuml;rfen die
<code>(node, slot) &rarr; target_capability</code>-Mappings direkt hier
pflegen (PUT/DELETE <code>/relay/v2/forward-mappings/&#123;node&#125;/&#123;slot&#125;</code>).
Slots selbst werden NICHT hier angelegt — sie kommen aus der
node.yaml (<code>forward_slots</code>, T-053).</p>
</div>"""


def _render_dashboard(
    *,
    banner: str | None = None,
    error: str | None = None,
    bearer_override: str | None = None,
    agent_token: str | None = None,
    edit_entry: dict[str, Any] | None = None,
) -> bytes:
    """The full node dashboard (configured state) — relay-design family."""
    try:
        meta = node_utils.load_meta()
    except FileNotFoundError:
        meta = {}
    cfg = node_utils.load_config()
    node_id = meta.get("node_id") or "unknown"
    node_name = meta.get("node_name") or "(unnamed)"
    base_url = (
        os.environ.get("RELAY_BASE_URL")
        or cfg.get("base_url")
        or meta.get("base_url")
        or "(unknown)"
    )
    token = node_utils.load_token() or {}
    bridge_cfg = bridge.load_bridge_config()
    if bridge_cfg:
        bridge_html = (
            "<span class='pill ok'><span class='dot'></span>configured</span>"
            f"<div class='mono' style='word-break:break-all;margin-top:.35rem;font-size:.85rem'>"
            f"{_esc(bridge_cfg['url'])}</div>"
        )
    else:
        bridge_html = "<span class='pill warn'><span class='dot'></span>not configured</span>"
    banner_html = f"<div class='flash ok'>{_esc(banner)}</div>" if banner else ""
    error_html = f"<div class='flash error'>{_esc(error)}</div>" if error else ""
    body = f"""
{_nav()}
<div class='wrap'>
<h1>Node dashboard</h1>
{banner_html}{error_html}
<h2>Overview</h2>
<div class='grid'>
  <div class='kv'><div class='k'>Node</div><div class='v'>{_esc(node_name)}</div></div>
  <div class='kv'><div class='k'>Node ID</div><div class='v mono'>{_esc(node_id)}</div></div>
  <div class='kv'><div class='k'>Relay URL</div><div class='v mono'>{_esc(base_url)}</div></div>
  <div class='kv'><div class='k'>Relay status</div><div>{_status_pill(token)}</div></div>
  <div class='kv'><div class='k'>Bridge</div><div>{bridge_html}</div></div>
  <div class='kv'><div class='k'>Version</div><div class='v mono'>{_esc(node_version())}</div></div>
</div>
<h2>Capabilities</h2>
{_capabilities_section(bridge_cfg, edit_entry)}
<h2>Forward-Slots</h2>
{_forward_section(dashboard.fetch_forward_panel())}
<h2>MCP endpoint</h2>
{_mcp_section(bearer_override)}
<h2>Agent-Tokens</h2>
{_agents_section(agent_token)}
<p class='note'>If the relay shows the node as <em>pending</em>, an admin
must approve it first. The daemon picks config changes up automatically
(SIGHUP / next poll).</p>
</div>
"""
    return _page(body)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

# Dashboard POST routes (configured state only — blank nodes get a 409).
# T-045: the per-row instant toggles /caps/publish + /caps/unpublish are
# REPLACED by the batch commit form /caps/update-publish.
_DASHBOARD_POST_ROUTES = (
    "/caps/create",
    "/caps/update",
    "/caps/delete",
    "/caps/update-publish",
    "/caps/auto-publish",
    "/caps/script-upload",
    "/forward/save",
    "/forward/delete",
    "/mcp/enable",
    "/mcp/disable",
    "/mcp/bearer/rotate",
    # T-035 P1c: agent-token management on the /data/agents store.
    "/agents/create",
    "/agents/revoke",
    "/agents/caps-toggle",
)

# T-030: the script-upload body may carry up to MAX_SCRIPT_BYTES of file
# content plus multipart overhead — larger than the plain form cap.
_MAX_UPLOAD_BODY_BYTES = dashboard.MAX_SCRIPT_BYTES + 256 * 1024


def _parse_multipart_file(content_type: str, body: bytes) -> tuple[str, bytes] | None:
    """Extract the first file part ``(filename, content)`` from a multipart body.

    Minimal stdlib parser (the dashboard has no framework): finds the
    boundary, walks the parts, returns the first part carrying a
    ``filename`` in its Content-Disposition. ``None`` on any mismatch.
    """
    match = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not match:
        return None
    boundary = ("--" + match.group(1)).encode("utf-8", errors="replace")
    for part in body.split(boundary):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        head, sep, content = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        disposition = head.decode("utf-8", errors="replace")
        if "filename=" not in disposition.lower():
            continue
        fn = re.search(r'filename="([^"]*)"', disposition, re.IGNORECASE)
        if fn is None:
            continue
        # one trailing CRLF belongs to the part framing, not the content
        if content.endswith(b"\r\n"):
            content = content[:-2]
        return fn.group(1), content
    return None


def _banner_with_recompile(banner: str) -> str:
    """Append the T-045 post-mutation recompile note to a success banner.

    The store functions recomposed the active profile right before this
    render; a failing recompose must surface in the Flash (the entry
    write itself succeeded — node.yaml stays byte-identical in that
    case). An empty note (recompile skipped, e.g. no node.yaml yet)
    adds nothing instead of a synthetic filler.
    """
    from nodes.common import active_profile as _ap

    note = _ap.last_recompile_note()
    if not note:
        return banner
    if note.startswith("recompile failed"):
        return f"{banner} — {note}"
    return f"{banner} ({note})"


def _handle_dashboard_post(path: str, form: dict[str, list[str]]) -> tuple[int, bytes]:
    """Dispatch one dashboard POST → (http_status, page).

    Never raises: store/supervisor failures surface as a 400 with the
    error rendered in the dashboard.
    """
    try:
        if path == "/caps/create":
            fields = {k: (v[0] if v else "") for k, v in form.items()}
            ok, msg = dashboard.create_provider(fields)
            if ok:
                return 200, _render_dashboard(
                    banner=_banner_with_recompile(f"Capability '{msg}' created")
                )
            return 400, _render_dashboard(error=msg)
        if path == "/caps/update":
            # T-036: name immutable — the hidden __name__ carries the
            # original routing key; a mismatched form name is a reject.
            fields = {k: (v[0] if v else "") for k, v in form.items()}
            target = (
                (form.get("__name__") or [""])[0].strip() or fields.get("name", "")
            )
            ok, msg = dashboard.update_provider(target, fields)
            if ok:
                return 200, _render_dashboard(
                    banner=_banner_with_recompile(msg)
                )
            return 400, _render_dashboard(error=msg)
        if path == "/caps/update-publish":
            # T-045: batch commit — the posted (checked) names are the
            # FULL desired published set; the server diffs them against
            # the live allowlist view, applies the delta per cap WITHOUT
            # a recompile each, recomposes the active profile ONCE at
            # the end and reports the counter flash.
            from nodes.common import active_profile as _ap
            from nodes.common import bridge as _bridge

            checked: list[str] = []
            for raw_value in form.get("caps", []):
                candidate = raw_value.strip()
                if candidate and candidate not in checked:
                    checked.append(candidate)
            providers = dashboard.load_providers()
            store_names = [
                str(p.get("name"))
                for p in providers
                if isinstance(p, dict) and isinstance(p.get("name"), str)
            ]
            bridge_cfg = _bridge.load_bridge_config()
            currently = {
                n for n in store_names if dashboard.is_published(n, bridge_cfg)
            }
            wanted = {n for n in checked if n in store_names}
            to_publish = sorted(wanted - currently)
            to_unpublish = sorted(currently - wanted)
            unchanged = len(currently & wanted)
            failures: list[str] = []
            for cap_name in to_publish:
                ok, msg = dashboard.set_capability_published(cap_name, True, recompile=False)
                if not ok:
                    failures.append(f"{cap_name}: {msg}")
            for cap_name in to_unpublish:
                ok, msg = dashboard.set_capability_published(cap_name, False, recompile=False)
                if not ok:
                    failures.append(f"{cap_name}: {msg}")
            # EINMAL recompile für den ganzen Batch (nicht je Cap)
            ok, recomp_msg = _ap.recompile_active_profile()
            counter = (
                f"published: {len(to_publish)}, unpublished: {len(to_unpublish)}, "
                f"unchanged: {unchanged}"
            )
            if failures or not ok:
                detail = "; ".join(failures) if failures else recomp_msg
                return 400, _render_dashboard(
                    error=f"Update Capabilities failed — {counter}: {detail}"
                )
            banner = f"Update Capabilities saved — {counter} ({recomp_msg})"
            return 200, _render_dashboard(banner=banner)
        name = (form.get("name") or [""])[0].strip()
        if path == "/caps/delete":
            ok, msg = dashboard.delete_provider(name)
            if ok:
                return 200, _render_dashboard(
                    banner=_banner_with_recompile(f"Capability '{msg}' deleted")
                )
            return 400, _render_dashboard(error=msg)
        elif path == "/caps/auto-publish":
            enabled = (form.get("enable") or [""])[0] == "1"
            ok, _ = dashboard.set_auto_publish(enabled)
            if ok:
                return 200, _render_dashboard(
                    banner=f"Auto-publish {'enabled' if enabled else 'disabled'} (reload triggered)"
                )
        elif path == "/forward/save":
            slot = (form.get("slot") or [""])[0].strip()
            target = (form.get("target_capability") or [""])[0].strip()
            pin = (form.get("target_node_id") or [""])[0].strip()
            if not slot or not target:
                return 400, _render_dashboard(
                    error="forward mapping: slot and target_capability are required"
                )
            try:
                meta = node_utils.load_meta()
                node_id = meta.get("node_id") or ""
                client = dashboard.build_forward_client()
            except Exception as exc:  # noqa: BLE001
                return 400, _render_dashboard(error=f"forward client: {exc}")
            ok, msg = dashboard.save_forward_mapping(
                client, node_id, slot, target, pin or None
            )
            if ok:
                return 200, _render_dashboard(banner=msg)
            return 400, _render_dashboard(error=msg)
        elif path == "/forward/delete":
            slot = (form.get("slot") or [""])[0].strip()
            if not slot:
                return 400, _render_dashboard(error="forward delete: slot is required")
            try:
                meta = node_utils.load_meta()
                node_id = meta.get("node_id") or ""
                client = dashboard.build_forward_client()
            except Exception as exc:  # noqa: BLE001
                return 400, _render_dashboard(error=f"forward client: {exc}")
            ok, msg = dashboard.delete_forward_mapping(client, node_id, slot)
            if ok:
                return 200, _render_dashboard(banner=msg)
            return 400, _render_dashboard(error=msg)
        elif path == "/mcp/enable":
            sup = dashboard.build_supervisor()
            # T-016d: enabling with no bearer at all would spawn a child that
            # exits on its fail-closed guard — provision one first.
            if not dashboard.current_bearer():
                dashboard.write_bearer(dashboard.generate_bearer())
                logger.info("mcp enable: provisioned initial bearer file")
            ok, msg = sup.start()
            if ok:
                return 200, _render_dashboard(banner=msg)
            return 500, _render_dashboard(error=msg)
        elif path == "/mcp/disable":
            ok, msg = dashboard.build_supervisor().stop()
            if ok:
                return 200, _render_dashboard(banner=msg)
            return 500, _render_dashboard(error=msg)
        elif path == "/mcp/bearer/rotate":
            bearer, restarted = dashboard.rotate_bearer(dashboard.build_supervisor())
            note = " — MCP process restarted with the new token" if restarted else ""
            return 200, _render_dashboard(
                banner=f"New bearer generated{note}", bearer_override=bearer
            )
        elif path == "/agents/create":
            # T-035 P1c: show-once — the raw token rides THIS render only.
            name = (form.get("name") or [""])[0].strip()
            if not name:
                return 400, _render_dashboard(error="agent name is required")
            agent_id, raw_token = agent_tokens.create(name)
            logger.info("agent token created: id=%s (never logged)", agent_id)
            return 200, _render_dashboard(
                banner=(f"Agent token '{name}' created ({agent_id}) — "
                        "copy the token below, it is shown only this once"),
                agent_token=raw_token,
            )
        elif path == "/agents/revoke":
            agent_id = (form.get("agent_id") or [""])[0].strip()
            entry = agent_tokens.revoke(agent_id) if agent_id else None
            if entry is not None:
                return 200, _render_dashboard(
                    banner=f"Agent '{entry.get('name', agent_id)}' revoked — "
                           "its bearer stops working immediately"
                )
            return 400, _render_dashboard(error=f"agent {agent_id!r} unknown")
        elif path == "/agents/caps-toggle":
            agent_id = (form.get("agent_id") or [""])[0].strip()
            enable = (form.get("enable") or [""])[0] == "1"
            entry = agent_tokens.set_caps_manage(agent_id, enable) if agent_id else None
            if entry is not None:
                verb = "granted" if enable else "removed"
                return 200, _render_dashboard(
                    banner=f"caps-manage {verb} for agent "
                           f"'{entry.get('name', agent_id)}'"
                )
            return 400, _render_dashboard(error=f"agent {agent_id!r} unknown")
    except Exception as exc:  # noqa: BLE001 — the page must never 500 raw
        logger.exception("dashboard POST %s failed", path)
        return 400, _render_dashboard(error=f"{type(exc).__name__}: {exc}")
    return 400, _render_dashboard(error="operation failed")


class OnboardingHandler(BaseHTTPRequestHandler):
    """Serves the onboarding page / dashboard; POST routes are state-aware."""

    server_version = "iowap-node-ui/2.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — stdlib name
        logger.debug("onboarding: " + format, *args)

    def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_form(self) -> dict[str, list[str]] | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > _MAX_BODY_BYTES:
            self._send(413, b"form body too large\n", "text/plain; charset=utf-8")
            return None
        if length <= 0:
            # empty body (e.g. a button-only dashboard POST) → empty form
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return parse_qs(raw, keep_blank_values=True)

    def do_GET(self) -> None:  # noqa: N802 — stdlib naming
        path, _, query = self.path.partition("?")
        if path == "/caps/edit":
            # T-036: the capability Edit button — render the create form
            # prefilled for one existing entry (name readonly).
            if node_utils.relay_config_ready() is None:
                self._send(404, b"not found\n", "text/plain; charset=utf-8")
                return
            name = (parse_qs(query).get("name") or [""])[0]
            entry = dashboard.find_provider(name)
            if entry is None:
                self._send(404, _render_dashboard(error=f"capability {name!r} not found"))
                return
            self._send(200, _render_dashboard(edit_entry=entry))
            return
        if path != "/":
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
            return
        if node_utils.relay_config_ready() is not None:
            self._send(200, _render_dashboard())
        else:
            self._send(200, _render_form())

    def do_POST(self) -> None:  # noqa: N802 — stdlib naming
        path = self.path.split("?", 1)[0]
        if path == "/configure":
            form = self._read_form()
            if form is None:
                return
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
                    _render_dashboard(
                        banner=banner,
                        bearer_override=os.environ.get("IOWAP_MCP_BEARER") or None,
                    ),
                )
                return
            if result.get("configured"):
                # after configuration the form is gone for good — dashboard only
                self._send(result["http_status"], _render_dashboard(banner=result["error"]))
                return
            if result["http_status"] == 400:
                # validation — re-show the form with the user's input preserved
                self._send(400, _render_form(error=result["error"], name=name, relay=relay))
                return
            self._send(result["http_status"], _render_form(error=result["error"], name=name, relay=relay))
            return
        if path in _DASHBOARD_POST_ROUTES:
            if node_utils.relay_config_ready() is None:
                self._send(
                    409,
                    "node is not configured yet — register it first "
                    "(POST /configure)\n".encode(),
                    "text/plain; charset=utf-8",
                )
                return
            if path == "/caps/script-upload":
                # T-030: multipart upload — raw body, JSON response
                # (the browser fetch() fills name/command from it).
                self._handle_script_upload()
                return
            form = self._read_form()
            if form is None:
                return
            status, body = _handle_dashboard_post(path, form)
            self._send(status, body)
            return
        self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def _handle_script_upload(self) -> None:
        """POST /caps/script-upload — multipart file → <workspace>/bridge/scripts/.

        JSON response (fetched from the capability form): on success
        ``{"ok", "name", "command", "stored"}`` — the JS prefills the
        capability name and the command argv (both stay editable).
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._send(400, json.dumps({"ok": False, "error": "empty body"}).encode(),
                       "application/json")
            return
        if length > _MAX_UPLOAD_BODY_BYTES:
            self._send(413, json.dumps({"ok": False, "error": "body too large"}).encode(),
                       "application/json")
            return
        body = self.rfile.read(length)
        part = _parse_multipart_file(self.headers.get("Content-Type", ""), body)
        if part is None:
            self._send(400, json.dumps(
                {"ok": False, "error": "no multipart file part found"}).encode(),
                "application/json")
            return
        filename, content = part
        ok, message, stored = dashboard.save_script(filename, content)
        if not ok:
            self._send(400, json.dumps({"ok": False, "error": message}).encode(),
                       "application/json")
            return
        self._send(
            200,
            json.dumps({
                "ok": True,
                "stored": stored,
                "name": dashboard.slugify_capability_name(stored),
                "command": dashboard.script_command_argv(stored),
            }).encode(),
            "application/json",
        )


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

# The running UI server (set by start_dashboard / wait_for_relay_config) so
# the daemon's idle→run transition can keep the SAME server alive: GET /
# renders state-based, no restart needed. Tests use it to shut the server
# down.
_active_server: ThreadingHTTPServer | None = None


def start_dashboard(
    *, host: str = "0.0.0.0", port: int | None = None
) -> ThreadingHTTPServer | None:
    """Start the node UI (form while blank, dashboard once configured).

    Runs for the whole daemon lifetime — T-021 turned the blank-start-only
    onboarding server into the permanent node dashboard on port 7783.
    Never raises; when the port is already bound (e.g. the idle loop
    started the server before the config appeared) it returns ``None``
    and the existing server keeps serving. Idempotent via
    :data:`_active_server`.
    """
    global _active_server
    if _active_server is not None and port is None:
        return _active_server
    try:
        bind_port = onboarding_port() if port is None else port
        server = ThreadingHTTPServer((host, bind_port), OnboardingHandler)
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever,
            name="node-dashboard-ui",
            daemon=True,
            kwargs={"poll_interval": 0.2},
        )
        thread.start()
        _active_server = server
        logger.info(
            "node UI serving on http://%s:%d (onboarding form / dashboard)",
            host,
            server.server_address[1],
        )
        return server
    except Exception as exc:  # noqa: BLE001 — never break the daemon
        logger.warning("node UI could not start: %s", exc)
        return None


def start_if_blank(*, host: str = "0.0.0.0", port: int | None = None) -> ThreadingHTTPServer | None:
    """T-017 compatibility wrapper: start the UI only while the node is blank.

    New callers should use :func:`start_dashboard` — since T-021 the server
    also serves the dashboard for configured nodes.
    """
    try:
        if node_utils.relay_config_ready() is not None:
            return None
    except Exception as exc:  # noqa: BLE001 — never break the daemon
        logger.warning("onboarding state check failed: %s", exc)
        return None
    return start_dashboard(host=host, port=port)


def shutdown(server: ThreadingHTTPServer | None) -> None:
    """Stop a server created by :func:`start_dashboard` (idempotent)."""
    global _active_server
    if server is None:
        return
    if server is _active_server:
        _active_server = None
    try:
        server.shutdown()
        server.server_close()
    except Exception as exc:  # noqa: BLE001 — best-effort teardown
        logger.warning("node UI shutdown failed: %s", exc)
