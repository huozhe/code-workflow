# ADR-16: Raising the Checkbox Requires an Observed Owner Tick

*A decision of record for `agentd`. Index and context: [unified design spec §16](../unified_design_spec.md#16-architecture-decision-records).*


*Resolves #89.* #65's restore-*up* branch (`was is True and now is False`) assumed `changes.body.from` being ticked meant *an owner tick was present before this edit*, and composed `checked=True`. The before-image has no actor. An agent who ticks, then unticks, produces the same pair. Observed live on session #84 (2026-08-13, gateway `d576107`, ADR-15 already deployed):

```
10:10:37  agent edit: - [ ] → - [x]                          (queued)
10:12:50  agent edit: - [x] → - [ ]  + marker                (queued)
10:14:02  checkbox moved since webhook (now=True current=False); skip stale restore   ← ADR-15, correct
10:14:03  patched issue body … restored=checked
```

Final body `- [x] Human Verification Complete`, `sessions.verified_at = NULL`, both senders `huozhegrok`. §10.3 classifies from the body, so a close in that state records `VERIFIED` on a tick no human made. §13.1's "Agent cannot forge the verification record" did not hold. An agent cannot undo it: every later agent untick re-enters the same branch. Only an owner untick clears the box, because the owner path records rather than restores.

**#65's B3 invariant does not cover this.** B3 guarantees the box never leaves an agent edit *more checked than it entered*, measured against that edit's own before-image. Restore-up deliberately increases checkedness relative to the *after*-image, and justifies it with a before-image whose provenance is never established.

**What #65 got right, and why the conclusion was still wrong.** The docstring on `_handle_session_issue_edited` said local `verified_at` must not drive restore, because a missed delivery while the daemon is down must not let a later agent step-refine untick a real owner mark. That reasoning is sound for B1 (already-checked before *and* after — leave it) and for "do not use `verified_at` as a reason to *untick*." It weighed only one direction:

| | cost |
|---|---|
| `verified_at` missing but the owner really ticked (dropped delivery) | the box is unticked; **the owner re-ticks once** |
| `was=True` trusted but the tick was an agent's | **a session records `VERIFIED` with no human verification** |

#65 already named the principle for the other way — *a missed restore costs one re-tick; a wrong restore can ABANDON a verified session* — and the mirror is worse: a wrong restore-up can **VERIFY an abandoned session**. The human gate failing open is worse than failing closed.

**New rule.** A composition that raises the checkbox needs `verified_at`; a composition that lowers or preserves it never does. That column is written only on the owner path (`is_owner and not is_agent`), so it is the only provenance signal in the system.

The raise spellings are one operation:

| branch | payload pair | raise would write |
|---|---|---|
| restore-up | `was=True, now=False` | `set_checkbox_in_body(current, checked=True)` |
| B4 | `was=True, now=None` (line deleted, block intact) | same call — and `set_checkbox_in_body` *inserts* the line when it is missing |
| B2 | `prev_block` ticked, `curr_block is None` | `reinsert_verification_block(current, prev_block)` — the ticked block comes back |

"Restore up" and "restore to `was`" read as different operations. Under the forge they are the same operation with different spelling: an agent tick, then an agent removal, then a compose that materialises a tick no owner wrote.

**What the refusal leaves behind is not always "nothing."** Restore-up and B4 refuse and leave the body: the agent already put the desired end state on GitHub (unticked, or line gone with the block still present). B2 is different. Closing the B2 forge (do not reinsert a ticked block) introduced the mirror: a body with no block classifies `ABANDONED` at §10.3, and the owner loses the Architect's steps. The refusal keeps; only the raise drops — reinsert `set_checkbox_in_body(prev_block, checked=False)`. Restoring the block *down* cannot forge; it is the same asymmetry as `checked=False` everywhere else.

`_can_raise_checkbox` decides and logs. It does not mark the delivery `done`. Callers that refuse and write nothing dispose themselves; B2 refuses and still composes.

A refusal that writes nothing posts no warning comment. A comment would wake the peer role (#85's family), and nothing was written. A B2 reinsert-unchecked *does* PATCH, so it uses the existing restore comment.

Log the refusal at WARNING with the *branch name*, `was`, `now`, `verified_at`, and the sender.

**Unchanged.** B3, B5, B4-with-`was=False` (restore *down* cannot forge), B2 with an unticked `prev_block` (reinsert as-is), and ADR-15's two guards — fresh read, `now_current != now` abort, sanity floor, skip-if-unchanged. Those were proven correct in the same live run (two deliveries aborted with `checkbox moved since webhook`). Restoring a recorded owner tick still raises — that is #65's real case and the regression this gate must not break.

**Rejected: keep #65's rule and try to attribute `changes.body.from`.** GitHub's `issues.edited` payload does not name who wrote the before-image. Reconstructing it from prior deliveries is a reconciler, and the reconciler is #81.

**Follow-up, not this ADR.** Close-time reconcile — a ticked body with `verified_at IS NULL` must escalate rather than classify `VERIFIED` — is the defence in depth for anything that slips past the branch. It is a different function (`_owner_close_teardown`) and it collides with §10.3 ("no attempt to reopen an issue the owner closed") plus the teardown suite that currently treats a ticked payload as sufficient authority. Filed as #90 so this reversal stays one branch.

*Out of scope:* the §10.2 warning comment waking agent turns — closed by #85 (`agentd:gateway` marker).
