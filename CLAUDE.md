# agentd — standing instructions for agents working in this repository

Design decisions live in [`docs/design/unified_design_spec.md`](docs/design/unified_design_spec.md).
Read the ADR, not the issue: several issues' own diagnoses were corrected by the ADR that fixed them.

## Identity (§5.5, #57)

**Use only your own identity's credential.** Any artifact attributed to an identity — a commit, a push,
a review, an approval, a merge, an issue close — must be produced by that identity's operator.

This is a protocol, not a mechanism. Inside a container it is enforced by construction: Linux, no
Keychain, a `0400` tmpfs token per role. On the host there is nothing stopping you, which is why the
rule is written here, where host-side agents actually read.

- The prohibition covers **read-only** use as well. Do not invoke another identity's token to check
  something; the check is one flag away from the act. If such a command is genuinely needed, ask the
  owner to run it — that is what the interactive `!` prefix is for.
- **Never close a session issue.** §10.3 reserves that to the human owner. The gateway's check is
  `sender == owner`, so an agent acting under the owner's identity would pass the mechanical test while
  defeating the rule it encodes.
- A machine user's GitHub credential usually still works when its **vendor** quota is exhausted. Using
  it anyway makes the audit trail assert that an agent acted which did not.
- If you are a host agent carrying an absent role's work under the owner's identity (§5.5.1), say so in
  the body of every artifact you author. GitHub records the identity, never the operator.

Where a demonstration's claim *is* an identity claim, record **how the action was produced** (§5.5.2).
GitHub logs the identity, not the credential's operator, so that class of claim cannot be re-checked
from the database afterwards.

## Review

Every real defect on this project was found by **running** the code, not reading it. Drive the real
entry point, verify inside the image when the change is in the runner, and confirm a new test fails
with its fix reverted. `--help` lies. Expect the counterpart's review to find a real defect, and expect
your own ADR to be wrong somewhere.

**A null or green result is only as good as its fixture.** Before believing "cannot reproduce" — or a
passing test — prove the fixture reproduces the property first. A stub that bypasses the mechanism under
test, a query that crosses sessions, a git fixture that is not a promisor repo, a poll loop whose filter
errors, and a test named for a case it does not cover all look exactly like good news. Each of those has
produced a confident wrong answer here, several on the same day. Assert the property in the fixture, then
assert the behaviour.
