# ADR-40: A `*_pr_opened` the FSM Never Sees — Take It Unconditionally, and Discover the PR the Sweep Cannot Reach

*A decision of record for `agentd`. Index and context: [unified design spec §16](../unified_design_spec.md#16-architecture-decision-records).*


*Fixes #229.* **`*_pr_opened` is the only event that fills `sessions.design_pr` / `feature_pr`, and it is the only event whose loss cannot be recovered.** One session lost it twice in one day by two unrelated causes — the spent-SHA gate dropped a delivered one, the funnel lost another in flight — and both outcomes were identical: a live PR the FSM cannot see, no self-heal, every health signal green. **Scope is both causes.** A change that only narrows the gate leaves the second instance exactly where it was, and a change that only teaches the reconciler to discover the PR leaves the gate free to drop the next one.

**The mechanism, and every step of it is behaving as written.**

`_HEAD_WM` (`session_loop.py:106`) maps four kinds to a watermark column, and two of them are `opened`. The gate ADR-37 (a″) placed before the FSM (`session_loop.py:902–929`, FSM at `:936`) reads that map, so it also guards the two `opened` kinds. Its stale arm — *payload SHA ≠ live head → spent* — is correct for a review cue and wrong for a state transition: for `design_pr_opened` the payload SHA is *expected* to lag, because the drain is serialized behind turns and the author keeps pushing. On #159 a 601 s first turn parked the delivery for 4m23s while the RFC commit moved the head, and the gate dropped it:

```
22:43:42  turn t-889ec5ce7199 opens Design PR #227 at head 2f9d710e   (webhook queued)
22:44:05  turn t-bc8e604d9856 starts — the drain is now parked behind it
22:47:28  that turn pushes the RFC commit; head moves to 86afa4dd     (synchronize queued)
22:48:04  turn ends, drain resumes
22:48:05  no turn id=420fa4e0 session=huozhe/code-workflow#159
          — stale head 2f9d710e3371 live 86afa4dd0a58 (design_pr_opened) (#211)
```

**The redelivery is the control, and it is what makes this a gate defect rather than a timing complaint.** The owner redelivered the *same* `opened` for PR #232 onto an idle drain:

```
10:07:44  delivery received (redelivery of pull_request.opened, PR 232)
10:07:45  fsm huozhe/code-workflow#159 → CODE_REVIEW (Feature PR open (developer branch))
```

Same event, same payload, same code path, opposite outcome. The only variable is how far behind the drain was.

**Why the loss is permanent rather than late.** `*_pr_opened` is once per PR — GitHub does not resend `opened` — and it is the sole writer of four things:

| written by `*_pr_opened` alone | site | what its absence costs |
|---|---|---|
| the FSM exit from `PLANNING` / `IMPLEMENTING` | `fsm.py:67`, `:75` | no other kind leaves those states; `design_revised` from `PLANNING` is `None` |
| `sessions.design_pr` / `feature_pr` | `session_loop.py:945`, `:947` | `_resolve_session_for_delivery` route 2 cannot map PR-keyed `issue_comment` to the session |
| `roles_locked` | `fsm.py:69` via `freeze_roles` | roles never froze for the rest of the session |
| the review turn itself | dispatch | the counterpart is never asked to review the PR |

**And it is self-sealing against ADR-37.** The later `synchronize` for `86afa4dd` was taken and stamped `design_pr_head`, so the drift comparison in `_synth_head_drift` (`reconciler.py:675`) sees watermark == live head and synthesizes nothing — forever. On the code half the seal is simpler and tighter: `_synth_head_drift` reads `sess["design_pr"]` / `sess["feature_pr"]` and returns immediately when the column is NULL, and `fetch_session_snapshot` only `GET`s the PRs those columns name (`github_fetch.py:282–285`), so an untracked PR is not in `snap["nodes"]` at all. **The set-diff has no node to miss and step 4 has no object whose state moved.** ADR-21 stated exactly this and left it; ADR-37 restated it as unchanged:

> **Dropped `pull_request.opened`.** ADR-21 residual: a PR not yet on `sessions.design_pr` / `feature_pr` is invisible to both halves. Unchanged.

That residual is now a live defect twice over, so this ADR closes it.

**The second instance, three hours later, with no gate involved.** Feature PR #232 was opened by `huozhegrok` on `agentd/huozhe__code-workflow/159/developer` at 09:54:59. `SELECT COUNT(*) FROM deliveries WHERE issue_num=232` returned **0** — the POST never arrived, ADR-37's transport class, confirmed by `check_suite.completed` for the same push landing at 09:58:30. Session sat in `IMPLEMENTING`, `feature_pr` NULL, no open turn, no pending delivery, `silent_turns` 0, `paused_reason` NULL. **Indistinguishable from the design-side strand**, which is the point: two causes, one observable, one recovery.

**Observed cost, and why nothing noticed.** Eight turns ran against the stranded FSM before the row was repaired by hand. Both agents did real work on #227 throughout, and the Developer's own turn summary reasons about the contradiction — *"PLANNING still says wait, but a Design PR synchronize usually means the developer reviews… State is still PLANNING, so the written duty is wait"* — and reviews on the merits anyway. **The agents compensating is what keeps every signal green.** This is the ledger's recurring shape: a fixture that cannot reach its case looks exactly like a pass.

**ADR-37 named this and the implementation went the other way.** The gate's entire argument in ADR-37 is about `design_revised` / `feature_revised` — *"A stale or duplicate head must not run `design_revised` / `feature_revised`"* (§(a″), and the 1.37.1 revision note). `*_pr_opened` is never argued for anywhere in that ADR. It is in `_HEAD_WM` because the *stamp* belongs there, and the gate reads the same map.

**The options, with cost, and why four of them lose.**

| | Repair | Cost | Complexity | Why not (or why) |
|---|---|---|---|---|
| **A** | **Narrow the gate to `*_revised`; discover an untracked role-branch PR in the sweep** | No schema. One arm swapped in `_process_one`; one predicate + one exact `GET` in `_apply_snapshot`; `head.sha` added to the existing `opened` envelope | Medium | **Chosen.** The only option that closes both causes, and the discovery is the only shape that needs no watermark — the tracked-PR comparison ADR-37 relies on is precisely what is unavailable here |
| **B** | Drop the two `opened` keys from `_HEAD_WM` | One-line | Trivial | **Rejected.** `_HEAD_WM` is where the *stamp* lives, not only the gate. Removing the keys leaves a PR opened and never synchronized with a NULL watermark forever, so ADR-37 (c)'s backfill arms decide from NULL on every pass. It also fixes nothing on the code half, where no gate ran |
| **C** | List every open PR on the repo each pass and match head refs | A repo-wide `GET /pulls` per pass | Low code, unbounded cost | **Rejected.** The sweep is issue-scoped by construction (ADR-21) and that is what makes it cheap. The role branch name is *computable* — `role_branch_name(repo, issue, role)` (`gitops.py:74`) — so the query can be exact and per-session |
| **D** | Redeliver from GitHub's hook UI | `admin:repo_hook`, a scope no token here holds | High privilege | **Rejected.** ADR-37 option D, rejected there for the same reasons, and it is what the owner had to do by hand for #232. Recovery must not require a scope the gateway cannot have |
| **E** | Escalate after N passes in `PLANNING` / `IMPLEMENTING` with no open turn | A counter on the session row | Low | **Rejected as a substitute.** A session correctly waiting for the author to open a PR is indistinguishable from this predicate. Same alarm ADR-37 (f) rejected, and it spends an owner round-trip every time drafting is slow |
| **F** | Adopt the transition from the snapshot (step 4) instead of synthesizing | `_adopt_forward(sess, "design_pr_opened")` | Low | **Rejected.** ADR-21's and ADR-37's standing rule: adopt writes `state` and dispatches nothing, so the counterpart is never asked — and the review turn *is* the event. It also would not write `roles_locked`, and teaching adopt to write columns is how B in ADR-37 manufactured a healthy-looking strand |

**A is the only option that repairs both causes and still produces a review turn.** B and C are the same repair at the wrong altitude. D is a scope. E names the silence. F converges a column and drops the turn.

---

**(a) The gate's stale and duplicate arms are for `*_revised`. `*_pr_opened` has exactly one arm: already tracked.**

- `sessions.<half>_pr` == this PR number → **spent**. Log and drop.
- otherwise → **take**. No SHA comparison of any kind.

The payload SHA of an `opened` is which commit the PR happened to be opened at. No FSM kind reads it, no routing decision reads it, no §8.4 check reads it. **Which SHA a PR was opened at does not change whether the session should now be in `DESIGN_REVIEW` with `design_pr` set.**

The tracked-column arm is not decoration, and it is the reason both halves of this ADR compose. `*_pr_opened` now has two producers — GitHub and (c)'s synth — and the arm is what makes every interleaving end at one turn:

| order | outcome |
|---|---|
| synth drains first | takes; FSM writes `design_pr`. The real `opened` arrives later, finds the column set, is spent |
| real drains first | takes; FSM writes `design_pr`. (c)'s predicate is false on the next pass, so no synth is ever built |
| synth queued, real drains first | synth drains second, finds the column set, is spent |
| GitHub redelivery of either | same `X-GitHub-Delivery` GUID; `deliveries.delivery_id` is the PK and rejects it before any of this |

Without the arm, the owner redelivery that recovered #232 would have dispatched a second review turn.

**(b) A `*_pr_opened` take stamps the *live* head, not the payload head.** ADR-37 (a) defines the watermark as the last head this session has **taken**. What a review turn opened by `design_pr_opened` actually reviews is the live PR — the counterpart reads the forge, not the payload. Stamping payload `X` while the live head is `Y` therefore records something the session did not take, and the trailing real `synchronize` for `Y` then passes both gate arms (`Y ≠ X`, `Y == live`) and dispatches a **second** review of the same head. Stamping `Y` makes that synchronize a duplicate-head drop, which is correct: one turn, on the head that exists.

This is the one place where live-head-at-take and payload-at-take differ, and the gap between them is exactly the interval the drain was behind — the 4m23s that produced the defect.

The live head comes from the GET `_fetch_live_pr` (`session_loop.py:1596`) already makes at this site for ADR-30 (c) and ADR-37 (a″). **Not a second fetch.** When that GET fails, fall back to the payload SHA: fail open, the same posture ADR-30 (c) and ADR-37 (a″) take, and a stale watermark costs one duplicate review turn where a NULL one costs a backfill decision.

`*_revised` is unchanged and still stamps the payload SHA — on that path the gate has already proven payload == live head, so the two are the same value by the time the stamp runs.

**(c) The sweep discovers an untracked role-branch PR by name, not by listing.** Per half, per pass, synthesize when **all three** hold:

1. `sessions.<half>_pr` IS NULL — the half is untracked;
2. `transition(state, "<half>_pr_opened")` is not `None` — `PLANNING` for the design half; `IMPLEMENTING` for the code half (`fsm.py:67`, `:75`). Not `AWAITING_VERIFICATION`: that state always has `feature_pr` set, so predicate 1 skips before `transition()` is consulted. Residual named below.
3. `GET /repos/{repo}/pulls?state=open&head=<owner>:<branch>&per_page=100` returns a PR, where `<branch>` is `role_branch_name(repo, issue_num, role)`, and `parse_role_branch(pr.head.ref, repo)` re-parses to **this** session's issue number and role.

Predicate 3 re-checks what the query filtered on. The filter is a convenience; the parse is the rule. `is_design_head_ref` / `is_feature_head_ref` (`gitops.py:96`, `:102`) already decide branch ownership everywhere else in the system and this must not become a second answer to the same question.

**Predicate 2 is binding, not an optimisation, and it is the one an implementer will drop.** Without it a synth that drains into a state with no transition for it leaves the column NULL — so the next pass, if the head has moved, builds another one, and every push buys another review turn. That is ADR-37 (a′)'s re-enqueue failure arriving on a different axis. With it, a `PAUSED_HUMAN` session emits nothing at all, and gets exactly one synth on the first pass after §8.5 restores `resume_state`. **The predicate self-clears the instant the synth works**, because taking the delivery is what writes the column; that is what bounds repetition, and the delivery id in (e) is the floor under it, not the mechanism.

Predicate 1 also bounds the cost: **one GET per NULL half per pass, and it stops the moment the column fills.** The window in which the bug can exist is the window in which we pay. A session with both PRs tracked adds no requests at all.

**More than one match:** take the lowest PR number, and log the rest at WARNING. Two open PRs sharing a head ref means different base branches; picking the newest would adopt a PR the session never opened, and skipping both re-seals the strand this ADR exists to break.

**Merged, closed or draft:** `state=open` excludes the first two, and the drain's ADR-30 (c) spent-PR gate is the backstop if the PR closes between the pass and the drain. Draft PRs are not excluded — a draft Design PR is still the session's Design PR, and `_event_kind` has never read `draft`.

**(d) The discovery envelope is `pull_request.opened`, built beside `_synthesize_synchronize`, and it must carry `head.sha`.** It is **not** built by widening `_synthesize_node` (`reconciler.py:792`). That function runs only over `snap["nodes"]`, and `fetch_session_snapshot` puts a PR there only when a column already names it (`github_fetch.py:282–285`) — the exact invisibility this ADR closes — and it keys `recon:{nid}`, not (e)'s id. `_synthesize_synchronize` (`reconciler.py:741`) is the right sibling: same shape, already carries `user`, `html_url` and `head.sha`, already builds its own delivery id. **Raised by the Developer on this PR;** the first draft pointed (d) at the wrong function, which would have shipped a binding that cannot reach its own case.

`_synthesize_node`'s `pull_request` branch still deserves the SHA, as the **set-diff backstop** and nothing more: it writes `"head": {"ref": …}` and no SHA, so if that branch ever emits an `opened` — a tracked PR whose node row is missing — the take stamps nothing (`session_loop.py:994` is guarded on `payload_sha`), the watermark stays NULL, and ADR-37 (c)'s backfill arms then decide from NULL on the next pass, recovering one strand and re-arming another. Add the field there too. It does not implement (c).

Minimum fields, all of them read by something:

- `action: "opened"`
- `pull_request.number`, `.node_id`, `.title`, `.html_url`, `.merged: false`
- `pull_request.head.ref` — **load-bearing twice.** `_is_design_pr` / `_is_feature_pr` classify from it and nothing else (§9.1: a model-written title is not a control-plane signal), *and* `_resolve_session_for_delivery` route 1 (`session_loop.py:3528`) maps the delivery to the session through `parse_role_branch`. That route is why this synth reaches its session at all while `design_pr` — the column it exists to fill — is still NULL
- `pull_request.head.sha` — the live head, per (d) above
- `pull_request.user.login` = the PR author, and `sender.login` = the same. **No reader today**, and the review synth's reason does not transfer: `_AUTHOR_PR_EVENTS` (`session_loop.py:96`) is review + comment only, so ADR-30's author-sent guard never runs on a `pull_request` event and #209's hole is not reachable from this envelope. **Checked by the Developer on this PR; the first draft cited that guard and was wrong.** Carry the field anyway: `deliveries.sender` is what the row records for the audit trail, the discovery GET already returns the PR object, and that frozenset is one edit from making the omission load-bearing
- `repository.full_name`

**(e) Idempotency: `recon:open:<pr>:<sha>`, `INSERT OR IGNORE`, and a `delivery_nodes` row the store already writes.** Count `synthesized` only when the insert returns True, as ADR-37 (a′) requires.

**The node row is required, and getting it wrong produces a second review turn on a recovery that looked complete.** The moment the synth drains and the column fills, `fetch_session_snapshot` starts returning that PR as a node, and ADR-21's set-diff would synthesize `recon:<node_id>` — a second `pull_request.opened` — because `has_delivery_node` has never seen it. Nothing is lost by recording it: membership is per object (ADR-21), so a later action on that PR node was never recoverable by the set-diff anyway. That is the whole reason ADR-37 exists.

**The row is already automatic, and that is the binding.** `insert_delivery` (`db.py:551–557`) calls `_index_nodes_locked` on every successful insert, and `node_ids_from_payload` (`db.py:173`) reads `pull_request.node_id` — in the same transaction, only when the insert returned True, exactly as ADR-21 requires. So the implementer adds nothing here; the binding is **do not defeat it**: the envelope must carry `pull_request.node_id`, which the discovery GET returns. An envelope without it inserts a delivery with no node row, and item 8 is the double-turn that follows.

That also settles the apparent conflict with ADR-37 (a′) rather than merely inverting it. That ADR says *do not write a `delivery_nodes` row for this id* — and the shipped code writes one anyway, because `_synthesize_synchronize`'s payload carries the node id and `insert_delivery` indexes unconditionally. Nothing broke, because for a *tracked* PR the node was already indexed by the real `opened` and the write is an `INSERT OR IGNORE` no-op. **ADR-37 (a′)'s sentence is unimplemented and moot, not violated by this ADR.** Correcting it is not this change.

The SHA stays in the delivery id even though it is not a cue. It is the floor under (c)'s predicate: a session that pushes while the synth is still queued must not be sealed by a one-shot id.

**(f) No schema change, no new column, no new token scope.** `design_pr_head` / `feature_pr_head` are unchanged and keep their ADR-37 meaning. The discovery reads the forge with the token the sweep already holds.

**(g) The recovery is named on the pass line, never escalated.** INFO on synth with session, half, PR number, branch and head SHA; the pass report's `synthesized` counts it. **Do not** raise `PAUSED_HUMAN` because a session sat in `PLANNING` without an open turn — that is option E, and it is a correct wait most of the time. After this ADR both causes converge within one `RECONCILE_INTERVAL_S` (≤ 5 min); the eight-turn strand is what disappears.

**Placement, because ordering is the design.** The gate change is inside the existing ADR-37 (a″) block in `_process_one`, before the FSM, sharing ADR-30 (c)'s GET — the block does not move, its arms are selected by kind. The discovery lives in `_apply_snapshot` beside `_synth_head_drift` and **after** it, so a session that is tracked takes the cheaper path and never reaches the new GET. Both are subject to the same per-pass synthesis cap ADR-21 set.

**Amendments in place, so an implementer starting at the section does not build the old sentence.**

- ADR-21's "two things this half does not recover" is now **one** thing. The untracked-PR half is stated as closed, in the past tense, with the pointer here — not struck through beside the sentence it contradicts. **Raised by the Developer on this PR:** the first revision left "not recovered here" standing next to "superseded", which is 1.18.0 / #122 again.
- ADR-37's *Deliberately out of scope* "Dropped `pull_request.opened`" bullet: no longer unchanged; points here.
- §11.2's `synchronize` paragraph gains the `opened` case beside it.

**Deliberately out of scope.**

- **The serialized drain.** It is the trigger, not the defect; #214 already records what it does. A gate that misbehaves only when the drain is behind is still the thing to fix.
- **Ingress reliability.** The Tailscale funnel is owner ops and ADR-37 parks it. This is about surviving a lost `opened`, not losing fewer.
- **Hook redelivery / `admin:repo_hook`.** Option D.
- **PR-conversation comments on a discovered PR.** ADR-21's *other* residual: the sweep reads the session issue's thread and the reviews on known PRs, never the PR conversation. Discovering the PR does not change that, and it is not this subject.
- **A second Design PR opened while already in `DESIGN_REVIEW`.** `transition()` returns `None` there today, so predicate 2 declines to discover it. Pre-existing, never observed, and widening the FSM is a different decision.
- **A second Feature PR opened while already in `AWAITING_VERIFICATION`.** The FSM admits re-enter to `IMPLEMENTING` (`fsm.py:88`), but `feature_pr` still names the merged predecessor, so predicate 1 skips and discovery issues no GET. Never observed; a standing GET per waiting session per pass is ADR-37's rejected posture. A real `opened` that arrives still drains. **Raised reviewing #237.**
- **Repairing session #159's row.** Data, already done by hand. `@huozhe`.

**Deploy is gateway-only.** No image rebuild, no `docker rm -f`. Restart the LaunchAgent — noting that a restart ships whatever *this* checkout holds. Verify by process start time and by a fixture that fails first, not by a quiet log.

**Acceptance — (1) and (6) are the two live causes, and both must fail first.**

1. **The ordering fixture, design half — this is where today's defect is proven.** Queue a `pull_request.opened` for the Design PR at SHA `X`; make the live-PR stub return head `Y` before the drain runs. Assert `PLANNING` → `DESIGN_REVIEW`, `sessions.design_pr` = the PR number, `roles_locked` = 1, exactly one Developer turn, `design_pr_head` = **`Y`**. Today: `stale head … (design_pr_opened)`, state `PLANNING`, `design_pr` NULL, zero turns. **The ordering is the fixture, not a detail of it** — a test that drains the `opened` promptly cannot reach the bug. Assert in the test body that the stub's live head differs from the payload SHA, or a passing run proves nothing.
2. **Same ordering, code half.** `IMPLEMENTING` → `CODE_REVIEW`, `feature_pr` set, one Architect turn, `feature_pr_head` = live head. A design-only test misses the half that lost its event entirely.
3. **A `pull_request.opened` for a PR already on `sessions.design_pr` dispatches no turn and does not move `silent_turns`.** Binding (a)'s only arm. Run both orders from (a)'s table; each must total one turn.
4. **ADR-37's acceptance (4′) still passes unchanged.** A real `synchronize` whose payload SHA is not the live head is still dropped, even when it also differs from the watermark. The narrowing is to `*_pr_opened` only; a change that moved the whole gate reopens #211.
5. **The watermark after a take equals the live head, and the trailing real `synchronize` for it dispatches nothing.** Binding (b). An implementation that stamps the payload SHA passes (1) and fails here, with a second review turn on a head already reviewed.
6. **The sweep synthesizes exactly one `pull_request.opened` for an untracked open PR on this session's developer branch, in `IMPLEMENTING` with `feature_pr` NULL** — the #232 instance. Assert `event=pull_request`, `action=opened`, `head.ref` = the role branch, `head.sha` = the live head, `pull_request.user.login` = `sender.login` = the PR author. Assert the sweep does **not** write `sessions.state`: the drain advances it, not the sweep.
7. **The same snapshot a second time synthesizes zero.** `INSERT OR IGNORE`. A test that checks only pass 1 passes against a version that re-enqueues every five minutes. **And the head moving while the first synth is still queued inserts a second id (`recon:open:<pr>:<Y>` and `recon:open:<pr>:<Z>`); draining both is one turn**, because (a)'s tracked arm spends the second and the watermark ends at the live head. A one-shot id would seal the push; a version that dropped (a) would dispatch two review turns with every other item still green. **Raised reviewing #237.**
8. **After that synth drains and `feature_pr` is set, the next sweep synthesizes nothing at all** — not the discovery (column filled) and not ADR-21's set-diff (node row written). This is the item binding (e)'s `delivery_nodes` row exists for; delete the row and this test must fail with a second `pull_request.opened`.
9. **Predicate 2, both arms, and the half must match the state.** `transition("PLANNING", "feature_pr_opened")` is `None`, so a fixture that restores to `PLANNING` while holding item 6's *developer*-branch PR is still silent — and an implementer then "fixes" the predicate into firing `feature_pr_opened` from `PLANNING`, which is the thing this item forbids. **Name the half in each arm.** Design: untracked architect-branch PR, `PAUSED_HUMAN` → nothing synthesized **and no discovery GET** → restore `PLANNING` (the §8.5 `resume_state`) → exactly one `design_pr_opened`. Code: untracked developer-branch PR, `PAUSED_HUMAN` → nothing → restore `IMPLEMENTING`, the stored `resume_state` and not `PLANNING`, which is only the fallback → exactly one `feature_pr_opened`. **Raised by the Developer on this PR.**
10. **Branch ownership is decided by the parse, not the query.** Feed the discovery stub a PR whose head ref is *another* session's role branch, and one that is merged. Neither is discovered. A fixture that only ever returns the right branch cannot reach predicate 3.
11. **The GET is suppressed per half, never per session.** `IMPLEMENTING` is reachable only through `DESIGN_APPROVED + design_merged`, so `design_pr` is *always* set on item 6's row — an assertion of the form "no fetch when `design_pr` is set" fails every correct code-half recovery, and passes for a version that stops discovering anything once the Design PR exists. **Raised by the Developer on this PR; the first draft said exactly that.** Assert instead: `design_pr` set → no GET for the **architect** branch; `feature_pr` set → no GET for the **developer** branch; and item 6's fixture, with `design_pr` set and `feature_pr` NULL, **must** GET the developer branch. Cost is the reason predicate 1 is first; a test asserting only "no synth" passes against a version that GETs every session every pass.
12. **Live, before sign-off.** The next Design PR opened *while a turn is running* transitions on the first drain of its `opened` — one `fsm … → DESIGN_REVIEW` line, `design_pr` non-NULL and `roles_locked=1` within that drain, and no `stale head … (design_pr_opened)` anywhere in the log. **A PR that opened onto an idle drain does not discharge this**: that is the case that already worked, and it is the shape of every green signal this defect produced.
13. **`AWAITING_VERIFICATION` with `feature_pr` set issues no discovery GET and synthesizes nothing.** Pins the residual in (c): predicate 1 skips before `transition()` can fire. A version that "fixes" the dead arm by widening fails this. **Raised reviewing #237.**
