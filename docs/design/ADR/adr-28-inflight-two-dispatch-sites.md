# ADR-28: The In-Flight Set Has Two Dispatch Sites, and ADR-27 Wired One

*A decision of record for `agentd`. Index and context: [unified design spec §16](../unified_design_spec.md#16-architecture-decision-records).*


*Fixes #158.* Not an incident — a code read of merged `main` (`b414b0c`), filed because the window is narrow enough to go unnoticed for a long time and the failure it produces is silent. ADR-27 populates `_inflight_turn_ids` from exactly one place: `_dispatch_turn`. `resume_interrupted_turn` (`design_loop.py:1272`) is a second, structurally identical call site — its own `_lock_for_project_role` (`:1308`), its own `RunnerClient`, its own blocking `turn.resume` RPC (`:1317`) — and ADR-27 never touched it.

**ADR-27's own reasoning about this function is correct, and incomplete.** It states: "because the guard sits above the resume decision, a live turn is never marked `resuming`, so `resume_interrupted_turn` never runs against one." True, and it is the right argument for what it addresses — a *second* resume cannot be started against a turn already in flight via `_dispatch_turn`. It says nothing about the window *during* a resume that has already started via `resume_interrupted_turn` itself. That window is this ADR's subject, and it is not on ADR-27's out-of-scope list — only `TEARDOWN` removal, an age floor, and step 7's demotion are excluded there.

**The window, and why it is reachable.** While `resume_interrupted_turn` holds its RPC open, the turn is live in this process and absent from `_inflight_turn_ids`. `_apply_open_turn`'s ordering on `main` puts the retire branch ahead of the one guard that would otherwise catch this:

```python
if tid in _inflight_turn_ids: return                    # resume path never enters the set
...
if not_running or too_old: … retire                     # reachable
if str(turn.get("status") or "") == "resuming": return   # too late — retire already ran
```

The reconciler runs on its own thread and takes no lock, so nothing serializes it against an in-progress resume. Two paths reach the retire branch, both pre-existing and unaffected by ADR-27:

- **`too_old`.** `started_at` is the turn's *original* start; resuming does not reset it, and an orphan is old by construction — it must be under `resume_max_age_s` (3600s) to be resumed at all. The resume RPC can then run for up to `rpc_timeout_s` (`turn_deadline_s + rpc_timeout_grace_s`, `config.py:85`). A turn resumed close to the 3600s bound can cross it while its own RPC is still executing.
- **`not_running`.** `process_resuming_turns` (`design_loop.py:289`) checks session state once, before calling `resume_interrupted_turn`. The state can change mid-RPC — an escalation on another path, or the owner closing the issue — and nothing re-checks it.

**ADR-27 turned this from invisible into permanent.** Before ADR-27, a wrong retire here would have been overwritten by the resume's own genuine completion — silent, but self-correcting. ADR-27 added `AND ended_at IS NULL` to `finish_turn` precisely so a bad retire stops hiding behind the turn's own completion (its evidence-preservation half). That guard is correct and stays; the consequence for this call site is that the resumed turn's real `finish_turn` (`:1348`) is now **discarded and logged, not applied**. The row is stuck `interrupted`, carrying the retire's reason, permanently, while the agent's work actually landed — and `report["retired"]` counts a recovery that never happened, which is the exact metric corruption #151 was filed about, now on the sibling call site.

**The fix is the same guard, at the second call site, through a shared helper rather than a second hand-rolled copy.** Two call sites have already diverged once — ADR-27 wired one and missed the other entirely — so a third dispatch site (or a future edit to either existing one) inheriting the same risk is exactly what a shared primitive removes:

```python
@contextlib.contextmanager
def _inflight(turn_id: str) -> Iterator[None]:
    """ADR-28: mark turn_id in-flight in this process for the block's duration."""
    _inflight_turn_ids.add(turn_id)
    try:
        yield
    finally:
        _inflight_turn_ids.discard(turn_id)
```

Defined in `design_loop.py` beside `_inflight_turn_ids` itself. `_dispatch_turn`'s existing `_inflight_turn_ids.add(turn_id)` / `try: … finally: _inflight_turn_ids.discard(turn_id)` becomes `with _inflight(turn_id):` wrapping the identical block — mechanical, no behavior change, and it removes the one place that pattern was written by hand. The two call sites are never live for the same `turn_id` at once under correct operation — `resume_interrupted_turn` only runs on turns ADR-27's own guard already kept out of `_dispatch_turn`'s hands — so a plain add/discard pair needs no reference counting.

**Placement in `resume_interrupted_turn` follows the same rule ADR-27's own review had to correct once already: guard exactly the block that can call `finish_turn`, not merely "around the RPC."** Open `with _inflight(turn_id):` immediately before `lock = _lock_for_project_role(project_key, role)` (`:1308`) — after the `busy_until` and no-`runner` early returns, neither of which starts an RPC, so neither has anything to guard — and let it wrap everything through the single `finish_turn` call (`:1348`), the budget update, and the function's `return True` (`:1364`). The existing RPC-only `try/except Exception as exc: … raise` (`:1309–1343`) nests inside unchanged; an exception there propagates through the `with` exactly as it would through a `finally`, discarding the id on the way out. Unlike `_dispatch_turn`, there is only one `finish_turn` call site here — the `except` arm re-raises without calling it — so there is no second arm to miss.

**Deliberately unchanged:** `_apply_open_turn`'s ordering, `NON_RUNNING_STATES`, `resume_max_age_s`, and every item ADR-27 already put out of scope (`TEARDOWN` removal, an age floor, step 7's demotion). This closes a gap inside what ADR-27 already built; it revisits none of ADR-27's decisions.

**Acceptance.** (1) A turn marked `resuming` with its id seeded into `_inflight_turn_ids` (simulating an in-progress `resume_interrupted_turn`), session flipped to a `NON_RUNNING_STATES` value — `_apply_open_turn` returns before the retire branch; `report["retired"] == 0`; the row is untouched. (2) The identical fixture with age pushed past `resume_max_age_s` instead of the state change — same result, `report["retired"] == 0`, row untouched; the two triggers named above are independent and both need coverage. (3) `resume_interrupted_turn` itself, exercised against a fake `RunnerClient`/`cli.call`: while the call is in progress, `turn_id in _inflight_turn_ids` is true; after `resume_interrupted_turn` returns (success or exception), it is false — proves the wiring, not just the guard's own logic, which (1)/(2) already cover structurally the same way ADR-27's acceptance (1) did for `_dispatch_turn`. (4) A retire attempt against a turn whose id is currently seeded must not be able to reach `finish_turn` at all — the in-flight check in `_apply_open_turn` is what prevents it, not the `ended_at IS NULL` guard added by ADR-27, which is the last line of defense, not the mechanism this ADR relies on. (5) `_dispatch_turn`'s existing behavior is unchanged by the `_inflight` refactor — ADR-27's own acceptance (1)–(4) continue to pass verbatim, proving the helper is a faithful extraction and not a second implementation.
