"""Gateway-owned GitHub writes (§8.5 escalation). Narrow surface — ADR-8."""

from __future__ import annotations

import logging
from typing import Any, Callable

import httpx

log = logging.getLogger("agentd.github_write")


def post_issue_comment(
    *,
    repo: str,
    issue_num: int,
    body: str,
    token: str | None,
    http_post: Callable[..., Any] | None = None,
) -> int:
    """POST /repos/{repo}/issues/{n}/comments. Returns comment id.

    This is the gateway's voice (escalation), not an agent acting with its PAT.
    """
    if not token:
        raise RuntimeError("no token for gateway GitHub write")
    if not repo or not issue_num:
        raise RuntimeError("repo and issue_num required for comment")
    if not body.strip():
        raise RuntimeError("empty comment body")

    url = f"https://api.github.com/repos/{repo}/issues/{int(issue_num)}/comments"
    post = http_post or _gh_post
    data = post(url, token=token, json_body={"body": body})
    cid = data.get("id") if isinstance(data, dict) else None
    if not cid:
        raise RuntimeError(f"GitHub comment response missing id: {data!r}")
    log.info("posted issue comment repo=%s issue=%s id=%s", repo, issue_num, cid)
    return int(cid)


def reopen_issue(
    *,
    repo: str,
    issue_num: int,
    token: str | None,
    http_patch: Callable[..., Any] | None = None,
) -> None:
    """PATCH issue state=open — undo a non-human close of a session issue (#36 / §10.3)."""
    if not token:
        raise RuntimeError("no token for gateway GitHub write")
    if not repo or not issue_num:
        raise RuntimeError("repo and issue_num required to reopen")

    url = f"https://api.github.com/repos/{repo}/issues/{int(issue_num)}"
    patch = http_patch or _gh_patch
    patch(url, token=token, json_body={"state": "open"})
    log.info("reopened issue repo=%s issue=%s", repo, issue_num)


def get_issue_body(
    *,
    repo: str,
    issue_num: int,
    token: str | None,
    http_get: Callable[..., Any] | None = None,
) -> str:
    """GET /repos/{repo}/issues/{n} → body string (may be empty)."""
    if not token:
        raise RuntimeError("no token for gateway GitHub read")
    if not repo or not issue_num:
        raise RuntimeError("repo and issue_num required to read issue")

    url = f"https://api.github.com/repos/{repo}/issues/{int(issue_num)}"
    get = http_get or _gh_get
    data = get(url, token=token)
    if not isinstance(data, dict):
        raise RuntimeError(f"GitHub issue response not an object: {data!r}")
    body = data.get("body")
    return body if isinstance(body, str) else ""


def patch_issue_body(
    *,
    repo: str,
    issue_num: int,
    body: str,
    token: str | None,
    http_patch: Callable[..., Any] | None = None,
) -> None:
    """PATCH issue body — gateway structural write (§10.1 verification block)."""
    if not token:
        raise RuntimeError("no token for gateway GitHub write")
    if not repo or not issue_num:
        raise RuntimeError("repo and issue_num required to patch issue body")
    if body is None:
        raise RuntimeError("issue body must be a string")

    url = f"https://api.github.com/repos/{repo}/issues/{int(issue_num)}"
    patch = http_patch or _gh_patch
    patch(url, token=token, json_body={"body": body})
    log.info("patched issue body repo=%s issue=%s bytes=%s", repo, issue_num, len(body))


def _gh_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "agentd",
    }


def _gh_get(url: str, *, token: str) -> Any:
    with httpx.Client(timeout=20.0) as client:
        r = client.get(url, headers=_gh_headers(token))
        r.raise_for_status()
        return r.json()


def _gh_post(url: str, *, token: str, json_body: dict[str, Any]) -> Any:
    with httpx.Client(timeout=20.0) as client:
        r = client.post(
            url,
            headers=_gh_headers(token),
            json=json_body,
        )
        r.raise_for_status()
        return r.json()


def _gh_patch(url: str, *, token: str, json_body: dict[str, Any]) -> Any:
    with httpx.Client(timeout=20.0) as client:
        r = client.patch(
            url,
            headers=_gh_headers(token),
            json=json_body,
        )
        r.raise_for_status()
        return r.json()


def format_escalation_comment(
    *,
    owner: str,
    session_key: str,
    state: str,
    role: str,
    reason: str,
) -> str:
    """§8.5 step 2: tag owner, question, state, what each answer causes."""
    owner_tag = owner if owner.startswith("@") else f"@{owner}"
    return (
        f"{owner_tag} — **agentd needs a decision** (session paused)\n\n"
        f"| | |\n|---|---|\n"
        f"| **Session** | `{session_key}` |\n"
        f"| **State when paused** | `{state}` |\n"
        f"| **Raised by** | `{role}` |\n\n"
        f"**Question / reason**\n\n{reason.strip()}\n\n"
        f"**What a reply does**\n\n"
        f"- **Any reply from you** — unpauses the session (returns to `{state}`), "
        f"closes this escalation, and injects your comment text into the next "
        f"agent turn for role `{role}` (or architect if system-raised).\n"
        f"- **No reply** — session stays `PAUSED_HUMAN`; non-owner bot activity is "
        f"deferred until you answer (P5: this comment is the non-silent signal).\n\n"
        f"<!-- agentd:escalation session={session_key} -->\n"
    )
