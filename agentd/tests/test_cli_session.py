"""#25 long-lived CLI session unit tests (no real vendor binaries).

Includes PR #26 Architect B1/B2 regression: agent→client ACP requests.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import UTC
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


def _wire_stdout_queue(sess: cli_session.LiveCliSession, lines: list[str]) -> None:
    """Simulate reader thread: put lines then leave queue open for more pushes."""
    import queue as qmod

    q: qmod.Queue[str | None] = qmod.Queue()
    for line in lines:
        q.put(line)
    sess._stdout_q = q
    sess.proc = MagicMock()
    sess.proc.poll.return_value = None
    sess.proc.pid = 4242
    sess.proc.stdin = MagicMock()


def test_claude_turn_ends_on_type_result(tmp_path: Path) -> None:
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "session_id": "s1",
                "message": {"content": [{"type": "text", "text": "hi "}]},
            }
        ),
        json.dumps({"type": "result", "session_id": "s1", "result": "hi done", "is_error": False}),
    ]
    progress: list[str] = []
    sess = cli_session.LiveCliSession(
        role="architect",
        adapter="claude-code",
        uid=1001,
        home=tmp_path / "h",
        tmp=tmp_path / "t",
        xdg=tmp_path / "x",
        spawn_cwd=tmp_path,
    )
    _wire_stdout_queue(sess, lines)
    with patch.object(cli_session.LiveCliSession, "_sample_rss"):
        result = sess.turn("hello", deadline_s=5, progress=lambda p: progress.append(p.get("chunk", "")))

    assert result["status"] == "done"
    assert "hi" in result["summary"]
    assert result["live_session"] is True
    assert sess.claude_session_id == "s1"
    assert any("hi" in c for c in progress)


def test_claude_spawn_uses_continue_flag(tmp_path: Path) -> None:
    captured: list[list[str]] = []

    def fake_popen(**kwargs):
        cmd = kwargs.get("args") or kwargs.get("args")
        if "args" in kwargs:
            cmd = kwargs["args"]
        captured.append(list(cmd))
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 1
        proc.stdin = MagicMock()
        # stdout lines for reader thread
        proc.stdout = MagicMock()
        proc.stdout.readline.side_effect = [""]  # EOF immediately after spawn
        return proc

    with patch("agentd_runner.cli_session.subprocess.Popen", side_effect=fake_popen), patch(
        "agentd_runner.cli_session.os.geteuid", return_value=501
    ), patch.object(cli_session.LiveCliSession, "_sample_rss"), patch.object(
        cli_session.LiveCliSession, "_acp_initialize_unlocked"
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
        sess.ensure_spawned(continue_session=False)
        sess.shutdown()
        sess.ensure_spawned(continue_session=True)

    # Vendor argv starts after: bash -c SCRIPT role-secret-wrap ROLE
    def vendor(cmd: list[str]) -> list[str]:
        i = cmd.index("role-secret-wrap")
        return cmd[i + 2 :]  # skip wrap marker + role

    assert "-c" not in vendor(captured[0])
    assert "-c" in vendor(captured[1])
    assert "--input-format" in vendor(captured[0])
    assert "stream-json" in vendor(captured[0])
    # B5: secrets loaded via bash wrapper, not by runner
    assert captured[0][0] == "bash"
    assert "role-secret-wrap" in captured[0]


def test_grok_acp_initialize_and_prompt(tmp_path: Path) -> None:
    """Grok agent stdio: initialize → session/new → session/prompt end_turn."""
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
                    # Assert B1 caps are false
                    caps = (msg.get("params") or {}).get("clientCapabilities") or {}
                    assert caps.get("fs", {}).get("readTextFile") is False
                    assert caps.get("terminal") is False
                    out_q.append(
                        json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1}})
                    )
                elif method == "session/new":
                    out_q.append(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": mid,
                                "result": {"sessionId": "acp-99"},
                            }
                        )
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
                    )
                    out_q.append(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": mid,
                                "result": {"stopReason": "end_turn"},
                            }
                        )
                    )
        return len(data) if isinstance(data, (bytes, str)) else 0

    sess = cli_session.LiveCliSession(
        role="developer",
        adapter="grok-cli",
        uid=1002,
        home=tmp_path / "h",
        tmp=tmp_path / "t",
        xdg=tmp_path / "x",
        spawn_cwd=tmp_path,
    )
    import queue as qmod

    q: qmod.Queue[str | None] = qmod.Queue()

    def feed_from_out() -> None:
        """Move agent frames into the session queue as the fake agent produces them."""
        seen = 0
        deadline = time.time() + 5
        while time.time() < deadline:
            with lock:
                while seen < len(out_q):
                    q.put(out_q[seen])
                    seen += 1
            time.sleep(0.01)

    feeder = threading.Thread(target=feed_from_out, daemon=True)
    feeder.start()

    sess.proc = MagicMock()
    sess.proc.poll.return_value = None
    sess.proc.pid = 7
    sess.proc.stdin = MagicMock()
    sess.proc.stdin.write.side_effect = on_write
    sess._stdout_q = q

    with patch.object(cli_session.LiveCliSession, "_sample_rss"):
        sess._acp_initialize_unlocked()
        assert sess.acp_session_id == "acp-99"
        result = sess.turn("ping", deadline_s=5)

    assert result["status"] == "done"
    assert result["summary"] == "ORANGE_TIGER"
    assert result["live_session"] is True


def test_grok_answers_permission_and_ignores_request_id_collision(tmp_path: Path) -> None:
    """B1+B2 regression: request_permission id=0 must not be treated as turn result.

    Real grok sends agent→client requests with ids starting at 0 while our
    client ids start high; a shape-blind id match would end the turn early or
    hang if we drop the permission request.
    """
    writes: list[dict] = []
    import queue as qmod

    q: qmod.Queue[str | None] = qmod.Queue()
    # Simulate: permission request id=0, then fs request id=1, then real result.
    # Our mid for session/prompt will be 1002 after init path skipped.
    q.put(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "session/request_permission",
                "params": {
                    "toolCall": {"kind": "edit", "title": "Write /tmp/tt.txt"},
                    "options": [
                        {"optionId": "allow-edits-session", "name": "allow always"},
                    ],
                },
            }
        )
    )
    q.put(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "fs/read_text_file",
                "params": {"path": "/tmp/tt.txt"},
            }
        )
    )
    q.put(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"text": "HELLO"},
                    }
                },
            }
        )
    )
    # Colliding id: agent response uses mid we will assign — only shape marks it response.
    # Our _rpc_id starts 1000; first prompt uses 1001 after we set acp_session_id.
    q.put(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1001,
                "result": {"stopReason": "end_turn"},
            }
        )
    )

    sess = cli_session.LiveCliSession(
        role="developer",
        adapter="grok-cli",
        uid=1002,
        home=tmp_path / "h",
        tmp=tmp_path / "t",
        xdg=tmp_path / "x",
        spawn_cwd=tmp_path,
    )
    sess.acp_session_id = "s-live"
    sess._rpc_id = 1000
    sess._stdout_q = q
    sess.proc = MagicMock()
    sess.proc.poll.return_value = None
    sess.proc.pid = 3
    sess.proc.stdin = MagicMock()

    def capture_write(data: str) -> int:
        writes.append(json.loads(data.strip()))
        return len(data)

    sess.proc.stdin.write.side_effect = capture_write

    with patch.object(cli_session.LiveCliSession, "_sample_rss"):
        result = sess.turn("write a file with HELLO", deadline_s=5)

    assert result["status"] == "done"
    assert result["summary"] == "HELLO"
    # Permission approved with Architect-verified optionId
    perm_replies = [
        w
        for w in writes
        if w.get("id") == 0 and "result" in w and not w.get("method")
    ]
    assert perm_replies, writes
    outcome = perm_replies[0]["result"]["outcome"]
    assert outcome["outcome"] == "selected"
    assert outcome["optionId"] == "allow-edits-session"
    # fs/* refused (capability not offered)
    fs_errs = [w for w in writes if w.get("id") == 1 and "error" in w]
    assert fs_errs


def test_id_collision_request_not_accepted_as_response() -> None:
    """B2 unit: method+id is a request, not a response even if id matches mid."""
    req = {"jsonrpc": "2.0", "id": 5, "method": "session/request_permission", "params": {}}
    resp = {"jsonrpc": "2.0", "id": 5, "result": {"stopReason": "end_turn"}}
    assert cli_session._is_jsonrpc_request(req) is True
    assert cli_session._is_jsonrpc_response(req) is False
    assert cli_session._is_jsonrpc_request(resp) is False
    assert cli_session._is_jsonrpc_response(resp) is True


def test_deadline_kills_child_and_role_usable(tmp_path: Path) -> None:
    import queue as qmod

    sess = cli_session.LiveCliSession(
        role="architect",
        adapter="claude-code",
        uid=1001,
        home=tmp_path / "h",
        tmp=tmp_path / "t",
        xdg=tmp_path / "x",
        spawn_cwd=tmp_path,
    )
    # Empty queue → timeouts
    sess._stdout_q = qmod.Queue()
    proc = MagicMock()
    proc.poll.return_value = None
    proc.pid = 9
    proc.stdin = MagicMock()
    sess.proc = proc

    with patch.object(cli_session.LiveCliSession, "_sample_rss"):
        result = sess.turn("slow", deadline_s=0)

    assert result["status"] == "failed"
    assert "deadline" in result["summary"]
    assert not sess.is_alive()
    assert sess.proc is None
    proc.send_signal.assert_called()


def test_stderr_redirected_to_role_log_not_pipe(tmp_path: Path) -> None:
    """B3: stderr must not be subprocess.PIPE (buffer fill wedge)."""
    seen: dict[str, Any] = {}

    def fake_popen(**kwargs):
        seen.update(kwargs)
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 1
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.readline.side_effect = [""]
        return proc

    with patch("agentd_runner.cli_session.subprocess.Popen", side_effect=fake_popen), patch(
        "agentd_runner.cli_session.os.geteuid", return_value=501
    ), patch.object(cli_session.LiveCliSession, "_sample_rss"), patch.object(
        cli_session.LiveCliSession, "_acp_initialize_unlocked"
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
        sess.ensure_spawned()

    import subprocess as sp

    assert seen.get("stderr") is not sp.PIPE
    assert seen.get("stderr") is not None
    assert (tmp_path / "t" / "cli-architect.stderr.log").is_file()
    sess.shutdown()


def test_registry_one_session_per_role(tmp_path: Path) -> None:
    with patch("agentd_runner.cli_session.subprocess.Popen") as popen, patch(
        "agentd_runner.cli_session.os.geteuid", return_value=501
    ), patch.object(cli_session.LiveCliSession, "_sample_rss"), patch.object(
        cli_session.LiveCliSession, "_acp_initialize_unlocked"
    ):
        fake = MagicMock()
        fake.poll.return_value = None
        fake.pid = 1
        fake.stdin = MagicMock()
        fake.stdout = MagicMock()
        fake.stdout.readline.side_effect = lambda: time.sleep(60) or ""  # type: ignore[misc]
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
    order: list[str] = []
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

        def slow_turn(prompt: str, deadline_s: int, progress=None) -> dict:
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


def test_ensure_role_dirs_chowns_when_root(tmp_path: Path) -> None:
    chowns: list[tuple] = []

    def fake_chown(path, uid, gid):
        chowns.append((str(path), uid, gid))

    sess = cli_session.LiveCliSession(
        role="architect",
        adapter="claude-code",
        uid=1001,
        home=tmp_path / "h",
        tmp=tmp_path / "t",
        xdg=tmp_path / "x",
        spawn_cwd=tmp_path / "cwd",
    )
    with patch("agentd_runner.cli_session.os.geteuid", return_value=0), patch(
        "agentd_runner.cli_session.os.chown", side_effect=fake_chown
    ):
        sess._ensure_role_dirs()
    assert any(c[1] == 1001 for c in chowns)
    assert (tmp_path / "h").is_dir()


def test_role_env_does_not_read_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B5: runner must not open 0400 role secret files as root."""
    # Even if files exist on the host path layout, _role_env must not load them.
    monkeypatch.setenv("GH_TOKEN", "leak-me")
    env = cli_session._role_env(
        "architect", tmp_path / "h", tmp_path / "t", tmp_path / "x"
    )
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["HOME"] == str(tmp_path / "h")


def test_wrap_with_role_secrets_prepends_bash() -> None:
    wrapped = cli_session._wrap_with_role_secrets("developer", ["grok", "agent", "stdio"])
    assert wrapped[:5] == ["bash", "-c", wrapped[2], "role-secret-wrap", "developer"]
    assert wrapped[-3:] == ["grok", "agent", "stdio"]
    assert "/run/agent/${ROLE}/token" in wrapped[2] or "/run/agent/" in wrapped[2]


def test_pick_permission_option_prefers_allow_once_for_execute() -> None:
    """B6: execute tools offer allow-once, not allow-edits-session."""
    assert (
        cli_session._pick_permission_option(
            {"options": [{"optionId": "allow-once"}, {"optionId": "reject-once"}]}
        )
        == "allow-once"
    )
    assert (
        cli_session._pick_permission_option(
            {
                "options": [
                    {"optionId": "allow-edits-session"},
                    {"optionId": "reject-once"},
                ]
            }
        )
        == "allow-edits-session"
    )
    assert (
        cli_session._pick_permission_option(
            {"options": [{"optionId": "allow_always"}, {"optionId": "allow-once"}]}
        )
        == "allow_always"
    )


def test_grok_execute_permission_picks_allow_once(tmp_path: Path) -> None:
    """B6 regression: hardcoded allow-edits-session is invalid for execute."""
    import queue as qmod

    writes: list[dict] = []
    q: qmod.Queue[str | None] = qmod.Queue()
    q.put(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "session/request_permission",
                "params": {
                    "toolCall": {"kind": "execute", "title": "Write file"},
                    "options": [
                        {"optionId": "allow-once"},
                        {"optionId": "reject-once"},
                    ],
                },
            }
        )
    )
    q.put(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"text": "DONE"},
                    }
                },
            }
        )
    )
    q.put(json.dumps({"jsonrpc": "2.0", "id": 1001, "result": {"stopReason": "end_turn"}}))

    sess = cli_session.LiveCliSession(
        role="developer",
        adapter="grok-cli",
        uid=1002,
        home=tmp_path / "h",
        tmp=tmp_path / "t",
        xdg=tmp_path / "x",
        spawn_cwd=tmp_path,
    )
    sess.acp_session_id = "s"
    sess._rpc_id = 1000
    sess._stdout_q = q
    sess.proc = MagicMock()
    sess.proc.poll.return_value = None
    sess.proc.stdin = MagicMock()
    sess.proc.stdin.write.side_effect = lambda data: writes.append(json.loads(data.strip())) or len(
        data
    )

    with patch.object(cli_session.LiveCliSession, "_sample_rss"):
        result = sess.turn("write file", deadline_s=5)

    assert result["status"] == "done"
    perm = next(w for w in writes if w.get("id") == 0 and "result" in w)
    assert perm["result"]["outcome"]["optionId"] == "allow-once"


def test_grok_cancelled_stop_reason_is_failed(tmp_path: Path) -> None:
    """B7: cancelled + empty text must not be status=done."""
    import queue as qmod

    q: qmod.Queue[str | None] = qmod.Queue()
    q.put(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1001,
                "result": {"stopReason": "cancelled", "agentResult": None},
            }
        )
    )
    sess = cli_session.LiveCliSession(
        role="developer",
        adapter="grok-cli",
        uid=1002,
        home=tmp_path / "h",
        tmp=tmp_path / "t",
        xdg=tmp_path / "x",
        spawn_cwd=tmp_path,
    )
    sess.acp_session_id = "s"
    sess._rpc_id = 1000
    sess._stdout_q = q
    sess.proc = MagicMock()
    sess.proc.poll.return_value = None
    sess.proc.stdin = MagicMock()

    with patch.object(cli_session.LiveCliSession, "_sample_rss"):
        result = sess.turn("do something", deadline_s=5)

    assert result["status"] == "failed"
    assert "cancelled" in result["summary"]
    assert result.get("stop_reason") == "cancelled"


def test_grok_empty_end_turn_is_failed(tmp_path: Path) -> None:
    import queue as qmod

    q: qmod.Queue[str | None] = qmod.Queue()
    q.put(json.dumps({"jsonrpc": "2.0", "id": 1001, "result": {"stopReason": "end_turn"}}))
    sess = cli_session.LiveCliSession(
        role="developer",
        adapter="grok-cli",
        uid=1002,
        home=tmp_path / "h",
        tmp=tmp_path / "t",
        xdg=tmp_path / "x",
        spawn_cwd=tmp_path,
    )
    sess.acp_session_id = "s"
    sess._rpc_id = 1000
    sess._stdout_q = q
    sess.proc = MagicMock()
    sess.proc.poll.return_value = None
    sess.proc.stdin = MagicMock()

    with patch.object(cli_session.LiveCliSession, "_sample_rss"):
        result = sess.turn("x", deadline_s=5)

    assert result["status"] == "failed"
    assert "empty" in result["summary"].lower()


def test_claude_session_limit_is_quota_exhausted(tmp_path: Path) -> None:
    """#86: recorded Claude session-limit envelope is not a failed turn."""
    from datetime import datetime

    lines = [
        json.dumps(
            {
                "type": "result",
                "session_id": "s-lim",
                "is_error": True,
                "result": "You've hit your session limit · resets 9:30am (UTC)",
            }
        ),
    ]
    sess = cli_session.LiveCliSession(
        role="architect",
        adapter="claude-code",
        uid=1001,
        home=tmp_path / "h",
        tmp=tmp_path / "t",
        xdg=tmp_path / "x",
        spawn_cwd=tmp_path,
    )
    _wire_stdout_queue(sess, lines)
    now = datetime(2026, 8, 13, 1, 43, tzinfo=UTC).timestamp()
    with (
        patch.object(cli_session.LiveCliSession, "_sample_rss"),
        patch("agentd_runner.quota.time.time", return_value=now),
    ):
        result = sess.turn("continue", deadline_s=5)
    assert result["status"] == "quota_exhausted"
    assert result["status"] != "failed"
    want = int(datetime(2026, 8, 13, 9, 30, tzinfo=UTC).timestamp())
    assert result["retry_after"] == want
    assert "session limit" in result["summary"].lower()


def test_claude_generic_is_error_still_failed(tmp_path: Path) -> None:
    """Regression: a real is_error is not swallowed as quota."""
    lines = [
        json.dumps(
            {
                "type": "result",
                "session_id": "s-err",
                "is_error": True,
                "result": "API Error: 500 Internal Server Error",
            }
        ),
    ]
    sess = cli_session.LiveCliSession(
        role="architect",
        adapter="claude-code",
        uid=1001,
        home=tmp_path / "h",
        tmp=tmp_path / "t",
        xdg=tmp_path / "x",
        spawn_cwd=tmp_path,
    )
    _wire_stdout_queue(sess, lines)
    with patch.object(cli_session.LiveCliSession, "_sample_rss"):
        result = sess.turn("go", deadline_s=5)
    assert result["status"] == "failed"
    assert result.get("retry_after") is None


def test_grok_rate_limit_stop_reason_is_quota_exhausted(tmp_path: Path) -> None:
    import queue as qmod

    q: qmod.Queue[str | None] = qmod.Queue()
    q.put(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1001,
                "result": {
                    "stopReason": "rate_limited",
                    "text": "rate limited, try again later",
                },
            }
        )
    )
    sess = cli_session.LiveCliSession(
        role="developer",
        adapter="grok-cli",
        uid=1002,
        home=tmp_path / "h",
        tmp=tmp_path / "t",
        xdg=tmp_path / "x",
        spawn_cwd=tmp_path,
    )
    sess.acp_session_id = "s"
    sess._rpc_id = 1000
    sess._stdout_q = q
    sess.proc = MagicMock()
    sess.proc.poll.return_value = None
    sess.proc.stdin = MagicMock()
    with patch.object(cli_session.LiveCliSession, "_sample_rss"):
        result = sess.turn("go", deadline_s=5)
    assert result["status"] == "quota_exhausted"
    assert result.get("retry_after") is None


def test_grok_error_session_limit_is_quota_exhausted(tmp_path: Path) -> None:
    import queue as qmod
    from datetime import datetime

    q: qmod.Queue[str | None] = qmod.Queue()
    q.put(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1001,
                "error": {
                    "code": -32000,
                    "message": "You've hit your session limit · resets 9:30am (UTC)",
                },
            }
        )
    )
    sess = cli_session.LiveCliSession(
        role="developer",
        adapter="grok-cli",
        uid=1002,
        home=tmp_path / "h",
        tmp=tmp_path / "t",
        xdg=tmp_path / "x",
        spawn_cwd=tmp_path,
    )
    sess.acp_session_id = "s"
    sess._rpc_id = 1000
    sess._stdout_q = q
    sess.proc = MagicMock()
    sess.proc.poll.return_value = None
    sess.proc.stdin = MagicMock()
    now = datetime(2026, 8, 13, 1, 43, tzinfo=UTC).timestamp()
    with (
        patch.object(cli_session.LiveCliSession, "_sample_rss"),
        patch("agentd_runner.quota.time.time", return_value=now),
    ):
        result = sess.turn("go", deadline_s=5)
    assert result["status"] == "quota_exhausted"
    want = int(datetime(2026, 8, 13, 9, 30, tzinfo=UTC).timestamp())
    assert result["retry_after"] == want


def test_parse_reset_epoch_past_clock_rolls_to_next_day() -> None:
    from datetime import datetime

    from agentd_runner.quota import parse_reset_epoch

    now = datetime(2026, 8, 13, 10, 0, tzinfo=UTC).timestamp()
    got = parse_reset_epoch("resets 9:30am (UTC)", now=now)
    want = int(datetime(2026, 8, 14, 9, 30, tzinfo=UTC).timestamp())
    assert got == want


def test_parse_reset_epoch_resets_in_hours() -> None:
    from agentd_runner.quota import parse_reset_epoch

    assert parse_reset_epoch("resets in 4h", now=1_000_000.0) == 1_000_000 + 4 * 3600


def test_claude_mentions_of_limits_are_not_quota(tmp_path: Path) -> None:
    """#94 B1: model text about a limit is a failed turn, not vendor quota."""
    cases = (
        "I hit the GitHub API rate limit while fetching review threads, so I stopped.",
        "Implemented #86 (vendor quota refusal). Tests fail: 2 errors in test_quota_exhausted.py",
        "Traceback: RuntimeError: gh api rate limit exceeded (5000/hr)",
    )
    for i, summary in enumerate(cases):
        lines = [
            json.dumps(
                {
                    "type": "result",
                    "session_id": f"s-fp-{i}",
                    "is_error": True,
                    "result": summary,
                }
            ),
        ]
        sess = cli_session.LiveCliSession(
            role="architect",
            adapter="claude-code",
            uid=1001,
            home=tmp_path / f"h{i}",
            tmp=tmp_path / f"t{i}",
            xdg=tmp_path / f"x{i}",
            spawn_cwd=tmp_path,
        )
        _wire_stdout_queue(sess, lines)
        with patch.object(cli_session.LiveCliSession, "_sample_rss"):
            result = sess.turn("go", deadline_s=5)
        assert result["status"] == "failed", summary
        assert result.get("retry_after") is None


def test_claude_quota_with_assistant_text_is_failed(tmp_path: Path) -> None:
    """Vendor refusal has no agent output. Talking about quota is not a refusal."""
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "session_id": "s-talk",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": "I will wait for the session limit.",
                        }
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "result",
                "session_id": "s-talk",
                "is_error": True,
                "result": "You've hit your session limit · resets 9:30am (UTC)",
            }
        ),
    ]
    sess = cli_session.LiveCliSession(
        role="architect",
        adapter="claude-code",
        uid=1001,
        home=tmp_path / "h",
        tmp=tmp_path / "t",
        xdg=tmp_path / "x",
        spawn_cwd=tmp_path,
    )
    _wire_stdout_queue(sess, lines)
    with patch.object(cli_session.LiveCliSession, "_sample_rss"):
        result = sess.turn("go", deadline_s=5)
    assert result["status"] == "failed"
