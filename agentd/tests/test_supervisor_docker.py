"""Docker integration — health.ping, no docker.sock, adversarial token (§5.2)."""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agentd.config import Config
from agentd.db import Store
from agentd.supervisor import (
    IMAGE,
    SessionSupervisor,
    assert_bearer_not_in_inspect_env,
    assert_bearer_not_readable_by_roles,
    assert_no_docker_sock_mount,
    image_present,
)


def _docker_ok() -> bool:
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not _docker_ok(), reason="docker not available")


class _GitHubStub(BaseHTTPRequestHandler):
    """Minimal GET /user stub keyed by Bearer token → login."""

    tokens: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/user":
            self.send_response(404)
            self.end_headers()
            return
        auth = self.headers.get("Authorization", "")
        pat = auth.removeprefix("Bearer ").strip()
        login = self.tokens.get(pat)
        if not login:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"message":"bad credentials"}')
            return
        body = json.dumps({"login": login}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


@pytest.fixture(scope="module")
def built_image() -> None:
    if image_present():
        return
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run(
        [
            "docker",
            "build",
            "-t",
            IMAGE,
            "-f",
            str(root / "docker" / "session-runner" / "Dockerfile"),
            str(root / "docker" / "session-runner"),
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"image build failed: {r.stderr[-500:]}")


@pytest.fixture()
def github_stub() -> tuple[str, dict[str, str]]:
    tokens = {
        "pat-architect-realshape": "huozheclaude",
        "pat-developer-realshape": "huozhegrok",
    }
    _GitHubStub.tokens = tokens
    server = HTTPServer(("0.0.0.0", 0), _GitHubStub)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # OrbStack/Linux containers reach the host via host.docker.internal
    base = f"http://host.docker.internal:{port}"
    yield base, tokens
    server.shutdown()


def test_session_health_ping_and_token_boundary(
    tmp_path: Path,
    built_image: None,
    monkeypatch: pytest.MonkeyPatch,
    github_stub: tuple[str, dict[str, str]],
) -> None:
    api_base, tokens = github_stub
    monkeypatch.setenv("AGENTD_SECRET_CLAUDE_BOT", "pat-architect-realshape")
    monkeypatch.setenv("AGENTD_SECRET_GROK_BOT", "pat-developer-realshape")
    store = Store(tmp_path / "state.db")
    cfg = Config(
        raw={
            "host": {"owner": "huozhe"},
            "agents": {
                "claude": {"login": "huozheclaude"},
                "grok": {"login": "huozhegrok"},
            },
        },
        root=tmp_path,
    )
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init"], cwd=src, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=src, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=src, check=True, capture_output=True
    )
    (src / "f").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=src, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "i"], cwd=src, check=True, capture_output=True
    )

    sup = SessionSupervisor(store, cfg)
    key = "test/repo#1"
    try:
        handle = sup.ensure_session(
            session_key=key,
            repo="test/repo",
            issue_num=1,
            clone_url=str(src),
            github_api_base=api_base,
        )
        assert handle.tier == "hot"
        assert_no_docker_sock_mount(handle.container_id)
        assert_bearer_not_in_inspect_env(handle.container_id)
        assert_bearer_not_readable_by_roles(handle.container_id)

        insp = subprocess.run(
            ["docker", "inspect", handle.container_id, "--format", "{{json .Mounts}}"],
            capture_output=True,
            text=True,
            check=True,
        )
        for m in json.loads(insp.stdout):
            assert "docker.sock" not in str(m.get("Source", ""))
            assert "docker.sock" not in str(m.get("Destination", ""))

        # Bearer must not sit on the session bind mount either
        sess = tmp_path / "sessions" / "test__repo__1"
        assert not (sess / ".runner" / "bearer").exists()

        boundary = sup.adversarial_token_check(handle)
        assert boundary["developer_can_read_architect_token"] is False, boundary
        assert boundary["architect_can_read_developer_token"] is False, boundary

        sup.demote_cold(key)
        handle2 = sup.promote_hot(key)
        assert handle2.tier == "hot"
    finally:
        subprocess.run(
            ["docker", "rm", "-f", f"agentd-{key.replace('/', '-').replace('#', '-')}"],
            capture_output=True,
        )
        store.close()


def test_load_tokens_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTD_SECRET_CLAUDE_BOT", raising=False)
    monkeypatch.delenv("AGENTD_SECRET_GROK_BOT", raising=False)
    monkeypatch.setattr(
        "agentd.supervisor.get_password", lambda account: None
    )
    store = Store(tmp_path / "state.db")
    cfg = Config(raw={}, root=tmp_path)
    sup = SessionSupervisor(store, cfg)
    with pytest.raises(RuntimeError, match="fail-closed"):
        sup._load_tokens()
    store.close()
