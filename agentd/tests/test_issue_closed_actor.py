"""#36 / §10.3: only the human owner may close a session issue."""

from __future__ import annotations

import json
from pathlib import Path

from agentd.config import Config
from agentd.db import Store
from agentd.design_loop import DesignLoop


def _cfg(tmp_path: Path) -> Config:
    return Config(
        raw={
            "host": {"owner": "huozhe"},
            "agents": {
                "claude": {"login": "huozheclaude"},
                "grok": {"login": "huozhegrok"},
            },
            "gateway": {"login": "huozhegateway"},
        },
        root=tmp_path,
    )


def _closed_payload(
    *,
    repo: str,
    issue_num: int,
    sender: str,
) -> bytes:
    return json.dumps(
        {
            "action": "closed",
            "issue": {
                "number": issue_num,
                "state": "closed",
                "state_reason": "completed",
                "title": "session work",
                "user": {"login": "huozhe"},
            },
            "repository": {"full_name": repo},
            "sender": {"login": sender},
        }
    ).encode()


def test_agent_close_session_reopens_and_escalates(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = "huozhe/code-workflow#32"
    store.upsert_session(
        session_key=sk,
        project_key="huozhe/code-workflow",
        repo="huozhe/code-workflow",
        issue_num=32,
        state="IMPLEMENTING",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
    )
    posts: list[dict] = []
    reopens: list[dict] = []

    def fake_post(*, repo, issue_num, body, token):  # noqa: ANN001
        posts.append(
            {"repo": repo, "issue_num": issue_num, "body": body, "token": token}
        )
        return 7001

    def fake_reopen(*, repo, issue_num, token):  # noqa: ANN001
        reopens.append({"repo": repo, "issue_num": issue_num, "token": token})

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        post_comment=fake_post,
        reopen_issue_fn=fake_reopen,
        gateway_token="gw",
    )
    store.insert_delivery(
        delivery_id="d-agent-close",
        event="issues",
        action="closed",
        repo="huozhe/code-workflow",
        issue_num=32,
        sender="huozheclaude",
        payload=_closed_payload(
            repo="huozhe/code-workflow", issue_num=32, sender="huozheclaude"
        ),
        status="deferred",
    )
    loop.process_deferred_batch()

    assert reopens == [
        {"repo": "huozhe/code-workflow", "issue_num": 32, "token": "gw"}
    ]
    assert len(posts) == 1
    assert "@huozhe" in posts[0]["body"]
    assert "§10.3" in posts[0]["body"] or "10.3" in posts[0]["body"]
    assert "huozheclaude" in posts[0]["body"]

    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "PAUSED_HUMAN"
    assert sess.get("resume_state") == "IMPLEMENTING"
    assert store.get_open_escalation(sk) is not None

    row = store._conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", ("d-agent-close",)
    ).fetchone()
    assert row["status"] == "done"
    # Not a terminal session state — still has resume_state for owner reply.
    assert sess["state"] != "TEARDOWN"
    store.close()


def test_developer_close_also_escalates(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = "o/r#1"
    store.upsert_session(
        session_key=sk,
        project_key="o/r",
        repo="o/r",
        issue_num=1,
        state="PLANNING",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
    )
    posts: list[str] = []
    reopens: list[str] = []

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        post_comment=lambda **k: posts.append(k["body"]) or 1,
        reopen_issue_fn=lambda **k: reopens.append(k["repo"]),
        gateway_token="gw",
    )
    store.insert_delivery(
        delivery_id="d-dev-close",
        event="issues",
        action="closed",
        repo="o/r",
        issue_num=1,
        sender="huozhegrok",
        payload=_closed_payload(repo="o/r", issue_num=1, sender="huozhegrok"),
        status="deferred",
    )
    loop.process_deferred_batch()
    assert reopens == ["o/r"]
    assert len(posts) == 1
    assert store.get_session(sk)["state"] == "PAUSED_HUMAN"
    store.close()


def test_owner_close_session_no_escalate_no_reopen(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    sk = "o/r#9"
    store.upsert_session(
        session_key=sk,
        project_key="o/r",
        repo="o/r",
        issue_num=9,
        state="IMPLEMENTING",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
    )
    sess_dir = tmp_path / "projects" / "o__r" / "sessions" / "9"
    sess_dir.mkdir(parents=True)
    (sess_dir / "keep").write_text("x", encoding="utf-8")
    posts: list[dict] = []
    reopens: list[dict] = []

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        post_comment=lambda **k: posts.append(k) or 1,
        reopen_issue_fn=lambda **k: reopens.append(k),
        gateway_token="gw",
    )
    store.insert_delivery(
        delivery_id="d-owner-close",
        event="issues",
        action="closed",
        repo="o/r",
        issue_num=9,
        sender="huozhe",
        payload=_closed_payload(repo="o/r", issue_num=9, sender="huozhe"),
        status="deferred",
    )
    loop.process_deferred_batch()

    assert posts == []
    assert reopens == []
    sess = store.get_session(sk)
    assert sess is not None
    # Owner close is the teardown trigger. Empty ledger → archive → CLOSED.
    assert sess["state"] == "CLOSED"
    assert sess["classification"] == "ABANDONED"
    row = store._conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", ("d-owner-close",)
    ).fetchone()
    assert row["status"] == "done"
    store.close()


def test_agent_close_non_session_issue_no_escalation(tmp_path: Path) -> None:
    """Fixes #NN maintenance close: no sessions row → drop, no escalate (Claude scope)."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    posts: list[dict] = []
    reopens: list[dict] = []

    loop = DesignLoop(
        store,
        cfg,
        supervisor=None,
        dispatch_turns=False,
        post_comment=lambda **k: posts.append(k) or 1,
        reopen_issue_fn=lambda **k: reopens.append(k),
        gateway_token="gw",
    )
    store.insert_delivery(
        delivery_id="d-fixes-34",
        event="issues",
        action="closed",
        repo="huozhe/code-workflow",
        issue_num=34,
        sender="huozhegrok",
        payload=_closed_payload(
            repo="huozhe/code-workflow", issue_num=34, sender="huozhegrok"
        ),
        status="deferred",
    )
    loop.process_deferred_batch()

    assert posts == []
    assert reopens == []
    assert store.get_session("huozhe/code-workflow#34") is None
    row = store._conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", ("d-fixes-34",)
    ).fetchone()
    assert row["status"] == "dropped"
    store.close()


def test_build_prompt_forbids_issue_close() -> None:
    import sys

    runner_root = Path(__file__).resolve().parents[1] / "docker" / "session-runner"
    sys.path.insert(0, str(runner_root))
    from agentd_runner.turn import build_prompt  # noqa: E402

    text = build_prompt(
        {"role": "architect", "turn_id": "t-1", "event": {"kind": "x"}},
        None,
    )
    assert "Never close" in text or "never close" in text.lower()
    assert "10.3" in text
