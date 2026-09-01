# RFC 248 — implementation binding for ADR-41: `agentctl sessions --live`, and the three fields the sweep's query does not carry

| | |
|---|---|
| **Status** | Proposed (Design PR for #248) |
| **Issue** | [#248](https://github.com/huozhe/code-workflow/issues/248) |
| **Decision of record** | ADR-41 — [`../ADR/adr-41-agentctl-sessions-live.md`](../ADR/adr-41-agentctl-sessions-live.md), indexed at [`../unified_design_spec.md`](../unified_design_spec.md) §16, spec 1.41.0 |
| **Prior art** | ADR-13 (`agentctl version`), ADR-14 (`agentctl review-stats`) — same class of change, same reason for choosing it. |
| **Baseline** | `main` @ `489be5f` (spec 1.40.3). Every line number and every measured claim below is against that commit. |
| **Touches** | **This PR:** `docs/design/ADR/adr-41-agentctl-sessions-live.md` · `docs/design/ADR/README.md` · `docs/design/unified_design_spec.md` (§16 row + 1.41.0 + header) · this RFC. **Implementation PR:** `agentd/src/agentctl/__main__.py` · `agentd/tests/test_agentctl_sessions_live.py` (new) · `docs/ops/live-sign-offs.md` (step 1). |
| **Produced by** | Architect turn `t-825a95427072`, session #248 (§5.5.2 stamp) |

## 0. What this RFC is

The issue is right about the defect, right about the remedy, and right to bind the definition of "live" to
`list_nonterminal_sessions`. This RFC does not re-open any of that.

What it adds is the part that only appears once you open the two methods and run them. Four things came
back different from what the issue's sketch assumes, and one of them changes a runbook edit from a
one-line swap into a decision:

1. **`list_nonterminal_sessions` cannot produce the suggested line on its own.** Its `SELECT` names fifteen
   columns and `turn_count`, `silent_turns` and `updated_at` are not among them — three of the six fields
   the example line prints. §1.1.
2. **It has no `ORDER BY`.** A human-facing list has to impose one, and `updated_at` is whole seconds, so
   the obvious key alone ties across every row of a fixture built in one loop. §1.2.
3. **The em dash in the suggested line can make the command exit 1**, which contradicts the issue's own
   acceptance that exit status is always 0. §1.3.
4. **A `TEARDOWN` session dispatches agent turns.** The runbook snippet `--live` is meant to replace shows
   `TEARDOWN`; `--live` by definition does not. Swapping one for the other silently narrows a check whose
   whole purpose is to catch a session in flight. §3.

None of the four argues against the feature. Three are implementation bindings and the fourth is a
sentence the runbook has to carry.

## 1. What was measured

Reproduce with the system interpreter from `agentd/`; `db.py` and `fsm.py` need no third-party imports.
The fixture holds one row of each class the acceptance asks for — `INTAKE`, `IMPLEMENTING`,
`PAUSED_HUMAN`, plus `TEARDOWN` and `CLOSED`:

```python
import pathlib, tempfile, sys
sys.path.insert(0, "src")
from agentd.db import Store
from agentd import fsm

s = Store(pathlib.Path(tempfile.mkdtemp()) / "state.db")
now = 1_756_700_000
rows = [
    ("huozhe/code-workflow#900", "INTAKE",       None, None,  0, 0),
    ("huozhe/code-workflow#248", "IMPLEMENTING",  249, None,  7, 0),
    ("huozhe/code-workflow#210", "PAUSED_HUMAN",  211,  212, 12, 3),
    ("huozhe/code-workflow#159", "TEARDOWN",      160,  161, 40, 0),
    ("huozhe/code-workflow#57",  "CLOSED",         58,   59, 99, 0),
]
for key, state, dpr, fpr, tc, sil in rows:
    s.upsert_session(session_key=key, repo="huozhe/code-workflow",
                     issue_num=int(key.split("#")[1]), state=state,
                     architect="huozheclaude", developer="huozhegrok",
                     created_at=now, updated_at=now, design_pr=dpr, turn_count=tc)
    s.update_session_fields(key, feature_pr=fpr, silent_turns=sil)
```

Note `upsert_session` takes no `feature_pr` and no `silent_turns`; both are reachable only through
`update_session_fields`. The implementation PR's fixture builder needs the same two-step.

### 1.1 The membership call does not carry the presentation fields

```
--- list_nonterminal_sessions order (as returned):
    huozhe/code-workflow#900 INTAKE
    huozhe/code-workflow#248 IMPLEMENTING
    huozhe/code-workflow#210 PAUSED_HUMAN
--- columns returned: ['architect', 'closed_issue_escalated_at', 'created_at', 'design_pr',
    'design_pr_head', 'developer', 'feature_pr', 'feature_pr_head', 'issue_num', 'paused_reason',
    'project_key', 'repo', 'session_key', 'state', 'verified_at']
--- turn_count present? False silent_turns? False updated_at? False
--- get_session has turn_count/silent_turns/updated_at: 7 0 1788241803
```

The set is right — three of five, `TEARDOWN` and `CLOSED` gone — and `design_pr` / `feature_pr` are there.
`turns=` and `silent=` are not, and neither is the column any sane ordering uses.

**Binding.** Membership from `list_nonterminal_sessions()`; fields from `get_session(session_key)` per row.
This is the same two-step the bare `sessions` branch already runs (`__main__.py:135–145`: `list_sessions()`
then `get_session()` per row, then `pop("runner_token")` / `pop("token")`). `--live` differs from bare
`sessions` in exactly two respects — which rows, and how they are rendered — and in no third.

Do **not** widen `list_nonterminal_sessions`'s `SELECT` to close this. Its only other caller is
`reconciler._sweep_github` (`reconciler.py:546`), and ADR-41 declines to edit the sweep's query for a
read-only flag. The cost of the two-step is an N+1 over a bounded set on an on-demand command, and a
nanosecond-wide race if a session is deleted between the two calls; take the membership row as the
fallback (`store.get_session(key) or row`) so the race degrades to a row rendered with `turns=0
silent=0` rather than a traceback.

`get_session` also `LEFT JOIN`s `runners` and returns its `token` as `runner_token`. `runners.project_key`
is the PRIMARY KEY (`db.py:75`) so the join is 1:1 and cannot duplicate a row — but the token is in the
dict. **`--live` does not print it and must not become a path that can.** Render named fields only; never
iterate the dict.

### 1.2 No `ORDER BY`, and `updated_at` ties

The rows above came back in insertion order — `#900, #248, #210` — because `list_nonterminal_sessions`
has none. And every `updated_at` was identical:

```
--- all updated_at equal (tie risk): {'huozhe/code-workflow#900': 1788241803,
    'huozhe/code-workflow#248': 1788241803, 'huozhe/code-workflow#210': 1788241803}
```

`updated_at` is written as `int(time.time())` (`db.py:837`, `:880`), whole seconds. A fixture that inserts
its rows in one loop ties all of them, and so does any real DB in which two sessions were touched in the
same second.

**Binding.** Sort in the CLI: `updated_at DESC, session_key ASC`. `updated_at DESC` because bare
`sessions` already orders that way (`db.py:678`), so the two commands agree. `session_key ASC` because
without it the acceptance test asserts against an order SQLite is free to change, and a green result from
a non-deterministic fixture is the failure mode this repo keeps meeting. Sort on the *enriched* dict, not
the membership row — the membership row has no `updated_at` at all.

### 1.3 The em dash can make the command exit 1

The issue's example line ends `feature_pr=—`. That was tried:

```
$ PYTHONIOENCODING=ascii python3 -c "print('feature_pr=—')" ; echo "rc=$?"
UnicodeEncodeError: 'ascii' codec can't encode character '\u2014' in position 11: ordinal not in range(128)
rc=1

$ LC_ALL=C LANG=C python3 -c "import sys; print(sys.stdout.encoding); print('x=—')"
utf-8
x=—
```

A `C` locale is safe — PEP 540 gives it a UTF-8 stdout. The explicit `PYTHONIOENCODING` override is not,
and it exits **1**, against the issue's own "exit status is unchanged and always 0".

**Binding.** ASCII output. The absent-PR placeholder is `-`. Every other printed field is ASCII by
construction: `session_key` is `repo#issue`, `state` is drawn from `fsm.SESSION_STATES`, the rest are
integers. `paused_reason` is *not* printed, and §2 says why.

## 2. The command

### 2.1 Surface

```
agentctl sessions            # unchanged: full JSON, every session, CLOSED included
agentctl sessions --live     # non-terminal only, one line each
```

`--live` is `action="store_true"` on the existing `sessions` subparser (`__main__.py:32`, currently
`sub.add_parser("sessions", help="List sessions")` with no arguments). Default `False`, so the existing
branch is entered on exactly the same condition as today. Exit status 0 in both arms, including the
empty one.

### 2.2 Format

One line per session; two spaces between columns; `session_key` and `state` left-padded to the widest
value **in the printed set** — one rule for both, applied to the rows actually being printed:

```
huozhe/code-workflow#248  IMPLEMENTING           turns=7   silent=0  design_pr=249  feature_pr=-
huozhe/code-workflow#210  AWAITING_VERIFICATION  turns=12  silent=3  design_pr=211  feature_pr=212
huozhe/code-workflow#9    INTAKE                 turns=0   silent=0  design_pr=-    feature_pr=-
```

- `turns=` is `sessions.turn_count`, already on the enriched row. Not `Store.count_turns()` — that is a
  second query per session and counts teardown turns, which `--live` never lists (`db.py:1052`).
- `silent=` is `sessions.silent_turns`.
- `design_pr=` / `feature_pr=` print the integer, or `-` when NULL.
- **No header row**, populated or empty. The acceptance says an empty DB prints *nothing*; a header would
  either be printed above zero rows — the `None` the issue rules out, spelled differently — or the command
  would have two shapes depending on row count.
- **No `paused_reason`.** It is free text truncated at 500 chars (`session_loop.py:2180`), it is written
  from agent- and owner-authored text (`:2997`), and the escalation text this project actually generates
  contains an em dash (`loop_safety.py:129`) — which would undo §1.3 on the one field it applies to. One
  line per session is what makes the output greppable. Bare `sessions` still prints it in full.

Padding the `key=value` fields to a common width is optional and not asserted; padding the two leftmost
columns is the readable property and is asserted.

### 2.3 Empty case

Zero non-terminal sessions: nothing on stdout, nothing on stderr, exit 0. Not `[]`, not `None`, not a
count, not a header.

## 3. The runbook change, and the narrowing it must not hide

`docs/ops/live-sign-offs.md:150–156` is the only in-repo copy of the snippet. (`.claude/STATE.md` carries
it too and is gitignored at `.gitignore:6`; no PR can update that one.) Step 1 today:

> 1. Confirm nothing is live first — a session in flight makes every observation below ambiguous:
>    ```bash
>    cd agentd && AGENTD_ROOT=$HOME/.agentd uv run python -c "...if r['state']!='CLOSED'..."
>    ```

The snippet filters `state != 'CLOSED'` **only**. It therefore shows a `TEARDOWN` session; `--live`, by the
definition the issue binds, does not. That gap is not theoretical:
`_run_teardown_turns` (`session_loop.py:3167`) dispatches a full agent turn for **both** roles while the
session is in `TEARDOWN` (`:3255`), and the state sticks across drains when the artifact ledger is still
open or no runner is up (`_TEARDOWN_DEFER`, `:3265`, `:3296`). A session mid-teardown is running agent
turns against the same runner and the same role locks that step 1 exists to warn about.

Excluding `TEARDOWN` from `--live` is still correct — the owner has closed the issue and the session has
left the loop — so the fix is not to widen `--live`. It is to make step 1 say what its one command covers
and name the instrument for the rest. Replace the snippet with:

```bash
agentctl sessions --live          # sessions still in the loop
agentctl reconcile --dry-run      # open_turns: covers a TEARDOWN session still running teardown turns
```

and one sentence saying `--live` deliberately omits `TEARDOWN` and `CLOSED`, that `TEARDOWN` still
dispatches two agent turns, and that `open_turns` (`reconciler.py:463`) is state-agnostic.

The Python snippet goes. That is the issue's stated value and it is delivered. What must not happen is
step 1 quietly answering a narrower question than it did before while reading as an improvement.

## 4. Order

1. `--live` flag + rendering in `agentctl/__main__.py`.
2. `agentd/tests/test_agentctl_sessions_live.py` — the fixture of §1, then acceptance (1)–(6) below.
3. The bare-`sessions` characterization test — and demonstrate it fails with the bare path perturbed.
4. `docs/ops/live-sign-offs.md` step 1.

## 5. Acceptance

Mapped to the issue's four items, plus what measurement added.

1. **`--live` omits `CLOSED` and `TEARDOWN` and includes everything else**, against the five-row fixture
   of §1 holding at least one of each. Assert the exact printed set, not a count.
2. **The set comes from `list_nonterminal_sessions`, not a filter beside it.** Assert by substitution, not
   by reading: monkeypatch `Store.list_nonterminal_sessions` to return a single row that a hand-written
   `NOT IN ('CLOSED','TEARDOWN')` predicate would *reject* (state `CLOSED`), and assert it is printed. A
   test that only checks the output set passes either way and proves nothing about which method ran.
3. **Bare `agentctl sessions` is byte-identical.** There is no test for it today, so this is a new
   characterization test: pin the exact JSON for the fixture DB. Per CLAUDE.md, show the fixture reaches
   the case — perturb the bare branch (drop the `runner_token` pop, or change `indent`), confirm the test
   fails, revert.
4. **Empty DB prints nothing and exits 0.** Assert `out == ""` and `err == ""`, and that no `SystemExit`
   is raised. Assert the *absence* of a header, which is what distinguishes this from a passing test that
   merely found no rows.
5. **Exit 0 under `PYTHONIOENCODING=ascii`** with a fixture whose live rows have NULL `design_pr` /
   `feature_pr` — the §1.3 case, asserted rather than assumed. This is the test that would have caught the
   em dash.
6. **Order is deterministic under ties.** Build the fixture so every `updated_at` is equal (§1.2 shows this
   is the default, not a contrivance) and assert the full ordered output. Then bump one row's `updated_at`
   and assert it moves to the top.
7. **The two definitions of terminal agree.** `assert fsm.TERMINAL_STATES == {"CLOSED", "TEARDOWN"}` and
   assert `list_nonterminal_sessions` excludes exactly those states over a fixture holding one of each
   member of `fsm.SESSION_STATES`. Cheap, no behaviour change, catches the divergence ADR-41 leaves open.

## 6. Out of scope, and residuals

**Out of scope, by the issue:** no change to `list_nonterminal_sessions` or to the sweep; no change to bare
`sessions` output; no `--json` or `--format`; no filtering by state, repo or role; no `agentd`
(gateway-process) equivalent.

**Residual (ADR-41).** `fsm.TERMINAL_STATES` and the SQL literal at `db.py:591` are two spellings of the
same set. `fsm` imports nothing from the package, so unifying them is available — it means parameterising
the sweep's `IN` clause — but that edits the sweep's query for tidiness, in a PR whose subject is a
read-only flag. Acceptance (7) guards the divergence instead. Left to whoever next has reason to touch
that query.

**Residual (not this issue).** `.claude/STATE.md` keeps its copy of the snippet and will drift. That is
#183's problem — the file is gitignored by design — and is noted here only so the drift is expected
rather than discovered.
