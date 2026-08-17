"""ADR-24 / #106: defuse closing keywords aimed at session issues."""

from __future__ import annotations

from agentd.closing_keywords import defuse_closing_keywords

_NEAR_MISS = (
    "Amends **ADR-21** so the closed-issue marker re-arms on the "
    "**reopen event**, not on a sweep observation. Closes the wording "
    "half of **#118**."
)


def test_closes_session_issue_becomes_refs() -> None:
    out = defuse_closing_keywords(
        "Closes #32", session_issues={32}, repo="huozhe/code-workflow"
    )
    assert out == "Refs #32"


def test_list_marker_and_period_stay() -> None:
    out = defuse_closing_keywords(
        "- Closes #32.", session_issues={32}, repo="huozhe/code-workflow"
    )
    assert out == "- Refs #32."


def test_fixes_and_resolves_are_rewritten() -> None:
    assert (
        defuse_closing_keywords(
            "Fixes #32", session_issues={32}, repo="huozhe/code-workflow"
        )
        == "Refs #32"
    )
    assert (
        defuse_closing_keywords(
            "Resolved #32", session_issues={32}, repo="huozhe/code-workflow"
        )
        == "Refs #32"
    )


def test_near_miss_phrasing_is_untouched() -> None:
    assert (
        defuse_closing_keywords(
            _NEAR_MISS, session_issues={118}, repo="huozhe/code-workflow"
        )
        is None
    )


def test_non_session_issue_is_untouched() -> None:
    assert (
        defuse_closing_keywords(
            "Closes #57", session_issues={32, 81}, repo="huozhe/code-workflow"
        )
        is None
    )


def test_session_issue_without_keyword_is_untouched() -> None:
    assert (
        defuse_closing_keywords(
            "See #32", session_issues={32}, repo="huozhe/code-workflow"
        )
        is None
    )


def test_qualified_and_url_refs() -> None:
    assert (
        defuse_closing_keywords(
            "close huozhe/code-workflow#32",
            session_issues={32},
            repo="huozhe/code-workflow",
        )
        == "Refs huozhe/code-workflow#32"
    )
    assert (
        defuse_closing_keywords(
            "fix https://github.com/huozhe/code-workflow/issues/32",
            session_issues={32},
            repo="huozhe/code-workflow",
        )
        == "Refs https://github.com/huozhe/code-workflow/issues/32"
    )


def test_other_repo_is_untouched() -> None:
    assert (
        defuse_closing_keywords(
            "Closes other/repo#32",
            session_issues={32},
            repo="huozhe/code-workflow",
        )
        is None
    )
