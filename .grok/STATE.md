# Session State — agentd / code-workflow

**Date**: 2026-08-08

## Current Objective
M3 design loop (#13) in four slices (Architect scope). Long-lived CLI (#25/#26) is on main.

## Next Concrete Step
Wait for Architect review on **PR #27 (M3-A)**. After merge: **M3-B** zero-thread-progress stall signal (§9.3), then M3-C artifact.register, M3-D live exit demo.

## Key Context to Load
- `main` has #26 (`af7a68e`); branch `feat/m3a-escalation-roundtrip` @ PR **#27**
- M3-A code: `github_write.py`, `design_loop._escalate` / `_resume_from_escalation`, schema **v3** `resume_state`
- #13 comment "M3 scoping — four PRs" is the checklist; OQ-4 RESOLVED
- Roles: `@huozhegrok` implements, `@huozheclaude` reviews; **Developer merges** after approve

## Important Decisions & Invariants
- Escalation is gateway's **first GitHub write** (not agent PAT path) — keep narrow
- Pre-pause state in `sessions.resume_state`; owner reply restores it
- Supervisor path for privilege demos; bare docker run is not faithful
- Spec is contract; amend same PR when needed

## Open Risks / Things to Watch
- Host M3-A exit: real issue comment needs daemon restart (ops: 3 merges behind) + token
- Tailscale stopped — M3-D webhook demos blocked until ingress back (do not `tailscale logout`)
- M3-B: do not touch fingerprint arming guard / head_sha in hash

## Verification
```bash
gh pr view 27
agentd/.venv/bin/python -m pytest agentd/tests -q
```
