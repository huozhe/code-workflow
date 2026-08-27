# RFC 159 — implementation binding for ADR-39: renaming `design_loop.py`, and the four references that must *not* be renamed

| | |
|---|---|
| **Status** | Proposed (Design PR for #159) |
| **Issue** | [#159](https://github.com/huozhe/code-workflow/issues/159) |
| **Decision of record** | ADR-39 — [`../unified_design_spec.md`](../unified_design_spec.md) §16 (new), spec 1.39.0 |
| **Baseline** | `main` @ `90ccc65` (spec 1.38.0). Every count and line number below is measured against that commit. |
| **Touches** | `agentd/src/agentd/{design_loop,dispatcher,server,reconciler,fsm}.py` · 37 files under `agentd/tests/` · `docs/design/unified_design_spec.md` (2 lines + 1 ADR) · `docs/ops/live-sign-offs.md` (1 note) |
| **Produced by** | Architect turn `t-889ec5ce7199`, session #159 (§5.5.2 stamp) |

## 0. What this RFC is

The issue is right about the defect and right about the remedy, and this RFC does not re-open either. A
rename does not need a long design document, and writing one would be its own altitude error.

What it does need is the part a rename always gets wrong: **which references are code, which are prose,
and which are records that a sweep must not touch.** There are 374 occurrences of the two names across
`src` and `tests` and 72 more in the spec, and they do not all mean the same thing. A `sed` over all of
them passes `pytest`, `lint` and `types` and silently falsifies four dated records.

Everything below was measured at `90ccc65`, not read from the issue. Three things came back different:

- **every count in the issue is stale**, because it was taken at `b414b0c` on 2026-08-19 (§1). None of
  them changes the decision, but two of them change the *work*;
- **the issue's evidence for the logger being safe to rename is wrong, and its conclusion is right
  anyway** — for a reason that makes the rename *less* safe than the issue thought, not more (§3);
- **the issue's evidence for the logger being risky to rename is wrong too**, and this one does change
  the decision: not one documented deploy-verification grep in this repository greps a logger name (§2).

## 1. The issue's numbers, re-measured at `90ccc65`

| Claim in #159 (`b414b0c`, 2026-08-19) | At `90ccc65` (2026-08-27) | Matters? |
|---|---|---|
| 3287 lines | **3748** | No. Still the largest module by 2.3×. |
| 232 references across `src` and `tests` | **374 occurrences on 315 lines** | No. |
| 3 real importers | **3** — `dispatcher.py:15` (`TYPE_CHECKING`), `server.py:85` (function-local), `reconciler.py:17` (module-level) | No. Verified, not carried over. |
| `fsm.py:54` is a comment only | **True.** `fsm.py:54`, one comment, no import | No. |
| 28 test files | **37** | **Yes** — nine more files than the issue budgets for. |
| 9 references in the spec | **72 occurrences on 55 lines** | **Yes**, and it inverts the task — see §4. |
| `insert_turn` at `:1014` is the only turn-creation site | **True, and sharper than stated.** `Store.insert_turn` is defined at `db.py:1086`; its only non-test caller in the tree is `design_loop.py:1352`, inside `_dispatch_turn` | No — this is the issue's load-bearing claim and it holds |

The other line numbers in the issue body have all moved (`class DesignLoop` is `:345`, not `:254`;
`process_resuming_turns` `:382`; `process_deferred_batch` `:419`; `_dispatch_turn` `:1259`;
`_run_teardown_turns` `:2993`; `_escalate` `:3247`). Cite this table, not the issue body.

**Sequencing precondition is met.** #151 and #158 are both `CLOSED`. `gh pr list --state open` returns
**zero** PRs, and the only non-`main` remote branch — `docs/ops-required-checks-live` — touches
`docs/ops/m4-a-branch-protection.md` and nothing else. The session brief calls this rename a conflict
magnet, and it is; right now the magnet has nothing to attract. That window closes the moment any branch
opens against `design_loop.py`, which is the argument for landing it in this session rather than a later
one.

## 2. The logger question is settled by the runbook, and the runbook says the opposite of the issue

The issue frames the logger string as an operational interface — *"it is what you grep when reading the
log after a deploy"* — and weighs option (2) (keep `agentd.design_loop` after the module moves) against
that. The premise is checkable, and it does not hold.

`configure_logging` (`__main__.py:36`) formats with `%(asctime)s %(levelname)s %(name)s %(message)s`, so
the logger name **is** in every line. But every deploy-verification grep this repository actually
documents keys on the **message**, never on `%(name)s`:

| `docs/ops/live-sign-offs.md` | What it greps |
|---|---|
| `:210` | `"unauthorized Feature PR merge"` |
| `:211` | `"merge_auth superseded"` |
| `:212` | `route defer id=[a-f0-9-]*` |
| `:224` | `"reconcile pass"` |
| `:239` | `"author-sent PR event, no turn"` |
| `:259`, `:278` | `grep -ic kill` |

Five of those six messages are emitted by `design_loop.py`. **A logger rename changes none of them.**
`docs/ops/m0-bootstrap.md` greps `dev.agentd` out of `launchctl`, which is a plist label. There is no
third ops file.

The one place the module's *name* is load-bearing to an operator is not the logger at all:

```
design_loop.py:426    log.exception("design_loop failed delivery=%s", row["delivery_id"])
```

That is a **message** string containing the module name, and it is in the class of thing the runbook
greps. It has to be renamed with the module, and it is the only occurrence of its kind — `grep -n
'"[^"]*design_loop[^"]*"' src/agentd/design_loop.py` returns exactly two lines, `:174` (the logger) and
`:426` (this).

**The retention bound, which the issue's 6869-line count omits.** `agentd.log` is a
`RotatingFileHandler` with `LOG_MAX_BYTES = 1 MiB` and `LOG_BACKUP_COUNT = 5` (`__main__.py:21–22`).
Six MiB is the entire retained window. Deriving from the issue's own 6869 lines ≈ 1 MiB, that is on the
order of 40k lines total — after which **no** line carrying the old logger name exists on disk. Option
(1)'s cost is a discontinuity that expires by itself after one rotation cycle. Option (2)'s cost is
permanent and structural: there are 16 other named `getLogger`
calls in the tree, and the **14** that name a module all use `agentd.<module>` matching the filename
exactly (`db`, `dispatcher`, `docker_wait`, `gc`, `github_fetch`, `github_write`, `gitops`, `governor`,
`intake`, `reconciler`, `rpc_client`, `server`, `supervisor`, `verify`; the other two, `agentd` and
`agentctl`, are package-level entry points). `session_loop.py` would be the only module in the tree
whose logger disagrees with its filename —
in a PR whose entire purpose is that a name disagreeing with its contents costs real time.

The issue leaned to (1) on taste. It is (1) on measurement. **Bound, stated rather than discovered
later:** anyone reading a log archive older than the cutover greps the old name. §6 (f) makes that
greppable instead of mysterious.

## 3. Three tests name the logger, and none of them can fail — verified, not reasoned

The issue asserts **0** matches for the logger string under `tests/`. There are **three**:

```
tests/test_adr30_author_pr_events.py:312    caplog.at_level(logging.WARNING, logger="agentd.design_loop")
tests/test_artifact_kind_agreement.py:215   caplog.at_level(logging.WARNING, logger="agentd.design_loop")
tests/test_m5_teardown.py:1384              caplog.at_level(logging.WARNING, logger="agentd.design_loop")
```

The obvious inference — "so the suite does protect the logger name after all" — is also wrong, and the
direction it is wrong in is the one that matters. `caplog`'s handler is attached to the **root** logger;
`at_level(level, logger=NAME)` sets the level on the named logger and on the capture handler, and binds
capture to neither. Records emitted under a *different* name still propagate to root and are still
captured.

Modelled directly against `logging`, since no interpreter in this session's container has `pytest`
installed (`python3 -c "import pytest"` → `ModuleNotFoundError`; no `uv`, no `.venv`):

```
control  (names match, WARNING):  ['did not kill cleanly']
renamed  (stale string, WARNING): ['did not kill cleanly']
renamed  (stale string, INFO)   : ['did not kill cleanly']
renamed  (stale string, DEBUG)  : ['did not kill cleanly']
```

All three tests would keep passing with the stale string in place, at every level. So the issue's
conclusion — nothing fails — survives, but the situation is worse than "no coverage": the rename would
leave three test sites *naming a logger that does not exist*, green, indefinitely, and the next person to
add a fourth would copy one of them.

**This is a model, not the suite.** It reproduces `caplog`'s documented mechanism and it is the reason
for acceptance item (5), which demands the real thing: mutate one of the three strings on a tree where
the rename has landed and show the test still passes. If it fails instead, this section is wrong and (5)
is where that becomes visible. Do not take this block as the evidence.

**Two other string classes, and they behave oppositely.** `tests/test_escalation.py:59,64` pass
`"agentd.design_loop.get_password"` to `monkeypatch.setattr`, which resolves the dotted path and raises
on a missing module — loud. `tests/conftest.py:12` does `from agentd import design_loop` — loud, and it
breaks *collection of the whole suite*, so it fails first and loudest of anything here. The rule is not
"strings are silent"; it is that **exactly one API in this tree takes a logger name as an unresolved
string**, and all three of its call sites are in the blast radius.

## 4. The 72 spec references are not 72 edits

The issue's checklist says *"Update the 9 spec references."* There are 72, on 55 lines, and mapping
each to its enclosing heading inverts the task rather than multiplying it eightfold:

| Where | Lines | Occurrences | Kind |
|---|---|---|---|
| **Revision history** (rows 1.19.0 → 1.36.0) | 11 | 19 | Dated record |
| **ADR-14, 23, 26, 27, 28, 29, 30, 31, 32, 34, 36, 37, 38** (§16 bodies) | 42 | 51 | Dated record |
| **§5.5.2** `:447` | 1 | 1 | Current-tense normative |
| **§12.3** `:1015` | 1 | 1 | Current-tense normative |

Forty-nine of the 72 are line-numbered citations of the form `design_loop.py:NNN`. Those line numbers are
already wrong — the file has grown 461 lines since most of them were written — and they are wrong in the
way a dated citation is *supposed* to be: each ADR states its own baseline, and the citation is true
against that baseline. Rewriting the path while leaving the line number is the worst of the three
options, because it produces a citation that is false against **both** commits and looks maintained.

**Binding: rewrite two lines, freeze the other fifty-three, and add one pointer.**

- `:447` (§5.5.2) quotes the comment at `design_loop.py:1035` as evidence about `turns.public_actions`.
  Present tense, load-bearing, and a reader is meant to go open it — **and it is already wrong.** At
  `90ccc65` that comment (`public_actions are claims (tool_use) — diagnostic, not a reset.`) is at
  **`:1119`**; `:1035` is a `RouteAction.DROP` condition. §5.5.2 was written against spec 1.36.0 and
  the citation drifted 84 lines. Rewrite the path **and** the number, to `session_loop.py:1119`. A pure
  rename shifts no lines, so that number is stable across this PR — the drift is pre-existing, not
  introduced here.
- `:1015` (§12.3) names *"a test suite constructing `DesignLoop` with a bare `Config()` (#121)"*. Present
  tense in a section about what GC may touch. Rewrite the identifier. It names no line.
- Everything under **Revision history** and everything inside a **§16 ADR body** is left exactly as
  written. An ADR is a record of a decision at a commit, not documentation of the current tree, and
  #159's own out-of-scope list — *"Any behaviour change whatsoever"* — extends naturally to not
  retro-editing the record of past ones.
- The freeze is made legible by **one line in §16**, immediately above ADR-39's body, and this is the
  "comment explaining why" that the issue correctly charges against option (2). Here it explains a
  genuine, permanent, historical fact rather than an ongoing inconsistency, which is the difference:

  > `design_loop.py` was renamed to `session_loop.py` and `DesignLoop` to `SessionLoop` in ADR-39 (#159,
  > spec 1.39.0). ADRs and revision-history rows written before 1.39.0 cite the old names against their
  > own baselines and are left as written; their line numbers were already relative to those baselines.

The same freeze applies to the two RFCs already in `docs/design/rfcs/`
(`169-adr-29-…` cites `design_loop.py:2257–2260` and `:2661–2665`; `57-adr-36-…` cites `:1035`, `:1546–1547`,
`:632`), and to `docs/ops/live-sign-offs.md:158`, which quotes a **captured log line** from session #57 on
2026-08-24:

> `design_loop route drop after fsm … reason=self-echo kind=issue_opened state=PLANNING`

That is a transcript of something that was observed. Editing it would assert that a log line read
`session_loop` on a day when it did not. It is the one reference in the tree where a rename is not a
correction but a falsification, and it is exactly the reference a `sed -i` sweep would hit without
comment.

**The four frozen records, named once so acceptance (6) has a subject:**

1. `docs/design/unified_design_spec.md` — the eleven **Revision history** rows (1.19.0 → 1.36.0);
2. `docs/design/unified_design_spec.md` — the 51 occurrences on 42 lines inside **§16 ADR bodies**
   (ADR-14, 23, 26, 27, 28, 29, 30, 31, 32, 34, 36, 37, 38);
3. `docs/design/rfcs/169-adr-29-held-pipe-turn-boundary.md` and
   `docs/design/rfcs/57-adr-36-identity-credential-protocol.md`, each stating its own baseline;
4. `docs/ops/live-sign-offs.md:158` — the captured log line above.

## 5. What must *not* be renamed, because `design` is a real word here

`design_loop.py` contains **17** distinct identifiers spelling `design` that are about the **Design PR**
— a real first-class concept that survives this change untouched. A regex on `design` rather than on
`design_loop` / `DesignLoop` destroys them:

`sessions.design_pr` (schema column) · `_is_design_pr` (`:3581`) · `is_design_head_ref` (`gitops.py:96`) ·
`design_approved_unverified` / `design_approval` / `design_revised` / `design_merged` (FSM kinds) ·
`DESIGN_STATES`, `DESIGN_REVIEW`, `DESIGN_REWORK` (`fsm.py`) · `get_session_by_design_pr` (`db.py`) ·
the log messages at `:659`, `:669`, `:780`, `:3604`, `:3612`, `:3619`.

**Binding: the sweep matches `design_loop` and `DesignLoop` only. Never the bare word.** The one
exception is the two strings in §2 — `:174` and `:426` — which contain `design_loop` and are therefore
already covered by that rule.

The three test *filenames* split on the same line: `tests/test_design_loop.py` and
`tests/test_stall_design_loop.py` are named for the module and are renamed with it;
`tests/test_m3d_design_exit.py` is named for the **design-half exit criteria** (M3-D) and is not.

## 6. Decisions

**(a) `design_loop.py` → `session_loop.py`.** As the issue proposes. `orchestrator.py` and `lifecycle.py`
were considered and rejected — neither says *loop*, and the exit condition is a filename that answers
"where is the code review cycle handled?" from a directory listing. `loop.py` is rejected because
`loop_safety.py` already exists and would read as its partner, which it is not.

**(b) `DesignLoop` → `SessionLoop`.** Rename it. The issue's own framing — *"renaming the module and
keeping the class half-solves the problem"* — is correct and is the whole argument: **199** of the 315
reference lines carry `DesignLoop` (163 carry `design_loop`; they overlap), so leaving it means `grep -ri design` still lands a reader on the
lifecycle owner, which is the defect.

**Named collision, considered and accepted.** `SessionSupervisor` (`supervisor.py`) already exists, and
`SessionLoop` sits one word away from it. They are genuinely different — the supervisor owns containers
and runners, the loop owns the FSM and turns — and the mitigation is one clause in each module
docstring, not a worse name. The reader at risk is one who already knows a `Session*` class exists;
the reader the exit condition is written for has not opened either file yet, and for them `session_loop`
is unambiguous where `supervisor` is not.

**(c) Logger → `agentd.session_loop`.** Option (1) in the issue, on §2's measurement rather than on
preference. The message at `:426` renames with it.

**(d) The module docstring is rewritten, and it is not cosmetic.** Today:

```python
"""Design-loop orchestration — deferred deliveries → sessions → turns (M3)."""
```

`(M3)` was true when written and has been false since M4-1 put the code half in the same module. A
reader who opens the file rather than listing the directory gets the exit condition discharged here or
not at all. Replace with a line that names all of it — design half, code half, teardown, escalation,
delivery drain — and states that `_dispatch_turn` holds the tree's only production call to
`Store.insert_turn`, since that is the fact §1 shows is the actual answer to "did this change cover
every turn dispatch?"

**(e) The three `caplog` strings and the two `monkeypatch` strings are updated in the same commit.** Not
a follow-up. §3 shows CI cannot see three of the five.

**(f) One note in `docs/ops/live-sign-offs.md`, in item 4 of *Running the exercise*** — the paragraph
that already tells an operator where INFO and WARNING land. One sentence: from `<merge commit>`,
2026-08-27, the lifecycle module logs as `agentd.session_loop`; archives older than that carry
`agentd.design_loop`; the documented greps in this file key on message text and are unaffected.

**The issue names `STATE.md` as the place for this, and `STATE.md` is not in this repository.** No file
by that name exists anywhere reachable from the worktree, and PR #224's body locates it at
`.claude/STATE.md`, host-side. This RFC therefore binds the note to a file that exists and that is
already where deploy verification is read. **Open for the owner:** if `.claude/STATE.md` carries a
deploy-verification rule that names a logger, mirror the sentence there — an agent in a session container
cannot see that file to check, and this RFC does not claim it has.

**(g) Spec 1.39.0, ADR-39, plus the two rewrites and the pointer in §4.** ADR-39's body is short by
construction and should stay short: the decision is (a)–(f), the alternatives are the issue's own two
logger options, and the rejected-alternative worth recording is the blanket sweep — because it is what a
careful person does by default and it is green when it is wrong.

## 7. Acceptance

Ordered so that a failure at any step names the thing it falsifies. (5) and (6) are the two a green suite
does not cover, and they are the reason this is not a one-line PR description.

1. **`grep -rn 'design_loop\|DesignLoop' agentd/src agentd/tests` returns zero.** The blunt one. Run it
   as the last step, not the first.
2. **`pytest`, `lint`, `types` green**, and `git diff --stat` shows no `.py` hunk that is not a rename of
   one of the two names. A semantic diff of `session_loop.py` against `design_loop.py` is the docstring,
   the logger, and `:426`.
3. **`grep -rn '\bdesign_pr\b\|_is_design_pr\|is_design_head_ref\|DESIGN_STATES' agentd/src` returns the
   same set of lines as at `90ccc65`.** §5's guard against a too-wide regex. This is the check that
   catches the sweep that passed CI because nothing tests a column name's spelling in prose.
4. **`tests/test_m3d_design_exit.py` still exists under that name**, and `test_design_loop.py` /
   `test_stall_design_loop.py` do not.
5. **The `caplog` claim is shown on the real suite, not on §3's model.** On the renamed tree, revert one
   of the three strings to `"agentd.design_loop"` and run that single test: it must **pass**. That
   demonstrates the suite cannot see the logger name, which is why (e) was a binding rather than a
   suggestion. If it fails, §3 is wrong — say so in the review and keep the strings correct anyway.
6. **The four frozen records are byte-identical.** `git diff origin/main -- docs/design/rfcs/
   docs/ops/live-sign-offs.md` shows changes only in this RFC and only in item 4 of *Running the
   exercise* — and in particular **`live-sign-offs.md:158`'s quoted log line is untouched**. Then, in the
   spec: `git diff` touches exactly two lines outside §16's new ADR — `:447` and `:1015` — and the
   Revision history's eleven old rows are unchanged while a 1.39.0 row is added. Fifty-three of the 55
   `design_loop`-bearing spec lines survive the PR untouched; count them.
7. **`:447` now reads `session_loop.py:1119`, not `session_loop.py:1035`.** Open the file at that line
   and confirm the `public_actions are claims (tool_use)` comment is on it. This is the one place a
   blind path-only sweep produces a citation that is false against *both* commits (§4), and the tree
   already contains the drift that makes it so — so this check has a live subject, not a hypothetical.
8. **The rebase commit is named in the Feature PR body**, per the session brief.

## 8. Out of scope, and residuals

- **Splitting the module.** #159's own exclusion, unchanged. 3748 lines is worse than the 3287 the issue
  measured, which strengthens the case for a separate issue and not for widening this one.
- **Re-measuring the 49 frozen line citations.** They are stale at `90ccc65` and this RFC leaves them
  stale. Correcting them is a different piece of work with a different argument, and doing it inside a
  rename would hide the rename.
- **Residual: `agentd.log` archives.** Lines older than the cutover carry the old logger name until
  6 MiB of rotation clears them (§2). Bounded, self-expiring, and documented by (f). Not engineered
  around.
- **Residual: the three renamed `caplog` strings remain unprotected by CI afterwards.** The rename fixes
  their current value; nothing stops the next rename from stranding them again. A test that asserts the
  logger name would fix that permanently and is not proposed here — it would be a new assertion on a
  string this project has just decided is not an operational interface (§2), which is a contradiction
  worth avoiding.
- **Open for the owner, from (f):** whether `.claude/STATE.md` needs the same sentence. Unreachable from
  a session container; not claimed either way.
