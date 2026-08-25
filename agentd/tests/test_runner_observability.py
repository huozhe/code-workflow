"""#208 / #210 / #212: what the runner reports, and what it cleans up."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

_RUNNER_ROOT = Path(__file__).resolve().parents[1] / "docker" / "session-runner"
sys.path.insert(0, str(_RUNNER_ROOT))

from agentd_runner import cli_session, server  # noqa: E402

# --- #208: the drain's level must discriminate ----------------------------


@pytest.mark.parametrize(
    "frame",
    [
        '{"jsonrpc":"2.0","method":"_x.ai/models/update","params":{}}',
        '{"method":"_x.ai/announcements/update"}',
    ],
)
def test_bare_notification_is_quiet(frame: str) -> None:
    assert cli_session._is_bare_notification(frame) is True


@pytest.mark.parametrize(
    "frame",
    [
        # #162's defect: a result frame must never drop below WARNING.
        '{"type":"result","subtype":"success"}',
        # A response someone may be waiting on.
        '{"jsonrpc":"2.0","id":7,"result":{}}',
        # A notification that also carries a type is not bare.
        '{"method":"x","type":"result"}',
        # Unparseable and non-dict fail towards WARNING.
        "not json at all",
        "[1, 2, 3]",
        '"a string"',
        # No method at all.
        '{"params":{}}',
    ],
)
def test_anything_else_stays_loud(frame: str) -> None:
    assert cli_session._is_bare_notification(frame) is False


def test_drain_logs_notifications_below_warning(caplog: pytest.LogCaptureFixture) -> None:
    """A result frame warns; vendor chatter does not — in the same drain."""
    sess = cli_session.LiveCliSession.__new__(cli_session.LiveCliSession)
    sess.role = "developer"
    import queue as _queue

    q: _queue.Queue = _queue.Queue()
    q.put('{"method":"_x.ai/models/update"}')
    q.put('{"type":"result","subtype":"success"}')
    sess._stdout_q = q

    with caplog.at_level(logging.DEBUG, logger="agentd_runner.cli_session"):
        assert sess._drain_stdout_unlocked(turn_id="t-1", reason="before prompt") == 2

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "exactly the result frame must warn"
    assert "type='result'" in warnings[0].getMessage()

    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(debugs) == 1 and "_x.ai/models/update" in debugs[0].getMessage()

    # The count survives the demotion, so a burst is never silent in aggregate.
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("notifications=1 of 2" in r.getMessage() for r in infos)


# --- #212: the probe must report what it just read ------------------------


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs /proc")
def test_rss_bytes_is_live_not_the_peak() -> None:
    """``rss_bytes`` must be VmRSS, not ``ru_maxrss``.

    Linux-only on purpose, and that is the whole point: off Linux there is no
    ``/proc`` and the function falls back to the peak, so an assertion that ran
    everywhere would pass on macOS whether or not the fix is present — the same
    platform-blindness #216 was about. The runner only ever runs on Linux.
    """
    with open("/proc/self/status", encoding="utf-8") as fh:
        vmrss = next(
            int(line.split()[1]) * 1024
            for line in fh
            if line.startswith("VmRSS:")
        )
    # Allocation between the two reads is possible; a page of slack, not a peak.
    assert abs(server.rss_bytes() - vmrss) < 512 * 1024
    assert server.rss_peak_bytes() >= server.rss_bytes()


def test_rss_snapshot_samples_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """``rss_snapshot`` must re-sample, not replay ``last_rss_kb``."""
    sess = cli_session.LiveCliSession.__new__(cli_session.LiveCliSession)
    sess.role = "architect"
    sess.last_rss_kb = 999_999  # a stale reading from some earlier turn

    sampled: list[str] = []

    def _fake_sample(self=sess) -> None:
        sampled.append(self.role)
        self.last_rss_kb = 42

    monkeypatch.setattr(sess, "_sample_rss", _fake_sample)
    monkeypatch.setattr(cli_session, "_SESSIONS", {"architect": sess})

    assert cli_session.rss_snapshot() == {"architect": 42}
    assert sampled == ["architect"], "the snapshot must take a fresh sample"


# --- #210: orphans must be reaped at turn end, not only at spawn ----------


def test_turn_end_reaps_orphans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn-time was the only call site, and spawns are rare (#210).

    175 zombies accumulated in one session against a cgroup ``pids.max`` of
    1024, every gateway-side summary reading healthy throughout — a zombie
    holds no memory, so ``rss_bytes`` never moved.
    """
    from test_cli_session import _wire_scripted_cli

    calls: list[set[int]] = []
    monkeypatch.setattr(
        cli_session, "reap_orphans", lambda tracked: calls.append(tracked) or []
    )

    sess = cli_session.LiveCliSession(
        role="architect",
        adapter="claude-code",
        uid=1001,
        home=tmp_path / "h",
        tmp=tmp_path / "t",
        xdg=tmp_path / "x",
        spawn_cwd=tmp_path,
    )
    _wire_scripted_cli(
        sess,
        [
            [
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "session_id": "s1",
                        "result": "done",
                        "is_error": False,
                    }
                )
            ]
        ],
    )
    monkeypatch.setattr(cli_session.LiveCliSession, "_sample_rss", lambda self: None)

    result = sess.turn("hello", deadline_s=5)

    assert result["status"] == "done"
    assert len(calls) == 1, "a completed turn must reap exactly once"
    assert isinstance(calls[0], set), "the reaper is passed the tracked-pid set"
