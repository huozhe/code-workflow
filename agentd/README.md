# agentd

Local orchestrator gateway for the asynchronous multi-agent AI coding system.

Contract: [`docs/design/unified_design_spec.md`](../docs/design/unified_design_spec.md).

## M0–M3 scope

- Host layout under `~/.agentd/`
- Webhook ingress (`POST /webhooks/github`) with HMAC + durable SQLite queue (P7)
- Full §15.1 schema
- Resource Governor (30 s disk sample; trip below 15 GB / reset above 20 GB)
- Intake gate (§4.3 label + collaborators) via dispatcher drain
- **M2 session supervisor:** `agentd/session-runner` image, JSON-RPC over loopback TCP + bearer, tmpfs tokens, worktrees, HOT/COLD
- **M3 design loop:** `turn.dispatch` (setuid → adapter), `session.resume` rehydration, routing §9.1, budgets/stall §9.2–9.3, design FSM, deferred→session→routed
- LaunchAgent template + Docker socket wait
- `agentctl status | sessions | logs`
- Keychain helpers for secrets

### Build session-runner image

```bash
cd agentd
docker build -t agentd/session-runner:1.2.0 -f docker/session-runner/Dockerfile docker/session-runner
# Pins: git 2.48.1, Claude Code 2.1.224, Grok CLI 1.0.0 (override via --build-arg)
```

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
