"""Session FSM design + code half (§8.1)."""

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


def test_code_happy_path() -> None:
    assert transition("IMPLEMENTING", "feature_pr_opened").new_state == "CODE_REVIEW"
    assert (
        transition("CODE_REVIEW", "code_changes_requested").new_state == "CODE_REWORK"
    )
    assert transition("CODE_REWORK", "feature_revised").new_state == "CODE_REVIEW"
    assert transition("CODE_REVIEW", "merge_authorized").new_state == "MERGING"
    assert (
        transition("MERGING", "feature_merged").new_state == "AWAITING_VERIFICATION"
    )


def test_awaiting_verification_reenterable() -> None:
    t = transition("AWAITING_VERIFICATION", "feature_pr_opened")
    assert t is not None and t.new_state == "IMPLEMENTING"


def test_escalation_pause() -> None:
    t = transition("DESIGN_REVIEW", "escalation")
    assert t is not None and t.new_state == "PAUSED_HUMAN"
    t2 = transition("CODE_REVIEW", "escalation")
    assert t2 is not None and t2.new_state == "PAUSED_HUMAN"


def test_owner_reply_from_pause_fallback() -> None:
    t = transition("PAUSED_HUMAN", "owner_reply")
    assert t is not None and t.new_state == "PLANNING"
