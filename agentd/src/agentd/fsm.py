"""Design-half session FSM (§8.1). Gateway drives from GitHub events (P1)."""

from __future__ import annotations

from dataclasses import dataclass


# Design-half states only (M3); code loop is M4.
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
        # M4 starts at IMPLEMENTING + feature_pr_opened
    }
    return table.get((s, k))
