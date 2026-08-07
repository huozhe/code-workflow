"""§9.1 routing + provenance."""

from __future__ import annotations

from agentd.config import Config
from agentd.routing import (
    RouteAction,
    parse_provenance,
    provenance_footer,
    route_for_recipient,
)


def _cfg() -> Config:
    return Config(
        raw={
            "host": {"owner": "huozhe"},
            "agents": {
                "claude": {"login": "huozheclaude"},
                "grok": {"login": "huozhegrok"},
            },
        }
    )


def test_self_echo_dropped() -> None:
    d = route_for_recipient(
        sender="huozheclaude",
        recipient_login="huozheclaude",
        other_bot_login="huozhegrok",
        owner="huozhe",
        body="hi",
        session_paused=False,
        config=_cfg(),
    )
    assert d.action == RouteAction.DROP
    assert "self-echo" in d.reason


def test_peer_bot_routed() -> None:
    d = route_for_recipient(
        sender="huozheclaude",
        recipient_login="huozhegrok",
        other_bot_login="huozheclaude",
        owner="huozhe",
        body="review please",
        session_paused=False,
        config=_cfg(),
    )
    assert d.action == RouteAction.ROUTE
    assert d.reset_consec is False


def test_owner_resets_consec() -> None:
    d = route_for_recipient(
        sender="huozhe",
        recipient_login="huozheclaude",
        other_bot_login="huozhegrok",
        owner="huozhe",
        body="continue",
        session_paused=False,
        config=_cfg(),
    )
    assert d.action == RouteAction.ROUTE
    assert d.reset_consec is True


def test_paused_defers_non_owner() -> None:
    d = route_for_recipient(
        sender="huozhegrok",
        recipient_login="huozheclaude",
        other_bot_login="huozhegrok",
        owner="huozhe",
        body="x",
        session_paused=True,
        config=_cfg(),
    )
    assert d.action == RouteAction.DEFER


def test_provenance_footer_roundtrip() -> None:
    f = provenance_footer(session_key="o/r#1", role="architect", turn_id="t-abc")
    p = parse_provenance(f"some comment\n{f}\n")
    assert p is not None
    assert p["session"] == "o/r#1"
    assert p["role"] == "architect"
    assert p["turn"] == "t-abc"
