"""Shared fixtures for agentd tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agentd import design_loop
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


# --- no test may write to GitHub -------------------------------------------
#
# ``DesignLoop._escalate`` falls back to the real ``post_issue_comment`` with the
# gateway's Keychain token whenever ``post_comment=`` is not injected, so a test
# that escalates posts a comment to a live issue as ``huozhegateway``. That is
# the §5.5 rule appearing as a defect: the identity says something no operator
# decided, and GitHub records only the identity, so it cannot be audited after
# the fact. On CI there is no Keychain and the call fails silently, which is why
# this went unnoticed — the only symptom is comments on a real issue.
#
# Default is to fail loudly. Tests that legitimately exercise the escalation
# comment path opt in with ``allows_github_post`` and get a recorder instead.


@pytest.fixture
def allows_github_post(monkeypatch):
    """Recorder for the two pre-existing tests that escalate as a side effect.

    Opt-in and named, so an exemption is visible rather than silent. Neither
    test asserts anything about the comment; recording is strictly better than
    posting, and no behaviour changes.
    """
    posted: list[dict] = []

    def _record(**kwargs):
        posted.append(kwargs)
        return len(posted)

    monkeypatch.setattr(design_loop, "post_issue_comment", _record)
    return posted


@pytest.fixture(autouse=True)
def _no_github_writes(request, monkeypatch):
    if "allows_github_post" in request.fixturenames:
        yield  # the opt-in fixture installs its own recorder
        return

    attempts: list[str] = []

    def _refuse(**kwargs):
        attempts.append(f"{kwargs.get('repo')}#{kwargs.get('issue_num')}")
        return 0

    monkeypatch.setattr(design_loop, "post_issue_comment", _refuse)
    yield
    # Raising inside the call would be swallowed: _escalate catches every
    # exception and appends ``[comment_post_failed: …]`` to the reason, which is
    # precisely why this has been invisible on CI. Fail after the test instead.
    assert not attempts, (
        "test would POST a comment as the gateway to "
        f"{', '.join(attempts)}. Inject post_comment= into DesignLoop, or "
        "request the allows_github_post fixture if the post is under test."
    )
