---
title: Peer-sent reset -- a session sent work by another session may drop its own chat context
status: in-progress
author: Pearcekieser, with kirocrew-worker
created: 2026-10-09
last-audited: 2026-10-20
audited-at: bdb1c87ff6
doc-pr: null
implementation-prs: [17493]
tracking-issues: [17471]
supersedes: []
superseded-by: []
---

# RFC: Peer-sent reset

- Status: in-progress. The implementation is [#17493](https://github.com/kirodotdev/KiroCrew/pull/17493). The operator who runs the worker fleet asked for it in [#17471](https://github.com/kirodotdev/KiroCrew/issues/17471). This document lands on its own first, as GOVERNANCE.md asks of an RFC, so the implementation's First Principles lane can read the decision off the base branch.
- Adds a second case to the human-only `reset_conversation` gate, beside the conductor round-close case in [rfc-conductor-round-reset.md](rfc-conductor-round-reset.md). That document says the gate opens in "exactly one case"; this one adds another in its own document, so the conductor decision is not edited and does not extend to this one.
- Measured at `bdb1c87ff6`.

## 1. Problem

A worker session in a fleet never has a person typing into it. A conductor or patrol session creates it, and every turn it runs arrives by `session_send`. Each turn re-sends the whole conversation, so a worker that has carried one PR through many review rounds pays for all of them on every wake.

The worker cannot drop that context today. `apply_session_directive_outcome` in `src/kiro_crew/dashboard/session_directive_apply.py` refuses every directive in `_USER_SURFACE_DIRECTIVES` (`session_directive_apply_outcome.py`, which lists `reset_conversation`) unless a person started the turn. A `session_send` turn is not one, so the call is refused. #17471 counts 36 refused resets a day from one fleet.

The conductor exception in `rfc-conductor-round-reset.md` does not reach workers. It admits a wake only for a `kirocrew-conductor` slot, from that slot's own self-armed loop.

## 2. Goals and non-goals

Goals:

- A session another session opened with `session_create`, or forked with `fork_session`, can reset its own conversation on a turn delivered with `session_send`.
- A person's session stays protected from that request. A person's session here means any slot a person opened, adopted or revived, and any created slot a person has typed into, pinned or not.

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
| the target slot was minted in this gateway process by `session_create` or by the agent fork verb `fork_session` (`_lineage_minted`, set in `session_create.py` and `session_tree.py`) | every dashboard slot starts unpinned, so the pin flag cannot tell a person's chat from a worker. `_lineage_minted` is set only at mint and never persisted; `_created_by` is restored from transcript metadata an agent's file tools can edit, so it is not used |
| no person has typed into the target from the dashboard (`_human_seen`) | a created worker a person has joined is that person's conversation too. The flag only goes from False to True, and the composer send sets it before the turn is dispatched |
| the target key is a dashboard key, not a messaging channel's | a channel conversation always has a person on the other end |
| the target slot is not pinned | a person pinned it |
| the target slot is not a crew member's DM thread (`members.DM_SLOT_KEY_PREFIX`) | a DM slot carries `pinned` only when its saved metadata says so, so the pin flag alone does not cover it |

A failed check refuses with a reason and nothing is queued. The refusal text names `session_send`, so the transcript shows why.

A person's own tab, an adopted tab and a revived slot have no `_lineage_minted`, so they refuse. After a gateway restart no slot has it, so every peer reset refuses until the conductor creates fresh workers. That limit is deliberate: the only restart-surviving record of who created a slot is metadata on disk, and this gate does not trust it. A worker that cannot reset keeps working with its full context, as it does today.

The sender is already named in the target's transcript by the `[sent by session X via session_send]` line, so no new audit field is added.

## 4. Risks

- **A reset that loses state.** A worker that resets must have written its state somewhere durable first. For a fleet worker that is its fleet row; the worker prompt owns that rule.
- **A prompt injected into a peer's message.** It can make a created worker forget its chat. It cannot reach a slot a person opened or typed into, another slot, the project or the transcript on disk.
- **A person who reads a worker without typing.** Watching a worker's tab does not set `_human_seen`, so that worker stays resettable by its peers. The person loses nothing they wrote, since they wrote nothing there, and the transcript stays.
- **The model does not see the refusal.** A refused directive returns the tool's own success text to the model, and only the transcript shows the refusal. A pinned target may therefore report a reset that did not happen. This is true of every refused directive today and is out of scope here.

## 5. Security

This narrows a human-only gate in one more place. `set_project` and `chat_tag` remain human-only.

The harm is bounded the same way as the conductor case:

1. A reset drops only the model's memory of the target slot. The transcript, the slot and its loop stay.
2. Only the target's own turn can call the tool, and only on a slot this process created for another session that no person has typed into.
3. Any session that can `session_send` to a target can already steer that target's next turn, which is a larger power than asking it to forget its chat.

## 6. Alternatives considered

- **Admit a turn delivered by the session's own cron.** Rejected. `api_send_message` takes `caller_session` from the request body, and `session="origin"` with `cron:<job-id>` produces a turn marked `_turn_actor="cron"`. An app holding the `/api/send-message` grant could forge that, so the actor label does not prove the cron produced the turn. It needs an authenticated cron producer first, in its own change.
- **A `session_reset` verb that resets another session.** Rejected. It adds a new cross-session command to the gateway for no benefit over asking the target, and the target would lose its own say.
- **Protect only pinned sessions and member DMs.** Rejected. Every dashboard slot starts unpinned, so any peer could make a person's ordinary chat drop its context in the middle of a task. That breaks the second goal.
- **Trust the restored `_created_by` after a restart.** Rejected. It is read back from transcript metadata, which an agent's file tools can edit, so a metadata edit would turn a person's tab into a resettable worker.
- **Extend the conductor exception to every agent.** Rejected. A worker's turns are not self-wakes, so the conductor's self-arm check never admits them.
