"""Docker integration — health.ping, no docker.sock, adversarial token (§5.2)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agentd.config import Config
from agentd.db import Store
from agentd.supervisor import IMAGE, SessionSupervisor, assert_no_docker_sock_mount, image_present


def _docker_ok() -> bool:
    try:
        r = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not _docker_ok(), reason="docker not available")


@pytest.fixture(scope="module")
def built_image() -> None:
    if image_present():
        return
    root = Path(__file__).resolve().parents[1]
    dockerfile = root / "docker" / "session-runner" / "Dockerfile"
    context = root / "docker" / "session-runner"
    r = subprocess.run(
        [
            "docker",
            "build",
            "-t",
            IMAGE,
            "-f",
            str(dockerfile),
            str(context),
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"image build failed: {r.stderr[-500:]}")


def test_session_health_ping_and_token_boundary(
    tmp_path: Path, built_image: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTD_SECRET_CLAUDE_BOT", "test-claude-pat")
    monkeypatch.setenv("AGENTD_SECRET_GROK_BOT", "test-grok-pat")
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
    # Local clone source
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
            skip_preflight=True,
            clone_url=str(src),
        )
        assert handle.tier == "hot"
        assert_no_docker_sock_mount(handle.container_id)

        # Mounts must not include docker.sock
        insp = subprocess.run(
            ["docker", "inspect", handle.container_id, "--format", "{{json .Mounts}}"],
            capture_output=True,
            text=True,
            check=True,
        )
        mounts = json.loads(insp.stdout)
        for m in mounts:
            assert "docker.sock" not in str(m.get("Source", ""))
            assert "docker.sock" not in str(m.get("Destination", ""))

        boundary = sup.adversarial_token_check(handle)
        assert boundary["developer_can_read_architect_token"] is False, boundary

        # COLD then HOT
        sup.demote_cold(key)
        handle2 = sup.promote_hot(key)
        assert handle2.tier == "hot"
    finally:
        subprocess.run(
            ["docker", "rm", "-f", f"agentd-{key.replace('/', '-').replace('#', '-')}"],
            capture_output=True,
        )
        store.close()
