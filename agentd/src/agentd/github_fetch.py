"""Gateway reads from GitHub for stall observation (§9.3) — not agent turns."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

import httpx

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
