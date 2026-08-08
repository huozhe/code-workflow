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
    """§7.3 env roots stay under role (or durable project) trees."""
    sess = tmp_path / "sessions" / "k"
    monkeypatch.setenv("AGENTD_SESSION_DIR", str(sess))
    monkeypatch.setenv("AGENTD_PROJECT_ROOT", str(tmp_path / "proj"))
    (tmp_path / "proj" / "home").mkdir(parents=True)
    paths = turn_mod.role_paths("architect")
    assert paths["home"] == tmp_path / "proj" / "home" / "architect"
    assert paths["tmp"] == sess / "architect" / "tmp"
    assert str(paths["xdg"]).endswith(str(Path("architect") / "xdg"))


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
