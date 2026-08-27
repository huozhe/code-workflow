"""#16: deferred backlog must not spawn sessions; quarantine; max_hot."""

from __future__ import annotations

import json
from pathlib import Path

from agentd.config import Config
from agentd.db import Store
from agentd.session_loop import SessionLoop


def _cfg(root: Path, **host: object) -> Config:
    raw = {
        "host": {
            "owner": "huozhe",
            "max_hot_containers": host.get("max_hot_containers", 4),
        },
        "intake": {"mode": "label", "label": "agentd", "actors": "collaborators"},
        "agents": {
            "claude": {"login": "huozheclaude"},
            "grok": {"login": "huozhegrok"},
        },
    }
    return Config(raw=raw, root=root)


def test_quarantine_deferred_moves_all(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    for i in range(5):
        store.insert_delivery(
            delivery_id=f"d-{i}",
            event="push",
            action=None,
            repo="o/r",
            issue_num=None,
            sender="u",
            payload=b"{}",
            status="deferred",
        )
    assert store.count_by_status().get("deferred") == 5
    n = store.quarantine_deferred()
    assert n == 5
    assert store.count_by_status().get("deferred") is None
    assert store.count_by_status().get("dropped") == 5
    store.close()


def test_count_deferred_respects_before(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    # insert_delivery stamps received_at=now; force two eras via SQL
    for i in range(3):
        store.insert_delivery(
            delivery_id=f"old-{i}",
            event="push",
            action=None,
            repo="o/r",
            issue_num=None,
            sender="u",
            payload=b"{}",
            status="deferred",
        )
    store._conn.execute(
        "UPDATE deliveries SET received_at = 1000 WHERE delivery_id LIKE 'old-%'"
    )
    for i in range(2):
        store.insert_delivery(
            delivery_id=f"new-{i}",
            event="push",
            action=None,
            repo="o/r",
            issue_num=None,
            sender="u",
            payload=b"{}",
            status="deferred",
        )
    store._conn.execute(
        "UPDATE deliveries SET received_at = 5000 WHERE delivery_id LIKE 'new-%'"
    )
    store._conn.commit()
    assert store.count_deferred() == 5
    assert store.count_deferred(before_received_at=3000) == 3
    assert store.quarantine_deferred(before_received_at=3000) == 3
    assert store.count_deferred() == 2
    store.close()


def test_deferred_backlog_creates_zero_sessions_without_intake_issues(
    tmp_path: Path,
) -> None:
    """Populated deferred queue of comments/PRs/pushes → 0 sessions."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)

    # Historical traffic shaped like #16 host: no intake-passing issues.opened
    for i, (event, action, issue) in enumerate(
        [
            ("issue_comment", "created", 6),
            ("pull_request", "opened", 10),
            ("pull_request_review", "submitted", 12),
            ("push", None, None),
            ("issues", "edited", 8),  # not open/reopen/labeled
        ]
    ):
        body = json.dumps(
            {
                "action": action,
                "issue": {"number": issue, "author_association": "OWNER", "labels": []},
                "pull_request": {"number": issue, "title": "x"},
                "repository": {"full_name": "huozhe/code-workflow"},
                "sender": {"login": "huozhe"},
            }
        ).encode()
        store.insert_delivery(
            delivery_id=f"hist-{i}",
            event=event,
            action=action,
            repo="huozhe/code-workflow" if issue else "",
            issue_num=issue,
            sender="huozhe",
            payload=body,
            status="deferred",
        )

    # Fake supervisor that would create sessions if asked — must never be called
    class BoomSupervisor:
        def ensure_session(self, **kwargs):
            raise AssertionError(f"ensure_session must not run: {kwargs}")

    loop = SessionLoop(store, cfg, supervisor=BoomSupervisor(), dispatch_turns=False)
    loop.process_deferred_batch(limit=50)
    assert store.list_sessions() == []
    # All deferred either dropped or still deferred (no session attach path)
    by = store.count_by_status()
    assert by.get("deferred", 0) == 0
    assert by.get("dropped", 0) >= 5
    store.close()


def test_intake_passing_issues_may_create_session(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path)
    created: list[str] = []

    class FakeSupervisor:
        def ensure_session(self, **kwargs):
            created.append(kwargs["session_key"])
            store.upsert_session(
                session_key=kwargs["session_key"],
                repo=kwargs["repo"],
                issue_num=kwargs["issue_num"],
                state="INTAKE",
                architect="huozheclaude",
                developer="huozhegrok",
                created_at=1,
                updated_at=1,
            )
            return object()

    body = json.dumps(
        {
            "action": "opened",
            "issue": {
                "number": 99,
                "author_association": "OWNER",
                "labels": [{"name": "agentd"}],
                "title": "real work",
            },
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhe"},
        }
    ).encode()
    store.insert_delivery(
        delivery_id="intake-ok",
        event="issues",
        action="opened",
        repo="huozhe/code-workflow",
        issue_num=99,
        sender="huozhe",
        payload=body,
        status="deferred",
    )
    loop = SessionLoop(store, cfg, supervisor=FakeSupervisor(), dispatch_turns=False)
    loop.process_deferred_batch()
    assert created == ["huozhe/code-workflow#99"]
    assert len(store.list_sessions()) == 1
    store.close()


def test_max_hot_containers_enforced(tmp_path: Path, monkeypatch) -> None:
    from agentd.supervisor import SessionSupervisor

    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path, max_hot_containers=2)
    # Seed two HOT **projects** (admission unit is project, #20)
    for i, repo in enumerate(("o/a", "o/b")):
        sk = f"{repo}#1"
        store.upsert_session(
            session_key=sk,
            project_key=repo,
            repo=repo,
            issue_num=1,
            state="PLANNING",
            architect="a",
            developer="d",
            created_at=1,
            updated_at=1,
        )
        store.upsert_runner(
            repo, container_id=f"c{i}", endpoint="127.0.0.1:1", token="t", tier="hot"
        )
    sup = SessionSupervisor(store, cfg)
    monkeypatch.setattr(sup, "_load_tokens", lambda: {"architect": "x", "developer": "y"})
    from agentd.refusals import CapacityRefusal

    try:
        # Third project should be refused
        sup.ensure_session(session_key="o/c#1", repo="o/c", issue_num=1)
        raise AssertionError("should have refused")
    except CapacityRefusal as e:
        assert "max_hot_containers" in str(e)
    store.close()
