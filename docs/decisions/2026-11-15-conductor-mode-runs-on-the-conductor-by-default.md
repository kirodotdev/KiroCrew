# Conductor mode runs on the conductor by default, not the team lead

Decided by: Joe Guo (maintainer, @iamwhatever)
Date: 2026-11-15
Supersedes: 2026-11-04-ultra-mode-pauses-its-crew-and-runs-on-the-team-lead.md (in part: "Ultra Mode runs on `kirocrew-team-lead` when `agent.crew_mode_agent` is unset"; Pause all / Resume all and the goal-first rule stand)

## Decision

A conductor-mode chat runs on `kirocrew-conductor` when `agent.crew_mode_agent` is
unset. A set value is kept. The team lead's rules that conductor mode needs are
folded into the goal-conductor skill instead of living in a second lead agent.
Pause all / Resume all on the Board, the goal-first rule, the lock after the
first message and the one-way mid-chat switch are unchanged.

## Why

- Conductor-to-conductor dispatch, nested two levels by default
  (`work_ledger.max_depth`), already exists in goal-conductor and
  `kirocrew-conductor`; the team lead did not add it.
- `kirocrew-conductor` carries the 17 shared work-ledger rules in its own
  prompt; the team lead's prompt carries none of them, so it gets them only by
  reading the goal-conductor skill, which it often does not do and does not
  re-read after compaction.
- The team-lead skill treats the ledger as optional ("When you keep a ledger")
  and prefers doing the work itself, against the 2026-11-15 ruling that the
  work ledger is a core component and the dispatch model of conductor mode.
- The team-lead skill contradicts goal-conductor in several places, so a fix
  made in goal-conductor does not reliably reach a conductor-mode chat.

## Evidence

- The pull request that adds this entry carries the audit and the maintainer's
  restatement of the ruling.
- The entry it supersedes in part, and its pull request:
  https://github.com/kirodotdev/KiroCrew/pull/29928
- The ruling that the work ledger is a core component:
  `2026-11-15-every-bot-thread-tracks-its-work-in-the-ledger.md`
