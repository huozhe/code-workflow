"""Intake gate §4.3."""

from __future__ import annotations

import json

from agentd.config import Config
from agentd.intake import evaluate_intake


def _cfg(**intake) -> Config:
    return Config(raw={"intake": intake or {}})


def test_issues_opened_without_label_dropped() -> None:
    body = json.dumps(
        {
            "action": "opened",
            "issue": {
                "number": 1,
                "author_association": "OWNER",
                "labels": [],
            },
        }
    ).encode()
    r = evaluate_intake(event="issues", action="opened", payload=body, config=_cfg())
    assert r is not None and r.accepted is False
    assert "label" in r.reason


def test_issues_opened_with_label_and_collab_accepted() -> None:
    body = json.dumps(
        {
            "action": "opened",
            "issue": {
                "number": 1,
                "author_association": "COLLABORATOR",
                "labels": [{"name": "agentd"}],
            },
        }
    ).encode()
    r = evaluate_intake(event="issues", action="opened", payload=body, config=_cfg())
    assert r is not None and r.accepted is True


def test_actors_rejects_outsider() -> None:
    body = json.dumps(
        {
            "action": "opened",
            "issue": {
                "number": 1,
                "author_association": "NONE",
                "labels": [{"name": "agentd"}],
            },
        }
    ).encode()
    r = evaluate_intake(event="issues", action="opened", payload=body, config=_cfg())
    assert r is not None and r.accepted is False
    assert "collaborators" in r.reason


def test_labeled_event_accepts_new_label() -> None:
    body = json.dumps(
        {
            "action": "labeled",
            "label": {"name": "agentd"},
            "issue": {
                "number": 2,
                "author_association": "MEMBER",
                "labels": [{"name": "agentd"}],
            },
        }
    ).encode()
    r = evaluate_intake(event="issues", action="labeled", payload=body, config=_cfg())
    assert r is not None and r.accepted is True


def test_mode_all_skips_label_requirement() -> None:
    body = json.dumps(
        {
            "action": "opened",
            "issue": {
                "number": 3,
                "author_association": "OWNER",
                "labels": [],
            },
        }
    ).encode()
    r = evaluate_intake(
        event="issues",
        action="opened",
        payload=body,
        config=_cfg(mode="all"),
    )
    assert r is not None and r.accepted is True


def test_non_issues_not_applicable() -> None:
    r = evaluate_intake(
        event="issue_comment",
        action="created",
        payload=b"{}",
        config=_cfg(),
    )
    assert r is None
