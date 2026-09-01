"""#228: `notify.progress` reached the gateway and was dropped on one line.

Every frame hit `if method != "artifact.register": return`, so a turn in flight
was opaque and the only liveness evidence was `/proc/<pid>/stat` CPU inside the
container — which reads the same for a CLI blocked on a vendor API and a wedged
one (#159, 2026-08-26: 889 ticks flat across 65 s, `state=S`).

Driven through the real `_on_runner_notify` closure, never a copy of it: the
handler is built inside `_dispatch_turn` and handed to `RunnerClient` as
`on_notification=`, so capturing that kwarg is the only way to exercise the
shipped code.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, ClassVar, Self

import pytest

import agentd.session_loop as dl
from agentd.config import Config
from agentd.db import Store
from agentd.session_loop import SessionLoop, _role_busy_until

SESSION = "o/r#1"


@pytest.fixture(autouse=True)
def _clear_role_holds():
    """A gateway timeout marks the role busy for 900 s (#34), and
    `_role_busy_until` is a module global — so the timeout tests below would park
    every later test in this file, and every later file, on `role held`. Same
    leak as #230's suite; clearing either side is the fix, not per-test dodges.
    """
    _role_busy_until.clear()
    yield
    _role_busy_until.clear()


def _cfg(tmp: Path) -> Config:
    return Config(
        raw={
            "host": {"owner": "huozhe"},
            "gateway": {"login": "huozhegateway"},
            "agents": {
                "claude": {"login": "a"},
                "grok": {"login": "d"},
            },
        },
        root=tmp,
    )


def _seeded(tmp: Path) -> tuple[Store, SessionLoop]:
    store = Store(tmp / "state.db")
    store.upsert_session(
        session_key=SESSION,
        project_key="o/r",
        repo="o/r",
        issue_num=1,
        state="PLANNING",
        architect="a",
        developer="d",
        created_at=1_000_000,
        updated_at=1_000_000,
    )
    store.upsert_runner(
        "o/r", container_id="c1", endpoint="127.0.0.1:9", token="tok", tier="hot"
    )
    loop = SessionLoop(
        store,
        _cfg(tmp),
        post_comment=lambda **k: 1,
        get_issue_fn=lambda **k: {"body": "", "state": "closed"},
        gateway_token="gw",
    )
    return store, loop


class _StreamingClient:
    """Fires `notify.progress` through the gateway's real closure."""

    frames: ClassVar[list[dict[str, Any]]] = []
    artifacts: ClassVar[list[dict[str, Any]]] = []

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
            assert self._notify is not None
            for art in self.artifacts:
                self._notify("artifact.register", art)
            for f in self.frames:
                self._notify("notify.progress", f)
            return {"status": "done", "summary": "ok", "public_actions": []}
        return {}


def _run(loop: SessionLoop, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dl, "RunnerClient", _StreamingClient)
    loop._dispatch_turn(
        session_key=SESSION,
        role="architect",
        turn_id="t-1",
        delivery_id="d-1",
        dig={},
        issue_num=1,
    )


def test_progress_frames_are_counted_not_dropped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect. Against `main` no `turn progress` line exists at all."""
    store, loop = _seeded(tmp_path)
    _StreamingClient.artifacts = []
    _StreamingClient.frames = [
        {"role": "architect", "chunk": "hello "},
        {"role": "architect", "chunk": "world"},
    ]
    with caplog.at_level(logging.INFO, logger="agentd.session_loop"):
        _run(loop, monkeypatch)

    lines = [r.getMessage() for r in caplog.records if "turn progress" in r.getMessage()]
    assert lines, "no progress line — every frame was dropped, which is #228"
    assert any("chunks=1" in m for m in lines), lines
    final = [m for m in lines if "turn progress final" in m]
    assert len(final) == 1, final
    assert "chunks=2" in final[0] and "bytes=11" in final[0], final[0]
    store.close()


def test_the_chunk_text_never_reaches_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Model output is not telemetry; the log is not the transcript."""
    store, loop = _seeded(tmp_path)
    secret = "SENTINEL-model-output-must-not-be-logged"
    _StreamingClient.artifacts = []
    _StreamingClient.frames = [{"role": "architect", "chunk": secret}]
    with caplog.at_level(logging.DEBUG, logger="agentd.session_loop"):
        _run(loop, monkeypatch)

    # Reach the case first. Without this the test is vacuous: on a build that
    # logs nothing at all, "the secret was not logged" is trivially true and the
    # assertion below proves only that the frame was dropped — which is #228.
    msgs = [r.getMessage() for r in caplog.records]
    assert any("turn progress" in m for m in msgs), "no progress line to leak from"
    assert any(f"bytes={len(secret)}" in m for m in msgs), msgs

    assert not any(secret in m for m in msgs)
    store.close()


def test_logging_is_throttled_not_per_frame(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-frame logging would recreate #208 one level up, in the gateway's log."""
    store, loop = _seeded(tmp_path)
    _StreamingClient.artifacts = []
    _StreamingClient.frames = [{"role": "architect", "chunk": "x" * 50}] * 400
    with caplog.at_level(logging.INFO, logger="agentd.session_loop"):
        _run(loop, monkeypatch)

    throttled = [
        r.getMessage()
        for r in caplog.records
        if "turn progress" in r.getMessage() and "final" not in r.getMessage()
    ]
    assert len(throttled) == 1, f"400 frames produced {len(throttled)} lines"
    final = [r.getMessage() for r in caplog.records if "turn progress final" in r.getMessage()]
    assert "chunks=400" in final[0] and "bytes=20000" in final[0], final[0]
    store.close()


def test_artifact_register_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: progress is handled *before* the artifact branch."""
    store, loop = _seeded(tmp_path)
    ref = str(tmp_path / "scratch-dir")
    _StreamingClient.artifacts = [{"role": "architect", "kind": "scratch", "ref": ref}]
    _StreamingClient.frames = [{"role": "architect", "chunk": "streaming"}]
    _run(loop, monkeypatch)
    rows = store.list_artifacts(SESSION, open_only=True)
    assert [str(r["ref"]) for r in rows] == [ref], rows
    store.close()


def test_a_turn_with_no_frames_still_reports_zero(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`chunks=0` on a completed turn is the signal, and it must be *stated*.

    A missing line is indistinguishable from a gateway that never counted, which
    is the reading #228 exists to remove.
    """
    store, loop = _seeded(tmp_path)
    _StreamingClient.artifacts = []
    _StreamingClient.frames = []
    with caplog.at_level(logging.INFO, logger="agentd.session_loop"):
        _run(loop, monkeypatch)
    final = [r.getMessage() for r in caplog.records if "turn progress final" in r.getMessage()]
    assert len(final) == 1 and "chunks=0" in final[0], final
    store.close()


class _TimingOutClient(_StreamingClient):
    """Streams, then the RPC dies — #159's shape: long in flight, then nothing."""

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method == "health.ping":
            return {"ok": True, "initialized": True}
        if method == "turn.dispatch":
            assert self._notify is not None
            for f in self.frames:
                self._notify("notify.progress", f)
            raise TimeoutError("deadline")
        return {}


@pytest.mark.parametrize(
    ("frames", "expect"),
    [([], "chunks=0"), ([{"role": "architect", "chunk": "abc"}], "chunks=1")],
)
def test_a_timed_out_turn_still_states_its_count(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    frames: list[dict[str, Any]],
    expect: str,
) -> None:
    """The path that motivated #228, and the success-path log missed it.

    #159 was a 269 s in-flight turn with a frozen CPU sample. If such a turn times
    out, `_dispatch_turn`'s `except` returns before any success-path logging — so
    `chunks=` was never written for exactly the case the counter exists for, and
    the log still read like a gateway that never counted. **Raised by the
    Architect on #245.** Both arms: no frames at all, and frames then a timeout.
    """
    store, loop = _seeded(tmp_path)
    _TimingOutClient.artifacts = []
    _TimingOutClient.frames = frames
    monkeypatch.setattr(dl, "RunnerClient", _TimingOutClient)
    with caplog.at_level(logging.INFO, logger="agentd.session_loop"):
        out = loop._dispatch_turn(
            session_key=SESSION,
            role="architect",
            turn_id="t-1",
            delivery_id="d-1",
            dig={},
            issue_num=1,
        )

    # Fixture proof: this must be the timeout path, not a quiet success.
    assert out.get("status") == "gateway_timeout", out

    final = [r.getMessage() for r in caplog.records if "turn progress final" in r.getMessage()]
    assert len(final) == 1, final
    assert expect in final[0], final[0]
    store.close()


def test_max_quiet_records_the_largest_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The field that answers the stall question, exercised on a fake clock.

    Unit-level on purpose: the gap has to be driven, and a real-time fixture
    would either sleep for minutes or assert nothing.
    """
    from agentd.session_loop import _ProgressCounter  # new symbol: import here so
    # the five tests above fail against `main` on their *assertions* — proving the
    # defect — rather than erroring at collection, which proves only that a name
    # is missing.

    clock = {"t": 1000.0}
    monkeypatch.setattr(dl.time, "time", lambda: clock["t"])
    c = _ProgressCounter()
    c.record("a")
    clock["t"] += 5
    c.record("b")
    clock["t"] += 180
    c.record("c")
    clock["t"] += 2
    c.record("d")
    assert c.chunks == 4
    assert c.max_quiet == pytest.approx(180.0)
    assert c.quiet == pytest.approx(2.0), "quiet is the last gap, not the worst"
