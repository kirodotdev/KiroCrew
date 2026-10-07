---
title: Crewmates list — only the crewmates the user chatted with, newest first
status: accepted
author: iamwhatever
created: 2026-10-07
last-audited: 2026-10-07
audited-at: acc08092d7
doc-pr:
implementation-prs: [17808]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Crewmates list — only the crewmates the user chatted with, newest first

- Status: accepted. This is the product owner's decision, given 2026-10-07 in the
  "Crew page polish" goal. It amends the listing and landing rules of
  [rfc-crewmates-launch.md](rfc-crewmates-launch.md) (screen 02, "landing").
  Claims about today's code were checked at `acc08092d7` (main).
- Author: iamwhatever
- Implementation: [#17808](https://github.com/kirodotdev/KiroCrew/pull/17808),
  built on [#17835](https://github.com/kirodotdev/KiroCrew/pull/17835), which
  makes the header switcher run the roster column's own hide rule
  (`rosterPopulation` / `rosterShows`). The rule below therefore lives in that
  one shared rule, and the chip and the column cannot disagree.

## 1. Summary

The Crewmates page lists a crewmate only after the user has sent it a message,
either in its Crewmates DM or in a normal chat, or has created it. The list is ordered by the
user's last message, newest first. With no `?member=`, the page opens the
crewmate the user last chatted with. That record is kept on the server, so it
survives a gateway restart and a new browser.

## 2. Motivation

At `acc08092d7`:

- `CrewmateSwitcher` gets every roster row (`members={orderedMembers}` in
  `MembersPage.tsx`). On a host with installed packages, that is dozens of rows
  the user never used.
- `listedByDefault` (`rosterFilter.ts`) lists a row if its DM thread holds any
  message, if it was created on the dashboard, if it is starred, or if it is the
  default crew. A DM message also comes from background work. Dashboard creation
  and default-crew status say nothing about use.
- Recent order and the landing fallback read `last_active_ts`, the crew log's
  recency. A background turn moves it as well.
- The landing tries this browser's memory (`mc-members-last-member`) first. A
  restart, or a different browser, loses which crewmate the user last talked to.

The product owner's ask: show only the crewmates the user actually chatted with,
newest first. Never show one that only ran in the background (crons, wakes,
sub-agents, dispatched workers) or that an app drove. After a restart, open the
one last talked to.

## 3. Decision

| Rule | Before | After |
|---|---|---|
| Listed unasked | DM holds a message, dashboard-created, starred, or the default crew | the user sent it a message or created it (`last_chat_ts > 0`), or starred it |
| Switcher list | every roster row | the column's own rule (#17835): the listed rows plus the open one; its search reaches every row |
| Recent order | `last_active_ts` | `last_chat_ts` |
| Landing with no `?member=` | browser memory, then greatest `last_active_ts` | greatest `last_chat_ts`, then browser memory, then greatest `last_active_ts` |

"The user sent it a message" means a `POST /api/chat` from the dashboard user:
no app token, no cron attestation, no peer relay. A chat with no crew picked
counts for the default crew. Creating a crewmate counts as well: the user's
`POST /api/agents` (owner-only, so never an app token; not an attested cron)
stamps the new crew, so it is listed at once and sorts first. A crew that the
agent sync, an app or any background writer adds is not stamped. The record
lives in `crew_recency.json` under the data home. A one-time seed fills it from typed rows already in DM threads.

## 4. Non-goals

- Backfilling normal-chat history from before the record existed.
- Changing the full roster's filters, teams, star or sort controls.
- Changing what background work writes or shows.

## 5. Backward compatibility

`last_chat_ts` is a new field. A row from an older gateway does not carry it and
keeps the old rule, so a mixed-version deploy does not blank the list.

## 6. Alternatives considered

- **Hide rows in CSS, or cap the switcher's length.** Rejected: the list would
  still carry crews the user never used, just fewer of them.
- **Keep "created on the dashboard" as its own listing rule.** Rejected: a
  flag that never changes cannot order the list. Stamping the creation time on
  the same record lists the new crewmate first, then lets it age like any other.
- **Derive "chatted" from DM transcripts or the crew log.** Rejected: both also
  record background turns and peer deliveries, so the signal would stay noisy.
- **Keep browser memory first.** Rejected: it does not survive a new browser,
  and it names the last row clicked, not the last conversation.
