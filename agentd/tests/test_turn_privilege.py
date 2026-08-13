"""turn.dispatch privilege drop + transcript (#25 re-derived for per-session setuid).

§7.3 properties that must survive the timing change:
- never run adapter as root
- correct per-role uid (when host can setuid)
- no supplementary groups
- HOME / TMPDIR / XDG_* under the role tree
- oneshot (mock) still works for tests; live path drops at spawn not per turn
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_RUNNER_ROOT = Path(__file__).resolve().parents[1] / "docker" / "session-runner"
sys.path.insert(0, str(_RUNNER_ROOT))

from agentd_runner import cli_session  # noqa: E402
from agentd_runner import server as runner_server  # noqa: E402
from agentd_runner import turn as turn_mod  # noqa: E402


@pytest.fixture()
def runner_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[int, Path]:
    monkeypatch.setenv("AGENTD_ADAPTER", "mock")
    monkeypatch.setattr(runner_server, "TOKEN_ROOT", tmp_path / "run-agent")
    runner_server.TOKEN_ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(runner_server.TOKEN_ROOT, 0o711)

    sess = tmp_path / "sessions" / "o__r__1"
    for role in ("architect", "developer"):
        (sess / role / "context").mkdir(parents=True, exist_ok=True)
        (sess / role / "worktrees" / "issue-1").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENTD_SESSION_DIR", str(sess))
    monkeypatch.setenv("AGENTD_PROJECT_ROOT", str(tmp_path / "project"))
    (tmp_path / "project" / "home").mkdir(parents=True)

    monkeypatch.setattr(
        runner_server,
        "identity_preflight",
        lambda tokens, expected: [],
    )

    bearer = "test-bearer-token-32bytes-minimum!!"
    monkeypatch.setenv("AGENTD_RUNNER_BEARER", bearer)
    runner_server.STATE = runner_server.RunnerState()
    runner_server.STATE.set_bearer(bearer)
    cli_session.shutdown_all()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    t = threading.Thread(
        target=runner_server.serve, args=("127.0.0.1", port), daemon=True
    )
    t.start()
    time.sleep(0.25)
    yield port, sess
    cli_session.shutdown_all()


def _rpc(port: int, lines: list[dict]) -> list[dict]:
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    out: list[dict] = []
    with s:
        f = s.makefile("rwb")
        for obj in lines:
            f.write((json.dumps(obj) + "\n").encode())
            f.flush()
            raw = f.readline()
            out.append(json.loads(raw.decode()))
    return out


def test_turn_dispatch_writes_transcript(runner_env: tuple[int, Path]) -> None:
    port, sess = runner_env
    resp = _rpc(
        port,
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session.attach",
                "params": {"bearer": "test-bearer-token-32bytes-minimum!!"},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session.init",
                "params": {
                    "session_key": "o/r#1",
                    "roles": {"architect": "a", "developer": "d"},
                    "tokens": {"architect": "tok-a", "developer": "tok-d"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "turn.dispatch",
                "params": {
                    "turn_id": "t-test1",
                    "role": "architect",
                    "event": {"kind": "issue_opened"},
                    "context": {},
                    "deadline_s": 30,
                },
            },
        ],
    )
    assert "result" in resp[0]
    assert resp[1]["result"]["initialized"] is True
    # mock → oneshot; no live CLI spawn
    spawn = resp[1]["result"].get("cli_spawn") or {}
    assert spawn.get("architect", {}).get("mode") == "oneshot" or spawn.get(
        "architect", {}
    ).get("spawned") is False
    assert "result" in resp[2]
    status = resp[2]["result"].get("status")
    assert status in ("done", "failed", "needs_human")

    resp2 = _rpc(
        port,
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session.attach",
                "params": {"bearer": "test-bearer-token-32bytes-minimum!!"},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session.resume",
                "params": {
                    "session_key": "o/r#1",
                    "roles": {"architect": "a", "developer": "d"},
                    "tokens": {"architect": "tok-a", "developer": "tok-d"},
                },
            },
        ],
    )
    assert "rehydrated" in resp2[1]["result"]


def test_role_paths_home_tmp_xdg_under_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§7.3 env roots stay under the durable project tree (ADR-4 / #76 B1)."""
    sess = tmp_path / "sessions" / "k"
    monkeypatch.setenv("AGENTD_SESSION_DIR", str(sess))
    monkeypatch.setenv("AGENTD_PROJECT_ROOT", str(tmp_path / "proj"))
    (tmp_path / "proj" / "home").mkdir(parents=True)
    paths = turn_mod.role_paths("architect")
    assert paths["home"] == tmp_path / "proj" / "home" / "architect"
    assert paths["tmp"] == tmp_path / "proj" / "home" / "architect" / "tmp"
    assert paths["xdg"] == tmp_path / "proj" / "home" / "architect" / "xdg"
    assert str(sess) not in str(paths["tmp"])
    assert str(sess) not in str(paths["xdg"])


def test_role_paths_keyed_by_issue_num_not_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#76: one container serves many issues; env pin must not win over issue_num."""
    pinned = tmp_path / "sessions" / "47"
    monkeypatch.setenv("AGENTD_SESSION_DIR", str(pinned))
    monkeypatch.setenv("AGENTD_PROJECT_ROOT", str(tmp_path))
    (tmp_path / "home").mkdir()

    p58 = turn_mod.role_paths("architect", 58)
    p32 = turn_mod.role_paths("developer", 32)

    assert p58["context"] == tmp_path / "sessions" / "58" / "architect" / "context"
    assert p32["context"] == tmp_path / "sessions" / "32" / "developer" / "context"
    assert p58["transcript"] == tmp_path / "sessions" / "58" / "architect" / "transcript.jsonl"
    assert p32["transcript"] == tmp_path / "sessions" / "32" / "developer" / "transcript.jsonl"
    assert p58["context"] != p32["context"]
    assert p58["home"] == tmp_path / "home" / "architect"
    assert str(pinned) not in str(p58["context"])
    assert str(pinned) not in str(p32["transcript"])


def test_turn_dispatch_writes_under_issue_not_env(runner_env: tuple[int, Path]) -> None:
    """#76: turn.dispatch audit trail follows context.issue_num, not AGENTD_SESSION_DIR."""
    port, pinned = runner_env
    project = Path(os.environ["AGENTD_PROJECT_ROOT"])
    resp = _rpc(
        port,
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session.attach",
                "params": {"bearer": "test-bearer-token-32bytes-minimum!!"},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session.init",
                "params": {
                    "session_key": "o/r#58",
                    "roles": {"architect": "a", "developer": "d"},
                    "tokens": {"architect": "tok-a", "developer": "tok-d"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "turn.dispatch",
                "params": {
                    "turn_id": "t-issue58",
                    "role": "architect",
                    "event": {"kind": "issue_opened"},
                    "context": {"issue_num": 58},
                    "deadline_s": 30,
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "turn.dispatch",
                "params": {
                    "turn_id": "t-issue32",
                    "role": "developer",
                    "event": {"kind": "issue_opened"},
                    "context": {"issue_num": 32},
                    "deadline_s": 30,
                },
            },
        ],
    )
    assert "result" in resp[2]
    assert "result" in resp[3]

    p58 = project / "sessions" / "58" / "architect"
    p32 = project / "sessions" / "32" / "developer"
    assert (p58 / "context" / "prompt-t-issue58.txt").is_file()
    assert (p32 / "context" / "prompt-t-issue32.txt").is_file()
    assert (p58 / "transcript.jsonl").is_file()
    assert (p32 / "transcript.jsonl").is_file()
    assert not (pinned / "architect" / "context" / "prompt-t-issue58.txt").exists()
    assert not (pinned / "developer" / "context" / "prompt-t-issue32.txt").exists()


def test_ensure_role_cli_spawned_tmp_xdg_ignore_env_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#76 B1: CLI spawn (no issue in scope) must not mkdir the env-pinned session."""
    pinned = tmp_path / "sessions" / "47"
    monkeypatch.setenv("AGENTD_SESSION_DIR", str(pinned))
    monkeypatch.setenv("AGENTD_PROJECT_ROOT", str(tmp_path))
    (tmp_path / "home").mkdir()
    monkeypatch.delenv("AGENTD_CLI_MODE", raising=False)

    captured: dict[str, Path] = {}

    def fake_get_or_create(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        fake = MagicMock()
        fake.proc = MagicMock()
        fake.proc.pid = 1
        fake.last_rss_kb = 0
        fake.ensure_spawned = MagicMock()
        return fake

    with (
        patch.object(turn_mod, "resolve_adapter", return_value="claude-code"),
        patch.object(cli_session, "use_oneshot", return_value=False),
        patch.object(cli_session, "get_or_create_session", side_effect=fake_get_or_create),
    ):
        result = turn_mod.ensure_role_cli_spawned("architect")

    assert result.get("spawned") is True
    assert captured["tmp"] == tmp_path / "home" / "architect" / "tmp"
    assert captured["xdg"] == tmp_path / "home" / "architect" / "xdg"
    assert captured["home"] == tmp_path / "home" / "architect"
    assert str(pinned) not in str(captured["tmp"])
    assert str(pinned) not in str(captured["xdg"])
    assert not pinned.exists()


def test_ensure_role_layout_without_issue_skips_env_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#76 B2: init-time layout is project-scoped; must not recreate a CLOSED session."""
    pinned = tmp_path / "sessions" / "47"
    monkeypatch.setenv("AGENTD_SESSION_DIR", str(pinned))
    monkeypatch.setenv("AGENTD_PROJECT_ROOT", str(tmp_path))
    (tmp_path / "home").mkdir()

    env = runner_server.ensure_role_layout("architect")
    assert Path(env["TMPDIR"]) == tmp_path / "home" / "architect" / "tmp"
    assert (tmp_path / "home" / "architect" / "tmp").is_dir()
    assert (tmp_path / "home" / "architect" / "xdg").is_dir()
    assert not pinned.exists()


def test_ensure_role_layout_with_issue_creates_context_not_env_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#76 B2: turn-time layout creates the issue tree, not the env pin."""
    pinned = tmp_path / "sessions" / "47"
    monkeypatch.setenv("AGENTD_SESSION_DIR", str(pinned))
    monkeypatch.setenv("AGENTD_PROJECT_ROOT", str(tmp_path))
    (tmp_path / "home").mkdir()

    runner_server.ensure_role_layout("architect", 32)
    assert (tmp_path / "sessions" / "32" / "architect" / "context").is_dir()
    assert (tmp_path / "sessions" / "32" / "architect" / "scratch").is_dir()
    assert not (pinned / "architect" / "context").exists()


def test_live_spawn_drops_privs_once_via_popen_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Privilege drop at spawn via Popen(user=) — not per turn (#25 / §7.3 / NB2)."""
    monkeypatch.delenv("AGENTD_CLI_MODE", raising=False)
    import queue as qmod

    spawn_kwargs: list[dict] = []

    def fake_popen(**kwargs):  # noqa: ANN003
        spawn_kwargs.append(dict(kwargs))
        fake = MagicMock()
        fake.poll.return_value = None
        fake.pid = 55
        fake.stdin = MagicMock()
        fake.stdout = MagicMock()
        fake.stdout.readline.side_effect = [""]  # reader thread EOF
        return fake

    with patch("agentd_runner.cli_session.subprocess.Popen", side_effect=fake_popen), patch(
        "agentd_runner.cli_session.os.geteuid", return_value=0
    ), patch("agentd_runner.cli_session.os.chown"), patch.object(
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
        sess.ensure_spawned()
        assert len(spawn_kwargs) == 1
        assert spawn_kwargs[0].get("user") == 1001
        assert spawn_kwargs[0].get("group") == 1001
        assert spawn_kwargs[0].get("extra_groups") == []
        # stderr is a file, not PIPE
        assert spawn_kwargs[0].get("stderr") is not None

        # Two turns without re-spawn: drop only once
        q: qmod.Queue[str | None] = qmod.Queue()
        for _ in range(2):
            q.put(
                json.dumps(
                    {
                        "type": "assistant",
                        "session_id": "x",
                        "message": {"content": [{"type": "text", "text": "ok"}]},
                    }
                )
            )
            q.put(json.dumps({"type": "result", "session_id": "x", "result": "ok"}))
        sess._stdout_q = q
        r1 = sess.turn("t1", deadline_s=5)
        r2 = sess.turn("t2", deadline_s=5)

    assert r1["status"] == "done" and r2["status"] == "done"
    assert len(spawn_kwargs) == 1


def test_drop_privs_clears_groups_and_rejects_root() -> None:
    """_drop_privs: setgroups([]) → setgid → setuid; fails closed if still euid 0."""
    calls: list[tuple] = []

    def setgroups(g):  # noqa: ANN001
        calls.append(("setgroups", list(g)))

    def setgid(g):  # noqa: ANN001
        calls.append(("setgid", g))

    def setuid(u):  # noqa: ANN001
        calls.append(("setuid", u))

    with patch("agentd_runner.cli_session.os.setgroups", side_effect=setgroups), patch(
        "agentd_runner.cli_session.os.setgid", side_effect=setgid
    ), patch("agentd_runner.cli_session.os.setuid", side_effect=setuid), patch(
        "agentd_runner.cli_session.os.geteuid", return_value=1001
    ):
        cli_session._drop_privs(1001)
    assert calls[0] == ("setgroups", [])
    assert calls[1] == ("setgid", 1001)
    assert calls[2] == ("setuid", 1001)

    with patch("agentd_runner.cli_session.os.setgroups"), patch(
        "agentd_runner.cli_session.os.setgid"
    ), patch("agentd_runner.cli_session.os.setuid"), patch(
        "agentd_runner.cli_session.os.geteuid", return_value=0
    ):
        with pytest.raises(RuntimeError, match="still euid 0"):
            cli_session._drop_privs(1001)


def test_role_env_paths_under_role_tree(tmp_path: Path) -> None:
    home = tmp_path / "home" / "architect"
    tmp = tmp_path / "tmp"
    xdg = tmp_path / "xdg"
    env = cli_session._role_env("architect", home, tmp, xdg)
    assert env["HOME"] == str(home)
    assert env["TMPDIR"] == str(tmp)
    assert env["XDG_CACHE_HOME"] == str(xdg / "cache")
    assert env["XDG_CONFIG_HOME"] == str(xdg / "config")
    assert env["XDG_DATA_HOME"] == str(xdg / "data")


def test_build_prompt_includes_worktree() -> None:
    p = turn_mod.build_prompt(
        {
            "role": "architect",
            "turn_id": "t1",
            "event": {"kind": "x"},
            "context": {"worktree": "/srv/agentd/sessions/i1/architect/worktrees/1"},
        },
        None,
    )
    assert "Issue worktree:" in p
    assert "/worktrees/1" in p


def test_health_ping_includes_cli_rss(runner_env: tuple[int, Path]) -> None:
    port, _ = runner_env
    resp = _rpc(
        port,
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session.attach",
                "params": {"bearer": "test-bearer-token-32bytes-minimum!!"},
            },
            {"jsonrpc": "2.0", "id": 2, "method": "health.ping", "params": {}},
        ],
    )
    assert resp[1]["result"]["ok"] is True
    assert "cli_rss_kb" in resp[1]["result"]


def test_teardown_shuts_down_cli(runner_env: tuple[int, Path], tmp_path: Path) -> None:
    # Place a fake live session in the registry
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
    cli_session._SESSIONS["architect"] = sess
    port, _ = runner_env
    _rpc(
        port,
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session.attach",
                "params": {"bearer": "test-bearer-token-32bytes-minimum!!"},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session.init",
                "params": {
                    "session_key": "o/r#1",
                    "roles": {"architect": "a", "developer": "d"},
                    "tokens": {"architect": "tok-a", "developer": "tok-d"},
                },
            },
            {"jsonrpc": "2.0", "id": 3, "method": "session.teardown", "params": {}},
        ],
    )
    assert "architect" not in cli_session._SESSIONS or not cli_session._SESSIONS
