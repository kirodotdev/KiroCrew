# CI lanes, issue tiers and GitHub fallbacks

Read from SKILL.md's *Lanes* when a lane other than `Fast Gate` is red, from
*Issue first* for the tier table, and from Phase 3 step 2 when `gh` cannot update
a PR body. Full detail: `docs/ci/ci-and-reviews.md` and `CONTRIBUTING.md`. CI wins
over prose.

## Lanes

`PR Readiness` folds every lane below into one verdict and one `readiness:` label.

| Lane | Blocks? | When it is red |
|---|---|---|
| `Fast Gate` | yes | fix it first: the heavy matrix and the fork AI lanes wait on it, so fewer red checks does not mean fewer defects |
| `Internal Content Scan` | yes | an added line carries internal-only content; remove it (`docs/system-specs/oss-fork-boundaries.md`) |
| `CI`, `Build` | yes | tests, lint, coverage, e2e, artifacts — Phase 3 (a) |
| `Code Review` | yes | grep rules, Semgrep, woke, and PR Hygiene: a Conventional-Commits title, at most two commits, the template's headings with a filled `**Goal:**`, `## Pattern harvest` on a `fix`/`revert` PR (plus a `## Sibling sweep` and, on a `fix:` PR, a `Proof:` line inside `## Tests`, both warnings for now), and a `Reader:` line when the diff takes something away |
| `Issue Gate` | once enabled | the issue line or its tier (SKILL.md's *Issue first*) |
| `Opus 5.5 Review`, `GPT 6.1 Review` | Critical/High | line-level findings — Phase 3 (b) |
| `Security Scope Review` | yes | each row is a legitimate operation your tightening newly refuses; narrow the rule, or override `scope` |
| `Design Review`, `First Principles Review`, `UX Review` | BLOCK only | PASS needs no reply; every BLOCK item is fixed or answered |
| CodeQL | yes | a fork head cannot run it: a "Not eligible" note, not a blocker |

A **fork** PR is aggregated the same way and can reach `passed`: its AI reviews
run in the Stage-2 `fork-*-review.yml` lanes under the same check names.

**Merge queue:** the workflows are ready, the ruleset has not switched it on yet.
Once it is on, auto-merge enqueues the PR; the queue re-runs the tests (not the AI
reviews) on the tree that lands, and a failing group ejects the PR — fix it, or rerun
it once after the skill's *Before you rerun a red test*, then re-arm.

## Rerun, don't re-push

Read from SKILL.md's *Before you rerun a red test*. When a job fails on a flaky
test, or an AI review lane dies on infrastructure before it produces a verdict,
rerun that one job: `gh run rerun <run-id> --failed`, or `--job <job-id>`. Do not
rebase and push an identical commit to re-trigger it. A new head restarts every
check, a full round of about 90 minutes, throws away the green rows already earned,
and can hit the same flake again. Push only when the base actually moved.

If `gh run rerun` is refused, read your current access before concluding you
cannot rerun: `gh api repos/<owner>/<repo> --jq .permissions`. An older note that
the account is read-only can be out of date. With `push: true` you can rerun the
job yourself. Without it, ask a maintainer to rerun that one job and give them the
run URL.

## File-overlap scan

Read from SKILL.md's Phase 0 *File overlap* gate. Do not use
`gh pr list --limit N`: any cap at or below the open-PR count scans a subset and
the gate reads as passing, and at this repo's size the single request 504s. Page
with cursors instead:

```bash
gh api graphql --paginate --slurp -f query='query($endCursor: String) { repository(owner: "<owner>", name: "<repo>") { pullRequests(states: OPEN, first: 50, after: $endCursor) { pageInfo { hasNextPage endCursor } nodes { number files(first: 100) { pageInfo { hasNextPage } nodes { path } } } } } }'
```

Then check two things before trusting the result:

1. The last page's `pullRequests.pageInfo.hasNextPage` is `false`.
2. Each PR's own `files.pageInfo.hasNextPage`. A PR with more than 100 files is
   truncated on that axis no matter how the outer list is paged; re-read it in
   full with `gh api --paginate repos/<owner>/<repo>/pulls/<n>/files`.

Why: `references/rationale.md`.

## Updating a PR body when `gh` fails

A GraphQL error or a rate limit on `gh pr edit` leaves the old body in place.
PATCH it over REST instead, then read the body back to confirm it landed:

```bash
python3 -c 'import json; print(json.dumps({"body": open("<file>").read()}))' > /tmp/pr-patch.json
gh api repos/<owner>/<repo>/pulls/<n> -X PATCH --input /tmp/pr-patch.json
```

## Issue tiers

Read from SKILL.md's *Issue first*. The Captain — the maintainers' triage crew — scans
issues with no tier, writes one tier label and marks the issue `pending-triage`.
The tier says how big the work is:

| Tier | Means | Before you build |
|---|---|---|
| `tier:T1` | a bug; the fix keeps the design | go |
| `tier:T2` | a small additive feature | go |
| `tier:T3` | a change to an existing experience; needs a one-pager | wait for `triaged` |
| `tier:T4` | a new concept; needs a design review | wait for `triaged` |

A person flips a T3/T4 issue from `pending-triage` to `triaged` after reading it.
Unsure of the size? Pick the higher tier. Only a maintainer applies
`issue-gate: waived` (a production fire, a release PR). The older `needs-triage`
and verdict labels (`auto-fixable`, `needs-investigation`, `needs-human`) drive
Issue Radar's dispatch; the gate does not read them.
