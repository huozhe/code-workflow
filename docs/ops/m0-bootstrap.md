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

Dev fallback (not for production): `export AGENTD_SECRET_WEBHOOK_SECRET=...`

## 3. Run gateway

```bash
uv run agentd serve --host 127.0.0.1 --port 8787
# waits for Docker/OrbStack socket with backoff unless --skip-docker-wait
```

```bash
uv run agentctl status
curl -s localhost:8787/healthz
```

## 4. LaunchAgent

```bash
# edit paths in packaging/dev.agentd.plist (REPLACE → your home)
cp packaging/dev.agentd.plist ~/Library/LaunchAgents/dev.agentd.plist
launchctl unload ~/Library/LaunchAgents/dev.agentd.plist 2>/dev/null || true
launchctl load ~/Library/LaunchAgents/dev.agentd.plist
```

## 5. Host power / OrbStack (NFR-1.1b)

```bash
sudo systemsetup -setrestartpowerfailure on
# OrbStack → Settings → Start at login: enabled
```

**Physical test (required for M0 exit):** power-cycle the Mac mini, unlock FileVault, confirm LaunchAgent starts `agentd` and OrbStack socket becomes ready without further action.

## 6. Ingress tunnel (pluggable)

Default design: Tailscale Funnel → `127.0.0.1:8787`.

```bash
# example
tailscale funnel 8787
```

Alternatives: `cloudflared tunnel`, `ngrok http 8787`, `smee -u <url> -t http://127.0.0.1:8787/webhooks/github`.

## 7. Register GitHub webhook

Repo → Settings → Webhooks → Add:

- Payload URL: `https://<tunnel-host>/webhooks/github`
- Content type: `application/json`
- Secret: same as Keychain `webhook-secret`
- Events: issues, issue comments, pull requests, PR reviews, PR review comments, check suites, statuses, pushes (or "Send me everything" for M0 smoke)

**M0 behavioural exit:** send a ping / open a test issue; confirm `agentd` logs `delivery queued` and `agentctl status` shows `queue_depth >= 1`.
