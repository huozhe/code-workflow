# ADR-37: A Dropped `synchronize` Is Not an Adopt — Recover It as a Head-SHA Delivery

*A decision of record for `agentd`. Index and context: [unified design spec §16](../unified_design_spec.md#16-architecture-decision-records).*


*Fixes #211.* **ADR-21 told the implementer to adopt a synchronized PR. That sentence is the defect.** Adopt writes `sessions.state` and returns. The event's job is to dispatch a review turn. Without it, `DESIGN_REWORK` / `CODE_REWORK` have no exit (`fsm.py:71`, `:79`), every health signal reads quiet, and the next real act on GitHub is judged against a stale FSM — on session #57, a legitimate merge recorded as unauthorized.

**The mechanism, and every step of it is behaving as written.** `_event_kind` produces `design_revised` / `feature_revised` only for `pull_request` + `action == "synchronize"` on the tracked Design / Feature PR (`design_loop.py:3552`). `_synthesize_node` (`reconciler.py:671`) has three kinds: `comment` → `issue_comment.created`, `pull_request` → `pull_request.opened`, `review` → `pull_request_review.submitted`. There is no kind for a head-SHA change, and the PR kind is permanently `opened` because membership is per object (ADR-21): `pull_request.node_id` is one ID from opened through merged. Step 4 currently adopts `feature_merged` / `design_merged` only (`reconciler.py:605–608`). Six consecutive passes on #57 reported `nodes=6 synthesized=0`. By the reconciler's own model there was nothing to do.

**Cause is transport, confirmed from GitHub's delivery log, not inferred.** The owner read the hook log (no token here has `admin:repo_hook`). The failed rows say *We couldn't deliver this payload: failed to connect to host*. The gateway never saw the POST — not HMAC rejection, not a 4xx/5xx. Five failures in one evening on one session, successes interleaved, so the subscription and ingress work; a brief Tailscale-funnel outage does not. A later empty commit (`eae0e847`, same tree) produced both `push` and `synchronize` within two seconds and unstuck `DESIGN_REWORK`. The class is "lost in flight", and it is not rare on this host.

**Three live instances, one session, worsening consequence.**

| when | lost event | FSM | what happened |
|---|---|---|---|
| 22:20:54 PDT | Architect push `b666f41f` — no `push`, no `synchronize`; `check_suite.completed` for that commit *did* arrive | `DESIGN_REWORK` | idle ~26 min, six passes, no error, no pause, `paused_reason` NULL, `turn_count` stuck at 5 |
| 22:49:19 | `pull_request_review.submitted` APPROVED on PR #207 | `DESIGN_REVIEW` | recovered only because the owner redelivered at 22:51:00 (101 s). Reviews *are* synthesizable; the next pass would have done it. Named because it shows the asymmetry, not because this ADR covers reviews |
| next day 09:20:34 | Developer push `a71fd4f` — only a `pull_request.edited` arrived | `CODE_REWORK` | three correct no-ops → `silent_turns` 3 → `PAUSED_HUMAN`; after unpause, Architect APPROVED and Developer merged; gate recorded `unauthorized Feature PR merge: state=CODE_REWORK expected MERGING` |

The audit trail now contains a bypass that did not happen. §8.4 was fed a stale state. All three symptoms — deadlock, stall pause, false unauthorized — are the same stale field.

**The options, with cost, and why four of them lose.**

| | Repair | Cost | Complexity | Why not (or why) |
|---|---|---|---|---|
| **A** | **Head-SHA watermark + synthesized `pull_request.synchronize`** | Schema v10: `sessions.design_pr_head`, `sessions.feature_pr_head` TEXT. Snapshot already `GET`s each tracked PR and discards `head.sha`. Two write sites (opened; `_process_one` on synchronize). Unique synth id | Medium. One new comparison in `_apply_snapshot`, one spent-SHA gate beside ADR-30 (c), payload fields the classifier already reads | **Chosen.** Same handlers, same routing, same FSM. Covers Design and Feature with one node kind. The snapshot GET is already paid |
| **B** | Adopt the FSM transition (ADR-21 as written) | A `_adopt_forward(sess, "design_revised")` next to the merge adopts. No schema | Low | **Rejected.** `_adopt_forward` (`reconciler.py:646`) writes `state` and does not dispatch. Reviewer is never asked. `fsm.transition("DESIGN_REVIEW", "design_revised")` is `None` anyway, so a second lost push while already in review is a no-op even as an adopt. This is how the issue's "looks healthy" row is manufactured in code |
| **C** | Treat `check_suite.completed` as `design_revised` | Map one event in `_event_kind` | Low | **Rejected.** The first instance *had* a `check_suite` and still stranded, because nothing maps it. The second evening's lost APPROVED had no suite that could stand in. Suites are optional (no CI, skipped) and droppable by the same funnel. A proxy that is a sibling of the lost event is not a backup |
| **D** | List failed hook deliveries and redeliver (`admin:repo_hook`) | New token scope the gateway does not have. New component. Recovers *every* dropped event, not just synchronize | High privilege, medium code | **Rejected as the repair.** Broader than the bug, and it does not help a POST GitHub never attempted. Funnel stability is owner ops, not an ADR. Named residual, not this change |
| **E** | Escalate `PAUSED_HUMAN` after N quiet reconcile passes in `*_REWORK`/`*_REVIEW` with no open turn | A counter on the session row | Low | **Rejected as a substitute.** A session correctly waiting for the author to push is indistinguishable from this detector's predicate, and §9.3 already pauses on three silent turns — that is instance three's *symptom*, not a fix. A pass-report field when we *do* synthesize is free and is kept |
| **F** | Reconciler dispatches the review turn directly, no delivery | Skip the envelope | Medium | **Rejected.** Second path into `DESIGN_REVIEW` / `CODE_REVIEW`. ADR-21's "prefer synthesis so the same handlers run" exists because teaching `_event_kind` / `route_for_recipient` / `build_digest` a second shape is how ADR-15/16/17/19 all got bugs. Do not add a third |

**A is the only option that both repairs the strand and still produces a review turn.** B converges the state column and drops the turn. C and D are different problems (event mapping; ingress). E names the silence and spends an owner round-trip every time an author is slow. F splits the loop.

**(a) The watermark is the last head SHA this session has *taken*, per tracked PR.** Two TEXT columns, `design_pr_head` and `feature_pr_head`, schema v10, incremental `ALTER TABLE` that **must preserve `deliveries`** (ADR-21's v7→v8 lesson). Not a JSON blob: the two PRs are already two columns. NULL means unknown, not "the zero SHA".

Write the column when `_process_one` takes a `pull_request` event for that PR whose payload carries `head.sha` — `opened` and `synchronize`, real or synthesized. **Take means: the delivery has passed the spent-SHA gate and is about to run the FSM / dispatch, not that the turn has returned.** Advancing on completion is how a five-minute pass during a long review re-synthesizes the same SHA and dispatches a second turn — #209's failure mode from the other direction, which is the constraint the issue stated. Advancing on synthesis *before* the delivery is queued is how a synth that then fails to insert seals the strand. The insert is `INSERT OR IGNORE` (`db.py:484`); the session write is the next statement in `_process_one`.

**(a′) Synth idempotency is the delivery primary key, not the watermark.** `delivery_id = "recon:sync:<pr_number>:<full sha>"`. A second pass that still sees a mismatch (drain has not run) hits `INSERT OR IGNORE` and does not enqueue a second row. Count `synthesized` only when the insert returns True. Do not write a `delivery_nodes` row for this id: it is not a GitHub node id, and stuffing it there would make a later real synchronize's node check mean nothing.

**(a″) The spent-SHA gate has three outcomes, and "newer" is not one of them.** A SHA is a hash. Nothing in the payload orders two SHAs. The draft said equal → spent, *newer* → take, NULL → take. An implementer then writes the only remaining rule — *not equal → take* — which reopens the defect this ADR closes:

1. Pass N synthesizes `recon:sync:5:X`. It drains, stamps `X`, dispatches a turn.
2. Author pushes `Y`. Its real `synchronize` is lost, as before.
3. Pass N+1 synthesizes `recon:sync:5:Y`. It drains, stamps `Y`. Correct so far.
4. **The original real webhook for `X` arrives late.** Measured latency for a redelivery on this host is **101 s**. Watermark is `Y`; `X ≠ Y`; *not equal → take* → a third turn, reviewing a head that no longer exists.

That is #209's shape from the other direction, on the read side. Acceptance (4) as first worded only constructed the equal case, so it could not catch this.

**Binding: compare the payload SHA to two facts, never to another SHA as if they were ordered.**

- payload SHA == watermark → spent (duplicate). This is the delayed-webhook race the draft named: synth drains first, stamps, real GUID arrives later with the same SHA; or the real event wins and the synth matches on drain. Either order, one turn.
- payload SHA ≠ live head → spent (stale), **even when it also differs from the watermark.** Live head comes from the **same** `GET /repos/{repo}/pulls/{n}` ADR-30 (c) already makes at this site (`fetch_pull`). Today that helper returns only `merged` and `state`; it grows `head.sha`. Not a second fetch. Not a git-graph walk. Not a new column.
- else take and stamp. The payload *is* the forge's current head and this session has not taken it. That is the only sense in which a SHA is "new". NULL watermark: the first bullet does not fire; the live-head bullet still does.

Place the gate **before the FSM**, sharing ADR-30 (c)'s GET, not inside the author-drop at `:443`. A stale or duplicate head must not run `design_revised` / `feature_revised`. A take still records P1 as before. A payload without `head.sha` fails open (cannot be this cue; dropping it would hide a fixture bug as a fix). A failed GET fails open on the stale bullet only — cannot prove stale, fall through to the duplicate check. Named residual: a late `X` during an API outage still dispatches. Same posture as ADR-30 (c) on a failed fetch.

**`deliveries.received_at` is not the stale clock.** `insert_delivery` stamps `received_at` at insert (`db.py:486–502`). A GitHub redelivery is a new POST; its row's `received_at` is *now*, after the newer take. A companion `design_pr_head_at` / `feature_pr_head_at` compared to `received_at` therefore misses the case it would be added to catch — the 101 s redelivery that is this ADR's own evidence. Rejected. Payload `updated_at` versus a take timestamp has the same shape if we trust GitHub's clock; the live head is the forge's fact and needs no clock.

A synth is built from the live head at pass time, so it passes the live-head check on drain unless a still-newer push landed in the interval; then it is stale and spent, and the next pass synthesizes that newer SHA. Correct.

**(b) The synthesized envelope is webhook-shaped and carries the fields the classifier and ADR-30 actually read.** Minimum:

- `action: "synchronize"`
- `pull_request.number`, `.title`, `.user.login` (the **PR author**, from the snapshot's already-fetched `pr.user`), `.head.ref`, `.head.sha` (the live SHA), `.node_id`, `.html_url`
- `sender.login` = that same PR author. GitHub's own `synchronize` is *usually* author-sent — ADR-30 (a″) excludes `pull_request` from the author drop for that reason — but it is not a forge law: the owner can push to a role branch, and did, on #57 (`eae0e847`, the unsticking commit). For the synth we *choose* the sender, so that case does not arise here. A synth that omitted `user.login` would not hit the author drop; it would hit #209's hole instead, routing to the counterpart as a comment.
- `repository.full_name`

`fetch_session_snapshot` already `GET`s `/pulls/{n}` and copies `head.ref` onto the node, discarding `head.sha`. Copy `head.sha` onto the PR node and onto the snapshot (`design_head_sha` / `feature_head_sha`, or one field per `_add_pr`). The GET is paid. Do not add a second fetch.

A synth whose `pull_request.user.login` is missing is a spec bug, not a runtime maybe. The review branch already carries `pull_request.user` from `node.pr_author` (`reconciler.py:733`, #209). This synth copies that field from the same snapshot PR object. Do not rebuild the hole.

**(c) NULL watermark, first pass after deploy.** Sealing is the failure mode: stamping the live SHA without synthesizing on a session that is already stranded makes the strand permanent.

- **State `DESIGN_REWORK` or `CODE_REWORK`, watermark NULL:** synthesize. The only FSM exit is `*_revised`. The live instances were this row. Residual: **every** `*_REWORK` session alive at deploy gets one review of the current head, stranded or correctly waiting. Each costs a vendor turn. Named, cheaper than sealing #57, and not "the one session that motivated the ADR".
- **Any other live state, watermark NULL:** stamp the live SHA, do not synthesize. A session in `DESIGN_REVIEW` that already reviewed this head must not get a duplicate turn (silent-turns risk, #209-shaped). Residual: a session already stranded *in review* at deploy, whose second push was lost, is not recovered by backfill. Tighten later if it is observed; it has not been.

Do not compare to `CHANGES_REQUESTED.commit_id` as the primary rule. It does not cover a second push while already in review, and it is a second mechanism. The watermark is the rule; NULL is only the backfill.

**(d) Do not synthesize `push`.** It is not an FSM kind. One event, the one `_event_kind` already maps. The owner-push on #57 still arrived as `synchronize`; `push` is the repo-level sibling and is not in the table.

**(e) Merged or closed PRs.** If the snapshot says merged, existing step-4 adopt of `design_merged` / `feature_merged` runs and this comparison does not. A synth `synchronize` against a merged PR would be ADR-30 (c)'s spent-PR on drain; do not emit it. If `fetch_snapshot` failed, skip the session (today's `if not snap: continue`); fail open, next pass retries. Do not invent a SHA.

**(f) The silence is named on the pass line, not as an escalation.** When a synth insert succeeds, the pass report counts it (`synthesized` already exists; a distinct `head_drift` is fine if it makes the log grepable, not required). Log at INFO with session, PR, old SHA, new SHA. **Do not** raise `PAUSED_HUMAN` because a `*_REWORK` session had no open turn for N passes — that is a correct wait for the author. Option E is the detector the issue asked for and it is the wrong alarm. After this ADR the stranded case is a synth within one interval (≤ 5 min, `RECONCILE_INTERVAL_S`); the 26-minute quiet is what disappears.

**Placement, because ordering is the design.** The comparison lives in `_apply_snapshot` after the merge adopts and before the node set-diff — same sweep, same snapshot, no extra GitHub call. It is not a new step in §11.2's numbered list; it is the missing later-action that step 5 cannot see and that step 4 must not adopt. The spent-SHA gate lives in `_process_one` with ADR-30 (c), **sharing that GET**, after `_event_kind` so the kind is `design_revised` / `feature_revised` when we decide, and **before** the FSM, because a stale or duplicate head must not run `design_revised` / `feature_revised`; the take path records P1 as before.

**Amendments in place, so an implementer starting at the section does not build the old sentence.**

- ADR-21's adopt bullet: synchronized PRs removed; pointer here.
- §11.2's "a missed webhook still converges" paragraph: merge still does; synchronize does not.

**Deliberately out of scope.**

- **The Tailscale funnel.** Transport. Owner ops. Reducing loss rate does not close the class.
- **Hook redelivery / `admin:repo_hook`.** Option D. A different component, a scope we do not have, and not a substitute.
- **The rest of #209.** The review-branch `user.login` hole is already patched; remaining sign-off on that issue is not this ADR. The payload rule here exists so the new synth does not reintroduce it.
- **Dropped `pull_request.opened`.** ADR-21 residual: a PR not yet on `sessions.design_pr` / `feature_pr` is invisible to both halves. Unchanged.
- **`check_suite` as a kind.** Option C, rejected.
- **Repairing session #57's recorded unauthorized merge.** Data. `@huozhe`.
- **A host-side comment on a busy PR consuming silent-turn budget.** Named in the issue, not this subject.

**Deploy is gateway-only.** No image rebuild, no `docker rm -f`. Restart the LaunchAgent. Verify by process start time and by the next rework round logging the synth (or by a unit fixture that fails first: snapshot with moved SHA, zero `_dispatch_turn` today, one after).

**Acceptance — (1) is where today's defect is proven, and it must fail first.**

1. **A session in `DESIGN_REWORK` whose Design PR head SHA is not the stored watermark enqueues exactly one `pull_request.synchronize` and does not adopt the state.** Drive `_apply_snapshot` with a snapshot whose `design_head_sha` differs from `sessions.design_pr_head`. Assert one new delivery, `event=pull_request` `action=synchronize`, session state still `DESIGN_REWORK` (the drain, not the sweep, advances it). Today's code: `synthesized=0`, state unchanged, no delivery. **Assert the delivery exists, never that an agent summary is sensible.**
2. **The same snapshot a second time inserts zero rows.** `INSERT OR IGNORE` on `recon:sync:<pr>:<sha>`. This is the #209-from-the-other-direction item. A test that only checks pass 1 passes against a version that re-enqueues every five minutes.
3. **The synth payload has `pull_request.user.login` equal to the PR author, `sender.login` the same, and `pull_request.head.sha` equal to the live SHA.** A payload that omits `pull_request.user` fails this item on purpose — that is the hole #209 named, already patched on the review branch (`reconciler.py:733`), and this synth must not reintroduce it. The SHA field is the one the watermark reads; a payload with author and no SHA cannot be spent against the column.
4. **A real `synchronize` for a SHA the watermark already holds dispatches no turn and does not change `silent_turns`.** The delayed-webhook race. Session may be `DESIGN_REVIEW` already (synth drained first) or still `DESIGN_REWORK` (synth queued, real arrived): either way, one turn across both deliveries.
4′. **A real `synchronize` whose payload SHA is not the live head dispatches no turn, even when it also differs from the watermark.** Watermark `Y`, payload `X`, live head `Y`. **This is the item that must fail first against *not equal → take*.** The 101 s redelivery of `X` after `Y` was taken. A test covering only (4) or only (5) does not reach it.
5. **A real `synchronize` whose payload SHA *is* the live head and differs from the watermark still routes, still yields `design_revised`, still dispatches one Developer turn.** Not "newer" — hashes are not ordered. ADR-30 (a″) paired with this so a spent-SHA gate written too wide does not kill rework. Same module. The GET that (4′) reads must return `head.sha`; a stub that only returns `merged`/`state` makes (4′) and (5) both untestable.
6. **`CODE_REWORK` + Feature PR head drift is the same synth, `feature_revised`, Architect turn.** Instance three. A Design-only test misses the merge-authorization lie.
7. **`check_suite.completed` for the new SHA, with no synchronize, does not advance the FSM and does not dispatch.** Option C must fail this. The first live instance is the fixture: suite arrived, session stayed `DESIGN_REWORK`.
8. **NULL watermark in `DESIGN_REVIEW` stamps and does not synth; NULL watermark in `DESIGN_REWORK` synths once.** Backfill both arms. The REVIEW arm fails if NULL is treated as mismatch unconditionally; the REWORK arm fails if NULL always stamps.
9. **A merged Design PR with a moved head synthesizes nothing; step 4's merge adopt still runs.** Do not emit a spent synchronize for the sweep to drain.
10. **Live, before sign-off.** After deploy, the next session that completes a rework round — real synchronize or synth — leaves `silent_turns` at 0 or 1, never 3, and does not record `unauthorized` on a merge that had Architect approval on the live head. Gateway-only; verify by LaunchAgent restart time. A quiet log with no rework round does not discharge this.
