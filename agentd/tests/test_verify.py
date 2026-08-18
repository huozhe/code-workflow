"""§8.4 design approval verification (M3-3)."""

from __future__ import annotations

from agentd.verify import verify_design_approval


def test_approval_ok_on_head() -> None:
    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return [
                {
                    "user": {"login": "huozhegrok"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                }
            ]
        return {"head": {"sha": "abc123"}, "number": 7}

    c = verify_design_approval(
        repo="o/r",
        pr_number=7,
        expected_approver_login="huozhegrok",
        head_sha="abc123",
        token="tok",
        http_get=fake_get,
    )
    assert c.ok is True


def test_approval_rejected_wrong_user() -> None:
    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return [
                {
                    "user": {"login": "random"},
                    "state": "APPROVED",
                    "commit_id": "abc123",
                }
            ]
        return {"head": {"sha": "abc123"}}

    c = verify_design_approval(
        repo="o/r",
        pr_number=7,
        expected_approver_login="huozhegrok",
        head_sha="abc123",
        token="tok",
        http_get=fake_get,
    )
    assert c.ok is False
    assert "no review" in c.reason or "expected" in c.reason


def test_approval_rejected_stale_head() -> None:
    def fake_get(url: str, *, token: str):
        if url.endswith("/reviews"):
            return [
                {
                    "user": {"login": "huozhegrok"},
                    "state": "APPROVED",
                    "commit_id": "oldsha",
                }
            ]
        return {"head": {"sha": "newsha"}}

    c = verify_design_approval(
        repo="o/r",
        pr_number=7,
        expected_approver_login="huozhegrok",
        head_sha="oldsha",
        token="tok",
        http_get=fake_get,
    )
    assert c.ok is False
    assert "not current head" in c.reason
