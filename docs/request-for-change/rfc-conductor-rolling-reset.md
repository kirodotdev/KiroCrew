---
title: Conductor rolling reset -- a conductor drops its chat after a turn that closes an item or fills a free seat
status: superseded
author: iamwhatever, with kirocrew-worker
created: 2026-10-19
last-audited: 2026-10-19
audited-at: c3f2b27de7
doc-pr: 22088
implementation-prs: []
tracking-issues: [22082]
supersedes: []
superseded-by: [rfc-conductor-context-cap.md]
---

# RFC: Conductor rolling reset

> **Superseded before acceptance.** On 2026-10-19 the operator who owns the
> conductor patrol (iamwhatever) decided that a prompt rule cannot bound the
> conductor's context, and that the gateway bounds it instead:
> [rfc-conductor-context-cap.md](rfc-conductor-context-cap.md) replaced this
> proposal. Nothing here was built, and its amendment of
> [rfc-conductor-round-reset.md](rfc-conductor-round-reset.md) never took
> effect. The sections below are the record of the proposal and of the
> measurements that led to it.

- Status: superseded. While it was a draft, the decision belonged to the
  operator who owns the conductor patrol and wrote
  [rfc-conductor-round-reset.md](rfc-conductor-round-reset.md) (iamwhatever).
  This document landed on its own first, because the First Principles lane
  reads an RFC's status off the base branch, and the implementation would have
  changed what the `kirocrew-conductor` agent and the goal-conductor skill do
  by default.
- Amends [rfc-conductor-round-reset.md](rfc-conductor-round-reset.md) in three
  named places: its first goal (when a conductor resets), its non-goal
  "Resetting mid-round, or on a timer" (what mid-round means) and the "Skill and
  prompt" paragraph of its section 3. Section 4 below says what survives of
  each. That document is
  `implemented` and is not edited; this one carries the amendment, the way
  [rfc-wake-on-stop-signal.md](rfc-wake-on-stop-signal.md) carries its own.
- Tracking issue: [#22082](https://github.com/kirodotdev/KiroCrew/issues/22082).
- Measured at `0adc85c233`.

## 1. Problem

rfc-conductor-round-reset lets a goal conductor drop its chat with
`reset_conversation` at a round close, "after the next round is dispatched".
The goal-conductor skill and `_CONDUCTOR_SYSTEM_PROMPT` in
`src/kiro_crew/agent.py` carry that rule.

The same skill also runs a rolling queue, and its sections disagree:

| Section of the goal-conductor skill | What it says |
|---|---|
| Close the round, Concurrency cap | "keep patrolling what is in flight and dispatch the next item when one lands" |
| Close the round, first line | "When every item in the round has landed" |
| Then drop your chat, Never reset when | "the round is still running — reset only at a round close" |

Under the concurrency cap a seat is filled as soon as an item lands, so some
item is always open. "Every item in the round has landed" never holds, and the
reset never comes. Every patrol wake then re-sends a chat that only grows.

Measured on one operator's conductor DM (`member-kirocrew-conductor`, one
session):

| Window (UTC) | What happened | `reset_conversation` calls |
|---|---|---|
| 2026-10-07 to 10-16 | 696 turns | 0 |
| 2026-10-16 21:10 to 10-18 06:00 | the last call came in the turn that closed a round's last 3 items | 6, all queued |
| 2026-10-18 06:00 to 10-19 05:00 | 10 patrol turns closed an item or dispatched one into a free seat | 0 |

On 2026-10-19 at 04:57 that conductor closed one item, dispatched the next
round's first item into the freed seat, and did not reset, because the previous
round's other items were still open.

What the missing reset costs, measured on the same host: a DM request carried
450K to 550K tokens. A request after a reset was about 2 to 5 s faster (first
output 12.7 s down to 8.4 s) and cost about 75% fewer credits. This DM's
patrol wakes come 900 s apart, longer than the prompt cache lives (about 5
minutes, inferred from the step in first-output time at 5 to 7 minutes idle),
so each wake pays for the whole chat.

## 2. Goals and non-goals

Goals:

- A conductor whose queue never empties still drops its chat: after a patrol
  turn that closed an item or dispatched one into a free seat, once that turn's
  work is at rest.
- The skill states one reset rule, and its Concurrency cap and Close the round
  sections both lead to it.

Non-goals, kept from rfc-conductor-round-reset:

- Any agent other than `kirocrew-conductor` gaining the tool.
- Letting a wake run `set_project` or `chat_tag`. They stay human-only.
- Resetting mid-round, with the meaning section 3.2 gives it, or on a timer.
- Changing what a reset drops.

New non-goals:

- A reset the gateway forces.
- Any change to the grant or to the gateway's admission checks
  (`_refuse_unvouched_wake_reset`).
- A context-size hint in the wake.
- Crewmate DMs that are not conductors.
- A Crew-side compaction threshold on KAS:
  [#18931](https://github.com/kirodotdev/KiroCrew/issues/18931) keeps the
  engine's own.

## 3. Design

### 3.1 The reset point

A patrol turn that closed an item, or dispatched one into a free seat, ends
with two calls, `session_ledger_record` and then `reset_conversation`, when all
three hold:

| Hold | Why |
|---|---|
| Every item the turn touched is at rest: created, bound and seeded, or its verdict recorded and closed | a half-done step lives only in the chat |
| The session ledger is written first, in that turn: `goal`, `next` (each open item id, its worker key and its PR) and `patrol_base`, checked against a fresh `work_ledger_read` | the next wake resumes from it alone |
| No question to the person is unanswered, and no worker question the turn read is unanswered | the answer would land in a chat that no longer holds the question |

A worker question is answered with `session_send` and then
`work_ledger_record` `action=decide`, so it is on record before the reset.

A round close is one such turn, so a conductor that dispatches in batches keeps
the behaviour it has today. Other items still being open does not hold the
reset back: their state is in the work ledger, and each wake carries its
snapshot.

### 3.2 What "mid-round" means

For the reset, mid-round means partway through those steps: an item created
but not yet bound and seeded, or a verdict recorded on an item not yet closed.
It no longer means "other items are still open". The original non-goal,
"Resetting mid-round", stands with this meaning.

The skill's other uses of "round" keep theirs: re-planning waits for a round
to finish, and a changed goal is applied at the round boundary.

### 3.3 Skill and prompt

- The goal-conductor skill's reset section states 3.1. Its "Never reset when"
  list adds the unanswered worker question and the partway case, and drops
  "the round is still running". A refused reset is tried again at the next
  turn that closes an item or fills a free seat.
- Its Concurrency cap section, which already says to dispatch the next item
  when one lands, points at the same rule, so the two no longer contradict.
- `_CONDUCTOR_SYSTEM_PROMPT` carries the same rule in its one paragraph.
- A text pin in `test/test_conductor_agent.py` holds both surfaces to the rule
  and to the absence of the old gate.

### 3.4 Unchanged

The gateway is not touched. `apply_session_directive` in
`src/kiro_crew/dashboard/session_directive_apply.py` still admits a wake's
reset only through `_refuse_unvouched_wake_reset`: a conductor agent, the
slot's current live loop, a recorded self-arm, and nothing waiting on a person.
A refused reset still changes nothing.

## 4. What survives of rfc-conductor-round-reset

| Place | Before | After |
|---|---|---|
| Goals, first bullet | resets at a round close only | resets at a round close, or after any patrol turn that closed an item or filled a free seat, once its work is at rest |
| Non-goals, "Resetting mid-round, or on a timer" | mid-round reads as "items still open" | mid-round is partway through the turn's own steps; the timer non-goal is unchanged |
| Section 3, "Skill and prompt" | "after the next round is dispatched … never mid-round" | section 3.1 and 3.3 of this document |
| Section 3, Grant, Admission from a wake, The trust record, Log | | unchanged |
| Sections 4 to 6, Risks, Security, Alternatives | | still hold; this document's sections 5 to 7 add to them |

## 5. Risks

- **More resets.** A conductor now resets after most patrol turns that close or
  fill. A reset drops what the chat held beyond the two ledgers. The ledger
  write comes first, so the worst case is still one wake that re-reads the work
  ledger to rebuild `next`. A person's own turn does not trigger this rule, and
  an unanswered question to the person still blocks it.
- **A fresh context each time.** The wake after a reset starts from the base
  context, which on the same host measured 51K to 69K tokens, instead of
  several hundred thousand. That wake is cheaper, not dearer.

## 6. Security

No new admission path. The checks in `_refuse_unvouched_wake_reset` are
unchanged, so rfc-conductor-round-reset's bound on harm holds: a reset drops
only the model's memory of the conductor's own slot.

## 7. Alternatives considered

- **Dispatch in batches only.** The old rule would then fire, but the conductor
  would leave seats empty while a round drains, which is what the concurrency
  cap's refill exists to avoid.
- **Reset after every patrol turn.** It would also reset on quiet cycles and
  in the middle of a step. "At rest" is what makes a reset safe.
- **Reset on context size.** It needs a size reading in the wake, which is out
  of scope here.
- **Compact instead of reset.** Rejected in rfc-conductor-round-reset section
  6, and on KAS the engine owns compaction (#18931).
- **Reset from the gateway.** Rejected in rfc-conductor-round-reset section 6:
  only the conductor knows when its state is in the ledger.

## 8. Open questions

None for the design. Acceptance is the operator's.

## 9. Rollout

One implementation pull request after this document is accepted: the prompt
and skill text, and the text pin. No migration and no stored data change.
