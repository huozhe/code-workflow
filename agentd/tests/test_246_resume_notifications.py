"""#246: `turn.resume` passed no `on_notification`, so a resumed turn was deaf.

The runner injects the progress writer for **both** turn methods
(`docker/session-runner/agentd_runner/server.py:618`), and `RunnerClient` drains
no-id frames and then discards them when the handler is `None`
(`rpc_client.py:112`). So after #228 a dispatched turn reported `chunks=`/`bytes=`
and a resumed one reported `chunks=0` however much it streamed — worse than
reporting nothing, because `chunks=0` is the signal #228 added.

`artifact.register` is wired here too and is **inert in production**: nothing in
the runner image sends it (M3-C moved the ledger to supervisor-observed
registration, `dd35b68`), and this host has zero `artifact.register notify` lines
across every rotated log against 94 supervisor-observed rows. It is tested
because the handler must behave identically on both paths — one answer to "what
does the gateway do with a runner notification", not two.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar, Self

import pytest

import agentd.session_loop as dl
from agentd.config import Config
from agentd.db import Store
from agentd.session_loop import SessionLoop, _inflight_turn_ids, _role_busy_until

_PROJECT = "huozhe/code-workflow"
_SK = f"{_PROJECT}#32"


@pytest.fixture(autouse=True)
def _clear_module_globals() -> Iterator[None]:
    """Both are module-level and both have leaked across files before (#230, #245)."""
    _inflight_turn_ids.clear()
    _role_busy_until.clear()
    yield
    _inflight_turn_ids.clear()
    _role_busy_until.clear()


def _seeded(tmp: Path) -> tuple[Store, SessionLoop]:
    store = Store(tmp / "state.db")
    store.upsert_session(
        session_key=_SK,
        project_key=_PROJECT,
        repo=_PROJECT,
        issue_num=32,
        state="IMPLEMENTING",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=100,
        updated_at=100,
    )
    store.upsert_runner(
        _PROJECT, container_id="c", endpoint="127.0.0.1:9", token="t", tier="hot"
    )
    store.insert_turn(
        turn_id="t-open",
        session_key=_SK,
        role="developer",
        delivery_id=None,
        started_at=10_000,
        ended_at=None,
        status=None,
        summary=None,
    )
    store.mark_turn_resuming("t-open")
    loop = SessionLoop(store, Config(root=tmp), dispatch_turns=False, gateway_token="")
    return store, loop


def _resume(
    store: Store, loop: SessionLoop, client: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dl, "RunnerClient", client)
    loop.resume_interrupted_turn(next(t for t in store.list_resuming_turns()))


class _ResumeClient:
    """Fires notifications through whatever handler the resume site passes.

    `on_notification` is read from the kwargs rather than assumed, so a site that
    passes none is exercised exactly as production would be — which is the defect.
    """

    frames: ClassVar[list[dict[str, Any]]] = []
    artifacts: ClassVar[list[dict[str, Any]]] = []
    result: ClassVar[dict[str, Any]] = {"status": "done", "summary": "ok"}
    raises: ClassVar[bool] = False

    def __init__(self, *a: Any, **k: Any) -> None:
        self._notify = k.get("on_notification")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a: object) -> bool:
        return False

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self._notify is not None:
            for art in self.artifacts:
                self._notify("artifact.register", art)
            for f in self.frames:
                self._notify("notify.progress", f)
        if self.raises:
            raise TimeoutError("deadline")
        return dict(self.result)


def test_a_resumed_turn_that_streams_does_not_report_zero(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect. Against `main` the site passes no handler, so `chunks=0`."""
    store, loop = _seeded(tmp_path)
    _ResumeClient.frames = [{"role": "developer", "chunk": "abcde"}] * 3
    _ResumeClient.artifacts = []
    _ResumeClient.raises = False
    with caplog.at_level(logging.INFO, logger="agentd.session_loop"):
        _resume(store, loop, _ResumeClient, monkeypatch)

    final = [r.getMessage() for r in caplog.records if "turn progress final" in r.getMessage()]
    assert len(final) == 1, final
    assert "chunks=3" in final[0] and "bytes=15" in final[0], final[0]
    store.close()


def test_a_resumed_turn_that_times_out_still_states_its_count(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same `finally` guarantee #245 added on the dispatch side."""
    store, loop = _seeded(tmp_path)
    _ResumeClient.frames = [{"role": "developer", "chunk": "xy"}]
    _ResumeClient.artifacts = []
    _ResumeClient.raises = True
    with (
        caplog.at_level(logging.INFO, logger="agentd.session_loop"),
        pytest.raises(TimeoutError),
    ):
        _resume(store, loop, _ResumeClient, monkeypatch)

    final = [r.getMessage() for r in caplog.records if "turn progress final" in r.getMessage()]
    assert len(final) == 1, final
    assert "chunks=1" in final[0], final[0]
    store.close()


def test_artifact_register_is_handled_on_the_resume_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inert in production, identical in behaviour — that is the point.

    Nothing in the runner image emits this today. It is wired so a future emitter
    cannot be heard by one call site and ignored by the other.
    """
    store, loop = _seeded(tmp_path)
    ref = str(tmp_path / "scratch-resume")
    _ResumeClient.frames = []
    _ResumeClient.artifacts = [{"role": "developer", "kind": "scratch", "ref": ref}]
    _ResumeClient.raises = False
    _resume(store, loop, _ResumeClient, monkeypatch)
    assert [str(r["ref"]) for r in store.list_artifacts(_SK, open_only=True)] == [ref]
    store.close()


def test_result_artifacts_are_registered_on_the_resume_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third path, and the one that was asymmetric without being a notification.

    `_dispatch_turn` has registered `result["artifacts"]` since M3-C; resume never
    did. Also inert — the runner returns `"artifacts": []` on every path — but
    leaving one of two paths asymmetric is how the notification gap got here.
    """
    store, loop = _seeded(tmp_path)
    ref = str(tmp_path / "from-result")
    _ResumeClient.frames = []
    _ResumeClient.artifacts = []
    _ResumeClient.raises = False
    _ResumeClient.result = {
        "status": "done",
        "summary": "ok",
        "artifacts": [{"kind": "worktree", "ref": ref}],
    }
    try:
        _resume(store, loop, _ResumeClient, monkeypatch)
        rows = store.list_artifacts(_SK, open_only=True)
        assert [(str(r["kind"]), str(r["ref"])) for r in rows] == [("worktree", ref)]
    finally:
        _ResumeClient.result = {"status": "done", "summary": "ok"}
    store.close()


def test_both_call_sites_share_one_handler(tmp_path: Path) -> None:
    """Guard against re-divergence.

    #246 exists because two sites answered "what does the gateway do with a
    runner notification" differently — one by not asking. A grep-level assertion
    is weak, so this asserts the shared factory is what both sites call.
    """
    import inspect

    src = inspect.getsource(dl.SessionLoop)
    assert src.count("def _runner_notify(") == 1, "the handler must be defined once"
    assert (
        inspect.getsource(dl.SessionLoop.resume_interrupted_turn).count(
            "self._runner_notify("
        )
        == 1
    ), "resume must use the shared handler, not a private copy"
    assert (
        inspect.getsource(dl.SessionLoop._dispatch_turn).count("self._runner_notify(")
        == 1
    ), "dispatch must use the shared handler, not an inline closure"
