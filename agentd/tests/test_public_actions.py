"""#39: public_actions from tool streams + silent-turn stall."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentd.config import Config
from agentd.db import SCHEMA_VERSION, Store
from agentd.design_loop import DesignLoop
from agentd.loop_safety import SilentTurnTracker

_RUNNER_ROOT = Path(__file__).resolve().parents[1] / "docker" / "session-runner"
sys.path.insert(0, str(_RUNNER_ROOT))

from agentd_runner import cli_session  # noqa: E402
from agentd_runner.public_actions import (  # noqa: E402
    classify_shell_command,
    dedupe_actions,
    from_claude_stream_obj,
    from_grok_session_update,
)


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    cli_session.shutdown_all()
    yield
    cli_session.shutdown_all()


def test_classify_gh_pr_create() -> None:
    act = classify_shell_command("gh pr create --title 'x' --body 'y'")
    assert act is not None
    assert act["kind"] == "pr_opened"


def test_classify_local_git_not_public() -> None:
    assert classify_shell_command("git status") is None
    assert classify_shell_command("ls -la") is None


def test_claude_stream_tool_use() -> None:
    obj = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu1",
                    "name": "Bash",
                    "input": {"command": "gh issue comment 1 --body hi"},
                }
            ]
        },
    }
    acts = from_claude_stream_obj(obj)
    assert len(acts) == 1
    assert acts[0]["kind"] == "comment"


def test_grok_tool_call_update() -> None:
    update = {
        "sessionUpdate": "tool_call_update",
        "status": "completed",
        "title": "Bash",
        "toolCallId": "tc1",
        "rawInput": {"command": "gh pr review 9 --approve"},
    }
    acts = from_grok_session_update(update)
    assert len(acts) == 1
    assert acts[0]["kind"] == "review"


def test_claude_live_turn_reports_public_actions(tmp_path: Path) -> None:
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "session_id": "s1",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Bash",
                            "input": {
                                "command": "gh pr create --title demo --body b"
                            },
                        }
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "session_id": "s1",
                "message": {"content": [{"type": "text", "text": "opened pr"}]},
            }
        ),
        json.dumps(
            {
                "type": "result",
                "session_id": "s1",
                "result": "opened pr",
                "is_error": False,
            }
        ),
    ]
    import queue as qmod

    sess = cli_session.LiveCliSession(
        role="architect",
        adapter="claude-code",
        uid=1001,
        home=tmp_path / "h",
        tmp=tmp_path / "t",
        xdg=tmp_path / "x",
        spawn_cwd=tmp_path,
    )
    q: qmod.Queue[str | None] = qmod.Queue()
    for line in lines:
        q.put(line)
    sess._stdout_q = q
    sess.proc = MagicMock()
    sess.proc.poll.return_value = None
    sess.proc.pid = 1
    sess.proc.stdin = MagicMock()
    with patch.object(cli_session.LiveCliSession, "_sample_rss"):
        result = sess.turn("do it", deadline_s=5)
    assert result["status"] == "done"
    assert result["public_actions"]
    assert result["public_actions"][0]["kind"] == "pr_opened"


def test_claude_silent_turn_empty_actions(tmp_path: Path) -> None:
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "session_id": "s1",
                "message": {
                    "content": [{"type": "text", "text": "nothing to do"}]
                },
            }
        ),
        json.dumps(
            {
                "type": "result",
                "session_id": "s1",
                "result": "nothing to do",
                "is_error": False,
            }
        ),
    ]
    import queue as qmod

    sess = cli_session.LiveCliSession(
        role="architect",
        adapter="claude-code",
        uid=1001,
        home=tmp_path / "h",
        tmp=tmp_path / "t",
        xdg=tmp_path / "x",
        spawn_cwd=tmp_path,
    )
    q: qmod.Queue[str | None] = qmod.Queue()
    for line in lines:
        q.put(line)
    sess._stdout_q = q
    sess.proc = MagicMock()
    sess.proc.poll.return_value = None
    sess.proc.pid = 1
    sess.proc.stdin = MagicMock()
    with patch.object(cli_session.LiveCliSession, "_sample_rss"):
        result = sess.turn("check", deadline_s=5)
    assert result["status"] == "done"
    assert result["public_actions"] == []


def test_silent_tracker_escalates_after_n() -> None:
    t = SilentTurnTracker(threshold=3)
    assert t.after_turn(public_actions=[], state_changed=False, status="done") is None
    assert t.silent_count == 1
    assert t.after_turn(public_actions=[], state_changed=False, status="done") is None
    assert t.silent_count == 2
    breach = t.after_turn(public_actions=[], state_changed=False, status="done")
    assert breach is not None
    assert "silent" in breach


def test_silent_tracker_resets_on_action() -> None:
    t = SilentTurnTracker(threshold=3)
    t.after_turn(public_actions=[], state_changed=False, status="done")
    t.after_turn(public_actions=[], state_changed=False, status="done")
    t.after_turn(
        public_actions=[{"kind": "comment"}], state_changed=False, status="done"
    )
    assert t.silent_count == 0


def test_silent_tracker_resets_on_state_change() -> None:
    t = SilentTurnTracker(threshold=3)
    t.after_turn(public_actions=[], state_changed=False, status="done")
    t.after_turn(public_actions=[], state_changed=True, status="done")
    assert t.silent_count == 0


def test_schema_v6_public_actions_and_silent(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    assert store._schema_version() == SCHEMA_VERSION
    store.upsert_session(
        session_key="o/r#1",
        repo="o/r",
        issue_num=1,
        state="PLANNING",
        architect="a",
        developer="d",
        created_at=1,
        updated_at=1,
    )
    store.insert_turn(
        turn_id="t1",
        session_key="o/r#1",
        role="architect",
        delivery_id="d1",
        started_at=1,
        ended_at=None,
        status=None,
        summary=None,
    )
    store.finish_turn(
        "t1",
        ended_at=2,
        status="done",
        summary="ok",
        public_actions=json.dumps([{"kind": "comment"}]),
    )
    row = store._conn.execute(
        "SELECT public_actions FROM turns WHERE turn_id=?", ("t1",)
    ).fetchone()
    assert "comment" in (row["public_actions"] or "")
    store.update_session_fields("o/r#1", silent_turns=2)
    sess = store.get_session("o/r#1")
    assert sess is not None
    assert int(sess["silent_turns"]) == 2
    store.close()


def test_design_loop_escalates_after_silent_run(tmp_path: Path) -> None:
    """N consecutive silent turns with no FSM move → §8.5."""
    store = Store(tmp_path / "state.db")
    cfg = Config(
        raw={
            "host": {"owner": "huozhe"},
            "budgets": {"silent_turn_limit": 3},
            "agents": {
                "claude": {"login": "huozheclaude"},
                "grok": {"login": "huozhegrok"},
            },
            "gateway": {"login": "huozhegateway"},
        },
        root=tmp_path,
    )
    sk = "o/r#7"
    store.upsert_session(
        session_key=sk,
        project_key="o/r",
        repo="o/r",
        issue_num=7,
        state="PLANNING",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
    )
    store.update_session_fields(sk, silent_turns=2)  # already 2 silent
    store.upsert_runner(
        "o/r",
        container_id="c1",
        endpoint="127.0.0.1:9",
        token="tok",
        tier="hot",
    )
    posts: list[str] = []

    class FakeSup:
        pass

    import agentd.design_loop as dl

    class FakeClient:
        def __init__(self, *a, **k):  # noqa: ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):  # noqa: ANN002
            return False

        def call(self, method, params=None):  # noqa: ANN001
            return {
                "status": "done",
                "summary": "nothing needed",
                "public_actions": [],
            }

    orig = dl.RunnerClient
    dl.RunnerClient = FakeClient  # type: ignore[misc, assignment]
    try:
        loop = DesignLoop(
            store,
            cfg,
            supervisor=FakeSup(),  # type: ignore[arg-type]
            dispatch_turns=True,
            post_comment=lambda **k: posts.append(k["body"]) or 1,
            gateway_token="gw",
        )
        body = json.dumps(
            {
                "action": "created",
                "issue": {"number": 7},
                "comment": {
                    "body": "ping <!-- agentd:turn session=o/r#7 role=developer turn=t-x -->",
                    "user": {"login": "huozhegrok"},
                },
                "repository": {"full_name": "o/r"},
                "sender": {"login": "huozhegrok"},
            }
        ).encode()
        store.insert_delivery(
            delivery_id="d-silent",
            event="issue_comment",
            action="created",
            repo="o/r",
            issue_num=7,
            sender="huozhegrok",
            payload=body,
            status="deferred",
        )
        loop.process_deferred_batch()
    finally:
        dl.RunnerClient = orig  # type: ignore[misc]

    sess = store.get_session(sk)
    assert sess is not None
    # Third silent → escalate
    assert sess["state"] == "PAUSED_HUMAN"
    assert len(posts) == 1
    assert "silent" in posts[0].lower()
    turn = store._conn.execute(
        "SELECT public_actions, status FROM turns ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert turn is not None
    assert turn["status"] == "done"
    assert turn["public_actions"] in ("[]", "null", None) or turn[
        "public_actions"
    ] == "[]"
    store.close()


def test_dedupe() -> None:
    a = [
        {"kind": "comment", "tool_use_id": "1", "command": "gh issue comment 1"},
        {"kind": "comment", "tool_use_id": "1", "command": "gh issue comment 1"},
    ]
    assert len(dedupe_actions(a)) == 1
