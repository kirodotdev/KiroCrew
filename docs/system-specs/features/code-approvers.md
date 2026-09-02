# Code Ownership and AI Review Overrides

## Ownership declaration

`.github/CODEOWNERS` assigns every repository path to `@kirodotdev/kirocrew-team` through a single wildcard rule. GitHub uses this file to request reviews; it does not itself establish an approval count or enforce branch protection. Do not infer a tier, a required number of reviewers, or a designated-maintainer requirement from this repository file. The wildcard ownership rule in `.github/CODEOWNERS` enforces the declaration, while GitHub branch protection remains the enforcement point for any required approval policy.

## AI review override authorization

`ai-review-human-override.yml` records a human decision for an AI-review lane only when the commenter has `admin`, `maintain`, or `write` collaborator permission. The `Validate and record the decision` step resolves the current PR head and accepts the supplied SHA only when it is a prefix of that head; it also requires a non-empty, bounded reason. `test_handler_requires_write_permission_fresh_sha_and_reason` pins these authorization and freshness checks.

The workflow writes a `github-actions[bot]` marker that names the lane and full PR head before it re-runs a reviewer. This ordering is load-bearing: `Resolve human override` in `claude-review.yml` and `codex-review.yml` consumes only bot-authored markers for the current head, so an untrusted PR comment or a decision for an earlier push cannot make an AI-review gate pass. `test_fable_consumes_only_a_bot_authored_sha_scoped_record` and `test_gpt_has_clear_verdict_banner_and_human_override` enforce that consumer contract.

## Fork review approval state

`pr-readiness.yml` treats a fork review workflow with GitHub's `action_required` conclusion as awaiting maintainer approval and publishes an action-required readiness state. This condition remains distinct from a review failure so contributors cannot clear an approval wait by changing the pull request. The `Evaluate readiness` step maintains that distinction.

## Related code

- `.github/CODEOWNERS` — repository-wide ownership declaration
- `.github/workflows/ai-review-human-override.yml` — authorization, SHA freshness, and trusted override record
- `.github/workflows/claude-review.yml` and `.github/workflows/codex-review.yml` — trusted-record consumers
- `.github/workflows/pr-readiness.yml` — fork approval-wait handling
- `test/test_ai_review_workflows.py` — regression coverage for override authorization and trusted consumption
