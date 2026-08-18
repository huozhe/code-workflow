"""#35: StructuralRefusal escalates once; CapacityRefusal stays deferred."""

from __future__ import annotations

import json
from pathlib import Path

from agentd.config import Config
from agentd.db import SCHEMA_VERSION, Store
from agentd.design_loop import DesignLoop
from agentd.refusals import CapacityRefusal, StructuralRefusal
from agentd.supervisor import assert_host_secrets_not_mounted


def _cfg(tmp_path: Path) -> Config:
    return Config(
        raw={
            "host": {"owner": "huozhe"},
            "intake": {"mode": "label", "label": "agentd", "actors": "collaborators"},
            "agents": {
                "claude": {"login": "huozheclaude"},
                "grok": {"login": "huozhegrok"},
            },
            "gateway": {"login": "huozhegateway"},
        },
        root=tmp_path,
    )


def _deferred_issues(
    store: Store,
    *,
    delivery_id: str,
    repo: str = "o/r",
    issue_num: int = 7,
    sender: str = "huozhe",
) -> None:
    body = json.dumps(
        {
            "action": "opened",
            "issue": {
                "number": issue_num,
                "title": "demo",
                "body": "x",
                "author_association": "OWNER",
                "labels": [{"name": "agentd"}],
                "user": {"login": sender},
            },
            "repository": {"full_name": repo},
            "sender": {"login": sender},
        }
    ).encode()
    store.insert_delivery(
        delivery_id=delivery_id,
        event="issues",
        action="opened",
        repo=repo,
        issue_num=issue_num,
        sender=sender,
        payload=body,
        status="deferred",
    )


def test_capacity_refusal_leaves_deferred(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    posts: list[dict] = []

    class CapSup:
        def ensure_session(self, **kwargs):
            raise CapacityRefusal("max_hot_containers=2 reached (hot=2)")

    loop = DesignLoop(
        store,
        cfg,
        supervisor=CapSup(),  # type: ignore[arg-type]
        dispatch_turns=False,
        post_comment=lambda **k: posts.append(k) or 1,
        gateway_token="gw",
    )
    _deferred_issues(store, delivery_id="d-cap")
    loop.process_deferred_batch()
    row = store._conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", ("d-cap",)
    ).fetchone()
    assert row["status"] == "deferred"
    assert posts == []
    assert store.get_session("o/r#7") is None
    store.close()


def test_structural_refusal_escalates_once_and_blocks_project(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    posts: list[dict] = []
    ensure_calls = {"n": 0}

    class StructSup:
        def ensure_session(self, **kwargs):
            ensure_calls["n"] += 1
            raise StructuralRefusal(
                "FORBIDDEN: unexpected path under /srv/agentd: 'work'"
            )

    def fake_post(*, repo, issue_num, body, token):
        posts.append(
            {"repo": repo, "issue_num": issue_num, "body": body, "token": token}
        )
        return 555

    loop = DesignLoop(
        store,
        cfg,
        supervisor=StructSup(),  # type: ignore[arg-type]
        dispatch_turns=False,
        post_comment=fake_post,
        gateway_token="gw",
    )
    _deferred_issues(store, delivery_id="d-s1", issue_num=7)
    loop.process_deferred_batch()

    assert ensure_calls["n"] == 1
    assert len(posts) == 1
    assert "@huozhe" in posts[0]["body"]
    assert "work" in posts[0]["body"]
    assert posts[0]["issue_num"] == 7

    sess = store.get_session("o/r#7")
    assert sess is not None
    assert sess["state"] == "PAUSED_HUMAN"
    assert "work" in str(sess.get("paused_reason") or "")

    block = store.get_open_project_block("o/r")
    assert block is not None
    assert block["comment_id"] == 555

    row = store._conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", ("d-s1",)
    ).fetchone()
    assert row["status"] == "done"

    # Second deferred for same project: block short-circuits at supervisor...
    # but design_loop only calls ensure_session when no session. New issue:
    _deferred_issues(store, delivery_id="d-s2", issue_num=8)
    loop.process_deferred_batch()
    # ensure_session ran once more and raised; no second comment
    assert ensure_calls["n"] == 2
    assert len(posts) == 1
    row2 = store._conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", ("d-s2",)
    ).fetchone()
    assert row2["status"] == "done"
    assert store.get_session("o/r#8") is not None
    assert store.get_session("o/r#8")["state"] == "PAUSED_HUMAN"

    snap = store.status_snapshot()
    assert len(snap["project_blocks"]) == 1
    assert snap["project_blocks"][0]["project_key"] == "o/r"
    store.close()


def test_open_project_block_skips_worktree_prepare(tmp_path: Path, monkeypatch) -> None:
    """ensure_session with open block raises before layout work (#35)."""
    from agentd.supervisor import SessionSupervisor

    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    store.open_project_block(
        project_key="o/r",
        reason="FORBIDDEN: unexpected path 'work'",
        session_key="o/r#1",
        issue_num=1,
        comment_id=1,
    )
    sup = SessionSupervisor(store, cfg)
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("must not prepare layout while blocked")

    monkeypatch.setattr(sup, "_prepare_project_issue_layout", boom)
    try:
        sup.ensure_session(session_key="o/r#2", repo="o/r", issue_num=2)
        raise AssertionError("expected StructuralRefusal")
    except StructuralRefusal as e:
        assert "work" in str(e)
    assert called["n"] == 0
    store.close()


def test_assert_host_secrets_raises_structural(monkeypatch) -> None:
    from agentd import supervisor as supmod

    class FakeR:
        returncode = 0
        stdout = "home\nrepo\nsessions\nwork\n"
        stderr = ""

    monkeypatch.setattr(supmod, "_docker", lambda *a, **k: FakeR())
    try:
        assert_host_secrets_not_mounted("cid")
        raise AssertionError("expected StructuralRefusal")
    except StructuralRefusal as e:
        assert "work" in str(e)
        assert isinstance(e, StructuralRefusal)


def test_schema_v5_project_blocks(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    assert store._schema_version() == SCHEMA_VERSION
    store.open_project_block(
        project_key="a/b",
        reason="x",
        session_key="a/b#1",
        issue_num=1,
        comment_id=None,
    )
    assert store.get_open_project_block("a/b") is not None
    assert store.close_project_block("a/b") == 1
    assert store.get_open_project_block("a/b") is None
    store.close()


def test_resume_clears_project_block(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    now = 1_700_000_000
    store.upsert_session(
        session_key="o/r#7",
        project_key="o/r",
        repo="o/r",
        issue_num=7,
        state="PAUSED_HUMAN",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=now,
        updated_at=now,
        paused_reason="structural",
    )
    store.update_session_fields(session_key="o/r#7", resume_state="PLANNING")
    store.open_escalation(
        session_key="o/r#7",
        role="system",
        reason="structural",
        comment_id=1,
    )
    store.open_project_block(
        project_key="o/r",
        reason="structural",
        session_key="o/r#7",
        issue_num=7,
        comment_id=1,
    )

    class NoRunner:
        def ensure_session(self, **kwargs):
            # Still structural if owner did not fix — test success path: clear only.
            raise CapacityRefusal("max_hot_containers=1 reached")

    loop = DesignLoop(
        store,
        cfg,
        supervisor=NoRunner(),  # type: ignore[arg-type]
        dispatch_turns=False,
        gateway_token="gw",
    )
    body = json.dumps(
        {
            "action": "created",
            "issue": {"number": 7},
            "comment": {"body": "fixed, go", "user": {"login": "huozhe"}},
            "repository": {"full_name": "o/r"},
            "sender": {"login": "huozhe"},
        }
    ).encode()
    store.insert_delivery(
        delivery_id="d-resume",
        event="issue_comment",
        action="created",
        repo="o/r",
        issue_num=7,
        sender="huozhe",
        payload=body,
        status="deferred",
    )
    loop.process_deferred_batch()
    # Capacity on resume leaves deferred; block must be cleared so retry can run.
    assert store.get_open_project_block("o/r") is None
    store.close()
