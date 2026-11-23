# Diff-based test selection never narrows a push to main or release/**, a merge_group run or a scheduled run

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-23

## Decision

The diff-based test selection from `scripts/ci_test_map.py select` never applies to a push to `main` or `release/**`, a `merge_group` run, or a scheduled run, and while the merge queue is off, the `CI` push run on `main` is the backstop that catches a selection miss.

## Why

- A merge-queue backstop needs the queue turned on, and the queue is off on `main` by the owner's decision. Moving the backstop to the queue needs its own decision: today a `merge_group` run takes the leaf-test and surface scopes, and a queue-landed push runs no test matrix.
- A required full run on every merging head gives back most of the time selection saves, and it needs new trigger machinery.
- The push run on `main` needs no workflow or settings change. It runs the full backend suite on Windows on every push. On Linux it does so when the pushed commit's own diff is not frontend-only; a frontend-only push takes the surface scope there.
- A newer push to `main` evicts a pending push run. A completed run tests the whole tree at its head, so a miss from a merge whose run was evicted is caught by the next completed run that runs the full suite on that system, and is traced to a range of merges.
- The cost: a Linux-only miss reaches `main`. `main-red-watch.yml` opens an issue for a break that fails in one job only when the previous verdict run on `main` was red on the same test file, so the issue opens after about two red completed runs. Until then, open pull requests can fail on it.
- Not decided here: whether the enforcement stage forces the full Linux backend suite on every push to `main`.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/39688#issuecomment-7091012913 -- the maintainer's decision, with five full-suite push runs linked.
- https://github.com/kirodotdev/KiroCrew/issues/39688#issuecomment-7091113454 -- the maintainer's clarification of its scope.
- https://github.com/kirodotdev/KiroCrew/issues/39688 -- the issue.
- https://github.com/kirodotdev/KiroCrew/issues/39690 -- the staged rollout whose enforcement stage waits on this backstop.
- https://github.com/kirodotdev/KiroCrew/issues/39687 -- the enforcement stage, whose scope already leaves these runs out of selection.
