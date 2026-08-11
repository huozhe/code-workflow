"""Turn budgets and stall detection (§9.2, §9.3)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass
class BudgetState:
    turn_count: int = 0
    consec_agent_turns: int = 0
    review_rounds: int = 0
    max_turns: int = 40
    max_consec_agent: int = 12
    max_review_rounds: int = 8

    def after_agent_turn(self) -> None:
        self.turn_count += 1
        self.consec_agent_turns += 1

    def after_owner(self) -> None:
        self.consec_agent_turns = 0

    def breach(self) -> str | None:
        if self.turn_count >= self.max_turns:
            return f"turn budget exhausted ({self.turn_count}>={self.max_turns})"
        if self.consec_agent_turns >= self.max_consec_agent:
            return (
                f"consec agent turns exhausted "
                f"({self.consec_agent_turns}>={self.max_consec_agent})"
            )
        if self.review_rounds >= self.max_review_rounds:
            return f"review rounds exhausted ({self.review_rounds}>={self.max_review_rounds})"
        return None


@dataclass
class SilentTurnTracker:
    """§9.3 third signal (#39): consecutive agent turns with no public action.

    One quiet turn is legitimate. A *run* of silent turns with no FSM movement
    means the loop has died without a failure or a stall fingerprint.
    """

    silent_count: int = 0
    threshold: int = 3

    def after_turn(
        self,
        *,
        public_actions: list | None,
        state_changed: bool,
        status: str | None,
    ) -> str | None:
        # Only completed agent work counts; failures/timeouts are separate paths.
        if status not in (None, "done", "changes_requested"):
            return None
        acted = bool(public_actions)
        if acted or state_changed:
            self.silent_count = 0
            return None
        self.silent_count += 1
        if self.silent_count >= self.threshold:
            return (
                f"stall: {self.silent_count} consecutive silent turns "
                f"(no public_actions, no state change)"
            )
        return None


def progress_fingerprint(
    *,
    open_thread_ids: list[str],
    unresolved_count: int,
    diff_stat: str,
) -> str:
    """§9.3 fingerprint of *progress state*, not including head_sha.

    head_sha is tracked separately so the stall counter can require at least one
    real head move before arming, without making identical progress after a
    cosmetic push unobservable (M3-1 / §9.3 amendment).
    """
    material = (
        ",".join(sorted(open_thread_ids))
        + "|"
        + str(unresolved_count)
        + "|"
        + hashlib.sha256(diff_stat.encode()).hexdigest()
    )
    return hashlib.sha256(material.encode()).hexdigest()


@dataclass
class StallTracker:
    """Both stall signals (§9.3)."""

    last_fp: str | None = None
    fp_repeat: int = 0
    last_head: str | None = None
    seen_head_change: bool = False
    zero_thread_rounds: int = 0
    fp_threshold: int = 3
    zero_thread_threshold: int = 3

    def observe_fingerprint(self, fp: str, head_sha: str | None) -> str | None:
        """Count identical progress fingerprints across consecutive turns.

        Arm only after the session has seen at least one non-trivial head_sha
        change (slow initial convergence does not fire). Once armed, count
        identical fingerprints every turn — including after further head moves
        that leave review/diff state frozen (cosmetic pushes).
        """
        if head_sha is None:
            # No head yet: store fp only; zero-thread + budgets cover comment loops
            if self.last_fp is None:
                self.last_fp = fp
            return None

        if self.last_head is None:
            self.last_head = head_sha
            self.last_fp = fp
            self.fp_repeat = 0
            return None

        if head_sha != self.last_head:
            self.seen_head_change = True
            self.last_head = head_sha

        if not self.seen_head_change:
            # Not armed yet — remember latest fp but do not escalate
            self.last_fp = fp
            self.fp_repeat = 0
            return None

        # Armed: count consecutive identical progress fingerprints
        if fp == self.last_fp:
            self.fp_repeat += 1
        else:
            self.fp_repeat = 1
            self.last_fp = fp

        if self.fp_repeat >= self.fp_threshold:
            return (
                f"stall: progress fingerprint identical {self.fp_repeat} "
                f"consecutive turns after a head change"
            )
        return None

    def observe_review_round(self, threads_resolved: int) -> str | None:
        if threads_resolved <= 0:
            self.zero_thread_rounds += 1
        else:
            self.zero_thread_rounds = 0
        if self.zero_thread_rounds >= self.zero_thread_threshold:
            return (
                f"stall: zero thread progress for {self.zero_thread_rounds} review rounds"
            )
        return None
