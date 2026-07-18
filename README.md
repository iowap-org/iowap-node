# Common AI Relay Node Utilities

This directory contains the **node-cli daemon** (`node_cli.py`) and shared
utility functions (`node_utils.py`) for AI Relay worker nodes.

> **Note:** The legacy `poller.py` has been removed. All worker nodes should
> use `node-cli` (the daemon) instead. See `docs/node/cli-reference.md` for
> the full command reference.

## Files

| File | Purpose |
|------|---------|
| `node_cli.py` | CLI + daemon: heartbeat, claim, execute, complete. The recommended worker implementation. |
| `node_utils.py` | Shared utility functions (config/meta/token file I/O). Used by `RelayClient`. |
| `capability_loader.py` | YAML profile loading, validation, and publishing. |
| `handler_runner.py` | Subprocess execution for capability handlers. |

## Quick start

```bash
# Start the daemon (runs heartbeat + claim loop)
python -m nodes.common.node_cli daemon start

# Check status
python -m nodes.common.node_cli status

# Query all capabilities on the relay server
python -m nodes.common.node_cli capabilities server

# Submit a task and wait for result
python -m nodes.common.node_cli task submit --name "my-task" --stage 'chat.ai:{...}'
python -m nodes.common.node_cli task wait <task_id>
```

## Configuration

See `~/.relay/relay_config.json` and `docs/node/setup.md`.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `403 Forbidden` on `complete` | Node is `offline` because heartbeats stopped during long work. | Ensure `background_heartbeat: true` in config. |
| `401 Unauthorized` on every call | Runtime token expired and recovery failed. | Re-register the node or restore a fresh `ai-relay-agent.json`. |
| Scheduler never assigns work | Capabilities do not match any stage capability exactly. | Use full capability names with suffix, e.g. `chat.ai`. |
| Heartbeats 200 but no claims | No pending matching stages. | Check dashboard or `node-cli capabilities server`. |
