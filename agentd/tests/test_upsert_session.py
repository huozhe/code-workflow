"""#72: upsert_session must not zero roles_locked or loop-safety counters."""

from __future__ import annotations

from pathlib import Path

from agentd.db import Store


def _insert(store: Store, **extra: object) -> str:
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
        **extra,  # type: ignore[arg-type]
    )
    return sk


def test_fresh_insert_defaults_counters_to_zero(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _insert(store)
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["roles_locked"] == 0
    assert sess["turn_count"] == 0
    assert sess["consec_agent_turns"] == 0
    assert sess["review_rounds"] == 0
    assert sess["progress_repeat"] == 0
    assert sess["zero_thread_rounds"] == 0
    store.close()


def test_upsert_without_counters_preserves_stored_values(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _insert(
        store,
        roles_locked=1,
        turn_count=25,
        consec_agent_turns=25,
        review_rounds=3,
        progress_repeat=2,
        zero_thread_rounds=1,
    )
    # ensure_session path: upsert again with no counter args (state=INTAKE).
    store.upsert_session(
        session_key=sk,
        project_key="huozhe/code-workflow",
        repo="huozhe/code-workflow",
        issue_num=32,
        state="INTAKE",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=9,
        updated_at=9,
    )
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["roles_locked"] == 1
    assert sess["turn_count"] == 25
    assert sess["consec_agent_turns"] == 25
    assert sess["review_rounds"] == 3
    assert sess["progress_repeat"] == 2
    assert sess["zero_thread_rounds"] == 1
    store.close()


def test_upsert_does_not_clobber_state(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _insert(store)
    store.upsert_session(
        session_key=sk,
        project_key="huozhe/code-workflow",
        repo="huozhe/code-workflow",
        issue_num=32,
        state="INTAKE",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=9,
        updated_at=9,
    )
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "IMPLEMENTING"
    store.close()
