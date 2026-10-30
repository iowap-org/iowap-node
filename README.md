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
node-cli mcp install              # optional: the MCP toolkit (own venv)
```

**From source (dev)**:

```bash
git clone https://github.com/iowap-org/iowap-node.git
cd iowap-node
pip install -e .
cp relay_config.json.example ~/.relay/relay_config.json
# edit ~/.relay/relay_config.json
node-cli node register
node-cli capabilities publish default
```

**Updates**: `node-cli update check` (node + MCP toolkit) and
`node-cli update apply` — PyPI-first with the GitHub release as fallback.

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
| `node-cli` | CLI tool — register, manage, submit tasks, list nodes |
| `node-daemon` | Event-driven worker — listens for tasks via SSE, runs handlers |
| `relay_client` | HTTP client library for relay API |
| `handler_runner` | Execute custom handlers with stdin/stdout contract |
| `node_config` | Capability/configuration management |

## CLI Reference

```bash
node-cli register                  # Register with a relay server
node-cli capabilities server       # List server-known capabilities
node-cli capabilities publish      # Publish your capabilities
node-cli task submit <capability>  # Submit a task
node-cli task wait <id>            # Wait for task completion
node-cli node list                 # List registered nodes
node-cli node info <id>            # Node details
node-cli daemon                    # Start the daemon (foreground)
node-cli bridge upload <file>      # Upload via bridge route
node-cli bridge download <url>     # Download via bridge route
```

## Forward Routing

A node can offer **forward slots**: submitters address a slot instead of
a concrete capability, and the relay resolves `(this node, slot)` →
target capability at submit time. Submission forces the relay's
`forward` pseudo-capability (the slot rides inside the payload):

```bash
node-cli task submit --stage 'probe.echo:{"operation":"status"}' --forward-slot 1
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
  profile is refused at `node-cli capabilities validate|publish`).
- Declared slots are transported with every daemon heartbeat
  (T-233/T-240): removing slots and heartbeating with the emptied
  declaration clears the server-side rows (empty list = delete all).
- The declaration is validated up front (T-239): a malformed
  `forward_slots` block fails `node-cli capabilities publish` instead of
  leaving the node silent at the relay.

Docs: live on your relay at `/relay/v2/docs/concepts/forward-routing` —
[concepts/forward-routing.md](https://github.com/iowap-org/iowap-docs/blob/main/concepts/forward-routing.md).

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
