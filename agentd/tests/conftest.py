"""Shared fixtures for agentd tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import httpx
import pytest

from agentd import design_loop
from agentd.supervisor import DEFAULT_IMAGE, runner_image


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

# #187: never the deployed tag. Building DEFAULT_IMAGE here would make every
# suite run a deploy — including one from a stale branch, which downgrades the
# runner with nothing to report it.
TEST_IMAGE = "agentd/session-runner:pytest"

_RUNNER_ROOT = Path(__file__).resolve().parents[1] / "docker" / "session-runner"


@pytest.fixture(scope="session", autouse=True)
def _isolate_runner_image() -> str:
    """#187: point the whole suite at its own image tag."""
    prev = os.environ.get("AGENTD_RUNNER_IMAGE")
    os.environ["AGENTD_RUNNER_IMAGE"] = TEST_IMAGE
    yield TEST_IMAGE
    if prev is None:
        os.environ.pop("AGENTD_RUNNER_IMAGE", None)
    else:
        os.environ["AGENTD_RUNNER_IMAGE"] = prev


def _docker_ok() -> bool:
    try:
        r = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10, check=False
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


@pytest.fixture(scope="session")
def session_runner_image(_isolate_runner_image: str) -> str:
    """Build the *test* session-runner image once per test session.

    When Docker is available, a failed build **fails** the suite (#24 B3) —
    skip is only for hosts without Docker.
    """
    if not _docker_ok():
        pytest.skip("docker not available")
    image = runner_image()
    assert image != DEFAULT_IMAGE, "#187: the suite must not build the deployed tag"
    r = subprocess.run(
        [
            "docker",
            "build",
            "-t",
            image,
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
            f"session-runner image build failed (tag={image}):\n"
            f"{(r.stderr or r.stdout or '')[-2000:]}"
        )
    return image


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


# --- no test may talk to the GitHub API (ADR-36 (e)) -----------------------
#
# ``_no_github_writes`` patches ``design_loop.post_issue_comment`` only. The
# live M4-A test writes through ``httpx`` and never imports ``design_loop``,
# so that guard is green while the write happens. A credential-read guard on
# ``get_password`` is the same hole: the live test prefers ``GH_TOKEN`` /
# ``AGENTD_SECRET_CLAUDE_BOT``, and ``from agentd.keychain import get_password``
# binds before fixtures run. Key the refusal on the act instead: any request
# from this process to api.github.com, reads included.
#
# ``httpx.Client.send`` is the chokepoint — every get/post/put/patch/delete
# on every Client goes through it. There is no AsyncClient and no module-level
# ``httpx.get`` in agentd/src or agentd/tests. urllib in the runner preflight
# is a named residual and does not run in this process.
#
# ``Client.send`` is one layer above the network: ``MockTransport`` and any
# other in-process stub still call it. Those tests must request
# ``allows_github_api`` (or re-patch ``send``) even though nothing leaves the
# process. That is the trade for wrapping ``send`` rather than
# ``HTTPTransport.handle_request`` — ADR-36 (e) named ``send``, and every
# gateway ``api.github.com`` call is behind ``except Exception`` (verify.py,
# github_fetch.py), so the raise must still be recorded and asserted after
# the test, matching ``_no_github_writes``.


def _github_api_url(request: httpx.Request) -> str | None:
    host = request.url.host or ""
    if host == "api.github.com":
        return str(request.url)
    return None


def _install_github_api_guard(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Refuse api.github.com at Client.send. Returns the attempts list.

    Autouse and tests share this so a test can fail the shipped refusal,
    not a copy of it.
    """
    attempts: list[str] = []
    real_send = httpx.Client.send

    def _refuse(self, request, *args, **kwargs):
        url = _github_api_url(request)
        if url is not None:
            attempts.append(url)
            # Fast failure where the exception propagates. Also recorded so a
            # caller that swallows (verify.py / github_fetch.py BLE001) still
            # fails the test after the yield — same lesson as _no_github_writes.
            raise RuntimeError(
                "test would talk to the GitHub API: "
                f"{url}. Request the allows_github_api fixture if the call "
                "is under test."
            )
        return real_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", _refuse)
    return attempts


def _assert_no_github_api(attempts: list[str]) -> None:
    assert not attempts, (
        "test would talk to the GitHub API at "
        f"{', '.join(attempts)}. Request the allows_github_api fixture if "
        "the call is under test."
    )


@pytest.fixture
def allows_github_api(monkeypatch):
    """Opt-in: record GitHub API calls and let them through.

    Named, so an exemption is visible in the test's signature. The live M4-A
    test needs this; (d) is what removes the cross-identity hazard, this is
    what stops a silent new one.
    """
    recorded: list[str] = []
    real_send = httpx.Client.send

    def _record(self, request, *args, **kwargs):
        url = _github_api_url(request)
        if url is not None:
            recorded.append(url)
        return real_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", _record)
    return recorded


@pytest.fixture(autouse=True)
def _no_github_api(request, monkeypatch):
    if "allows_github_api" in request.fixturenames:
        yield
        return

    attempts = _install_github_api_guard(monkeypatch)
    yield
    _assert_no_github_api(attempts)
