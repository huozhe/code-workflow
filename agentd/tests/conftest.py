"""Shared fixtures for agentd tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentd.supervisor import IMAGE

_RUNNER_ROOT = Path(__file__).resolve().parents[1] / "docker" / "session-runner"


def _docker_ok() -> bool:
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


@pytest.fixture(scope="session")
def session_runner_image() -> str:
    """Build agentd/session-runner once per test session.

    When Docker is available, a failed build **fails** the suite (#24 B3) —
    skip is only for hosts without Docker.
    """
    if not _docker_ok():
        pytest.skip("docker not available")
    r = subprocess.run(
        [
            "docker",
            "build",
            "-t",
            IMAGE,
            "-f",
            str(_RUNNER_ROOT / "Dockerfile"),
            str(_RUNNER_ROOT),
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        pytest.fail(
            f"session-runner image build failed (tag={IMAGE}):\n"
            f"{(r.stderr or r.stdout or '')[-2000:]}"
        )
    return IMAGE
