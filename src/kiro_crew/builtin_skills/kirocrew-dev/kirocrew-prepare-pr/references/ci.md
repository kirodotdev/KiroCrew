# CI lanes and GitHub fallbacks

Read from SKILL.md's *Lanes* when a lane other than `Fast Gate` is red, and from
Phase 3 step 2 when `gh` cannot update a PR body. Full detail:
`docs/ci/ci-and-reviews.md` and `CONTRIBUTING.md`. CI wins over prose.

## Lanes

`PR Readiness` folds every lane below into one verdict and one `readiness:` label.

| Lane | Blocks? | When it is red |
|---|---|---|
| `Fast Gate` | yes | fix it first: the heavy matrix and the fork AI lanes wait on it, so fewer red checks does not mean fewer defects |
| `Internal Content Scan` | yes | an added line carries internal-only content; remove it (`docs/system-specs/oss-fork-boundaries.md`) |
| `CI`, `Build` | yes | tests, lint, coverage, e2e, artifacts — Phase 3 (a) |
| `Code Review` | yes | grep rules, Semgrep, woke, and PR Hygiene: a Conventional-Commits title, at most two commits, the template's headings with a filled `**Goal:**`, `## Pattern harvest` on a `fix`/`revert` PR, and a `Reader:` line when the diff takes something away |
| `Issue Gate` | once enabled | the issue line or its tier (SKILL.md's *Issue first*) |
| `Opus 5.5 Review`, `GPT 6.1 Review` | Critical/High | line-level findings — Phase 3 (b) |
| `Security Scope Review` | yes | each row is a legitimate operation your tightening newly refuses; narrow the rule, or override `scope` |
| `Design Review`, `First Principles Review`, `UX Review` | BLOCK only | CONCERNS is advisory but every item must be answered |
| CodeQL | yes | a fork head cannot run it: a "Not eligible" note, not a blocker |

A **fork** PR is aggregated the same way and can reach `passed`: its AI reviews
run in the Stage-2 `fork-*-review.yml` lanes under the same check names.

**Merge queue:** the workflows are ready, the ruleset has not switched it on yet.
Once it is on, auto-merge enqueues the PR; the queue re-runs the tests (not the AI
reviews) on the tree that lands, and a failing group ejects the PR — fix it, or rerun
it once after the skill's *Before you rerun a red test*, then re-arm.

## Updating a PR body when `gh` fails

A GraphQL error or a rate limit on `gh pr edit` leaves the old body in place.
PATCH it over REST instead, then read the body back to confirm it landed:

```bash
python3 -c 'import json; print(json.dumps({"body": open("<file>").read()}))' > /tmp/pr-patch.json
gh api repos/<owner>/<repo>/pulls/<n> -X PATCH --input /tmp/pr-patch.json
```
