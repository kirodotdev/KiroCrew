# A message a person types into a cron result tab runs the folder agent's own hooks

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-15

## Decision

A message a person types into a cron result tab runs the folder agent's own hooks like any chat turn, while a scheduled run still runs none of them and keeps its auto-approvals.

## Why

- The tab and the scheduled run share one KAS session, so a scheduled run that
  follows a typed turn re-projects that session first when that turn's batch
  kept the folder agent's own PreToolUse hooks or left out a folder sub-agent
  target for them, and waits while sub-agent work
  still runs on the session; a typed turn that lands on the scheduled run's
  session re-projects it too, and runs without the folder hooks only while
  sub-agent work runs on that session. A person who types into a cron tab
  while the folder agent's consented spec cannot be read gets the scheduled
  run's session re-projected, so that session is torn down and the turn fails
  closed. A manual Run is never held back for
  this: while that work runs it runs as projected.
  A queued batch that mixes a typed message with a completion card is not a
  typed turn.
- The folder-hooks design agreed earlier covers chat turns and leaves out only
  scheduled runs; excluding a typed turn in a cron tab was a recorded shortcut,
  not a decision.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/34727#issuecomment-6892775090 -- the
  maintainer decision.
- https://github.com/kirodotdev/KiroCrew/issues/34727 -- the issue.
- https://github.com/kirodotdev/KiroCrew/issues/33576 -- the agreed parent design:
  folder hooks fire on chat turns, cron is unchanged.
- https://github.com/kirodotdev/KiroCrew/pull/33578 -- the change that recorded the
  shortcut.
