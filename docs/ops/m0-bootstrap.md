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
```

**Test-only env override (not for production LaunchAgent):**

```bash
export AGENTD_SECRET_WEBHOOK_SECRET='...'   # webhook HMAC
export AGENTD_SECRET_CLAUDE_BOT='...'       # optional PAT overrides
export AGENTD_SECRET_GROK_BOT='...'
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

Application logs: DEBUG/INFO → stdout (`gateway.log` under LaunchAgent); WARNING+ → stderr (`gateway.err.log`).

## 4. LaunchAgent

```bash
# After `uv sync`, edit packaging/dev.agentd.plist:
#   - REPLACE → your home directory basename (e.g. alice)
#   - ProgramArguments[0] → absolute path to agentd/.venv/bin/agentd
#     (not `uv run` — boot after power-cut must not re-resolve deps)
#   - WorkingDirectory → ~/.agentd (data root; not the git checkout)
cp packaging/dev.agentd.plist ~/Library/LaunchAgents/dev.agentd.plist
launchctl unload ~/Library/LaunchAgents/dev.agentd.plist 2>/dev/null || true
launchctl load ~/Library/LaunchAgents/dev.agentd.plist
```

### Log rotation (newsyslog)

LaunchAgent writes unbounded files under `~/.agentd/logs/`. Install size-based rotation (1 MB × 5 archives):

```bash
# edit REPLACE in packaging/newsyslog.agentd.conf first
sudo cp packaging/newsyslog.agentd.conf /etc/newsyslog.d/agentd.conf
sudo newsyslog -nvv   # dry-run; should list both gateway paths
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
