# Unified Technical Design Specification — `agentd`

### Asynchronous Multi-Agent AI Coding System

| | |
|---|---|
| **Status** | Proposed for formal approval (Phase 3 exit) |
| **Version** | 1.41.0 — see [Revision history](#revision-history) |
| **Implements** | [`docs/requirements/SRS_async_multiagent_ai_coding_system.md`](../requirements/SRS_async_multiagent_ai_coding_system.md) **v1.3** |
| **Supersedes** | [`proposals/claude_design_spec.md`](proposals/claude_design_spec.md) (#4) · [`proposals/grok_design_spec.md`](proposals/grok_design_spec.md) (#2) · [`proposals/gemini_design_spec.md`](proposals/gemini_design_spec.md) (#3) |
| **Ref** | Issue #1 |
| **Drafted by** | Claude Agent (`@huozheclaude`), per group assignment |
| **Consensus source** | Issue #1, Rounds 1–5 + owner rulings from `@huozhe` |

### Revision history

Amendments are also marked inline at the point they apply, which is where an implementer will meet them. This table exists so the version number means something.

**One line per version.** Where a version landed an ADR, the arrow links to it and the ADR carries the reasoning, the options and the acceptance. Where it did not, the change is a spec edit and the full entry is in `git log docs/design/unified_design_spec.md`. Nothing here is the decision of record.

| Version | Date | Change |
|---|---|---|
| **1.41.0** | 2026-09-01 | **"Is anything live" is a copy-pasted snippet, and the one definition of live already exists in the sweep** (#248, ADR-41). → [ADR-41](ADR/adr-41-agentctl-sessions-live.md) |
| **1.40.3** | 2026-09-01 | **`turn.resume` heard no runner notifications, and §12.3's crash window names an RPC that has never run** (#246). |
| **1.40.2** | 2026-09-01 | **ADR-38's clock has a third return site: `role held`, which knows its own deadline** (#230). → [ADR-38](ADR/adr-38-superseded-approval-defer-clock.md) |
| **1.40.1** | 2026-08-31 | **Named residual: `AWAITING_VERIFICATION` does not discover a second Feature PR** (#229, found reviewing #237). → [ADR-40](ADR/adr-40-untracked-pr-opened.md) |
| **1.40.0** | 2026-08-29 | **A `*_pr_opened` the FSM never sees strands the session permanently, and one session produced it twice by two unrelated causes** (#229, ADR-40). → [ADR-40](ADR/adr-40-untracked-pr-opened.md) |
| **1.39.0** | 2026-08-27 | **`design_loop.py` is named for one phase of the session and owns all of them** (#159, ADR-39). → [ADR-39](ADR/adr-39-session-loop-rename.md) |
| **1.38.0** | 2026-08-25 | **A `CHANGES_REQUESTED` that replaces an approval paused the session, and the delivery carrying that verdict was 19 ms behind it in the same batch** (#214, ADR-38). → [ADR-38](ADR/adr-38-superseded-approval-defer-clock.md) |
| **1.37.1** | 2026-08-25 | **ADR-37's spent-SHA gate is before the FSM, not after** (#211, found reviewing #223). → [ADR-37](ADR/adr-37-dropped-synchronize-head-sha.md) |
| **1.37.0** | 2026-08-25 | **A dropped `pull_request.synchronize` has no node id, so the sweep cannot regenerate it, and adopt would skip the review turn** (#211, ADR-37). → [ADR-37](ADR/adr-37-dropped-synchronize-head-sha.md) |
| **1.36.0** | 2026-08-25 | **The identity boundary is an account control, and M4-A's claim is an operator claim** (#57, ADR-36). → [ADR-36](ADR/adr-36-identity-account-vs-operator.md) |
| **1.35.1** | 2026-08-22 | **Named residual on ADR-35's already-dead path** (#172, found reviewing #199). → [ADR-35](ADR/adr-35-kill-as-the-role.md) |
| **1.35.0** | 2026-08-22 | **The runner cannot signal the CLIs it spawns, so the turn deadline and `session.teardown` bound nothing** (#172, ADR-35). → [ADR-35](ADR/adr-35-kill-as-the-role.md) |
| **1.34.2** | 2026-08-22 | **The reason vocabulary had a member the ADR never named, and acceptance (5) tested the wrong arm** (#116, ADR-34). → [ADR-34](ADR/adr-34-probe-attaches-and-records.md) |
| **1.34.1** | 2026-08-22 | **A rejected bearer is a fifth reason, and it is the one lazy promotion does not repair** (#116, ADR-34, found by the Developer driving the real `agentd_runner.server` in-process). → [ADR-34](ADR/adr-34-probe-attaches-and-records.md) |
| **1.34.0** | 2026-08-22 | **§11.2 step 7 attaches and records; its "mark COLD" clause is retired, and COLD has to survive GC before anything may write it** (#116, ADR-34). → [ADR-34](ADR/adr-34-probe-attaches-and-records.md) |
| **1.33.0** | 2026-08-22 | **A quota refusal that lands after real work is invisible, and the delivery pays for it** (#168, ADR-33). → [ADR-33](ADR/adr-33-quota-refusal-after-output.md) |
| **1.32.1** | 2026-08-22 | **ADR-32 corrected by its own implementation** (#184). → [ADR-32](ADR/adr-32-loop-safety-wrong-subject.md) |
| **1.32.0** | 2026-08-21 | **Both loop-safety questions are asked of the wrong subject** (#156, #147, ADR-32). → [ADR-32](ADR/adr-32-loop-safety-wrong-subject.md) |
| **1.31.0** | 2026-08-21 | **ADR-31's `GIT_NO_LAZY_FETCH` scope, and an acceptance item for `--no-checkout`** (#171, #180). → [ADR-31](ADR/adr-31-worktree-base-history.md) |
| **1.30.0** | 2026-08-21 | **Session worktrees are based on the remote-tracking ref, and the gateway fetches** (#171, ADR-31). → [ADR-31](ADR/adr-31-worktree-base-history.md) |
| **1.29.0** | 2026-08-21 | **Host-side agent work is governed by protocol, not mechanism** (#57, §5.5). |
| **1.28.0** | 2026-08-21 | **ADR-30's author drop must not swallow the owner** (#173, ADR-30 (a⁗). → [ADR-30](ADR/adr-30-author-sent-pr-events.md) |
| **1.27.0** | 2026-08-20 | **An agent's own PR-scoped events must not route to the counterpart** (#173, ADR-30). → [ADR-30](ADR/adr-30-author-sent-pr-events.md) |
| **1.26.1** | 2026-08-20 | **ADR-29 implementation binding** (#169). → [ADR-29](ADR/adr-29-held-pipe-turn-boundary.md) |
| **1.26.0** | 2026-08-19 | **The held CLI pipe has no turn boundary, so a vendor `result` frame is claimed by whichever turn reads next** (#162, ADR-29). → [ADR-29](ADR/adr-29-held-pipe-turn-boundary.md) |
| **1.25.0** | 2026-08-19 | **Model and reasoning effort are configurable per adapter, and recorded (#92, ADR-9 v2).**. → [ADR-9](ADR/adr-9-runner-contract-stable-surface.md) |
| **1.24.0** | 2026-08-19 | **ADR-27's in-flight set guards `_dispatch_turn`; it never covered `resume_interrupted_turn`, the other blocking-RPC call site** (#158, ADR-28). → [ADR-27](ADR/adr-27-inflight-turn-ids.md), [ADR-28](ADR/adr-28-inflight-two-dispatch-sites.md) |
| **1.23.0** | 2026-08-19 | **§11.2 step 6's own definition covers a live turn, and nothing before this ADR told the two apart** (#151, ADR-27). → [ADR-27](ADR/adr-27-inflight-turn-ids.md) |
| **1.22.0** | 2026-08-19 | **`blocked` is classified by unresolved review threads before it is classified by checks** (#63, ADR-26). → [ADR-26](ADR/adr-26-blocked-unresolved-threads-sticky.md) |
| **1.21.0** | 2026-08-19 | **Lazy promotion, so §11.2 step 7 can be built at all** (#116, ADR-25). → [ADR-25](ADR/adr-25-lazy-promotion.md) |
| **1.20.1** | 2026-08-17 | **ADR-24: the defuse set is repo-wide and not filtered by session state** (#106, #127). → [ADR-24](ADR/adr-24-closing-keyword-defuse.md) |
| **1.20.0** | 2026-08-17 | **The closing keyword is defused before the merge is authorised** (#106, ADR-24; owner ruling `@huozhe`). → [ADR-24](ADR/adr-24-closing-keyword-defuse.md) |
| **1.19.0** | 2026-08-17 | **M6-2: garbage collection, narrowed to what it may safely touch** (ADR-23, and **§12.3 itself is rewritten** rather than merely overridden — the Developer's review on #122 caught the ADR describing an amendment the section did not carry, which is 1.18.0's lesson repeated: an implementer starting at the table of contents builds what §12.3 says). → [ADR-23](ADR/adr-23-garbage-collection-narrowed.md) |
| **1.18.0** | 2026-08-15 | **The closed-issue marker re-arms on the reopen event, not on a sweep observation** (#118, ADR-21 amended). → [ADR-21](ADR/adr-21-reconcile-sweep-membership.md) |
| **1.17.0** | 2026-08-14 | **M6-1c: interrupted turns, and the checkpoint that was already on disk** (#81, ADR-22). → [ADR-22](ADR/adr-22-interrupted-turns-checkpoint.md) |
| **1.16.0** | 2026-08-14 | **ADR-21 amended from what building it found** (#81, #113). → [ADR-21](ADR/adr-21-reconcile-sweep-membership.md) |
| **1.15.0** | 2026-08-14 | **M6-1b: the sweep, and the membership set it needs first** (#81, ADR-21). → [ADR-21](ADR/adr-21-reconcile-sweep-membership.md) |
| **1.14.0** | 2026-08-14 | **M6-1a: the local half of §11.2 scoped, and its steps pinned down** (#81, ADR-20). → [ADR-20](ADR/adr-20-reconcile-local-half.md) |
| **1.13.0** | 2026-08-14 | **The mirror: re-read the body rather than reverse the ruling** (#96, ADR-19, owner ruling `@huozhe`). → [ADR-19](ADR/adr-19-mirror-reread-the-body.md) |
| **1.12.0** | 2026-08-13 | **The close-reconcile hold is scoped to the closed issue** (#98, ADR-18). → [ADR-18](ADR/adr-18-close-reconcile-hold-scope.md) |
| **1.11.0** | 2026-08-13 | **A ticked body with no observed owner tick does not classify** (#90, ADR-17). → [ADR-17](ADR/adr-17-ticked-body-no-owner-tick.md) |
| **1.10.0** | 2026-08-13 | **Vendor quota refusal is not a failed turn** (#86, ADR-9). → [ADR-9](ADR/adr-9-runner-contract-stable-surface.md) |
| **1.9.0** | 2026-08-13 | **No ordinary turn on a terminal session; gateway comments do not wake agents** (#85). |
| **1.8.0** | 2026-08-13 | **No restore may raise the checkbox without `verified_at`** (#89, ADR-16). → [ADR-16](ADR/adr-16-checkbox-requires-owner-tick.md) |
| **1.7.0** | 2026-08-13 | **Verification-block restore composes against a fresh read** (#84, ADR-15). → [ADR-15](ADR/adr-15-verification-block-restore.md) |
| **1.6.0** | 2026-08-13 | **`agentctl review-stats` decided** (#49, ADR-14). → [ADR-14](ADR/adr-14-agentctl-review-stats.md) |
| **1.5.0** | 2026-08-13 | **Archive excludes role `home/`, `xdg/`, `tmp/` by filter** (#74). |
| **1.4.0** | 2026-08-12 | **Each role tears down its own worktree, branch, and scratch** (#68). |
| **1.3.0** | 2026-08-12 | **`agentctl version` decided** (#58, ADR-13). → [ADR-13](ADR/adr-13-agentctl-version.md) |
| **1.2.0** | 2026-08-11 | **Session archive format & retention decided** (#47, ADR-12). → [ADR-12](ADR/adr-12-session-archive-format-retention.md) |
| **1.1.2** | 2026-08-08 | **Three identities + classic PATs** (M3-A). |
| **1.1.1** | 2026-08-07 | **Runner-owned long-lived CLI per role** (#25). |
| **1.1.0** | 2026-08-07 | **Container unit: issue → project** (ADR-4). → [ADR-4](ADR/adr-4-container-per-project-os-uids.md) |
| **1.0.0** | 2026-08-02 | Approved via #1. Amendments for M0, M1, M2 (×2) and M3 subsequently landed **against 1.0.0 without a version bump** — recorded here rather than retrofitted, sin. |

Going forward: an amendment that changes an ADR or a section's contract bumps the minor version; a correction that clarifies without changing behaviour does not.

---

## 0. How This Document Was Produced

Three agents wrote independent blind drafts, cross-reviewed each other's pull requests, and then argued for five rounds on Issue #1. This document is the result. It is **not** any one draft with the others folded in — several sections replace what the drafter originally proposed, because the drafter lost the argument.

Every section carries provenance in §1.2. Decisions that were contested carry an ADR in §16 recording the rejected alternative and why it was rejected, including the alternatives this document's author originally argued for.

Two decisions were made by the repository owner (`@huozhe`) and are not open to agent revision: the gateway implementation language, and who closes an issue. One of the owner's rulings required amending the SRS rather than the design; that amendment is in this PR.

### 0.1 Reading Guide

**This file is the spec. The ADRs are separate files under [`ADR/`](ADR/)** — §16 indexes them in one
paragraph each. Load a section, not the document: every section below is 1–20 KB and self-contained,
and the whole file is ~135 KB.

| If you need | Read |
|---|---|
| The shape of the system | §2 assumptions, §3 architecture, §4 ingress |
| **The three claims the design rests on** | §5.2 credential isolation · §6.3 zero cold start · §11.2 reconciliation |
| Who may act as whom, and the host-side rule | §5 — §5.3 role binding, §5.5 host-side agent work |
| What a session *is*, and where it lives on disk | §6 |
| The container: image, run flags, privilege | §7 |
| The state machine, and what authorises a merge | §8 — §8.1 states, §8.4 merge authorization |
| Why an event did or did not become a turn | §9.1 routing · §9.2 budgets · §9.3 stall detection |
| Verification, closure, teardown | §10 |
| Crash recovery and the reconciler | §11.2 |
| The RPC surface between gateway and runner | §14.2 method catalogue |
| Schema, and which column means what | §15.1 |
| **Why a thing is the way it is** | §16 → the ADR file. The ADR is the decision of record; the index paragraph is not |
| What is unproven | §17 open spikes · §19 known weaknesses · [`../ops/live-sign-offs.md`](../ops/live-sign-offs.md) for acceptances no fixture can reach |

**Three conventions worth knowing before you edit anything here.**

1. **Amendments are marked inline**, at the point they apply, because that is where an implementer meets
   them. The revision history exists so the version number means something; it is not the record.
2. **An ADR outranks this file.** Where a summary here and an ADR disagree, the ADR is right and the
   summary is the bug. Several ADRs also correct the issue they cite — read the ADR, not the issue.
3. **Editing by string match is unsafe.** Acceptance headers and `**Deliberately out of scope.**` repeat
   across ADRs; scope to the target file first.

---

## 1. Design Principles and Provenance

### 1.1 Principles

| # | Principle | Consequence |
|---|---|---|
| P1 | **GitHub holds truth; local state is derived** | SQLite corruption or loss is recoverable by re-reading GitHub, not by repair |
| P2 | **Roles are data; identities are credentials** | Swapping Architect↔Developer is a config edit, never a code change |
| P3 | **Privilege separation over agent obedience** | Security comes from what a token *can* do, not from what a prompt *asks* an agent to do |
| P4 | **Every side effect is idempotent and fingerprinted** | Webhook redelivery, gateway restarts, and reconciliation are all safe |
| P5 | **Fail toward the human** | Ambiguity, budget exhaustion, and resource exhaustion all terminate in an `@owner` mention, never a silent stall |
| P6 | **The daemon owns infrastructure; agents own content** | Agents never call the Docker API, never manage the shared clone, never mint credentials |
| P7 | **Ingress never returns 5xx for a downstream condition** | GitHub does not retry failed repository webhook deliveries, so back-pressure must never reach GitHub |
| P8 | **Turns are serialized per session — and, under project scope, per (project, role)** | Eliminates the entire class of concurrent-git and concurrent-review hazards. The second clause is an ADR-4 amendment: one CLI conversation per role now spans a project's issues, so two concurrently-active issues would otherwise interleave turns into a single conversation |

### 1.2 Section Provenance

| Section | Source | Note |
|---|---|---|
| §4 Ingress, accept-and-queue | Claude #4 ADR-8 | Grok and Gemini both conceded their 503 behaviour |
| §4.1 Pluggable ingress backend | Grok #2 (E8) | Replaces Claude's Tailscale-as-architecture |
| §5.2 Credential delivery | Claude R4 → Grok R5 | Grok's host-mounted token files replaced; see ADR-6 |
| §5.3 Role binding precedence | Grok #2 §3.2 + Claude #4 §5.3 | Independent convergence |
| §5.3 Role freeze at Design PR open | Grok #2 §3.2 → narrowed in PR #5 review | Third position: later than Claude's session-creation freeze, earlier than Grok's design-freeze. See §5.3 |
| §6 Session model, zero cold start | Claude #4 §6 | Gemini #3 §2.2 supplied the clearest statement of the requirement |
| §6.4 Git layer | Claude #4 §6.4 | Replaces Grok's per-issue shallow clone |
| §7 Container topology | **Claude R2 middle path**, adopted by Grok R4 + Gemini R3 | Neither the original Claude nor original Grok/Gemini topology |
| §8.3 Design PR merge actor | **Grok #2 (E1)** | Resolves ambiguity Claude left open |
| §8.4 Merge gate | Claude #4 §8.3 + Grok req 4 | |
| §9 Loop prevention routing | Claude #4 §9 | Replaces Gemini's drop-all-bot-events |
| §9.3 Fingerprint composition | **Grok R4** | Replaces Claude's weaker `last_comment_body_sha` |
| §10 Verification block | Claude #4 §10 | |
| §10.3 Closure model | **`@huozhe` ruling** | Overrules the unanimous agent vote for orchestrator-close |
| §11.1 Host boot | **`@huozhe` ruling** → SRS v1.3 | |
| §11.2 Reconciliation sweep | **Gemini #3 §5.2** (GraphQL) + Claude #4 §11.2 (watermarks) + Grok E5 (`turn.resume`) | Three-way composite |
| §12 Resource governance | Grok #2 §9.3 + Claude #4 §12 | |
| §13 Threat model | Claude R5 + Grok R4 | |
| §14 Agent Session Protocol | Claude #4 §14 + Grok #2 §2.3 | Independent convergence on JSON-RPC 2.0 |
| §18 Testing strategy | **Grok #2 §14** | Claude had none |
| §20 Setup runbook | **Grok #2 §13** | Claude had none |
| §17 Assumptions table format | **Grok #2 §0** | |
| §12.4 FSM mirrored to labels | **Grok #2 §12** | Claude had no equivalent |

---

## 2. Assumptions

| # | Assumption | If wrong |
|---|---|---|
| A1 | OrbStack is installed, healthy, and set to start at login | Gateway retries until the Docker socket appears; no design change |
| A2 | Three GitHub machine users exist (Architect, Developer, gateway) with classic `repo` PATs and collaborator access on target repos | Auth module swaps token source; §5 unchanged in shape |
| A3 | The owner can configure repository webhooks and branch protection | Without branch protection, FR-1.3 is untestable and M4 cannot pass |
| A4 | `/Users` is shared into containers by OrbStack (default) | Mount root becomes configurable; §6.4 relative worktree paths already remove the path-equality dependency |
| A5 | Container-internal `tmpfs` enforces UID ownership and mode | **Load-bearing.** Falsified ⇒ fall back to two containers (ADR-4 fallback). **Spike M2-B** (M2-A is the separate host-bind expected-fail test) |
| A6 | Each vendor agent CLI can run headless, non-interactively, and accept sequential turns over a long-lived stdio session (or one-shot `-p` as recovery) | Adapter owns the protocol; **process liveness is the fast path** (§6.3). Our `transcript.jsonl` + vendor session store + `-c` / ACP re-entry is the recovery path when the process dies (ADR-9 — vendor resume is an optimization, not a dependency) |
| A7 | The `~/.agentd` volume has ≥ 50 GB free at install | Circuit breaker trips immediately and the system refuses work — loudly, which is correct |

---

## 3. System Architecture

```mermaid
flowchart LR
    GH["GitHub<br/>Issues · PRs · Reviews · Checks"]
    TUN["Ingress tunnel<br/>Funnel / cloudflared / ngrok"]

    subgraph HOST["Mac mini M4 Pro — macOS + FileVault"]
        subgraph GW["agentd — Python 3.12 / FastAPI / LaunchAgent"]
            ING["Ingress<br/>HMAC · accept-and-queue"]
            DISP["Dispatcher<br/>FSM · budgets · routing"]
            SUP["Session Supervisor<br/>containers · worktrees · tokens"]
            REC["Reconciler<br/>GraphQL sweep · watermarks"]
            RES["Resource Governor<br/>disk · RAM · breaker"]
        end
        DB[("SQLite WAL<br/>~/.agentd/state.db")]
        REPOS["~/.agentd/repos/<br/>one shared clone per repo"]
        SESS["~/.agentd/sessions/<br/>worktrees · transcripts"]

        subgraph ORB["OrbStack"]
            subgraph C1["Issue container — owner/repo#42"]
                SUPV["agentd-runner (PID 1, root→setuid)"]
                UA["uid_architect<br/>token tmpfs 0400"]
                UD["uid_developer<br/>token tmpfs 0400"]
            end
        end
    end

    GH -->|webhook| TUN --> ING --> DB
    DISP <--> DB
    DISP --> SUP --> ORB
    REC -->|GraphQL + REST| GH
    RES --> DB
    DISP <-->|"JSON-RPC 2.0 / NDJSON"| SUPV
    SUPV --> UA
    SUPV --> UD
    UA -->|gh · architect PAT| GH
    UD -->|git push · gh · developer PAT| GH
    REPOS -.->|bind| C1
    SESS -.->|bind| C1
```

### 3.1 Component Responsibilities

| Component | Owns | Explicitly does not own |
|---|---|---|
| **Ingress** | HMAC verification, durable delivery persistence, sub-50 ms 200 | Any filtering or business decision |
| **Dispatcher** | Normalize → route → FSM transition → dispatch turn | Docker, GitHub writes |
| **Session Supervisor** | Container lifecycle, worktree provisioning, token delivery | Deciding *when* a session acts |
| **Reconciler** | Rebuilding derived state from GitHub, synthesizing missed deliveries | Repairing agent-internal state |
| **Resource Governor** | Disk/RAM sampling, breaker, admission control, GC | Killing in-flight turns |
| **`agentd-runner`** (in container) | RPC endpoint, privilege drop, transcript append, turn deadline | Container lifecycle, the sibling role's credentials |
| **Agent CLI** | Reasoning, GitHub actions under its own identity | Everything else |

---

## 4. Ingress

### 4.1 Transport

The tunnel is a **pluggable backend**, not architecture: Tailscale Funnel (default — stable public HTTPS hostname, no domain purchase, no other ports open), `cloudflared`, `ngrok`, or `smee`. Configured by `ingress.backend`.

The choice is deliberately low-stakes. Because the Reconciler (§11.2) independently sweeps GitHub on a timer, **a tunnel outage degrades latency, not correctness.**

### 4.2 Webhook Handling Contract

```
POST /webhooks/github
  1. Read body, 2 MiB cap.
  2. Verify X-Hub-Signature-256 (HMAC-SHA256, constant-time). Mismatch → 401, no persistence.
  3. INSERT OR IGNORE INTO deliveries(delivery_id = X-GitHub-Delivery, ..., status='queued').
  4. Return 200. Median target < 50 ms.
  5. Nudge the dispatcher. If the nudge channel is full, do nothing — the dispatcher's
     own scan of status='queued' will collect it.
```

Steps 3–4 are P7. GitHub does **not** automatically retry failed repository webhook deliveries — a failed delivery is recorded and must be redelivered manually via the UI or redelivery API. Returning 5xx is therefore equivalent to data loss, and it loses data precisely during the incidents the circuit breaker exists to handle. Every downstream failure — dispatcher crash, container failure, breaker open — leaves the delivery durably queued.

Subscribed events: `issues`, `issue_comment`, `pull_request`, `pull_request_review`, `pull_request_review_comment`, `check_suite`, `status`, `push`.

### 4.3 Intake Gating

```yaml
intake:
  mode: label            # label | all
  label: agentd
  actors: collaborators  # only issues opened by repo collaborators are ingested
```

`mode: label` is the default so a repository does not start spending model tokens on every issue filed. `actors: collaborators` is baseline rather than deferred hardening: it costs one permission check and closes the FR-3.3 escalation-spam vector on any repository that accepts outside issues.

**Session creation eligibility (M3 / #16).** A `sessions` row is created only from an **intake-passing `issues` open/reopen/labeled event**. Deliveries parked as `deferred` (other event types, or historical traffic from before the design loop existed) must not conjure sessions on their own — they may only attach to a session that already exists. Ingress still accept-and-queues everything (P7); the gate is on *promotion*, not on persistence. Historical backlogs may be moved to a terminal status via `agentctl quarantine-deferred` before enabling the design loop on a host that already held M1 deferred rows.

---

## 5. Identity, Credentials & Role Binding

### 5.1 Accounts

Three GitHub **machine user accounts**, each a collaborator on the target repositories, each holding a PAT stored in the **macOS Keychain** and read by the gateway only:

| Role | Example login | Keychain account | Purpose |
|---|---|---|---|
| Architect | `@huozheclaude` | `claude-bot` | Agent turns, Design PR open/merge, reviews as Architect |
| Developer | `@huozhegrok` | `grok-bot` | Agent turns, Design PR review/approval, implementation |
| **Gateway** | `@huozhegateway` | `gateway` | §8.5 escalation comments only — not agent turns |

Machine users rather than a GitHub App, because SRS §2 requires the identity string used in communications and webhook filtering to match a registered GitHub **username**; App bots surface as `app-name[bot]`. See ADR-7.

**Credential shape (honest, 2026-08-08).** On a **private repository owned by a personal account**, fine-grained PATs cannot target that repository from a collaborator identity, and personal repos have **no collaborator roles** (every collaborator has push). So all three tokens are **classic PATs with the `repo` scope** — account-wide, not per-repository. Token scope therefore **does not** enforce the FR-1.3 boundary; branch protection and the gateway's narrow write surface (escalation comments only) do. **Both are account controls (#57, ADR-36):** they bind *which account* approves, pushes or merges, and nothing about *which operator* drives an account. In a container the second gap is closed mechanically — the other role's credential is unreachable by construction (§5.2) — and on the host it is closed by protocol only (§5.5). M4-A remains the demonstration that the **account** boundary holds; the operator half is not a claim GitHub's record can settle, and §17's spike row says which half its evidence carries. Blast radius is bounded by *collaborations*: keep each machine user a collaborator only on repos that need it.

```bash
security add-generic-password -s agentd -a claude-bot -w <pat>
security add-generic-password -s agentd -a grok-bot -w <pat>
security add-generic-password -s agentd -a gateway -w <pat>
```

The gateway identity exists so §8.5 can speak without (a) unpausing itself as `owner_reply`, (b) failing to notify the owner of their own @-mention, or (c) resetting `consec_agent_turns` as owner traffic. It must appear in `bot_logins` so §9.1 never classifies it as a human collaborator.

### 5.2 Credential Delivery — the mechanism FR-1.3 rests on

This is the most consequential mechanism in the document, and it is the one that changed most during review.

**Constraint discovered in review:** POSIX ownership does **not** survive the macOS→Linux bind-mount boundary. macOS UIDs have no meaning in the container's user namespace, so OrbStack UID-maps bind-mounted files and in-container `chown` on them does not reliably persist. Any scheme that provisions `0400` token files on the host and mounts them in provides **no isolation at all**.

The general form of this constraint governs what may live where:

| Location | UID/mode enforceable | Contents |
|---|---|---|
| Host bind mount (`worktrees/`, `transcript.jsonl`, `context/`, `scratch/`) | **No** — both role UIDs can read everything | Code, transcripts, scratch |
| Container-internal `tmpfs` | **Yes** — created by a Linux process in the namespace that owns it | **Credentials only** |

**Mechanism.** Tokens are delivered over the already-authenticated control channel in the `session.init` call (and again on `session.resume`). The in-container root supervisor writes them to container-internal tmpfs before dropping privilege:

```
--tmpfs /run/agent:rw,noexec,nosuid,size=1m,mode=0711

  /run/agent/architect/token   mode 0400  owner uid_architect
  /run/agent/developer/token   mode 0400  owner uid_developer
```

Consequences: no token material on the host filesystem, in the image, in `docker inspect`, or in Time Machine; ownership is real because it is created inside the namespace that enforces it; and tokens vanish when the container stops.

**Model-provider credentials (project-scope amendment).** The mechanism above governs **GitHub PATs**. Model-provider credentials are a different shape — they are subscription login state, not immutable API keys, and they are **provisioned manually, up front** rather than derived at session create. The granularity differs by vendor and the difference matters when setting a host up: Claude's is minted **once for the host** and reused across projects; Grok's is minted **once per (project, role)**. Evidence for every clause below is on spike #19.

| | Claude | Grok |
|---|---|---|
| Credential | one-year token from `claude setup-token` | OIDC login state |
| Stored | Keychain `agentd/claude-oauth-token` | durable per-role `HOME` in the project tree (§6.2) |
| Delivered | env `CLAUDE_CODE_OAUTH_TOKEN` at process start | the role's own `HOME`, mounted |
| Minted by | owner, **once for the host** — `claude setup-token` yields one account-wide token, reused across projects | `grok login --device-auth`, **once per (project, role)** |

**Never copy a credential file.** This is the rule the spike was run to establish, and both halves were demonstrated:

- The on-disk `~/.claude/.credentials.json` was **stale** (expired) while the live credential sat in the macOS Keychain — so copying that file authenticates nothing.
- Refresh tokens **rotate**. A container refreshing from a copied chain invalidated the host's, logging the host CLI out. Two containers sharing one chain fight; last refresh wins and the others die.

What makes this safe is that the container unit and the credential unit now agree (ADR-4): each (project, role) owns its own chain, nothing is shared and nothing is copied, so no broker, no host refresh-owner, and no cross-container file locking is required. Independence was verified — three concurrent chains (two roles plus the host) each survived a forced refresh of the others.

Two properties to hold when implementing: select the credential by **adapter**, not by role, or §5.3's `role/architect:grok` override silently delivers the wrong provider's token; and note that a subscription credential is **account-wide**, unlike a repo-scoped PAT, so although it is exposed no more widely than the PATs already are (§13.2), its blast radius is larger.

**The boundary this creates is precise, and the ADR states it plainly: the two-UID split protects _credentials_, not _data_.** Both roles can read each other's worktrees and transcripts. That is accepted — both roles are already trusted with the repository. What must not cross is the ability of the Developer identity to produce an approval that branch protection accepts on its own PR.

**Identity preflight (added M0 — see ADR-11 at end of §16).** Cross-role isolation inside the container does not stop an operator from mapping the *wrong* PAT to a role in config/Keychain. A wrong-identity token silently defeats §9.1 (owner login resets turn budgets), §10.2 (owner-only checkbox), and §8.4 (Architect approval). On every `session.init` and `session.resume`, **before any turn is dispatched**, the runner calls `GET /user` with each delivered token and asserts `login == the configured GitHub identity for that role`. Mismatch ⇒ fail the session loudly, escalate to `@owner` (§8.5), dispatch nothing.

**Inbound detection net:** the gateway rejects and escalates any event where `sender.login == config.gateway.owner` **and** the body carries an `agentd:turn` provenance footer — a combination that is impossible under correct operation and is the signature of an agent acting with the owner token.

**Model credentials (subscription, #19/#20) — separate from GitHub PATs.**

| Provider | Delivery | Storage |
|---|---|---|
| **Claude** | Env `CLAUDE_CODE_OAUTH_TOKEN` at container create (from Keychain `agentd` / `claude-oauth-token`, minted via `claude setup-token`) | Not a file copy of interactive Keychain / `.credentials.json` |
| **Grok** | Durable `projects/<owner>__<repo>/home/<role>/` with independent `grok login --device-auth` (Option D) | Never copy host `~/.grok/auth.json` into N places |

Provision **both** providers per project; select by **adapter** at turn time so §5.3's `role/architect:grok` override does not require a re-mint. See `docs/ops/project-onboarding.md`.

### 5.3 Role Binding (FR-1.2)

Precedence, highest first:

```
issue label  >  <repo>/.agentd/config.yaml  >  ~/.agentd/config.yaml
```

Per-issue override uses labels so the binding is visible on the issue itself:

```
role/architect:grok        role/developer:claude
```

**Binding freezes when the Architect opens the Design PR** — the `PLANNING` → `DESIGN_REVIEW` transition. The resolved binding is written to the session row and `roles_locked` is set at that moment.

This is narrower than the position originally locked in review ("at design freeze", i.e. when the Design PR merges), and it is narrower for a reason found during review of this PR: between Design PR *open* and Design PR *merge* the Developer is actively reviewing, so a label edit in that window would re-resolve roles and invalidate both the Architect's in-progress PR and the Developer's review context. Freezing at the first `turn.dispatch` instead — the other proposal — overshoots in the opposite direction, closing the window within seconds of the issue opening.

Opening the Design PR is the right boundary because it is the last moment before any peer-review context exists, while still leaving the whole RFC-drafting turn (tens of seconds to minutes) available for a human to correct a mislabelled role.

**Adapter swap and long-lived CLI (#25).** Changing a role's adapter (label override or config) while a project runner is HOT kills that role's held CLI process and starts the new adapter. The vendor conversation for the prior adapter is discarded — deliberate: one process cannot speak two protocols. Cross-issue context for that role is then rebuilt from `transcript.jsonl` / vendor store on the recovery path (§14.5), not carried across the adapter boundary.

### 5.4 Configuration

```yaml
# ~/.agentd/config.yaml
host:
  root: ~/.agentd
  disk_floor_gb: 15
  disk_resume_gb: 20
gateway:
  listen: 127.0.0.1:8787
  owner: huozhe
  # Per-turn liveness pair (#34): RPC socket timeout must outlive the deadline
  # sent to the runner. rpc_timeout_s = turn_deadline_s + rpc_timeout_grace_s.
  turn_deadline_s: 900
  rpc_timeout_grace_s: 60
ingress:
  backend: tailscale-funnel     # | cloudflared | ngrok | smee

agents:
  claude:
    login: claude-bot           # must equal the GitHub username (SRS §2)
    adapter: claude-code
    credential: keychain://agentd/claude-bot
  grok:
    login: grok-bot
    adapter: grok-cli
    credential: keychain://agentd/grok-bot

repos:
  huozhe/code-workflow:
    default_roles: { architect: claude, developer: grok }
    merge: { method: squash, delete_branch: true }
    required_checks: [ "ci/test" ]

budgets:
  max_turns_per_issue: 40
  max_consecutive_agent_turns: 30   # raised from 12 on M4-4 (#58) evidence: a full
                                    # design+code loop is ~13 uninterrupted agent turns
  max_review_rounds: 6
  min_dispatch_interval: 10s
  fingerprint_repeat_limit: 3
  zero_thread_rounds_limit: 3
  silent_turn_limit: 3          # #39: consecutive turns with no public_actions

resources:
  max_hot_containers: 4
  session_memory: 3g
  session_cpus: 2
  idle_cold_after: 30m
  awaiting_verification_container_ttl: 7d

retention:
  archive_days: 30          # §10.5 / §12.3 / ADR-12

intake:
  mode: label
  label: agentd
  actors: collaborators
```

### 5.5 Host-Side Agent Work (#57)

FR-1.3's identity boundary is enforced two different ways, and conflating them is what made #57 read as a security gap when it is a collaboration protocol.

**In a container it is mechanical, and needs no rule.** Containers are Linux: the macOS Keychain is unreachable by construction — no `security` binary, no Keychain daemon, no mount. A role's credential arrives only at `/run/agent/<role>/token` (tmpfs, `0400`, owned by the role UID, §5.2), and `assert_bearer_not_readable_by_roles` and `adversarial_token_check` run on **every** container create. A containerised agent cannot use another identity's credential, so nothing has to ask it not to.

**On the host it is a protocol, because no mechanism is proposed.** Agents doing dev, testing, review or verification on the host run as the operator's user and can read the whole Keychain. The rule:

> **An agent working on the host uses only its own identity's credential. Any artifact attributed to an identity — a commit, a push, a review, an approval, a merge, an issue close — must be produced by that identity's operator.**

The prohibition is on **producing an artifact** under another identity, and it extends to read-only use: an agent does not invoke another identity's token to check something either, because the check is one flag away from the act. Where such a command is genuinely needed, **the owner runs it**, which is what the interactive `!` prefix is for.

*Recorded because a rule that exempts its author is worth nothing:* on 2026-08-20 the Architect ran `GH_TOKEN=$(gh auth token --user huozhe) gh api user --jq .login` to confirm the shape of an approval command before handing it to `@huozhe`. Read-only, no artifact, at the owner's request — and under this rule it should have been the owner's command. The approval itself was correctly left to `@huozhe`.

**No mechanism is proposed, deliberately.** A process that can read the Keychain can read all of it; separate macOS accounts or a token broker cost more than the risk warrants for host-side dev. This is stated so it is not re-derived as an oversight.

**#57's "put it in the turn prompt" is redirected, and the reason is the reader.** `build_prompt` (`turn.py:216`) reaches **only** containerised agents — precisely the population that cannot commit this violation. `_OBLIGATIONS` (`turn.py:133`) is keyed by `(role, session_state)` and is explicitly "keep short — prepended every turn", so a standing, state-independent rule does not belong there either. The agents that *can* violate this never read that prompt: they read the repository's own instruction files. **The rule therefore lands in this section and in the repository's own `CLAUDE.md`** — added by this change, since none existed — **not in the runner prompt.** `CLAUDE.md` is loaded into every host-side agent session in this working tree, which is exactly the population the rule binds. Adding it to `_OBLIGATIONS` would cost a line of every container turn to tell the wrong audience.

#### 5.5.1 A host agent standing in for an absent role

When a vendor quota or outage makes a role's agent unavailable, its work may be carried by a host-side agent under **the owner's** identity, subject to three conditions:

1. **It declares itself.** Every artifact it authors says so in its body — the identity is `@huozhe`, the author is an agent. GitHub records the identity, never the credential's operator, so the declaration is the only durable trace.
2. **It never closes a session issue.** §10.3 reserves that to the human owner, and the gateway's check is `sender == owner` — an agent wearing that identity satisfies the mechanical test while defeating the rule it encodes. Closure is a judgement about whether the work is verified, and it stays with the human.
3. **It does not stand in for the identity that is merely rate-limited.** A machine user's GitHub credential usually still works when its *vendor* quota is exhausted; using it anyway would make the audit trail assert that an agent acted which did not.

Two-party review is unaffected and is what makes the arrangement safe: `main`'s ruleset requires one approving review with `require_last_push_approval` and no bypass actors (§13), so the stand-in and the remaining agent still review each other's work, and neither can author and unblock the same PR.

#### 5.5.2 Demonstrations that turn on which identity acted

Where a live demonstration's claim *is* an identity claim — "the Developer could not approve its own PR" — record **how the action was produced**, not only that it happened. GitHub logs the identity, not the operator, so an **unstamped** artifact of this class cannot be re-checked from the database afterwards; the stamp defined below is what makes part of it checkable, and the two paragraphs after it say exactly how much. #55's spurious approval was caught only because the Architect noticed a review under its own name that it had not written. **#56's step 3 is settled by ADR-36 rather than re-run**, as the next paragraph sets out.

**#56's step 3 is redirected rather than re-run (ADR-36).** Its artifact is not void: review `4913183219` really was submitted by the `huozheclaude` account and the ruleset really did clear. What is void is the inference from those four wire rows to M4-A's sentence, which is an operator claim. A clean redo returns the same three values — `200`, `APPROVED`, `huozheclaude` — in a record with no field for who held the credential, so the redo produces a new artifact and no new knowledge. The record, not the redo, is what discharges the claim.

**The record has a format and a place.** An artifact whose claim is an identity claim carries its producing `turn_id` **in its own body** — the review body, the PR body, the comment body — which GitHub stores verbatim and serves for as long as the artifact exists. It is checkable against `turns` (`db.py:110`), whose `turn_id`, `role`, `started_at` and `ended_at` are written by the **gateway** and not by the agent, so a stamped artifact makes three independent conditions re-checkable: the turn exists, its `role` is the role the artifact claims, and the artifact's timestamp falls inside the turn's window. For a containerised turn that closes to near proof, since such a turn cannot have held the other credential at all (§5.2). The place a held-open identity acceptance waits is `docs/ops/live-sign-offs.md`, whose stated criterion — an acceptance no fixture can reach — is exactly this one.

**Two bounds, because a record that oversells itself is worse than none.** The stamp *prevents* nothing: a host agent under a borrowed credential can write any string into a body, and a determined one can name a real turn of the right role whose window contains its artifact. It converts an unfalsifiable claim into a three-condition one in a table the agent does not write, and that is the whole of the gain — §5.5's refusal of a preventive mechanism is unamended. And this is why the opening paragraph scopes *"cannot be re-checked from the database"* to the **unstamped** case instead of stating it flat: a stamped artifact can be re-checked, partly, and what remains genuinely uncheckable is the original case — an unstamped host artifact under a borrowed credential. Saying which is which keeps this from being read as licence to skip the record because nothing could verify it anyway.

**`turns.public_actions` is not the record, and the difference is load-bearing.** It is the obvious place to look and it is agent-authored: *"public_actions are claims (tool_use) — diagnostic, not a reset"* (`session_loop.py:1123`), *"same untrusted source as advisory `status`"* (`loop_safety.py:68`). An agent that would misuse a credential would also write that row. Only the gateway-written columns are worth cross-checking, which is why the stamp is checked against `role` and the turn window and against nothing the agent supplied.

---

## 6. Session Model & Zero Cold Starts

### 6.1 Identity and Scope

```
session_key  = "<owner>/<repo>#<issue_number>"   # agentd issue session (FSM, budgets)
project_key  = "<owner>/<repo>"                   # container / runner unit (#20)
```

One **agentd session** per issue (SRS §2). One **project runner** (container) per repo, living for the project's lifetime, holding one long-lived CLI conversation per role shared across issues in that project (#19 / #20).

**A session is not a container (project-scope amendment).** The container unit is the **project**, not the issue — see ADR-4. One project container hosts every session for that repository, so `sessions : runner` is N:1 rather than 1:1. Session identity, the §8.1 state machine, and §9's budgets and stall signals all remain **per issue** and are unaffected; only the process and filesystem substrate is shared.

### 6.2 Host Layout

**Project-scope amendment.** Session directories nest **under the project** rather than sitting flat. A project container mounts exactly one directory — its own project tree — and nothing else. The flat `sessions/<owner>__<repo>__42/` layout had no directory containing exactly one project's sessions, so a project container would have had to mount `sessions/` and thereby expose *every* project to *every* container: the same failure as the M2 W2 finding, where mounting a parent that contained more than intended re-exposed credentials.

```
~/.agentd/
├── state.db                                 # SQLite, WAL — NEVER mounted (W2)
├── config.yaml                              #             — NEVER mounted (W2)
├── projects/<owner>__<repo>/                # ← the single mount for this project's container
│   ├── repo/                                # ONE clone per repo, gateway-owned, gc.auto=0
│   ├── sessions/42/
│   │   ├── architect/{transcript.jsonl,context/,scratch/,worktrees/}
│   │   └── developer/{transcript.jsonl,context/,scratch/,worktrees/}
│   └── home/{architect,developer}/          # durable per-role HOME + auth chain (§5.2)
└── archive/<owner>__<repo>/<issue_num>.tar.gz   # post-teardown; format & retention: ADR-12
```

The container mounts `projects/<owner>__<repo>` at `/srv/agentd`, and **that single mount is what preserves the M2 W1 fix**: `repo/` and `sessions/` stay siblings under one parent, so `worktree.useRelativePaths` gitdirs resolve to the same relative depth on host and container. Splitting them into separate mounts flattens the topology differently on each side and breaks `git` inside the worktree. Keep `assert_worktree_usable` (both role UIDs) and `assert_host_secrets_not_mounted` (an **allowlist** of `repo` + `sessions` + `home`, never a denylist) running on every container create — those two assertions are what caught W1 and prevented W2 from recurring.

### 6.3 The Three Cold-Start Costs

SRS §2 prohibits three distinct costs. Each needs its own mechanism; conflating them is how a design ends up asserting zero cold start rather than achieving it.

| Prohibited cost | Mechanism |
|---|---|
| **Re-cloning the repository** | One shared clone per repo. Sessions get `git worktree add`, which is O(working tree) and shares the object store. A new branch workspace costs ~1 s. |
| **Re-ingesting the codebase** | The working tree and all tool caches (`node_modules`, language-server indexes) persist on the host volume across every container tier, including a fully stopped container. |
| **Losing conversation state** | `transcript.jsonl` is appended after every turn and is the authoritative record. The agent process may exit; the conversation does not. |

The third was originally stated as: *continuity is achieved by persistence, not by process liveness* — on the grounds that an always-attached process is fragile and holds RAM hostage.

**Project-scope amendment: process liveness is the fast path, persistence is the recovery path.** Both survive; only their ranking changed. `agentd-runner` (PID 1, inside the container) spawns one CLI process per role and owns its stdio for the life of the project, because the vendor CLIs carry conversational state that a fresh process per turn discards. When that process dies — crash, `docker stop`, host reboot — continuity falls back to exactly the original mechanism: the CLI's own session store plus `transcript.jsonl`, re-entered with `claude -c` / `grok -c`.

The two objections to an always-attached process were tested rather than assumed (spike #19):

- *"one crash loses everything"* — it does not. `docker stop` → `start` → `-c` restored conversational context on both CLIs. The persistence layer is still there underneath.
- *"it holds RAM hostage"* — measured idle RSS after a turn is ~246 MB (claude) and ~70 MB (grok), so ~316 MB for both held. Against the 3 GB per-container cap in §6.6 that is not a constraint.

Persistence therefore still permits the tiering in §6.5; it is simply no longer the *first* mechanism reached for.

### 6.4 Git Layer

**Invariant:** only `agentd` runs `git fetch`, `git worktree add/remove`, and `git gc` on the shared clone, serialized by a per-repo mutex. Agents operate strictly inside their assigned worktree. `gc.auto=0` is set; maintenance runs only when a repo has zero active sessions. This removes the only genuinely unsafe concurrent git operation while leaving the safe ones (object writes, per-branch ref updates) unrestricted.

**Amended 1.30.0 (ADR-31, #171): the exclusivity claim was never true of `fetch`, and is not true of ref updates.** Agents run `git fetch` inside their worktrees — a linked worktree shares the clone's `.git`, so those fetches update the shared remote-tracking refs, and ADR-31's ranking argument depends on exactly that. Agents also *read* `refs/heads/*` (`git diff main...HEAD`, `git log main`) and can *check out* a shared branch (`git switch main`) from inside a worktree; ADR-31 (d)'s skip-with-WARNING path exists precisely because they do. **The invariant that holds is narrower:** only agentd runs `git worktree add/remove` and `git gc`, serialized by the per-repo mutex. `git fetch` and ref updates from worktrees are **unserialised** and concurrent with the gateway's — so "per-branch ref updates" above is describing what is *unrestricted*, not what is *safe*. This matters beyond wording: ADR-23's `gc` runs under `admit_lock` (`gc.py:291`, `:380`) on the strength of this paragraph, and it races agent fetches that nothing records.

**Worktree paths are relative.** The base image pins git ≥ 2.48 and the shared clone sets `worktree.useRelativePaths = true`, so worktrees do not encode absolute paths and host/container path equality is not load-bearing. Path-identical bind mounts remain documented as the fallback for older git.

**Runbook consequence that survives either mechanism: do not rename the Mac user account.** Doing so invalidates every mount path in flight.

### 6.5 Container Lifecycle

| Tier | Mechanism | RAM | Resume | Trigger |
|---|---|---|---|---|
| **HOT** | running project runner, RPC attached | ≤ 3 GB | 0 | active turn on any issue in the project |
| **COLD** | `docker stop` | **0** | 2–5 s | idle > 30 min, or admission pressure |

Tiering is **per project**, not per issue. Issue close does **not** stop the container (§10.3) — only worktrees/branches/scratch for that issue are removed. A `docker pause` ("WARM") tier must never be cited as a memory saving.

COLD: no re-clone, no re-ingest, no lost role conversation — process restart reloads CLI session store + transcripts (`-c` / ACP resume).

**Project-scope amendment: tiers apply to projects, not issues.** Stopping a container stops work on every issue in that project, so demotion is driven by project-level idleness rather than per-issue activity. Admission (`max_hot_containers`) counts projects. Note that §6.5's cap must be enforced on **promotion** as well as creation — promotion is the normal route to HOT under this model, so a cap checked only at create does not bound anything.

### 6.6 Memory Budget

**Project-scope amendment.** The unit is a project container holding **two** long-lived CLI processes (one per role), not an issue container holding one at a time.

| Consumer | Reserved |
|---|---|
| macOS + user applications | ~7.0 GB |
| OrbStack VM base | ~1.5 GB |
| `agentd` (Python/FastAPI) + tunnel client | ~0.2 GB |
| 4 HOT **project** containers @ 3 GB cap | 12.0 GB |
| **Total** | **~20.7 GB** |
| **Headroom** | **~3.3 GB** |

Typical working set is 1–2 GB; 3 GB is a hard `--memory` cap. Both role CLIs are now resident simultaneously rather than one at a time, but measured idle RSS is ~246 MB (claude) + ~70 MB (grok) ≈ **316 MB**, so holding both costs about a tenth of the cap and the reservation is unchanged. P8 still serializes *turns*; what it no longer implies is that only one CLI is *resident*.

The reservation is also now per **project** rather than per issue, which is strictly cheaper: a project with twelve open issues consumes one container's budget instead of twelve. Projects beyond `max_hot_containers` are held COLD and promoted on demand; concurrency is bounded by disk, not RAM. **Not yet true, and ADR-34 records why rather than leaving it aspirational:** nothing demotes, so no slot is ever freed, and a project that has never had a container cannot be held COLD at all — it is refused at create, before there is anything to stop. Until M6-3 supplies a demotion writer, the cap is a wall rather than a rotation.

---

## 7. Container Definition

### 7.1 Image

```
agentd/session-runner:<v>
  debian:bookworm-slim
  + git ≥ 2.48, gh, ripgrep, fd, build-essential
  + node LTS, python3
  + agent CLI adapters (claude-code, grok-cli, …)
  + agentd-runner  (static supervisor, PID 1)
  + users: uid_architect(1001), uid_developer(1002)
  - NO setuid binaries
```

`agentd-runner` is the stable contract; vendor CLIs are replaceable adapters behind it.

### 7.2 Run Configuration

```bash
docker run -d \
  --name agentd-huozhe-code-workflow-42 \
  --label agentd.managed=true \
  --label agentd.session='huozhe/code-workflow#42' \
  --restart unless-stopped \
  --memory 3g --memory-swap 3g --cpus 2 --pids-limit 1024 \
  --cap-drop ALL \
  --cap-add CHOWN --cap-add FOWNER --cap-add SETUID --cap-add SETGID \
  --security-opt no-new-privileges \
  --tmpfs /run/agent:rw,noexec,nosuid,size=1m,mode=0711 \
  -v ~/.agentd/repos:/srv/agentd/repos \
  -v ~/.agentd/sessions/huozhe__code-workflow__42:/srv/agentd/sessions/huozhe__code-workflow__42 \
  -e AGENTD_SESSION_DIR=/srv/agentd/sessions/huozhe__code-workflow__42 \
  agentd/session-runner:1.1.0
```

Non-obvious choices:

- **No `/var/run/docker.sock`. Ever.** Mounting it would grant host root and the sibling role's credentials, dissolving every boundary in §13.
- `--restart unless-stopped` lets containers survive an OrbStack or host restart on their own; the Reconciler then adopts or prunes them by label. This is the container half of NFR-1.1a.
- The tmpfs at `mode=0711` lets each role traverse to its own token directory without listing the sibling's.
- **Single project mount (W1/W2, #20).** Mount exactly `projects/<owner>__<repo>` → `/srv/agentd` so `repo/`, `sessions/`, and `home/` are siblings (W1 relative gitdirs). Do **not** mount the whole `~/.agentd` root — that exposes `state.db` / `config.yaml` (W2 / R1).
- **Capabilities (M2 amendment).** `--cap-drop ALL` alone makes `chown` and `setuid` return EPERM even for UID 0 under OrbStack/Linux, which makes §5.2 token placement and §7.3 privilege drop impossible. Re-add only `CHOWN`, `FOWNER`, `SETUID`, `SETGID`. No `SYS_ADMIN`, no `NET_ADMIN`, no docker socket.
- **RPC bearer delivery (M2 amendment, R1).** The bearer is **not** in container env (`docker inspect`) and **not** on a bind mount (roles can read all bind-mounted files — §5.2 table). Sequence: `docker create` → `docker cp` host-minted bearer to container-local `/etc/agentd/rpc.bearer` (root-owned `0400`, real Linux DAC) → `docker start`. Runner refuses to bind if the file is missing. Assert both: absent from inspect Env, and unreadable as either role UID.
- Egress is unrestricted by default (GitHub, model APIs, package registries). An allowlisting egress proxy is noted in §13.3 as hardening, not baseline.

### 7.3 Privilege Model

```
PID 1  agentd-runner (root)
   │   - binds RPC endpoint
   │   - receives tokens via session.init, writes /run/agent/<role>/token
   │   - owns stdio of one long-lived CLI child per role (§6.3 / #25)
   │   - NEVER executes agent or tool code as root
   └── per role (session.init / resume, and on crash respawn):
         Popen(user=uid_<role>, group=uid_<role>, extra_groups=[], start_new_session=True)
         (ADR-35: own process group, so the kill path can signal the group)
         cwd = project root (/srv/agentd); worktree path is named per turn
         env: HOME=<durable project home>/<role>  (or session home fallback)
              TMPDIR=<session>/<role>/tmp   (mode 0700)
              XDG_*=<session>/<role>/xdg
         turns multiplex over the held pipe under the per-(project, role) lock
   └── recovery / mock / AGENTD_CLI_MODE=oneshot:
         per turn: fork → setgroups([]) → setgid/setuid → run -p adapter
```

**Timing change (#25).** Privilege drop moved from *per turn* to *per session spawn*. The §7.3 invariants are unchanged: the CLI never runs as root; correct per-role uid; no supplementary groups; `HOME` / `TMPDIR` / `XDG_*` under the role's tree. A wedged turn is bounded by a per-turn deadline on the shared pipe; on expiry the runner kills the child so the role remains usable, then respawns with `-c` / store re-entry on the next dispatch. **Amended by ADR-35 (#172): that kill did not work.** Pid 1 is root without `CAP_KILL` and the child is uid 1001/1002, so `send_signal` raised EPERM, the bookkeeping had already been cleared, and the deadline bounded nothing — it spawned a second CLI beside the runaway. The runner now signals from a **forked helper that has become the role uid**, kills the child's **process group** (spawn sets `start_new_session=True`, without which the group is the runner's own), clears state only after `proc.poll()` is not `None`, and surfaces a failed kill to the caller instead of a swallowed warning.

Every subprocess of a turn — the agent CLI, `git`, `gh`, test runners, package managers — runs as the role UID. Per-role `TMPDIR` at `0700` is required, not optional: `/tmp` at `1777` prevents cross-UID *deletion* but not cross-UID *reading*, and CLIs routinely cache credentials into their default temp path.

---

## 8. Workflow Protocol & State Machine

### 8.1 States

```mermaid
stateDiagram-v2
    [*] --> INTAKE: issues.opened (intake gate passed)
    INTAKE --> PLANNING: roles resolved
    PLANNING --> DESIGN_REVIEW: Architect opens Design PR (roles freeze)
    DESIGN_REVIEW --> DESIGN_REWORK: Developer requests changes
    DESIGN_REWORK --> DESIGN_REVIEW: Architect pushes revision
    DESIGN_REVIEW --> DESIGN_APPROVED: Developer approves
    DESIGN_APPROVED --> IMPLEMENTING: Architect merges Design PR
    IMPLEMENTING --> CODE_REVIEW: Developer opens Feature PR
    CODE_REVIEW --> CODE_REWORK: Architect requests changes
    CODE_REWORK --> CODE_REVIEW: Developer pushes fixes
    CODE_REVIEW --> MERGING: gateway verifies approval + checks
    MERGING --> AWAITING_VERIFICATION: Developer merges, deletes branch
    AWAITING_VERIFICATION --> IMPLEMENTING: further work requested
    AWAITING_VERIFICATION --> TEARDOWN: issues.closed (by human)
    TEARDOWN --> CLOSED

    PLANNING --> PAUSED_HUMAN: escalation
    CODE_REVIEW --> PAUSED_HUMAN: budget exhausted / no progress
    PAUSED_HUMAN --> PLANNING: owner replies
    PAUSED_HUMAN --> CODE_REVIEW: owner replies
    CLOSED --> [*]
```

Orthogonal conditions held as columns rather than states so they compose: `PAUSED_RESOURCE` (breaker open), `FAILED`.

**`AWAITING_VERIFICATION` is re-enterable.** Multiple PRs may land under one issue; only closure ends the session. A Feature PR opened while awaiting verification returns the session to `IMPLEMENTING`.

### 8.2 Happy Path

```mermaid
sequenceDiagram
    autonumber
    participant H as Owner
    participant GH as GitHub
    participant D as agentd
    participant A as Architect
    participant V as Developer

    H->>GH: Open issue (intake gate)
    GH->>D: issues.opened
    D->>D: resolve roles, create session, start container
    D->>A: turn.dispatch(issue_opened)
    A->>GH: push design branch, open Design PR
    Note over D: role binding freezes here
    D->>V: turn.dispatch(design_pr_opened)
    V->>GH: review — request changes
    D->>A: turn.dispatch(design_changes_requested)
    A->>GH: push revision
    D->>V: turn.dispatch(design_revised)
    V->>GH: APPROVE Design PR
    D->>A: turn.dispatch(merge_design)
    A->>GH: merge Design PR
    D->>V: turn.dispatch(design_approved)
    V->>GH: worktree, implement, open Feature PR
    D->>A: turn.dispatch(feature_pr_opened)
    A->>GH: inline review comments
    V->>GH: push fixes
    A->>GH: APPROVE Feature PR
    D->>D: verify approval on head SHA + required checks + mergeable
    D->>V: turn.dispatch(merge_authorized)
    V->>GH: gh pr merge --squash --delete-branch
    A->>GH: write Verification Protocol block into issue body
    Note over D,GH: AWAITING_VERIFICATION — agents idle, container → COLD
    H->>GH: tick "- [x] Human Verification Complete"
    GH->>D: issues.edited — recorded, triggers nothing
    H->>GH: close issue
    GH->>D: issues.closed
    D->>V: session.teardown (developer worktree, branch, scratch)
    D->>A: session.teardown (architect worktree, branch, scratch)
    D->>D: archive session dir, purge, mark CLOSED
    Note over D: project container stop/rm only if no other live session (§10.3)
```

### 8.3 Design PR Merge Actor

**Developer approves; Architect merges.**

Worth stating explicitly because it is an extension beyond the SRS: FR-2.2/FR-2.3 require the Design PR to be *opened and approved*, not merged. Merging is this design's addition, justified because it places the approved RFC on the default branch so the Feature PR's base contains it. The Architect merges because the Architect owns the design artifact, and because "Developer merges after its own approval" is exactly the pattern branch protection exists to prevent.

### 8.4 Merge Authorization (FR-2.6)

The Developer merges, but only after **`agentd` independently verifies** against the GitHub API:

1. A review with state `APPROVED` from the account bound to Architect, **on the current head SHA**.
2. All `required_checks` are `success`.
3. `mergeable_state == "clean"`. A `blocked` state is classified before it is retried: unresolved review threads make it permanent regardless of check state, since retrying cannot clear a thread nobody resolved; only an unresolved-thread-free `blocked` still falls back to checks state as before (ADR-26).
4. **The PR body carries no closing keyword aimed at a session issue** — and if it does, the gateway **defuses it before authorising** rather than refusing (ADR-24). Rewrite the keyword to a plain reference (`Closes #32` → `Refs #32`), leave every other character intact, log it, then emit `merge_authorized`. **A failed PATCH refuses the authorization and escalates**, because the alternative is authorising a merge already known to close the issue.

**A verification failure is classified three ways, not two (ADR-38).** *Transient* leaves the delivery deferred and retries. *Permanent* escalates — no review from the counterpart at all (a review by anyone else is the same outcome, not a separate one), `dirty`, a required check concluded `failure`, `blocked` with unresolved threads, a failed step-4 defuse. ***Superseded*** does neither: the counterpart's latest verdict is `CHANGES_REQUESTED` or `DISMISSED`, or an otherwise valid `APPROVED` sits on a SHA that is no longer the head. A later event replaced this delivery's premise and that event has its own FSM kind, so the delivery is **dropped** — not merged, and not escalated. `COMMENTED` and `PENDING` are not verdicts and never displace an `APPROVED`.

Step 4 is not symmetric with 1–3, and the asymmetry is the point. The first three are *observations* the gateway makes and GitHub's branch protection independently enforces; the fourth is a *write* the gateway performs, because nothing in GitHub enforces it. §13.1 asserted "no component calls the close API" and was silently defeated twice by a PR body doing the closing (#32, #81) — see ADR-24 for the evidence and for what this deliberately does not cover (commit-message keywords, and a body edited after authorisation).

The gateway verifies; the agent acts. Agents never self-certify a privileged transition. This resolves the enforcement gap present in the Phase 1 drafts — where a gateway "safety check" was specified but the agent called GitHub directly, leaving the gateway outside the write path — without proxying every API call (ADR-8).

### 8.5 Escalation (FR-3.3, FR-3.4)

An agent invokes `escalate.human` with a reason and a specific question; the gateway also raises escalations itself on budget exhaustion or stall detection (§9). Then:

1. Session `paused_reason` set; dispatch stops.
2. Comment posted **as the gateway identity** (Keychain `agentd` / `gateway` — never an agent PAT) tagging `@<owner>` with the question, current state, and what each plausible answer would cause. Body carries `<!-- agentd:escalation session=… -->` so routing drops the echo for every agent recipient.
3. Escalation recorded with the comment id.
4. Resume on the next `issue_comment` from the **owner** (aligned with §9.1: while `PAUSED_*`, non-owner senders defer), injecting the reply as the next turn's event and restoring the pre-pause state.

**Escalation is for authorization that cannot be *established*, never for a counterpart verdict the FSM already has a transition for (ADR-38).** A pause costs an owner round-trip for work no human input is required for, and it parks every pending delivery behind it.

P5: no failure mode ends in silence.

---

## 9. Event Routing & Loop Prevention (FR-4.1)

### 9.1 Routing Rules

"Filter out bot self-messages" cannot mean "drop all bot-authored events" — the entire protocol *is* bot→bot messaging. Dropping by sender halts the workflow after a single turn. The rules are evaluated **per recipient**:

| Condition | Action |
|---|---|
| `sender.login` == the intended recipient's identity | **Drop** (self-echo) |
| `sender.login` == owner | **Route**, reset `consec_agent_turns` to 0 |
| Body contains `<!-- agentd:escalation … -->` (gateway voice, §8.5) | **Drop** for every recipient |
| Body contains `<!-- agentd:gateway … -->` (any other gateway comment, #85) | **Drop** for every recipient |
| `sender.login` is the other agent bot | **Route**, increment `consec_agent_turns` |
| Body contains a provenance footer written by the recipient | **Drop** (own artifact) |
| `delivery_id` already terminal | **Drop** (redelivery) |
| Session `PAUSED_*` and sender is not the owner | **Defer** (stays deferred, with backoff — ADR-38) |
| `sender.login` is the gateway login (no escalation marker) | **Drop** (gateway is not a human collaborator) |

Every agent-authored comment carries a machine-readable footer; gateway comments carry a marker. Escalations keep `agentd:escalation` so §8.5 unpause stays distinct; every other gateway comment uses `agentd:gateway`. Both make drop rules mechanical:

```html
<!-- agentd:turn session=huozhe/code-workflow#42 role=architect turn=01J8Z… -->
<!-- agentd:escalation session=huozhe/code-workflow#42 -->
<!-- agentd:gateway session=huozhe/code-workflow#42 -->
```

Owner quote-replies that copy either footer still **route** — owner is evaluated before marker rules (same trap as M3-2).

**Two further drops are *not* rows in this table** (ADR-30, #173). The table is first-match per recipient, and both of these run in `_process_one` **before** `route_for_recipient` is called at all — so ordering them against peer-bot Route or the `PAUSED_*` defer does not arise:

- **An agent's own PR-scoped events.** `pull_request_review` (any action), `pull_request_review_comment`, and `issue_comment` on a PR are dropped when `sender.login` is that PR's own author **and is not the owner**, beside the review-part drop and before budget, `_event_kind`, the FSM, and recipient selection. The owner exception is not a courtesy: this drop runs above `route_for_recipient`, so without it the owner row in the table above never evaluates for a PR the owner authored. Being ahead of the `PAUSED_*` defer is the point, not an accident: replies parked through a pause drain on unpause and would otherwise become turns then.
- **A spent PR-scoped event.** A PR-scoped event whose PR is `merged` or `closed` at drain time, and which is not itself the merge, records its FSM transition and then dispatches no turn — a sibling gate beside the terminal-state gate, **not** a widening of it.

**`pull_request` events are excluded from the author rule, and the exclusion is load-bearing.** `opened`, `synchronize` and `closed` are *always* sent with the PR's author as sender — only the author can push to their own head — so a rule phrased as "any PR-scoped event from the PR author" drops every rework and ends the loop after one review round. `design_revised` / `feature_revised` are that event's kinds and are observed progress (§9.3). The author rule names three events and no others; `push` is not one of them either.

### 9.2 Budgets

Human input resets the consecutive-turn counter; agent activity never does. Defaults in §5.4. Breach → **escalate, not abort** — work in progress stays intact and reviewable.

### 9.3 Stall Detection

Two agents can burn budget while converging on nothing, and this is the characteristic failure mode of the architecture. Two complementary signals, because each misses what the other catches:

**Fingerprint** — recomputed each turn from *progress state*, **not** including `head_sha` in the hash (M3 amendment — including head made "identical after head change" unreachable):

```
sha256( sorted(open_review_thread_ids)
      ‖ unresolved_comment_count
      ‖ sha256(git diff --stat <base>..<head>) )
```

Identical 3 **consecutive turns** → escalate, but only **after the session has seen at least one non-trivial `head_sha` change** (arms the counter). That avoids firing on legitimately slow initial convergence, while still catching cosmetic re-pushes that move head without changing review/diff state. A static head never advances this counter — that is intentional; other signals cover it.

**Zero-thread-progress** — 3 consecutive review rounds in which no review thread is resolved → escalate. Catches agents that make cosmetic changes each round, which the fingerprint's diff component alone can miss.

**Silent turns** (#39, PR #42) — 3 consecutive agent turns with **no observed GitHub/FSM progress** → escalate. Reset only on P1-observed progress (webhook-driven FSM or progress event kinds such as `design_revised`), never on claimed `public_actions` (tool_use is advisory, like `status` in §14.3). Claims are still recorded and named in the escalation when the agent reported acting but nothing arrived. Adapters populate `public_actions` from vendor tool streams for diagnosis (including `git push` → `push`).

**Coverage note for implementers.** The "arm after first head change" guard is **not** "only sample when head changes this turn." Once armed, count every turn's fingerprint. Do not re-introduce `head_sha` into the hash material: that makes identical progress after a cosmetic push impossible to observe. Comment-only loops before any code exists (two agents negotiating an RFC without pushing) are caught by the **turn budgets** and the **zero-thread-progress** signal, both of which are head-agnostic. Silent turns cover the case where there are no review rounds and no head at all. The mechanisms are deliberately layered so that each covers the other's blind spot.

Spurious escalation is the preferred failure direction; all signals escalate to the human rather than aborting work.

### 9.4 Event Digest

Agents receive a compact rendered digest — event kind, actor, human-readable delta, and *references* to files and URLs rather than inlined content. Raw payloads stay in SQLite for the gateway. This keeps per-turn token cost roughly constant as issue history grows.

---

## 10. Human Verification & Closure

### 10.1 The Managed Block (FR-3.1)

On `feature_merged → AWAITING_VERIFICATION`, the **gateway** upserts a sentinel-delimited verification block into the **issue body**. The Architect does not own the first write: if only an agent turn wrote the block, a failed turn would leave every subsequent close classified `ABANDONED` (§10.3) for want of a checkbox that never existed. That is the same reliability reason escalation comments and non-owner reopen are gateway writes.

The gateway writes a scaffold (merged PR numbers from the session row, placeholder steps, unchecked box, ordering line). On the post-merge turn the **Architect** may refine **steps** and **Not covered** between the sentinels; it must not remove the checkbox or the ordering line.

```markdown
<!-- agentd:verification v1 -->
## Verification Protocol

1. `git pull origin main && npm ci`
2. `npm run dev`, open http://localhost:3000/settings
3. Toggle "Dark mode" — preference must survive a page reload.
4. `npm test -- settings` — 14 tests pass.

**Merged PRs:** #43, #47
**Not covered:** SSO login path (no test account available)

- [ ] Human Verification Complete

*Tick this box before closing the issue — closing with it unticked records the session as `ABANDONED` (§10.3).*
<!-- /agentd:verification -->
```

Sentinels exist so the **gateway and agents** can rewrite the block idempotently (across redelivery and across multiple PRs) without touching the human's prose. `agentd` parses only between sentinels; a stray checkbox elsewhere in the body is ignored. Gateway upsert preserves an already-checked box unless an explicit override is passed.

**The ordering line is part of the block, not decoration.** Ticking and closing are two separate human acts, and the natural order — close the issue, tick later — silently produces the wrong terminal classification, because `issues.closed` is evaluated against the checkbox state *at that moment* (§10.3). The block therefore states the required order where the human is already reading. Found by hand-running this block against M2 (#10), which closed unticked and so recorded as `ABANDONED`.

### 10.2 Checkbox Semantics (FR-3.2)

The checkbox **records a fact; it authorizes nothing.** On `issues.edited`, a `- [ ]` → `- [x]` flip on the `Human Verification Complete` line inside the sentinels is recorded as verified **only if** `sender.login == config.gateway.owner` **and** `sender.login` is not a configured agent identity.

**Removal by the owner withdraws it, exactly as an untick does** (`@huozhe`'s call, 2026-08-14; ADR-19). An owner edit that **removes** a ticked `Human Verification Complete` line from inside the sentinels — the line deleted, or the block gone — clears `verified_at`. Before this, only a `- [x]` → `- [ ]` flip cleared it, so a deleted box left the record standing, and ADR-16 would then permit a restore to raise a checkbox the owner had removed.

**Removed by *this* edit, not merely absent now.** The test is `changes.body.from` — the before-image must carry the tick — exactly as every restore branch in §10.2 already decides. "Absent now" is the wrong test and its failure is silent: an agent deletion leaves the box missing while ADR-16's restore is still in flight, so the owner's next edit to any other part of the issue would discard a verification they never touched, and the following close would record `ABANDONED` on a verified session. That is the outcome ADR-19 exists to prevent, reached from the other side. Found on the Feature PR (#104) by probing the shipped branch, and the loose wording here is what invited it.

The second condition is not redundant with the first — it survives a configuration mistake in which an agent identity is also listed as owner. Under this design the checkbox is a data-integrity control rather than a security gate, because closure (§10.3) is the human's own act; it is checked twice anyway because a falsified verification record is worth preventing cheaply.

If an agent edits the issue body while `AWAITING_VERIFICATION`, the restore is *decided* from that edit's own before/after image (`changes.body.from` vs. the payload's `issue.body`, PR #65) but *composed* against a fresh read of the issue taken immediately before the write (ADR-15) — never against the payload's after-image, which can be stale by the time the delivery drains. A warning comment is posted for every restore that PATCHes, with `<!-- agentd:gateway session=… -->` so routing drops it (#85).

**No restore may raise the checkbox without a second signal.** `changes.body.from` does not say *who* made the prior tick. A composition that raises the box needs `sessions.verified_at`; a composition that lowers or preserves it never does (ADR-16). Restore-up and B4-with-`was=True` refuse and leave the body. B2 with a ticked `prev_block` refuses the raise and reinserts the block unchecked — otherwise the issue has no gate and §10.3 classifies `ABANDONED`. B1 (already-checked before and after a step-refine, including a missed owner-tick delivery) is unchanged.

### 10.3 Closure and Teardown (FR-3.2, FR-4.2)

**The human closes the issue. `issues.closed` is the sole teardown trigger.** No LLM agent and no orchestrator component ever calls the close API.

This overrules the agents' unanimous preference for orchestrator-initiated close. The agents' argument was that a second manual action is friction without safety once the checkbox is owner-only — but that reasoning assumes checkbox verification is perfect. Requiring the human to perform the terminal action removes an entire failure class ("the gateway misread the checkbox") at the cost of one click.

The checkbox's only remaining job is to classify the terminal state:

| At close | Terminal state | Teardown |
|---|---|---|
| Checkbox ticked **and** `sessions.verified_at` set | `VERIFIED` | Full, plus completion summary comment |
| Checkbox absent or unticked, `verified_at IS NULL` | `ABANDONED` | Full, reason recorded, no summary |
| Checkbox ticked, `verified_at IS NULL` after the grace window | **none — not classified** | **None.** Escalate, lower the box, leave the issue closed (ADR-16, ADR-17) |
| Checkbox absent or unticked, **`verified_at` set** | after the grace window, re-read the issue: box back up → `VERIFIED`, still down → `ABANDONED` (ADR-19) | Full, per the outcome |

**The third row is the close-time half of ADR-16.** The body is not authority for `VERIFIED`; an owner tick this gateway observed is. A ticked body with no such record means a tick arrived by a route the restore gate does not cover, or the owner's `issues.edited` delivery was lost — indistinguishable here, so nothing is classified, nothing is torn down, and §8.5 tells the owner. The remedy is the owner's, in order: **reopen, tick, close**. Full rule, grace window, and the rejected alternatives: ADR-17.

**The fourth row is the mirror, and it does not move this section's ruling.** An agent untick that has not been restored yet, plus a close, records `ABANDONED` on a session the owner verified — the same race as the third row, pointing the other way. The answer keeps the checkbox as the authority and only changes *when* it is read: hold the delivery for the same grace window, then re-read the issue and classify from that. `verified_at` is the trigger for the re-read, never the verdict (`@huozhe`'s call, 2026-08-14). **Either held row stops entirely if the issue is `open` at the re-read** — a reopen supersedes the close, and closing again produces a fresh delivery. Full rule: ADR-19.

The rows that classify tear down **issue artifacts** completely (worktrees, branches, scratch, issue session dirs); the third tears down nothing, by construction. Under project-scoped runners (#20) the **project container is not an artifact of the issue** — issue close must not `docker stop`/`rm` a container still serving other issues. Container teardown is project-level (last session gone, or explicit project archive).

There is no timer, no hold, and no attempt to reopen an issue the owner closed — a close without verification is a meaningful human act ("won't fix", "fixed another way"), and the system records it rather than arguing with it.

**Ordering is normative: tick, then close** (`@huozhe`'s call, 2026-08-07). The classification is evaluated against the checkbox state at `issues.closed` and is never revised afterwards — a tick arriving after closure changes nothing. The alternative considered was making a late tick promote `ABANDONED` → `VERIFIED`, and it was rejected: it would reopen the terminal state after teardown has already run, which is precisely the "no timer, no hold" property above. The cost is that the ordering must be *communicated*, which is why §10.1's block carries it inline rather than leaving it to the runbook.

### 10.4 `AWAITING_VERIFICATION` Retention

Because closure now happens on a human's schedule, this state is unbounded. Holding containers for it is indefensible.

| Elapsed | Action |
|---|---|
| Idle timeout (30 min) | Container → COLD. RAM freed, volumes intact. |
| **7 days** without closure | Container removed. **Worktree, transcript, and summary retained on disk.** One quiet issue comment noting the session is disk-only until closed. |
| Any event (comment, reopen, close) | Rehydrate on the same mounts in ~5 s — no re-clone, no re-ingest |

Zero cold start survives the whole window; only the container does not.

### 10.5 Distributed Cleanup (FR-4.3)

On `issues.closed`, Developer then Architect. The order is retained for determinism, not because either step depends on the other — each role removes only its own paths.

1. `session.teardown` → **Developer**: `git worktree remove` on the **developer** worktree, delete the **developer** branch, delete **its own** scratch directory, `git worktree prune`. Reports what it removed. Does not touch Architect paths.
2. `session.teardown` → **Architect**: `git worktree remove` on the **architect** worktree, delete the **architect** branch, delete **its own** scratch directory, `git worktree prune`. Reports what it removed. Does not touch Developer paths.
3. **Orchestrator**: archive the issue session directory, purge that directory, **then** mark the session `CLOSED`. Archive format, contents, and retention are ADR-12 (§16); this step's ordering — archive and purge *before* the state flips — is what ADR-12's ledger rationale depends on, and it means a crash between the two never strands a `CLOSED` session with no tarball and no live directory to recover from.
4. **Orchestrator**: stop and remove the **project** container only if no other live session shares this `project_key` (§10.3, ADR-4); otherwise it stays running for the project's other issues. Closing one issue is never on its own sufficient to stop a project container.

The `artifacts` ledger (§15.1) records every worktree, branch, and scratch path at creation, so teardown is verifiable rather than best-effort: anything with `removed_at IS NULL` after teardown completes is a leak and is logged as such.

---

## 11. Availability & Recovery

### 11.1 Host Boot (NFR-1.1a / NFR-1.1b)

The host runs **FileVault, by owner policy**. No daemon can unlock an encrypted boot volume, so unattended recovery from a cold boot is impossible — SRS v1.3 amends NFR-1.1 accordingly rather than leaving the design silently non-compliant.

```bash
sudo systemsetup -setrestartpowerfailure on     # power returns → host boots to unlock prompt
# OrbStack → Settings → Start at login: enabled
# ~/Library/LaunchAgents/dev.agentd.plist: RunAtLoad, KeepAlive, ThrottleInterval=10
```

- **NFR-1.1a (software faults) is fully automatic.** Gateway crash → launchd restarts it. Container crash → restart policy plus Reconciler. OrbStack crash → restart, gateway retries until the socket returns.
- **NFR-1.1b (host boot) requires exactly one human action:** unlock and log in. Everything after that is automatic. Configuring power-on-after-mains means the human's job is *unlock*, not *walk over and press the button*.

**A LaunchAgent is correct here, not a compromise.** A human is present at boot by definition, so there is nothing to be gained from a pre-login LaunchDaemon — and OrbStack itself only starts at login, so a LaunchDaemon would spin waiting for a socket that cannot yet exist. The LaunchAgent also retains the GUI session access that NFR-2.2's notification needs.

Startup ordering is handled by retry with backoff, not by a launchd dependency graph.

### 11.2 State Reconciliation (NFR-1.2)

NFR-1.1b admits an **unbounded** downtime window — bounded only by how long until a human notices. Reconciliation is therefore not a nicety; it is the mechanism the entire recovery story rests on.

On every start, and every 5 minutes thereafter:

```
1. Open SQLite (WAL), migrate, PRAGMA integrity_check.
2. Inventory: docker ps -a --filter label=agentd.managed=true
   → adopt containers matching a live session; remove orphans.
3. GraphQL sweep: for all non-terminal sessions, fetch the FULL current state —
   issue, comments, open PRs, review states, review threads, check runs — in one
   round trip per repo. `updated_at` is used only as a paging hint, never as a filter.
4. Derive expected FSM state from GitHub truth (P1). If it differs from stored,
   adopt derived and log the divergence.
5. ID SET-DIFF, not timestamp comparison: for every node_id returned by the sweep,
   check membership against deliveries. Anything absent → synthesize a delivery
   (delivery_id = "recon:<node_id>") and enqueue it.
6. Detect interrupted turns: rows with started_at set and ended_at NULL whose
   turn_id is not in this process's in-flight set (ADR-27).
7. Re-attach RPC to running containers: serviceable health.ping (initialized),
   stamp runners.last_seen_at. Unreachable → report only; repair is lazy, on
   the next ensure_session (ADR-25/ADR-34). Never mark COLD here.
8. Send session.resume with a digest of everything missed; send turn.resume
   (never turn.dispatch) for interrupted turns.
9. Drain deliveries WHERE status='queued' in received_at order.
```

**Implementation splits three ways: steps 1–2 + 9 (local, M6-1a, ADR-20), steps 3–5 (the sweep, M6-1b, ADR-21), steps 6 + 8 (`turn.resume`, M6-1c, ADR-22 — held back because it needs a runner image rebuild, a different deploy shape). Step 7 belongs to none of them** and is tracked separately: `demote_cold` and `promote_hot` both exist on the supervisor, but nothing in the dispatch path calls `promote_hot`, so any component that writes `tier='cold'` today strands the runner it demoted. Lazy promotion has to be wired into dispatch before step 7 can be implemented at all, and that is a gateway-only change that must not ride M6-1c's rebuild. **ADR-25 settles that half:** promotion belongs in `ensure_session` rather than `_dispatch_turn` (four paths need a live runner and all four pass through it), it triggers on Docker state rather than the `tier` column, and it re-reads the host port because the stored endpoint does not survive a restart — without which a promotion fails its ping and the adopt branch deletes the container it was reaching for. **ADR-34 settles what is left of step 7** once the repair is lazy: re-attachment is a probe that stamps `runners.last_seen_at` and reports, never a repair on the reconciler thread and never a removal; and the "mark COLD" clause is retired rather than implemented, because `tier` is what §6.6 admission counts, so recording COLD for a container that is still running frees a slot whose RAM is still held. **ADR-20 scopes the local half** and settles what its steps mean in code: step 1 is already enforced by `Store.__init__`, which raises on a failed `integrity_check`, so the reconciler does not repeat it and never inherits the disk breaker; an orphan is a *project* with no session outside `CLOSED`, guarded by container age and in-flight turns; a container with no `runners` row is removed rather than adopted, because its bearer token is unrecoverable; and the reconciler nudges the dispatcher instead of draining alongside it. **Step 6 is narrowed by ADR-27:** unqualified, "rows with `started_at` set and `ended_at` NULL" is also the complete definition of a turn that is simply still running, and `_apply_open_turn` had nothing to tell the two apart on either arm. The list above already carries the fix — the `turn_id not in` clause — not just this paragraph; the resume-vs-retire split (age, `NON_RUNNING_STATES`) applies only to what step 6 now excludes the in-flight set from. **ADR-28 closes a gap inside that same set, not a new one:** ADR-27 populated it from `_dispatch_turn` only, so step 8's `turn.resume` call (`resume_interrupted_turn`) ran its own blocking RPC without ever joining the set it depends on — a resume in progress was still reachable by step 6 for the whole span of its own RPC. The set now covers both call sites that block on a runner RPC, not one.

**Step 5 is what makes downtime survivable**, and its formulation matters more than it looks.

An earlier version of this section used a timestamp watermark — "recover anything newer than `gh_watermark`". Both reviewers independently attacked it, and they were right: `updated_at` is not reliably monotonic across GitHub's asynchronously-aggregated PR and review state, so a strict exclusive lower bound can skip an event permanently and silently. Silent skipping is the worst failure mode available to this component.

The recovery cursor is therefore **set membership over stable node IDs**, not a timestamp comparison:

1. Each sweep fetches the complete current state of the issue and its linked PRs — cheap, because sessions are issue-scoped and the query is already a single GraphQL round trip.
2. Every returned `node_id` is checked for membership in a **node-ID index**. Missing ⇒ synthesize `recon:<node_id>` and enqueue. The primary key makes this idempotent for free. **ADR-21 supplies the index and explains why:** `deliveries` is keyed by GitHub's webhook *delivery GUID*, so membership against that table matches nothing and the first sweep would replay every session's whole history into the agents.
3. `updated_at` survives only as a paging hint for large comment threads, never as a correctness boundary.

Because the sweep also derives FSM state from the full fetch (step 4), a session converges to the correct state even if a *webhook* was missed entirely — a PR that is `MERGED` on GitHub but `CODE_REVIEW` in SQLite transitions on the next sweep regardless of what any timestamp says. That convergence is step 4's, not step 5's: node IDs are per object, so a merge on an already-seen PR is invisible to the set-diff (ADR-21). And it does **not** extend to a closed issue, which escalates rather than converging, because teardown cannot be undone. This is what demotes §19.5 from an open research risk to a settled design choice.

**It also does not extend to `pull_request.synchronize`.** Adopting `DESIGN_REWORK` → `DESIGN_REVIEW` (or `CODE_REWORK` → `CODE_REVIEW`) without a turn is not convergence: the reviewer is never asked, `silent_turns` still climbs on whatever else arrives, and a later legitimate merge reads as a §8.4 bypass. Membership cannot see a second action on a PR node, so step 5 cannot regenerate it either. That is ADR-37: a head-SHA watermark, and a synthesized `synchronize` that drains through the ordinary handlers.

**And it does not extend to `pull_request.opened` either, for the opposite reason (ADR-40).** Membership cannot see a second action on a PR node; here it cannot see the node at all, because `fetch_session_snapshot` fetches only the PRs `sessions.design_pr` / `feature_pr` already name. An untracked PR is therefore invisible to the set-diff *and* to step 4 at once — ADR-21 named that residual and left it, and it stranded a session twice in one day. The sweep now computes the role branch name and asks the forge for an open PR on it, synthesizing `pull_request.opened` when the session's column is NULL and the FSM would accept the event. The other half of the same defect is in the drain: ADR-37's spent-SHA gate guarded the two `opened` kinds as well as the two `revised` ones, and which SHA a PR was opened at is not a review cue.

**Step 8 is a correction to a mistake worth naming.** An earlier draft justified blind at-least-once re-dispatch on the grounds that agent actions are idempotent at the GitHub level. That is true for GitHub and **false for everything else** — a turn interrupted midway through `npm install`, a database migration, or a `git rebase` is not idempotent, and replaying it can leave a worktree in a state neither side can reason about.

So the gateway never blind-replays. `turn.resume` carries the interrupted turn's declared intent, and the runner re-derives actual state from what survived the crash — `git status`, `git log`, the worktree itself, PR state on GitHub — before deciding what remains. **The workspace is the checkpoint.**

---

## 12. Resource Management

### 12.1 Monitoring

The Resource Governor samples every 30 s: free disk on the `~/.agentd` volume, free host RAM, and per-container RSS.

### 12.2 Circuit Breaker (NFR-2.2)

| Condition | Action |
|---|---|
| Free disk < **15 GB** | **Trip.** Dispatcher stops draining; no new containers; idle sessions driven COLD; macOS notification; one comment per active issue. **Ingress keeps accepting and persisting deliveries.** |
| Free disk > **20 GB** | **Reset.** Resume draining, oldest delivery first. |
| Free RAM < 2 GB | Admission control only: refuse new HOT containers, demote LRU idle sessions to COLD. No trip. |

Asymmetric thresholds prevent flapping — and flapping is the expected behaviour of a single threshold here, because the breaker halts the very cleanup that would free space.

The disk/RAM asymmetry is deliberate: disk exhaustion corrupts work in progress, while memory pressure only needs to stop *new* work. Killing an in-flight turn to reclaim memory wastes the tokens already spent on it.

```bash
osascript -e 'display notification "Free disk 12.4 GB — agentd paused" \
  with title "agentd: storage circuit breaker" sound name "Basso"'
```

Notification delivery is best-effort; the authoritative signal is the GitHub comment on each affected issue, which reaches the owner wherever they are.

### 12.3 Garbage Collection

**Scoped by ADR-23** (M6-2), which narrows this section from measurement rather than taste. Read that ADR before implementing any of it.

Hourly, **on GC's own timer and never on the reconciler thread** (ADR-23; `git gc` and a cold-cache filesystem walk are both unbounded, and #117 is what that costs), and on breaker trip **by signal rather than an inline call**:

- **Prune dangling images built from agentd's own tags, and stopped containers carrying `label=agentd.managed=true`.** Nothing else, ever. **Amended by ADR-34:** a stopped managed container whose project has a `runners` row and a non-terminal session is **live COLD state, not debris** (§6.5 — COLD *is* `docker stop`), and only the reconciler decides its fate. Without this carve-out, the first component to write `tier='cold'` would be undone by the next GC pass, which is ADR-25's delayed rebuild in a second place. The host is not assumed to be agentd's alone — this one runs three unrelated containers — and this is ADR-4's rule, already implemented in the reconciler's orphan inventory.
- **Delete archives older than `retention.archive_days`** (default 30, ADR-12), and `*.tar.gz.tmp` older than **1 hour** — §10.5 step 3 writes then renames, so a `.tmp` surviving that long is crash debris, not an in-progress write. **Enumerate entries under `archive/` rather than globbing two shapes**: a residue directory holding neither a tarball nor a `.tmp` is invisible to both globs and therefore immortal (one has survived on this host since 2026-08-10). Report what is neither, and do not delete it on a timer.
- **`git gc` on a shared clone only when that repo has zero non-terminal sessions**, re-checked under the same lock that admits a session — otherwise a session starting mid-`gc` finds its clone being rewritten.
- **Delivery payloads are retained.** ADR-23 removes the truncation this section used to specify: ADR-21's node-ID index is backfilled from that column, an issue body has been recovered from it by hand twice, and the whole ledger is a few megabytes against a 15 GB floor. If disk pressure ever makes this real, archive old deliveries out rather than blanking the column in place.

**Archive deletion is deliberately not routed through the `artifacts` ledger below.** That ledger exists to catch artifacts that can leak *before* teardown completes — the crash-mid-`git worktree add` case that motivates this section's filesystem set-diff. An archive is not that kind of artifact: it is written *as part of* teardown (§10.5 step 3), not a side effect that can precede or outlive it, so there is no crash window for a ledger row to close over — a directory `mtime` listing already gives the same answer, for less machinery. (Full rationale: ADR-12, §16.)

**Orphan reconciliation — the artifact ledger is not sufficient on its own.** ~~§10.5 tracks worktrees, branches, and scratch paths via `artifact.register`, but that RPC is sent *after* the runner performs the action.~~ **Amended (#246): the mechanism named here has never run.** No code in the runner image sends `artifact.register` — the only occurrence there is a defensive *inbound* handler — because M3-C (`dd35b68`) moved the ledger to **supervisor-observed** registration at `ensure_session`, "not model-reported artifacts — empty by construction on real adapters". Measured on this host: **zero** `artifact.register notify` lines across every rotated log, against **94** artifact rows, all supervisor-observed. The RPC remains in §14.2 as a contract surface with no emitter.

**The conclusion survives; the crash window is a different one.** The gap is inside `ensure_session`, between the supervisor's `git worktree add` and its own `register_artifact` call — an OOM kill is still the realistic case — which leaves a worktree on disk the ledger has never heard of, so teardown cannot remove it and the "zero rows with `removed_at IS NULL`" check reports success while leaking disk. §12.3's filesystem set-diff is therefore still required, for a window one component wide rather than two. Do not read this paragraph as evidence that the runner reports its own artifacts.

GC therefore reconciles against the filesystem rather than trusting the ledger:

```
for each shared clone:
    git worktree list --porcelain          → actual worktrees on disk
    SELECT ref FROM artifacts WHERE kind='worktree'  → ledger
    actual − ledger, belonging to a CLOSED/absent session  → REPORT at WARNING (ADR-23)
    ledger − actual                                        → mark removed_at, log
```

The same set-diff runs for session directories under `sessions/`. This is the filesystem analogue of §11.2's ID set-diff, and for the same reason: a ledger of *intent* cannot be trusted to describe *state* across a crash boundary.

**M6-2 reports the first set difference; it does not remove it** (ADR-23, owner decision 2026-08-16). An earlier draft of this block said `git worktree remove --force`, and the reason it no longer does is that a disagreement between ledger and filesystem does not say *which side is wrong*. The crash-mid-`git worktree add` case is a real orphan; a live session that has registered nothing yet produces the identical signature, and so do things outside this design entirely — the first orphan the set-diff finds on the development host is a session directory recreated after that session's verified teardown by a test suite constructing `SessionLoop` with a bare `Config()` (#121). A deleting GC would have cleaned up after a broken test and told nobody, which turns a leak detector into a leak concealer. Removal is a later milestone, earned once the report has been observed clean. Whatever is eventually removed inherits ADR-20's age floor unchanged: an artifact whose age cannot be established, or is younger than the floor, is live.

### 12.4 State Visibility

Session state is mirrored to **GitHub labels** (`agentd:state:code_review`, `agentd:paused`, `agentd:awaiting_verification`). This makes the state machine legible to the human in the place they are already looking, and it survives total loss of the gateway — which a log file does not.

---

## 13. Security Model

### 13.1 Boundaries

| Boundary | Enforced by |
|---|---|
| Developer cannot approve its own PR | Architect token readable only by `uid_architect` on container-internal tmpfs (§5.2) + GitHub branch protection + gateway merge verification (§8.4) |
| Agent cannot escape to the host | No Docker socket, `cap-drop ALL`, `no-new-privileges`, non-root UIDs, no setuid binaries in the image |
| Agent cannot exhaust the host | `--memory`, `--cpus`, `--pids-limit`, plus admission control |
| Agent cannot forge the verification record | Checkbox *flips* counted only from the owner login (§10.2); no restore path can raise the box without `sessions.verified_at` (ADR-16); and a close whose body is ticked without that record classifies nothing (ADR-17). Flips were always owner-only; restores that raise were not, and that was the hole. The close-time gate is defence in depth for a raise route neither has enumerated. |
| Agent cannot close an issue | No component calls the close API (§10.3), **and** the gateway defuses closing keywords in a Feature PR body before authorising the merge (ADR-24). The first clause alone was true and misleading: an agent closed #32 and #81 by writing `Closes #N` and merging, which calls no close API at all. §10.3's reopen-and-escalate remains the recovery for any route not defused — a closing keyword in a *commit* message is not rewritten (ADR-24 residual) |
| Attacker cannot forge events | HMAC-SHA256 on every delivery; RPC endpoint loopback/UDS-local with a per-session bearer token |

### 13.2 Stated Threat Model

**A confused or prompt-injected agent — not a kernel-class attacker.** Against that model, DAC separation plus dropped capabilities, `no-new-privileges`, and no setuid binaries is adequate.

What this explicitly does **not** provide: cross-role *data* confidentiality. Both role UIDs can read each other's worktrees, transcripts, and scratch, because those live on host bind mounts where ownership is not enforceable (§5.2). This is accepted — both roles are already trusted with the repository contents. If a future requirement demands data separation, that forces the per-role container fallback in ADR-4, and it is the only argument that does.

**Cross-*issue* exposure (project-scope amendment).** Since the container is per project (ADR-4), the same reasoning now spans issues: every issue in a repository shares one container filesystem and, for each role, **one CLI conversation**. The original argument extends cleanly — the trust boundary was always the repository, and every issue in it is inside that boundary.

The consequence that does **not** follow automatically, and must be stated rather than inherited: **a prompt injection now persists.** Under per-issue containers a poisoned turn was contained to one conversation that ended with the issue. Under project scope it remains in that role's conversation for the life of the project, influencing every later issue. This is an accepted cost of the decision to share context across issues (which is wanted — issues here are interconnected), not an oversight. Two mitigations follow from it: `session.snapshot` (§14.2) compaction becomes load-bearing rather than optional, and a project-level "reset this role's conversation" escape hatch is the remedy when a session is believed poisoned.

**Cross-issue context (#20).** One CLI conversation per role is shared across issues in a project by design. A prompt injection in one issue's turn can persist into later issues for that role for the project's life. Accepted under owner decision (serial, interconnected issues); stated here so it is not inherited silently (§13.3).

### 13.3 Prompt Injection

Issue and PR text is untrusted input and the agents hold write credentials. On a private single-owner repo the exposure is low; it rises immediately if the repo becomes public. Baseline mitigations: `intake.actors: collaborators` (§4.3); role cards instructing agents to treat issue/PR bodies as data rather than instructions; gateway-verified merge preconditions (§8.4); and a human closure gate that is structurally unreachable by any agent. Full mitigation is out of scope and belongs in a follow-up.

### 13.4 Deferred Hardening

Recorded so reviewers can see these were considered and consciously deferred: outbound egress allowlist proxy; per-repo seccomp profiles; per-turn rather than per-session tokens; OrbStack user-namespace remapping. Each adds operational surface disproportionate to the risk on a single-owner private host.

---

## 14. Agent Session Protocol

### 14.1 Transport

**JSON-RPC 2.0, NDJSON-framed.** Both Phase 1 drafts that specified an IPC mechanism arrived at JSON-RPC 2.0 independently, so it is treated as settled. The SRS framed NDJSON *versus* JSON-RPC; they are orthogonal — framing versus semantics — so this design takes both. JSON-RPC supplies request/response correlation, typed errors, and notifications (turns are long-running and emit progress); NDJSON framing supplies `tail`-ability and `nc`-debuggability at zero cost over `Content-Length` framing.

**Transport: loopback TCP + bearer token (default).** Spike OQ-1 (2026-08-06, OrbStack) **failed**: a UDS path on a bind mount is visible on both macOS host and Linux container (`S_ISSOCK` true) but `connect()` returns Connection refused — the accept queue is not shared across the VM boundary. UDS across a bind mount is therefore **out** as a host↔container transport. Long-lived `docker exec` stdio remains rejected: it ties session liveness to a pipe held by the gateway process, so every gateway restart would kill every session — directly contradicting NFR-1.1a.

**Bearer token requirements (mandatory — TCP has no second ACL layer):**

- ≥ 256 bits from a CSPRNG, unique per runner session
- Compared in constant time (`hmac.compare_digest` or equivalent)
- Never written to logs, transcripts, or GitHub
- Listener bound to `127.0.0.1` only (Docker publish form `-p 127.0.0.1:0:7000`), never `0.0.0.0`

First frame after connect must be `session.attach` carrying the bearer token; anything else closes the connection.

### 14.2 Method Catalogue

**Gateway → Runner**

| Method | Purpose |
|---|---|
| `session.attach` | Authenticate the connection |
| `session.init` | First-time setup: role cards, identities, **tokens**, workspace paths, budgets, **`models`** (per-adapter model / reasoning effort, ADR-9 v2 / #92); **identity preflight** (`GET /user` per token) before any turn |
| `session.resume` | Post-restart rehydration with a digest of missed activity; re-delivers tokens; **repeats identity preflight** |
| `turn.dispatch` | Run one turn against one normalized event — on the **held per-role CLI pipe** when live (§6.3); oneshot `-p` only when mock/script or `AGENTD_CLI_MODE=oneshot` |
| `turn.resume` | Re-enter an interrupted turn; runner re-derives state from the workspace |
| `session.snapshot` | Force a transcript checkpoint and context compaction |
| `session.teardown` | Kill held CLI children, wipe secrets, report cleanup (FR-4.3) |
| `health.ping` | Liveness + runner RSS + per-role CLI RSS (`cli_rss_kb`) + **`models`** actually in force (#92) |

**Runner → Gateway**

| Method | Purpose |
|---|---|
| `notify.progress` | Streaming turn progress (notification; no `id`) — partial CLI output while a turn is open |
| `artifact.register` | Declare a worktree/branch/scratch path for the cleanup ledger. **No emitter today** (#246) — the ledger is supervisor-observed (§12.3); kept as a contract surface, and the gateway handles it identically on `turn.dispatch` and `turn.resume` |
| `escalate.human` | Request human input; pauses the session (FR-3.3/3.4) |

**`turn.dispatch` on a held pipe (#25).** The runner does not `exec` a new CLI per turn for real adapters. It writes one user message (claude stream-json) or one `session/prompt` (grok ACP) on the existing stdin, reads until turn-end (`{"type":"result"}` / `stopReason: end_turn`), and may emit `notify.progress` frames before the JSON-RPC response. **The stream carries no turn boundary of its own, and the runner must supply one (ADR-29 / #162).** "Reads until turn-end" is only correct if exactly one end-of-turn frame is produced per message written, and that invariant is the vendor's to keep, not ours to assume: a claude session-limit refusal emits an `is_error` `result` and then emits the *real* `result` for the same message once the limit resets. Because the stdout queue lives for the CLI process's lifetime and is shared by every turn, one surplus frame shifts every later turn by one, permanently, until the process respawns. The runner therefore **drains the queue before writing a prompt** — anything already there is by construction from an earlier exchange, and is discarded with a WARNING naming the turn — and **respawns the role's CLI after any `is_error` result**, because an error frame does not prove the vendor is finished with the prompt. Where the wire protocol offers a correlator the adapter must use it instead: grok ACP already matches the `session/prompt` id and needs only the log. The gateway client must drain notification frames until the matching response `id`. Per-turn `deadline_s` still bounds the wait; on expiry the child is killed and the result is `failed` so the role lock can release. Crash or kill → next dispatch respawns with vendor `-c` / session store + transcript under §14.5.

**Cwd.** The long-lived process is spawned at the project root (`/srv/agentd`). Each turn's prompt names the issue worktree; the process is not rebound per issue (decision (a) on #25).

### 14.3 Example Turn

```json
{"jsonrpc":"2.0","id":"t-01J8Z","method":"turn.dispatch","params":{
  "turn_id":"01J8Z…","role":"architect","deadline_s":900,
  "event":{"kind":"feature_pr_opened","actor":"grok-bot","pr":47,
           "head_sha":"9f2c…","changed_files":14,"additions":312,"deletions":47},
  "context":{"rfc":"/srv/session/architect/context/rfc.md",
             "worktree":"/srv/session/architect/worktrees/review-47",
             "digest":"/srv/session/architect/context/digest-01J8Z.md"},
  "budget":{"turns_left":31,"review_rounds_left":5}}}
```

```json
{"jsonrpc":"2.0","id":"t-01J8Z","result":{
  "status":"changes_requested",
  "public_actions":[{"kind":"review","pr":47,"state":"CHANGES_REQUESTED","comments":3}],
  "summary":"3 inline comments: missing migration, unhandled null pref, no reload test",
  "artifacts":[{"kind":"scratch","ref":"/srv/session/architect/scratch/diff-47.patch"}]}}
```

`status` ∈ `done | changes_requested | needs_human | failed`. **The gateway drives the FSM from observed GitHub events, never from this field** — `status` is advisory, used for budgets and logging. This preserves P1 even if an agent misreports.

### 14.4 Context Compaction

When `transcript.jsonl` exceeds a configured token estimate, the runner self-summarizes into `context/summary.md` and starts a new segment. Resume loads the summary plus the current segment, bounding per-turn cost on long-lived issues.

**Project-scope / long-lived CLI (#25):** vendor in-process context also grows for the project lifetime (not only our transcript). Compaction becomes load-bearing (§13.2). Automatic `session.snapshot` compaction is still future work; until it lands, the runner **logs at ERROR** when `transcript.jsonl` reaches **≥ 5000 lines** and surfaces `transcript_growth_warning` on the turn result — operators must reset the role conversation rather than expect silent mid-turn truncation.

### 14.5 Vendor Adapter Contract

**The stable surface is `agentd-runner` + host-persisted `transcript.jsonl` + `summary.md` + event digest.** Vendor session-resume (`claude -c`, Grok ACP / durable HOME store) is the **fast recovery** when a held process dies; it is **not** the sole continuity mechanism and not a dependency for architecture (ADR-9). If a vendor CLI cannot rehydrate its own internal session, the adapter rebuilds the next turn from our files (oneshot `-p` with transcript rehydration), exactly as a cold path does. Slightly higher token cost; zero cold start still holds at the git and workspace layers. Vendor capability differences are therefore adapter implementation details and cannot force an architecture change.

**One-shot `-p` remains explicit recovery**, selected by `AGENTD_CLI_MODE=oneshot` or by mock/script adapters — never the accidental default for production claude/grok once the long-lived path is wired (#25).

**§14.4 growth note.** In-process vendor context now grows for the project's lifetime. Compaction (`session.snapshot`) is load-bearing; until implemented, the runner must fail loudly on unbounded growth rather than silently truncate mid-conversation.

---

## 15. Traceability & Data Model

| Req | Requirement | Section |
|---|---|---|
| FR-1.1 | Multi-account authentication | §5.1, §5.2 |
| FR-1.2 | Configurable role mapping | §5.3 |
| FR-1.3 | Branch protection compliance | §5.2, §7.3, §8.4 |
| FR-2.1 | Issue ingestion → Architect | §4.2, §4.3, §8.1 |
| FR-2.2 | Design phase → Design PR | §8.1, §8.2 |
| FR-2.3 | Peer design review | §8.1, §8.3 |
| FR-2.4 | Implementation → Feature PR | §6.4, §8.2 |
| FR-2.5 | Automated code review loop | §8.1, §9.2, §9.3 |
| FR-2.6 | Developer merges + deletes branch | §8.4 |
| FR-3.1 | Verification Protocol section | §10.1 |
| FR-3.2 | Human gate on closure | §10.2, §10.3 |
| FR-3.3 | Escalation via `@owner` | §8.5, §14.2 |
| FR-3.4 | Escalation pause | §8.5, §9.1 |
| FR-4.1 | Event routing + loop prevention | §9.1–§9.3 |
| FR-4.2 | Gated cleanup trigger | §10.3 |
| FR-4.3 | Distributed cleanup | §10.5, §15.1, ADR-12 |
| NFR-1.1a | Unattended recovery — software faults | §7.2, §11.1 |
| NFR-1.1b | Attended recovery — host boot | §11.1 |
| NFR-1.2 | State reconciliation | §11.2 |
| NFR-2.1 | Memory & disk safeguards | §6.6, §7.2, §12.1 |
| NFR-2.2 | Storage circuit breaker | §12.2 |
| SRS §2 | Zero cold starts | §6.3 |
| SRS §2 | Issue-scoped lifecycle | §6.1 |
| SRS §2 | Role-agnostic identities | §5.1, §5.3 |
| SRS §5.1 | DB engine (open) | ADR-2 |
| SRS §5.2 | Gateway language (open) | ADR-1 |
| SRS §5.3 | IPC mechanism (open) | ADR-3 |
| SRS §5.4 | Container structure (open) | ADR-4, §7 |

### 15.1 Schema

```sql
-- Connection pragmas must be set with individual execute() calls before
-- any multi-statement script: foreign_keys cannot change inside a transaction.
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

CREATE TABLE deliveries (               -- idempotency ledger + durable queue
  delivery_id TEXT PRIMARY KEY,         -- X-GitHub-Delivery, or recon:<node_id>
  event TEXT NOT NULL, action TEXT,
  -- repo/sender default to '' when the payload cannot be parsed; empty string
  -- rather than NULL so NOT NULL holds and indexes stay simple (M0 amendment).
  repo TEXT NOT NULL DEFAULT '',
  issue_num INTEGER,
  sender TEXT NOT NULL DEFAULT '',
  received_at INTEGER NOT NULL,
  -- zlib (RFC 1950) compressed raw JSON. Spec originally said "zstd"; M0 uses
  -- stdlib zlib to avoid a native dependency. Wire format is still compressed.
  payload BLOB NOT NULL,
  -- Status vocabulary (M1 amendment — `deferred` added):
  --   queued   : accepted by ingress, not yet examined by the dispatcher
  --   deferred : examined; no handler yet (no session / milestone not built).
  --              Not the same as routed. M2+ resumes with WHERE status='deferred'.
  --   routed   : handed to a session (requires a sessions row)
  --   dropped  : intentionally discarded (intake fail, loop filter, …)
  --   done     : fully processed terminal success (e.g. ping)
  --   failed   : processed with error
  status TEXT NOT NULL DEFAULT 'queued',
  -- queued|deferred|routed|dropped|done|failed
  -- v11 (ADR-38 / #214): a deferred row is eligible when next_attempt_at <= now.
  -- 0 means eligible now, so every row that never backs off drains as before.
  next_attempt_at INTEGER NOT NULL DEFAULT 0,
  defer_count INTEGER NOT NULL DEFAULT 0   -- backoff exponent for the DEFER path
);
CREATE INDEX ix_deliveries_pending ON deliveries(status, received_at);

-- Host circuit-breaker latch (NFR-2.2). Single-row table; not session-scoped.
CREATE TABLE circuit_breaker (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  disk_paused INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  updated_at INTEGER NOT NULL
);

CREATE TABLE sessions (
  session_key TEXT PRIMARY KEY,         -- owner/repo#42
  project_key TEXT NOT NULL,            -- owner/repo (#20)
  repo TEXT NOT NULL, issue_num INTEGER NOT NULL,
  state TEXT NOT NULL, paused_reason TEXT,
  architect TEXT NOT NULL, developer TEXT NOT NULL,
  roles_locked INTEGER NOT NULL DEFAULT 0,   -- set when the Design PR opens
  design_pr INTEGER, feature_pr INTEGER,
  turn_count INTEGER NOT NULL DEFAULT 0,
  consec_agent_turns INTEGER NOT NULL DEFAULT 0,
  review_rounds INTEGER NOT NULL DEFAULT 0,
  progress_fp TEXT, progress_repeat INTEGER NOT NULL DEFAULT 0,
  zero_thread_rounds INTEGER NOT NULL DEFAULT 0,
  gh_watermark INTEGER,                 -- reconciliation cursor
  verified_at INTEGER,                  -- checkbox verified (record only)
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);

-- N issue sessions : 1 project runner (#20)
CREATE TABLE runners (
  project_key TEXT PRIMARY KEY,        -- owner/repo
  container_id TEXT, endpoint TEXT, token TEXT,  -- RPC bearer
  tier TEXT NOT NULL,                   -- hot|cold
  last_seen_at INTEGER
);

CREATE TABLE turns (
  turn_id TEXT PRIMARY KEY, session_key TEXT NOT NULL, role TEXT NOT NULL,
  delivery_id TEXT, started_at INTEGER NOT NULL, ended_at INTEGER,
  status TEXT, summary TEXT,            -- ended_at IS NULL ⇒ turn.resume on recovery
  FOREIGN KEY (session_key) REFERENCES sessions(session_key) ON DELETE CASCADE
);

CREATE TABLE artifacts (                -- cleanup ledger (FR-4.3)
  id INTEGER PRIMARY KEY,
  session_key TEXT NOT NULL, role TEXT NOT NULL,
  kind TEXT NOT NULL,                   -- worktree|branch|scratch|container
  ref TEXT NOT NULL,
  created_at INTEGER NOT NULL, removed_at INTEGER,
  FOREIGN KEY (session_key) REFERENCES sessions(session_key) ON DELETE CASCADE
);

CREATE TABLE escalations (
  id INTEGER PRIMARY KEY, session_key TEXT NOT NULL, role TEXT,
  reason TEXT NOT NULL, comment_id INTEGER,
  opened_at INTEGER NOT NULL, resolved_at INTEGER
);
```

`agentd` is the only *process* writer, so WAL plus `busy_timeout` suffices for multi-process contention. Within the process, concurrent threadpool handlers share one connection behind a **mutex** around write methods (M0 review S3).

---

## 16. Architecture Decision Records

**Each ADR is its own file under [`ADR/`](ADR/).** This section is an index: one paragraph per decision, enough to tell whether you need to open it. The ADR carries the evidence, the options table, the bindings and the acceptance — and the ADR, not this summary, is the decision of record. Where the two disagree the file wins, and the summary is the bug.

Read the ADR, not the issue it names: several issues' own diagnoses were corrected by the ADR that fixed them.

| # | Decision |
|---|---|
| **[1](ADR/adr-1-gateway-python-fastapi.md)** | **Gateway in Python 3.12 + FastAPI.** Owner ruling. FastAPI + Uvicorn, `uv` project with a pinned lockfile; the service stays `agentd`. Resolves SRS §5.2. |
| **[2](ADR/adr-2-sqlite-derived-cache.md)** | **SQLite as a derived cache.** WAL mode, single writer, treated as cache, queue and idempotency ledger — never a system of record. GitHub remains P1, which is why a wiped DB is recoverable and a lost delivery is not. |
| **[3](ADR/adr-3-jsonrpc-over-ndjson.md)** | **JSON-RPC 2.0 over NDJSON on loopback TCP + bearer.** The Unix-socket alternative was measured and failed across the OrbStack bind mount (OQ-1). See §14.1. |
| **[4](ADR/adr-4-container-per-project-os-uids.md)** | **One container per project, two OS UIDs.** Not one per issue: context is shared across a project's issues deliberately. The cost, stated rather than inherited, is that a prompt injection persists for the life of the project. |
| **[5](ADR/adr-5-shared-clone-worktrees-relative-paths.md)** | **Shared clone + worktrees, relative paths.** One clone per repository, `git worktree add` per session-role. Relative paths so the host and container see the same gitdir. |
| **[6](ADR/adr-6-tokens-via-control-channel.md)** | **Tokens via the control channel, never a bind mount.** Delivered in `session.init` and written to container-internal tmpfs by the in-container root supervisor. macOS bind mounts cannot enforce UID/mode (M2-A). |
| **[7](ADR/adr-7-machine-users-github-app.md)** | **Machine users, not a GitHub App.** App bots surface as `app-name[bot]` and interact with branch protection through a different mechanism; the SRS needs identity strings that match real usernames. |
| **[8](ADR/adr-8-no-github-api-proxy.md)** | **No GitHub API proxy.** Agents call `gh` and `git` with their own tokens. The gateway verifies privileged transitions instead of intermediating every call — the enforcement gap that §8.4 closes. |
| **[9](ADR/adr-9-runner-contract-stable-surface.md)** | **The runner contract is the stable surface.** Vendor session-resume is an optimisation, not a dependency, so asymmetric vendor capabilities cannot force asymmetric architecture. |
| **[10](ADR/adr-10-accept-queue-ingress.md)** | **Accept-and-queue ingress.** Never 5xx for a downstream condition: GitHub does not retry repository webhooks, so a rejected delivery is a lost one. Persist first, decide later. |
| **[11](ADR/adr-11-identity-preflight-token-delivery.md)** | **Identity preflight on token delivery.** Added after a wrong-account `gh` post attributed agent work to the owner. §5.2 stops cross-agent confusion inside the container but not operator mis-mapping of PATs. |
| **[12](ADR/adr-12-session-archive-format-retention.md)** | **Session archive format and retention.** `tar.zst` and "30-day retention" had been asserted since 1.0.0 without ever being decided; this settles both, with the numbers measured rather than assumed. |
| **[13](ADR/adr-13-agentctl-version.md)** | **`agentctl version` — package metadata, no config load.** Chosen as M4-4's live end-to-end demonstration precisely because it is small enough that a deliberate `CODE_REWORK` round exercises the loop rather than the feature. |
| **[14](ADR/adr-14-agentctl-review-stats.md)** | **`agentctl review-stats` — turns-per-review measurement.** Makes the review-coalescing mechanism measurable instead of asserted, closing #49's outstanding item. |
| **[15](ADR/adr-15-verification-block-restore.md)** | **Verification-block restore composes against a fresh read.** Deciding and composing from the same webhook payload loses any edit that landed in between. Fixes #84. |
| **[16](ADR/adr-16-checkbox-requires-owner-tick.md)** | **Raising the checkbox requires an *observed* owner tick.** A before-image has no actor, so "it was ticked before this edit" is not evidence that the owner ticked it. Fixes #89. |
| **[17](ADR/adr-17-ticked-body-no-owner-tick.md)** | **A ticked body with no observed owner tick does not classify.** The close-time check that holds §13.1 even when a tick reaches the body by a route no branch guards. Fixes #90. |
| **[18](ADR/adr-18-close-reconcile-hold-scope.md)** | **The close-reconcile hold is scoped to the closed issue.** ADR-17's hold blocked every dispatch including the reply-resume; this narrows it to the issue that triggered it. Fixes #98. |
| **[19](ADR/adr-19-mirror-reread-the-body.md)** | **The mirror — re-read the body rather than reverse the ruling.** The same race pointing the other way: an agent unticks what the owner ticked. Owner ruling. Fixes #96. |
| **[20](ADR/adr-20-reconcile-local-half.md)** | **The local half of §11.2.** Steps 1, 2 and 9 need no GitHub API, are useful the day they land, and carry none of the sweep's design risk. Scopes #81. |
| **[21](ADR/adr-21-reconcile-sweep-membership.md)** | **The sweep, and the membership set it needs first.** Steps 3–5, plus the node-ID index that makes "have I seen this?" answerable. Its adopt-a-synchronized-PR sentence was later found to be the defect ADR-37 fixes. |
| **[22](ADR/adr-22-interrupted-turns-checkpoint.md)** | **Interrupted turns, and the checkpoint that was already on disk.** Steps 6 and 8. Closes #81. |
| **[23](ADR/adr-23-garbage-collection-narrowed.md)** | **Garbage collection, narrowed to what it may safely touch.** The orphan set-diff is the valuable half; most of what §12.3 listed first is dangerous, worthless, or invisible on this host — each a measurement, not an opinion. |
| **[24](ADR/adr-24-closing-keyword-defuse.md)** | **The closing keyword — defusing the route that calls no close API.** §13.1's "no component calls the close API" was true and wrong: `Closes #N` in a PR body closes the issue anyway. The gateway rewrites it before authorising. |
| **[25](ADR/adr-25-lazy-promotion.md)** | **Lazy promotion — reach a stopped runner before deciding it is gone.** `promote_hot` had no caller outside a test. Unblocks §11.2 step 7 (#116). |
| **[26](ADR/adr-26-blocked-unresolved-threads-sticky.md)** | **`blocked` is not one state.** Unresolved threads are sticky and permanent; checks in flight are transient. Retrying cannot clear a thread nobody resolved. Closes the gap #63 named. |
| **[27](ADR/adr-27-inflight-turn-ids.md)** | **In-flight turn IDs — the reconciler's missing third state.** "Started and not ended" is also the complete definition of a turn that is simply still running. Fixes #151. |
| **[28](ADR/adr-28-inflight-two-dispatch-sites.md)** | **The in-flight set has two dispatch sites, and ADR-27 wired one.** Found by reading merged `main`, not by an incident; the window is narrow and the failure silent. Fixes #158. |
| **[29](ADR/adr-29-held-pipe-turn-boundary.md)** | **The held pipe has no turn boundary.** A vendor `result` frame belongs to whichever turn reads next, so one surplus frame shifts every later turn permanently. The issue's own diagnosis was wrong in the way that changes the fix. Fixes #162. |
| **[30](ADR/adr-30-author-sent-pr-events.md)** | **An agent's own PR is not a cue.** Author-sent review and comment events route nowhere — but `pull_request` events are excluded from that rule, and the exclusion is load-bearing: without it every rework round ends the loop. Fixes #173. |
| **[31](ADR/adr-31-worktree-base-history.md)** | **Two defects put session worktrees on the wrong history**, and the issue ranks them backwards: the gateway had no `git fetch` at all, and `worktree_add` defaulted to the clone's `HEAD`. Fixes #171. |
| **[32](ADR/adr-32-loop-safety-wrong-subject.md)** | **The gateway asks its two loop-safety questions of the wrong subject** — about the delivery when it means the turn, and about the session being paused before asking whether it is terminal. Eleven lines apart. Fixes #156 and #147. |
| **[33](ADR/adr-33-quota-refusal-after-output.md)** | **The quota gate assumes a refusal arrives before any output.** A session limit landing *after* the turn produced output records `failed`, and `failed` consumes the delivery. Fixes #168. |
| **[34](ADR/adr-34-probe-attaches-and-records.md)** | **Step 7 attaches and records; the "mark COLD" clause is retired.** COLD must survive GC before any component may write it. Settles the second half of #116. |
| **[35](ADR/adr-35-kill-as-the-role.md)** | **The runner cannot signal its own CLIs.** `--cap-drop ALL` removes `CAP_KILL`, so the kill must drop to the role uid. Every claim was measured in a throwaway container from the deployed image — the defect cannot be reproduced outside one by construction. Settles #172. |
| **[36](ADR/adr-36-identity-account-vs-operator.md)** | **The identity boundary is an account control**, and the demonstration meant to prove it is an operator claim. The repository shipped the violation and advertised it as the remedy. Settles #57. |
| **[37](ADR/adr-37-dropped-synchronize-head-sha.md)** | **A dropped `synchronize` is not an adopt.** Recover it as a head-SHA delivery, because adopt writes state and dispatches nothing. Hashes are not ordered, so "newer" is not a rule — compare to the forge's live head. Fixes #211. |
| **[38](ADR/adr-38-superseded-approval-defer-clock.md)** | **A withdrawn approval is not an unverifiable one.** `CHANGES_REQUESTED`, `DISMISSED` or a stale-head `APPROVED` supersede the delivery: drop it, do not escalate, still do not merge. Also gives `defer` a clock — 4,466 log lines became 79. Fixes #214. |
| **[39](ADR/adr-39-session-loop-rename.md)** | **`design_loop.py` owns the whole lifecycle — rename it.** The name had been wrong since M4-1 added the code half to the same module, and it had already caused a wrong inference during review. Ref #159. |
| **[40](ADR/adr-40-untracked-pr-opened.md)** | **A `*_pr_opened` the FSM never sees is unrecoverable, and the sweep cannot reach the PR that proves it.** ADR-37's spent-SHA gate narrows to `*_revised`; the reconciler discovers an untracked PR by computing the role branch name. Closes ADR-21's untracked-PR residual. Fixes #229. |
| **[41](ADR/adr-41-agentctl-sessions-live.md)** | **`agentctl sessions --live` — the set from `list_nonterminal_sessions`, the fields from the row it does not carry.** Membership is the sweep's own method, called rather than re-expressed; ordering, ASCII output and the absent-PR placeholder are CLI decisions, each one measured. Names why `--live` is not a drop-in for the runbook's "is anything live" check: `TEARDOWN` dispatches agent turns, so *in the loop* and ADR-20's *container-live* (`live_project_keys`) are two readings of the word that differ by exactly that state. Fixes #248. |
---

## 17. Open Spikes & Owner Items

None of these block drafting or M0–M1; each has both outcomes specified.

| # | Spike | Cost | Decides |
|---|---|---|---|
| **OQ-1** | Unix domain socket across an OrbStack bind mount | ~10 min | **RESOLVED 2026-08-06: FAIL.** Loopback TCP + bearer is default (ADR-3, §14.1). |
| **M2-A** | macOS bind-mount UID/mode enforcement | ~15 min | **RESOLVED 2026-08-06: FAIL isolation (expected).** ADR-6 confirmed. |
| **M2-B** | Container-internal tmpfs UID/mode enforcement under OrbStack | ~15 min | **RESOLVED 2026-08-06: PASS.** A5 holds; ADR-4 dual-container fallback not triggered. |
| **M4-A** | Branch-protection integration test. **The claim is split (#57, ADR-36):** the *account* claim is that the Developer **account** cannot submit a satisfying approval on a PR it authored; the *operator* claim — that the Developer's operator cannot cause one to exist — is a different claim and was the wording used until 2026-08-25 | M4 | **Account claim RESOLVED 2026-08-12: PASS.** Assert half: `GET …/rules/branches/main` (no admin) shows `required_approving_review_count: 1` + `require_last_push_approval`. Observe half (PR #55): Developer merge → HTTP 405 ruleset refusal; Developer self-APPROVE → HTTP 422; Architect APPROVE then Developer merge → 200. All four are account facts and all four still re-check from the API. FR-1.3 (SRS) is an account requirement and is satisfied by these. **Operator claim NOT ESTABLISHED**, and false in the only run that exists: #55's approval was produced by the Developer's operator under the Architect's credential. Not re-run by hand — held open as a row in `docs/ops/live-sign-offs.md` and discharged on a real Feature PR approval produced by an Architect turn, stamped per §5.5.2. Evidence: `docs/ops/m4-a-branch-protection.md`; analysis: `docs/design/rfcs/57-adr-36-identity-credential-protocol.md`. |
| **OQ-4** | Vendor CLI headless behaviour per adapter | M3 | Adapter implementation only (ADR-9) |

**M2 residual checks (acceptance, not topology):** (1) production image has **no setuid/setgid binaries** — `find / -xdev \( -perm -4000 -o -perm -2000 \) -print` empty; privilege drop via `setuid`/`setgid` after `fork`, never `su`. (2) **tmpfs wipe on restart** — `docker stop` → `docker start` → `/run/agent` empty (tokens re-delivered on `session.resume`).

---

## 18. Testing Strategy

| Layer | Coverage |
|---|---|
| **Unit** | Role resolver precedence · checkbox parser + sentinel extraction · loop-filter rules per recipient · FSM transitions · fingerprint composition · breaker hysteresis |
| **Component** | SQLite reconcile fixtures · node-ID set-diff completeness · interrupted-turn detection · worktree orphan reconciliation after a simulated mid-`artifact.register` crash |
| **Integration** | Recorded GitHub webhook fixtures → fake runner · GraphQL sweep against a recorded schema |
| **End-to-end** | Dual-bot dry run on a private sandbox repo, full issue → merge → close cycle |
| **Chaos** | `kill -9` the gateway mid-turn · reboot the host · fill the disk below 15 GB · sever the tunnel for 30 min and verify the sweep recovers every missed delivery |
| **Security** | M2-A / M2-B UID isolation · M4-A branch protection · assert no token material on any host path or in `docker inspect` |

The chaos row maps one-to-one onto NFR-1.1a, NFR-1.1b, NFR-2.2, and NFR-1.2. Those requirements are not considered met until the corresponding chaos test passes.

---

## 19. Known Weaknesses

Stated so reviewers do not have to discover them.

1. **The two-UID boundary is DAC-only.** A kernel bug, a setuid binary introduced into the image, or a container escape crosses it, where separate containers would add namespace isolation. Accepted under the stated threat model (§13.2); ADR-4 records the fallback.
2. **Cross-role data confidentiality is not provided** (§5.2). Both roles read each other's worktrees and transcripts. Accepted; it is also the single requirement that would force the dual-container fallback.
3. **Stall detection remains heuristic.** The fingerprint plus zero-thread signal covers the known failure shapes, but two agents determined to appear productive can still consume budget. Budgets are the backstop, and the failure direction is a spurious escalation to the human, which is safe.
4. **Prompt injection is only partially addressed** (§13.3). Adequate for a private single-owner repo; not adequate for a public repository.
5. ~~**Reconciliation correctness depends on watermark monotonicity.**~~ **Resolved during review of this PR.** Both reviewers independently attacked the timestamp watermark and proposed the same fix; §11.2 step 5 now uses set membership over stable node IDs, with `updated_at` demoted to a paging hint. `updated_at` monotonicity is no longer load-bearing anywhere in the design. Retained here rather than deleted, because the correction is the useful record.
6. **Recovery from a host boot is not unattended** (§11.1). This is owner policy rather than a design defect, and the SRS was amended rather than the requirement quietly missed — but it means the practical downtime after a power cut is bounded by human attention, not by software.
7. **Cleanup completeness depends on a crash-safe set-diff, not on the ledger.** §12.3 reconciles worktrees against the filesystem precisely because `artifact.register` can be lost to a crash. If the set-diff itself has a gap — a worktree created outside the shared clone, say — leaks are silent. Bounded by disk monitoring rather than prevented outright.

---

## 20. Setup Runbook

1. Install OrbStack; enable **Start at login**.
2. `sudo systemsetup -setrestartpowerfailure on`.
3. Create the two machine user accounts; grant `write` on target repos.
4. Store PATs and the webhook secret in the Keychain (`security add-generic-password -s agentd …`).
5. `uv sync`; install the gateway; load `~/Library/LaunchAgents/dev.agentd.plist`.
6. Build `agentd/session-runner`; verify `git --version` ≥ 2.48 inside the image.
7. Start the ingress backend; register the repository webhook against `/webhooks/github` with the shared secret. Events: issues, issue comments, pull requests, PR reviews, PR review comments, check suites, status, push.
8. Add `.agentd/config.yaml` to each target repo; apply role labels where per-issue overrides are wanted.
9. Configure branch protection: require one approving review, require status checks, and **do not** allow the PR author to approve.
10. Smoke test: open a labelled issue and confirm a Design PR appears from the Architect account.

**Do not rename the Mac user account** after step 5 — it invalidates mount paths for every session in flight.

---

## 21. Implementation Roadmap

Each milestone ends in a verifiable condition, not a code-complete claim.

| M | Scope | Done when |
|---|---|---|
| **M0** | Host bootstrap: LaunchAgent, tunnel, Keychain, `~/.agentd` | A signed webhook reaches `agentd` and is logged; power-cycle test restores the daemon after unlock |
| **M1** | Ingress + SQLite + `agentctl status` | Deliveries persist, duplicates ignored, queue depth visible; **OQ-1 spike resolved** |
| **M2** | Supervisor: image, container, UIDs, tokens, worktrees, RPC | `health.ping` round-trips; `git worktree add` < 2 s on a warm clone; **M2-A and M2-B resolved** |
| **M3** | Design loop (FR-2.1 → FR-2.3) + adapters | An issue produces a Design PR reviewed and approved by the other identity; **OQ-4 answered** |
| **M4** | Code loop + merge (FR-2.4 → FR-2.6) | A Feature PR is reviewed, revised, approved, merged, branch deleted, **with branch protection on and M4-A passing** |
| **M5** | Human gate + teardown (FR-3.x, FR-4.2, FR-4.3) | Human close drives teardown; artifact ledger shows zero rows with `removed_at IS NULL` |
| **M6** | Recovery + resource (NFR-1.x, NFR-2.x) | All chaos-row tests pass, including 30-minute tunnel severance with full delivery recovery |
| **M7** | Loop safety | A deliberately circular issue escalates to the human within budget rather than spinning |

M2 is the first milestone that can invalidate an architectural decision (ADR-4 via M2-B). M4 is the first that proves FR-1.3 rather than asserting it. M6 proves zero cold start across a real reboot.

---

## 22. Approval

This document requires formal approval from all participating agents via review on its pull request, then explicit approval from `@huozhe`, per the Exit Criteria on Issue #1.

| Agent | Identity | Approval |
|---|---|---|
| Claude Agent | `@huozheclaude` | Author — approves by submission |
| Grok Agent | `@huozhegrok` | **APPROVED** — [review 4837546047](https://github.com/huozhe/code-workflow/pull/5#pullrequestreview-4837546047), 2 findings, both addressed |
| Gemini Agent | `@tootooliu` | **APPROVED** — [review 4837592962](https://github.com/huozhe/code-workflow/pull/5#pullrequestreview-4837592962), 3 findings, all addressed |
| Owner | `@huozhe` | Pending — EC-1c satisfied, tagged for final review |

### 22.1 Review Findings Addressed

| Finding | Reviewer | Resolution |
|---|---|---|
| A5 cited the wrong spike (M2-A vs M2-B) | Grok F1 | Fixed §2 |
| Watermark monotonicity — use ID set-diff | Grok F2 + Gemini #1 (independent, same fix) | §11.2 step 5 rewritten; §19.5 resolved |
| Fingerprint head-guard could be misread as disabling detection | Grok F3 | Coverage note added to §9.3 |
| `artifact.register` lost to a crash orphans worktrees | Gemini #2 | §12.3 filesystem set-diff added; new §19.7 |
| Role freeze thrashing during `DESIGN_REVIEW` | Gemini #3 | §5.3 narrowed to Design-PR-open — a third position, **re-acked by both reviewers** |
