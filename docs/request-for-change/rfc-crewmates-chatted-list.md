---
title: Crewmates list — only the crewmates the user chatted with, newest first
status: superseded
author: iamwhatever
created: 2026-10-07
last-audited: 2026-11-04
audited-at: e86fbdc606
doc-pr:
implementation-prs: [17808]
tracking-issues: [19341, 29769]
supersedes: []
superseded-by: [rfc-session-template-vs-crewmate.md]
---

# RFC: Crewmates list — only the crewmates the user chatted with, newest first

> **Superseded.** On 2026-10-09 the product owner decided that the Crewmates
> list is a messages list: a thread with a message is listed, whoever sent it.
> [rfc-session-template-vs-crewmate.md](rfc-session-template-vs-crewmate.md)
> §3 rules 3 and 4 replaced this document's §3, and
> [#18342](https://github.com/kirodotdev/KiroCrew/pull/18342) built it:
> `crew_recency.json` and `last_chat_ts` are gone, and Recent order reads
> `last_active_ts`. So the §2 ask below that a crewmate which only ran in the
> background is never shown no longer holds; a heartbeat wake's message lists
> its row. At `e86fbdc606`, `listedByDefault` in
> `website/src/pages/members/rosterFilter.ts` lists a starred row, Mate, a
> crewmate the person created (restored by
> [#24987](https://github.com/kirodotdev/KiroCrew/pull/24987)), the `default`
> row once the person sent it a message, and any other row once its DM thread
> holds a message; a row from an older gateway that sends no message field is
> listed. The sections
> below are the record of the 2026-10-07 decision.

- Status: superseded by
  [rfc-session-template-vs-crewmate.md](rfc-session-template-vs-crewmate.md).
  It was accepted as the product owner's decision, given 2026-10-07 in the
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
crewmate the user last opened in this browser, else the one last chatted with
(§3.1). The chat record is kept on the server, so it survives a new browser.

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
one last talked to. (Reversed 2026-10-09: see the note at the top. Listing and
landing follow the thread's messages, whoever sent them; only the `default` row
still needs a message from the person.)

## 3. Decision

| Rule | Before | After |
|---|---|---|
| Listed unasked | DM holds a message, dashboard-created, starred, or the default crew | the user sent it a message or created it (`last_chat_ts > 0`), or starred it |
| Switcher list | every roster row | the column's own rule (#17835): the listed rows plus the open one; its search reaches every row |
| Recent order | `last_active_ts` | `last_chat_ts` |
| Landing with no `?member=` | browser memory, then greatest `last_active_ts` | greatest `last_chat_ts`, then browser memory, then greatest `last_active_ts` — **amended by #19341:** browser memory, then greatest `last_chat_ts`, then greatest `last_active_ts` (see §3.1) |

"The user sent it a message" means a `POST /api/chat` from the dashboard user:
no app token, no cron attestation, no peer relay. A chat with no crew picked
counts for the default crew. Creating a crewmate counts as well: the user's
`POST /api/agents` (owner-only, so never an app token; not an attested cron)
stamps the new crew, so it is listed at once and sorts first. A crew that the
agent sync, an app or any background writer adds is not stamped. The record
lives in `crew_recency.json` under the data home. A one-time seed fills it from typed rows already in DM threads.

### 3.1 Amendment (#19341): the landing reopens the row last opened

Ranking the server's last-chatted crewmate above the browser memory reopened
the wrong thread on every ordinary return: a crewmate the user clicked but did
not message never moves `last_chat_ts`, so leaving the page and coming back
landed on whoever was chatted with last, not on the row the user had open. The
product owner's call (2026-10-12): coming back to the Crewmates page lands on
the crewmate last opened.

| Rule | Before (this RFC) | After |
|---|---|---|
| Landing with no `?member=` | greatest `last_chat_ts`, then browser memory, then greatest `last_active_ts` | browser memory (any row still on the roster, `default` included), then greatest `last_chat_ts`, then greatest `last_active_ts` |
| `mc-members-last-member-chat-mark` | written beside the memory when `default` is opened, to rank a remembered `default` against the last chat | removed; the memory needs no tie-break once it ranks first |

`last_chat_ts` keeps every other job this RFC gave it (listing, recent order)
and stays the landing fallback for a browser with no usable memory — a new
browser, a cleared storage, a remembered crewmate since deleted — where it
still beats background recency. A gateway restart does not clear localStorage,
so the restart case this RFC set out to fix lands on the same row: the one the
user had open.

Considered and rejected: keeping the chat mark, generalized to every row, so
that a chat with another crewmate started elsewhere would move the landing.
The product owner's answer (#19346): a crewmate's conversation IS its Crewmates
thread; there is no other place to chat with one, so a chat that moves
`last_chat_ts` without an open on this page is not a case the landing has to
weigh. The memory alone is the rule.

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
- **Keep browser memory first.** Rejected at the time: it does not survive a
  new browser, and it names the last row clicked, not the last conversation.
  Reversed by §3.1: the product owner wants the last row clicked.
