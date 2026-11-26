# A Bot's Workers at once counts every session an agent opens as that Bot, and no sub-agent run

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-25

## Decision

A Bot's `Workers at once` limit (`agents.<name>.max_workers`) counts every open
session that an agent opened with `session_create`, `session_fork` or
`session_revive` and that runs as the Bot (its execution record carries the
Bot's member id), at any depth: the Bot's workers, and the sessions they open on
another template or as the Bot itself. The Bot's own chat, chats a person opens
and sessions with no member id are not counted. Runs started with `spawn_run` or
`spawn_sub_agents` are not counted.

## Why

- The issue asks for one number the owner sets to bound the sessions a Bot's
  workers open on other templates, which the limit did not count.
- A sub-agent run is a step inside a session that is already counted. It is
  bounded by `agent.max_subagents`, past which a spawn waits in the queue, and by
  the spawn depth limit. Counting it would refuse an ordinary sub-task of every
  worker of a Bot at its limit: with a limit of 1, the Bot's one worker could
  never start a sub-agent run.
- It follows the rule that Bot sessions create sessions and what is managed is
  the sub-agent inside those sessions
  (`2026-11-24-conductors-hand-work-to-sessions-and-engine-sub-agents-replace-spawn-run.md`).

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/37538#issuecomment-7172777754 --
  the maintainer's decision on the issue, which picks option 3 of the three
  options in https://github.com/kirodotdev/KiroCrew/issues/37538#issuecomment-6907533097
  for sub-agent runs.
- https://github.com/kirodotdev/KiroCrew/issues/37538 -- the issue and its design
  note.
- https://github.com/kirodotdev/KiroCrew/pull/37381 -- the pull request that
  added `max_workers`.
