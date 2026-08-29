# ADR-14: `agentctl review-stats` — Turns-per-Review Measurement

*A decision of record for `agentd`. Index and context: [unified design spec §16](../unified_design_spec.md#16-architecture-decision-records).*


*Resolves #49's outstanding checklist item.* The review-coalescing mechanism (`_REVIEW_PART_EVENTS`, `ba61eb1` / PR #52) is code-complete and tested: `pull_request_review_comment` and `pull_request_review_thread` deliveries are marked `done` without a turn, and only `pull_request_review.submitted` dispatches one. What is not done is the re-measurement the issue's exit condition asks for, and re-measuring by hand-writing a `SELECT` against columns that did not exist yet when it was sketched is the same kind of one-off instrument the delivery-requeue problem already showed is worth not repeating. `agentctl review-stats` turns that ad hoc query into a command.

**Grouping key: `comment.pull_request_review_id`, not delivery timestamp proximity.** GitHub's `pull_request_review_comment` webhook payload carries `comment.pull_request_review_id`, linking each inline comment to its parent review — confirmed in the coalescing tests (`test_review_coalesce.py:109`) and matching GitHub's documented schema. That is the join key between a review (`pull_request_review.submitted`, decompressed for `review.id`) and its inline comments (`pull_request_review_comment`, decompressed for `comment.pull_request_review_id`).

**Session membership: `deliveries.repo` + `issue_num IN (sessions.issue_num, sessions.design_pr, sessions.feature_pr)`, not `issue_num = sessions.issue_num`.** `deliveries.issue_num` is the webhook's own issue-or-PR number — ingress writes `issue.number` when present, else `pull_request.number` (`server.py:218–221`) — and `deliveries` carries no `session_key`. Review traffic lands on the *PR*, not the session issue, so the naive filter drops every `pull_request_review`, `pull_request_review_comment`, and `pull_request_review_thread` row this command exists to measure. A delivery belongs to a session iff:

```
deliveries.repo = sessions.repo
AND deliveries.issue_num IN (
      sessions.issue_num,
      sessions.design_pr,     -- NULL does not match an IN list member
      sessions.feature_pr
    )
```

This is the same predicate for review deliveries, `thread_events`, and `issue_comment_created` — the amplifier comments also live on the PR. `turns` already carries `session_key` directly and needs no such join.

**`pull_request_review_thread` events are not attributable to one review_id — reported at PR/session level, not folded into a review's count.** The `pull_request_review_thread` webhook payload (`resolved`/`unresolved`) carries only `thread.id` and `thread.is_resolved`, no review linkage (`test_review_coalesce.py:129`). A thread can accumulate comments across more than one review round, so attributing a resolve event to "the" review it belongs to would be a guess dressed as data. `review-stats` reports these as a session-level `thread_events` total (via the membership predicate above), not distributed across reviews.

**No new column — decompress at query time, scoped by session.** `deliveries.payload` already holds everything needed (zlib-compressed JSON, `db.decompress_payload`); adding a `review_id` column would mean a schema bump (v7 → v8) and a backfill decision for rows written before the column existed, for a command that runs on demand against a bounded, session-scoped row set — not a hot path. `turns.public_actions` (schema v6, #39) already answers "was this turn empty" directly; no new state there either. This is a query and a report, not new persistence, matching the issue's own framing of the ask.

**Two grains, not one: per-review turn count vs. session-wide `totals`.** These answer different questions and must not be collapsed into a single join.

- *Per-review* `turns_woken` / `turns_empty` is the coalescing check ("did N inline comments still produce more than one turn?"). It joins `turns.delivery_id` to the routed `pull_request_review` delivery's `delivery_id` — the FK `insert_turn` already records at dispatch (`design_loop.py`, `_dispatch_turn`). After #52 this is normally 0 or 1 turn per review; more than 1 means a redelivery or a pre-#52 regression.
- *Session-wide* `totals` is the #47-baseline comparison (19 turns, 10 empty) and must come from `turns WHERE session_key = …` directly, with **no** review join — the baseline counts every turn the session produced, including `issue_comment` amplifier turns and non-review turns (`pull_request.opened`, `.synchronize`, …), which a per-review rollup would silently drop:

```
totals: { turns: <int>, empty: <int> }
empty := public_actions IN ('[]', '', NULL)
```

`totals` is the number pasted into the verification block; per-review counts are the diagnostic for *why* it moved.

**Inline comments with no matching review are counted, not dropped.** The left side of the per-review join is `pull_request_review.submitted`; a `pull_request_review_comment` whose `pull_request_review_id` has no such parent delivery (the residual risk the issue itself named: a threaded reply or standalone comment path that does not emit `review.submitted`) would otherwise vanish from the report with no signal. Session-level `unmatched_inline_comments` counts these explicitly, so the instrument can see that failure mode rather than silently under-reporting.

**Output is JSON, scoped by `--session`, defaulting to all sessions.** `agentctl review-stats [--session <key>]` follows `write-verification`'s `_resolve_session` (accepts `repo#issue` or a bare issue number) and `status`/`sessions`'s JSON convention. Shape — a `sessions` array regardless of whether `--session` narrows it to one entry, so callers never special-case the single-session case:

```json
{
  "sessions": [
    {
      "session_key": "huozhe/code-workflow#49",
      "reviews": [
        {
          "review_id": 9001,
          "pr": 82,
          "state": "changes_requested",
          "inline_comments": 7,
          "turn_id": "t-…",
          "turns_woken": 1,
          "turns_empty": 0
        }
      ],
      "thread_events": 7,
      "issue_comment_created": 7,
      "unmatched_inline_comments": 0,
      "totals": { "turns": 19, "empty": 10 }
    }
  ]
}
```

Review rows are exactly the deliveries with `event = pull_request_review AND action = submitted` for the session (per the membership predicate above); `state` is GitHub's `review.state` (`approved` / `changes_requested` / `commented`), not delivery status. `turn_id` is the single routed turn's id, or `null` if the review delivery never dispatched (e.g. still `queued`/`deferred`); `turns_woken` is a count, not a boolean, so a redelivery is visible rather than masked.

*Out of scope, by the issue:* no change to the coalescing mechanism itself (#49's code half, done in #52) or to §9.4's digest format; no fix for the `issue_comment.created` "reply once per review" amplifier — `review-stats` exposes the count, and whether it is real and worth a role-card change is a decision for after the live M4-4 measurement, not for this command; no change to the silent-turn thresholds (#42).
