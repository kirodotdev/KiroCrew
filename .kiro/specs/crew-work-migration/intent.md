# Intent: crew-to-crew migration of live work

**Status:** accepted

Author: timwukp (via GitHub issue #7577).
Source: https://github.com/kirodotdev/KiroCrew/issues/7577
Labels on source: `area: agents`, `area: cron`, `bug`, `enhancement`, `needs-human`

## SDLC artifact mapping

This change runs the AI-Native SDLC loop, but its artifacts live in the repo's own
`.kiro/specs/` convention rather than the skill's `intent/<slug>/` layout — the
repo is the single source of truth per the skill's brownfield rule. The gate reads
these files under those names:

| SDLC stage | Skill filename | This repo |
|---|---|---|
| Plan | `intent.md` | `intent.md` (this file) |
| Design | `spec.md` | `requirements.md` + `design.md` |
| Build | `plan.md` | `tasks.md` |

An RFC in `docs/request-for-change/` is additionally required before code, because
`CONTRIBUTING.md` mandates one for architectural changes. That RFC is the Design
stage's *external* review artifact; `requirements.md` + `design.md` are its input.

## Problem

A crew's sessions, task-runner runs, and cron schedules are bound to the crew that
created them and run on that crew's host. There is no way to move an **in-flight**
unit of work to another crew.

The concrete wall: a user works in a session on their laptop crew, needs to close
the laptop, and has a 24/7 remote crew on their own EC2 (`kirocrew cloud launch`).
The control plane already moves — the crew selector and `kirocrew cloud connect`
drive the remote crew fine. The *workload* does not.

Today's only option is manual reconstruction on the target: re-paste conversation
context, re-create the cron by hand with `cron_add`, restart the task-runner run
from spec and lose its progress. That is lossy, error-prone, and defeats the main
reason to run a remote crew at all.

## Proposed outcome

A first-class, user-initiated **"move to another crew"** action for three unit
types, targeting a crew the local Kiro Crew already knows about:

1. **Session** — dashboard "Move to crew…" + `kirocrew session move <id> --to <crew>`.
   Source becomes a tombstone pointing at its new home.
2. **Cron / schedule** — `kirocrew cron move <job-id> --to <crew>` + Schedule-tab
   action. Full `CronJob` record moves; source is removed or paused with a pointer
   so it never double-fires.
3. **Task-runner run** — move an in-flight run carrying spec **and** progress so
   the target resumes rather than restarts; explicit warning if it must restart.

## Affected users and systems

- **Users:** anyone running a local crew plus a remote crew (the EC2 case).
- **Systems:** `dashboard/session_transfer.py` (already does copy-transfer),
  `session_storage.py`, `session_map.json`, `cron.py` + `crons.json`,
  `taskrunner.py` + `runs.json` + `task_models.Project`, the `instances/` +
  `tunnel/` authenticated peer channel, `peer_resolve.py`.

## Constraints

- **Explicit and user-initiated.** No auto-migration. Distinct from #3278's auto-routing.
- **Secrets are not silently shipped.** Machine-specific credentials, tokens, and
  local paths stay put. Preflight surfaces what the target lacks *before* committing.
- **Atomic-ish handoff.** The unit runs in exactly one place at any time. Prefer
  move-with-tombstone over copy. No window where both crews fire the same cron.
- **Graceful failure.** Target unreachable or rejecting ⇒ source keeps ownership,
  nothing lost.
- **Reuse the existing transport.** The authenticated Instances tunnel already
  powers the crew selector; migration should not invent a second channel.

## Open questions

1. Do all three unit types land together or in sequence? The issue proposes cron
   first (single serializable record). Recon agrees: cron is the smallest slice.
2. Should this be framed as an extension of #4923 (generalize its target from
   "Kiro Cloud" to "any known crew", add schedule + task-runner units) rather
   than a standalone feature? The issue explicitly flags this to avoid a
   duplicate close.
3. Task-runner runs carry git worktree state (`worktree_path`, `repo_root`,
   `branch_name`, `commit_hashes`). Is repo materialization on the target in
   scope, or a documented precondition the preflight merely checks?
4. Does a migrated session keep its project binding? Today's copy path
   deliberately drops `project`, `model`, and `workspace` as dangling local-graph
   references — migration inherits that question.
