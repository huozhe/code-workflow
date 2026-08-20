"""#92 / ADR-9 v2: per-adapter model + reasoning effort.

Covers the four places the value has to survive: config, the spawn command,
the cached-session respawn, and the archive manifest.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentd.archive import build_manifest
from agentd.config import Config

_RUNNER_ROOT = Path(__file__).resolve().parents[1] / "docker" / "session-runner"
sys.path.insert(0, str(_RUNNER_ROOT))

from agentd_runner import cli_session  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    cli_session.shutdown_all()
    yield
    cli_session.shutdown_all()


def _cfg(agents: dict) -> Config:
    cfg = Config.__new__(Config)
    cfg.raw = {"agents": agents}
    return cfg


# ---------------------------------------------------------------- config


def test_agent_models_reads_model_and_effort() -> None:
    cfg = _cfg(
        {
            "claude": {"login": "a", "model": "opus", "reasoning_effort": "high"},
            "grok": {"login": "b", "model": "grok-4.6", "reasoning_effort": "high"},
        }
    )
    assert cfg.agent_models() == {
        "claude": {"model": "opus", "reasoning_effort": "high"},
        "grok": {"model": "grok-4.6", "reasoning_effort": "high"},
    }


def test_agent_models_omits_agents_with_neither_key() -> None:
    """Absent ⇒ omitted ⇒ CLI default. This is the pre-#92 behaviour."""
    cfg = _cfg({"claude": {"login": "a"}, "grok": {"login": "b"}})
    assert cfg.agent_models() == {}


def test_agent_models_allows_effort_without_model() -> None:
    cfg = _cfg({"grok": {"login": "b", "reasoning_effort": "high"}})
    assert cfg.agent_models() == {"grok": {"reasoning_effort": "high"}}


def test_agent_models_ignores_blank_and_non_dict_entries() -> None:
    cfg = _cfg({"claude": {"model": "   "}, "grok": "not-a-dict"})
    assert cfg.agent_models() == {}


# ------------------------------------------------------------ spawn flags


def _sess(adapter: str, **kw) -> cli_session.LiveCliSession:
    return cli_session.LiveCliSession(
        role="architect",
        adapter=adapter,
        uid=1001,
        home=Path("/tmp/h"),
        tmp=Path("/tmp/t"),
        xdg=Path("/tmp/x"),
        spawn_cwd=Path("/tmp"),
        **kw,
    )


def test_claude_cmd_appends_model_and_effort() -> None:
    cmd = _sess("claude", model="opus", reasoning_effort="high")._claude_cmd(False)
    assert cmd[cmd.index("--model") + 1] == "opus"
    assert cmd[cmd.index("--effort") + 1] == "high"


def test_grok_cmd_puts_flags_before_the_stdio_subcommand() -> None:
    """Verified against grok 1.0.5: `grok agent stdio -m X` exits 2.

    -m / --reasoning-effort belong to `grok agent`, not to `stdio`. Appending
    them after the subcommand makes every grok turn fail with
    "error: unexpected argument '-m' found".
    """
    cmd = _sess("grok", model="grok-4.6", reasoning_effort="high")._grok_cmd()
    assert cmd[cmd.index("-m") + 1] == "grok-4.6"
    assert cmd[cmd.index("--reasoning-effort") + 1] == "high"
    assert cmd[-1] == "stdio"
    assert cmd.index("-m") < cmd.index("stdio")
    assert cmd.index("--reasoning-effort") < cmd.index("stdio")


def test_unknown_claude_effort_refuses_instead_of_defaulting() -> None:
    """claude warns and uses the default on a bad --effort; refuse before spawn."""
    with pytest.raises(ValueError, match="unknown claude reasoning_effort"):
        _sess("claude", reasoning_effort="NOTAVALUE")._claude_cmd(False)


def test_known_claude_efforts_all_accepted() -> None:
    for level in ("low", "medium", "high", "xhigh", "max"):
        cmd = _sess("claude", reasoning_effort=level)._claude_cmd(False)
        assert cmd[cmd.index("--effort") + 1] == level


def test_unset_model_leaves_command_unchanged() -> None:
    """No flags at all — not an empty string, which the CLI would reject."""
    claude = _sess("claude")._claude_cmd(False)
    grok = _sess("grok")._grok_cmd()
    assert "--model" not in claude and "--effort" not in claude
    assert "-m" not in grok and "--reasoning-effort" not in grok


def test_claude_cmd_keeps_continue_flag_with_model() -> None:
    cmd = _sess("claude", model="opus")._claude_cmd(True)
    assert "-c" in cmd and cmd[cmd.index("--model") + 1] == "opus"


# ------------------------------------------------- cached-session respawn


def test_model_change_replaces_cached_session() -> None:
    """#92: model is a spawn-time flag, so a change must not reuse the CLI."""
    kw = {
        "role": "architect",
        "adapter": "claude",
        "uid": 1001,
        "home": Path("/tmp/h"),
        "tmp": Path("/tmp/t"),
        "xdg": Path("/tmp/x"),
        "spawn_cwd": Path("/tmp"),
    }
    first = cli_session.get_or_create_session(model="opus", **kw)
    second = cli_session.get_or_create_session(model="sonnet", **kw)
    assert second is not first
    assert second.model == "sonnet"


def test_same_model_reuses_live_session() -> None:
    """An unchanged model must NOT respawn — that would reset the conversation."""
    kw = {
        "role": "developer",
        "adapter": "grok",
        "uid": 1002,
        "home": Path("/tmp/h"),
        "tmp": Path("/tmp/t"),
        "xdg": Path("/tmp/x"),
        "spawn_cwd": Path("/tmp"),
    }
    first = cli_session.get_or_create_session(model="grok-4.6", **kw)
    first.proc = MagicMock(poll=MagicMock(return_value=None))  # make it alive
    assert first.is_alive()
    second = cli_session.get_or_create_session(model="grok-4.6", **kw)
    assert second is first

    # ...and a changed effort on the same live session still forces a respawn.
    third = cli_session.get_or_create_session(
        model="grok-4.6", reasoning_effort="high", **kw
    )
    assert third is not first
    assert third.reasoning_effort == "high"


# ------------------------------------------------------------- the record


def test_manifest_records_models() -> None:
    m = build_manifest(
        session_key="huozhe/code-workflow#1",
        project_key="huozhe/code-workflow",
        terminal_state="VERIFIED",
        design_pr=1,
        feature_pr=2,
        turn_count=3,
        closed_at=1_700_000_000,
        models={"claude": {"model": "opus", "reasoning_effort": "high"}},
    )
    assert m["models"] == {"claude": {"model": "opus", "reasoning_effort": "high"}}


def test_manifest_models_defaults_to_empty_not_missing() -> None:
    """{} means "every CLI ran its own default", which is a real answer."""
    m = build_manifest(
        session_key="huozhe/code-workflow#1",
        project_key="huozhe/code-workflow",
        terminal_state="ABANDONED",
        design_pr=None,
        feature_pr=None,
        turn_count=0,
        closed_at=1_700_000_000,
    )
    assert m["models"] == {}
