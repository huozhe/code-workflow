# Live sign-offs — what is still unproven, and the one session that proves most of it

**Status:** open, 2026-08-22. Owner action (`@huozhe`).

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
| Only if it outlives a **5-minute** reconcile pass — and, for #116, one that lands while **no turn is open** | **#116**, **#173** | The reconciler is a timer (`RECONCILE_INTERVAL_S`), not something a session triggers — a short session never shows them. For #116 duration is necessary and not sufficient: `_probe_attachments` (`reconciler.py:377`) skips any project in `inflight` with `reason="open turn"` and reports `probe_skipped`, never `attached`, because a runner busy in a turn can miss the 2 s timeout and emit a WARNING that lies. A continuously busy session never produces `attached=1`, however long it runs |
| At close | **#172**'s kill | `session.teardown` → `shutdown_all()` → `_kill_unlocked` (`server.py:417`) is the reliable half |

**Two cannot be forced and must not be counted on the plan.** The reasons are
different and both are worth stating, because "run it longer" fixes neither:
**#172's deadline kill** is an **error path** — a healthy turn never enters it, so
no amount of session length produces one; **#168** depends on an **external**
event, a real vendor quota refusal, which no local action triggers. Opportunistic
only.

**#57 is a different exercise, and it is plannable.** The operator half of M4-A
is a Feature PR opened by the Developer and approved by an Architect turn from
inside its own container, with the producing `turn_id` in the review body. A
session run to teardown never produces that pair; the ordinary design loop does,
deliberately, whenever a Feature PR is reviewed. Plan that round; do not wait
for it as if it were #168.

## The ledger

| Issue | ADR | What must be observed | Not discharged by |
|---|---|---|---|
| **#116** | ADR-34 | The reconciler's probe reaching a runner **inside a real container over a published port**: `attached=1` with a real `rss_bytes` in the pass report | Any unit fixture. Everything run so far is a socket stub or an in-process server |
| **#172** | ADR-35 | A **real vendor CLI** (`claude`/`grok`) killed by the runner, with no descendant surviving | Every kill driven so far was against `sh` |
| **#171** | ADR-31 | Both role worktrees `behind=0` on the live clone, observed on a **fresh** session with no agent action | Arriving after the fact: the repair happens inside the first `ensure_session`, so a `behind=0` read later does not tell you whether you saw it repaired or saw it already fine. Nor by repairing it by hand — that leaves nothing to observe |
| **#156** | ADR-32 | A real turn that opens a Design PR leaves `silent_turns` at 0 | A unit fixture trips the same log line without exercising the counter's subject |
| **#173** | ADR-30 | A rework round logs `author-sent PR event, no turn`, and `silent_turns` never exceeds 1 | As above |
| **#168** | ADR-33 | A real quota refusal recorded `quota_exhausted`, the delivery re-picked after the hold, the session continuing **without pausing meanwhile** | Cannot be forced; opportunistic only |
| **#57** | ADR-36 | Operator half of M4-A: a Feature PR opened by the Developer, approved by an Architect turn from inside its own container, review body carrying the producing `turn_id`, checkable against `turns` (`role=architect`, `submitted_at` inside `[started_at, ended_at]`) | The live M4-A test (Developer token only after ADR-36 (d)). A hand-run probe. PR #55. |

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

**This file has already done it once.** An earlier draft put #171 under *During*
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
