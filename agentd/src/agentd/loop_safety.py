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


def progress_fingerprint(
    *,
    head_sha: str | None,
    open_thread_ids: list[str],
    unresolved_count: int,
    diff_stat: str,
) -> str:
    """§9.3 fingerprint composition."""
    material = (
        (head_sha or "")
        + "|"
        + ",".join(sorted(open_thread_ids))
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
    zero_thread_rounds: int = 0
    fp_threshold: int = 3
    zero_thread_threshold: int = 3

    def observe_fingerprint(self, fp: str, head_sha: str | None) -> str | None:
        """Return escalate reason or None.

        Fingerprint is sampled on each non-trivial head_sha change (§9.3).
        Identical fingerprint across ``fp_threshold`` head changes ⇒ escalate.
        Static head does not advance the counter (zero-thread + budgets cover that).
        """
        if not head_sha:
            return None
        if self.last_head is None:
            self.last_head = head_sha
            self.last_fp = fp
            self.fp_repeat = 0
            return None
        if head_sha == self.last_head:
            # no head change — do not count (not a "fix": other signals handle this)
            return None
        # head moved
        self.last_head = head_sha
        if fp == self.last_fp:
            self.fp_repeat += 1
        else:
            self.fp_repeat = 1
            self.last_fp = fp
        if self.fp_repeat >= self.fp_threshold:
            return (
                f"stall: fingerprint repeated {self.fp_repeat} times across head changes"
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
