# A chat running a bot on another harness says so once

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-22

## Decision

A dashboard chat that runs a bot outside the bot's own chat keeps its own
harness and, when that is not the harness the bot's own chat runs on, says so
once in the chat (and again if either harness later changes) with a pointer to
the bot's own chat; nothing is refused and no harness or model selection
changes.

## Why

- It turns the silent run into a clear message without taking away the picker
  choice that
  `docs/decisions/2026-11-09-only-a-bots-own-thread-hands-work-to-its-worker.md`
  keeps, so the switch is not refused.
- Moving the chat onto the bot's harness is design work under #21910, as are
  agent channels, channels bound to a bot and scheduled runs, which are not
  told.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/38637#issuecomment-7080079927 --
  the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/38637 -- the issue.
- `docs/decisions/2026-11-09-only-a-bots-own-thread-hands-work-to-its-worker.md`
  -- the decision that keeps the picker's choice.
