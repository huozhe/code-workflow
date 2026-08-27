"""#20: turns for the same (project, role) serialize rather than interleave."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from agentd.config import Config
from agentd.db import Store
from agentd.session_loop import SessionLoop, _lock_for_project_role


def test_project_role_lock_is_shared() -> None:
    a = _lock_for_project_role("o/r", "architect")
    b = _lock_for_project_role("o/r", "architect")
    c = _lock_for_project_role("o/r", "developer")
    d = _lock_for_project_role("o/other", "architect")
    assert a is b
    assert a is not c
    assert a is not d


def test_two_issues_same_role_serialize(tmp_path: Path) -> None:
    """Two concurrent dispatches to the same project+role cannot interleave body work."""
    store = Store(tmp_path / "state.db")
    cfg = Config(
        raw={
            "host": {"owner": "o"},
            "agents": {
                "claude": {"login": "a"},
                "grok": {"login": "d"},
            },
        },
        root=tmp_path,
    )
    store.upsert_session(
        session_key="o/r#1",
        project_key="o/r",
        repo="o/r",
        issue_num=1,
        state="PLANNING",
        architect="a",
        developer="d",
        created_at=1,
        updated_at=1,
    )
    store.upsert_session(
        session_key="o/r#2",
        project_key="o/r",
        repo="o/r",
        issue_num=2,
        state="PLANNING",
        architect="a",
        developer="d",
        created_at=1,
        updated_at=1,
    )
    store.upsert_runner(
        "o/r",
        container_id="c1",
        endpoint="127.0.0.1:9",
        token="tok",
        tier="hot",
    )

    order: list[str] = []
    lock = threading.Lock()

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def call(self, method, params=None):
            if method != "turn.dispatch":
                return {"ok": True}
            sk = (params or {}).get("context", {}).get("session_key", "?")
            with lock:
                order.append(f"start:{sk}")
            time.sleep(0.08)
            with lock:
                order.append(f"end:{sk}")
            return {"status": "done", "summary": sk}

    import agentd.session_loop as dl

    orig = dl.RunnerClient
    dl.RunnerClient = FakeClient  # type: ignore[misc, assignment]
    try:
        loop = SessionLoop(store, cfg, supervisor=None, dispatch_turns=True)

        def run(sk: str, issue: int) -> None:
            loop._dispatch_turn(
                session_key=sk,
                role="architect",
                turn_id=f"t-{issue}",
                delivery_id=f"d-{issue}",
                dig={"kind": "test"},
                issue_num=issue,
            )

        t1 = threading.Thread(target=run, args=("o/r#1", 1))
        t2 = threading.Thread(target=run, args=("o/r#2", 2))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
    finally:
        dl.RunnerClient = orig  # type: ignore[misc]

    # Without serialization: start:1, start:2, end:*, end:*
    # With serialization: start:X, end:X, start:Y, end:Y
    assert len(order) == 4, order
    assert order[0].startswith("start:") and order[1].startswith("end:")
    assert order[2].startswith("start:") and order[3].startswith("end:")
    assert order[0].split(":")[1] == order[1].split(":")[1]
    assert order[2].split(":")[1] == order[3].split(":")[1]
    store.close()
