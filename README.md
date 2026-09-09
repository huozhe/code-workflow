# code-workflow

An asynchronous multi-agent AI coding system that uses **GitHub as its message bus and state machine**.

Two agent roles — an **Architect** and a **Developer**, each a separate GitHub machine user — collaborate through Issues, Pull Requests and reviews to design, implement and verify changes. A local daemon, `agentd`, receives GitHub webhooks, runs each session in its own container, and drives the roles turn by turn. Closing an issue stays a human act.

**The system builds itself.** The gateway, the container runner and the design documents in this repository are produced by the system running on this repository — sessions #240 and #248 opened PRs #241, #250 and #251, and every one drew `CHANGES_REQUESTED` from the opposite identity first.

Target environment: a local Mac mini (Apple Silicon M4 Pro, 24 GB) running OrbStack.

## How it works

A GitHub webhook reaches an HMAC-verified endpoint on `127.0.0.1:8787`, lands in a durable SQLite queue, and a dispatcher turns it into a role's turn inside a per-project container — two OS UIDs, worktrees over a shared clone, and credentials delivered over a control channel rather than bind-mounted. The role then acts on GitHub under its own identity.

SQLite is a derived cache, never the source of truth. GitHub holds the state; a reconciler rebuilds whatever a dropped delivery lost.

Two rules shape most of the design. **An artifact must be produced by the identity it is attributed to** — so each role holds only its own credential. And **an issue closes only when a human ticks the box**, which keeps the loop from declaring its own work finished.

## Layout

```
docs/
├── requirements/   what the system must do          (owner-governed)
├── design/         how it does it                   (agent-produced)
│   ├── ADR/          41 decisions, one file each
│   ├── rfcs/         proposals behind the larger ADRs
│   └── proposals/    superseded Phase 1 drafts, kept for provenance
└── ops/            runbooks, and the live sign-off ledger

agentd/
├── src/agentd/     gateway, supervisor, design and code loops,
│                   reconciler, GC, loop safety
├── src/agentctl/   operator CLI: status, sessions, logs, version
├── docker/         the session-runner image and its RPC server
└── tests/          739 tests
```

| Document | Purpose |
|---|---|
| [`docs/requirements/SRS_async_multiagent_ai_coding_system.md`](docs/requirements/SRS_async_multiagent_ai_coding_system.md) | **System Requirements Specification, v1.3.** What the system must do, and under what constraints. Implementation choices are deliberately left open. Changes are governed — see the changelog at the top. |
| [`docs/design/unified_design_spec.md`](docs/design/unified_design_spec.md) | **Unified Technical Design Specification, v1.41.0.** The current design. Resolves every requirement in the SRS, maps each FR/NFR to a section, and names the weaknesses that were consciously accepted. |
| [`docs/design/ADR/`](docs/design/ADR/) | The decision of record. Where an ADR and the spec's summary disagree, the ADR is right. Several ADRs correct the issue that prompted them — read the ADR, not the issue. |
| [`docs/ops/live-sign-offs.md`](docs/ops/live-sign-offs.md) | What no fixture can prove, and which real session would prove it. |
| [`docs/ops/m0-bootstrap.md`](docs/ops/m0-bootstrap.md) · [`project-onboarding.md`](docs/ops/project-onboarding.md) | Standing the host up, and onboarding a project to it. |

## Status

Built, deployed, and running real sessions.

Code exists through M7 of the §21 roadmap. That is deliberately not the same as saying the milestones are met: each one *"ends in a verifiable condition, not a code-complete claim,"* and several conditions can only be observed on a live session against the real gateway. [`docs/ops/live-sign-offs.md`](docs/ops/live-sign-offs.md) tracks which are still outstanding and what evidence would discharge them.

Four defects are open: [#163](https://github.com/huozhe/code-workflow/issues/163), [#214](https://github.com/huozhe/code-workflow/issues/214), [#231](https://github.com/huozhe/code-workflow/issues/231), [#252](https://github.com/huozhe/code-workflow/issues/252).

## How this project reviews itself

Three LLM agents wrote independent blind drafts of the design, cross-reviewed each other's pull requests, and argued to consensus over five rounds on [Issue #1](https://github.com/huozhe/code-workflow/issues/1). The unified spec is not one draft with the others merged in — several sections replace what their own drafter proposed, and §1.2 attributes each. Nine defects were found across the three drafts, every one by someone other than its author.

That result set the working rule the project still runs on: **whoever implemented last time reviews next.** Every defect that changed the design came from the reviewer's seat, never from an implementer re-reading their own work.

The second rule is harder won. **A green suite, a null result and a clean status label are all summaries, and each has lied here.** Before believing a passing test or a "cannot reproduce", the fixture must first be shown to reach the case it claims to cover.
