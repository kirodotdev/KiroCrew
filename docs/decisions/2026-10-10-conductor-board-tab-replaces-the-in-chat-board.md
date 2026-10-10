# A Conductor whose Dashboard tab shows the task board does not write a second board in the chat

Decided by: Joe Guo (maintainer, @iamwhatever)
Date: 2026-10-10
Supersedes: 2026-10-02-conductor-introduces-itself-in-every-chat.md

## Decision

Every `kirocrew-conductor` chat introduces itself in plain words and reports with a live task board that puts "Needs you" first, wherever it is opened; when its Dashboard tab shows `goal-board` or `work-kanban`, that tab is the task board, and the chat reply carries only the "Needs you" lines and one line on what changed and what is next, with no in-chat "Task board" widget and no `task-dashboard` artifact written.

## Why

- Crew Mode users saw every task twice: the Dashboard tab already draws the board, live from the work ledger.
- Not writing the second board also saves the output tokens of a widget and an artifact update at every milestone.
- It is one Conductor on every surface: the rule reads the page its tab already shows, with no new flag. Surfaces with no board tab keep the in-chat board.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/18620 -- the RFC `docs/request-for-change/rfc-conductor-chat-board-on-tab.md`, merged by the maintainer on 2026-10-10.
- https://github.com/kirodotdev/KiroCrew/pull/18620#issuecomment-6100865280 -- the maintainer's on-record comment on that RFC.
- https://github.com/kirodotdev/KiroCrew/pull/18608 -- the pull request that changes the Conductor's prompt.
