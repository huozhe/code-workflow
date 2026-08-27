"""ADR-34 / §11.2 step 7 (#116): the reconciler attaches and records.

Acceptance items (1)–(11) of ADR-34, one test each, named for the item.

The probe's whole deliverable is a column and a log line, so a fixture that
produces "unreachable" for a reason unrelated to the code under test — a wrong
port, a missing token, a stub that was never started — passes every negative
assertion here while proving nothing. Items (2) and (9) therefore prove the
fixture is reachable-shaped *first* and only then assert the negative, and the
positives run against a real socket rather than a stand-in.
"""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

from agentd.db import Store
from agentd.gc import GarbageCollector
from agentd.reconciler import Reconciler
from agentd.rpc_client import ProbeResult, probe_runner

PROJECT = "o/r"


class _StubRunner:
    """A real NDJSON runner socket. `initialized` is flippable between passes."""

    BEARER = "tok"

    def __init__(self, *, initialized: bool = True) -> None:
        self.initialized = initialized
        self.pings = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = int(self._sock.getsockname()[1])
        self._stop = False
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rwb") as f:
            while True:
                raw = f.readline()
                if not raw:
                    return
                req = json.loads(raw.decode())
                method = req.get("method")
                if method == "session.attach":
                    if str((req.get("params") or {}).get("bearer") or "") != self.BEARER:
                        # the runner's own shape: server.py returns -32001 here
                        f.write(
                            (json.dumps({
                                "jsonrpc": "2.0", "id": req.get("id"),
                                "error": {"code": -32001, "message": "invalid bearer"},
                            }) + "\n").encode()
                        )
                        f.flush()
                        continue
                    result: dict[str, Any] = {"attached": True}
                elif method == "health.ping":
                    self.pings += 1
                    result = {
                        "ok": True,
                        "initialized": self.initialized,
                        "rss_bytes": 246_000_000,
                        "cli_rss_kb": 70_000,
                    }
                else:
                    result = {}
                f.write(
                    (json.dumps({"jsonrpc": "2.0", "id": req.get("id"), "result": result}) + "\n").encode()
                )
                f.flush()

    def close(self) -> None:
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass


@pytest.fixture()
def stub() -> Any:
    s = _StubRunner()
    yield s
    s.close()


def _closed_port() -> int:
    """A port with nothing listening — a connect failure, not a hang."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _seed(
    store: Store,
    *,
    project: str = PROJECT,
    issue: int = 1,
    cid: str = "cid-1",
    endpoint: str = "127.0.0.1:1",
    state: str = "IMPLEMENTING",
    last_seen_at: int | None = None,
) -> None:
    store.upsert_session(
        session_key=f"{project}#{issue}",
        project_key=project,
        repo=project,
        issue_num=issue,
        state=state,
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=1,
        updated_at=1,
    )
    store.upsert_runner(
        project, container_id=cid, endpoint=endpoint, token="tok", tier="hot"
    )
    if last_seen_at is not None:
        store.touch_runner_seen(project, now=last_seen_at)


def _container(
    *, cid: str = "cid-1", project: str = PROJECT, running: bool = True, started: int = 1
) -> dict[str, Any]:
    return {"id": cid, "project": project, "started_at": started, "running": running}


def _pass(
    store: Store,
    containers: list[dict[str, Any]],
    *,
    now: int = 1_000_000,
    probe: Any = None,
    removed: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    gone: list[str] = removed if removed is not None else []
    kw: dict[str, Any] = {}
    if probe is not None:
        kw["probe_runner"] = probe
    rec = Reconciler(
        store,
        list_containers=lambda: list(containers),
        remove_container=gone.append,
        now_fn=lambda: now,
        **kw,
    )
    return rec.reconcile_once(dry_run=dry_run)


STALE = 4_242


# (1) + (2) --------------------------------------------------------------


def test_1_2_uninitialised_is_unattached_after_the_same_fixture_proves_reachable(
    tmp_path: Path, stub: Any
) -> None:
    """(2) first: the very same fixture must probe *attached* before (1) means anything.

    Without the positive leg, "unreachable" is indistinguishable from a stub
    that was never listening — which is the failure this repository has shipped
    more than once, and the reason CLAUDE.md carries the rule.
    """
    store = Store(tmp_path / "state.db")
    _seed(store, endpoint=f"127.0.0.1:{stub.port}", last_seen_at=STALE)
    containers = [_container()]

    # (2) positive leg: reachable-shaped, and the stamp advances.
    stub.initialized = True
    rep = _pass(store, containers)
    assert [e["reason"] for e in rep["attached"]] == ["attached"]
    assert rep["unattached"] == []
    assert store.get_runner(PROJECT)["last_seen_at"] > STALE
    # the RSS sample §12.1 has no other source for
    assert rep["attached"][0]["rss_bytes"] == 246_000_000
    assert rep["attached"][0]["cli_rss_kb"] == 70_000

    # (1) negative leg: same socket, same row, only `initialized` flips.
    store.touch_runner_seen(PROJECT, now=STALE)
    stub.initialized = False
    removed: list[str] = []
    rep = _pass(store, containers, removed=removed)

    assert [(e["project"], e["reason"]) for e in rep["unattached"]] == [
        (PROJECT, "uninitialised")
    ]
    assert rep["attached"] == []
    row = store.get_runner(PROJECT)
    assert row["tier"] == "hot", "the probe must never move tier (ADR-34 decision 1)"
    assert row["last_seen_at"] == STALE, "no stamp on a failed probe"
    assert removed == [], "an unreachable runner is not evidence of an orphan"
    store.close()


# (3) --------------------------------------------------------------------


def test_3_stopped_container_survives_the_pass_and_ensure_session_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass that has already seen a stopped container must not break ADR-25."""
    import test_lazy_promotion as lp  # sibling harness: fake docker + fake RPC

    store = Store(tmp_path / "state.db")
    _seed(store, project="o/r", cid="cid-keep", last_seen_at=STALE)

    removed: list[str] = []
    raised: list[Any] = []

    def _never(row: Any) -> ProbeResult:  # pragma: no cover - must not run
        raised.append(row)
        raise AssertionError("stopped must be reached without a socket")

    rep = _pass(
        store,
        [_container(cid="cid-keep", running=False)],
        probe=_never,
        removed=removed,
    )
    assert [(e["project"], e["reason"]) for e in rep["unattached"]] == [
        ("o/r", "stopped")
    ]
    assert raised == []
    assert removed == []
    before = store.get_runner("o/r")
    assert before["tier"] == "hot" and before["container_id"] == "cid-keep"

    world = lp._DockerWorld(running=False)
    sup = lp.SessionSupervisor(store, lp._cfg(tmp_path))
    lp._wire_supervisor(sup, world, monkeypatch)
    handle = sup.ensure_session(session_key="o/r#1", repo="o/r", issue_num=1)

    assert handle.container_id == "cid-keep", "ADR-25 still repairs it lazily"
    assert world.removed == []
    store.close()


# (4) --------------------------------------------------------------------


def test_4_last_seen_at_advances_only_on_success_against_a_planted_stale_value(
    tmp_path: Path,
) -> None:
    """A NULL or freshly-upserted row would pass "is recent" with no probe at all."""
    store = Store(tmp_path / "state.db")
    _seed(store, last_seen_at=STALE)
    assert store.get_runner(PROJECT)["last_seen_at"] == STALE

    _pass(
        store,
        [_container()],
        probe=lambda row: ProbeResult(False, "unreachable"),
        now=999_000,
    )
    assert store.get_runner(PROJECT)["last_seen_at"] == STALE

    _pass(
        store,
        [_container()],
        probe=lambda row: ProbeResult(True, "attached", {"rss_bytes": 1}),
        now=999_000,
    )
    assert store.get_runner(PROJECT)["last_seen_at"] == 999_000
    store.close()


# (5) --------------------------------------------------------------------


def test_5_one_projects_failure_does_not_abort_the_pass(
    tmp_path: Path, stub: Any
) -> None:
    """Two projects, one refusing connections; the other is still probed and stamped."""
    store = Store(tmp_path / "state.db")
    _seed(store, project="o/good", cid="cid-good", endpoint=f"127.0.0.1:{stub.port}",
          last_seen_at=STALE)
    _seed(store, project="o/bad", issue=2, cid="cid-bad",
          endpoint=f"127.0.0.1:{_closed_port()}", last_seen_at=STALE)

    rep = _pass(
        store,
        [_container(cid="cid-good", project="o/good"),
         _container(cid="cid-bad", project="o/bad")],
        now=999_000,
    )

    assert [e["project"] for e in rep["attached"]] == ["o/good"]
    assert [(e["project"], e["reason"]) for e in rep["unattached"]] == [
        ("o/bad", "unreachable")
    ]
    assert store.get_runner("o/good")["last_seen_at"] == 999_000
    assert store.get_runner("o/bad")["last_seen_at"] == STALE
    store.close()


def test_5b_a_probe_that_raises_does_not_abort_the_pass(tmp_path: Path) -> None:
    """(5)'s other arm — the only one that could actually abort a pass.

    A seam that *returns* a failed `ProbeResult` never reaches the `except`, so
    test_5 does not cover this branch despite looking like it does. Unreachable
    with the default probe, which raises nothing; this guards an injected seam.
    """
    store = Store(tmp_path / "state.db")
    _seed(store, project="o/boom", issue=1, cid="cid-boom", last_seen_at=STALE)
    _seed(store, project="o/fine", issue=2, cid="cid-fine", last_seen_at=STALE)

    def _seam(row: dict[str, Any]) -> ProbeResult:
        if str(row["project_key"]) == "o/boom":
            raise RuntimeError("seam blew up")
        return ProbeResult(True, "attached", {"rss_bytes": 1})

    rep = _pass(
        store,
        [_container(cid="cid-boom", project="o/boom"),
         _container(cid="cid-fine", project="o/fine")],
        probe=_seam,
        now=999_000,
    )

    assert [(e["project"], e["reason"]) for e in rep["unattached"]] == [
        ("o/boom", "probe_error")
    ]
    assert [e["project"] for e in rep["attached"]] == ["o/fine"]
    assert store.get_runner("o/boom")["last_seen_at"] == STALE
    assert store.get_runner("o/boom")["tier"] == "hot"
    assert store.get_runner("o/fine")["last_seen_at"] == 999_000
    store.close()


# (6) --------------------------------------------------------------------


def test_6_project_with_an_in_flight_turn_is_skipped(tmp_path: Path) -> None:
    """The turn *is* the attachment evidence; a busy runner can miss a 2 s timeout."""
    store = Store(tmp_path / "state.db")
    _seed(store, last_seen_at=STALE)
    store.insert_turn(
        turn_id="t-inflight",
        session_key=f"{PROJECT}#1",
        role="developer",
        delivery_id="d1",
        started_at=999_000,
        ended_at=None,
        status="running",
        summary=None,
    )

    called: list[Any] = []

    def _raises(row: Any) -> ProbeResult:
        called.append(row)
        raise AssertionError("in-flight project must not be probed")

    rep = _pass(store, [_container()], probe=_raises, now=999_010)

    assert called == [], "the seam must never be reached, not merely tolerated"
    assert [(e["project"], e["reason"]) for e in rep["probe_skipped"]] == [
        (PROJECT, "open turn")
    ]
    assert rep["attached"] == [] and rep["unattached"] == []
    assert store.get_runner(PROJECT)["last_seen_at"] == STALE
    store.close()


# (7) --------------------------------------------------------------------


def test_7_the_reconciler_writes_tier_on_no_path(tmp_path: Path) -> None:
    """Decision (1) in full: no probe outcome may reach the column admission counts."""
    import inspect

    import agentd.reconciler as rc

    src = inspect.getsource(rc)
    assert "upsert_runner" not in src, "the stamp is a bare UPDATE, never an upsert"
    assert 'tier="cold"' not in src and "tier='cold'" not in src

    store = Store(tmp_path / "state.db")
    outcomes = {
        "o/p0": ProbeResult(True, "attached", {}),
        "o/p1": ProbeResult(False, "uninitialised"),
        "o/p2": ProbeResult(False, "unreachable"),
        "o/p3": ProbeResult(False, "no_endpoint"),
    }
    # One pass over the whole fleet: a pass that lists only one container
    # deletes every other `runners` row as stale, which would make the count
    # below pass for a reason that has nothing to do with the probe.
    for i, pk in enumerate(outcomes):
        _seed(store, project=pk, issue=i + 1, cid=f"cid-{i}")
    _seed(store, project="o/stopped", issue=99, cid="cid-stopped")
    containers = [
        _container(cid=f"cid-{i}", project=pk) for i, pk in enumerate(outcomes)
    ]
    # the stopped path never reaches a ProbeResult at all
    containers.append(
        _container(cid="cid-stopped", project="o/stopped", running=False)
    )
    before = store.count_hot_sessions()
    assert before == 5

    _pass(store, containers, probe=lambda row: outcomes[str(row["project_key"])])

    for pk in [*outcomes, "o/stopped"]:
        assert store.get_runner(pk)["tier"] == "hot"
    assert store.count_hot_sessions() == before
    store.close()


# (8) --------------------------------------------------------------------


def test_8_gc_sweep_removes_nothing_and_separates_live_cold_from_unowned(
    tmp_path: Path,
) -> None:
    """§12.3 as amended: a stopped container owned by a live project is state, not debris."""
    import test_gc as tg  # for the Config shape the GC expects

    store = Store(tmp_path / "state.db")
    _seed(store, project="o/live", cid="cid-live")

    containers = [
        {"id": "cid-live", "name": "agentd-o-live", "running": False,
         "labels": {"agentd.managed": "true", "agentd.project": "o/live"}},
        {"id": "cid-orphan", "name": "agentd-o-gone", "running": False,
         "labels": {"agentd.managed": "true", "agentd.project": "o/gone"}},
        {"id": "cid-other", "name": "not-ours", "running": True, "labels": {}},
    ]
    gc = GarbageCollector(
        store,
        tg._cfg(tmp_path),
        list_containers=lambda: list(containers),
        now_fn=lambda: 1_000_000,
    )
    rep = gc.collect_once(dry_run=True)

    by_id = {c["id"]: c for c in rep["containers"]}
    assert "cid-other" not in by_id, "unmanaged containers are never reported"
    assert by_id["cid-live"]["owner"] == "live-cold"
    assert by_id["cid-live"]["project"] == "o/live"
    assert by_id["cid-orphan"]["owner"] == "unowned"
    assert not rep.get("removed")
    store.close()


# (9) --------------------------------------------------------------------


def test_9_whole_fleet_reboot_leaves_every_tier_hot_in_both_shapes(
    tmp_path: Path, stub: Any
) -> None:
    """`--restart unless-stopped` returns the fleet *running*. Both shapes it produces.

    Shape A is every runner answering `initialized: false`. Shape B is every
    runner running and initialised but answering at a port `runners.endpoint`
    no longer names — ADR-25's planted-wrong-port case, which fails at connect
    with no reply at all, and which shape A cannot reach.
    """
    store = Store(tmp_path / "state.db")
    fleet = [f"o/p{i}" for i in range(4)]
    for i, pk in enumerate(fleet):
        _seed(store, project=pk, issue=i + 1, cid=f"cid-{i}",
              endpoint=f"127.0.0.1:{stub.port}", last_seen_at=STALE)
    containers = [_container(cid=f"cid-{i}", project=pk) for i, pk in enumerate(fleet)]
    before = store.count_hot_sessions()
    assert before == 4

    # Fixture proof: this fleet is reachable-shaped right now.
    stub.initialized = True
    rep = _pass(store, containers, now=999_000)
    assert len(rep["attached"]) == 4 and rep["unattached"] == []

    # Shape A — running, answering, not initialised.
    for pk in fleet:
        store.touch_runner_seen(pk, now=STALE)
    stub.initialized = False
    rep = _pass(store, containers, now=999_100)
    assert {e["reason"] for e in rep["unattached"]} == {"uninitialised"}
    assert len(rep["unattached"]) == 4
    assert all(store.get_runner(pk)["tier"] == "hot" for pk in fleet)
    assert all(store.get_runner(pk)["last_seen_at"] == STALE for pk in fleet)
    assert store.count_hot_sessions() == before

    # Shape B — running and initialised, but the stored endpoint is stale.
    stub.initialized = True
    dead = _closed_port()
    for i, pk in enumerate(fleet):
        store.upsert_runner(pk, container_id=f"cid-{i}",
                            endpoint=f"127.0.0.1:{dead}", token="tok", tier="hot")
        store.touch_runner_seen(pk, now=STALE)
    rep = _pass(store, containers, now=999_200)
    assert {e["reason"] for e in rep["unattached"]} == {"unreachable"}
    assert len(rep["unattached"]) == 4
    assert all(store.get_runner(pk)["tier"] == "hot" for pk in fleet)
    assert all(store.get_runner(pk)["last_seen_at"] == STALE for pk in fleet)
    assert store.count_hot_sessions() == before
    store.close()


# (10) -------------------------------------------------------------------


def test_10_one_shared_predicate_two_callers_one_ping_per_project_per_pass(
    tmp_path: Path, stub: Any
) -> None:
    """A shared boolean plus a private second ping is the failure this item catches."""
    import inspect
    import re

    import agentd.reconciler as rc
    import agentd.session_loop as dl

    # exactly two callers, and neither has a probe of its own
    call_sites = [
        m
        for mod in (dl, rc)
        for m in re.findall(r"probe_runner\(", inspect.getsource(mod))
    ]
    assert len(call_sites) == 2, call_sites
    assert "probe_runner(runner, client_cls=RunnerClient).serviceable" in inspect.getsource(
        dl.SessionLoop._runner_reachable
    )
    assert "health.ping" not in inspect.getsource(dl.SessionLoop._runner_reachable)

    # one project, two containers claiming it: still one ping
    store = Store(tmp_path / "state.db")
    _seed(store, cid="cid-1", endpoint=f"127.0.0.1:{stub.port}")
    stub.initialized = True
    stub.pings = 0
    rep = _pass(
        store,
        [_container(cid="cid-1"), _container(cid="cid-2", started=2)],
        now=999_000,
    )
    assert stub.pings == 1, "one probe per project per pass, not per container"
    assert len(rep["attached"]) == 1

    # the SessionLoop caller reaches the same predicate
    stub.pings = 0
    assert probe_runner(store.get_runner(PROJECT)).serviceable is True
    assert stub.pings == 1
    store.close()


# (11) -------------------------------------------------------------------


def test_11_each_cause_has_its_own_reason_and_stopped_never_connects(
    tmp_path: Path, stub: Any
) -> None:
    """Four causes, four reasons. `stopped` must not arrive by relabelling a connect error."""
    store = Store(tmp_path / "state.db")
    dead = _closed_port()
    stub.initialized = False

    _seed(store, project="o/stopped", issue=1, cid="c-stop",
          endpoint=f"127.0.0.1:{stub.port}")
    _seed(store, project="o/noaddr", issue=2, cid="c-addr", endpoint="")
    _seed(store, project="o/unreach", issue=3, cid="c-unreach",
          endpoint=f"127.0.0.1:{dead}")
    _seed(store, project="o/uninit", issue=4, cid="c-uninit",
          endpoint=f"127.0.0.1:{stub.port}")

    probed: list[str] = []

    def _recording(row: dict[str, Any]) -> ProbeResult:
        probed.append(str(row.get("project_key") or ""))
        return probe_runner(row)

    rep = _pass(
        store,
        [
            _container(cid="c-stop", project="o/stopped", running=False),
            _container(cid="c-addr", project="o/noaddr"),
            _container(cid="c-unreach", project="o/unreach"),
            _container(cid="c-uninit", project="o/uninit"),
        ],
        probe=_recording,
        now=999_000,
    )

    assert {e["project"]: e["reason"] for e in rep["unattached"]} == {
        "o/stopped": "stopped",
        "o/noaddr": "no_endpoint",
        "o/unreach": "unreachable",
        "o/uninit": "uninitialised",
    }
    assert "o/stopped" not in probed, "no socket for a container we know is stopped"
    assert set(probed) == {"o/noaddr", "o/unreach", "o/uninit"}
    store.close()


# (12) -------------------------------------------------------------------


def test_12_wrong_bearer_is_unauthorized_not_unreachable(
    tmp_path: Path, stub: Any
) -> None:
    """The one cause lazy promotion does not repair — it recreates instead.

    `promote_hot` reuses `runners.token` as the bearer, so a wrong-token row
    fails its ping again inside `_ensure_session_locked` and the adopt branch
    runs `docker rm -f` on a healthy, initialised runner. Labelling it
    `unreachable` points the reader at the port story; the repair is the row.
    """
    store = Store(tmp_path / "state.db")
    stub.initialized = True

    # (2)'s rule: the same runner must be shown serviceable first, or
    # "unauthorized" is indistinguishable from a stub that never listened.
    _seed(store, endpoint=f"127.0.0.1:{stub.port}", last_seen_at=STALE)
    rep = _pass(store, [_container()], now=999_000)
    assert [e["reason"] for e in rep["attached"]] == ["attached"]
    assert store.get_runner(PROJECT)["last_seen_at"] == 999_000

    # same live, initialised runner — only the stored token is wrong
    store.upsert_runner(
        PROJECT,
        container_id="cid-1",
        endpoint=f"127.0.0.1:{stub.port}",
        token="not-the-bearer",
        tier="hot",
    )
    store.touch_runner_seen(PROJECT, now=STALE)
    removed: list[str] = []
    rep = _pass(store, [_container()], now=999_100, removed=removed)

    assert [(e["project"], e["reason"]) for e in rep["unattached"]] == [
        (PROJECT, "unauthorized")
    ]
    row = store.get_runner(PROJECT)
    assert row["tier"] == "hot"
    assert row["last_seen_at"] == STALE
    assert removed == []
    store.close()


def test_12b_only_minus_32001_is_unauthorized(tmp_path: Path, stub: Any) -> None:
    """Keyed on the typed code, never the message (#35)."""
    from agentd.rpc_client import RpcError, RunnerClient

    class _Erroring(RunnerClient):
        code = -32603

        def connect(self) -> None:
            raise RpcError(type(self).code, "boom")

    row = {"endpoint": f"127.0.0.1:{stub.port}", "token": "tok"}
    _Erroring.code = -32001
    assert probe_runner(row, client_cls=_Erroring).reason == "unauthorized"
    _Erroring.code = -32603
    assert probe_runner(row, client_cls=_Erroring).reason == "unreachable"
