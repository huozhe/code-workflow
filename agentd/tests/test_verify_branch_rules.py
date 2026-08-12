"""M4-3: assert half of M4-A — ruleset readable without admin."""

from __future__ import annotations

from agentd.verify import verify_branch_pull_request_rules


def _rules(
    *,
    count: int = 1,
    last_push: bool = True,
    with_status: bool = False,
) -> list:
    out: list = [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
        {
            "type": "pull_request",
            "parameters": {
                "required_approving_review_count": count,
                "dismiss_stale_reviews_on_push": True,
                "required_review_thread_resolution": True,
                "require_last_push_approval": last_push,
                "require_code_owner_review": False,
            },
        },
    ]
    if with_status:
        out.append(
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "ci"}]},
            }
        )
    return out


def test_branch_rules_ok_min_one_approving() -> None:
    def fake_get(url: str, *, token: str):
        assert "rules/branches/main" in url
        return _rules(count=1, last_push=True)

    c = verify_branch_pull_request_rules(
        repo="huozhe/code-workflow",
        branch="main",
        token="tok",
        http_get=fake_get,
    )
    assert c.ok is True
    assert c.required_approving_review_count == 1
    assert c.require_last_push_approval is True
    assert c.has_required_status_checks is False
    assert "required_approving_review_count=1" in c.reason


def test_branch_rules_fails_when_count_zero() -> None:
    def fake_get(url: str, *, token: str):
        return _rules(count=0)

    c = verify_branch_pull_request_rules(
        repo="o/r", branch="main", token="tok", http_get=fake_get
    )
    assert c.ok is False
    assert c.required_approving_review_count == 0
    assert "required_approving_review_count=0" in c.reason


def test_branch_rules_fails_when_no_pull_request_rule() -> None:
    def fake_get(url: str, *, token: str):
        return [{"type": "deletion"}, {"type": "non_fast_forward"}]

    c = verify_branch_pull_request_rules(
        repo="o/r", branch="main", token="tok", http_get=fake_get
    )
    assert c.ok is False
    assert "no pull_request rule" in c.reason


def test_branch_rules_detects_required_status_checks() -> None:
    def fake_get(url: str, *, token: str):
        return _rules(with_status=True)

    c = verify_branch_pull_request_rules(
        repo="o/r", branch="main", token="tok", http_get=fake_get
    )
    assert c.ok is True
    assert c.has_required_status_checks is True


def test_branch_rules_no_token() -> None:
    c = verify_branch_pull_request_rules(repo="o/r", branch="main", token=None)
    assert c.ok is False
    assert "no token" in c.reason


def test_branch_rules_api_error() -> None:
    def fake_get(url: str, *, token: str):
        raise RuntimeError("boom")

    c = verify_branch_pull_request_rules(
        repo="o/r", branch="main", token="tok", http_get=fake_get
    )
    assert c.ok is False
    assert "boom" in c.reason
