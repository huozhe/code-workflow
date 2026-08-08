# M0 bootstrap runbook

Implements issue #6 M0 checklist against `docs/design/unified_design_spec.md`.

## 1. Layout

```bash
cd agentd
uv sync --all-extras
uv run agentd init-layout
# creates ~/.agentd/{repos,sessions,archive,logs,config.yaml,state.db on first serve}
```

## 2. Secrets (macOS Keychain)

```bash
# Webhook HMAC (same value as GitHub webhook secret)
security add-generic-password -s agentd -a webhook-secret -w '<random-hex>' -U

# Bot PATs (used from M2+; store now so M0 host is complete)
security add-generic-password -s agentd -a claude-bot -w '<pat>' -U
security add-generic-password -s agentd -a grok-bot -w '<pat>' -U

# Gateway voice (M3-A §8.5 escalations). Fail-closed: agentd will NOT fall
# back to claude-bot/grok-bot for escalation comments (PR #27 B2 / ADR-11).
# Account: @huozhegateway (classic repo PAT — fine-grained unavailable on
# private personal repos; see unified_design_spec §5.1 v1.1.2). Collaborator
# on target repos only. Also set gateway.login in config.yaml.
security add-generic-password -s agentd -a gateway -w '<gateway-pat>' -U
```

In `~/.agentd/config.yaml`:

```yaml
gateway:
  login: huozhegateway   # GitHub username for escalation comments
  docker_socket: unix:///var/run/docker.sock
```

**Test-only env override (not for production LaunchAgent):**

```bash
export AGENTD_SECRET_WEBHOOK_SECRET='...'   # webhook HMAC
export AGENTD_SECRET_CLAUDE_BOT='...'       # optional PAT overrides
export AGENTD_SECRET_GROK_BOT='...'
export AGENTD_SECRET_GATEWAY='...'         # gateway escalation comment PAT
```

These bypass Keychain when set. The production plist must **not** set them; production loads only from Keychain at process start and **exits non-zero** if the webhook secret is missing (so a lagging login-keychain after FileVault unlock fails loud instead of 5xx-dropping GitHub deliveries).

## 3. Run gateway

```bash
uv run agentd serve --host 127.0.0.1 --port 8787
# waits for Docker/OrbStack socket with backoff unless --skip-docker-wait
```

```bash
uv run agentctl status
curl -s localhost:8787/healthz
```

**Logs**

| Path | Role |
|---|---|
| `~/.agentd/logs/agentd.log` | **Primary** (app + `uvicorn.access` / `uvicorn.error`) — in-process `RotatingFileHandler` (1 MiB × 5). `agentctl logs` tails this. `uvicorn.run(..., log_config=None)` so access traffic is not stranded on an uncapped stdout sink. |
| `~/.agentd/logs/gateway.log` | LaunchAgent stdout crash/pre-config sink only — no INFO mirror (would re-unbound the loud path). |
| `~/.agentd/logs/gateway.err.log` | LaunchAgent stderr sink for WARNING+ and hard crashes / uncaught tracebacks. |

newsyslog cannot rotate LaunchAgent `StandardOutPath` files safely: launchd holds the inode open, so rename leaves the process writing unbounded data into `gateway.log.0`. Rotation is in-process on `agentd.log` only.

## 4. LaunchAgent

```bash
# After `uv sync`, edit packaging/dev.agentd.plist — replace BOTH tokens:
#   REPLACE_HOME          → home basename (e.g. alice)
#   REPLACE_CHECKOUT_PATH → path from $HOME to the code-workflow repo
#                           (e.g. claude_repos/code-workflow — not a guess)
# ProgramArguments must be the absolute .venv/bin/agentd (not `uv run`).
# WorkingDirectory stays ~/.agentd (data root; not the git checkout).
test -x /Users/"$USER"/REPLACE_CHECKOUT_PATH/agentd/.venv/bin/agentd   # before load
cp packaging/dev.agentd.plist ~/Library/LaunchAgents/dev.agentd.plist
# edit tokens in the installed copy if you prefer not to touch the template
launchctl unload ~/Library/LaunchAgents/dev.agentd.plist 2>/dev/null || true
launchctl load ~/Library/LaunchAgents/dev.agentd.plist
launchctl list | grep dev.agentd
# expect:  <pid>  0  dev.agentd
# middle column non-zero (78/127) → ProgramArguments path wrong → silent respawn
```

## 5. Host power / OrbStack (NFR-1.1b)

```bash
sudo systemsetup -setrestartpowerfailure on
# OrbStack → Settings → Start at login: enabled
```

**Physical test (required for M0 exit):** power-cycle the Mac mini, unlock FileVault, confirm LaunchAgent starts `agentd` and OrbStack socket becomes ready without further action.

## 6. Ingress tunnel (pluggable)

Default design: Tailscale Funnel → `127.0.0.1:8787`.

**Working Funnel form** (path must be on the *target*, not only `--set-path` alone — otherwise Funnel silently strips the path and every webhook 404s):

```bash
# background so it survives reboot / terminal exit
tailscale funnel --bg --set-path=/webhooks/github \
  http://127.0.0.1:8787/webhooks/github
```

Verify from the public hostname:

```bash
# 401 = Funnel path + HMAC gate OK (no signature)
# 404 / 405 = path broken (often the silent strip from a bare --set-path)
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "https://<funnel-host>/webhooks/github"
curl -sS -o /dev/null -w '%{http_code}\n' "https://<funnel-host>/readyz"   # expect 404
```

Exposing only `/webhooks/github` also keeps `/healthz` and `/readyz` (disk telemetry) off the public internet. That is tunnel-vendor behaviour verified empirically — not an app-layer guarantee.

Alternatives: `cloudflared tunnel`, `ngrok http 8787`, `smee -u <url> -t http://127.0.0.1:8787/webhooks/github`.

## 7. Register GitHub webhook

Repo → Settings → Webhooks → Add:

- Payload URL: `https://<tunnel-host>/webhooks/github`
- Content type: `application/json`
- Secret: same as Keychain `webhook-secret`
- Events: issues, issue comments, pull requests, PR reviews, PR review comments, check suites, statuses, pushes (or "Send me everything" for M0 smoke)

**M0 behavioural exit:** send a ping / open a test issue; confirm `agentd` logs `delivery queued` and `agentctl status` shows `queue_depth >= 1`.
