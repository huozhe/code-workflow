# agentd

Local orchestrator gateway for the asynchronous multi-agent AI coding system.

Contract: [`docs/design/unified_design_spec.md`](../docs/design/unified_design_spec.md).

## M0–M1 scope

- Host layout under `~/.agentd/`
- Webhook ingress (`POST /webhooks/github`) with HMAC + durable SQLite queue (P7)
- Full §15.1 schema (sessions/runners/turns reserved for M2+)
- Resource Governor (30 s disk sample; trip below 15 GB / reset above 20 GB)
- Intake gate (§4.3 label + collaborators) via dispatcher drain
- LaunchAgent template + Docker socket wait
- `agentctl status | sessions | logs`
- Keychain helpers for secrets

## Quick start

```bash
cd agentd
uv sync --all-extras
uv run agentd init-layout
uv run agentd serve --host 127.0.0.1 --port 8787
```

In another terminal:

```bash
uv run agentctl status
```

See [`docs/ops/m0-bootstrap.md`](../docs/ops/m0-bootstrap.md) for Keychain, LaunchAgent, tunnel, and webhook registration.
