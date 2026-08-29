# Architecture Decision Records

One file per decision. **The ADR is the decision of record** — where it and the spec's summary disagree, the ADR is right.

The one-paragraph index, with the context each decision sits in, is [unified design spec §16](../unified_design_spec.md#16-architecture-decision-records).

Several ADRs correct the issue they cite: read the ADR, not the issue.

| # | Decision |
|---|---|
| **1** | [Gateway in Python 3.12 + FastAPI](adr-1-gateway-python-fastapi.md) |
| **2** | [SQLite as a Derived Cache](adr-2-sqlite-derived-cache.md) |
| **3** | [JSON-RPC 2.0 over NDJSON on Loopback TCP + Bearer](adr-3-jsonrpc-over-ndjson.md) |
| **4** | [One Container per Project, Two OS UIDs](adr-4-container-per-project-os-uids.md) |
| **5** | [Shared Clone + Worktrees, Relative Paths](adr-5-shared-clone-worktrees-relative-paths.md) |
| **6** | [Tokens via Control Channel, Never via Bind Mount](adr-6-tokens-via-control-channel.md) |
| **7** | [Machine Users, Not a GitHub App](adr-7-machine-users-github-app.md) |
| **8** | [No GitHub API Proxy](adr-8-no-github-api-proxy.md) |
| **9** | [Runner Contract Is the Stable Surface](adr-9-runner-contract-stable-surface.md) |
| **10** | [Accept-and-Queue Ingress](adr-10-accept-queue-ingress.md) |
| **11** | [Identity Preflight on Token Delivery](adr-11-identity-preflight-token-delivery.md) |
| **12** | [Session Archive Format & Retention](adr-12-session-archive-format-retention.md) |
| **13** | [`agentctl version` — Package Metadata, No Config Load](adr-13-agentctl-version.md) |
| **14** | [`agentctl review-stats` — Turns-per-Review Measurement](adr-14-agentctl-review-stats.md) |
| **15** | [Verification-Block Restore Composes Against a Fresh Read](adr-15-verification-block-restore.md) |
| **16** | [Raising the Checkbox Requires an Observed Owner Tick](adr-16-checkbox-requires-owner-tick.md) |
| **17** | [A Ticked Body With No Observed Owner Tick Does Not Classify](adr-17-ticked-body-no-owner-tick.md) |
| **18** | [The Close-Reconcile Hold Is Scoped to the Closed Issue](adr-18-close-reconcile-hold-scope.md) |
| **19** | [The Mirror — Re-read the Body Rather Than Reverse the Ruling](adr-19-mirror-reread-the-body.md) |
| **20** | [M6-1a — the Local Half of §11.2, and What Its Steps Actually Mean](adr-20-reconcile-local-half.md) |
| **21** | [M6-1b — the Sweep, and the Membership Set It Needs First](adr-21-reconcile-sweep-membership.md) |
| **22** | [M6-1c — Interrupted Turns, and the Checkpoint That Was Already on Disk](adr-22-interrupted-turns-checkpoint.md) |
| **23** | [M6-2 — Garbage Collection, Narrowed to What It May Safely Touch](adr-23-garbage-collection-narrowed.md) |
| **24** | [The Closing Keyword — Defusing the Route That Calls No Close API](adr-24-closing-keyword-defuse.md) |
| **25** | [Lazy Promotion — Reaching a Stopped Runner Before Deciding It Is Gone](adr-25-lazy-promotion.md) |
| **26** | [`blocked` Is Not One State — Unresolved Threads Are Sticky, Checks-in-Flight Are Not](adr-26-blocked-unresolved-threads-sticky.md) |
| **27** | [In-Flight Turn IDs — the Reconciler's Missing Third State](adr-27-inflight-turn-ids.md) |
| **28** | [The In-Flight Set Has Two Dispatch Sites, and ADR-27 Wired One](adr-28-inflight-two-dispatch-sites.md) |
| **29** | [The Held Pipe Has No Turn Boundary — a Vendor `result` Frame Belongs to Whichever Turn Reads Next](adr-29-held-pipe-turn-boundary.md) |
| **30** | [An Agent's Own PR Is Not a Cue — Author-Sent Review and Comment Events Route Nowhere](adr-30-author-sent-pr-events.md) |
| **31** | [Two Defects Put Session Worktrees on the Wrong History, and the Issue Ranks Them Backwards](adr-31-worktree-base-history.md) |
| **32** | [The Gateway Asks Its Two Loop-Safety Questions of the Wrong Subject](adr-32-loop-safety-wrong-subject.md) |
| **33** | [The Quota Gate Assumes a Refusal Arrives Before Any Output, and the Delivery Pays for It](adr-33-quota-refusal-after-output.md) |
| **34** | [Step 7 Attaches and Records — the "Mark COLD" Clause Is Retired, and COLD Must Survive GC First](adr-34-probe-attaches-and-records.md) |
| **35** | [The Runner Cannot Signal Its Own CLIs — Kill as the Role, and Three Things That Follow](adr-35-kill-as-the-role.md) |
| **36** | [The Identity Boundary Is an Account Control, and the Demonstration That Was Meant to Prove It Is an Operator Claim](adr-36-identity-account-vs-operator.md) |
| **37** | [A Dropped `synchronize` Is Not an Adopt — Recover It as a Head-SHA Delivery](adr-37-dropped-synchronize-head-sha.md) |
| **38** | [A Withdrawn Approval Is Not an Unverifiable One — Supersede the Delivery, and Give `defer` a Clock](adr-38-superseded-approval-defer-clock.md) |
| **39** | [`design_loop.py` Owns the Whole Lifecycle — Rename It, and the Four Record Classes a Sweep Would Falsify](adr-39-session-loop-rename.md) |
| **40** | [A `*_pr_opened` the FSM Never Sees — Take It Unconditionally, and Discover the PR the Sweep Cannot Reach](adr-40-untracked-pr-opened.md) |
