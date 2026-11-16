# A Bot's sessions open in its side panel's Sessions tab, and always leave the person's session list

Decided by: Joe Guo (maintainer, @iamwhatever)
Date: 2026-11-16
Supersedes: 2026-11-03-a-crewmates-sessions-leave-the-sidebar-for-its-sessions-tab.md, 2026-11-12-a-bots-sessions-tab-opens-a-session-and-shows-the-task-it-was-last-given.md, 2026-11-15-a-bots-sessions-leave-the-session-list-by-default.md

## Decision
The Bot page's side panel has a second standing tab, **Sessions**, after Dashboard (which stays first and is the tab the panel opens on), listing the Bot's sessions; a click on a row there, or on a row of the Profile card's Sessions tab, opens that session inside the side panel's Sessions tab, where the person can read and type to it, with the recap one toggle away and an "Open in chat" link to the chat page, and a click no longer expands a recap in place; and the per-Bot **Show in my Sessions list** switch is removed, so a live Bot thread's sessions always leave the person's own session lists and a stored `show_sessions_in_list` value is ignored; this supersedes, in part, `2026-11-03-a-crewmates-sessions-leave-the-sidebar-for-its-sessions-tab.md` (its "a click expands the row in place into a recap" clause), `2026-11-12-a-bots-sessions-tab-opens-a-session-and-shows-the-task-it-was-last-given.md` (its "a click on a row still expands it in place into a recap" clause and the recap's open button) and, in part, `2026-11-15-a-bots-sessions-leave-the-session-list-by-default.md` (its per-Bot switch), and every other rule of these entries stands.

## Why
- A recap shown in place on the Profile card stood alone: it led nowhere the person could act.
- With the side panel listing a Bot's sessions and opening them to chat, the switch is no longer needed.
- The Sessions list must not change the Bot's Dashboard.

## Evidence
- The maintainer's restatement on the pull request that adds this entry: https://github.com/kirodotdev/KiroCrew/pull/37915#issuecomment-6923854391
- The issue for opening a session from the Sessions tab: https://github.com/kirodotdev/KiroCrew/issues/36175
- The pull request that added the switch: https://github.com/kirodotdev/KiroCrew/pull/37713
