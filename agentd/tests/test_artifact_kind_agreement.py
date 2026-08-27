"""#192 — `artifacts.kind` is agent-supplied, and the two reclaim paths must agree.

Before this change, a row registered with a kind neither path named was
reclaimable by teardown (`else: not Path(ref).exists()`) and permanently
un-reclaimable by GC (`return False`). Measured on the real writer and both real
predicates, with a ref that does not exist:

    scratch    gc_stale=True   confirm_gone=True
    worktree   gc_stale=True   confirm_gone=True
    log        gc_stale=False  confirm_gone=True   <-- disagree
    pr         gc_stale=False  confirm_gone=True   <-- disagree

The agreement asserted here is worthless if both sides simply answer False for
everything, so every agreement test also shows the harness producing a **True**
for the known kinds against the same fixture.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, ClassVar, Self

import pytest

from agentd.config import Config
from agentd.db import ARTIFACT_KINDS, Store
from agentd.gc import GarbageCollector
from agentd.session_loop import SessionLoop

SESSION = "o/r#1"


def _cfg(tmp: Path) -> Config:
    return Config(
        raw={
            "host": {"owner": "huozhe"},
            "agents": {"claude": {"login": "a"}, "grok": {"login": "d"}},
        },
        root=tmp,
    )


def _seeded(tmp: Path, *, state: str = "TEARDOWN") -> tuple[Store, SessionLoop]:
    store = Store(tmp / "state.db")
    store.upsert_session(
        session_key=SESSION,
        project_key="o/r",
        repo="o/r",
        issue_num=1,
        state=state,
        architect="a",
        developer="d",
        created_at=1_000_000,
        updated_at=1_000_000,
    )
    loop = SessionLoop(
        store,
        _cfg(tmp),
        post_comment=lambda **k: 1,
        get_issue_fn=lambda **k: {"body": "", "state": "closed"},
        gateway_token="gw",
    )
    return store, loop


def test_every_kind_gets_the_same_answer_from_both_paths(tmp_path: Path) -> None:
    """The exit condition: no kind for which GC and teardown disagree."""
    store, loop = _seeded(tmp_path)
    missing = str(tmp_path / "does-not-exist")

    # every kind through the real writer — unknown ones are kept, not refused
    for kind in ("scratch", "worktree", "log", "pr"):
        store.register_artifact(
            session_key=SESSION, role="architect", kind=kind, ref=missing
        )

    gc_answers = {
        str(r["kind"]): GarbageCollector._ledger_row_is_stale(
            str(r["kind"]), str(r["ref"]), None
        )
        for r in store.list_artifacts(SESSION, open_only=True)
    }
    loop._confirm_teardown_artifacts(SESSION, "o/r")
    still_open = {str(r["kind"]) for r in store.list_artifacts(SESSION, open_only=True)}
    confirm = {k: k not in still_open for k in gc_answers}

    # Fixture proof: this harness can produce a True. Without this leg the
    # agreement below passes on an implementation that answers False for
    # everything, which is the failure this repository keeps meeting.
    assert gc_answers["scratch"] is True and confirm["scratch"] is True
    assert gc_answers["worktree"] is True and confirm["worktree"] is True

    for kind, gc_stale in gc_answers.items():
        assert gc_stale == confirm[kind], (
            f"{kind}: gc_stale={gc_stale} confirm_gone={confirm[kind]}"
        )
    # and the unknown ones agree by *refusing*, not by guessing
    assert gc_answers["log"] is False and confirm["log"] is False
    assert gc_answers["pr"] is False and confirm["pr"] is False
    store.close()


def test_unknown_kind_is_kept_and_warned_not_dropped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Refusing the write would make the artifact invisible, not safe.

    Every mechanism that surfaces an unreclaimed artifact is ledger-driven, so a
    dropped row leaves the directory on disk with `removed_at IS NULL → 0`
    reading clean over it. `artifact.register` is a notification, so a refusal
    never reaches the agent either — the row itself is the visibility.
    """
    store, _ = _seeded(tmp_path)
    ref = str(tmp_path / "x")

    with caplog.at_level(logging.WARNING, logger="agentd.db"):
        store.register_artifact(
            session_key=SESSION, role="architect", kind="log", ref=ref
        )
    rows = store.list_artifacts(SESSION, open_only=True)
    assert [str(r["kind"]) for r in rows] == ["log"], "the row must be kept"
    assert any("is not one of" in r.message for r in caplog.records)

    # #167's three arms are untouched: each still registers, and silently.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="agentd.db"):
        for kind in sorted(ARTIFACT_KINDS):
            store.register_artifact(
                session_key=SESSION, role="architect", kind=kind, ref=f"{ref}-{kind}"
            )
    assert {str(r["kind"]) for r in store.list_artifacts(SESSION, open_only=True)} == {
        "log",
        *ARTIFACT_KINDS,
    }
    assert caplog.records == [], "a known kind must not warn"
    store.close()


class _NotifyingClient:
    """Fake runner that fires `artifact.register` through the real closure.

    `_dispatch_turn` builds its `RunnerClient` with `on_notification=`, so
    capturing that kwarg drives the gateway's real handler rather than a
    re-implementation of it.
    """

    sent: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, *a: Any, **k: Any) -> None:
        self._notify = k.get("on_notification")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a: object) -> bool:
        return False

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method == "health.ping":
            return {"ok": True, "initialized": True}
        if method == "turn.dispatch":
            for art in self.sent:
                assert self._notify is not None
                self._notify("artifact.register", art)
            return {"status": "done", "summary": "ok", "public_actions": []}
        return {}


def test_notify_path_keeps_an_unknown_kind_and_the_turn_survives(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Driven through the real `_on_runner_notify`, not a copy of it."""
    import agentd.session_loop as dl

    store, loop = _seeded(tmp_path, state="PLANNING")
    store.upsert_runner(
        "o/r", container_id="c1", endpoint="127.0.0.1:9", token="tok", tier="hot"
    )
    good = str(tmp_path / "scratch-dir")
    _NotifyingClient.sent = [
        {"role": "architect", "kind": "scratch", "ref": good},
        {"role": "architect", "kind": "log", "ref": str(tmp_path / "turn.log")},
    ]
    monkeypatch.setattr(dl, "RunnerClient", _NotifyingClient)

    with caplog.at_level(logging.WARNING, logger="agentd.db"):
        result = loop._dispatch_turn(
            session_key=SESSION,
            role="architect",
            turn_id="t-1",
            delivery_id="d-1",
            dig={},
            issue_num=1,
        )

    # Fixture proof: the notification really reached the real writer — the
    # valid one landed. Without it, "no log row" would also be true of a
    # fixture that never delivered a notification at all.
    kinds = {str(r["kind"]) for r in store.list_artifacts(SESSION, open_only=True)}
    assert kinds == {"scratch", "log"}, kinds
    assert (result or {}).get("status") == "done", "an odd artifact must not fail a turn"
    store.close()


def test_teardown_leaves_an_unknown_row_open_and_reports_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Refusing to guess is visible: the row stays open and the leak is logged."""
    store, loop = _seeded(tmp_path)
    store.register_artifact(
        session_key=SESSION, role="architect", kind="log", ref=str(tmp_path / "gone.log")
    )

    with caplog.at_level(logging.WARNING, logger="agentd.session_loop"):
        loop._confirm_teardown_artifacts(SESSION, "o/r")
        loop._log_teardown_leaks(SESSION)

    open_rows = store.list_artifacts(SESSION, open_only=True)
    assert [str(r["kind"]) for r in open_rows] == ["log"]
    assert any("unknown artifact kind" in r.message for r in caplog.records)
    assert any("teardown leak" in r.message for r in caplog.records)
    store.close()
