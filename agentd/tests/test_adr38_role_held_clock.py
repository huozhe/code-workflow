"""#230: `role held` is ADR-38's third deferred-return site and had no clock.

The hold's own deadline is known — `held_until` — so this site does not need
backoff. It needs the exact time. The restart case is the trap: the hold lives
in memory and is forgotten on restart, the clock lives in the DB and is not.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agentd.config import Config
from agentd.db import Store
from agentd.session_loop import SessionLoop, _role_busy_until, _role_key

_REPO = "huozhe/code-workflow"
_ISSUE = 84


@pytest.fixture(autouse=True)
def _clear_holds():
    """`_role_busy_until` is a module global. Leaking a live hold out of this
    file parks every later test's drain on `role held`."""
    _role_busy_until.clear()
    yield
    _role_busy_until.clear()


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
    sk = f"{_REPO}#{_ISSUE}"
    store.upsert_session(
        session_key=sk,
        project_key=_REPO,
        repo=_REPO,
        issue_num=_ISSUE,
        state="PLANNING",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
    )
    store.upsert_runner(
        _REPO, container_id="c1", endpoint="127.0.0.1:9", token="tok", tier="hot"
    )
    return sk


def _insert_comment(store: Store, *, did: str) -> None:
    payload = json.dumps(
        {
            "action": "created",
            "issue": {"number": _ISSUE, "title": "session"},
            "comment": {"body": "go", "user": {"login": "huozhe"}},
            "repository": {"full_name": _REPO},
            "sender": {"login": "huozhe"},
        }
    ).encode()
    store.insert_delivery(
        delivery_id=did,
        event="issue_comment",
        action="created",
        repo=_REPO,
        issue_num=_ISSUE,
        sender="huozhe",
        payload=payload,
        status="deferred",
    )


def _loop(store: Store, tmp: Path) -> SessionLoop:
    return SessionLoop(
        store, _cfg(tmp), supervisor=object(), dispatch_turns=True, gateway_token="gw"
    )


def _hold(store: Store, *, seconds: int) -> float:
    """Park the architect role, the way a quota refusal does (ADR-33)."""
    sess = store.get_session(f"{_REPO}#{_ISSUE}")
    assert sess is not None
    until = time.time() + seconds
    _role_busy_until[_role_key(_REPO, "architect")] = until
    return until


def test_role_held_delivery_is_not_repicked_until_the_hold_expires(
    tmp_path: Path,
) -> None:
    """#230 acceptance. Assert the row is not *returned* — a suppressed log line
    looks identical from the outside and is the wrong fix."""
    store = Store(tmp_path / "state.db")
    _seed(store)
    _insert_comment(store, did="d-held")
    _role_busy_until.clear()
    until = _hold(store, seconds=3600)

    loop = _loop(store, tmp_path)
    loop.process_deferred_batch()

    # Prove the fixture reached the case: the row is still deferred (held, not
    # consumed), and the hold is genuinely in the future.
    row = store._conn.execute(
        "SELECT status, next_attempt_at FROM deliveries WHERE delivery_id='d-held'"
    ).fetchone()
    assert row["status"] == "deferred"
    assert until > time.time()

    assert store.list_deferred() == []
    assert int(row["next_attempt_at"]) == int(until)
    store.close()


def test_role_held_delivery_is_repicked_once_after_the_hold(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _seed(store)
    _insert_comment(store, did="d-expired")
    _role_busy_until.clear()
    _hold(store, seconds=1)

    loop = _loop(store, tmp_path)
    loop.process_deferred_batch()
    assert store.list_deferred() == []

    _role_busy_until.clear()  # hold expired
    store._conn.execute(
        "UPDATE deliveries SET next_attempt_at = ? WHERE delivery_id='d-expired'",
        (int(time.time()) - 1,),
    )
    store._conn.commit()
    assert [r["delivery_id"] for r in store.list_deferred()] == ["d-expired"]
    store.close()


def test_a_restart_does_not_strand_the_delivery_the_hold_parked(
    tmp_path: Path,
) -> None:
    """The trap. `_role_busy_until` is in-memory and forgotten on restart
    (`session_loop.py:271` says so); `next_attempt_at` is persisted. Writing the
    exact hold without clearing it on startup turns 2,065 noisy lines into a
    silently stranded delivery — a worse bug than the one being fixed."""
    store = Store(tmp_path / "state.db")
    _seed(store)
    _insert_comment(store, did="d-restart")
    _role_busy_until.clear()
    _hold(store, seconds=4 * 3600)

    loop = _loop(store, tmp_path)
    loop.process_deferred_batch()
    assert store.list_deferred() == []

    # Restart: the in-memory hold is gone, the DB clock is not.
    _role_busy_until.clear()
    store.clear_deferred_clocks()

    assert [r["delivery_id"] for r in store.list_deferred()] == ["d-restart"]
    store.close()


def test_the_hold_line_still_logs_once_per_attempt(tmp_path: Path, caplog) -> None:
    """A long hold must stay visible; the fix is fewer attempts, not a quieter log."""
    import logging

    store = Store(tmp_path / "state.db")
    _seed(store)
    _insert_comment(store, did="d-log")
    _role_busy_until.clear()
    _hold(store, seconds=3600)

    loop = _loop(store, tmp_path)
    with caplog.at_level(logging.INFO, logger="agentd.session_loop"):
        loop.process_deferred_batch()
    assert sum("role held" in r.message for r in caplog.records) == 1
    store.close()


def test_role_held_still_makes_no_stall_observation(tmp_path: Path) -> None:
    """Unrelated property at session_loop.py:1034-1038; must not change."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store)
    _insert_comment(store, did="d-stall")
    _role_busy_until.clear()
    _hold(store, seconds=3600)

    loop = _loop(store, tmp_path)
    loop.process_deferred_batch()

    sess = store.get_session(sk)
    assert sess is not None
    assert int(sess.get("silent_turns") or 0) == 0
    assert int(sess.get("zero_thread_rounds") or 0) == 0
    assert int(sess.get("review_rounds") or 0) == 0
    store.close()
