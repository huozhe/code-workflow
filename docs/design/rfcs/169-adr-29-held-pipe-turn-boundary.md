# RFC 169 — Implementation binding for ADR-29: give the held CLI pipe a turn boundary

| | |
|---|---|
| **Status** | Proposed (Design PR for #169) |
| **Issue** | [#169](https://github.com/huozhe/code-workflow/issues/169) |
| **Decision of record** | ADR-29 — [`../unified_design_spec.md`](../unified_design_spec.md) §16, and the §14.2 held-pipe paragraph |
| **Incident** | #162 — forensics sound, **diagnosis superseded** by ADR-29 |
| **Baseline** | `main` @ `09539ba` (spec 1.26.0). Every line number below is against that commit. |
| **Touches** | `agentd/docker/session-runner/agentd_runner/{cli_session,turn}.py` · `agentd/src/agentd/{rpc_client,design_loop}.py` · `agentd/tests/{test_cli_session,test_public_actions,test_turn_privilege}.py` |

## 0. What this RFC is, and what it is not

ADR-29 already decided *what* to do and *why*, down to call sites. It is the decision of record and this RFC does not re-open it. What ADR-29 does not have — because it was written from the incident record rather than from a working tree — is the set of things that only appear when you try to place the code:

- one of its five changes, **(d)**, is inert as literally specified, in two ways: the retry counter it routes through is reset earlier in the same handler, so the delivery defers at attempt 1 forever and never escalates; and the same block re-arms a budget `_run_teardown_turns` has *already* exhausted, buying a second `_DELIVERY_MAX_ATTEMPTS` of teardown turns against a closed issue and a second escalation (§2.1);
- **(a)** invalidates the fixture pattern most of the CLI-session suite is built on — for both adapters, not just claude — and **(b)** turns four of those tests into live `claude` spawns (§2.3), which is also the mechanism by which acceptance (1) can be made to fail first;
- the WARNING line that (a) is defined by needs a `turn_id` that `CliSession.turn()` is not given today (§2.2);
- the post-spawn poll of **(b′)** has a window, and a child that dies *after* the window leaves the role spawning against a rejected id on every subsequent turn (§2.4);
- acceptance (7)'s live sign-off criterion — "the first drain discards zero frames" — has a benign counter-example that must not be read as a recurrence (§2.5).

So: §1 is the placement, §2 the corrections, §3 the order, §4 the test plan against ADR-29's acceptance (1)–(8), §5 risk. Read ADR-29 for the reasoning; read this for the contract.

The exit condition is unchanged and is ADR-29's: *a turn result produced for a different prompt is never persisted to `turns` or the transcript.*

---

## 1. The change set

### (a) Drain `_stdout_q` before writing a prompt

One helper on `LiveCliSession`, called by both adapters. It is the only new concept; everything else is placement.

```python
def _drain_stdout_unlocked(self, *, turn_id: str | None, reason: str) -> int:
    """ADR-29 (a). Anything queued before this turn's prompt is, by construction,
    from an earlier exchange. Discard it and name it in the log."""
    q = self._stdout_q
    if q is None:
        return 0
    n = 0
    while True:
        try:
            line = q.get_nowait()
        except queue.Empty:
            return n
        if line is None:
            q.put(None)          # EOF sentinel: re-put and stop (:804-806)
            return n
        n += 1
        log.warning(
            "stale frame discarded role=%s turn_id=%s reason=%s %s",
            self.role, turn_id or "unknown", reason, _frame_fields(line),
        )
```

`_frame_fields(line)` parses the frame once and renders `type`, `subtype`, `is_error`, `session_id` (claude) plus `method`, `id` (grok ACP), falling back to a truncated raw line on `JSONDecodeError`. One helper serves both adapters; a missing key renders as absent rather than as a second log format.

**The EOF discipline is load-bearing and is the reason this is `get_nowait()` and not `_readline_timeout()`.** `_readline_timeout` collapses "queue empty" and "EOF" into the same `None` return (`:794–809`), so a drain written on top of it cannot tell the two apart and would consume the sentinel as one more stale frame. Draining across a dead CLI would then eat the only end-of-stream marker later readers have.

Placement:

- `_claude_turn_unlocked` — immediately before the `stdin.write` at `:572`, after `msg` is built. Not earlier: the drain must be the last thing that happens before the prompt goes out.
- `_grok_turn_unlocked` — after the `if not self.acp_session_id: self._acp_initialize_unlocked()` block and after `mid` is taken (`:655–656`), immediately before `_acp_write(req)` (`:667`). Draining before `_acp_initialize_unlocked` would be wrong: initialize does its own correlated reads (`_read_jsonrpc_result`), and its frames are not stale.

Grok's behaviour does not change — it already discards by `mid` at `:700`. The log is the gain, and §4's acceptance (4) exists to keep it that way.

### (b) Respawn the role's CLI after any `is_error` result

In `_claude_turn_unlocked`, after `is_err` is computed (`:616`) and the envelope is logged, and **before both `is_err` return paths** — the `quota` early return (`:634–639`) and the common return (`:641`). Placing it before the `quota` branch is what makes it keyed on `is_error` rather than on quota recognition; #94 B1's gate (`:629–632`) suppresses `classify_quota` for precisely the shape #162 was made of, a limit hit *after* real work.

```python
if is_err:
    log.warning("claude failed envelope ...")          # existing
    self._respawn_after_error_unlocked()               # new — before any return
    quota = classify_quota(...) if not texts and not public_actions else None
    ...
```

```python
def _respawn_after_error_unlocked(self) -> None:
    """ADR-29 (b). is_error does not prove the vendor is done with the prompt;
    a fresh process gets a fresh _stdout_q and the abandoned stream dies with
    the old one. Never let a failed respawn discard the turn's own result."""
    log.warning("respawning cli after is_error role=%s pid=%s", self.role, ...)
    try:
        self._kill_unlocked()
        self._spawn_unlocked(continue_session=True)
    except Exception as exc:
        log.error("respawn after is_error failed role=%s: %s", self.role, exc)
```

Three properties of that shape, all deliberate:

1. **It is called under the lock.** `_claude_turn_unlocked` runs inside `turn()`'s `with self._lock` (`:493–501`), so the `_unlocked` variants are the correct ones and no re-entrancy exists.
2. **A failed respawn must not cost the result.** `_kill_unlocked` has already set `self.proc = None` and `self._stdout_q = None`, so a raising `_spawn_unlocked` leaves the session cleanly dead, `is_alive()` false, and the next `turn()` respawns through the existing dead-process path (`:495–503`). Swallowing here is not defensive habit — it is the difference between "this turn failed and we know why" and "this turn's result was replaced by a spawn traceback".
3. `turn()` calls `_sample_rss()` after the adapter returns (`:534`), so `cli_rss_kb` on an `is_error` turn now describes the **new** process. That is cosmetic and correct; it is stated so a reviewer does not read it as a bug.

`_grok_turn_unlocked` does not get (b). It is correlated by `mid`, a surplus frame cannot be claimed by a later turn, and killing a correlated session on every vendor error would trade a real cost for no gain.

### (b′) Re-enter by session id, never by recency, never bare

`_claude_cmd` (`:346`) is one function and all four `continue_session=True` sites go through it — `turn()`'s dead-process respawn (`:503`), `respawn()` (`:199`), (b)'s new respawn, and `ensure_role_cli_spawned` (`turn.py:573` / `server.py:397`).

```python
if continue_session:
    if self.claude_session_id:
        cmd += ["--resume", self.claude_session_id]   # named conversation
    else:
        cmd.append("-c")                              # most recent in cwd — fallback only
```

Never a bare `--resume`: at claude 2.1.237 the value is optional and an absent value opens an interactive picker. On our piped stdin that does not hang anything (stderr goes to a file, not a PIPE — `:275`), but every outcome costs a turn and none of them says why: an exit on non-tty stdin surfaces as a `failed` turn whose stderr tail is indistinguishable from a real crash; a picker that blocks on stdin eats our JSON message and the turn spins to `deadline_s`. The `-c` fallback is what makes the value structurally mandatory rather than a convention.

**Post-spawn liveness poll** — a hard dependency of the fallback, not a tidy-up. `Popen` succeeds for a child that dies 50 ms later; `_spawn_unlocked` goes straight from the `Popen` try/except (`:300–314`) to `_start_stdout_reader()` (`:316`), and `_sample_rss` (`:319`) swallows the `OSError` from a missing `/proc/<pid>`, so a rejected resume id is invisible until a write fails inside a turn.

**Exact order, because the obvious reading is wrong.** The poll goes *after the `Popen` try/except* (`:300–314`) and *before* `_start_stdout_reader()` (`:316`) — three statements that are adjacent today. Starting the reader first would race an immediate EOF sentinel into a queue the fallback is about to replace, which is the failure this ordering exists to prevent. (Developer's catch: the first draft cited `:301–320`, a range that already contains the reader, and then told the implementer to extract that same range — the two instructions could not both be followed.)

```python
_SPAWN_LIVENESS_S = 0.75   # module constant; tests patch it to ~0

if not self._await_liveness(_SPAWN_LIVENESS_S):
    tail = self._stderr_tail()
    if self.adapter in ("claude-code", "claude") and self.claude_session_id and continue_session:
        log.warning("claude died %.2gs after --resume %s; falling back to -c: %s",
                    _SPAWN_LIVENESS_S, self.claude_session_id, tail)
        self.claude_session_id = None            # the id is what died — do not reuse it
        self._discard_dead_child_unlocked()      # close its pipes before overwriting self.proc
        cmd = _wrap_with_role_secrets(self.role, self._claude_cmd(True))   # now -c
        self._popen_unlocked(cmd)                # one retry, never a loop
        if not self._await_liveness(_SPAWN_LIVENESS_S):
            raise RuntimeError(f"claude died after -c fallback: {self._stderr_tail()}")
    else:
        raise RuntimeError(f"cli died {_SPAWN_LIVENESS_S}s after spawn: {tail}")
```

`_popen_unlocked(cmd)` is an extraction of the `Popen` call plus its `preexec_fn` fallback — **`:300–314` and nothing else**. Not `_start_stdout_reader`, not `_acp_initialize_unlocked`, not `_sample_rss`: those stay in `_spawn_unlocked`, after the poll, and run once regardless of which attempt produced the live child. `_await_liveness` waits the full window in small increments, returning early only on death — "still alive after N ms" cannot be established faster than N ms. Nothing is draining stdout during that window, which is a second reason to keep it short.

`_discard_dead_child_unlocked()` closes the dead child's `stdin`/`stdout` before `self.proc` is overwritten. Precisely: `Popen.poll()` has already reaped the exit status (that is how `_await_liveness` learned the child was dead), so there is no zombie — but the pipe file descriptors stay open until the abandoned `Popen` is garbage-collected, and a `ResourceWarning` in a runner log is a poor way to find that out. `_kill_unlocked` does the equivalent cleanup for a *live* child and is not reusable here: it nulls `self.proc`, `self._stdout_q` and `self._reader_thread`, which is wrong mid-spawn.

Clearing `claude_session_id` here is the **single sanctioned reset**. ADR-29 records that `_spawn_unlocked` resets `acp_session_id` and `_rpc_id` (`:258–259`) and deliberately does not reset `claude_session_id`; that stays true for the ordinary path, and this one exception exists because the id is the thing that was just rejected. `_claude_cmd` then yields `-c` naturally, which is why the id is cleared *before* rebuilding rather than the argv being special-cased.

Non-claude adapters raise rather than fall back: grok has no equivalent recency flag, and a dead grok child would otherwise be discovered 30 s later as "ACP initialize timed out".

### (c) Process-unique RPC request ids — and this is not the fix for #162

`rpc_client.py:39`, `self._id = 0` per instance, and every client is constructed in a per-turn `with` block, so `session.attach` is always `id=1` and `turn.dispatch` always `id=2`. The mismatch guard at `:109` has **0** occurrences across every rotated log because it cannot distinguish two turns.

```python
_req_ids = itertools.count(1)   # module level; next() on a C iterator is atomic under the GIL
...
req_id = next(_req_ids)         # replaces self._id += 1; req_id = self._id
```

Module level, not per instance, because concurrent clients across role threads are the case that matters. The runner echoes `req.get("id")` back unmodified (`server.py:283–293`), so nothing on the wire cares about the value.

**Name the test for what it covers.** It closes an unobserved transport class; #162 is not in that class, because the frame was correctly addressed and the wrong content was already in the runner's own transcript record. A test named for #162 here would leave the real defect in the image behind a green check.

### (d) A teardown with open artifacts defers instead of giving up

`design_loop.py:2257–2260` marks the delivery `done` and returns before `_archive_and_close` — nothing ever retries, which is what turned one wrong turn into a permanent zombie.

The deferral belongs **inside `_run_teardown_turns`, on its success path** — not in the caller's block at `:2257`. §2.1 has the derivation; the shape is:

```python
# _run_teardown_turns, replacing the success path at :2661–2665
self._log_teardown_leaks(session_key)
if not turn_failed:
    if self.store.list_artifacts(session_key, open_only=True):
        return _TEARDOWN_DEFER if self._teardown_retry_or_give_up(
            delivery_id=delivery_id,
            session_key=session_key,
            reason="teardown turns ran; ledger still open",
        ) else _TEARDOWN_EXHAUSTED
    return _TEARDOWN_CLEAN          # no _delivery_attempts.pop here — see §2.1
return _TEARDOWN_DEFER if self._teardown_retry_or_give_up(
    delivery_id=delivery_id, session_key=session_key, reason=fail_reason
) else _TEARDOWN_EXHAUSTED
```

`_run_teardown_turns` returns a three-valued outcome instead of a bool, because `False` today means two incompatible things — *succeeded* and *exhausted* — and the caller must not treat them alike. The four other `return self._teardown_retry_or_give_up(...)` sites (`:2600`, `:2607`, `:2627`, `:2666`) each become the same one-line `DEFER`/`EXHAUSTED` mapping; the drained short circuit (`:2578–2586`) returns `CLEAN`.

The caller then keeps today's terminal behaviour exactly:

```python
if self.dispatch_turns:
    outcome = self._run_teardown_turns(...)
    if outcome is _TEARDOWN_DEFER:
        return                                   # stays 'deferred' — redriven by the dispatcher
    if outcome is _TEARDOWN_EXHAUSTED and self.store.list_artifacts(session_key, open_only=True):
        self.store.set_delivery_status(delivery_id, "done")   # escalated already; do not archive
        return
elif self.store.list_artifacts(session_key, open_only=True):
    # No turn dispatch configured: nothing will ever clear these. As today.
    self._log_teardown_leaks(session_key)
    self.store.set_delivery_status(delivery_id, "done")
    return
```

That reproduces `:2257–2260` for the two cases it is right for — turns disabled, and a budget already spent — and removes it as a second, fresh budget after a successful pass. `_teardown_retry_or_give_up` (`:2724`) logs the leaks itself on exhaustion, so the surviving `_log_teardown_leaks` calls are the per-attempt one already at `:2660` and the `dispatch_turns is False` branch. Deferred deliveries are re-driven by `Dispatcher.drain_once` → `process_deferred_batch` (`dispatcher.py:71–76`), so "defer" here genuinely means "retry", not "park".

**This change is inert, and on one edge harmful, without §2.1.**

---

## 2. Corrections to ADR-29 as written

These are the design content of this RFC. Each was found reading the tree at `09539ba`, and each changes what an implementer must type.

### 2.1 The teardown attempt budget: (d) is inert on one edge and unbounded on another

`_run_teardown_turns` returns `False` ("do not defer") when both teardown turns succeed, and on the way out it pops the counter (`design_loop.py:2661–2665`):

```python
self._log_teardown_leaks(session_key)
if not turn_failed:
    _delivery_attempts.pop(delivery_id, None)      # ← :2664
    return False
```

Control then falls through to the caller's open-artifact check at `:2257`, which under (d) calls `_teardown_retry_or_give_up` → `_retry_or_give_up` → `n = _delivery_attempts.get(delivery_id, 0) + 1` = **1**, every time. The delivery defers, the dispatcher re-drives it, the turns succeed again, the counter is popped again, and the attempt number never reaches `_DELIVERY_MAX_ATTEMPTS`. The result is an unbounded loop that also re-dispatches two teardown turns per dispatcher tick against a closed issue — strictly worse than the `done`-and-stop it replaces, and it makes acceptance (6)'s "escalates only after `_DELIVERY_MAX_ATTEMPTS`" unreachable.

**And the mirror defect on the other edge: the caller re-arms a budget that is already spent.** (Developer's catch.) `_run_teardown_turns` returns `False` for *both* "turns succeeded" and "turns exhausted" — the latter via `return self._teardown_retry_or_give_up(...)` at `:2666`, which has already popped the counter at `:2695` and escalated to `@huozhe`. The caller cannot tell the two apart, so under (d) as ADR-29 words it the exhausted case falls into the same `:2257` block and starts again at `n = 1`: another `_DELIVERY_MAX_ATTEMPTS` of two teardown turns per dispatcher tick against a closed issue, ending in a **second** escalation for one delivery. Same defect family as the reset, smaller bound, and it is not fixed by deleting the pop.

**Binding — one budget per delivery, cleared on exactly two events inside this handler.**

1. **Delete the pop at `:2664`** (success path) and the one in the drained short circuit at `:2585`. Neither means the delivery finished: after both, control continues into `_archive_and_close`, which can fail and defer on its own budget. `:2585`'s pop is in fact the same bug on the archive edge today — drained ledger, archive fails, defer at `n=1`, redeliver, short circuit pops, archive fails, `n=1` again — an unbounded loop that predates this RFC and costs one deleted line to close.
2. **Clear it where the handler completes** — immediately before the success `set_delivery_status(delivery_id, "done")` at **`:2284`**. Not `:2281`: that is the archive-failure *defer* `return`, and clearing there would pop the counter on deferral, which is §2.1's own bug on a third edge. (Developer's catch; the first draft cited `:2281`.) Exhaustion already clears at `:2695`.
3. **Move the leak deferral inside `_run_teardown_turns`** and give it a three-valued outcome, per §1 (d), so `CLEAN`, `DEFER` and `EXHAUSTED` are distinguishable. The leak check then shares the turn budget instead of opening a second one, and it is unreachable after exhaustion by construction rather than by an `if`.

Acceptance (6) is therefore asserted across passes, not one: pass 1 defers at attempt 1, pass `_DELIVERY_MAX_ATTEMPTS` exhausts, escalates **once**, and marks the delivery `done`. Asserting only "not `done` after a single pass" passes against every version above, including both broken ones.

### 2.2 `turn_id` is not available where the WARNING has to be logged

ADR-29 (a) specifies logging each discarded frame with the `turn_id`. `CliSession.turn()` (`:493`) takes `(prompt, deadline_s, progress)` — no turn id, and neither adapter method has one. `exec_turn_as_role` computes `turn_id` at `turn.py:503`, seven lines before it calls `_live_result` at `:510`, so it is available and simply never passed down. (`turn.py:528` is `result["turn_id"] = turn_id`, *after* the call — cited in the first draft, and an implementer reading it would think the computation has to move. Developer's catch.)

**Binding:** thread it explicitly.

- `LiveCliSession.turn(prompt, *, deadline_s=900, progress=None, turn_id: str | None = None)` — keyword-only, defaulting to `None`, so every existing caller and test call site keeps working;
- `_claude_turn_unlocked` / `_grok_turn_unlocked` take `turn_id` and pass it to the drain;
- `turn.py::_live_result` grows a `turn_id: str | None` parameter and `exec_turn_as_role` passes the id it already has.

Rejected: stashing the current turn id on the session object. It is per-turn state on an object whose whole failure mode is per-turn state leaking across turns, and a stale attribute would mislabel exactly the log line that exists to name the culprit. The log renders a missing id as `turn_id=unknown` rather than omitting the field, so a grep for `turn_id=` never silently misses a frame.

### 2.3 (a) and (b) invalidate the existing CLI-session fixtures — and that is how (1) fails first

The suite's fake CLI seeds frames onto `_stdout_q` **before** calling `turn()`. Every one of those seeded frames is, by (a)'s definition, stale: the drain discards them, the turn then blocks for a response that will never come, and the test fails on `deadline_s` instead of on its assertion.

The exact surface, because "18" in the first draft was a grep hit count and not a site count (Developer's catch): **12 direct `_stdout_q` assignments** across `test_cli_session.py`, `test_public_actions.py` and `test_turn_privilege.py` — one of them inside the `_wire_stdout_queue` helper (`test_cli_session.py:33–43`), which 5 tests call, and 11 inline. **Both adapters are affected, not just claude**: `_wire_stdout_queue`'s callers at `:68, 743, 778, 909, 951`, the inline claude copies at `test_public_actions.py:222` and `:268`, and the inline **grok** error-path tests at `test_cli_session.py:679, 708, 812, 850`, which pre-seed the `session/prompt` response that (a)'s grok-side drain now discards. The write-triggered fixtures that already exist (`test_cli_session.py:232, 319, 638`, which drive frames from a `stdin.write` side effect and a feeder thread) are the shape everything else has to move to, and they are unaffected. Each remaining site is an audit, not an assumed rewrite.

**Binding:** delivery moves to the write. Replace `_wire_stdout_queue` with a scripted responder whose `proc.stdin.write` side effect enqueues that turn's frames:

**The script is per write, not one frame list.** (Developer's catch: a single `frames` list re-enqueued on every write makes acceptance (1) inexpressible — turn 2's own write would re-put turn 1's `result`, so turn 2 could pass by reading a fresh `r1` even with no drain at all.) Each write consumes the next batch:

```python
def _wire_scripted_cli(sess, script: list[list[str]], *, preseed: Iterable[str] = ()) -> None:
    """Frames arrive because the prompt was written — the same causality the real
    CLI has. script[i] is what the vendor emits in response to write i; a vendor
    that emits two results for one prompt is script[0] with two result frames."""
    q: queue.Queue[str | None] = queue.Queue()
    for line in preseed:                      # only for drain-helper tests, never for turns
        q.put(line)
    batches = iter(script)
    sess._stdout_q = q
    sess.proc = MagicMock(); sess.proc.poll.return_value = None; sess.proc.pid = 4242
    sess.proc.stdin = MagicMock()
    sess.proc.stdin.write.side_effect = lambda _line: [
        q.put(f) for f in next(batches, ())
    ]
```

Acceptance (1) is then literally ADR-29's mechanism rather than a simulation of its symptom: `script = [[assistant_1, result_1, result_1_late], [result_2]]` — one prompt, two `result` frames, exactly what the session-limit refusal did. Turn 1 takes `result_1` and leaves `result_1_late` queued; on today's code turn 2 returns it in ~0 s, which is #162 in a unit test; with (a) turn 2 discards it at WARNING and blocks for `result_2`. `preseed` stays for direct tests of `_drain_stdout_unlocked` (stale frames plus the EOF sentinel), where no turn runs.

This is not fixture churn for its own sake: the pre-seeded queue *is* the defect, expressed as a test convention, and acceptance (1) cannot exist while frames arriving before the write are the normal case.

Second consequence: **four** claude tests assert on `is_error` results — `test_cli_session.py:743` (session limit), `:778` (generic `is_error`), `:909` (a loop over several envelopes) and `:951`, which *is* the `#94 B1` quota-with-text case the first draft double-counted (Developer's catch). Under (b) each now calls `_spawn_unlocked` — a real `Popen` of `claude` inside the unit suite. Each must either patch `_spawn_unlocked` or, for the ones about respawn semantics, assert on it. A test that neither patches nor asserts is a latent live spawn on the CI host.

Third: `_SPAWN_LIVENESS_S` is a module constant so the suite can patch it to ~0. Left at 0.75 s it is paid by every spawning test.

### 2.4 A child that dies after the poll window must not strand the role

The poll window is a bet on how fast a rejected `--resume` exits. If the child instead dies at, say, 2 s, the poll passes, the id is not cleared, the turn fails on `claude exited early` (`:581`), and the *next* turn spawns with the same rejected id and dies the same way — the role fails every turn until a human intervenes.

**Binding:** treat any observed early death of a resume-spawned child as evidence against the id, wherever it is observed. `_spawn_unlocked` **assigns** `self._spawned_with_resume` on every spawn — `True` iff *this* spawn's argv carried `--resume <id>`, `False` otherwise, including on the `-c` fallback retry; in `_claude_turn_unlocked`, at the `proc.poll() is not None` check (`:580–581`), clear `claude_session_id` before raising when that flag is set.

It is an assignment and not a set-once flag for the reason §2.2 rejects stashing the turn id: stale per-spawn state on this object is the failure mode the whole ADR is about. A flag only ever set `True` would make a later `-c` child's mid-turn death clear a `claude_session_id` that `:591–592` had just legitimately re-learned — throwing away the correlator on evidence that says nothing about it. (Developer's catch.) The next spawn falls back to `-c`, and `:591–592` re-learns a real id from the first frame that carries one. Cost: one turn, bounded, logged. Without it the fallback only works inside a 0.75 s window, which is a coin flip dressed as a mechanism.

### 2.5 Acceptance (7) needs a benign counter-example named in advance

ADR-29 (7) makes the live sign-off "the first live architect turn after deployment must show the drain discarding **zero** frames". A claude process may legitimately emit a startup `{"type":"system","subtype":"init",...}` frame before the first prompt is written, in which case the first drain of a freshly spawned process discards one frame and the sign-off criterion reads as failed when nothing is wrong.

**Binding:** sign-off is *zero discarded frames with `type == "result"`*, and the drain's log line renders `type` explicitly so the distinction is greppable. A discarded `result` frame is the defect, is the thing that shifts a turn, and is the thing whose recurrence must name its own origin; a discarded startup banner is noise, expected at most once per spawn. If any frame is discarded on the first turn, capture it in the issue before signing off — this is the ADR's durable half and it is worth being able to read it correctly the first time.

Note this is also why the drain does not learn `claude_session_id` from discarded frames: `:591–592` fires on any in-turn frame, so a discarded startup banner costs nothing, while a discarded foreign `result` must not be allowed to name our conversation.

---

## 3. Order

Not a preference — (a) before (b′) is a correctness constraint from ADR-29: `:591–592` assigns `claude_session_id` from *any* frame carrying `session_id`, so until the drain lands, the id itself can be learned off another turn's stream, and (b′) would then re-enter a conversation named by a foreign frame.

1. **§2.3 fixture rework** — mechanical, no production change, keeps the suite honest for everything after it.
2. **(a) drain + `turn_id` threading (§2.2)** — with acceptance (1) written to fail first against a two-`result` fixture.
3. **(b) `is_error` respawn** — depends on (a) only for a clean log.
4. **(b′) `--resume` / `-c` + post-spawn poll (+ §2.4)** — must follow (a).
5. **(c) process-unique ids** — independent; ships whenever.
6. **(d) teardown deferral + §2.1 (counter lifecycle *and* the three-valued outcome)** — independent of the runner; all of it lands in one commit, or (d) is a no-op on one edge and an unbounded retry on another, both of which look green.

Steps 2–4 are one runner deploy: they share a file and only the whole set satisfies the exit condition. Steps 5 and 6 are gateway-side and carry no image rebuild.

## 4. Test plan

Against ADR-29's acceptance (1)–(8). Traps ADR-29 already caught are restated only where the placement is not obvious.

| # | Test | Where | Notes |
|---|---|---|---|
| 1 | Two `result` frames for **one** prompt (`script[0]` has both): turn 1 takes the first; turn 2 must block for its own, not return the leftover | `test_cli_session.py` | **Must fail first** against today's code — assert on turn 2's summary and that it is not turn 1's. Then pass with (a). Both runs required, per ADR-29: a fixed-behaviour-only test passes today whenever turn 1 leaves nothing behind. The surplus frame must ride on turn 1's write, per §2.3 — a fixture that re-emits its whole script on every write lets turn 2 pass with no drain at all. |
| 2 | `is_error` respawns | `test_cli_session.py` | Assert **`sess.proc.pid` differs, and nothing else.** Not argv — `:591–592` sets `claude_session_id` from the error frame itself, so the live path after `is_error` is `--resume <id>`; an assertion pinning `-c` here contradicts (b′). |
| 3 | Re-entry names the conversation | `test_cli_session.py` | Assert argv for **both** branches: id set → `["--resume", id]`; id `None` → `-c`. The bug being prevented is a bare `--resume`, which no happy-path assertion catches. Plus: kill the child immediately after `Popen` and assert the poll notices, falls back to `-c`, clears the id, and logs at WARNING — without that, the fallback can silently never fire and an argv-only test still passes. Assert the reader is started **once, on the surviving child** (§1 b′ ordering): a fixture counting `_start_stdout_reader` calls catches an implementer who extracted too much into `_popen_unlocked`. |
| 3b | §2.4 late death | `test_cli_session.py` | Child dies mid-turn after a resume spawn → `claude_session_id` is cleared and the next spawn's argv is `-c`. |
| 4 | Grok is unchanged | `test_cli_session.py` | Leftover response with a **lower `mid`**, injected **after** `_acp_write`, during the wait. Seeded before the write it is removed by (a)'s drain, the test passes on the drain, `:700`'s id filter is never reached, and the test would keep passing if that filter were deleted. |
| 5 | Two consecutive `RunnerClient`s issue different `turn.dispatch` ids | `test_rpc_runner.py` | Today both are `2`. Name it for the transport class, **not** for #162. |
| 6 | Teardown with one open artifact does not mark the delivery `done` | `test_m5_teardown.py` | Assert across passes: pass 1 → `deferred` at attempt 1; pass `_DELIVERY_MAX_ATTEMPTS` → escalation + leaks logged. A single-pass assertion also passes against both §2.1 bugs. |
| 6b | §2.1 counter lifecycle | `test_m5_teardown.py` | Successful teardown turns with the ledger still open must **not** reset `_delivery_attempts` — attempts increment monotonically across passes. Regression guard for the inert edge. |
| 6c | §2.1 no second budget after exhaustion | `test_m5_teardown.py` | On the pass where the budget exhausts: the delivery ends **`done`**, exactly **one** escalation is posted, and **no further teardown turns are dispatched** (assert the dispatch count). Without this, the re-arm lands green — the delivery merely stays `deferred` and buys another `_DELIVERY_MAX_ATTEMPTS` of turns against a closed issue. |
| 6d | §2.1 archive edge | `test_m5_teardown.py` | Drained ledger + failing `_archive_and_close`, redelivered: attempts increment and exhaust. Guards the `:2585` pop, whose deletion closes a pre-existing unbounded loop. |
| 7 | Live | — | No unit test proves a vendor emits two `result`s for one prompt; (1) simulates it. Live criterion per §2.5: first architect turn after deploy discards **zero `result` frames**. Green tests are not sign-off. |
| 8 | In-image | — | `_claude_turn_unlocked` is unreachable from the gateway suite. Rebuild → `docker rm -f` the project container → restart → confirm the code is in the *running* image. `ensure_session` adopts a running container regardless of image, so a rebuild alone reads like success. Verify `-r, --resume [value]` on the real binary at 2.1.237 while inside (`--help` on a parent command has misdescribed a subcommand's flags before — #92). |

Regression surface to keep green: the whole of `test_cli_session.py`, `test_public_actions.py`, `test_turn_privilege.py` after the §2.3 rework — including the four grok error-path tests, which fail on the *grok* side of the drain if only the claude fixtures are moved — and `test_quota_exhausted.py` (the `#94 B1` gate is untouched: (b) fires before it, not instead of it).

## 5. Risk and rollback

| Risk | Mitigation |
|---|---|
| Drain discards a frame that was not stale | Impossible by construction *if* the drain is the last thing before the write. Reviewers should check that ordering specifically — it is the whole safety argument. |
| Respawn on `is_error` costs conversational continuity | `is_error` turns are already failed turns; ADR-29 rules the continuity worth nothing. `--resume <id>` preserves it anyway in the ordinary case. |
| `--resume` id rejected by the vendor | Poll → `-c` fallback (§1 b′), plus §2.4 for deaths outside the window. |
| Spawn latency +0.75 s | Once per CLI process lifetime, not per turn. Constant is patchable for tests. |
| (d) defers forever, or re-arms after exhaustion | §2.1: one budget per delivery, cleared only at completion (`:2284`) or exhaustion (`:2695`), and a three-valued `_run_teardown_turns` so `CLEAN` and `EXHAUSTED` stop being the same `False`. Acceptance (6b)/(6c) are the guards; both bugs pass a single-pass test. |
| Rollback | (a)–(b′) are one runner file plus a `turn.py` parameter — revert and rebuild the image. (c) and (d) are independent gateway commits, revertible on their own. |

## 6. Out of scope

Unchanged from ADR-29 and #169, restated so no reviewer has to re-derive the boundary:

- **#167** — GC's stale-ledger sweep does not cover `branch` (`gc.py:334`). Caused nothing here; needs `local_branch_gone` semantics `_sweep_ledger` does not have.
- **#168** — quota misclassification of a limit hit that follows real work. Real, separate; naming it inside this acceptance would let a green quota test read as evidence for the pipe fix.
- **`--session-id <uuid>`** — the gateway assigning the conversation id up front. Better long-term shape; moves ownership across the runner contract and touches `session.init`.
- **Unsticking #151** — data repair, `@huozhe`'s call.
- **Correlating claude frames by an id we choose** — the protocol affords none; (a) and (b) are what it does afford.
