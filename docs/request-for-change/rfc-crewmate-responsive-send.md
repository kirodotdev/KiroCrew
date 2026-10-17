---
title: Crewmate responsive send — answer the person first, schedule the work behind it
status: accepted
author: buluoray
created: 2026-10-15
last-audited: 2026-10-15
audited-at: 37fa963dac
doc-pr:
implementation-prs: [20240]
tracking-issues: [20181]
supersedes: []
superseded-by: []
---

# RFC: Crewmate responsive send

- Status: accepted. This is the product owner's decision, given 2026-10-15 by
  rayrayxu. Claims about today's code were checked at `37fa963dac` (main).
- Author: buluoray
- Related: [#20181](https://github.com/kirodotdev/KiroCrew/issues/20181),
  implementation [#20240](https://github.com/kirodotdev/KiroCrew/pull/20240),
  [rfc-crewmates-launch.md](rfc-crewmates-launch.md),
  [docs/decisions/2026-10-09-crewmate-chat-draws-messages-only.md](../decisions/2026-10-09-crewmate-chat-draws-messages-only.md).

## 1. Summary

A crewmate DM reads like a conversation with a colleague. When the person sends
a message while the crewmate is working, the crewmate answers them soon, then
goes back to the work, instead of making them wait for the running turn to end.

- The composer offers one **Send**. While work runs and nothing is typed, the
  control is **Stop**, as on main. Once the person types, it is the ordinary
  Send icon: never the Steer / Queue / Jev-auto split.
- The gateway decides how the send reaches the crewmate: steer first, and
  cancel the running turn only if the steer is still unread after a 3-second
  grace.
- Scheduling machinery is not drawn in the crewmate chat: no queue card, no
  "interrupted" or "steered" badge, no worker cards. Questions, approvals,
  errors, the stop card and main's typing bubble with its tool line stay.

The Sessions page keeps every control it has today. Ordinary sessions are
unchanged.

## 2. Problem

Verified at `37fa963dac`:

| Today on main | Effect on the person |
|---|---|
| `MembersPageThread.tsx` mounts `ChatPane` in the default split busy mode | A send while the crewmate works asks the person to pick Steer, Queue or Jev auto |
| Queue picks wait for the running turn | Pod benchmarks: 36–38 s before the crewmate answered a queued message |
| A queued message is drawn as a yellow queue card, separate from the bubbles | The message jumps from bubble to card to bubble, and replies land above it |
| Steer and interrupt badges are drawn on user rows | The person reads runtime scheduling they did not ask about |

A person talking to a crewmate does not know or care that a turn is running.
Asking them to choose a scheduling mode, and then waiting half a minute, makes
the crewmate feel stuck.

## 3. Decision

### 3.1 Send path (gateway)

1. A crewmate DM send carries `responsive: true`. The gateway admits it only for
   the member's own DM slot (`slot.mode == members.DM_SLOT_MODE`, bound to that
   member) and treats it as an ordinary send everywhere else.
2. The message is written to the existing queue first, so no later step can
   lose it.
3. It is offered to the running turn as a steer. If the harness reports the
   steer consumed within **3 seconds**, nothing is cancelled.
4. If the steer is still unread after the grace, the gateway makes one bounded,
   exact-owner cooperative cancel of that turn, and the message leads the next
   turn. A harness without steer support, or a steer the backend refused, has
   nothing to wait for, so that send cancels at once.
5. No cancel while a question card, an approval, a compaction or a Stop is
   pending; the message queues behind it.
6. Messages that wait together drain as one turn, so a burst gets one reply.
7. The next turn carries turn-only guidance: answer the person first, hand
   heavy remaining work to the background, do not mention the pause, and for
   each earlier tool step say whether it finished. A read-only step may be
   re-run; a step that changes something is checked before it is run again.

Measured on a pod: a busy crewmate answered in about 1.5 s with this path,
against 36–38 s with plain queueing.

### 3.2 Composer and transcript (dashboard)

- Busy and empty: **Stop**, with main's soft-stop and force-stop escalation.
- Busy and typed: the ordinary **Send** icon. Pressing it takes the path in
  §3.1.
- A waiting message stays the person's own bubble where it was sent. No queue
  card is drawn, so it cannot be edited or cancelled from the crewmate chat;
  the Sessions page still can, until its row is written.
- The stop card (`stop_event`) stays drawn: it records the person's own press.
- Runtime notices stay hidden, per the 2026-10-09 decision.

## 4. Costs accepted

- **A long tool call can be cut.** A steer is read only between tool calls, so
  a tool that runs past 3 seconds is cancelled and re-run. kiro-cli stops the
  tool when the turn is cancelled; its result is not recoverable. Whether a
  write is repeated rests on the turn guidance in §3.1 step 7, not on a runtime
  guarantee.
- **A fast answer is likely, not guaranteed.** The cancel can time out, and the
  model can still call a tool before it speaks.
- **No queue editing in the crewmate chat.** The person who needs it uses the
  Sessions page.

## 5. Non-goals

- Changing ordinary sessions, the Sessions page, or channel (Slack, Discord, …)
  chats.
- A setting for the grace period or for turning the behaviour off.
- A runtime rule that the model must speak before it may call a tool.
