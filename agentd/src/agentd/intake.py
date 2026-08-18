"""Intake gate §4.3 — label + collaborators before any session work."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from agentd.config import Config

log = logging.getLogger("agentd.intake")

# GitHub author_association values that count as "collaborators" for actors gate.
_COLLAB_ASSOC = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


@dataclass(frozen=True)
class IntakeResult:
    accepted: bool
    reason: str


def evaluate_intake(
    *,
    event: str,
    action: str | None,
    payload: bytes,
    config: Config,
) -> IntakeResult | None:
    """Return an intake decision for events that form sessions; None = not applicable.

    M1 only evaluates ``issues`` open/reopen/labeled. Other events leave the
    dispatcher to mark them without intake (session FSM is M2+).
    """
    if event != "issues":
        return None
    if action not in ("opened", "reopened", "labeled"):
        return None

    try:
        data: dict[str, Any] = json.loads(payload)
    except json.JSONDecodeError:
        return IntakeResult(False, "unparseable payload")

    issue = data.get("issue") if isinstance(data.get("issue"), dict) else {}
    assoc = str(issue.get("author_association") or data.get("author_association") or "")

    if (
        config.intake_actors == "collaborators"
        and assoc.upper() not in _COLLAB_ASSOC
    ):
        return IntakeResult(
            False,
            f"actors:collaborators rejected author_association={assoc or 'missing'}",
        )

    if config.intake_mode == "label":
        want = config.intake_label
        names = _label_names(issue, data, action)
        if want not in names:
            return IntakeResult(
                False,
                f"mode:label requires label {want!r}; have {sorted(names)}",
            )

    return IntakeResult(True, "intake pass")


def _label_names(issue: dict[str, Any], data: dict[str, Any], action: str) -> set[str]:
    names: set[str] = set()
    for lab in issue.get("labels") or []:
        if isinstance(lab, dict) and lab.get("name"):
            names.add(str(lab["name"]))
        elif isinstance(lab, str):
            names.add(lab)
    # issues.labeled carries the new label at top level
    if action == "labeled":
        lab = data.get("label")
        if isinstance(lab, dict) and lab.get("name"):
            names.add(str(lab["name"]))
        elif isinstance(lab, str):
            names.add(lab)
    return names
