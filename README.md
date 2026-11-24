# iowap-node

**IOWAP Node Framework — Build your own node in the IOWAP ecosystem**

> **THIS IS THE REPO YOU WANT** if you want to run a node. Clone this, configure your capabilities, and join the network.

The node framework provides everything you need to register, heartbeat, claim tasks, and run handlers. Nodes are the workers — they advertise what they can do and execute tasks when matched.

## Quick Start (Linux)

**One-liner** — installs the Python package **from PyPI** into a dedicated
venv (systemd user unit, registration; re-runs offer update / override /
uninstall). `IOWAP_SOURCE=git` switches to a checkout-based dev install.

```bash
curl -fsSL https://raw.githubusercontent.com/iowap-org/iowap-node/main/install.sh | bash
```

**From PyPI (manual, any platform)**:

```bash
pip install "iowap-node"          # import namespace: nodes
iowap-cli mcp install              # optional: the MCP toolkit (own venv)
```

**From source (dev)**:

```bash
git clone https://github.com/iowap-org/iowap-node.git
cd iowap-node
pip install -e .
cp relay_config.json.example ~/.relay/relay_config.json
# edit ~/.relay/relay_config.json
iowap-cli node register
iowap-cli capabilities publish default
```

**Updates**: `iowap-cli update check` (node + MCP toolkit) and
`iowap-cli update apply` — PyPI-first with the GitHub release as fallback.

## What You Can Build

A node is just a process that:

1. **Heartbeats** its capabilities to a relay server (what can I do?)
2. **Claims tasks** that match its capabilities (I'll take this)
3. **Runs handlers** — scripts or programs — to execute the work
4. **Completes tasks** with results

Your capabilities are defined in a `node.yaml`:

```yaml
node_name: "my-notifier"
node_description: "Sends push notifications"
capabilities:
  notification.send:
    description: "Send a push notification to my phone"
    type: script
    input_schema:
      fields:
        - name: title
          type: string
          required: true
          description: "Notification title"
        - name: body
          type: string
          required: true
          description: "Notification body"
        - name: priority
          type: string
          required: false
          description: "low / normal / high"
```

Just write a handler script that reads the JSON handler envelope from stdin, calls your push service, and writes the result to stdout. Your schema fields arrive NESTED under the `input` key of the envelope — not at the top level:

```
{"task_id": "...", "capability": "notification.send", "input": {"title": "...", "body": "...", "priority": "normal"}}
```

That is the frozen T-005b Envelope-Contract (`handler_runner._build_stdin_payload`); stdout is normalized back into the Response Envelope (`{"status": "completed", "result": {...}}`). That's it — the node framework handles the rest.

## Components

| Component | Description |
|-----------|-------------|
| `iowap-cli` | CLI tool — register, manage, submit tasks, list nodes |
| `iowap-daemon` | Event-driven worker — listens for tasks via SSE, runs handlers |
| `relay_client` | HTTP client library for relay API |
| `handler_runner` | Execute custom handlers with stdin/stdout contract |
| `node_config` | Capability/configuration management |

## CLI Reference

```bash
iowap-cli register                  # Register with a relay server
iowap-cli capabilities server       # List server-known capabilities
iowap-cli capabilities publish      # Publish your capabilities
iowap-cli task submit <capability>  # Submit a task
iowap-cli task wait <id>            # Wait for task completion
iowap-cli node list                 # List registered nodes
iowap-cli node info <id>            # Node details
iowap-cli daemon                    # Start the daemon (foreground)
iowap-cli bridge upload <file>      # Upload via bridge route
iowap-cli bridge download <url>     # Download via bridge route
```

## Forward Routing

A node can offer **forward slots**: submitters address a slot instead of
a concrete capability, and the relay resolves `(this node, slot)` →
target capability at submit time. Submission forces the relay's
`forward` pseudo-capability (the slot rides inside the payload):

```bash
iowap-cli task submit --stage 'probe.echo:{"operation":"status"}' --forward-slot 1
```

Slots are declared in the active profile (`~/.relay/node.yaml`):
a `forward` slot takes a target capability via the relay's forward
mappings; `text`/`bool` slots carry typed values or references —
e.g. feeding a prompt into another node's slot:

```yaml
forward_slots:
  "1":
    type: forward
    label: Wartungsslot
  "2":
    type: text
    label: Instruktprompt
    value: "Prüfe …"
```

- `forward` is never a real capability and never advertised by the node —
  it exists for submission only (a `capability: forward` block in a
  profile is refused at `iowap-cli capabilities validate|publish`).
- Declared slots are transported with every daemon heartbeat
  (T-233/T-240): removing slots and heartbeating with the emptied
  declaration clears the server-side rows (empty list = delete all).
- The declaration is validated up front (T-239): a malformed
  `forward_slots` block fails `iowap-cli capabilities publish` instead of
  leaving the node silent at the relay.

### Forward-Slots section in the node dashboard (T-030)

The node dashboard (port 7783, section „Forward-Slots" under
Capabilities) shows the node's declared slots together with their
`(node, slot) → target_capability` mappings:

* Read side: the dashboard fetches
  `GET /relay/v2/forward-mappings/slots?node=<own>` and
  `GET /relay/v2/forward-mappings?node=<own>` with the node token.
* Admin nodes (admin-role node token, human admin or master) can edit
  the mapping target per slot inline — saved via
  `PUT /relay/v2/forward-mappings/{node_id}/{slot}` with
  `{"target_capability": "…"}` (upsert, optional `target_node_id` pin)
  — and delete a mapping via `DELETE /relay/v2/forward-mappings/{node_id}/{slot}`.
* Nodes without the admin role get a quiet note („nur für Admin-Nodes
  konfigurierbar") instead of a dead editor.
* Slots themselves are NOT created in the dashboard — they come from the
  `forward_slots` block in node.yaml and ride the heartbeat (T-053).

Docs: live on your relay at `/relay/v2/docs/concepts/forward-routing` —
[concepts/forward-routing.md](https://github.com/iowap-org/iowap-docs/blob/main/concepts/forward-routing.md).

## Capability dashboard fields (T-030)

The node dashboard's capability form (Add) writes
`<workspace>/bridge/providers.yaml`; besides the T-019 fields it carries:

| Field | Meaning | Default |
|---|---|---|
| `max_parallel` | max parallel claims the scheduler grants for this capability | 1 |
| `cost` | admission points charged per claim (T-247) | 1 |
| `artifacts_glob` | workspace-relative glob of files uploaded as artifacts after a run (e.g. `artifacts/host.sysinfo/*`) | — |

Empty fields mean the defaults; invalid values (non-integer, below the
minimum) are rejected without writing anything. `max_parallel`/`cost`
are forwarded from the providers entry into the bridge manifest →
heartbeat → relay scheduler (they take effect once the bridge daemon
passes them through the manifest — the Go side currently serves name/
type/summary/long_run/input_schema).

### Script upload (T-030)

Next to the command field, **„Script wählen…"** opens the native file
picker (`.bat .cmd .ps1 .py .sh .exe`) and uploads the file to
`POST /caps/script-upload` (multipart). The dashboard stores it
atomically under `<workspace>/bridge/scripts/` (2 MB cap, filename
sanitized, collisions get a numeric suffix like `cleanup.1.py`) and
prefills the form — capability name (slugified filename) and command
argv per extension:

| Extension | Default argv |
|---|---|
| `.bat` / `.cmd` | `cmd /c scripts\<file>` |
| `.ps1` | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\<file>` |
| `.py` | `python scripts\<file>` |
| `.sh` | `bash scripts/<file>` |
| `.exe` | `scripts\<file>` |

Both fields stay editable. Relative `scripts/...` paths work because the
bridge daemon executes process commands with `cwd = workspace`
(process.go: `cmd.Dir = workspaceDir`).

## Bridge workspace channel (`daemon.json`, T-026)

In a universal-node setup the host-side bridge daemon and the node share a
workspace volume. Two channels run over it:

* **Node → daemon:** `<workspace>/bridge/providers.yaml` — capability
  definitions (written by the node dashboard, watched by the daemon, T-022).
* **Daemon → node:** `<workspace>/bridge/daemon.json` —
  `{"url": "http://host.docker.internal:7780", "token": "<bridge token>"}`,
  written by the daemon after every successful bootstrap. The node's
  watcher (10 s poll) adopts `url` + `token` into the `bridge:` block of
  the active node.yaml — ONLY those two fields, everything else stays
  untouched — and SIGHUPs the daemon pid files so the change is picked up
  live. This repairs containers that were re-created by hand (`docker
  compose up` without the token envs): the daemon starts existing
  containers as-is, so the shared file is the only channel that survives
  every recreate.

**Precedence: `daemon.json` wins.** While the file exists, a
hand-configured `bridge.url`/`bridge.token` in node.yaml is overwritten by
the next poll (every takeover is logged). To configure the bridge by hand,
delete `<workspace>/bridge/daemon.json` first — a missing file is a no-op.

## Docker

A base Docker image is available for containerized nodes:

```bash
docker pull ghcr.io/iowap-org/iowap-node:latest
```

See `docker/nodes/base/` for the Dockerfile.

## Docs

Full documentation in [iowap-org/iowap-docs](https://github.com/iowap-org/iowap-docs):

- [getting-started.md](https://github.com/iowap-org/iowap-docs/blob/main/getting-started.md) — first steps
- [node/setup.md](https://github.com/iowap-org/iowap-docs/blob/main/node/setup.md) — node setup
- [node/cli.md](https://github.com/iowap-org/iowap-docs/blob/main/node/cli.md) — full CLI reference
- [node/capabilities.md](https://github.com/iowap-org/iowap-docs/blob/main/node/capabilities.md) — capability definitions
- [federation/concept.md](https://github.com/iowap-org/iowap-docs/blob/main/federation/concept.md) — cross-relay federation

## License

MIT — see [LICENSE](LICENSE).
