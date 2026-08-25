"""M4-A live (opt-in): assert ruleset + observe merge/self-approve refusal.

Run only with a real Developer token against a real repo:

    AGENTD_LIVE_M4A=1 \\
    AGENTD_LIVE_REPO=huozhe/code-workflow \\
    uv run --directory agentd pytest -q tests/test_m4a_branch_protection_live.py -s

Token: Developer only — GH_TOKEN or Keychain agentd/grok-bot (must be huozhegrok).
No Architect token. ADR-36 (d): steps 3 and 4 are not automatable.

Creates a short-lived Feature-style PR, proves refusal without Architect
approval, proves Developer self-approve is rejected, then closes the PR and
deletes the branch in a ``finally`` (ADR-36 (d′)). Does not merge.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import UTC, datetime

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


def _repo() -> str:
    return os.environ.get("AGENTD_LIVE_REPO") or "huozhe/code-workflow"


def cleanup_m4a_probe(
    client: httpx.Client,
    *,
    repo: str,
    headers: dict[str, str],
    head: str | None,
    pr_num: int | None,
) -> None:
    """ADR-36 (d′). Close the probe PR, then delete the ref.

    Armed from the moment the ref exists, not after the asserts: a failed
    step 1 or 2 must not leak an open PR or a branch. PATCH then DELETE, under
    the Developer token. ``pr_num is None`` means the PR was never opened —
    still delete the ref.
    """
    if pr_num is not None:
        client.patch(
            f"{API}/repos/{repo}/pulls/{int(pr_num)}",
            headers=headers,
            json={"state": "closed"},
        )
    if head:
        client.delete(
            f"{API}/repos/{repo}/git/refs/heads/{head}",
            headers=headers,
        )


def test_m4a_assert_rules_then_observe_refuse(allows_github_api) -> None:
    del allows_github_api  # opt-in: exemption must be visible in the signature
    dev = _dev_token()
    repo = _repo()
    branch_base = "main"
    headers = _headers(dev)

    # --- Assert half (settings, no admin needed) ---
    rules = verify_branch_pull_request_rules(
        repo=repo, branch=branch_base, token=dev, min_approving_reviews=1
    )
    assert rules.ok, rules.reason
    assert (rules.required_approving_review_count or 0) >= 1
    # Live repo note (2026-08): require_last_push_approval true; no status checks rule.
    print("ASSERT rules:", rules.reason)

    with httpx.Client(timeout=30.0) as client:
        me = client.get(f"{API}/user", headers=headers).json()
        assert me.get("login") == "huozhegrok", f"developer token is {me.get('login')!r}"

        main_ref = client.get(
            f"{API}/repos/{repo}/git/ref/heads/{branch_base}",
            headers=headers,
        )
        main_ref.raise_for_status()
        main_sha = main_ref.json()["object"]["sha"]

        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        suffix = uuid.uuid4().hex[:8]
        head: str | None = None
        pr_num: int | None = None
        finished_ok = False
        try:
            head = f"agentd/m4a-live-{suffix}"
            path = "docs/ops/m4-a-live-stamp.txt"
            body = f"m4a live {stamp} {suffix}\n"

            r = client.post(
                f"{API}/repos/{repo}/git/refs",
                headers=headers,
                json={"ref": f"refs/heads/{head}", "sha": main_sha},
            )
            r.raise_for_status()

            blob = client.post(
                f"{API}/repos/{repo}/git/blobs",
                headers=headers,
                json={"content": body, "encoding": "utf-8"},
            )
            blob.raise_for_status()
            tree = client.post(
                f"{API}/repos/{repo}/git/trees",
                headers=headers,
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
                headers=headers,
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
                headers=headers,
                json={"sha": head_sha},
            ).raise_for_status()

            pr = client.post(
                f"{API}/repos/{repo}/pulls",
                headers=headers,
                json={
                    "title": f"M4-A live probe {suffix}",
                    "head": head,
                    "base": branch_base,
                    "body": (
                        "Automated M4-A (issue #51 M4-3 / ADR-36). Assert rules "
                        "+ observe Developer merge refuse and self-approve refuse. "
                        "Not merged."
                    ),
                },
            )
            pr.raise_for_status()
            pr_num = int(pr.json()["number"])
            print(f"OBSERVE pr=#{pr_num} head={head}")

            mergeable_state = "unknown"
            for _ in range(15):
                time.sleep(1)
                pr_s = client.get(
                    f"{API}/repos/{repo}/pulls/{pr_num}", headers=headers
                )
                pr_s.raise_for_status()
                mergeable_state = str(pr_s.json().get("mergeable_state") or "unknown")
                if mergeable_state not in ("unknown", "unstable", ""):
                    break
            print(f"mergeable_state before merge attempt: {mergeable_state}")

            refuse = client.put(
                f"{API}/repos/{repo}/pulls/{pr_num}/merge",
                headers=headers,
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

            self_rev = client.post(
                f"{API}/repos/{repo}/pulls/{pr_num}/reviews",
                headers=headers,
                json={"event": "APPROVE", "body": "self-approve must fail"},
            )
            print("SELF_APPROVE status", self_rev.status_code, self_rev.text[:400])
            assert self_rev.status_code == SELF_APPROVE_REFUSAL_HTTP, self_rev.text
            self_body = self_rev.json()
            err = json.dumps(self_body).lower()
            assert "own pull request" in err or "can not approve" in err

            # Acceptance 3: no Architect review exists. Read the reviews array,
            # do not trust the test reporting success.
            reviews = client.get(
                f"{API}/repos/{repo}/pulls/{pr_num}/reviews", headers=headers
            )
            reviews.raise_for_status()
            arch_approvals = [
                rv
                for rv in (reviews.json() or [])
                if (rv.get("user") or {}).get("login") == "huozheclaude"
                and str(rv.get("state") or "").upper() == "APPROVED"
            ]
            assert arch_approvals == [], arch_approvals
            print("M4-A PASS (account steps 1–2) pr=", pr_num)
            finished_ok = True
        finally:
            cleanup_m4a_probe(
                client, repo=repo, headers=headers, head=head, pr_num=pr_num
            )
            # Assert leftover-gone on the success path only. A second
            # assertion in ``finally`` during unwind can replace the
            # original step-1/2 failure. Failure-path leftover is
            # acceptance 3b in test_adr36_github_api_guard.
            if finished_ok:
                if pr_num is not None:
                    closed = client.get(
                        f"{API}/repos/{repo}/pulls/{pr_num}", headers=headers
                    )
                    closed.raise_for_status()
                    assert closed.json().get("state") == "closed", closed.json()
                if head:
                    gone = client.get(
                        f"{API}/repos/{repo}/git/ref/heads/{head}",
                        headers=headers,
                    )
                    assert gone.status_code == 404, gone.text
