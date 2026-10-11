---
title: Mate's landing is for a first visit only
status: draft
author: iamwhatever
created: 2026-10-10
last-audited: 2026-10-10
audited-at: 4e9848549c
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Mate's landing is for a first visit only

- Status: draft. Proposed on a user report of 2026-10-10: a returning user who
  had chatted with a crewmate was moved onto Mate. It amends one paragraph of
  [rfc-crewmate-guides-and-mate.md](rfc-crewmate-guides-and-mate.md) ("When it
  opens") and its amendment of
  [rfc-crewmates-chatted-list.md](rfc-crewmates-chatted-list.md) § 3, in its own
  document so that accepted document is not edited. Claims about today's code
  were checked at `4e9848549c` (main).
- Author: iamwhatever. Acceptance asked of buluoray, the author of
  rfc-crewmate-guides-and-mate.

## 1. Problem

At `4e9848549c`, `MembersPage.tsx` runs `resolveMateLanding(members)` before
every other landing rule. It answers Mate whenever Mate's thread is empty
(`pendingMate`), whatever else the user has done. The ordinary landing
(`lastChattedMember`, then the remembered crewmate) never runs.

So a user who already chatted with crewmate B (`last_chat_ts` from
`crew_recency`) opens the page and lands on Mate. The conversation they came
back for is one click away. rfc-crewmates-chatted-list § 3 says the landing
opens the last crewmate the user chatted with; for these users it does not.

## 2. Decision

A bare Crewmates visit (no `?member=`, preview on) opens a never-chatted Mate
only while the user has no other crewmate to come back to:

| Mate's thread | Another crewmate chatted with (`last_chat_ts`) | Remembered crewmate (this browser) still listed | Lands on |
|---|---|---|---|
| empty | no | no | Mate (first visit, unchanged) |
| empty | yes | any | the last-chatted crewmate |
| empty | no | yes | the remembered crewmate |
| has a message | any | any | the ordinary landing (unchanged) |

- `default` is not "another crewmate": its `last_chat_ts` and a remembered
  `default` do not end Mate's first visit.
- An explicit `?member=` link still wins.
- Mate's welcome, `rename_self`, the once-per-visit rule below md and the
  Meet CrewMates hold are unchanged. A returning user who lands elsewhere can
  still open Mate from the roster and gets its first welcome then.

## 3. What this amends

- [rfc-crewmate-guides-and-mate.md](rfc-crewmate-guides-and-mate.md) "When it
  opens": "ahead of every other landing rule" becomes "ahead of every other
  landing rule while the user has no other crewmate chatted with or
  remembered".
- The same document's amendment of rfc-crewmates-chatted-list § 3 ("a
  never-chatted Mate is opened first") takes the same condition.

## 4. Alternatives considered

- Keep Mate first for everyone. Rejected: it hides the last conversation from
  every existing user until they talk to Mate.
- Key only on `last_chat_ts`. Rejected: a crewmate the user opened in this
  browser and left is also a conversation they came back for, and the ordinary
  landing already restores it.

## 5. Rollout

One pull request on `MembersPage.tsx` (`resolveMateLanding` takes the
remembered name and yields to either signal) with tests for both a fresh user
(lands on Mate) and a returning user (lands on the last-chatted crewmate).
