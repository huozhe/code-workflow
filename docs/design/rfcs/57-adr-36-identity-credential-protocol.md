# RFC 57 — implementation binding for ADR-36: the identity-credential protocol, and the demonstration that cannot carry its own claim

| | |
|---|---|
| **Status** | Proposed (Design PR for #57) |
| **Issue** | [#57](https://github.com/huozhe/code-workflow/issues/57) |
| **Decision of record** | ADR-36 — [`../unified_design_spec.md`](../unified_design_spec.md) §16, binding §5.5 / §5.5.1 / §5.5.2 |
| **Prior art** | §5.5 landed in PR #178 (spec 1.29.0). This RFC does not re-open it. |
| **Baseline** | `main` @ `bb8aa5c` (spec 1.35.1). Every line number below is against that commit. |
| **Touches** | `docs/design/unified_design_spec.md` · `CLAUDE.md` (this PR) · `docs/ops/m4-a-branch-protection.md` · `docs/ops/live-sign-offs.md` · `agentd/tests/test_m4a_branch_protection_live.py` · `agentd/tests/conftest.py` (implementation PR) |

## 0. What this RFC is

§5.5 already decided the rule and declined a mechanism, and both of those are settled. What §5.5 did not
have — because it was written to close a rescoped issue rather than from the working tree — is the set of
things that only appear when you go looking for where the rule is already broken:

- the repository ships a test that acquires **both** identities' tokens in one process — from the Keychain,
  or from `GH_TOKEN` / `AGENTD_SECRET_CLAUDE_BOT`, which is the documented path — and posts an approval
  under the Architect's account from whoever ran `pytest`, and the M4-A runbook advertises it as
  the *"Automated re-check"* (§2). It is the most likely way an agent would try to discharge #56 step 3, and
  running it reproduces exactly the act that voided step 3 in the first place;
- **#57's item 3 does not work as written** (§3). A clean redo of step 3 produces wire evidence
  byte-identical to the tainted run — `200`, `APPROVED`, `huozheclaude` — so redoing it discharges nothing.
  What fails is not the step; it is the sentence the four steps were assembled to prove, which is an
  operator claim that no wire capture can carry;
- **§5.5.2 asks for a record and supplies neither a format nor a place** (§4), and its "cannot be re-checked
  from the database" is an absolute that a one-line stamp makes partly false — which is worth having, and
  worth bounding precisely;
- **item 2 is simply not done** (§1), and the sentence it was meant to correct is still standing in §5.1.

**Corrected in review, and the correction is the point.** The first draft's decision (e) proposed a
credential-read guard and called it *"the precise property §5.5 names rather than a proxy for it"*. The
Developer's review showed it to be **the same one-function-one-module defect §2 of this RFC diagnoses** —
blind to the env arm the documented invocation actually takes, unable to rebind the test module's imported
name, and a proxy the gateway's own code trips legitimately. It is withdrawn and replaced in (e), with the
reasoning kept rather than smoothed over, because writing that diagnosis and then committing it four
sections later is worth a reader knowing. Two further bindings came from the same review: the probe-PR
cleanup is the test's own `finally` and not a one-shot on a PR that does not exist (d′), and §5.5.2's lead
paragraph had to be rewritten **in place** rather than appended to, since as first committed it stated
*"#56's step 3 is outstanding … unchanged by this section"* directly above the paragraph saying it is
redirected.

## 1. The checklist, audited against the tree

#57's three boxes, read against `bb8aa5c` rather than against the issue.

**Item 1 — "write the rule where agents encounter it" — done, and correctly redirected.** `CLAUDE.md:7–29`
carries the rule, the read-only extension, the session-issue prohibition, the vendor-quota case, and the
§5.5.1 declaration duty. §5.5's argument for putting it there rather than in `build_prompt` — the turn prompt
reaches only the population that cannot commit the violation — holds up: `_OBLIGATIONS` (`turn.py:133`) is
still keyed by `(role, session_state)`. Nothing to do.

**Item 2 — "one line in §5.1 or the runbook" — not done.** §5.1's *Credential shape (honest, 2026-08-08)*
paragraph (`unified_design_spec.md:256`) still ends:

> Token scope therefore **does not** enforce the FR-1.3 boundary; branch protection and the gateway's narrow
> write surface (escalation comments only) do. M4-A remains the demonstration that the boundary holds.

That is the exact sentence #57 was rescoped around, and it is one-sided in two ways at once: it names branch
protection as the enforcement without saying that branch protection is an **account** control that binds
nothing about who drives an account, and it points at M4-A as the demonstration — which is the demonstration
§3 shows cannot carry that weight. No file under `docs/ops/` mentions §5.5 either; `grep -n '5\.5'
docs/ops/*.md` is empty at this commit. A reader who arrives at §5.1 and stops there learns the opposite of
what §5.5 says.

**Item 3 — "#56's step 3 still needs redoing" — open, and redirected in §3.**

## 2. The finding the checklist does not contain: the re-check *is* the violation

`agentd/tests/test_m4a_branch_protection_live.py` is a single test function that holds two identities:

| Line | What it does |
|---|---|
| `:54` | `os.environ.get("GH_TOKEN") or get_password("grok-bot")` — the Developer credential |
| `:61` | `os.environ.get("AGENTD_SECRET_CLAUDE_BOT") or get_password("claude-bot")` — the Architect credential |
| `:208–215` | `POST …/pulls/{n}/reviews` with `event: APPROVE` under the Architect token, asserting `user.login == "huozheclaude"` |
| `:228–238` | `PUT …/merge` under the Developer token, asserting `merged_by.login == "huozhegrok"` |

One process, two identities' tokens, and a durable public artifact — an approving review — attributed to an
identity whose operator did not decide it. Under §5.5 that is not a grey area; it is the rule's central case,
and the rule does not exempt the owner, because `huozheclaude`'s operator is the Architect agent and not
`@huozhe`.

**The `or` in those two lines is load-bearing, and not a wording detail.** Both are
`os.environ.get(…) or get_password(…)`, so on the documented `AGENTD_LIVE_M4A=1` run with `GH_TOKEN` set the
Keychain is never consulted at all — and `turn.py:310` exports `GH_TOKEN` into every container turn from the
role's token file, so the env arm is the normal arm rather than an override. This is what makes a
credential-read guard blind to the very invocation it would exist for; see (e).

**Three things make it worse than an ordinary stale test.**

*It is advertised as the remedy.* `docs/ops/m4-a-branch-protection.md` has an **"Automated re-check"**
section whose live half is exactly this command. An agent told to redo #56 step 3 will find that section
first — it is the only runnable thing in the document — and running it produces a fresh approval under
`huozheclaude` posted by whoever typed the command. The document offers, as the way to re-establish the
claim, the act that destroyed it.

*The suite-wide guard does not reach it.* `conftest.py:120–145` installs an autouse `_no_github_writes`
fixture, and its own comment (`:88–99`) names the class correctly — *"That is the §5.5 rule appearing as a
defect: the identity says something no operator decided, and GitHub records only the identity, so it cannot
be audited after the fact."* But the guard is `monkeypatch.setattr(design_loop, "post_issue_comment", …)`:
one function, one module. `test_m4a_branch_protection_live.py` imports `httpx` and `agentd.keychain` and
never imports `design_loop`, so the guard sees no attempts, `assert not attempts` passes, and the test writes
to GitHub with the guard green. The guard is a summary of a property it does not hold — the same shape
CLAUDE.md warns about, one level up from a fixture.

*CI is safe by a comment.* `.github/workflows/pr.yml:7` reads *"The seventh skip is
test_m4a_branch_protection_live.py (AGENTD_LIVE_M4A) — do not set that."* That is correct today and is
worth nothing against a host agent following a runbook, which is the population §5.5 exists for.

## 3. #57's item 3 is redirected, and the reason is what the evidence can carry

**FR-1.3 is an account requirement.** `SRS:40`: *"The system shall enforce distinct account credentials so
that formal PR approvals from the designated **Architect account** satisfy GitHub branch protection rules
for PRs opened by the **Developer account**."*

**M4-A's claim is an operator claim.** `unified_design_spec.md:2483` and the runbook's subtitle both state
the spike as: *"Developer identity cannot produce a satisfying approval on its own PR."* Read as an account
claim it is true and step 2 proves it. Read as a claim about who can *cause* a satisfying approval to exist
— which is how it is worded, and how #57 reads it — it is **false**, and the run that was assembled to prove
it is the counterexample. The gap between FR-1.3 and the spike's sentence is the whole of #57's item 3.

**The four steps, re-read today, and what each still carries.** All four are re-checkable from the GitHub
API and I re-checked them at this commit rather than copying the runbook:

| Step | Wire fact | Still stands? |
|---|---|---|
| 1 | Developer merge without approval → `405` ruleset refusal | **Yes.** An account fact; the ruleset refused `huozhegrok` |
| 2 | Developer self-`APPROVE` → `422` *"Can not approve your own pull request"* | **Yes.** The strongest of the four, and the one FR-1.3 actually needs |
| 3 | Architect `APPROVE` → `200`, `mergeable_state: clean` | **As an account fact, yes** — review `4913183219`, `state: APPROVED`, `user.login: huozheclaude`, body *"architect approve for m4a probe"*, `2026-08-12T05:04:54Z`. As evidence for the spike's sentence, no |
| 4 | Developer merges the approved PR → `200`, `merged_by: huozhegrok` | **Yes.** An account fact |

So step 3's *artifact* is not void, and saying "step 3 is void" — as the issue and §5.5.2 both do — is
slightly wrong in a way that matters, because it points the remedy at the wrong object. The approval really
was submitted by the `huozheclaude` account and the ruleset really did clear. What is void is the inference
from those four rows to the spike's sentence.

**Which is why a redo cannot repair it.** Re-run step 3 with the Architect's own operator and GitHub returns
`200`, `APPROVED`, `huozheclaude` — the same three values, in the same shape, in a record that has no field
for who held the credential. Two runs, one clean and one not, are indistinguishable in the only durable
evidence either produces. #57's "step 3 still needs redoing" would therefore produce a new artifact, a new
line in the runbook, and no new knowledge. **The redo is worth running only if something outside GitHub
records the provenance at the time**, and that record — not the redo — is what discharges the claim.

**Corroboration, labelled as corroboration.** PR #55 was created `05:04:17Z`, approved `05:04:54Z`, and
merged `05:05:00Z` — 43 seconds end to end across three identities — and its title is *"M4-A probe (do not
merge by hand)"*, i.e. a hand-run probe rather than a session PR. That is consistent with one operator
driving all three, and inconsistent with the webhook loop, whose drain alone polls on a 5-second interval
before a turn starts. It is not proof, and the reason it cannot be proof is §5.5.2's whole point. The
finding of fact here is the owner's, in #57: *"a review under my name that I had not written."*

**A null result of my own, recorded so it is not re-run.** I tried to settle the same question from the
gateway's own state — a session or an architect turn behind #55 — and got zero rows. That proves nothing:
the `state.db` reachable from this worktree's `AGENTD_ROOT` has **no sessions at all** (57 KB, last written
2026-08-10), so the query could not have returned a row for any PR. It is CLAUDE.md's rule landing on this
RFC while it was being drafted, and the next person to reach for that query should know the fixture cannot
reach the case.

## 4. §5.5.2 asks for a record and gives it neither a format nor a place

§5.5.2's instruction is *"record how the action was produced"*. Today an agent that wants to comply has
nowhere to write it and no agreed shape, so compliance means a sentence in a turn summary that no later
reader will find. Two cheap additions fix that, and neither is the preventive mechanism §5.5 declined.

**(i) The stamp.** An artifact whose claim is an identity claim carries its producing `turn_id` in its own
body — the review body, the PR body, the comment body. GitHub stores those verbatim and serves them forever,
so the stamp is as durable as the artifact.

**(ii) What the stamp is worth, stated exactly.** It is checkable against `turns` (`db.py:110–122`), and the
`turns` row is written by the **gateway** — `turn_id`, `session_key`, `role`, `started_at`, `ended_at` — not
by the agent. So a stamped artifact makes three independent conditions checkable after the fact: the
`turn_id` exists; its `role` is the role the artifact claims; and the artifact's timestamp falls inside
`[started_at, ended_at]`. A containerised turn additionally cannot have held the other credential at all
(§5.2, `adversarial_token_check`), so for the container path the stamp closes to something close to proof.

**(iii) What it is not.** It prevents nothing. A host agent under a borrowed credential can write any string
into a review body, and a determined one can name a real architect turn whose window contains its artifact.
The stamp converts an unfalsifiable claim into one with three conditions in a table the agent does not write
— that is the whole of the gain, and it must not be described as more, because §5.5 declined preventive
mechanisms deliberately and this RFC does not reopen that.

**(iv) `public_actions` is not this, and the difference is load-bearing.** The obvious place to look for
provenance is `turns.public_actions`, and it is the wrong place: `design_loop.py:1035` — *"public_actions are
claims (tool_use) — diagnostic, not a reset"* — and `loop_safety.py:68–69` — *"same untrusted source as
advisory `status` … escalation names what the agent **claimed**"*. It is agent-authored, so an agent that
would misuse a credential would also write the row. The gateway-written columns are the only ones worth
checking, which is exactly why the stamp is cross-checked against `role` and the turn window and not against
anything the agent supplied.

**(v) The place.** `docs/ops/live-sign-offs.md` already exists for *"acceptances that no fixture can
reach"*, which is precisely this one, and it already distinguishes itself from a list of open issues. The
operator half of M4-A becomes a row there.

**(vi) §5.5.2's absolute is scoped, in its own lead paragraph.** The 1.29.0 text says this class of claim
*"cannot be re-checked from the database afterwards"*, flat. With a stamp, a stamped artifact can be,
partly; what remains genuinely uncheckable is the original case — an **unstamped** host artifact under a
borrowed credential. **The lead paragraph is rewritten in place to say so, not annotated further down**, and
that placement is the binding, not a formatting preference: as first committed, this PR appended the
narrowing below a lead that still stated the absolute *and* still said *"#56's step 3 is outstanding … and
is unchanged by this section"* — the sentence an implementer reads first, disagreeing with the paragraph
under it about the one checklist item this RFC exists to settle. Two paragraphs of the same section
contradicting each other is the **#118 / 1.18.0** shape — *"1.16.0 said … and an implementer builds what
that says"* — and it is the prose form of a fixture that cannot reach its case: it looks complete.
(Developer's review, which cites this precedent as #126; 1.18.0's row is #118, and #126 appears in the
revision history only inside 1.20.0. The precedent itself is exactly right.)

## 5. Decisions (ADR-36)

**(a) §5.1 carries the two-context sentence and the pointer.** The *Credential shape* paragraph gains a
clause: branch protection is an **account** control, so it binds which account approves and nothing about
which operator drives it; in a container that gap is closed mechanically (§5.2), on the host by protocol
(§5.5). Discharges #57 item 2.

**(b) M4-A's claim is split, and the spike row says which half is proven.** The account claim — *the
Developer **account** cannot submit a satisfying approval on a PR it authored* — is **PASS**, on steps 1, 2
and 4, all re-checkable. The operator claim — *the Developer's operator cannot cause a satisfying approval
to exist on its own PR* — is **not established**, was false in the only run that exists, and is not a claim
GitHub's record can settle. §17's spike row and the runbook are re-worded to the half the evidence carries, with
the other half named rather than dropped. FR-1.3 is satisfied either way; it never asked for the operator
half.

**(c) The operator half is discharged on a real Feature PR, not on a probe.** The standing design loop
already produces the exact event every time it runs — a Feature PR opened by the Developer, approved by the
Architect in its own turn from inside its own container, where the other credential is unreachable by
construction. That is a *better* demonstration than any hand-run probe, because the container path is the one
production uses. The binding: the next Feature PR approval produced by an Architect turn carries the §4
stamp, and a row in `docs/ops/live-sign-offs.md` holds the acceptance until it is observed. **No new probe PR
is to be opened for this**, and #56 step 3 is not re-run by hand.

**(d) The live M4-A test loses its Architect half.** `test_m4a_branch_protection_live.py` keeps the assert
half and steps 1 and 2 — all of which need the Developer token alone — and drops `_arch_token` (`:60–64`),
the approve call (`:206–226`) and the **success** merge (`:228–241`). The 405 merge attempt at `:181` is
step 1 and stays. **Stated consequence, because it is a real loss and not a tidy-up:** the automated
re-check no longer covers step 3 or step 4. That is the correct outcome. Steps 3 and 4 require a second
operator, and a test process is by definition a single operator; a test that appears to cover them can only
do so by committing the violation. The runbook's "Automated re-check" section says which steps it covers and
which it structurally cannot.

**(d′) Cleanup is the test's own `finally`, not a one-shot.** The draft said "the probe PR must be closed and
its branch deleted", listed beside the code drops, and that is wrong in a way an implementer would ship:
there is no standing probe PR to close. PR #55 merged on 2026-08-12, and the live test **creates a fresh
one every run** — `head = f"agentd/m4a-live-{uuid4().hex[:8]}"` at `:99`, a branch, a commit and a PR, all
before the first assertion. Today the success merge disposes of it. Remove the merge and every
`AGENTD_LIVE_M4A=1` run — acceptance 3 included — leaks an open PR and a branch, and a failed run leaks one
too. Binding: the branch and PR are created inside a `try`, and a `finally` under the **Developer** token
does `PATCH …/pulls/{n}` `state=closed` then `DELETE …/git/refs/heads/{head}`, running when steps 1 or 2
assert-fail as well as when they pass. The cleanup must be armed from the moment the ref is created, not
appended after the asserts, or the failure path is exactly the one that leaks.

**(e) The no-writes guard moves to the egress chokepoint. The credential-read guard this RFC first
proposed is withdrawn, and the reason is that it was the same defect it diagnosed.** The draft had
`conftest.py` gain an autouse fixture wrapping `agentd.keychain.get_password`, failing any test that read
more than one distinct account, and called that "the precise property §5.5 names rather than a proxy for
it". All three clauses are wrong, and the Developer's review is what surfaced it:

1. **It cannot see the advertised invocation.** `:54` and `:61` are
   `os.environ.get(…) or get_password(…)`. The documented run is `AGENTD_LIVE_M4A=1` with `GH_TOKEN` set,
   and `turn.py:310` exports `GH_TOKEN` into **every** container turn from the role's token file, so the
   env arm is the normal arm. On that path `get_password` is never called and the wrapper observes nothing.
2. **The rebinding does not reach the caller even when it is called.** `:29` is
   `from agentd.keychain import get_password`; pytest imports the test module before fixtures run, so the
   module-level name is already bound and `monkeypatch.setattr(agentd.keychain, "get_password", …)` does
   not touch it.
3. **The property is a proxy, and the repository has legitimate dual-readers.** `supervisor._load_tokens`
   (`:898–900`) reads `claude-bot` **and** `grok-bot` on every container create — that is the gateway's
   job. `design_loop` at `:1546–1547` and `:632` are `get_password("claude-bot") or
   get_password("grok-bot")` fallbacks. Any test driving those on a host with a Keychain reads two
   accounts legitimately, so a guard tuned to spare them is tuned to miss the live test, and one tuned to
   catch the live test fails the suite on the gateway's own code.

(1) and (3) are *"one function, one module, and the case it exists to catch does not go through that
name"* — the sentence §2 of this RFC uses about `_no_github_writes`. Writing that diagnosis and then
committing it four sections later is the thing to record, not to smooth over.

**The replacement keys on the act rather than on a precursor to it.** §5.5 prohibits *producing an artifact
under another identity*; the artifact is made by a request leaving the process, not by a credential being
read. So: an autouse fixture that **refuses any HTTP request from the test process to the GitHub API**, with
a named opt-in fixture (`allows_github_api`, sibling of `allows_github_post`) for tests that must talk to it.

- **The chokepoint is `httpx.Client.send`.** Every `get`/`post`/`put`/`patch`/`delete` on every `httpx.Client`
  funnels through it, and `agentd/src` and `agentd/tests` contain no `AsyncClient` and no module-level
  `httpx.get(...)`/`httpx.post(...)` — checked, not assumed. **This is not the mistake above repeated**, and
  the difference is worth stating because "one function, one module" is now a known failure shape here:
  `post_issue_comment` is *one of N* ways to write to GitHub, whereas `Client.send` is *the* way an httpx
  request leaves this process. It is route-independent — it does not care whether the token came from the
  Keychain, from `GH_TOKEN`, or from a literal in the test.
- **Every request, not only writes.** §5.5's prohibition covers read-only use, so a `GET` under a borrowed
  credential is in scope. A method allowlist would be a second proxy.
- **The opt-in is the point, and it does not stop the live test.** The retained live test must reach the API
  — its assert half and both refusals are real calls — so it opts in and stays exempt. The guard's work is to
  make an exemption *visible in the test's signature* and reviewable, not to prevent it. **(d) is what removes
  the hazard; (e) is what stops the class returning silently.** Claiming more for it would repeat the
  original error.
- **Named residual: `urllib`, runner-side.** `agentd_runner/server.py:246` uses `urllib.request` for ADR-11's
  identity preflight and does not pass through `httpx`. It runs **in the container**, and `supervisor.py:116`
  / `:429` point it at a stub via `AGENTD_GITHUB_API_BASE`, so it is out of the test process's egress today.
  A future in-process test that drives the preflight against the real API is outside this guard. Recorded
  rather than engineered around.

## 6. Placement

**This Design PR (design artifacts only).**

| File | Edit |
|---|---|
| `docs/design/rfcs/57-adr-36-identity-credential-protocol.md` | this file |
| `unified_design_spec.md` §5.1 `:256` | decision (a) — the two-context clause and the §5.5 pointer |
| `unified_design_spec.md` §5.5.2 | §4 (i)–(vi): the stamp's format, its place, its bound, and the scoping of "cannot be re-checked". The **lead paragraph is rewritten in place**, not appended to — as first committed it still read *"#56's step 3 is outstanding … unchanged by this section"* directly above the paragraph saying it is redirected, and still stated the absolute as fact. (Developer's review.) |
| `unified_design_spec.md` §17 spike table `:2483` | decision (b) — the split claim |
| `unified_design_spec.md` §16 | ADR-36 |
| `unified_design_spec.md` revision history | 1.35.1 → 1.36.0 |
| `CLAUDE.md` §Identity | the stamp duty, and a standing *do not run the live M4-A test* — see below |

**Why `CLAUDE.md` moves with the spec and not with the code.** PR #178 set that precedent when §5.5
landed, on §5.5's own argument: the rule belongs where host agents read it. There is a second reason
here. This RFC defers the runbook fix to the implementation PR, which opens a window in which the
runbook still advertises the violation as the remedy — and #56 step 3 is exactly the errand that sends
an agent to that section. A one-bullet prohibition closes the window at the moment this PR merges rather
than at the moment the next one does. It is a stopgap and is written as one; ADR-36 (d) removes the
hazard rather than warning about it.

**The implementation PR (not this one — §5.5's residue is code and ops, and the invariant is design-only
until this is approved).** In priority order, because the first item is actively misleading a reader today:

1. `docs/ops/m4-a-branch-protection.md` — split **Status: PASS** into the two halves per (b); annotate step 3
   with the provenance finding and a pointer to §5.5.2; rewrite **"Automated re-check"** to say what the live
   test covers after (d) and that steps 3 and 4 are not automatable, with an explicit *do not run this to
   discharge #56 step 3*.
2. `agentd/tests/test_m4a_branch_protection_live.py` — decision (d): drop `_arch_token` (`:60–64`), the
   approve block (`:206–226`) and the merge block (`:228–241`); close the probe PR and delete its branch;
   update the module docstring, which currently documents two tokens.
3. `agentd/tests/conftest.py` — decision (e).
4. `docs/ops/live-sign-offs.md` — decision (c): a new row in **The ledger**, with the *Not discharged by*
   column naming the live test and a hand-run probe explicitly.
5. `CLAUDE.md` — **retire the stopgap bullet in the same PR as (d), not a later one.** Its stated reason is
   *"it reads both machine users' Keychain entries in one process and posts an approval as `huozheclaude`"*,
   and (d) makes that false; the runbook rewritten in item 1 will simultaneously say the test is safe to
   run. A stopgap whose reason has expired reads as a live prohibition, and the two documents would then
   disagree — the failure this RFC spends §2 on. The bullet's *"until then"* is not enough on its own,
   because nothing in the file list brought a reader back to it. (Developer's review.)

## 7. Acceptance

1. **§5.1 no longer reads one-sided.** A reader arriving at `:256` and stopping there is told that branch
   protection binds accounts and not operators, and where the other half lives. `grep -c '5\.5' ` on §5.1's
   paragraph is non-zero.
2. **The spike row states which claim is proven.** `:2483` no longer asserts the operator claim as PASS, and
   names the account claim, the evidence for it, and the outstanding half.
3. **A fresh checkout cannot produce a cross-identity artifact from the suite.** With `AGENTD_LIVE_M4A=1`
   and both Keychain entries present, the live test runs to completion and no review is created under
   `huozheclaude` — asserted by reading the probe PR's `reviews` array, which must be empty, **not** by the
   test reporting success.
3b. **The run leaves nothing behind, on the failure path as well as the success path.** After the run the
   probe PR is `state: closed` and `GET …/git/ref/heads/agentd/m4a-live-<suffix>` is 404. Asserted **twice**:
   once on a clean run, and once with step 1's assertion forced to fail, because per (d′) the leak this
   binds is the failure path and a green-only check cannot reach it.
4. **The egress guard fails on the path the withdrawn guard could not see.** A throwaway test that issues an
   `httpx` request to `api.github.com` **with `GH_TOKEN` set and the Keychain never consulted** fails, and
   the failure names the URL. Asserted with the guard reverted first, so the fixture is shown to reach the
   case. The Developer's review asked that this also use the live test's `from … import` style; the
   redesign dissolves that half of the requirement — `Client.send` is route-independent, so import style
   cannot affect it — and the env-var path, which is the half that mattered, is what item 4 pins.
4b. **The opt-in works and is visible.** The same request, in a test requesting `allows_github_api`, is
   permitted and recorded. A test that talks to the API without the fixture in its signature cannot pass.
5. **The guard does not fire on the gateway's legitimate dual-reads.** The full suite is green with the
   guard installed, checked specifically against tests that drive `supervisor._load_tokens` (`:898–900`) and
   the `design_loop` fallbacks (`:1546–1547`, `:632`) — the three call sites the withdrawn credential guard
   would have failed. Naming them is the point: "the suite is green" is the kind of summary this project has
   been burned by, so the check is against those sites, not against the total.
6. **The stamp is checkable end to end, on one real artifact.** For the Architect approval named in (c):
   the review body contains a `turn_id`; that `turn_id` exists in `turns`; its `role` is `architect`; and the
   review's `submitted_at` lies within `[started_at, ended_at]`. All four from the live `state.db` and the
   GitHub API, not from a turn summary.
7. **The ledger row exists before the observation, not after.** `docs/ops/live-sign-offs.md` carries the
   operator half as an open row at the time the implementation PR merges, so the acceptance is held rather
   than discovered.
8. **No new probe PR was opened for any of this** — (c) is discharged on a Feature PR the loop produced
   anyway.

## 8. What this RFC does not establish

- **That the #55 approval was produced by the Developer's operator.** That finding is the owner's, from
  direct observation. Everything reachable from the API is consistent with it and none of it is proof, which
  is the property under discussion rather than a weakness in the argument.
- **That the stamp cannot be forged.** §4 (iii). It raises the cost and leaves the act possible; §5.5's
  refusal of a preventive mechanism stands unamended.
- **That the container path is clean today.** §5.2's guarantees are asserted on `assert_bearer_not_readable_by_roles`
  and `adversarial_token_check`, verified live during #24. This RFC re-reads neither and claims nothing new
  about them.
- **Anything from the gateway database.** §3's null is a fixture failure and is recorded as one.

## 9. Out of scope

- **Any host-side enforcement mechanism.** §5.5 declined one with its reasoning recorded; (e) is a test-suite
  guard, not a host control, and is scoped that way on purpose.
- **The `!`-prefix workflow** by which the owner runs a command an agent may not. Named in §5.5, unchanged.
- **§5.5.1's stand-in conditions.** Landed in PR #178 and untouched here.
- **A rule for a non-owner *reopen* of a session issue.** `:1618` parks it with "#57's identity family"; it
  is a §10.3 question about the gateway's own policy, needs no identity analysis, and widening this RFC to
  reach it would put a gateway behaviour change inside a design round about a protocol.
