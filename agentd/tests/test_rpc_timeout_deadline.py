"""#34: gateway RPC timeout must outlive turn deadline; record + busy on residual."""

from __future__ import annotations

import json
import time
from pathlib import Path

from agentd.config import Config
from agentd.db import Store
from agentd.design_loop import DesignLoop, _role_busy_until, _role_key
from agentd.gitops import project_path


def _cfg(tmp_path: Path, **gateway: object) -> Config:
    raw: dict = {
        "host": {"owner": "o"},
        "agents": {
            "claude": {"login": "a"},
            "grok": {"login": "d"},
        },
        "gateway": {
            "turn_deadline_s": 900,
            "rpc_timeout_grace_s": 60,
            **gateway,
        },
    }
    return Config(raw=raw, root=tmp_path)


def _seed(store: Store, tmp_path: Path, *, issue: int = 1) -> None:
    store.upsert_session(
        session_key=f"o/r#{issue}",
        project_key="o/r",
        repo="o/r",
        issue_num=issue,
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


def test_rpc_timeout_derived_from_deadline() -> None:
    cfg = Config(
        raw={"gateway": {"turn_deadline_s": 100, "rpc_timeout_grace_s": 7}},
        root=Path("/tmp"),
    )
    assert cfg.turn_deadline_s == 100
    assert cfg.rpc_timeout_grace_s == 7
    assert cfg.rpc_timeout_s == 107.0


def test_rpc_timeout_defaults() -> None:
    cfg = Config(raw={}, root=Path("/tmp"))
    assert cfg.turn_deadline_s == 900
    assert cfg.rpc_timeout_grace_s == 60
    assert cfg.rpc_timeout_s == 960.0


def test_dispatch_sends_config_deadline_and_derived_timeout(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path, turn_deadline_s=42, rpc_timeout_grace_s=8)
    _seed(store, tmp_path)
    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *a, **k):  # noqa: ANN002, ANN003
            seen["timeout_s"] = k.get("timeout_s")

        def __enter__(self):
            return self

        def __exit__(self, *a):  # noqa: ANN002
            return False

        def call(self, method, params=None):  # noqa: ANN001
            seen["method"] = method
            seen["deadline_s"] = (params or {}).get("deadline_s")
            return {"status": "done", "summary": "ok"}

    import agentd.design_loop as dl

    orig = dl.RunnerClient
    dl.RunnerClient = FakeClient  # type: ignore[misc, assignment]
    try:
        loop = DesignLoop(store, cfg, supervisor=None, dispatch_turns=True)
        out = loop._dispatch_turn(
            session_key="o/r#1",
            role="architect",
            turn_id="t-ok",
            delivery_id="d1",
            dig={"kind": "test"},
            issue_num=1,
        )
    finally:
        dl.RunnerClient = orig  # type: ignore[misc]

    assert seen["timeout_s"] == 50.0  # 42 + 8
    assert seen["deadline_s"] == 42
    assert seen["method"] == "turn.dispatch"
    assert out is not None and out["status"] == "done"
    row = store._conn.execute(
        "SELECT status, summary FROM turns WHERE turn_id=?", ("t-ok",)
    ).fetchone()
    assert row["status"] == "done"
    store.close()


def test_gateway_timeout_records_turn_and_transcript(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    # Short deadline so busy-until is testable without long sleeps.
    cfg = _cfg(tmp_path, turn_deadline_s=2, rpc_timeout_grace_s=0)
    _seed(store, tmp_path)
    _role_busy_until.clear()

    class TimeoutClient:
        def __init__(self, *a, **k):  # noqa: ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):  # noqa: ANN002
            return False

        def call(self, method, params=None):  # noqa: ANN001
            raise TimeoutError("timed out")

    import agentd.design_loop as dl

    orig = dl.RunnerClient
    dl.RunnerClient = TimeoutClient  # type: ignore[misc, assignment]
    try:
        loop = DesignLoop(store, cfg, supervisor=None, dispatch_turns=True)
        t0 = time.time()
        out = loop._dispatch_turn(
            session_key="o/r#1",
            role="developer",
            turn_id="t-to",
            delivery_id="d-to",
            dig={"kind": "design_pr_opened"},
            issue_num=1,
        )
    finally:
        dl.RunnerClient = orig  # type: ignore[misc]

    assert out is not None
    assert out["status"] == "gateway_timeout"

    row = store._conn.execute(
        "SELECT ended_at, status, summary FROM turns WHERE turn_id=?", ("t-to",)
    ).fetchone()
    assert row is not None
    assert row["status"] == "gateway_timeout"
    assert row["ended_at"] is not None
    assert "timed out" in (row["summary"] or "").lower()

    # No phantom -err row
    n = store._conn.execute("SELECT COUNT(*) AS c FROM turns").fetchone()["c"]
    assert n == 1

    transcript = (
        project_path(tmp_path, "o/r") / "sessions" / "1" / "developer" / "transcript.jsonl"
    )
    assert transcript.is_file()
    lines = [json.loads(x) for x in transcript.read_text().splitlines() if x.strip()]
    assert len(lines) == 1
    assert lines[0]["turn_id"] == "t-to"
    assert lines[0]["status"] == "gateway_timeout"
    assert lines[0]["source"] == "gateway"
    assert lines[0]["kind"] == "design_pr_opened"

    rkey = _role_key("o/r", "developer")
    assert rkey in _role_busy_until
    # Residual covers remaining runner deadline from turn start.
    assert _role_busy_until[rkey] >= t0 + 1.5
    store.close()


def test_gateway_timeout_role_busy_returns_immediately(tmp_path: Path) -> None:
    """Second dispatch returns role_busy without sleeping (PR #37 B1)."""
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path, turn_deadline_s=30, rpc_timeout_grace_s=0)
    _seed(store, tmp_path)
    _role_busy_until.clear()

    calls = {"n": 0}

    class FlakyClient:
        def __init__(self, *a, **k):  # noqa: ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):  # noqa: ANN002
            return False

        def call(self, method, params=None):  # noqa: ANN001
            if method != "turn.dispatch":
                return {"ok": True}
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("timed out")
            return {"status": "done", "summary": "recovered"}

    import agentd.design_loop as dl

    orig = dl.RunnerClient
    dl.RunnerClient = FlakyClient  # type: ignore[misc, assignment]
    try:
        loop = DesignLoop(store, cfg, supervisor=None, dispatch_turns=True)
        t0 = time.time()
        r1 = loop._dispatch_turn(
            session_key="o/r#1",
            role="architect",
            turn_id="t-a",
            delivery_id="d-a",
            dig={"kind": "test"},
            issue_num=1,
        )
        r2 = loop._dispatch_turn(
            session_key="o/r#1",
            role="architect",
            turn_id="t-b",
            delivery_id="d-b",
            dig={"kind": "test"},
            issue_num=1,
        )
        elapsed = time.time() - t0
        # After busy expires (or is cleared), a third dispatch may proceed.
        _role_busy_until.clear()
        r3 = loop._dispatch_turn(
            session_key="o/r#1",
            role="architect",
            turn_id="t-c",
            delivery_id="d-c",
            dig={"kind": "test"},
            issue_num=1,
        )
    finally:
        dl.RunnerClient = orig  # type: ignore[misc]
        _role_busy_until.clear()

    assert r1 is not None and r1["status"] == "gateway_timeout"
    assert r2 is not None and r2["status"] == "role_busy"
    assert r3 is not None and r3["status"] == "done"
    assert calls["n"] == 2  # second attempt never opened RPC
    # Must not block the drain thread for the residual deadline.
    assert elapsed < 1.0
    store.close()


def test_connect_failure_does_not_mark_busy(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = _cfg(tmp_path, turn_deadline_s=30, rpc_timeout_grace_s=0)
    _seed(store, tmp_path)
    _role_busy_until.clear()

    class BoomClient:
        def __init__(self, *a, **k):  # noqa: ANN002, ANN003
            pass

        def __enter__(self):
            raise ConnectionRefusedError("refused")

        def __exit__(self, *a):  # noqa: ANN002
            return False

    import agentd.design_loop as dl

    orig = dl.RunnerClient
    dl.RunnerClient = BoomClient  # type: ignore[misc, assignment]
    try:
        loop = DesignLoop(store, cfg, supervisor=None, dispatch_turns=True)
        out = loop._dispatch_turn(
            session_key="o/r#1",
            role="architect",
            turn_id="t-conn",
            delivery_id="d-c",
            dig={"kind": "test"},
            issue_num=1,
        )
    finally:
        dl.RunnerClient = orig  # type: ignore[misc]

    assert out is not None and out["status"] == "failed"
    assert _role_key("o/r", "architect") not in _role_busy_until
    row = store._conn.execute(
        "SELECT status FROM turns WHERE turn_id=?", ("t-conn",)
    ).fetchone()
    assert row["status"] == "failed"
    store.close()
