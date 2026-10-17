# A Conductor whose session has a Dashboard tab does not open with an introduction card

Decided by: Joe Guo (maintainer, @iamwhatever)
Date: 2026-10-13
Supersedes: 2026-10-10-conductor-board-tab-replaces-the-in-chat-board.md

## Decision

A `kirocrew-conductor` chat whose `[RUNTIME]` line names the Kiro Crew dashboard AND whose turn carries a `[DASHBOARD]` block naming any template skips the "Your Conductor" introduction card and gets straight to work; a dashboard chat with no Dashboard tab still opens with the card, and a messaging channel (Slack, DM and the like) still opens with its short plain-text introduction even when a `[DASHBOARD]` block is present, because that channel cannot see the tab; the task-board rule of the superseded entry is unchanged: when the tab shows `goal-board` or `work-kanban` the chat carries only the "Needs you" lines and one line on what changed and what is next, otherwise it shows the in-chat "Task board".

## Why

- The Dashboard tab already shows who the Conductor is and what is on the table, so the intro card only repeats it.
- Any Dashboard tab identifies the crewmate, so the intro rule keys on any tab, not only the board pages.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/19850#issuecomment-6151461095 -- the decision, recorded on the pull request that changes the prompt.
- https://github.com/kirodotdev/KiroCrew/pull/19850 -- the pull request that changes the Conductor's prompt.
