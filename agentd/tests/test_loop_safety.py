"""§9.2 budgets + §9.3 stall signals."""

from __future__ import annotations

from agentd.loop_safety import BudgetState, StallTracker, progress_fingerprint


def test_budget_consec_breach() -> None:
    b = BudgetState(max_consec_agent=3)
    b.after_agent_turn()
    b.after_agent_turn()
    assert b.breach() is None
    b.after_agent_turn()
    assert b.breach() is not None and "consec" in b.breach()


def test_owner_resets_consec() -> None:
    b = BudgetState(max_consec_agent=2)
    b.after_agent_turn()
    b.after_agent_turn()
    assert b.breach() is not None
    b.after_owner()
    assert b.consec_agent_turns == 0
    assert b.breach() is None


def test_fingerprint_counts_only_after_head_change() -> None:
    """§9.3: identical fp across head changes escalates; static head does not."""
    st = StallTracker(fp_threshold=3)
    fp = progress_fingerprint(
        head_sha="aaa", open_thread_ids=["1"], unresolved_count=2, diff_stat="a"
    )
    assert st.observe_fingerprint(fp, "aaa") is None  # first store
    # same head — must NOT advance fingerprint stall counter
    assert st.observe_fingerprint(fp, "aaa") is None
    assert st.observe_fingerprint(fp, "aaa") is None
    # three head changes with the same fingerprint
    assert st.observe_fingerprint(fp, "bbb") is None  # count=1
    assert st.observe_fingerprint(fp, "ccc") is None  # count=2
    reason = st.observe_fingerprint(fp, "ddd")  # count=3
    assert reason is not None and "fingerprint" in reason


def test_zero_thread_progress_escalates() -> None:
    st = StallTracker(zero_thread_threshold=3)
    assert st.observe_review_round(0) is None
    assert st.observe_review_round(0) is None
    reason = st.observe_review_round(0)
    assert reason is not None and "zero thread" in reason
    # progress resets
    assert st.observe_review_round(1) is None
    assert st.zero_thread_rounds == 0
