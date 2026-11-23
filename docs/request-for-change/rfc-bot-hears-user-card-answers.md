---
title: A Bot wakes for one turn when the user answers its card or runs its command
status: implemented
author: buluoray, with kirocrew-worker
created: 2026-11-16
last-audited: 2026-11-20
audited-at: c157748343
doc-pr: 32182
implementation-prs: [32182]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: A Bot wakes for one turn when the user answers its card or runs its command

Status: implemented. The repository owner (buluoray) decided on 2026-11-16 to
keep this behaviour and to record it here, in the same pull request that builds
it ([#32182](https://github.com/kirodotdev/KiroCrew/pull/32182)), which merged
on 2026-11-17 (`dashboard/mate_user_actions.py`,
queued by `dashboard/mate_thread_wakes.py`). Every code reference was read at
`e1d7ff2428` with #32182 applied.

Related: [`rfc-crewmate-goals.md`](rfc-crewmate-goals.md) (its §3.8 wakes a Bot
after a goal or milestones card, and keeps a goal card's Undo silent; this RFC
keeps that rule) and [`rfc-crewmate-heartbeat.md`](rfc-crewmate-heartbeat.md)
(its §3.2 has the Bot hear a card's ending at the start of its next turn; this
RFC adds a turn for that ending and keeps the milestones card's Decline silent).

## 1. Problem

A Bot often needs the user to do something before it can go on: apply a
setting, a schedule or a tool grant on a card, or run a command it handed over.
Today most of those answers start no turn.

- Only the goal, milestones, capabilities, tool-access and page-offer cards wake
  the Bot when applied. Every other card kind (`setting.change`,
  `schedule.create`, `crewmate.update`, `mcp.*`, `secret.save`, ...) starts no
  turn on Apply, and no kind starts one on Decline or Undo.
- The outcome reaches the Bot only as a `[CHANGE CARD RESULTS]` block on its next
  turn, and the user has to start that turn.
- Run in terminal (a shell code block or a run-it-yourself card) tells the Bot
  nothing at all, ever.

So the Bot sits idle after the user answered, and the user has to type "done" or
"I declined it" to get the work moving again.

## 2. Decision

### 2.1 What wakes which Bot

| User action | Bot told | Note line |
|---|---|---|
| Apply, Decline or Undo on a card the Bot raised in its own thread | that Bot's own thread | one short line; the facts come from `[CHANGE CARD RESULTS]` (§2.3) |
| Undo pressed but refused or failed | that Bot's own thread | its own line: the change is still in place |
| Apply pressed again after a reported failure, and it failed again | that Bot's own thread | its own line, with the error |
| Run in terminal on a command from the Bot's own main transcript: ran, copied, or did not run | that Bot's own thread | its own line, with the source and the reason |

Exceptions, kept from the earlier RFCs:

- A card whose apply already wakes the Bot (goal, milestones, page offer,
  capabilities, tool access) marks the request `REQ_CARD_OWN_WAKE`, so that
  apply sends no second note.
- A `crewmate.goal` card's Apply and Undo send nothing (`rfc-crewmate-goals`
  §3.8). Its Decline and a failed Apply or Undo do.
- A `crewmate.key_results` (milestones) card's Decline sends nothing; the Bot
  hears it once through `[CHANGE CARD RESULTS]` (`rfc-crewmate-heartbeat` §3.2).

Nobody else is told: not the main assistant, a worker, an app, or another Bot.
A card raised outside a Bot's own pinned thread sends nothing. A Run in
terminal from any other chat, or from a side chat, worker window, reply thread
or app embed shown inside the Bot's page, still runs but sends nothing: the
request carries the slot of the transcript it came from
(`RunInTerminalOriginSlot`), and only the Bot's own main transcript reports.
Expiry, a closed chat's card retirement and hiding a
finished card are not the user's answer and send nothing.

### 2.2 One turn

Each answer becomes one line queued on the thread that owns the card or command
(`mate_thread_wakes.queue_user_action`). The thread's existing flush sends every
queued line as ONE hidden `[User action]` turn, and only once the thread is
idle. It never steers a running turn. A thread holds at most
`MAX_PENDING_LINES` (12) lines; past that the oldest is dropped, and the turn
says once how many earlier answers it does not list (`user_action.dropped`).

### 2.3 What the wake turn carries

The turn opens with `[User action]` and the shared `wake.preamble`, then the
queued lines, then a short guidance line. The guidance says these are facts
about what the user already did, not a new request.

A hidden turn already carries `[CHANGE CARD RESULTS]` for every card outcome the
Bot has not heard (`turn_dispatch_gates.py`, `take_unreported`). So a card line
does NOT repeat the card's title, kind, status, result or error: it is one fixed
line (`user_action.card_answered`) that points the Bot at that block, queued once
however many cards were answered. Two card outcomes the block cannot carry keep
their own line naming the card: an Undo that was refused or failed (the card's
status does not change), and an Apply that failed again after its first failure
was reported (the status repeats one already heard). Run in terminal has no
block, so its lines carry the facts.

## 3. Cost and loop limits

- **Who can start it.** Only the owner's own action in the dashboard: a card
  step on the card routes, or `POST /api/chat/slots/{slot}/terminal-run`, which
  is owner-only and refuses app tokens. The Bot cannot press its own card or
  report its own command run, so a Bot's own actions wake nothing.
- **No chain of wakes.** The wake turn is a hidden gateway turn. Nothing it does
  queues another `[User action]` line: a card it raises in that turn waits for
  the user's next press, and that press is a new human answer.
- **One turn per idle point.** Many answers while the Bot is busy become one
  turn, not one each. A busy thread waits.
- **Cost.** At most one extra model turn per idle point that follows a real user
  answer. A user who answers nothing costs nothing.

## 4. Alternatives rejected

| Option | Why not |
|---|---|
| Keep today's behaviour | The Bot stays idle until the user nudges it, which is the hand-holding a Bot's thread is meant to remove |
| A per-Bot or global setting to turn it off | Nobody asked for one; it can be added if users report unwanted turns |
| Steer the running turn | A steer interrupts work in flight; waiting for idle keeps the turn whole |
| Repeat the card facts in the note | `[CHANGE CARD RESULTS]` already carries them in the same turn; two copies drift apart |
| A new channel for these notes | `mate_thread_wakes` already delivers worker reports and upgrade notices this way |

## 5. How to undo it

Revert the pull request that ships it. Its parts are additive: the
`mate_user_actions` module, the `terminal-run` route, the `queue_user_action`
queue in `mate_thread_wakes`, the `_note_card_step` hook in `change_cards`, the
frontend report in `useRunInTerminalBridge`, and the `user_action.*` prompt
strings. With them gone, card outcomes reach the Bot only through
`[CHANGE CARD RESULTS]` on its next turn, as before.
