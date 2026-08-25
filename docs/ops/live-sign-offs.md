# Live sign-offs — what is still unproven, and the one session that proves most of it

**Status:** 2026-08-25. Six items discharged on the live session for #57 (2026-08-24/25).
**#173 and #172 are not**, and both failed for reasons no fixture had reached. Owner action
(`@huozhe`) on what is left.

Several merged and deployed changes carry an acceptance item that no fixture can
discharge: it has to be observed on a real session, against the real gateway.
This note exists because that list has been kept in `.claude/STATE.md`, which is
**gitignored** (#183) and therefore per-checkout — a fresh session in another
working copy reads a different, older answer, or none. Which host is deployed and
what pid it runs belong there. *Which acceptance is unproven, and what would prove
it,* is true regardless of whose tree you are in, so it belongs here.

## The one thing worth knowing first

**These are one exercise, not five.** A single real session, run to a turn and a
teardown, produces most of the evidence below. Scheduling them separately spends
four sessions on one exercise.

It does **not** produce all of it, and the gaps are not obvious:

| When | Discharges | Why it lands there |
|---|---|---|
| Immediately | `DEFAULT_IMAGE = 1.3.0` creates a container; **#171** | Both are `ensure_session` facts. `_prepare_project_issue_layout` (`supervisor.py:823`) runs `resolve_base_ref` + `worktree_add` for both roles, and its only callers are `_ensure_session_locked` (`:407`) and `_adopt_or_promote` (`:595`) — **before any turn is dispatched**. #171's acceptance says the same: *of a fresh session, with no agent action* |
| During, **on the first turn that opens the Design PR** | **#156** | The counter's subject is that turn. A session whose early turns defer, hit `role_busy`, or are intake turns leaves #156 unproven while the exercise looks like it is progressing |
| Only if it outlives a **5-minute** reconcile pass, landing while **no turn is open** | **#116** | The reconciler is a timer (`RECONCILE_INTERVAL_S`), not something a session triggers — a short session never shows them. For #116 duration is necessary and not sufficient: `_probe_attachments` (`reconciler.py:377`) skips any project in `inflight` with `reason="open turn"` and reports `probe_skipped`, never `attached`, because a runner busy in a turn can miss the 2 s timeout and emit a WARNING that lies. A continuously busy session never produces `attached=1`, however long it runs |
| At close | **nothing** — see #172 below | ~~`session.teardown` → `shutdown_all()` → `_kill_unlocked` is the reliable half~~. **Wrong.** The gateway never sends `session.teardown`: its whole RPC vocabulary is `session.init`, `session.resume`, `session.attach`, `health.ping`. The kill path has no caller, so nothing lands here. On #57 the CLIs died when the reconciler removed the container 2m25s after close (`reason=no live session`), and `grep -ic kill` over a 12-hour log returned **0** |

**On what "cannot be forced" turned out to mean.** This section used to name two
such items. Both were wrong, in opposite directions.

**#168 was called unforceable and it simply happened** — 2026-08-24 22:53:52, an
Architect turn that had already merged the Design PR, refused mid-turn. Waiting
cost nothing; a long session was enough. Plan for it opportunistically, but do
not treat it as out of reach.

**#172 was called an error path that a healthy turn never enters.** The real
reason is worse: the kill has **no caller at all**, so no turn of any kind can
reach it. Length was never the obstacle.

**#57 was a different exercise, and it needed no planning at all.** The operator
half of M4-A is a Feature PR opened by the Developer and approved by an Architect
turn from inside its own container, with the producing `turn_id` in the review
body. The paragraph that added this row said to plan that round rather than wait
for it — and the very session that wrote the row produced the pair on its way past,
on PR #213. The prediction was right about the mechanism: the ordinary design loop
yields it whenever a Feature PR is reviewed, so it costs nothing extra.

## The ledger

Six rows closed on the live session for #57, 2026-08-24/25 — including #57's own,
added mid-session by the design round that then satisfied it. Two did not close;
the `Result` column says which. Evidence for each is in the issue.

| Issue | ADR | What must be observed | Result |
|---|---|---|---|
| **#116** | ADR-34 | The reconciler's probe reaching a runner **inside a real container over a published port**: `attached=1` with a real `rss_bytes` in the pass report | **Discharged** 22:26:12 — `attached=1 probe_skipped=0`, payload `rss_bytes=25440256` over `127.0.0.1:33083`, reproduced on three later passes. Read #212 before writing a rule against those numbers: `rss_bytes` is a high-water mark and `cli_rss_kb` is frozen at turn end |
| **#172** | ADR-35 | A **real vendor CLI** (`claude`/`grok`) killed by the runner, with no descendant surviving | **Not discharged, and not reachable.** The gateway never sends `session.teardown`, so `shutdown_all()` → `_kill_unlocked` has no caller. Another live session will not produce it; wiring the call must come first. Note both CLIs had **zero children** at close, so a kill there would satisfy "no descendant survived" only vacuously — the acceptance needs a case that can reach it |
| **#171** | ADR-31 | Both role worktrees `behind=0` on the live clone, observed on a **fresh** session with no agent action | **Discharged** 22:52:19 with both sides captured: clone at `85107b4` with `FETCH_HEAD` 4 days stale before the label, `bb8aa5c` and both worktrees `behind=0 ahead=0` after. The fetch happened inside `ensure_session`, and the triggering delivery was dropped `self-echo`, so *no agent action* is literal |
| **#156** | ADR-32 | A real turn that opens a Design PR leaves `silent_turns` at 0 | **Discharged** 22:06:46 — turn opened Design PR #207, `silent_turns` 0. `turn_count` 0→1 proves `bump_turn_counters` ran (same SQL statement), `status='done'` rules out `keep`, and `inc` would have read 1 — so the mode was `reset`, from observed progress |
| **#173** | ADR-30 | A rework round logs `author-sent PR event, no turn`, and `silent_turns` never exceeds 1 | **Failed.** First half held on webhooks (two kinds took the branch). Second half did not: the reconciler synthesised the same reviews with no `pull_request.user`, so `_pr_author_login` returned `None`, the guard short-circuited, and two no-op turns pushed `silent_turns` to **2**. See #209 — the one-line fix there is a trap |
| **#168** | ADR-33 | A real quota refusal recorded `quota_exhausted`, the delivery re-picked after the hold, the session continuing **without pausing meanwhile** | **Discharged** 22:53:52, unforced. The refusing turn had already merged Design PR #207 — the *after real work* case exactly. Status `quota_exhausted` not `failed`; `paused_reason` stayed `NULL` and two Developer turns ran; delivery re-picked at **02:30:00**, precisely `retry_after` |
| **#57** | ADR-36 | Operator half of M4-A: a Feature PR opened by the Developer, approved by an Architect turn from inside its own container, review body carrying the producing `turn_id`, checkable against `turns` (`role=architect`, `submitted_at` inside `[started_at, ended_at]`) | **Discharged** on this same session, 2026-08-25. Feature PR #213 opened by `huozhegrok` (Developer); approved 16:34:17Z by `huozheclaude`; review body carries *"Produced by turn `t-2095e4fce899` (architect), per §5.5.2"*; `turns` has `t-2095e4fce899` `role=architect` spanning **09:32:40–09:34:41 PDT**, and the review lands at 09:34:17 — inside the window. The row was added by the design round that then satisfied it |

## What the run surfaced

Seven defects, none reachable by a fixture. Filed unlabelled — the `agentd` label
opens a session.

| # | Defect |
|---|---|
| #208 | Stale-drain logs benign vendor notifications at WARNING, burying the discarded-`result` signal the level exists for |
| #209 | Reconciler-synthesised reviews omit `pull_request.user`, bypassing ADR-30's author-sent guard. The obvious one-line fix silently stalls the loop instead |
| #210 | Runner pid 1 never reaps adopted children: **175 zombies** in one session, against a `pids.max` of 1024 |
| #211 | A dropped `pull_request.synchronize` strands a session, and the reconciler has no node kind to regenerate it |
| #212 | The probe's `rss_bytes` is a high-water mark and `cli_rss_kb` is frozen at turn end — unfit for M6-3's memory rule |
| #214 | `CHANGES_REQUESTED` pauses the session instead of dispatching Developer rework; the pending delivery then re-defers every 5 s (**4,417 times**) |
| #172 | The kill path has no caller — see the row above |

**#211 is the one to fix first.** Webhook delivery failed five times in one evening
(`failed to connect to host` — the ingress is a Tailscale funnel). Four were
recoverable; the `synchronize` was not, and losing it desynced the FSM from GitHub
until an agent acted on the true state — producing, in order, a deadlock, a stall
pause, a recorded **§8.4 merge bypass** that was not a bypass, and a session
archived `class=ABANDONED` whose Feature PR had merged to `main`. Everything
reasoning from FSM state is wrong for as long as the gap lasts, and nothing detects
the divergence.

## What is not in this ledger, and why

Two open issues are deliberately absent. Both would be plausible rows, and the
reasons they are not differ.

**#192 — a deploy with no signature is not an unproven acceptance.** Its exit
condition is *"there is no `kind` for which GC and the teardown confirm path
disagree"*, and that is settled deterministically by tests: both paths refuse a
kind outside `ARTIFACT_KINDS`, and the fixtures drive the real writer. What the
deploy lacks is a *signature* — no counter changes shape, no image to inspect, and
its one runtime observable is a WARNING that fires only when an agent registers an
unknown kind. Waiting to observe that WARNING is not a sign-off; it is waiting for
a bug. A well-behaved agent never emits an unknown kind, and if none ever does,
that is the system working rather than an acceptance going unproven. #192 appears
in the fixture section below as an *example* of a signature-less deploy, which is
a different thing from an item this ledger is holding open.

**#147 — an owner repair, not a sign-off.** What it needs is data: sessions `#32`
and `#84` are `CLOSED` carrying a stale `paused_reason`. Nothing is observed on a
live session; something is corrected. It also **must not be closed on a quiet
log** — its counters read clean only because `agentctl` ran a manual
`quarantine_deferred`, and a handoff note once said otherwise and was wrong.

The distinction the two share: this ledger holds **acceptances that no fixture can
reach**. It is not a list of open issues, and it is not a list of things that were
hard to verify.

## Running the exercise

1. Confirm nothing is live first — a session in flight makes every observation
   below ambiguous:
   ```bash
   cd agentd && AGENTD_ROOT=$HOME/.agentd uv run python -c "from agentd.db import Store; import pathlib; \
     s=Store(pathlib.Path.home()/'.agentd'/'state.db'); \
     print([(r['session_key'],r['state']) for r in s.list_sessions() if r['state']!='CLOSED'])"
   ```
2. **Labelling an issue `agentd` opens a live session** (#67) — but the label
   alone does not start any work. Two separate things have to happen, and the
   label is only the first:
   - **The session comes up.** `ensure_session` fetches the clone, adds both role
     worktrees and creates the container. This runs on the label event whoever
     sent it — which is why #171 and the `DEFAULT_IMAGE` check land here, and why
     they are observable with *no agent action at all*.
   - **The first turn is dispatched** — and only by an event that **routes**.
     `route_for_recipient` (`routing.py:74`) drops a sender equal to the
     recipient as `self-echo` — rule 1, ahead of every other rule. An agent
     labelling an issue whose recipient role is itself is exactly that case:
     the session comes up complete, and no turn is ever dispatched.

   So **the owner, or another human, must send the routable event** — rule 2
   (owner) or rule 7 (other human); an issue comment is enough. An agent cannot
   start its own session's first turn, and **#156 and #173 stay unreachable until
   someone does**, however healthy the session looks.

   Observed on session #57, 2026-08-24: the Architect's own login applied the
   label. Worktrees, container and health ping all succeeded, then
   `design_loop route drop after fsm … reason=self-echo kind=issue_opened
   state=PLANNING`, and `turn_count` stayed 0 until the owner commented.

   File anything you do not want run as unlabelled.
3. Let it run **past five minutes** if #116 and #173 matter to you, and to a real
   teardown if #172 does. For #116, also leave it **idle across a pass** — the
   probe is skipped while a turn is open, so a session you keep feeding never
   produces `attached=1`. The pass line tells you which you got:
   `probe_skipped=1` is a skip, not a sample.
4. INFO goes to `~/.agentd/logs/agentd.log`. `gateway.err.log` is WARNING and
   above, so a pass line is not in the file the plist names as stderr.

## Why this list keeps being written down wrong

Every item here was at some point believed discharged on evidence that could not
reach the mechanism. The rule that catches it — CLAUDE.md's *a null or green
result is only as good as its fixture* — earned six more instances across #116 and
#172, and the shape is always the same: the fixture could not **reach** the case it
was named for, and the result looked like evidence rather than absence.

**This file has now done it twice.** The second time was the #172 row: it named a
chain — `session.teardown` → `shutdown_all()` → `_kill_unlocked` — and called it
*the reliable half*, without anyone checking that the gateway sends that verb. It
does not. A reader following that row would have watched a teardown for a kill
that cannot occur, seen the container vanish and the CLIs with it, and marked the
item discharged. Every visible signal agreed: teardown logged confirmed removals,
the session archived, no CLI survived. `grep -ic kill` returned 0.

**And once before that.** An earlier draft put #171 under *During*
while the same row's advice read "watch the first `ensure_session` repair it" —
the two columns disagreed, and the one that was wrong was the one a reader acts
on. Someone following it would have watched a turn for something that had already
happened, then read `behind=0` as evidence without knowing whether they saw the
repair or arrived after it. A document whose columns disagree is the prose form of
a suite whose fixture cannot reach its case: both fail by looking complete. Check
the *how* against the *when* before trusting either.

Three forms are worth naming because none of them looks like a fixture at all:

- **A reproduction that fails.** "Cannot reproduce" is a null result. Prove the
  fixture holds the property first — a leader that also traps `SIGTERM`, or a
  polled child that has already been reaped, removes the very condition under test.
- **A deploy with no signature at all.** #192 is the sharpest case: gateway-only,
  no counter changes shape, no image to inspect, and its one observable — a WARNING
  when an agent registers an unknown artifact kind — needs a live session to fire.
  The whole evidence is *restart-after-merge on an editable install*. That is worth
  recording as evidence, and worth never calling verification.
- **A contaminated working tree.** A `src/` staged from the branch under review
  silently executes the fix during a "pre-fix" reproduction. *Verify what your tree
  contains before believing a reproduction,* not only what your fixture contains —
  a staged checkout looks like a checkout, so none of the usual suspicion fires.
