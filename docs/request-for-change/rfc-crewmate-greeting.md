---
title: Crewmate greeting on open — the warm resume card and the cold welcome
status: accepted
author: iamwhatever
created: 2026-10-07
last-audited: 2026-10-07
audited-at: c65697eaf6
doc-pr:
implementation-prs: [17797, 17822]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Crewmate greeting on open — the warm resume card and the cold welcome

- Status: accepted. This is the product owner's decision, given 2026-10-07 in the
  "Crew page polish" goal ("as a mate, it should send a welcome message when the
  user starts to chat, guiding them on what to do, based on the past tasks the
  user assigned to that mate or that are paused"). It adds to the crewmate DM
  rules of [rfc-crewmates-launch.md](rfc-crewmates-launch.md). Claims about
  today's code were checked at `c65697eaf6` (main).
- Author: iamwhatever
- Implementation: warm resume [#17797](https://github.com/kirodotdev/KiroCrew/pull/17797),
  cold welcome [#17822](https://github.com/kirodotdev/KiroCrew/pull/17822)
  (stacked on #17797).

## 1. Summary

When the user opens a crewmate's chat on the Crewmates page, the chat can open
on one card above the transcript, in the crewmate's voice:

| Open | Card |
|---|---|
| A goal is in flight | **warm**: where the goal stands and the next step |
| No goal in flight, thread new or idle 6h+ | **cold**: goals it left open, its recent sessions |
| Anything else | none |

Both kinds come from one hook, `useMateGreeting` in
`website/src/pages/members/mateGreeting.ts`, so one open shows at most one card.
Neither calls a model or adds a chat turn: both read data the crewmate already
recorded.

## 2. Motivation

At `c65697eaf6`, a crewmate's chat opens on its transcript and nothing else
(`MembersPage.tsx` mounts `ChatPane` with no greeting). A brand-new crewmate gets
one seeded first message at creation (`greeting_seed`), and that is the only
greeting there is. A user coming back mid-goal has to read back or open the Crew
board to learn what finished and what is stuck. A user coming back after a day
has to remember what they gave this crewmate, or ask it, which spends a turn and
a model call on something already written down in its work ledger and sessions.

## 3. Decision

**Who sees it.** The owner, on the Crewmates page, in a crewmate's DM. Every
read is owner-gated like the other `/api/members/*` reads. No other surface
(chat page, channels, side chat) shows it.

**What it reads.**

- Warm: the crewmate's own work ledger, through the masked `GET /api/crew-board`
  the Crew board already uses.
- Cold: `GET /api/members/{slug}/recap`. Open goals first (the thread's session
  ledger, then a recent session's), then the three newest other sessions that ran
  as this crewmate. Member threads, incognito rows and untitled rows are skipped.
  Each line is folded, redacted and capped at 120 characters.

**When it fires.**

- Once per open of a confirmed thread, never on every render.
- Never while the crewmate's own turn runs: its reply is about to say where
  things stand. A turn starting takes a shown card down, and a read still in
  flight when a turn starts is dropped.
- Warm wins: the cold read runs only when the board says no goal is in flight.
- Warm repeats only when the status changed, or after 15 minutes
  (`RESUME_REPEAT_MS`), so hopping between crewmates or a reconnect does not
  bring back a card the user just dismissed.
- Cold fires when the thread is new or idle for at least `COLD_AFTER_MS`, and
  once per idle stretch: it is remembered against the roster's
  `last_active_ts`, so a reload with nothing new stays quiet and the next idle
  stretch welcomes again. A recap with nothing in it draws no card.
- A failed read says so through `ErrorNotice` (a crewmate with no ledger is not a
  failure); it never blocks the chat.

**Why 6 hours.** The cold welcome answers "where were we" after the context has
gone cold for the user. Six hours is past a lunch break or a meeting block, where
the user still remembers, and short of the next working day, where they do not.
It is one named constant, `COLD_AFTER_MS` in `mateGreeting.ts`, read nowhere
else, so a different value is a one-line change once real idle patterns are
measured. No telemetry backs the number today; that is the open question in §7.

**Off switch.** None. The card is a dismissable recap with no side effect: it
sends nothing, writes nothing to the transcript, and costs no model call. A
dismiss holds for the open (warm: 15 minutes for an unchanged status; cold: the
rest of the idle stretch). Crew Members itself is a feature preview behind
**Settings → Developer → Feature Previews**, which turns the whole page off. A
per-user setting would be a new settings field for a card that can already be
closed in one click; it is added only if users ask for it.

## 4. Non-goals

- Carrying the recap into the crewmate's next turn. The user's reply names the
  task in their own words; a context-injection path is a separate decision.
- Acting from the card (answering, resuming, unblocking). The user does that in
  the chat.
- Any model-written summary.

## 5. Backward compatibility

Compatible. Both cards are new and appear only above a crewmate DM. Nothing is
removed, and every chat with no in-flight goal and recent activity opens exactly
as on `c65697eaf6`.

## 6. Alternatives considered

- **A model-written welcome turn.** Rejected: it spends a model call on every
  cold open and writes a turn into the transcript, which the model then reads
  back as its own speech. The data is already recorded.
- **Two independent greetings.** Rejected: one open could show both. One hook
  with a union of kinds makes "at most one card" true by construction.
- **Greeting on every open.** Rejected: switching between crewmates would repeat
  it constantly.
- **A user setting for the idle threshold or an off toggle.** Deferred to
  evidence (§7); the card is cheap to dismiss.

## 7. Open questions

- Is six hours right? Measure the gap between a user's last message and their
  next open of the same crewmate, and move `COLD_AFTER_MS` to the observed
  knee.
