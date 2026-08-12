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
    assert c.transient is False
    assert "clean" in c.reason
    assert "required_checks" in c.reason or "success" in c.reason


def test_feature_merge_unknown_is_transient() -> None:
    """GitHub computes mergeability async — unknown must not escalate (PR #54 B1)."""
    head = "abc123"

    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return _ok_reviews(head=head)
        if url.endswith("/pulls/7"):
            return _ok_pr(head=head, mergeable_state="unknown")
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
    assert c.transient is True
    assert "will retry" in c.reason
    assert "unknown" in c.reason


def test_feature_merge_null_mergeable_state_is_transient() -> None:
    head = "abc123"

    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return _ok_reviews(head=head)
        if url.endswith("/pulls/7"):
            return {
                "number": 7,
                "head": {"sha": head},
                "mergeable_state": None,
            }
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
    assert c.ok is False and c.transient is True


def test_feature_merge_dirty_is_permanent() -> None:
    head = "abc123"

    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return _ok_reviews(head=head)
        if url.endswith("/pulls/7"):
            return _ok_pr(head=head, mergeable_state="dirty")
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
    assert c.transient is False
    assert "dirty" in c.reason


def test_feature_merge_blocked_with_pending_checks_is_transient() -> None:
    head = "abc123"

    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return _ok_reviews(head=head)
        if url.endswith("/pulls/7"):
            return _ok_pr(head=head, mergeable_state="blocked")
        if url.endswith("/check-runs"):
            return {
                "check_runs": [
                    {
                        "name": "ci/test",
                        "status": "in_progress",
                        "conclusion": None,
                    }
                ]
            }
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
    assert c.transient is True


def test_feature_merge_missing_check_is_transient() -> None:
    """Not-yet-reported required check → retry, not escalate."""
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
    assert c.transient is True
    assert "ci/test" in c.reason


def test_feature_merge_rejects_failed_check_permanent() -> None:
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
    assert c.transient is False
    assert "not success" in c.reason or "failure" in c.reason


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


def test_feature_merge_rejects_wrong_approver_permanent() -> None:
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
    assert c.transient is False
