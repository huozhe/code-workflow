"""Gateway-side privileged-transition verification (§8.4) — design half."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

import httpx

log = logging.getLogger("agentd.verify")


@dataclass(frozen=True)
class ApprovalCheck:
    ok: bool
    reason: str


def verify_design_approval(
    *,
    repo: str,
    pr_number: int,
    expected_approver_login: str,
    head_sha: str | None,
    token: str | None,
    http_get: Callable[..., Any] | None = None,
) -> ApprovalCheck:
    """§8.4 design-PR gate: APPROVED from the *other* role on the current head.

    Full required-checks + mergeable_state clean is M4. Here we only gate
    DESIGN_REVIEW → DESIGN_APPROVED.
    """
    if not token:
        return ApprovalCheck(False, "no token for GitHub API verification")
    if not head_sha:
        return ApprovalCheck(False, "missing head_sha for approval verification")
    if not pr_number:
        return ApprovalCheck(False, "missing pr_number")

    get = http_get or _gh_get
    try:
        reviews = get(
            f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews",
            token=token,
        )
        pr = get(
            f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
            token=token,
        )
    except Exception as exc:  # noqa: BLE001
        return ApprovalCheck(False, f"GitHub API error: {exc}")

    current_head = (pr.get("head") or {}).get("sha") or head_sha
    if current_head != head_sha:
        # Prefer live head if webhook was stale
        head_sha = current_head

    # Latest review per user wins (GitHub returns chronological)
    latest: dict[str, dict[str, Any]] = {}
    if isinstance(reviews, list):
        for rev in reviews:
            user = ((rev.get("user") or {}).get("login") or "").lower()
            if user:
                latest[user] = rev

    want = expected_approver_login.lower()
    rev = latest.get(want)
    if not rev:
        return ApprovalCheck(
            False, f"no review from expected approver {expected_approver_login!r}"
        )
    if str(rev.get("state") or "").upper() != "APPROVED":
        return ApprovalCheck(
            False,
            f"latest review from {expected_approver_login} is {rev.get('state')!r}, not APPROVED",
        )
    # commit_id is the head the review was submitted against
    reviewed_sha = rev.get("commit_id") or rev.get("commitId")
    if reviewed_sha and reviewed_sha != head_sha:
        return ApprovalCheck(
            False,
            f"approval is on {reviewed_sha[:8]}… not current head {head_sha[:8]}…",
        )
    return ApprovalCheck(True, "approved on current head by other role")


def _gh_get(url: str, *, token: str) -> Any:
    with httpx.Client(timeout=20.0) as client:
        r = client.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "agentd",
            },
        )
        r.raise_for_status()
        return r.json()
