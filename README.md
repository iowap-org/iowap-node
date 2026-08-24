# iowap-node

**IOWAP Node Framework — Build your own node in the IOWAP ecosystem**

> **THIS IS THE REPO YOU WANT** if you want to run a node. Clone this, configure your capabilities, and join the network.

The node framework provides everything you need to register, heartbeat, claim tasks, and run handlers. Nodes are the workers — they advertise what they can do and execute tasks when matched.

## Quick Start

```bash
# Clone
git clone https://github.com/iowap-org/iowap-node.git
cd iowap-node

# Setup
pip install -e .
cp relay_config.json.example .relay/relay_config.json
# Edit .relay/relay_config.json: set relay_url, register with a server

# Register & run
node-cli node register    # Get a node ID and token
node-cli capabilities publish default   # Publish your capabilities
node-daemon               # Start the event-driven worker
```

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

Just write a handler script that reads the JSON payload from stdin, calls your push service, and writes the result to stdout. That's it — the node framework handles the rest.

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

## Docker

A base Docker image is available for containerized nodes:

```bash
docker pull ghcr.io/iowap-org/iowap-node:latest
```

See `docker/nodes/base/` for the Dockerfile.

## Docs

Full documentation in [iowap-org/iowap-docs](https://github.com/iowap-org/iowap-docs):

- `docs/getting-started.md` — first steps
- `docs/node/setup.md` — node setup
- `docs/node/cli-reference.md` — full CLI reference
- `docs/node/capabilities.md` — capability definitions
- `docs/node/federation.md` — peer-to-peer federation

## License

AGPL-3.0