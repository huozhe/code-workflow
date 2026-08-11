"""#45: turn prompt carries session state + role obligation (§8.2)."""

from __future__ import annotations

import sys
from pathlib import Path

from agentd.config import Config
from agentd.db import Store
from agentd.design_loop import DesignLoop

_RUNNER_ROOT = Path(__file__).resolve().parents[1] / "docker" / "session-runner"
sys.path.insert(0, str(_RUNNER_ROOT))

from agentd_runner.turn import build_prompt, role_obligation  # noqa: E402


def test_role_obligation_table() -> None:
    assert "Do not implement" in role_obligation("architect", "PLANNING")
    assert "Design PR" in role_obligation("architect", "PLANNING")
    assert "request changes or approve" in role_obligation("developer", "DESIGN_REVIEW")
    assert "Do not implement" in role_obligation("developer", "DESIGN_REVIEW")
    assert "Merge the Design PR" in role_obligation("architect", "DESIGN_APPROVED")
    assert "Feature PR" in role_obligation("developer", "IMPLEMENTING")
    assert "Revise" in role_obligation("architect", "DESIGN_REWORK")
    # Idle / wait rows — PR #46 B1 (Architect post-merge is the live failure mode)
    arch_impl = role_obligation("architect", "IMPLEMENTING")
    assert "Wait for the Feature PR" in arch_impl
    assert "Do not implement" in arch_impl
    assert "do not close" in arch_impl.lower()
    assert "Wait" in role_obligation("developer", "PLANNING")
    assert "Wait" in role_obligation("developer", "DESIGN_APPROVED")
    assert role_obligation("architect", "INTAKE") == ""


def test_build_prompt_includes_state_and_obligation() -> None:
    text = build_prompt(
        {
            "role": "architect",
            "turn_id": "t-45",
            "session_state": "PLANNING",
            "event": {"kind": "issue_opened"},
            "context": {"worktree": "/srv/wt"},
        },
        None,
    )
    assert "Session state: PLANNING" in text
    assert "Your obligation in this state:" in text
    assert "Do not implement" in text
    assert "no implementation code before DESIGN_APPROVED" in text
    assert "Role: architect" in text


def test_build_prompt_developer_design_review() -> None:
    text = build_prompt(
        {
            "role": "developer",
            "turn_id": "t-45b",
            "session_state": "DESIGN_REVIEW",
            "event": {"kind": "design_pr_opened"},
        },
        None,
    )
    assert "Session state: DESIGN_REVIEW" in text
    assert "request changes or approve" in text
    assert "Do not implement" in text


def test_build_prompt_architect_implementing_waits() -> None:
    """Architect after Design PR merge must be told to wait (PR #46 B1)."""
    text = build_prompt(
        {
            "role": "architect",
            "turn_id": "t-45c",
            "session_state": "IMPLEMENTING",
            "event": {"kind": "design_merged"},
        },
        None,
    )
    assert "Session state: IMPLEMENTING" in text
    assert "Your obligation in this state:" in text
    assert "Wait for the Feature PR" in text
    assert "Do not implement" in text


def test_build_prompt_invariant_without_state() -> None:
    """Missing session_state still states the no-code invariant."""
    text = build_prompt(
        {"role": "architect", "turn_id": "t-x", "event": {"kind": "x"}},
        None,
    )
    assert "Session state:" not in text
    assert "no implementation code before DESIGN_APPROVED" in text


def test_dispatch_passes_session_state(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.upsert_session(
        session_key="o/r#1",
        project_key="o/r",
        repo="o/r",
        issue_num=1,
        state="DESIGN_REVIEW",
        architect="a",
        developer="d",
        created_at=1,
        updated_at=1,
    )
    store.upsert_runner(
        "o/r",
        container_id="c1",
        endpoint="127.0.0.1:9",
        token="tok",
        tier="hot",
    )
    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *a, **k):  # noqa: ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):  # noqa: ANN002
            return False

        def call(self, method, params=None):  # noqa: ANN001
            seen["method"] = method
            seen["params"] = dict(params or {})
            return {"status": "done", "summary": "ok"}

    import agentd.design_loop as dl

    orig = dl.RunnerClient
    dl.RunnerClient = FakeClient  # type: ignore[misc, assignment]
    try:
        cfg = Config(raw={}, root=tmp_path)
        loop = DesignLoop(store, cfg, supervisor=None, dispatch_turns=True)
        out = loop._dispatch_turn(
            session_key="o/r#1",
            role="developer",
            turn_id="t-state",
            delivery_id="d1",
            dig={"kind": "design_pr_opened"},
            issue_num=1,
        )
    finally:
        dl.RunnerClient = orig  # type: ignore[misc]

    assert seen["method"] == "turn.dispatch"
    params = seen["params"]
    assert isinstance(params, dict)
    assert params.get("session_state") == "DESIGN_REVIEW"
    assert params.get("role") == "developer"
    assert out is not None and out["status"] == "done"
    store.close()
