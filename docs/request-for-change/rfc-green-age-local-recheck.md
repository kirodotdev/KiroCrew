---
title: A moved base is a reason to re-test locally, not to push -- the prepare-pr loop pushes a green-age re-sync only when the re-check conflicts or goes red
status: implemented
author: rnoack1, with a kirocrew agent session
created: 2026-11-17
last-audited: 2026-11-20
audited-at: c28a04d44e
doc-pr: 38682
implementation-prs: [38192]
tracking-issues: [38153]
supersedes: []
superseded-by: []
---

# RFC: Re-check locally on a moved base; push only on a conflict or a red

Status: implemented. A maintainer (bolichen97) accepted it on 2026-11-20 in the
approving review of [#38682](https://github.com/kirodotdev/KiroCrew/pull/38682),
and [#38192](https://github.com/kirodotdev/KiroCrew/pull/38192) built it (merged
2026-11-20). Checked at `c28a04d44e`: on exit 30 `SKILL.md` step 5 says "Green: do NOT
push", `references/monitor-loop.md` tells the woken loop to keep a green re-check
local, `scripts/green_age.py` says a STALE answer means re-check locally and push
only on a conflict or a red, and
`test/test_prepare_pr_green_age.py::test_a_clean_re_check_keeps_the_rebase_local`
pins the rule.

## 1. Summary

When `green_age.py` exits 30, the base moved in files the PR also touches since
its CI ran. Today the loop then rebases **and pushes**. This RFC changes the
loop to rebase locally and run the local gates and the related tests. It
pushes only when that re-check finds something.

| After the local re-check | Today | Proposed |
|---|---|---|
| clean rebase, gates and related tests green | push + `Rebased:` comment | no push; keep the rebase local and report it |
| conflict, or a red this PR causes | push + comment | push the fix + comment (unchanged) |
| red because `main` itself is red | *When main is red*: no push | same, now also named in the exit-30 step |

## 2. Problem

The rule lives in four lines across three files at `a347c11883`:

- `src/kiro_crew/builtin_skills/kirocrew-dev/kirocrew-prepare-pr/SKILL.md:443`
  lists `green_age.py` exit 30 among the five things that justify a push, and
  `SKILL.md:523` says "re-sync through Phase 1, run only the related tests ...
  push with the SHA-pinned lease".
- `references/monitor-loop.md:17`, the text a woken loop actually reads:
  "green_age 30: re-sync, run the related tests, push."
- `scripts/green_age.py`'s `main()` prints, on a STALE verdict, "Rebase onto
  origin/main ... and push."

A push buys nothing a merge needs on this repository. The `main` ruleset, read
live with `gh api repos/kirodotdev/KiroCrew/rules/branches/main`:

| Rule | Value | What it means for a base-only push |
|---|---|---|
| `strict_required_status_checks_policy` | `false` | a PR behind `main` still merges |
| `required_status_checks` | `PR Readiness` only | nothing else gates merge |
| `dismiss_stale_reviews_on_push` | `true` | the push clears approvals |
| `require_last_push_approval` | `true` | a human must approve again |
| merge queue on `main` | none (GraphQL `mergeQueue` is `null`) | nothing re-tests at merge either way |

`docs/ci/ci-and-reviews.md:121` confirms the queue is off by the owner's
decision. `references/rationale.md:314` still says the repository adopted one;
that sentence is out of date and #38192 corrects it.

So the push re-runs CI and can reset review, and leaves mergeability as it was.

## 3. Evidence

Measured across all of `kirodotdev/KiroCrew` on 2026-11-16. The queries and raw
rows are in #38192's description.

| Measure | Result |
|---|---|
| PRs carrying a `Rebased: main moved in` comment | 1,465 PRs, 72 authors, 56 posting accounts |
| Re-sync pushes | 2,435; 269 PRs had three or more |
| Re-sync pushes onto an approved head | 94 on 81 PRs; next approval median 5.6 h; 25 never followed by another approval |
| Sample of 221 re-syncs, up to 6 per account | 123 pushed a byte-identical patch |
| Sample: green head, identical patch, decided verdict | 26; 16 green again; at most 2 followed by a code change |
| Workflow runs per re-sync push (sample) | median 23 |

## 4. Goals and non-goals

Goals:

- A re-sync push carries a fix, or does not happen.
- The woken loop, the skill and the script's printed advice say the same thing.

Non-goals:

- No change to `green_age.py`'s verdict, exit codes, or the re-sync cap.
- No server-side change: ruleset, required checks, merge queue.
- No change for a repository with strict up-to-date checks or a merge queue;
  there being behind does block merge, so the push is needed.

## 5. Design

On exit 30, the loop:

1. Rebases onto `origin/<base>` in its own checkout.
2. Runs Phase 2's gates and the related tests. The scoped runner picks those
   from the PR's own diff, as today's rule already does; it does not add tests
   for the files the base moved (open decision 2).
3. Green: keeps the rebase local and reports it. No push, no comment.
4. Conflict, or a red this PR causes: fixes it, pushes with the SHA-pinned
   lease, posts the `Rebased: main moved in` comment the cap counts.
5. A red also on `origin/<base>`: follows *When main is red*. No push here.

Two readings then differ, on purpose:

- `green_age.py` in the loop's checkout measures the local head. After a clean
  re-check it reads fresh, so the loop does not repeat the work.
- `pr_status.py` measures the published head. It keeps saying the CI green
  predates the base, so a person about to merge still sees it.

Review-ready criterion 3 accepts either a fresh reading or an answered 30.

## 6. What this gives up

CI's full suite on the rebased tree. Local related tests do not select
tree-wide checks: a ratchet over every test file, a count pin, a per-file line
cap.

The `Main is red` issues since `green_age.py` shipped (2026-09-18) give a lower
bound on the cost, not a full count. `main-red-watch.yml` keeps one open issue
per red streak and edits it, and files nothing for a red that names no failing
pytest node. Inside that bound:

- 23 issues. Most are two PRs merged 12 s to 1 h 41 min apart. A quiet loop
  checks about every 100 minutes, so neither rule reaches those.
- In the longer gaps (about 2 h, 10.9 h, 22 h) none of the stale PRs carried a
  re-sync comment, so today's rule did not catch them either.
- Six issues (three breaks) were tree-wide checks of the kind above. They would
  still turn `main` red after merge, where push-to-main CI shows them, as it
  already does for PRs merged inside the 100-minute window.

The re-sync cap counts `Rebased: main moved in` comments, which a clean
re-check no longer posts, so the cap no longer bounds clean local re-checks.
Those cost local test time only, at most one per loop turn.

## 7. Rollout

One phase: #38192 changes the prose and the printed advice, plus one test that
pins the rule in `SKILL.md` and in the arming template.

Exit criteria:

- `test/test_prepare_pr_green_age.py::test_a_clean_re_check_keeps_the_rebase_local`
  passes, and fails with `main`'s `monitor-loop.md` restored.
- Across 30 days after merge, re-sync pushes onto an approved head fall
  against the 94 measured in section 3, on the same query.

Undo: a `git revert` of #38192 restores today's rule. This is a two-way door.

## 8. Backward compatibility

Compatible. No exit code, flag, comment marker or cap changes. A loop armed
with the old text pushes as before; a loop on the new text pushes less.

## 9. Security considerations

None. No trust boundary, credential or permission moves. The change removes
pushes; it adds none.

## 10. Alternatives considered

- **Keep pushing, capped at three.** Today's rule. Each push costs about two
  dozen runs and can cost an approval; the sample found at most 2 real breaks
  in 26 green identical-patch re-syncs.
- **Turn on strict up-to-date checks or the merge queue.** That closes the gap
  server-side. `docs/ci/ci-and-reviews.md` records the queue is off by the
  owner's decision, and `references/rationale.md` gives the reason: it
  serialises merges behind a ~19-minute CI.
- **Push, but skip the re-run of review lanes.** The ruleset still dismisses
  approvals on any push, so this saves CI and not review.

## 11. Open decisions

1. Accept this rule for the `kirocrew-prepare-pr` loop's default. This is the
   decision a maintainer is asked for.
2. Whether a later change should widen the related-test set: tests for the
   files the base moved, and the tree-wide checks named in section 6. Not
   needed for this RFC; today's rule has the same gap.
