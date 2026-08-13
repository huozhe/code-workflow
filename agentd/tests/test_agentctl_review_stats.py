"""#49 / ADR-14: agentctl review-stats — turns-per-review measurement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentctl.__main__ import main
from agentd.db import Store
from agentd.review_stats import collect_review_stats, session_issue_nums


REPO = "huozhe/code-workflow"


def _seed_session(
    store: Store,
    *,
    issue: int = 47,
    design_pr: int | None = 48,
    feature_pr: int | None = None,
    suffix: str = "",
) -> str:
    sk = f"{REPO}#{issue}{suffix}"
    store.upsert_session(
        session_key=sk,
        project_key=REPO,
        repo=REPO,
        issue_num=issue,
        state="DESIGN_REVIEW",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
        design_pr=design_pr,
    )
    if feature_pr is not None:
        store.update_session_fields(sk, feature_pr=feature_pr)
    return sk


def _insert_delivery(
    store: Store,
    *,
    did: str,
    event: str,
    action: str | None,
    issue_num: int,
    payload: dict,
) -> None:
    store.insert_delivery(
        delivery_id=did,
        event=event,
        action=action,
        repo=REPO,
        issue_num=issue_num,
        sender="huozhegrok",
        payload=json.dumps(payload).encode(),
        status="done",
    )


def _insert_turn(
    store: Store,
    *,
    turn_id: str,
    session_key: str,
    delivery_id: str | None,
    public_actions: str | None,
    started_at: int = 1,
) -> None:
    store.insert_turn(
        turn_id=turn_id,
        session_key=session_key,
        role="developer",
        delivery_id=delivery_id,
        started_at=started_at,
        ended_at=started_at + 1,
        status="done",
        summary="ok",
    )
    store.finish_turn(
        turn_id,
        ended_at=started_at + 1,
        status="done",
        summary="ok",
        public_actions=public_actions,
    )


def _review_payload(review_id: int, pr: int, state: str = "changes_requested") -> dict:
    return {
        "action": "submitted",
        "review": {"id": review_id, "state": state},
        "pull_request": {"number": pr},
    }


def _comment_payload(review_id: int | None, pr: int) -> dict:
    comment: dict = {"id": 1, "body": "nit"}
    if review_id is not None:
        comment["pull_request_review_id"] = review_id
    return {"action": "created", "comment": comment, "pull_request": {"number": pr}}


def test_session_issue_nums_omits_null_and_dedupes() -> None:
    assert session_issue_nums(
        {"issue_num": 49, "design_pr": 82, "feature_pr": None}
    ) == [49, 82]
    assert session_issue_nums(
        {"issue_num": 49, "design_pr": 49, "feature_pr": 90}
    ) == [49, 90]


def test_coalesced_review_is_n_comments_one_turn(tmp_path: Path) -> None:
    """After #52: 7 inline comments + 1 submitted review → 1 woken turn."""
    store = Store(tmp_path / "state.db")
    issue, pr, rid = 47, 48, 9001
    sk = _seed_session(store, issue=issue, design_pr=pr)

    for i in range(7):
        _insert_delivery(
            store,
            did=f"cmt-{i}",
            event="pull_request_review_comment",
            action="created",
            issue_num=pr,
            payload=_comment_payload(rid, pr),
        )
        _insert_delivery(
            store,
            did=f"thr-{i}",
            event="pull_request_review_thread",
            action="resolved",
            issue_num=pr,
            payload={"action": "resolved", "thread": {"id": f"t{i}"}},
        )
    _insert_delivery(
        store,
        did="rev-1",
        event="pull_request_review",
        action="submitted",
        issue_num=pr,
        payload=_review_payload(rid, pr),
    )
    _insert_turn(
        store,
        turn_id="t-review",
        session_key=sk,
        delivery_id="rev-1",
        public_actions='[{"kind":"comment"}]',
    )
    # Non-review turns must land in totals, not on the review row.
    for i in range(3):
        _insert_turn(
            store,
            turn_id=f"t-other-{i}",
            session_key=sk,
            delivery_id=f"other-{i}",
            public_actions="[]",
            started_at=10 + i,
        )
    # Amplifier replies live on the PR, not the session issue.
    for i in range(7):
        _insert_delivery(
            store,
            did=f"ic-{i}",
            event="issue_comment",
            action="created",
            issue_num=pr,
            payload={"action": "created", "issue": {"number": pr, "pull_request": {}}},
        )

    report = collect_review_stats(store, sessions=[store.get_session(sk)])
    sess = report["sessions"][0]
    assert sess["session_key"] == sk
    assert len(sess["reviews"]) == 1
    rev = sess["reviews"][0]
    assert rev == {
        "review_id": rid,
        "pr": pr,
        "state": "changes_requested",
        "inline_comments": 7,
        "turn_id": "t-review",
        "turns_woken": 1,
        "turns_empty": 0,
    }
    assert sess["thread_events"] == 7
    assert sess["issue_comment_created"] == 7
    assert sess["unmatched_inline_comments"] == 0
    assert sess["totals"] == {"turns": 4, "empty": 3}
    store.close()


def test_membership_uses_pr_number_not_session_issue(tmp_path: Path) -> None:
    """Review traffic keyed by PR would vanish under issue_num = session.issue_num."""
    store = Store(tmp_path / "state.db")
    issue, pr, rid = 49, 82, 11
    sk = _seed_session(store, issue=issue, design_pr=pr)
    _insert_delivery(
        store,
        did="rev-pr",
        event="pull_request_review",
        action="submitted",
        issue_num=pr,
        payload=_review_payload(rid, pr, state="APPROVED"),
    )
    _insert_delivery(
        store,
        did="cmt-pr",
        event="pull_request_review_comment",
        action="created",
        issue_num=pr,
        payload=_comment_payload(rid, pr),
    )
    # A delivery on some other session's issue must not leak in.
    _insert_delivery(
        store,
        did="rev-other",
        event="pull_request_review",
        action="submitted",
        issue_num=99,
        payload=_review_payload(99, 99),
    )

    sess = collect_review_stats(store, sessions=[store.get_session(sk)])["sessions"][0]
    assert [r["review_id"] for r in sess["reviews"]] == [rid]
    assert sess["reviews"][0]["state"] == "approved"
    assert sess["reviews"][0]["inline_comments"] == 1
    store.close()


def test_unmatched_inline_comments_counted(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    issue, pr = 47, 48
    sk = _seed_session(store, issue=issue, design_pr=pr)
    _insert_delivery(
        store,
        did="cmt-orphan",
        event="pull_request_review_comment",
        action="created",
        issue_num=pr,
        payload=_comment_payload(777, pr),
    )
    _insert_delivery(
        store,
        did="cmt-no-id",
        event="pull_request_review_comment",
        action="created",
        issue_num=pr,
        payload=_comment_payload(None, pr),
    )
    sess = collect_review_stats(store, sessions=[store.get_session(sk)])["sessions"][0]
    assert sess["reviews"] == []
    assert sess["unmatched_inline_comments"] == 2
    store.close()


def test_empty_actions_predicate_and_undispatched_review(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed_session(store, issue=1, design_pr=2)
    _insert_delivery(
        store,
        did="rev-queued",
        event="pull_request_review",
        action="submitted",
        issue_num=2,
        payload=_review_payload(5, 2, state="commented"),
    )
    _insert_turn(
        store, turn_id="t-null", session_key=sk, delivery_id="x", public_actions=None
    )
    _insert_turn(
        store, turn_id="t-blank", session_key=sk, delivery_id="y", public_actions=""
    )
    sess = collect_review_stats(store, sessions=[store.get_session(sk)])["sessions"][0]
    assert sess["reviews"][0]["turn_id"] is None
    assert sess["reviews"][0]["turns_woken"] == 0
    assert sess["totals"] == {"turns": 2, "empty": 2}
    store.close()


def test_ignores_non_created_comment_and_non_submitted_review(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed_session(store, issue=1, design_pr=2)
    _insert_delivery(
        store,
        did="rev-edited",
        event="pull_request_review",
        action="edited",
        issue_num=2,
        payload=_review_payload(5, 2),
    )
    _insert_delivery(
        store,
        did="cmt-edited",
        event="pull_request_review_comment",
        action="edited",
        issue_num=2,
        payload=_comment_payload(5, 2),
    )
    sess = collect_review_stats(store, sessions=[store.get_session(sk)])["sessions"][0]
    assert sess["reviews"] == []
    assert sess["unmatched_inline_comments"] == 0
    store.close()


def _cli_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Store:
    monkeypatch.setenv("AGENTD_ROOT", str(tmp_path))
    store = Store(tmp_path / "state.db")
    return store


def test_cli_default_all_sessions_and_session_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _cli_root(tmp_path, monkeypatch)
    a = _seed_session(store, issue=47, design_pr=48)
    b = _seed_session(store, issue=49, design_pr=82)
    _insert_delivery(
        store,
        did="rev-a",
        event="pull_request_review",
        action="submitted",
        issue_num=48,
        payload=_review_payload(1, 48),
    )
    _insert_delivery(
        store,
        did="rev-b",
        event="pull_request_review",
        action="submitted",
        issue_num=82,
        payload=_review_payload(2, 82, state="approved"),
    )
    store.close()

    main(["review-stats"])
    all_sessions = json.loads(capsys.readouterr().out)
    keys = {s["session_key"] for s in all_sessions["sessions"]}
    assert keys == {a, b}

    main(["review-stats", "--session", "49"])
    one = json.loads(capsys.readouterr().out)
    assert [s["session_key"] for s in one["sessions"]] == [b]
    assert one["sessions"][0]["reviews"][0]["review_id"] == 2

    main(["review-stats", "--session", a])
    by_key = json.loads(capsys.readouterr().out)
    assert [s["session_key"] for s in by_key["sessions"]] == [a]


def test_cli_unknown_session_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_root(tmp_path, monkeypatch).close()
    with pytest.raises(SystemExit) as ei:
        main(["review-stats", "--session", "no-such"])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "no session for 'no-such'" in err


def test_cli_empty_host_prints_empty_array(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_root(tmp_path, monkeypatch).close()
    main(["review-stats"])
    assert json.loads(capsys.readouterr().out) == {"sessions": []}
