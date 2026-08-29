# ADR-32: The Gateway Asks Its Two Loop-Safety Questions of the Wrong Subject

*A decision of record for `agentd`. Index and context: [unified design spec §16](../unified_design_spec.md#16-architecture-decision-records).*


*Fixes #156 and #147.* **Both are the same shape and they are eleven lines apart in `_process_one`: the gateway asks a question about the *delivery* when it means the *turn*, and asks whether the session is *paused* before asking whether it is *alive*.** Neither `SilentTurnTracker` (`loop_safety.py:48`) nor `route_for_recipient` (`routing.py:74`) is wrong — both faithfully answer what they are handed.

**(A) `observed` is a property of the triggering delivery, not of the turn (#156).**

```python
observed = state_changed or kind in _OBSERVED_PROGRESS_KINDS   # design_loop.py:1011
```

`kind` is the kind of the delivery that *woke* the turn; `state_changed` is the FSM transition **that same delivery** caused. Both are fixed before the agent starts work, so a webhook produced *by* the turn cannot contribute to either term. `silent_turns` is reset at `:817` (`decision.reset_consec`, owner activity), `:3093` (owner unpause) and `:3205` (the post-unpause resume turn, which passes `observed_progress=True`), and written by the tracker at `:1017`. **A mid-turn webhook reaches none of them** — it can only count later, if it happens to trigger a turn of its own.

**The evidence, stated narrowly, because the first draft of this ADR overstated it.** (Owner's catch.) The draft named session **#84** as the case (a) fixes. It is not. Every delivery #84 ever received is `issues.*` or `issue_comment.*` — **zero `pull_request` deliveries, ever** — so the three turns that escalated it contain no progress kind in any window and **(a) would not have prevented #84.** Its `paused_reason` names `public_actions=['api_write','comment']`, and that comment *was* observed, as a kind this ADR deliberately excludes. #84 is a **different instance of the same wrong subject**, and the only thing that reaches it is the change listed under *Deliberately out of scope*. The same correction applies to #156's own worked example: session #151's `t-1ddbb413275e` was triggered by `issues.labeled`, which `_event_kind` maps to `issue_opened` — already a member of `_OBSERVED_PROGRESS_KINDS` (`:137`) — so it resets today and demonstrates nothing.

**What does support (a) is one confirmed live instance**, and this ADR claims no more than that:

```
t-9e31cfd2f1f1   #63, developer, 98 s
  triggered by  issue_comment.created (sender huozheclaude — non-owner, so kind is not a progress kind)
  in window     pull_request.synchronize on PR #149 (the session's own PR) at +91 s
  outcome       claimed public_actions; observed=False; silent_turns incremented
```

One instance, not a pattern. The mechanism is nonetheless certain from the code, and the cost model is arithmetic rather than anecdote: turns on this project run 15 s to ~11 min, `silent_turn_limit` defaults to **3** (`config.py:120`), and each escalation costs an owner round-trip.

**The window is observable, and cheaply.** Ingress is accept-and-queue (ADR-10) and writes `deliveries` from the request path (`server.py:238`), independent of the drain — so a webhook produced mid-turn **is already in the table** with a `received_at` inside `[started_at, ended_at]`, even while the single drain thread sits blocked in `_dispatch_turn`.

**Binding, and it widens only:**

```
observed = kind in _OBSERVED_PROGRESS_KINDS          # the trigger term, unchanged
        or <FSM state changed across dispatch>       # captured before, compared after
        or <a progress-kind delivery for this session with started_at < received_at <= ended_at>
```

**The trigger term is retained explicitly.** (Owner's catch.) The draft said only "capture `state` before dispatch and compare after", which places the capture *after* the FSM at `:844` — so the triggering delivery's own transition falls outside the comparison, and the trigger is excluded from the window query too. Read literally, a turn woken by `design_pr_opened` — genuine progress that resets today via *both* terms — would score `observed=False`. That is the opposite failure to the one this ADR exists to fix.

**The triggering delivery is excluded by a stated bound, not "by construction".** (Owner's catch.) `deliveries.received_at` (`db.py:477`) and `turns.started_at` are both whole seconds, so the trigger does not *precede* `started_at` — it can equal it, and on this host it does: `t-a9044e280f21` and `t-797b18feee34` both have `received_at == started_at`. The exclusion rests entirely on choosing a **strict** lower bound, which is a decision to write down and to test, not a property to rely on.

**Residual, recorded rather than solved:** the canonical case is a turn that opens the PR at the *end* of its window, and that webhook can land after `ended_at`. It then degrades to today's behaviour and the next turn resets, so it is a missed reset rather than a false escalation — but no fixture can fail on it, because a fixture plants the delivery itself. Only acceptance (9) would see it.

**(B) The paused defer is evaluated before the session is known to be alive, and `paused_reason` outlives the close (#147).**

- `paused` is computed at `:603` from `state == "PAUSED_HUMAN" or bool(sess.get("paused_reason"))`, and the DEFER branch at `:810` returns unconditionally — no age bound, no terminal check.
- The close path writes `state="CLOSED"` and **nothing else** (`:2675`); `TEARDOWN` likewise (`:2775`). `paused_reason` is cleared only on owner unpause (`:3093`).

A session closed while paused therefore reads as paused forever and re-defers every delivery it owns on every 5 s tick. #147 measured ~24 log lines a minute and 5,918 `route defer` lines from **two** delivery ids, and the precondition is live today: `#84` and `#32` are both `CLOSED` with a non-empty `paused_reason`.

**#147's own framing understates it.** It says *"Low priority. No correctness impact."* The churn is harmless; the state is not, because it makes #85's terminal gate unreachable on the path that most needs it.

**And the quiet log is not evidence of a fix.** Both of #147's named deliveries are `dropped`, and the only caller of `quarantine_deferred` is `agentctl` (`src/agentctl/__main__.py:171`) — a manual purge. This session's own handoff recorded #147 as "already fixed in practice" on exactly that inference, which is why it is written here.

**The decision.**

- **(a) `observed` is measured over the turn's window**, per the three-term binding above, with a strict lower bound. `public_actions` are untouched and still never reset the counter: this widens what counts as *observed*, it does not start trusting the agent (PR #42 B1).
- **(b) The whole post-turn counter write becomes atomic — not just `silent_turns`.** (Owner's catch.) `sess` is read at **`:497`**, and `paused` (`:603`) and `BudgetState` (`:605–609`) are built from that same snapshot; the tracker reads its count at `:1005` and the write-back at `:998–1000` / `:1017` carries `turn_count` and `consec_agent_turns` in the identical read-modify-write shape. Making only `silent_turns` atomic would satisfy the acceptance item while leaving two counters racing, which does not deliver the stated justification. All three become SQL increments.
- **(c) The defer branch is gated on liveness — a second check, not a move.** (Owner's catch.) The draft said "the terminal check moves above the paused defer". It cannot: `:855` reassigns `state = tr.new_state`, so the gate at `:873` deliberately tests the **post-transition** state, which is what catches a delivery that moves a *live* session into `TEARDOWN`. Moving it above `:810` would make it read the pre-transition state and regress #85 — an `issues_closed` for a live session would fall through to dispatch. Binding: leave `:873` exactly where it is, and make the DEFER branch at `:810` conditional on `state not in TERMINAL_STATES`, marking such a delivery `done`. **This is the mirror of ADR-30 (c) only in class, not in remedy** — there one gate moved *after* the FSM; here a second gate is added *before* the defer.
- **(d) Closing or tearing down a session clears `paused_reason`.** Both `:2675` and `:2775`. Without this, (c) fixes new closes and leaves every already-closed session still reading as paused. Existing rows are named in acceptance as owner data repair.
- **(e) The escalation text is re-checked.** *"no matching event arrived"* may only be emitted once the window has actually been examined and found empty.

**Deliberately out of scope.**

- **Widening `_OBSERVED_PROGRESS_KINDS` to include `issue_comment`.** This is the change that would reach #84, and it is refused: it makes almost every turn observe progress and destroys the signal. Naming it here rather than in a footnote, because #84 is otherwise the most tempting evidence in the file.
- **Threading the drain.** (b) is the prerequisite that makes it safe later; this ADR does not touch concurrency.
- **§9.3's fingerprint and zero-thread-progress signals.** Untouched.
- **Whether a terminal session should keep a reason for forensics.** (d) clears it because it is load-bearing control state; `escalations` already stores the text.

> **Amended 1.32.1 (#184), by implementation.** Four corrections, each proven by
> reverting a binding and watching which test survived: **(3)** cannot fail on an
> inclusive lower bound — the trigger term already covers self-observation, and
> what the strict bound protects is a *different* delivery from the same second
> before the turn; **(7)**'s stated failure mode is unreachable, since no webhook
> kind transitions a live session into `TERMINAL_STATES` on the path that reaches
> the gate, so it now asserts the gate still runs for a delivery that never
> defers; **(d)** named a conditional re-assert rather than the `issues_closed`
> transition where a session enters TEARDOWN; and the escalation must stay gated
> on the counter having moved, which the original read-modify-write got for free
> from its early return. The numbering below is the original; see the revision
> history for what each became.

**Acceptance — (1) and (6) are where today's defects are proven, and both must fail first.**

1. **A turn that produces a progress webhook mid-flight resets `silent_turns`.** Reproduce `t-9e31cfd2f1f1`'s shape: `issue_comment.created` trigger from a non-owner, a `pull_request.synchronize` for the session's own PR planted from the dispatch stub with `received_at` inside the window. Assert `silent_turns` is **0**; today it is 1. **Assert the counter, not the absence of an escalation** — the latter passes for two turns regardless.
2. **Three such turns do not escalate.** The unit of the bug is the run; (1) alone passes on an implementation that resets one turn in three.
3. **The triggering delivery does not count as its own progress, and the fixture must pin `received_at == started_at`.** (Owner's catch: without that constraint an implementation using `>=` passes this item, which is the exact error it exists to catch, and both columns are whole seconds so the collision is ordinary rather than contrived.) Same shape as (1) with no mid-turn delivery: `silent_turns` must increment.
4. **A turn woken by a progress kind still resets.** `design_pr_opened` trigger, nothing in the window, no state change: `silent_turns` must be 0. This fails against the draft's two-term reading and nothing else in the suite detects it.
5. **`public_actions` still never reset.** A turn claiming `api_write` with nothing observed increments. Name the test for PR #42 B1.
6. **A delivery for a session in `TERMINAL_STATES` is marked `done`, not deferred** — with `paused_reason` set, which is the shape on disk. Assert the **delivery status**; "no turn dispatched" passes against the bug.
7. **#85 is not regressed.** An `issues_closed` for a **live** session still reaches the gate at `:873` after its FSM transition and dispatches no turn. This is the item that fails if (c) is implemented as a move.
8. **Closing *and* tearing down clear `paused_reason`.** Both paths; and a session that was never paused is unaffected.
9. **Atomicity across all three counters**, asserted as *"the write is a single SQL statement"* rather than as a race — a threading test would be flaky and prove less. `turn_count` and `consec_agent_turns` included, or (b)'s justification is not met.
10. **Live, before sign-off.** No unit test proves the wiring: a real turn that opens a Design PR must leave `silent_turns` at 0, and it is also the only thing that can observe the late-webhook residual above. **Gateway-only**: no image rebuild, no `docker rm -f`; verify by daemon restart time.
11. **Data repair, recorded rather than performed by code.** `#84` and `#32` carry stale `paused_reason` values today. Clearing them is an owner action; a migration that rewrites session state is a larger decision than this ADR makes.
