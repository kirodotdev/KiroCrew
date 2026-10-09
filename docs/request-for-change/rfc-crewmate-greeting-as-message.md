---
title: The crewmate greeting is a message in its chat, not a card above it
status: accepted
author: iamwhatever
created: 2026-10-09
last-audited: 2026-10-09
audited-at: d4c3a7334d
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: The crewmate greeting is a message in its chat, not a card above it

- Status: accepted. This is the product owner's decision, given 2026-10-09 in
  the "Crew page polish" goal: the warm "Where we left off" greeting reads as a
  pop-out card, and it should read as a message from the crewmate in the chat,
  so the crewmate feels like a partner and not a UI popup; the cold welcome the
  same, so the two stay one greeting. It amends §1 and §5 of
  [rfc-crewmate-greeting.md](rfc-crewmate-greeting.md) in this document, so
  that accepted document is not edited. Claims about today's code were checked
  at `d4c3a7334d` (main).
- Author: iamwhatever

## 1. Today

At `d4c3a7334d`, `MembersPage.tsx` mounts `MateResumeCard` (warm) or
`MateWelcomeCard` (cold) in the block between the DM header and `ChatPane`.
Both draw a bordered `bg-card` panel with the crewmate's avatar, across the
top of the chat. That is the "one card above the transcript" of
rfc-crewmate-greeting §1.

## 2. Decision

The greeting draws as **the crewmate's message at the end of its chat**:

- The same bubble and row as the crewmate's replies (`crewmateBubbleClass`,
  `CrewmateMessage`, rfc-crewmates-launch's bubble rules): no avatar and no
  author line, since the DM header already names the one speaker.
- After the last transcript row, inside the scroll container, where the next
  reply would land. The host hands it to `ChatPane` as a trailing node; the
  pane draws it only for a crewmate chat.
- The hide action stays, as a small worded button under the bubble.

Unchanged from rfc-crewmate-greeting: what it reads, when it fires, when it
goes away (the user's own turn takes it down; a patrol turn does not), at most
one greeting per open, and the failure notice. It is still **drawn, never
sent**: no model call, no row in the slot's transcript, so the model never reads
it back as its own speech (rfc-crewmate-greeting §6, first alternative). A
reload shows it again only under the existing repeat rules.

## 3. What this amends

- rfc-crewmate-greeting §1 "on one card above the transcript": read as "as one
  message from the crewmate at the end of its chat".
- rfc-crewmate-greeting §5 "appear only above a crewmate DM": read as "appear
  only inside a crewmate DM".

## 4. Alternatives considered

- **Write the greeting into the transcript as a real assistant row.** Rejected
  for the reason rfc-crewmate-greeting §6 gives: the model would read it back as
  its own words, and every open would grow the history.
- **Keep it above the transcript, restyled as a bubble.** Rejected: a bubble
  over the oldest visible rows still reads as chrome, and it scrolls away from
  where the user is reading.
