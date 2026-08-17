"""ADR-24: rewrite GitHub closing keywords aimed at session issues."""

from __future__ import annotations

import re

_KW = r"close[sd]?|fix(?:e[sd])?|resolve[sd]?"
_REF = (
    r"#\d+"
    r"|[\w.-]+/[\w.-]+#\d+"
    r"|https://github.com/[\w.-]+/[\w.-]+/issues/\d+"
)
_PAT = re.compile(
    rf"(?<![A-Za-z])({_KW})([ \t]+)({_REF})",
    re.IGNORECASE,
)
_HASH = re.compile(r"^#(\d+)$")
_QUAL = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")
_URL = re.compile(r"^https://github.com/([\w.-]+/[\w.-]+)/issues/(\d+)$", re.I)


def _issue_num(ref: str, repo: str) -> int | None:
    m = _HASH.fullmatch(ref)
    if m:
        return int(m.group(1))
    m = _QUAL.fullmatch(ref)
    if m:
        if m.group(1).lower() != repo.lower():
            return None
        return int(m.group(2))
    m = _URL.fullmatch(ref)
    if m:
        if m.group(1).lower() != repo.lower():
            return None
        return int(m.group(2))
    return None


def defuse_closing_keywords(
    body: str,
    *,
    session_issues: set[int],
    repo: str,
) -> str | None:
    """Rewrite closing keywords aimed at session issues. None if unchanged."""
    if not body or not session_issues:
        return None
    changed = False

    def repl(m: re.Match[str]) -> str:
        nonlocal changed
        n = _issue_num(m.group(3), repo)
        if n is None or n not in session_issues:
            return m.group(0)
        changed = True
        return f"Refs{m.group(2)}{m.group(3)}"

    out = _PAT.sub(repl, body)
    return out if changed else None
