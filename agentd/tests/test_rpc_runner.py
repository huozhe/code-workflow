"""In-process runner RPC protocol tests (no Docker)."""

from __future__ import annotations

import json
import os
import socket

# Import runner package from docker tree
import sys
import threading
import time
from pathlib import Path

import pytest

_RUNNER_ROOT = Path(__file__).resolve().parents[1] / "docker" / "session-runner"
sys.path.insert(0, str(_RUNNER_ROOT))

from agentd_runner import server as runner_server  # noqa: E402


@pytest.fixture()
def runner_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setenv("AGENTD_SKIP_PREFLIGHT", "1")
    # Point token root at tmp so we don't need /run/agent
    monkeypatch.setattr(runner_server, "TOKEN_ROOT", tmp_path / "run-agent")
    runner_server.TOKEN_ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(runner_server.TOKEN_ROOT, 0o711)

    bearer = "test-bearer-token-32bytes-minimum!!"
    runner_server.STATE = runner_server.RunnerState()
    runner_server.STATE.set_bearer(bearer)

    # Bind ephemeral
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    t = threading.Thread(
        target=runner_server.serve,
        args=("127.0.0.1", port),
        daemon=True,
    )
    # serve() overwrites bearer from env — set env
    monkeypatch.setenv("AGENTD_RUNNER_BEARER", bearer)
    t.start()
    time.sleep(0.2)
    return port


def _rpc(port: int, lines: list[dict]) -> list[dict]:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    out: list[dict] = []
    with s:
        f = s.makefile("rwb")
        for obj in lines:
            f.write((json.dumps(obj) + "\n").encode())
            f.flush()
            raw = f.readline()
            out.append(json.loads(raw.decode()))
    return out


def test_first_frame_must_be_attach(runner_port: int) -> None:
    resp = _rpc(
        runner_port,
        [{"jsonrpc": "2.0", "id": 1, "method": "health.ping", "params": {}}],
    )
    assert "error" in resp[0]
    assert "session.attach" in resp[0]["error"]["message"]


def test_attach_and_ping(runner_port: int) -> None:
    resp = _rpc(
        runner_port,
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
    assert "result" in resp[0]
    assert resp[1]["result"]["ok"] is True
    assert "rss_bytes" in resp[1]["result"]


def test_bad_bearer_rejected(runner_port: int) -> None:
    resp = _rpc(
        runner_port,
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session.attach",
                "params": {"bearer": "wrong"},
            }
        ],
    )
    assert "error" in resp[0]


def test_write_role_secret_rewrites_after_0400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """session.resume writes the token twice. euid==role-uid leaves 0400."""
    monkeypatch.setattr(runner_server, "TOKEN_ROOT", tmp_path / "run-agent")
    first = runner_server.write_role_secret("architect", "token", "first")
    os.chmod(first, 0o400)
    second = runner_server.write_role_secret("architect", "token", "second")
    assert second.read_text(encoding="utf-8") == "second"


def test_consecutive_runner_clients_issue_distinct_dispatch_ids() -> None:
    """ADR-29 acceptance (5): process-unique ids — not a #162 regression test."""
    from agentd.rpc_client import RunnerClient

    written: list[int] = []

    class FakeWfile:
        def write(self, data: bytes) -> None:
            written.append(json.loads(data.decode())["id"])

        def flush(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeRfile:
        def __init__(self) -> None:
            self._n = 0

        def readline(self) -> bytes:
            self._n += 1
            req_id = written[-1]
            return (
                json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"ok": True}})
                + "\n"
            ).encode()

        def close(self) -> None:
            return None

    ids: list[int] = []
    for _ in range(2):
        cli = RunnerClient("127.0.0.1", 1, "b")
        cli._wfile = FakeWfile()  # type: ignore[assignment]
        cli._rfile = FakeRfile()  # type: ignore[assignment]
        cli.call("turn.dispatch", {})
        ids.append(written[-1])
    assert ids[0] != ids[1]
