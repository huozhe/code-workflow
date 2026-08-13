"""M5-3 / §10.5 step 3 / ADR-12: archive, purge, then CLOSED."""

from __future__ import annotations

import json
import os
import tarfile
from pathlib import Path

from agentd.archive import (
    archive_tarball_path,
    session_dir_path,
    write_tarball,
)
from agentd.config import Config
from agentd.db import Store
from agentd.design_loop import DesignLoop
from agentd.paths import DEFAULT_CONFIG
from agentd.verification import render_verification_block


def _cfg(tmp: Path) -> Config:
    return Config(
        raw={
            "host": {"owner": "huozhe"},
            "gateway": {"login": "huozhegateway"},
            "agents": {
                "claude": {"login": "huozheclaude"},
                "grok": {"login": "huozhegrok"},
            },
            "retention": {"archive_days": 30},
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
        "repository": {"full_name": "huozhe/code-workflow"},
        "sender": {"login": sender},
    }


def _seed(
    store: Store,
    *,
    state: str = "TEARDOWN",
    issue: int = 58,
    classification: str | None = "VERIFIED",
    design_pr: int | None = 59,
    feature_pr: int | None = 60,
    turn_count: int = 13,
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
        design_pr=design_pr,
        turn_count=turn_count,
    )
    extra: dict = {}
    if feature_pr is not None:
        extra["feature_pr"] = feature_pr
    if classification is not None:
        extra["classification"] = classification
    if extra:
        store.update_session_fields(sk, **extra)
    return sk


def _insert_close(
    store: Store,
    *,
    did: str,
    payload: dict,
    issue: int = 58,
) -> None:
    store.insert_delivery(
        delivery_id=did,
        event="issues",
        action="closed",
        repo="huozhe/code-workflow",
        issue_num=issue,
        sender="huozhe",
        payload=json.dumps(payload).encode(),
        status="deferred",
    )


def _loop(store: Store, tmp: Path, *, posts: list | None = None) -> DesignLoop:
    def _post(**k):  # noqa: ANN003
        if posts is not None:
            posts.append(k)
        return 1

    return DesignLoop(
        store,
        _cfg(tmp),
        supervisor=None,
        dispatch_turns=False,
        post_comment=_post,
        reopen_issue_fn=lambda **k: None,
        gateway_token="gw",
    )


def _make_session_dir(tmp: Path, issue: int = 58, *, leftover: str | None = "note.txt") -> Path:
    d = session_dir_path(tmp, "huozhe/code-workflow", issue)
    (d / "architect" / "scratch").mkdir(parents=True)
    (d / "developer" / "context").mkdir(parents=True)
    (d / "architect" / "transcript.jsonl").write_text("{}\n", encoding="utf-8")
    if leftover:
        (d / "architect" / "scratch" / leftover).write_text("keep me\n", encoding="utf-8")
    return d


# --- config ---


def test_archive_days_defaults_to_30() -> None:
    assert Config(raw={}).archive_days == 30
    assert Config(raw={"retention": {"archive_days": 7}}).archive_days == 7


def test_default_config_declares_archive_days() -> None:
    assert "archive_days: 30" in DEFAULT_CONFIG


# --- write discipline ---


def test_write_tarball_uses_tmp_then_rename(tmp_path: Path, monkeypatch) -> None:
    session = tmp_path / "sessions" / "58"
    session.mkdir(parents=True)
    (session / "f").write_text("x", encoding="utf-8")
    dest = tmp_path / "archive" / "huozhe__code-workflow" / "58.tar.gz"
    seen: list[tuple[str, str]] = []
    real = os.replace

    def spy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        seen.append((Path(src).name, Path(dst).name))
        assert Path(src).exists()
        assert Path(src).name.endswith(".tar.gz.tmp")
        real(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    write_tarball(session, dest)
    assert dest.is_file()
    assert seen == [("58.tar.gz.tmp", "58.tar.gz")]
    assert not dest.with_name("58.tar.gz.tmp").exists()


# --- owner close: archive → purge → CLOSED ---


def test_drained_teardown_archives_then_closes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("time.time", lambda: 1_700_000_000)
    store = Store(tmp_path / "state.db")
    sk = _seed(store)
    session = _make_session_dir(tmp_path)
    _insert_close(
        store,
        did="d-arch",
        payload=_closed_payload(body=_block(checked=True)),
    )
    _loop(store, tmp_path).process_deferred_batch()

    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "CLOSED"
    assert sess["classification"] == "VERIFIED"
    assert not session.exists()
    dest = archive_tarball_path(tmp_path, "huozhe/code-workflow", 58)
    assert dest.is_file()
    assert not dest.with_name("58.tar.gz.tmp").exists()

    with tarfile.open(dest, "r:gz") as tf:
        names = tf.getnames()
        assert "58/manifest.json" in names
        assert "58/architect/scratch/note.txt" in names
        assert "58/architect/transcript.jsonl" in names
        raw = tf.extractfile("58/manifest.json")
        assert raw is not None
        manifest = json.loads(raw.read().decode("utf-8"))
    assert manifest == {
        "session_key": sk,
        "project_key": "huozhe/code-workflow",
        "terminal_state": "VERIFIED",
        "design_pr": 59,
        "feature_pr": 60,
        "turn_count": 0,
        "closed_at": 1_700_000_000,
    }
    # Archive is not an artifacts-ledger row (ADR-12).
    assert store.list_artifacts(sk, open_only=False) == []
    store.close()


def test_abandoned_archives_without_summary(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, classification="ABANDONED")
    _make_session_dir(tmp_path)
    posts: list = []
    _insert_close(
        store,
        did="d-aban",
        payload=_closed_payload(body=_block(checked=False)),
    )
    _loop(store, tmp_path, posts=posts).process_deferred_batch()
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "CLOSED"
    assert sess["classification"] == "ABANDONED"
    dest = archive_tarball_path(tmp_path, "huozhe/code-workflow", 58)
    assert dest.is_file()
    with tarfile.open(dest, "r:gz") as tf:
        manifest = json.loads(
            tf.extractfile("58/manifest.json").read().decode("utf-8")  # type: ignore[union-attr]
        )
    assert manifest["terminal_state"] == "ABANDONED"
    assert posts == []
    store.close()


def test_verified_posts_completion_summary(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, classification="VERIFIED")
    _make_session_dir(tmp_path)
    posts: list = []
    _insert_close(
        store,
        did="d-sum",
        payload=_closed_payload(body=_block(checked=True)),
    )
    _loop(store, tmp_path, posts=posts).process_deferred_batch()
    assert len(posts) == 1
    body = posts[0]["body"]
    assert sk in body
    assert "VERIFIED" in body
    assert "archive/huozhe__code-workflow/58.tar.gz" in body
    store.close()


def test_closed_redelivery_is_noop(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="CLOSED", classification="VERIFIED")
    dest = archive_tarball_path(tmp_path, "huozhe/code-workflow", 58)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"already")
    posts: list = []
    _insert_close(
        store,
        did="d-re",
        payload=_closed_payload(body=_block(checked=False)),
    )
    _loop(store, tmp_path, posts=posts).process_deferred_batch()
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "CLOSED"
    assert sess["classification"] == "VERIFIED"
    assert dest.read_bytes() == b"already"
    assert posts == []
    store.close()


def test_missing_dir_still_flips_closed(tmp_path: Path) -> None:
    """Crash between purge and flip: retry the flip, do not fail."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="TEARDOWN", classification="ABANDONED")
    dest = archive_tarball_path(tmp_path, "huozhe/code-workflow", 58)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"prior")
    _insert_close(
        store,
        did="d-flip",
        payload=_closed_payload(body=_block(checked=False)),
    )
    _loop(store, tmp_path).process_deferred_batch()
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "CLOSED"
    assert dest.read_bytes() == b"prior"
    store.close()


def test_open_ledger_does_not_archive(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="TEARDOWN", classification="ABANDONED")
    session = _make_session_dir(tmp_path)
    store.register_artifact(
        session_key=sk, role="developer", kind="scratch", ref="/still/there"
    )
    _insert_close(
        store,
        did="d-open",
        payload=_closed_payload(body=_block(checked=False)),
    )
    _loop(store, tmp_path).process_deferred_batch()
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "TEARDOWN"
    assert session.exists()
    dest = archive_tarball_path(tmp_path, "huozhe/code-workflow", 58)
    assert not dest.exists()
    store.close()


def test_manifest_turn_count_reads_turns_table(tmp_path: Path) -> None:
    """#72: ADR-12 turn_count is observed rows, not the cached column."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, classification="ABANDONED", turn_count=0)
    store.update_session_fields(sk, turn_count=0)
    for i in range(3):
        store.insert_turn(
            turn_id=f"t-{i}",
            session_key=sk,
            role="developer",
            delivery_id=None,
            started_at=i,
            ended_at=i,
            status="done",
            summary="ok",
        )
    _make_session_dir(tmp_path)
    _insert_close(
        store,
        did="d-turns",
        payload=_closed_payload(body=_block(checked=False)),
    )
    _loop(store, tmp_path).process_deferred_batch()
    dest = archive_tarball_path(tmp_path, "huozhe/code-workflow", 58)
    with tarfile.open(dest, "r:gz") as tf:
        manifest = json.loads(
            tf.extractfile("58/manifest.json").read().decode("utf-8")  # type: ignore[union-attr]
        )
    assert store.get_session(sk)["turn_count"] == 0
    assert manifest["turn_count"] == 3
    store.close()


def test_no_dir_no_tarball_does_not_close(tmp_path: Path) -> None:
    """B1: never CLOSED with neither tarball nor live directory."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="TEARDOWN", classification="ABANDONED")
    import agentd.design_loop as dl

    dl._teardown_attempts.clear()
    _insert_close(
        store,
        did="d-nodir",
        payload=_closed_payload(body=_block(checked=False)),
    )
    _loop(store, tmp_path).process_deferred_batch()
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "TEARDOWN"
    dest = archive_tarball_path(tmp_path, "huozhe/code-workflow", 58)
    assert not dest.exists()
    row = store._conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", ("d-nodir",)
    ).fetchone()
    assert row["status"] == "deferred"
    store.close()


def test_archive_failure_exhausts_and_escalates(tmp_path: Path, monkeypatch) -> None:
    """B2: archive errors use the same 5-attempt cap as teardown turns."""
    store = Store(tmp_path / "state.db")
    sk = _seed(store, state="TEARDOWN", classification="ABANDONED")
    _make_session_dir(tmp_path)
    posts: list = []
    import agentd.design_loop as dl

    dl._teardown_attempts.clear()

    def boom(**k):  # noqa: ANN003
        raise OSError("disk full")

    monkeypatch.setattr(dl, "archive_and_purge", boom)
    _insert_close(
        store,
        did="d-archfail",
        payload=_closed_payload(body=_block(checked=False)),
    )
    loop = _loop(store, tmp_path, posts=posts)
    for _ in range(7):
        loop.process_deferred_batch()
    row = store._conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", ("d-archfail",)
    ).fetchone()
    assert row["status"] == "done"
    sess = store.get_session(sk)
    assert sess is not None
    assert sess["state"] == "PAUSED_HUMAN"
    assert store.get_open_escalation(sk) is not None
    assert posts and "archive" in posts[0]["body"].lower()
    store.close()


def test_write_tarball_fsyncs_file_then_directory(tmp_path: Path, monkeypatch) -> None:
    session = tmp_path / "sessions" / "58"
    session.mkdir(parents=True)
    (session / "f").write_text("x", encoding="utf-8")
    dest = tmp_path / "archive" / "huozhe__code-workflow" / "58.tar.gz"
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def spy_fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def spy_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        events.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)
    write_tarball(session, dest)
    assert events[0] == "fsync"
    assert "replace" in events
    assert events.index("fsync") < events.index("replace")
    assert events[-1] == "fsync"
    assert events.count("fsync") >= 2
