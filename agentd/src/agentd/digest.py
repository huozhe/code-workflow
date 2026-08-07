"""Event digest — references, not inlined content (§9.4)."""

from __future__ import annotations

import json
from typing import Any


def build_digest(
    *,
    event: str,
    action: str | None,
    repo: str,
    issue_num: int | None,
    sender: str | None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compact digest for turn prompts."""
    d: dict[str, Any] = {
        "kind": f"{event}.{action}" if action else event,
        "repo": repo,
        "issue": issue_num,
        "actor": sender,
    }
    data = payload or {}
    if "pull_request" in data and isinstance(data["pull_request"], dict):
        pr = data["pull_request"]
        d["pr"] = pr.get("number")
        d["pr_url"] = pr.get("html_url")
        d["head_sha"] = (pr.get("head") or {}).get("sha")
        d["title"] = pr.get("title")
    if "issue" in data and isinstance(data["issue"], dict):
        d["issue_url"] = data["issue"].get("html_url")
        d["title"] = data["issue"].get("title")
        labels = data["issue"].get("labels") or []
        d["labels"] = [
            (x.get("name") if isinstance(x, dict) else str(x)) for x in labels
        ]
    if "comment" in data and isinstance(data["comment"], dict):
        d["comment_id"] = data["comment"].get("id")
        d["comment_url"] = data["comment"].get("html_url")
        # Reference only — do not inline body
        body = data["comment"].get("body") or ""
        d["comment_chars"] = len(body)
    if "review" in data and isinstance(data["review"], dict):
        d["review_state"] = data["review"].get("state")
        d["review_id"] = data["review"].get("id")
    return d


def digest_to_markdown(d: dict[str, Any]) -> str:
    return (
        "# Event digest\n\n```json\n"
        + json.dumps(d, indent=2)[:6000]
        + "\n```\n\n(Bodies not inlined — fetch via URL/id if needed.)\n"
    )
