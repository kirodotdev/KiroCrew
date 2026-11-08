# A crewmate's sessions are grouped by lineage stamps and leave the person's session list for the mate's Sessions tab

Decided by: Joe Guo (maintainer, @iamwhatever)
Date: 2026-11-03
Supersedes: 2026-10-17-crewmate-chat-hands-off-to-worker-sessions.md

## Decision
A crewmate's sessions are grouped by lineage stamps (`mate_root`, `mate_dispatched_by`), not by the mate home folder: the sessions a mate dispatches leave the person's session lists and show on the mate's Sessions tab, where a click expands the row in place into a recap built from the crew log only, and Crew Mode and mate conductors lose their folder tools and folder guidance (path B of `docs/request-for-change/rfc-mate-sessions-folder-or-lineage.md`); this supersedes the sidebar clause of `2026-10-17-crewmate-chat-hands-off-to-worker-sessions.md` ("appears in the sidebar", "stays visible as the worker sessions themselves in the sidebar") once the mate-worker preview flag of `rfc-crewmate-workers-under-the-crewmate.md` step 6 is on by default (step 9), and every other rule of that entry stands.

## Why
- Lineage stamps group a mate's sessions at every depth; folder filing reaches only the mate chat's direct children, and it fails a new mate's first session at the folder cap.
- The person's own session list holds no mate sessions. Under the folder path each mate adds a folder there that the person cannot hide.
- Until the flag is on by default, nothing the person sees changes: mate workers stay in the sidebar and keep filing into the mate home folder, so the 2026-10-17 sidebar clause still describes the product and is not yet replaced. This entry does not change the flag default.

## Evidence
- RFC that asked the question and records the pick: `docs/request-for-change/rfc-mate-sessions-folder-or-lineage.md`, merged by the maintainer in https://github.com/kirodotdev/KiroCrew/pull/27837 (`eef67c812a`), with the maintainer's note https://github.com/kirodotdev/KiroCrew/pull/27837#issuecomment-6595994590
- RFC for the path B design and its migration plan: `docs/request-for-change/rfc-crewmate-workers-under-the-crewmate.md`, merged in https://github.com/kirodotdev/KiroCrew/pull/26643
- First implementation step (server stamps, inert): https://github.com/kirodotdev/KiroCrew/pull/26952
- The maintainer's restatement on the pull request that adds this entry; that comment is the other on-record artifact this entry rests on.
