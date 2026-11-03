# Ultra Mode shows only the Board

Decided by: Joe Guo (maintainer, @iamwhatever)
Date: 2026-11-02
Supersedes: 2026-10-27-crew-mode-is-a-composer-switch-to-the-conductor-template.md (in part: the Ultra Mode side panel's Dashboard tab that design decision 5 of #24860 put beside the task board)
Supersedes: 2026-10-10-conductor-board-tab-replaces-the-in-chat-board.md (in part: in an Ultra Mode chat with the preview on, the Board is the task board in place of the Dashboard tab)
Supersedes: 2026-10-13-conductor-with-a-dashboard-tab-skips-its-intro-card.md (in part: in an Ultra Mode chat with the preview on, the Board is the tab that lets the Conductor skip its introduction card)

## Decision

With the Ultra Mode preview on, an Ultra Mode chat (a Crew Mode root or one of its
crew members) shows the **Board** and no **Dashboard** tab: the tab is not on its
strip and its + menu does not offer it. Only the dashboard hides it. The gateway is
unchanged: a root that turns Crew Mode on still adopts the `goal-board` page, the
conductor's `[DASHBOARD]` block still names it, and a stored Dashboard tab stays
stored. With the preview off, or in any chat that is not Ultra Mode, the Dashboard
tab shows as before.

The Conductor's two rules that key on its Dashboard tab keep their wording and their
gateway signal (the `[DASHBOARD]` block naming `goal-board`). In an Ultra Mode chat
with the preview on, the Board plays that tab's role for both: it already shows who
is working and what is on the table, so the chat still skips the introduction card,
and it draws every task from the work ledger, so the chat still writes no in-chat
Task board. Everything else in the 2026-10-27, 2026-10-10 and 2026-10-13 entries
applies unchanged.

## Why

- Ultra Mode is one task board. The Board shows the goal, what needs the person
  (with the conductor's suggested answers as Reply buttons, #27891), the
  milestones and a recap, so a second tab showing the same tasks only repeats it.
- The Board gained goal-board's Reply buttons first (#27891), so hiding the tab
  loses nothing the person used it for.
- The Board shows the goal, the crew and every task, so it says what the intro
  card and the in-chat board would say, the reason both earlier rules give for
  leaving them out.
- The preview is kept per browser and the gateway cannot see it. A person with
  the preview off has no Board, so the gateway keeps the page and its prompt rules
  for them.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/24860 -- design decision 5, which
  adopted the task board onto the Dashboard tab.
- https://github.com/kirodotdev/KiroCrew/pull/27891 -- the Board's Reply buttons,
  merged first as the ruling required.
