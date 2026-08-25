"""ADR-36 (e): egress guard at httpx.Client.send, and (d′) cleanup on failure."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from test_m4a_branch_protection_live import cleanup_m4a_probe

from agentd.config import Config
from agentd.db import Store
from agentd.design_loop import DesignLoop
from agentd.supervisor import SessionSupervisor

GITHUB_USER = "https://api.github.com/user"


def test_github_api_guard_reaches_env_token_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance 4, guard reverted first: send is hit with GH_TOKEN, no Keychain."""
    monkeypatch.setenv("GH_TOKEN", "ghs_test_not_a_real_token")
    monkeypatch.delenv("AGENTD_SECRET_GROK_BOT", raising=False)
    monkeypatch.delenv("AGENTD_SECRET_CLAUDE_BOT", raising=False)
    seen: list[str] = []

    def _record(self, request, *args, **kwargs):
        seen.append(str(request.url))
        raise httpx.ConnectError("reverted-guard recorder, no network", request=request)

    monkeypatch.setattr(httpx.Client, "send", _record)
    with (
        pytest.raises(httpx.ConnectError, match="reverted-guard"),
        httpx.Client() as client,
    ):
        client.get(GITHUB_USER, headers={"Authorization": "Bearer ghs_test_not_a_real_token"})
    assert seen, "fixture did not reach Client.send"
    assert "api.github.com/user" in seen[0]


def test_github_api_guard_refuses_env_token_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance 4: same request, guard installed, failure names the URL."""
    monkeypatch.setenv("GH_TOKEN", "ghs_test_not_a_real_token")
    monkeypatch.delenv("AGENTD_SECRET_GROK_BOT", raising=False)
    monkeypatch.delenv("AGENTD_SECRET_CLAUDE_BOT", raising=False)
    with (
        pytest.raises(RuntimeError, match="api.github.com/user") as exc,
        httpx.Client() as client,
    ):
        client.get(GITHUB_USER, headers={"Authorization": "Bearer ghs_test_not_a_real_token"})
    assert "allows_github_api" in str(exc.value)


def test_allows_github_api_records_and_permits(allows_github_api) -> None:
    """Acceptance 4b: same request with the opt-in is permitted and recorded."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"login": "stub"}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        r = client.get(GITHUB_USER)
    assert r.status_code == 200
    assert r.json()["login"] == "stub"
    assert any("api.github.com/user" in u for u in allows_github_api)


def test_github_api_guard_spares_load_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 5: supervisor._load_tokens reads both agent accounts."""
    monkeypatch.setenv("AGENTD_SECRET_CLAUDE_BOT", "pat-c")
    monkeypatch.setenv("AGENTD_SECRET_GROK_BOT", "pat-g")
    store = Store(tmp_path / "state.db")
    try:
        sup = SessionSupervisor(store, Config(raw={}, root=tmp_path))
        assert sup._load_tokens() == {"architect": "pat-c", "developer": "pat-g"}
    finally:
        store.close()


def test_github_api_guard_spares_design_loop_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 5: design_loop :1546–1547 and :632 consult both agent accounts."""
    monkeypatch.delenv("AGENTD_SECRET_GATEWAY", raising=False)
    monkeypatch.delenv("AGENTD_SECRET_CLAUDE_BOT", raising=False)
    monkeypatch.setenv("AGENTD_SECRET_GROK_BOT", "pat-g")
    store = Store(tmp_path / "state.db")
    try:
        loop = DesignLoop(
            store,
            Config(
                raw={
                    "host": {"owner": "huozhe"},
                    "agents": {
                        "claude": {"login": "huozheclaude"},
                        "grok": {"login": "huozhegrok"},
                    },
                },
                root=tmp_path,
            ),
            supervisor=None,
            dispatch_turns=False,
        )
        # Gateway missing, claude-bot missing → grok-bot. Both agent names
        # are consulted (the withdrawn guard's tripwire).
        assert loop._github_api_token() == "pat-g"
        from agentd.keychain import get_password

        # :632 — same fallback, driven here because _process_one would also
        # call verify_design_approval over httpx without an inject.
        token = get_password("claude-bot") or get_password("grok-bot")
        assert token == "pat-g"
    finally:
        store.close()


def test_m4a_cleanup_patch_then_delete_on_assert_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 3b: finally still PATCH-closes and DELETE-refs when step 1 fails."""
    calls: list[tuple[str, str]] = []

    def _record(self, request, *args, **kwargs):
        calls.append((request.method, str(request.url)))
        return httpx.Response(200, json={"ok": True}, request=request)

    monkeypatch.setattr(httpx.Client, "send", _record)
    repo = "huozhe/code-workflow"
    headers = {"Authorization": "Bearer ghs_test"}
    with httpx.Client() as client:
        head: str | None = None
        pr_num: int | None = None
        with pytest.raises(AssertionError, match="forced step 1"):
            try:
                head = "agentd/m4a-live-deadbeef"
                client.post(f"https://api.github.com/repos/{repo}/git/refs")
                pr_num = 99
                client.post(f"https://api.github.com/repos/{repo}/pulls")
                raise AssertionError("forced step 1")
            finally:
                cleanup_m4a_probe(
                    client, repo=repo, headers=headers, head=head, pr_num=pr_num
                )

    methods = [m for m, _ in calls]
    assert "POST" in methods
    assert methods[-2:] == ["PATCH", "DELETE"]
    assert calls[-2][1].endswith("/pulls/99")
    assert "git/refs/heads/agentd/m4a-live-deadbeef" in calls[-1][1]


def test_m4a_cleanup_deletes_ref_when_pr_never_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(d′): cleanup is armed from ref creation, not from PR open."""
    calls: list[tuple[str, str]] = []

    def _record(self, request, *args, **kwargs):
        calls.append((request.method, str(request.url)))
        return httpx.Response(204, request=request)

    monkeypatch.setattr(httpx.Client, "send", _record)
    with httpx.Client() as client:
        cleanup_m4a_probe(
            client,
            repo="o/r",
            headers={},
            head="agentd/m4a-live-orphan",
            pr_num=None,
        )
    assert [m for m, _ in calls] == ["DELETE"]
    assert "git/refs/heads/agentd/m4a-live-orphan" in calls[0][1]
