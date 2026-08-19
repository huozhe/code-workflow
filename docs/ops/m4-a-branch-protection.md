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

**A `required_status_checks` rule now exists**, added by the owner on
2026-08-18 as ruleset `main-required-checks` (id `21017175`, enforcement
`active`, applied to `~DEFAULT_BRANCH` and `refs/heads/main`). Collaborators
still cannot add or change one (`admin: false`).

| Rule `type` | Parameters (relevant) |
|---|---|
| `required_status_checks` | `lint`, `types`, `pytest` — each `integration_id: 15368` (GitHub Actions) |
| | `strict_required_status_checks_policy: false` |

agentd `repos.huozhe/code-workflow.required_checks` **is** the same three
`pr.yml` check-run names, set in `~/.agentd/config.yaml` the same evening:

```yaml
repos:
  huozhe/code-workflow:
    required_checks: [pytest, lint, types]
```

Do not list `image`. That workflow is path-filtered and would leave
`merge_authorized` settling on every PR that does not touch the runner — see
the runbook note below on why a check that never reports hangs rather than
fails.

`strict_required_status_checks_policy: false` means a PR may merge without
being up to date with `main`, so two individually green PRs can still break it
together. Acceptable while work is serial; revisit if that stops being true.

Gateway helper: `agentd.verify.verify_branch_pull_request_rules` — asserts
`required_approving_review_count >= 1` and surfaces the flags above.

### Alignment risk (runbook)

**The same three strings must appear in three places**: the `pr.yml` job ids,
the ruleset contexts, and `repos.<repo>.required_checks` in config. They agree
today. If they ever drift, the two directions fail differently:

- **Ruleset has a check that config does not** → the gateway emits
  `merge_authorized` while GitHub still blocks the Developer merge. Loud, and
  visible on the PR.
- **Config names a check that never reports** — a typo, or a path-filtered
  workflow like `image` — → **it hangs rather than fails.**
  `verify._classify_required_checks` buckets an absent check as *missing*,
  missing counts as *pending*, and the gateway retries forever without ever
  emitting a permanent refusal. The stall is indistinguishable from "CI is
  still running"; the evidence is `missing/not-yet-reported: <name>` in the
  gateway log.

On `merge_authorized`, the gateway logs the configured `required_checks` list
for post-hoc comparison.

**This file records live host and GitHub state, which the repo cannot verify.**
It went stale once already: it described the ruleset rule as "a separate owner
step" for hours after the owner had taken it, and an agent reading it drafted
an ADR on the false premise (#63 / PR #148). Check it against
`gh api repos/huozhe/code-workflow/rulesets` and `~/.agentd/config.yaml` before
relying on it.

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
