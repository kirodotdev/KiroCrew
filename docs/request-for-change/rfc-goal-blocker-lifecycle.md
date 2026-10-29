---
title: Goal blocker lifecycle — remediate without retiring autonomous work
status: draft
author: rubencu
created: 2026-09-23
last-audited: 2026-10-20
audited-at: ea45e802e9
doc-pr: 13013
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Goal blocker lifecycle — remediate without retiring autonomous work

- Status: `draft`. Acceptance is requested from a maintainer, in one record or
  two (Phase 0); the status flips to `accepted` once both are recorded here.
  Nothing of this design is on main.
  Implementation follows in its own PRs: Phase 1 as two (1a, then 1b) and
  Phase 2 as one.
- Builds on [rfc-goal-loop-approval-hold.md](rfc-goal-loop-approval-hold.md),
  implemented by [#16993](https://github.com/kirodotdev/KiroCrew/pull/16993).
  The next section says what that document decided and what this one adds.
- Related: [rfc-goal-popover-pause-play-controls.md](rfc-goal-popover-pause-play-controls.md)
  (the popover's Pause, Play and Clear controls and the `fresh_run` reset),
  [rfc-work-ledger-person-wait-hold.md](rfc-work-ledger-person-wait-hold.md),
  [rfc-perpetual-agent.md](rfc-perpetual-agent.md) (the owner's switch to an
  uncapped crewmate loop) and the goal-conductor skill's
  `scripts/patrol_budget.py` (agent-side renewal of a patrol's bounds).
- Measured at `8ce11f0924`.

## Relationship to rfc-goal-loop-approval-hold

[rfc-goal-loop-approval-hold.md](rfc-goal-loop-approval-hold.md) decided what
the runtime does when a tool approval goes unanswered in a legacy prompt loop's
session: the loop holds. It stays active, fires no cycle, spends neither its
cycle cap nor its runtime budget, and resumes when a person answers an
approval, types into the dashboard session, or presses Play. Held time is
credited back to the runtime budget, a `work-ledger` watch with open items is
extended past a spent bound up to a runaway backstop, and structured monitors
keep their own `approval_stall` disposition. On main the hold is
`notify_approval_stalled`, `_record_approval_hold` and `release_approval_hold`
in `src/kiro_crew/autonudge_service/timers.py`, read by `_timer` in
`src/kiro_crew/autonudge_service/firing.py`.

This document takes that design as given and proposes nothing about the
response to an unanswered approval. It proposes five things that design does
not decide:

1. agent-facing contracts that treat a remediable blocker as work, not as a
   reason to stop the loop (§1);
2. an authority ceiling on remediation, which covers the loop's own budget
   (§2);
3. a per-slot commitment record, sealed against the agent, that the ceiling
   reads (§3);
4. a commitment generation and turn provenance that make an owner's ending of
   a loop final against automation the loop set in motion (§4); and
5. finite default caps for the dashboard goal surfaces, and the refusal of a
   negative cap everywhere (§5).

## Summary

An autonomous goal does not end because one step meets a missing permission,
credential, configuration or dependency, or a failing tool, build or test.
Those conditions are intermediate work: inspect the owning configuration, make
a least-privilege repair the agent is already authorized to make, verify it,
and continue. If only a person can grant the next approval, the agent reports
that once, continues other safe work, and rechecks later. An unanswered prompt
holds the loop, as the approval-hold document defines, and the agent does not
stop the loop over it.

Remediation never widens the agent's own authority. It never grants the agent
access, weakens approval policy or governance controls, or enlarges the loop
the agent runs in. The last point needs mechanism, because on main the loop's
own session can renew the budget its owner set: it can raise its bounds with
`monitor_update`, stop and re-arm itself for a fresh budget, replace its own
active row through a workflow's `ctx.nudge`, and re-arm its slot from a
workflow run, cron or subagent completion after the owner cleared it. Phase 1
records the bounds the owner committed, per slot, in a leaf the agent cannot
write, caps every self-session bound write at that pair, and makes a stop the
agent can trigger retain the commitment instead of ending it. Phase 2 adds a
per-slot commitment generation and sealed provenance for scheduled automation,
so that a run, cron or completion launched under a commitment the owner has
since ended arms nothing.

The dashboard goal surfaces stop arming unbounded loops by default: a fresh
popover goal and a `POST /api/autonudge` body that omits `max_cycles` commit
50 cycles, the budget `/goal` already ships. A negative cap is refused at every
boundary instead of being stored as the `0` that means unlimited.

## Motivation

### Agents retire goals on remediable blockers

A goal-running agent identified a missing permission and the least-privilege
configuration repair that owned it, then called the loop's stop tool with a
blocker reason. When a person told the same agent to fix the permission and
continue, it did so. The dependency was remediable, and treating it as a
terminal outcome created the intervention.

The agent-facing contracts on main still teach that ending. The generated
`/goal` instruction (`_handle_goal_command` in
`src/kiro_crew/dashboard/chat_turn/slash_commands.py`) says "Hard blocker -> state it once
and autonudge_stop(reason="blocked")". The base prompt's monitoring recipe
(`src/kiro_crew/config/prompt.md`) lists "blocker" among the reasons to call
`autonudge_stop`, and the tool's own description in
`src/kiro_crew/mcp_tools/control_parts/schemas.py` gives "blocked on user input" as a reason
to halt. The bundled babysit skill's example nudge and its execution step 7,
and the kirocrew-prepare-pr skill's example nudge, name a blocker as a stop
condition. The repo-checkout self-nudge recipe and its scaffolded template
stop on "all blocked". The goal-loop skill's persistence rule says the service
may deactivate the loop on an approval stall, which no longer describes the
runtime: the loop holds.

### The loop can renew its owner's budget

With an unanswered approval holding the loop rather than ending it, the cycle
cap and the runtime budget are the service-enforced endings left to a loop that
keeps working. On main the loop's own session can move both:

- `MONITOR_UPDATE_SCHEMA` in `src/kiro_crew/validation_parts/core_agent_schemas.py`
  (re-exported by `src/kiro_crew/validation.py`) admits a
  `max_cycles` up to 1,000 and a `max_runtime_secs` up to the runtime ceiling,
  and the applier `_monitor_update` in
  `src/kiro_crew/dashboard/session_directive_apply_monitor.py` refuses only a cap at or
  below the delivered cycle count or a budget at or below the loop's age. A
  loop armed at 24 cycles can raise itself to 1,000.
- `autonudge_stop` and `monitor_stop` reach `_stop_resolved_loop` in the same
  module, which removes an ordinary legacy loop's row, so a following
  `monitor_start` commits `monitor_start`'s defaults afresh: 24 cycles and
  14,400 seconds (`src/kiro_crew/mcp_tools/_limits.py`).
- A workflow's `ctx.nudge` reaches `authorize_and_add_nudge` in
  `src/kiro_crew/autonudge_authz.py` through `_wf_nudge_authorizer` in
  `src/kiro_crew/dashboard/server_runtime/workflow_startup.py`, which passes no
  `replace_existing`, so the chokepoint's default `True` removes the slot's
  active row and arms a fresh one with a fresh count.
- A workflow run outlives the turn that launched it (`RunHandle.session_key` in
  `src/kiro_crew/workflows/registry.py` carries its originating session), and a
  cron's origin injection or a subagent completion starts a turn on the slot.
  After the owner's clear removes the row, such a run or turn finds an empty
  slot and arms a fresh loop.
- The loop store, `~/.kiro/crew/autonudge.json`, is agent-writable by
  `src/kiro_crew/autonudge_service/store_loader.py`'s own account, so no bound kept only on the row
  binds the agent.

### Dashboard goals arm unbounded by default

The Set-a-goal popover (`website/src/components/AutoNudgePopover.tsx`) seeds a
fresh goal's cycles field with `0`, turns an empty field into `0` through
`parseCycles` (`parseInt(s, 10) || 0`), and labels `0` as infinite.
`api_autonudge_start` in `src/kiro_crew/dashboard/handlers/autonudge.py` stores
an omitted `max_cycles` as `0`. A goal nobody gave a budget therefore runs
until the person stops it, the STOP sentinel fires or a failure stand-down
ends it. A negative cap is worse: `_add_unserialized` and
`_update_unserialized` in `src/kiro_crew/autonudge_service/mutations.py` store
`max(0, int(max_cycles))`, so a negative that reaches the service becomes the
`0` that means unlimited.

## Goals

1. Keep autonomous goals active through remediable blockers.
2. Make least privilege an authority ceiling, not permission to self-grant,
   and include the loop's own budget in that ceiling.
3. Make an owner's ending of a loop final against automation the loop set in
   motion that would arm, replace or resume a loop on the slot.
4. Give every dashboard goal a finite budget unless the person types `0`.
5. Keep hand-written and generated self-nudge instructions consistent.

## Non-goals

- The response to an unanswered approval, the work-ledger bound extension and
  the person-wait hold. Their documents decide them.
- Automatically approving a tool request or creating an authorization grant.
- Weakening, bypassing or editing security policy, governance controls, trust
  roots or denied-command configuration.
- Removing cycle caps, runtime budgets, the owner's stop, STOP sentinels or
  terminal goal completion.
- Changing typed structured-monitor outcomes.
- A finite default for `ctx.nudge` or the Issue Radar crew runtime.
- A new UI or notification surface.

## Design

### 1. Remediable blockers in the agent-facing contracts

A blocker is terminal only when it matches a terminal condition:

- the objective or Definition of Done is complete, with concrete evidence;
- the owner ends the loop through a control the agent cannot invoke (§2);
- a configured STOP sentinel fires;
- a host or tooling failure stays unrecoverable after bounded retries and no
  safe work remains; or
- a service-enforced cycle cap or runtime budget is spent.

The last is a backstop, not success. Any other missing permission, credential,
configuration or dependency, or failing command, is remediation work: the
agent inspects the owning code or configuration, makes the least-privilege
repair it is already authorized to make, verifies it, and continues. When only
a person can act, the agent reports the condition once, continues any other
safe work, and rechecks on later cycles. If a prompt goes unanswered meanwhile,
the service holds the loop
([rfc-goal-loop-approval-hold.md](rfc-goal-loop-approval-hold.md) §3.1) and
the agent leaves it held. A hold is not a stop condition.

The rule appears at every agent-facing producer. Packaged producers ship in the
wheel: the base prompt (`src/kiro_crew/config/prompt.md`), the generated
`/goal` instruction (`src/kiro_crew/dashboard/chat_turn/slash_commands.py`), the
`autonudge_stop` tool description (`src/kiro_crew/mcp_tools/control_parts/schemas.py`), and
the bundled `kirocrew-dev/babysit/SKILL.md` (its example message and execution
step 7) and `kirocrew-dev/kirocrew-prepare-pr/SKILL.md` (its example message)
under `src/kiro_crew/builtin_skills/`. `_ensure_builtin_skills` in
`src/kiro_crew/skills.py` copies the bundled tree into every install's skills
directory, so installed copies follow the packaged text. Repo-checkout
producers sync only when the gateway runs from this checkout: the self-nudge
recipe (`skills/self-nudge-loop/SKILL.md`), its scaffolded template
(`skills/self-nudge-loop/scaffold.sh`), and the goal-loop skill
(`skills/goal-loop/SKILL.md`). The goal-loop skill's persistence rule states
the hold instead of a deactivation on an approval stall. Contract tests pin
the remediation wording and the absence of a generic blocker stop across the
whole set.

The `autonudge_stop` description and the base prompt also say what the stop
does under §2: it stops the loop and keeps the owner's commitment, whichever
turn calls it. A person who asks the agent to stop and wants the goal ended is
pointed at the popover's Clear control for a stopped goal, which sends
`DELETE /api/autonudge/{id}?intent=clear`, or at `/goal clear`.

### 2. Authority ceiling

"Fix the owning permission or configuration" means changing an
application-owned policy or configuration only when the current authorization
already allows the change and the result is least privilege. It never means
granting the agent access, changing the approval mechanism that refused it,
weakening a governance rule, bypassing a safety control, or widening the loop
the agent runs in. When that ceiling leaves only a human action, the loop
records the condition, reports it once and rechecks later.

The loop's budget is the owner's, and the loop's own session may spend it but
not enlarge it. The **committed pair** is the `max_cycles` and
`max_runtime_secs` an arming surface (§5) or the owner committed for the slot,
as the service stores them, kept in the record of §3. The rules:

- **Bound writes.** A `max_cycles` or `max_runtime_secs` write from the loop's
  own session (`monitor_update`, or any bound write a session directs at its
  own loop) is capped at the committed pair, per field. It may tighten a live
  bound, or restore one up to its committed value, never raise it above. A
  write that asks for more is refused with the live bounds unchanged, and the
  refusal names the committed ceiling and the owner routes that can recommit
  it. A field committed at `0` has no ceiling of its own.
- **Stops the agent can trigger.** `autonudge_stop` and `monitor_stop`, from
  any turn on the session whoever started it, and a fired STOP sentinel, a file
  the loop's instruction names and the agent can write, deactivate the loop
  and retain its record: the remaining cycles and seconds and the committed
  pair. They do not end the commitment. The stop wrappers read no turn
  provenance for this, because provenance says who started the turn, not who
  asked for the stop, and in a turn a person typed the agent holds
  `autonudge_stop` and `monitor_start` together.
- **Re-arms and proxies.** A `monitor_start`, a workflow's `ctx.nudge` on its
  originating session, or any other arm or replacement the loop's session
  directs at a slot that holds a committed loop (an active row or a retained
  record) commits no fresh bounds. The loop it produces inherits the committed
  pair and carries the spend forward: the row it writes keeps the cycles
  already delivered under the commitment and the runtime anchor (its
  `cycle_count` and `created_ts`), so what is left is the cycles not yet
  delivered and the seconds not yet spent, and a self-session bound write
  capped at the committed pair restores nothing a stop already spent. The seconds are read the way the
  service reads the runtime budget (`runtime_budget_exceeded` in
  `src/kiro_crew/autonudge_service/model.py`): elapsed from the persisted
  `created_ts` once per process, then advanced on the monotonic clock, so a
  wall-clock step neither refunds nor charges them. A replacement of the active row
  carries the spend forward the same way instead of starting a fresh count. An arm
  at an exhausted remainder is refused. An arm at a slot whose loop is held
  ([rfc-goal-loop-approval-hold.md](rfc-goal-loop-approval-hold.md) §3.1) is
  refused, so a replacement cannot release a hold that only a person's return
  may release.
  The gateway's dispatch patrol is such an arm although it names no initiator.
  `conductor_patrol.ensure_patrol` (`src/kiro_crew/conductor_patrol.py`) arms a
  `work-ledger` watch at 300 cycles and 86,400 seconds, as an EXTERNAL arm with
  no `initiator_slot_key`, after the session's own work-ledger `bind` or
  `session_create` commits, and on main it replaces a loop the system stopped,
  a fired STOP sentinel included (`_stopped_row_is_replaceable`). At a slot
  that holds a committed loop it inherits like any other re-arm: a dispatch
  is new work for the session's ledger, not a recommit of the owner's budget,
  so it buys no fresh pair after the session's own sentinel stop.
- **Owner acts.** Only the owner commits a fresh pair on a slot that holds
  one, recommits it or ends it, through the routes `_require_monitor_owner`
  gates in `src/kiro_crew/dashboard/handlers/autonudge.py`: `POST
  /api/autonudge`, `PATCH /api/autonudge/{id}`, `DELETE /api/autonudge/{id}`
  and `POST /api/monitors/{id}/restart`. `/goal` and `/goal clear`, typed by
  the person, are owner acts too (§4 gates them on provenance). A spent cap or
  budget and the timer's terminal stand-downs are service endings and end the
  commitment as well.
- **Fresh slots.** A slot that holds no committed loop takes its first pair
  from whichever surface arms it, as today. A slot whose row is gone while its
  record is open is not fresh: at load that record becomes a retained stop
  (§3), and an arm there inherits.

The slot-close pair in `src/kiro_crew/dashboard/chat_api/slot_lifecycle.py`
already computes the remainder this section names: `_retire_slot_nudge_loop`
retires a dismissed tab's loop, and `_restore_slot_nudge_loop` re-arms it with
its remaining cycle and runtime budget when the close fails to persist. Under
this design that restore is an inheriting re-arm at the retired record, and it
carries the spend forward the same way.

The cost of the retained stop is stated plainly: when a person asks the agent
in chat to stop, the stop is retained, and a workflow, subagent or cron the
loop set in motion may still spend the remaining budget until the person uses
an owner control. The contracts of §1 point the person at that control.

**Agent-side renewal.** The bound-write cap collides with two designs on main.
[rfc-goal-loop-approval-hold.md](rfc-goal-loop-approval-hold.md) keeps the
goal-conductor's agent-side renewal beside its server-side extension:
`patrol_budget.py renew` prints raised bounds that the conductor passes to
`monitor_update` from its own session, one more base budget per renewal, at
most three renewals, 1,000 cycles or seven days. And
[rfc-goal-popover-pause-play-controls.md](rfc-goal-popover-pause-play-controls.md)
describes a `monitor_update` that raises a spent loop's stopping bound as a
revival in which "a raise buys its increment". The cap refuses both. Which way
that resolves is open question 1, and the Phase 1 cap is blocked on it. The
other rules of this section do not depend on the answer.

### 3. The commitment record

The rules of §2 read a record the agent cannot forge. It holds, per slot, the
committed pair as the service stores it and the commitment's state: open, or
ended with the source of the ending. From Phase 2 it also holds the slot's
generation, the spend charged against the pair and the provenance of scheduled
automation (§4).

**Not on the row, not under `trust/`.** The loop store,
`~/.kiro/crew/autonudge.json`, is agent-writable by
`src/kiro_crew/autonudge_service/store_loader.py`'s own account, and a pair kept there would be a
hint a sandboxed shell or a cron could rewrite before the next load trusted it.
`trust/` is no better: `_CREW_SANDBOX_VISIBLE_LEAVES` in
`src/kiro_crew/sandbox.py` keeps `trust` read-write inside the sandbox on
purpose, and the file-tool gate that lists it does not reach a shell's
runtime-composed path.

**The precedent on main.** The same pattern already guards one bit of the
loop row: `NudgeLoop.self_armed` is a hint that must agree with a record the
agent cannot forge, `autonudge-self-armed.json`, written by
`src/kiro_crew/autonudge_selfarm.py` under `trust/`. That record has the
file-tool gate but not the OS seal, for the reason just given. The two stay
separate in this design: the commitment record needs the OS seal, and moving
the self-arm entries into the sealed leaf would give them a seal they lack
today, a change to an existing record that belongs to its own PR.

**A dedicated leaf under three layers.** Phase 1 keeps the record in
`autonudge-commitments.json`, a leaf of the crew home:

1. *OS seal.* The leaf is listed in `_CREW_READONLY_LEAVES` in
   `src/kiro_crew/sandbox.py`, which the sandbox seals read-only for every
   sandboxed process, and in `_CREW_PRECREATE_READONLY_FILE_LEAVES`, so the
   Linux seal has a file to bind. An empty pre-created `{}` means what an
   absent record means, so a stale sealed read fails toward fewer commitments,
   never more. It also carries no `cutover` marker, so the gateway's first
   load after the upgrade runs the cutover described below.
2. *File-edit gate.* The leaf is listed in `_WRITE_PROTECTED_HOME_PATHS` in
   `src/kiro_crew/security/paths.py`, so `is_sensitive_write_path`, which the
   tool hook in `src/kiro_crew/hooks.py` and `_edit_target_denial` in
   `src/kiro_crew/llm_helpers.py` apply to an edit call's target, refuses the
   edit whatever the sandbox setting. It is the write-only tier, not the
   read-and-write floor, because the record holds no secret.
3. *Writer set.* Only the unsandboxed gateway writes the leaf, through one
   transition helper the autonudge service calls under its `_lock` at every
   persisted row transition: a fresh pair when an arm commits one, a recommit
   on the owner's signal (below), an ending, a retained stop and, from Phase 2,
   the delivered-cycle charge. No code in the agent's process writes it.

The leaf is classified in `_CREW_CHILD_READABLE_LEAVES`, which
`test_every_non_hidden_leaf_is_classified_for_a_foreign_child` in
`test/test_sandbox_governance_mask.py` requires of every read-only leaf. A
foreign harness's child may read committed bounds, and reading them gives it
nothing to act on.

**One reader that trusts no name.** The seal binds a path, not the inode
behind it: `_warn_if_alias_backed` in `src/kiro_crew/sandbox.py` reports a
symlinked or multiply-linked leaf before a spawn and continues. Every read of
the record therefore opens the leaf with `O_NOFOLLOW` and checks, on the open
descriptor, that it is a regular file with one link, as
`require_unaliased_launch_state` in the same module does for the launch-state
leaves. A read that fails the check is decided as a read that finds no record,
and writes an SEL audit line naming the leaf and the shape it found. Under the
lock, the writer replaces an aliased leaf with a fresh empty record before it
applies its transition, and the owners recommit. The fresh record carries the
`cutover` marker: a writer runs only after a load that has completed the
cutover, and a marker-less replacement would make the next load run the
cutover again and seal whatever bounds the rows hold then. An alias created,
written through and removed before a read leaves a lone file no check can tell
from a genuine one. The seal and the gate are what keep the agent from creating
the alias.

**The record decides, the row mirrors.** Where the row and the record disagree
in a cap field, the record wins. A live bound above a positive committed field
disagrees, and so does a live `0` beside a positive committed value, because
to `_timer` a `0` is no bound: its cycle check reads `if loop.max_cycles and
loop.cycle_count >= loop.max_cycles`, and `runtime_budget_exceeded` is false
for a `0` budget. Such a row is audited and brought back to the record at load
and at every tick.

**Order of the two writes.** The record is written before the row on every
transition, and the record itself says when a row write is in flight: the
writer sets a pending-transition mark on the record that names the transition
(an arm, a recommit, a stop, an ending) and the remainder it knew, writes the
row, then clears the mark. A crash between the two leaves a record whose mark
is still set and whose row never landed. At load, a record with the mark set
and no row is resolved by what the mark names: a pending arm whose row never
landed is closed, because that commitment never produced a loop, and any other
pending transition resolves as a retained stop holding the remainder the
record knew. An open record with the mark clear and no row is not a crash:
nothing the gateway writes removes a row without a transition, so the row was
removed by another hand. Such a record becomes a retained stop (§2) at load,
holding the remainder it last knew, and the next arm on the slot inherits it.
The deletion buys the deleter nothing: the slot still holds a committed loop,
the Fresh slots rule does not apply, and only an owner act commits a fresh
pair. Until Phase 2 charges the spend into the record, the remainder it last
knew is the one from the last persisted transition, so a deleted row can at
most recover the cycles delivered since that transition, never a fresh or a
wider pair. A row whose slot holds no record is read as uncommitted, except
at the cutover: the first load after the upgrade seals a record for each row
written before Phase 1 from its stored bounds (Backward compatibility). The
cutover runs only while the leaf lacks its `cutover` marker. The gateway writes
that marker in the same atomic leaf write as the records it seals, also when
there are none, so a later load never runs the cutover again and a row that
appears in the store afterwards stays uncommitted. A row
write that fails after the record write never reopens an ended commitment,
and a record with the mark set beside a present row has its mark cleared at
load.

**The owner's signal.** The owner's recommit and the loop's own
`monitor_update` reach one service transition: `authorize_and_update_nudge` in
`src/kiro_crew/autonudge_authz.py` checks no ownership, and both callers pass
through it to `_update_unserialized`. The route already tells them apart in
one case: `api_autonudge_update` passes `fresh_run=True` after
`_require_monitor_owner` admits the request, and the applier passes nothing.
Phase 1 generalizes that bit into an owner-recommit signal that
`api_autonudge_update` sets only after the owner gate succeeds and threads
through `authorize_and_update_nudge` into the locked transition. No request
body or directive field can set it, so no turn the agent runs carries it.
Every other caller is read as non-owner.

**One rule for what the seal covers.** No field `_timer`, an automation's fire
path or a gate trusts as deciding whether or what to run, and no field the fire
path hands the run as its input, may live outside the sealed record. A row
field the seal does not cover may mirror the record for display, and the record
decides. The committed pair, the commitment state, and from Phase 2 the
generation, the spend, the provenance triple, the definition digest and the
sealed run-state component are the instances this document names. A later reading that finds the timer or a
fire path deciding by a writable field seals it or re-derives it from what is
sealed, under this same rule. So does a field a later change adds for the tick
to decide by, such as the `subject_outcome` row field the open
[#21377](https://github.com/kirodotdev/KiroCrew/pull/21377) proposes. The hold's own fields, `approval_stalled` and
`approval_stalled_at`, belong to
[rfc-goal-loop-approval-hold.md](rfc-goal-loop-approval-hold.md) and stay on
the row here. Whether they move into the record is open question 2.

### 4. Commitment generation and turn provenance

What a slot holds when an arm arrives cannot tell a person's new request from a
proxy launched under a commitment that has since ended. A workflow run keeps
executing after `workflow_run` returns its id, and a cron's origin injection
or a subagent completion starts a turn on the slot later. After the owner's
clear removes the row, or a spent cap leaves a replaceable stopped row, such an
arm meets a slot the fresh-slot rule of §2 would let it fill. Phase 2 closes
that with a per-slot **commitment generation**: a counter in the sealed record
of §3, keyed by slot because a removed row takes `NudgeLoop.config_generation`
with it.

**What moves the generation.** Every owner act advances it (`POST`, a `PATCH`
recommit and `DELETE` on `/api/autonudge`, the monitor restart route, `/goal`
and `/goal clear`), and so does every fresh commitment on a slot holding none
and every ending: a spent cap or budget and the timer's terminal stand-downs.
A stop the agent can trigger advances nothing, because the commitment
continues (§2), and an inheriting re-arm or replacement advances nothing for
the same reason.

**Proxies carry the generation.** A workflow run carries the generation its
launching turn hands it. `_nudge_port` in `src/kiro_crew/workflows/service.py`
passes it with each `ctx.nudge`, `_wf_nudge_authorizer` hands it to
`authorize_and_add_nudge`, and `AutoNudgeService.add` compares it under the
service `_lock`, in the critical section where `_add_unserialized` resolves the
slot. A mismatch is refused whatever the slot now holds, and the refusal is
recorded in the run's stream as `ctx.nudge`'s other refusals are. A matching
arm is decided by the rules of §2.

**Automation-originated turns carry it too.** `_run_chat` in
`src/kiro_crew/dashboard/chat_runner.py` takes `_directive_user_origin`,
default `False`: the runner's authenticated-human provenance bit, meaning the
author typed into the session's own surface. Only the paths that carry a
person's own message set it, such as the Slack inbound handler
(`src/kiro_crew/slack/handler_runtime/inbound.py`), the channel hand-off
(`src/kiro_crew/dashboard/channel_handoff.py`) and Spec Builder's delivery of
a person's decision. A queue drain grants it only when every consumed entry
carries it. A turn with the bit set arms generation-free. Every other turn
carries the generation its scheduling turn handed it, under one inheritance
rule:

- a turn or run that carries a generation hands that value to anything it
  schedules;
- a turn carrying none schedules automation carrying none; and
- only a turn with authenticated-human provenance captures the slot's current
  generation at a scheduling call (`spawn_run`, `workflow_run`, `cron_add`).

The nudge fire captures the slot's generation at dispatch, because the wake is
the commitment's own turn. Capturing the current generation at every
scheduling call, whoever makes it, would let a turn whose arm was just refused
as stale schedule one more hop that reads the advanced value and re-arms the
cleared slot. Under inheritance the stale value travels the whole chain, and
only a person's turn can put a fresh one into it.

**Sealed provenance.** The carried generation cannot live on the automation's
own record. `crons.json` is read-write inside the sandbox
(`_CREW_SANDBOX_VISIBLE_LEAVES` in `src/kiro_crew/sandbox.py`), and so is a
workflow run's file under `workflows.dir` (`src/kiro_crew/workflows/store.py`),
while the commitment leaf is readable, so a shell could copy the current
generation onto a stale job. The gateway therefore writes a triple at the
scheduling call: the automation's id, the origin slot it was scheduled from
and the generation it carries, into the sealed record or a sibling leaf under
the same three layers. An injection or completion turn is authorized only
against that triple, looked up by the automation's id. A record whose captured
generation or origin disagrees with its triple, or that has no triple, yields
a turn whose arms are refused.

**A digest of the definition.** The triple binds which automation fires from
which generation, not what it says, and on main what it says can be rewritten
in place. `cron_update` in `src/kiro_crew/mcp_cron.py` is gated only by
`_check_cron_job_ownership` on the job's session, and `spawn_steer` reaches
`steer_run` (`api_spawn_steer` in
`src/kiro_crew/dashboard/messaging_api/run_control.py`) on a running run.
Phase 2 seals a digest beside the triple over the automation's definition:
every field an owner sets and the fire path only reads, named by that class
rather than by a list. For a cron job (`CronJob` in
`src/kiro_crew/cron_service/model.py`) that covers the message and schedule,
and also the `approval_mode`, `command` and `script` fields that decide
whether the fire runs a model turn at all and under what approval, and the
owner's `user_paused`. Run state the fire path hands the next run as input
is sealed as its own component: a persistent job's `last_result`, which
`build_cron_session_context` in `src/kiro_crew/cron_service/identity.py`
prepends to the prompt, and the `acked_items` the Slack cron turn in
`src/kiro_crew/slack/gateway_runtime/cron_turn.py` appends to it, which
`ack_job` in `src/kiro_crew/cron_service/service/job_writes.py` records. Only the gateway's own result
and acknowledgement writers reseal that component. Display and timing state
stays outside the seal. An admitted update reseals the digest, and an update
the inheritance rule refuses leaves it stale, so the next fire's arms are
refused. The loop's own instruction and
slot are sealed the same way, and so is a structured monitor's definition:
the `kind`, `target`, `objective`, `budgets`, `cadence_secs` and
`wake_instructions` of `MonitorState` in `src/kiro_crew/monitoring/models.py`,
which `MonitorController.tick` and `_dispatch_claimed` in
`src/kiro_crew/monitoring/controller.py` read, are verified before every tick
and wake. The monitor's typed outcomes do not change.

**What the digest costs the other subsystems.** The digest is defined by class
(a field that decides whether or what runs, or that the fire path hands the run
as input), not by a list, so this document needs no edit when a definition
grows. The cost falls on the six definitions it reaches: `CronJob`,
`MonitorState`, a workflow run, the Slack cron turn's `acked_items`, and the
Issue Radar and auto-research watchdog records. A change that adds a field to
one of them has to say which class the field is in, in that change, and add it
to the digest or the run-state component when it is in one; display and timing
state stays out. Phase 2 pins that with one test per definition that fails when
a field is added without a class, so the classification is a review question
asked once per new field rather than a sweep this document repeats.

**`/goal` from a person only.** The slash dispatch in `_run_chat` hands a
`/goal` message to `_handle_goal_command` before anything reads the turn's
provenance, and the handler arms or removes the loop directly. An app holding
the `sessionApproval` grant can send `/goal` or `/goal clear` into a person's
slot through `POST /api/chat`, at once or queued and drained, with the bit
`False`. Phase 2 refuses `/goal` and `/goal clear` from a turn or drained
entry without authenticated-human provenance and reports the refusal into the
turn.

**Channel provenance.** `build_directive_consumer` in
`src/kiro_crew/messaging/dispatch.py` calls `apply_session_directive` with
`producer_is_channel=True` and never passes `producer_is_user_facing`, whose
default is `False`, so every channel turn reads as automation. Phase 2 has the
channel consumer pass `producer_is_user_facing` for a turn a person's inbound
message started, and withhold it from a bot- or automation-authored message
and from a channel loop's own wake, before the bit becomes the arming gate.
Without that, a person's Slack, Discord or Webex watch request would be
refused on an empty slot.

**App runtimes honor an ending.** Two app watchdogs arm or revive loops from
the row alone. `watchdog_cycle` in
`src/kiro_crew/apps/builtins/issue_radar/backend/crew_runtime.py` re-activates
every inactive loop of a live crew and calls `launch_crew` when a live crew's
slot holds no loop, and `_watchdog_loop` in
`src/kiro_crew/apps/builtins/auto_research/campaign/watchdog.py` re-activates
every inactive loop of a RUNNING campaign. An owner's `DELETE` on a crew loop
is therefore re-armed on the next pass. Phase 2 has both read the sealed
record first: a watchdog revives a row only while its slot's commitment is
open (a manual pause, an app-disable suspension), and arms nothing on a slot
whose commitment is ended. The app's own resume act reopens an ended slot with
a fresh commitment: `_handle_crew_pause` in
`src/kiro_crew/apps/builtins/issue_radar/backend/crew_routes.py` on `paused`
false, and the `resume` action of `_handle_action` in
`src/kiro_crew/apps/builtins/auto_research/handlers.py`, which commits before
the campaign is published RUNNING so a refused commitment leaves the campaign
in the status it held.

### 5. Arming surfaces

Every legacy prompt loop is created through `AutoNudgeService.add`, whose own
defaults are `max_cycles=0` and `max_runtime_secs=0`. The first pair a surface
commits on a fresh slot is the pair §2 reads.

| Surface | Default cap on main | Runtime budget | Change (phase) |
|---|---|---|---|
| `/goal` (`_handle_goal_command`, `src/kiro_crew/dashboard/chat_turn/slash_commands.py`) | 50 cycles; `--max N` matches digits only and clamps to 1–50 | none | cap unchanged; Phase 2 refuses `/goal` and `/goal clear` without authenticated-human provenance (§4) |
| Set-a-goal popover (`website/src/components/AutoNudgePopover.tsx`) | fresh goal seeded `0`; an empty field parses to `0`; always sent; `0` labelled infinite | not exposed | Phase 1: fresh goal seeds 50; an empty or unparseable field commits 50; a typed `0` commits `0`; a negative is refused in the field; a live loop is shown as stored; drafts as below |
| Popover Pause, Play and Clear | `pause()` sends `PATCH {active: false}`; Play sends the edited fields with `active: true`; Clear sends `DELETE /api/autonudge/{id}?intent=clear` | none | Phase 1: Play on a stopped loop is an owner recommit, still under the goal save's stale-baseline check (`expect_fingerprint`, refused as `goal_stale_baseline`); Clear ends the commitment (§2) |
| `POST /api/autonudge` (`api_autonudge_start`) | omitted `max_cycles` stored `0`; a negative is coerced with `int()` and forwarded | omitted stays `0`; explicit value up to the ceiling | Phase 1: omitted `max_cycles` commits 50; explicit `0` stays unlimited; a negative in either field is refused |
| `monitor_start` (`src/kiro_crew/mcp_tools/control.py`) | `_MONITOR_DEFAULT_MAX_CYCLES` = 24; schema refuses below `1` | `_MONITOR_DEFAULT_MAX_RUNTIME_SECS` = 14,400 s | unchanged |
| Dispatch patrol (`ensure_patrol` in `src/kiro_crew/conductor_patrol.py`, after a work-ledger `bind` or a `session_create` commits) | `PATROL_MAX_CYCLES` = 300; an EXTERNAL arm (no `initiator_slot_key`) that replaces a system-stopped loop, a fired STOP sentinel included | `PATROL_MAX_RUNTIME_SECS` = 86,400 s | fresh slot unchanged; Phase 1 inherits at a committed slot, a retained sentinel stop included, and refuses an exhausted remainder (§2) |
| Spec Builder handoff (`orchestration/execution.py` under `src/kiro_crew/apps/builtins/spec_builder/backend/`) | `_EXEC_MAX_CYCLES` = 60 (`orchestration/execution_state.py`) | none | unchanged |
| auto-research (`src/kiro_crew/apps/builtins/auto_research/`) | campaign row default 30; `validate_campaign` checks only `MAX_CYCLES_HARD_CAP` and runs for the validate and create routes; the `fork` action builds its config with `body.get("max_cycles", 30)` and calls `create_campaign` directly | none | Phase 1: a negative cap is refused at `create_campaign`, the insert both routes reach, and reported by `validate_campaign` |
| `ctx.nudge` (`src/kiro_crew/workflows/runner.py`) | `0` (signature default) | no parameter | default excluded; Phase 1 refuses a negative at the service; Phase 2 inherits at a committed slot and refuses a stale generation (§2, §4) |
| Issue Radar crew runtime (`launch_crew` in `src/kiro_crew/apps/builtins/issue_radar/backend/crew_runtime.py`) | `0` by design | none | default excluded; Phase 2 watchdog rule (§4) |
| Perpetual mode switch ([rfc-perpetual-agent.md](rfc-perpetual-agent.md)) | both caps `0`, owner-set | `0` | when implemented, an owner recommit of both caps to `0` through the commitment gate |

**The popover's field rewrites itself.** `startNow()` sends `max_cycles` on
every create through `formFields()`, so the REST omission default never fires
for it. The field's `onBlur` writes `parseCycles(maxCyclesInput)` back into
it, so an emptied or unparseable field shows a literal `0` before `startNow()`
reads it, and at that point a cleared field and a typed `0` are the same
string. Phase 1 therefore tracks provenance: whether the field was ever given
a non-empty value that parses. A `0` a person typed commits `0`. A `0` the
blur wrote into an emptied field commits 50.

**Remembered drafts.** `draftToPersist` drops a draft only when all three
fields are pristine, and `hasEdited` is set by the message field as much as by
the cycles field, so a person who edits only the message persists the untouched
seed `0` with it. Phase 1 records a per-draft cap-commitment marker only when
the cycles field was given a non-empty value that parses, restores a draft
verbatim only when the marker is present, and for a legacy draft or an
uncommitted-cap draft at `0` restores the message and idle and reseeds the
cycles field with 50. A positive legacy cap is restored as it was. Unlimited
operation stays available by typing `0`, which sets the marker.

**Negative caps are refused, never clamped.** `_add_unserialized` and
`_update_unserialized` in `src/kiro_crew/autonudge_service/mutations.py`
store `max(0, int(max_cycles))`, so a negative cap that reaches the service
becomes the `0` that means unlimited, while a negative `max_runtime_secs` is
refused first by `validate_runtime_secs`. Phase 1 refuses a negative
`max_cycles` or `max_runtime_secs` at every arming and recommit boundary with
a validation error that names the field: in the popover before it sends, at
`api_autonudge_start` and `authorize_and_update_nudge`, at `create_campaign`,
and, as the backstop no surface can bypass, at the two service transitions,
raised as `MonitorUpdateConflict`, which `authorize_and_add_nudge` already
returns as a denial. The record of §3 is written from the stored value, so a
stored `0` means unbounded and a stored positive value means bounded, in the
row and in the record alike. `_load` reads each stored bound through
`stored_bound` (`src/kiro_crew/autonudge_service/model.py`): a lossless integer
form (an int, a whole float, or a string holding one) is kept as that int, and
any other value stops the loop fail-closed under `structural_terminal` with the
value kept as stored, so the record is written from the int `_load` keeps.

**Why 50.** `/goal` ships 50 cycles, so the three dashboard goal surfaces share
one goal budget and `/goal`'s contract does not change. `monitor_start`'s 24
cycles and 14,400 seconds suit a pull-request watch at a 300-second interval
and would end a goal at a 60-second idle in minutes. The dashboard goal
surfaces get no runtime default: the cycle cap is the service-enforced ending,
as it is for `/goal`.

**Why the two programmatic callers keep their `0`.** `ctx.nudge`'s default is
a public workflow contract, and the Issue Radar crew runtime documents
`max_cycles=0` as its intended shape, with the crew record's flags, its STOP
sentinel and the app gate as its brakes. The rules of §2 and §4 reach both
without moving that default: an arm at a committed slot inherits, and an arm
whose commitment has ended is refused. A later RFC may give either a finite
default.

## Migration plan

### Phase 0 — decision record

This document lands on its own, so the implementation PRs can cite it from the
base branch.

Exit criteria:

- a maintainer records acceptance here, in one record or two: of §1, §5 and
  the negative-cap refusals, which is all Phase 1a needs, and of §2 to §4. The
  status flips to `accepted` once both are recorded; and
- open questions 1 and 3 have answers recorded here, or the Phase 1 bound-write
  cap is split out to wait for them; and
- the accepting maintainer records, in so many words, that a person asking the
  agent in chat to stop is a stop from a turn: §2 retains it, and the goal ends
  only through an owner control. Phase 1b carries that rule, so it does not
  start without this record; Phase 1a does not depend on it; and
- the accepting maintainer records a separate decision on Phase 2: whether the
  sealed definition digests it adds to the six definitions it reaches are
  worth their lasting cost while a cron the loop's turn scheduled still runs
  model turns the commitment does not bound (see "Hold Phase 2 until it also
  bounds a self-scheduled cron"). Phase 2 does not start without that record.

### Phase 1 — contracts, commitment record, self-session ceiling and defaults

Two implementation PRs, each after the Phase 0 records it needs. Together
they align every producer named in §1, add the commitment leaf of §3 under
its three layers with the one reader and the one transition helper, apply
the rules of §2 to the loop's own bound writes, stops, `monitor_start`
re-arms and the dispatch patrol's re-arm, thread the owner-recommit signal,
refuse negative caps at every boundary, and change the popover and REST
defaults of §5. The bound-write cap is blocked on open questions 1 and 3.

Phase 1 lands as two PRs, Phase 1a first and on its own, ahead of the
commitment-record work. Phase 1a can start as soon as §1, §5 and the
negative-cap refusals are accepted. **Phase 1a** carries the §1 contracts,
the §5 defaults and drafts, and the negative-cap refusals; none of them
depends on the commitment record or on open question 1, and they remove the
harm people meet most often today. **Phase 1b** carries the record of §3 and
the rules of §2. The exit criteria below split the same way: the first,
and the negative-cap and defaults items, are Phase 1a's; the rest are
Phase 1b's.

Exit criteria, each pinned by a test:

- every producer of §1 carries the remediation rule and none names a generic
  blocker as a stop condition; the goal-loop skill states the hold;
- the leaf is in `_CREW_READONLY_LEAVES`, `_CREW_PRECREATE_READONLY_FILE_LEAVES`,
  `_WRITE_PROTECTED_HOME_PATHS` and `_CREW_CHILD_READABLE_LEAVES`, and a
  file-edit call naming it is refused with the sandbox off;
- a read of an aliased leaf is decided as no record and audited, and the
  writer replaces the alias with a fresh empty record that keeps the `cutover`
  marker, so the next load does not run the cutover again;
- a self-session `monitor_update` above the committed pair is refused with the
  live bounds unchanged, and a tighten or a restore up to the pair is applied;
- `autonudge_stop`, `monitor_stop` and a fired STOP sentinel retain the
  record, and a following `monitor_start` from the session inherits the
  remaining budget and is refused at an exhausted one;
- after a fired STOP sentinel, a work-ledger `bind` or a `session_create` from
  the session leaves the retained record in force: the dispatch patrol inherits
  the remaining budget and is refused at an exhausted one;
- stops, inheriting `monitor_start` re-arms and self-session `monitor_update`
  restores up to the committed pair, in any order and repeated, deliver no more
  than the committed pair in total, because every inheriting re-arm carries the
  delivered count and the runtime anchor forward;
- `DELETE /api/autonudge/{id}` ends the commitment, and a `PATCH` recommit
  from the owner route replaces the pair while the applier's identical write
  does not;
- a negative `max_cycles` or `max_runtime_secs` is refused at the popover,
  `api_autonudge_start`, `authorize_and_update_nudge`, `create_campaign` and
  both service transitions, and nothing stores one;
- a fresh popover goal commits 50, a blur-normalized empty field commits 50, a
  typed `0` commits `0`, a legacy draft at `0` reseeds 50, and `POST
  /api/autonudge` without `max_cycles` commits 50; and
- a row that disagrees with its record in a cap field is brought back to the
  record at load and at the next tick; and
- at load, an open record with no row and no pending-transition mark becomes a
  retained stop that a following `monitor_start` from the session inherits,
  while a record whose pending arm never landed its row is closed; and
- the first load after the upgrade seals a record for each row written before
  Phase 1 from its stored bounds and writes the `cutover` marker, also on a
  host with no rows; a later out-of-band raise of a sealed row's `max_cycles`
  or `max_runtime_secs` is brought back to the sealed pair at load; and a row
  written into the store after the cutover stays uncommitted and is not
  sealed.

### Phase 2 — commitment generation and provenance

Its own PR, entered once Phase 1 is on main. It adds the generation, the
inheritance rule, the sealed triple and definition digest, the `/goal`
provenance gate, the channel provenance and the watchdog rule of §4, and moves
the cap and budget `_timer` enforces onto the record's sealed spend.

Exit criteria, each pinned by a test:

- an owner act or an ending advances the generation; a stop the agent
  triggers and an inheriting re-arm do not;
- a `ctx.nudge` at a committed slot inherits the commitment and the spent
  budget; at a held loop it is refused; from a run whose generation is stale
  it is refused and the refusal appears in the run's stream;
- a `monitor_start` from a turn a cron or a subagent completion started is
  refused when the generation it carries is stale, at every hop of the chain,
  and a person's turn on the same slot arms;
- a cron whose definition was rewritten without a reseal arms nothing on its
  next injection, a run-state value rewritten out of band never reaches the
  next prompt, and a structured monitor whose sealed definition disagrees
  with its row is not ticked from the row;
- `/goal` and `/goal clear` from an app-sent or drained entry without
  authenticated-human provenance are refused and reported;
- a person's Slack, Discord or Webex message passes `producer_is_user_facing`,
  and a bot-authored message and a channel loop's own wake do not; and
- the Issue Radar and auto-research watchdogs revive only rows whose
  commitment is open, arm nothing on an ended slot, and the app's resume act
  reopens one.

After Phase 1 alone, the proxy and automation paths of §4 stay open: a
workflow run, cron or completion launched before the owner's clear can still
arm a fresh loop on the cleared slot.

## Backward compatibility

- **Rows with no record.** A row written before Phase 1 has no commitment
  record, and the store holds no evidence of who committed its bounds. The
  first load an upgraded gateway makes seals one for it: the row's stored
  bounds become its committed pair, so every later load is decided by the
  record and not by the agent-writable row. From then on a self-session write
  may tighten them and never raise them, a re-arm or replacement inherits them
  as the remainder, and a row rewritten out of band is brought back to the
  record at load. The owner gives it a fresh pair through any owner route of
  §2. A stored `0` is never rewritten to the new default and is shown as `0`.
  The cutover runs once: the `cutover` marker it writes into the leaf, rows or
  none, keeps every later load from running it again, so a row that appears in
  the store afterwards stays uncommitted (§3). It cannot tell a row the agent
  rewrote before the upgrade from one the owner armed that way; main seals
  nothing today, so that window is the one main already has, and it closes at
  the first load.
- **The agent's stop.** On main `_stop_resolved_loop` removes an ordinary
  legacy loop's row, and `monitor_inspect` then reports nothing armed. After
  Phase 1 the row is retained as a stopped loop with its remaining budget.
- **The STOP sentinel.** On main a fired sentinel deactivates the loop and
  keeps it as a finished goal whose reason lets the next arm displace it with
  fresh bounds (`FINISHED_LOOP_REASONS` and `_REPLACEABLE_LOOP_STOP_REASONS`
  in `src/kiro_crew/autonudge_service/model.py`), and the dispatch patrol a
  later `bind` or `session_create` arms is such an arm. After Phase 1 the next
  arm from the loop's own session, the dispatch patrol included, inherits the
  remaining budget, and an owner's arm commits fresh. The popover still shows the goal as finished.
- **Remembered drafts.** A legacy popover draft at `0` reopens at 50, and a
  positive one as it was (§5).
- **Workflow scripts.** A `ctx.nudge` that today replaces an active row with a
  fresh count inherits the row's commitment from Phase 2.
- **Leaf cutover.** The first load an upgraded gateway makes treats an aliased
  leaf as no record, so a planted alias is dropped, not adopted.

## Security considerations

- **No new authority.** Every rule here removes a path by which the agent
  could widen its own loop. Nothing grants an approval, creates a permission,
  or lets the agent end or renew a commitment it did not make.
- **The record is readable and write-sealed.** It holds slot keys, committed
  bounds, commitment states and, from Phase 2, generations, spend and
  automation provenance, none of them secret. The OS seal holds every
  sandboxed process, and the file-edit gate holds the agent's edit tool
  whatever the sandbox setting. What neither reaches is a shell the agent
  spawns on a host whose sandbox is off. That is the standing residual of the
  write-only tier, shared by every ceiling on it and by the loop store beside
  the leaf. `agent.sandbox` defaults to `"auto"`
  (`src/kiro_crew/config/sections.py`), which seals the child.
- **Aliases.** The reader's descriptor check stops a record being consumed
  through a second name. It cannot see an alias that was written through and
  removed before the read, as §3 states. The seal and the gate keep the agent
  from creating one.
- **Fail closed, with one gap.** A record that disagrees with its row wins:
  the row is brought back to the record. A missing or aliased record is read
  as no commitment (§3), never as a wider one, but the row is then
  uncommitted, and the cap of §2 applies only against a committed pair, so the
  loop's own session can raise that row's bounds until an owner recommits the
  slot. Whether that gap closes is open question 3.
- **Provenance.** The generation-free arm is reserved for a turn with
  authenticated-human provenance, which only the runner sets, on the paths
  that carry a person's own message. An app's `POST /api/chat` send and every
  injected turn carry `False`, so they arm only under a generation their
  scheduling turn handed them.
- **A deleted row is not a reset.** The row lives in the agent-writable store,
  so removing it is a write the agent can make. The load rule of §3 turns a
  rowless open record into a retained stop rather than an ending, so a row
  deleted before a restart leaves the slot committed with the remainder it
  last knew, and the crash window the write order creates is told apart by a
  mark the record carries, which only the gateway writes.
- **The retained stop.** A person's chat request to stop leaves the
  commitment open until the person uses an owner control. Automation the loop
  set in motion may spend the remaining budget meanwhile, which is bounded by
  that budget.
- **A self-scheduled cron is a stated residual.** Under `capabilities.cron` a
  loop's turn can schedule a recurring cron on its origin slot (`cron_add`,
  `src/kiro_crew/mcp_cron.py`), and each firing runs a full model turn without
  arming a loop, so no committed pair, generation or digest bounds those turns.
  This document bounds loops and the automation that would arm, replace or
  resume them (Goal 3), not cron turns: the owner grants the cron capability,
  sees the job in the cron list and ends it there. Counting a self-scheduled
  cron's turns against the slot's commitment is left to a later RFC.

## Alternatives considered

### Record the committed pair on the loop's row

Rejected. `~/.kiro/crew/autonudge.json` is agent-writable by
`src/kiro_crew/autonudge_service/store_loader.py`'s own account. A pair or a generation kept there
is a hint a cron the loop created could rewrite before the next load trusts it.

### Seal `autonudge.json` itself

Rejected. Sealing the loop store would make the row the record and remove the
second leaf, but the store fails the second of the two criteria a pre-created
sealed file leaf must meet (`_CREW_PRECREATE_READONLY_FILE_LEAVES` in
`src/kiro_crew/sandbox_precreate.py`): a stale sealed read must fail toward
refusal. The gateway rewrites the store on every cycle, so a sandboxed reader
can read an older copy. For the commitment record that stale read fails toward
fewer commitments (§3). The store has an in-sandbox reader that fails the other
way: `kirocrew-prepare-pr/scripts/monitor_armed.py`,
which the prepare-pr and babysit skills run in the agent's shell to confirm a
`monitor_start` armed, would keep reporting a loop that stopped after the
sandbox started as active, so an agent would trust a watch that no longer
runs. The store also carries state the agent legitimately reads as live, such
as cycle counts and deadlines, that a frozen copy misstates. A dedicated leaf
holds only the fields a writable row must not decide, so the seal costs
nothing the store's readers need.

### Record the commitment under `trust/`

Rejected. `_CREW_SANDBOX_VISIBLE_LEAVES` keeps `trust` read-write inside the
sandbox, because in-sandbox code reads `trust/sel_hmac.key`, and the file-tool
gate that lists the directory does not reach a shell's composed path.

### Clamp a negative cap to `0`

Rejected. It is what main does, and it turns a value no surface meant as
unlimited into the `0` that is. A validation error that names the field is the
only reading that keeps the stored value and the committed one the same.

### Refuse the self-session stop

Rejected. A stop is how a loop reports that its goal is complete with
evidence. Refusing it blocks that ending. Retaining the record keeps the stop
and closes the budget reset.

### Let turn provenance end the commitment

Rejected. Provenance says the turn was the person's, not that the person asked
for a stop. In any turn a person types, the agent holds both `autonudge_stop`
and `monitor_start`, so a stop read as the owner's would let the agent end the
commitment and commit a fresh pair in the same turn.

### Let a self-session re-arm commit fresh bounds

Rejected. `autonudge_stop` then `monitor_start` would hand the loop
`monitor_start`'s 24 cycles and 14,400 seconds anew, at every turn it chooses.

### Let a self-session write raise live bounds within the schema ceilings

Rejected, subject to open question 1. The schema ceilings are the store's
limits, not the owner's budget: a loop armed at 24 cycles could raise itself
to 1,000 cycles and the runtime ceiling from its own turn.

### Exempt workflow-armed loops from the self-session rule

Rejected. `workflow_run` is a tool the loop's turn holds, and `_nudge_port`
arms on the run's originating session, so the run is the loop's own turn by
another name. Exempting it leaves the widest replacement open.

### Cancel the run when its loop stops

Rejected. A run may carry unrelated work the turn delegated, and `ctx.nudge`
is one optional call at its end. Cancelling every run a tab launched when its
loop stops would turn the owner's clear into a kill switch for work the owner
never asked to stop.

### Refuse every `ctx.nudge` on a slot that ever held a loop

Rejected. It describes history, not a live commitment, and would refuse a
person's own workflow on a tab whose loop ended long ago. The generation
refuses exactly the arm launched under a commitment that has since ended.

### Extend `_EXTERNAL_ARM_REFUSED_MODES` to every mode

Rejected. `_EXTERNAL_ARM_REFUSED_MODES` in `src/kiro_crew/autonudge_authz.py`
is `frozenset({"crew", "member"})`. Adding every mode would refuse a person's
own cron that arms a loop on its origin tab, forever.

### Refuse any arm for a while after an owner clear

Rejected as the narrower alternative to Phase 2. A cron or a workflow fires on
its own schedule, often hours after the clear (a daily job, a long run), so a
window short enough not to block the next goal a person starts on the slot
misses the stale arm, and one long enough to catch it blocks that new goal.
The generation of §4 decides by what an arm descends from, not by when it
arrives, which is the distinction a window cannot make. Phase 2 ships in its
own PR, so its cost stays reversible.

### Treat every automation-originated turn as the loop's own

Rejected. A person's cron on a tab whose slot nothing has cleared holds a
legitimate arm. Only the generation tells automation dispatched under an ended
commitment from a person's automation on a slot that never moved.

### Capture the slot's current generation at every scheduling call

Rejected. A turn whose arm was refused as stale could schedule one more hop,
which would read the advanced generation, match it and re-arm the cleared
slot. The rule would defeat itself at a chain of two.

### Advance the generation on a retained self-session stop

Rejected. The retained record is the commitment continuing. Advancing the
generation would refuse the inheriting re-arm the rule allows, and would hand
the agent a way to move a counter only the owner and the service move.

### Let any `/goal` through as today

Rejected. An app holding the `sessionApproval` grant can send `/goal` or
`/goal clear` into a person's slot, and the dispatch reads no provenance, so
that entry would renew or end the commitment with no person's act.

### Decide the watchdog revival from the row

Rejected. The row lives in the agent-writable store. A forged
`stopped_reason`, or a row deleted before a restart, would let a watchdog that
reads only the row revive or re-arm a slot whose commitment is ended.

### Close a rowless open record at load

Rejected. An earlier draft closed it so a crashed arm would arm nothing. The
row is agent-writable, so a row deleted before a restart would end the
commitment and hand the next self-session arm a fresh pair, the reset §2
removes and the forgery the alternative above names. The crash window is told
apart by the record's own pending-transition mark instead (§3), and only a
pending arm whose row never landed is closed.

### Seal the provenance triple without the definition

Rejected. The triple binds which automation fires, not what it says.
`cron_update` and `spawn_steer` rewrite what an automation says with its id
kept, so a stale chain could carry its payload into a current generation.

### Seal every field the fire path reads

Rejected. One digest over every field the fire path reads, resealed only on
the update paths, is too wide a class with too narrow a reseal list. The fire
path writes run state back after every fire, such as a persistent cron job's
`last_result` (read by `build_cron_session_context` in
`src/kiro_crew/cron_service/identity.py`), so that digest would refuse the next
valid run. The definition digest and a separate run-state component that the
gateway's own writers reseal keep both sealed (§4).

### Drop the empty-case blur normalization instead of tracking provenance

Rejected as the rule. Clearing the field fires `onChange` before any blur, so
a marker keyed to an edit would still be set by the clear alone. Only the
value's provenance tells a typed `0` from a normalized blank. An
implementation may also leave an emptied field empty on blur.

### Let the agent grant itself access

Rejected. That turns remediation into escalation and makes the model that met
a control responsible for removing it. Human-only authorization stays
human-only.

### Reuse the `monitor_start` defaults for dashboard goals

Rejected. 24 cycles and 14,400 seconds disagree with the 50 cycles `/goal`
ships and are sized for a different envelope (§5). Both are finite, which is
the property that matters.

### Give `ctx.nudge` and Issue Radar a finite default here

Deferred. Each cap belongs to its caller's contract, and the rules of §2 and §4
reach both without moving it. A later RFC may revisit either.

### Land the commitment record and the generation in one PR

Rejected. Phase 1 touches the service's row transitions, the sandbox lists and
the dashboard defaults. Phase 2 adds provenance across workflows, crons,
subagents, channels and two apps. Phases here are independently shippable and
independently abandonable, and the two have different blast radii.

### Hold Phase 2 until it also bounds a self-scheduled cron

Rejected, as this document's recommendation: the decision itself is the
accepting maintainer's, recorded under Phase 0, and Phase 2 does not start
until it is.

A cron the loop's turn scheduled on its origin slot runs model turns
without arming a loop, so Phase 2's generation and digest do not bound those
turns. Security considerations states this as a residual. What Phase 2 closes
is Goal 3: automation the loop set in motion arming, replacing or resuming a
loop after its owner ended it, through a workflow step, a subagent, a channel
or an app. That re-arm brings back a goal the person already ended, and
nothing shows it to them. A cron job is in the owner's cron list, and the owner
grants the capability it needs and can end it there. The cross-subsystem cost
buys those re-arm paths, and waiting for one design that also counts cron turns
would leave them open while the cron path already has an owner control.

## Open questions

1. **Agent-side renewal and the bound-write cap.** The cap of §2 refuses the
   goal-conductor's `patrol_budget.py renew`, which
   [rfc-goal-loop-approval-hold.md](rfc-goal-loop-approval-hold.md) keeps,
   and the `monitor_update` raise that
   [rfc-goal-popover-pause-play-controls.md](rfc-goal-popover-pause-play-controls.md)
   describes as a revival. The choices: (a) the cap stands, a conductor patrol
   relies on the server-side extension of that document's §3.2 alone, and the
   renewal mode is retired; (b) the cap admits a renewal on a `work-ledger`
   watch within the limits `patrol_budget.py` already enforces (three
   renewals, 1,000 cycles, seven days), counted in the sealed record so the
   agent cannot reset the count; or (c) the cap is dropped and the other rules
   of §2 ship alone. The Phase 1 cap is blocked on the answer.
2. **The hold's fields.** `approval_stalled` and `approval_stalled_at` live on
   the agent-writable row, so a sandboxed write to `autonudge.json` can end a
   hold that rfc-goal-loop-approval-hold says only a person's return ends.
   Moving them into the sealed record would amend that document. Neither
   phase is blocked on it.
3. **A slot with no readable record.** §3 reads a missing or aliased record
   as no commitment, which leaves the row uncommitted and outside the cap of
   §2. The choices: (a) cap a self-session bound write at the row's stored
   bounds whenever its slot holds no readable record, which closes the gap
   but refuses a raise an uncommitted row can make on main; or (b) accept the
   gap, because removing or aliasing a record takes a write to the protected
   leaf from a spawned shell outside the seal, which the default
   `agent.sandbox` of `"auto"` does not give the agent. The Phase 1 cap
   already waits on open question 1, and this choice joins it.

A later RFC may give `ctx.nudge` and the Issue Radar crew runtime a finite
default, or choose a backoff schedule for repeated human-only rechecks that
keeps the goal active and the ceiling of §2 intact.
