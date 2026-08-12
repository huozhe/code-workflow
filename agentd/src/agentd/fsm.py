"""Session FSM (§8.1). Gateway drives from GitHub events (P1)."""

from __future__ import annotations

from dataclasses import dataclass


# Design half (M3) + code half (M4-1). TEARDOWN/CLOSED are M5.
DESIGN_STATES = frozenset(
    {
        "INTAKE",
        "PLANNING",
        "DESIGN_REVIEW",
        "DESIGN_REWORK",
        "DESIGN_APPROVED",
        "IMPLEMENTING",
        "PAUSED_HUMAN",
        "FAILED",
    }
)

CODE_STATES = frozenset(
    {
        "CODE_REVIEW",
        "CODE_REWORK",
        "MERGING",
        "AWAITING_VERIFICATION",
    }
)

SESSION_STATES = DESIGN_STATES | CODE_STATES


@dataclass(frozen=True)
class Transition:
    new_state: str
    freeze_roles: bool = False
    note: str = ""


def transition(state: str, event_kind: str) -> Transition | None:
    """Map (state, normalized event kind) → next state, or None if no change."""
    s = state or "INTAKE"
    k = event_kind

    if k == "escalation":
        return Transition("PAUSED_HUMAN", note="escalate.human")

    if k == "owner_reply" and s == "PAUSED_HUMAN":
        # design_loop restores sessions.resume_state; PLANNING is fallback only.
        return Transition("PLANNING", note="resume from pause (fallback PLANNING)")

    table: dict[tuple[str, str], Transition] = {
        ("INTAKE", "session_created"): Transition("PLANNING", note="roles resolved"),
        ("PLANNING", "design_pr_opened"): Transition(
            "DESIGN_REVIEW", freeze_roles=True, note="roles freeze at Design PR open"
        ),
        ("DESIGN_REVIEW", "design_changes_requested"): Transition("DESIGN_REWORK"),
        ("DESIGN_REWORK", "design_revised"): Transition("DESIGN_REVIEW"),
        ("DESIGN_REVIEW", "design_approved"): Transition("DESIGN_APPROVED"),
        ("DESIGN_APPROVED", "design_merged"): Transition("IMPLEMENTING"),
        # M4-1 code half — Feature PR identified by developer branch (#31 rule).
        ("IMPLEMENTING", "feature_pr_opened"): Transition(
            "CODE_REVIEW", note="Feature PR open (developer branch)"
        ),
        ("CODE_REVIEW", "code_changes_requested"): Transition("CODE_REWORK"),
        ("CODE_REWORK", "feature_revised"): Transition("CODE_REVIEW"),
        # merge_authorized is emitted after §8.4 verify (M4-2 wires the checks).
        ("CODE_REVIEW", "merge_authorized"): Transition(
            "MERGING", note="gateway authorized Feature PR merge"
        ),
        ("MERGING", "feature_merged"): Transition(
            "AWAITING_VERIFICATION", note="Feature PR merged + branch delete"
        ),
        # Re-enterable (§8.1): further Feature PR under one issue.
        ("AWAITING_VERIFICATION", "feature_pr_opened"): Transition(
            "IMPLEMENTING", note="re-enter for further Feature PR"
        ),
    }
    return table.get((s, k))
