# agentd

The gateway, supervisor and container runner for the asynchronous multi-agent AI coding system. The repository root [`README.md`](../README.md) describes what the system does; this file is how to run it.

Contract: [`docs/design/unified_design_spec.md`](../docs/design/unified_design_spec.md). Where the spec's summary and an [ADR](../docs/design/ADR/) disagree, the ADR is right.

## What is in here

| Concern | Where |
|---|---|
| **Ingress** — `POST /webhooks/github`, HMAC-verified, accept-and-queue into a durable SQLite queue (§15.1 schema) | `src/agentd/server.py`, `db.py`, `intake.py` |
| **Dispatch** — intake gate (§4.3 label + collaborators), routing (§9.1), budgets and stall detection (§9.2–9.3) | `dispatcher.py`, `routing.py` |
| **Supervisor** — the `agentd/session-runner` image, one container per project, two OS UIDs, JSON-RPC over loopback TCP with a bearer, tmpfs tokens, worktrees over a shared clone, HOT/COLD | `supervisor.py`, `gitops.py`, `rpc_client.py` |
| **Loops** — design and code FSMs, `turn.dispatch` (setuid → adapter), `session.resume` rehydration | `session_loop.py`, `fsm.py` |
| **Human gate** — the §10.1 verification block, and teardown on a human close | `verification.py`, `archive.py` |
| **Recovery** — reconciler for deliveries GitHub sent and the queue lost; GC for orphaned artifacts | `reconciler.py`, `gc.py` |
| **Safety** — loop budgets and escalation, vendor-refusal handling, disk governor (30 s sample; trip below 15 GB, resume above 20 GB) | `loop_safety.py`, `refusals.py`, `governor.py` |
| **Runner** — what executes inside the container | `docker/session-runner/agentd_runner/` |

739 tests under `tests/`. Credentials come from the Keychain on the host (`keychain.py`) and reach a role over the control channel — never a bind mount (ADR-6).

## Commands

```
agentd    init-layout                 create ~/.agentd and a default config
          serve [--host --port]       run the webhook gateway

agentctl  version                     installed distribution version
          status [--json]             queue depth and host readiness
          sessions [--live]           all sessions, or the in-the-loop set (ADR-41)
          logs [-n]                   tail ~/.agentd/logs/agentd.log
          reconcile [--dry-run --once]  local reconcile report (ADR-20)
          gc [--dry-run]              GC report; the daemon applies deletes (ADR-23)
          review-stats [--session]    turns per review (ADR-14)
          write-verification SESSION [--dry-run]   upsert the §10.1 block
          quarantine-deferred [--before --dry-run] one-shot backlog purge
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

This gets a gateway answering on loopback. It does **not** get you a working system: that needs the Keychain entries, the LaunchAgent, the tunnel and the webhook registration in [`docs/ops/m0-bootstrap.md`](../docs/ops/m0-bootstrap.md), then a project onboarded per [`docs/ops/project-onboarding.md`](../docs/ops/project-onboarding.md).

## Build the session-runner image

The supervisor's default is pinned in `src/agentd/supervisor.py` and asserted by `tests/test_runner_image_isolation.py`. Build that tag, or the supervisor will not use what you built:

```bash
cd agentd
docker build -t agentd/session-runner:1.5.0 \
  -f docker/session-runner/Dockerfile docker/session-runner
```

Pinned inside the image, each overridable with `--build-arg`: git 2.48.1, Claude Code 2.1.237, Grok CLI 1.0.5. The build verifies the two CLI versions it installed and fails if either does not match.
