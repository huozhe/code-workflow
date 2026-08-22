"""#187: a suite run must not be able to rebuild the deployed runner image.

`session_runner_image` used to build `IMAGE` — the tag `ensure_session` creates
containers from — so any suite run redeployed the host's runner from whatever
worktree it happened to be in. The direction that matters is the bad one: a run
from a stale branch downgraded the deployed image with nothing to report it.
"""

from __future__ import annotations

import os

import pytest

from agentd.supervisor import DEFAULT_IMAGE, runner_image


def test_suite_never_resolves_to_the_deployed_tag() -> None:
    """The guard itself: under the suite, nothing names the deployed image."""
    assert os.environ.get("AGENTD_RUNNER_IMAGE"), "conftest must isolate the tag"
    assert runner_image() != DEFAULT_IMAGE


def test_deployed_tag_is_still_the_default_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production is unchanged: absent the variable, the deployed tag is used.

    Pins the half that must *not* move — an isolation that also changed what the
    daemon runs would trade one silent deploy for another.
    """
    monkeypatch.delenv("AGENTD_RUNNER_IMAGE", raising=False)
    assert runner_image() == "agentd/session-runner:1.2.0"
    assert DEFAULT_IMAGE == "agentd/session-runner:1.2.0"


def test_resolution_is_not_frozen_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read per call, not at import.

    A module constant assigned from the environment would be fixed by whichever
    import ran first, which is the ordering hazard that would reintroduce #187
    silently.
    """
    monkeypatch.setenv("AGENTD_RUNNER_IMAGE", "agentd/session-runner:probe")
    assert runner_image() == "agentd/session-runner:probe"


def test_fixture_builds_a_tag_that_is_not_the_deployed_one(
    session_runner_image: str,
) -> None:
    """The build target itself, for the run that actually invokes docker."""
    assert session_runner_image != DEFAULT_IMAGE
