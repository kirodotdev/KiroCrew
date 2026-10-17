---
title: Conductor round reset -- a conductor drops its chat context at a round close
status: implemented
author: iamwhatever, with kirocrew-worker
created: 2026-10-08
last-audited: 2026-10-11
audited-at: a84f616557
doc-pr: null
implementation-prs: [18113]
tracking-issues: [18093]
supersedes: []
superseded-by: []
---

# RFC: Conductor round reset

- Status: implemented in [#18113](https://github.com/kirodotdev/KiroCrew/pull/18113). The operator who owns the conductor patrol decided it on 2026-10-08. This document lands on its own first, as GOVERNANCE.md asks of an RFC, so the implementation's First Principles lane can read the decision off the base branch.
- Amends one rule on main, named in section 5: `reset_conversation` is no longer human-only in exactly one case.
- Related: [rfc-conductor-work-ledger.md](rfc-conductor-work-ledger.md) (the item store a conductor resumes from) and [rfc-conductor-default-patrol.md](rfc-conductor-default-patrol.md) (the patrol loop whose wakes this applies to).
- Measured at `8c7417ea82`.

## 1. Problem

A goal conductor runs for days. Each patrol wake is a model turn, and each turn re-sends the conductor's whole conversation. Most wakes find nothing new, so the conductor pays for a context that only grows to learn that nothing changed.

The conversation is not where the conductor's state lives. The work ledger holds every item, its bound worker and its acceptance (including its PR). The conductor's session ledger holds `goal`, `phase`, `next` and `patrol_base`. `compose_nudge_body` in `src/kiro_crew/dashboard/handlers/autonudge.py` already prefixes every wake with a snapshot of that ledger.

The conductor cannot drop its context today, for two reasons:

1. `_CONDUCTOR_CORE_GRANTS` in `src/kiro_crew/agent.py` does not grant `reset_conversation`, and `test/test_conductor_agent.py` pins it as denied.
2. `apply_session_directive` in `src/kiro_crew/dashboard/session_directive_apply.py` puts `reset_conversation` in `_USER_SURFACE_DIRECTIVES`, which admit only a turn a person started. A patrol wake is refused, and `test/test_autonudge_member_self_arm.py` pins that.

## 2. Goals and non-goals

Goals:

- A conductor can drop its chat context at a round close, from the patrol wake in which the round closed.
- The next wake resumes from the ledgers alone.

Non-goals:

- Any agent other than `kirocrew-conductor` gaining the tool.
- Letting a wake run `set_project` or `chat_tag`. They stay human-only.
- Resetting mid-round, or on a timer.
- Changing what a reset drops. It still drops only the model's memory; the slot, its loop and the transcript stay.

## 3. Design

**Grant.** `reset_conversation` joins `_CONDUCTOR_CORE_GRANTS`, so the conductor's call is auto-approved. No other agent spec changes. The grant decides approval, not availability: other specs mount the whole `kirocrew-core` server, so the gate below checks the slot's agent itself.

**Admission from a wake.** A `reset_conversation` directive from a turn a person started keeps today's rule. A directive from a monitor wake (`producer_is_self_wake`) is admitted only when all of these hold:

| Check | Why |
|---|---|
| the slot runs `kirocrew-conductor` (or its deprecated alias) | other agents can call the tool too; the exception is the conductor's alone |
| the wake names its loop id | a wake from outside the fire path has no loop to vouch for it |
| that loop is the slot's current loop and is active | a wake that outlived a Stop or a replacement may not act |
| the self-arm record (`autonudge_selfarm.is_recorded_self_arm`) says this loop was armed by this slot's own turn | the gateway never writes it for a cron, a sub-agent, a person's dashboard arm or the bind-time default patrol |
| no question card and no tool approval is pending in the slot | a reset would drop the context the answer is meant for |

Any failed check refuses with a reason, audited `denied`, and nothing is queued. The loop is read again after the off-loop trust read, so a Pause or Stop landing inside it still wins. A reset a wake queued also waits, at the turn boundary where it lands, while a question card or tool approval is pending; a person's own reset does not.

**The trust record.** Today the authorizer writes the self-arm record only for a crew or member slot, because only those modes refuse an outside arm. The implementation also writes it when an ordinary dashboard slot arms itself through the session-directive consumer. The record's meaning does not change: "this loop was armed by this slot's own turn". Its revocation already keys on the loop leaving the store, not on the mode, so the new entries are revoked the same way. The persisted `self_armed` bit and its crew/member fire-time rule are unchanged.

**Log.** Each admitted wake reset logs one WARNING naming the session and the loop id.

**Skill and prompt.** The goal-conductor skill's "Close the round" and the `kirocrew-conductor` prompt say: after the next round is dispatched, write the session ledger (`goal`, `next`, `patrol_base`, and in `next` the open item ids, their worker keys and their PRs), then call `reset_conversation` as the last act of that turn. Never with an unanswered question pending, never mid-round.

## 4. Risks

- **A reset that loses state.** The ledger write comes first, and the work ledger already holds every item. The worst case is one wake that re-reads the work ledger to rebuild `next`.
- **A prompt injected into a wake.** It can make the conductor forget its chat. It cannot reach another slot, cannot change the project, and cannot touch the transcript on disk. The checks above keep it to the slot's own self-armed loop.
- **A reset that never lands.** It is queued for a turn boundary and waits while sub-agents run. A wake that sees its context intact simply carries on.

## 5. Security

This narrows a human-only gate in one place. `set_project` and `chat_tag` remain human-only.

The checks keep honest callers out; they are not proof against a hostile conductor. The self-arm record lives under `trust/`, which agent file tools cannot reach, but `trust` is a VISIBLE leaf of the crew sandbox (`_CREW_SANDBOX_VISIBLE_LEAVES` in `src/kiro_crew/sandbox_leaves.py`) so in-sandbox code can append to the audit log. A shell the conductor runs could write the record, and the loop store is agent-writable too. That residual is pre-existing and is the same one the crew/member self-arm rule carries.

The case therefore rests on the harm, which is bounded:

1. The agent check limits the exception to the conductor's own slot.
2. A reset drops only the model's memory of that slot. The transcript, the slot, its loop, the work ledger and the session ledger stay.
3. It cannot change the project, reach another slot, or touch another session.

So the worst a forged record buys is a conductor forgetting its own chat, which it could already cause by asking a person to press the reset button. The authorizer's SEL audit records every arm, and each admitted wake reset logs a WARNING with its loop id.

## 6. Alternatives considered

- **Compact instead of reset.** Compaction keeps a summary that still grows across rounds, and it is not under the conductor's control.
- **Reset from the gateway on every wake.** Removes the conductor's choice of when state is safely in the ledger. Rejected: only the conductor knows a round has closed.
- **Admit any self-wake.** A default patrol or a person's dashboard arm also produces wakes. Rejected: the reset should follow only from the conductor's own decision to patrol.
