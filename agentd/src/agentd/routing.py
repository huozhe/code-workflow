"""Event routing per recipient (§9.1) + provenance footer (§9.1)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

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
    recipient_role: str,
    other_bot_login: str | None,
    owner: str,
    body: str | None,
    session_paused: bool,
    # Session-resolved bindings (not global config) — §5.3 / M3-5
    role_logins: dict[str, str] | None = None,
    bot_logins: set[str] | None = None,
) -> RouteDecision:
    """Evaluate §9.1 table for one recipient identity.

    Order (matches §9.1 and M3-2): self-echo → other-bot → owner →
    own-artifact provenance → paused non-owner → other human.
    Owner must win over provenance so quote-replies unpause PAUSED_HUMAN.
    """
    sender_s = (sender or "").lower()
    recipient = recipient_login.lower()
    owner_s = owner.lower()
    other = (other_bot_login or "").lower()
    roles = {k: v.lower() for k, v in (role_logins or {}).items()}
    bots = {b.lower() for b in (bot_logins or set())} | {
        recipient,
        other,
    } - {""}

    # 1. Self-echo
    if sender_s == recipient:
        return RouteDecision(RouteAction.DROP, "self-echo")

    # 2. Owner before provenance (M3-2: quote-reply carries agent footer)
    #    and before paused (owner is the only unpause path).
    if sender_s == owner_s:
        return RouteDecision(RouteAction.ROUTE, "owner", reset_consec=True)

    # 3. Paused: any non-owner defers (§9.1) — including peer bots.
    if session_paused:
        return RouteDecision(RouteAction.DEFER, "session paused; non-owner")

    # 4. Other bot (peer)
    if other and sender_s == other:
        return RouteDecision(RouteAction.ROUTE, "peer-bot")

    # 5. Own-artifact provenance (recipient role produced this body)
    prov = parse_provenance(body)
    if prov and prov.get("role") == recipient_role:
        return RouteDecision(RouteAction.DROP, "own-artifact provenance")

    # 6. Other human / collaborator
    if sender_s and sender_s not in bots:
        return RouteDecision(RouteAction.ROUTE, "human-or-other")

    return RouteDecision(RouteAction.DROP, "unclassified sender")
