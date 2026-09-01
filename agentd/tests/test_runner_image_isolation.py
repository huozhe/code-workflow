"""#187: a suite run must not be able to rebuild the deployed runner image.

`session_runner_image` used to build `IMAGE` — the tag `ensure_session` creates
containers from — so any suite run redeployed the host's runner from whatever
worktree it happened to be in. The direction that matters is the bad one: a run
from a stale branch downgraded the deployed image with nothing to report it.
"""

from __future__ import annotations

import logging
import os

import pytest
from test_lazy_promotion import _cfg, _DockerWorld, _seed, _wire_supervisor

from agentd.supervisor import DEFAULT_IMAGE, SessionSupervisor, runner_image


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

    The literal is the guard, so moving it is never incidental: a change here is
    a **deploy**, and it is only correct paired with a build of that exact tag
    plus `docker rm -f` on the project container (a running container is adopted
    regardless of image — ADR-25's named residual). Bumped 1.2.0 -> 1.3.0 by
    ADR-35 (#172), 1.3.0 -> 1.4.0 by #208/#210/#212, and 1.4.0 -> 1.5.0 by #233 —
    the teardown token wipe now drops to the role uid, because `--cap-drop ALL`
    denies uid 0 the `0700` dir. All of these are inside the image.
    """
    monkeypatch.delenv("AGENTD_RUNNER_IMAGE", raising=False)
    assert runner_image() == "agentd/session-runner:1.5.0"
    assert DEFAULT_IMAGE == "agentd/session-runner:1.5.0"


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


def _drive_create(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reach `docker create` on the recreate path, then stop before real docker.

    Reuses `test_lazy_promotion`'s fake world rather than duplicating it; the
    log under test is emitted just before the create call.
    """
    from agentd.db import Store

    store = Store(tmp_path / "state.db")
    sk = _seed(store, cid="cid-old")
    world = _DockerWorld(running=False, image="agentd/session-runner:old")
    sup = SessionSupervisor(store, _cfg(tmp_path))
    _wire_supervisor(sup, world, monkeypatch)
    monkeypatch.setattr(sup, "_prepare_project_issue_layout", lambda **k: None)
    orig = world.docker

    def docker_create(*args, check=True):
        if args and args[0] == "create":
            raise RuntimeError("stop-before-real-create")
        return orig(*args, check=check)

    monkeypatch.setattr("agentd.supervisor._docker", docker_create)
    with pytest.raises(RuntimeError, match="stop-before-real-create"):
        sup.ensure_session(session_key=sk, repo="o/r", issue_num=1)
    store.close()


def test_create_logs_the_resolved_image(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#187 follow-up: which image this daemon is on must be answerable from the log.

    `AGENTD_RUNNER_IMAGE` is read on every call, so a daemon started from a shell
    that exported it creates every container from that tag while the mismatch
    guards — which compare against the same call — stay consistent and say
    nothing. An override is a warning; the ordinary case is info.
    """
    with caplog.at_level(logging.INFO, logger="agentd.supervisor"):
        monkeypatch.setenv("AGENTD_RUNNER_IMAGE", "agentd/session-runner:elsewhere")
        _drive_create(tmp_path / "a", monkeypatch)
    assert any(
        r.levelno == logging.WARNING and "OVERRIDDEN" in r.getMessage()
        and "agentd/session-runner:elsewhere" in r.getMessage()
        for r in caplog.records
    ), "an overridden image must be logged as a warning"

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="agentd.supervisor"):
        monkeypatch.delenv("AGENTD_RUNNER_IMAGE", raising=False)
        _drive_create(tmp_path / "b", monkeypatch)
    msgs = [r.getMessage() for r in caplog.records]
    assert any(f"container image {DEFAULT_IMAGE}" in m for m in msgs), msgs
