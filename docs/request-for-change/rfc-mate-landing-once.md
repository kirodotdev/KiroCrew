---
title: Mate gets one landing, then the ordinary landing applies
status: accepted
author: iamwhatever
created: 2026-10-10
last-audited: 2026-10-11
audited-at: a84f616557
doc-pr: 18903
implementation-prs: [18904]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Mate gets one landing, then the ordinary landing applies

- Status: accepted. buluoray, the author of rfc-crewmate-guides-and-mate,
  approved it on #18903 on 2026-10-11 and asked for the status to be flipped.
  The "owed" condition in § 2 is the one gap that review raised (Design lane
  finding, checked at main); it is folded in here. Proposed on a user report
  of 2026-10-10: a user was moved onto
  Mate on every bare Crewmates visit. The rule below is the one buluoray, the
  author of [rfc-crewmate-guides-and-mate.md](rfc-crewmate-guides-and-mate.md),
  asked for in the review of #18903. It amends that document's "When it opens"
  paragraph and its amendment of
  [rfc-crewmates-chatted-list.md](rfc-crewmates-chatted-list.md) § 3, in its own
  document so the accepted one is not edited. Claims about today's code were
  checked at `73d44b420b` (main).
- Author: iamwhatever. Accepted by buluoray.

## 1. Problem

When Mate's welcome lands, main already does the right thing:

- The welcome is a real assistant row in Mate's thread
  (`dashboard/mate_welcome.py`: "The greeting itself is the crewmate's own
  assistant row").
- The roster's `has_dm_message` counts that row, including a live row that has
  not flushed yet (`dashboard/handlers/members.py`, the `has_dm_message`
  assignment in the roster read).
- So `pendingMate` (`website/src/lib/assistantMember.ts`) stops answering Mate,
  and on the next bare visit `resolveMateLanding` yields to the ordinary
  landing: the remembered crewmate, then `lastChattedMember` (order amended by
  #19341, see rfc-crewmates-chatted-list.md §3.1; the last-chatted step is
  replaced by the newest conversation, rfc-session-template-vs-crewmate.md
  rule 4).

The pin is a welcome that never lands. `maybe_start_first_greeting` claims the
once-only marker (`claim_greeting_marker`, `members/<slug>/welcome_claimed.json`)
BEFORE it dispatches the welcome turn. When that turn fails (a signed-out CLI,
no backend), no row is written and the marker is never released. Mate's thread
stays empty for good, no welcome is ever coming, and `pendingMate` answers Mate
on every bare visit. The user is pinned on an empty Mate and never reaches the
crewmate they last chatted with.

## 2. Decision

Mate gets ONE landing, for fresh and existing users alike. Once its welcome
has been sent, or attempted and failed, Mate counts as seen and the ordinary
landing applies.

| Mate's welcome | Lands on (bare visit, preview on) |
|---|---|
| still coming | Mate (one introduction, fresh and existing users) |
| sent | the ordinary landing (the remembered crewmate, then the newest conversation; #19341, rfc-session-template-vs-crewmate.md rule 4) |
| attempted and failed | the ordinary landing (counts as seen) |

- **Still coming** = the welcome is owed (`welcome_owed`), its marker is not
  claimed (`claim_greeting_marker`), and Mate's thread has no row
  (`has_dm_message`, which counts live unflushed rows). Anything else counts
  as **seen**: claimed, a row exists, or never owed. A Mate whose owed record
  was never written (it predates the record, or the write failed:
  `first_crewmate._owe_first_welcome` "only costs the welcome") is refused
  `NOT_OWED` before the claim, so it would otherwise read as "not sent yet"
  forever. All three are server-side, so the answer holds across browsers and
  gateway restarts. No new client marker.
- **Unreadable**: if the roster cannot say whether Mate was seen, the page
  uses the ordinary landing. A read failure never pins the user on Mate.
- An explicit `?member=` link still wins.
- `default` still does not count as a crewmate for any of this.
- Mate stays on the roster: the user can always open it.

The owed record, the marker and the row are read on the roster read the page
already makes; Mate's roster row carries the owed and claimed states beside
`has_dm_message`.

## 3. What this amends

- [rfc-crewmate-guides-and-mate.md](rfc-crewmate-guides-and-mate.md) "When it
  opens": "while the user has never exchanged a message with it" becomes
  "while its welcome is still coming (owed, unclaimed, no row)".
- The same document's amendment of rfc-crewmates-chatted-list § 3 ("a
  never-chatted Mate is opened first") takes the same condition.

## 4. Alternatives considered

- **Mate yields once its welcome is sent or attempted. Chosen.** It keeps the
  original intent: every user, fresh or existing, is introduced to Mate once.
  It ends the pin, because a failed welcome counts as seen. Both signals are
  server state the gateway already writes.
- **Mate first until the user replies.** Rejected. A user who reads the
  welcome and leaves would land on Mate again and again, ahead of the crewmate
  they actually talk to. A failed welcome would still pin them, since nothing
  was sent to reply to.
- **Release the marker when the welcome turn fails, so it is retried.**
  Rejected. A failing backend (signed-out CLI) fails every retry too, so the
  user would land on an empty Mate on each visit until it is fixed: the same
  pin. It also reopens the double-greet race the once-only claim closes.
- **Never land on Mate for a returning user** (yield whenever another crewmate
  was chatted with or is remembered; the first draft of this document).
  Rejected. It drops the original decision's point: existing users would never
  be introduced to Mate unless they went looking.

## 5. Tests

The implementation (#18904) pins:

1. An existing user (another crewmate chatted with, Mate's welcome owed, its marker unclaimed,
   Mate's thread empty) lands on Mate once.
2. After the welcome row exists, the next bare visit lands on the ordinary
   landing (the remembered crewmate, then the last-chatted one — #19341; at
   #18904 the last-chatted one came first; rfc-session-template-vs-crewmate.md
   rule 4 replaces the last-chatted step with the newest conversation).
3. With a claimed marker and an empty thread (a failed welcome), the next bare
   visit lands on the ordinary landing likewise.
4. A roster row that does not say whether the marker is claimed, or whether
   the welcome is owed, lands on the ordinary landing.
5. An empty Mate with no owed record and no marker lands on the ordinary
   landing.
6. A fresh user (no other crewmate) still lands on Mate and gets its welcome.
7. `?member=` still wins; `default`'s chats do not count; Mate is still
   openable from the roster.
8. Backend: the roster row reports the marker as claimed after
   `claim_greeting_marker` and as unclaimed before it, and reports the owed
   state.

## 6. Rollout

One pull request, #18904: Mate's roster row reports whether its welcome is
owed and whether its marker is claimed, and the page lands on Mate only while
the welcome is owed, the marker is unclaimed and the thread has no row.
