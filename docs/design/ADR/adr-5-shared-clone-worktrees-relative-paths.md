# ADR-5: Shared Clone + Worktrees, Relative Paths

*A decision of record for `agentd`. Index and context: [unified design spec §16](../unified_design_spec.md#16-architecture-decision-records).*


One full clone per repository plus `git worktree add` per session-role.

*Rejected: per-issue clone.* Four concurrent issues on one repo means four object stores. It satisfies the SRS literally ("no re-clone between event interactions") but pays per issue instead of per repo, and it makes the Architect fetch again to review a PR head.

*Rejected: `--depth` shallow clone.* Breaks rebase-onto-main, removes the history an Architect needs for review context, and complicates pushes. `--filter=blob:none` gets most of the size win with none of these failure modes if size ever becomes the concern.

Worktree paths are relative (`worktree.useRelativePaths`, git ≥ 2.48 pinned in the image), which removes the host/container path-equality dependency that an earlier draft carried.
