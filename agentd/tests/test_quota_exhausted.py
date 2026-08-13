"""#86: quota_exhausted defers; does not consume the delivery or charge budget."""

from __future__ import annotations

import json
import time
from pathlib import Path

from agentd.config import Config
from agentd.db import Store
from agentd.design_loop import DesignLoop, _role_busy_until, _role_key


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


def _seed(store: Store) -> str:
    sk = "huozhe/code-workflow#84"
    store.upsert_session(
        session_key=sk,
        project_key="huozhe/code-workflow",
        repo="huozhe/code-workflow",
        issue_num=84,
        state="PLANNING",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
    )
    store.upsert_runner(
        "huozhe/code-workflow",
        container_id="c1",
        endpoint="127.0.0.1:9",
        token="tok",
        tier="hot",
    )
    return sk


def _insert_comment(store: Store, *, did: str) -> None:
    payload = json.dumps(
        {
            "action": "created",
            "issue": {"number": 84, "title": "session"},
            "comment": {"body": "go", "user": {"login": "huozhe"}},
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhe"},
        }
    ).encode()
    store.insert_delivery(
        delivery_id=did,
        event="issue_comment",
        action="created",
        repo="huozhe/code-workflow",
        issue_num=84,
        sender="huozhe",
        payload=payload,
        status="deferred",
    )


def _status(store: Store, did: str) -> str:
    row = store._conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", (did,)
    ).fetchone()
    assert row is not None
    return str(row["status"])


class _StatusClient:
    def __init__(self, payload: dict, calls: list) -> None:
        self._payload = payload
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *a):  # noqa: ANN002
        return False

    def call(self, method, params=None):  # noqa: ANN001
        self._calls.append(method)
        if method != "turn.dispatch":
            return {"ok": True}
        return dict(self._payload)


def _run(store: Store, tmp: Path, client_cls: type) -> DesignLoop:
    import agentd.design_loop as dl

    loop = DesignLoop(
        store, _cfg(tmp), supervisor=object(), dispatch_turns=True, gateway_token="gw"
    )
    orig = dl.RunnerClient
    dl.RunnerClient = client_cls  # type: ignore[misc]
    try:
        loop.process_deferred_batch()
    finally:
        dl.RunnerClient = orig  # type: ignore[misc]
    return loop


def test_quota_exhausted_defers_and_does_not_charge_budget(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store)
    _insert_comment(store, did="d-q")
    _role_busy_until.clear()
    retry = int(time.time()) + 3600
    calls: list[str] = []

    class C(_StatusClient):
        def __init__(self, *a, **k):  # noqa: ANN002, ANN003
            super().__init__(
                {
                    "status": "quota_exhausted",
                    "retry_after": retry,
                    "summary": "You've hit your session limit",
                    "public_actions": [],
                },
                calls,
            )

    _run(store, tmp_path, C)
    assert _status(store, "d-q") == "deferred"
    sess = store.get_session(sk)
    assert sess is not None
    assert int(sess.get("turn_count") or 0) == 0
    assert int(sess.get("consec_agent_turns") or 0) == 0
    assert int(sess.get("silent_turns") or 0) == 0
    rkey = _role_key("huozhe/code-workflow", "architect")
    assert _role_busy_until.get(rkey, 0) >= retry - 1

    # Redrain before retry_after: role_busy, still deferred, no second RPC.
    n_dispatch = calls.count("turn.dispatch")
    _run(store, tmp_path, C)
    assert _status(store, "d-q") == "deferred"
    assert calls.count("turn.dispatch") == n_dispatch
    store.close()


def test_quota_exhausted_redrains_after_retry_after(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store)
    _insert_comment(store, did="d-later")
    _role_busy_until.clear()
    calls: list[str] = []
    payloads = [
        {
            "status": "quota_exhausted",
            "retry_after": int(time.time()) + 3600,
            "summary": "session limit",
            "public_actions": [],
        },
        {"status": "done", "summary": "ok", "public_actions": []},
    ]

    class C:
        def __init__(self, *a, **k):  # noqa: ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):  # noqa: ANN002
            return False

        def call(self, method, params=None):  # noqa: ANN001
            calls.append(method)
            if method != "turn.dispatch":
                return {"ok": True}
            return dict(payloads[min(len([c for c in calls if c == "turn.dispatch"]) - 1, 1)])

    _run(store, tmp_path, C)
    assert _status(store, "d-later") == "deferred"
    _role_busy_until.clear()
    _run(store, tmp_path, C)
    assert _status(store, "d-later") == "routed"
    sess = store.get_session(sk)
    assert sess is not None
    assert int(sess.get("turn_count") or 0) == 1
    store.close()


def test_genuine_failed_still_consumes(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store)
    _insert_comment(store, did="d-fail")
    _role_busy_until.clear()
    calls: list[str] = []

    class C(_StatusClient):
        def __init__(self, *a, **k):  # noqa: ANN002, ANN003
            super().__init__(
                {"status": "failed", "summary": "API Error: 500", "public_actions": []},
                calls,
            )

    _run(store, tmp_path, C)
    assert _status(store, "d-fail") == "routed"
    sess = store.get_session(sk)
    assert sess is not None
    assert int(sess.get("turn_count") or 0) == 1
    store.close()


def test_unknown_status_degrades_to_failed(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store)
    _insert_comment(store, did="d-unk")
    _role_busy_until.clear()
    calls: list[str] = []

    class C(_StatusClient):
        def __init__(self, *a, **k):  # noqa: ANN002, ANN003
            super().__init__(
                {"status": "brand_new_thing", "summary": "future runner", "public_actions": []},
                calls,
            )

    _run(store, tmp_path, C)
    assert _status(store, "d-unk") == "routed"
    sess = store.get_session(sk)
    assert sess is not None
    assert int(sess.get("turn_count") or 0) == 1
    row = store._conn.execute(
        "SELECT status FROM turns WHERE session_key=? ORDER BY started_at DESC",
        (sk,),
    ).fetchone()
    assert row is not None
    assert row["status"] == "failed"
    store.close()
