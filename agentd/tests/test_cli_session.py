"""#25 long-lived CLI session unit tests (no real vendor binaries)."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_RUNNER_ROOT = Path(__file__).resolve().parents[1] / "docker" / "session-runner"
sys.path.insert(0, str(_RUNNER_ROOT))

from agentd_runner import cli_session  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    cli_session.shutdown_all()
    yield
    cli_session.shutdown_all()


def _fake_claude_proc(lines: list[dict[str, Any]]) -> MagicMock:
    """Popen mock: stdin accepts writes; stdout yields NDJSON lines then blocks."""
    out_lines = [json.dumps(o) + "\n" for o in lines]
    idx = {"i": 0}
    lock = threading.Lock()

    proc = MagicMock()
    proc.poll.return_value = None
    proc.pid = 4242
    proc.stderr = MagicMock()
    proc.stderr.read.return_value = ""

    stdin = MagicMock()
    proc.stdin = stdin

    def readline() -> str:
        with lock:
            i = idx["i"]
            if i >= len(out_lines):
                time.sleep(0.05)
                return ""
            idx["i"] = i + 1
            return out_lines[i]

    stdout = MagicMock()
    stdout.fileno.return_value = 3
    stdout.readline.side_effect = readline
    proc.stdout = stdout
    return proc


def test_claude_turn_ends_on_type_result(tmp_path: Path) -> None:
    home = tmp_path / "home"
    tmp = tmp_path / "tmp"
    xdg = tmp_path / "xdg"
    lines = [
        {"type": "assistant", "session_id": "s1", "message": {"content": [{"type": "text", "text": "hi "}]}},
        {"type": "result", "session_id": "s1", "result": "hi done", "is_error": False},
    ]
    fake = _fake_claude_proc(lines)
    progress: list[str] = []

    with patch("agentd_runner.cli_session.subprocess.Popen", return_value=fake), patch(
        "agentd_runner.cli_session.select.select", return_value=([3], [], [])
    ), patch.object(cli_session.LiveCliSession, "_sample_rss"):
        sess = cli_session.LiveCliSession(
            role="architect",
            adapter="claude-code",
            uid=1001,
            home=home,
            tmp=tmp,
            xdg=xdg,
            spawn_cwd=tmp_path,
        )
        sess.ensure_spawned()
        # Popen should have been called with stream-json flags and no -c
        cmd = fake  # just ensure spawn happened
        assert sess.proc is fake
        result = sess.turn("hello", deadline_s=5, progress=lambda p: progress.append(p.get("chunk", "")))

    assert result["status"] == "done"
    assert "hi" in result["summary"]
    assert result["live_session"] is True
    assert sess.claude_session_id == "s1"
    assert any("hi" in c for c in progress)


def test_claude_spawn_uses_continue_flag(tmp_path: Path) -> None:
    captured: list[list[str]] = []

    def fake_popen(cmd, **kwargs):  # noqa: ANN001, ANN003
        captured.append(list(cmd))
        return _fake_claude_proc([{"type": "result", "result": "ok"}])

    with patch("agentd_runner.cli_session.subprocess.Popen", side_effect=fake_popen), patch(
        "agentd_runner.cli_session.select.select", return_value=([3], [], [])
    ), patch.object(cli_session.LiveCliSession, "_sample_rss"):
        sess = cli_session.LiveCliSession(
            role="architect",
            adapter="claude-code",
            uid=1001,
            home=tmp_path / "h",
            tmp=tmp_path / "t",
            xdg=tmp_path / "x",
            spawn_cwd=tmp_path,
        )
        sess.ensure_spawned(continue_session=False)
        sess.shutdown()
        sess.ensure_spawned(continue_session=True)

    assert "-c" not in captured[0]
    assert "-c" in captured[1]
    assert "--input-format" in captured[0]
    assert "stream-json" in captured[0]


def test_grok_acp_initialize_and_prompt(tmp_path: Path) -> None:
    """Grok agent stdio: initialize → session/new → session/prompt end_turn."""
    replies: dict[int, dict] = {}
    out_q: list[str] = []
    lock = threading.Lock()

    def on_write(data: str) -> int:
        line = data if isinstance(data, str) else data.decode()
        for part in line.strip().split("\n"):
            if not part:
                continue
            msg = json.loads(part)
            mid = msg.get("id")
            method = msg.get("method")
            with lock:
                if method == "initialize":
                    out_q.append(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1}}) + "\n")
                elif method == "session/new":
                    out_q.append(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": mid,
                                "result": {"sessionId": "acp-99"},
                            }
                        )
                        + "\n"
                    )
                elif method == "session/prompt":
                    out_q.append(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "method": "session/update",
                                "params": {
                                    "update": {
                                        "sessionUpdate": "agent_message_chunk",
                                        "content": {"text": "ORANGE_"},
                                    }
                                },
                            }
                        )
                        + "\n"
                    )
                    out_q.append(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "method": "session/update",
                                "params": {
                                    "update": {
                                        "sessionUpdate": "agent_message_chunk",
                                        "content": {"text": "TIGER"},
                                    }
                                },
                            }
                        )
                        + "\n"
                    )
                    out_q.append(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": mid,
                                "result": {"stopReason": "end_turn"},
                            }
                        )
                        + "\n"
                    )
        return len(data) if isinstance(data, (bytes, str)) else 0

    proc = MagicMock()
    proc.poll.return_value = None
    proc.pid = 7
    proc.stderr = MagicMock()
    proc.stderr.read.return_value = ""
    stdin = MagicMock()
    stdin.write.side_effect = on_write
    proc.stdin = stdin
    stdout = MagicMock()
    stdout.fileno.return_value = 5

    def readline() -> str:
        deadline = time.time() + 2
        while time.time() < deadline:
            with lock:
                if out_q:
                    return out_q.pop(0)
            time.sleep(0.01)
        return ""

    stdout.readline.side_effect = readline
    proc.stdout = stdout

    with patch("agentd_runner.cli_session.subprocess.Popen", return_value=proc), patch(
        "agentd_runner.cli_session.select.select", return_value=([5], [], [])
    ), patch.object(cli_session.LiveCliSession, "_sample_rss"):
        sess = cli_session.LiveCliSession(
            role="developer",
            adapter="grok-cli",
            uid=1002,
            home=tmp_path / "h",
            tmp=tmp_path / "t",
            xdg=tmp_path / "x",
            spawn_cwd=tmp_path,
        )
        sess.ensure_spawned()
        assert sess.acp_session_id == "acp-99"
        result = sess.turn("ping", deadline_s=5)

    assert result["status"] == "done"
    assert result["summary"] == "ORANGE_TIGER"
    assert result["live_session"] is True
    _ = replies


def test_deadline_kills_child_and_role_usable(tmp_path: Path) -> None:
    proc = MagicMock()
    proc.poll.return_value = None
    proc.pid = 9
    proc.stderr = MagicMock()
    proc.stdin = MagicMock()
    stdout = MagicMock()
    stdout.fileno.return_value = 8
    # Never produces a result line
    stdout.readline.return_value = ""
    proc.stdout = stdout

    with patch("agentd_runner.cli_session.subprocess.Popen", return_value=proc), patch(
        "agentd_runner.cli_session.select.select", return_value=([], [], [])
    ), patch.object(cli_session.LiveCliSession, "_sample_rss"):
        sess = cli_session.LiveCliSession(
            role="architect",
            adapter="claude-code",
            uid=1001,
            home=tmp_path / "h",
            tmp=tmp_path / "t",
            xdg=tmp_path / "x",
            spawn_cwd=tmp_path,
        )
        sess.ensure_spawned()
        result = sess.turn("slow", deadline_s=0)  # immediate deadline

    assert result["status"] == "failed"
    assert "deadline" in result["summary"]
    assert not sess.is_alive()
    proc.send_signal.assert_called()


def test_registry_one_session_per_role(tmp_path: Path) -> None:
    with patch("agentd_runner.cli_session.subprocess.Popen") as popen, patch.object(
        cli_session.LiveCliSession, "_sample_rss"
    ), patch.object(cli_session.LiveCliSession, "_acp_initialize_unlocked"):
        fake = MagicMock()
        fake.poll.return_value = None
        fake.pid = 1
        fake.stdin = MagicMock()
        fake.stdout = MagicMock()
        fake.stderr = MagicMock()
        popen.return_value = fake
        a1 = cli_session.get_or_create_session(
            role="architect",
            adapter="claude-code",
            uid=1001,
            home=tmp_path / "h",
            tmp=tmp_path / "t",
            xdg=tmp_path / "x",
            spawn_cwd=tmp_path,
        )
        a1.ensure_spawned()
        a2 = cli_session.get_or_create_session(
            role="architect",
            adapter="claude-code",
            uid=1001,
            home=tmp_path / "h",
            tmp=tmp_path / "t",
            xdg=tmp_path / "x",
            spawn_cwd=tmp_path,
        )
        assert a1 is a2


def test_use_oneshot_for_mock_and_env() -> None:
    assert cli_session.use_oneshot("mock") is True
    assert cli_session.use_oneshot("script") is True
    assert cli_session.use_oneshot("claude-code") is False
    old = os.environ.get("AGENTD_CLI_MODE")
    try:
        os.environ["AGENTD_CLI_MODE"] = "oneshot"
        assert cli_session.use_oneshot("claude-code") is True
    finally:
        if old is None:
            os.environ.pop("AGENTD_CLI_MODE", None)
        else:
            os.environ["AGENTD_CLI_MODE"] = old


def test_role_lock_serializes_pipe_writes(tmp_path: Path) -> None:
    """Two concurrent turn() calls on one session must not interleave (lock)."""
    order: list[str] = []
    lines_a = [
        {"type": "result", "result": "A"},
    ]
    # Use real sessions with slow turn by patching the unlocked method
    with patch.object(cli_session.LiveCliSession, "_spawn_unlocked"), patch.object(
        cli_session.LiveCliSession, "_sample_rss"
    ):
        sess = cli_session.LiveCliSession(
            role="architect",
            adapter="claude-code",
            uid=1001,
            home=tmp_path / "h",
            tmp=tmp_path / "t",
            xdg=tmp_path / "x",
            spawn_cwd=tmp_path,
        )
        sess.proc = MagicMock()
        sess.proc.poll.return_value = None

        def slow_turn(prompt: str, deadline_s: int, progress=None) -> dict:  # noqa: ANN001
            order.append(f"start:{prompt}")
            time.sleep(0.08)
            order.append(f"end:{prompt}")
            return {"status": "done", "summary": prompt, "public_actions": [], "artifacts": []}

        sess._claude_turn_unlocked = slow_turn  # type: ignore[method-assign]

        def run(p: str) -> None:
            sess.turn(p, deadline_s=5)

        t1 = threading.Thread(target=run, args=("1",))
        t2 = threading.Thread(target=run, args=("2",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

    assert len(order) == 4
    assert order[0].startswith("start:") and order[1].startswith("end:")
    assert order[0].split(":")[1] == order[1].split(":")[1]
    _ = lines_a
