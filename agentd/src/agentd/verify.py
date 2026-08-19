"""Gateway-side privileged-transition verification (§8.4) + M4-A ruleset assert."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from agentd.github_fetch import fetch_pr_review_threads

log = logging.getLogger("agentd.verify")


@dataclass(frozen=True)
class ApprovalCheck:
    ok: bool
    reason: str
    # True → timing/compute artefact: leave deferred and retry (PR #54 B1).
    # False → permanent fault: escalate now.
    transient: bool = False


@dataclass(frozen=True)
class BranchRulesCheck:
    """Read-side half of M4-A / FR-1.3 (repository ruleset, not classic protection).

    ``GET /repos/{owner}/{repo}/rules/branches/{branch}`` needs no admin.
    Classic ``/branches/{branch}/protection`` still 404s without admin.
    """

    ok: bool
    reason: str
    required_approving_review_count: int | None = None
    require_last_push_approval: bool | None = None
    dismiss_stale_reviews_on_push: bool | None = None
    required_review_thread_resolution: bool | None = None
    has_required_status_checks: bool = False


# Wire shape of PUT /pulls/{n}/merge when ruleset blocks (M4-3 live, PR #55):
# HTTP 405, body.status "405", message starts with "Repository rule violations found".
# Self-approve as PR author: POST /pulls/{n}/reviews → HTTP 422
# "Review Can not approve your own pull request".
MERGE_RULE_REFUSAL_HTTP = 405
SELF_APPROVE_REFUSAL_HTTP = 422


def verify_branch_pull_request_rules(
    *,
    repo: str,
    branch: str,
    token: str | None,
    min_approving_reviews: int = 1,
    http_get: Callable[..., Any] | None = None,
) -> BranchRulesCheck:
    """Assert FR-1.3 *settings* from the ruleset (M4-3 assert half).

    Looks for a ``pull_request`` rule with
    ``required_approving_review_count >= min_approving_reviews``.
    Does **not** prove enforcement — pair with a live merge refusal (observe half).
    """
    if not token:
        return BranchRulesCheck(False, "no token for GitHub rules API")
    if not repo or not branch:
        return BranchRulesCheck(False, "repo and branch required")

    get = http_get or _gh_get
    url = f"https://api.github.com/repos/{repo}/rules/branches/{branch}"
    try:
        rules = get(url, token=token)
    except Exception as exc:  # noqa: BLE001
        return BranchRulesCheck(False, f"GitHub rules API error: {exc}")

    if not isinstance(rules, list):
        return BranchRulesCheck(
            False, f"rules response is not a list: {type(rules).__name__}"
        )

    pr_rule: dict[str, Any] | None = None
    has_status_checks = False
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rtype = str(rule.get("type") or "")
        if rtype == "pull_request":
            pr_rule = rule
        elif rtype == "required_status_checks":
            has_status_checks = True

    if pr_rule is None:
        return BranchRulesCheck(
            False,
            f"no pull_request rule on {repo}@{branch}",
            has_required_status_checks=has_status_checks,
        )

    params = pr_rule.get("parameters") or {}
    if not isinstance(params, dict):
        params = {}
    try:
        count = int(params.get("required_approving_review_count") or 0)
    except (TypeError, ValueError):
        count = 0
    last_push = params.get("require_last_push_approval")
    dismiss = params.get("dismiss_stale_reviews_on_push")
    thread_res = params.get("required_review_thread_resolution")

    if count < min_approving_reviews:
        return BranchRulesCheck(
            False,
            f"required_approving_review_count={count} < {min_approving_reviews}",
            required_approving_review_count=count,
            require_last_push_approval=(
                bool(last_push) if last_push is not None else None
            ),
            dismiss_stale_reviews_on_push=(
                bool(dismiss) if dismiss is not None else None
            ),
            required_review_thread_resolution=(
                bool(thread_res) if thread_res is not None else None
            ),
            has_required_status_checks=has_status_checks,
        )

    return BranchRulesCheck(
        True,
        (
            f"pull_request rule ok: required_approving_review_count={count}"
            f" (>= {min_approving_reviews}); "
            f"require_last_push_approval={last_push!r}; "
            f"required_status_checks={'yes' if has_status_checks else 'no'}"
        ),
        required_approving_review_count=count,
        require_last_push_approval=bool(last_push) if last_push is not None else None,
        dismiss_stale_reviews_on_push=bool(dismiss) if dismiss is not None else None,
        required_review_thread_resolution=(
            bool(thread_res) if thread_res is not None else None
        ),
        has_required_status_checks=has_status_checks,
    )


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
    fetch_threads: Callable[..., Any] | None = None,
) -> ApprovalCheck:
    """§8.4 Feature PR merge authorization (M4-2).

    All three conditions:

    1. APPROVED from expected approver (Architect) on current head SHA
    2. Every ``required_checks`` entry is ``success``
    3. ``mergeable_state == "clean"``

    Gateway verifies only — it does **not** merge (ADR-8). On success the
    design loop emits ``merge_authorized`` so the **Developer** acts.

    Failures are classified (PR #54 B1 / #38 split, ADR-26):

    - **transient** (``transient=True``): GitHub still computing mergeability
      (``unknown`` / null), or ``blocked`` with no unresolved review threads
      while checks are pending / unset — caller must leave the delivery
      deferred and retry.
    - **permanent** (``transient=False``): wrong approver, stale head,
      ``dirty``, a required check that has concluded ``failure``, or
      ``blocked`` with ``unresolved_count > 0``.
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
        # Network blip — retry rather than page the owner.
        return ApprovalCheck(
            False, f"GitHub API error (transient): {exc}", transient=True
        )

    approval = _check_approver_on_head(
        reviews=reviews,
        pr=pr,
        expected_approver_login=expected_approver_login,
        head_sha=head_sha,
    )
    if not approval.ok:
        # Approver / stale-head faults are permanent.
        return ApprovalCheck(False, approval.reason, transient=False)

    live_head = str((pr.get("head") or {}).get("sha") or head_sha)
    wanted = [str(c) for c in (required_checks or []) if str(c).strip()]

    checks_pending = False
    checks_all_success = True
    checks_reason = ""
    if wanted:
        try:
            chk = _classify_required_checks(
                get=get,
                repo=repo,
                head_sha=live_head,
                token=token,
                required_checks=wanted,
            )
        except Exception as exc:  # noqa: BLE001
            return ApprovalCheck(
                False, f"GitHub API error (checks, transient): {exc}", transient=True
            )
        if chk.permanent_fail:
            return ApprovalCheck(False, chk.reason, transient=False)
        checks_pending = chk.pending
        checks_all_success = chk.all_success
        checks_reason = chk.reason
        if not checks_all_success and not checks_pending:
            return ApprovalCheck(False, chk.reason, transient=False)

    # mergeable_state is async — unknown/null right after review is normal.
    raw_ms = pr.get("mergeable_state")
    mergeable_state = "" if raw_ms is None else str(raw_ms).lower()
    if mergeable_state in ("", "unknown", "unstable"):
        label = mergeable_state or "null"
        return ApprovalCheck(
            False,
            f"mergeable_state still computing ({label!r}) — will retry",
            transient=True,
        )
    if mergeable_state == "dirty":
        return ApprovalCheck(
            False,
            "mergeable_state is 'dirty' (permanent: conflicts or unmergeable)",
            transient=False,
        )
    if mergeable_state == "blocked":
        # ADR-26: unresolved threads outrank checks. Fetch only on blocked.
        unresolved = _observe_unresolved_threads(
            repo=repo,
            pr_number=pr_number,
            token=token,
            fetch_threads=fetch_threads,
        )
        if unresolved is not None and unresolved > 0:
            return ApprovalCheck(
                False,
                (
                    f"mergeable_state is 'blocked': {unresolved} "
                    "unresolved review thread(s) — resolve before merge"
                ),
                transient=False,
            )
        if checks_pending or not wanted:
            # Checks still running, or no named checks yet — not ready.
            detail = checks_reason or "settling"
            return ApprovalCheck(
                False,
                f"mergeable_state is 'blocked' ({detail}) — will retry",
                transient=True,
            )
        if checks_all_success:
            return ApprovalCheck(
                False,
                "mergeable_state is 'blocked' with required checks already success "
                "(permanent: branch protection or review rule)",
                transient=False,
            )
        return ApprovalCheck(
            False,
            "mergeable_state is 'blocked' — will retry",
            transient=True,
        )
    if mergeable_state != "clean":
        # behind / draft / other — not a compute race; treat as permanent.
        return ApprovalCheck(
            False,
            f"mergeable_state is {mergeable_state!r}, want 'clean' (permanent)",
            transient=False,
        )

    if wanted and checks_pending:
        return ApprovalCheck(
            False,
            f"{checks_reason} — will retry",
            transient=True,
        )
    if wanted and not checks_all_success:
        return ApprovalCheck(False, checks_reason or "required_checks not all success")

    if wanted:
        return ApprovalCheck(
            True,
            "approved on current head; required_checks success; mergeable_state=clean",
        )
    return ApprovalCheck(
        True,
        "approved on current head; mergeable_state=clean; no required_checks configured",
    )


@dataclass(frozen=True)
class _CheckClass:
    all_success: bool
    pending: bool
    permanent_fail: bool
    reason: str



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


# Commit-status / check-run states that mean "still running" (retry).
_PENDING_CHECK_STATES = frozenset(
    {
        "pending",
        "queued",
        "in_progress",
        "waiting",
        "requested",
        "expected",
    }
)
# Concluded non-success (escalate).
_FAILED_CHECK_STATES = frozenset(
    {
        "failure",
        "error",
        "cancelled",
        "timed_out",
        "action_required",
        "startup_failure",
        "stale",
        "neutral",  # not success for required gates
    }
)


def _observe_unresolved_threads(
    *,
    repo: str,
    pr_number: int,
    token: str | None,
    fetch_threads: Callable[..., Any] | None,
) -> int | None:
    """Unresolved review-thread count, or None if the fetch is not a verdict.

    ``None`` is *not* zero: a failed or empty observation falls through to
    the pre-existing checks classification (ADR-26). ``fetch_pr_review_threads``
    already returns None on error; the injectable hook may also raise.
    """
    try:
        if fetch_threads is not None:
            snap = fetch_threads(repo=repo, pr_number=pr_number, token=token)
        else:
            snap = fetch_pr_review_threads(
                repo=repo, pr_number=pr_number, token=token
            )
    except Exception:  # noqa: BLE001
        return None
    if snap is None:
        return None
    try:
        return int(snap.unresolved_count)
    except (AttributeError, TypeError, ValueError):
        return None


def _classify_required_checks(
    *,
    get: Callable[..., Any],
    repo: str,
    head_sha: str,
    token: str,
    required_checks: list[str],
) -> _CheckClass:
    """Classify required checks: all success / pending / permanent failure."""
    check_runs_payload = get(
        f"https://api.github.com/repos/{repo}/commits/{head_sha}/check-runs",
        token=token,
    )
    status_payload = get(
        f"https://api.github.com/repos/{repo}/commits/{head_sha}/status",
        token=token,
    )

    by_name: dict[str, str] = {}
    runs = (
        check_runs_payload.get("check_runs")
        if isinstance(check_runs_payload, dict)
        else None
    )
    if isinstance(runs, list):
        for run in runs:
            name = str(run.get("name") or "")
            if not name:
                continue
            if str(run.get("status") or "") != "completed":
                by_name[name] = str(run.get("status") or "pending").lower()
            else:
                by_name[name] = str(run.get("conclusion") or "").lower()

    statuses = (
        status_payload.get("statuses") if isinstance(status_payload, dict) else None
    )
    if isinstance(statuses, list):
        for st in statuses:
            ctx = str(st.get("context") or "")
            if ctx:
                by_name[ctx] = str(st.get("state") or "").lower()

    missing: list[str] = []
    pending: list[str] = []
    failed: list[str] = []
    for name in required_checks:
        state = by_name.get(name)
        if state is None:
            missing.append(name)
        elif state == "success":
            continue
        elif state in _PENDING_CHECK_STATES:
            pending.append(f"{name}={state}")
        elif state in _FAILED_CHECK_STATES or state != "success":
            failed.append(f"{name}={state}")

    if failed:
        return _CheckClass(
            all_success=False,
            pending=False,
            permanent_fail=True,
            reason="required_checks not success: " + ", ".join(failed),
        )
    # Missing often means not reported yet → treat as pending (retry).
    if missing or pending:
        parts = []
        if missing:
            parts.append("missing/not-yet-reported: " + ", ".join(missing))
        if pending:
            parts.append("pending: " + ", ".join(pending))
        return _CheckClass(
            all_success=False,
            pending=True,
            permanent_fail=False,
            reason="required_checks still settling (" + "; ".join(parts) + ")",
        )
    return _CheckClass(
        all_success=True,
        pending=False,
        permanent_fail=False,
        reason="required_checks all success",
    )


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
