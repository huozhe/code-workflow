# RFC 159 — implementation binding for ADR-39: renaming `design_loop.py`, and the four references that must *not* be renamed

| | |
|---|---|
| **Status** | Proposed (Design PR for #159) |
| **Issue** | [#159](https://github.com/huozhe/code-workflow/issues/159) |
| **Decision of record** | ADR-39 — [`../unified_design_spec.md`](../unified_design_spec.md) §16 (new), spec 1.39.0 |
| **Baseline** | `main` @ `90ccc65` (spec 1.38.0). Every count and line number below is measured against that commit. |
| **Touches** | **This PR:** `docs/design/unified_design_spec.md` (ADR-39 + §16 naming note + 1.39.0 row) · this RFC. **Implementation PR:** `agentd/src/agentd/{design_loop,dispatcher,server,verify,reconciler,fsm}.py` — `dispatcher` and `server` carry **log messages *and* prose** (§2, §5″); `verify` carries **prose only** — one line, `:215`, and no log message; `reconciler` and `fsm` are import/comment only · 37 files under `agentd/tests/` · `docs/design/unified_design_spec.md` (the 2 current-tense citations only — §5.5.2 **`:448`**, §12.3 **`:1016`** *on the post-merge base; `:447`/`:1015` at `90ccc65`*) · `docs/ops/live-sign-offs.md` (1 note) |
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

**Revised three times since the first draft, and the three failures are not all the same one.** The
first two were: §2's claim was scoped to the file being renamed, and §5 was scoped to the *identifier* —
both queries that could not reach the case, and both answers that looked complete. The third, found on
`280b666`, is a different and sharper shape: **acceptance (1″) prescribed one command and stated another
command's answer.** The number `5` was real — I had measured it with a two-stage pipeline that subtracts
identifier hits — but the item told the implementer to run `design[-_ ]?loop` over `docs/`, which returns
**69**, of which 64 are the very citations §4 binds as frozen. An implementer following the item as
written would see a large finding pointing straight at the frozen records, with no warning in the item
that a result *above* five is possible at all.

That is worth separating from the first two because a scoped query is caught by widening it, and this is
not: the measurement was correct, the binding was correct, and the two were written down as if one
implied the other. The remedy is that (1″) now carries **two commands with two regexes and two expected
numbers**, each runnable verbatim, and reads its result in both directions.

**The same class four times, and the fourth is the instructive one.** Adding ADR-39 to the spec moved
several numbers the acceptance items are stated against. I caught two of them before pushing — the
frozen-record count (55 → **64** spec lines) and the docs mirror's fixed point (5 → **8**) — rewrote (6)
and (1″) to name the base they run against, said so in the commit message, **and in that same commit
left `:447` / `:1015` standing in (6), (7) and `Touches`, and left ADR-39's own Acceptance paragraph
publishing the superseded `→ 5`.** The 1.39.0 revision row shifts every spec line below it by one, so
the correct pair is **`:448` / `:1016`**; on the post-merge base `:447` is a blank line. Both were the
Developer's finding on `770fab4`.

That is worth more than the fix. Knowing the failure mode, naming it in prose, and rewriting two items to
prevent it did **not** stop me from committing two more instances of it in the same breath — because the
knowledge was applied where I was looking and the remaining copies were somewhere else. The structural
remedy, not the vigilance one: **each number now lives in exactly one place.** ADR-39 no longer restates
any count and points at §7 instead (the decision of record and the binding cannot drift if only one of
them carries the figure); §4's table is explicitly captioned as the `90ccc65` measurement and keeps the
old pair; and every item that runs later verifies by **content** — the paragraph at `:448`, the sentence
at `:1016`, the eight enumerated mirror rows — because a number checks nothing about the thing it names.
§4's and §5″'s tables remain the `90ccc65` measurement and are correct as such; (1″) and (6) are the two
items that run later, and they say so.

The taxonomy is four classes plus two boundaries — identifiers (§5), split constants (§5′), colloquial
prose (§5″), and dated citations that live inside the sweep and so cannot be frozen (§5‴). Everything in
§5″ and §5‴ came from review, as did the correction to (1″), the `verify.py` line in `Touches`, and
(c′)'s promotion into the decision of record.

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

**Four** of those six messages are emitted by `design_loop.py` (`:821`, `:702`, `:866`, `:555`); the
fifth is `reconciler.py:204` and the sixth is the runner's kill path. **A logger rename changes none of
them.** `docs/ops/m0-bootstrap.md` greps `dev.agentd` out of `launchctl`, which is a plist label. There
is no third ops file.

The places where the module's *name* is load-bearing to an operator are not the logger at all, and
**there are three of them, in three different modules**:

```
design_loop.py:426    log.exception("design_loop failed delivery=%s", row["delivery_id"])
dispatcher.py:77      log.exception("design_loop batch failed")
server.py:98          log.exception("design_loop init failed; deferred deliveries stay parked")
```

All three are `log.exception`, so all three are ERROR and land in `gateway.err.log` — the crash sink
`live-sign-offs.md:170` names. They are the highest-value operator greps in the tree: *"the gateway is
not draining"* is answered by grepping this name in that file. All three rename with the module.

**Corrected after the first draft, and the correction is the point.** This section originally claimed
`:426` was *"the only occurrence of its kind"*, on the evidence of `grep -n '"[^"]*design_loop[^"]*"'
src/agentd/design_loop.py` — **a query scoped to the one file being renamed, whose answer was then
generalised to the tree.** That is this project's recurring failure shape, committed inside the RFC
that catalogues it: the fixture could not reach the case. Two of the three sites are in the importers,
which is precisely where a message about a module gets logged from. The corrected query resolves
string *constants* rather than source lines, tree-wide (§5′).

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

*Line numbers in this table are `90ccc65`, this RFC's stated baseline, and stay that way. The Design PR
inserts the 1.39.0 revision row, which moves everything below it by one: on the base the implementation
branches from, these two are **`:448`** and **`:1016`**. Acceptance (6), (7) and `Touches` use the later
pair, because they are the items that run later — see the note in §0.*

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
  the citation drifted 84 lines. Rewrite the path **and** the number. **Not to a flat `:1119`** — an
  earlier draft said that, on the reasoning that "a pure rename shifts no lines". True of a pure rename;
  this PR is not one, because decision (d) rewrites the module docstring in the same commit. The number
  is `1119 + Δ` for (d)'s line delta, and acceptance (7) verifies it by content. The 84-line drift is
  pre-existing; Δ is introduced here and is the only line shift in the PR.
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

**This bounds the *sweep*, not the *edit list*.** §5″ adds six prose sites that no identifier query
reaches and that are edited by enumeration. "Never the bare word" and "never the bare phrase" both hold;
what changes is that the enumeration, not the regex, is what makes them safe.

The three test *filenames* split on the same line: `tests/test_design_loop.py` and
`tests/test_stall_design_loop.py` are named for the module and are renamed with it;
`tests/test_m3d_design_exit.py` is named for the **design-half exit criteria** (M3-D) and is not.

## 5′. The sweep's blind spot: a line grep cannot see a string constant

The corrected §2 was found by a query that resolves **string constants** instead of source lines, and
the difference is a hazard the acceptance list has to name.

This tree wraps long log messages mid-phrase. `design_loop.py:555` is the live example — the runbook
greps `"author-sent PR event, no turn"`, and that phrase **exists in no source line**:

```python
log.info(
    "delivery done id=%s event=%s action=%s — author-sent PR "
    "event, no turn (#173)",
```

Implicit concatenation makes the emitted string whole; `grep` sees two halves. That is how §2's original
count was wrong by two, and it is the shape a rename sweep is exposed to: a name split across a
concatenation boundary is invisible to acceptance item (1).

**Measured at `90ccc65`: the exposure is currently zero.** Parsing every file under `src/` and `tests/`
and walking `ast.Constant`, there are **14** string constants whose *value* contains `design_loop` or
`DesignLoop`, and for every one of them the name appears intact on at least one source line. **Zero**
are split across the boundary. So acceptance item (1)'s grep is sufficient at this commit.

It is sufficient by luck, not by construction, and the luck is exactly what the rename can destroy: an
implementer who rewraps `"design_loop init failed; deferred deliveries stay parked"` to fit a line
length while renaming it can produce `"session_"` `"loop init failed…"` — green, greppable by nothing,
and wrong in the file the crash sink points at. Hence acceptance (1′).

**The attribute and keyword-argument sites are not this class and need no special handling.**
`Dispatcher.__init__` takes `design_loop=` (`dispatcher.py:40`, called from `server.py:100` and
`tests/test_turn_resume.py:535`) and reads `self.design_loop` at `:72`, `:74`, `:75`. Six sites, all
matched by the plain name, and a missed keyword breaks loudly at the call.

## 5″. The colloquial name — where `design loop` means the module, and where it means the protocol

**Developer finding on PR #227, and it is the same shape §2 had to correct: a query scoped to
`design_loop` / `DesignLoop` whose answer was then treated as the set of current-tense names.** It is
not. The module is also referred to in prose, with a hyphen or a space, and no identifier sweep reaches
any of it.

The complete set in `src`/`tests` is **six** lines, matching `design[- ]loop` case-insensitively —
hyphen or space, **not** the broad `design[-_ ]?loop`, which also matches `design_loop` and `DesignLoop`
and so returns 321 here and 69 in `docs/` (see acceptance (1″), where the distinction is the whole
item):

| Site | Text | Status |
|---|---|---|
| `design_loop.py:1` | `Design-loop orchestration — … (M3).` (module docstring) | Already bound by decision **(d)** |
| `dispatcher.py:1` | `Dispatcher — intake gate + design-loop drain (M1/M3).` | **New edit** |
| `dispatcher.py:24` | `handed to a sessions row (M3 design loop). See §15.1.` | **New edit** |
| `server.py:80` | `# Design loop: session/turn orchestration (M3). Supervisor is` | **New edit** |
| `verify.py:215` | `design loop emits ``merge_authorized`` so the **Developer** acts.` | **New edit** |
| `tests/test_design_loop.py:1` | `Design loop: deferred → session row + route without docker turns.` | **New edit** (file renames too) |

**`verify.py:215` is the load-bearing one and it is worth reading in place.** It is the docstring of
`verify_feature_merge` — headed *"§8.4 Feature PR merge authorization (M4-2)"*, the **code half** — and
it tells the reader that *"the design loop emits `merge_authorized`"*. That is not a stale label; it is
#159's defect stated as documentation, in the module a reviewer opens to understand merge
authorization. `server.py:80` is the second worst: after a literal application of this RFC it would
introduce the lifecycle owner as *"Design loop: session/turn orchestration"* **five lines above**
`from agentd.session_loop import SessionLoop` (`server.py:80` and `:85`), in the same block.

**The boundary is not a curated list, it is a measured rule.** The same narrow query over `docs/`,
`README.md` and `CLAUDE.md`, with this RFC excluded, returns **five** lines, and every one is the
**protocol** — the M3
milestone rows (`unified_design_spec.md:2832`, `proposals/claude_design_spec.md:881`), §-body M3
language (`:242`), the standing-loop reference in `rfcs/57-adr-36-…:223`, and
`live-sign-offs.md:51`'s *"the ordinary design loop yields it whenever a Feature PR is reviewed"*.

> **In `src` and `tests` the phrase always names the module (6/6). In `docs` it always names the
> protocol (5/5).** That is the binding; the enumerated table is its instance, not its definition.

**Do not widen the sweep to the bare phrase `design loop`.** The rule above is exactly why the
enumeration is safe and a regex is not: an implementer who greps `design loop` across the repo and edits
what it finds renames the M3 milestone and rewrites a captured observation in `live-sign-offs.md` —
§4's falsification class, reached by a different route.

## 5‴. A fifth record class: dated citations that live inside the sweep

§4 splits references into code, current-tense prose, and dated record, and freezes the third. **Two
references are both at once**, and the taxonomy as written has no cell for them — Developer finding:

```
tests/test_adr30_author_pr_events.py:209   Acceptance (4): design_loop.py:74–79 — standalone inline comment wake.
tests/test_adr36_github_api_guard.py:90    Acceptance 5: design_loop :1546–1547 and :632 consult both agent accounts.
```

They are ADR-30 and ADR-36 **acceptance labels**, dated against those ADRs' baselines exactly like the
55 spec citations §4 freezes — and both are already stale at `90ccc65`: `:74–79` is now a comment block
about review-webhook *parts*, and `:632` / `:1546–1547` are now `event=event,`, `pr_num: int,` and
`delivery_id: str,` — nothing to do with the `get_password("claude-bot")` fallbacks ADR-36 cited.

They cannot be frozen, because they sit inside `tests/` and acceptance (1) requires zero matches there.

**Binding: rewrite the path, never the number — and this is the one place in the RFC where new-path /
old-line is correct rather than "the worst of the three options" (§4).** In the spec, freezing keeps
path and number consistent against a stated baseline. Here (1) forces the path, so consistency is not
available, and the choice is between a citation that is honestly stale — matching the ADR it labels —
and one silently re-measured to a line whose meaning nobody checked. Name both in the Feature PR body
so the reviewer does not read the stale numbers as a miss.

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
preference. **All three** operator-facing messages naming the module rename with it — `design_loop.py:426`,
`dispatcher.py:77`, `server.py:98` — not just the one in the renamed file.

**(c′) The five colloquial-name sites in §5″ are renamed too.** `dispatcher.py:1`, `:24`, `server.py:80`,
`verify.py:215`, and the `test_session_loop.py` module docstring. Prose, not identifiers; invisible to
every other check in §7; and `verify.py:215` is the single site in the tree that states #159's defect as
documentation.

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

**It will be a multi-line docstring, and §4's "a pure rename shifts no lines" does not survive that —
Developer finding, accepted with the remedy inverted.** The review asks that (d) be bound
*line-count-neutral* so acceptance (7)'s `:1119` stays true. Measured: (d)'s content on one line is
**199 characters**, against a longest existing line in this file of **100** and a conventional 88.
`line-length` is unset and `E501` is not in the enabled `ruff` set, so a 199-character docstring would
*pass lint* — which makes it worse, not better: the constraint would be invisible to CI and enforced
only by this sentence. Forcing an unreadable line to protect a hard-coded citation is the tail wagging
the dog.

Bind it the other way. **(d) is a normal multi-line docstring**, and the load-bearing claim moves from
the number to the arithmetic: this PR's only line-shifting edit is (d), its delta Δ is `new_docstring_lines − 1`
and is known by inspection, so §4's `:1119` becomes **`1119 + Δ`**, verified by content (§7 item 7).
That the RFC hard-coded `1119` at all was introduced in `86afa4d`, replacing a content-anchored check
with a numeric one — the review caught the consequence, and the cause was mine.

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
construction and should stay short: the decision is **(a)–(f) including (c′)**, the alternatives are the
issue's own two logger options, and the rejected-alternative worth recording is the blanket sweep —
because it is what a careful person does by default and it is green when it is wrong.

**(c′) is in that list deliberately, and the review had to ask for it.** An earlier draft wrote the
decision set as "(a)–(f)", which reads as a range and silently drops the primed member — and (c′) is the
only decision that puts `verify.py:215` in the work at all. A decision of record that omits it leaves
the one site where #159's defect is stated *as documentation* resting on §6 and §7 alone. Reviewer
finding on the approving review; carried into ADR-39's body as its own bullet, not folded into (c).

**Which PR each artifact lands in, since the RFC did not say and the two are not interchangeable.**
ADR-39, the §16 naming note and the 1.39.0 revision row are **design artifacts and land in the Design
PR** — the convention every prior binding RFC follows (RFC 57 shipped `unified_design_spec.md` and its
RFC file in one commit, `8d28414`; RFC 169 likewise, `6b117e3`), and §8.3's reason for merging the
Design PR at all is that the Feature PR's base must contain the approved design. The **two current-tense
citation rewrites in §5.5.2 and §12.3 cannot**: §5.5.2's new number is `1119 + Δ`, and Δ does not exist
until the rename does. They land in the **implementation PR**, with §5.5.2 verified by content per
acceptance (7).

## 7. Acceptance

Ordered so that a failure at any step names the thing it falsifies. **(1″), (1′), (5) and (6) are the
four a green suite does not cover**, and they are the reason this is not a one-line PR description.

1. **`grep -rn 'design_loop\|DesignLoop' agentd/src agentd/tests` returns zero.** The blunt one. Run it
   as the last step, not the first.

1″. **The colloquial sweep (§5″) — two commands, two different regexes, two expected numbers.** They
   are not the same query and must not be written as if they were. Run each verbatim.

   **(a) `src`/`tests`, a post-condition of zero:**

   ```bash
   grep -rniE 'design[-_ ]?loop' agentd/src agentd/tests        # want: 0
   ```

   The broad form is right *here* because after the rename both classes are gone. At `90ccc65` it
   returns **321** — 315 identifier lines plus the 6 prose lines of §5″ — so this is a post-condition,
   not a count to reconcile against six.

   **(b) The docs mirror — a fixed point, and the regex is deliberately narrower:**

   ```bash
   grep -rniE 'design[- ]loop' docs README.md CLAUDE.md \
        --exclude=159-adr-39-session-loop-rename.md            # want: 8, unchanged
   ```

   Hyphen **or space only**: no `_`, and no `?` making the separator optional. Both of those were in an
   earlier draft of this item and both are wrong here, because they make the query match `design_loop`
   and `DesignLoop` — which drags in **64** identifier citations that §4 has just bound as frozen
   records. Measured at `90ccc65`, RFC excluded: the broad form returns **69**, the narrow form **5**.
   *(Developer finding. The earlier draft told the implementer to run the broad query and expect the
   narrow query's answer; the "5 / 16" pair it quoted came from a third procedure again — a two-stage
   pipeline subtracting identifier hits — which is why its numbers matched neither.)*

   **Why 8 and not 5, and this is the same base problem as item (6).** Five is the `90ccc65` figure. The
   Design PR then adds ADR-39 and its revision row, which quote the phrase three times — spec `:21`
   (revision row), `:2775` ((c′) quoting `verify.py:215`) and `:2785` (the boundary paragraph saying the
   sweep must never match the bare phrase). None of the three is a protocol reference and none is a site
   to edit. So against the base the implementer actually branches from, the fixed point is **8**:

   | Line | What it is |
   |---|---|
   | `unified_design_spec.md:2865` | M3 milestone row *(was `:2832` at `90ccc65`; ADR-39 shifted it)* |
   | `unified_design_spec.md:243` | §11 M3 session-creation language *(was `:242`)* |
   | `proposals/claude_design_spec.md:881` | M3 milestone row, superseded draft |
   | `rfcs/57-adr-36-…:223` | the standing design loop — the protocol |
   | `live-sign-offs.md:51` | *"the ordinary design loop yields it whenever a Feature PR is reviewed"* |
   | `unified_design_spec.md:21`, `:2775`, `:2785` | ADR-39 and its revision row, describing this change |

   **Read the result in both directions.** *Below* eight: the sweep was widened to the bare phrase and
   has renamed an M3 milestone or rewritten `live-sign-offs.md:51`'s captured observation — §4's
   falsification class. *Above* eight: the mirror was run with the broad regex, and the number it
   reports is mostly the frozen records themselves. Neither reading is available from the count alone,
   so **print the eight lines and check them against the table**, which is the only form of this check
   that cannot go stale silently.

1′. **The same, resolved through the parser, because (1) cannot see a split string (§5′).** Walk
   `ast.Constant` over every `.py` under `src/` and `tests/` and assert no constant's *value* contains
   either name. At `90ccc65` this finds 14 constants that (1) also finds; after the rename it must find
   zero. This is the check that survives a rewrap, and it is four lines of throwaway script — not a test
   to commit.
2. **`pytest`, `lint`, `types` green**, and `git diff --stat` shows no `.py` hunk that is not a rename of
   one of the two names **or an edit named in (c′) / §5‴**. Enumerated, because "nothing else" would
   otherwise make the §5″ prose fixes a failed acceptance item (Developer finding): a semantic diff of
   `session_loop.py` against `design_loop.py` is the docstring, the logger and the `:426` message; of
   `dispatcher.py`, the `:77` message plus the `:1` and `:24` prose; of `server.py`, the `:98` message
   plus the `:80` comment; of `verify.py`, the `:215` docstring line alone; of
   `tests/test_adr30_author_pr_events.py:209` and `tests/test_adr36_github_api_guard.py:90`, the cited
   **path only, with the line numbers left stale** (§5‴). Nothing else.
3. **`grep -rn '\bdesign_pr\b\|_is_design_pr\|is_design_head_ref\|DESIGN_STATES' agentd/src` returns the
   same set of lines as at `90ccc65`.** §5's guard against a too-wide regex. This is the check that
   catches the sweep that passed CI because nothing tests a column name's spelling in prose.
4. **`tests/test_m3d_design_exit.py` still exists under that name**, and `test_design_loop.py` /
   `test_stall_design_loop.py` do not.
5. **The `caplog` claim is shown on the real suite, not on §3's model.** On the renamed tree, revert one
   of the three strings to `"agentd.design_loop"` and run that single test: it must **pass**. That
   demonstrates the suite cannot see the logger name, which is why (e) was a binding rather than a
   suggestion. If it fails, §3 is wrong — say so in the review and keep the strings correct anyway.
6. **The four frozen records are byte-identical — and the base to diff against is `main` *after* this
   Design PR merges, not `90ccc65`.** ADR-39, the §16 naming note and the 1.39.0 revision row land in
   the Design PR (decision (g)), so by the time the implementation branches they are already history and
   the Feature PR must not touch them either.

   `git diff origin/main -- docs/design/rfcs/ docs/ops/live-sign-offs.md` shows changes only in item 4
   of *Running the exercise* — and in particular **`live-sign-offs.md:158`'s quoted log line is
   untouched**; this RFC itself should not change in the Feature PR at all. Then, in the spec:
   **`git diff origin/main -- docs/design/unified_design_spec.md` touches exactly two lines, `:448` and
   `:1016`.** Not "two lines outside §16" — §16 is finished by then. **Not `:447`/`:1015`** — those are
   the `90ccc65` numbers, and on the post-merge base `:447` is a blank line. The 1.39.0 revision row this
   Design PR inserts shifts everything below it by one. *(Developer finding: (6) had just been rewritten
   to name its base and then quoted numbers measured on the other one.)* Verify by content, not by
   number: `:448` is the `turns.public_actions` / `design_loop.py:1035` paragraph, `:1016` is the
   `DesignLoop` with a bare `Config()` sentence.

   Counted: the spec carries **64** `design_loop`-bearing lines once the Design PR is in (42 in older
   §16 ADR bodies, 12 revision rows, 8 in ADR-39 itself, 2 current-tense). **62 of the 64 survive the
   Feature PR untouched.** Count them; a third changed line is a sweep that reached a frozen record.
   *(These numbers replace the 53-of-55 pair an earlier draft carried, which was measured at `90ccc65`
   — before this PR added nine lines of its own. §4's table is still the `90ccc65` measurement and is
   correct as such; this item is the only one that runs against the later base, and it says so.)*
7. **§5.5.2 — at `:448` on the post-merge base, `:447` at `90ccc65` — cites `session_loop.py:1119 + Δ`,
   where Δ is (d)'s docstring line delta, and the check is
   the content, not the number.** Open `session_loop.py` at whatever line §5.5.2 now cites and confirm
   the `public_actions are claims (tool_use)` comment is on it. Δ is `0` only if (d)'s docstring stays
   one line, which decision (d) says it will not. This is the one place a
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
