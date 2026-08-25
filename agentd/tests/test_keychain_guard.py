"""#216: the Keychain arm of ``get_password`` must not fire in tests."""

from __future__ import annotations

import subprocess

import pytest

from agentd import keychain


def test_keychain_lookup_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback raises with a named reason, not a real ``security`` miss.

    Matching on ``#216`` is what makes this fail on Linux too. A real absent
    ``security`` raises ``FileNotFoundError`` as well, so asserting the type
    alone would pass in CI whether or not the guard is installed — the exact
    platform-dependence this guards against.
    """
    with pytest.raises(FileNotFoundError, match="#216"):
        keychain.subprocess.run(["security", "find-generic-password"])

    monkeypatch.delenv("AGENTD_SECRET_CLAUDE_BOT", raising=False)
    assert keychain.get_password("claude-bot") is None


def test_env_arm_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the Keychain fallback is refused; ``AGENTD_SECRET_*`` is untouched."""
    monkeypatch.setenv("AGENTD_SECRET_CLAUDE_BOT", "pat-c")
    assert keychain.get_password("claude-bot") == "pat-c"


def test_allows_keychain_restores_the_real_lookup(allows_keychain: bool) -> None:
    """The opt-in hands back the real module, or the live M4-A run cannot read its token."""
    assert keychain.subprocess is subprocess
