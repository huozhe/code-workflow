"""M4-A live (opt-in): assert ruleset + observe merge refuse/success on GitHub.

Run only with real tokens against a real repo:

    AGENTD_LIVE_M4A=1 \\
    AGENTD_LIVE_REPO=huozhe/code-workflow \\
    uv run --directory agentd pytest -q tests/test_m4a_branch_protection_live.py -s

Tokens:
  - Developer: GH_TOKEN or Keychain agentd/grok-bot (must be huozhegrok)
  - Architect: AGENTD_SECRET_CLAUDE_BOT or Keychain agentd/claude-bot

Creates a short-lived Feature-style PR, proves refusal without Architect approval,
proves Developer self-approve is rejected, then Architect approves and Developer
merges. Leaves a one-line stamp under docs/ops/ for the merge commit.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone

import httpx
import pytest

from agentd.keychain import get_password
from agentd.verify import (
    MERGE_RULE_REFUSAL_HTTP,
    SELF_APPROVE_REFUSAL_HTTP,
    verify_branch_pull_request_rules,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENTD_LIVE_M4A") != "1",
    reason="set AGENTD_LIVE_M4A=1 for live M4-A against GitHub",
)

API = "https://api.github.com"
UA = "agentd-m4a-live"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": UA,
    }


def _dev_token() -> str:
    tok = os.environ.get("GH_TOKEN") or get_password("grok-bot")
    if not tok:
        pytest.skip("no developer token (GH_TOKEN / grok-bot)")
    return tok


def _arch_token() -> str:
    tok = os.environ.get("AGENTD_SECRET_CLAUDE_BOT") or get_password("claude-bot")
    if not tok:
        pytest.skip("no architect token (claude-bot)")
    return tok


def _repo() -> str:
    return os.environ.get("AGENTD_LIVE_REPO") or "huozhe/code-workflow"


def test_m4a_assert_rules_then_observe_refuse_and_merge() -> None:
    dev = _dev_token()
    arch = _arch_token()
    repo = _repo()
    branch_base = "main"

    # --- Assert half (settings, no admin needed) ---
    rules = verify_branch_pull_request_rules(
        repo=repo, branch=branch_base, token=dev, min_approving_reviews=1
    )
    assert rules.ok, rules.reason
    assert (rules.required_approving_review_count or 0) >= 1
    # Live repo note (2026-08): require_last_push_approval true; no status checks rule.
    print("ASSERT rules:", rules.reason)

    with httpx.Client(timeout=30.0) as client:
        me = client.get(f"{API}/user", headers=_headers(dev)).json()
        assert me.get("login") == "huozhegrok", f"developer token is {me.get('login')!r}"

        main_ref = client.get(
            f"{API}/repos/{repo}/git/ref/heads/{branch_base}",
            headers=_headers(dev),
        )
        main_ref.raise_for_status()
        main_sha = main_ref.json()["object"]["sha"]

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        suffix = uuid.uuid4().hex[:8]
        head = f"agentd/m4a-live-{suffix}"
        path = "docs/ops/m4-a-live-stamp.txt"
        body = f"m4a live {stamp} {suffix}\n"

        # Branch from main
        r = client.post(
            f"{API}/repos/{repo}/git/refs",
            headers=_headers(dev),
            json={"ref": f"refs/heads/{head}", "sha": main_sha},
        )
        r.raise_for_status()

        blob = client.post(
            f"{API}/repos/{repo}/git/blobs",
            headers=_headers(dev),
            json={"content": body, "encoding": "utf-8"},
        )
        blob.raise_for_status()
        tree = client.post(
            f"{API}/repos/{repo}/git/trees",
            headers=_headers(dev),
            json={
                "base_tree": main_sha,
                "tree": [
                    {
                        "path": path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob.json()["sha"],
                    }
                ],
            },
        )
        tree.raise_for_status()
        commit = client.post(
            f"{API}/repos/{repo}/git/commits",
            headers=_headers(dev),
            json={
                "message": f"test(m4a): live stamp {suffix}",
                "tree": tree.json()["sha"],
                "parents": [main_sha],
            },
        )
        commit.raise_for_status()
        head_sha = commit.json()["sha"]
        client.patch(
            f"{API}/repos/{repo}/git/refs/heads/{head}",
            headers=_headers(dev),
            json={"sha": head_sha},
        ).raise_for_status()

        pr = client.post(
            f"{API}/repos/{repo}/pulls",
            headers=_headers(dev),
            json={
                "title": f"M4-A live probe {suffix}",
                "head": head,
                "base": branch_base,
                "body": (
                    "Automated M4-A (issue #51 M4-3). Assert rules + observe "
                    "Developer merge refuse, then Architect approve + Developer merge."
                ),
            },
        )
        pr.raise_for_status()
        pr_num = int(pr.json()["number"])
        print(f"OBSERVE pr=#{pr_num} head={head}")

        # Wait for mergeability computation
        mergeable_state = "unknown"
        for _ in range(15):
            time.sleep(1)
            pr_s = client.get(
                f"{API}/repos/{repo}/pulls/{pr_num}", headers=_headers(dev)
            )
            pr_s.raise_for_status()
            mergeable_state = str(pr_s.json().get("mergeable_state") or "unknown")
            if mergeable_state not in ("unknown", "unstable", ""):
                break
        print(f"mergeable_state before merge attempt: {mergeable_state}")

        # --- Observe half: refuse without Architect approval ---
        refuse = client.put(
            f"{API}/repos/{repo}/pulls/{pr_num}/merge",
            headers=_headers(dev),
            json={
                "commit_title": "should be refused",
                "merge_method": "squash",
            },
        )
        print("REFUSE status", refuse.status_code, refuse.text[:500])
        assert refuse.status_code == MERGE_RULE_REFUSAL_HTTP, refuse.text
        refuse_body = refuse.json()
        assert "rule" in (refuse_body.get("message") or "").lower()
        assert str(refuse_body.get("status") or "") == str(MERGE_RULE_REFUSAL_HTTP)

        # Self-approve as author must fail (FR-1.3 author cannot satisfy)
        self_rev = client.post(
            f"{API}/repos/{repo}/pulls/{pr_num}/reviews",
            headers=_headers(dev),
            json={"event": "APPROVE", "body": "self-approve must fail"},
        )
        print("SELF_APPROVE status", self_rev.status_code, self_rev.text[:400])
        assert self_rev.status_code == SELF_APPROVE_REFUSAL_HTTP, self_rev.text
        self_body = self_rev.json()
        err = json.dumps(self_body).lower()
        assert "own pull request" in err or "can not approve" in err

        # Architect approves on current head
        arch_rev = client.post(
            f"{API}/repos/{repo}/pulls/{pr_num}/reviews",
            headers=_headers(arch),
            json={"event": "APPROVE", "body": "M4-A architect approval"},
        )
        print("ARCH_APPROVE status", arch_rev.status_code, arch_rev.text[:300])
        assert arch_rev.status_code == 200, arch_rev.text
        assert str(arch_rev.json().get("state") or "").upper() == "APPROVED"
        assert (arch_rev.json().get("user") or {}).get("login") == "huozheclaude"

        for _ in range(15):
            time.sleep(1)
            pr_s = client.get(
                f"{API}/repos/{repo}/pulls/{pr_num}", headers=_headers(dev)
            )
            pr_s.raise_for_status()
            if str(pr_s.json().get("mergeable_state") or "") == "clean":
                break

        # Same PR merges as Developer
        ok_merge = client.put(
            f"{API}/repos/{repo}/pulls/{pr_num}/merge",
            headers=_headers(dev),
            json={
                "commit_title": f"test(m4a): live stamp {suffix}",
                "merge_method": "squash",
            },
        )
        print("MERGE status", ok_merge.status_code, ok_merge.text[:400])
        assert ok_merge.status_code == 200, ok_merge.text
        assert ok_merge.json().get("merged") is True

        final = client.get(
            f"{API}/repos/{repo}/pulls/{pr_num}", headers=_headers(dev)
        )
        final.raise_for_status()
        assert final.json().get("merged") is True
        assert (final.json().get("merged_by") or {}).get("login") == "huozhegrok"
        print("M4-A PASS pr=", pr_num)
