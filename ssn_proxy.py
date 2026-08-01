#!/usr/bin/env python3
"""SSN Proxy — HTMX server for capability pages (T-076).

Runs on localhost:8790. Heartbeats its endpoints as Dynamic Routes (T-075)
so the relay proxies requests from the dashboard to this server.

Endpoints:
  POST /api/task-submit   — submit a task to the relay (with SSN node token)
  GET  /api/tasks/{id}    — get task status from the relay
  GET  /api/storage/{id}  — download an artifact from the relay

  GET  /mflux             — mflux capability page (HTMX)
  POST /mflux/generate    — generate an image (submit task, poll, show result)
  GET  /mflux/bilder/{id} — serve a cached image

The SSN node token is read from ~/.relay/<node_id>.json on startup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger("ssn-proxy")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

RELAY_BASE_URL = os.environ.get("RELAY_BASE_URL", "http://127.0.0.1:8788")
SSN_PROXY_PORT = int(os.environ.get("SSN_PROXY_PORT", "8790"))
SSN_PROXY_HOST = os.environ.get("SSN_PROXY_HOST", "127.0.0.1")
CACHE_DIR = Path.home() / ".ssn" / "cache" / "bilder"

# ---------------------------------------------------------------------------
# Token loading
# ---------------------------------------------------------------------------

def _load_token() -> str:
    """Load the SSN node token from the relay token file.

    Since T-088 the token file is a JSON envelope
    ``{"token": "...", "expires_at": "..."}``. Legacy plaintext files
    (pre-T-088) are still tolerated as a fallback.
    """
    meta_path = Path.home() / ".relay" / "ai-relay-agent.json"
    if not meta_path.exists():
        raise RuntimeError(f"SSN meta file not found: {meta_path}")
    meta = json.loads(meta_path.read_text())
    node_id = meta["node_id"]
    token_path = Path.home() / ".relay" / f"{node_id}.token"
    if not token_path.exists():
        # Try the legacy path
        token_path = Path.home() / ".relay" / "ai-relay-agent.token"
    if not token_path.exists():
        raise RuntimeError(f"SSN token file not found for node {node_id}")
    raw = token_path.read_text().strip()
    if raw.lstrip().startswith("{"):
        try:
            return json.loads(raw)["token"]
        except (json.JSONDecodeError, KeyError):
            pass
    return raw


def _get_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_load_token()}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="SSN Proxy", version="1.0.0")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Register known capability pages so the ssn.capability-pages handler
# finds them via its list action. Each page is a marker file under
# ~/.ssn/pages/<capability>.html that the handler reads.
_PAGES_DIR = Path.home() / ".ssn" / "pages"
_PAGES_DIR.mkdir(parents=True, exist_ok=True)

# mflux page marker — the dashboard opens the Dynamic Route URL directly
(_PAGES_DIR / "image.generate.mflux.html").write_text(
    '<html><body>mflux capability page</body></html>'
)


# ---------------------------------------------------------------------------
# Relay proxy endpoints (used by Dynamic Routes)
# ---------------------------------------------------------------------------


@app.post("/api/task-submit")
async def proxy_task_submit(request: Request):
    """Submit a task to the relay using the SSN node token."""
    body = await request.json()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{RELAY_BASE_URL}/relay/v2/scheduler/task-simple",
            json=body,
            headers=_get_headers(),
        )
        return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")


@app.get("/api/tasks/{task_id}")
async def proxy_task_get(task_id: str):
    """Get task status from the relay."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{RELAY_BASE_URL}/relay/v2/scheduler/tasks/{task_id}",
            headers=_get_headers(),
        )
        return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")


@app.get("/api/storage/{artifact_id}")
async def proxy_storage_get(artifact_id: str):
    """Download an artifact from the relay."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{RELAY_BASE_URL}/relay/v2/storage/files/{artifact_id}",
            headers=_get_headers(),
        )
        return Response(content=resp.content, status_code=resp.status_code, media_type=resp.headers.get("content-type", "application/octet-stream"))


# ---------------------------------------------------------------------------
# mflux capability page
# ---------------------------------------------------------------------------


MFLUX_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>mflux Bildgenerierung</title>
<script src="https://unpkg.com/htmx.org@2.0.4"></script>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: system-ui, -apple-system, sans-serif;
    background: #0b0d11; color: #e0e2e8;
    padding: 1.5rem; line-height: 1.5;
  }
  h1 { font-size: 1.3rem; margin-bottom: 1rem; color: #fff; }
  .layout { display: flex; gap: 1.5rem; align-items: flex-start; }
  .form-col { flex: 0 0 340px; min-width: 280px; }
  .preview-col { flex: 1; min-width: 0; }
  .form-group { margin-bottom: 1rem; }
  label { display: block; font-size: .85rem; color: #9aa0b0; margin-bottom: .35rem; }
  textarea, input, select {
    width: 100%; padding: .6rem .8rem;
    background: #1a1d25; border: 1px solid #2a2f3a; border-radius: .4rem;
    color: #e0e2e8; font-size: .9rem; font-family: inherit;
  }
  textarea { min-height: 80px; resize: vertical; }
  textarea:focus, input:focus, select:focus { outline: none; border-color: #7aa2ff; }
  button {
    padding: .6rem 1.5rem; border: none; border-radius: .4rem;
    font-size: .9rem; cursor: pointer; font-weight: 500;
  }
  .btn-primary { background: #7aa2ff; color: #0b0d11; }
  .btn-primary:hover { background: #5c8aff; }
  .btn-primary:disabled { opacity: .5; cursor: not-allowed; }
  #result { margin-top: 1rem; }
  #result img { max-width: 100%; max-height: 70vh; border-radius: .5rem; }
  .status { padding: .75rem; border-radius: .4rem; font-size: .85rem; margin-top: 1rem; }
  .status-info { background: #1a2a3a; border: 1px solid #2a4a6a; }
  .status-ok { background: #1a2a1a; border: 1px solid #2a6a2a; }
  .status-err { background: #2a1a1a; border: 1px solid #6a2a2a; }
  .htmx-indicator { opacity: 0; transition: opacity .3s; }
  .htmx-request .htmx-indicator { opacity: 1; }
  .meta { font-size: .8rem; color: #6a7080; margin-top: .5rem; }
  @media (max-width: 800px) { .layout { flex-direction: column; } .form-col { flex: none; width: 100%; } }
</style>
</head>
<body>

<h1>🎨 mflux Bildgenerierung</h1>
<p style="font-size:.85rem;color:#9aa0b0;margin-bottom:1.25rem;">
  FLUX.2 Klein 4B q8 · 4 Steps · 16GB M4 Mac mini
</p>

<div class="layout">
  <div class="form-col">
    <form hx-post="mflux/generate" hx-target="#result" hx-indicator="#spinner">
      <div class="form-group">
        <label for="prompt">Prompt</label>
        <textarea id="prompt" name="prompt" placeholder="Beschreibe das Bild..." required></textarea>
      </div>
      <div class="form-group">
        <label for="format">Format</label>
        <select id="format" name="format">
          <option value="quadrat">Quadrat (512×512, ~18s, 10.4 GB)</option>
          <option value="hochformat">Hochformat (512×768, ~33s, 13.5 GB)</option>
          <option value="breitformat">Breitformat (768×512, ~33s, 13.5 GB)</option>
        </select>
      </div>
      <button type="submit" class="btn-primary">
        ✨ Generieren
        <span id="spinner" class="htmx-indicator"> ⏳</span>
      </button>
    </form>
  </div>
  <div class="preview-col">
    <div id="result">
      <p style="color:#3a4050;text-align:center;padding:2rem">Prompt eingeben und Generieren klicken</p>
    </div>
  </div>
</div>

</body>
</html>"""


@app.get("/mflux", response_class=HTMLResponse)
async def mflux_page():
    return MFLUX_HTML


@app.post("/mflux/generate")
async def mflux_generate(prompt: str = Form(...), format: str = Form("quadrat")):
    """Submit an image generation task, poll until done, return result HTML."""
    # Step 1: Submit task
    payload = {"prompt": prompt, "format": format}
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{RELAY_BASE_URL}/relay/v2/scheduler/task-simple",
            json={"capability": "image.generate.mflux", "payload": payload},
            headers=_get_headers(),
        )
        if resp.status_code != 200:
            return HTMLResponse(f'<div class="status status-err">❌ Task submit failed: {resp.text}</div>')

        data = resp.json()
        task_id = data["task_id"]

        # Step 2: Poll until completed or failed
        deadline = time.monotonic() + 300  # 5 min timeout
        while time.monotonic() < deadline:
            poll_resp = await client.get(
                f"{RELAY_BASE_URL}/relay/v2/scheduler/tasks/{task_id}",
                headers=_get_headers(),
            )
            if poll_resp.status_code != 200:
                await asyncio.sleep(2)
                continue

            task_data = poll_resp.json()
            status = task_data.get("status")

            if status == "completed":
                # Extract artifact_id from stages
                artifact_id = _extract_artifact_id(task_data)
                if artifact_id:
                    # Download and cache the image
                    img_resp = await client.get(
                        f"{RELAY_BASE_URL}/relay/v2/storage/files/{artifact_id}",
                        headers=_get_headers(),
                    )
                    if img_resp.status_code == 200:
                        import base64
                        img_b64 = base64.b64encode(img_resp.content).decode()
                        return HTMLResponse(
                            f'<div class="status status-ok">✅ Bild generiert</div>'
                            f'<img src="data:image/png;base64,{img_b64}" alt="Generiertes Bild" style="max-width:100%;max-height:70vh;border-radius:.5rem">'
                            f'<div class="meta">Prompt: {prompt}</div>'
                        )
                return HTMLResponse(f'<div class="status status-ok">✅ Task abgeschlossen, aber kein Bild gefunden</div>')

            elif status in ("failed", "timed_out"):
                return HTMLResponse(f'<div class="status status-err">❌ Task {status}</div>')

            await asyncio.sleep(2)

        return HTMLResponse(f'<div class="status status-err">⏰ Timeout nach 5 Minuten</div>')


@app.get("/mflux/bilder/{filename}")
async def mflux_bild(filename: str):
    """Serve a cached image."""
    # Guard against path traversal
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    cache_path = CACHE_DIR / filename
    if not cache_path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    return Response(content=cache_path.read_bytes(), media_type="image/png")


def _extract_artifact_id(task_data: dict) -> str | None:
    """Extract the first artifact_id from a completed task."""
    stages = task_data.get("stages", [])
    for stage in stages:
        result = stage.get("result")
        if result:
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except json.JSONDecodeError:
                    continue
            artifact_id = result.get("artifact_id") or (result.get("result") or {}).get("artifact_id")
            if artifact_id:
                return artifact_id
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=getattr(logging, os.environ.get("RELAY_LOG_LEVEL", "INFO").upper()),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logger.info("SSN Proxy starting on %s:%s", SSN_PROXY_HOST, SSN_PROXY_PORT)
    uvicorn.run(app, host=SSN_PROXY_HOST, port=SSN_PROXY_PORT, log_level="info")


if __name__ == "__main__":
    main()
