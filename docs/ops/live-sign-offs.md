# Live sign-offs — what is still unproven, and the one session that proves most of it

**Status:** 2026-08-31. Six items discharged on the live session for #57 (2026-08-24/25).
**#173 and #172 are not.** The seven defects that run surfaced are now fixed and deployed, and each
carries a live acceptance of its own — so the list of what is unproven grew rather than shrank. They
are one session, and it is scheduled per [Running the exercise](#running-the-exercise). Owner action
(`@huozhe`).

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
| At close | **#172, partly** | **#221 (`101c3c1`) wired the caller, and #159's teardown ran it: no vendor CLI for either role remained.** That half is observed. The *descendant* half is not, and this row is no longer the route to it — `_run_teardown_turns` runs **two full LLM turns before** the kill (`session_loop.py:2630` vs `:2920`), so a child alive at close is dead long before `_kill_unlocked`. Attempting it on #159 captured a real `claude → bash → pip` tree at close and a kill minutes later that met nothing. See *Why the descendant clause is not a live observation* |

**On what "cannot be forced" turned out to mean.** This section used to name two
such items. Both were wrong, in opposite directions.

**#168 was called unforceable and it simply happened** — 2026-08-24 22:53:52, an
Architect turn that had already merged the Design PR, refused mid-turn. Waiting
cost nothing; a long session was enough. Plan for it opportunistically, but do
not treat it as out of reach.

**#172 was called an error path that a healthy turn never enters.** The real
reason was worse: the kill had **no caller at all**, so no turn of any kind could
reach it. Length was never the obstacle. **#221 (`101c3c1`) wired the caller**, and
#159 then ran the kill five times — so the obstacle moved twice: first to the *live
child*, and then, once three attempts at that failed, out of live observation
altogether. The remaining clause is a fixture question, not a session question. See
*Why the descendant clause is not a live observation*.

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
| **#172** | ADR-35 | A **real vendor CLI** (`claude`/`grok`) killed by the runner, with no descendant surviving | **Kill observed five times; descendant clause not, and not reachable live.** On #159 the path ran for the first time in production — `grep -ic kill` over #57's whole log had returned **0**. Three turn-deadline kills (pids `20149`, `22817`, `25154`, each confirmed absent from `/proc` after, `status=failed` / `cli killed`, nothing orphaned, role respawnable) and one `session.teardown` leaving **no vendor CLI for either role**. What remains is *no descendant survived*, and three attempts show live observation cannot reach it — see the section below. Needs an in-image fixture that holds a child open deliberately. Teardown also surfaced **#233** |
| **#171** | ADR-31 | Both role worktrees `behind=0` on the live clone, observed on a **fresh** session with no agent action | **Discharged** 22:52:19 with both sides captured: clone at `85107b4` with `FETCH_HEAD` 4 days stale before the label, `bb8aa5c` and both worktrees `behind=0 ahead=0` after. The fetch happened inside `ensure_session`, and the triggering delivery was dropped `self-echo`, so *no agent action* is literal |
| **#156** | ADR-32 | A real turn that opens a Design PR leaves `silent_turns` at 0 | **Discharged** 22:06:46 — turn opened Design PR #207, `silent_turns` 0. `turn_count` 0→1 proves `bump_turn_counters` ran (same SQL statement), `status='done'` rules out `keep`, and `inc` would have read 1 — so the mode was `reset`, from observed progress |
| **#173** | ADR-30 | A rework round logs `author-sent PR event, no turn`, and `silent_turns` never exceeds 1 | **Failed.** First half held on webhooks (two kinds took the branch). Second half did not: the reconciler synthesised the same reviews with no `pull_request.user`, so `_pr_author_login` returned `None`, the guard short-circuited, and two no-op turns pushed `silent_turns` to **2**. See #209 — the one-line fix there is a trap |
| **#168** | ADR-33 | A real quota refusal recorded `quota_exhausted`, the delivery re-picked after the hold, the session continuing **without pausing meanwhile** | **Discharged** 22:53:52, unforced. The refusing turn had already merged Design PR #207 — the *after real work* case exactly. Status `quota_exhausted` not `failed`; `paused_reason` stayed `NULL` and two Developer turns ran; delivery re-picked at **02:30:00**, precisely `retry_after` |
| **#57** | ADR-36 | Operator half of M4-A: a Feature PR opened by the Developer, approved by an Architect turn from inside its own container, review body carrying the producing `turn_id`, checkable against `turns` (`role=architect`, `submitted_at` inside `[started_at, ended_at]`) | **Discharged** on this same session, 2026-08-25. Feature PR #213 opened by `huozhegrok` (Developer); approved 16:34:17Z by `huozheclaude`; review body carries *"Produced by turn `t-2095e4fce899` (architect), per §5.5.2"*; `turns` has `t-2095e4fce899` `role=architect` spanning **09:32:40–09:34:41 PDT**, and the review lands at 09:34:17 — inside the window. The row was added by the design round that then satisfied it |

## What the run surfaced

Seven defects, none reachable by a fixture. Filed unlabelled — the `agentd` label
opens a session.

**All seven are now fixed and deployed, and that is why this list did not shrink.**
Each fix carries a live acceptance its own tests cannot reach, so the ledger above
gained eight rows rather than losing seven — #173 comes back with them, because its
failure *was* #209. A deploy is evidence and never verification, and the third
column below is what would make it verification.

| # | Defect as filed | Fixed in — and what must now be observed |
|---|---|---|
| #208 | Stale-drain logs benign vendor notifications at WARNING, burying the discarded-`result` signal the level exists for | Runner **1.4.0** (#219). No `WARNING … stale frame discarded … _x.ai`, **and** at least one INFO `stale frames discarded … notifications=N of M`. Both halves: zero warnings with no `notifications=` line means the drain never ran |
| #209 | Reconciler-synthesised reviews omit `pull_request.user`, bypassing ADR-30's author-sent guard. The obvious one-line fix silently stalls the loop instead | **#220**. A synthesised review from the PR's own author logged `author-sent PR event, no turn (#173)` |
| #173 | *(re-opened by the above)* A rework round logs `author-sent PR event, no turn`, and `silent_turns` never exceeds 1 | Same **event** as #209, **two reads.** The log line is #209's half; #173's is `SELECT silent_turns` on the session row, and the grep does not stand in for it. On #57 the log half held and the counter half failed at `silent_turns` **2** |
| #210 | Runner pid 1 never reaps adopted children: **175 zombies** in one session, against a `pids.max` of 1024 | Runner **1.4.0** (#219). Zombie count **flat** across turns, not merely small |
| #211 | A dropped `pull_request.synchronize` strands a session, and the reconciler has no node kind to regenerate it | ADR-37, #222 + #223. A completed rework round leaves `silent_turns` 0 or 1, never 3, and records no `unauthorized` on a merge that had Architect approval on the live head |
| #212 | The probe's `rss_bytes` is a high-water mark and `cli_rss_kb` is frozen at turn end — unfit for M6-3's memory rule | Runner **1.4.0** (#219). `rss_bytes`, `rss_peak_bytes` and `sampled_at` all present **and `rss_bytes` moving between two passes**. One sample cannot show movement |
| #214 | `CHANGES_REQUESTED` pauses the session instead of dispatching Developer rework; the pending delivery then re-defers every 5 s (**4,417 times**) | ADR-38, #224 + #225, schema v11. An Architect verdict that replaces an approval dispatches a Developer rework turn, the session stays out of `PAUSED_HUMAN`, one `merge_auth superseded` logs, and no delivery id exceeds ~20 `route defer` lines an hour |
| #172 | *(as filed)* The kill path has no caller | **#221** gave it one, and #159 ran it five times. Only *no descendant survived* is left, and it needs an in-image fixture rather than a session |
| #229 | A `*_pr_opened` the FSM never sees strands the session permanently — the gate dropped a delivered one, the funnel lost another | ADR-40, **#237**, deployed 2026-08-31. A real `design_pr_opened` whose **payload `head.sha` is not the PR's live head at drain time** is taken: one `fsm … → DESIGN_REVIEW`, `design_pr` non-NULL and `roles_locked=1` within that drain, and **no** `stale head … (design_pr_opened)` anywhere in the log. **Show the two SHAs differ before reading the outcome** — equal SHAs mean the case was never reached, whatever the FSM did. See *Window 0 — how to run it* |

**#229 is window 0, and it is the one that cannot be retried.** The gate half only misbehaves when the
payload SHA is stale by the time the delivery drains, so the observation has to be made on a PR whose head
moved inside that gap. On #159 the gap was 4m23s, because the Architect's first turn ran 601 s and the drain
is one thread behind it. Open the PR onto an idle drain and the gap is a couple of seconds: the event is
taken correctly by code that was never broken, which is exactly the green signal the defect produced eight
turns in a row.

**ADR-40 puts the serialized drain out of scope — "the trigger, not the defect" — so the turn is not the
condition; the stale SHA is.** Item 12 was first written as "while a turn is running", which describes how
#159 produced it rather than what has to be true. Any honest way of parking the drain counts, which is what
makes this schedulable at all: waiting for an agent to push at the right moment is luck, not a procedure.

The second half of #229 — a lost `opened` recovered by the sweep's discovery — has no window at all: it needs
a *dropped* delivery, which cannot be arranged from this side. Unit acceptance covers it; a live observation
would be luck, not a test, and it is not listed as one.

**#211 was the one to fix first, and it was.** Webhook delivery failed five times in
one evening (`failed to connect to host` — the ingress is a Tailscale funnel). Four
were recoverable; the `synchronize` was not, and losing it desynced the FSM from
GitHub until an agent acted on the true state — producing, in order, a deadlock, a
stall pause, a recorded **§8.4 merge bypass** that was not a bypass, and a session
archived `class=ABANDONED` whose Feature PR had merged to `main`. Everything
reasoning from FSM state is wrong for as long as the gap lasts, and nothing detects
the divergence. ADR-37 recovers the lost event from the forge's live head; the
transport itself is unchanged and still owner ops.

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
3. Duration alone is not a plan. The open acceptances fall into **five windows**,
   and two of them contradict each other — see *The five windows* below. Do not
   also carry a second copy of the timing rules in your head: a session run
   "until it looks done" gets **0 and A** and silently misses C and D, which is
   how #57 discharged six and left two. **Window 0 is the trap in that sentence.**
   It is the *first* window and it is spent the instant the Design PR exists, so a
   session you join, resume, or start after that PR is open has already lost it —
   and every later turn still looks green, because the code that runs then was
   never the broken code. If you cannot show the payload SHA differed from the live
   head at drain time, you do not have #229; you have A. **And window 0 does not
   happen by itself** — the lag has to be staged, see *Window 0 — how to run it*.
4. INFO goes to `~/.agentd/logs/agentd.log`. `gateway.err.log` is WARNING and
   above, so a pass line is not in the file the plist names as stderr. **The
   runner logs to neither** — it runs in the container, so #208 and #210 are read
   with `docker logs` / `docker exec`, not with `grep` over the gateway log.
   From the merge of this Feature PR (2026-08-27), the lifecycle module logs as
   `agentd.session_loop`; archives older than that carry `agentd.design_loop`.
   The documented greps in this file key on message text and are unaffected.

### The five windows

Container `agentd-huozhe-code-workflow`; roles are uid **1001** architect, **1002**
developer.

| Window | Discharges | Condition | Conflicts with |
|---|---|---|---|
| **0** — a Design PR `opened` drains **stale** | #229 | the head must move **before the drain reaches the `opened`**; staging the lag is the whole exercise | — |
| **A** — any real turn | #208, #210 | passive; the vendor CLI only has to run | — |
| **B** — a completed rework round | #211, #214 | a `synchronize` (real or synthesised) that yields a review turn | — |
| **C** — a reconcile pass landing with **no turn open** | #212, and #209/#173 if forced | the session must sit **idle ≥ 5 min** | D |
| **D** — close the issue | *(nothing — see below)* | ends the session | — |

Window 0 happens once, at the start, and cannot be re-run without a new session. C needs the
session idle and D is terminal, so **the order is 0 → A → B → C → D** and D is last. D no longer discharges anything: the close is how the session ends, not how #172
is observed — see *Why the descendant clause is not a live observation*.

**Window 0 — how to run it.** Everything is real: real gateway, real ingress, real
PR on a real role branch, real session row. The only staged thing is the lag, and it
has to be staged because the drain is **one thread** — `drain_once` → `process_deferred_batch`
→ `_process_one`, which blocks inside the turn RPC. Nothing else parks it. An
open-turn row in the DB does not: the block is the thread, not a flag. The disk
breaker does, but `Governor.sample_once` resets it within **≤ 30 s** whenever free
disk is healthy, so it gives an uncontrolled sub-30-second window and is not the tool.

**Use a real turn as the lag.** It costs one turn and buys 15 s to ~12 min.

1. Confirm nothing is live (step 1 above), then have **the owner** label a throwaway
   issue `agentd`. **The actor is load-bearing and it is not the Architect.** Step 2
   above has the rule and the instance: `route_for_recipient` drops sender == recipient
   as `self-echo` (rule 1, ahead of everything), `issue_opened` routes to the Architect,
   so an Architect-applied label brings the session up complete and dispatches **no
   turn at all** — `turn_count` stayed 0 on #57. No turn, no lag, and window 0 is
   spent without ever being entered. Owner (rule 2) or any other human (rule 7) works;
   a Developer label routes too, since sender ≠ recipient. Only the Architect must not.
   The session comes up in `PLANNING` and the first Architect turn dispatches. **That
   turn is the lag** — the drain is now behind it, for every session, not just this one.
2. While it runs, push a commit to `agentd/<proj>/<issue>/architect` and open a PR
   from it. The `opened` queues at SHA **X**.
3. Push again. The head moves to **Y**; the `synchronize` queues behind the `opened`.
4. Let the turn end. The drain resumes and reaches the `opened`.

**Read the fixture before the outcome.** This is the step that separates the
observation from #159's green log, where every signal read fine for eight turns:

```bash
# payload SHA the delivery carries (X) — must NOT equal the live head (Y)
cd agentd && AGENTD_ROOT=$HOME/.agentd uv run python -c "
from agentd.db import Store, decompress_payload
import json, pathlib
s = Store(pathlib.Path.home()/'.agentd'/'state.db')
for r in s.list_deliveries_for('<repo>', [<PR>]):
    d = json.loads(decompress_payload(r['payload']))
    if d.get('action') == 'opened' and 'pull_request' in d:
        print(r['delivery_id'][:12], r['issue_num'], d['pull_request']['head']['sha'])"
gh api repos/<repo>/pulls/<PR> --jq .head.sha      # live head (Y)
```

**The check is known to reach its case, because the case exists in this database.**
Run against #159's own stranded delivery it prints `420fa4e0-a1d 227 2f9d710e3371`,
while PR #227's head is `0da33933fa03` — differing SHAs, on the delivery the gate
actually dropped. A check that cannot produce a positive on the one instance we have
is not a check; this one does.

Only when the SHAs differ, assert:

```bash
SK="huozhe/code-workflow#<throwaway issue>"
grep -hF "fsm $SK → DESIGN_REVIEW" ~/.agentd/logs/agentd.log*    # one, on this session
grep -hF "$SK" ~/.agentd/logs/agentd.log* | grep -c "stale head .*design_pr_opened"  # 0
```

**Read `agentd.log*`, not `agentd.log`.** The log rotates at ~1 MB and keeps `.1`–`.5`;
a rotation inside the exercise moves the line you are looking for into a file the plain
path does not name. That failure is silent in the worst direction — a `stale head` count
of `0` then means *wrong file*, not *not dropped*, and window 0 cannot be re-run to find
out. Checked while writing this: `agentd.log` currently holds **zero** `fsm … →` lines;
every one of them is in `agentd.log.1`.

**Scope both greps to the throwaway session key.** An unscoped `DESIGN_REVIEW` count is
wrong the moment any session does a rework round — `fsm huozhe/code-workflow#159 →
DESIGN_REVIEW` appears **twice** across the logs — and an unscoped `stale head` count
picks up other sessions' legitimate drops of `*_revised`, which this acceptance does not
touch. Scoped, expect exactly one for this exercise because the throwaway session stops
at window 0; carry it into a rework round and a second line is correct, not a failure.

**Both greps are known to reach their case, on the same instance the SHA check uses.**
Run scoped to `huozhe/code-workflow#159` across `agentd.log*` they return **2** for
`DESIGN_REVIEW` (the initial take plus the rework round) and — the one that matters —
**1** for `stale head … design_pr_opened`. That single line *is* #229: the drop this
acceptance exists to prove no longer happens. A "must be 0" check that has never
produced a 1 anywhere is indistinguishable from a typo.

Plus `design_pr` non-NULL and `roles_locked=1` on the session row **within that drain**,
not eventually.

Cost is ~2–3 vendor turns: the Architect turn that creates the lag, and the Developer
review turn the take dispatches. No image rebuild. If the turn ends before step 3
lands, the `opened` drains onto an idle gap and window 0 is spent — start a new issue
rather than reading the green line.

**Window A.** Sample the zombie count **while a turn is running**, at least twice,
and the acceptance is *flat*, not *small*:

```bash
docker exec agentd-huozhe-code-workflow sh -c \
  'grep -l "^State:.*Z" /proc/*/status 2>/dev/null | wc -l'
```

Check that probe against its own case before trusting a `0` — in
`session-runner:1.4.0` a forked orphan makes it report `1` with `state=Z` in
`/proc/<pid>/stat`. A healthy container and a broken probe both print `0`.

**And then check *when* you ran it, which is the half that actually went wrong.**
A sample taken between turns reads `0` on a container that is accumulating
hundreds, because zombies are produced only while turns fork. #210 was closed on
exactly that reading, and two later mid-turn samples on the same container and
image showed **79** and **44** — the 44 taken 40 s into a single turn, so that is
the accrual rate, not a backlog. A verified probe run at the wrong moment is
still a null result; eliminating "broken probe" is not the same as eliminating
"nothing to see yet".

For #208, both halves:

```bash
docker logs agentd-huozhe-code-workflow 2>&1 | grep -c 'WARNING.*stale frame discarded.*_x\.ai'  # want 0
docker logs agentd-huozhe-code-workflow 2>&1 | grep    'stale frames discarded.*notifications='   # want >=1
```

**Window B.** `silent_turns` through the round, and the absence of a false bypass:

```bash
sqlite3 ~/.agentd/state.db \
  "SELECT state, silent_turns, turn_count, feature_pr, feature_pr_head FROM sessions WHERE issue_num=<N>;"
grep -n "unauthorized Feature PR merge" ~/.agentd/logs/agentd.log | tail -3
grep -n "merge_auth superseded" ~/.agentd/logs/agentd.log
grep -o "route defer id=[a-f0-9-]*" ~/.agentd/logs/agentd.log | sort | uniq -c | sort -rn | head -3
```

The last line is ADR-38's backoff: no delivery id should pass roughly 20 in an hour.
Pre-fix, one id reached **4,466** in 6h14m. #214 also needs the Architect to
actually replace an approval — put the *submit the verdict, then re-read the checks*
protocol in the session brief rather than hoping for it; that sequence is what
produced #57's instance.

**Window C.** Stop answering the session, then:

```bash
grep -n "reconcile pass" ~/.agentd/logs/agentd.log | tail -3     # want attached=1 probe_skipped=0
AGENTD_ROOT=$HOME/.agentd uv run agentctl reconcile --dry-run | python3 -m json.tool
```

The pass line carries counts only; `rss_bytes` / `rss_peak_bytes` / `sampled_at` are
in `attached[]` of the `agentctl` report, which is read-only and safe mid-session.
Take **two** samples minutes apart — #212's acceptance is that the value moves.
`probe_skipped=1` is a skip, not a sample.

#209 and #173 need a reconciler-*synthesised* review, which naturally requires the
real webhook to be lost. If none is, force it in this window by deleting that
review's node before a pass:

```bash
sqlite3 ~/.agentd/state.db "DELETE FROM delivery_nodes WHERE node_id='<PRR_… of that review>';"
grep -n "author-sent PR event, no turn" ~/.agentd/logs/agentd.log | tail -3   # #209's half
sqlite3 ~/.agentd/state.db "SELECT silent_turns FROM sessions WHERE issue_num=<N>;"  # #173's half
```

**One event, two reads.** The log line discharges #209; #173 is the counter, and
the grep does not stand in for it. On #57 the log half held and the counter reached
**2**.

**Record in both issues that it was forced.** It proves the drop, which is what both
acceptances name. It does not prove the loss.

**Window D.** Just close the issue. Tick the §10.1 block first — closing unticked
classifies the session `ABANDONED` rather than `VERIFIED`. Teardown takes minutes:
two LLM turns, then the kill.

## Why the descendant clause is not a live observation

Session #159 tried three times to observe #172's *"no descendant survived"* and could
not. The three failures have different causes and together they are the finding.

| attempt | route | why it could not reach the clause |
|---|---|---|
| 1 | close while a CLI held a child | `_run_teardown_turns` (`session_loop.py:2630`) runs **two full LLM turns before** `teardown_session` (`:2920`) issues the kill. The proc table at close was captured and held a real `claude → bash → pip/tail` tree; it was dead long before `_kill_unlocked` ran |
| 2 | turn-deadline kill, `turn_deadline_s = 60` | CLI was between tool calls. The sample taken within 300 ms of death showed **no children** |
| 3 | turn-deadline kill, `turn_deadline_s = 40` | same — and that sample carried **44 zombies**, so 44 subprocesses had been spawned and had already exited during those 40 s |

Attempt 3 settles it. The CLI was working tools hard the whole time; vendor tool
subprocesses are simply short — sub-second to a few seconds — while the kill lands at
a fixed wall-clock offset. **The overlap is not controllable from outside the
container.** A fourth attempt would be luck, not method.

What the same three runs *did* establish is most of the exit condition: five kills,
each confirmed by the CLI's absence from `/proc` rather than by the kill returning;
`status=failed` with `cli killed`; nothing orphaned; the role respawnable; and after
`session.teardown`, no vendor CLI for either role.

**So the remaining clause belongs in an in-image fixture**, which is what #172's own
checklist item 6 asks for: spawn a stand-in for the role CLI holding a child open,
call `_kill_unlocked` as the role, assert `/proc/<child>` is gone. The fixture must
first assert the capability set it runs under — measured in the live container,
`CapEff: 00000000000000c9` (CHOWN, FOWNER, SETGID, SETUID; **no** `DAC_OVERRIDE`, **no**
`KILL`) — or it is testing a host that cannot reach the case, which is the trap item 6
names.

**A note for whoever reads the teardown log while working on this.** #159's teardown
logged `session.teardown did not kill cleanly`, and that line is **false**: the kill
succeeded and the tmpfs secret wipe after it crashed, taking the RPC down with it
(**#233**). Do not read that warning as evidence about the kill.

## Why this list keeps being written down wrong

Every item here was at some point believed discharged on evidence that could not
reach the mechanism. The rule that catches it — CLAUDE.md's *a null or green
result is only as good as its fixture* — earned six more instances across #116 and
#172, and the shape is always the same: the fixture could not **reach** the case it
was named for, and the result looked like evidence rather than absence.

**This file has now done it twice.** The second time was the #172 row: it named a
chain — `session.teardown` → `shutdown_all()` → `_kill_unlocked` — and called it
*the reliable half*, without anyone checking that the gateway sends that verb. It
did not — not until **#221** wired it, months of rows later. A reader following
that row in the interval would have watched a teardown for a kill that could not
occur, seen the container vanish and the CLIs with it, and marked the item
discharged. Every visible signal agreed: teardown logged confirmed removals,
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
