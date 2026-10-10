---
title: Peer-sent reset -- a session sent work by another session may drop its own chat context
status: in-progress
author: Pearcekieser, with kirocrew-worker
created: 2026-10-09
last-audited: 2026-10-09
audited-at: 96c00c10cc
doc-pr: null
implementation-prs: [17493]
tracking-issues: [17471]
supersedes: []
superseded-by: []
---

# RFC: Peer-sent reset

- Status: in-progress. The implementation is [#17493](https://github.com/kirodotdev/KiroCrew/pull/17493). The operator who runs the worker fleet asked for it in [#17471](https://github.com/kirodotdev/KiroCrew/issues/17471). This document lands on its own first, as GOVERNANCE.md asks of an RFC, so the implementation's First Principles lane can read the decision off the base branch.
- Adds a second case to the human-only `reset_conversation` gate, beside the conductor round-close case in [rfc-conductor-round-reset.md](rfc-conductor-round-reset.md). That document says the gate opens in "exactly one case"; this one adds another in its own document, so the conductor decision is not edited and does not extend to this one.
- Measured at `96c00c10cc`.

## 1. Problem

A worker session in a fleet never has a person typing into it. A conductor or patrol session creates it, and every turn it runs arrives by `session_send`. Each turn re-sends the whole conversation, so a worker that has carried one PR through many review rounds pays for all of them on every wake.

The worker cannot drop that context today. `apply_session_directive_outcome` in `src/kiro_crew/dashboard/session_directive_apply.py` puts `reset_conversation` in `_USER_SURFACE_DIRECTIVES`, which admit only a turn a person started. A `session_send` turn is not one, so the call is refused. #17471 counts 36 refused resets a day from one fleet.

The conductor exception in `rfc-conductor-round-reset.md` does not reach workers. It admits a wake only for a `kirocrew-conductor` slot, from that slot's own self-armed loop.

## 2. Goals and non-goals

Goals:

- A session can reset its own conversation on a turn another session delivered with `session_send`.
- A person's session stays protected from that request.

Non-goals:

- A session resetting another session directly. The sender can only ask; the target's own turn makes the call.
- Admitting a cron delivery, a sub-agent, a task runner or a plain loop wake.
- Letting a peer-sent turn run `set_project` or `chat_tag`. They stay human-only.
- Changing what a reset drops. It still drops only the model's memory; the slot, its loop and the transcript stay.

## 3. Design

A `reset_conversation` directive from a turn a person started keeps today's rule. A directive from a turn delivered by `session_send` is admitted only when all of these hold:

| Check | Why |
|---|---|
| the turn was delivered live by `session_send`, to an idle target or from its queue | session control authenticates the sender; the mark is set on the delivery path and nowhere else |
| the turn is not a queue entry restored after a gateway restart | the restored sender record comes from a file on disk, so it proves nothing |
| the target slot is not pinned | pinned sessions are a person's own |
| the target slot is not a crew member's DM thread (`members.DM_SLOT_KEY_PREFIX`) | a DM slot carries `pinned` only when its saved metadata says so, so the pin flag alone does not cover it |

A failed check refuses with a reason and nothing is queued. The refusal text names `session_send`, so the transcript shows why.

The sender is already named in the target's transcript by the `[sent by session X via session_send]` line, so no new audit field is added.

## 4. Risks

- **A reset that loses state.** A worker that resets must have written its state somewhere durable first. For a fleet worker that is its fleet row; the worker prompt owns that rule.
- **A prompt injected into a peer's message.** It can make the target forget its chat. It cannot reach another slot, change the project or touch the transcript on disk, and it cannot reach a pinned session or a member DM.
- **The model does not see the refusal.** A refused directive returns the tool's own success text to the model, and only the transcript shows the refusal. A pinned target may therefore report a reset that did not happen. This is true of every refused directive today and is out of scope here.

## 5. Security

This narrows a human-only gate in one more place. `set_project` and `chat_tag` remain human-only.

The harm is bounded the same way as the conductor case:

1. A reset drops only the model's memory of the target slot. The transcript, the slot and its loop stay.
2. Only the target's own turn can call the tool, and a pinned session or member DM refuses.
3. Any session that can `session_send` to a target can already steer that target's next turn, which is a larger power than asking it to forget its chat.

## 6. Alternatives considered

- **Admit a turn delivered by the session's own cron.** Rejected. `api_send_message` takes `caller_session` from the request body, and `session="origin"` with `cron:<job-id>` produces a turn marked `_turn_actor="cron"`. An app holding the `/api/send-message` grant could forge that, so the actor label does not prove the cron produced the turn. It needs an authenticated cron producer first, in its own change.
- **A `session_reset` verb that resets another session.** Rejected. It adds a new cross-session command to the gateway for no benefit over asking the target, and the target would lose its own say.
- **Extend the conductor exception to every agent.** Rejected. A worker's turns are not self-wakes, so the conductor's self-arm check never admits them.
