"""§9.1 routing + provenance (M3-2 order, M3-5 session bindings)."""

from __future__ import annotations

from agentd.routing import (
    RouteAction,
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
