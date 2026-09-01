"""#248 / ADR-41: agentctl sessions --live, and the bare-sessions characterization."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from agentctl.__main__ import main
from agentd import fsm
from agentd.db import Store

REPO = "huozhe/code-workflow"
NOW = 1_756_700_000

# RFC §1 five-row fixture.
_FIVE = (
    (f"{REPO}#900", "INTAKE", None, None, 0, 0),
    (f"{REPO}#248", "IMPLEMENTING", 249, None, 7, 0),
    (f"{REPO}#210", "PAUSED_HUMAN", 211, 212, 12, 3),
    (f"{REPO}#159", "TEARDOWN", 160, 161, 40, 0),
    (f"{REPO}#57", "CLOSED", 58, 59, 99, 0),
)


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state.db")


def _seed_row(
    store: Store,
    key: str,
    state: str,
    dpr: int | None,
    fpr: int | None,
    tc: int,
    sil: int,
    *,
    updated_at: int = NOW,
) -> None:
    store.upsert_session(
        session_key=key,
        repo=REPO,
        issue_num=int(key.rsplit("#", 1)[1]),
        state=state,
        architect="huozheclaude",
        developer="huozhegrok",
        created_at=NOW,
        updated_at=updated_at,
        design_pr=dpr,
        turn_count=tc,
    )
    store.update_session_fields(
        key, feature_pr=fpr, silent_turns=sil, updated_at=updated_at
    )


def _seed_five(store: Store, *, updated_at: int = NOW) -> None:
    for key, state, dpr, fpr, tc, sil in _FIVE:
        _seed_row(store, key, state, dpr, fpr, tc, sil, updated_at=updated_at)


def _run_live(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    main(["sessions", "--live"])
    cap = capsys.readouterr()
    return cap.out, cap.err


def test_live_omits_closed_and_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance (1): exact printed set against the five-row fixture."""
    monkeypatch.setenv("AGENTD_ROOT", str(tmp_path))
    store = _store(tmp_path)
    _seed_five(store)
    store.close()

    out, err = _run_live(capsys)
    assert err == ""
    lines = out.splitlines()
    keys = [line.split()[0] for line in lines]
    assert keys == [f"{REPO}#210", f"{REPO}#248", f"{REPO}#900"]
    joined = "\n".join(lines)
    assert f"{REPO}#159" not in joined
    assert f"{REPO}#57" not in joined
    assert "CLOSED" not in joined
    assert "TEARDOWN" not in joined
    assert "PAUSED_HUMAN" in joined
    assert "IMPLEMENTING" in joined
    assert "INTAKE" in joined


def test_live_set_comes_from_list_nonterminal_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance (2): substitution, not a set-equality that a parallel filter would pass."""
    monkeypatch.setenv("AGENTD_ROOT", str(tmp_path))
    store = _store(tmp_path)
    _seed_five(store)
    closed = store.get_session(f"{REPO}#57")
    assert closed is not None
    assert closed["state"] == "CLOSED"
    store.close()

    def _closed_only(self: Store) -> list[dict]:
        return [
            {
                "session_key": f"{REPO}#57",
                "state": "CLOSED",
            }
        ]

    monkeypatch.setattr(Store, "list_nonterminal_sessions", _closed_only)
    out, err = _run_live(capsys)
    assert err == ""
    assert f"{REPO}#57" in out
    assert "CLOSED" in out
    assert f"{REPO}#248" not in out


def test_bare_sessions_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance (3): pin the JSON; distinct updated_at so order is not SQLite's choice."""
    monkeypatch.setenv("AGENTD_ROOT", str(tmp_path))
    store = _store(tmp_path)
    for i, (key, state, dpr, fpr, tc, sil) in enumerate(_FIVE):
        _seed_row(store, key, state, dpr, fpr, tc, sil, updated_at=NOW + i)
    store.upsert_runner(
        REPO,
        container_id="cid-secret",
        endpoint="http://127.0.0.1:9",
        token="SECRET-TOKEN-LEAK",
        tier="hot",
    )
    expected_rows = []
    for r in store.list_sessions():
        full = store.get_session(str(r["session_key"])) or r
        full.pop("runner_token", None)
        full.pop("token", None)
        expected_rows.append(full)
    store.close()
    expected = json.dumps({"sessions": expected_rows}, indent=2) + "\n"

    main(["sessions"])
    out = capsys.readouterr().out
    assert out == expected
    assert "SECRET-TOKEN-LEAK" not in out
    assert "runner_token" not in out


def test_live_empty_prints_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance (4): empty table, no header, exit 0, nothing on stderr."""
    monkeypatch.setenv("AGENTD_ROOT", str(tmp_path))
    main(["sessions", "--live"])
    cap = capsys.readouterr()
    assert cap.out == ""
    assert cap.err == ""
    assert "session" not in cap.out.lower()
    assert "None" not in cap.out


def test_live_no_live_sessions_prints_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue acceptance: a DB with no live sessions — not an empty table.

    CLOSED + TEARDOWN is the runbook's second-line scenario: --live is all-clear
    while grep still finds TEARDOWN.
    """
    monkeypatch.setenv("AGENTD_ROOT", str(tmp_path))
    store = _store(tmp_path)
    _seed_row(store, f"{REPO}#57", "CLOSED", 58, 59, 99, 0)
    _seed_row(store, f"{REPO}#159", "TEARDOWN", 160, 161, 40, 0)
    assert store.list_nonterminal_sessions() == []
    store.close()

    out, err = _run_live(capsys)
    assert out == ""
    assert err == ""
    assert "None" not in out

    main(["sessions"])
    bare = capsys.readouterr().out
    assert '"state": "TEARDOWN"' in bare
    assert '"state": "CLOSED"' in bare


def test_live_ascii_with_null_prs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance (5): PYTHONIOENCODING=ascii / ascii stdout, NULL PRs, exit 0."""
    monkeypatch.setenv("AGENTD_ROOT", str(tmp_path))
    store = _store(tmp_path)
    _seed_row(store, f"{REPO}#900", "INTAKE", None, None, 0, 0)
    store.close()

    buf = io.BytesIO()
    wrapper = io.TextIOWrapper(buf, encoding="ascii", errors="strict", newline="\n")
    monkeypatch.setattr(sys, "stdout", wrapper)
    main(["sessions", "--live"])
    wrapper.flush()
    text = buf.getvalue().decode("ascii")
    assert "feature_pr=-" in text
    assert "design_pr=-" in text
    assert "\u2014" not in text
    assert "—" not in text


def test_live_order_under_ties_then_bump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance (6): equal updated_at → session_key ASC; a bump moves to the top."""
    monkeypatch.setenv("AGENTD_ROOT", str(tmp_path))
    store = _store(tmp_path)
    _seed_five(store, updated_at=NOW)
    store.close()

    out, err = _run_live(capsys)
    assert err == ""
    assert [line.split()[0] for line in out.splitlines()] == [
        f"{REPO}#210",
        f"{REPO}#248",
        f"{REPO}#900",
    ]

    store = _store(tmp_path)
    store.update_session_fields(f"{REPO}#248", updated_at=NOW + 10)
    store.close()
    out, err = _run_live(capsys)
    assert err == ""
    assert [line.split()[0] for line in out.splitlines()] == [
        f"{REPO}#248",
        f"{REPO}#210",
        f"{REPO}#900",
    ]


def test_live_columns_are_right_padded_not_right_aligned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance (7): full rendered line, keys of different widths. rjust would indent #9."""
    monkeypatch.setenv("AGENTD_ROOT", str(tmp_path))
    store = _store(tmp_path)
    _seed_row(store, f"{REPO}#248", "IMPLEMENTING", 249, None, 7, 0)
    _seed_row(store, f"{REPO}#210", "AWAITING_VERIFICATION", 211, 212, 12, 3)
    _seed_row(store, f"{REPO}#9", "INTAKE", None, None, 0, 0)
    store.close()

    out, err = _run_live(capsys)
    assert err == ""
    expected = (
        f"{REPO}#210  AWAITING_VERIFICATION  turns=12  silent=3  design_pr=211  feature_pr=212\n"
        f"{REPO}#248  IMPLEMENTING           turns=7  silent=0  design_pr=249  feature_pr=-\n"
        f"{REPO}#9    INTAKE                 turns=0  silent=0  design_pr=-  feature_pr=-\n"
    )
    assert out == expected
    # A rjust implementation indents the short key; this would pass a field-set check.
    assert not out.splitlines()[-1].startswith(" ")


def test_terminal_definitions_agree(tmp_path: Path) -> None:
    """Acceptance (8): fsm.TERMINAL_STATES and the sweep's SQL literal name the same set."""
    assert fsm.TERMINAL_STATES == {"CLOSED", "TEARDOWN"}
    store = _store(tmp_path)
    for i, state in enumerate(sorted(fsm.SESSION_STATES)):
        _seed_row(
            store,
            f"{REPO}#{1000 + i}",
            state,
            None,
            None,
            0,
            0,
            updated_at=NOW,
        )
    nt_states = {str(r["state"]) for r in store.list_nonterminal_sessions()}
    store.close()
    assert nt_states == set(fsm.SESSION_STATES) - set(fsm.TERMINAL_STATES)
    assert nt_states.isdisjoint(fsm.TERMINAL_STATES)
    assert len(nt_states) == len(fsm.SESSION_STATES) - 2


def test_live_does_not_print_runner_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTD_ROOT", str(tmp_path))
    store = _store(tmp_path)
    _seed_row(store, f"{REPO}#248", "IMPLEMENTING", 249, None, 7, 0)
    store.upsert_runner(
        REPO,
        container_id="cid-secret",
        endpoint="http://127.0.0.1:9",
        token="SECRET-TOKEN-LEAK",
        tier="hot",
    )
    assert store.get_session(f"{REPO}#248")["runner_token"] == "SECRET-TOKEN-LEAK"
    store.close()

    out, err = _run_live(capsys)
    assert err == ""
    assert "SECRET-TOKEN-LEAK" not in out
    assert "runner_token" not in out
    assert f"{REPO}#248" in out
