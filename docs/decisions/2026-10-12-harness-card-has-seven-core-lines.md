# The harness card shows exactly seven core lines and the count covers them alone

Decided by: Joe Guo (maintainer, @iamwhatever)
Date: 2026-10-12

## Decision

The user-facing card for each ACP backend shows exactly seven capability lines
("core"). The count "supports N of 7" covers these seven lines; no other line
contributes to the number.

A line is core if and only if all three hold:

1. Its absence is a **loss**, not an alternative: both states are not "correct" —
   one means the user gave something up.
2. The user asks it when **choosing or using** the harness, not while configuring
   a per-session detail.
3. It answers **"does Crew work here?" or "can I do X here?"**, not "how does X
   work here?" or "what protocol carries X?"

The seven lines that satisfy the criterion:

| Id | Criterion test |
|---|---|
| `crew_tools` | Crew's own tools available? Absent: no tool calls. |
| `member_thread_tools` | Crewmate DM thread gets member tools? Absent: no crewmates. |
| `side_chat_tools` | Side Chat can run read-only tools? Absent: side turns do nothing. |
| `mid_turn_steer` | User can add a message to a running turn? |
| `manual_compact` | User can /compact the session? |
| `reasoning_effort` | User can set the model's reasoning effort? |
| `model_switch` | User can switch models in the session? |

Capabilities that do NOT meet the criterion go to detail, security, or operator
lines: they describe how a feature works or what the harness does internally —
how a saved spec loads, which verb carries a slash command, which credential
store the harness uses.

## Why

A count is useful only when the user can predict what moves it. A criterion written
as examples invites every new capability to argue for inclusion by analogy. A
binary three-part test lets the card grow consistently as new capabilities land
without reopening the curation.

The test also resolves the one recurring disagreement: `member_saved_agent`
(how a crewmate's custom spec loads) and `subagent_continuation` (whether a
subagent session can be continued) are detail lines not because they are minor
but because they answer "how" rather than "can I". A user chooses the harness for
a different reason and discovers the detail after.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/19071 — the PR that introduces this
  curation, with `test_the_core_card_is_the_approved_seven` pinning the set.
- https://github.com/kirodotdev/KiroCrew/pull/19071#issuecomment-6120082116 — the
  maintainer's on-record statement confirming the seven-line decision and the removal
  of the zero-consumer complete-capabilities surface.
