"""M6-1c / ADR-22: retire or resume interrupted turns."""

from __future__ import annotations

import sys
from pathlib import Path

_RUNNER_ROOT = Path(__file__).resolve().parents[1] / "docker" / "session-runner"
if str(_RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(_RUNNER_ROOT))

from agentd.db import Store
from agentd.reconciler import RESUME_MAX_AGE_S, Reconciler


def _sess(
    store: Store,
    *,
    issue: int = 32,
    state: str = "IMPLEMENTING",
    created_at: int = 100,
    project: str = "huozhe/code-workflow",
) -> str:
    sk = f"{project}#{issue}"
    store.upsert_session(
        session_key=sk,
        project_key=project,
        repo=project,
        issue_num=issue,
        state=state,
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=created_at,
        updated_at=created_at,
    )
    return sk


def _turn(
    store: Store,
    sk: str,
    *,
    turn_id: str = "t-open",
    started_at: int,
    status: str | None = None,
) -> None:
    store.insert_turn(
        turn_id=turn_id,
        session_key=sk,
        role="developer",
        delivery_id=None,
        started_at=started_at,
        ended_at=None,
        status=status,
        summary=None,
    )


def _rec(
    store: Store,
    *,
    now: int,
    dry_run: bool = False,
    missed: list | None = None,
) -> tuple[Reconciler, dict]:
    rec = Reconciler(
        store,
        list_containers=list,
        remove_container=lambda _c: None,
        notify_missed=lambda sk, n: missed.append((sk, n)) if missed is not None else None,
        now_fn=lambda: now,
        resume_max_age_s=RESUME_MAX_AGE_S,
    )
    return rec, rec.reconcile_once(dry_run=dry_run)


def test_old_turn_retires_without_resume(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _sess(store)
    _turn(store, sk, started_at=1)
    _, report = _rec(store, now=1 + RESUME_MAX_AGE_S + 10)
    assert report["retired"] == 1
    assert report["resumed"] == 0
    row = next(t for t in store.list_turns(sk) if t["turn_id"] == "t-open")
    assert row["ended_at"] is not None
    assert row["status"] == "interrupted"
    assert store.get_session(sk)["turn_count"] == 0
    assert store.list_open_turns() == []
    store.close()


def test_paused_session_retires_young_turn(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _sess(store, state="PAUSED_HUMAN")
    now = 10_000
    _turn(store, sk, started_at=now - 30)
    _rec(store, now=now)
    row = next(t for t in store.list_turns(sk))
    assert row["status"] == "interrupted"
    store.close()


def test_young_running_turn_is_marked_not_dispatched(tmp_path: Path) -> None:
    """Reconciler hands off; it does not run the RPC (ADR-20)."""
    store = Store(tmp_path / "state.db")
    sk = _sess(store, state="IMPLEMENTING")
    now = 10_000
    _turn(store, sk, started_at=now - 30)
    rec, report = _rec(store, now=now)
    assert report["resumed"] == 1
    assert report["retired"] == 0
    open_row = next(t for t in store.list_open_turns())
    assert open_row["status"] == "resuming"
    assert int(open_row["resume_attempts"] or 0) == 1
    rec.reconcile_once()
    rec.reconcile_once()
    open_row = next(t for t in store.list_open_turns())
    assert int(open_row["resume_attempts"] or 0) == 1
    store.close()


def test_resume_fails_twice_then_retires(tmp_path: Path) -> None:
    from agentd.config import Config
    from agentd.design_loop import DesignLoop

    store = Store(tmp_path / "state.db")
    sk = _sess(store, state="IMPLEMENTING")
    now = 10_000
    _turn(store, sk, started_at=now - 30)
    rec = Reconciler(
        store,
        list_containers=list,
        remove_container=lambda _c: None,
        now_fn=lambda: now,
        resume_max_age_s=RESUME_MAX_AGE_S,
    )
    loop = DesignLoop(store, Config(), dispatch_turns=False, gateway_token="")

    def boom(_turn: dict) -> None:
        raise RuntimeError("rpc down")

    loop.resume_interrupted_turn = boom  # type: ignore[method-assign]
    rec.reconcile_once()
    assert next(t for t in store.list_open_turns())["status"] == "resuming"
    loop.process_resuming_turns()
    rec.reconcile_once()
    loop.process_resuming_turns()
    row = next(t for t in store.list_turns(sk))
    assert row["status"] == "interrupted"
    assert row["ended_at"] is not None
    assert store.get_session(sk)["state"] == "PAUSED_HUMAN"
    store.close()


def test_dry_run_does_not_retire_or_resume(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _sess(store)
    _turn(store, sk, started_at=1)
    _, report = _rec(store, now=1 + RESUME_MAX_AGE_S + 10, dry_run=True)
    assert report["retired"] == 1
    assert store.list_open_turns()
    store.close()


def test_retire_notifies_session_resume(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _sess(store, state="PAUSED_HUMAN")
    _turn(store, sk, started_at=1)
    missed: list = []
    _rec(store, now=1 + RESUME_MAX_AGE_S + 10, missed=missed)
    assert missed
    assert missed[0][0] == sk
    assert missed[0][1][0]["turn_id"] == "t-open"
    store.close()


def test_drain_skips_paused_session(tmp_path: Path) -> None:
    from agentd.config import Config
    from agentd.design_loop import DesignLoop

    store = Store(tmp_path / "state.db")
    sk = _sess(store, state="IMPLEMENTING")
    store.upsert_runner(
        "huozhe/code-workflow",
        container_id="c",
        endpoint="127.0.0.1:9",
        token="t",
        tier="hot",
    )
    now = 10_000
    _turn(store, sk, started_at=now - 30)
    _rec(store, now=now)
    store.update_session_fields(sk, state="PAUSED_HUMAN")
    loop = DesignLoop(store, Config(), dispatch_turns=False, gateway_token="")
    seen: list = []
    loop.resume_interrupted_turn = lambda t: seen.append(t)  # type: ignore[method-assign]
    n = loop.process_resuming_turns()
    assert n == 0
    assert seen == []
    row = next(t for t in store.list_turns(sk))
    assert row["ended_at"] is None
    assert row["status"] is None
    store.close()


def test_drain_runs_marked_resume(tmp_path: Path) -> None:
    import agentd.design_loop as dl
    from agentd.config import Config
    from agentd.design_loop import DesignLoop

    store = Store(tmp_path / "state.db")
    sk = _sess(store, state="IMPLEMENTING")
    store.upsert_runner(
        "huozhe/code-workflow",
        container_id="c",
        endpoint="127.0.0.1:9",
        token="t",
        tier="hot",
    )
    now = 10_000
    _turn(store, sk, started_at=now - 30)
    store.mark_turn_resuming("t-open")
    calls: list = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def call(self, method, params=None):
            calls.append(method)
            return {"status": "done", "summary": "ok"}

    orig = dl.RunnerClient
    dl.RunnerClient = FakeClient
    try:
        loop = DesignLoop(store, Config(), dispatch_turns=False, gateway_token="")
        n = loop.process_resuming_turns()
    finally:
        dl.RunnerClient = orig
    assert n == 1
    assert calls == ["turn.resume"]
    row = next(t for t in store.list_turns(sk))
    assert row["status"] == "done"
    store.close()


def test_drain_once_runs_resuming_turns(tmp_path: Path) -> None:
    import threading

    from agentd.config import Config
    from agentd.dispatcher import Dispatcher

    store = Store(tmp_path / "state.db")
    seen: list[int] = []

    class _Loop:
        def process_resuming_turns(self) -> int:
            seen.append(1)
            return 1

        def process_deferred_batch(self, limit: int = 20) -> int:
            return 0

    d = Dispatcher(store, Config(), threading.Event(), design_loop=_Loop())
    assert d.drain_once() == 1
    assert seen == [1]
    store.close()


def test_resume_takes_role_lock_and_reads_budget_fresh(tmp_path: Path) -> None:
    import agentd.design_loop as dl
    from agentd.config import Config
    from agentd.design_loop import DesignLoop, _lock_for_project_role

    store = Store(tmp_path / "state.db")
    sk = _sess(store, state="IMPLEMENTING")
    store.update_session_fields(sk, turn_count=10)
    store.upsert_runner(
        "huozhe/code-workflow",
        container_id="c",
        endpoint="127.0.0.1:9",
        token="t",
        tier="hot",
    )
    now = 10_000
    _turn(store, sk, started_at=now - 30)
    store.mark_turn_resuming("t-open")
    locks: list = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def call(self, method, params=None):
            lock = _lock_for_project_role("huozhe/code-workflow", "developer")
            locks.append(lock.locked())
            store.update_session_fields(sk, turn_count=12)
            return {"status": "done", "summary": "ok"}

    orig = dl.RunnerClient
    dl.RunnerClient = FakeClient
    try:
        loop = DesignLoop(store, Config(), dispatch_turns=False, gateway_token="")
        loop.resume_interrupted_turn(
            next(t for t in store.list_resuming_turns())
        )
    finally:
        dl.RunnerClient = orig
    assert locks == [True]
    assert store.get_session(sk)["turn_count"] == 13
    store.close()


def test_resume_timeout_sets_busy_until(tmp_path: Path) -> None:
    import time

    import agentd.design_loop as dl
    from agentd.config import Config
    from agentd.design_loop import DesignLoop, _role_busy_until, _role_key

    store = Store(tmp_path / "state.db")
    sk = _sess(store, state="IMPLEMENTING")
    store.upsert_runner(
        "huozhe/code-workflow",
        container_id="c",
        endpoint="127.0.0.1:9",
        token="t",
        tier="hot",
    )
    _turn(store, sk, started_at=int(time.time()) - 30)
    store.mark_turn_resuming("t-open")

    class Boom:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def call(self, method, params=None):
            raise TimeoutError("timed out")

    orig = dl.RunnerClient
    dl.RunnerClient = Boom
    try:
        loop = DesignLoop(
            store,
            Config(raw={"gateway": {"turn_deadline_s": 30}}),
            dispatch_turns=False,
            gateway_token="",
        )
        try:
            loop.resume_interrupted_turn(next(t for t in store.list_resuming_turns()))
        except TimeoutError:
            pass
        else:
            raise AssertionError("expected timeout")
    finally:
        dl.RunnerClient = orig
    rkey = _role_key("huozhe/code-workflow", "developer")
    assert _role_busy_until.get(rkey, 0) > time.time()
    _role_busy_until.clear()
    store.close()


def test_drain_busy_is_not_counted(tmp_path: Path) -> None:
    import time

    from agentd.config import Config
    from agentd.design_loop import DesignLoop, _role_busy_until, _role_key

    store = Store(tmp_path / "state.db")
    sk = _sess(store, state="IMPLEMENTING")
    store.upsert_runner(
        "huozhe/code-workflow",
        container_id="c",
        endpoint="127.0.0.1:9",
        token="t",
        tier="hot",
    )
    now = 10_000
    _turn(store, sk, started_at=now - 30)
    store.mark_turn_resuming("t-open")
    rkey = _role_key("huozhe/code-workflow", "developer")
    _role_busy_until[rkey] = time.time() + 60
    try:
        loop = DesignLoop(store, Config(), dispatch_turns=False, gateway_token="")
        n = loop.process_resuming_turns()
    finally:
        _role_busy_until.clear()
    assert n == 0
    row = next(t for t in store.list_turns(sk))
    assert row["status"] == "resuming"
    assert row["ended_at"] is None
    store.close()


def test_resume_prompt_instructs_rederive() -> None:
    from agentd_runner.turn import build_prompt

    text = build_prompt(
        {
            "role": "developer",
            "turn_id": "t-1",
            "session_state": "IMPLEMENTING",
            "resuming": True,
            "event": {},
            "context": {},
        },
        None,
    )
    assert "turn.resume" in text or "resuming" in text.lower()
    assert "git status" in text
    assert "worktree" in text.lower()
