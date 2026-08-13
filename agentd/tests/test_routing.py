"""§9.1 routing + provenance (M3-2 order, M3-5 session bindings)."""

from __future__ import annotations

from agentd.github_write import format_escalation_comment
from agentd.routing import (
    RouteAction,
    gateway_footer,
    parse_escalation_marker,
    parse_gateway_marker,
    parse_provenance,
    provenance_footer,
    route_for_recipient,
)


def test_self_echo_dropped() -> None:
    d = route_for_recipient(
        sender="huozheclaude",
        recipient_login="huozheclaude",
        recipient_role="architect",
        other_bot_login="huozhegrok",
        owner="huozhe",
        body="hi",
        session_paused=False,
    )
    assert d.action == RouteAction.DROP
    assert "self-echo" in d.reason


def test_peer_bot_routed() -> None:
    d = route_for_recipient(
        sender="huozheclaude",
        recipient_login="huozhegrok",
        recipient_role="developer",
        other_bot_login="huozheclaude",
        owner="huozhe",
        body="review please",
        session_paused=False,
    )
    assert d.action == RouteAction.ROUTE
    assert d.reset_consec is False


def test_owner_resets_consec() -> None:
    d = route_for_recipient(
        sender="huozhe",
        recipient_login="huozheclaude",
        recipient_role="architect",
        other_bot_login="huozhegrok",
        owner="huozhe",
        body="continue",
        session_paused=False,
    )
    assert d.action == RouteAction.ROUTE
    assert d.reset_consec is True


def test_owner_quote_reply_with_agent_footer_routes() -> None:
    """M3-2: GitHub quote-reply copies provenance; owner must still unpause."""
    footer = provenance_footer(
        session_key="o/r#1", role="architect", turn_id="t-1"
    )
    body = f"> earlier agent text\n{footer}\n\nPlease continue."
    d = route_for_recipient(
        sender="huozhe",
        recipient_login="huozheclaude",
        recipient_role="architect",
        other_bot_login="huozhegrok",
        owner="huozhe",
        body=body,
        session_paused=True,
    )
    assert d.action == RouteAction.ROUTE
    assert d.reset_consec is True
    assert d.reason == "owner"


def test_paused_defers_non_owner() -> None:
    d = route_for_recipient(
        sender="huozhegrok",
        recipient_login="huozheclaude",
        recipient_role="architect",
        other_bot_login="huozhegrok",
        owner="huozhe",
        body="x",
        session_paused=True,
    )
    assert d.action == RouteAction.DEFER


def test_own_artifact_dropped_for_matching_role() -> None:
    footer = provenance_footer(session_key="o/r#1", role="architect", turn_id="t-1")
    d = route_for_recipient(
        sender="someone",
        recipient_login="huozheclaude",
        recipient_role="architect",
        other_bot_login="huozhegrok",
        owner="huozhe",
        body=f"echo\n{footer}",
        session_paused=False,
        bot_logins={"huozheclaude", "huozhegrok", "someone"},
    )
    assert d.action == RouteAction.DROP
    assert "provenance" in d.reason


def test_provenance_uses_session_role_not_global_config() -> None:
    """M3-5: grok is architect this session; footer role=architect drops for grok."""
    footer = provenance_footer(session_key="o/r#1", role="architect", turn_id="t-1")
    d = route_for_recipient(
        sender="outsider",
        recipient_login="huozhegrok",
        recipient_role="architect",  # session-bound
        other_bot_login="huozheclaude",
        owner="huozhe",
        body=footer,
        session_paused=False,
        role_logins={"architect": "huozhegrok", "developer": "huozheclaude"},
        bot_logins={"huozhegrok", "huozheclaude", "outsider"},
    )
    assert d.action == RouteAction.DROP


def test_provenance_footer_roundtrip() -> None:
    f = provenance_footer(session_key="o/r#1", role="architect", turn_id="t-abc")
    p = parse_provenance(f"some comment\n{f}\n")
    assert p is not None
    assert p["session"] == "o/r#1"
    assert p["role"] == "architect"
    assert p["turn"] == "t-abc"


def test_gateway_escalation_comment_dropped_for_both_roles() -> None:
    """PR #27 B1: agentd:escalation must never become an agent turn."""
    body = format_escalation_comment(
        owner="huozhe",
        session_key="o/r#42",
        state="DESIGN_REVIEW",
        role="system",
        reason="budget",
    )
    assert parse_escalation_marker(body) is not None
    assert parse_provenance(body) is None  # different marker

    for role, login, other in (
        ("architect", "huozheclaude", "huozhegrok"),
        ("developer", "huozhegrok", "huozheclaude"),
    ):
        # Sender is the other bot (B1: agent-PAT post) — not self-echo.
        # Unpaused after owner unpause is the failure mode in the review.
        d = route_for_recipient(
            sender=other,
            recipient_login=login,
            recipient_role=role,
            other_bot_login=other,
            owner="huozhe",
            body=body,
            session_paused=False,
            bot_logins={"huozheclaude", "huozhegrok"},
        )
        assert d.action == RouteAction.DROP, (role, d)
        assert "escalation" in d.reason

        # While still paused — drop immediately, do not park as deferred.
        d2 = route_for_recipient(
            sender=other,
            recipient_login=login,
            recipient_role=role,
            other_bot_login=other,
            owner="huozhe",
            body=body,
            session_paused=True,
            bot_logins={"huozheclaude", "huozhegrok"},
        )
        assert d2.action == RouteAction.DROP
        assert "escalation" in d2.reason


def test_gateway_login_not_human_collaborator() -> None:
    """Without escalation marker, gateway sender still must not route as human."""
    d = route_for_recipient(
        sender="huozhegateway",
        recipient_login="huozheclaude",
        recipient_role="architect",
        other_bot_login="huozhegrok",
        owner="huozhe",
        body="accidental non-marker comment",
        session_paused=False,
        bot_logins={"huozheclaude", "huozhegrok", "huozhegateway"},
    )
    assert d.action == RouteAction.DROP
    assert d.reason in ("unclassified sender", "gateway escalation comment")


def test_gateway_marker_dropped_for_both_roles() -> None:
    """#85: any gateway comment, not only escalations, must not wake a role."""
    body = (
        "**agentd** restored the checkbox.\n"
        + gateway_footer(session_key="o/r#84")
    )
    assert parse_gateway_marker(body) is not None
    assert parse_escalation_marker(body) is None
    for role, login, other in (
        ("architect", "huozheclaude", "huozhegrok"),
        ("developer", "huozhegrok", "huozheclaude"),
    ):
        d = route_for_recipient(
            sender=other,
            recipient_login=login,
            recipient_role=role,
            other_bot_login=other,
            owner="huozhe",
            body=body,
            session_paused=False,
            bot_logins={"huozheclaude", "huozhegrok"},
        )
        assert d.action == RouteAction.DROP, (role, d)
        assert d.reason == "gateway comment"


def test_owner_quote_with_gateway_marker_still_routes() -> None:
    body = (
        "> quoted restore\n"
        + gateway_footer(session_key="o/r#1")
        + "\n\nProceed."
    )
    d = route_for_recipient(
        sender="huozhe",
        recipient_login="huozheclaude",
        recipient_role="architect",
        other_bot_login="huozhegrok",
        owner="huozhe",
        body=body,
        session_paused=True,
    )
    assert d.action == RouteAction.ROUTE
    assert d.reset_consec is True


def test_owner_quote_with_escalation_marker_still_routes() -> None:
    """Owner unpause wins over escalation footer (same rule as turn provenance)."""
    body = format_escalation_comment(
        owner="huozhe",
        session_key="o/r#1",
        state="PLANNING",
        role="system",
        reason="x",
    )
    body = f"> quoted\n{body}\n\nProceed with option A."
    d = route_for_recipient(
        sender="huozhe",
        recipient_login="huozheclaude",
        recipient_role="architect",
        other_bot_login="huozhegrok",
        owner="huozhe",
        body=body,
        session_paused=True,
    )
    assert d.action == RouteAction.ROUTE
    assert d.reset_consec is True
