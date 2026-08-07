"""Event routing per recipient (§9.1) + provenance footer (§9.1)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from agentd.config import Config

PROVENANCE_RE = re.compile(
    r"<!--\s*agentd:turn\s+session=(?P<session>[^\s]+)\s+role=(?P<role>\w+)\s+turn=(?P<turn>[^\s]+)\s*-->"
)


class RouteAction(str, Enum):
    DROP = "drop"
    ROUTE = "route"
    DEFER = "defer"


@dataclass(frozen=True)
class RouteDecision:
    action: RouteAction
    reason: str
    reset_consec: bool = False  # owner activity resets consec_agent_turns


def provenance_footer(*, session_key: str, role: str, turn_id: str) -> str:
    return f"<!-- agentd:turn session={session_key} role={role} turn={turn_id} -->"


def parse_provenance(body: str | None) -> dict[str, str] | None:
    if not body:
        return None
    m = PROVENANCE_RE.search(body)
    if not m:
        return None
    return m.groupdict()


def route_for_recipient(
    *,
    sender: str | None,
    recipient_login: str,
    other_bot_login: str | None,
    owner: str,
    body: str | None,
    session_paused: bool,
    config: Config,
) -> RouteDecision:
    """Evaluate §9.1 table for one recipient identity."""
    sender_s = (sender or "").lower()
    recipient = recipient_login.lower()
    owner_s = owner.lower()
    other = (other_bot_login or "").lower()

    if sender_s == recipient:
        return RouteDecision(RouteAction.DROP, "self-echo")

    prov = parse_provenance(body)
    if prov and prov.get("role") and recipient in (
        (config.agent_login("claude") or "").lower(),
        (config.agent_login("grok") or "").lower(),
    ):
        # Drop if footer says this recipient produced the artifact
        role = prov["role"]
        if role == "architect" and recipient == (config.agent_login("claude") or "").lower():
            return RouteDecision(RouteAction.DROP, "own-artifact provenance")
        if role == "developer" and recipient == (config.agent_login("grok") or "").lower():
            return RouteDecision(RouteAction.DROP, "own-artifact provenance")

    if session_paused and sender_s != owner_s:
        return RouteDecision(RouteAction.DEFER, "session paused; non-owner")

    if sender_s == owner_s:
        return RouteDecision(RouteAction.ROUTE, "owner", reset_consec=True)

    if other and sender_s == other:
        return RouteDecision(RouteAction.ROUTE, "peer-bot")

    # Collaborator / other human
    if sender_s and sender_s not in {
        (config.agent_login("claude") or "").lower(),
        (config.agent_login("grok") or "").lower(),
    }:
        return RouteDecision(RouteAction.ROUTE, "human-or-other")

    return RouteDecision(RouteAction.DROP, "unclassified sender")
