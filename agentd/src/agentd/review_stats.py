"""ADR-14: turns-per-review measurement for ``agentctl review-stats``.

Query and report only — no new schema. Groups inline comments onto
``pull_request_review.submitted`` via ``comment.pull_request_review_id``,
counts session-wide empty turns separately, and never guesses a review
for ``pull_request_review_thread`` events.
"""

from __future__ import annotations

import json
import zlib
from typing import Any

from agentd.db import Store, decompress_payload
from agentd.digest import json_obj


def session_issue_nums(sess: dict[str, Any]) -> list[int]:
    """Membership set: issue + design_pr + feature_pr, NULLs omitted."""
    nums: list[int] = []
    for key in ("issue_num", "design_pr", "feature_pr"):
        raw = sess.get(key)
        if raw is None or raw == "":
            continue
        try:
            n = int(raw)
        except (TypeError, ValueError):
            continue
        if n not in nums:
            nums.append(n)
    return nums


def _payload_dict(blob: bytes | None) -> dict[str, Any]:
    if not blob:
        return {}
    try:
        data = json.loads(decompress_payload(blob))
    except (OSError, ValueError, TypeError, zlib.error):
        return {}
    return data if isinstance(data, dict) else {}


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_empty_actions(public_actions: Any) -> bool:
    return public_actions in ("[]", "", None)


def collect_session_review_stats(store: Store, sess: dict[str, Any]) -> dict[str, Any]:
    """One session object in the ADR-14 ``sessions`` array."""
    session_key = str(sess["session_key"])
    repo = str(sess.get("repo") or "")
    issue_nums = session_issue_nums(sess)
    deliveries = store.list_deliveries_for(repo, issue_nums)
    turns = store.list_turns(session_key)

    turns_by_delivery: dict[str, list[dict[str, Any]]] = {}
    empty_turns = 0
    for turn in turns:
        if _is_empty_actions(turn.get("public_actions")):
            empty_turns += 1
        did = turn.get("delivery_id")
        if did:
            turns_by_delivery.setdefault(str(did), []).append(turn)

    submitted: list[dict[str, Any]] = []
    submitted_ids: set[int] = set()
    comment_review_ids: list[int | None] = []
    thread_events = 0
    issue_comment_created = 0

    for row in deliveries:
        event = row.get("event")
        action = row.get("action")
        if event == "pull_request_review" and action == "submitted":
            payload = _payload_dict(row.get("payload"))
            review = json_obj(payload.get("review"))
            pr_obj = json_obj(payload.get("pull_request"))
            rid = _as_int(review.get("id"))
            pr = _as_int(pr_obj.get("number"))
            if pr is None:
                pr = _as_int(row.get("issue_num"))
            state = str(review.get("state") or "").lower()
            if rid is not None:
                submitted_ids.add(rid)
            submitted.append(
                {
                    "delivery_id": str(row["delivery_id"]),
                    "review_id": rid,
                    "pr": pr,
                    "state": state,
                }
            )
        elif event == "pull_request_review_comment" and action == "created":
            payload = _payload_dict(row.get("payload"))
            comment = json_obj(payload.get("comment"))
            comment_review_ids.append(_as_int(comment.get("pull_request_review_id")))
        elif event == "pull_request_review_thread":
            thread_events += 1
        elif event == "issue_comment" and action == "created":
            issue_comment_created += 1

    comments_by_review: dict[int, int] = {}
    unmatched = 0
    for rid in comment_review_ids:
        if rid is None or rid not in submitted_ids:
            unmatched += 1
        else:
            comments_by_review[rid] = comments_by_review.get(rid, 0) + 1

    reviews_out: list[dict[str, Any]] = []
    for item in submitted:
        did = item["delivery_id"]
        tlist = list(turns_by_delivery.get(did, []))
        tlist.sort(key=lambda t: int(t.get("started_at") or 0))
        rid = item["review_id"]
        reviews_out.append(
            {
                "review_id": rid,
                "pr": item["pr"],
                "state": item["state"],
                "inline_comments": comments_by_review.get(rid, 0) if rid is not None else 0,
                "turn_id": tlist[0]["turn_id"] if tlist else None,
                "turns_woken": len(tlist),
                "turns_empty": sum(
                    1 for t in tlist if _is_empty_actions(t.get("public_actions"))
                ),
            }
        )

    return {
        "session_key": session_key,
        "reviews": reviews_out,
        "thread_events": thread_events,
        "issue_comment_created": issue_comment_created,
        "unmatched_inline_comments": unmatched,
        "totals": {"turns": len(turns), "empty": empty_turns},
    }


def collect_review_stats(
    store: Store, *, sessions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Top-level ADR-14 document: always a ``sessions`` array."""
    return {
        "sessions": [collect_session_review_stats(store, s) for s in sessions]
    }
