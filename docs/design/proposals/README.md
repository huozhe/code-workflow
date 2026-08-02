# Phase 1 Design Proposals — superseded, retained for provenance

**These documents are not current.** They are the three independent blind drafts written in Phase 1 of Issue #1, before any cross-review. Every one of them contains decisions that were overturned, and several contain defects that were found and fixed during Phase 3.

The current specification is [`../unified_design_spec.md`](../unified_design_spec.md).

| Proposal | Author | PR | Baseline |
|---|---|---|---|
| [`claude_design_spec.md`](claude_design_spec.md) | Claude Agent (`@huozheclaude`) | #4, closed unmerged | SRS v1.2 |
| [`grok_design_spec.md`](grok_design_spec.md) | Grok Agent (`@huozhegrok`) | #2, closed unmerged | SRS v1.2 |
| [`gemini_design_spec.md`](gemini_design_spec.md) | Gemini Agent (`@tootooliu`) | #3, closed unmerged | SRS v1.2 |

## Why they are kept

§1.2 of the unified spec attributes every section to whichever proposal it came from, and §16 records rejected alternatives. That traceability is only meaningful if the sources are readable. These files previously existed solely on the `design/claude`, `design/grok`, and `design/gemini` branches, where any routine branch cleanup would have silently destroyed them.

## Reading them safely

- They target **SRS v1.2**. The SRS is now **v1.3** — NFR-1.1 was split into 1.1a/1.1b after the owner ruled that FileVault stays enabled. Internal references in these files to `plans/design/…` and to v1.2 are left unedited on purpose: they are historical records, and rewriting them would falsify what was actually proposed.
- Known defects in these drafts, each found by someone other than its author, are catalogued in the Phase 3 discussion on Issue #1 and reflected in the unified spec's ADRs.

Do not build from these. Do not cite them as design decisions.
