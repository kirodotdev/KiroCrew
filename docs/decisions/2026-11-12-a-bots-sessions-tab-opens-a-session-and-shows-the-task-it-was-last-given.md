# A Bot's Sessions tab opens a session from its recap, and a session with no report shows the task the Bot last gave it

Decided by: Ray Xu (maintainer, @buluoray)
Date: 2026-11-12
Supersedes: 2026-11-03-a-crewmates-sessions-leave-the-sidebar-for-its-sessions-tab.md

## Decision
On a Bot's Sessions tab a click on a row still expands it in place into a recap; the opened recap carries a button that opens that session in the chat view ("Reopen session" for a closed session, which is reopened from its saved conversation first), the recap's one action, with everything else in it still read-only; and a session whose work item has no report, or that has no work item, shows a bounded excerpt of the latest message the Bot sent to that worker, a few lines through the same redaction as the session's row with the rest reached through the button, in place of "No report yet", which stays only for a session the Bot never sent a message; this supersedes, in part, `2026-11-03-a-crewmates-sessions-leave-the-sidebar-for-its-sessions-tab.md` (its "recap built from the crew log only" clause, and with it the sentences "the Sessions tab no longer opens it" and "shows its title and 'no report yet'" of `docs/request-for-change/rfc-mate-sessions-folder-or-lineage.md`), and every other rule of that entry stands.

## Why
- A Bot's sessions leave the person's session lists for this tab, and the tab gave no way to open one.
- The recap read only the work item bound to a session, so a session with no bound item always read "No report yet", whatever its worker had done.
- A worker's last reply can be long. The task the Bot last gave the worker is short and says what the session is for.

## Evidence
- The maintainer's restatement on the pull request that adds this entry: https://github.com/kirodotdev/KiroCrew/pull/36187#issuecomment-6862131743
- The issue for opening a session from the tab: https://github.com/kirodotdev/KiroCrew/issues/36175
- The issue for the recap that reads "No report yet": https://github.com/kirodotdev/KiroCrew/issues/36177
- The RFC section this changes: `docs/request-for-change/rfc-mate-sessions-folder-or-lineage.md`, "Owner decision: a click shows the recap in place".
- The pull request that built the recap in place: https://github.com/kirodotdev/KiroCrew/pull/28931
- The entry that names why a recap is empty ("A task that skips the ledger has no item, so the Bot's Sessions tab shows 'No report yet' for it"): `2026-11-06-every-task-a-bot-dispatches-goes-into-its-work-ledger.md`, which stands.
