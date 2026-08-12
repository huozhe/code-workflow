"""Gateway-side privileged-transition verification (§8.4)."""

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

    Design half only needs approver + head (M3). Full required_checks +
    mergeable_state clean is Feature-PR merge authorization (M4-2).
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

    return _check_approver_on_head(
        reviews=reviews,
        pr=pr,
        expected_approver_login=expected_approver_login,
        head_sha=head_sha,
    )


def verify_feature_merge(
    *,
    repo: str,
    pr_number: int,
    expected_approver_login: str,
    head_sha: str | None,
    token: str | None,
    required_checks: list[str] | None = None,
    http_get: Callable[..., Any] | None = None,
) -> ApprovalCheck:
    """§8.4 Feature PR merge authorization (M4-2).

    All three conditions:

    1. APPROVED from expected approver (Architect) on current head SHA
    2. Every ``required_checks`` entry is ``success``
    3. ``mergeable_state == "clean"``

    Gateway verifies only — it does **not** merge (ADR-8). On success the
    design loop emits ``merge_authorized`` so the **Developer** acts.
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

    approval = _check_approver_on_head(
        reviews=reviews,
        pr=pr,
        expected_approver_login=expected_approver_login,
        head_sha=head_sha,
    )
    if not approval.ok:
        return approval

    # Prefer live head after approval resolved it
    live_head = str((pr.get("head") or {}).get("sha") or head_sha)

    mergeable_state = str(pr.get("mergeable_state") or "").lower()
    if mergeable_state != "clean":
        return ApprovalCheck(
            False,
            f"mergeable_state is {mergeable_state!r}, want 'clean'",
        )

    wanted = [str(c) for c in (required_checks or []) if str(c).strip()]
    if not wanted:
        return ApprovalCheck(
            True,
            "approved on current head; mergeable_state=clean; no required_checks configured",
        )

    try:
        status_ok, status_reason = _required_checks_success(
            get=get,
            repo=repo,
            head_sha=live_head,
            token=token,
            required_checks=wanted,
        )
    except Exception as exc:  # noqa: BLE001
        return ApprovalCheck(False, f"GitHub API error (checks): {exc}")
    if not status_ok:
        return ApprovalCheck(False, status_reason)
    return ApprovalCheck(
        True,
        "approved on current head; required_checks success; mergeable_state=clean",
    )


def _check_approver_on_head(
    *,
    reviews: Any,
    pr: dict[str, Any],
    expected_approver_login: str,
    head_sha: str,
) -> ApprovalCheck:
    current_head = (pr.get("head") or {}).get("sha") or head_sha
    if current_head != head_sha:
        head_sha = str(current_head)

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
    reviewed_sha = rev.get("commit_id") or rev.get("commitId")
    if reviewed_sha and reviewed_sha != head_sha:
        return ApprovalCheck(
            False,
            f"approval is on {str(reviewed_sha)[:8]}… not current head {str(head_sha)[:8]}…",
        )
    return ApprovalCheck(True, "approved on current head by other role")


def _required_checks_success(
    *,
    get: Callable[..., Any],
    repo: str,
    head_sha: str,
    token: str,
    required_checks: list[str],
) -> tuple[bool, str]:
    """Every configured check name must be success (check-run or status context)."""
    check_runs_payload = get(
        f"https://api.github.com/repos/{repo}/commits/{head_sha}/check-runs",
        token=token,
    )
    status_payload = get(
        f"https://api.github.com/repos/{repo}/commits/{head_sha}/status",
        token=token,
    )

    by_name: dict[str, str] = {}
    runs = check_runs_payload.get("check_runs") if isinstance(check_runs_payload, dict) else None
    if isinstance(runs, list):
        for run in runs:
            name = str(run.get("name") or "")
            if not name:
                continue
            # conclusion is set when completed; treat pending as not success
            if str(run.get("status") or "") != "completed":
                by_name[name] = str(run.get("status") or "pending")
            else:
                by_name[name] = str(run.get("conclusion") or "").lower()

    statuses = status_payload.get("statuses") if isinstance(status_payload, dict) else None
    if isinstance(statuses, list):
        for st in statuses:
            ctx = str(st.get("context") or "")
            if ctx:
                by_name[ctx] = str(st.get("state") or "").lower()

    missing: list[str] = []
    failed: list[str] = []
    for name in required_checks:
        state = by_name.get(name)
        if state is None:
            missing.append(name)
        elif state != "success":
            failed.append(f"{name}={state}")
    if missing or failed:
        parts = []
        if missing:
            parts.append(f"missing: {', '.join(missing)}")
        if failed:
            parts.append(f"not success: {', '.join(failed)}")
        return False, "required_checks " + "; ".join(parts)
    return True, "required_checks all success"


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
