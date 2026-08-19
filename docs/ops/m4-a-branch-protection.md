# M4-A — Branch protection (FR-1.3)

**Status:** PASS (2026-08-12). Issue #51 **M4-3**. Spike M4-A in the design
spec: *Developer identity cannot produce a satisfying approval on its own PR*.

Two evidence halves. Neither alone is enough; together they prove FR-1.3.

| Half | What it shows | What it does **not** show |
|---|---|---|
| **Assert** (settings) | Ruleset on `main` requires ≥1 approving review | That GitHub will refuse a bad merge today |
| **Observe** (enforcement) | Real API refusal / success on the wire | That the rule is still configured tomorrow |

## Assert half — ruleset is machine-readable

Classic branch protection (`GET …/branches/main/protection`) returns **404** for
collaborators without admin. That is why early notes said FR-1.3 was only
*inferred*.

The owner converted classic protection to a **repository ruleset** (2026-08-11).
Collaborators can read:

```http
GET /repos/{owner}/{repo}/rules/branches/main
```

Observed as `huozhegrok` / `huozheclaude` (no admin):

| Rule `type` | Parameters (relevant) |
|---|---|
| `pull_request` | `required_approving_review_count: 1` |
| | `dismiss_stale_reviews_on_push: true` |
| | `require_last_push_approval: true` |
| | `required_review_thread_resolution: true` |
| | `require_code_owner_review: false` |
| `deletion` | (main cannot be deleted) |
| `non_fast_forward` | (no force-push) |

**`required_status_checks` rule wired 2026-08-18/19 (owner step, #144 + #63).**
This section said "a separate owner step" for five days after #144 named the
list; the owner took that step, and this file did not move — which is how
#63's ADR-26 draft sourced a stale "`required_checks` unset" premise straight
from here (corrected in ADR-26 1.22.0 / PR #148, #149). Live, verified via:

```http
GET /repos/huozhe/code-workflow/rules/branches/main
```

```json
{
  "type": "required_status_checks",
  "parameters": {
    "required_status_checks": [
      {"context": "lint", "integration_id": 15368},
      {"context": "types", "integration_id": 15368},
      {"context": "pytest", "integration_id": 15368}
    ]
  }
}
```

agentd `repos.huozhe/code-workflow.required_checks` matches (per #144):

```yaml
repos:
  huozhe/code-workflow:
    required_checks: [pytest, lint, types]
```

Do not list `image`. That workflow is path-filtered and would leave
`merge_authorized` settling on every PR that does not touch the runner.

Gateway helper: `agentd.verify.verify_branch_pull_request_rules` — asserts
`required_approving_review_count >= 1` and surfaces the flags above.

### Alignment risk (runbook) — this is not just theoretical

The two lists (ruleset `required_status_checks`, config `required_checks`)
must stay aligned, or the gateway can emit `merge_authorized` while GitHub
still blocks the Developer merge — or, the failure mode #63 actually hit,
a stale *description* of the ruleset (this file, not the ruleset itself)
misleads whoever reads it next into re-deriving an already-fixed bug's
premise. On `merge_authorized`, the gateway logs the configured
`required_checks` list for post-hoc comparison against the ruleset. When
the owner changes the ruleset, **this file needs a same-day edit** — "the
owner does the ruleset step, this file names the list" was the old
one-directional framing, and it silently rots the moment the owner acts
without a matching doc update. Treat this file as a cache of ruleset state,
not a description of intent, and invalidate it the same way: on write, not
on next read.

`require_last_push_approval: true` means after a `CODE_REWORK` push the Architect
must approve **again** on the new head. M4-2 already binds approval to current
head SHA; M4-4 exercises this live.

## Observe half — wire shapes (PR #55, 2026-08-12)

Identities: Developer = `huozhegrok`, Architect = `huozheclaude`.
Feature PR opened and merged by Developer; approval only from Architect.

### 1. Merge without Architect approval → refused

```http
PUT /repos/huozhe/code-workflow/pulls/55/merge
Authorization: Bearer <developer>
```

```json
{
  "message": "Repository rule violations found\n\nNew changes require approval from someone other than the last pusher.\n\n",
  "documentation_url": "https://docs.github.com/rest/pulls/pulls#merge-a-pull-request",
  "status": "405"
}
```

HTTP **405**. Message text reflects `require_last_push_approval` (not a generic
"needs review"). Agent / tooling must treat this as a **ruleset refusal**, not a
crash or transport failure.

Constants in code: `MERGE_RULE_REFUSAL_HTTP = 405`.

### 2. Developer self-approve → refused at review API

```http
POST /repos/huozhe/code-workflow/pulls/55/reviews
{"event":"APPROVE", ...}
```

```json
{
  "message": "Unprocessable Entity",
  "errors": ["Review Can not approve your own pull request"],
  "documentation_url": "https://docs.github.com/rest/pulls/reviews#create-a-review-for-a-pull-request",
  "status": "422"
}
```

HTTP **422**. Author cannot even submit `APPROVE` on their own PR — stronger
than "approval does not count". Constant: `SELF_APPROVE_REFUSAL_HTTP = 422`.

### 3. Architect approve → `mergeable_state` becomes `clean`

```http
POST /repos/…/pulls/55/reviews   # as huozheclaude
→ 200 {"state":"APPROVED","user":{"login":"huozheclaude"}, ...}
```

PR: `mergeable: true`, `mergeable_state: "clean"`.

### 4. Same PR, Developer merges → success

```http
PUT /repos/…/pulls/55/merge   # as huozhegrok
→ 200 {"merged":true,"message":"Pull Request successfully merged", ...}
```

`merged_by.login == "huozhegrok"`.

## Automated re-check

Unit (always): `tests/test_verify_branch_rules.py`.

Live (opt-in, writes a stamp file on success):

```bash
AGENTD_LIVE_M4A=1 \
  uv run --directory agentd pytest -q tests/test_m4a_branch_protection_live.py -s
```

## Cleanup note

One-shot probe PR #55 left a temporary `.agentd-m4a-probe` on `main`; removed in
the M4-3 code PR.
