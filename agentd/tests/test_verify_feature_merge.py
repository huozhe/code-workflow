"""M4-2: §8.4 Feature merge authorization (approver + checks + mergeable)."""

from __future__ import annotations

from agentd.github_fetch import PrReviewThreadSnapshot
from agentd.verify import verify_feature_merge


def _snap(unresolved: int = 0) -> PrReviewThreadSnapshot:
    ids = [f"t{i}" for i in range(unresolved)]
    return PrReviewThreadSnapshot(
        open_thread_ids=ids,
        unresolved_count=unresolved,
        all_thread_ids=ids,
    )


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
        fetch_threads=lambda **_k: _snap(0),
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


def _http_get(
    head: str,
    *,
    mergeable_state: str = "blocked",
    check_status: str | None = None,
    check_conclusion: str | None = None,
):
    """REST fake for reviews + PR (+ optional check-runs)."""

    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return _ok_reviews(head=head)
        if url.endswith("/pulls/7"):
            return _ok_pr(head=head, mergeable_state=mergeable_state)
        if url.endswith("/check-runs"):
            if check_status is None:
                raise AssertionError(url)
            return {
                "check_runs": [
                    {
                        "name": "ci/test",
                        "status": check_status,
                        "conclusion": check_conclusion,
                    }
                ]
            }
        if url.endswith("/status"):
            return {"statuses": [], "state": "pending"}
        raise AssertionError(url)

    return fake_get


def test_adr26_blocked_unresolved_threads_unset_checks_is_permanent() -> None:
    """Acceptance (1): blocked + ≥1 thread + required_checks unset."""
    head = "abc123"
    c = verify_feature_merge(
        repo="o/r",
        pr_number=7,
        expected_approver_login="huozheclaude",
        head_sha=head,
        token="tok",
        required_checks=[],
        http_get=_http_get(head),
        fetch_threads=lambda **_k: _snap(2),
    )
    assert c.ok is False
    assert c.transient is False
    assert "2 unresolved review thread" in c.reason


def test_adr26_blocked_unresolved_threads_pending_checks_is_permanent() -> None:
    """Acceptance (2): in-flight checks must not buy a thread-blocked PR retries."""
    head = "abc123"
    c = verify_feature_merge(
        repo="o/r",
        pr_number=7,
        expected_approver_login="huozheclaude",
        head_sha=head,
        token="tok",
        required_checks=["ci/test"],
        http_get=_http_get(head, check_status="in_progress"),
        fetch_threads=lambda **_k: _snap(1),
    )
    assert c.ok is False
    assert c.transient is False
    assert "1 unresolved review thread" in c.reason


def test_adr26_blocked_zero_threads_pending_checks_is_transient() -> None:
    """Acceptance (3): thread-free blocked + pending checks stays transient."""
    head = "abc123"
    c = verify_feature_merge(
        repo="o/r",
        pr_number=7,
        expected_approver_login="huozheclaude",
        head_sha=head,
        token="tok",
        required_checks=["ci/test"],
        http_get=_http_get(head, check_status="in_progress"),
        fetch_threads=lambda **_k: _snap(0),
    )
    assert c.ok is False
    assert c.transient is True


def test_adr26_blocked_zero_threads_checks_success_keeps_generic_reason() -> None:
    """Acceptance (4): thread-free blocked + green checks — old permanent wording."""
    head = "abc123"
    c = verify_feature_merge(
        repo="o/r",
        pr_number=7,
        expected_approver_login="huozheclaude",
        head_sha=head,
        token="tok",
        required_checks=["ci/test"],
        http_get=_http_get(
            head, check_status="completed", check_conclusion="success"
        ),
        fetch_threads=lambda **_k: _snap(0),
    )
    assert c.ok is False
    assert c.transient is False
    assert "branch protection or review rule" in c.reason
    assert "unresolved" not in c.reason


def test_adr26_thread_fetch_none_falls_through() -> None:
    """Acceptance (5): fetch returns no data — today's classification, not a verdict."""
    head = "abc123"
    c = verify_feature_merge(
        repo="o/r",
        pr_number=7,
        expected_approver_login="huozheclaude",
        head_sha=head,
        token="tok",
        required_checks=["ci/test"],
        http_get=_http_get(head, check_status="in_progress"),
        fetch_threads=lambda **_k: None,
    )
    assert c.ok is False
    assert c.transient is True


def test_adr26_thread_fetch_raises_falls_through() -> None:
    """Acceptance (5): fetch raises — same fall-through as no data."""
    head = "abc123"

    def boom(**_k):
        raise RuntimeError("graphql down")

    c = verify_feature_merge(
        repo="o/r",
        pr_number=7,
        expected_approver_login="huozheclaude",
        head_sha=head,
        token="tok",
        required_checks=["ci/test"],
        http_get=_http_get(head, check_status="in_progress"),
        fetch_threads=boom,
    )
    assert c.ok is False
    assert c.transient is True


def test_adr26_thread_fetch_not_called_unless_blocked() -> None:
    """Acceptance (6): clean / dirty / unknown never call the thread fetch."""
    head = "abc123"
    calls: list[object] = []

    def fetch_threads(**_k):
        calls.append(1)
        raise AssertionError("thread fetch must not run off the blocked branch")

    for state in ("clean", "dirty", "unknown"):
        calls.clear()
        c = verify_feature_merge(
            repo="o/r",
            pr_number=7,
            expected_approver_login="huozheclaude",
            head_sha=head,
            token="tok",
            required_checks=[],
            http_get=_http_get(head, mergeable_state=state),
            fetch_threads=fetch_threads,
        )
        assert calls == [], state
        if state == "clean":
            assert c.ok is True
        elif state == "dirty":
            assert c.ok is False and c.transient is False
        else:
            assert c.ok is False and c.transient is True


def test_adr26_blocked_zero_threads_unset_checks_is_transient() -> None:
    """Acceptance (7): not wanted + zero threads stays transient."""
    head = "abc123"
    c = verify_feature_merge(
        repo="o/r",
        pr_number=7,
        expected_approver_login="huozheclaude",
        head_sha=head,
        token="tok",
        required_checks=[],
        http_get=_http_get(head),
        fetch_threads=lambda **_k: _snap(0),
    )
    assert c.ok is False
    assert c.transient is True
    assert "will retry" in c.reason
