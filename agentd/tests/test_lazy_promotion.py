"""ADR-25 / #116: lazy promotion — serviceable probe + ensure_session repair."""

from __future__ import annotations

import logging
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Any, ClassVar

import pytest

from agentd.config import Config
from agentd.db import Store
from agentd.design_loop import DesignLoop
from agentd.refusals import CapacityRefusal
from agentd.supervisor import IMAGE, SessionSupervisor


def _cfg(tmp: Path, *, max_hot: int = 4) -> Config:
    return Config(
        raw={
            "host": {"owner": "huozhe", "max_hot_containers": max_hot},
            "gateway": {"login": "huozhegateway"},
            "agents": {
                "claude": {"login": "huozheclaude"},
                "grok": {"login": "huozhegrok"},
            },
        },
        root=tmp,
    )


def _seed(store: Store, *, sk: str = "o/r#1", cid: str = "cid-keep") -> str:
    store.upsert_session(
        session_key=sk,
        project_key="o/r",
        repo="o/r",
        issue_num=1,
        state="IMPLEMENTING",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
    )
    store.upsert_runner(
        "o/r",
        container_id=cid,
        endpoint="127.0.0.1:1",
        token="tok",
        tier="cold",
    )
    return sk


class _FakePing:
    """RunnerClient stand-in. ping_result is what health.ping returns."""

    ping_result: ClassVar[Any] = {"ok": True, "initialized": True}
    ping_exc: ClassVar[BaseException | None] = None
    calls: ClassVar[list[str]] = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def call(self, method, params=None):
        type(self).calls.append(method)
        if method == "health.ping":
            if type(self).ping_exc is not None:
                raise type(self).ping_exc
            return type(self).ping_result
        return {}


def test_runner_reachable_requires_initialized(tmp_path: Path) -> None:
    """Reachability is serviceable, not answering (ADR-25)."""
    store = Store(tmp_path / "state.db")
    loop = DesignLoop(store, _cfg(tmp_path), supervisor=None, dispatch_turns=False)
    runner = {
        "endpoint": "127.0.0.1:9",
        "token": "tok",
    }
    import agentd.design_loop as dl

    orig = dl.RunnerClient
    try:
        _FakePing.ping_result = {"ok": True, "initialized": False}
        _FakePing.ping_exc = None
        dl.RunnerClient = _FakePing  # type: ignore[misc]
        assert loop._runner_reachable(runner) is False

        _FakePing.ping_result = {"ok": True, "initialized": True}
        assert loop._runner_reachable(runner) is True
    finally:
        dl.RunnerClient = orig  # type: ignore[misc]
    store.close()


class _DockerWorld:
    def __init__(self, *, running: bool, image: str = IMAGE, exists: bool = True):
        self.running = running
        self.image = image
        self.exists = exists
        self.removed: list[tuple] = []
        self.started: list[str] = []
        self.worktrees: list[tuple[str, int]] = []

    def docker(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else ""
        if cmd == "inspect":
            if not self.exists:
                proc = subprocess.CompletedProcess(args, 1, "", "No such container")
                if check:
                    raise subprocess.CalledProcessError(1, args, "", "No such container")
                return proc
            # last arg is format string
            fmt = args[-1] if args else ""
            if "State.Running" in fmt:
                out = f"{str(self.running).lower()}\t{self.image}\n"
            else:
                out = "{}\n"
            return subprocess.CompletedProcess(args, 0, out, "")
        if cmd == "start":
            self.started.append(args[1] if len(args) > 1 else "")
            self.running = True
            return subprocess.CompletedProcess(args, 0, "", "")
        if cmd == "rm":
            self.removed.append(args)
            self.exists = False
            return subprocess.CompletedProcess(args, 0, "", "")
        if cmd == "port":
            return subprocess.CompletedProcess(args, 0, "127.0.0.1:5555\n", "")
        if cmd == "image":
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")


def _wire_supervisor(sup: SessionSupervisor, world: _DockerWorld, monkeypatch) -> None:
    monkeypatch.setattr("agentd.supervisor._docker", world.docker)
    monkeypatch.setattr("agentd.supervisor.image_present", lambda image=IMAGE: True)
    monkeypatch.setattr(
        "agentd.supervisor._host_port_from_inspect", lambda cid: 5555
    )
    monkeypatch.setattr(sup, "_prepare_project_issue_layout", lambda **k: None)
    monkeypatch.setattr(sup, "_register_session_layout_artifacts", lambda **k: None)
    monkeypatch.setattr(sup, "_load_tokens", lambda: {"architect": "a", "developer": "d"})
    monkeypatch.setattr(sup, "_load_model_credentials", dict)
    monkeypatch.setattr(sup, "_wait_rpc", lambda *a, **k: None)

    def _wt(_cid, *, issue_num, role, uid):
        world.worktrees.append((role, issue_num))

    monkeypatch.setattr("agentd.supervisor.assert_worktree_usable", _wt)
    import agentd.supervisor as sm

    monkeypatch.setattr(sm, "RunnerClient", _FakePing)
    _FakePing.ping_exc = None
    _FakePing.ping_result = {"ok": True, "initialized": True}
    _FakePing.calls = []


def test_ensure_stopped_promotes_same_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance (1): demoted/stopped → same container_id, tier hot."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, cid="cid-keep")
    world = _DockerWorld(running=False)
    sup = SessionSupervisor(store, _cfg(tmp_path))
    _wire_supervisor(sup, world, monkeypatch)
    _FakePing.ping_exc = None
    _FakePing.ping_result = {"ok": True, "initialized": True}
    _FakePing.calls = []

    handle = sup.ensure_session(session_key=sk, repo="o/r", issue_num=1)
    assert handle.container_id == "cid-keep"
    row = store.get_runner("o/r")
    assert row is not None
    assert row["tier"] == "hot"
    assert row["container_id"] == "cid-keep"
    assert not world.removed
    assert {r for r, n in world.worktrees if n == 1} == {"architect", "developer"}
    store.close()


def test_ensure_stopped_promotes_without_session_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Create path: new issue, stopped project runner, no session row (ADR-25)."""
    store = Store(tmp_path / "state.db")
    store.upsert_runner(
        "o/r",
        container_id="cid-keep",
        endpoint="127.0.0.1:1",
        token="tok",
        tier="cold",
    )
    world = _DockerWorld(running=False)
    sup = SessionSupervisor(store, _cfg(tmp_path))
    _wire_supervisor(sup, world, monkeypatch)
    handle = sup.ensure_session(session_key="o/r#42", repo="o/r", issue_num=42)
    assert handle.container_id == "cid-keep"
    assert not world.removed
    row = store.get_runner("o/r")
    assert row is not None
    assert row["tier"] == "hot"
    sess = store.get_session("o/r#42")
    assert sess is not None
    assert {r for r, n in world.worktrees if n == 42} == {"architect", "developer"}
    store.close()


def test_ensure_plants_port_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance (2): planted endpoint must not survive promotion."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, cid="cid-keep")
    world = _DockerWorld(running=False)
    sup = SessionSupervisor(store, _cfg(tmp_path))
    _wire_supervisor(sup, world, monkeypatch)
    _FakePing.ping_exc = None
    _FakePing.ping_result = {"ok": True, "initialized": True}
    _FakePing.calls = []

    handle = sup.ensure_session(session_key=sk, repo="o/r", issue_num=1)
    row = store.get_runner("o/r")
    assert row is not None
    assert row["endpoint"] == "127.0.0.1:5555"
    assert row["endpoint"] != "127.0.0.1:1"
    assert handle.endpoint == "127.0.0.1:5555"
    store.close()


def test_ensure_stopped_wrong_image_recreates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance (3): stopped + image != IMAGE → recreate, not start."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, cid="cid-old")
    world = _DockerWorld(running=False, image="agentd/session-runner:old")
    sup = SessionSupervisor(store, _cfg(tmp_path))
    _wire_supervisor(sup, world, monkeypatch)
    created: list[str] = []

    def fake_create(*a, **k):
        created.append("yes")
        raise RuntimeError("stop-before-real-create")

    monkeypatch.setattr(sup, "_prepare_project_issue_layout", lambda **k: None)
    # Recreate path will try docker create after rm. We only need rm + no start.
    with pytest.raises(RuntimeError, match="stop-before-real-create"):
        # Fall through to create; stub the rest of create by exploding at create args
        orig_docker = world.docker

        def docker_create(*args, check=True):
            if args and args[0] == "create":
                created.append("create")
                raise RuntimeError("stop-before-real-create")
            return orig_docker(*args, check=check)

        monkeypatch.setattr("agentd.supervisor._docker", docker_create)
        sup.ensure_session(session_key=sk, repo="o/r", issue_num=1)

    assert world.started == []
    assert any(t[0] == "rm" for t in world.removed) or created
    store.close()


def test_ensure_capacity_refusal_keeps_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance (4): at cap, CapacityRefusal and the stopped container stays."""
    store = Store(tmp_path / "state.db")
    store.upsert_session(
        session_key="other/r#9",
        project_key="other/r",
        repo="other/r",
        issue_num=9,
        state="IMPLEMENTING",
        architect="a",
        developer="d",
        created_at=1,
        updated_at=1,
    )
    store.upsert_runner(
        "other/r",
        container_id="hot-1",
        endpoint="127.0.0.1:2",
        token="t",
        tier="hot",
    )
    sk = _seed(store, cid="cid-keep")
    world = _DockerWorld(running=False)
    sup = SessionSupervisor(store, _cfg(tmp_path, max_hot=1))
    _wire_supervisor(sup, world, monkeypatch)
    with pytest.raises(CapacityRefusal):
        sup.ensure_session(session_key=sk, repo="o/r", issue_num=1)
    assert world.exists is True
    assert world.removed == []
    row = store.get_runner("o/r")
    assert row is not None
    assert row["container_id"] == "cid-keep"
    store.close()


def test_ensure_running_uninitialised_promotes_same_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance (5): running + initialized false → resume, keep container_id."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, cid="cid-keep")
    store.upsert_runner(
        "o/r",
        container_id="cid-keep",
        endpoint="127.0.0.1:1",
        token="tok",
        tier="hot",
    )
    world = _DockerWorld(running=True)
    sup = SessionSupervisor(store, _cfg(tmp_path))
    _wire_supervisor(sup, world, monkeypatch)
    _FakePing.ping_exc = None
    _FakePing.ping_result = {"ok": True, "initialized": False}
    _FakePing.calls = []

    handle = sup.ensure_session(session_key=sk, repo="o/r", issue_num=1)
    assert handle.container_id == "cid-keep"
    assert "session.resume" in _FakePing.calls
    assert not world.removed
    assert {r for r, n in world.worktrees if n == 1} == {"architect", "developer"}
    store.close()


def test_recreate_warning_names_reason_and_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The recreate destroys the evidence, so the log must carry it (#116)."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, cid="cid-gone")
    world = _DockerWorld(running=False, exists=False)  # inspect fails -> "gone"
    sup = SessionSupervisor(store, _cfg(tmp_path))
    _wire_supervisor(sup, world, monkeypatch)

    # The recreate that follows is not what this test is about; the fake world
    # cannot complete a real create, so only the warning matters here.
    with caplog.at_level(logging.WARNING, logger="agentd.supervisor"), suppress(Exception):
        sup.ensure_session(session_key=sk, repo="o/r", issue_num=1)

    recreate = [r for r in caplog.records if "recreating" in r.getMessage()]
    assert recreate, "no recreate warning emitted"
    msg = recreate[0].getMessage()
    assert "reason=" in msg, msg
    assert "cid-gone" in msg, msg
    store.close()
