"""ADR-33: a quota refusal after real work, and the delivery that pays for it.

Acceptance (5)-(8). (1), (2) and (4) are runner-side and live in
``test_cli_session.py``; (3) is ``test_claude_mentions_of_limits_are_not_quota``
surviving unchanged, which is the point of that item. (9) is inside the image
and (10) is live.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

import agentd.design_loop as design_loop_mod
from agentd.config import Config
from agentd.db import Store
from agentd.design_loop import DesignLoop
from agentd.github_fetch import PrReviewThreadSnapshot
from agentd.gitops import role_branch_name

REPO = "huozhe/code-workflow"
ISSUE = 169
DESIGN_PR = 170
LIMIT_COPY = "You've hit your session limit · resets 9:50am (UTC)"


@pytest.fixture(autouse=True)
def _clear_role_holds() -> None:
    design_loop_mod._role_busy_until.clear()
    yield
    design_loop_mod._role_busy_until.clear()


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


def _seed(store: Store, *, state: str = "DESIGN_REVIEW") -> str:
    sk = f"{REPO}#{ISSUE}"
    store.upsert_session(
        session_key=sk,
        project_key=REPO,
        repo=REPO,
        issue_num=ISSUE,
        state=state,
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
        design_pr=DESIGN_PR,
    )
    return sk


def _changes_requested_payload() -> dict[str, Any]:
    return {
        "action": "submitted",
        "review": {
            "id": 1,
            "state": "CHANGES_REQUESTED",
            "body": "please fix",
            "commit_id": "deadbeef",
            "user": {"login": "huozhegrok"},
        },
        "pull_request": {
            "number": DESIGN_PR,
            "title": "Design: x",
            "user": {"login": "huozheclaude"},
            "head": {
                "sha": "deadbeef",
                "ref": role_branch_name(REPO, ISSUE, "architect"),
            },
            "base": {"ref": "main"},
        },
        "repository": {"full_name": REPO},
        "sender": {"login": "huozhegrok"},
    }


def _insert_review(store: Store, *, did: str) -> None:
    store.insert_delivery(
        delivery_id=did,
        event="pull_request_review",
        action="submitted",
        repo=REPO,
        issue_num=DESIGN_PR,
        sender="huozhegrok",
        payload=json.dumps(_changes_requested_payload()).encode(),
        status="deferred",
    )


def _loop(store: Store, tmp: Path, *, retry_after: float | None = None) -> DesignLoop:
    """Loop whose dispatch refuses for quota, exactly as the runner now does.

    The stub also takes the role hold, which the real ``_dispatch_turn`` does at
    ``:1385``; item (7) drives that path unstubbed.
    """
    fetches: list[int] = []

    def _threads(**kwargs: Any) -> PrReviewThreadSnapshot:
        fetches.append(1)
        return PrReviewThreadSnapshot(
            open_thread_ids=["T1", "T2"],
            unresolved_count=2,
            all_thread_ids=["T1", "T2"],
            base_ref="main",
            head_oid="deadbeef",
        )

    loop = DesignLoop(
        store,
        _cfg(tmp),
        supervisor=object(),
        dispatch_turns=True,
        github_token="tok",
        gateway_token="gw",
        post_comment=lambda **k: 1,
        fetch_threads=_threads,
        fetch_diff=lambda **k: " 1 file changed",
    )
    dispatched: list[str] = []

    def _refuse(*, session_key: str, role: str, **kwargs: Any) -> dict[str, Any]:
        dispatched.append(role)
        until = retry_after if retry_after is not None else time.time() + 3600
        design_loop_mod._hold_role_for_quota(
            design_loop_mod._role_key(REPO, role),
            until,
            session_key=session_key,
            role=role,
        )
        return {
            "status": "quota_exhausted",
            "summary": LIMIT_COPY,
            "retry_after": until,
            "public_actions": [],
        }

    loop._dispatch_turn = _refuse  # type: ignore[method-assign]
    loop.dispatched = dispatched  # type: ignore[attr-defined]
    loop.thread_fetches = fetches  # type: ignore[attr-defined]
    return loop


def test_quota_refusal_leaves_the_delivery_retryable(tmp_path: Path) -> None:
    """(5) The delivery is not consumed.

    Necessary but not sufficient — it stays true all the way to the escalation
    that (6) catches, which is why (6) and not this item is the one that must
    fail first.
    """
    store = Store(tmp_path / "state.db")
    _seed(store)
    loop = _loop(store, tmp_path)
    _insert_review(store, did="d-quota")
    loop.process_deferred_batch(limit=5)

    assert loop.dispatched == ["architect"]  # type: ignore[attr-defined]
    assert store.count_by_status().get("routed", 0) == 0
    assert store.count_by_status().get("deferred", 0) == 1
    store.close()


def test_repicks_under_a_hold_are_inert(tmp_path: Path) -> None:
    """(6) ADR-33 (f): the hold is consulted above the stall observer.

    Four re-picks, because the escalation lands on the third. Without (f) this
    fails on pass three with ``stall: zero thread progress for 3 review rounds``
    — the drain's own re-picks counted as review rounds that made no progress.
    """
    store = Store(tmp_path / "state.db")
    sk = _seed(store)
    loop = _loop(store, tmp_path)
    _insert_review(store, did="d-quota")

    for _ in range(5):  # 1 dispatch + 4 re-picks under the hold
        loop.process_deferred_batch(limit=5)

    assert loop.dispatched == ["architect"]  # type: ignore[attr-defined]
    # One fetch, from the pass that actually dispatched; none from the re-picks.
    assert len(loop.thread_fetches) == 1  # type: ignore[attr-defined]
    sess = store.get_session(sk)
    assert sess is not None
    assert int(sess["zero_thread_rounds"]) == 1
    assert int(sess["review_rounds"]) == 1
    assert sess["state"] != "PAUSED_HUMAN"
    assert not sess["paused_reason"]
    assert store.count_by_status().get("deferred", 0) == 1
    store.close()


def test_expired_hold_lets_the_delivery_through(tmp_path: Path) -> None:
    """(6), other side: the hold is a delay, not a drop.

    Fails if (f) is implemented as an unconditional skip rather than a
    time-bounded one — the delivery would never be retried at all.
    """
    store = Store(tmp_path / "state.db")
    _seed(store)
    loop = _loop(store, tmp_path, retry_after=time.time() + 0.2)
    _insert_review(store, did="d-quota")
    loop.process_deferred_batch(limit=5)
    assert len(loop.dispatched) == 1  # type: ignore[attr-defined]

    design_loop_mod._role_busy_until.clear()  # stand in for the reset arriving
    loop.process_deferred_batch(limit=5)
    assert len(loop.dispatched) == 2  # type: ignore[attr-defined]
    store.close()


def test_hold_short_circuits_dispatch_without_spawning(tmp_path: Path) -> None:
    """(7) Assert the spawn did not happen, not merely that the status is right.

    Drives the real ``_dispatch_turn``: a live hold must return before
    ``get_runner``, the reachability ping and ``ensure_session``.
    """
    store = Store(tmp_path / "state.db")
    sk = _seed(store)

    class _Supervisor:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def ensure_session(self, **kwargs: Any) -> None:
            self.calls.append("ensure_session")

    sup = _Supervisor()
    loop = DesignLoop(
        store,
        _cfg(tmp_path),
        supervisor=sup,
        dispatch_turns=True,
        github_token="tok",
        gateway_token="gw",
        post_comment=lambda **k: 1,
    )
    design_loop_mod._hold_role_for_quota(
        design_loop_mod._role_key(REPO, "architect"),
        time.time() + 3600,
        session_key=sk,
        role="architect",
    )

    result = loop._dispatch_turn(
        session_key=sk,
        role="architect",
        turn_id="t-held",
        delivery_id="d-quota",
        dig={"kind": "pull_request_review.submitted"},
        issue_num=ISSUE,
    )

    assert result is not None
    assert result["status"] == "role_busy"
    assert sup.calls == []
    assert store.get_turn("t-held") is None  # no turn row: nothing was started
    store.close()


def test_refusal_spends_no_turn_budget(tmp_path: Path) -> None:
    """(8) Passing today via the ``:1014`` early return; asserted so it stays.

    ADR-33 (e) as drafted named these three counters as needing work. They did
    not — and in the same run the two that actually moved were the ones (f) is
    about, which is why (6) exists.
    """
    store = Store(tmp_path / "state.db")
    sk = _seed(store)
    loop = _loop(store, tmp_path)
    _insert_review(store, did="d-quota")
    loop.process_deferred_batch(limit=5)

    sess = store.get_session(sk)
    assert sess is not None
    assert int(sess["turn_count"]) == 0
    assert int(sess["consec_agent_turns"]) == 0
    assert int(sess["silent_turns"]) == 0
    store.close()
