# Technical Design Specification: Asynchronous Multi-Agent AI Coding System

**Author:** Grok Agent (xAI)  
**Branch:** `design/grok`  
**Document version:** 1.0  
**Status:** Phase 1 blind draft (local only)  
**SRS baseline:** `plans/design/SRS_async_multiagent_ai_coding_system.md` v1.2  

---

## 0. Design Intent & Principles

This design turns GitHub into a durable message bus and state machine, with a thin local gateway on the Mac mini that keeps long-lived agent sessions warm inside OrbStack.

**Principles (ordered):**

1. **GitHub is source of truth** for collaboration state (issues, PRs, reviews, approvals). Local state is a cache for routing and recovery only.
2. **Issue-scoped sessions** — one lifecycle per issue; multiple PRs allowed before teardown.
3. **Zero cold starts** — no re-clone, no full re-ingest, no conversation wipe between webhook events for the same issue.
4. **Role-agnostic identities** — bot GitHub accounts never embed role names; roles are config.
5. **Human gates** — agents never close issues; verification checkboxes and owner responses control progression.
6. **Simplicity under 24 GB** — one host daemon + one container per active issue; SQLite; no distributed queue.

### Assumptions (explicit)

| # | Assumption | If wrong |
|---|------------|----------|
| A1 | Two bot GitHub users exist (e.g. `claude-bot`, `grok-bot`) with PATs scoped `repo`, `read:org` as needed | Auth module swaps token source |
| A2 | Host runs macOS with OrbStack Docker API at default socket | Gateway config points to alternate socket |
| A3 | Webhooks reach the Mac mini via a tunnel (Cloudflare Tunnel / ngrok) or reverse proxy | Tunnel is ops, not core design |
| A4 | Each bot account can run a CLI agent (Claude Code, Grok CLI, or compatible headless runner) inside containers | Runner adapter interface is pluggable |
| A5 | Repo owner GitHub login is known per repo (for `@owner` escalation tags) | Configured in repo config file |
| A6 | Branch protection requires a non-author approving review from a different account | Satisfied by FR-1.3 dual-account design |

### Success criteria (verifiable)

- New issue → Architect produces Design PR without human prompt.
- Design PR approved by Developer role → Feature PR opened by Developer account.
- Feature PR review loop uses Architect account for approvals that satisfy branch protection.
- Agents never close issues; close only after human `- [x] Human Verification Complete` + human close.
- Host reboot → launchd starts gateway + OrbStack; active issue sessions resume from SQLite + GitHub reconcile within 2 minutes.
- Free disk &lt; 15 GB → webhooks paused + macOS notification; no new containers.

---

## 1. System Architecture Overview

```
                    ┌─────────────────────────────────────┐
                    │            GitHub.com               │
                    │  Issues · PRs · Comments · Reviews  │
                    │         (message bus / FSM)         │
                    └───────────────┬─────────────────────┘
                                    │ webhooks (HTTPS)
                                    ▼
                    ┌─────────────────────────────────────┐
                    │  Cloudflare Tunnel / ngrok (optional)│
                    └───────────────┬─────────────────────┘
                                    │ localhost
                                    ▼
 ┌──────────────────────────────────────────────────────────┐
 │  Mac mini host (macOS + OrbStack)                        │
 │                                                          │
 │  ┌────────────────────────────────────────────────────┐  │
 │  │  gatewayd  (Python 3.12, FastAPI, launchd)         │  │
 │  │  - HMAC verify webhooks                            │  │
 │  │  - loop filter (bot self-events)                   │  │
 │  │  - role resolver                                   │  │
 │  │  - SQLite route + session registry                 │  │
 │  │  - disk circuit breaker                            │  │
 │  │  - container lifecycle (Docker API → OrbStack)     │  │
 │  └─────────────┬──────────────────┬───────────────────┘  │
 │                │ JSON-RPC /stdio  │ bind-mounts          │
 │                ▼                  ▼                      │
 │  ┌──────────────────┐  ┌──────────────────────────────┐  │
 │  │ issue-N container│  │ Host volumes                 │  │
 │  │  session-runner  │  │  /var/lib/aacs/issues/{id}/  │  │
 │  │  + agent CLIs    │  │    workspace/  (git worktree)│  │
 │  │  (long-lived)    │  │    session/    (transcripts) │  │
 │  └──────────────────┘  │    scratch/    (artifacts)   │  │
 │         …              └──────────────────────────────┘  │
 └──────────────────────────────────────────────────────────┘
```

### Component summary

| Component | Runs where | Responsibility |
|-----------|------------|----------------|
| **gatewayd** | Host process | Webhooks, routing, config, SQLite, container lifecycle, circuit breaker, reconcile |
| **session-runner** | Per-issue container | Receives routed events; maintains conversation; invokes Architect/Developer runners; git ops |
| **agent runners** | Inside session container | Model-specific CLI wrappers (Claude / Grok / future); produce git + GitHub actions under the correct bot identity |
| **GitHub** | Cloud | Durable workflow state, branch protection, human UI |

---

## 2. Technology Choices (Independent Decisions)

These items were unconstrained by the SRS.

### 2.1 Webhook gateway language & framework

- **Language:** Python 3.12  
- **HTTP:** FastAPI + Uvicorn  
- **Why:** Fast to evolve with agent-written code, excellent GitHub/webhook ecosystem, async I/O sufficient for tens of concurrent issues on one host. Not a high-QPS service.  
- **Process supervision:** `launchd` LaunchAgent/Daemon for auto-start after reboot (NFR-1.1).  
- **Packaging:** `uv` project + pinned lockfile; install under `/opt/aacs/gateway`.

### 2.2 Local state store

- **Engine:** SQLite 3, WAL mode, `busy_timeout=5000`  
- **Path:** `/var/lib/aacs/gateway.db`  
- **Why:** Zero ops, crash-safe enough with WAL + fsync on commit, trivial backup, matches single-host deployment. No Postgres/Redis.

### 2.3 IPC between gateway and session-runner

- **Control plane:** **JSON-RPC 2.0 over Unix domain socket** (`/var/lib/aacs/issues/{issue_id}/rpc.sock`)  
  - Methods: `event.submit`, `session.ping`, `session.pause`, `session.resume`, `session.shutdown`, `session.status`  
- **Log / progress stream:** **NDJSON** on a second socket or append-only file `session/events.ndjson` for observability  
- **Why not pure stdio:** Containers stay up; gateway attaches/detaches without restarting the agent process. UDS is local-only and simple under OrbStack bind mounts.  
- **Fallback:** If UDS unavailable, gateway uses `docker exec` + one-shot JSON-RPC on stdio for recovery tooling only.

### 2.4 Container model (OrbStack)

- **One long-lived container per open issue** (not per agent, not per event).  
- **Image:** `aacs/session-runner:<version>` — Debian slim + git + gh + both agent CLIs + Python session-runner.  
- **Resources (defaults, tunable):**  
  - Memory hard limit: **3 GB** per issue container  
  - CPU: 2 cores  
  - Concurrent active issue containers: **cap at 4** (≈12 GB) leaving headroom for macOS + OrbStack + gateway (~24 GB total).  
- **Mounts:**  
  - `.../issues/{issue_id}/workspace` → `/workspace`  
  - `.../issues/{issue_id}/session` → `/session`  
  - `.../issues/{issue_id}/scratch` → `/scratch`  
  - Read-only secrets: bot tokens via file mounts (not env in `docker inspect`-friendly defaults where possible)  
- **Network:** egress for GitHub + LLM APIs only; no inbound ports published.  
- **Restart policy:** `unless-stopped`; gateway re-attaches RPC on start.

### 2.5 Naming

System codename in paths/services: **AACS** (Asynchronous Agent Coding System).  
Repo-facing docs may still say “async multi-agent AI coding system.”

---

## 3. Identity, Configuration & Governance

### 3.1 Multi-account authentication (FR-1.1)

**Host secrets layout** (mode `0600`, owner `_aacs`):

```
/etc/aacs/secrets/
  bots/
    claude-bot.token      # GitHub PAT or fine-grained token
    grok-bot.token
  llm/
    anthropic.key
    xai.key
  webhook.secret          # GitHub webhook HMAC
```

**Git identity inside container:** session-runner sets `gh auth` / `GIT_CONFIG` per *action role*, not per container lifetime:

- Before Architect-side git/gh commands → credentials for the account mapped to Architect.
- Before Developer-side commands → credentials for the account mapped to Developer.

Implementation: short-lived `GH_TOKEN` / `gh auth setup-git` in a subprocess env for each command batch. Never commit tokens into repo or worktree.

### 3.2 Configurable role mapping (FR-1.2)

**Precedence (highest first):**

1. Issue label or body front-matter: `aacs:architect=claude-bot`, `aacs:developer=grok-bot`  
2. Issue milestone / project field (optional)  
3. Repo file `.aacs/config.yaml` on default branch  
4. Global gateway default in `/etc/aacs/config.yaml`

Example `.aacs/config.yaml`:

```yaml
owner: huozhe
bots:
  - login: claude-bot
  - login: grok-bot
roles:
  default:
    architect: claude-bot
    developer: grok-bot
# optional per-label overrides
role_overrides:
  - when_label: "architect:grok"
    architect: grok-bot
    developer: claude-bot
```

Role resolution is stored on the session row at issue open and can be updated if config labels change before design freeze (after Design PR merge, roles are locked for that issue to avoid mid-flight identity thrash).

### 3.3 Branch protection compliance (FR-1.3)

- **Developer account** opens Feature PRs and pushes commits.  
- **Architect account** submits the formal approving PR review (`gh api .../reviews` with `APPROVE`).  
- Accounts are distinct → GitHub branch protection “require review from someone other than the author” is satisfied.  
- Design PR is opened by **Architect**; reviewed/approved by **Developer** (symmetric peer review of design).

Gateway refuses to post an `APPROVE` using the same login that opened the PR (hard safety check).

---

## 4. Workflow State Machine

Logical states tracked in SQLite (`sessions.state`) and mirrored in GitHub labels for human visibility.

```
                  issue_opened
                       │
                       ▼
              ┌─────────────────┐
              │ DESIGN_DRAFTING │  Architect writes RFC + Design PR
              └────────┬────────┘
                       │ design_pr_opened
                       ▼
              ┌─────────────────┐
              │ DESIGN_REVIEW   │  Developer reviews Design PR
              └────────┬────────┘
                 ┌─────┴─────┐
                 │ changes   │ approved
                 ▼           ▼
           DESIGN_DRAFTING  DESIGN_APPROVED
                                 │
                                 ▼
                        ┌─────────────────┐
                        │ IMPLEMENTING    │  Developer Feature PR
                        └────────┬────────┘
                                 │ feature_pr_opened
                                 ▼
                        ┌─────────────────┐
                        │ CODE_REVIEW     │◄────┐
                        └────────┬────────┘     │
                           ┌─────┴─────┐        │
                           │ changes   │ approve│
                           ▼           ▼        │
                      IMPLEMENTING  AWAITING_CI │
                                        │       │
                                   CI pass      │
                                        ▼       │
                                   MERGING ─────┘ (if more work)
                                        │
                                        ▼
                              AWAITING_HUMAN_VERIFY
                                        │
                          human checks box + closes issue
                                        ▼
                                   TEARING_DOWN
                                        │
                                        ▼
                                     CLOSED
```

**Paused overlay:** any state may set `paused=1` on escalation (FR-3.4). Events still enqueue; runner does not execute model turns until unpaused (owner comment detected).

### 4.1 Issue ingestion (FR-2.1)

On `issues.opened` (or `reopened` if no session):

1. Circuit breaker check (disk, concurrency).  
2. Create SQLite session + host dirs.  
3. Pull/create container from `aacs/session-runner`.  
4. Bootstrap: shallow clone once into `workspace/` (single clone for the issue lifetime; later updates are `git fetch` only).  
5. Resolve roles; inject system prompt + issue body to Architect runner.  
6. Transition → `DESIGN_DRAFTING`.

### 4.2 Design phase (FR-2.2)

Architect runner shall:

1. Create branch `design/issue-{n}-{slug}` from default branch.  
2. Write RFC at `docs/design/issue-{n}-rfc.md` (path convention).  
3. Push + open **Design PR** titled `Design: #{n} …` with body `Ref #{n}`.  
4. Comment on issue with Design PR URL.  
5. State → `DESIGN_REVIEW`.

### 4.3 Peer design review (FR-2.3)

Developer runner on Design PR events:

- Review RFC vs issue; either request changes (PR review `CHANGES_REQUESTED`) or `APPROVE`.  
- On approve → state `DESIGN_APPROVED`. Gateway or Developer merges Design PR **only if** repo policy allows (default: Developer merges design PR after own approval is insufficient alone — prefer Architect merge of Design PR after Developer approval, or merge queue with dual approval; **default policy:** Developer approves, **Architect merges** Design PR to keep “planner owns design” semantics).  
- Spec text: **Developer approves Design PR; Architect merges Design PR** after that approval.

### 4.4 Implementation phase (FR-2.4)

On `DESIGN_APPROVED` (Design PR merged):

1. Developer creates worktree/branch `feat/issue-{n}-{slug}` from updated default branch (fetch, no re-clone).  
2. Implements against approved RFC.  
3. Opens **Feature PR** `Ref #{n}`.  
4. Ensures issue body contains **Verification Protocol** section (FR-3.1) — if missing, updates issue body via API.  
5. State → `CODE_REVIEW`.

### 4.5 Automated code review loop (FR-2.5)

- Architect receives Feature PR diffs (prefer `pull_request_review` / synchronize events).  
- Posts **inline** review comments via GitHub review API.  
- `CHANGES_REQUESTED` → Developer addresses, pushes, comments `@architect-bot ready for re-review` (login resolved from role map).  
- Loop until Architect `APPROVE` and CI green.

### 4.6 Merge & branch deletion (FR-2.6)

On Architect approval + required CI success:

1. **Developer account** merges Feature PR (merge method: repo default, prefer squash).  
2. **Developer account** deletes feature branch (remote + local worktree branch).  
3. State → `AWAITING_HUMAN_VERIFY`.  
4. Issue remains **open**. Agents do not close it.

Multiple Feature PRs under one issue are allowed (e.g. follow-up fixes): return to `IMPLEMENTING` if human or Architect requests more work via issue comment before verification checkbox.

---

## 5. Human Verification, Escalation & Gating

### 5.1 Verification Protocol section (FR-3.1)

Before Feature PR is marked ready for human test, Developer ensures the issue body includes:

```markdown
## Verification Protocol

1. ...
2. ...

## Human Sign-off
- [ ] Human Verification Complete
```

Agents may update this section as implementation evolves; they must never check the human box.

### 5.2 Issue closure gate (FR-3.2)

- **Close trigger for teardown:** `issues.closed` **and** verification checkbox is checked in issue body at close time.  
- If issue is closed without checkbox → gateway comments warning, does **not** tear down session immediately; reopens or waits for owner policy (default: comment + leave session paused 24h, then teardown with warning).  
- Preferred path: owner checks box, then closes issue → `TEARING_DOWN`.

Agents **never** call close issue API.

### 5.3 Escalation (FR-3.3, FR-3.4)

Escalation conditions:

- Conflicting requirements, missing secrets, repeated CI failure (&gt;N=3), design deadlock (2 full design review loops with no convergence), or explicit model uncertainty flag.

Action:

1. Post issue comment: `@{owner} Escalation: …` with options / questions.  
2. Set `sessions.paused=1`, state overlay `PAUSED`.  
3. Ignore agent-turn triggers until owner comments (non-bot).  
4. On owner comment → `paused=0`, re-inject last context + owner reply to the waiting role.

---

## 6. Event Routing & Loop Prevention

### 6.1 Ingestion pipeline (FR-4.1)

```
HTTP POST /webhooks/github
  → verify X-Hub-Signature-256
  → parse event + delivery id (idempotent insert)
  → drop if actor login ∈ configured bot logins AND event is pure echo of our own write
     (exceptions: CI status from GitHub Actions bots may pass)
  → map to repo + issue number (from issue, PR, or comment payload)
  → load/create session
  → if circuit_open: 503 + log; do not enqueue new sessions
  → if paused: enqueue only (or drop model-turn types)
  → JSON-RPC event.submit to session-runner
  → 200 quickly (&lt;1s); heavy work async in container
```

**Loop prevention rules:**

| Event | Drop? |
|-------|-------|
| Comment by bot that is only “ack” of own prior action | Yes if delivery linked to our outbound idempotency key |
| PR review by Architect on Developer PR | No — route to Developer |
| PR synchronize from Developer push | No — route to Architect |
| Issue edit by bot updating Verification Protocol | Yes for re-entrancy into same writer; allow once via generation counter |

**Idempotency:** table `webhook_deliveries(delivery_id PRIMARY KEY, received_at, result)`.

### 6.2 Gated cleanup (FR-4.2, FR-4.3)

On valid teardown trigger:

| Actor | Cleanup |
|-------|---------|
| **Developer session path** | Remove git worktrees, local feat/design branches, `workspace/` dirty state; `git worktree prune` |
| **Architect session path** | Delete `/scratch/*` diffs, temporary patch files, review bundles |
| **Orchestrator (gatewayd)** | JSON-RPC `session.shutdown`; stop & remove container; delete `rpc.sock`; purge `/var/lib/aacs/issues/{id}/` after optional T-day retention (default 7 days metadata keep, workspace wipe immediate) |

Order: agent cleanups → container stop → volume purge → SQLite session `CLOSED`.

---

## 7. SQLite Schema (Gateway)

```sql
PRAGMA journal_mode=WAL;

CREATE TABLE repos (
  repo_full_name TEXT PRIMARY KEY,     -- e.g. huozhe/code-workflow
  owner_login    TEXT NOT NULL,
  config_json    TEXT,                 -- cached .aacs/config.yaml
  updated_at     TEXT NOT NULL
);

CREATE TABLE sessions (
  id              TEXT PRIMARY KEY,    -- uuid
  repo_full_name  TEXT NOT NULL,
  issue_number    INTEGER NOT NULL,
  state           TEXT NOT NULL,
  paused          INTEGER NOT NULL DEFAULT 0,
  architect_login TEXT NOT NULL,
  developer_login TEXT NOT NULL,
  container_id    TEXT,
  design_pr       INTEGER,
  feature_pr      INTEGER,             -- latest
  roles_locked    INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  UNIQUE (repo_full_name, issue_number)
);

CREATE TABLE webhook_deliveries (
  delivery_id  TEXT PRIMARY KEY,
  event_type   TEXT NOT NULL,
  received_at  TEXT NOT NULL,
  session_id   TEXT,
  result       TEXT NOT NULL            -- processed|dropped|error
);

CREATE TABLE event_outbox (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id   TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  delivered_at TEXT
);

CREATE TABLE circuit_breaker (
  id              INTEGER PRIMARY KEY CHECK (id = 1),
  disk_paused     INTEGER NOT NULL DEFAULT 0,
  reason          TEXT,
  updated_at      TEXT NOT NULL
);
```

Session transcripts and model context live **on disk under the issue volume**, not in SQLite (keeps DB small; zero cold-start = volume reuse).

---

## 8. Session Runner Internals

### 8.1 Process model inside container

```
PID 1: session-runner (Python)
  - serves JSON-RPC on /session/rpc.sock
  - owns conversation stores:
      /session/architect/transcript.jsonl
      /session/developer/transcript.jsonl
  - spawns agent CLI subprocesses with role-scoped env
  - serializes turns per role (no concurrent Architect turns)
```

### 8.2 Zero cold-start mechanics (constraint)

| Resource | Persistence |
|----------|-------------|
| Git object DB | Single clone in `/workspace` for issue life; fetch/rebase only |
| Conversation | Append-only transcript + rolling summary file `summary.md` refreshed each turn |
| Tool caches | `/session/cache` (package managers, linters) retained |
| Container | Not destroyed between events |

On gateway restart: reconnect UDS; if container missing, recreate **same mounts** → transcripts and git state intact → send synthetic `system.resume` event with last known GitHub issue/PR snapshot.

### 8.3 Agent runner adapter interface

```python
class AgentRunner(Protocol):
    login: str  # GitHub login this runner acts as
    def turn(self, role: Literal["architect","developer"],
             prompt: str, github_event: dict) -> TurnResult: ...
```

`TurnResult` includes: summary text, optional gh/git side effects already applied, escalation flag, next recommended state.

Concrete adapters: `ClaudeCodeRunner`, `GrokCliRunner`. Selection by login → vendor map in config.

### 8.4 Prompt / context packaging

Each turn receives:

1. Role system charter (Architect vs Developer responsibilities from SRS).  
2. Approved RFC path contents (if any).  
3. Issue body (including Verification Protocol).  
4. Compacted transcript summary + last K turns.  
5. Triggering GitHub event payload (trimmed).  
6. Repo tree sketch (cached `git ls-files` + optional symbols index under `/session/index`).

Full codebase re-embed on every event is **forbidden**. Incremental index update on `git fetch` only.

---

## 9. Non-Functional Design

### 9.1 Unattended recovery (NFR-1.1)

**launchd** (`com.aacs.gateway.plist`):

- `RunAtLoad` + `KeepAlive`  
- Depends on OrbStack running (wait loop for Docker socket)  

**OrbStack:** user setting “Start at login” enabled (documented setup step).

### 9.2 State reconciliation (NFR-1.2)

On gateway start:

1. Wait for Docker socket.  
2. Open SQLite; mark in-flight deliveries.  
3. For each `sessions.state NOT IN ('CLOSED', 'TEARING_DOWN')`:  
   - Ensure container running (recreate if needed with same mounts).  
   - `GET` issue + open PRs from GitHub.  
   - Recompute state if GitHub advanced while down (e.g. human merged PR).  
   - Send `system.resume` to session-runner.  
4. Clear stale `disk_paused` only after fresh disk check.

### 9.3 Memory & disk (NFR-2.1, NFR-2.2)

**Memory budget (planning):**

| Slice | Budget |
|-------|--------|
| macOS + desktop | ~6 GB |
| OrbStack VM overhead | ~1–2 GB |
| gatewayd | &lt;256 MB |
| 4 × issue containers @ 3 GB | 12 GB |
| Headroom / spikes | rest |

**Disk circuit breaker:**

- Background task every 30s: `shutil.disk_usage` on `/var/lib/aacs` volume.  
- If free &lt; **15 GB**: set `circuit_breaker.disk_paused=1`; reject new session creation; return 503 to webhooks that would create work; **macOS notification** via `osascript`:

  `display notification "AACS paused: free disk < 15 GB" with title "AACS"`

- Resume automatically when free ≥ **20 GB** (hysteresis) or operator runs `aacsctl circuit reset` after freeing space.

---

## 10. HTTP API (Host Gateway)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/webhooks/github` | GitHub webhooks |
| GET | `/healthz` | Liveness |
| GET | `/readyz` | Docker + SQLite + disk OK |
| GET | `/v1/sessions` | List sessions (local admin) |
| POST | `/v1/sessions/{id}/pause` | Operator pause |
| POST | `/v1/sessions/{id}/resume` | Operator resume |
| POST | `/v1/admin/reconcile` | Force reconcile |

Admin endpoints bind **localhost only**.

CLI: `aacsctl` (thin HTTP client) for operator use.

---

## 11. Security Considerations

- Webhook HMAC required; reject unsigned.  
- Bot PATs: least privilege; separate per bot; never into git.  
- Containers non-root user `aacs` where possible; no Docker socket mount into session containers (only gateway talks to Docker).  
- Network egress allowlist optional (Phase 2 hardening).  
- Prompt injection: treat issue/PR text as untrusted data; tools that push code still run but merge remains policy-gated by Architect + CI + human verification.  
- Secrets not logged; redaction filter on NDJSON logs.

---

## 12. Observability

- Structured logs: gateway → `/var/log/aacs/gateway.log` (JSON lines).  
- Per-issue: `/session/events.ndjson` (turn start/end, gh commands, errors).  
- Metrics (optional lightweight): counters in SQLite `metrics` table or Prometheus textfile for node_exporter — **not required for v1**.  
- Human-visible: GitHub labels `aacs:state:*`, `aacs:paused`.

---

## 13. Setup Runbook (Host)

1. Install OrbStack; enable start at login.  
2. Create bot accounts; store PATs under `/etc/aacs/secrets`.  
3. `uv sync` / install gateway; load launchd plist.  
4. Build `aacs/session-runner` image.  
5. Create GitHub webhook → tunnel URL → `/webhooks/github` (events: Issues, Issue comments, Pull requests, Pull request reviews, Pushes optional).  
6. Add `.aacs/config.yaml` to target repos.  
7. Configure branch protection: require 1 approving review; CI required on Feature PRs.  
8. Smoke test: open issue → Design PR appears from Architect login.

---

## 14. Testing Strategy

| Layer | What |
|-------|------|
| Unit | Role resolver precedence; checkbox parser; loop-filter rules; state transitions |
| Component | SQLite reconcile fixtures; circuit breaker hysteresis |
| Integration | Recorded GitHub webhook fixtures → fake session-runner |
| E2E (manual/staging) | Dual-bot dry-run on a private sandbox repo |
| Chaos | Kill gateway mid-turn; reboot host; fill disk below 15 GB |

---

## 15. Implementation Phases (Post-Design)

Not part of Phase 1 delivery, but proposed build order:

1. **M1 – Gateway skeleton:** webhooks, SQLite, loop filter, `aacsctl`, launchd.  
2. **M2 – Session container:** mounts, JSON-RPC, single echo agent.  
3. **M3 – Real Developer adapter + Feature PR path** (human acts as Architect).  
4. **M4 – Real Architect adapter + full dual-agent loop.**  
5. **M5 – Recovery, circuit breaker, cleanup polish.**  
6. **M6 – Multi-repo config + role overrides hardening.**

---

## 16. Requirements Traceability

| ID | Requirement | Design section |
|----|-------------|----------------|
| FR-1.1 | Multi-account auth | §3.1 |
| FR-1.2 | Configurable roles | §3.2 |
| FR-1.3 | Branch protection dual accounts | §3.3 |
| FR-2.1 | Issue ingestion | §4.1 |
| FR-2.2 | Design PR | §4.2 |
| FR-2.3 | Peer design review | §4.3 |
| FR-2.4 | Implementation / Feature PR | §4.4 |
| FR-2.5 | Code review loop | §4.5 |
| FR-2.6 | Developer merge + branch delete | §4.6 |
| FR-3.1 | Verification Protocol section | §5.1 |
| FR-3.2 | Human gate on close | §5.2 |
| FR-3.3 | Escalation tag owner | §5.3 |
| FR-3.4 | Pause on escalation | §5.3, §4 |
| FR-4.1 | Routing + loop prevention | §6.1 |
| FR-4.2 | Cleanup only after human close path | §6.2 |
| FR-4.3 | Distributed cleanup duties | §6.2 |
| NFR-1.1 | Auto-start recovery | §9.1 |
| NFR-1.2 | Reconcile on startup | §9.2 |
| NFR-2.1 | Memory/disk awareness | §9.3 |
| NFR-2.2 | 15 GB circuit breaker + notification | §9.3 |
| Constraint | Issue-scoped sessions | §1, §4 |
| Constraint | Zero cold-starts | §8.2 |
| Constraint | Role-agnostic identities | §3 |
| Constraint | 24 GB / OrbStack | §2.4, §9.3 |

---

## 17. Alternatives Considered (Brief)

| Topic | Rejected | Why |
|-------|----------|-----|
| Per-event ephemeral containers | Cold start + re-clone cost | Violates zero cold-start |
| Postgres / Redis | Ops weight on single Mac mini | SQLite enough |
| stdio-only IPC with restart per event | Loses process warmth | UDS long-lived RPC preferred |
| Kubernetes | Overkill | OrbStack Docker API sufficient |
| Agents close issues after merge | Convenient but forbidden | FR-3.2 human gate |
| Role baked into bot usernames | Inflexible | FR-1.2 dynamic roles |

---

## 18. Open Questions for Phase 3 (Non-Blocking)

1. Design PR merge actor: Architect-after-Developer-approve (this draft) vs. human-only design merge.  
2. Retention window for closed issue volumes (default 7 days).  
3. Exact max concurrent issues (default 4) vs. memory limits per model CLI.  
4. Whether CI must be GitHub Actions-only or also local OrbStack CI runners.

---

## 19. Document History

| Ver | Date | Notes |
|-----|------|-------|
| 1.0 | 2026-08-01 | Phase 1 blind draft by Grok Agent |

---

*End of Grok design specification.*
