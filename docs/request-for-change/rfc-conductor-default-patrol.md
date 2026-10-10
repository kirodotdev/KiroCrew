---
title: Conductor default patrol -- a bind arms a work-ledger watch when the conductor has no loop
status: in-progress
author: iamwhatever, with kirocrew-worker
created: 2026-10-05
last-audited: 2026-10-10
audited-at: 950be41684
doc-pr: null
implementation-prs: [17069, 18592]
tracking-issues: [17051, 17603]
supersedes: []
superseded-by: []
---

# RFC: Conductor default patrol

- Status: in-progress. The decision was made by the operator who owns the conductor patrol (2026-10-05). The implementation is [#17069](https://github.com/kirodotdev/KiroCrew/pull/17069). That PR's First Principles lane reads an RFC's status off the base branch, so this document lands on its own first, as GOVERNANCE.md asks of an RFC. The implementation then rebases onto it.
- Revision of 2026-10-10: a crew- or member-mode conductor gets the patrol too. The patrol's owner asked for it in [#17603](https://github.com/kirodotdev/KiroCrew/issues/17603) two days after the first decision; sections 2, 3, 4, 5, 6 and 8 record how, and why not as a self-arm. The implementation is [#18592](https://github.com/kirodotdev/KiroCrew/pull/18592), which rebases onto this revision for the same reason as above.
- Amends [rfc-conductor-work-ledger.md](rfc-conductor-work-ledger.md) in one place, named in section 3: arming the `work-ledger` watch is no longer only the conductor's own act.
- Related: [rfc-crew-log-wake.md](rfc-crew-log-wake.md) (how a worker's report wakes the conductor) and the goal-conductor skill's `scripts/patrol_budget.py` (the bounds a patrol must pass).
- Measured at `a92dde1a6c`.

## 1. Problem

A conductor hands an item to a worker with `work_ledger_record action=bind`. Its prompt then tells it to arm a loop on its own session with `monitor_start`. Nothing checks that it did.

A conductor that skips the arm ends its turn with workers running and nobody reading their reports. `done`, `blocked` and `question` sit in the ledger until a person notices. On main, `api_work_ledger_record` in `src/kiro_crew/dashboard/handlers/work_ledger.py` writes the binding and returns; it arms nothing. `work_ledger_read` derives `orphaned` and `stale`, and neither says "nobody is patrolling this board".

## 2. Goals and non-goals

Goals:

- A conductor that binds a worker and holds no loop gets a patrol without doing anything.
- A crew- or member-mode conductor gets the same patrol, as the gateway's own arm: admitted only with the fixed patrol text and the slot's own ledger watch, never claimed as the session's own arm. Section 5 says what that admits and what it does not.
- The agent's own `monitor_start` stays the primary path, and it always wins over the default.
- When no patrol runs, the conductor's next ledger read says so.

Non-goals:

- Reviving a loop a person stopped.
- Giving the bind route turn provenance, or self-arming a crew/member conductor from it. Section 5 explains why.
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

**On a crew- or member-mode conductor.** Such a slot refuses every arm from outside the session and admits one exception for caller-authored work, the self-arm (the session's own turn, proven by the session-directive consumer). The default patrol is a second exception of a different kind: the gateway's own arm, carrying nothing of a caller's. The authorizer admits `default_patrol` on such a slot only together with the fixed patrol text, byte for byte, and the `work-ledger` watch (`autonudge_authz.is_gateway_patrol`); the flag with any other text, or with a structured monitor, is refused as the outside arm it is. The admission is audited under its own `gateway_patrol` outcome, and a gateway-patrol trust entry is written before the add in the same keystone-gated record the self-arm uses (`autonudge_selfarm`, entry `kind: gateway_patrol`), failing closed when it cannot be written. At fire time the slot admits the wake only when three things agree: the stored row's `default_patrol` bit, the row's CONTENT (`conductor_patrol.is_patrol_loop`: the fixed text, this slot's own `work-ledger` watch, and the shape the patrol is armed in: gated, no banner, no `wake_instructions` on the monitor, since each of those is a field that puts text in front of the model), and the trust entry naming that loop on that slot. The content check is what the entry cannot do, because the loop store is agent-writable and the entry names only an id and a slot: a message, a banner or a structured action line rewritten under the patrol's own id is refused like any other outside text. Because the content is pinned, `monitor_update` on such a slot may tune the patrol's bounds but not its message, watch or banner; the authorizer refuses those and names the conductor's own `monitor_start` as the arm that replaces the patrol. The two entry kinds never vouch for each other, so the patrol's wake is not a self-arm and cannot `reset_conversation`. `default_patrol=` is passed by `conductor_patrol` alone, pinned by a tree-scanning test, as `initiator_slot_key=` is.

This amends [rfc-conductor-work-ledger.md](rfc-conductor-work-ledger.md), which says the ledger gate is "the one a conductor arms". The conductor still arms it; the gateway arms it too, as a fallback, when the conductor has not.

## 4. Risks

- **A default the conductor did not want.** It costs a gated loop that fires only when the ledger moves. The conductor's own `monitor_start` replaces it, and `autonudge_stop` ends it.
- **A conductor that never reads the flag.** The flag only helps on the next turn. The armed default is what covers the silent case; the flag covers the slots it cannot arm.
- **The tag in an agent-writable store.** On an ordinary slot a forged `default_patrol` only lets the same slot's own create-only arm replace that slot's loop; an agent that can write the store can already rewrite the loop. On a crew/member slot the tag also names the one non-self loop the slot fires, so there it is a hint and not authorization: the fire-time guard requires the gateway-patrol trust entry, which agent file tools cannot write, and the patrol's content on the row. A forged tag has no entry; a rewritten message fails the content check; neither fires.
- **A patrol the conductor cannot reword.** On a crew/member slot the text, the watch and the banner are fixed. The conductor that wants its own wording arms its own loop, which replaces the default; the refusal says so.
- **An edit to the patrol text strands the crew/member patrols already stored.** The fire-time pin reads the current text, so a row armed under the old text is refused at every fire, and its budget does not end it while the ledger holds open items. Accepted: the text is pinned by a digest test so the edit is deliberate, the change that makes it says what happens to stored patrols, and the recovery is the conductor's own arm or a stop followed by the next bind. A re-arm of a content-stale default on the next bind is a follow-up if the text ever changes.

## 5. Security

The default arm is the GATEWAY'S arm. It passes no `initiator_slot_key`. The bind route knows which session called it, not which turn: a cron injection or a sub-agent sharing the slot sends the same session key as the session's own turn. Only the session-directive consumer can tell them apart, so the arm never claims to be the session's own and no self-arm trust record is written.

The crew/member refusal exists so no outsider's instruction reaches a member's thread through a loop. The patrol carries none: its message is fixed gateway text, never agent input, and it watches the slot's own ledger. So a crew- or member-mode conductor admits it, pinned on that content at arm time and again on the stored row at fire time, with a gateway-patrol trust entry beside the row's tag (section 3). What a cron-injected or sub-agent-shared bind can start on such a slot is therefore the fixed patrol on the slot's own ledger and nothing else; what the patrol's wake can do is what the conductor's prompt already does on a ledger wake, minus `reset_conversation`, which stays closed to it because the entry never vouches a self-arm. The authorizer's SEL audit records every arm, under `gateway_patrol` for this one, and every refusal, including a fire refused because the row's content or entry did not agree with its tag.

## 6. Alternatives considered

- **Arm at the conductor's turn end instead of at bind.** The turn's provenance is known there, so a member conductor could be self-armed. Rejected for now: it moves the arm into the chat runner's turn-end path, a much wider change, and the replacement rule above already removes the conflict with the agent's own arm.
- **Only flag, never arm.** Leaves the silent case open, which is the failure this RFC exists for.
- **Self-arm member conductors from the bind route.** Rejected (section 5): the route cannot prove the turn, so the claim would be false, and a cron or sub-agent turn would arm a member's thread under the session's own name.
- **Arm a crew/member conductor as a self-arm through a path that knows the turn, the session-directive consumer or the runner's turn end ([#17603](https://github.com/kirodotdev/KiroCrew/issues/17603)'s suggestion).** Rejected in favour of the gateway's own arm: it needs a new directive from the bind tool through the runner to the applier, it still leaves a conductor dispatching from a cron-injected or sub-agent-shared turn without a patrol, and a self-arm record opens the wake-reset gate to a loop the conductor never wrote. The content-pinned admission gives every crew/member conductor the patrol and keeps that gate closed.
- **Refuse crew/member conductors, as the first decision did.** Rejected: member DM threads are the normal conductor shape, so the safety net was inert where conductors run; on a live gateway every refusal since 2026-09-24 was a member-mode conductor's bind.
- **Let `monitor_start` answer 409 and tell the conductor to use `monitor_update`.** Rejected: every conductor that follows the skill would hit the 409.

## 7. Open questions

None open.

## 8. Rollout

Two implementation PRs. [#17069](https://github.com/kirodotdev/KiroCrew/pull/17069) landed the bind-time arm after the first version of this document. Exit criteria, each pinned by a test there:

- The first bind on a loop-less conductor arms exactly one `work-ledger` loop with the bounds above, tagged `default_patrol`.
- A second bind, or a stopped retained loop, arms nothing.
- A refused arm logs a WARNING and the bind still commits.
- A system-stopped default patrol is re-armed on the next bind; a person-stopped one is left alone.
- The conductor's own create-only arm replaces an active default patrol, also mid-wake; it still answers 409 over an ordinary loop.
- The displaced wake's completion is dropped: it charges nothing to the replacement and leaves it active.
- `unpatrolled` is true for an open item whose conductor holds no active `work-ledger` watch, including one whose loop watches something else, and false otherwise.

[#18592](https://github.com/kirodotdev/KiroCrew/pull/18592) lands the crew/member admission after this revision. Exit criteria, each pinned by a test there:

- A crew- or member-mode conductor's first bind arms the patrol: the add carries `default_patrol`, the fixed text and the `work-ledger` watch, no `self_armed` bit and no self-arm entry, and a gateway-patrol entry names the loop; the audit says `gateway_patrol`.
- The flag with any other text, or with a structured monitor, is refused 409 as an outside arm and writes nothing; an unwritable trust record denies before the add.
- At fire time such a slot admits the patrol only when the tag, the row's content and the entry agree; a message, banner, gate or `wake_instructions` rewritten in the store under the patrol's own id, a forged tag without an entry, and a tag beside a self-arm entry are each refused and audited.
- `monitor_update` on such a slot tunes the patrol's bounds; a message, watch or banner edit is refused with the arm that replaces the patrol named.
- The patrol's wake cannot `reset_conversation`.
- An ordinary conductor's patrol arms and fires as before, with no trust entry.
- `default_patrol=` is passed by `conductor_patrol` alone.
