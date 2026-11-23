# An isolated spawn_run worker gets a detached worktree beside the repository

Decided by: Bolin Chen (@bolichen97)
Date: 2026-11-13

## Decision

`spawn_run`'s opt-in `isolate: "worktree"` gives each task a git worktree
detached at the HEAD of the checkout its `cwd` is in, with no branch, placed
beside the repository as `<repo>-wt-<run id>` and only when that folder is
under `agent.subagent_cwd_allowed_roots`; otherwise the task is refused with
the reason. The completion event names the worktree's path and its commits.
A worktree with commits, changes or ignored files is never removed and stays
listed on the run record while the record is kept; a clean one is removed with
`git worktree remove`, never `--force`, when the run's retention ends rather
than when it finishes. A task without `isolate` behaves exactly as before.

## Why

- The session scratch root is reclaimed with the session, and both it and the
  default crewmate workspace sit outside the allowed roots, so commits could be
  lost there or a `spawn_continue` of an isolated run refused.
- Removing a clean worktree when the worker finishes would break continuing
  that run.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/35037#issuecomment-6860685611 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/35037 -- the request.
