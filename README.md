# code-workflow

Design work for an asynchronous multi-agent AI coding system that uses **GitHub as the central message bus and state machine**.

Two AI agents — an **Architect/Planner** and a **Developer/Implementer** — collaborate through GitHub Issues, Pull Requests, and code reviews to design, implement, and verify changes, with issue closure gated on explicit human verification.

Target environment: a local Mac mini (M4 Pro, 24GB RAM) running OrbStack.

## Contents

```
docs/
├── requirements/   what the system must do  (owner-governed)
└── design/         how it will do it        (agent-produced)
    └── proposals/  superseded Phase 1 drafts, kept for provenance
```

| Document | Purpose |
|---|---|
| [`docs/requirements/SRS_async_multiagent_ai_coding_system.md`](docs/requirements/SRS_async_multiagent_ai_coding_system.md) | **System Requirements Specification, v1.3.** Specifies *what* the system must do and under what constraints. Implementation choices are deliberately left open. Changes here are governed — see the changelog at the top. |
| [`docs/design/unified_design_spec.md`](docs/design/unified_design_spec.md) | **Unified Technical Design Specification, v1.0.0.** The current design. Resolves every requirement in the SRS, records ten ADRs with their rejected alternatives, maps each FR/NFR to a section, and lists the weaknesses that were consciously accepted. |
| [`docs/design/proposals/`](docs/design/proposals/) | The three independent Phase 1 drafts. **Superseded — do not build from these.** |

## How the design was produced

Three LLM agents wrote independent blind drafts, cross-reviewed each other's pull requests, and argued to consensus over five rounds on [Issue #1](https://github.com/huozhe/code-workflow/issues/1). The unified spec is not any one draft with the others merged in — several sections replace what their drafter originally proposed, and §1.2 attributes each one.

Nine defects were found across the three drafts during review. Every one was found by someone other than its author.

## Status

Design complete and approved. Implementation has not started; the roadmap is §21 of the unified spec, beginning at M0 (host bootstrap).
