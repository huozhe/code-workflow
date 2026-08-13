"""#78: ordinary turn path must probe/recreate, not trust the runners row."""

from __future__ import annotations

import json
from pathlib import Path

from agentd.config import Config
from agentd.db import Store
from agentd.design_loop import DesignLoop
from agentd.refusals import CapacityRefusal


def _cfg(tmp: Path) -> Config:
    return Config(
        raw={
            "host": {"owner": "huozhe"},
            "gateway": {"login": "huozhegateway"},
            "agents": {
                "claude": {"login": "huozheclaude"},
                "grok": {"login": "huozhegrok"},
            },
        },
        root=tmp,
    )


def _seed_session(store: Store, *, state: str = "IMPLEMENTING") -> str:
    sk = "huozhe/code-workflow#32"
    store.upsert_session(
        session_key=sk,
        project_key="huozhe/code-workflow",
        repo="huozhe/code-workflow",
        issue_num=32,
        state=state,
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
    )
    return sk


def _owner_comment(store: Store, *, did: str) -> None:
    body = json.dumps(
        {
            "action": "created",
            "issue": {"number": 32, "title": "live", "user": {"login": "huozhe"}},
            "comment": {"body": "ping", "user": {"login": "huozhe"}},
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhe"},
        }
    ).encode()
    store.insert_delivery(
        delivery_id=did,
        event="issue_comment",
        action="created",
        repo="huozhe/code-workflow",
        issue_num=32,
        sender="huozhe",
        payload=body,
        status="deferred",
    )


class _RecordingClient:
    calls: list[tuple[str, dict]] = []

    def __init__(self, *a, **k):  # noqa: ANN002, ANN003
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):  # noqa: ANN002
        return False

    def call(self, method, params=None):  # noqa: ANN001
        self.calls.append((method, dict(params or {})))
        return {"status": "done", "summary": "ok", "public_actions": []}


class _EnsureSupervisor:
    def __init__(self, store: Store, *, clobber_intake: bool = False) -> None:
        self.store = store
        self.calls = 0
        self.clobber_intake = clobber_intake

    def ensure_session(self, **k):  # noqa: ANN003
        self.calls += 1
        if self.clobber_intake:
            self.store.upsert_session(
                session_key=k["session_key"],
                project_key="huozhe/code-workflow",
                repo=k["repo"],
                issue_num=k["issue_num"],
                state="INTAKE",
                architect="huozheclaude",
                developer="huozhegrok",
                created_at=9,
                updated_at=9,
            )
        self.store.upsert_runner(
            "huozhe/code-workflow",
            container_id="c-recreated",
            endpoint="127.0.0.1:9",
            token="tok",
            tier="hot",
        )


def _run(store: Store, tmp: Path, supervisor, *, posts: list | None = None) -> DesignLoop:
    import agentd.design_loop as dl

    def _post(**k):  # noqa: ANN003
        if posts is not None:
            posts.append(k)
        return 1

    _RecordingClient.calls = []
    loop = DesignLoop(
        store,
        _cfg(tmp),
        supervisor=supervisor,
        dispatch_turns=True,
        post_comment=_post,
        gateway_token="gw",
    )
    orig = dl.RunnerClient
    dl.RunnerClient = _RecordingClient  # type: ignore[misc]
    try:
        loop.process_deferred_batch()
    finally:
        dl.RunnerClient = orig  # type: ignore[misc]
    return loop


def test_no_runner_row_calls_ensure_and_dispatches(tmp_path: Path) -> None:
    """#78: missing runners row must recreate, not mark routed with no turn."""
    store = Store(tmp_path / "state.db")
    sk = _seed_session(store)
    sup = _EnsureSupervisor(store)
    _owner_comment(store, did="d-norow")
    _run(store, tmp_path, sup)
    assert sup.calls == 1
    assert _RecordingClient.calls
    assert any(m == "turn.dispatch" for m, _ in _RecordingClient.calls)
    row = store._conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", ("d-norow",)
    ).fetchone()
    assert row["status"] == "routed"
    turns = store._conn.execute(
        "SELECT COUNT(*) AS n FROM turns WHERE session_key=?", (sk,)
    ).fetchone()
    assert int(turns["n"]) >= 1
    store.close()


def test_stale_runner_row_still_calls_ensure_on_turn(tmp_path: Path) -> None:
    """#78: a runners row is not reachability. Always probe."""
    store = Store(tmp_path / "state.db")
    _seed_session(store)
    store.upsert_runner(
        "huozhe/code-workflow",
        container_id="dead",
        endpoint="127.0.0.1:1",
        token="stale",
        tier="hot",
    )
    sup = _EnsureSupervisor(store)
    _owner_comment(store, did="d-stale")
    _run(store, tmp_path, sup)
    assert sup.calls == 1
    assert any(m == "turn.dispatch" for m, _ in _RecordingClient.calls)
    store.close()


def test_ensure_failure_leaves_deferred_not_routed(tmp_path: Path) -> None:
    """#78: no turn → do not claim routed."""
    store = Store(tmp_path / "state.db")
    _seed_session(store)

    class Boom:
        def ensure_session(self, **k):  # noqa: ANN003
            raise RuntimeError("docker daemon down")

    _owner_comment(store, did="d-boom")
    import agentd.design_loop as dl

    dl._teardown_attempts.clear()
    _run(store, tmp_path, Boom())
    row = store._conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", ("d-boom",)
    ).fetchone()
    assert row["status"] == "deferred"
    turns = store._conn.execute("SELECT COUNT(*) AS n FROM turns").fetchone()
    assert int(turns["n"]) == 0
    store.close()


def test_capacity_refusal_stays_typed_and_deferred(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _seed_session(store)

    class Cap:
        def ensure_session(self, **k):  # noqa: ANN003
            raise CapacityRefusal("max_hot_containers=2 reached (hot=2)")

    _owner_comment(store, did="d-cap")
    import agentd.design_loop as dl

    dl._teardown_attempts.clear()
    _run(store, tmp_path, Cap())
    row = store._conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", ("d-cap",)
    ).fetchone()
    assert row["status"] == "deferred"
    sess = store.get_session("huozhe/code-workflow#32")
    assert sess is not None
    assert sess["state"] == "IMPLEMENTING"
    store.close()


def test_ensure_does_not_clobber_session_state(tmp_path: Path) -> None:
    """#73: ensure_session upsert INTAKE must not flip a live session."""
    store = Store(tmp_path / "state.db")
    _seed_session(store, state="IMPLEMENTING")
    sup = _EnsureSupervisor(store, clobber_intake=True)
    _owner_comment(store, did="d-state")
    _run(store, tmp_path, sup)
    assert sup.calls == 1
    sess = store.get_session("huozhe/code-workflow#32")
    assert sess is not None
    assert sess["state"] == "IMPLEMENTING"
    store.close()


def test_ensure_failure_exhausts_and_escalates(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _seed_session(store)
    posts: list = []

    class Boom:
        def ensure_session(self, **k):  # noqa: ANN003
            raise RuntimeError("still down")

    import agentd.design_loop as dl

    dl._teardown_attempts.clear()
    _owner_comment(store, did="d-ex")
    for _ in range(5):
        store._conn.execute(
            "UPDATE deliveries SET status='deferred' WHERE delivery_id=?",
            ("d-ex",),
        )
        store._conn.commit()
        _run(store, tmp_path, Boom(), posts=posts)
    row = store._conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", ("d-ex",)
    ).fetchone()
    assert row["status"] == "done"
    assert posts
    assert any("still failing after 5 attempts" in (p.get("body") or "") for p in posts)
    store.close()
