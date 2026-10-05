---
title: Conductor default patrol -- a bind arms a work-ledger watch when the conductor has no loop
status: in-progress
author: iamwhatever, with kirocrew-worker
created: 2026-10-05
last-audited: 2026-10-05
audited-at: a92dde1a6c
doc-pr: null
implementation-prs: [17069]
tracking-issues: [17051]
supersedes: []
superseded-by: []
---

# RFC: Conductor default patrol

- Status: in-progress. The decision was made by the operator who owns the conductor patrol (2026-10-05). The implementation is [#17069](https://github.com/kirodotdev/KiroCrew/pull/17069). That PR's First Principles lane reads an RFC's status off the base branch, so this document lands on its own first, as GOVERNANCE.md asks of an RFC. The implementation then rebases onto it.
- Amends [rfc-conductor-work-ledger.md](rfc-conductor-work-ledger.md) in one place, named in section 3: arming the `work-ledger` watch is no longer only the conductor's own act.
- Related: [rfc-crew-log-wake.md](rfc-crew-log-wake.md) (how a worker's report wakes the conductor) and the goal-conductor skill's `scripts/patrol_budget.py` (the bounds a patrol must pass).
- Measured at `a92dde1a6c`.

## 1. Problem

A conductor hands an item to a worker with `work_ledger_record action=bind`. Its prompt then tells it to arm a loop on its own session with `monitor_start`. Nothing checks that it did.

A conductor that skips the arm ends its turn with workers running and nobody reading their reports. `done`, `blocked` and `question` sit in the ledger until a person notices. On main, `api_work_ledger_record` in `src/kiro_crew/dashboard/handlers/work_ledger.py` writes the binding and returns; it arms nothing. `work_ledger_read` derives `orphaned` and `stale`, and neither says "nobody is patrolling this board".

## 2. Goals and non-goals

Goals:

- A conductor that binds a worker and holds no loop gets a patrol without doing anything.
- The agent's own `monitor_start` stays the primary path, and it always wins over the default.
- When no patrol runs, the conductor's next ledger read says so.

Non-goals:

- Reviving a loop a person stopped.
- Arming on a crew- or member-mode conductor. Section 5 explains why.
- A Crew page badge. The read flag comes first; a badge is a follow-up.
- Changing the goal-conductor skill or the conductor prompt's Patrol section.

## 3. Design

**Arm on bind.** After a bind commits, `conductor_patrol.ensure_patrol` looks up the conductor's slot. If it holds no loop record at all, it arms one through `autonudge_authz.authorize_and_add_nudge`, the chokepoint every arm uses:

| Field | Value |
|---|---|
| `watch` | `work-ledger`, gated |
| interval | 600 s (inside the conductor's 300..900 s band) |
| `max_cycles` | 300 |
| `max_runtime_secs` | 86400 |
| message | fixed gateway text: read the ledger compactly, verify `done` with `accept_eval.py`, answer `question` and `blocked` |

The bounds pass `patrol_budget.py check`. The arm is create-only. A stopped row is left alone, with one exception: the gateway's own default patrol that the system stopped (its budget or cap ran out, the same `_stopped_row_is_replaceable` allowlist a directive re-arm uses) is replaced by a fresh default, because a new bind means new work. A person's stop of the default stays evidence. A refusal is logged at WARNING, and the bind still succeeds. The bind reply carries `patrol: armed | existing | refused | unsupported`, plus a `patrol_note` whenever no `work-ledger` watch is active after the bind, telling the conductor to arm its own `monitor_start` in the same turn.

**The agent's arm wins.** The default loop is stored with `default_patrol: true`. That tag is the one exception to create-only. ANY create-only arm of a loop that is not itself a default patrol displaces an ACTIVE default patrol instead of answering 409, even while its wake is in flight. That covers the conductor's own `monitor_start`, and equally a person's dashboard create or a channel arm on that slot: each is an explicit choice, and the default is only a fallback. The conductor's own `monitor_start` therefore replaces the default with its own message, exit condition and bounds, and starts its own cycle count. A stopped default patrol keeps the ordinary retained-row rules, so a person's stop of it stays evidence. One default never displaces another.

**The displaced wake.** The mid-wake exception exists because the default patrol's own wake is usually the turn in which the conductor calls `monitor_start`. The guard it bypasses keeps an accepted wake's completion evidence tied to its record. On displacement that completion is **dropped**: the default's record is removed, so when the turn's completion arrives (`record_monitor_turn_completion`) it names a loop id that no longer exists, clears its accepted-turn entry and returns. Nothing is charged to the replacement, nothing re-arms the removed loop, and the replacement starts its own count. Nothing else waits on that completion: the default's only job was to wake the conductor, and the wake it is completing is the one that armed its successor.

**Backstop flag.** `work_ledger_read` marks every open item `unpatrolled: true` while the conductor holds no ACTIVE `work-ledger` watch, compact read included. The flag is keyed on the watch, not on any loop. A slot holds one loop, so a conductor whose loop watches a pull request has nobody reading its ledger, and the flag says so. The bind-time arm still keys on "no loop at all", because it cannot add a second loop beside the first; in that case the flag is the signal.

This amends [rfc-conductor-work-ledger.md](rfc-conductor-work-ledger.md), which says the ledger gate is "the one a conductor arms". The conductor still arms it; the gateway arms it too, as a fallback, when the conductor has not.

## 4. Risks

- **A default the conductor did not want.** It costs a gated loop that fires only when the ledger moves. The conductor's own `monitor_start` replaces it, and `autonudge_stop` ends it.
- **A conductor that never reads the flag.** The flag only helps on the next turn. The armed default is what covers the silent case; the flag covers the slots it cannot arm.
- **The tag in an agent-writable store.** A forged `default_patrol` only lets the same slot's own create-only arm replace that slot's loop. An agent that can write the store can already rewrite the loop.

## 5. Security

The default arm is an OUTSIDE arm. It passes no `initiator_slot_key`. The bind route knows which session called it, not which turn: a cron injection or a sub-agent sharing the slot sends the same session key as the session's own turn. Only the session-directive consumer can tell them apart. So a crew- or member-mode conductor refuses the default, exactly as it refuses any outside arm, and no self-arm trust record is written. The message is fixed gateway text, never agent input. The authorizer's SEL audit records every arm and refusal.

## 6. Alternatives considered

- **Arm at the conductor's turn end instead of at bind.** The turn's provenance is known there, so a member conductor could be self-armed. Rejected for now: it moves the arm into the chat runner's turn-end path, a much wider change, and the replacement rule above already removes the conflict with the agent's own arm.
- **Only flag, never arm.** Leaves the silent case open, which is the failure this RFC exists for.
- **Self-arm member conductors from the bind route.** Rejected (section 5): it would let a cron or sub-agent turn arm a member's thread.
- **Let `monitor_start` answer 409 and tell the conductor to use `monitor_update`.** Rejected: every conductor that follows the skill would hit the 409.

## 7. Open questions

None open.

## 8. Rollout

One implementation PR, [#17069](https://github.com/kirodotdev/KiroCrew/pull/17069), after this document lands. Exit criteria, each pinned by a test there:

- The first bind on a loop-less conductor arms exactly one `work-ledger` loop with the bounds above, tagged `default_patrol`.
- A second bind, or a stopped retained loop, arms nothing.
- A refused arm logs a WARNING and the bind still commits; a member-mode conductor is refused, gets the `patrol_note`, and no trust record is written.
- A system-stopped default patrol is re-armed on the next bind; a person-stopped one is left alone.
- The conductor's own create-only arm replaces an active default patrol, also mid-wake; it still answers 409 over an ordinary loop.
- The displaced wake's completion is dropped: it charges nothing to the replacement and leaves it active.
- `unpatrolled` is true for an open item whose conductor holds no active `work-ledger` watch, including one whose loop watches something else, and false otherwise.
