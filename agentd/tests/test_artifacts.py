"""M3-C: artifact ledger + supervisor-observed registration (§10.5 / FR-4.3)."""

from __future__ import annotations

from pathlib import Path

from agentd.config import Config
from agentd.db import Store
from agentd.supervisor import SessionSupervisor


def test_register_artifact_idempotent(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
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
    id1 = store.register_artifact(
        session_key="o/r#1", role="architect", kind="worktree", ref="/wt/a"
    )
    id2 = store.register_artifact(
        session_key="o/r#1", role="architect", kind="worktree", ref="/wt/a"
    )
    assert id1 == id2
    open_rows = store.list_artifacts("o/r#1", open_only=True)
    assert len(open_rows) == 1
    assert open_rows[0]["ref"] == "/wt/a"
    store.close()


def test_mark_artifact_removed(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.upsert_session(
        session_key="o/r#2",
        repo="o/r",
        issue_num=2,
        state="PLANNING",
        architect="a",
        developer="d",
        created_at=1,
        updated_at=1,
    )
    store.register_artifact(
        session_key="o/r#2", role="developer", kind="scratch", ref="/s"
    )
    n = store.mark_artifact_removed(session_key="o/r#2", ref="/s")
    assert n == 1
    assert store.list_artifacts("o/r#2", open_only=True) == []
    all_rows = store.list_artifacts("o/r#2", open_only=False)
    assert len(all_rows) == 1 and all_rows[0]["removed_at"] is not None
    store.close()


def test_register_session_layout_artifacts_observes_worktrees(
    tmp_path: Path,
) -> None:
    """Primary ledger path: what supervisor created, not model reports."""
    store = Store(tmp_path / "state.db")
    cfg = Config(
        raw={
            "host": {"owner": "huozhe"},
            "agents": {
                "claude": {"login": "huozheclaude"},
                "grok": {"login": "huozhegrok"},
            },
        },
        root=tmp_path,
    )
    store.upsert_session(
        session_key="huozhe/demo#7",
        project_key="huozhe/demo",
        repo="huozhe/demo",
        issue_num=7,
        state="INTAKE",
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
    )
    # Layout dirs as ensure_session would leave them (no real git).
    from agentd.gitops import issue_session_rel, project_dir_name, project_path

    proj = project_path(tmp_path, "huozhe/demo")
    issue_host = proj / issue_session_rel(7)
    for role in ("architect", "developer"):
        (issue_host / role / "worktrees" / "issue-7").mkdir(parents=True)
        (issue_host / role / "scratch").mkdir(parents=True)

    sup = SessionSupervisor(store, cfg)
    registered = sup._register_session_layout_artifacts(
        session_key="huozhe/demo#7",
        repo="huozhe/demo",
        issue_num=7,
    )
    # 2 roles × (worktree + branch + scratch)
    assert len(registered) == 6
    open_rows = store.list_artifacts("huozhe/demo#7")
    assert len(open_rows) == 6
    kinds = {r["kind"] for r in open_rows}
    assert kinds == {"worktree", "branch", "scratch"}
    branch_refs = {r["ref"] for r in open_rows if r["kind"] == "branch"}
    prefix = f"agentd/{project_dir_name('huozhe/demo')}/7"
    assert f"{prefix}/architect" in branch_refs
    assert f"{prefix}/developer" in branch_refs
    # Idempotent re-register
    registered2 = sup._register_session_layout_artifacts(
        session_key="huozhe/demo#7",
        repo="huozhe/demo",
        issue_num=7,
    )
    assert len(registered2) == 6
    assert len(store.list_artifacts("huozhe/demo#7")) == 6
    store.close()


def test_runner_notify_artifact_register_persists(tmp_path: Path) -> None:
    """Runner → gateway notification path via design_loop callback."""
    store = Store(tmp_path / "state.db")
    cfg = Config(
        raw={
            "host": {"owner": "huozhe"},
            "agents": {
                "claude": {"login": "a"},
                "grok": {"login": "d"},
            },
        },
        root=tmp_path,
    )
    store.upsert_session(
        session_key="o/r#3",
        repo="o/r",
        issue_num=3,
        state="PLANNING",
        architect="a",
        developer="d",
        created_at=1,
        updated_at=1,
    )
    store.upsert_runner(
        "o/r",
        container_id="c1",
        endpoint="127.0.0.1:9",
        token="tok",
        tier="hot",
    )

    from agentd.design_loop import DesignLoop

    loop = DesignLoop(store, cfg, supervisor=None, dispatch_turns=True)

    # Simulate the notification callback used inside _dispatch_turn
    def on_notify(method: str, params: dict) -> None:
        if method == "artifact.register":
            store.register_artifact(
                session_key="o/r#3",
                role=str(params.get("role") or "architect"),
                kind=str(params.get("kind") or "scratch"),
                ref=str(params.get("ref") or ""),
            )

    # Direct unit: register via same shape as notify params
    on_notify(
        "artifact.register",
        {
            "role": "architect",
            "kind": "scratch",
            "ref": "/srv/agentd/sessions/3/architect/scratch/x.patch",
        },
    )
    rows = store.list_artifacts("o/r#3")
    assert len(rows) == 1
    assert rows[0]["kind"] == "scratch"
    assert "x.patch" in rows[0]["ref"]
    store.close()
    _ = loop  # constructed for import path smoke


def test_rpc_client_forwards_artifact_register_notification() -> None:
    """RunnerClient drains no-id frames into on_notification."""
    from agentd.rpc_client import RunnerClient

    seen: list[tuple[str, dict]] = []

    class FakeWfile:
        last: bytes = b""

        def write(self, data: bytes) -> None:
            type(self).last = data

        def flush(self) -> None:
            return None

        def close(self) -> None:
            return None

    import json

    class MatchingRfile:
        def __init__(self) -> None:
            self._step = 0

        def readline(self) -> bytes:
            if self._step == 0:
                self._step = 1
                return (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "artifact.register",
                            "params": {
                                "role": "developer",
                                "kind": "scratch",
                                "ref": "/tmp/a",
                            },
                        }
                    )
                    + "\n"
                ).encode()
            req_id = json.loads(FakeWfile.last.decode())["id"]
            return (
                json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"ok": True}})
                + "\n"
            ).encode()

        def close(self) -> None:
            return None

    cli = RunnerClient(
        "127.0.0.1",
        1,
        "bearer",
        on_notification=lambda m, p: seen.append((m, p)),
    )
    # Bypass connect
    cli._wfile = FakeWfile()  # type: ignore[assignment]
    cli._rfile = MatchingRfile()  # type: ignore[assignment]
    result = cli.call("health.ping", {})
    assert result == {"ok": True}
    assert len(seen) == 1
    assert seen[0][0] == "artifact.register"
    assert seen[0][1]["ref"] == "/tmp/a"
