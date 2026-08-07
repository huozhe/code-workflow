"""Design-half FSM (§8.1)."""

from __future__ import annotations

from agentd.fsm import transition


def test_design_happy_path() -> None:
    assert transition("INTAKE", "session_created").new_state == "PLANNING"
    t = transition("PLANNING", "design_pr_opened")
    assert t is not None
    assert t.new_state == "DESIGN_REVIEW"
    assert t.freeze_roles is True
    assert transition("DESIGN_REVIEW", "design_changes_requested").new_state == "DESIGN_REWORK"
    assert transition("DESIGN_REWORK", "design_revised").new_state == "DESIGN_REVIEW"
    assert transition("DESIGN_REVIEW", "design_approved").new_state == "DESIGN_APPROVED"
    assert transition("DESIGN_APPROVED", "design_merged").new_state == "IMPLEMENTING"


def test_escalation_pause() -> None:
    t = transition("DESIGN_REVIEW", "escalation")
    assert t is not None and t.new_state == "PAUSED_HUMAN"
