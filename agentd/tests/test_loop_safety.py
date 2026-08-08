"""§9.2 budgets + §9.3 stall signals (M3-1 amended fingerprint)."""

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


def test_fingerprint_fires_on_cosmetic_pushes_after_arm() -> None:
    """Real path: head moves, progress state (threads/diff) frozen → escalate.

    progress_fingerprint does NOT include head_sha, so identical progress after
    a cosmetic push is observable (M3-1).
    """
    st = StallTracker(fp_threshold=3)
    # Frozen review/diff state — only head changes each round
    frozen = dict(
        open_thread_ids=["t1", "t2"],
        unresolved_count=2,
        diff_stat="1 file changed, 1 insertion(+)",
    )
    # Initial head — not armed yet
    fp0 = progress_fingerprint(**frozen)
    assert st.observe_fingerprint(fp0, "sha0") is None
    assert st.seen_head_change is False
    # First head change arms the counter
    assert st.observe_fingerprint(progress_fingerprint(**frozen), "sha1") is None
    assert st.seen_head_change is True
    assert st.fp_repeat == 1
    # Further cosmetic pushes with same progress fingerprint
    assert st.observe_fingerprint(progress_fingerprint(**frozen), "sha2") is None
    assert st.fp_repeat == 2
    reason = st.observe_fingerprint(progress_fingerprint(**frozen), "sha3")
    assert reason is not None and "fingerprint" in reason
    assert st.fp_repeat == 3


def test_fingerprint_not_armed_before_any_head_change() -> None:
    st = StallTracker(fp_threshold=3)
    fp = progress_fingerprint(
        open_thread_ids=[], unresolved_count=0, diff_stat="empty"
    )
    # Same head forever — never arm, never escalate via fingerprint
    for _ in range(5):
        assert st.observe_fingerprint(fp, "sha-static") is None
    assert st.seen_head_change is False


def test_fingerprint_resets_when_progress_moves() -> None:
    st = StallTracker(fp_threshold=3)
    a = progress_fingerprint(
        open_thread_ids=["1"], unresolved_count=1, diff_stat="a"
    )
    b = progress_fingerprint(
        open_thread_ids=["1", "2"], unresolved_count=0, diff_stat="b"
    )
    st.observe_fingerprint(a, "h0")
    st.observe_fingerprint(a, "h1")  # arm, repeat=1
    st.observe_fingerprint(a, "h2")  # repeat=2
    st.observe_fingerprint(b, "h3")  # progress changed → repeat=1
    assert st.fp_repeat == 1
    assert st.last_fp == b


def test_zero_thread_progress_escalates() -> None:
    st = StallTracker(zero_thread_threshold=3)
    assert st.observe_review_round(0) is None
    assert st.observe_review_round(0) is None
    reason = st.observe_review_round(0)
    assert reason is not None and "zero thread" in reason
    assert st.observe_review_round(1) is None
    assert st.zero_thread_rounds == 0


def test_zero_thread_fires_while_fingerprint_disarmed() -> None:
    """M3-B: layered signals — static head never arms fingerprint; zero-thread still fires.

    Do not re-introduce head_sha into the fingerprint hash (§9.3 standing warning).
    """
    st = StallTracker(fp_threshold=3, zero_thread_threshold=3)
    fp = progress_fingerprint(
        open_thread_ids=["t1"],
        unresolved_count=1,
        diff_stat="frozen",
    )
    # Same head forever → fingerprint stays disarmed
    for _ in range(5):
        assert st.observe_fingerprint(fp, "sha-static") is None
    assert st.seen_head_change is False
    assert st.fp_repeat == 0

    assert st.observe_review_round(0) is None
    assert st.observe_review_round(0) is None
    reason = st.observe_review_round(0)
    assert reason is not None
    assert "zero thread" in reason
    assert "fingerprint" not in reason
    # Still disarmed after zero-thread fired
    assert st.seen_head_change is False
    assert st.observe_fingerprint(fp, "sha-static") is None
