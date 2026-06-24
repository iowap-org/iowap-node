# Common AI Relay Worker Poller

This directory contains a **reference poller implementation** for AI Relay worker
nodes. It is intentionally generic: it provides the protocol logic
(authentication, heartbeat, claim, execute, complete), while the surrounding
service wrapper is platform-specific.

A node operator copies `poller.py`, writes a small bootstrap script for their
platform (systemd, launchd, cron, Windows Task Scheduler, Docker, ...), and
registers task handlers for the capabilities their node offers.

---

## 1. What the poller does

```
                ┌──────────────────────────────────────────┐
                │           AI Relay Server                │
                │  (registration, scheduler, artifacts)    │
                └──────────────────────────────────────────┘
                           ▲            │
        heartbeat          │            │ claim / complete
       (every N s)         │            │
                           │            ▼
                ┌──────────────────────────────────────────┐
                │              Worker Node                 │
                │                                          │
                │  ┌──────────────┐    ┌──────────────┐   │
                │  │  heartbeat   │    │ claim loop   │   │
                │  │  thread      │    │              │   │
                │  │  (keeps node │    │  for each    │   │
                │  │   online)    │    │  capability  │   │
                │  └──────────────┘    └──────┬───────┘   │
                │                               │          │
                │                       handler(stage)     │
                │                               │          │
                │                          result          │
                │                               │          │
                │                       complete(...)      │
                │                                          │
                └──────────────────────────────────────────┘
```

### Lifecycle

1. **Load credentials** from the node metadata file.
2. **Recover** a missing runtime token (`rt_...`) with the registration secret
   (`rs_...`) if necessary.
3. **Start a background heartbeat thread** that keeps the node `online` while
   the node is busy.
4. **Claim stages** matching one of the registered capabilities.
5. **Execute** the registered handler for the stage.
6. **Complete** the stage with the handler's result dict.
7. **Refresh credentials proactively** before they expire.

### Why a background heartbeat matters

Older pollers sent the heartbeat inside the same loop that executed tasks. If a
task ran for minutes (image generation, large backup, model inference), no
heartbeat reached the server. The server then marked the node `offline`, and the
subsequent `complete` call was rejected with **403 Forbidden** — even though the
node was still healthy and the token was valid.

The common poller avoids this by running heartbeats in a separate daemon thread.

---

## 2. Files and conventions

| File / Path | Purpose |
|-------------|---------|
| `poller.py` | Reference implementation. Copy to your node. |
| `relay_config.json.example` | Example configuration. Rename to `~/.relay/relay_config.json`. |
| `~/.relay/ai-relay-agent.json` | Node metadata created during registration. Contains `node_id`, `registration_secret`, `capabilities`, `base_url`. |
| `~/.relay/ai-relay-agent.token` | Current runtime token. Written atomically by the poller. |
| `~/.relay/worker_status.json` | Last known worker status for external monitoring. |

### Node metadata (`ai-relay-agent.json`)

```json
{
  "node_id": "V34ETT74",
  "node_name": "mac-mini-mflux",
  "base_url": "http://relay.example.com:8788",
  "registration_secret": "rs_...",
  "capabilities": ["image.generate.ai", "chat.ai"]
}
```

Capabilities may also be objects:

```json
{
  "capabilities": [
    {"name": "image.generate.ai", "available": true},
    {"name": "chat.ai", "available": true}
  ]
}
```

### Configuration (`relay_config.json`)

```json
{
  "base_url": null,
  "heartbeat_interval": 8,
  "claim_interval": 5,
  "status_interval": 7200,
  "rt_refresh_before_seconds": 86400,
  "rs_refresh_before_seconds": 3600,
  "request_timeout": 10,
  "task_timeout": 600,
  "load_cap": 1.0,
  "log_level": "INFO",
  "background_heartbeat": true
}
```

| Key | Meaning |
|-----|---------|
| `heartbeat_interval` | Seconds between heartbeats (default 8). |
| `claim_interval` | Seconds between claim attempts (default 5). |
| `status_interval` | Seconds between proactive credential-lifetime checks. |
| `rt_refresh_before_seconds` | Refresh runtime token this many seconds before expiry. |
| `rs_refresh_before_seconds` | Refresh registration secret this many seconds before expiry. |
| `background_heartbeat` | Run heartbeat in a background thread (default true). |

---

## 3. Writing a node bootstrap

A bootstrap script does three things:

1. Ensure `~/.relay/ai-relay-agent.json` exists (register if missing).
2. Ensure the correct Python environment is active.
3. Import `Poller`, register handlers, and call `poller.run()`.

### Minimal example

```python
#!/usr/bin/env python3
import sys
from pathlib import Path

# Adjust path if poller.py is not next to this script.
sys.path.insert(0, str(Path(__file__).parent))

from poller import Poller

def handle_image_generate(stage):
    prompt = stage["payload"]["prompt"]
    # ... run local tool ...
    return {"status": "ok", "files": ["artifact_id_123"]}

poller = Poller()
poller.register("image.generate.ai", handle_image_generate)
poller.run()
```

Save this as e.g. `mac-poller.py` and run it with your platform's service
wrapper.

---

## 4. Platform wrappers

The poller itself is Python. How it is started/stopped depends on the OS.

### 4.1 systemd (Linux)

Place a unit file in `~/.config/systemd/user/` (user service) or
`/etc/systemd/system/` (system service).

```ini
# ~/.config/systemd/user/ai-relay-agent-poller.service
[Unit]
Description=AI Relay Agent Poller
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/path/to/venv/bin/python3 /path/to/mac-poller.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable ai-relay-agent-poller.service
systemctl --user start ai-relay-agent-poller.service
```

### 4.2 launchd (macOS)

Create a `launchd` plist in `~/Library/LaunchAgents/`.

```xml
<!-- ~/Library/LaunchAgents/com.example.ai-relay-agent.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.example.ai-relay-agent</string>

  <key>ProgramArguments</key>
  <array>
    <string>/path/to/venv/bin/python3</string>
    <string>/Users/felix/.hermes/scripts/mac-poller.py</string>
  </array>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
    <key>Crashed</key>
    <true/>
  </dict>

  <key>StandardOutPath</key>
  <string>/Users/felix/.relay/ai-relay-agent.log</string>

  <key>StandardErrorPath</key>
  <string>/Users/felix/.relay/ai-relay-agent.log</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.example.ai-relay-agent.plist
launchctl start com.example.ai-relay-agent
```

To inspect logs:

```bash
log show --predicate 'process == "python3"' --last 30m
# or tail the file:
tail -f ~/.relay/ai-relay-agent.log
```

### 4.3 cron / manual

For testing or simple deployments, run the bootstrap manually:

```bash
nohup /path/to/venv/bin/python3 /path/to/mac-poller.py > ~/.relay/poller.log 2>&1 &
```

Or add a crontab entry that restarts it if it died:

```cron
*/5 * * * * pgrep -f mac-poller.py || nohup /path/to/venv/bin/python3 /path/to/mac-poller.py > ~/.relay/poller.log 2>&1 &
```

---

## 5. Handler contract

A handler receives a `stage` dict:

```python
{
  "stage_id": "stage_...",
  "task_id": "task_...",
  "stage_name": "generate-image",
  "capability": "image.generate.ai",
  "payload": {"prompt": "a cat", "width": 768, "height": 768},
  "timeout_seconds": 600
}
```

It must return a JSON-serializable `result` dict, e.g.:

```python
{
  "status": "ok",
  "artifacts": ["artifact_id_123"],
  "summary": "generated 768x768 image"
}
```

If the handler raises an exception, the poller completes the stage with
`{"error": str(exc)}` and counts it as failed.

Handlers may run for a long time. The background heartbeat thread keeps the node
online. If the handler itself needs parallelism (e.g. run a subprocess), it is
responsible for that.

---

## 6. KI-capable vs KI-less nodes

The poller does not care whether a node is KI-capable or KI-less. The
**capability suffix** tells the scheduler how the work is executed:

| Suffix | Meaning | Typical node |
|--------|---------|--------------|
| `.native` | Direct execution, no KI involved. | Storage, backup, printer, switch. |
| `.ai` | Local KI decides/tools which tools to call. | Hermes/Matrix/TUI agent nodes. |
| `.relay` | Worker forwards the stage to another specialized service. | Gateway/bridge nodes. |

KI-capable nodes should implement a thin handler that delegates the stage to
the local Hermes AI. See `examples/agent-integration/` for a reference.

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `403 Forbidden` on `complete` | Node is `offline` because heartbeats stopped during long work. | Ensure `background_heartbeat: true` and use the latest `poller.py`. |
| `401 Unauthorized` on every call | Runtime token expired and recovery failed. | Re-register the node or restore a fresh `ai-relay-agent.json`. |
| Scheduler never assigns work | Capabilities do not match any stage capability exactly. | Use full capability names with suffix, e.g. `storage.archive.native`. |
| Heartbeats 200 but no claims | `claim_interval` too long or no pending matching stages. | Check dashboard or task list. |
| `ModuleNotFoundError` for `httpx` | Missing Python dependency. | Install `httpx` in the venv running the poller. |

---

## 8. Customizing

You may copy `poller.py` and modify it for your node. Common customizations:

- Change `STATUS_PATH` to a node-specific file.
- Add custom logging or metrics.
- Implement a custom `heartbeat()` that reports GPU load, disk space, etc.
- Replace the synchronous `httpx` calls with `asyncio` if the node is
  I/O-bound.

The server-side protocol is documented in `docs/node-readme.md` and
`docs/nodes-design.md`.
