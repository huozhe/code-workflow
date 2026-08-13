"""M5-2 / §10.5 steps 1–2: classify at close, teardown turns, ledger confirm."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from agentd.config import Config
from agentd.db import Store
from agentd.design_loop import DesignLoop
from agentd.refusals import CapacityRefusal
from agentd.fsm import SESSION_STATES, TERMINAL_STATES, transition
from agentd.gitops import ensure_shared_clone, role_branch_name, shared_clone_path
from agentd.verification import (
    classify_at_close,
    render_verification_block,
)


def _cfg(tmp: Path) -> Config:
    return Config(
        raw={
            "host": {"owner": "huozhe"},
            "gateway": {"login": "huozhegateway"},
            "agents": {
                "claude": {"login": "huozheclaude"},
                "grok": {"login": "huozhegrok"},
            },
        },
        root=tmp,
    )


def _block(*, checked: bool) -> str:
    return render_verification_block(
        steps=["pull", "test"],
        merged_prs=[59, 60],
        checked=checked,
    )


def _closed_payload(
    *,
    repo: str = "huozhe/code-workflow",
    issue_num: int = 58,
    sender: str = "huozhe",
    body: str | None = None,
) -> dict:
    issue: dict = {
        "number": issue_num,
        "state": "closed",
        "state_reason": "completed",
        "title": "session work",
        "user": {"login": "huozhe"},
    }
    if body is not None:
        issue["body"] = body
    return {
        "action": "closed",
        "issue": issue,
        "repository": {"full_name": repo},
        "sender": {"login": sender},
    }


def _seed(
    store: Store,
    tmp: Path,
    *,
    state: str = "AWAITING_VERIFICATION",
    issue: int = 58,
    with_runner: bool = True,
    classification: str | None = None,
    with_session_dir: bool = False,
    verified_at: int | None = 1,
) -> str:
    sk = f"huozhe/code-workflow#{issue}"
    store.upsert_session(
        session_key=sk,
        project_key="huozhe/code-workflow",
        repo="huozhe/code-workflow",
        issue_num=issue,
        state=state,
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
    )
    if classification is not None:
        store.update_session_fields(sk, classification=classification)
    # ADR-17: VERIFIED requires verified_at. Default stamp; opt out with 0.
    if verified_at:
        store.update_session_fields(sk, verified_at=int(verified_at))
    if with_runner:
        store.upsert_runner(
            "huozhe/code-workflow",
            container_id="c-shared",
            endpoint="127.0.0.1:9",
            token="tok",
            tier="hot",
        )
    if with_session_dir:
        d = tmp / "projects" / "huozhe__code-workflow" / "sessions" / str(issue)
        d.mkdir(parents=True, exist_ok=True)
        (d / "keep").write_text("x", encoding="utf-8")
    return sk


def _insert_close(
    store: Store,
    *,
    did: str,
    payload: dict,
    issue: int = 58,
    sender: str = "huozhe",
) -> None:
    store.insert_delivery(
        delivery_id=did,
        event="issues",
        action="closed",
        repo="huozhe/code-workflow",
        issue_num=issue,
        sender=sender,
        payload=json.dumps(payload).encode(),
        status="deferred",
    )


def _register_open(store: Store, sk: str, *, kind: str = "scratch", ref: str = "/tmp/x") -> None:
    store.register_artifact(session_key=sk, role="developer", kind=kind, ref=ref)


def _loop(
    store: Store,
    tmp: Path,
    *,
    dispatch: bool = False,
    client_factory=None,
    supervisor=None,
    posts: list | None = None,
) -> DesignLoop:
    def _post(**k):  # noqa: ANN003
        if posts is not None:
            posts.append(k)
        return 1

    loop = DesignLoop(
        store,
        _cfg(tmp),
        supervisor=supervisor,
        dispatch_turns=dispatch,
        post_comment=_post,
        reopen_issue_fn=lambda **k: None,
        gateway_token="gw",
    )
    if client_factory is not None:
        import agentd.design_loop as dl

        loop._orig_client = dl.RunnerClient  # type: ignore[attr-defined]
        dl.RunnerClient = client_factory  # type: ignore[misc, assignment]
    return loop


# --- classify_at_close (pure) ---


def test_classify_ticked_is_verified() -> None:
    assert classify_at_close(_block(checked=True)) == "VERIFIED"


def test_classify_unticked_is_abandoned() -> None:
    assert classify_at_close(_block(checked=False)) == "ABANDONED"


def test_classify_missing_block_is_abandoned() -> None:
    assert classify_at_close("no block here") == "ABANDONED"


def test_classify_bare_tick_outside_sentinels_is_abandoned() -> None:
    assert (
        classify_at_close("- [x] Human Verification Complete\n") == "ABANDONED"
    )


# --- FSM ---


def test_teardown_and_closed_are_fsm_states_not_classifications() -> None:
    assert "TEARDOWN" in SESSION_STATES
    assert "CLOSED" in SESSION_STATES
    assert "TEARDOWN" in TERMINAL_STATES
    assert "CLOSED" in TERMINAL_STATES
    assert "VERIFIED" not in SESSION_STATES
    assert "ABANDONED" not in SESSION_STATES


def test_issues_closed_from_any_live_state() -> None:
    live = (
        "INTAKE",
        "PLANNING",
        "DESIGN_REVIEW",
        "DESIGN_REWORK",
        "DESIGN_APPROVED",
        "IMPLEMENTING",
        "CODE_REVIEW",
        "CODE_REWORK",
        "MERGING",
        "AWAITING_VERIFICATION",
        "PAUSED_HUMAN",
        "FAILED",
    )
    for state in live:
        tr = transition(state, "issues_closed")
        assert tr is not None, state
        assert tr.new_state == "TEARDOWN", state


def test_issues_closed_from_teardown_or_closed_is_noop() -> None:
    assert transition("TEARDOWN", "issues_closed") is None
    assert transition("CLOSED", "issues_closed") is None


# --- owner close classifies + moves FSM ---


def test_owner_close_ticked_is_verified_teardown(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, with_session_dir=True)
    _insert_close(
        store,
        did="d-tick",
        payload=_closed_payload(body=_block(checked=True)),
    )
    _loop(store, tmp_path).process_deferred_batch()
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "CLOSED"
    assert sess["classification"] == "VERIFIED"
    store.close()


def test_owner_close_unticked_is_abandoned(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, state="IMPLEMENTING", with_session_dir=True)
    _insert_close(
        store,
        did="d-untick",
        payload=_closed_payload(body=_block(checked=False)),
    )
    _loop(store, tmp_path).process_deferred_batch()
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "CLOSED"
    assert sess["classification"] == "ABANDONED"
    store.close()


def test_owner_close_unticked_null_verified_at_is_abandoned(tmp_path: Path) -> None:
    """Ordinary abandon: no observed tick. Must not enter the ADR-17 gate."""
    store = Store(tmp_path / "state.db")
    sk = _seed(
        store,
        tmp_path,
        state="IMPLEMENTING",
        with_session_dir=True,
        verified_at=0,
    )
    posts: list = []
    _insert_close(
        store,
        did="d-abandon-null",
        payload=_closed_payload(body=_block(checked=False)),
    )
    _loop(store, tmp_path, posts=posts).process_deferred_batch()
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "CLOSED"
    assert sess["classification"] == "ABANDONED"
    assert not sess.get("verified_at")
    assert store.get_open_escalation(sk) is None
    assert posts == []
    store.close()


def test_owner_close_reads_payload_body_not_verified_at(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path)
    store.update_session_fields(sk, verified_at=1_700_000_000)
    _insert_close(
        store,
        did="d-body",
        payload=_closed_payload(body=_block(checked=False)),
    )
    _loop(store, tmp_path).process_deferred_batch()
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["classification"] == "ABANDONED"
    assert sess["verified_at"] == 1_700_000_000
    store.close()


def test_classification_never_revised_after_close(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, with_session_dir=True)
    _insert_close(
        store,
        did="d-first",
        payload=_closed_payload(body=_block(checked=False)),
    )
    _loop(store, tmp_path).process_deferred_batch()
    assert store.get_session(sk)["classification"] == "ABANDONED"

    _insert_close(
        store,
        did="d-retick",
        payload=_closed_payload(body=_block(checked=True)),
    )
    _loop(store, tmp_path).process_deferred_batch()
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["classification"] == "ABANDONED"
    assert sess["state"] == "CLOSED"
    store.close()


def test_reclose_after_closed_is_noop(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, state="CLOSED", classification="VERIFIED")
    _insert_close(
        store,
        did="d-closed",
        payload=_closed_payload(body=_block(checked=False)),
    )
    _loop(store, tmp_path).process_deferred_batch()
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "CLOSED"
    assert sess["classification"] == "VERIFIED"
    row = store._conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", ("d-closed",)
    ).fetchone()
    assert row["status"] == "done"
    store.close()


# --- two agent turns, order, no container RPC ---


class _RecordingClient:
    calls: list[tuple[str, dict]] = []

    def __init__(self, *a, **k):  # noqa: ANN002, ANN003
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):  # noqa: ANN002
        return False

    def call(self, method, params=None):  # noqa: ANN001
        self.__class__.calls.append((method, dict(params or {})))
        return {"status": "done", "summary": "cleaned", "public_actions": []}


def test_owner_close_dispatches_developer_then_architect(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, with_session_dir=True)
    _register_open(store, sk)
    _RecordingClient.calls = []
    _insert_close(
        store,
        did="d-turns",
        payload=_closed_payload(body=_block(checked=True)),
    )
    loop = _loop(store, tmp_path, dispatch=True, client_factory=_RecordingClient)
    try:
        loop.process_deferred_batch()
    finally:
        import agentd.design_loop as dl

        dl.RunnerClient = loop._orig_client  # type: ignore[attr-defined, misc]

    dispatches = [(m, p) for m, p in _RecordingClient.calls if m == "turn.dispatch"]
    methods = [m for m, _ in dispatches]
    roles = [p.get("role") for _, p in dispatches]
    assert methods == ["turn.dispatch", "turn.dispatch"]
    assert roles == ["developer", "architect"]
    for _, params in dispatches:
        assert params.get("session_state") == "TEARDOWN"
        event = params.get("event") or {}
        assert event.get("kind") == "issues_closed"
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "CLOSED"
    assert sess["classification"] == "VERIFIED"
    runner = store.get_runner("huozhe/code-workflow")
    assert runner is not None
    assert runner["container_id"] == "c-shared"
    store.close()


def test_teardown_never_calls_session_teardown_rpc(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path)
    _register_open(store, sk)
    _RecordingClient.calls = []
    _insert_close(
        store,
        did="d-rpc",
        payload=_closed_payload(body=_block(checked=True)),
    )
    loop = _loop(store, tmp_path, dispatch=True, client_factory=_RecordingClient)
    try:
        loop.process_deferred_batch()
    finally:
        import agentd.design_loop as dl

        dl.RunnerClient = loop._orig_client  # type: ignore[attr-defined, misc]
    assert all(m != "session.teardown" for m, _ in _RecordingClient.calls)
    store.close()


def test_teardown_turns_do_not_trip_silent_or_budget(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, with_session_dir=True)
    _register_open(store, sk)
    store.update_session_fields(sk, silent_turns=2, consec_agent_turns=6)
    _RecordingClient.calls = []
    _insert_close(
        store,
        did="d-silent",
        payload=_closed_payload(body=_block(checked=True)),
    )
    loop = _loop(store, tmp_path, dispatch=True, client_factory=_RecordingClient)
    try:
        loop.process_deferred_batch()
    finally:
        import agentd.design_loop as dl

        dl.RunnerClient = loop._orig_client  # type: ignore[attr-defined, misc]
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "CLOSED"
    assert sess["silent_turns"] == 2
    assert sess["consec_agent_turns"] == 6
    assert store.get_open_escalation(sk) is None
    store.close()


def test_redelivery_while_teardown_does_not_reclassify(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, state="TEARDOWN", classification="ABANDONED")
    _RecordingClient.calls = []
    _insert_close(
        store,
        did="d-again",
        payload=_closed_payload(body=_block(checked=True)),
    )
    loop = _loop(store, tmp_path, dispatch=True, client_factory=_RecordingClient)
    try:
        loop.process_deferred_batch()
    finally:
        import agentd.design_loop as dl

        dl.RunnerClient = loop._orig_client  # type: ignore[attr-defined, misc]
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["classification"] == "ABANDONED"
    # B3: drained ledger → no second pair of turns.
    assert _RecordingClient.calls == []
    store.close()


# --- ledger: observe disk, do not trust the turn report ---


def test_removed_at_only_after_gateway_confirms_gone(
    tmp_path: Path, caplog
) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path)
    wt = tmp_path / "projects" / "huozhe__code-workflow" / "sessions" / "58" / "developer" / "worktrees" / "issue-58"
    scratch = (
        tmp_path
        / "projects"
        / "huozhe__code-workflow"
        / "sessions"
        / "58"
        / "architect"
        / "scratch"
    )
    wt.mkdir(parents=True)
    scratch.mkdir(parents=True)
    (scratch / "diff.patch").write_text("x", encoding="utf-8")
    store.register_artifact(
        session_key=sk, role="developer", kind="worktree", ref=str(wt)
    )
    store.register_artifact(
        session_key=sk, role="architect", kind="scratch", ref=str(scratch)
    )

    class KeepOnDisk(_RecordingClient):
        pass

    KeepOnDisk.calls = []
    _insert_close(
        store,
        did="d-keep",
        payload=_closed_payload(body=_block(checked=True)),
    )
    loop = _loop(store, tmp_path, dispatch=True, client_factory=KeepOnDisk)
    with caplog.at_level(logging.WARNING):
        try:
            loop.process_deferred_batch()
        finally:
            import agentd.design_loop as dl

            dl.RunnerClient = loop._orig_client  # type: ignore[attr-defined, misc]
    open_rows = store.list_artifacts(sk, open_only=True)
    assert {r["ref"] for r in open_rows} == {str(wt), str(scratch)}
    assert any("leak" in r.message.lower() for r in caplog.records)
    store.close()


def test_gateway_marks_removed_when_path_gone(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path)
    issue_host = (
        tmp_path / "projects" / "huozhe__code-workflow" / "sessions" / "58"
    )
    wt = issue_host / "developer" / "worktrees" / "issue-58"
    scratch = issue_host / "architect" / "scratch"
    wt.mkdir(parents=True)
    scratch.mkdir(parents=True)

    store.register_artifact(
        session_key=sk, role="developer", kind="worktree", ref=str(wt)
    )
    store.register_artifact(
        session_key=sk, role="architect", kind="scratch", ref=str(scratch)
    )

    class RemoveOnTurn(_RecordingClient):
        def call(self, method, params=None):  # noqa: ANN001
            role = (params or {}).get("role")
            if role == "developer" and wt.exists():
                wt.rmdir()
            if role == "architect" and scratch.exists():
                scratch.rmdir()
            return super().call(method, params)

    RemoveOnTurn.calls = []
    _insert_close(
        store,
        did="d-gone",
        payload=_closed_payload(body=_block(checked=True)),
    )
    loop = _loop(store, tmp_path, dispatch=True, client_factory=RemoveOnTurn)
    try:
        loop.process_deferred_batch()
    finally:
        import agentd.design_loop as dl

        dl.RunnerClient = loop._orig_client  # type: ignore[attr-defined, misc]
    assert store.list_artifacts(sk, open_only=True) == []
    all_rows = store.list_artifacts(sk, open_only=False)
    assert len(all_rows) == 2
    assert all(r["removed_at"] is not None for r in all_rows)
    store.close()


def test_branch_marked_removed_only_when_git_list_empty(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path)
    clone = shared_clone_path(tmp_path, "huozhe/code-workflow")
    clone.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=clone, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "i"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    branch = role_branch_name("huozhe/code-workflow", 58, "developer")
    subprocess.run(
        ["git", "branch", branch], cwd=clone, check=True, capture_output=True
    )
    store.register_artifact(
        session_key=sk, role="developer", kind="branch", ref=branch
    )

    class DeleteBranch(_RecordingClient):
        def call(self, method, params=None):  # noqa: ANN001
            role = (params or {}).get("role")
            if role == "developer":
                subprocess.run(
                    ["git", "branch", "-D", branch],
                    cwd=clone,
                    check=True,
                    capture_output=True,
                )
            return super().call(method, params)

    DeleteBranch.calls = []
    _insert_close(
        store,
        did="d-br",
        payload=_closed_payload(body=_block(checked=True)),
    )
    loop = _loop(store, tmp_path, dispatch=True, client_factory=DeleteBranch)
    try:
        loop.process_deferred_batch()
    finally:
        import agentd.design_loop as dl

        dl.RunnerClient = loop._orig_client  # type: ignore[attr-defined, misc]
    open_rows = store.list_artifacts(sk, open_only=True)
    assert open_rows == []
    store.close()


# --- B1: ensure_session must not leave INTAKE on the wire ---


class _IntakeClobberSupervisor:
    """Mirrors supervisor.ensure_session create path: upsert_session(INTAKE)."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.calls = 0

    def ensure_session(self, **k):  # noqa: ANN003
        self.calls += 1
        now = 9
        self.store.upsert_session(
            session_key=k["session_key"],
            project_key="huozhe/code-workflow",
            repo=k["repo"],
            issue_num=k["issue_num"],
            state="INTAKE",
            architect="huozheclaude",
            developer="huozhegrok",
            created_at=now,
            updated_at=now,
        )
        self.store.upsert_runner(
            "huozhe/code-workflow",
            container_id="c-recreated",
            endpoint="127.0.0.1:9",
            token="tok",
            tier="hot",
        )


def test_no_runner_ensure_session_still_sends_teardown_state(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, with_runner=False, with_session_dir=True)
    _register_open(store, sk)
    sup = _IntakeClobberSupervisor(store)
    _RecordingClient.calls = []
    _insert_close(
        store,
        did="d-norunner",
        payload=_closed_payload(body=_block(checked=True)),
    )
    loop = _loop(
        store,
        tmp_path,
        dispatch=True,
        client_factory=_RecordingClient,
        supervisor=sup,
    )
    try:
        loop.process_deferred_batch()
    finally:
        import agentd.design_loop as dl

        dl.RunnerClient = loop._orig_client  # type: ignore[attr-defined, misc]
    assert sup.calls == 1
    dispatches = [(m, p) for m, p in _RecordingClient.calls if m == "turn.dispatch"]
    assert len(dispatches) == 2
    states = [p.get("session_state") for _, p in dispatches]
    assert states == ["TEARDOWN", "TEARDOWN"]
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "CLOSED"
    assert sess["classification"] == "VERIFIED"
    store.close()


# --- B2: empty scratch dir is gone ---


def test_empty_scratch_directory_is_confirmed_removed(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path)
    scratch = (
        tmp_path
        / "projects"
        / "huozhe__code-workflow"
        / "sessions"
        / "58"
        / "architect"
        / "scratch"
    )
    scratch.mkdir(parents=True)
    store.register_artifact(
        session_key=sk, role="architect", kind="scratch", ref=str(scratch)
    )
    _RecordingClient.calls = []
    _insert_close(
        store,
        did="d-empty-scratch",
        payload=_closed_payload(body=_block(checked=True)),
    )
    loop = _loop(store, tmp_path, dispatch=True, client_factory=_RecordingClient)
    try:
        loop.process_deferred_batch()
    finally:
        import agentd.design_loop as dl

        dl.RunnerClient = loop._orig_client  # type: ignore[attr-defined, misc]
    assert store.list_artifacts(sk, open_only=True) == []
    store.close()


# --- B3: leftover artifacts still retry ---


def test_redelivery_retries_only_when_ledger_still_open(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, state="TEARDOWN", classification="ABANDONED")
    _register_open(store, sk, ref="/still/there")
    _RecordingClient.calls = []
    _insert_close(
        store,
        did="d-retry",
        payload=_closed_payload(body=_block(checked=True)),
    )
    loop = _loop(store, tmp_path, dispatch=True, client_factory=_RecordingClient)
    try:
        loop.process_deferred_batch()
    finally:
        import agentd.design_loop as dl

        dl.RunnerClient = loop._orig_client  # type: ignore[attr-defined, misc]
    assert len([m for m, _ in _RecordingClient.calls if m == "turn.dispatch"]) == 2
    store.close()


# --- B4: needs_human stays in TEARDOWN ---


class _NeedsHumanClient(_RecordingClient):
    def call(self, method, params=None):  # noqa: ANN001
        super().call(method, params)
        return {"status": "needs_human", "summary": "cleanup", "public_actions": []}


def test_teardown_needs_human_does_not_pause(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, with_session_dir=True)
    _register_open(store, sk)
    posts: list = []
    _NeedsHumanClient.calls = []
    _insert_close(
        store,
        did="d-nh",
        payload=_closed_payload(body=_block(checked=True)),
    )
    loop = _loop(
        store,
        tmp_path,
        dispatch=True,
        client_factory=_NeedsHumanClient,
        posts=posts,
    )
    try:
        loop.process_deferred_batch()
    finally:
        import agentd.design_loop as dl

        dl.RunnerClient = loop._orig_client  # type: ignore[attr-defined, misc]
    sess = store.get_session(sk)
    assert sess is not None
    # Ledger ref is gone, so archive proceeds. needs_human must not pause.
    assert sess["state"] == "CLOSED"
    assert store.get_open_escalation(sk) is None
    assert all("needs a decision" not in (p.get("body") or "") for p in posts)
    store.close()


# --- #67: stale runners row must still call ensure_session ---


def test_stale_runner_row_still_calls_ensure_session(tmp_path: Path) -> None:
    """#67: truthy runners row is not reachability. Always ensure_session."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, with_runner=True)
    _register_open(store, sk)
    sup = _IntakeClobberSupervisor(store)
    _RecordingClient.calls = []
    _insert_close(
        store,
        did="d-stale",
        payload=_closed_payload(body=_block(checked=True)),
    )
    loop = _loop(
        store,
        tmp_path,
        dispatch=True,
        client_factory=_RecordingClient,
        supervisor=sup,
    )
    try:
        loop.process_deferred_batch()
    finally:
        import agentd.design_loop as dl

        dl.RunnerClient = loop._orig_client  # type: ignore[attr-defined, misc]
    assert sup.calls == 1
    states = [
        p.get("session_state")
        for m, p in _RecordingClient.calls
        if m == "turn.dispatch"
    ]
    assert states == ["TEARDOWN", "TEARDOWN"]
    store.close()


class _FailedClient(_RecordingClient):
    def call(self, method, params=None):  # noqa: ANN001
        super().call(method, params)
        return {
            "status": "failed",
            "summary": "[Errno 61] Connection refused",
            "public_actions": [],
        }


def test_failed_teardown_turn_leaves_delivery_deferred(tmp_path: Path) -> None:
    """#67: a failed teardown must stay retryable — do not mark done."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path)
    _register_open(store, sk)
    _FailedClient.calls = []
    import agentd.design_loop as dl

    dl._delivery_attempts.clear()
    _insert_close(
        store,
        did="d-refused",
        payload=_closed_payload(body=_block(checked=True)),
    )
    loop = _loop(store, tmp_path, dispatch=True, client_factory=_FailedClient)
    try:
        loop.process_deferred_batch()
    finally:
        dl.RunnerClient = loop._orig_client  # type: ignore[attr-defined, misc]
    row = store._conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", ("d-refused",)
    ).fetchone()
    assert row["status"] == "deferred"
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "TEARDOWN"
    store.close()


def test_failed_teardown_exhausts_and_escalates(tmp_path: Path) -> None:
    """#69 B1/B2: 5 failed drains then done + escalate. No unbounded loop."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path)
    wt = tmp_path / "worktree"
    wt.mkdir()
    (wt / "keep").write_text("x", encoding="utf-8")
    _register_open(store, sk, kind="worktree", ref=str(wt))
    posts: list = []
    _FailedClient.calls = []
    import agentd.design_loop as dl

    dl._delivery_attempts.clear()
    _insert_close(
        store,
        did="d-exhaust",
        payload=_closed_payload(body=_block(checked=True)),
    )
    loop = _loop(
        store,
        tmp_path,
        dispatch=True,
        client_factory=_FailedClient,
        posts=posts,
    )
    try:
        for _ in range(7):
            loop.process_deferred_batch()
    finally:
        dl.RunnerClient = loop._orig_client  # type: ignore[attr-defined, misc]
    row = store._conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", ("d-exhaust",)
    ).fetchone()
    assert row["status"] == "done"
    # 2 roles × 5 attempts, not more (idle_wait would otherwise spin forever)
    assert len([m for m, _ in _FailedClient.calls if m == "turn.dispatch"]) == 10
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "PAUSED_HUMAN"
    assert store.get_open_escalation(sk) is not None
    assert posts and "@huozhe" in posts[0]["body"]
    assert "teardown" in posts[0]["body"].lower()
    store.close()


class _CapacitySupervisor:
    def __init__(self) -> None:
        self.calls = 0

    def ensure_session(self, **k):  # noqa: ANN003
        self.calls += 1
        raise CapacityRefusal("hot slots full")


def test_teardown_capacity_refusal_uses_same_retry_bound(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path)
    _register_open(store, sk, kind="worktree", ref=str(tmp_path / "wt"))
    (tmp_path / "wt").mkdir()
    (tmp_path / "wt" / "f").write_text("x", encoding="utf-8")
    posts: list = []
    import agentd.design_loop as dl

    dl._delivery_attempts.clear()
    _insert_close(
        store,
        did="d-cap",
        payload=_closed_payload(body=_block(checked=True)),
    )
    loop = _loop(
        store,
        tmp_path,
        dispatch=True,
        supervisor=_CapacitySupervisor(),
        posts=posts,
    )
    for _ in range(7):
        loop.process_deferred_batch()
    row = store._conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", ("d-cap",)
    ).fetchone()
    assert row["status"] == "done"
    assert store.get_session(sk)["state"] == "PAUSED_HUMAN"
    assert posts and "capacity" in posts[0]["body"].lower()
    store.close()


# --- #78 B2: dispatch-time ensure must not resurrect a just-removed branch ---


def test_teardown_second_role_does_not_resurrect_branch(tmp_path: Path) -> None:
    """After developer deletes its branch, architect dispatch must not recreate it."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, tmp_path, with_runner=True, with_session_dir=True)
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init"], cwd=src, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=src, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=src, check=True, capture_output=True
    )
    (src / "f").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "i"], cwd=src, check=True, capture_output=True)
    clone = ensure_shared_clone(
        tmp_path, "huozhe/code-workflow", clone_url=str(src)
    )
    branch = role_branch_name("huozhe/code-workflow", 58, "developer")
    subprocess.run(["git", "branch", branch], cwd=clone, check=True, capture_output=True)
    store.register_artifact(
        session_key=sk, role="developer", kind="branch", ref=branch
    )

    class _RealisticSupervisor:
        def __init__(self) -> None:
            self.calls = 0

        def ensure_session(self, **k):  # noqa: ANN003
            self.calls += 1
            listed = subprocess.run(
                ["git", "branch", "--list", branch],
                cwd=clone,
                check=True,
                capture_output=True,
                text=True,
            )
            if not listed.stdout.strip():
                subprocess.run(
                    ["git", "branch", branch], cwd=clone, check=True, capture_output=True
                )
            store.register_artifact(
                session_key=k["session_key"], role="developer", kind="branch", ref=branch
            )
            store.upsert_runner(
                "huozhe/code-workflow",
                container_id="c-shared",
                endpoint="127.0.0.1:9",
                token="tok",
                tier="hot",
            )

    class _DeleteOwnBranch(_RecordingClient):
        def call(self, method, params=None):  # noqa: ANN001
            if (params or {}).get("role") == "developer":
                subprocess.run(
                    ["git", "branch", "-D", branch],
                    cwd=clone,
                    check=False,
                    capture_output=True,
                )
            return super().call(method, params)

    sup = _RealisticSupervisor()
    _DeleteOwnBranch.calls = []
    _insert_close(
        store,
        did="d-resurrect",
        payload=_closed_payload(body=_block(checked=True)),
    )
    loop = _loop(
        store,
        tmp_path,
        dispatch=True,
        client_factory=_DeleteOwnBranch,
        supervisor=sup,
    )
    try:
        loop.process_deferred_batch()
    finally:
        import agentd.design_loop as dl

        dl.RunnerClient = loop._orig_client  # type: ignore[attr-defined, misc]

    listed = subprocess.run(
        ["git", "branch", "--list", branch],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    )
    assert listed.stdout.strip() == ""
    assert store.list_artifacts(sk, open_only=True) == []
    assert (store.get_session(sk) or {})["state"] == "CLOSED"
    assert sup.calls == 1
    store.close()
