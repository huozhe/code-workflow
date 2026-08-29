# ADR-38: A Withdrawn Approval Is Not an Unverifiable One — Supersede the Delivery, and Give `defer` a Clock

*A decision of record for `agentd`. Index and context: [unified design spec §16](../unified_design_spec.md#16-architecture-decision-records).*


*Fixes #214. Ref #147, #63, #57.*

**Owner decision (`@huozhe`): `CHANGES_REQUESTED` on a Feature PR must dispatch a Developer rework turn. It must not pause the session.** The issue's diagnosis — that the merge-authorization guard (`design_loop.py:708`) classifies that verdict as a permanent authorization fault — is correct and incomplete. It explains the pause. It does not explain why the rework the FSM already had a transition for (`fsm.py:78`) never ran, because `code_changes_requested` routes to the Developer today (`design_loop.py:3705`) and had done so twice in that same session. Two causes, and the second is ordering.

**The drain is serialized behind the running turn, so both reviews land in one batch, oldest first.** Read from `agentd.log.1`, session #57, PR #213, times PDT:

```
02:45:10  turn complete t-120615511cac        ← previous batch drains
02:47:03  delivery queued eb822240  pull_request_review.submitted   (APPROVED)
02:49:02  delivery queued 3341560a  pull_request_review.submitted   (CHANGES_REQUESTED)
02:49:21  turn complete t-bc7973fe621a        ← 4m11s of drain silence ends
02:49:21  dispatcher deferred eb822240 … deferred 3341560a          (same batch)
02:49:23  feature merge_authorized blocked (permanent) id=eb822240:
          latest review from huozheclaude is 'CHANGES_REQUESTED', not APPROVED
02:49:24  escalation session=#57 … comment_id=5408613551 resume=CODE_REVIEW
02:49:24  route defer id=3341560a reason=session paused; non-owner   ← 19 ms later
```

Nothing raced. `list_deferred` orders by `received_at ASC` (`db.py:893`) and `process_deferred_batch` walks that order (`design_loop.py:409`), so the **older delivery, whose premise the newer one had already withdrawn, is guaranteed to run first** whenever a turn is in flight across both reviews. It read live GitHub state, correctly saw `CHANGES_REQUESTED`, and paused — 19 ms before the delivery carrying that same verdict reached the router, which then found the session paused and deferred it 4,466 times over 6h14m. The withdrawn approval killed the session with the withdrawal sitting in the same batch.

**This is not the first instance, and the second one is worse.** `gateway.err.log:888`, session #63, 2026-08-18 23:58:07:

```
feature merge_authorized blocked (permanent) id=dc37fc80: latest review
  from huozheclaude is 'DISMISSED', not APPROVED
escalation session=#63 … resume=CODE_REWORK
```

`DISMISSED` is `dismiss_stale_reviews_on_push` doing its job after a rework push. `resume=CODE_REWORK` is the pre-pause state, so **the FSM had already processed that push and moved on**; the session was proceeding correctly and a leftover delivery paused it anyway. Same class, different superseding event, and it shows the rule is about the *delivery's premise*, not about one review state.

**The claim the guard is entitled to make is narrower than the one it makes.** "I cannot verify authorization to merge" and "this session cannot continue" are different claims (the issue's phrasing, and it is exactly right). Refusing to merge is correct and stays fail-closed. But a verdict that the counterpart has since replaced is not an anomaly needing a human — it is the ordinary outcome of a review round, with an ordinary next step already in the FSM and already routed. Escalation is for authorization that genuinely **cannot be established**: no review from the counterpart at all, a reviewer who is not the counterpart, a `dirty` merge, a required check that concluded `failure`, an unresolved thread. Not for a counterpart verdict.

**The design half has the same defect and no transient arm at all.** `design_approved_unverified` (`design_loop.py:648`) escalates on *any* `check.ok == False`, so on that half a network blip also pauses the session — `verify_design_approval` returns `ApprovalCheck(False, "GitHub API error: …")` with `transient` left at its default. Both halves call `_check_approver_on_head` (`verify.py:370`), which is where the superseded/permanent split belongs: one function, both halves, no second mechanism. That answers the issue's "worth checking whether the design and feature halves can share one path" — they already do, one level down.

**The options, with cost, and why four of them lose.**

| | Repair | Cost | Complexity | Why not (or why) |
|---|---|---|---|---|
| **A** | **Third classification `superseded` on `ApprovalCheck`, set in `_check_approver_on_head`; both guards drop the delivery instead of escalating** | One field, one function, two call sites | Low | **Chosen.** The claim moves to where the evidence is read. Design and Feature halves inherit it from the shared helper. Refusal to merge is untouched |
| **B** | Special-case `CHANGES_REQUESTED` at `design_loop.py:708` only | One `if` | Lowest | **Rejected.** Misses `DISMISSED` (#63, live), misses the design half entirely, and puts a review-state rule in the loop rather than in the verifier that reads reviews. Two more copies the next time a state is added |
| **C** | Guard re-dispatches the rework itself from the review list it already fetched | No extra GET | Medium | **Rejected, and the reason is mechanical.** Dedupe of a synthesized review is `delivery_nodes`, checked *only* by the reconciler (`reconciler.py:637`); a real webhook is deduped by `delivery_id` PK alone (`db.py:509`). A synth emitted here with the real CR delivery still in the batch behind it produces **two** turns — #209's failure mode. Drop is enough: the real delivery is already queued (it is what superseded us), and if it was lost in flight, reviews *are* synthesizable and the next reconcile pass recovers it (ADR-37's own finding) |
| **D** | Treat a red required check as rework too | New FSM kind, new dispatch | Medium | **Rejected as out of scope, named as residual.** An APPROVED PR with a failed check has no counterpart verdict to route and no FSM transition; inventing one is a separate design. Today it still escalates, and that is unchanged by this ADR |
| **E** | Suppress the repeated log line, keep a periodic summary | A rate limiter on one `log.info` | Lowest | **Rejected as the whole fix**, by the issue itself: it hides the symptom and leaves the spin. The backoff below makes the line rare as a consequence, which is the right order of causation |

**(a) `ApprovalCheck` gains `superseded: bool = False`, and it is set in one place.** `_check_approver_on_head` (`verify.py:370`) returns `superseded=True` when the expected approver's latest verdict review is `CHANGES_REQUESTED` or `DISMISSED`, and when an otherwise-valid `APPROVED` is on a SHA that is not the current head. All three mean the same thing — *a later event replaced the premise of this delivery, and that event has its own FSM kind* (`code_changes_requested` / `design_changes_requested`, `feature_revised` / `design_revised`). `superseded` implies `not ok` and is disjoint from `transient`: it is neither a retry nor a fault.

**Set in one place, and dropped one line later unless a second line changes too.** `verify_design_approval` returns the helper's result directly (`verify.py:180`) and inherits the flag. `verify_feature_merge` — the #214 caller — does **not**: it rebuilds the result at `verify.py:249–251`, `return ApprovalCheck(False, approval.reason, transient=False)`, which discards any field the helper set. That return must propagate `superseded` (return `approval` unchanged, or copy the flag through). **Acceptance (1) fails against a patch that only touches the helper and the two `design_loop` sites**, and the failure looks like the helper is wrong. Named here so the implementer does not go looking there. (Reviewer finding, PR #224.)

Permanent, unchanged, still escalating: **no review from the expected approver at all** (`no review from expected approver …` — which is also what a review by anyone else produces, since the helper looks up the expected login and nothing else, so "a reviewer who is not the counterpart" is not a distinct outcome and must not be written as one), a `dirty` merge, a concluded `failure` on a required check, `blocked` with unresolved threads, a failed closing-keyword defuse (§8.4 step 4). Those are the cases where authorization cannot be established and no other event is coming.

**(a′) `COMMENTED` is not a verdict, and today it is treated as one.** `_check_approver_on_head` takes the last review row per user of *any* type (`verify.py:382–387`), so an Architect who approves and then leaves a review-comment makes the latest state `COMMENTED` → not `APPROVED` → permanent → paused. GitHub's own merge gate ignores `COMMENTED`; ours must too. Compute the latest review whose state is in {`APPROVED`, `CHANGES_REQUESTED`, `DISMISSED`} and ignore `COMMENTED` and `PENDING`. **Define that set in `verify.py` and do not widen `_VERDICT_REVIEW_STATES` (`design_loop.py:99`) to reach it.** The two sets share two members and not a meaning, and the draft's "one definition, not two" was wrong on the mechanism (reviewer finding, PR #224). ADR-30's set is the *author-drop exception*: an author-sent `APPROVED` / `CHANGES_REQUESTED` alarms and falls through (`design_loop.py:532`) because it has an FSM kind; every other author-sent PR-scoped event is marked `done` (`:543`). `DISMISSED` is excluded there deliberately. Widen it and an author-sent `pull_request_review` with `state=DISMISSED` — which is exactly what `dismiss_stale_reviews_on_push` produces on a rework push — falls through instead; `_event_kind` maps only `CHANGES_REQUESTED` and `APPROVED` (`:3641–3652`), so the kind is the fall-through `pull_request_review.dismissed` and `_pick_recipient`'s default (`:3710`) dispatches it to the counterpart. A spurious turn, in the same class as #63. Two names. Not observed live — reachable by inspection, cheap, and in the function this ADR is already rewriting.

**(b) Both guards drop the delivery on `superseded`; neither escalates, and neither merges.** `dropped` is the existing terminal status for "intentionally discarded" (§15.1), so the row cannot spin. Log at INFO — this is an ordinary outcome and a WARNING here trains the reader to ignore warnings — naming the delivery, the PR, the superseding state, and the session state at drop:

```
merge_auth superseded id=eb822240 pr=213 latest=CHANGES_REQUESTED state=CODE_REVIEW — dropped, no escalation
```

Do **not** advance the FSM here, and do not synthesize (option C). The superseding delivery carries the transition; this one carries nothing.

**(c) The FSM, the merge refusal and §8.4's three conditions are untouched.** Nothing about what authorizes a merge changes. This ADR changes only what the gateway does with a delivery it has just proved cannot authorize one.

**(d) Named residual: `CODE_REVIEW` with no rework delivery.** If the superseding webhook is lost in flight *and* the reconciler cannot synthesize it, the session sits in `CODE_REVIEW` with no turn — quiet, not paused. Bounded by one reconcile pass (`RECONCILE_INTERVAL_S`, ≤ 5 min) because reviews carry node ids and step 5's set-diff regenerates them; ADR-37 recorded exactly this asymmetry ("Reviews *are* synthesizable; the next pass would have done it"). Strictly better than today's outcome, which is a permanent pause. Not engineered around.

---

**Part 2 — `defer` has no clock, so any long-lived non-routable state spins.**

`route_for_recipient` returns `DEFER` for a paused session and a non-owner sender (`routing.py:122`), and the caller logs and returns with the row still `deferred` (`design_loop.py:832`). The next drain, ~5 s later, re-picks the identical row. On #57 that ran **4,466 times over 6h14m**, ~12 lines/min, and the log reached 670 KB. Two secondary costs the issue does not name: `list_deferred(limit=20)` is `received_at ASC`, so a parked row permanently occupies a head slot in every batch, and the row is re-evaluated — session lookup, digest, route — not merely re-logged.

**#147's fix does not cover this, exactly as the issue predicted.** ADR-32 (c) suppresses the spin for `state in TERMINAL_STATES` (`design_loop.py:823–831`). `PAUSED_HUMAN` is not terminal, and must not be: the row has to survive until the owner replies. The gap is that `defer` means "not now" with no answer to "then when".

| | Repair | Cost | Complexity | Why not (or why) |
|---|---|---|---|---|
| **P1** | **`deliveries.next_attempt_at` + `defer_count`; `list_deferred` filters on the clock; exponential backoff to a ceiling** | Schema v11, two columns, one setter, one `WHERE` clause | Low | **Chosen.** Fails safe in the worst case — a forgotten wake-up costs latency, never the delivery |
| **P2** | A new `parked` status, excluded from the drain, flipped back to `deferred` on unpause | No schema change | Low | **Rejected, and the reason is the failure mode.** Any unpause path that does not flip it back seals the delivery **silently, with no log line at all** — and there are several (reconciler adopt, `issues.closed` → `TEARDOWN`, a gateway restart mid-pause, the owner's manual DB repair in `~/fix-151.sh`). It converts a noisy bug into a quiet one, which is the failure this repository keeps paying for |
| **P3** | Drop the delivery on pause and re-fetch on resume | No schema change | Medium | **Rejected.** Re-fetch is a new path into `_process_one`, needs a GitHub read the pause has no budget for, and loses the payload that is the whole point of the durable queue (ADR-2: SQLite is the ledger) |
| **P4** | Rate-limit the log line only | Trivial | Lowest | **Rejected**, per the issue. Hides the spin, keeps the head-slot cost and the re-evaluation cost |

**(e) Schema v11: `deliveries.next_attempt_at INTEGER NOT NULL DEFAULT 0` and `deliveries.defer_count INTEGER NOT NULL DEFAULT 0`.** Incremental `ALTER TABLE` that **must preserve `deliveries`** — the ledger is the queue (ADR-21's v7→v8 lesson, restated by ADR-37). `list_deferred` gains `AND next_attempt_at <= ?` with the current unix time. Default `0` means every row that never backs off behaves exactly as today, so this is not a change to first-pass drain latency for anything.

**(f) One call site sets the clock: the `RouteAction.DEFER` branch.** `Store.defer_delivery(delivery_id, retry_after_s)` increments `defer_count` and writes `next_attempt_at = now + retry_after_s`; the DEFER branch passes `min(5 * 2 ** defer_count, 300)` computed from the row's `defer_count` **as read, before the setter increments it** — the first defer reads 0 and waits 5 s. Read it after and every wait doubles: 10, 20, 40, and the "unchanged first retry" below is false. First retry 5 s (unchanged), then 10, 20, 40, 80, 160, and 300 s thereafter — the ceiling is `RECONCILE_INTERVAL_S`, so nothing waits longer than a reconcile pass already does. #57's 4,466 lines become **79**.

**Deliberately not extended to the other two paths that leave a row deferred**, and the reason is different for each:

- **`feature merge_auth deferred (transient)`** (`design_loop.py:691`) returns without touching status and already has its own cap, `_MERGE_AUTH_MAX_ATTEMPTS = 5` (`:116`). It never calls the setter, so its ~5 s cadence is untouched. Slowing merge authorization while GitHub settles `mergeable_state` would be a regression to fix a log-volume bug.
- **The capacity/`ensure_session` re-defer on resume** (`design_loop.py:3379`, `:3395`) is an **owner-reply** delivery. Backing it off delays the one thing the owner is waiting for. Named residual: it can spin, it is bounded by the owner fixing the host, and it is #35's subject, not this one.

**(g) Unpause clears the clock globally, and globally is deliberate.** `_resume_from_escalation` (`design_loop.py:3295`) runs `UPDATE deliveries SET next_attempt_at = 0, defer_count = 0 WHERE status = 'deferred'` before dispatching. Not scoped by session: `deliveries.issue_num` for a PR event is the **PR** number, not the session's, so a session-scoped `WHERE` would either miss the parked PR-review row — the exact row #214 is about — or need the `sessions.design_pr` / `feature_pr` join that has manufactured false evidence here before. The cost of the global form is one extra immediate attempt for unrelated parked rows, which then back off again. **This clear is an optimisation, not a correctness requirement**: if it is skipped, every parked delivery still runs within the 300 s ceiling. Stating that is the point — P1 was chosen over P2 precisely because no single site is load-bearing. The cleared row drains on the **next** `list_deferred`, not inside the batch that resumed the session: `process_deferred_batch` iterates a list fetched before the resume ran. See acceptance (10′).

**Placement.** `superseded` lives in `verify.py`; the drop lives at both guard sites in `design_loop.py`; the clock lives in `db.py` and the one DEFER branch. No new module, no new loop, no new status.

**Amendments in place, so an implementer starting at the section does not build the old sentence.**

- §8.4 gains the superseded clause: verification failure is classified three ways, and a replaced counterpart verdict drops the delivery.
- §8.5 gains the bound: escalation is for authorization that cannot be established, never for a counterpart verdict the FSM has a transition for.
- §9.1's `PAUSED_*` **Defer** row: "stays queued" → stays deferred **with backoff**; parenthetical pointer here.
- §15.1's `deliveries` DDL: two columns, with the comment that `0` means "eligible now".

**Deliberately out of scope.**

- **A red required check on an approved PR.** Option D. Still escalates. It needs an FSM kind that does not exist.
- **The pause→resume protocol itself.** §8.5 unchanged: owner reply is still the only exit, and this ADR reduces how often a pause happens rather than changing what one is.
- **`_MERGE_AUTH_MAX_ATTEMPTS` and the in-process `_merge_auth_attempts` dict** (`design_loop.py:115`), which is lost on restart. Real, unrelated, and made less reachable by (b) rather than more.
- **#147.** Closed by ADR-32 (c) and staying closed; (e) subsumes its shape without reopening it.
- **Repairing session #57's or #63's recorded pause.** Data. `@huozhe`.
- **The design half's missing transient arm.** `verify_design_approval` returns `ApprovalCheck(False, "GitHub API error: …")` with `transient` left at its default (`verify.py:178`), so a network blip escalates there where the same blip retries on the feature half. Diagnosed above and **deliberately not repaired here**: the fix is not the one word at `:178` but a retry arm at `design_loop.py:648` that the design half does not have, which means duplicating `_merge_auth_attempts` and `_MERGE_AUTH_MAX_ATTEMPTS`. This ADR's subject is classification, not retries. Named so it is in a column rather than in the prose. (Reviewer finding, PR #224.)
- **The Tailscale funnel.** ADR-37's residual, unchanged.

**Deploy is gateway-only** — no image rebuild, no `docker rm -f`. Schema v11 migrates on gateway start; confirm `PRAGMA user_version` is 11 **and** that `SELECT COUNT(*) FROM deliveries` is unchanged across the restart, because a migration that recreates the table would silently discard the queue this ADR is about.

**Acceptance — (1) and (7) are where today's defects are proven, and both must fail first.**

1. **A `feature_approved_unverified` delivery whose PR's latest Architect review is `CHANGES_REQUESTED` is dropped, does not escalate, and leaves the session unpaused.** Fixture: session in `CODE_REVIEW`, stub reviews list `[APPROVED@head, CHANGES_REQUESTED@head]` from the Architect. Assert delivery status `dropped`, `sessions.state == 'CODE_REVIEW'`, `paused_reason IS NULL`, zero escalation rows. Today: `PAUSED_HUMAN` with the #214 reason string. **Assert the session row, never the log line.**
2. **The same two deliveries in one batch dispatch exactly one Developer rework turn, approval first.** This is the live shape and item (1) alone does not reach it. Insert the APPROVED delivery with the earlier `received_at`, then the `CHANGES_REQUESTED` one, and drain **one** batch. Assert one `_dispatch_turn` to `developer`, `fsm` at `CODE_REWORK`, `silent_turns` unchanged. A test that drains them separately passes against a version that still paused on the first.
3. **`DISMISSED` behaves identically to `CHANGES_REQUESTED`.** Session #63, live. A fixture that only builds the `CHANGES_REQUESTED` arm passes against option B.
4. **An `APPROVED` on a stale SHA is superseded, not permanent.** Latest Architect review `APPROVED` with `commit_id = X`, PR head `Y`. Dropped, no escalation. Pairs with ADR-37: the `*_revised` delivery for `Y` is what carries the session forward, real or synthesized.
5. **No review at all from the expected approver still escalates, and so does a required check concluded `failure`.** The guard must not become permissive. A patch that returns `superseded` for every non-`APPROVED` outcome fails here, and this is the item that catches it.
6. **The design half inherits it: `design_approved_unverified` with a Developer `CHANGES_REQUESTED` drops and does not pause.** Same helper, other caller. A Feature-only test misses the half that has no transient arm at all.
7. **A latest review of `COMMENTED` from the Architect, with an `APPROVED` behind it on the current head, still authorizes the merge.** (a′). Today this escalates. Fixture order matters: `[APPROVED@head, COMMENTED@head]`, in that order, from the same login — reversed, it proves nothing.
8. **A deferred delivery on a paused session is re-attempted on a widening interval and stops being re-picked in between.** Drive `process_deferred_batch` against a fake clock. Assert `defer_count` increments, `next_attempt_at` moves 5→10→20→…→300 and stops at 300, and that a drain at `now < next_attempt_at` returns the row **zero** times. Today: every drain, forever. **Assert the row is not returned by `list_deferred`, not merely that nothing was logged** — a suppressed log line looks identical and is option P4.
9. **A parked row does not consume a `list_deferred` head slot.** Twenty-one deferred rows, the oldest parked with a future `next_attempt_at`, `limit=20`: the twenty-first is returned. This is the starvation cost, and item (8) does not reach it.
10. **Owner reply clears the clock.** Assert `next_attempt_at == 0` and `defer_count == 0` on the parked row after `_resume_from_escalation`. Columns only — this item does not reach the route.
10′. **The next drain returns that row and routes it, without waiting out the ceiling.** Assert `list_deferred` now includes it and that `process_deferred_batch` routes it. **Not the same batch**, and a test that asserts one `process_deferred_batch` call both resumes and routes fails against a faithful (g): `process_deferred_batch` iterates a list fetched before the resume runs (`design_loop.py:411`), and a parked row with a future `next_attempt_at` was never in it. (g)'s `UPDATE` cannot insert a row into an in-memory batch. This pair is the anti-P2 property — item (10) alone passes against a version that clears the columns and never drains. (Reviewer finding, PR #224.)
11. **Migration v10 → v11 preserves every `deliveries` row and its status.** Seed a v10 DB with rows in each status, migrate, assert counts and statuses per status are identical and `user_version == 11`. The ledger is the queue.
12. **Live, before sign-off.** The next session whose Architect replaces an approval with `CHANGES_REQUESTED` dispatches a Developer rework turn, stays out of `PAUSED_HUMAN`, and logs `merge_auth superseded` once. In the same window, `grep -c "route defer id=" agentd.log` for any one delivery id stays under ~20/hour. Gateway-only; verify by LaunchAgent start time. **A quiet log with no review round does not discharge this**, and neither does a pause that did not happen for some other reason — the round must actually reach `CHANGES_REQUESTED` after an `APPROVED`.
