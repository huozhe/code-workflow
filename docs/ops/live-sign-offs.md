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
| Immediately | `DEFAULT_IMAGE = 1.3.0` creates a container | The gateway's half of ADR-35's deploy has never run; nothing else exercises it |
| During | **#171**, **#156** | Worktree base and the silent-turn counter are turn-time facts |
| Only if it outlives a **5-minute** reconcile pass | **#116**, **#173** | The reconciler is a timer (`RECONCILE_INTERVAL_S`), not something a session triggers — a short session never shows them |
| At close | **#172**'s kill | `session.teardown` → `shutdown_all()` → `_kill_unlocked` (`server.py:417`) is the reliable half |

**Two cannot be forced and must not be counted on the plan:** #172's *deadline*
kill needs a genuinely wedged turn — the deadline is an error path a clean session
never enters — and **#168** needs a real quota refusal. Both are opportunistic.

## The ledger

| Issue | ADR | What must be observed | Not discharged by |
|---|---|---|---|
| **#116** | ADR-34 | The reconciler's probe reaching a runner **inside a real container over a published port**: `attached=1` with a real `rss_bytes` in the pass report | Any unit fixture. Everything run so far is a socket stub or an in-process server |
| **#172** | ADR-35 | A **real vendor CLI** (`claude`/`grok`) killed by the runner, with no descendant surviving | Every kill driven so far was against `sh` |
| **#171** | ADR-31 | Both role worktrees `behind=0` on the live clone | Watch the first `ensure_session` repair it — do not repair it by hand, or there is nothing to observe |
| **#156** | ADR-32 | A real turn that opens a Design PR leaves `silent_turns` at 0 | A unit fixture trips the same log line without exercising the counter's subject |
| **#173** | ADR-30 | A rework round logs `author-sent PR event, no turn`, and `silent_turns` never exceeds 1 | As above |
| **#168** | ADR-33 | A real quota refusal recorded `quota_exhausted`, the delivery re-picked after the hold, the session continuing **without pausing meanwhile** | Cannot be forced; opportunistic only |

**#147 must not be closed on a quiet log.** Its counters read clean only because
`agentctl` ran a manual `quarantine_deferred`. A handoff note once said otherwise
and was wrong.

## Running the exercise

1. Confirm nothing is live first — a session in flight makes every observation
   below ambiguous:
   ```bash
   cd agentd && AGENTD_ROOT=$HOME/.agentd uv run python -c "from agentd.db import Store; import pathlib; \
     s=Store(pathlib.Path.home()/'.agentd'/'state.db'); \
     print([(r['session_key'],r['state']) for r in s.list_sessions() if r['state']!='CLOSED'])"
   ```
2. **Labelling an issue `agentd` opens a live session** (#67). That is the trigger;
   there is no other. File anything you do not want run as unlabelled.
3. Let it run **past five minutes** if #116 and #173 matter to you, and to a real
   teardown if #172 does.
4. INFO goes to `~/.agentd/logs/agentd.log`. `gateway.err.log` is WARNING and
   above, so a pass line is not in the file the plist names as stderr.

## Why this list keeps being written down wrong

Every item here was at some point believed discharged on evidence that could not
reach the mechanism. The rule that catches it — CLAUDE.md's *a null or green
result is only as good as its fixture* — earned six more instances across #116 and
#172, and the shape is always the same: the fixture could not **reach** the case it
was named for, and the result looked like evidence rather than absence.

Two forms are worth naming because they do not look like fixtures at all:

- **A reproduction that fails.** "Cannot reproduce" is a null result. Prove the
  fixture holds the property first — a leader that also traps `SIGTERM`, or a
  polled child that has already been reaped, removes the very condition under test.
- **A contaminated working tree.** A `src/` staged from the branch under review
  silently executes the fix during a "pre-fix" reproduction. *Verify what your tree
  contains before believing a reproduction,* not only what your fixture contains —
  a staged checkout looks like a checkout, so none of the usual suspicion fires.
