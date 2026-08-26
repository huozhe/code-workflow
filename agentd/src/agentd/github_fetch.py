"""Gateway reads from GitHub for stall observation (§9.3) — not agent turns."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from agentd.digest import json_obj

log = logging.getLogger("agentd.github_fetch")

_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      baseRefName
      headRefOid
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
        }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class PrReviewThreadSnapshot:
    """Unresolved review threads + optional base for diff_stat."""

    open_thread_ids: list[str]
    unresolved_count: int
    all_thread_ids: list[str]
    base_ref: str | None = None
    head_oid: str | None = None


def fetch_pr_review_threads(
    *,
    repo: str,
    pr_number: int,
    token: str | None,
    http_post: Callable[..., Any] | None = None,
) -> PrReviewThreadSnapshot | None:
    """GraphQL reviewThreads — works with classic ``repo`` PAT.

    Returns None on missing token / HTTP / GraphQL errors (caller skips signals).
    """
    if not token or not repo or not pr_number:
        return None
    if "/" not in repo:
        return None
    owner, _, name = repo.partition("/")
    post = http_post or _gh_graphql
    try:
        data = post(
            "https://api.github.com/graphql",
            token=token,
            json_body={
                "query": _THREADS_QUERY,
                "variables": {
                    "owner": owner,
                    "name": name,
                    "number": int(pr_number),
                },
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "reviewThreads fetch failed repo=%s pr=%s: %s", repo, pr_number, exc
        )
        return None
    if not isinstance(data, dict):
        return None
    if data.get("errors"):
        log.warning(
            "reviewThreads GraphQL errors repo=%s pr=%s: %s",
            repo,
            pr_number,
            data["errors"],
        )
        return None
    pr = (
        ((data.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
    )
    nodes = ((pr.get("reviewThreads") or {}).get("nodes")) or []
    all_ids: list[str] = []
    open_ids: list[str] = []
    for n in nodes:
        if not isinstance(n, dict) or not n.get("id"):
            continue
        tid = str(n["id"])
        all_ids.append(tid)
        if not n.get("isResolved"):
            open_ids.append(tid)
    return PrReviewThreadSnapshot(
        open_thread_ids=open_ids,
        unresolved_count=len(open_ids),
        all_thread_ids=all_ids,
        base_ref=str(pr["baseRefName"]) if pr.get("baseRefName") else None,
        head_oid=str(pr["headRefOid"]) if pr.get("headRefOid") else None,
    )


def fetch_diff_stat(
    *,
    repo: str,
    base: str,
    head: str,
    token: str | None,
    http_get: Callable[..., Any] | None = None,
) -> str | None:
    """REST compare API — third fingerprint component (§9.3).

    Returns a stable string of total changes, or None if unavailable.
    """
    if not token or not repo or not base or not head:
        return None
    get = http_get or _gh_get
    url = f"https://api.github.com/repos/{repo}/compare/{base}...{head}"
    try:
        data = get(url, token=token)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "compare fetch failed repo=%s %s...%s: %s", repo, base[:8], head[:8], exc
        )
        return None
    if not isinstance(data, dict):
        return None
    # Prefer aggregate totals; fall back to per-file path+stats list.
    files = data.get("files") or []
    parts: list[str] = []
    for f in files:
        if not isinstance(f, dict):
            continue
        parts.append(
            f"{f.get('filename')}|{f.get('status')}|{f.get('additions')}+{f.get('deletions')}-"
        )
    if parts:
        return "\n".join(sorted(parts))
    # Empty compare is still a real observation (no file delta).
    return f"empty|{data.get('status') or 'identical'}|{data.get('ahead_by')}|{data.get('behind_by')}"


def _gh_graphql(url: str, *, token: str, json_body: dict[str, Any]) -> Any:
    with httpx.Client(timeout=30.0) as client:
        r = client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "agentd",
            },
            json=json_body,
        )
        r.raise_for_status()
        return r.json()


def _iso_to_epoch(raw: str) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        return None


def fetch_session_snapshot(
    *,
    repo: str,
    issue_num: int,
    feature_pr: int | None,
    design_pr: int | None,
    token: str | None,
    http_get: Callable[..., Any] | None = None,
) -> dict[str, Any] | None:
    """REST snapshot for the M6-1b sweep: issue + comments + known PRs."""
    if not token or not repo or not issue_num:
        return None
    get = http_get or _gh_get
    try:
        issue = get(f"https://api.github.com/repos/{repo}/issues/{int(issue_num)}", token=token)
        comments = get(
            f"https://api.github.com/repos/{repo}/issues/{int(issue_num)}/comments?per_page=100",
            token=token,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("sweep snapshot failed repo=%s issue=%s: %s", repo, issue_num, exc)
        return None
    if not isinstance(issue, dict):
        return None
    nodes: list[dict[str, Any]] = []
    if isinstance(comments, list):
        for c in comments:
            if not isinstance(c, dict) or not c.get("node_id"):
                continue
            nodes.append(
                {
                    "id": str(c["node_id"]),
                    "kind": "comment",
                    "created_at": _iso_to_epoch(str(c.get("created_at") or "")),
                    "author": str((c.get("user") or {}).get("login") or ""),
                    "body": str(c.get("body") or ""),
                }
            )

    def _add_pr(num: int | None) -> tuple[bool, str]:
        if not num:
            return False, ""
        try:
            pr = get(f"https://api.github.com/repos/{repo}/pulls/{int(num)}", token=token)
        except Exception as exc:  # noqa: BLE001
            log.warning("sweep PR fetch failed repo=%s pr=%s: %s", repo, num, exc)
            return False, ""
        if not isinstance(pr, dict) or not pr.get("node_id"):
            if not isinstance(pr, dict):
                return False, ""
            return bool(pr.get("merged")), str(json_obj(pr.get("head")).get("sha") or "")
        head = json_obj(pr.get("head"))
        head_ref = str(head.get("ref") or "")
        head_sha = str(head.get("sha") or "")
        pr_title = str(pr.get("title") or "")
        nodes.append(
            {
                "id": str(pr["node_id"]),
                "kind": "pull_request",
                "created_at": _iso_to_epoch(str(pr.get("created_at") or "")),
                "author": str((pr.get("user") or {}).get("login") or ""),
                "merged": bool(pr.get("merged")),
                "number": int(num),
                "head_ref": head_ref,
                "head_sha": head_sha,
                "title": pr_title,
            }
        )
        try:
            reviews = get(
                f"https://api.github.com/repos/{repo}/pulls/{int(num)}/reviews?per_page=100",
                token=token,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("sweep reviews fetch failed repo=%s pr=%s: %s", repo, num, exc)
            reviews = []
        if isinstance(reviews, list):
            for rev in reviews:
                if not isinstance(rev, dict) or not rev.get("node_id"):
                    continue
                if not rev.get("submitted_at"):
                    continue
                nodes.append(
                    {
                        "id": str(rev["node_id"]),
                        "kind": "review",
                        "created_at": _iso_to_epoch(str(rev.get("submitted_at") or "")),
                        "author": str((rev.get("user") or {}).get("login") or ""),
                        # #209: the *PR's* author, not the review's. ADR-30's
                        # guard reads pull_request.user.login, and a synthesized
                        # review that omits it short-circuits the guard.
                        "pr_author": str((pr.get("user") or {}).get("login") or ""),
                        "state": str(rev.get("state") or ""),
                        "number": int(num),
                        "head_ref": head_ref,
                        "title": pr_title,
                    }
                )
        return bool(pr.get("merged")), head_sha

    feature_merged, feature_head_sha = False, ""
    design_merged, design_head_sha = False, ""
    if feature_pr:
        feature_merged, feature_head_sha = _add_pr(int(feature_pr))
    if design_pr:
        design_merged, design_head_sha = _add_pr(int(design_pr))
    return {
        "issue_state": str(issue.get("state") or ""),
        "issue_body": issue.get("body") if isinstance(issue.get("body"), str) else "",
        "issue_node_id": str(issue.get("node_id") or ""),
        "issue_title": str(issue.get("title") or ""),
        "issue_html_url": str(issue.get("html_url") or ""),
        "issue_labels": list(issue.get("labels") or []),
        "nodes": nodes,
        "feature_merged": feature_merged,
        "design_merged": design_merged,
        "feature_head_sha": feature_head_sha,
        "design_head_sha": design_head_sha,
    }


def fetch_pull(
    *,
    repo: str,
    pr_number: int,
    token: str | None,
    http_get: Callable[..., Any] | None = None,
) -> dict[str, Any] | None:
    """REST GET /repos/{repo}/pulls/{n} — merged/state/head for ADR-30 (c) / ADR-37."""
    if not token or not repo or not pr_number:
        return None
    get = http_get or _gh_get
    try:
        pr = get(f"https://api.github.com/repos/{repo}/pulls/{int(pr_number)}", token=token)
    except Exception as exc:  # noqa: BLE001
        log.warning("pull fetch failed repo=%s pr=%s: %s", repo, pr_number, exc)
        return None
    if not isinstance(pr, dict):
        return None
    return {
        "merged": bool(pr.get("merged")),
        "state": str(pr.get("state") or ""),
        "head_sha": str(json_obj(pr.get("head")).get("sha") or "") or None,
    }


def _gh_get(url: str, *, token: str) -> Any:
    with httpx.Client(timeout=30.0) as client:
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
