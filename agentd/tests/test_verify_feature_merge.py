"""M4-2: §8.4 Feature merge authorization (approver + checks + mergeable)."""

from __future__ import annotations

from agentd.verify import verify_feature_merge


def _ok_pr(head: str = "abc123", mergeable_state: str = "clean") -> dict:
    return {
        "number": 7,
        "head": {"sha": head},
        "mergeable_state": mergeable_state,
        "mergeable": True,
    }


def _ok_reviews(login: str = "huozheclaude", head: str = "abc123") -> list:
    return [
        {
            "user": {"login": login},
            "state": "APPROVED",
            "commit_id": head,
        }
    ]


def test_feature_merge_ok_full_gate() -> None:
    head = "abc123"

    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return _ok_reviews(head=head)
        if url.endswith("/pulls/7"):
            return _ok_pr(head=head)
        if url.endswith("/check-runs"):
            return {
                "check_runs": [
                    {
                        "name": "ci/test",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            }
        if url.endswith("/status"):
            return {"statuses": [], "state": "success"}
        raise AssertionError(url)

    c = verify_feature_merge(
        repo="o/r",
        pr_number=7,
        expected_approver_login="huozheclaude",
        head_sha=head,
        token="tok",
        required_checks=["ci/test"],
        http_get=fake_get,
    )
    assert c.ok is True
    assert "clean" in c.reason
    assert "required_checks" in c.reason or "success" in c.reason


def test_feature_merge_rejects_dirty_mergeable() -> None:
    head = "abc123"

    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return _ok_reviews(head=head)
        if url.endswith("/pulls/7"):
            return _ok_pr(head=head, mergeable_state="blocked")
        raise AssertionError(url)

    c = verify_feature_merge(
        repo="o/r",
        pr_number=7,
        expected_approver_login="huozheclaude",
        head_sha=head,
        token="tok",
        required_checks=[],
        http_get=fake_get,
    )
    assert c.ok is False
    assert "mergeable_state" in c.reason


def test_feature_merge_rejects_missing_check() -> None:
    head = "abc123"

    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return _ok_reviews(head=head)
        if url.endswith("/pulls/7"):
            return _ok_pr(head=head)
        if url.endswith("/check-runs"):
            return {"check_runs": []}
        if url.endswith("/status"):
            return {"statuses": [], "state": "pending"}
        raise AssertionError(url)

    c = verify_feature_merge(
        repo="o/r",
        pr_number=7,
        expected_approver_login="huozheclaude",
        head_sha=head,
        token="tok",
        required_checks=["ci/test"],
        http_get=fake_get,
    )
    assert c.ok is False
    assert "missing" in c.reason
    assert "ci/test" in c.reason


def test_feature_merge_rejects_failed_check() -> None:
    head = "abc123"

    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return _ok_reviews(head=head)
        if url.endswith("/pulls/7"):
            return _ok_pr(head=head)
        if url.endswith("/check-runs"):
            return {
                "check_runs": [
                    {
                        "name": "ci/test",
                        "status": "completed",
                        "conclusion": "failure",
                    }
                ]
            }
        if url.endswith("/status"):
            return {"statuses": [], "state": "failure"}
        raise AssertionError(url)

    c = verify_feature_merge(
        repo="o/r",
        pr_number=7,
        expected_approver_login="huozheclaude",
        head_sha=head,
        token="tok",
        required_checks=["ci/test"],
        http_get=fake_get,
    )
    assert c.ok is False
    assert "not success" in c.reason


def test_feature_merge_accepts_status_context() -> None:
    """Commit status context names count as required_checks too."""
    head = "abc123"

    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return _ok_reviews(head=head)
        if url.endswith("/pulls/7"):
            return _ok_pr(head=head)
        if url.endswith("/check-runs"):
            return {"check_runs": []}
        if url.endswith("/status"):
            return {
                "statuses": [{"context": "ci/test", "state": "success"}],
                "state": "success",
            }
        raise AssertionError(url)

    c = verify_feature_merge(
        repo="o/r",
        pr_number=7,
        expected_approver_login="huozheclaude",
        head_sha=head,
        token="tok",
        required_checks=["ci/test"],
        http_get=fake_get,
    )
    assert c.ok is True


def test_feature_merge_rejects_wrong_approver() -> None:
    head = "abc123"

    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return _ok_reviews(login="huozhegrok", head=head)
        if url.endswith("/pulls/7"):
            return _ok_pr(head=head)
        raise AssertionError(url)

    c = verify_feature_merge(
        repo="o/r",
        pr_number=7,
        expected_approver_login="huozheclaude",
        head_sha=head,
        token="tok",
        required_checks=[],
        http_get=fake_get,
    )
    assert c.ok is False
