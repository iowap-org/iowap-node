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
routes (``/caps/*``, ``/mcp/*``) are refused while blank (409).

Config pickup needs no signal: the T-011 idle loop polls every 2 s and
starts the daemon on its own (see ``node_utils.wait_for_relay_config``).

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
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

from nodes.common import bridge, dashboard, node_config, node_utils
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


def _capability_rows(providers: list[dict[str, Any]], bridge_cfg: dict[str, Any] | None) -> str:
    if not providers:
        return (
            "<tr><td colspan='5' class='note'>No capabilities defined yet — "
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
        toggle_action = "unpublish" if published else "publish"
        toggle_label = "Unpublish" if published else "Publish"
        rows.append(f"""
<tr>
  <td class='mono'>{_esc(name)}</td>
  <td><span class='badge accent'>{_esc(ctype)}</span></td>
  <td class='note'>{_esc(summary)}</td>
  <td>{badge}</td>
  <td>
    <form class='inline' method='post' action='/caps/{toggle_action}'>
      <input type='hidden' name='name' value='{_esc(name)}'>
      <button class='btn' type='submit'>{toggle_label}</button></form>
    <form class='inline' method='post' action='/caps/delete'
          onsubmit="return confirm('Delete capability {_esc(name)}?');">
      <input type='hidden' name='name' value='{_esc(name)}'>
      <button class='btn btn-danger' type='submit'>Delete</button></form>
  </td>
</tr>""")
    return "".join(rows)


def _capabilities_section(bridge_cfg: dict[str, Any] | None) -> str:
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
  <thead><tr><th>Capability</th><th>Type</th><th>Summary</th><th>Status</th>
  <th>Actions</th></tr></thead>
  <tbody>{_capability_rows(providers, bridge_cfg)}</tbody>
</table>
<p class='note'>Definitions live in
<code>{_esc(dashboard.providers_path())}</code> (the bridge daemon picks
them up automatically). Publishing adds the capability to
<code>bridge.allowlist</code> in node.yaml and reloads the daemon.</p>
<p style='margin-top:1rem'>Auto-publish (all capabilities while the
allowlist is empty): {auto_badge} {auto_form}</p>
</div>
<div class='card'>
<h2 style='margin-top:0'>Add capability</h2>
<form method='post' action='/caps/create'>
  <div class='fields'>
    <div><label>Name</label>
      <input name='name' required pattern='[a-z][a-z0-9_.-]{{1,63}}'
             placeholder='script.cleanup'></div>
    <div><label>Type</label>
      <select name='type'>
        <option value='process'>process (run a host command)</option>
        <option value='http'>http (call a REST endpoint)</option>
      </select></div>
    <div><label>Summary (optional)</label>
      <input name='summary' placeholder='Run the cleanup script'></div>
  </div>
  <label style='margin-top:1rem'>
    <input type='checkbox' name='long_run' style='width:auto'> Long-running task (minutes+)</label>
  <h2>process fields</h2>
  <div class='fields'>
    <div><label>Command (argv template)</label>
      <input name='command' class='mono'
             placeholder="python C:/scripts/cleanup.py --target {{target}}"></div>
    <div><label>Artifacts glob (workspace-relative, optional)</label>
      <input name='artifacts_glob' class='mono' placeholder='out/*.png'></div>
  </div>
  <h2>http fields</h2>
  <div class='fields'>
    <div><label>Health URL (optional)</label>
      <input name='health_url' class='mono' placeholder='http://127.0.0.1:9000/health'></div>
    <div><label>Method</label>
      <select name='method'>
        <option>POST</option><option>GET</option><option>PUT</option>
        <option>PATCH</option><option>DELETE</option>
      </select></div>
    <div><label>URL template</label>
      <input name='url_template' class='mono'
             placeholder='http://127.0.0.1:9000/api/{{task_id}}/render'></div>
    <div><label>Body template (optional)</label>
      <input name='body_template' class='mono' placeholder='{{"file": "{{file}}"}}'></div>
    <div><label>Result path (optional dot path)</label>
      <input name='result_path' class='mono' placeholder='result'></div>
    <div><label>Artifact paths (comma-separated dot paths)</label>
      <input name='artifact_paths' class='mono' placeholder='result.output_path'></div>
  </div>
  <div style='margin-top:1.25rem'>
    <button class='btn btn-primary' type='submit'>Create capability</button>
  </div>
</form>
</div>"""


def _render_dashboard(
    *,
    banner: str | None = None,
    error: str | None = None,
    bearer_override: str | None = None,
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
{_capabilities_section(bridge_cfg)}
<h2>MCP endpoint</h2>
{_mcp_section(bearer_override)}
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
_DASHBOARD_POST_ROUTES = (
    "/caps/create",
    "/caps/delete",
    "/caps/publish",
    "/caps/unpublish",
    "/caps/auto-publish",
    "/mcp/enable",
    "/mcp/disable",
    "/mcp/bearer/rotate",
)


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
                return 200, _render_dashboard(banner=f"Capability '{msg}' created")
            return 400, _render_dashboard(error=msg)
        name = (form.get("name") or [""])[0].strip()
        if path == "/caps/delete":
            ok, msg = dashboard.delete_provider(name)
            if ok:
                return 200, _render_dashboard(banner=f"Capability '{msg}' deleted")
            return 400, _render_dashboard(error=msg)
        if path in ("/caps/publish", "/caps/unpublish"):
            if not name:
                return 400, _render_dashboard(error="capability name is required")
            ok, _ = dashboard.set_capability_published(name, path == "/caps/publish")
            if ok:
                verb = "published" if path == "/caps/publish" else "unpublished"
                return 200, _render_dashboard(
                    banner=f"Capability '{name}' {verb} (allowlist + reload)"
                )
        elif path == "/caps/auto-publish":
            enabled = (form.get("enable") or [""])[0] == "1"
            ok, _ = dashboard.set_auto_publish(enabled)
            if ok:
                return 200, _render_dashboard(
                    banner=f"Auto-publish {'enabled' if enabled else 'disabled'} (reload triggered)"
                )
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
        path = self.path.split("?", 1)[0]
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
            form = self._read_form()
            if form is None:
                return
            status, body = _handle_dashboard_post(path, form)
            self._send(status, body)
            return
        self._send(404, b"not found\n", "text/plain; charset=utf-8")


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
