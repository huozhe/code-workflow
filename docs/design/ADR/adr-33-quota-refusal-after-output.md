# ADR-33: The Quota Gate Assumes a Refusal Arrives Before Any Output, and the Delivery Pays for It

*A decision of record for `agentd`. Index and context: [unified design spec §16](../unified_design_spec.md#16-architecture-decision-records).*


*Fixes #168.* **A vendor session limit that lands *after* the turn has produced output is recorded `failed`, and `failed` consumes the delivery.** The classification defect is one condition in the runner; the stall is the gateway treating `failed` as a routed outcome. The second is what makes the first permanent.

**The gate.**

```python
# cli_session.py:761-767, inside `if is_err:`
# Vendor refusal produces no assistant text and no tool use.
# A turn that *talked about* a limit still has both (#94 B1).
quota = (
    classify_quota(text=str(summary))
    if not texts and not public_actions
    else None
)
```

The comment states the assumption plainly: a refusal arrives before any output. A turn that runs for minutes, streams assistant text, and *then* hits the session limit has `texts` non-empty, so `classify_quota` is never called and the limit is invisible to the gateway.

**The evidence, re-derived from the database rather than taken from the issue.**

```
t-a2c3e6ebe3ae   #151, architect, 340 s   status=failed
t-690b8db01e8b   #169, architect, 195 s   status=failed
   both summaries  "You've hit your session limit · resets <hh:mm> (UTC)"
   both public_actions  []
```

`classify_quota` returns `{'status': 'quota_exhausted', 'retry_after': …}` for both strings — run, not read — and `parse_reset_epoch` resolves each to a real instant. `public_actions` is empty on both, so the only term that can have suppressed the call is `texts`. Delivery `2e2e5a1e-9c64-11f1-9ae0-99eb3acd4373` (`pull_request_review.submitted`, the `CHANGES_REQUESTED` on PR #170) is still `routed` on disk today.

**#168's central premise is wrong, and correcting it is what decides the binding.**

The issue asks that #94 B1's counter-example be reconstructed as a fixture "so the regression this gate exists to prevent is pinned before the gate moves". It does not need reconstructing: it exists, as `test_claude_mentions_of_limits_are_not_quota`, and **it does not depend on this gate.** Its three cases are GitHub rate-limit prose, a test-failure summary that mentions "vendor quota refusal", and a `gh api rate limit exceeded` traceback. None contains `session limit` or `usage limit`, so `_LIMIT_PHRASES` — which deliberately excludes bare "quota" and "rate limit", for exactly this reason — refuses all three whether the gate runs or not.

Deleting the condition outright and running the whole suite:

```
1 failed, 562 passed, 1 skipped
FAILED tests/test_cli_session.py::test_claude_quota_with_assistant_text_is_failed
```

**One test holds this gate, and its fixture is the live defect.** `test_claude_quota_with_assistant_text_is_failed` scripts an assistant frame followed by `is_error: true` whose `result` is the limit copy — which is, frame for frame, what `t-a2c3e6ebe3ae` and `t-690b8db01e8b` produced. Its docstring, *"Vendor refusal has no agent output"*, is the false premise stated as a fact. It was written to encode the assumption, not to record an observation, and it must be replaced rather than preserved.

**The refinement the issue proposes does not do the work it is offered for.** #168 suggests additionally requiring the limit copy to be the entire `result` string rather than a substring. Against the only counter-example that exists, that changes nothing: in that fixture the `result` *is* entirely the limit copy. Whole-string matching may still be worth having against a future envelope that wraps the copy in a longer error, but it has to be argued on that ground; it does not rescue #94 B1, because #94 B1 was never at risk.

**What actually separates the two cases is already in scope.** The branch is `if is_err:`. A turn that merely discusses a limit finishes successfully and ends `is_error: false`, so it never reaches the gate at all. `is_error: true` plus `_LIMIT_PHRASES` plus `_RESET_PHRASE` is the whole signal, and the output-emptiness test adds nothing to it.

**The delivery is the larger half.** `_process_one` returns early for `("role_busy", "quota_exhausted")` at `:1014`, leaving the delivery in its prior state for the drain to re-pick. `failed` falls through to `:1062`, `set_delivery_status(delivery_id, "routed")`. `routed` is terminal: the dispatcher scans `status='queued'` and promotes `deferred`, and the only other reader of `routed` in the tree is `list_live_sessions_with_closed_delivery`, a read-only reconcile helper scoped to `issues`/`closed`. Nothing re-delivers a `pull_request_review`. One misclassification therefore decides between *"wait for the reset and continue"* and *"the session is over"*, which is why #169 sat in `DESIGN_REWORK` for 10.5 hours with `open_turns=0`.

**The decision. The change is (a) and (b) — one condition and one test.** (c), (d) and (e) are consequences of the existing `:1014` early return and need no new code; they are listed because they must be asserted, not because they must be built. (f) is the one thing this ADR adds beyond the deleted conditional, and it is the item worth reviewing.

- **(a) The gate drops the output-emptiness condition.** Classification is `is_error: true` and vendor limit copy. `_LIMIT_PHRASES` remains the thing that keeps model prose out, because that is the thing that has always been doing it.
- **(b) `test_claude_quota_with_assistant_text_is_failed` is replaced, not amended.** Its shape is the defect; keeping it under a new assertion would preserve the fixture that taught the wrong lesson. The #94 B1 test stands unchanged and is the regression guard.
- **(c) A quota-refused turn leaves its delivery retryable.** Already true via `:1014`. Asserted on the delivery status, because it is the item that prevents the stall.
- **(d) `retry_after` reaches `_hold_role_for_quota`.** Already true: `:1385` calls it on `status == "quota_exhausted"`, and the hold short-circuits at `:1187`, ahead of `get_runner`, the reachability ping and `ensure_session`. **The draft claimed the re-pick therefore "costs a dict lookup and never a vendor call". That was wrong — see (f).**
- **(e) A refusal spends no turn budget.** Already true, and for the same reason as (c): `quota_exhausted` returns at `:1014`, above `bump_turn_counters` at `:1017`. Verified — two refused turns leave `turn_count`, `consec_agent_turns` and `silent_turns` all at 0. Asserted so it cannot regress, in the manner of (3), not built.
- **(f) The role hold is consulted before `_observe_stall_signals`, not only inside `_dispatch_turn`.** This is the binding the draft was missing.

**(f), and why the draft's (d) was wrong.** (Owner's catch.) The hold check lives at `:1187`, inside `_dispatch_turn`. `_process_one` does not reach it first: it reaches `_observe_stall_signals` at `:944`, twenty-eight lines earlier, and that helper runs for exactly `design_changes_requested`, `design_revised`, `code_changes_requested` and `feature_revised`. Delivery `2e2e5a1e…` is a `pull_request_review.submitted` carrying `CHANGES_REQUESTED`, which `_event_kind` normalises to `design_changes_requested` — the ADR's own evidence takes this path.

On every re-pick the helper fetches review threads from GitHub uncached (`# reviewThreads: always fetch — resolve does not move head`; only the diff stat is cached, keyed on `base...head`, which does not move either), then increments `zero_thread_rounds` because nothing resolved between two ticks five seconds apart, then increments `review_rounds` unconditionally for `design_changes_requested`. Driven directly, with `_dispatch_turn` stubbed to refuse:

```
pass 0: delivery=deferred  state=DESIGN_REWORK  zero_thread_rounds=1 review_rounds=1 threads_fetches=1
pass 1: delivery=deferred  state=DESIGN_REWORK  zero_thread_rounds=2 review_rounds=2 threads_fetches=2
pass 2: delivery=done      state=PAUSED_HUMAN   escalated: "stall: zero thread progress for 3 review rounds"
```

`Dispatcher.idle_wait_s` is 5.0, so that is **about fifteen seconds** from refusal to a paused session, on a stall signal describing the drain rather than the agents — the same class of defect as #156, a §9.3 counter answering a question about the wrong subject. Without (f), (a) turns a recoverable quota hold into an escalation on the common path, which is worse than the stall it replaces.

Two corrections to how the hazard was first described, both from running it:

- **The churn is bounded, not unbounded.** The escalation marks the delivery `done` on the third pass, so the cost is three GraphQL fetches and one wrong escalation. A sustained per-hour call rate never materialises, because the thing that stops the loop is the bug.
- **Marking the delivery `deferred` on refusal does not help.** It is *already* `deferred` — that is how the drain re-picks it — and `list_deferred` selects `WHERE status = 'deferred'` with no backoff column. Only the hold check does the work.

**Binding for (f):** compute the recipient's role key in `_process_one` and, when `_role_busy_until` shows a live hold, leave the delivery `deferred` and return **above** the `_observe_stall_signals` block. Placed there rather than earlier, the FSM still applies exactly as it does on any other re-pick, and only the observation and its counters are skipped. This also covers the pre-existing `role_busy`-after-gateway-timeout case, which has the same shape; that is not a defect this ADR introduces, but it is the same line, so scoping around it would cost more than owning it.

**Deliberately out of scope.**

- **Whole-string matching of the limit copy.** It does not decide the case it is usually offered for: the only counter-example's `result` already *is* the limit copy entire. If a wrapped envelope shows up, it gets its own item and its own sample.
- **Persisting the role hold.** `_role_busy_until` is a module-level dict, already annotated *"In-memory; forgotten on restart"* (`:191`). A restart inside a hold re-dispatches and burns one more refusal. Bounded, recorded, and a schema change is a larger decision than this ADR makes.
- **Whether a `failed` turn should reach `routed` at all.** #168 raises it and it is a real question — consuming a delivery on the first failure with no bounded retry is what makes any bad turn terminal, not just this one. Broader than the misclassification, and folding it in would let a green quota test read as evidence for it.
- **Making `_observe_stall_signals` cheap or cached.** (f) stops it running under a hold; it does not make the helper itself cheaper, and the uncached fetch is correct when a round really has happened.
- **Grok's path.** `_GROK_STOP_QUOTA` classifies from `stopReason` and never reaches this gate.
- **Repairing `t-a2c3e6ebe3ae`'s recorded status.** Historical row.

**Acceptance — (1) and (6) are where today's defects are proven, and both must fail first.**

1. **A limit that follows real work is `quota_exhausted`.** Script an assistant text frame, then `is_error: true` with the limit copy as `result`. Assert `status == "quota_exhausted"` and `retry_after` equal to the parsed instant. Today this is `failed` with no `retry_after`. **Assert `retry_after`, not just the status** — a status-only assertion passes against a classification that loses the reset.
2. **Tool use before the limit is the same case.** Same shape with a non-empty `public_actions` and no assistant text. The gate has two terms and (1) exercises one of them.
3. **#94 B1 still returns `failed`.** `test_claude_mentions_of_limits_are_not_quota` unchanged and passing, `retry_after` absent. Satisfied by an existing test surviving, which is the point — it was never held by the gate.
4. **A turn that ends `is_error: false` is never quota, whatever it says.** A successful turn whose text is the limit copy verbatim returns `done`. Fails if (a) is implemented by dropping the `is_err` guard rather than the emptiness condition.
5. **The delivery survives.** Assert the delivery is **not** `routed` after a quota-refused turn. Necessary but not sufficient — see (6), which is the item that catches what this one misses.
6. **Re-picks under a hold are inert.** Drive **four** re-picks of a `design_changes_requested` delivery while the role is held and assert: `zero_thread_rounds` and `review_rounds` unmoved, **zero** calls to the thread fetcher, session still not `PAUSED_HUMAN`. Four, because the escalation lands on the third. Without (f) this fails on pass three with `stall: zero thread progress`; acceptance (5) passes throughout and detects nothing.
7. **The hold is taken and short-circuits the dispatch.** `_role_busy_until` is set to the parsed reset, and the next attempt returns `role_busy` **without spawning** — assert the spawn did not happen, not merely that the status is `role_busy`.
8. **A refusal spends no turn budget.** `turn_count`, `consec_agent_turns` and `silent_turns` unchanged across a quota-refused turn. Passing today; asserted against regression.
9. **Verified inside the image.** This is runner code. The gateway suite imports `agentd_runner` from the source tree, so a green unit run proves the logic and not the deployment — rebuild and drive one turn in the container before sign-off.
10. **Live, before sign-off.** The next real quota refusal is recorded `quota_exhausted`, its delivery is re-picked after the hold, and the session continues **without pausing in the meantime**. **Runner change**: image rebuild plus `docker rm -f` on the project container, since `ensure_session` adopts a running container regardless of image.
