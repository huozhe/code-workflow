"""M6-1c / ADR-22: retire or resume interrupted turns."""

from __future__ import annotations

import logging
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
    resumes: list | None = None,
    resume_err: Exception | None = None,
    missed: list | None = None,
) -> tuple[Reconciler, dict]:
    calls: list = resumes if resumes is not None else []

    def resume(turn: dict) -> None:
        calls.append(dict(turn))
        if resume_err is not None:
            raise resume_err
        store.finish_turn(
            str(turn["turn_id"]), ended_at=now, status="done", summary="resumed"
        )

    rec = Reconciler(
        store,
        list_containers=lambda: [],
        remove_container=lambda _c: None,
        resume_turn=resume,
        notify_missed=lambda sk, n: missed.append((sk, n)) if missed is not None else None,
        now_fn=lambda: now,
        resume_max_age_s=RESUME_MAX_AGE_S,
    )
    return rec, rec.reconcile_once(dry_run=dry_run)


def test_old_turn_retires_without_resume(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _sess(store)
    _turn(store, sk, started_at=1)
    resumes: list = []
    _, report = _rec(store, now=1 + RESUME_MAX_AGE_S + 10, resumes=resumes)
    assert resumes == []
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
    resumes: list = []
    _rec(store, now=now, resumes=resumes)
    assert resumes == []
    row = next(t for t in store.list_turns(sk))
    assert row["status"] == "interrupted"
    store.close()


def test_young_running_turn_resumes_once(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _sess(store, state="IMPLEMENTING")
    now = 10_000
    _turn(store, sk, started_at=now - 30)
    resumes: list = []
    rec, report = _rec(store, now=now, resumes=resumes)
    assert report["resumed"] == 1
    assert report["retired"] == 0
    assert len(resumes) == 1
    assert resumes[0]["turn_id"] == "t-open"
    rec.reconcile_once()
    rec.reconcile_once()
    assert len(resumes) == 1
    store.close()


def test_resume_fails_twice_then_retires(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _sess(store, state="IMPLEMENTING")
    now = 10_000
    _turn(store, sk, started_at=now - 30)
    esc: list = []
    rec = Reconciler(
        store,
        list_containers=lambda: [],
        remove_container=lambda _c: None,
        resume_turn=lambda _t: (_ for _ in ()).throw(RuntimeError("rpc down")),
        escalate=lambda sk, reason, **kw: esc.append((sk, reason)),
        now_fn=lambda: now,
        resume_max_age_s=RESUME_MAX_AGE_S,
    )
    rec.reconcile_once()
    assert store.list_open_turns()
    rec.reconcile_once()
    row = next(t for t in store.list_turns(sk))
    assert row["status"] == "interrupted"
    assert row["ended_at"] is not None
    assert esc
    store.close()


def test_dry_run_does_not_retire_or_resume(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _sess(store)
    _turn(store, sk, started_at=1)
    resumes: list = []
    _, report = _rec(
        store, now=1 + RESUME_MAX_AGE_S + 10, resumes=resumes, dry_run=True
    )
    assert report["retired"] == 1
    assert resumes == []
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
