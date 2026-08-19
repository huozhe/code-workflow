"""Shared fixtures for agentd tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agentd.supervisor import IMAGE


@pytest.fixture(scope="session", autouse=True)
def _isolate_agentd_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """#121: point AGENTD_ROOT at a throwaway dir for the whole suite."""
    root = tmp_path_factory.mktemp("agentd-root")
    prev = os.environ.get("AGENTD_ROOT")
    os.environ["AGENTD_ROOT"] = str(root)
    yield root
    if prev is None:
        os.environ.pop("AGENTD_ROOT", None)
    else:
        os.environ["AGENTD_ROOT"] = prev

_RUNNER_ROOT = Path(__file__).resolve().parents[1] / "docker" / "session-runner"


def _docker_ok() -> bool:
    try:
        r = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10, check=False
        )
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
        check=False,
    )
    if r.returncode != 0:
        pytest.fail(
            f"session-runner image build failed (tag={IMAGE}):\n"
            f"{(r.stderr or r.stdout or '')[-2000:]}"
        )
    return IMAGE
