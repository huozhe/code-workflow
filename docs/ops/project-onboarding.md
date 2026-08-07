# Project onboarding (auth + layout)

Per #19 / #20: **one project container**, durable per-role HOME, manual auth at setup.

## Layout

```
~/.agentd/projects/<owner>__<repo>/
  repo/                         # shared clone (gateway-owned)
  sessions/<issue>/{architect,developer}/…
  home/{architect,developer}/   # durable CLI HOME + Grok auth chain
```

Host `state.db` / `config.yaml` stay at `~/.agentd/` and are **never** mounted into the container.

## Claude (subscription)

1. On a browser-capable host:
   ```bash
   claude setup-token
   ```
2. Store the printed token:
   ```bash
   security add-generic-password -s agentd -a claude-oauth-token -w -U
   ```
3. agentd injects `CLAUDE_CODE_OAUTH_TOKEN` into the project container at create time.
   Do **not** copy `~/.claude/.credentials.json` or Keychain interactive dumps.

## Grok (subscription, Option D)

For each **role** that will run `grok` (usually developer; both if §5.3 may swap):

```bash
export HOME=~/.agentd/projects/<owner>__<repo>/home/developer
mkdir -p "$HOME"
grok login --device-auth
```

Repeat for `home/architect` if needed. Never copy `auth.json` between roles or from your interactive host `~/.grok`.

## Verify

```bash
agentctl status   # docker true, hot_sessions ≤ max_hot_containers
# After ensure_session for an issue in the project:
docker exec -u 1001:1001 <project-container> ls /srv/agentd
# expect: home  repo  sessions
```
