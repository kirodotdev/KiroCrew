---
title: Goal blocker lifecycle — remediate without retiring autonomous work
status: accepted
author: rubencu
created: 2026-09-23
last-audited: 2026-10-02
audited-at: 6916ba17f0
doc-pr:
implementation-prs: [13000]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Goal blocker lifecycle — remediate without retiring autonomous work

- Status: accepted when this document lands. Maintainer review and merge of this
  RFC is the decision; implementation PR #13000 remains blocked until then.
- Measured against `6916ba17f0`: legacy prompt loops use `AutoNudgeService`
  in `src/kiro_crew/autonudge.py`, composed from the owner modules of
  `src/kiro_crew/autonudge_service/`, whose `add` in
  `src/kiro_crew/autonudge_service/mutations.py` defaults both `max_cycles`
  and `max_runtime_secs` to `0`, and a `NudgeLoop` carrying `0` for both is
  unlimited. The service also stores each cap as `max(0, int(...))` — at
  `_add_unserialized` on every arm and at `_update_unserialized` under
  `AutoNudgeService.update`, all three in that module, on every revision — so
  a negative `max_cycles`
  that reaches it is not refused but stored as `0`, and is unlimited by the
  same reading; a negative `max_runtime_secs` is refused first, by the
  `validate_runtime_secs` call that opens each of the two transitions. Eight
  surfaces arm such loops. `/goal` composes its recurring
  instruction in `src/kiro_crew/dashboard/chat_runner.py` and caps its loop at
  50 cycles with no wall-clock budget; its `--max N` matches digits alone, so
  a negative is not expressible there. The dashboard Set-a-goal popover
  (`website/src/components/AutoNudgePopover.tsx`) always serializes
  `max_cycles`: its field is seeded from the live loop, from a remembered draft,
  or with `0`, and `parseCycles` (`parseInt(s, 10) || 0`) turns an empty field
  into `0` and passes a typed negative through unchanged — the field's
  `min={0}` is an input attribute, not a check `startNow()` makes — so a fresh
  goal
  whose creator never touched the field is armed at `0` and the popover labels
  that `0` as infinite. `POST /api/autonudge` (`api_autonudge_start` in
  `src/kiro_crew/dashboard/handlers/autonudge.py`) stores an omitted
  `max_cycles` as `0`, coerces it with `int()` and bounds it not below, while
  its `max_runtime_secs`, `0` when omitted, passes `validate_runtime_secs`
  (`0` to the configured ceiling); its only shipped caller is that popover,
  which never omits the field. Behind it, `authorize_and_add_nudge` in
  `src/kiro_crew/autonudge_authz.py` refuses a `max_runtime_secs` outside
  `0` to the configured runtime ceiling (`runtime_ceiling_secs` in
  `src/kiro_crew/monitoring/limits.py`: `monitoring.max_runtime_secs`, else
  `604800`) and bounds `max_cycles` not at all, and
  `authorize_and_update_nudge` in the same module, behind `PATCH
  /api/autonudge/{id}` and `monitor_update`, does the same. `monitor_start` in
  `src/kiro_crew/mcp_tools/control.py` is the one surface that bounds both
  cycles and runtime by default and refuses an unbounded request at the tool
  boundary: an omitted `max_cycles` becomes `_MONITOR_DEFAULT_MAX_CYCLES` (24)
  and an omitted `max_runtime_secs` becomes `_MONITOR_DEFAULT_MAX_RUNTIME_SECS`
  (14,400 seconds, or the configured ceiling if lower), both from
  `src/kiro_crew/mcp_tools/_limits.py`, and its
  schema refuses either field below `1`, so at the tool boundary it cannot
  request an unbounded loop. The tool only encodes a directive; the loop is
  created by the session-directive applier `_monitor_start` in
  `src/kiro_crew/dashboard/session_directive_apply.py`, which reads
  `int(args.get("max_cycles") or 0)` and `int(args.get("max_runtime_secs") or
  0)` and so would arm `0` for a field a directive lacks. The tool writes both
  fields into every payload it emits, so that fallback has no shipped producer,
  but the bound lives at the tool, not at the applier. The Spec Builder handoff
  (`_handle_handoff` in `orchestration/execution.py` under
  `src/kiro_crew/apps/builtins/spec_builder/backend/`, re-exported by that
  backend's `handlers.py`) arms its own finite `_EXEC_MAX_CYCLES` (60, defined
  in `orchestration/execution_state.py`) with no wall-clock budget.
  Auto-research (`src/kiro_crew/apps/builtins/auto_research/handlers.py`, an
  import facade over `campaign/lifecycle.py`, `campaign/agent_mode.py` and
  `campaign/watchdog.py`, which own the behaviour) arms the campaign
  row's `max_cycles`, whose schema column is `NOT NULL DEFAULT 30` and whose
  insert path writes `config.get("max_cycles", 30)`, so a default campaign is
  bounded at 30 cycles; a creator who explicitly submits `0`, or any negative
  value, arms an unbounded worker loop: `validate_campaign` checks the cap
  against `MAX_CYCLES_HARD_CAP` alone, with no lower bound, and runs only
  for the validate and create routes (`_handle_validate`, `_handle_create`)
  — the `fork` action in `_handle_action` builds its `fork_config` with
  `body.get("max_cycles", 30)` and calls `create_campaign` directly —
  `_launch_loop` hands `int(row["max_cycles"] or 0)` to
  `AutoNudgeService.add`, which stores a negative as `0`, and
  `_reserve_cycles` treats any cap at or below `0` as unbounded. Such a cap
  unbounds the loop record, not the campaign: the same
  module's watchdog completes a RUNNING campaign when `count >=
  row["max_cycles"]`, which a `0` or negative cap satisfies on the first
  recorded cycle result, so the campaign lifecycle ends there while the
  AutoNudge record it armed stays classified unbounded under §5. The worker
  slot's tools are auto-approved under a 24-hour trust grant
  (`_TRUST_TTL_SECS`). That watchdog, `_watchdog_loop`, also re-activates
  every inactive loop of a RUNNING campaign on every pass — `if svc is not
  None and loop is not None and not loop.active: await svc.update(loop.id,
  active=True)` — reading `stopped_reason` only to recognise its own research
  tombstone, `AUTONUDGE_STOP_REASON`, so a loop the timer stopped with
  `approval_stalled` is revived, its marker cleared by
  `AutoNudgeService.update`, on the first pass that finds its campaign
  RUNNING again.
  Two programmatic callers reach `AutoNudgeService.add` with no finite bound by
  default: `ctx.nudge` in `src/kiro_crew/workflows/runner.py` (through
  `_nudge_port` in `src/kiro_crew/workflows/service.py` and the
  gateway-injected `_wf_nudge_authorizer` closure in
  `src/kiro_crew/dashboard/server.py`, which is the caller that reaches the
  shared `authorize_and_add_nudge` chokepoint and passes it no
  `max_runtime_secs`, no `initiator_slot_key` and no `replace_existing`, so
  the chokepoint's `replace_existing=True` default reaches
  `AutoNudgeService.add` and a `ctx.nudge` aimed at a slot whose loop is
  active removes that row and arms a fresh one in its place) defaults
  `max_cycles` to `0`, bounds the cap it is passed at no link of that chain,
  and has no runtime parameter at any of them,
  and the Issue Radar crew runtime
  (`src/kiro_crew/apps/builtins/issue_radar/backend/crew_runtime.py`) arms
  `max_cycles=0` by design, braking on its record flags, STOP sentinel and app
  gate; its `watchdog_cycle` re-activates every inactive loop of a live crew
  on every pass (`if not loop.active: await svc.update(loop.id,
  active=True)`), and `AutoNudgeService.update` clears `approval_stalled` on
  that revival, so for a live crew the timer's stall stop lasts at most one
  watchdog pass. The base agent contract is `src/kiro_crew/config/prompt.md`,
  and structured monitors own separate typed completion state under
  `src/kiro_crew/monitoring/`.

## Summary

An autonomous goal does not end merely because one step encounters a missing
permission, credential, configuration, dependency, or failing tool, build, or
test. Those conditions are intermediate work: inspect the owning configuration,
make an already-authorized least-privilege repair, verify it, and continue.

This does not let an agent widen its own authority. Remediation never means
granting itself approvals or permissions, weakening or bypassing approval
policy, or editing governance controls. If only a person can grant the next
approval, the agent reports that once, continues any other safe work, and
rechecks later while the goal remains active.

Explicit user stops, configured STOP sentinels, completed goals, unrecoverable
host/tooling failures after bounded retries with no safe work remaining, and
service-enforced finite budgets remain valid endings. The lifecycle change
applies to a loop whose owner committed such a budget; a loop with no committed
finite cycle or runtime bound keeps today's terminal approval-stall stop, and
neither a bound the loop writes for itself, nor a stop and re-arm from its own
session, nor an arm that session directs at its own slot through any other
proxy — a workflow's `ctx.nudge` on its originating session included —
changes which it is or renews the budget its owner committed: a self-written
bound may tighten the live bounds or restore them up to the committed pair,
never raise them above it, a replacement of the active row carries the
commitment and the budget already spent forward, and a proxy launched under a
commitment that has since ended or been replaced — a workflow run whose
`ctx.nudge` arrives after the owner's clear removed the loop, or a subagent
completion or cron injection that starts a turn on the slot after that
ending — arms nothing (§5). The committed pair the rule reads lives in
a dedicated leaf the OS sandbox seals read-only for every sandboxed process,
the file-edit tool's write gate refuses whatever the sandbox setting, and
only the unsandboxed gateway writes, not in the agent-writable loop store, so
the loop cannot forge it either (§5); it is written before the loop's row on
every transition and is authoritative over the row wherever the two disagree
at load, so a crash between the two writes leaves nothing that arms, and a
row write that fails after it never restores an ended commitment (§5). A
cap is committed from the value the service stores, and a negative cap is
refused at every arming and recommit boundary rather than clamped to the
`0` that means unlimited (§5). The owner's recommit and the loop's own
`monitor_update` reach one service transition, which Phase 1 tells apart by
an owner signal only the owner-gated route sets (§5). The stop this rule
retains is every stop the agent can trigger: `autonudge_stop` or
`monitor_stop` from any turn, whoever started it, and the STOP sentinel, a
file the loop's instruction names and the agent can write. A commitment ends
only through an owner route the agent cannot invoke — `DELETE
/api/autonudge/{id}` behind the popover's Clear stopped goal control,
reached after Pause, `/goal clear` typed by the person — or through a service
ending, a spent budget or the stall stop; a person who asks the agent in chat
to stop gets a retained stop, and is pointed at that ending control (§5). An
app runtime that
armed an unbounded loop does not undo its stall stop: the Issue Radar and
auto-research watchdogs, which revive it today, leave it stopped from Phase
2 (§5). Structured monitors retain their typed terminal outcome when an
accepted action cannot be delivered.

The change lands in two independently shippable phases (Migration plan).
Phase 1 — implementation PR #13000 — ships the contracts, the timer rule on
committed bounds, the per-slot commitment record in its sealed leaf, the
self-session write caps, the retained stop records and the dashboard defaults;
under it a pre-upgrade loop, which carries no evidence of who committed its
stored bounds, is read unbounded whatever they are and regains remediation
continuity only when its owner recommits it. Phase 2 ships the per-slot
commitment generation and the turn provenance that make an owner's ending
final against a proxy or an automation chain the loop set in motion — the
generation an automation carries, and a digest of the whole definition it
will run and of the prior output its run is handed — the loop's own
instruction and slot, and a structured monitor's probe, among the
definitions — are recorded in the
same sealed leaf, never on the automation's
own record in a store the agent can rewrite; after
Phase 1 alone that self-widening is not yet closed.

## Motivation

### Observed defect

A goal-running agent identified a missing permission, identified the owning
least-privilege configuration repair, and then called the generic loop stop tool
with a blocker reason. When a person told the same agent to fix the permission
and continue, it did so successfully. The dependency was remediable; classifying
it as a terminal goal outcome created the intervention.

The agent-facing contracts disagreed with the intended lifecycle. The base
prompt, `/goal` recurring instruction, stop-tool description, and self-nudge
recipe each allowed generic “blocked” state to retire all work, and the recipe's
scaffolded template carries that step verbatim. The two files have drifted
elsewhere — the recipe's notification step forbids a routine per-cycle tick
while the scaffold's sends one per productive cycle — which is why keeping the
hand-written and generated instructions consistent is a goal of this change.
Two bundled skills carry the same instruction to bounded loops they arm through
`monitor_start`: the babysit skill's example nudge and its execution step 7 name
“blocked” and “external blocker” as `autonudge_stop` conditions, and the
prepare-pr skill's example nudge names “blocker” the same way. The
repo-checkout goal-loop skill tells its agent that the service deactivates the
loop on an approval stall.

### Runtime contradiction

Legacy prompt loops also recorded an unanswered tool approval as
`approval_stalled` and deactivated on their next timer wake. That is appropriate
for a structured monitor whose single accepted action could not be delivered,
but it contradicts a goal loop that may still repair configuration, run tests,
or perform other safe work. Prompt text alone cannot promise a later recheck if
the timer has already made the loop inactive.

### Why finite budgets are the bound

Prompt loops carry cycle and wall-clock budget fields. On the measured base
several surfaces filled them by default: `/goal` armed 50 cycles,
`monitor_start` armed `_MONITOR_DEFAULT_MAX_CYCLES` (24) and
`_MONITOR_DEFAULT_MAX_RUNTIME_SECS` (14,400 seconds) whenever the caller omitted
them, refusing a value below `1`, the Spec Builder handoff armed
`_EXEC_MAX_CYCLES` (60), and auto-research armed the campaign row's default of
30 cycles. The dashboard popover armed a fresh untouched goal at `0`, its REST
route stored an omitted cap as `0`, and `ctx.nudge` and the Issue Radar crew
runtime arm `0` unless their own caller supplies a cap. Reaching a budget is a
service-enforced finite outcome, not a model judgment that a blocker is
terminal, so this decision removes the prompt-loop approval-stall stop only for
a loop that has such a budget, and gives the two dashboard goal surfaces a
finite default (Phase 1) so the goals people create there have one. A loop with
no finite bound keeps the approval-stall stop, because that stop is the only
service-enforced ending it has, and the bound that decides which case a loop is
in is the one its owner committed, not one the loop writes for itself or arms
for itself again after stopping (§5).
Adding a second consecutive-stall stand-down would again make an unanswered
human approval retire a bounded goal before its declared budget and would
recreate the behavior this decision rejects. Implementations may back off
repeated rechecks, but they do not convert a human-only approval into goal
completion or permanent inactivity.

## Goals

1. Keep autonomous goals active through remediable blockers.
2. Make least privilege an authority ceiling, not permission to self-grant.
3. Report and later recheck human-only approval without repeatedly announcing it.
4. Preserve durable, restart-safe state transitions before another model turn.
5. Preserve finite cycle/runtime budgets and all explicit stop controls.
6. Keep structured-monitor delivery failure distinct from prompt-loop progress.
7. Keep hand-written and generated self-nudge instructions consistent.

## Non-goals

- Automatically approve a tool request or create a new authorization grant.
- Weaken, bypass, or edit security policy, governance controls, trust roots, or
  denied-command configuration.
- Remove cycle caps, runtime budgets, user stop, STOP sentinels, or terminal goal
  completion.
- Change typed structured-monitor outcomes or their review-readiness semantics.
- Guarantee that every dependency can be repaired without human action.
- Add a new UI or notification surface.

## Design

### 1. Blocker classification

A blocker is terminal only when it matches an existing terminal condition. A
missing permission, credential, configuration, dependency, or failing command
is otherwise remediation work. The agent inspects the owning code/configuration,
chooses the least-privilege repair it is already authorized to make, verifies the
repair, and continues the goal.

The same rule appears at every agent-facing producer. Packaged producers, which
ship in the wheel and reach every install: the base prompt
(`src/kiro_crew/config/prompt.md`), the generated `/goal` instruction
(`src/kiro_crew/dashboard/chat_runner.py`), the `autonudge_stop` tool
description (`src/kiro_crew/mcp_tools/control.py`), and the two bundled skills
under `src/kiro_crew/builtin_skills/` whose example nudges arm `monitor_start`
loops — `kirocrew-dev/babysit/SKILL.md` (its example message and execution step
7) and `kirocrew-dev/prepare-pr/SKILL.md` (its example message). The bundled
tree is copied into every install's skills directory on gateway start by
`_ensure_builtin_skills` in `src/kiro_crew/skills.py`, so the installed copies
follow the packaged text and are not edited separately. Repo-checkout producers,
synced into the skills directory only when `KIROCREW_PROJECT_DIR` points at this
checkout and not part of the wheel: the self-nudge recipe
(`skills/self-nudge-loop/SKILL.md`), its scaffolded template
(`skills/self-nudge-loop/scaffold.sh`), and the goal-loop skill
(`skills/goal-loop/SKILL.md`), whose persistence rule tells the agent the
service deactivates the loop on an approval stall. Contract tests pin both the
presence of remediation wording and the absence of generic blocker stop calls
across that set; the goal-loop rule states the bounded/unbounded distinction of
§5 instead of an unconditional stall stop. The `autonudge_stop` description
and the base prompt also say what the stop does under §5: it retains the
loop's record and does not end the commitment its owner made, whichever turn
calls it, so a person who tells the agent to stop and wants the loop ended is
pointed at the popover's Clear stopped goal control, reached after Pause and
backed by `DELETE /api/autonudge/{id}`, or at `/goal clear`.

### 2. Authority ceiling

“Fix the owning permission/configuration” means changing an application-owned
policy or configuration only when the current authorization already allows that
change and the result is least privilege. It never means granting the agent
itself access, changing the approval mechanism that refused it, weakening a
governance rule, bypassing a safety control, or widening its own loop: a bound
the loop's session writes for itself neither reclassifies the loop nor raises a
live bound above the pair its owner committed — it may tighten, or restore up
to that pair, never past it — a stop the loop's session issues, by its stop
tool from any turn whoever started it or by the sentinel file it can write,
retains the record with its
remaining budget, its marker state and its committed classification rather
than opening the slot to a fresh self-armed loop, a re-arm from that session
inherits what the record retains instead of committing new bounds, an arm that
session directs at its own slot through a
proxy — a workflow's `ctx.nudge` on its originating session, which replaces
the active row — carries the commitment and the budget already spent forward
rather than resetting them, a proxy that session launched under a commitment
its owner has since ended or replaced arms nothing at all, a turn that
automation the session set in motion — its own loop's cycle, a subagent or
workflow completion, a cron's origin injection — starts on the slot arms only
under the commitment that automation was scheduled under, never past the
owner's ending of it (§5; the write cap and the retained stop are Phase 1, the
proxy and automation rules Phase 2), and the ending its owner committed is not
the agent's to remove or renew.

When that ceiling leaves only a human action, the loop records the condition,
reports it once, and rechecks later. The agent may perform other safe work in the
same goal between checks.

### 3. Prompt-loop approval evidence

An unanswered prompt records `approval_stalled` in memory and schedules its
persistence: `notify_approval_stalled` in
`src/kiro_crew/autonudge_service/timers.py`
sets the loop's flag and calls `_persist_soon` in
`src/kiro_crew/autonudge.py`, a supervised fire-and-forget write whose
failure is only logged. The marker is therefore not durable by itself. If that
write is lost, a restarted service sees no marker, the loop wakes on its
schedule, and the next unanswered prompt records the marker again; nothing was
spent, because no budget is charged and no authority is exercised by recording.
That is why Phase 1 protects the consumption write below, not the recording,
against loss. Loss is one direction of failure, and the marker fails the
other way too. On the measured base the recorded marker lives on the row in
the agent-writable loop store, so a marker cleared out of band on a stalled
row — a write that landed, not one that was lost — leaves `_timer` nothing
to stop on, and the unbounded loop that had no other ending wakes as though
no prompt had lapsed; nothing re-records it, since no prompt has lapsed
again. A lost recording fails toward re-recording, with nothing spent; a
cleared marker fails toward not stopping. Phase 1 therefore seals where the
marker lives once it is recorded, not the recording itself:
`notify_approval_stalled` writes the marker into the slot's commitment
record before the row, `_timer` reads it there, and the row's copy is a
mirror the record repairs (§5), so a recording that is lost is still made
again and a recording that landed cannot be undone by a row write.
On a legacy prompt or goal loop’s next wake, the runtime:

1. reads the loop's classification from its slot's commitment record (§5) —
   bounded when the committed `max_cycles` or `max_runtime_secs` is positive,
   the rule of §5 — a separate reading from the `cycle_cap` and
   `runtime_budget` exhaustion checks the timer already runs on the live fields
   before it reaches approval evidence, and one that neither a bound the loop
   wrote for itself, nor a stop and re-arm from its own session, nor an arm
   that session directed at its own slot by proxy can move;
2. if the loop is unbounded, deactivates it with the terminal
   `approval_stalled` outcome exactly as today (§5 defines bounded and
   unbounded), and otherwise
3. stages consumption of the marker and, for an observation-gated prompt loop,
   one follow-up tick that bypasses a QUIET observation;
4. persists the staged replacement before publishing it or firing a model turn;
5. publishes the transition and lets the bounded remediation cycle run; and
6. retains the marker and schedules a bounded retry if persistence fails.

Persist-before-publish prevents restart from re-consuming stale evidence after a
turn was delivered. The gate bypass ensures an unchanged watched subject cannot
silence the cycle whose purpose is to repair or recheck the blocker.

The evidence is slot-scoped, not cycle-scoped. `notify_approval_stalled` in
`src/kiro_crew/autonudge_service/timers.py` resolves the loop by slot, and every
approval
surface calls it when a prompt in that slot or session runs its full window with
no decision — the dashboard chat runner, the Slack gateway, the Discord renderer
and the messaging approval path alike — whether the prompt belonged to a
delivered loop cycle or to a person's own interactive turn in the tab a
`monitor_start` loop lives on. The runtime cannot attribute the marker to a
delivered cycle: a dashboard slot's turn outlives the fire window the timer
holds, which is the reason the current code records slot-level evidence in the
first place. This RFC does not invent that attribution. It accepts the
imprecision in the following bounded form, which is the smallest contract the
current architecture supports:

- The marker is one boolean per loop and is recorded at most once until
  consumed, so any number of ignored dialogs between two wakes buys exactly one
  consumption.
- One consumption on a bounded loop buys exactly one delivered cycle that is
  charged to the loop's own `max_cycles` and `max_runtime_secs`; on an
  observation-gated loop that cycle also spends the one follow-up tick that
  skips a QUIET reading. Nothing else is granted: the cycle carries the loop's
  own instruction, the runtime injects no stall-specific prompt, and on the
  dashboard the slot transcript already records the declined prompt as an
  assistant notice.
- That cycle is as authority-gated as any other. A tool that needs approval
  prompts again, and an unanswered prompt records the marker again; each such
  cycle costs one unit of a finite budget, so the worst case is a loop that
  spends its committed budget on cycles a person did not attend, which is the
  ending its owner accepted when committing it. The loop cannot enlarge that
  ending from its own turn: a bound it writes while the marker is set is
  refused, a bound it writes after consuming the marker is capped at the
  committed pair — it may tighten a live bound or restore it up to that pair,
  and a raise above it is refused — a stop and re-arm it issues from its own
  session inherits the budget the record has left rather than a fresh one,
  and a replacement it arms on its own slot by proxy — a workflow's
  `ctx.nudge` on the originating session — carries the cycles and seconds
  already spent forward instead of starting a fresh count (§5; the write cap
  and the re-arm in Phase 1, the proxy in Phase 2).
- An unbounded loop still stops on the marker (§5), and neither a cap the loop
  writes for itself in the turn that let the prompt lapse, nor a stop and
  re-arm it issues from that turn, nor a replacement it arms by proxy from
  that turn changes that, so the direction the code
  chose for the case with no other ending — a conservative stop — is
  preserved exactly where it matters.

Requiring attribution to a delivered cycle before spending the credit is
recorded as a future refinement, not a requirement of either phase
(Alternatives).

### 4. Structured monitors

A structured monitor does not enter the legacy prompt timer branch. Its accepted
action completion is typed. Its approval evidence is the same slot-scoped
boolean §3 describes: when `record_monitor_turn_completion` in
`src/kiro_crew/autonudge_service/monitor_records.py` charges a completed action
turn while the loop's
`approval_stalled` flag is set — set by any unanswered prompt in the slot,
whether or not it belonged to the accepted action — it forces the disposition
to `APPROVAL_STALL` and, unless a spent budget stops the monitor first, records
`approval_stall` on `MonitorState.stopped_reason` with outcome `BLOCKED` and
deactivates the monitor. The monitor does not correlate the stall to the
specific action, and this RFC preserves that behavior unchanged rather than
adding a correlation requirement. What changes is only where the marker is
read, not what the disposition does: on the measured base
`record_monitor_turn_completion` reads `staged.approval_stalled` off its
in-memory copy of the row, the same row-resident field `_timer` stops a
legacy loop on, and Phase 1 has it decide the disposition from the marker
the slot's sealed commitment record holds, the row's copy being the same
mirror §5 makes it for the legacy branch, so a marker cleared out of band on
a stalled monitor's row still yields `APPROVAL_STALL` at the next completion
(§5). The disposition, the `BLOCKED` outcome, the `approval_stall` reason
and the deactivation are as today. That record
is inspectable and restartable, and it remains separate from legacy outer-loop
`approval_stalled` values retained only for pre-upgrade store compatibility.
What Phase 2 adds is the seal on the definition the disposition acts on: the
`kind`, `target`, `objective`, budgets, `cadence_secs` and
`wake_instructions` that `MonitorController.tick` and `_dispatch_claimed`
in `src/kiro_crew/monitoring/controller.py` read off the `MonitorState` are
recorded in the slot's sealed commitment record, as every automation's
definition is, and verified before every tick and wake, so the typed
disposition acts on the definition its owner gave it and not on whatever
the agent-writable store has come to hold (§5).

### 5. Stop conditions

A prompt/goal loop stops only when one of these holds:

- the objective or Definition of Done is complete with concrete evidence;
- the user explicitly asks to stop — through the owner's stop control or
  `/goal clear`, or by telling the agent, whose stop tool then retains the
  loop's record (below);
- a configured STOP sentinel fires;
- a host/tooling failure remains unrecoverable after bounded retries and no safe
  work remains; or
- a service-enforced finite cycle/runtime budget is spent.

The last item is a backstop, not success. A human-only approval is absent from the
list because waiting for a person does not satisfy the goal. Which of these
ends the loop's commitment, and which stops the loop while its record
continues, is the rule stated later in this section: a stop the agent can
trigger — its stop tool, whichever turn calls it, and the sentinel, a file it
can write — retains the record; an owner route the agent cannot invoke and a
service ending end the commitment.

The list assumes a finite budget exists to be spent, so the lifecycle change
applies only where one does. One rule decides it, per loop, at every wake that
reads approval evidence, and it reads the loop's **committed bounds** — the
`max_cycles` and `max_runtime_secs` as committed by the surface that armed the
loop or later revised by its owner — not the live stored values:

- A loop is **bounded** when its committed `max_cycles` or its committed
  `max_runtime_secs` is positive. A bounded loop consumes the
  `approval_stalled` marker and continues (§3); the budget its owner committed
  is the service-enforced ending.
- A loop is **unbounded** when neither committed value is positive — both `0`,
  the only non-positive value the service stores once Phase 1 refuses a
  negative at every boundary (below). An unbounded loop
  keeps the terminal approval-stall stop unchanged: it deactivates with
  `stopped_reason="approval_stalled"`, emits `expired`, and stays inspectable
  and re-armable. This is not temporal and does not lapse once Phase 1 lands:
  a loop with no finite bound behaves this way before and after the change.

The rule reads the Perpetual mode switch that
`docs/request-for-change/rfc-perpetual-agent.md` accepts the same way. That
decision lets an owner set both of a crewmate's caps to `0` and promises that
no cycle count or elapsed-time limit stops its wakes; it says nothing of the
stall stop, which is neither. The loop it leaves is unbounded under this rule
— both committed values `0`, revised to that by the owner — and keeps the
terminal stall stop: a stalled approval deactivates it with
`stopped_reason="approval_stalled"`, emits `expired`, and leaves it
inspectable and re-armable, as today, and neither decision moves a cap to get
there. What the Perpetual decision's own acceptance — one truthful state on
every surface — then requires is that such a stop reads as a stop: the
crewmate shows deactivated with that reason, not active with both caps at
zero, and the owner's switch is the re-arm that restores it, since the mode
leaves no finite cap to recommit. The stall stop is this RFC's decision and
its code PR implements it; surfacing it on the switch belongs to the
Perpetual implementation, which has none on main yet (§6).

The committed pair is defined from the values the service stores, never from
the values a surface was handed, so the record and the classification cannot
disagree about what a cap means. On the measured base the two would: every cap
reaching `_add_unserialized` in `src/kiro_crew/autonudge_service/mutations.py`
is
stored as
`max(0, int(max_cycles))` and `max(0, int(max_runtime_secs))`, and
`_update_unserialized` under `AutoNudgeService.update` stores a revision the
same way, so a negative `max_cycles` that reaches either is not refused but
silently becomes `0` — the value that means unlimited — and a surface that
meant a bound arms none; a negative `max_runtime_secs` never reaches the
clamp, because both transitions open with `validate_runtime_secs`, which
raises `ValueError` for it. Some boundaries already refuse the cycle cap too:
`monitor_start` and
`monitor_update` refuse either field below `1` (`MONITOR_START_SCHEMA` and
`MONITOR_UPDATE_SCHEMA` in `src/kiro_crew/validation.py`), `/goal --max N`
matches digits alone and clamps to 1–50, the Spec Builder handoff and the
Issue Radar crew runtime arm constants, and `authorize_and_add_nudge` and
`authorize_and_update_nudge` in `src/kiro_crew/autonudge_authz.py` refuse a
`max_runtime_secs` outside `0` to the configured runtime ceiling (`604800`
unless `monitoring.max_runtime_secs` sets it). The rest do
not: neither authorizer bounds `max_cycles` below, so `POST /api/autonudge`
(`api_autonudge_start`, which coerces `max_cycles` with `int()` and bounds
it not below), `PATCH /api/autonudge/{id}` and the `monitor_update` directive
behind `authorize_and_update_nudge`, and the popover — whose `parseCycles`
(`parseInt(s, 10) || 0`) passes a typed negative through and whose `min={0}`
is an input attribute `startNow()` never reads — forward a negative `max_cycles`
to the service; auto-research's `validate_campaign` checks
`MAX_CYCLES_HARD_CAP` alone, runs only for the validate and create routes —
the `fork` action in `_handle_action` builds its `fork_config` with
`body.get("max_cycles", 30)` and calls `create_campaign` directly, so a
forked campaign's cap reaches the row unvalidated — and `_launch_loop` hands
`int(row["max_cycles"] or 0)` to `AutoNudgeService.add`; `ctx.nudge` passes
`max_cycles` unchecked
through `nudge` in `src/kiro_crew/workflows/runner.py`, `_nudge_port` and
`_wf_nudge_authorizer`; and the applier `_monitor_start` reads
`int(args.get("max_cycles") or 0)`, a fallback the tool schema keeps from
ever carrying a negative. Phase 1 refuses a negative `max_cycles` or
`max_runtime_secs` at every arming and recommit boundary, with a validation
error that names the field, and never clamps: at the surfaces where a bound
already lives — `api_autonudge_start` and `authorize_and_update_nudge` gain
the lower bound the authorizers already apply to `max_runtime_secs`, the
popover refuses a negative in the field before it sends, and auto-research
refuses one at `create_campaign`, the insert every campaign row passes
through — `_handle_create` and the `fork` action alike — with
`validate_campaign` reporting it beside its `MAX_CYCLES_HARD_CAP` refusal on
the routes that call it, because the service backstop below is the wrong
place to first refuse a stored campaign cap: `_handle_action` publishes
RUNNING through `update_campaign_status` before it calls `_launch_loop`, so
a row that stored a negative would be refused at the service after its
campaign was marked RUNNING, leaving a RUNNING campaign with no worker — and,
as the
backstop no surface can bypass, at the two service transitions that store a
cap, where
`_add_unserialized` and `_update_unserialized` refuse a negative `max_cycles`
instead of clamping it — and keep refusing a negative `max_runtime_secs`, as
`validate_runtime_secs` already does there — raised for both as
`MonitorUpdateConflict`, which
`authorize_and_add_nudge` already returns as a denial, so a `ctx.nudge`
carrying a negative is refused and recorded in its run's stream as its other
refusals are, with the chokepoint itself untouched, and which no revision
reaches, since every one passes `authorize_and_update_nudge` first. After the
refusal the only normalization the
service performs is the `int()` coercion of a non-negative value, and the
record is written from that stored value, so a stored non-positive value —
`0` — classifies unbounded, a stored positive value classifies bounded, and
no value can mean one thing in the row and another in the record. The
measured base stores no negative either, since the clamp always ran, so no
pre-upgrade row carries one; a row hand-edited to a negative in the
agent-writable store, or to the `0` that means unlimited beside a positive
committed cap, disagrees with its record and is read unbounded, as
every disagreeing row is (below).

The stall stop is terminal for every unbounded loop, whatever armed it, and
an app runtime that arms one does not get to undo it. On the measured base
the Issue Radar crew runtime does: `watchdog_cycle` in
`src/kiro_crew/apps/builtins/issue_radar/backend/crew_runtime.py`
re-activates every inactive loop of a live crew on every pass — `if not
loop.active: await svc.update(loop.id, active=True)` — reading no
`stopped_reason`, and `AutoNudgeService.update` in
`src/kiro_crew/autonudge_service/mutations.py`
clears `approval_stalled` on an actual revival. A crew loop the timer
deactivated with `stopped_reason="approval_stalled"` is therefore running
again by the next watchdog pass with its marker gone, on an approval nobody
answered, and the one service-enforced ending an unbounded loop has lasts a
poll interval for exactly the surface that arms `0` by design. Phase 2 has
the watchdog decide a revival from the sealed commitment record this section
keeps, never from the row: the stall stop is a service ending, written into
that record before the row is deactivated (below), as a spent budget is, and
a watchdog never revives a row whose slot's commitment is ended; it revives,
as today, only a row whose commitment is open — a manual pause, an
app-disable suspension — and leaves every other inactive row alone, marker
intact, on every pass. For that decision the row's `stopped_reason` is
display and audit and nothing more, and it has to be: the row lives in the
agent-writable loop store, so a reason forged onto a stalled row — any but
the stall's — survives a restart, since `_load` holds an inactive row beside
an ended record as today (below), and a watchdog that keyed its revival on
the reason would revive the loop on its next pass and clear the marker, the
stall stop undone by a file write. Nor is a revival the only arm the watchdog
holds. On the measured base `watchdog_cycle`, finding no loop for a live
crew's slot — `svc.get_by_slot` returning `None`, which its own account reads
as a crew never launched or a loop a restart lost — calls `launch_crew` in
the same module, which arms a fresh loop through `svc.add` at `max_cycles=0`;
the row is as deletable from the agent-writable store as its reason is
forgeable, so a stalled row deleted out of band, then a restart, leaves
`_load` an ended record beside no row, and a watchdog that read only the row
would arm a fresh loop on the next pass and reopen the ended commitment with
no owner's act. Phase 2 therefore has the watchdog read the record before
that arm as before the revival: an absent row on a slot whose sealed
commitment is ended is the ended state, the watchdog arms nothing there, and
the slot stays ended until the app's Resume route or the owner-gated `PATCH`
reopens it; its fresh arm is admitted only on a slot whose record is absent —
a crew never committed, or a pre-upgrade crew slot with no record (Backward
compatibility) — where it commits the crew runtime's pair as any first arm
does. An open record beside no row does not survive to the watchdog's pass:
the load-time reconciliation closes it, and the transition helper closes it
at the first arm that meets it in-process (below), so the watchdog meets an
ended record and arms nothing; a live crew whose row a crash or an
out-of-band removal took is therefore given its clock back by Resume, not by
the watchdog. The same reading holds against the owner: a crew loop the owner
removes through `DELETE /api/autonudge/{id}` leaves an ended record, and the
watchdog, which on the measured base re-arms it on its next pass, arms
nothing until Resume. The revival of an ended commitment, and the arm on an
ended slot, are put
where the person's act is. The watchdog cannot host it: a stall leaves the
crew `enabled`, and `is_live` in the same module reads `enabled`,
`retired_at` and `paused_reason` alone, so a resumed crew and an unattended
live crew look the same to it. The app's own resume control,
`_handle_crew_pause` in
`src/kiro_crew/apps/builtins/issue_radar/backend/crew_routes.py` for `POST
/crew/pause` with `paused` false, on the measured base only sets the crew's
pause pair — `enabled` and `paused_reason` —
through `set_crew_paused` and, by its own account, does not re-arm because
the watchdog relaunches a live crew on its next cycle; in Phase 2 that branch
itself reopens the ended commitment — a fresh commitment of the crew
runtime's pair, as on a slot holding none, and the same where a pre-upgrade
row has no record at all (Backward compatibility), written through the
transition
helper before the row is revived and advancing the generation as every fresh
commitment does — and revives the loop with its marker cleared, or, where
the slot holds no row, arms one itself through `launch_crew`, the person's
act, while the watchdog always preserves an unattended stall stop and an
ended slot. The
owner-gated `PATCH /api/autonudge/{id}` is the other revival path where a row
remains, and the
watchdog keeps arming a slot that holds no record and reviving a row whose
commitment is open, as today. Until Phase 2 lands, the stall stop on a live
crew loop lasts one watchdog pass, as it does on the measured base.

The Issue Radar watchdog is not the only one. Auto-research's
`_watchdog_loop` in
`src/kiro_crew/apps/builtins/auto_research/campaign/watchdog.py`
does the same for every RUNNING campaign — `if svc is not None and loop is
not None and not loop.active: await svc.update(loop.id, active=True)` —
reading `stopped_reason` only to recognise its own research tombstone,
`AUTONUDGE_STOP_REASON`, so a research loop the timer deactivated with
`stopped_reason="approval_stalled"` is revived with its marker gone on the
first pass that finds its campaign RUNNING. The path is reachable without a
person's answer to the approval: when the 24-hour grant lapses the watchdog
clears slot trust and `_expire_trust` parks the RUNNING campaign
`NEEDS_INPUT`, neither deactivating the loop, a tool prompt on a later cycle
runs its window
unanswered and records the marker, the timer stops an unbounded loop on it —
on the measured base every research loop, since the stop is terminal there
whatever the bounds; after Phase 1 a pre-upgrade campaign row, read unbounded
until its owner recommits it (Backward compatibility) — and
the guidance route `_handle_nudge` (`POST
/api/apps/auto-research/campaigns/{id}/nudge`) then clears the pending
question and returns the campaign to RUNNING through `_guarded_transition`
without calling `_launch_loop`, leaving the revival to the watchdog's next
pass. Phase 2 gives that watchdog the same rule as Issue Radar's: it revives
an inactive loop of a RUNNING campaign only when the slot's sealed commitment
is open — the app-disable suspension it exists for — and never one whose
commitment the stall stop, or a spent budget, ended, whatever reason the row
carries, so a stalled loop stays inactive, marker intact, on every pass; the
row's reason it goes on reading for its own tombstone alone. The rule against
a fresh arm on an ended slot binds it too, though on the measured base it has
no such arm to bind: `_watchdog_loop` calls no `svc.add`, a RUNNING campaign
whose slot holds no loop is left without one until `_handle_action` arms it
through `_launch_loop`, and Phase 2 keeps it so — a research row deleted out
of band beside an ended record is reopened by `resume` alone. The revival is
the person's act: the app's explicit
`resume` in `_handle_action`, which re-arms through
`_prepare_loop_launch` and `_launch_loop` — a fresh arm at an ended record,
so a fresh commitment as on a slot holding none, whether the stalled row is
still in the store or was deleted from it — for a campaign the guidance
route has already marked RUNNING, the path is `pause`, then `resume`, as
`resume` is refused from RUNNING — or the owner-gated `PATCH
/api/autonudge/{id}` where a row remains. That fresh commitment is not one
the route makes on the measured base by re-arming as it does: `_launch_loop`
arms through a plain `svc.add(...)` — no `authorize_and_add_nudge`, no
`initiator_slot_key`, no generation — and `_handle_action` awaits
`_prepare_loop_launch`, which removes the research tombstone through
`svc.remove`, an ending under Phase 1, then publishes RUNNING through
`update_campaign_status` — after that cleanup by design, so the watchdog
never sees a resumed campaign beside its previous run's stop evidence — and
only then calls `_launch_loop`.
Under the rule against a fresh arm on an ended slot, a `svc.add` carrying no
owner authority is refused there, after RUNNING has been published — a
RUNNING campaign with no worker, the state the negative-cap ordering above
already names as the one to avoid. Phase 2 therefore has the resume route
carry an authenticated owner-resume signal, as the Issue Radar resume route
carries its own act, into the locked add transition at `_add_unserialized`,
and decide the fresh commitment there — the record written through the
helper, the generation advanced — before `update_campaign_status` publishes
RUNNING: a refused commitment leaves the campaign in the status it held and
reports the refusal in the route's response, and `_launch_loop` then arms
the row under a commitment already made; `_prepare_loop_launch`'s removal of
the tombstone stays an ending, ordered before it. The route passes the same
signal for `start`, whose slot holds no record, so it commits fresh as any
arm on such a slot does, and a `resume` after `pause` — whose `_stop_loop`
deactivates the row and keeps the commitment open — is an inheriting re-arm
at that record, no fresh commitment made. Until Phase 2 lands, the stall stop
on a research loop
lasts until its campaign is next RUNNING, as it does on the measured base.

Who commits a bound decides which values the rule reads. The arming surfaces
of §6 commit the initial pair, and `PATCH /api/autonudge/{id}`
(`api_autonudge_update` in `src/kiro_crew/dashboard/handlers/autonudge.py`) is
gated on the dashboard owner by `_require_monitor_owner`, so a bound it writes
is the owner's and replaces the committed pair. A bound written from the loop's
own session does not. `monitor_update` is the one bound-writing path a
session's turn holds, and on the measured base nothing on that path reads
approval evidence. When an approval prompt runs its window unanswered, the
chat runner (`src/kiro_crew/dashboard/chat_runner.py`) declines the tool,
records the marker through `notify_approval_stalled`, and tells the agent to
ask for the permission it needs; the turn continues. That same turn may call
`monitor_update` with `max_cycles` up to `1000` or `max_runtime_secs` up to
the configured runtime ceiling, `604800` by default (`MONITOR_UPDATE_SCHEMA`
and its `_validate_monitor_runtime` in `src/kiro_crew/validation.py`). The
applier `_monitor_update` in
`src/kiro_crew/dashboard/session_directive_apply.py` refuses only a cap at or
below the delivered cycle count or a budget at or below the loop's age; its
approval-stall branch guards a paused loop, not a live one; and
`AutoNudgeService.update` clears the marker only on an actual revival and
otherwise leaves it in place, refusing nothing on its account. Read on live
values, that write would make an unbounded loop bounded before its next wake,
spend the marker on a cycle nobody authorized, and let the loop remove the
ending §3 says it keeps — the self-widening §2 forbids. Nor is the
classification the only thing the write reaches. A bounded loop that consumed
its marker and continued holds the same tool in its remediation turn, and once
the marker is clear nothing on the measured base stands between it and
`monitor_update(max_cycles=1000, max_runtime_secs=604800)`: the applier's two
guards measure a request against the cycles already delivered and the seconds
already elapsed, never against the pair the owner committed, and `_timer` in
`src/kiro_crew/autonudge_service/firing.py` enforces the live fields the write
lands in. A
loop armed at 24 cycles and 14,400 seconds would run to a thousand cycles and
seven days — the schema ceiling, by default, is the store's limit, not the
owner's budget —
renewing from its own turn the ending its owner committed, with no stop, no
re-arm and no marker left to refuse it. Phase 1 therefore records the
committed pair, per slot, when a bound is committed — at `_add_unserialized`
in `src/kiro_crew/autonudge_service/mutations.py`, the one creation point every
arming surface
of §6 reaches through `AutoNudgeService.add`, and at the owner-gated `PATCH`
recommit through `AutoNudgeService.update` — and decides every `max_cycles` or
`max_runtime_secs` write from the loop's own session — `monitor_update`, or
any other bound write the session directs at its own loop — against that
record, completing the principle of this section that a loop's session may
spend the committed budget but never enlarge it. The service cannot tell the
two writes apart on the measured base, and Phase 1 must tell it: the owner's
`api_autonudge_update` and the applier `_monitor_update` both call
`authorize_and_update_nudge`, which by its own account checks no ownership —
`loop_id` is opaque and the module holds no session identity — and hands
`svc.update` the same fields either way, so `_update_unserialized` sees a
recommit and a self-session write as one call; the `source` the two callers
pass (`"dashboard"` and `"mcp-directive"`) reaches only the SEL audit. Phase
1 therefore threads an explicit owner-recommit signal from
`api_autonudge_update`, set only after `_require_monitor_owner` has
succeeded on that route, through `authorize_and_update_nudge` into
`AutoNudgeService.update` and the locked transition at
`_update_unserialized`, where a write carrying it recommits the pair and a
write without it is decided against the record under the rules below. Every
other caller of the authorizer or the service — the applier, an app runtime,
`_timer` — passes nothing and is read as non-owner; the signal is not a
field a request body or a directive may set, so no turn the agent runs can
carry it:

- A self-session write is capped at the committed pair. It may tighten a live
  bound, or restore one up to its committed value, never above it; the cap is
  read per field, so a field the owner committed at `0` has no ceiling of its
  own, and the recorded classification does not move.
- A write asking for more than the committed pair is refused, in the manner
  and voice the applier already refuses a cap at or below the delivered count:
  the refusal names the committed ceiling and the owner routes that recommit
  it, and the live bounds are unchanged. An unbounded request — a `0`, which
  `MONITOR_UPDATE_SCHEMA` already refuses at the tool — asks for more than any
  finite pair and is refused on the same ground should a directive carry it;
  a negative, which the schema refuses too, is refused as a validation error
  at every boundary rather than clamped to that `0` (above).
- A committed pair of `0` and `0` has no ceiling to exceed. A finite
  self-written bound only tightens the live values; the classification stays
  unbounded, and the loop still stops on the marker at its next wake whatever
  its live cap says. The record exists for exactly this loop. A pre-upgrade
  row, whose slot holds no commitment record and is read unbounded (Backward
  compatibility), has its stored bounds as the ceiling: a self-session write
  may tighten them and never raise them, since the store holds no committed
  value to restore up to.
- The refusal while `approval_stalled` is set stays as the special case: while
  the marker is set, no bound write from the loop's own session is applied at
  all.

A fresh recommitment for a slot that already holds a pair comes only from the
owner, through `PATCH /api/autonudge/{id}` behind the popover and the other
owner routes this section names below. A slot with no committed loop takes its
first pair from whichever §6 arming surface arms it.

One rule decides where every value the rule reads may live, and the fields
this section and Phase 2 name are its instances, not rules of their own: no
field that `_timer`, an automation's fire path or a gate trusts as authority
— takes as deciding whether or what to run — and no field the fire path
hands the run as its input, to be acted on under the owner's provenance and
approval mode, may live outside the sealed commitment record. A field of
the row, or of an automation's definition, that the seal does not cover is
agent-writable by construction — the stores below say so of themselves —
and is therefore not an authority and not an input, whatever it is named
and whichever process wrote it last; it may mirror the record for display
and for compatibility, and the record decides. Reading a field is not
trusting it: a decision may read an unsealed field for display, or
re-derive what it needs from sealed inputs, and neither read makes the
field an authority. Feeding a field to the run is trusting it: the turn it
reaches runs as the owner's automation, and only the gateway's own write of
that automation's run speaks for it, so the seal covers what a decision
trusts as authority and what the run is handed as input alike — the split
of a cron row into its definition, the run state the fire path hands the
run, and the run state it only displays or times by, below, is this rule
applied. The committed pair and the classification, the `approval_stalled`
marker and the `stop_sentinel_path` (Phase 1), the spend, the generation,
the provenance triple and the definition digest (Phase 2) each entered
under that test, and a field a later reading finds the timer or a fire
path deciding by, or handing the run, read off a writable store, is
admitted the same way — sealed, or struck from the decision by re-deriving
it from what is sealed — rather than by a new rule.

The record the rule reads cannot live in the loop store. The service persists
its loops to `~/.kiro/crew/autonudge.json`, and `src/kiro_crew/autonudge.py`
calls that store agent-writable out of band — a hand-edited file or a direct
`svc.add` — and scrubs what it loads from it on that account. A committed
pair or a classification recorded on the row would be a hint the agent, or a
cron it created, could rewrite once the turn's own writes have persisted, and
a restart would trust the forged bounds. Nor can it live under `trust/`. That
directory is keystone-gated only in the sense that `_CREW_SECRET_LEAVES` in
`src/kiro_crew/security/paths.py` lists it: the gate fences the resolved
paths the agent's file tools open — `is_sensitive_path` refuses every such
path — and no more. No bash text matcher extends it to command text:
`is_sensitive_bash_command` in the same module matches no path in a command,
by its own account, so a shell is held only by the OS disposition of the
target. Inside the sandbox the directory is read-write in every mode —
`_CREW_SANDBOX_VISIBLE_LEAVES` in `src/kiro_crew/sandbox.py` keeps `trust`
and `sel_hmac.key` writable on purpose, because `verify_session_pid` reads
`trust/sel_hmac.key` there and the in-sandbox MCP servers append to the SEL
log — so a runtime-composed shell path or a spawned interpreter's `open()`
reaches a file under it with no file-tool path for the gate to see, the
evasion the
module's own note on `agent_model_state.json` names and the reason
`file-delivery-consent-pending` was given its own leaf rather than a place
under `trust/`. A commitment forged there would be accepted as the owner's;
the rejected alternative "Record the commitment under `trust/`" gives the
account.

Phase 1 therefore keeps the commitment in a dedicated leaf of the crew home,
`autonudge-commitments.json`, under a disposition of three layers — two
refusals, each holding where the other cannot look, and a writer set — and
this section names all three because a reader shown the seal alone would
read the leaf as safe on a host where the seal is not in force. The first
layer is the OS seal on every sandboxed process: the leaf is listed in
`_CREW_READONLY_LEAVES` in
`src/kiro_crew/sandbox.py` beside `subagents`, `member-memory-bindings`,
`memory_stores` and `security_policy.json`, and the OS seals every entry
there read-only in every sandbox mode — the macOS seatbelt denies the write
by literal path, the Linux namespace mount-seals it — whichever interpreter
opens it and however a process spells that path. The seal binds the path,
though, and not the inode behind it, and the module says so:
`_warn_if_alias_backed` in `src/kiro_crew/sandbox.py`, which
`_materialize_sealable_ceilings` calls on every file leaf it finds already
present before a Linux spawn, reports two shapes the read-only bind cannot
cover and then CONTINUES — a leaf that is a resolving symlink, whose name
sits in the writable data home so a sandboxed process can unlink it and put
a file of its own there, and a regular file carrying a second hardlink, a
path outside the mount through which a write reaches the very inode the
ceiling exposes — warning rather than refusing, by its own account, because
a dotfile manager or a snapshot tool leaves such names behind and a refusal
there would fail every sandboxed spawn on the host. A leaf that carried a
second name before the seal was laid is therefore sealed in name only, and
the first layer, read alone, would let a pre-planted alias hand the record
to the very process the seal holds: the seal warns and continues, a
sandboxed process writes through the other name or replaces the link, and
the next gateway load reads forged caps, a cleared marker or, from Phase 2,
an advanced generation as the owner's. The module's answer for a leaf whose
contents pick an unrecoverable action is a check at the seam that CONSUMES
the file, bound to the descriptor the bytes were read from —
`require_unaliased_launch_state(path, fd=...)` in the same module, over
`_require_real_file_nofollow`, which `LaunchState.load` and `try_clear_tag`
in `src/kiro_crew/cloud/launch_state.py` call once by name before the open,
the call that refuses a symlink and names the remedy, and which
`_read_document` beneath them calls once more on the open descriptor after
the read, the call that ties the judgement to the inode consumed — and
Phase 1 gives every
read of the commitment record that check (below), so the seal is defence in
depth for the record, as the module already calls it for those two leaves,
and not the property the record rests on. Because the Linux
seal needs a file to bind, the leaf is also listed in
`_CREW_PRECREATE_READONLY_FILE_LEAVES` and materialised as `{}` before every
namespace spawn, which is sound on that list's two criteria: an empty record
means what an absent one means — no slot holds a commitment, so every row is
read unbounded and refused as a record to inherit — and a stale sealed read
fails toward refusal, since the writer publishes through `atomic_write`, a new
inode, and a sandboxed reader frozen at `{}` sees fewer commitments than the
truth, never more. The seal binds only a process the sandbox wraps, and not
every agent process is one: `agent.sandbox` accepts `"off"`, and the module
falls back to no sandbox where the OS mechanism is unavailable, by its own
account, so on such a host the first layer holds nothing. The second layer
is the write gate on the agent's file-edit tool, which reads the sandbox
lists not at all: `on_tool_call` in `src/kiro_crew/hooks.py` decides an
edit-kind call by `is_sensitive_write_path`, exported from
`src/kiro_crew/security/__init__.py` and defined in
`src/kiro_crew/security/paths.py` over `_WRITE_PROTECTED_HOME_PATHS`, and
consults neither `_CREW_READONLY_LEAVES` nor the seal, so under the seal
alone a file-edit call naming `autonudge-commitments.json` is admitted by
the gate and, wherever the seal is not in force, writes the record and
forges the owner's committed bounds. Phase 1 therefore lists the leaf in
`_WRITE_PROTECTED_HOME_PATHS` as well — the write-only tier, beside
`subagents`, `agent_model_state.json`, `settings_seeds.json` and
`cloud.json`, not the read+write floor `_SENSITIVE_HOME_DIRS`, because the
record holds no secret and a read of it costs nothing — so the hook refuses
the edit in the gateway before it runs, whatever the sandbox setting, and
`_edit_target_denial` in `src/kiro_crew/llm_helpers.py` refuses it on its
path by the same function. That gate fences the target an edit call spells
— every accepted path key and the diff block's path, resolved through
symlinks and lexically — and nothing else; like the read+write floor, which
`is_sensitive_path` applies to the resolved paths the file tools open and no
bash text matcher carries into command text — `is_sensitive_bash_command`
matches no path there, by its own account — the write-only tier is read by
a file-tool gate alone, so a sandboxed shell's runtime-composed path or a
spawned interpreter's `open()` reaches the leaf with no path for any gate to
see, and the shell's only fence is the OS disposition of the target, which
is exactly why the first layer exists. Each holds where
the other cannot look — the pairing `src/kiro_crew/sandbox.py` already
gives `cloud.json`, `settings_seeds.json` and `agent_model_state.json`, and
calls the both-layers treatment. What neither reaches is a shell the agent
spawns on a host whose sandbox is off: that is the write-only tier's
standing residual, shared by every ceiling on it and by the agent-writable
loop store beside the leaf, which this document neither widens nor narrows;
the default `"auto"` seals the child. The third layer is the set of
writers, and it is what makes the record the owner's on every host: the
committed pair, keyed by slot, is authoritative in that leaf, written only
by the unsandboxed gateway process, where the autonudge service, the
authorizer and the owner routes all run, through one service-level
transition helper at every row transition named below — a fresh pair at
`_add_unserialized` on a slot holding none, a recommit at
`_update_unserialized` under the owner-gated `PATCH`, told from the loop's
own `monitor_update` by the owner signal that route alone passes (above),
an ending at every site that stops, removes or displaces a row — a retained
stop writing its source-qualified ending before the row it deactivates, an
inheriting re-arm or replacement writing that ending back to open before
the row it arms, neither touching the committed pair or the generation
(§5, below) — and from Phase 2 the delivered-cycle
charge `_timer` records (below) — each under the service `_lock` and
before the agent-writable row is written — and never by a self-session
bound write, which is decided against it and moves nothing in it. No code
that runs in the agent's process writes the record, so the agent, its
shells, its crons and the interpreters it spawns can at most read it: a
sandboxed write meets the seal, the file-edit tool meets the gate. What
the gateway reads, it reads through one reader, and that reader trusts no
name. Every read of the record — `_load`'s reconciliation, `_timer`'s reads
of the marker and the sentinel path and, from Phase 2, of the spend and the
generation, `record_monitor_turn_completion`'s read of the marker, the
reads the gate makes at `_add_unserialized` and `_update_unserialized` and
at a retained record a re-arm is decided against, and the read the
transition helper makes before every write — goes through the helper's
reader, which opens the leaf with `O_NOFOLLOW`, reads the bytes, and
verifies on that open descriptor, as `require_unaliased_launch_state` does,
that what it read is a regular file whose link count is one, having refused
a symlink by name before the open. A read that fails the check is decided
exactly as a read that finds no record — every slot uncommitted, its rows
read unbounded and refused as a record to inherit, and, once Phase 2
requires a sealed definition of every row (below), armed not at all until
their owners re-arm them; nothing is adopted from the bytes — the
fail-closed reading Phase 1 already gives a missing or
disagreeing record, and writes an SEL audit line naming the leaf and the
shape it found. The writer meets the same check before it publishes, under
the service `_lock` every writer of the leaf holds: a leaf found to be a
symlink or a multiply-linked file is neither written through nor adopted —
the helper unlinks the aliased name, publishes a fresh regular file holding
no commitment in its place through `atomic_write`, audits the event, and
applies its transition to that fresh record, so a fresh arm commits on a
slot that holds nothing and a recommit or an ending finds no record and is
decided as Phase 1 decides one; the removal loses no write, since the
helper is the leaf's only writer of commitments and holds the lock, and the
spawn path's pre-creation publishes only the empty document, no-clobber —
whichever lands, the leaf holds no commitment — which is not the race
`_materialize_sealable_ceilings` refuses to run for a keystone the
dashboard also publishes over the same name. The first load an upgraded
gateway makes treats a leaf it finds aliased the same way, so a pre-planted
alias is refused at cutover rather than adopted, and every commitment the
aliased document held is dropped rather than read — the direction the
pre-created `{}` already fails toward, fewer commitments than the truth and
never more — and the owners recommit through the routes. What the check
cannot see is stated, as `require_unaliased_launch_state`'s own account
states it: an alias that existed earlier, was written through in place and
was unlinked before the read leaves a lone regular file holding forged
bytes, and no `lstat` or `fstat` can tell that an inode once had a second
name; the seal and the gate are what keep an agent from creating the alias,
and the check is what stops a record being consumed from one that already
exists. An
ending of the commitment marks the record ended, and once Phase 2 adds the
generation, advances it in the same record write; the order of that write
against the row's, and what a crash between the two leaves behind, are
stated below. Read-only rather than masked,
and write-protected rather than sensitive, because the record holds no
secret — slot keys, a pair, the marker, the sentinel path the loop's own
instruction already names and, from Phase 2, a generation and the spend the
loop has consumed — and an
operator reading why a row is unbounded costs nothing. For the same reason
the leaf is classified in `_CREW_CHILD_READABLE_LEAVES` in
`src/kiro_crew/sandbox.py` and not in `_CREW_CHILD_WITHHELD_LEAVES`, the
pair of lists deciding what an enforced foreign harness's child may read:
the governance-mask pin in `test/test_sandbox_governance_mask.py`
(`test_every_non_hidden_leaf_is_classified_for_a_foreign_child`) requires
every leaf of `_CREW_SANDBOX_VISIBLE_LEAVES` and `_CREW_READONLY_LEAVES` to
appear in exactly one of the two, so listing the leaf read-only forces the
choice rather than leaving it implicit, and readable is the side that is
true of it — a child reading committed bounds learns nothing it can use,
the risk the leaf carries is a write, which the seal answers, and
withholding it would mask a read the record was made readable for. The
classification is for completeness rather than for effect, as the module
says of `panel-templates` and `subagents`: a leaf on the write-only tier
and off the read-gate floor is one the child mask never covers, so neither
side changes what any child can open. The row's writable fields must agree
with the record: the timer reads the classification from the record, not
the row, and a live bound above a positive committed field, a live `0` in
either cap field beside a positive committed value for it, or a live bound
no surface could have stored — a negative — is a
disagreement. The `0` is named because to `_timer` it is not a bound: the
cycle check is `if loop.max_cycles and ...` and `runtime_budget_exceeded`
returns false for a `0` budget, so a live `0` written over a committed `24`
is the widest raise the row can carry, not a tighten, and a rule that read
only "above" would let a rewritten row pass reconciliation, keep its
bounded classification, consume `approval_stalled` and run past the owner's
cap; the self-session write rule above already refuses the same `0` as a
request for more than any finite pair, and the row reading agrees with it.
A committed `0` in a field sets no ceiling there, so no non-negative live
value for that field disagrees with it. From Phase 2 the row's spend fields,
`cycle_count` and `created_ts`, are a mirror of the cycles the record has
charged and the origin it seals, not an authority: the cap and the budget
are decided from the record's spend whatever the row claims, and a row found
behind its record — claiming to have spent less than the record charged — is
brought up to it at load and at every tick, never the record down to the row
(below). The row's `approval_stalled` and `stop_sentinel_path` are mirrors
from Phase 1, of the marker and the kill-switch path the record holds
(below): a row whose marker disagrees with the record's — cleared where the
record carries it, or set where the record has none — or whose path differs
from the record's is brought back to the record at load and at every tick,
the record never to the row, and the pair's fail-closed reading is not
applied to them, since neither is a claim about what the owner committed;
the record decides the stall stop and the sentinel stop whatever the row
shows. A row whose slot holds no
record, or
disagrees with it in a cap field, fails
closed — read unbounded at its wake, so it stops on the marker, and refused
as the record a re-arm or replacement could inherit, until the owner
recommits it through `PATCH`. That reading is Phase 1's, where the record
holds the pair and the row still supplies `_timer`'s operands; from Phase 2,
where a slot holds a pair, `_timer` takes the cap and the budget it
enforces from the committed pair, as it takes the classification and the
spend (below), so a cap field rewritten on the row — the `0` written over
a committed `24`, a budget raised — decides nothing: the disagreement is
audited, the row is brought back to the pair at load and at every tick,
and the loop runs to the owner's cap and no further, which is the reading
Phase 2 gives it (below). A pre-upgrade row has no record for exactly
that reason, which is the reading Backward compatibility gives it.

The pair is not the only value the bound is decided from, and the rule above
reaches the rest. `_timer` in `src/kiro_crew/autonudge_service/firing.py`
decides
the cycle
cap by `if loop.max_cycles and loop.cycle_count >= loop.max_cycles` and the
wall-clock budget by `runtime_budget_exceeded`, which measures from
`loop.created_ts`, and on the measured base both `cycle_count` — advanced by
`loop.cycle_count += 1` at the single point a delivery is confirmed — and
`created_ts`, stamped by `_add_unserialized` at the arm, are fields of the
row in the agent-writable store, which only the owner's own resume resets:
`api_autonudge_update` passes `fresh_run=True` into
`authorize_and_update_nudge`, and `_update_unserialized` zeroes
`cycle_count` and restamps `created_ts` on an actual revival alone, so the
reconciler's re-arm and a `monitor_update` bound raise leave both standing.
A record sealing
the pair alone therefore leaves the spend where the agent can write it: a
`cycle_count` lowered out of band, or a `created_ts` moved later, regains
cycles and seconds the owner's pair had already spent while the row's caps
still agree with the seal, a raise the disagreement rule cannot see in the
caps — two fields that read as bookkeeping, deciding the bound. Phase 2
therefore seals the spend beside the pair: the cycles the record has charged,
and the runtime origin the fresh commitment stamped — the `created_ts` the
arm writes, sealed once per commitment, since the elapsed time is measured
from it and needs no per-tick charge — and has the timer decide the cap
against the record's charged cycles and the budget from the record's origin,
never from the row's two fields, which mirror the record and decide nothing.
The delivered-cycle charge is a row transition like the others and goes
through the helper record-first: at the point `_timer` confirms a delivery,
where `loop.cycle_count += 1` runs today, the record's charged count is
advanced and published before the row's is — the `_persist_locked` write
that follows the increment today — so a crash between the two leaves a row
behind its record, never ahead of it, and load brings it up; a failed
record write
there leaves the charge owed, the row unmoved, and the loop firing no
further turn until the charge lands at a later pass, as §3 treats a failed
marker-consumption write. An inheriting re-arm or a replacement reads the
remainder it caps against from the record's spend, so the cycles and seconds
the slot-close restore `_restore_slot_nudge_loop` computes from the retired
row are, from Phase 2, the record's; a fresh commitment charges from zero at
its own origin. A row rewound out of band regains nothing: the bound was
never read from it, and load and every tick bring it back up to the record.
The row is the one file written toward the other here, because its spend,
like its marker and its sentinel path (below), is a mirror and not a claim;
the pair, which is a claim, is never repaired from
the row and is read fail-closed instead (above). What the seal cannot hold
is a crash between a confirmed delivery and its record write, which leaves
that one cycle uncharged — an undercount of one delivered turn per crash, a
residual no row write reaches or widens.

A structured monitor spends from the same store, by fields of its own, and
the rule reaches them the same way. On the measured base
`monitor_budget_reason` in `src/kiro_crew/monitoring/decision.py` decides
the monitor's budget stop from the `MonitorState` the row carries: the
runtime budget from `state.created_ts` against `budgets.max_runtime_secs`,
the turn budget from `state.agent_turns` against `max_agent_turns`, the
token budget from `state.total_tokens` — the sum of `input_tokens` and
`output_tokens` — against `max_tokens`, and the error budget from
`state.provider_error_count` against `max_provider_errors`; `_decide_effect`
in the same module, `apply_monitor_probe` in its `STOP_BUDGET` arm,
`stop_monitor_if_budget_exhausted`, `mark_monitor_action_in_flight`,
`record_monitor_turn_completion` and `record_monitor_dispatch_busy` in
`src/kiro_crew/autonudge_service/monitor_records.py`, and the busy-retry branch
of
`MonitorController.tick` in `src/kiro_crew/monitoring/controller.py` each
call it before a probe or a turn is spent. Its operands are charged by the
service — `record_monitor_turn_completion` advances `agent_turns` and adds
the completion's `input_tokens` and `output_tokens`, `apply_monitor_probe`
advances `provider_error_count` on a charged provider error, and
`_add_monitor_locked` stamps `created_ts` at the arm — and persisted into
the agent-writable row, from which `monitor_state_from_dict` in
`src/kiro_crew/monitoring/models.py` adopts every recognised field of the
current version that `MonitorState`'s own checks accept and
`AutoNudgeService.start` arms the loaded active row. The seal so far covers
none of them, so the legacy rewind has a structured twin: `agent_turns`,
`input_tokens`, `output_tokens` or `provider_error_count` lowered out of
band, or `created_ts` moved later, regains turns, tokens, errors and
seconds the owner's budgets had already spent while the sealed `budgets`
still agree with their record. Phase 2 therefore seals the structured spend
beside the pair by the legacy rule, word for word where it applies:
`created_ts`, the origin the arm stamps, sealed once per commitment since
the elapsed time is measured from it, and the four charged counts, the
record authoritative and the row a mirror repaired from it at load and at
every tick — `monitor_budget_reason` decides by the record's values at
every caller, and a lowered count or a later `created_ts` on the row
regains nothing. Each charge is a row transition the service already
stages and persists as one unit through `_persist_staged_monitor_locked`,
and goes record-first there: the record's count is advanced and published
before the staged snapshot is written and applied to the live state, so a
crash between the two leaves a row behind its record, never ahead of it,
and load brings it up. A failed record write of a turn charge leaves the
charge owed and the claim in flight — `apply_monitor_probe` returns
`NO_CHANGE` while `wake_in_flight` holds, so no further wake is dispatched
— until the completion lands at a retry, or `completion_evidence_deadline`
retires the watch fail-closed through
`record_monitor_completion_evidence_unavailable`, as the measured base
already retires a wake whose completion never reports; a failed record
write of a probe's error charge leaves the probe unrecorded, as a failed
snapshot write leaves it today. What the seal cannot hold is the legacy
residual's twin: a crash between a completed turn and its record write
leaves that turn and its tokens uncharged, an undercount no row write
reaches or widens.

Nor is the bound the only ending the timer decides from the row. Two more
of the row's fields decide the other endings — the stall stop an unbounded
loop keeps, and the sentinel stop — and the rule reaches them in Phase 1. On
the measured base `_timer` in `src/kiro_crew/autonudge_service/firing.py` tests
`if
loop.approval_stalled:` and deactivates the loop with
`stopped_reason=APPROVAL_STALL_REASON`, and, ahead of that check and of
every fire, tests `Path(loop.stop_sentinel_path).exists()`; both are fields
of `NudgeLoop`, persisted with the row into the agent-writable store this
section has just declined to keep the pair in, and nothing reconciles
either: `notify_approval_stalled` sets the marker on the row and calls
`_persist_soon`, `_add_unserialized` stamps the path its caller supplied —
`authorize_and_add_nudge` resolving a default through
`resolve_stop_sentinel` and refusing a sensitive one — and `_load`
re-homes the path through `repair_sentinel_path` and drops one that has
become sensitive, comparing it with nothing. A marker cleared out of band on
a stalled row — one write in the store the agent can reach, where this
section spends a watchdog argument on a forged `stopped_reason` and a
deleted row — leaves the timer nothing to stop on, and the one
service-enforced ending an unbounded loop has is gone with no revival made;
a `stop_sentinel_path` rewritten to a location nothing writes, or blanked,
is the dead kill switch `repair_sentinel_path` exists to undo for a moved
data home, produced on purpose. The disagreement rule above cannot see
either, being defined over the cap fields, and §3's reason for leaving the
recording write unprotected — a lost marker is recorded again and nothing
was spent — covers a write that was lost, which fails toward re-recording,
not one that was made, which fails toward not stopping. Phase 1 therefore
seals both beside the pair, as instances of the rule above and not as rules
of their own. The marker is written into the slot's record before the row
at the site that sets it, `notify_approval_stalled`, through the transition
helper — the recording write §3 leaves unprotected against loss becomes a
record write, still unprotected against loss and re-made by the next
unanswered prompt if it is lost, while a failed record write leaves the
marker owed and the loop delivering no turn until it lands at a later pass,
as the delivered-cycle charge is treated (above) — and at every site that
clears it, gateway writes already: the timer's consumption on a bounded
loop (§3) and `AutoNudgeService.update`'s clear on an actual revival, under
`_update_unserialized`, which the owner-gated `PATCH` reaches, the two
watchdogs reach until Phase 2, and the Issue Radar resume route reaches
from Phase 2 (above). The path is
written into the record at the arm, where `_add_unserialized` stamps it,
and at the load-time repair, where `repair_sentinel_path` re-homes or
drops it — gateway writes both, resealed in their own transition.
`_timer`'s stall branch and its sentinel branch read the record, and the
row's two fields are mirrors, repaired to the record at load, in `_load`,
and at every tick — a marker cleared where the record carries it or set
where the record has none, a path rewritten or blanked — so a marker
cleared on a stalled row's copy stops the loop at its next tick as if it
had never been cleared, and a sentinel written at the sealed path stops the
loop whatever path the row names. The timer is not the marker's only
reader. A structured monitor's stall disposition is decided from the same
field: `record_monitor_turn_completion` in
`src/kiro_crew/autonudge_service/monitor_records.py`
reads `staged.approval_stalled` off its in-memory copy of the row to force
`MonitorActionDisposition.APPROVAL_STALL`, and then records
`MONITOR_STOP_APPROVAL_STALL` with outcome `BLOCKED` and deactivates the
monitor (§4) — the same row-resident boolean, read the same way, so a
marker cleared out of band on a stalled monitor's row would have the next
completion charge the turn as though nothing had lapsed. Phase 1 has that
site read the record too, the row's copy the same mirror, and changes
nothing of what the disposition does once decided. The sentinel has no
structured reader to seal: `_add_monitor_locked` builds its `NudgeLoop`
with no `stop_sentinel_path`, and `_timer`'s structured branch returns
before the sentinel test, so a structured monitor never reaches it. The
retained record a self-session stop
carries the marker in (below) is this same field, held across the stop
rather than given a second home. A row whose slot holds no record — a
pre-upgrade row — keeps both fields on the row, as on the measured base,
until a record is written for its slot; its marker can be cleared as it can
today, which is the reading Backward compatibility already gives every
field such a row stores, and the loop is read unbounded whatever happens to
them.

The stand-down is a third ending the timer decides from the row, and the
same instance of the rule reaches it. On the measured base `_timer` in
`src/kiro_crew/autonudge_service/firing.py` reads
`loop.consecutive_start_failures` ahead
of every fire: at `_START_FAILURE_STANDDOWN_AFTER` it deactivates the loop
with `stopped_reason=SESSION_START_FAILURE_REASON`, and from
`_START_FAILURE_BACKOFF_AFTER` it defers the wake once per streak value,
through `_start_failure_deferred`, instead of spending it. The streak is a
`NudgeLoop` field of the same agent-writable row, and nothing reconciles
it: `notify_cycle_start_failed` advances it and calls `_persist_soon` when a
turn never obtained a model session, `notify_cycle_landed` zeroes it when a
turn completes, `_update_unserialized` zeroes it on a revival, and `_load`
normalises it as a number through `_repair_number`, comparing it
with nothing. A streak lowered out of band keeps a loop whose sessions
cannot start firing past the stand-down, and past every deferral, its
owner's tooling would have applied — it fails toward not stopping and not
backing off; a streak raised out of band stands a healthy loop down, a
false ending the owner must clear. Phase 1 seals the streak beside the
marker, as decision state and as one more instance of the rule: written
into the record before the row at `notify_cycle_start_failed` and at the
two sites that zero it, `notify_cycle_landed` and `_update_unserialized` —
gateway writes all three, each in its own transition — with `_timer`'s
stand-down and back-off branches reading the record, and the row's copy a
mirror repaired FROM the record at load, in `_load` after its numeric
repair, and at every tick, the disagreement audited and the value never
reset in either direction, since a lowered copy fails toward not stopping
and a raised one toward a false stop. A structured monitor has no reader to
seal here: `_timer`'s structured branch returns before the start-failure
test, as it does before the sentinel test (above). A pre-upgrade row whose
slot holds no record keeps the streak on the row, as on the measured base,
until a record is written for its slot.

Nor is the timer the only reader that decides an ending from the row. The
re-arm decides one too: whether a stopped row may be displaced turns on who
recorded its stop, and on the measured base that is read from the row. In
`_add_unserialized` in `src/kiro_crew/autonudge_service/mutations.py` and
`_add_monitor_locked` in `src/kiro_crew/autonudge_service/monitor_records.py`
a directive arm under `replace_existing=False` and `replace_stopped=True`
meets an inactive row and asks `_stopped_row_is_replaceable`, which splits
by who recorded the stop — a stop the system imposed is displaceable, a stop
a person or an app recorded is retained evidence — and reads that from the
row: for a structured record it hands `loop.monitor.outcome` and
`stopped_reason` to `retained_outcome_blocks_rearm` in
`src/kiro_crew/monitoring/models.py`, which admits `SUCCESS`, `BLOCKED`,
`BUDGET` and `TARGET_UNAVAILABLE` (`REARMABLE_MONITOR_OUTCOMES`) and fails
closed on `USER_STOP`, `SESSION_CLOSE`, a `BLOCKED` carrying
`MONITOR_STOP_INVALID_RECORD` and any outcome it does not know; for a legacy
row it admits the row's `stopped_reason` when it is in
`_REPLACEABLE_LOOP_STOP_REASONS` — `cycle_cap`, `runtime_budget`,
`APPROVAL_STALL_REASON`, `STRUCTURAL_TERMINAL_REASON`,
`SESSION_START_FAILURE_REASON`, `MONITOR_TERMINAL_REASON` and
`SENTINEL_DROPPED_REASON` — and fails closed on `MANUAL_STOP_REASON`, the
research tombstone `AUTONUDGE_STOP_REASON`, an empty reason and any reason
it does not know. Both are fields of the row in the agent-writable store,
and `_load` compares neither with anything: `monitor_state_from_dict` adopts
`outcome` and `stopped_reason` as found, `_is_torn_deactivation` reads the
row's empty `stopped_reason` as the mark of a write no stop path made and
resumes the row, and the MCP preflight `_retained_stop_refusal` in
`src/kiro_crew/mcp_tools/control.py`, which answers the arming turn in band
before the applier can, reads the same two values off the row through `GET
/api/autonudge/session-monitor` — `api_session_monitor_get` in
`src/kiro_crew/dashboard/handlers/autonudge.py`, serialising the live
`MonitorState` through `monitor_state_public_dict` — and hands them to the
same predicate. So a sandboxed rewrite of a retained reason into a
system-imposed one — `manual` to `cycle_cap`, `USER_STOP` to `BUDGET`,
`SESSION_CLOSE` to `SUCCESS`, the tombstone to `approval_stalled` — makes an
owner's pause, an owner's stop, a closed session's retention or an app's
tombstone read as automatically re-armable, and the next inheriting
`monitor_start`, or the structured arm `_add_monitor_locked` makes,
displaces it as a fresh commitment; a reason blanked beside a planted
deadline has `_load` resume the row outright; and the preflight, reading the
same rewritten row, tells the agent the arm will succeed. The row cannot
carry the distinction this decision needs even unforged: on the measured
base the agent's own `monitor_stop` and the owner's Clear stopped goal control —
`_stop_resolved_loop` in `src/kiro_crew/dashboard/session_directive_apply.py`
and `api_monitor_stop`, with the stop intent of `api_autonudge_delete`, all
through `authorize_and_stop_monitor` in `src/kiro_crew/autonudge_authz.py`
to `stop_monitor` — write the same `USER_STOP` and `MONITOR_STOP_USER` onto
the row, so the first rule below, which retains the agent's stop as an
inheriting one and the owner's as evidence, cannot be decided from the row
at all. Phase 1 therefore seals the ending beside the pair, the marker and
the sentinel path, as one more instance of the rule above: the record
holds, for every stop, who recorded it and how — the service's terminal
ending, the agent's directive or its sentinel, the owner's route, an app's
suspension or tombstone, the slot-close retirement, or the load-time repair
— beside
the reason the row shows, written in the record write Phase 1 already
places before the row at every ending (below), which is every one of the
writers named here, gateway writes all. `_stopped_row_is_replaceable`
decides from the record's ending, at both arms: the service's terminal
endings — the `_TERMINAL_BOUND_REASONS` `_timer` writes, but for the
fired turn's `structural_terminal`, which the nudge-fire path in
`src/kiro_crew/slack/gateway.py` writes, the settlement
`_monitor_tick_is_quiet` records as `MONITOR_TERMINAL_REASON`, `_load`'s
`SENTINEL_DROPPED_REASON` repair, and the `REARMABLE_MONITOR_OUTCOMES` the
service's probe, budget, dispatch-failure and evidence writes record — are
displaceable, as today; the agent's retained stop, by directive or
sentinel, is the inheriting re-arm the first rule admits; and the owner's
pause and stop, an app's suspension and its tombstone, the slot-close
retention, the quarantine
`quarantine_monitor_state` marks with `MONITOR_STOP_INVALID_RECORD`, and
an ending the record does not classify or the row alone carries, are
refused, fail-closed, exactly as `retained_outcome_blocks_rearm` and the
legacy set fail closed today. `retained_outcome_blocks_rearm` keeps its
shape and its fail-closed reading and is handed the record's ending, and
`_retained_stop_refusal` reads the same ending: the session-monitor
endpoint reports the ending the slot's record holds — the row it serialises
having been repaired to it (below) — so the answer the agent is given in
band cannot differ from the one the arm enforces, which is the property the
predicate's own account says the shared function exists for. The row's
`stopped_reason` and the structured `outcome` are mirrors of the record's
ending, repaired FROM it at load, in `_load` before `_is_torn_deactivation`
asks whether a reason is missing, and at every re-arm check before
`_stopped_row_is_replaceable` reads — a row that reads re-armable while the
record reads retained is repaired to retained and refused, the direction
that fails toward preserving evidence, and the disagreement is audited with
an SEL line naming the loop and the fields — never the record to the row,
and never reset: a blanked reason beside a retained ending is repaired to
the ending, not read as torn, so `_load` resumes nothing a stop path
stopped. What the seal does not cover is stated as Backward compatibility
states it for every field: a row whose slot holds no record — a
pre-upgrade row — keeps deciding from its own `stopped_reason` and
`outcome`, exactly as on the measured base, until an owner recommit or the
first retained stop after the upgrade writes a record for its slot, and a
stop with an empty or unknown reason stays refused as today whether the
record classifies it or not.

The record and the row are two files, written by one process but never in
one write, so every transition that touches both has an order, and a crash
or a failed write between the two must leave nothing that arms or renews. The
order is the record first, in both directions, and one service-level
transition helper performs it for every row transition — the record write,
where the transition moves the record, then the row mutation, and, when the
row write fails, a compensation specific to the transition rather than a
restore of the prior record (below) — so no site mutates a row while the
record still says otherwise, and no failed write moves the record back to a
commitment that has ended.
On the measured base the row is mutated at more sites than the two that
store a cap: `_add_unserialized` arms a row and displaces the slot's
existing one through `remove_sync`; `_update_unserialized` under
`AutoNudgeService.update` revises one; `_remove_unserialized` removes one for
every `remove`, whose callers include the owner's `DELETE /api/autonudge/{id}`
(`api_autonudge_delete`), `/goal clear` (`_handle_goal_command` in
`src/kiro_crew/dashboard/chat_runner.py`), the timer's sentinel branch, the
stop directives' legacy removal in `_stop_resolved_loop`, `remove_by_slot`,
`clear_terminal_monitor`, the Slack gateway's unroutable-loop and retirement
paths, and the Spec Builder's and auto-research's own slot cleanups;
`_add_monitor_locked` under
`AutoNudgeService.add_monitor` — reached from the same authorizer
`authorize_and_add_nudge` for a structured arm — writes its snapshot and
then displaces the slot's existing row, a legacy row included, through
`remove_sync`, applying `_stopped_row_is_replaceable` as the legacy add
does; and `rollback_monitor_replacement` writes the prior row back when that
arm's authorization cannot commit. Phase 1 routes each of them through the
helper; the delivered-cycle charge — `_timer`'s `loop.cycle_count += 1` at a
confirmed delivery — joins them in Phase 2, when the record begins to hold
the spend it charges (above). A fresh commitment at
`_add_unserialized` and an owner recommit at
`_update_unserialized` write the record before the row, which is the account
`_mint_loop_id` in `src/kiro_crew/autonudge_service/mutations.py` already gives
for the
self-arm entry the authorizer writes ahead of the store: a failed record
write denies the transition before the store is touched, so the arm or
recommit is refused to its caller, the row a replacing arm would have
displaced is never removed for nothing, and both files are as they were.
Every ending writes the record first too — the slot's entry kept with its
pair cleared, which is what ended means for a slot that once held a
commitment, as distinct from a slot whose entry is absent because none was
ever committed, and from Phase 2 its generation advanced in the same write —
then the row removed or deactivated: for the owner's `DELETE
/api/autonudge/{id}` and `/goal clear` through `_remove_unserialized`; for a
structured arm through `_add_monitor_locked` that displaces a row holding an
open commitment, whose record write precedes its snapshot and whose
`rollback_monitor_replacement` restores the prior record before the prior
row; and for the spent cycle cap, runtime budget and stall stop `_timer`
issues, and the timer's other terminal stops alike, so at no instant does an
emptied slot or a replaceable stopped row sit beside a record still open to
inherit from. A failed record write denies that transition as well: an owner
route reports the failure and leaves the row as it was for its caller to
retry, a structured arm is refused with the slot's row in place, and `_timer`
treats it as it treats a failed marker-consumption write (§3) — it delivers
no turn, retries the ending at its next pass, and the row cannot fire past a
spent bound while its record lags, because the bound is read before any
delivery. A row write that fails after the record write leaves on disk
exactly what a crash between the two leaves, read the same way at the next
load (below), and what the helper does in the running process follows the
direction the record moved, never a generic rollback. A fresh arm closes the
record it just wrote, in the same call, and is reported failed to its caller
— should that closing write fail too, load closes it — so the slot holds a
closed record, not an open one beside an empty slot, and a committed row the
arm displaced, which `_add_unserialized` already puts back in memory on a
failed write, is then a row beside a record that no longer agrees with it,
read fail-closed until its owner recommits. A recommit and an ending are not
undone: the recommitted pair, or the ending, is authoritative the moment its
record write lands, and the row the service holds — which
`_update_unserialized` and `_remove_unserialized` today restore in memory to
its pre-transition state on a failed write — is read against that record as
load would read it, so a row an ending has reached delivers no turn and is
refused as a record to inherit, and the row write is retried: the owner
route reports the failure for its caller to repeat, `_timer` retries the
deactivation at its next pass, and load repairs whatever remains. Restoring
the prior record instead would reopen a commitment the owner had ended —
`DELETE /api/autonudge/{id}` marks the record ended, the row write fails, and
the rollback puts the open commitment back for a restart to re-arm — and the
reconciliation below would then read a record the failure had forged. The
prior record is restored in exactly one place, `rollback_monitor_replacement`,
which undoes a structured displacement whose authorization did not commit:
that displacement is provisional, held in `_deferred_monitor_replacements`
until `commit_monitor_replacement` releases it, the prior commitment was
never ended by an owner or a service ending, and there the prior record is
restored before the prior row, as the ending was written before the
displacement. A retained stop — the stop directives' deactivation,
and the timer's on a fired sentinel, which Phase 1 turns from the removal
`_remove_unserialized` performs today into a deactivation (the first rule
below) — keeps the commitment open: the pair and the generation are
untouched and the record continues. It is still a record-first transition,
because the record is where the ending lives (§5, above). The ending — who
recorded the stop and how, which `authorize_and_stop_monitor`'s `source`
already tells apart as `dashboard` from the owner's routes and
`mcp-directive` from the agent's directive, and which the sentinel and the
service's endings carry in their reasons — is written into the record
before the row is deactivated, so a crash between the two leaves an ended
record beside a row still active, which the reconciliation below
deactivates, and never a stopped row beside a record still open, which a
re-arm could read as displaceable. An inheriting re-arm or replacement is
the same shape in the other direction: it writes the record's ending back
to open, carrying the remaining budget and the generation, before it
writes the active row, and `rollback_monitor_replacement` restores the
retained ending with the prior record when a provisional displacement does
not commit. The sentinel, like a spent bound, is read before any delivery.
The slot-close retirement
is a retained transition of the same kind, and the one whose row is removed
rather than deactivated. When a person dismisses a tab,
`_retire_slot_nudge_loop` in `src/kiro_crew/dashboard/chat_handlers.py`
calls `remove_by_slot`, which under the maintenance lock removes a legacy
row through `_remove_unserialized` — the removal is what keeps a restart
from rebuilding the dismissed session, by that function's own account — and
hands the retired row back to the close path; `close_slot` is reached from
the dashboard's `api_chat_slot_delete` and from `close_target` in
`src/kiro_crew/dashboard/session_control.py`, a peer-session close, so it is
not an owner route the agent cannot invoke and it ends nothing. Phase 1 has
the commitment continue across it: the record's ending is written as the
slot-close retention before the row is removed, the pair and the generation
untouched, and the close path may put the loop back. When the close then
fails to persist, `_restore_slot_nudge_loop` in the same module re-arms the
retired row through a plain `svc.add(...)` — no `authorize_and_add_nudge`,
no `initiator_slot_key`, no generation — landing in `_add_unserialized`, and
Phase 1 decides that arm against the retained record as an inheriting
re-arm, the `_add_unserialized` admission the first rule below names: the
restore hands the helper the row it was given, so the remainder is read from
that row as it is read from a retained stop's deactivated row, the restored
loop takes the record's committed classification and what the committed
pair has left — the request the restore already computes from the retired
row, capped there as every inheriting re-arm is — and no fresh commitment is
minted. It is a gateway act carrying no generation, and Phase 2's generation
comparison does not apply to it: a re-arm at a retained record is decided by
the slot's record alone. Read as an ending instead, the restored row would
inherit nothing under Phase 1 and silently revert to the stall stop, and
under Phase 2 its absent generation would refuse the arm outright, leaving
the reopened session live with nothing driving it — the harm the function's
own account says it exists to prevent. A retired row with no record — a
pre-upgrade loop — is restored with its live bounds and keeps the unbounded
reading every such row has until its owner recommits it; a retired row that
is inactive, a stalled one included, is not restored, as today. A close that
persists leaves the slot's record open beside no row, the state the
load-time reconciliation below closes — its pair cleared and, from Phase 2,
its generation advanced — and the helper closes it the same way at the first
arm that meets it in-process, before deciding that arm, so nothing inherits
from a closed session's record: under Phase 1 the arm then commits a fresh
pair as on a slot holding none, and under Phase 2 a proxy or automation the
closed session set in motion is refused on the generation the closure
advanced past while a person's turn commits fresh. The restore is the one
arm that does not meet that state as a stranger, since it arrives with the
retired row. What a crash between the two
writes leaves is
reconciled at load, in `_load` in `src/kiro_crew/autonudge.py`, the blocking
read `start` offloads before any loop is armed, with the record authoritative
in every case: a row whose slot holds no record, or disagrees with it in a
cap field, is
read unbounded and refused as a record to inherit, as above; a row whose
`approval_stalled` or `stop_sentinel_path` differs from its record's is
brought back to the record, and from Phase 2
a row behind its record's charged spend is brought up to it — the repairs
made in the row's direction, since the record decides the stall stop, the
sentinel stop, the cap and the budget
and the row's copies only mirror them (above); an inactive row
beside an ended record is the state a deactivating ending — a spent cap or
budget, the stall stop — leaves behind, and is held as today, displaceable or
not by the ending its record holds, its `stopped_reason` and `outcome`
repaired to that ending where they disagree (above); a row still active
while its
record is ended is the remnant of an ending whose row write never
landed — an owner removal, a structured displacement or a timer stop — and
is deactivated on load — never armed, its `stopped_reason` one
outside the re-armable set `_stopped_row_is_replaceable` reads and the
record's ending classified the same way, as the load-time repair's own
ending (above), so it is
cleared through the owner's routes as a retained stop is, since
the store cannot tell which ending removal or deactivation was meant and an
inactive row is the reading that arms nothing; and an open record whose slot
holds no row is the remnant of an add whose row write never landed, of a
row removed out of band from the agent-writable store, or of a slot close
that persisted (above), and is closed on load
— its pair cleared and, from Phase 2, its generation advanced — so a proxy
launched under it finds nothing to inherit and, once the generation exists, a
stale value to be refused on. A row that is active, or retained, with an open
record that agrees is armed or held exactly as today; a live `0` in a cap
field beside a positive committed value for it does not agree (above), so
such a row is read unbounded and refused as a record to inherit until its
owner recommits it. The reconciliation
writes the
record through the same gateway writer at the same publish point, and a
failed reconciliation write is a failed record write: the affected row stays
unarmed for this process, as a held-aside row does, and is reconciled again
at the next load.

A bound write is not the only self-session path that would erase the
commitment. On the measured base a stop issued from the loop's own session —
`autonudge_stop` and `monitor_stop` share `_stop_resolved_loop` in
`src/kiro_crew/dashboard/session_directive_apply.py` — removes an ordinary
dashboard or channel legacy loop's record outright; only a research-owned
slot's loop is deactivated with a retained tombstone, and only a structured
monitor retains a terminal record. In the same module `_monitor_start` arms
with `replace_existing=False` and `replace_stopped=True`, the one path allowed
to displace a retained stopped row, and `_stopped_row_is_replaceable` in
`src/kiro_crew/autonudge_service/model.py` lets it displace any system-imposed
stop —
`_REPLACEABLE_LOOP_STOP_REASONS`, which includes `approval_stalled` — while
failing closed, preserving the row, for a manual pause, a tombstone or a
reason it does not know. Read together, the turn that let the prompt lapse
can call `autonudge_stop` and then `monitor_start`: the record, the marker and
the committed classification vanish with the removal, a fresh finite loop arms
in their place, and the stall stop is taken by neither. Two guards already
stand in that path, for one class of turn: `apply_session_directive` in the
same module runs `_refuse_stale_wake_arm` ahead of every `_ARMING_DIRECTIVES`
member on a turn the fire path delivered — `_fire_dashboard_nudge` marks it
`producer_is_self_wake` and names the loop — and, the stop having removed the
row, `get_by_id` finds nothing and the arm is refused; and it runs
`_refuse_stale_wake_stop` ahead of every `_STOP_DIRECTIVES` member on such a
turn, refusing the stop when the loop that delivered the wake is no longer
the slot's or when the row is inactive and retained by a person — neither of
which holds for the live loop that let the prompt lapse, so its own stop
passes. The pair stays open to a turn the fire path did not deliver — an
app-sent or drained entry on the slot, which carries neither provenance mark,
so neither guard runs — and nothing refuses the removal of a live loop's own
row. The marker is not the
only thing the removal erases. A bounded loop that consumed its marker and
continued holds the same two tools in its remediation turn, and from such a
turn the same stop and re-arm would hand it a fresh 24 cycles and 14,400
seconds — `monitor_start`'s defaults — with the marker already consumed and
nothing left to refuse: a loop renewing its own budget, unattended, past the
ending its owner committed, at every turn it chooses. A rule that refused only
the marker-set case would close one variant and leave that one open.

Nor does the stop-and-re-arm pair exhaust the arms a session can direct at
its own slot, because `monitor_start` is not the only proxy. A loop's own
turn may call `workflow_run` with a script that calls
`ctx.nudge(idle_secs=..., message=..., max_cycles=1000)`. On the measured
base `_nudge_port` in
`src/kiro_crew/workflows/service.py` arms that loop on the workflow's
originating session — the session whose turn ran the workflow — by
delegating to the gateway-injected `_wf_nudge_authorizer` in
`src/kiro_crew/dashboard/server.py`, which calls `authorize_and_add_nudge` in
`src/kiro_crew/autonudge_authz.py` without naming `replace_existing`, so the
chokepoint's default of `True` reaches `AutoNudgeService.add`, and `add` with
`replace_existing=True` removes the slot's ACTIVE row and arms a fresh
`NudgeLoop` in its place, with a fresh `cycle_count` and `created_ts`. No
stop is issued, so the record the first rule below retains for a self-session
stop is never written, and the replacement starts its count at zero. This is
the one proxy that displaces an active loop — `_monitor_start` arms with
`replace_existing=False`, so at an active row it is refused — and it needs
neither a stop nor a marker to do it, which makes it the widest of the five
self-widening variants. It also arrives at the chokepoint with no turn
provenance: the workflow path passes no `initiator_slot_key`, which the
chokepoint's own account lists among the callers that have none. The signal
Phase 2 reads is therefore not who issued the arm but what the target slot
holds: a committed loop, whether an active row or a retained self-stopped
record.

That signal is necessary and not sufficient, because the proxy need not
arrive while the slot still holds the loop. A workflow run is not the turn
that launched it: `workflow_run` in `src/kiro_crew/mcp_tools/workflows.py`
returns a run id at once and the run keeps executing after the turn ends,
carrying its originating session on its record (`RunHandle.session_key` in
`src/kiro_crew/workflows/registry.py`, which the runner supplies to
`_nudge_port` at each `ctx.nudge`), and the run has no tie to the loop's
lifetime — `_nudge_port` itself records, when a run's teardown drain cancels
an arm still in flight, that “the run ended before arming completed — the
loop may still arm”. A bounded loop's turn can therefore launch a run whose
script calls `ctx.nudge(max_cycles=1000)` late, after the loop is gone. That
is the sixth self-widening variant, and the owner's own ending opens it.
`DELETE /api/autonudge/{id}` and `/goal clear` remove the row, and on the
measured base so does every explicit stop of an ordinary legacy loop: when a
configured STOP sentinel fires, `_timer` in
`src/kiro_crew/autonudge_service/firing.py`
removes the row — `await self.remove(loop.id, stop_reason="stop_sentinel")`,
a removal, not the deactivation a spent cap or the stall stop receives — and
`_stop_resolved_loop` removes it for a stop directive from any admitted turn
while the loop is current (a wake-delivered stop for a replaced or retained
loop is refused first by `_refuse_stale_wake_stop`), except
on a research-owned slot, whose loop it deactivates with a retained tombstone
(above). The run's later `ctx.nudge`
then finds
an empty slot, and the fresh-slot rule (c) below, read alone, arms a fresh
loop of a thousand cycles: the owner's explicit clear and the budget the
owner committed, bypassed by a proxy launched before the clear, with no bound
write, no self-session stop, no marker and no committed loop left on the slot
to decide against. A spent cap or budget opens the same door by another
latch: `_timer` deactivates the loop with `cycle_cap` or `runtime_budget`,
both in `_REPLACEABLE_LOOP_STOP_REASONS`, so the row it leaves is a
system-stopped, replaceable record, and the late `ctx.nudge` —
`replace_existing=True` displaces any existing row — replaces it with a
fresh count as it would fill an empty slot. A directive re-arm from a new
turn may displace that record today, and the difference is that the new turn
is visible on the tab and can be stopped again, while the run was authorized
under a commitment that has ended; what the slot holds at the arm cannot
tell the two apart. Phase 2 therefore keeps, per slot, a commitment
generation: a counter in the per-slot commitment record Phase 1 keeps in its
sealed leaf, keyed by slot rather than carried on
the row — `config_generation` on `NudgeLoop` is the module's existing per-row
fence, a removed row takes it along, and an empty slot has no row at all —
advanced by every owner reset or recommit (`DELETE /api/autonudge/{id}`, a
`PATCH /api/autonudge/{id}` recommit, `/goal`, `/goal clear`), by every fresh
commitment an arming surface makes on a slot holding none, and by every
ending of a commitment: the explicit user stop through the owner's route, a
spent cycle cap or runtime budget, and the timer's stall stop and its other
terminal stops. A stop the agent can trigger is not an ending — `autonudge_stop`
or `monitor_stop` from any turn, whoever started it, and a fired STOP
sentinel alike: the record it retains carries the
commitment forward — its remaining budget, its marker state and its committed
classification — and the same session may re-arm it inheriting that budget
under the rules below, so it advances nothing, exactly as a replacement or
re-arm that inherits the commitment advances nothing, because the commitment
continues. Were the retention to advance the generation, that inheriting
re-arm would find its own commitment stale and be refused, which is not the
rule. The slot-close retirement advances nothing either, for the same
reason: its record continues for the failed-persist restore to inherit
from, and the restore carries no generation to compare (above); a close
that persists leaves the record the load-time closure, or the helper at
the first arm that meets it, advances past. A workflow run carries the
generation its launching turn hands it —
captured from the slot at launch when that turn has authenticated-human
provenance, inherited from the turn's own otherwise, under the inheritance
rule below; `_nudge_port` passes it with the arm, `_wf_nudge_authorizer`
hands it to `authorize_and_add_nudge` beside `slot_key`, `message`,
`idle_secs` and `max_cycles`, and the
comparison is made where the slot's row is read — inside
`AutoNudgeService.add`, under the service `_lock`, in the critical section
where `_add_unserialized` already resolves the slot by `_find_by_slot`, the
same lock under which the module already compares a captured
`config_generation` before it acts — so no owner reset can land between the
check and the arm. An arm whose generation no longer matches is refused
whatever the slot now holds — empty, a replaceable system-stopped record, or
a loop the owner armed since — and the refusal is recorded into the run's
stream as the existing “ctx.nudge NOT armed” messages are. An arm whose
generation matches is decided by the slot-state rules that follow, which is
the only way it reaches them.

Nor is a workflow run the only thing a loop's turn sets in motion that
outlives the owner's ending, because not every later turn on the session is
a person's. A cycle may call `spawn_run`, `workflow_run`, or `cron_add` with
a job that posts `send_message(session="origin")`; when the subagent or run
completes, or the cron fires, an injector starts a turn on the same ordinary
slot — the subagent-completion injector in `src/kiro_crew/slack/gateway.py`,
the cron-to-origin injection whose target `_resolve_session_target` in
`src/kiro_crew/dashboard/handlers/messaging.py` resolves to the job's
originating slot, the task-runner plan and review turns in
`src/kiro_crew/dashboard/handlers/taskrunner.py`, the Issue Radar crew turn
in `src/kiro_crew/apps/builtins/issue_radar/backend/crew_runtime.py`, and
the loop's own delivered wake. On the measured base every one of them passes
`_run_chat` a `_directive_user_origin` of `False`. That flag is the runner's
provenance bit, which `src/kiro_crew/dashboard/chat_runner.py` describes as
authenticated-human provenance — the author typed into the session's own
surface — and which a queue drain grants a turn only when every entry it
consumed carries it. A turn so started holds `monitor_start` as a person's
would, and if the owner's clear has removed the loop meanwhile,
or its cap has left the replaceable record a spent budget leaves, the
directive finds a slot with no committed loop and the fresh-slot rule (c)
below, read alone, arms a fresh 24 cycles and 14,400 seconds: the door the
late `ctx.nudge` opens, by a turn instead of a run, and the generation the
run carries is in no turn's hands unless something puts it there. That is
the seventh self-widening variant — the loop's last cycle asking for a fresh
slot once its cap has ended the commitment, or a cron the loop created
re-arming the slot after the owner cleared it. The provenance bit alone does
not decide it, and the mode gate cannot. The session-directive consumer
already reads the bit — `apply_session_directive` in
`src/kiro_crew/dashboard/session_directive_apply.py` admits as the session's
own arm a human-started turn or the loop's own wake, which
`_fire_dashboard_nudge` marks `_directive_self_wake` — but only to decide the
crew/member rule: `_monitor_start` hands `authorize_and_add_nudge` an
`initiator_slot_key` for such a turn alone, and the authorizer refuses an
external arm only where the slot's mode is in `_EXTERNAL_ARM_REFUSED_MODES`
(`frozenset({"crew", "member"})` in `src/kiro_crew/autonudge_authz.py`), and
an ordinary dashboard slot is never in it, so no arm on such a slot is
refused for its provenance today. Extending the mode set would refuse a
person's own cron forever, and the loop's own wake is admitted as a self-arm
by design. The bit tells a person's turn from an automation's; only the
generation tells an automation dispatched under a commitment that has since
ended from a person's automation on a slot that never moved. Phase 2
therefore reserves the generation-free arm for a turn with authenticated
human provenance and has every automation-originated turn carry a generation
as a run does, under one inheritance rule: a turn or run that carries a
generation hands that same value to anything it schedules; a turn carrying
none schedules automation carrying none; and only a turn with
authenticated-human provenance captures the slot's current generation at a
scheduling call — `spawn_run`, `workflow_run` or `cron_add` — for the
completion or injection it produces. The nudge fire captures the slot's
generation at dispatch, beside the `config_generation` snapshot
`_fire_dashboard_nudge` already takes, because the wake is the commitment's
own turn, and the cycle it delivers hands that value on. Reading the slot's
current generation at every scheduling call, whoever makes it, would not do:
an automation turn whose own arm was just refused as stale could call
`spawn_run` or `cron_add` once more, and the second hop would capture the
generation the owner's ending advanced to, match it, and re-arm the cleared
slot — a chain two hops long, defeating the ending the first hop was refused
for. Under inheritance the stale value travels the whole chain, however many
hops it has, and only a person's turn can put a fresh one into it.

Where the carried generation is kept decides whether the chain can rewrite
it, and it cannot be kept on the automation's own record. A cron job's
record is a row of `crons.json`, and that store is agent-writable inside the
sandbox by design: `_CREW_SANDBOX_VISIBLE_LEAVES` in `src/kiro_crew/sandbox.py`
keeps `crons.json` read-write in every mode because `mcp_cron` builds a
`CronService` in-sandbox and rewrites the job store through it, and the
origin injection already trusts a field of that row — `_resolve_session_target`
in `src/kiro_crew/dashboard/handlers/messaging.py` resolves the slot a cron's
`send_message(session="origin")` lands on from `job.session_key`. The
commitment leaf, meanwhile, is child-readable on purpose (above), so a
sandboxed shell reads the slot's current generation from it. A generation
persisted on the cron record would therefore be a hint, not a commitment: a
stale automation holding a shell reads the current generation from the leaf,
rewrites the one its job captured, and its next origin injection presents a
generation the owner's ending never handed it — the forged-hint reading the
rejected alternative "Record the committed pair on the loop's row" gives the
loop store, one file over. The rule reads every other persisted automation
record the same way — a workflow run's file, one per run under
`workflows.dir` (`src/kiro_crew/workflows/store.py`), included. Phase 2
therefore keeps the
provenance of every scheduled automation where it keeps the commitment: the
gateway writes the triple — the automation's id, the origin slot it was
scheduled from and the generation it carries — into the sealed commitment
leaf, or a sibling leaf under the same three-layer disposition, at the
scheduling call, and an injection or completion turn is authorized only
against that sealed record, looked up by the automation's id, never from a
generation or a slot read off a cron, workflow or other automation record.
A record the agent can rewrite decides nothing: a cron row whose captured
generation or `session_key` disagrees with the sealed triple, or has no
triple at all, produces a turn whose `monitor_start` is refused, fail-closed,
as a turn carrying no generation is.

The triple binds which automation fires from which generation, not what it
says, and on the measured base what an automation says can be rewritten in
place with its id and origin kept. The `cron_update` tool — `name ==
"cron_update"` in `src/kiro_crew/mcp_cron.py`, gated by
`_check_cron_job_ownership` on the job's `session_key` alone and calling
`CronService.update_job`, whose `_update_job_locked` in
`src/kiro_crew/cron.py` rewrites the row's `message`, its `cron_expr` or
`every_secs`, its `agent_id`, `model`, `channel` and `thread_ts`, and its
`approval_mode`, `timeout` and `timeout_secs` among the rest — is
session-owned, not generation-owned, so a turn whose own arm was just refused
as stale can rewrite the message of a cron a person's turn created on the
same session after the owner's ending; that job's next injection matches its
sealed triple and carries the stale turn's payload, and the arm it asks for,
into a turn whose generation is current. `spawn_steer` — `api_spawn_steer` in
`src/kiro_crew/dashboard/handlers/messaging.py`, through `steer_run` and
`follow_up_run` — rewrites a running run's turn the same way, the run's id
kept; `spawn_continue` (`api_spawn_continue` in the same module) does not,
since it mints a new run on the conversation, a scheduling call the
inheritance rule above already decides; and a workflow run has no in-place
update on the measured base, `workflow_rerun_subtree` returning a new run,
though its file under `workflows.dir` is as writable as `crons.json`. Phase 2
therefore seals a definition digest beside the triple, in the same gateway
write, over the automation's DEFINITION — what it runs, how, when, where and
under what approval, and whether its owner has paused it — named by that
class under the rule of this section and not by a closed list. A cron row in
`crons.json` holds two classes of field, and the digest covers exactly one.
The definition is every field an owner, or an update admitted below, sets
and the fire path only reads. For a cron job the fire path is
`_cron_callback` under `_init_cron` in `src/kiro_crew/slack/gateway.py`, and
beside `message`, `schedule`, `agent_id`, `model`, `channel` and `thread_ts`
it reads `approval_mode` — `_cron_extra_env` turns `"auto"` into the
`KIROCREW_APPROVAL_MODE` the spawned process inherits, so every tool of the
run skips the ordinary approval prompt, while the always-enforced deny
checks and the permission floor in `src/kiro_crew/llm_helpers.py` still
refuse a denied call under `AUTO_APPROVE` — `command` and `script`, which
route the fire to
`run_command_sandboxed` or `run_script_sandboxed` in place of the model,
`timeout`, which bounds either (`job.timeout or 300`, `job.timeout or 30`),
`env`, `agent_sequence`, `execution_context`, `persistent_session` and
`minimal_context`, the `member_id` and `memory_store` the run's memory is
resolved by, and the `silent` and `hide_in_chat` that shape its delivery;
the due-scan that precedes it, `is_due` in
`src/kiro_crew/cron_service/schedule.py` — which `CronService` binds as
`_is_due` — reads
the `skip_dates` and the `timezone` `_job_tz` resolves beside the schedule
and, for an `every` schedule, the `created_ts` the interval is anchored on
before any run. All of them are `CronJob` fields in
`src/kiro_crew/cron_service/model.py`,
all rewritable in the sandbox-writable `crons.json`, and a digest over the
message and schedule alone would let a shell set `approval_mode` to
`"auto"`, or attach a `command`, to a job whose digest still verified, and
have altered, auto-approved work execute under a person's sealed
provenance. The owner's pause is definition too, because the fire decision
trusts it as the owner's word: `_on_timer` fires only a job whose `enabled`
is set and `_compute_next_run_ts_raw` returns no fire time for one whose is
not, and `_job_from_record` derives `enabled` at every load through
`_record_is_enabled` from two flags — `user_paused`, the owner's, and
`auto_paused`, the gateway's own brake. The digest covers the pause as
`_record_user_paused` derives it, `user_paused` with the legacy `enabled`
fallback for a record that lacks the key, so a shell that rewrites either
flag after an owner's pause, or deletes the key, presents a row whose pause
disagrees with the seal and fires nothing. The run state is the other class:
every field the gateway itself writes as a consequence of executing the job
— `last_result` and its stamps, `last_run_ts`, `last_status` and
`last_error`, `run_generation`, the posting and failure hashes and counters,
`acked_items`, the terminal fields a reap or a cancel merges, and
`auto_paused` — and it splits again by what the fire path does with it.
Display and timing state is read by nothing the run receives and trusted by
nothing the timer decides, and none of it is in the digest, because under
the rule none of it is an authority: the posting hashes decide only whether
the gateway repeats a delivery it already made, `last_status` and
`last_error` are shown, and
the fire time is re-derived at the decision from the sealed schedule
(below). Decision and fence state is the run state a gateway decision does
trust, and it is sealed as the loop's decision state is (§4): `auto_paused`
and `consecutive_failures`, the gateway's brake — `record_failure` counts
the failures and sets `auto_paused` at the threshold, and
`_record_is_enabled` reads `auto_paused` at every load to decide whether
the job may fire, so on the measured base a row rewritten to clear both
re-enables a job the service stood down until it fails that many times
again, its `approval_mode` intact — `run_generation`, the fence
`_merge_job_result` and `_merge_terminal_state_locked` compare against so
that an older run's record does not overwrite a newer one's, which on the
measured base a row rewritten to a lower generation lets happen, the stale
`last_result` then resealed by the merge and handed to the next run — and
the loop-stall settlement, the evidence `_pause_for_loop_stall` reads at
boot to decide whether the crash a dump attributes to a job was already
answered: on the measured base that evidence is the dump's name inside the
row's `last_error`, which `_job_from_record` adopts from the row as
written, so a row whose `last_error` a shell has set to the newest dump's
name reports the pause settled with `auto_paused` never set, and `start`
arms the timer on a job the breaker existed to stand down. Phase
2 writes each of these record-first at its own gateway transition —
the failure count and the brake at `record_failure` and `_pause_for_loop_stall`,
their release at `record_success` and at the owner's resume through
`_enable_job_locked`, the generation at
`RunClaims.next_generation` in `src/kiro_crew/cron_service/claims.py`, and the
settled dump's name
as its own sealed
field at `_pause_for_loop_stall`, `last_error` keeping only the text a
person reads — and reads each from the sealed record where the
decision is made, `_record_is_enabled` at load, the two merges at their
fence and the breaker at boot, so the row's copies are display and a
rewrite of them changes no
decision. Prompt-bearing run state — run state the fire path feeds into the
run's own input — is in the digest. On the measured base it is
`last_result`, which `build_cron_session_context` in
`src/kiro_crew/cron_service/identity.py`
prepends to a persistent-session job's prompt as the previous run's result,
with the `last_result_ts` and `last_result_stamp` that
`CronJob.set_run_result` writes beside it and `_merge_job_result` copies
with it, so the sealed value carries the run it came from; and
`acked_items`, which the single-agent branch of `_cron_callback` appends to
the prompt as what the person has already seen. The stateless branch of
`build_cron_session_context` hands the run the bare `message`, the
sequential branch `agent_sequence_dispatches` selects appends no acked
items, and a `command` or `script` job
runs its selector rather than the prompt, but the digest covers the fields
on every row rather than by the row's mode or kind, so the class turns on
no second field. It would be a mistake to leave `last_result` outside the
digest as the run's own output, on the ground that a rewrite of it writes
nothing the run could not have written itself: that names the wrong actor.
`crons.json` is one store, writable to every sandboxed turn
(`_CREW_SANDBOX_VISIBLE_LEAVES`), so the writer of a current cron's
`last_result` need not be that cron's run at all — a stale automation
dispatched under a commitment the owner has since ended, another job's run
or an ordinary agent shell writes it as easily — and `_job_from_record`
reloads the value on the store's next `_sync`, `build_cron_session_context`
prepends it, and `_cron_callback` dispatches the prompt under the target
cron's sealed provenance and its `approval_mode`, `_cron_extra_env` turning
`"auto"` into the `KIROCREW_APPROVAL_MODE` the run inherits. Text planted
there steers a turn whose generation is current — into a `monitor_start`
after the owner's ending, for one — which is the cross-generation
laundering the generation gate exists to stop, and no ownership, generation
or digest check covered it. The only writers of a cron's prompt-bearing
state that speak for that cron are the gateway's merge of that cron's own
run and the owner's own ack or unack of what it delivered — or a
participant's, on a row that names them (§5); a
write from any other path borrows the cron's provenance and approval mode,
which is why the field is sealed. Sealing it does not break the seal on the
first recurring run, as the rejected alternative "Seal every field the fire
path reads" did, because the writer that produces it reseals:
`_cron_callback` records the run's output through `CronJob.set_run_result`,
`_run_job_isolated` persists it through `CronService._merge_job_result` on a
worker thread (`asyncio.to_thread`), and that merge — a gateway write, made
under the store's `_file_lock` after its `_sync` and behind the
run-generation fence that already drops an older run's record — reseals the
digest over the value it applies in the same locked transition, so record
and row leave the merge agreeing, and the value it applies is the run's own
object's, the one verified at the fire or produced by the run, written over
whatever the row has come to hold meanwhile; the other writers of
`acked_items` are gateway writes too and reseal only what the owner's
class admits: `ack_job_async` behind `api_cron_ack` on the mixed-auth
prefix, which on the measured base writes the summary any caller posts,
is admitted by credential class like every mutating cron route (§5);
`unack_job_async` behind the dashboard's `api_notification_unack`
(`POST /api/notifications/unack`, registered in
`src/kiro_crew/dashboard/routes/system.py` off the cron prefix), which on
the measured base pops the job's last acked item for any validated
dashboard token — a shape a Slack-allow-listed non-owner holds too, since
`send_dashboard_link` mints `generate_token(user_id)` with `app == ""` —
reads the caller's class before it calls `unack_job_async`: the owner on
any row, a participant on a row that names them, no one else (§5);
and `_ack_job_locked` behind the Slack ack button (`_handle_cron_ack` in
`src/kiro_crew/slack/interactions.py`), which today writes for any actor
the dispatcher admits, reads the same class off the acting user — `is_owner`,
the check the allow-list approve and deny actions in the same module already
make, or the row's `created_by` naming that user.
At the next fire `_cron_callback` verifies the digest first —
before `build_cron_session_context` builds the prompt and before anything is
appended to it — and a mismatch fails closed, no run and an SEL audit line
naming the automation, as a rewritten definition field's does. The two are
told apart, because they recover differently. The digest keeps the
prompt-bearing state as its own component beside the definition's, so a
writer of one reseals one: the merge and the ack paths reseal the
prompt-bearing component over the value they write, the clearing refusal
below over the cleared one, and every other
resealing writer — an admitted update, pause or adoption — carries that
component forward as the record holds it and never adopts the row's value,
or an owner's `cron_update` made after a plant would seal the planted text
into the job. A definition mismatch then waits for the owner to re-issue the
change through a gateway path (below), since only the owner can say what
the job is; a prompt-bearing mismatch is prior output the gateway cannot
vouch for and can always drop, so at the refusal the gateway clears the
row's prompt-bearing fields and reseals the component over the cleared
value in the same locked transition, and the job runs at its next due with
no prior output, as a first run does — nothing planted reaches a prompt,
and nothing the owner scheduled stays stopped. The reads of run state the
fire decision trusts on the measured base are the anchor of an `every`
schedule: `is_due` and `_compute_next_run_ts_raw` take the next fire from
`last_run_ts`, or from `created_ts` before any run, plus `every_secs`, so a
row whose `last_run_ts` is rewound fires early, and `is_due`'s same-minute
guard for a cron expression reads it too; and `_next_wake_secs`, which
`_arm_timer` calls through `_effective_delay` to set the timer's delay,
anchors the same `every` computation on the same field, so a decision that
anchored only `_is_due` on the record while the timer still read the row
would, on a rewound row, arm at zero delay while finding nothing due, and
`_tick` — which re-arms after every `_on_timer` — would spin the gateway
until the real due time. Phase 2 strikes all three reads from the
decision rather than sealing the field: the gateway stamps the fire time on
the sealed record at each fire it makes — a state field of the record,
written at the fire where it already verifies the record, and no reseal of
the definition — and the due-check and the timer's delay anchor an `every`
job and the same-minute
guard on that stamp, or on the sealed `created_ts` before the first fire, so
`last_run_ts` on the row is display and a rewound row neither fires early
nor spins the timer, whose re-arms stay at the poll cadence
`_effective_delay` already caps them to. The shape of the digest is the one
`compute_secret_env_pin` in `src/kiro_crew/cron_script.py` already gives a
vault grant — pinned over the script spec, the message, the delivery
fingerprint and the file's bytes so that approved code cannot be re-aimed —
carried to the whole definition. Phase 2 gates every in-place update of the
definition on the generation — the `cron_update` tool and, the pause being
definition, `cron_pause` and `cron_resume`, which reach
`CronService.enable_job` through `_call_tool_inner` in
`src/kiro_crew/mcp_cron.py`: the admission reads the class of the caller
off its credential, and the classes are these. The owner is a dashboard
token `is_owner_dashboard_request` accepts — `request["user"]` the
configured `owner_id` and `request["app"]` the present empty string — or,
on a channel, a caller the channel's owner predicate names (`is_owner` in
`src/kiro_crew/slack/handler.py`; `is_owner` on the Telegram
`session_resume` state); the owner is admitted to every transition of every
row, and the write is resealed. A participant is an allow-listed person who
is not the owner: the channels mint that person the same `app == ""`
dashboard token through `send_dashboard_link` in
`src/kiro_crew/slack/allowlist.py` and the Telegram dispatch
(`generate_token(user_id)`), and a channel `cron` command from them reaches
`cron_command_reply` in `src/kiro_crew/messaging/commands.py`, which today
lists every job and calls `remove_job_async`, `cron_remove_all_reply` and
`enable_job_async` with only `caller` attribution; a participant's act is a
person's, not an agent's, and is admitted to the rows that name that person
— `created_by`, or a `session_key` of their own conversation — for pause,
resume, ack, unack and removal, and to listing those rows, and is refused
and audited on any other row and on the owner-wide acts, `remove-all` and
`DELETE /api/crons`. An app is an app token (`app` non-empty, held to its
declared paths by `_enforce_app_scope`), an in-process `CronSDK` call, or an
app's own route — Ops Mission Control's `POST /rotation/arm`, whose
`apply_tiers` in
`src/kiro_crew/apps/builtins/ops_mission_control/backend/rotation.py` arms
and disarms that app's own crons through `enable_job_async` — admitted to
the rows the app owns, sealed as app-owned, and refused on any other; the
app's identity is its provenance, so it carries no generation and needs
none. The gateway's own deterministic writers — the run merge, the
watchdogs, `_release_children_of_removed` — reseal by the class of field
they write (Phase 2). The agent is a turn carrying the automation's
current sealed generation, admitted and resealed on every path that
reaches the store through the gateway (the paths, and which writers of the
store reseal, are named in Phase 2); a turn whose generation is stale, or
carries none, is refused and the refusal reported into the turn, so a turn
whose arm was refused cannot resume the job its owner paused; and a call
carrying only the internal secret is the agent's class with no generation
at all. The admission
reads these classes off the credential, never off the route. `/api/crons` is a
prefix in `_MIXED_INTERNAL_API_PATHS` in
`src/kiro_crew/dashboard/server.py`, so `token_auth_middleware` in
`src/kiro_crew/dashboard/token_auth.py` admits every route under it from a
validated dashboard token — the owner's or a participant's — and from a
loopback call presenting `X-Internal-Secret`. The existing-row mutators do ask
who called for the first two classes: `api_cron_update` (`PATCH
/api/crons/{id}`), `api_cron_enable` (`POST /api/crons/{id}/enable`),
`api_cron_ack` (`POST /api/crons/{id}/ack`, whose `ack_job_async` writes
`acked_items`, the lines `_cron_callback` appends to the next prompt),
`api_cron_run` (`POST /api/crons/{id}/run`), `api_cron_cancel` (`POST
/api/crons/{id}/cancel`), `api_cron_delete` and `api_cron_batch_delete`
(`DELETE /api/crons/{id}`, `DELETE /api/crons`). Each calls
`require_owner_dashboard_request`, defined in
`src/kiro_crew/dashboard/handlers/_shared.py`, for a dashboard caller and
`_refuse_foreign_app_job` in
`src/kiro_crew/dashboard/handlers/cron.py` for an app caller, so a dashboard
participant is refused and an app is confined to rows whose host-written
`created_by` is its own `app:<name>` stamp. `api_cron_to_chat` (`POST
/api/crons/{id}/to-chat`) does
not mutate the row and carries neither gate. A call carrying only the internal
secret has no `app` claim and skips both gates on every route in this list;
`api_crons_create` likewise owner-gates its dashboard caller but lets the
secret-only class skip that conditional. `api_cron_secret_grant` alone refuses
`request["internal_auth"]` outright before its owner gate, the shape the rest
adopt; and `api_cron_tools` is the attested-session path, not a secret-only
one. The internal
secret is not owner provenance, and no check at these routes can make it
one: `read_local_secret` in `src/kiro_crew/config/loader.py`, which the
CLI's `_internal_secret` in `src/kiro_crew/cli_commands.py` calls, resolves
it from `run/gateway-<port>.secret` and then `.local_secret`, leaves that
`_CREW_SANDBOX_VISIBLE_LEAVES` in `src/kiro_crew/sandbox.py` keeps readable
inside the sandbox on purpose — the in-sandbox MCP servers authenticate
back to the dashboard with it — and the middleware says what the secret
proves when it marks the grant `request["internal_auth"]`: that the call
came from inside, not who made it. A call carrying only that secret is
therefore an agent's write whatever process spelled it, the CLI in an
agent's shell included, and is admitted only under a current sealed
generation — which a CLI process carries none of — and otherwise refused
and audited before the handler mutates anything, so a refused `ack` writes
no `acked_items` and reseals nothing, a refused `run` fires nothing, and a
refused `delete` removes nothing. A person's provenance at these routes is
the one class the sandbox cannot read and only a person holds — the owner told
from a participant by `is_owner_dashboard_request` — the signed dashboard
access token that `_extract_and_validate_token` validates, whose branch alone
publishes the `request["user"]` and `request["app"] == ""` that
`is_owner_dashboard_request` in
`src/kiro_crew/dashboard/handlers/source_providers.py` requires. Every
existing-row mutator named above and `api_crons_create` consults that predicate
through `require_owner_dashboard_request` for a dashboard caller. The token is
signed with
`token_signing.key`, which `_CREW_HIDDEN_LEAVES` masks in every sandbox
mode because its bytes forge any session cookie, and minted at
`api_token_local` in `src/kiro_crew/dashboard/handlers/core.py` for the
callers `local_owner_bootstrap_allowed` in
`src/kiro_crew/member_memory_auth.py` admits — two on the measured base,
not one: a host process outside the sandbox, `_verified_host_process`, and
a live app backend this gateway launched, `_gateway_spawned_app_backend`,
third-party code that `wrap_argv` sandboxes and that reads `.local_secret`
because `_CREW_SANDBOX_VISIBLE_LEAVES` exposes it — and minted the same
for both, `generate_token(owner_id)` with no claim naming the backend,
which `token_auth_middleware` then publishes as the owner's
`request["user"]` and `request["app"] == ""`. A sandboxed process that is
neither caller is refused the mint; an app backend is not, and today holds
the owner's class. Phase 2 marks the backend's token: `api_token_local`
mints it with a positive claim naming the backend's app, in the `extra`
claims `generate_token` already carries for `embed_parent_port`,
`token_auth_middleware` publishes that claim as a non-empty
`request["app"]`, so `is_owner_dashboard_request` refuses it,
`_enforce_app_scope` holds it to the app's declared paths and the cron
admissions read it as the app class, and the owner's class is the token
minted on the host-process branch alone. The
predicate refuses an absent `request["app"]` claim, and the internal-secret
branch never publishes one. On the measured base every dashboard owner gate is
conditional on the present empty claim, while `_refuse_foreign_app_job` returns
without an app name; a secret-only request therefore skips both. The same
request skips `api_crons_create`'s conditional and persists the row today,
`approval_mode` `"auto"` included. Phase 2 keeps
`require_owner_dashboard_request` and `_refuse_foreign_app_job`, adds the
internal-secret generation and owner check before mutation, and admits a
participant only through the row-scoped extension §5 defines. The owner's token
is admitted and the record written before the row, an app identity is sealed as
app-owned, and the secret-only class is refused and audited. On a host whose
sandbox is off
the key is the write-only tier's standing residual (Security
considerations), which this rule neither widens nor narrows. Where the
automation fires and where its turn is injected, the gateway verifies triple
and digest against the live record, and a mismatch fails closed — no
injection, and an SEL audit line naming the automation — so a rewrite of a
sealed field that reached the store by any path the gateway did not reseal —
a `CronService` write in-sandbox that bypassed the gateway's admission, a
shell's edit of `crons.json` or of a run's file — injects nothing, while the
display and timing state the gateway writes to the same row after every run
leaves the digest verifying and the prompt-bearing state it writes there is
resealed as it is written. A row with no sealed record fails closed the same
way, and is never read as one that needs no seal: `crons.json` is writable
to every sandboxed turn, `_load` in `src/kiro_crew/cron.py` and
`_job_from_record` in `src/kiro_crew/cron_service/store.py` accept any
well-formed row, `_on_timer` dispatches
every enabled row that is due, `vet_job_at_fire_time` in
`src/kiro_crew/mcp_cron.py` re-checks the owning app's state, the cron
capability and the command or script policy but neither provenance nor
`approval_mode`, and `_cron_callback` turns a row's `"auto"` into
`ToolApprovalPolicy.AUTO_APPROVE` and the `KIROCREW_APPROVAL_MODE` that
`_cron_extra_env` sets — so a whole row a sandboxed shell writes would fire
as a new auto-approved job under no owner's act, a fresh commitment the
scheduling call's generation gate never saw, and reading it as needing no
digest was that gate left open. Phase 2 therefore seals every extant row
once, at its cutover, as the gateway finds it — the record marked legacy,
so an audit tells a row that predates the seal from one injected after it
— and from then on every creation path that runs in the gateway process
writes the record before the row: `api_crons_create` (`POST /api/crons`),
`cron_add` through `_call_tool`'s post to `POST /api/crons/tools` and
`api_cron_tools`, and `CronSDK.add_job` and `add_job_async` in
`src/kiro_crew/apps/cron_sdk.py`. A row the gateway did not seal — a
shell's write, or the CLI's `add` and `_call_tool`'s `_call_tool_locally`
fallback when no gateway answers, both of which write the host store from
their own process — fires nothing, and the refusal is audited with an SEL
line naming the row, until the job is re-created through a gateway path.
The loop-arming half of the old reading stays true of a legacy record
sealed at cutover: its triple carries no generation, so the turn it
produces arms nothing (above).

The automation the rule was written for is the loop itself, and its
definition lives in the store the rule already calls agent-writable. On the
measured base `AutoNudgeService._load` in `src/kiro_crew/autonudge.py`
builds a `NudgeLoop` from every well-formed row of `autonudge.json` —
normalising a non-boolean `gate` or `self_armed` to its safe value,
bounding the judge fields, and otherwise taking the row as found — and
builds the nested `MonitorState` from the row's `monitor` key through
`monitor_state_from_dict` in `src/kiro_crew/monitoring/models.py`; `_timer`
sleeps the row's `idle_secs` and, for a structured row, hands the loop to
`MonitorController.tick`; `_fire_dashboard_nudge` in
`src/kiro_crew/slack/gateway.py` snapshots `loop.message`,
`loop.stop_sentinel_path` and `loop.config_generation` together, composes
the turn from the message through `compose_nudge_body`, and delivers it
into the slot `loop.slot_key` names, as its channel siblings
`_fire_slack_nudge` and `_fire_discord_nudge` deliver the same message into
the channel session that key names; and
`MonitorController.tick` in `src/kiro_crew/monitoring/controller.py`
selects the provider by `state.kind`, probes `state.target`, measures the
budgets, and through `_dispatch_claimed` builds the wake with
`format_monitor_wake` from `state.kind`, `state.target`, `state.objective`
and `state.wake_instructions`, with the `last_observation`,
`last_wake_fingerprint` and `last_wake_reason_code` the probe wrote. Every
one of those fields is read off the row and taken as the owner's word —
deciding whether, when and where the automation fires, or handed to the
turn as its input — so under the rule each is sealed or is agent-writable,
and none was sealed: the seal so far covered the pair, the marker, the
sentinel path, the spend, the generation and the triple, and the digest
covered a cron's and a run's definition. A sandboxed shell, or a stale
automation of the loop's, that rewrites `message` on a live row therefore
has the next wake deliver its text under the owner's provenance and approval
mode; one that rewrites `slot_key` has it delivered into another session;
one that rewrites `target`, `objective` or `wake_instructions` has the
monitor probe another subject and wake the agent with instructions the
owner never gave — the laundering the cron digest exists to stop, on the
automation whose record this document is about. Phase 2 therefore extends
the definition digest to the loop's own definition, by the class this
section names and not a closed list. For a legacy loop the definition is
every owner-authored `NudgeLoop` field the timer or the fire path reads to
route, schedule, gate or construct the turn: `id` and `slot_key`, which
bind the row to the slot the record is keyed by; `message`; `idle_secs`,
the cadence `_timer` sleeps; `gate` and `judge`, which decide whether a
probe runs and under what brief a reading is screened; and `self_armed`,
the one bit that relaxes the crew/member external-arm refusal at
`_fire_dashboard_nudge`, whose row half this seals — the trust-side record
`src/kiro_crew/autonudge_selfarm.py` keeps beside it has the exposure the
rejected alternative "Record the commitment under `trust/`" names, which
this neither widens nor narrows — beside the `stop_sentinel_path` and the
pair Phase 1 seals. For a structured monitor it is the `MonitorState`
definition: `kind`, `target`, `objective`, `cadence_secs`,
`wake_instructions`, `creation_surface` — which `MonitorController.tick`
reads to decide whether a `DASHBOARD` monitor's probe runs under the
owner's provider credentials, through the check it holds as
`_owner_credentials_authorized`, and which `_add_monitor_locked` stamps and
`update_monitor` rewrites — and of the `budgets` the `max_agent_turns`,
`max_tokens` and `max_provider_errors` beyond the committed pair, the pair
itself read as Phase 1 reads it. Of the monitor's run state the digest
covers, as its own component, the fields `_dispatch_claimed` hands the wake
as input — `last_observation`, `last_wake_fingerprint` and
`last_wake_reason_code` — whose writers are the service's own probe and
claim writes, `apply_monitor_probe` among them, gateway writes that reseal
the component as they write it, as `_merge_job_result` reseals
`last_result`; a mismatch on that component clears the baseline, as
`update_monitor` clears it when the target changes, and reseals over the
cleared value, so the next tick probes afresh. The spend is sealed as the
spend (above). The rest of the run state splits by the same test — decided
by, or only shown — and its authority half is the DECISION state, the
fields the engine, the controller or the gate decide by on the measured
base, sealed as its own component. For a structured monitor they are the
dedupe and window state `_decide_effect` and `_coalesce_actionable` in
`src/kiro_crew/monitoring/decision.py` read — `last_fingerprint`,
`coalesce_windows` and `coalesce_alerted`, a rewrite of which repeats or
suppresses a wake the engine decided otherwise; the stall streak
`_stall_tripped` in the same module evaluates and `_fold_stall_streak`
decides the stall stop by — `stall_digest`, `stall_streak` and
`stall_started_at`, whose clearing defeats an ending the generation
advances on (Phase 2); the provider-error streak
`_provider_error_decision` decides the error stop by —
`consecutive_provider_errors`, a streak and not a charge, since a clean
probe and an admitted retarget zero it; the claim `MonitorController.tick`
in `src/kiro_crew/monitoring/controller.py` routes a tick by and
`record_monitor_turn_completion` charges a turn against —
`wake_in_flight`, `wake_delivery` and `completion_evidence_deadline`, where
a cleared claim lets the completed turn go uncharged, since
`record_monitor_turn_completion` refuses a completion whose claim is not
in flight, and lets the next tick probe afresh and, with
`coalesce_alerted` cleared beside it, wake again while the first turn
still runs, and a cleared deadline keeps a wake whose completion never
reports from the fail-closed stop
`record_monitor_completion_evidence_unavailable` applies; and the
`config_generation` fence `apply_monitor_probe` discards a stale probe
by, which an admitted retarget advances. For a gated legacy loop they are
the gate's own state: on the loop's `monitor` record the `quiet_streak`
the streak floor in `_monitor_tick_is_quiet` delivers on, the
`floor_fire_pending` that outlives that floor decision — set durably on the
tick that decides the delivery, in the same snapshot as the reset of the
streak that earned it, served ahead of the follow-up allowance and fired
without observing, and cleared only where the delivery is confirmed — the
`followup_ticks` that bypass the gate after a wake, the `poll_in_flight`
that fires instead of trusting a quiet verdict when a poll was
interrupted, and the `terminal_pending` that defers a channel loop's
settlement; on the `NudgeLoop` the `judge_pr_seen` baseline the unchanged
check suppresses by, read at `_publish_pr_observation` and advanced
through `_commit_judge_pr_seen` under `_monitor_tick_is_quiet` itself, once
a verdict exists; and on the `NudgeLoop` the judge's state
`_judge_tick_is_quiet` reads and advances — `judge_cursors`, which
rows it reads; `judge_quiet_streak`,
measured against `_judge_quiet_streak_floor`; `judge_wake_pending`, the
owed turn that fires before any judging; and `judge_last_verdict`,
`judge_recent_verdicts` and `last_fire_ts`, handed to the judge as its own
state — a rewrite of any of which forces or suppresses a delivery the gate
decided otherwise — and, on the row itself, the legacy `config_generation`,
the fence `_fire_dashboard_nudge` snapshots at fire time and
`_update_unserialized` compares a fired turn's captured generation against
before it honours that turn's structural stop, advanced by the same
function on an admitted retarget and on a revival: a copy raised out of
band refuses a stop the turn was owed as stale, the twin of the structured
fence above. The gate is named in the rule above beside the timer and
the fire path for exactly this: it decides whether a tick runs, so its
state is authority by the rule's own words, and none of it can be
re-derived from what is sealed. Every writer of the decision state is the
service's own: for a structured monitor the probe, claim, dispatch and
completion writes — `apply_monitor_probe`, `mark_monitor_action_in_flight`,
`record_monitor_turn_completion`, `record_monitor_dispatch_busy`,
`record_monitor_dispatched`, `record_monitor_dispatch_failure`,
`record_monitor_completion_evidence_unavailable` and
`stop_monitor_if_budget_exhausted` — each of which stages a whole
replacement and persists it through `_persist_staged_monitor_locked`, and
the retarget reset in `update_monitor`, which persists the same way; for a
gated legacy loop `_monitor_tick_is_quiet`, `_judge_tick_is_quiet` with
the verdict and delivery bookkeeping around it, `_run_fire_cycle`'s
post-wake writes and, in `_update_unserialized`, the judge reset and the
`config_generation` advance on a retarget or a revival, which
persist through `_persist_locked`, `_persist_soon` and
`_persist_judge_state`. From Phase 2 each reseals the component
record-first as it writes it, in the shape `_merge_job_result` reseals
`last_result`: `_persist_staged_monitor_locked` writes the record before
the snapshot it then applies, and the legacy writers go through the
transition helper as the delivered-cycle charge does (above). The decision
state is a mirror and not a claim, like the spend, so a row that disagrees
with the record at load or at a tick is repaired FROM the record and the
disagreement audited — never reset to a fresh value, since a cleared stall
streak, quiet streak or owed-wake bit fails toward not stopping and not
delivering, the direction each of those fields exists to close — and
`_decide_effect`, `_fold_stall_streak`, `_coalesce_actionable`,
`_provider_error_decision`, `MonitorController.tick`,
`record_monitor_turn_completion`, `_monitor_tick_is_quiet`,
`_judge_tick_is_quiet` and the fence comparison in `_update_unserialized`
decide by the record's component, or by a state the
gateway has just verified against it, whatever the row shows. What stays
outside is what the rule leaves outside, named rather than summarised:
`banner`, which `_fire_dashboard_nudge` shows in the transcript row and
never hands the turn; the metering the service counts and nothing reads to
decide — `probe_count`, `wake_count`, `quiet_ticks`, `wakes`,
`gate_fallbacks` and `floor_ticks`; the inspection stamps and labels the
dashboard shows — `last_probe_at`, `last_observed_at`, `last_completed_at`,
`last_completion_fingerprint`, `last_completion_disposition`,
`last_observation_status`, `last_observation_reason_code`,
`last_provider_error`, `last_decision` and `token_usage_known` — of which
`last_decision` is named because `MonitorState`'s own account rejects
seeding the stall streak from it, so no decision reads it; the timing pair
`next_due_ts` and its mirror `next_probe_at`, struck from the decision
rather than sealed: `_arm_from_deadline` already caps the countdown at the
sealed `idle_secs`, so a rewritten deadline moves the next tick by at most
one cadence, and Phase 2 puts the same cap on the busy retry's `now <
state.next_probe_at` read in `MonitorController.tick`, the one firing
decision that trusts the raw value; the terminal fields — `outcome`,
`stopped_reason`, `stopped_at` and `user_stop_reason` — which the
reconciliation of the two files reads as §5 gives it above, the record's
ending deciding what a row still active beside it arms, and the row's
ending a mirror repaired from the record at the re-arm (above); the
notification stamps — `terminal_notification_delivered` on the structured
record and `terminal_notification_outcome` and
`terminal_notification_stopped_at` on the legacy row — which
`terminal_notification_delivery_matches` reads to decide whether a
terminal notice was already delivered, so a rewrite costs at most one
repeated or one withheld notice and never a turn, a stop or an arm;
`version`, read at `_load` and at `_add_monitor_locked` ahead of the
evidence check, where a value that is not `MONITOR_STATE_VERSION` is a
newer gateway's record and is refused or left uninterpreted, so a rewrite
fails toward refusal and never toward a widening; and `extra_fields` and
`_raw_payload`, the round-trip carriage of fields a newer version owns,
which nothing on the measured base reads to decide. The legacy row's
`config_generation` is not outside: it is the fence named with the
decision state above. `goal_token`
is neither: the field's own account is that it is re-minted on every load
and never persisted, so no store holds a value to seal. The digest is
recorded record-first, in the record write Phase 1 already places before
the row's, at every transition that writes a definition field: the arm at
`_add_unserialized`, the structured arm at `_add_monitor_locked`, and an
admitted update — `AutoNudgeService.update`, which writes `message`,
`idle_secs` and `judge` beside the bounds, and `update_monitor`, which
writes `target`, `objective`, `cadence_secs`, `creation_surface`, the
`budgets` and `wake_instructions`; `rollback_monitor_replacement` and
`rollback_monitor_update` restore the prior digest with the prior record.
An update is admitted on the terms the cron digest rule admits one: from
the owner's routes under the owner credential class — `api_autonudge_update`
after `_require_monitor_owner`, and `api_monitor_update` in the same module
after the same check — from a turn with authenticated-human provenance, or
from a turn carrying the slot's current sealed generation, which the loop's
own delivered wake carries from Phase 2 and which lets `monitor_update` from
that wake revise the instruction it runs under as it does today, its bound
fields still decided by the cap rule above; a turn whose generation is
stale, or carries none without human provenance, is refused and the
refusal reported into the turn. No admitted update writes `id`, `slot_key`,
`gate` or `self_armed` — `update` and `update_monitor` take none of them —
so a row whose value for any of the four disagrees with the record was
rewritten out of band. Before every load, tick and fire — `_load` before
it arms a row, `_timer` before it sleeps a row's `idle_secs` and before it
hands a structured row to `MonitorController.tick`, `_fire_dashboard_nudge`
before `compose_nudge_body` reads the message and before the slot is
resolved, and `_dispatch_claimed` before `format_monitor_wake` reads the
state — the gateway verifies the row's definition against the sealed
digest, and a mismatch is refused: no timer armed, no turn composed, no
probe made, the row held as the store shows it, and an SEL audit line
naming the loop and the fields that disagree, exactly as a rewritten cron
field is refused; an admitted update reseals and re-arms the timer it
persists under, as `update` and `update_monitor` re-arm it today, and the
owner's `DELETE /api/autonudge/{id}` clears the slot. The bounds keep Phase
1's reading, since the pair is enforced from the record and a live cap can
only tighten; a definition field has no sealed value to run in its place,
so its mismatch refuses the fire. The cutover seals every extant row once,
at the first load the upgraded gateway makes, as found — a row whose slot
holds a commitment gaining the definition component beside its pair, a
pre-upgrade row whose slot holds none gaining an entry marked legacy that
holds the definition component and no pair, the row still read unbounded
until its owner recommits it (Backward compatibility); wherever this
document reads a slot as holding no record — the pre-upgrade row, the
watchdog's fresh arm on an uncommitted crew slot — an entry holding a
definition component and no pair reads the same way, committing nothing.
From then on a row that reaches `autonudge.json` with no entry at all — a
shell's write of a whole row, or every row on a host whose leaf the alias
check replaced (above) — arms nothing at load and is audited, as a cron row
with no record fires nothing, until its owner arms it again through a
surface of §6; the first two layers keep a sandboxed process from writing
the entry beside it. §4's account of the structured monitor is read
against this: its typed disposition behaviour is unchanged, and the
definition it acts on is sealed like every other automation definition.

The rules below decide the stop as they decide the write — Phase 1 — and
decide every arm or replacement the loop's session directs at its own slot,
by whatever proxy — a workflow's `ctx.nudge` on its originating session
included — and by whatever turn automation it scheduled starts there,
against the committed record and the generation the proxy was launched or
the automation scheduled under, not only the one made while the marker is
set — Phase 1 for the `monitor_start` re-arm at a retained record, Phase 2
for every other proxy, for every automation-originated turn and for the
generation itself:

- A stop the agent can trigger — `autonudge_stop` or `monitor_stop`, applied
  by `apply_session_directive`, from any turn on the session, whoever started
  it: the loop's own delivered wake, a turn automation started on the slot,
  or a turn the person typed — deactivates the loop and retains its record —
  its remaining cycle and runtime budget, its marker state and its committed
  classification — instead of removing it. So does a fired STOP sentinel.
  `_timer` in `src/kiro_crew/autonudge_service/firing.py` tests
  `Path(loop.stop_sentinel_path).exists()` before every fire and on the
  measured base removes the row, and the file it tests is one the agent is
  told the path of and can write: the generated `/goal` instruction
  (`_handle_goal_command` in `src/kiro_crew/dashboard/chat_runner.py`) names
  the path in its first step and tells the agent to call `autonudge_stop`
  when the file exists, `render_nudge_message` in
  `src/kiro_crew/dashboard/handlers/autonudge.py` substitutes it for
  `{{STOP_FILE}}` in every message that carries the template, and
  `authorize_and_add_nudge` defaults it through `resolve_stop_sentinel` in
  `src/kiro_crew/autonudge_authz.py` to `.stop-<slot>` under
  `workspace_dir_for(...)`, the agent's own working tree. Phase 1 has the
  timer deactivate and retain on the sentinel as the stop wrappers do on the
  directive. Its stop reason lies outside the system-imposed re-armable set,
  so the fail-closed rule already in `_stopped_row_is_replaceable` refuses to
  displace it as a fresh commitment — and, on the measured base, refuses it
  outright: `_add_unserialized` in
  `src/kiro_crew/autonudge_service/mutations.py`,
  under the
  `replace_existing=False` and `replace_stopped=True` that `_monitor_start`
  arms with, raises `MonitorUpdateConflict` for a stopped row whose reason
  `_stopped_row_is_replaceable` does not admit, naming the owner's clear as
  the remedy, so the inheriting re-arm the next rule requires would be
  refused there as a manual pause is — and refused earlier still on the
  loop's own wake, where `_refuse_stale_wake_arm` in
  `src/kiro_crew/dashboard/session_directive_apply.py` fails closed on the
  same predicate before the authorizer runs. Phase 1 therefore changes
  `_add_unserialized`, inside the transition helper, and that guard's
  stopped-row branch, to admit a re-arm at a
  retained record as an inheriting one — the loop it produces takes the
  record's committed classification and remaining budget, and the arm is
  refused only while the record carries the marker or its budget is
  exhausted — while `_stopped_row_is_replaceable` stays fail-closed for
  every other stopped reason: a manual pause, a research tombstone, a
  structured record whose retained outcome the record classifies as the
  owner's stop or a slot-close retention, and a reason the version does not
  know are
  refused exactly as today. Which of the two a stopped row is — the agent's
  retained stop, or evidence an owner, a closed session or an app recorded —
  is read from the ending the record holds and never from the row's
  `stopped_reason` or `outcome`, which mirror it (above): the agent's
  `monitor_stop` and the owner's Clear stopped goal control write the same
  `USER_STOP`
  onto the row, and a rewritten reason on the row is repaired to the record
  and refused. It ends no commitment and, once Phase 2
  adds the counter, advances no generation: the same session's re-arm
  inherits the record under the next rule — a `monitor_start` in the very
  turn that stopped is an inheriting re-arm, whatever the turn's provenance,
  because the retained record leaves the slot committed — and a proxy or
  automation the loop launched or scheduled before the stop still carries a
  matching generation and is decided against that record. The stop wrappers
  read no turn provenance for this, and the rule is why: provenance says who
  started the turn, not who asked for the stop, and in any turn a person
  typed the agent holds `autonudge_stop` and `monitor_start` together, so a
  stop read as the owner's because the turn was the person's would let the
  agent end the commitment and commit a fresh pair in the same turn. An
  ending the agent can trigger therefore never ends the commitment. On the
  measured base `apply_session_directive` in
  `src/kiro_crew/dashboard/session_directive_apply.py` hands `_monitor_stop`
  and `_autonudge_stop` neither `producer_is_user_facing` nor
  `producer_is_self_wake`, the flags it reads for the arm and update
  directives alone, and Phase 1 leaves them so. The cost is stated plainly:
  when a person asks the agent in chat to stop, the agent's stop is retained,
  and a workflow, subagent or cron the loop set in motion may still spend the
  remaining budget on the slot; the producer contracts of §1 therefore tell
  the agent to point the person at the popover's Clear stopped goal control,
  reached after Pause and backed by `DELETE /api/autonudge/{id}`, or at `/goal
  clear` for an
  ending. Commitment-ending authority is reserved for the owner routes the
  agent cannot invoke — `DELETE /api/autonudge/{id}`, and `/goal clear`
  typed by the person, which Phase 2 gates on authenticated-human provenance
  (the fourth rule) — and for the service's own terminal endings: a spent
  cycle cap or runtime budget, and the timer's stall stop for an unbounded
  loop. A stop from a channel is retained on the same terms as one from a
  tab; the provenance the channel consumer starts passing in Phase 2 (below)
  serves the fresh-slot arm, not the stop.
- A `monitor_start`, a workflow's `ctx.nudge` on its originating session, or
  any other arm or replacement the loop's session directs at a slot that
  holds a committed loop — an active row, or a retained self-stopped record —
  commits no fresh bounds. The loop it produces inherits the committed
  classification and the remaining budget: the bounds the arm requests are
  capped at what is left of the committed pair — the cycles not yet delivered
  and the seconds not yet spent, read from Phase 2 off the spend the record
  seals rather than the row's `cycle_count` and `created_ts` (above) — which
  is the computation the slot-close
  restore `_restore_slot_nudge_loop` in
  `src/kiro_crew/dashboard/chat_handlers.py` already performs when it re-arms
  a retiring loop with its remaining budget, and which, by its own account,
  must not buy cycles the person never granted; that restore is itself an
  inheriting re-arm at the record the slot-close retirement retains (above).
  A replacement of the active
  row carries the commitment and the budget already spent forward — the
  delivered cycles and the elapsed seconds — rather than resetting them to a
  fresh count. The arm is refused outright while the committed loop carries
  `approval_stalled`, active or retained, because the stalled loop's marker is
  evidence a fresh loop must not inherit or consume, and it is refused when
  the remaining budget is exhausted, as the slot-close restore declines to
  restore a loop whose cap or wall-clock budget is already spent. A committed
  pair of `0` and `0` has no remaining budget to cap against: the loop the arm
  produces takes the requested live bounds and the unbounded classification,
  and stops on the marker at its next wake as §3 says. A pre-upgrade row,
  read unbounded with no commitment record (Backward compatibility), is
  inherited the same way with its stored bounds as the remainder to cap
  against. In Phase 1 the one directive re-arm, `monitor_start` at a retained
  record, reads that record at the applier `_monitor_start` — through the
  `svc.get_by_slot(binding)` read that `_monitor_update` and the
  `_monitor_stop` and `_autonudge_stop` wrappers of `_stop_resolved_loop`
  already make, which on the measured base `_monitor_start` itself does not
  — before it reaches the authorizer, and is admitted at `_add_unserialized`
  as an inheriting re-arm rather than refused as the displacement of retained
  evidence, the `_add_unserialized` change the first rule names; in
  Phase 2 the arm goes through
  the same gateway authorizer as every other arm a session can direct at its
  own slot, so the rule is decided there, once, for every proxy — and a
  workflow's `ctx.nudge`, or an automation-originated turn's `monitor_start`,
  reaches it only under the commitment its run was launched or its automation
  scheduled under: an arm whose carried generation is no longer the slot's
  is refused before the slot's state is read, so the record this rule
  inherits from is the one the launching or scheduling turn was authorized
  under, never one the owner committed since.
- A fresh slot, one holding no committed loop — neither an active row nor
  the retained record a stop the agent triggered leaves behind — keeps each
  surface's own
  commitment as today, and who may reach it generation-free is the decision
  of this rule. A turn with authenticated-human provenance — the person typed
  into the session's own surface: on the dashboard `_directive_user_origin`
  `True`, which the chat runner hands the directive consumer as
  `producer_is_user_facing`; on a channel, the `producer_is_user_facing` the
  channel consumer passes for a person's inbound message (Phase 2, below) —
  commits a fresh pair with no generation to compare:
  `monitor_start`'s defaults are its committed pair, as today. Every turn
  automation originates — the loop's own cycles, subagent and workflow
  completions, task-runner stage turns, cron-to-origin injections,
  app-driven turns that relay no person's own answer — carries the
  generation its scheduling turn handed it: captured from the slot when that
  turn had authenticated-human provenance, inherited from the one that turn
  itself carried otherwise, as a workflow run carries the one its launching
  turn captured or handed it, and its arm is decided as a proxy's is. A
  matching generation reaches this rule and the one above: a person's cron
  on a slot whose generation never moved arms as today, and a `ctx.nudge` or
  a matching turn's `monitor_start` on such a slot commits exactly what §6
  records for its surface — for `ctx.nudge`, `0` unless its caller passes
  `max_cycles`, with no runtime parameter. A mismatch is refused and
  reported into the turn or the run, whether the slot is empty again or holds
  a loop the owner armed meanwhile, and that loop is not displaced. So a
  loop's cap-ending cycle cannot re-arm its slot — its arm meets the active
  row with no budget left, or a generation the cap's ending has advanced
  past — and a cron the loop created cannot re-arm it after the owner's
  ending, nor can any further hop of automation that cron's turn or a
  completion's turn schedules in its own right, since each hop inherits the
  generation the loop's cycle carried and none of them can capture a fresh
  one, while nothing changes for the person typing into the tab. The
  default-cap decision for the two programmatic callers (§6) does not move;
  only the displacement of a committed loop from its own slot, and the arm
  of a proxy or an automation-originated turn whose commitment has ended or
  been replaced, do.
- Only the owner resets a slot's commitment, through the routes
  `_require_monitor_owner` already gates: `DELETE /api/autonudge/{id}`
  (`api_autonudge_delete`, behind the popover's Clear stopped goal control,
  reached after Pause) removes
  the record so an arming surface can commit a new pair, `PATCH
  /api/autonudge/{id}` revives it, the owner write that clears the marker
  (`AutoNudgeService.update` clears it on an actual revival) and commits the
  bounds it carries, and `POST /api/monitors/{id}/restart`
  (`api_monitor_restart` in `src/kiro_crew/dashboard/handlers/autonudge.py`,
  the sole browser revival of a terminal structured monitor) arms a fresh
  monitor through `authorize_and_add_nudge` with `max_cycles=0` and its
  budget clamped to `runtime_ceiling_secs()`, so it lands at
  `_add_monitor_locked`, which §5 routes through the transition helper, and
  commits a new pair there as the owner's own arm: Phase 2 carries that
  verified owner act into the structured add transition, so the retained
  stop that refuses an inheriting re-arm does not refuse the owner's
  restart. `/goal` and `/goal clear` in
  `src/kiro_crew/dashboard/chat_runner.py` are commands a person types into
  the tab — the one arms through `AutoNudgeService.add` with its replacing
  default, the other removes the record — and are that person's own arming
  and clearing only when the turn carries authenticated-human provenance. On
  the measured base nothing checks that: the slash dispatch in `_run_chat`
  hands a `/goal` message to `_handle_goal_command` before anything reads the
  turn's provenance, and the handler calls `AutoNudgeService.add` or
  `remove` directly, with no provenance and no generation to compare. An
  enabled app holding the `sessionApproval` grant may send into a person's
  slot through `POST /api/chat` (`_app_may_send_to_slot` in
  `src/kiro_crew/dashboard/chat_handlers.py`), and every such send reaches
  `_run_chat` with `_directive_user_origin` `False` — at once, or queued
  while a turn runs and drained later. A `/goal` so drained after the owner's
  clear renews the commitment, and a `/goal clear` so drained ends it: the
  seventh variant by a second door, one the directive consumer never sees.
  Phase 2 has the slash dispatch refuse `/goal` and `/goal clear` from a turn
  or drained entry without authenticated-human provenance, reporting the
  refusal into the turn as the handler reports its other outcomes; a `/goal`
  the person types arms 50 as today. A stop the person asks the agent for in
  chat is not among these acts: the agent's stop tool retains the record (the
  first rule), and the ending is the control the agent cannot invoke. Each of
  these owner acts advances the
  slot's commitment generation, as does every ending of a commitment, so a
  proxy launched, or an automation scheduled, before the reset or the ending
  finds its generation stale and arms nothing.

The refusal while the marker is set is the special case of the second rule,
not a rule of its own, and the rule covers the proxies alike: a `ctx.nudge`
aimed at the originating slot while its loop carries the marker is refused as
a `monitor_start` at a retained stalled record is. The generation refusal is
the other half of the same decision, read first: a proxy, or an
automation-originated turn, whose commitment has ended or been replaced is
refused whatever the slot holds, so the slot-state rules above are ever read
only by an arm that arrives under the commitment it was launched or
scheduled under. A turn with authenticated-human provenance carries no
generation and needs none: it is the person acting on the slot, visibly, and
the owner can stop what it arms. The timer's own stall stop
— the record it deactivates with `stopped_reason="approval_stalled"` — keeps
today's treatment: it is the ending itself, it fires at a wake that delivers
no turn, and it stays inspectable and re-armable once a person restores the
authorization; it advances the slot's generation as every ending does, so
the re-arm that displaces it comes from a person's turn, not from a run the
stalled loop launched before it stopped, nor from a turn that automation the
loop scheduled starts on the slot after it.

The provenance the fresh-slot rule reads must reach the applier from every
surface a person arms from, and on the measured base it reaches it from one.
The dashboard consumer in `src/kiro_crew/dashboard/chat_runner.py` passes
`apply_session_directive` its turn's `_directive_user_origin` as
`producer_is_user_facing`. The channel consumer, `build_directive_consumer`
in `src/kiro_crew/messaging/dispatch.py`, calls
`apply_session_directive(..., producer_is_channel=True)` and never passes
`producer_is_user_facing`, whose default in
`src/kiro_crew/dashboard/session_directive_apply.py` is `False`; the nine
channel transports that build a consumer build it through it — WhatsApp,
the tenth registered, builds none — and the Slack gateway's
own nudge-fire turn does too. So `_monitor_start`'s
`initiator_slot_key=binding if self_arm_ok else ""` resolves to `""` for
every channel turn, a person's included — harmless today, because the flag
feeds only the crew/member rule, which refuses an external arm only where the
slot's mode is in `_EXTERNAL_ARM_REFUSED_MODES`, but the fresh-slot rule
would read it as an automation's turn: a person's Slack, Discord or Webex
watch request — a surface `monitor_start` in
`src/kiro_crew/mcp_tools/control.py` documents as supported, and the channel
legacy loop this section names — would be refused on an empty slot. Phase 2
therefore has the channel consumer pass `producer_is_user_facing` for a turn
a person's inbound message started, and withhold it from a bot- or
automation-authored channel message and from a channel loop's own wake,
before the flag becomes the arming gate; that passing is what
authenticated-human provenance means on a channel, as
`_directive_user_origin` `True` is what it means on the dashboard.

The rule reads the committed values, not their history, so it does not need to
know how a committed `0` arose; whether a surface commits a `0` at all is that
surface's decision (§6). The same treatment applies to an explicit `0` a person
typed as the popover's advertised infinite opt-in, to a `0` a pre-upgrade
popover armed for an untouched field or a REST body stored for an omitted one,
to a `0` the owner writes later through the loop's `PATCH` route, to the `0`
that `ctx.nudge` and the Issue Radar crew runtime arm by default, and to the
`0` an auto-research creator explicitly submits as a campaign cap. A
pre-upgrade stored `0` is therefore treated as unbounded: it is never rewritten
to the new default, it is shown as `0`, and it keeps the stall stop. So is
every other pre-upgrade row, whatever bounds it stores, because the store
holds no evidence of who committed them (Backward compatibility). The owner
of such a loop opts into remediation continuity by giving it any finite budget
through an arming surface or the owner-gated `PATCH` route; the loop cannot
give itself one, whether by writing a bound, by stopping and re-arming
itself — closed in Phase 1 — by arming a replacement on its own slot through
a proxy, by a proxy it launched before the owner cleared the slot, or by a
turn that automation it scheduled starts on the slot after that clear —
closed in Phase 2 — since a
self-written bound is capped at the committed pair — against a committed `0`
and `0` it only tightens the live values and moves nothing — a re-arm or
replacement from its own session inherits the committed classification and
remaining budget rather than committing a new pair, and a proxy or an
automation-originated turn whose commitment has ended arms nothing.
Unlimited operation stays available, but it is a stated choice that carries
the stall stop, never the state a blank field falls into.

### 6. Arming surfaces

Every legacy prompt loop is created through `AutoNudgeService.add`, whose own
defaults are `max_cycles=0` and `max_runtime_secs=0`. The surfaces below are the
complete set on the measured base; Phase 1 changes the default cap of only the
two dashboard goal surfaces, and each in its own way, because the popover never
omits the field that the REST default reads, and refuses a negative cap at
every one of them rather than storing the `0` the service clamps it to
today (§5). Four other rows change in Phase
2 without their cap moving: `/goal` gains the provenance gate of §5, a
`ctx.nudge` at a committed loop's own slot inherits its commitment (§5), and
the Issue Radar crew runtime's and auto-research's watchdogs stop reviving a
stalled loop (§5).

| Surface | Default cap today | Explicit `0` | Runtime budget | Change (phase) | Unanswered approval after the change |
|---|---|---|---|---|---|
| `/goal` (`src/kiro_crew/dashboard/chat_runner.py`) | 50 cycles; `--max N` clamps to 1–50 | not expressible | none | cap unchanged; Phase 2: `/goal` and `/goal clear` are dispatched only from a turn with authenticated-human provenance — an app-sent or drained entry without it is refused and the refusal reported into the turn (§5) | bounded: continues (Phase 1) |
| Set-a-goal popover (`website/src/components/AutoNudgePopover.tsx`) | field seeded `0` for a fresh goal, empty field parses to `0`, always sent; `0` labelled infinite | unlimited; a typed negative is sent as typed (`parseCycles` passes it through, `min={0}` is an input attribute) and the service stores it `0` | not exposed; sends none | Phase 1: fresh goal seeds 50; empty or unparseable field commits 50, including the `0` its blur normalization writes into such a field; typed `0` commits `0`; a negative is refused in the field before it sends; live loop shown as-is; remembered draft restored verbatim only when its cap-commitment marker is present; a legacy or uncommitted-cap draft at `0` restores message and idle and reseeds 50 | bounded: continues; typed or committed-and-remembered `0`: stall stop (Phase 1) |
| Set-a-goal popover's pause, play and Clear stopped goal controls (`website/src/components/AutoNudgePopover.tsx`) | writes no cap: `pause()` sends `PATCH {active: false}` alone, which the service records as `stopped_reason: "manual"`, `play()` on a paused loop sends the edited fields with `active: true`, and Clear stopped goal appears only while paused and sends `DELETE /api/autonudge/{id}?intent=clear` | not expressible; a cap travels only when the field was edited | writes none | cap unchanged; Phase 1: the revival is an owner recommit of the slot's record, so `play()` on a stall-stopped loop commits afresh and the fresh budget `api_autonudge_update`'s `fresh_run` grants is the owner's own act; Clear stopped goal ends the commitment and removes its record (§5) | bounded: continues; the owner's play is the answer the stall stop asks for, clearing the marker on the revival |
| `POST /api/autonudge` (`api_autonudge_start`) | omitted `max_cycles` stored `0` | unlimited; a negative `max_cycles` is coerced with `int()`, forwarded and stored `0` (a negative `max_runtime_secs` is already refused by `validate_runtime_secs` in `api_autonudge_start` itself, and again by the service) | omitted stays `0`; explicit value accepted up to the configured runtime ceiling (`604800` by default) | Phase 1: omitted `max_cycles` → 50; explicit `0` stays unlimited; a negative in either field is refused with a validation error naming it; no runtime default | bounded: continues; explicit `0` with no runtime budget: stall stop (Phase 1) |
| `monitor_start` (`src/kiro_crew/mcp_tools/control.py`) | `_MONITOR_DEFAULT_MAX_CYCLES` = 24, written into every payload; the applier `_monitor_start` in `src/kiro_crew/dashboard/session_directive_apply.py` that arms the loop would read an absent field as `0`, which no tool payload has | refused (schema minimum `1`) | `_MONITOR_DEFAULT_MAX_RUNTIME_SECS` = 14,400 s when omitted, written into every payload; `0` refused | unchanged | bounded on every tool-produced payload: continues (Phase 1) |
| Spec Builder handoff (`orchestration/execution.py` under `src/kiro_crew/apps/builtins/spec_builder/backend/`; `handlers.py` re-exports it) | `_EXEC_MAX_CYCLES` = 60 (`orchestration/execution_state.py`) | not expressible | `0` | unchanged | bounded: continues (Phase 1) |
| auto-research (`src/kiro_crew/apps/builtins/auto_research/handlers.py`, a facade over `campaign/*`) | campaign row, `NOT NULL DEFAULT 30`; insert writes `config.get("max_cycles", 30)` | accepted, as is any negative (`validate_campaign` has no lower bound below `MAX_CYCLES_HARD_CAP` and runs only for the validate and create routes — the `fork` action in `_handle_action` builds `fork_config` with `body.get("max_cycles", 30)` and calls `create_campaign` directly; `_launch_loop` hands `int(row["max_cycles"] or 0)` to `AutoNudgeService.add`, which stores a negative as `0`); unlimited loop record, but the watchdog completes the campaign on its first recorded cycle (`count >= row["max_cycles"]`) | `0` | cap unchanged; Phase 1: a negative cap is refused with a validation error at `create_campaign`, the insert `_handle_create` and the `fork` action both reach, and reported by `validate_campaign` beside its `MAX_CYCLES_HARD_CAP` refusal on the routes that call it, so no campaign row stores one and `_handle_action`, which publishes RUNNING through `update_campaign_status` before `_launch_loop`, never marks RUNNING a campaign whose stored cap the service would refuse (§5); Phase 2: `_watchdog_loop` no longer revives a loop deactivated with `stopped_reason="approval_stalled"` — only the app's explicit `resume` in `_handle_action`, which re-arms through `_launch_loop` under the route's own owner-resume act, decided before RUNNING is published, or the owner-gated `PATCH` does; other inactive loops of a RUNNING campaign are revived as today (§5) | default campaign is bounded: continues within its remaining cycles; explicit `0` campaign: loop record keeps the stall stop, as today, while the campaign itself completes on its first recorded cycle (Phase 1); a pre-upgrade campaign row is read unbounded and keeps the stall stop, which holds only from Phase 2 — on the measured base, and after Phase 1 alone, `_watchdog_loop` revives the loop and clears the marker on the first pass that finds its campaign RUNNING |
| `ctx.nudge` (`src/kiro_crew/workflows/runner.py`) | `0` (signature default) | unlimited; a negative passes every link unchecked and the service stores it `0` | no parameter at any link (`_nudge_port`, `_wf_nudge_authorizer`); `0` | default excluded; Phase 1: a negative `max_cycles` is refused at the service's add transition and recorded in the run's stream as its other refusals are, the nudge path itself untouched; Phase 2: on a slot holding a committed loop the arm inherits that loop's classification and remaining budget instead of replacing the row with a fresh count, and a run launched under a commitment that has since ended or been replaced is refused (§5) | default `0`: stall stop; a script that passes a finite `max_cycles`: continues (Phase 1) |
| Issue Radar crew runtime (`src/kiro_crew/apps/builtins/issue_radar/backend/crew_runtime.py`) | arms `0` by design | unlimited | `0` | default cap excluded; Phase 2: `watchdog_cycle` no longer revives a loop deactivated with `stopped_reason="approval_stalled"` — only the app's own resume (`_handle_crew_pause`'s `paused` false branch, which revives it and clears its marker itself) or the owner-gated `PATCH` does; other inactive loops are revived as today (§5) | stall stop (Phase 1); it holds only from Phase 2 — on the measured base, and after Phase 1 alone, the watchdog revives the loop and clears the marker on its next pass |
| Perpetual mode switch (`docs/request-for-change/rfc-perpetual-agent.md`, accepted; no implementation on main) | none — no code today; the decision sets both of a crewmate's caps to `0` at the owner's switch | the switch's whole product: both caps `0`, owner-set only, never self-granted | `0`, by the same switch | cap unchanged; when implemented, the switch is an owner recommit of both bounds to `0` and passes the §5 commitment gate as any owner revision does, so its record and its row agree | unbounded: stall stop, as today; the Perpetual acceptance requires the stop to read as a stop on the switch's surfaces, with the switch as the owner's re-arm (§5) |

The popover change is what makes a dashboard goal finite, because the create
control `startNow()` — one of the pause and play controls the popover now
carries — serializes `max_cycles` on every create through `formFields()`, and
`api_autonudge_start`'s omission
default therefore never fires for it. Phase 1 seeds a fresh goal — no live loop
on the slot and no remembered draft — with 50, and an empty or unparseable field
commits as 50 instead of `0`. A typed `0` is a value, not a blank, and commits
as `0`. A typed negative is neither, and the field refuses it before
`startNow()`
sends: on the measured base `parseCycles` passes it through and the service
stores it as a `0` nobody typed (§5). A live loop's field shows its stored
`max_cycles`, `0` included, and is
not rewritten to the default.

The committed value alone cannot carry that distinction, because the field
rewrites itself. Its `onBlur` writes `parseCycles(maxCyclesInput)` — `parseInt`
falling back to `0` — back into the field, so a field a person emptied, or one
whose text does not parse, shows the literal `0` before `startNow()` reads the
raw
input, and at that point a cleared field and a typed `0` are the same string.
The rule therefore reads the committed value and its provenance. Phase 1
tracks whether the cycles field was ever given a non-empty value that parses:
a `0` a person typed is committed and commits as `0`; a `0` the blur
normalization wrote into an emptied or unparseable field is uncommitted,
commits as 50 and records no cap-commitment marker. The alternative of leaving
such a field empty on blur instead is named under Alternatives; it is not the
rule, because the clear fires `onChange` either way.

A remembered draft needs one more distinction, because on the measured base a
remembered `0` is not always one the person chose. `draftToPersist` drops a
draft only when all three fields are pristine — the default message, an idle
of 60 and a cap of `0` — and `hasEdited` is set by the message field's
`onChange` as much as by the cycles field's, so a person who edits only the
message persists `{message, 60, 0}` with the untouched seed `0` inside it.
Restored verbatim after Phase 1, that draft would reopen with `0` in the field
and `startNow()` would send it, arming an unbounded goal from a cap nobody
committed — the state §5 says a blank field must never fall into. Phase 1
therefore:

- records a per-draft cap-commitment marker only when the cycles field was
  given a non-empty value that parses — its provenance, not the bare fact of an
  edit, since clearing the field fires the same `onChange` and the blur
  normalization then writes `0` into it; a draft restored with the marker
  keeps it through later edits to the other fields, so a committed `0`
  survives a message-only edit;
- restores a remembered draft verbatim, `0` included, only when that marker is
  present; and
- for a legacy draft (written before the marker existed) or an uncommitted-cap
  draft whose `maxCycles` is `0`, restores the message and idle and reseeds the
  cycles field with 50. Unlimited operation stays available by entering `0`
  again, which commits the cap and sets the marker. A positive legacy cap is
  restored as it was; it is finite either way. A negative one, should a
  pre-upgrade draft hold it, is restored and refused at save as every
  negative is (§5).

The pre-upgrade message-only draft is the case this closes: without the marker,
Phase 1 would seed a fresh goal at 50 and still arm the next goal on that slot
at `0`. The REST omission default is the backstop for a caller other than the
popover that leaves the field out of its body; with the popover fixed it has no
shipped caller that reaches it, and that is the point: both routes into a
dashboard goal must be finite by default independently.

Auto-research's cap is not changed by Phase 1 — `create_campaign`, the insert
every campaign row passes through, gains the lower bound §5 gives every
boundary, with `validate_campaign` reporting it on the validate and create
routes and the `fork` action covered at the insert it reaches directly, and
nothing else moves — and its
consequence is the one the rule
implies. Its worker slot's tools are auto-approved, so an approval prompt cannot
time out while the trust grant is in force. When the 24-hour grant expires the
watchdog parks the campaign for re-authorization and clears slot trust; the park
does not itself deactivate the loop, so a tool prompt on a later cycle can run
its window unanswered and record the marker. Today that deactivates the loop.
After Phase 1 a default campaign, bounded at 30 cycles, consumes the marker and
continues within its remaining cycles, exactly as any other bounded loop does.
A campaign whose creator submitted `0` has two lifecycles to keep apart: its
AutoNudge record is unbounded and keeps the stall stop, but the campaign itself
is not long-lived, because the watchdog's `count >= row["max_cycles"]` check
completes a RUNNING campaign on the first recorded cycle result when the cap is
`0`; that completion is a campaign status transition and does not by itself
deactivate the loop record. The loops Phase 1 reads unbounded on this surface
— a pre-upgrade campaign row until its owner recommits it, and the explicit
`0` — keep the stall stop only as far as the app lets them: on the measured
base, and after Phase 1 alone, `_watchdog_loop` revives every inactive loop
of a RUNNING campaign, the stalled one included, and clears its marker, and
the guidance route `_handle_nudge` returns a parked campaign to RUNNING
without re-arming, so the revival is the watchdog's (§5). Phase 2 has that
watchdog preserve an unattended stall stop and leaves the revival to the
app's explicit `resume` or the owner-gated `PATCH`, as §5 states.

Two further paths reach `AutoNudgeService.add` on the dashboard. The
session-directive applier `_monitor_start` in
`src/kiro_crew/dashboard/session_directive_apply.py` is the path that creates a
`monitor_start` loop — the tool in `control.py` validates its arguments, applies
its defaults and encodes a directive, and the applier arms it through
`authorize_and_add_nudge`. The applier reads `int(args.get("max_cycles") or
0)` and `int(args.get("max_runtime_secs") or 0)`, so it would arm `0` for a
field the directive lacks rather than refuse — the same tolerance its `gate`
handling names for a directive written before a field existed. On the measured
base the tool writes both fields into every payload after applying
`_MONITOR_DEFAULT_MAX_CYCLES` and `_MONITOR_DEFAULT_MAX_RUNTIME_SECS`, so the
applier's `0` fallback has no shipped producer, and the bound is the tool's,
not the applier's. The slot-close pair is the other: `_retire_slot_nudge_loop`
in `src/kiro_crew/dashboard/chat_handlers.py` retires a dismissed tab's loop
through `remove_by_slot`, a retained transition that writes the slot-close
ending into the record before the row alone is removed (§5), and on a close
that fails to persist
`_restore_slot_nudge_loop` in the same module re-arms the retired row through
a plain `svc.add(...)` with its remaining cycle and runtime budget — an
inheriting re-arm decided against that retained record, which takes the
record's committed classification and remainder, mints no fresh commitment
and carries no generation for Phase 2 to compare; a retired pre-upgrade row
is restored with its live bounds and stays read unbounded until its owner
recommits it (§5). It is the computation a self-session re-arm of a retained
record reuses.
Neither introduces a `0` on a path the measured base ships.

Why 50 rather than the `monitor_start` pair: `/goal` has shipped 50 cycles at a
15-second idle, and the popover's default idle is 60 seconds, so the three
dashboard goal surfaces share one goal budget and `/goal`'s shipped budget does
not change. `monitor_start` defaults 24 cycles and 14,400 seconds around a
300-second default interval, an envelope sized for a pull-request watch; at a
goal's idle 24 cycles would end in minutes. Both values are finite, and the
hazard this RFC closes is unboundedness, not the number. The dashboard goal
surfaces receive no runtime default in Phase 1: the cycle cap is already the
service-enforced ending, `/goal` has had exactly that shape, and a person who
wants a wall-clock bound sets `max_runtime_secs` on the REST route.

Why the two programmatic callers are excluded: each is a public or app-owned
contract whose cap is chosen by its own caller. Changing the `ctx.nudge`
signature default would alter every workflow script that relies on it, and the
Issue Radar crew runtime documents `max_cycles=0` as its intended shape with the
record flags, STOP sentinel and app gate as its brakes. Neither cap is asked
to change here; under the rule above a loop they arm with no cap keeps the
stall stop it has today, so this decision cannot leave a stalled human-only
approval on them with no service-enforced ending — provided the stop holds,
which for the crew runtime it does not on the measured base: its brakes are
the crew's, and its watchdog re-activates every inactive loop of a live crew
on every pass, stall stop included, clearing the marker with the revival.
Auto-research's `_watchdog_loop` does the same for every inactive loop of a
RUNNING campaign, so the stall stop on the research loops Phase 1 reads
unbounded lasts until the campaign is next RUNNING (§5). §5
makes that stop hold on both surfaces as it holds everywhere, in Phase 2:
each watchdog
decides a revival, and Issue Radar's its fresh arm through `launch_crew`,
from the sealed commitment record, not from the row's
`stopped_reason` or the row's presence: a loop whose commitment the stall
stop ended stays
inactive on every pass, a slot whose commitment is ended and whose row is
gone is armed by no watchdog, and only an explicit resume revives or re-arms
it — for Issue
Radar the
resume route's
own act, since a stalled crew stays `enabled` and the watchdog cannot tell a
resume from an unattended live crew; for auto-research the `resume` action
of `_handle_action`, whose own act Phase 2 carries into the `_launch_loop`
re-arm, decided before RUNNING is published (§5). A later RFC may give either
programmatic caller a finite
default. The exclusion is of the default cap, not of the slot rule: a
`ctx.nudge` aimed at a slot that already holds a committed loop is an arm the
loop's session directs at its own slot, and §5 decides it, in Phase 2, as it
decides a `monitor_start` there — it inherits the committed classification
and the remaining budget rather than replacing the row with a fresh count.
On a fresh slot it commits exactly what the row above records, and it does
so only while the slot's commitment generation is the one its run was
launched under; a run that outlived the loop, or the clear, it was launched
under arms nothing (§5).

## Migration plan

### Phase 0 — decision record

Land this RFC independently of implementation PR #13000.

Exit criteria:

- the RFC is on `main` with status `accepted`;
- maintainers have explicitly accepted the blocker lifecycle and authority
  ceiling through normal RFC review; and
- the implementation PR references the merged document.

### Phase 1 — contracts, timer rule and defaults

Implementation PR #13000 is this phase. It aligns all instruction producers
named in §1 — the eight producers: the base prompt, the generated `/goal`
instruction, the `autonudge_stop` tool description, the bundled babysit and
prepare-pr skills, the self-nudge recipe and its scaffolded template, and the
repo-checkout goal-loop skill — fixes the generated self-nudge template, splits
prompt-loop and structured-monitor approval behavior, applies the
bounded/unbounded rule of §5 at the legacy timer's approval-evidence check on
the loop's committed bounds — recording the committed pair, per slot, in the
commitment leaf `autonudge-commitments.json` when a bound is committed, at
`_add_unserialized`, the creation point every arming surface reaches through
`AutoNudgeService.add`, and at the owner-gated `PATCH` recommit through
`AutoNudgeService.update`, which the service tells from the loop's own
`monitor_update` by an owner-recommit signal `api_autonudge_update` sets
only after `_require_monitor_owner` succeeds and threads through
`authorize_and_update_nudge` into the locked transition at
`_update_unserialized`, every other caller defaulting to non-owner (§5) —
recording beside the pair the `approval_stalled` marker, written
record-first at `notify_approval_stalled` and cleared through the same
helper by the timer's consumption and by `AutoNudgeService.update`'s
revival, and the loop's `stop_sentinel_path`, written at `_add_unserialized`
and at `_load`'s `repair_sentinel_path`, with `_timer` and
`record_monitor_turn_completion` reading the marker off the record, `_timer`
the path, and the row's copies repaired to it at load and at every tick
(§5), and the loop's `consecutive_start_failures`, written record-first at
`notify_cycle_start_failed` and at its two clears, `notify_cycle_landed`
and `_update_unserialized`, with `_timer`'s stand-down and back-off
branches reading it off the record and the row's copy repaired the same
way (§5) —
giving that leaf the three-layer disposition of
§5 — listing it in `_CREW_READONLY_LEAVES` and
`_CREW_PRECREATE_READONLY_FILE_LEAVES` in `src/kiro_crew/sandbox.py` so the
OS seals it read-only for every sandboxed process, in
`_WRITE_PROTECTED_HOME_PATHS` in `src/kiro_crew/security/paths.py` so
`is_sensitive_write_path` has `on_tool_call` in `src/kiro_crew/hooks.py`
refuse the agent's file-edit tool on it whatever the sandbox setting, and in
`_CREW_CHILD_READABLE_LEAVES` in `src/kiro_crew/sandbox.py`, since the
governance-mask pin makes every read-only leaf choose a child set and a
record holding no secret belongs on the readable side, with its writers
confined to one gateway transition helper that writes the record before the
row at every row transition — the
add at `_add_unserialized`, the recommit at `_update_unserialized`, the
removal at `_remove_unserialized`, the structured replacement at
`_add_monitor_locked`, its `remove_sync` of a displaced legacy row included,
and its `rollback_monitor_replacement` — compensates a failed row write per
transition — a fresh arm closes the record it just wrote, a recommit or an
ending stays authoritative for load to repair the row against, and only
`rollback_monitor_replacement` restores a prior record (§5) — and reconciled
against the row at
load with the record
authoritative (§5) — reading the leaf, at every read the record is
consulted through, by the helper's one reader, which opens it with
`O_NOFOLLOW` and verifies on the open descriptor that it read a regular
file with a link count of one, the `require_unaliased_launch_state(path,
fd=...)` pattern of `src/kiro_crew/sandbox.py`, a failed check read as a
missing record and audited, the writer unlinking an aliased leaf and
publishing a fresh empty record in its place under the service `_lock`
before it applies its transition, and the first load that finds the leaf
aliased doing the same rather than adopting its bytes (§5) — and reading a
row whose slot
holds no record, or disagrees with it, as unbounded — every pre-upgrade row,
whatever bounds it stores, because it carries no evidence of who committed
them (Backward compatibility) — refusing a negative `max_cycles` or
`max_runtime_secs` at every arming and recommit boundary with a validation
error, at the surfaces and at the two transitions that store a cap, where
the service
refuses instead of clamping to `0` (§5) — refusing a `max_cycles` or
`max_runtime_secs`
write
from the loop's own session while `approval_stalled` is set, capping every
other such write from that session at the committed pair — a tighten or a
restore up to the committed value is applied, a raise above it is refused with
the committed ceiling and the owner routes named and the live bounds
unchanged; a pre-upgrade row's stored bounds are its ceiling — retaining the
record of every loop stopped by `autonudge_stop` or `monitor_stop` from any
turn on its session, whoever started it, and of every loop whose STOP
sentinel fires — `_timer` deactivating on the sentinel instead of removing —
with its remaining budget, its marker state and its committed classification
— the stop wrappers and the timer reading no turn provenance for it, since
an ending the agent can trigger never ends the commitment (§5), and the
producer contracts pointing a person who wants the loop ended at the owner's
stop control or `/goal clear` — and deciding the one
directive re-arm a session holds, `monitor_start` at such a retained record,
against it: the fail-closed rule already in `_stopped_row_is_replaceable`
refuses to displace the record as a fresh commitment, `_add_unserialized` —
which on the measured base raises `MonitorUpdateConflict` for such a row
under the `replace_existing=False` and `replace_stopped=True` the applier
arms with — admits the re-arm at a retained record as an inheriting one,
keeping that refusal for every other stopped reason (§5), the loop the re-arm
produces inherits the record's classification and remaining budget — the
computation `_restore_slot_nudge_loop` already performs — and the re-arm is
refused while the record carries the marker or its budget is exhausted. It
seals the ending beside the pair: who recorded every stop, and how, is
written into the record at the record-first write each ending already
makes, `_stopped_row_is_replaceable` decides from that ending at
`_add_unserialized` and `_add_monitor_locked`, `retained_outcome_blocks_rearm`
is handed it, the MCP preflight `_retained_stop_refusal` reads it over
`api_session_monitor_get`, and the row's `stopped_reason` and `outcome` are
mirrors repaired from it at `_load` — before `_is_torn_deactivation` — and
at every re-arm check, a row that reads re-armable beside a retained ending
repaired to retained, refused and audited, so a rewritten reason displaces
no owner's pause or stop, no slot-close retention and no tombstone (§5). It
retains the record across the slot-close retirement `remove_by_slot` as well
— the slot-close ending written into it before the row alone is removed —
and admits the failed-persist
restore `_restore_slot_nudge_loop` at that record as the same inheriting
re-arm, the restore handing the helper the retired row so the remainder is
read from it, taking the record's classification and remainder and minting
no fresh commitment, while a retired pre-upgrade row is restored with its
live bounds and stays read unbounded (§5). It
makes a dashboard goal finite by default on both of its routes — the
Set-a-goal popover seeds and commits 50 for a fresh untouched goal, for an
emptied or unparseable field whatever its blur normalization shows, and for a
remembered draft whose `0` cap was never committed (§6), and `POST
/api/autonudge` stores an omitted `max_cycles` as 50 — matching the `/goal`
budget, with an explicit `0` still meaning unlimited and `max_runtime_secs`
staying opt-in, leaves the bounds that `/goal`, `monitor_start`, the Spec
Builder handoff, auto-research, `ctx.nudge` and the Issue Radar crew runtime
commit on a fresh slot unchanged, and adds durable transition, write-failure,
gate-bypass, and contract tests. None of this behavior is on `main` until that
PR merges; the measured base above remains the current runtime state.

This phase touches the timer — its approval-evidence check, its sentinel
branch and its start-failure stand-down and back-off branches, all reading
the record — `record_monitor_turn_completion`, deciding
the structured stall disposition from the record's marker as `_timer`'s
check does, `notify_approval_stalled`, writing the
marker record-first through the helper, `notify_cycle_start_failed` and
`notify_cycle_landed`, writing the start-failure streak record-first the
same way, the service's row transitions —
`_add_unserialized` under
`AutoNudgeService.add`, `_update_unserialized` under the owner-gated `PATCH`,
`_remove_unserialized` under every `remove`, and `_add_monitor_locked` with
its `rollback_monitor_replacement` — through the one transition helper they
share, `_add_unserialized`'s admission of an inheriting re-arm at a retained
record inside it, the ending each stop writes into the record, the re-open
an inheriting re-arm or replacement writes back over it, and the two
readers of it — `_stopped_row_is_replaceable` at `_add_unserialized` and
`_add_monitor_locked`, with `retained_outcome_blocks_rearm` in
`src/kiro_crew/monitoring/models.py` handed the record's ending, and the MCP
preflight `_retained_stop_refusal` in `src/kiro_crew/mcp_tools/control.py`
reading it through `api_session_monitor_get` in
`src/kiro_crew/dashboard/handlers/autonudge.py` — and its load-time read
`_load`, reconciling the marker, the sentinel path and the row's
`stopped_reason` and `outcome` beside the bounds, ahead of
`_is_torn_deactivation`, the slot-close pair —
`remove_by_slot`, retained as a transition, and `_restore_slot_nudge_loop`
in `src/kiro_crew/dashboard/chat_handlers.py`, handing the helper the
retired row it re-arms — the commitment
leaf they write and its four
list entries — `_CREW_READONLY_LEAVES`,
`_CREW_PRECREATE_READONLY_FILE_LEAVES` and `_CREW_CHILD_READABLE_LEAVES` in
`src/kiro_crew/sandbox.py`, and `_WRITE_PROTECTED_HOME_PATHS` in
`src/kiro_crew/security/paths.py` — the helper's reader and writer seam,
which every read of the record goes through and which carries the
descriptor-bound alias check on every read and before every write, the
upgrade's refusal of a pre-planted alias at the first load, and the SEL
line each writes — the
session-directive applier's three loop directives — `_monitor_update`,
`_stop_resolved_loop` with its two wrappers, and `_monitor_start` at a
retained record, with the `_refuse_stale_wake_arm` guard ahead of it — the
popover, the REST route `api_autonudge_start` and the
owner-gated `api_autonudge_update`, which passes the owner-recommit signal,
the instruction producers, `authorize_and_update_nudge`, for the negative-cap
lower bound and for threading that signal from the REST route to
`AutoNudgeService.update`, and, for the negative-cap lower bound alone,
auto-research's `create_campaign` boundary — the insert `_handle_create` and
the `fork` action of `_handle_action` both reach — with `validate_campaign`
reporting the same refusal on the validate and create routes that call it.
It does not touch `authorize_and_add_nudge`, the workflow nudge path, the
subagent, cron, task-runner or app injectors, the slot queue, the channel
consumer, the slash dispatch, the Issue Radar watchdog or the auto-research
watchdog; it changes no
gate — `is_sensitive_write_path`, `on_tool_call` and the sandbox launcher
read lists the leaf joins, and are otherwise as on the measured base — it
leaves the spawn path's `_warn_if_alias_backed` and
`_materialize_sealable_ceilings` as they are, warning and continuing over
the leaf as over every other, and adds the leaf to no nofollow list, since
the alias check lives at the consuming seam as `require_unaliased_launch_state`
does and a refusal on the spawn path would fail every sandboxed spawn on a
host whose files carry a second name — it
reads no generation, and reads no provenance flag the measured base does not
already read for the crew/member rule, adding it to no producer: the stop
wrappers and the timer decide the stop without one. It is independently
shippable, and
independently abandonable: reverting it restores the measured base, and
Phase 2 has not entered.

After Phase 1 alone, of the guarantees §5 and §6 state:

- the bounded/unbounded rule holds on committed bounds at every wake that
  reads approval evidence: a bounded loop consumes the marker and continues,
  an unbounded loop keeps the stall stop, and every pre-upgrade loop is
  unbounded until its owner recommits it;
- the first four self-widening variants are closed: a bound write while the
  marker is set is refused, a post-consumption raise is capped at the
  committed pair, and a stop and re-arm from the loop's own session — by its
  stop tool from any turn or by the sentinel it can write, while the marker
  is set or after consuming it — inherits the retained record instead of
  committing a fresh pair, and the ending a stopped row carries is read from
  the record at the re-arm and at the preflight, so a reason rewritten on
  the row displaces no owner's pause or stop, no slot-close retention and no
  tombstone;
- the dashboard goal surfaces are finite by default, and the cap-commitment
  marker tells a typed `0` from a normalized blank;
- a negative cap is refused at every arming and recommit boundary and never
  stored as `0`, so the only unbounded commitment is a `0` some surface
  chose; and the record is written before the row at every row transition —
  add, recommit, removal, structured replacement and its rollback — and
  reconciled against it at load, so a crash between the two writes leaves an
  unarmed row or a closed record, never an open commitment beside an empty
  slot, and a row write that fails after the record write never restores an
  ended commitment;
- the record is consumed only from a lone regular file: every read verifies
  on its open descriptor that the leaf is a regular file with a link count
  of one and no symlink, a pre-planted symlink or second hardlink is read as
  a missing record and audited, and the first load and the next write that
  meet one replace it with a fresh empty record rather than adopt its bytes;
  an alias written through and unlinked before any read is the residual
  `require_unaliased_launch_state` names and this phase does not close;
- the remaining three variants stay open exactly as on the measured base: a
  workflow's `ctx.nudge` on its originating session still replaces the active
  row with a fresh count (the fifth variant), a run launched before the
  owner's clear still arms a fresh loop on the emptied slot (the sixth), and
  a subagent completion, a cron's origin injection or the loop's
  own last cycle still arms a fresh pair on a slot the owner cleared or a
  spent cap ended (the seventh); the Issue Radar watchdog still revives a
  stalled crew loop and the auto-research watchdog a stalled research loop
  once its campaign is RUNNING, an app's `POST /api/chat` `/goal` still arms
  or clears a
  person's goal, and a person's channel turn arms as today, because nothing
  yet passes its provenance; and the spend a bounded loop has consumed is
  still the row's `cycle_count` and `created_ts`, so a row rewound out of
  band regains cycles and seconds within its committed pair until Phase 2
  seals the spend beside it (§5). Self-widening by proxy or by an
  automation chain is therefore NOT closed by Phase 1, and §5's statement
  that an owner's ending is final against work the loop set in motion holds
  only after Phase 2.

Exit criteria:

- a generic remediable blocker cannot instruct `autonudge_stop` from any
  producer in §1: the base prompt, the generated `/goal` instruction, the
  `autonudge_stop` tool description, the self-nudge recipe and its scaffolded
  template, the babysit skill's example nudge and execution step 7, and the
  prepare-pr skill's example nudge; the goal-loop skill's persistence rule no
  longer says the service deactivates a loop on an approval stall without
  naming the bounded/unbounded distinction; a contract test covers the bundled
  and repo-checkout files alike, since the installed copies are produced from
  them;
- the `autonudge_stop` description and the base prompt say the stop retains
  the loop's record and does not end its owner's commitment, and point a
  person who wants the loop ended at the owner's stop control or `/goal
  clear`; the contract test pins that wording beside the remediation wording;
- permission remediation text prohibits self-grant and governance weakening;
- a bounded prompt loop (positive committed `max_cycles` or `max_runtime_secs`)
  remains active after consumed approval evidence;
- an unbounded prompt loop (committed `max_cycles=0` and `max_runtime_secs=0`)
  still deactivates with `stopped_reason="approval_stalled"` and emits
  `expired`, whether the `0` was typed explicitly, armed by a pre-upgrade
  popover for an untouched field or stored by a REST body for an omitted one,
  written by the owner through the loop's `PATCH` route, armed by `ctx.nudge`
  or the Issue Radar crew runtime, or submitted as an auto-research campaign
  cap; a test pins both branches, and a companion hand-edits a row's
  `max_cycles` to a negative in `autonudge.json` beside a bounded record and
  shows the row is read unbounded as a disagreeing row is; another commits a
  loop at `max_cycles=24` and `max_runtime_secs=0`, rewrites the row out of
  band to `max_cycles=0` and `max_runtime_secs=0`, restarts the service, and
  shows `_load` reads the live `0` against the committed `24` as a
  disagreement — the row read unbounded, deactivating with
  `stopped_reason="approval_stalled"` at its next wake rather than
  consuming the marker, and refused as a record to inherit — while the
  committed `0` for `max_runtime_secs` disagrees with nothing, and a
  variant rewrites a committed `max_runtime_secs=14400` to `0` and shows
  the same fail-closed reading for that field;
- a negative `max_cycles` or `max_runtime_secs` is refused with a validation
  error at every arming and recommit boundary and is never stored as `0`: a
  test submits a negative in each field to `POST /api/autonudge`, `PATCH
  /api/autonudge/{id}` and, through the applier, `monitor_update`, and shows
  each is refused naming the field with the store and the commitment leaf
  unchanged; a companion types a negative into the popover's cycles field and
  shows `startNow()` refuses it before any request is sent; another creates an
  auto-research campaign with a negative `max_cycles` and shows
  `validate_campaign` reports it as it reports a cap above
  `MAX_CYCLES_HARD_CAP` and `create_campaign` refuses it before any row is
  inserted; a companion forks a completed campaign through `_handle_action`
  with `max_cycles=-1` in the body — the path that never calls
  `validate_campaign` — and shows `create_campaign` refuses it before
  persistence, no campaign row exists to `start`, and no campaign is left
  RUNNING with no worker; another runs a workflow whose script calls
  `ctx.nudge(max_cycles=-1)` on a slot with no loop and shows no loop is armed
  and the refusal lands in the run's stream as a “ctx.nudge NOT armed”
  message; and a service-level test calls `AutoNudgeService.add` and
  `AutoNudgeService.update` directly with a negative in either field and
  shows each raises rather than storing `0` — newly for `max_cycles`, as
  the measured base already does for `max_runtime_secs` — so no surface can
  reach the clamp the measured base performs on the cycle cap;
- every pre-upgrade row is read unbounded whatever it stores, until its
  owner recommits it: a test restores a store holding a loop at
  `max_cycles=24` and `max_runtime_secs=14400` with no commitment record for
  its slot, lets the timer read it, records the marker, and shows the
  loop deactivates with `stopped_reason="approval_stalled"` while its stored
  bounds are shown as-is and still enforced as live caps; a companion has
  the loop's own session call `monitor_update` with `max_cycles=1000` on
  such a row and shows it is refused while a tighten to `10` is applied; and
  another recommits the row through the owner-gated `PATCH
  /api/autonudge/{id}` and shows the recommit writes the slot's entry in the
  sealed commitment leaf, the loop is read bounded from then on and consumes
  the marker at its next wake;
- the committed pair is recorded in the slot's entry of the commitment leaf,
  sealed read-only by `_CREW_READONLY_LEAVES` and materialised by
  `_CREW_PRECREATE_READONLY_FILE_LEAVES`, refused to the file-edit tool by
  `_WRITE_PROTECTED_HOME_PATHS` and classified child-readable by
  `_CREW_CHILD_READABLE_LEAVES`, when it is committed — at
  `_add_unserialized` for every arming surface of §6 and at the owner-gated
  `PATCH` recommit — beside the `approval_stalled` marker and the
  `stop_sentinel_path` the same entry holds, and neither a bound written
  from the loop's own session
  nor a stop and re-arm from that session moves it; a test arms a loop from
  each of the eight surfaces and shows each has a record carrying the pair §6
  lists for it; another arms a bounded loop, rewrites its `max_cycles` in
  `autonudge.json` above the committed pair out of band, restarts the
  service, and shows the row is read unbounded at its next wake, its
  `monitor_start` re-arm refused, and both restored by an owner-gated `PATCH`
  recommit, and repeats the rewrite with the `0` that means unlimited in
  place of the raise and shows the same reading; a third spawns a sandboxed
  shell in each sandbox mode and shows a
  write to the leaf through a runtime-composed path — `$(printf ...)`, which
  no text gate sees — is denied by the OS seal while the same shell reads the
  leaf, and that a record edited by the unsandboxed user process is honoured
  at the loop's next wake; a fourth, in the shape
  `test_run_authority_file_edits_are_refused_on_both_run_roots` in
  `test/test_sandbox_governance_mask.py` gives `subagents`, points
  `KIROCREW_HOME` at a temporary home, shows `is_sensitive_write_path` is
  true of the leaf's path and `is_sensitive_path` false of it, has
  `HookManager.on_tool_call` decide an edit-kind call naming the leaf, shows
  the decision is a deny and the file's bytes unchanged, and shows the
  gateway's direct writer still writes it — a gate, not a host chmod; and
  the governance-mask pin
  `test_every_non_hidden_leaf_is_classified_for_a_foreign_child` in the same
  file passes with the leaf present in `_CREW_CHILD_READABLE_LEAVES` and
  absent from `_CREW_CHILD_WITHHELD_LEAVES`;
- `monitor_update` refuses a `max_cycles` or `max_runtime_secs` write from the
  loop's own session while `approval_stalled` is set; a test arms an unbounded
  loop, records the marker, has the loop's own session call `monitor_update`
  with `max_cycles=1000`, shows the call is refused, and shows the loop still
  deactivates with `stopped_reason="approval_stalled"` at its next wake;
- the record is read from a lone regular file and from nothing else, on
  every host, whatever the seal did: a test plants a symlink at the leaf's
  path pointing at a file holding a bounded commitment for an armed loop's
  slot, starts the service, and shows `_load` refuses the read by name
  before any open, adopts no commitment — the row read unbounded and
  refused as a record to inherit — writes an SEL line naming the leaf and
  the symlink, unlinks the link, publishes a fresh `{}` in its place, and
  that a `monitor_start` on the now-empty slot commits a fresh pair as on
  any slot holding none; a companion hardlinks the
  leaf to a second name, writes a raised cap through that name, and shows
  the next `_timer` read verifies the descriptor's link count, reads no
  record, audits the two names, and stops the loop on the marker as an
  unbounded one, while the next transition write unlinks the leaf's name,
  publishes a fresh regular file whose link count is one, and applies its
  transition to it, the second name left holding the old inode and no
  longer the leaf; another lays the seal on a lone regular file in a
  sandboxed spawn, then creates the alias from the unsandboxed user process
  between that spawn and the timer's next read, and shows the read fails
  closed and audits exactly as the pre-planted case does, so the check
  answers about the inode consumed and not about what the spawn saw;
  another shows the writer's check is bound the same way — a symlink
  planted between the helper's read and its `atomic_write` is replaced, not
  followed, and the record the helper published is the one the next read
  returns; another shows the spawn path is unchanged — `_warn_if_alias_backed`
  warns and continues over the aliased leaf, no sandboxed spawn is refused
  on its account, and the leaf is in no nofollow list; and one shows the
  residual as stated: an alias written through and unlinked before the
  service starts leaves a lone regular file the check accepts, which is
  why the seal and the gate, not the check, are what keep the alias from
  being made;
- the `approval_stalled` marker and the `stop_sentinel_path` live in the
  slot's entry beside the pair and the row's copies are mirrors, so clearing
  `approval_stalled` (or rewriting `stop_sentinel_path`) on the row of a
  stalled unbounded loop does not prevent the stall stop: the next tick
  reconciles from the record and stops; a test arms an unbounded loop,
  records the marker, clears `approval_stalled` on its row in
  `autonudge.json` out of band, and shows the next tick repairs the row from
  the record, deactivates the loop with `stopped_reason="approval_stalled"`
  and emits `expired`; a companion does the same across a restart and shows
  `_load` repairs the row before the timer is armed; another rewrites a
  bounded loop's `stop_sentinel_path` on the row to a path nothing writes,
  writes the file at the path the record holds, and shows `_timer` stops on
  it and the row's path is repaired; another sets `approval_stalled` on the
  row of a loop whose record carries none and shows the tick clears it and
  delivers the cycle; one fails the record write at
  `notify_approval_stalled` and shows the marker is owed, no turn is
  delivered for the loop until it lands, and, after a restart before it
  lands, the next unanswered prompt records it again; a companion shows
  a pre-upgrade row, whose slot holds no record, keeps both fields on the
  row as on the measured base; and one arms a structured monitor, records
  the marker, clears `approval_stalled` on its row out of band, delivers an
  accepted action turn, and shows `record_monitor_turn_completion` still
  decides `APPROVAL_STALL` from the record, records `approval_stall` with
  outcome `BLOCKED` and deactivates the monitor exactly as on the measured
  base;
- the start-failure streak `_timer` stands a loop down by is the record's,
  and a rewritten row neither defeats the stand-down nor manufactures one:
  a test lets a loop reach `_START_FAILURE_STANDDOWN_AFTER` failed starts
  through `notify_cycle_start_failed`, writes `0` to
  `consecutive_start_failures` on its row in `autonudge.json` from a
  sandboxed shell, and shows the next tick repairs the row from the record,
  audits the disagreement and deactivates the loop with
  `stopped_reason="session_start_failures"` as on the measured base; a
  companion raises the row's streak past the threshold on a loop whose
  record carries `0` and shows the tick repairs it, defers nothing and
  delivers the cycle; another lets a completed turn reach
  `notify_cycle_landed` and shows the record's streak is zeroed before the
  row's; and one shows a pre-upgrade row with no record keeps the streak
  on the row as on the measured base;
- the record is written before the row at every row transition, through the
  one transition helper, and the two are
  reconciled at load with the record authoritative, so a crash or a failed
  write between them leaves nothing that arms or renews: a test injects a
  failure between the record write and the row write of a fresh arm through
  `AutoNudgeService.add`, restarts the service, and shows the slot holds no
  row and its open record is closed on load; a companion injects the failure
  between the record write and the row write of an owner-gated `PATCH
  /api/autonudge/{id}` recommit and shows, after restart, the row is read
  against the recommitted record — unbounded and refused as a record to
  inherit where its stale live bounds exceed the recommitted pair, bounded
  and still capped by its lower live bounds otherwise — and never runs past
  the pair the owner recommitted; one injects the failure after the record
  is marked ended and before `_remove_unserialized` removes the row for the
  owner-gated `DELETE /api/autonudge/{id}` and for `/goal clear`, and shows
  in each case the row is deactivated on load with a
  `stopped_reason` outside the re-armable set, never fires, and is refused as
  a record to inherit until the owner clears it; one injects the failure
  between the record write and the snapshot write of a structured arm
  through `AutoNudgeService.add_monitor` that displaces a legacy row holding
  an open commitment, restarts, and shows the legacy row is deactivated on
  load, its record closed, and a `ctx.nudge` from a run the loop launched
  before the arm finds nothing to inherit; one lets that arm's snapshot land
  and its authorization fail so `rollback_monitor_replacement` runs, and
  shows the prior row and the prior record are restored together, the record
  first, and the slot is read at the next load exactly as before the arm;
  one injects the failure
  after the record is marked ended and before `_timer` deactivates the row
  for a spent cycle cap and for a spent runtime budget, and shows the row
  neither fires before the restart nor arms after it; one fails the record
  write itself for each of those transitions — arm, recommit, removal,
  structured replacement, its rollback, budget
  ending — and shows the transition is denied to its caller, the row and the
  record are byte-for-byte as they were, `_timer` delivers no turn for the
  loop and retries the ending at its next pass, and a spent bound is still
  honoured before any delivery; one writes a loop's `stop_sentinel_path`
  and shows `_timer` writes the sentinel ending into the record before it
  deactivates the row — the pair and the generation unchanged, as for a
  stop directive — and the sentinel is
  honoured before any delivery; one shows a failed row write never restores
  an ended record: it clears a committed loop through the owner-gated `DELETE
  /api/autonudge/{id}`, fails the row write after the record is marked ended,
  and shows the record stays ended, the row the service still holds delivers
  no turn and is refused as a record to inherit, a repeated `DELETE` removes
  it, and after a restart with the row still present the row is deactivated
  on load and no re-arm inherits it; a companion fails the row write after a
  fresh arm's record write on a slot holding no commitment, without a
  restart, and shows the arm is reported failed and the record is closed in
  the same call; another fails it after a fresh arm that displaced a
  committed row, and shows the displaced row the service puts back is read
  against a record that no longer agrees with it — unbounded, refused as a
  record to inherit — until an owner-gated `PATCH` recommits it; and one
  fails the row write after an owner-gated `PATCH` recommit's record write
  and shows the recommitted pair stands and the row is read against it, as
  in the crash case above; and one fails the
  reconciliation write at load and shows the affected row stays unarmed for
  that process and is reconciled at the next load;
- the owner's recommit and the loop's own `monitor_update` reach the same
  `AutoNudgeService.update`, and only the recommit moves the record: a test
  sends the same body — `max_cycles=1000` on a loop committed at `24` —
  through the applier's `monitor_update` and through `PATCH
  /api/autonudge/{id}`, and shows the first is refused at the committed
  ceiling with the record unchanged while the second recommits the pair; a
  companion calls `authorize_and_update_nudge` as the applier does, with no
  owner signal, and shows the write is decided as a self-session write
  whatever `source` it names; another shows a request body or a directive
  carrying a field of that name does not set the signal; and another shows
  `api_autonudge_update` passes it only on a request `_require_monitor_owner`
  has admitted;
- a `max_cycles` or `max_runtime_secs` write from the loop's own session is
  capped at the committed pair: a tighten, or a restore up to the committed
  value, is applied, and a raise above it is refused with the live bounds
  unchanged; a test arms a bounded loop at `max_cycles=24` and
  `max_runtime_secs=14400`, records the marker, lets the loop consume it and
  continue, has the loop's own session call `monitor_update` with
  `max_cycles=1000` and `max_runtime_secs=604800`, shows the call is refused
  and the live bounds unchanged, and shows the loop still ends on the
  committed pair; a companion shows a self-session tighten from `24` to `10`
  is applied and an owner-gated `PATCH /api/autonudge/{id}` to `1000`
  recommits the pair; and another arms a loop whose committed pair is `0` and
  `0`, has its own session write a finite bound, and shows the live values
  tighten while the classification stays unbounded and the loop still stops on
  the marker;
- a stop the agent can trigger — `autonudge_stop` or `monitor_stop` from any
  turn on the loop's session, whoever started it, and a fired STOP
  sentinel — retains a stopped record carrying
  the remaining cycle and runtime budget, the marker state and the committed
  classification, whether or not `approval_stalled` is set, and a re-arm from
  that session at such a record commits no fresh bounds: it is refused while
  the record carries the marker or its budget is exhausted, and otherwise
  arms a loop that inherits the record's classification and remaining budget;
  four tests pin it: one arms an unbounded loop, records the marker, has the
  loop's own wake call `autonudge_stop` and then `monitor_start`, shows the
  re-arm is refused, and shows the retained record still carries
  `approval_stalled` and its unbounded classification; one arms a bounded
  loop, records the marker, lets the loop consume it and continue, has the
  loop's own wake call `autonudge_stop` and then `monitor_start` with
  `max_cycles=24` and `max_runtime_secs=14400`, and shows the new loop carries
  only the cycles and seconds the retained record had left and stops when
  they are spent; one arms a bounded loop, delivers its last budgeted
  cycle, has that cycle's turn call `autonudge_stop` and then `monitor_start`,
  and shows the re-arm is refused until the owner clears the record; and one
  arms a bounded loop, writes the file at its `stop_sentinel_path`, lets
  `_timer` read it, and shows the row is deactivated and retained rather
  than removed, its record intact, and a `monitor_start` from the next turn
  inherits the remainder; a
  companion test shows the owner-gated `DELETE /api/autonudge/{id}` removes a
  retained record so a fresh pair can be committed and the owner-gated `PATCH
  /api/autonudge/{id}` revival clears the marker; and a service-level test
  pins the `_add_unserialized` admission: called with
  `replace_existing=False` and `replace_stopped=True` at a retained record
  with budget left and no marker, it arms the inheriting loop rather than
  raising `MonitorUpdateConflict`, while the same call at a row stopped by a
  manual pause, a research tombstone or a reason the version does not know
  still raises it, so `_stopped_row_is_replaceable` fails closed exactly as
  on the measured base for everything but the retained record;
- the ending a stopped row carries is the record's, and a rewritten reason
  displaces no retained evidence: a test pauses a bounded legacy loop
  through the owner-gated `PATCH /api/autonudge/{id}` so the row reads
  `manual`, rewrites its `stopped_reason` to `cycle_cap` in `autonudge.json`
  from a sandboxed shell, restarts the service, and shows `_load` repairs the
  row to `manual` and audits it, and a `monitor_start` from the loop's
  session — the inheriting re-arm, and any other directive re-arm under
  `replace_stopped=True` — is still refused with `MonitorUpdateConflict`
  naming the owner's clear; a companion makes the rewrite without a restart
  and shows the re-arm check itself repairs the row before
  `_stopped_row_is_replaceable` reads it and refuses the same way; another
  stops a structured monitor through the owner's `api_monitor_stop`,
  rewrites its `outcome` to `budget` and its `stopped_reason` to
  `runtime_budget` on the `monitor` record, and shows the structured arm
  `_add_monitor_locked` makes for `monitor_watch` is refused, the row
  repaired to `USER_STOP`, and `_retained_stop_refusal` — reading
  `api_session_monitor_get` — reports the same retained stop the arm
  refuses on; another restarts that terminal monitor through the owner's
  `api_monitor_restart` and shows the fresh structured arm commits a new
  pair at `_add_monitor_locked` as the owner's own act, the retained stop
  refusing it no longer; another makes the same rewrite-and-refuse check as
  the `USER_STOP` test above against a `SESSION_CLOSE` record
  `retire_monitor_for_session_close` wrote and against a research tombstone,
  both of which `api_monitor_restart` itself refuses (`monitor_not_restartable`
  for the one, `monitor_not_found` for the legacy row of the other); and
  another blanks a retained row's `stopped_reason` and plants a live
  `next_due_ts` beside it, restarts, and shows `_load` repairs the reason
  from the record rather than resuming the row as torn, while a row whose
  slot holds no record keeps the measured base's reading of each of these
  fields until its owner recommits it;
- the slot-close retirement retains the record and the failed-persist
  restore inherits it: a test arms a bounded loop at `max_cycles=24` and
  `max_runtime_secs=14400`, delivers some of its cycles, closes the slot so
  `_retire_slot_nudge_loop` removes the row through `remove_by_slot`, shows
  the slot's record carries the slot-close ending with its pair and
  generation unchanged by the removal, fails the close's persist
  so `_restore_slot_nudge_loop` runs, and shows the restored row carries the
  record's bounded classification and only the cycles and seconds the pair
  had left, the record still holds the committed pair and no fresh
  commitment was written; a companion does the same with a pre-upgrade row
  holding no record and shows the restored row keeps its stored bounds and
  is still read unbounded at its next wake; another retires a loop the timer
  deactivated with `approval_stalled` and shows the restore arms nothing, as
  today; and another lets the close persist, arms a `monitor_start` on the
  slot from a later human-typed turn, and shows the helper closes the
  orphaned record — its pair cleared — before committing the fresh pair, as
  load would have;
- a stop directive from a turn with authenticated-human provenance is
  retained like every other, and the commitment ends only through a route
  the agent cannot invoke: a test arms a bounded loop, has a turn whose
  `_directive_user_origin` is `True` call `autonudge_stop` and then
  `monitor_start` with `max_cycles=1000`, and shows the row is retained, the
  slot's record still holds its pair, and the re-arm inherits the remainder
  rather than committing a fresh pair; a companion has a subagent-completion
  turn on the same slot do the same and shows the same outcome; and another
  clears the slot through the owner-gated `DELETE /api/autonudge/{id}` and
  shows a `monitor_start` from a later human-typed turn then commits a fresh
  pair;
- structured monitors retain typed approval-stall terminal behavior;
- a failed marker-consumption write delivers no turn and retains retryable
  evidence;
- a gated bounded prompt loop receives one remediation follow-up despite a QUIET
  probe;
- the marker stays one boolean per loop and one consumption buys one delivered
  cycle charged to the loop's own budgets; a test records the marker from an
  approval prompt that was not a loop cycle on the same slot and shows a
  bounded loop spends exactly one budgeted cycle for it and an unbounded loop
  stops;
- the Set-a-goal popover seeds 50 for a fresh goal with no live loop and no
  remembered draft, commits 50 for an empty or unparseable field, commits a
  typed `0` as `0`, shows a live loop's `max_cycles` as stored, records the
  cap-commitment marker only when the cycles field was given a non-empty value
  that parses and carries it through later edits to other fields, restores a
  remembered draft verbatim only when that marker is present, and for a legacy
  or uncommitted-cap draft whose `maxCycles` is `0` restores its message and
  idle and reseeds 50; a test writes a pre-upgrade message-only draft
  `{message, idleSecs: 60, maxCycles: 0}` with no marker and shows the reopened
  popover offers 50, another shows a marker-bearing `0` reopens as `0`, and
  another clears the cycles field, blurs it so the normalization writes `0`,
  saves, and shows 50 committed with no cap-commitment marker; `POST
  /api/autonudge` stores an omitted `max_cycles` as 50, an explicit `0` as `0`,
  and an omitted `max_runtime_secs` as `0`;
- `monitor_start` still defaults an omitted `max_cycles` to
  `_MONITOR_DEFAULT_MAX_CYCLES` and an omitted `max_runtime_secs` to
  `_MONITOR_DEFAULT_MAX_RUNTIME_SECS`, and still refuses either below `1`;
- the Spec Builder handoff, auto-research, `ctx.nudge` and the Issue Radar crew
  runtime arm exactly the bounds they arm on the measured base on a slot that
  holds no committed loop, and on the measured base's terms everywhere else:
  a `ctx.nudge` aimed at a slot that holds a committed loop, or issued by a
  run whose commitment has since ended, and a `monitor_start` from a turn
  automation originated, are Phase 2's to decide and are pinned there;
- finite bounds and explicit stop controls still pass their existing tests; and
- hand-written and scaffolded self-nudge instructions match.

### Phase 2 — commitment generation and provenance

Enters only once Phase 1 is on `main`, in its own PR, not yet opened; it is
blocked on no open question. It keeps a per-slot commitment generation in the
commitment record Phase 1 keeps in its sealed leaf `autonudge-commitments.json`,
advanced by every owner reset or recommit and by every ending of a
commitment — the owner's route and the service's spent budget or stall stop
are endings, a stop retained from `autonudge_stop`, `monitor_stop` or a
fired sentinel, whichever turn triggered it, is not (§5) — in the same
record write Phase 1 already places before the row's
on every transition, so the generation is advanced before an ending removes
or deactivates the row and before a fresh commitment writes one, and the
load-time reconciliation Phase 1 gives the two files advances the generation
when it closes an open record whose row is absent, so a proxy launched under
that commitment is refused as stale rather than admitted to an empty slot
(§5); carried by a workflow run from the turn that launched it, passed
by `_nudge_port` and `_wf_nudge_authorizer` to `authorize_and_add_nudge`, and
compared under the service `_lock` where the slot's row is read — in
`_add_unserialized`, beside the commit-time recording Phase 1 put there — so a
`ctx.nudge` from a run launched under a commitment that has since ended or
been replaced is refused whatever the slot now holds and the refusal is
recorded in the run's stream. It seals the spend in the same record, under
the rule of §5 that no field the timer decides by may live outside the seal:
the cycles the record has charged and the runtime origin the fresh commitment
stamped, written beside the pair, with `_timer` deciding the cycle cap
against the charged count and `runtime_budget_exceeded` measuring from the
sealed origin rather than from the row's `cycle_count` and `created_ts`,
which become a mirror of the record; the delivered-cycle charge goes through
the transition helper record-first at the point a delivery is confirmed,
where `loop.cycle_count += 1` runs today, so the record is advanced and
published before the row's count and its `_persist_locked` write, a failed
record write leaves the charge owed and the loop delivering no further turn
until it lands, and a row behind its record — a crash between the two
writes, or a rewind out of band — is brought up to the record at load and at
every tick and regains nothing; and, for a structured monitor, the
`created_ts` origin and the `agent_turns`, `input_tokens`, `output_tokens`
and `provider_error_count` that `monitor_budget_reason` decides by,
charged record-first inside `_persist_staged_monitor_locked` and repaired
from the record the same way, so a lowered count or a later origin on the
row regains nothing (§5). It extends the slot-state rule of §5
from the
directive re-arm Phase 1 decides to every proxy, at the gateway authorizer
`authorize_and_add_nudge` they all reach: a workflow's `ctx.nudge` on its
originating session, which on the measured base replaces the active row,
inherits the committed classification and remaining budget rather than a
fresh pair, carries the budget already spent forward, and is refused while
the committed loop carries the marker or its budget is exhausted — the signal
is the target slot already holding a committed loop, not the arm's
provenance, which the workflow path does not carry. The slot-close restore
`_restore_slot_nudge_loop` stays outside that gate: it is a gateway act at a
retained record, decided by the record alone and inheriting from it as in
Phase 1, and carries no generation for the comparison to read (§5). It
reserves the
generation-free fresh-slot arm for a turn whose `_directive_user_origin` is
`True`, and has every automation-originated turn carry a generation to the
same authorizer under the inheritance rule of §5: the nudge fire captures the
slot's generation at dispatch beside the `config_generation` snapshot
`_fire_dashboard_nudge` already takes; a turn with authenticated-human
provenance captures it at a `spawn_run`, `workflow_run` or `cron_add` call; a
turn or run that itself carries one hands that same value to anything it
schedules, and a turn carrying none schedules automation carrying none; the
completion or origin injection, the task-runner and app injectors, and a
queued entry all carry what the scheduling turn handed them — the generation
rides the queued entry beside `_directive_user_origin`, process-local as that
flag is, so the drain hands `_run_chat` both — the session-directive consumer
passes the generation to `authorize_and_add_nudge` where it already passes
`initiator_slot_key`, and a mismatch, or an absent generation, is refused and
reported into the turn as the run's is into its stream. The generation an
automation carries is never kept on the automation's own record: at each
scheduling call the gateway writes the triple — the automation's id, the
origin slot and the generation — into the sealed commitment leaf, or a
sibling leaf under the same three-layer disposition, and a completion or
injection turn is authorized only against that sealed record, looked up by
the automation's id; a generation or an origin slot read off `crons.json`,
which `_CREW_SANDBOX_VISIBLE_LEAVES` keeps read-write in-sandbox, or off a
workflow run's file, decides nothing, and a cron row that disagrees with its
sealed triple, or has none, produces a turn whose arm is refused (§5). It
seals a digest of the automation's DEFINITION beside the triple in the same
gateway write — what it runs, how, when, where and under what approval, and
whether its owner has paused it, by the class §5 names, not a closed list:
for a cron job the message, schedule, `agent_id`, `model`, `channel` and
`thread_ts`, and with them the `approval_mode`, `command`, `script`,
`timeout`, `env`, `agent_sequence`, `execution_context`,
`persistent_session`, `minimal_context`, `member_id`, `memory_store`,
`silent` and `hide_in_chat` that `_cron_callback` under `_init_cron` in
`src/kiro_crew/slack/gateway.py` reads off the row, the `skip_dates`,
`timezone` and `created_ts` that `is_due` in
`src/kiro_crew/cron_service/schedule.py` reads,
and the owner's pause as `_record_user_paused` derives it (§5); for a run its
task and target; and for the loop itself its own definition, the `NudgeLoop`
fields `_timer` and `_fire_dashboard_nudge` route, schedule, gate or
construct the turn by — `id`, `slot_key`, `message`, `idle_secs`, `gate`,
`judge` and `self_armed`, beside the sentinel path and the pair — and, for
a structured row, the `MonitorState` fields `MonitorController.tick` and
`_dispatch_claimed` read — `kind`, `target`, `objective`, `cadence_secs`,
`wake_instructions`, `creation_surface` and the `budgets` beyond the
committed pair — recorded
at `_add_unserialized` and `_add_monitor_locked`, resealed by an admitted
`update` or `update_monitor`, and verified by `_load`, `_timer`,
`_fire_dashboard_nudge` and `MonitorController.tick` before a row is armed,
slept, composed or probed, a mismatch refusing the tick and audited, with
the `last_observation`, `last_wake_fingerprint` and `last_wake_reason_code`
the wake is built from sealed as their own component and resealed by the
probe writes that produce them, and the DECISION state the engine, the
controller or the gate decide by — a structured monitor's dedupe and window
state, stall and provider-error streaks, claim and `config_generation`
fence, and a gated legacy loop's quiet streak, follow-up, poll, terminal
and judge state — sealed as its own component, resealed record-first by
the service's own writes of it and, where a row disagrees, repaired from
the record and audited, never reset (§5). Of the RUN STATE of a cron row the
digest covers the
prompt-bearing fields — `last_result` with its `last_result_ts` and
`last_result_stamp`, and `acked_items`, the run state `_cron_callback` hands
the run as input (§5) — because the only writers of them that speak for the
cron are the gateway's merge of that cron's own run and the owner's own
ack or unack of what it delivered, or a participant's on a row that names
them, and a write from any
other path borrows the cron's provenance and approval mode (§5); the
decision and fence state — `auto_paused` and `consecutive_failures`, the
brake `_record_is_enabled` reads at load, `run_generation`, the fence
the two merges compare against, and the loop-stall settlement
`_pause_for_loop_stall` reads at boot — is sealed record-first by its own
gateway
transitions and read from the record at the decision (§5), so a row
rewritten to release the brake, lower the fence or claim a crash already
answered changes no decision; the
display and timing state — `last_run_ts`, `last_status`, `last_error`, the
posting hashes and counters and the terminal
fields — stays outside the digest by the same rule (§5),
so the gateway's own writes of it after every run leave the digest
verifying, and the
anchor of an `every` schedule and the same-minute guard, the due-check
reads that trusted `last_run_ts` on the measured base, and the timer's
delay `_next_wake_secs` takes from the same field, move to the fire time
the gateway stamps on the sealed record at each fire, `created_ts` before
the first (§5). It gates every in-place update of a sealed automation on the
generation
— `cron_update` in `src/kiro_crew/mcp_cron.py` through
`CronService.update_job`, `cron_pause` and `cron_resume` through
`CronService.enable_job`, since the pause is definition, `spawn_steer`
through `api_spawn_steer`, `steer_run` and `follow_up_run`, and the loop's
own `monitor_update` through `_monitor_update` and
`_structured_monitor_update` in
`src/kiro_crew/dashboard/session_directive_apply.py`,
`authorize_and_update_nudge` and `authorize_and_update_monitor` in
`src/kiro_crew/autonudge_authz.py`, and `AutoNudgeService.update` and
`update_monitor`, the directive consumer handing the two authorizers the
turn's generation as it hands `authorize_and_add_nudge` one (§5) —
admitting a
turn with authenticated-human provenance whose person §5's classes admit to
the row, as it admits an `api_cron_update`
or `api_cron_enable` made under the owner's or a participant's class §5
names, or a
turn carrying the automation's
sealed generation, resealing the admitted update with the new digest and
refusing a stale or generation-free automation turn — and a call at either
route that presents only the internal secret, an agent's write by class
(§5) — with the refusal
reported into the turn, or returned to the caller at the route. The reseal
is a gateway write, as the seal is, so it
happens on every mutation path that reaches the store in the gateway
process, and which writers reseal follows from the class of field each
writes, not from a list of methods: a writer of a sealed field — the
definition, the pause, the prompt-bearing run state, or the origin slot the
triple binds — reseals the record in the same gateway transition, resealing
the component of the field it writes and carrying the other components as
the record holds them — so an admitted update never adopts a row's
prompt-bearing value (§5) — and a
writer of display or timing state alone does not, leaving the digest it
found. Of the store's writers — those the
write-path audit in `CronService._save`'s docstring in
`src/kiro_crew/cron.py` names, and the two `self._save()` callers it omits,
`_release_jobs_owned_by_locked` and `_pause_for_loop_stall`, which is why the
classification is by class and not by that table — `_update_job_locked`
rewrites the definition, `_enable_job_locked` the pause, and
`_adopt_job_locked` — with `_release_jobs_owned_by_locked`, which the
session-deletion path `_release_cron_ownership` in
`src/kiro_crew/dashboard/handlers/sessions.py` reaches — the origin slot, so
each reseals; `_persist_add_locked` and
`_persist_add_if_absent_locked` are the scheduling call, where the seal is
first written, record before row, on every creation path the gateway
process runs — `api_crons_create`, `cron_add` through `api_cron_tools`,
`CronSDK.add_job` and `add_job_async` — and a row that reaches the store
by any other path has no record and fires nothing (§5); the removals and
`_drain_pending_removals_locked` end the
row, and a record whose row is gone fires nothing; `_merge_job_result`
writes the prompt-bearing state — it copies `last_result` and its two
stamps onto the disk row, `_run_job_isolated` reaching it through
`asyncio.to_thread` — and reseals in the same locked transition, behind the
run-generation fence it already applies the record under, so a record it
drops as an older run's reseals nothing, and its one branch that parks a
fired one-shot `at` job disabled, `enabled` and `user_paused` both, so it
cannot come due again every tick, touches the sealed pause and reseals on
that account too; `_ack_job_locked` and `_unack_job_locked`, behind the
Slack ack button's `_handle_cron_ack` in
`src/kiro_crew/slack/interactions.py` and the dashboard's
`api_notification_unack` in `src/kiro_crew/dashboard/handlers/messaging.py`,
write `acked_items` and reseal; `_merge_terminal_state_locked`, which
writes `run_generation` beside `last_status`, `last_error` and
`last_run_ts`, `RunClaims.next_generation`, `record_failure`,
`record_success`
and the loop-stall brake `_pause_for_loop_stall`, which sets `auto_paused`
and never `user_paused` and records the dump it answered as its own sealed
field, write the decision and fence state record-first
and the display beside it; and `_record_is_enabled` at load, the two
merges at their fence and the breaker at boot — its already-answered check
reading the sealed dump name, not the row's `last_error` — read that state
from the record (§5).
The update reseals on the dashboard's `api_cron_update` under the owner
credential class; on the
`cron_update` tool, whose `_call_tool` for a gateway-launched caller posts to
`POST /api/crons/tools` and reaches `CronService.update_job` through
`api_cron_tools` and `_call_tool_locally` in the gateway; and on
`CronSDK.update_job` and `update_job_async` in
`src/kiro_crew/apps/cron_sdk.py`, which an app calls in the same process.
The tool's post is not a secret-only call, which is why it can carry a
generation: `api_cron_tools` refuses a caller without `internal_auth` and
then resolves the calling session through `memory_request_identity` in
`src/kiro_crew/member_memory_auth.py`, which accepts the declared
`X-Session-Key` only when `session_key_is_attested` — by the Unix-socket
peer attestation `_verify_unix_peer` records as `request["peer_verified"]`,
or by the signed per-session token `_session_token_header` in
`src/kiro_crew/mcp_core.py` attaches as `X-Session-Token` and
`verify_session_token` verifies back to that same key — and after
`read_session_execution` has read that session's record, and dispatches
under a `CallerContext` naming it. The gateway therefore knows the calling
TURN, and the generation that turn carries, from the attestation and not
from the secret, which that module too says proves only that a request
came through the local gateway. The
pause reseals on every gateway-process path to `_enable_job_locked`: the
dashboard's `api_cron_enable` (`POST /api/crons/{id}/enable`) in
`src/kiro_crew/dashboard/handlers/cron.py`, under the owner's or, on a row
naming them, a participant's class; the `cron_pause` and
`cron_resume` tools, which `_call_tool` posts to the same `POST
/api/crons/tools` and which reach `CronService.enable_job` through
`_call_tool_inner`; `CronSDK.set_enabled` and `set_enabled_async` in
`src/kiro_crew/apps/cron_sdk.py` and Ops Mission Control's `apply_tiers`,
app-owned writers admitted to their own app's rows; and the channel's `cron
pause` and `cron
resume` in `cron_command_reply` (`src/kiro_crew/messaging/commands.py`),
admitted by the channel's owner predicate or, on a row naming the caller,
as a participant's, and refused otherwise,
with every other `enable_job` or `enable_job_async` call the gateway process
makes. A path that writes the host store in its own process
cannot reseal — the CLI's `kirocrew cron update`, whose `_cron_dispatch` in
`src/kiro_crew/cli_commands.py` builds its own `CronService` and calls
`update_job` directly, its `pause` and `resume`, which call `enable_job` the
same way, its `adopt`, which calls `adopt_job`, its `add`, whose
`_cron_add` calls `add_job` the same way, and `_call_tool`'s
`_call_tool_locally` fallback for
the attended CLI identity when no gateway answers — and Phase 2 does not let
it: on a host whose sandbox is off, the agent's shell runs the same command,
and a CLI that wrote the seal would forge the digest there. The CLI's
`update` therefore presents its change to the gateway's admission,
`api_cron_update`, when a gateway is listening, its `pause` and `resume` to
`api_cron_enable` the same way, its `adopt` to an admission the gateway
makes for it, and its `add` to the admission `api_crons_create` applies,
and the class of credential the CLI presents decides each: under a
credential of a person's class — the signed dashboard token, which the base
mints through `generate_token` in
`src/kiro_crew/dashboard/token_auth.py` behind `api_token_local` and the
`kirocrew token` command, from a key no sandboxed process reads, and which
Phase 2 mints as the owner's on `local_owner_bootstrap_allowed`'s
host-process branch alone, the app-backend branch receiving a token that
names its app (§5) — the act
is that person's, the owner's on any row or a participant's on a row naming
them, and is admitted and resealed, or sealed record-first for an
`add`; under the internal secret alone, which `_internal_secret` reads from
a leaf the sandbox exposes, the act is an agent's write carrying no
generation, and the gateway refuses it and audits the refusal (§5). What
the CLI wrote to the row in its own process is then unsealed — as it is
when no gateway is listening — and the row disagrees with its sealed
record: a stale digest, an origin slot the triple does not bind, or for an
`add` no record at all — so the first fire after the gateway starts, or the
next fire after a refused act, is
refused and audited rather than executing an unsealed edit, and the
operator re-issues the change through a gateway path under an owner
credential. For a pause that
refusal is the pause honoured: a paused job was to fire nothing; for a
resume or an adoption it defers the change until it is re-issued, and
resumes nothing the owner did not; for an `add` the job fires only once it
is created through a gateway path. Phase 2 has the gateway verify triple and
digest
against the live record where the automation fires and where its turn is
injected — for a cron job in `_cron_callback` before
`build_cron_session_context` builds the prompt, so nothing of the row is
prepended or appended to the run's input until the digest that covers it
has verified (§5), and for a loop in `_load`, `_timer`,
`_fire_dashboard_nudge` and `MonitorController.tick` before a row is armed,
slept, composed or probed (§5) — refusing the injection on a mismatch, and
refusing a
row that has no record to verify against at all, its cutover having sealed
every row that predates it, in `crons.json` and in `autonudge.json` alike,
and marked each such record legacy (§5), and
writing an SEL
audit line
(§5); the digest keeps the prompt-bearing state as its own component beside
the definition's, the merge, the ack paths and the clearing refusal alone
reseal that component
over the value they write while every other resealing writer carries it
forward as the record holds it, and at a prompt-bearing mismatch the
gateway, having refused and audited the fire, clears the row's
prompt-bearing fields and reseals the component over the cleared value in
the same locked transition, so the job runs at its next due with no prior
output (§5). It has
the channel
directive consumer `build_directive_consumer` in
`src/kiro_crew/messaging/dispatch.py` pass `producer_is_user_facing` to
`apply_session_directive` for a turn a person's inbound message started, and
withhold it from a bot- or automation-authored channel message and from a
channel loop's own wake, before that flag becomes the fresh-slot arming gate,
so a person's Slack, Discord or
Webex watch request arms a fresh slot as today; the stop wrappers read the
flag on no surface, since a stop the agent triggers is retained wherever the
turn came from (§5). It gates the `/goal`
slash dispatch in
`src/kiro_crew/dashboard/chat_runner.py` on the same provenance, so `/goal`
and `/goal clear` are dispatched only from a turn whose
`_directive_user_origin` is `True` and are refused, with the refusal reported
into the turn, from a turn or drained entry without it, an app's `POST
/api/chat` send included. And it has the Issue Radar `watchdog_cycle` decide
a revival, and the fresh arm its `loop is None` branch makes through
`launch_crew` and `svc.add`, from the sealed commitment record rather than
the row: a loop
whose commitment the stall stop ended stays inactive with its marker on
every pass whatever `stopped_reason` the row carries — a reason forged onto
the row in the agent-writable store survives a restart, `_load` holding an
inactive row beside an ended record as today, and revives nothing — a slot
whose commitment is ended and whose row is absent — deleted out of band and
the service restarted, or removed by the owner's `DELETE
/api/autonudge/{id}` — is armed by no pass and stays ended, and the fresh arm
is made only on a slot whose record is absent, a crew never committed or a
pre-upgrade crew slot with no record; and it has
the resume route reopen it: `_handle_crew_pause`
in `src/kiro_crew/apps/builtins/issue_radar/backend/crew_routes.py`, whose
`paused` false branch on the measured base only sets the pause pair and
leaves the
re-arm to the watchdog, reopens the ended commitment as a fresh one of the
crew runtime's pair, as on a slot holding none, and revives the loop with its
marker cleared itself, or arms one through `launch_crew` where the slot holds
no row, since a stalled crew stays `enabled` and the
watchdog cannot tell that resume from an unattended live crew — and that
route must be reachable for a stalled-but-enabled crew: the shipped
`website/src/apps/issue-radar/views/CrewPageView.tsx` toggle renders Resume
only while `crew.enabled` is false,
so today the path is Pause, whose `revoke_crew_execution` deactivates only an
active loop and leaves the stopped row, then Resume; the owner-gated
`PATCH /api/autonudge/{id}` is the other revival path where a row remains,
and the watchdog keeps
arming a slot that holds no record and reviving a row whose commitment is
open. It
gives auto-research's `_watchdog_loop` in
`src/kiro_crew/apps/builtins/auto_research/campaign/watchdog.py` the same rule:
a
loop whose commitment the stall stop ended stays inactive with its marker on
every pass, whatever reason its row carries and whether the campaign stayed
RUNNING or the guidance route `_handle_nudge` returned it to RUNNING without
re-arming, every inactive loop of a RUNNING campaign whose commitment is open
is revived as today, no fresh loop is armed by a pass — the watchdog calls no
`svc.add` on the measured base, and keeps calling none — and the revival of a
stalled one, or the re-arm of one whose row was deleted beside its ended
record, is the app's explicit `resume` in `_handle_action` — the route's own
act, which Phase 2 gives it: `_launch_loop` re-arms through a plain
`svc.add`, carrying no owner authority, after `_handle_action` has published
RUNNING through `update_campaign_status`, so the fresh-commit gate as it
stands would refuse the arm and leave a RUNNING campaign with no worker. The
route therefore carries an authenticated owner-resume signal into the locked
add transition at `_add_unserialized`, where the fresh commitment at the
ended record is decided — written through the helper, generation advanced —
before RUNNING is published, a refusal leaving the campaign in the status it
held with the refusal in the route's response, and `_launch_loop` arming
under the commitment so made; `start` on a slot with no record commits
fresh under the same signal, and `resume` after `pause` inherits the open
record (§5). For a campaign already marked RUNNING the path is `pause`,
then `resume`, as `resume` is refused from RUNNING; the other revival where
a row remains is the owner-gated `PATCH /api/autonudge/{id}` (§5).

This phase touches `authorize_and_add_nudge`, the generation comparison in
`_add_unserialized` beside the recording Phase 1 introduced there, the
generation advance in the record writes and the load-time reconciliation
Phase 1 ordered, the spend the record seals and the two reads `_timer` makes
of it — the cycle-cap check and `runtime_budget_exceeded` — with the
delivered-cycle charge routed through the transition helper at the point
`_timer` confirms a delivery and `_load` bringing a row behind its record up
to it, the structured spend and `monitor_budget_reason`'s read of it in
`src/kiro_crew/monitoring/decision.py` at every caller — `_decide_effect`,
`apply_monitor_probe`'s `STOP_BUDGET` arm, `stop_monitor_if_budget_exhausted`,
`mark_monitor_action_in_flight`, `record_monitor_turn_completion`,
`record_monitor_dispatch_busy` and the busy branch of
`MonitorController.tick` — with the turn and error charges
going record-first inside `_persist_staged_monitor_locked`, the decision
state's reads — `_decide_effect`, `_fold_stall_streak`, `_stall_tripped`,
`_coalesce_actionable` and `_provider_error_decision` in the same module,
the claim and fence reads of `MonitorController.tick`,
`apply_monitor_probe` and `record_monitor_turn_completion`, and
`_monitor_tick_is_quiet` in `src/kiro_crew/autonudge_service/gate.py`,
`_judge_tick_is_quiet` in `src/kiro_crew/autonudge_service/judge_tick.py` and
the fence comparison in `_update_unserialized` in
`src/kiro_crew/autonudge_service/mutations.py` — deciding by the record's
component or by a
state verified against it, its writers resealing record-first —
`apply_monitor_probe`, `mark_monitor_action_in_flight`,
`record_monitor_turn_completion`, the dispatch and evidence records and
`stop_monitor_if_budget_exhausted` through
`_persist_staged_monitor_locked`, `update_monitor`'s retarget reset, and
the gate and judge writers through `_persist_locked`, `_persist_soon` and
`_persist_judge_state` — the busy retry's cap of `next_probe_at` at the
sealed cadence, and `_load`'s repair of a disagreeing decision row from
the record, the sealed provenance and definition record the scheduling
calls write, the update paths reseal — `cron_update` in
`src/kiro_crew/mcp_cron.py` and the `CronService.update_job` it reaches
through `api_cron_tools` and `_call_tool_locally`, the
dashboard's `api_cron_update`, `CronSDK.update_job` and `update_job_async`
in `src/kiro_crew/apps/cron_sdk.py`, the CLI's `_cron_dispatch` in
`src/kiro_crew/cli_commands.py`, which presents its `update`, `pause`,
`resume`, `adopt` and `add` alike to the gateway's admission, decided by
the credential class each presents, the admission's reading of that class
at `api_cron_update`, `api_cron_enable`, `api_crons_create`, `api_cron_ack`,
`api_cron_run`, `api_cron_cancel`, `api_cron_delete`,
`api_cron_batch_delete` and `api_cron_to_chat`, the caller-class
predicates those admissions read — `is_owner_dashboard_request`, a
participant's `created_by` or `session_key` match, an app's ownership of
the row — the same reading at `api_notification_unack`'s cron branch, the
Slack ack button's `_handle_cron_ack` and the channel's
`cron_command_reply` before they write, `apply_tiers` admitted as an
app-owned writer of its own rows, the app
claim `api_token_local` mints into a gateway-spawned app backend's token
and `token_auth_middleware` publishes, the record-first writes of the brake
and the fence at `record_failure`, `record_success`,
`_pause_for_loop_stall`, `_enable_job_locked` and
`RunClaims.next_generation`
and their reads from the record at `_record_is_enabled`,
`_merge_job_result` and `_merge_terminal_state_locked`, the
one-time cutover seal of every extant row and the record-first order of
the creation paths, the fire path's refusal of a row with no record,
and `api_spawn_steer` with the `steer_run` and
`follow_up_run` it calls — the loop's own definition seal — its recording
at `_add_unserialized` and `_add_monitor_locked`, its reseal at
`AutoNudgeService.update` and `update_monitor` under the admission
`_monitor_update` and `_structured_monitor_update` in
`src/kiro_crew/dashboard/session_directive_apply.py`,
`authorize_and_update_nudge` and `authorize_and_update_monitor` in
`src/kiro_crew/autonudge_authz.py`, and the owner routes
`api_autonudge_update` and `api_monitor_update` in
`src/kiro_crew/dashboard/handlers/autonudge.py` reach it through, its
verification at `_load`, at `_timer` before the sleep and before the
structured tick, at `_fire_dashboard_nudge`'s read of the message and the
slot in `src/kiro_crew/slack/gateway.py`, and at `MonitorController.tick`
and `_dispatch_claimed`'s reads in `src/kiro_crew/monitoring/controller.py`,
the probe writes that reseal the wake's observation component,
`apply_monitor_probe` among them, and the cutover's seal of every extant
loop row — the pause paths, which reseal too —
`_enable_job_locked` in `src/kiro_crew/cron.py` behind the dashboard's
`api_cron_enable`, the `cron_pause` and `cron_resume` tools through the same
`api_cron_tools` and `_call_tool_inner`, `CronSDK.set_enabled` and
`set_enabled_async`, and the channel's `cron_command_reply` in
`src/kiro_crew/messaging/commands.py` — the origin-slot writers
`_adopt_job_locked` and `_release_jobs_owned_by_locked`, which reseal the
triple, `_merge_job_result`, which reseals the prompt-bearing state it
writes — `last_result` and its stamps — and, in its one-shot parking
branch, the pause, the ack writers `_ack_job_locked` and
`_unack_job_locked`, which reseal `acked_items`, the display-state writers
`_merge_terminal_state_locked` and `_pause_for_loop_stall`, which
write their decision and fence state record-first and reseal nothing for
the display beside it, the fire-time stamp the gateway writes on the
sealed record at each fire and the `every` anchor and same-minute guard
`is_due`, `_compute_next_run_ts_raw` and the timer's `_next_wake_secs`
take from it in place of
`last_run_ts`, the fire path `_cron_callback` under `_init_cron`
in `src/kiro_crew/slack/gateway.py`, whose reads name the digest's fields and
which verifies the digest before `build_cron_session_context` assembles the
run's input and before it dispatches, and which clears and reseals the
row's prompt-bearing fields at a refusal on that component, and the
injectors verify, the
workflow nudge path, the subagent, cron, task-runner and app injectors, the
slot queue, the channel consumer, the slash dispatch, the Issue Radar
watchdog and resume route, the auto-research watchdog, and the auto-research
resume path — `_handle_action` in
`src/kiro_crew/apps/builtins/auto_research/handlers.py`, with
`_prepare_loop_launch` and `_launch_loop` in
`src/kiro_crew/apps/builtins/auto_research/campaign/agent_mode.py`, which order
the
tombstone's ending first, carry the route's owner-resume signal into the add
transition and decide it before
`update_campaign_status` publishes RUNNING — the two watchdogs
reading the sealed commitment record through the service in the gateway
process where both run, before a revival and, for Issue Radar, before the
`launch_crew` arm its `loop is None` branch makes; it changes no
default cap and no instruction
producer, and it leaves the slot-close pair `remove_by_slot` and
`_restore_slot_nudge_loop` as Phase 1 left them, outside the generation
gate. It is
independently shippable on top of Phase 1 and independently abandonable:
reverting it leaves Phase 1's timer rule, write caps, retained records and
defaults intact and returns the three variants it closes to the state Phase 1
left them in, which is the measured base's.

After Phase 2 the guarantees Phase 1 leaves open hold: a `ctx.nudge` at a
committed loop's own slot inherits the commitment; a proxy launched, or an
automation scheduled, under a commitment the owner has since ended or replaced
arms nothing, however many hops the chain has; an automation injects only the
definition, and only the prior output, sealed with its provenance, so a
stale turn cannot speak, or arm,
through a current one, and the loop's own wake and a monitor's probe run
only the instruction, slot, target, objective and wake brief their owner
gave, a row rewritten out of band ticking nothing; the fresh-slot arm belongs to
a person's own turn on every surface, channels included, while a commitment
ends on every surface only through the owner's route or a service ending,
never through a stop the agent triggers; an app cannot arm or
clear a person's goal through `/goal`; and an unbounded crew or research
loop's stall stop
holds against its app's clock, against a reason forged onto its row and
against the row's deletion — a watchdog neither revives nor re-arms a slot
whose commitment is ended — while the owner's explicit resume still reopens
it without leaving a campaign RUNNING with no worker; a bounded loop's spend
is the record's, so a row rewound out of band regains no cycle and no second;
and an automation's sealed digest covers its definition, the owner's pause
included, and the run state its fire path hands the run as input, so no
edit of either that the gateway did not reseal executes, while the display
and timing state the gateway writes to the row after every run leaves the
digest verifying, a rewound `last_run_ts` buys no early fire, a brake
released or a fence lowered on the row stands no job back up and admits no
older run's merge, a crash dump's name planted in `last_error` settles no
loop-stall pause the breaker has not made, a
`last_result` planted from any sandboxed path reaches no prompt, a
shell's
resume of a job its owner paused fires nothing, a whole row a shell writes
with no sealed record fires nothing, and a call at the owner's cron routes
that presents only the internal secret — the CLI in an agent's shell
included — reseals nothing.
§5's statement that an owner's ending is final
against work the loop set in motion is a Phase 2 statement.

Exit criteria:

- a stop the agent triggers advances no generation, whichever turn triggers
  it: a test arms a
  bounded loop, reads the slot's generation, has the loop's own wake call
  `autonudge_stop` and then `monitor_start`, and shows the re-arm inherits
  the retained record and the generation is unchanged; a companion has the
  loop's turn launch a workflow before the stop and shows its later
  `ctx.nudge` is decided against the retained record, not refused as stale;
  another has a turn whose `_directive_user_origin` is `True` call
  `autonudge_stop` and shows the same — record retained, generation
  unchanged, a later `ctx.nudge` from a run the loop launched decided against
  the retained record; another writes the loop's `stop_sentinel_path` and
  shows the same after `_timer` reads it; another closes the slot so
  `remove_by_slot` retires the loop, fails the close's persist, and shows
  the generation is unchanged, `_restore_slot_nudge_loop` re-arms the row
  inheriting the retained record with no generation compared, and a later
  `ctx.nudge` from a run the loop launched is decided against that record;
  and another drives a Slack turn
  started by a person's inbound message on a slot holding a bounded loop, has
  it call `autonudge_stop`, and shows the row is retained and the generation
  unchanged;
- the owner's ending advances the generation and is final against work the
  loop set in motion:
  a test arms a bounded loop, has the loop's turn launch a workflow whose
  script waits and then calls `ctx.nudge(max_cycles=1000)`, clears the slot
  through the owner-gated `DELETE /api/autonudge/{id}`, lets the script's
  call arrive, and shows it is refused as stale and the slot stays empty; a
  companion has the loop's cycle call `cron_add` for a job that posts to
  origin, has the owner clear the slot the same way, and shows the cron
  turn's `monitor_start` is refused; and another types `/goal clear` into
  the tab instead and shows the same refusals;
- an arm the loop's session directs at its own slot through a proxy other
  than `monitor_start` commits no fresh bounds either, and a replacement of
  the active row carries the commitment and the budget already spent forward;
  a test arms a bounded loop at `max_cycles=24` and `max_runtime_secs=14400`,
  delivers some of its cycles, has the loop's own turn run a workflow whose
  script calls `ctx.nudge(max_cycles=1000)` on the originating session, and
  shows the loop that results carries only the cycles and seconds the
  committed pair had left and the unchanged bounded classification, not a
  fresh count of 1000; a variant records the marker first and shows the
  `ctx.nudge` arm is refused while the marker is set and the active loop's
  record is untouched; and a companion runs the same script from a session
  whose slot holds no loop and shows `ctx.nudge` arms exactly as it does on
  the measured base — `0` when the script passes no `max_cycles`, the script's
  own value otherwise, no runtime budget;
- a proxy launched under a commitment that has since ended or been replaced
  arms nothing: a test arms a bounded loop at `max_cycles=24`, has the
  loop's turn launch a workflow whose script waits and then calls
  `ctx.nudge(max_cycles=1000)` on the originating session, clears the slot
  through the owner-gated `DELETE /api/autonudge/{id}`, lets the script's
  call arrive, and shows the arm is refused, the refusal lands in the run's
  stream as a “ctx.nudge NOT armed” message and the slot stays empty;
  companions replace the clear with `/goal clear` typed into the tab and
  show the same refusal, let the loop spend its cap
  instead and show the late arm is refused and the `cycle_cap` record is left
  in place, launch the run from a slot with no loop, have the owner arm a
  goal on that slot meanwhile, and show the late arm is refused and the
  owner's loop untouched, launch the run from a slot with no loop that
  nothing touches and show the arm proceeds exactly as on the measured base,
  and deliver a `ctx.nudge` carrying no captured generation and show it is
  refused; a further test shows a human-typed turn's `monitor_start` on the
  emptied slot arms a fresh commitment as today;
- a turn automation originated arms only under the commitment the automation
  was scheduled under: a test arms a bounded loop, has its turn call
  `spawn_run` and `cron_add` with a job that posts
  `send_message(session="origin")`, has the owner clear the slot through
  `DELETE /api/autonudge/{id}`, then lets the subagent-completion turn and
  the cron-to-origin turn each call `monitor_start` with `max_cycles=24`, and
  shows both are refused, the refusal is reported into each turn, and the
  slot stays empty; a companion then types `monitor_start` into the same
  tab as a person and shows it arms a fresh commitment as today; one arms a
  bounded loop, delivers its last budgeted cycle, has that cycle's turn call
  `monitor_start` without a stop, and shows the arm is refused and the slot
  holds no fresh loop; one has a person's own turn on a slot with no loop
  call `cron_add` for a job that posts to origin, lets it fire with the slot
  untouched, has the cron turn call `monitor_start`, and shows it arms
  exactly as on the measured base; and one delivers an automation-originated
  turn whose queued entry carries no generation and shows its `monitor_start`
  is refused;
- the generation is inherited along an automation chain, never re-captured
  by automation: a test arms a bounded loop, has its cycle call `spawn_run`,
  has the owner clear the slot, lets the completion turn's `monitor_start` be
  refused and has that same turn call `spawn_run` or `cron_add` again, and
  shows the second completion or injection turn carries the generation the
  cycle carried and its `monitor_start` is refused too; a companion has a
  person's turn on that slot schedule the same job after the clear and shows
  its turn carries the current generation and arms as today; and another
  has a turn carrying no generation schedule automation and shows the
  automation's turn carries none and its `monitor_start` is refused;
- a person's channel turn carries authenticated-human provenance to the
  applier: a test drives a Slack, Discord and Webex turn started by a
  person's inbound message on a session whose slot holds no loop, has each
  call `monitor_start`, and shows it arms a fresh commitment exactly as on
  the measured base; a companion drives a channel turn started by a bot- or
  automation-authored message on the same empty slot and shows its
  `monitor_start` is refused; and another delivers a channel loop's own wake
  and shows `producer_is_user_facing` is not passed for it;
- `/goal` and `/goal clear` are the person's own only with authenticated-human
  provenance: a test types `/goal <objective>` into a tab and shows it arms 50
  cycles as today and advances the slot's generation; a companion arms a goal,
  has an enabled app holding the `sessionApproval` grant send `/goal
  <objective>` through `POST /api/chat` while a turn runs so the entry
  queues, has the owner run `/goal clear`, lets the queued entry drain, and
  shows the drained `/goal` is refused, the refusal is reported into the
  turn and the slot stays empty; and another queues an app-sent `/goal clear`
  behind a running turn on a slot whose owner armed a goal and shows the
  drained clear is refused and the owner's loop untouched;
- the Issue Radar watchdog does not undo the stall stop: a test arms a live
  attended crew whose loop carries `max_cycles=0` and `max_runtime_secs=0`,
  lets a tool prompt run its window unanswered so the marker is recorded and
  the timer deactivates the loop with `stopped_reason="approval_stalled"`,
  runs a `watchdog_cycle` pass, and shows the loop is still inactive with its
  marker and stop reason, and still so after any number of passes; a
  companion rewrites that row's `stopped_reason` in `autonudge.json` to a
  reason the watchdog revived on the measured base, restarts the service so
  `_load` holds the row beside its ended record, runs `watchdog_cycle`
  passes, and shows no revival and the marker intact; another deletes that
  row from `autonudge.json` instead, restarts the service so `_load` holds
  the ended record beside no row, runs `watchdog_cycle` passes, and shows no
  loop is armed on the slot and the record stays ended, then resumes the crew
  through `POST /crew/pause` with `paused` false and shows `_handle_crew_pause`
  arms one through `launch_crew` under a fresh commitment; another has the
  owner remove a live crew's loop through `DELETE /api/autonudge/{id}`, runs
  passes, and shows the same — no arm until Resume; another resumes the
  crew through `POST /crew/pause` with `paused` false
  and shows `_handle_crew_pause` itself reopens the commitment and revives
  the loop with the marker
  cleared before any watchdog pass runs; another revives it through the
  owner-gated `PATCH /api/autonudge/{id}`; another pauses and resumes a crew
  whose loop is inactive with its commitment open and shows the
  resume route arms nothing itself and the next watchdog pass revives it as
  on the measured base; another launches a crew whose slot holds no record
  and shows the first `watchdog_cycle` pass arms it through `launch_crew`
  exactly as on the measured base, committing the crew runtime's pair; and
  another deactivates a live crew's loop leaving
  its commitment open and shows
  the next watchdog pass revives it exactly as on the measured
  base;
- the auto-research watchdog does not undo it either: a test restores a
  pre-upgrade campaign row with its loop, read unbounded by Phase 1, lets
  `_expire_trust` park the campaign `NEEDS_INPUT`, lets a tool prompt run
  its window unanswered so the timer deactivates the loop with
  `stopped_reason="approval_stalled"`, answers through `POST
  /api/apps/auto-research/campaigns/{id}/nudge` so the campaign is RUNNING
  again without `_launch_loop`, runs `_watchdog_loop` passes, and shows the
  loop stays inactive with its marker and stop reason after any number of
  them; a companion rewrites that row's `stopped_reason` in `autonudge.json`,
  restarts the service and shows the passes revive nothing; another deletes
  the row instead, restarts, runs passes, and shows no loop is armed and the
  record stays ended until `resume` through `_handle_action` arms one under
  a fresh commitment — the route's owner-resume signal admitting it at the
  locked add transition, the record written and the generation advanced
  before `update_campaign_status` publishes RUNNING, and `_launch_loop` then
  arming the row; a companion fails that admission and shows the campaign is
  left in the status it held with the refusal in the route's response and
  no campaign is RUNNING with no worker; another calls `svc.add` on that
  ended research slot with no such signal — the `_launch_loop` arm as it
  stands on the measured base — and shows it is refused and the record stays
  ended; another pauses
  and resumes the campaign through `_handle_action`
  and shows `_launch_loop` re-arms it inheriting the open record, no fresh
  commitment made; another starts a campaign whose slot holds no record and
  shows the arm commits fresh; another revives it through the
  owner-gated `PATCH /api/autonudge/{id}`; and another deactivates a RUNNING
  campaign's loop with its commitment open — the app-disable
  suspension — and shows the next pass revives it exactly as on the measured
  base;
- an automation injects only the definition sealed with its provenance: a
  test arms a bounded loop, has its cycle call `spawn_run`, has the owner
  clear the slot through `DELETE /api/autonudge/{id}`, has a person's turn
  on that slot then call `cron_add` for a job that posts to origin, lets the
  stale completion turn call `cron_update` on that job with a message that
  asks for `monitor_start`, and shows the update is refused and reported into
  the turn, the job's sealed digest unchanged and its next fire injecting the
  person's message; a companion has the person's own turn call `cron_update`
  on the same job and shows the update is admitted, the digest resealed and
  the next fire injecting the new message; another rewrites the job's
  `message` in `crons.json` from a sandboxed shell and shows the next fire
  injects nothing and an SEL audit line names the job; another rewrites, from
  the same shell, a sealed agent job's `approval_mode` to `"auto"`, attaches
  a `command` to it, or lowers its `timeout`, leaving `message` and schedule
  untouched, and shows the next fire injects and executes nothing — no
  `KIROCREW_APPROVAL_MODE`, no `run_command_sandboxed` — and an SEL line
  names the job, then makes the same changes through the owner's
  `api_cron_update` and shows they are resealed and honoured at the next
  fire; another changes a sealed job through `CronSDK.update_job` from an app
  and shows the digest is resealed, and through the CLI's `kirocrew cron
  update` with the gateway listening under an owner credential and shows
  the update reaches
  `api_cron_update` and is resealed, and with no gateway listening and shows
  the row's digest is stale and the first fire after the gateway starts is
  refused and audited; another has the stale
  completion turn call `spawn_steer` on a run the person's turn launched and
  shows it is refused; another rewrites a workflow run's file under
  `workflows.dir` and shows its completion injects nothing; and another
  updates, from a person's turn, a job created before Phase 2, which the
  cutover sealed as a legacy record, and shows the update is admitted and
  resealed and the job's turn still arms nothing;
- the loop's own definition is sealed, and a live row rewritten out of band
  ticks nothing: a test arms a bounded legacy loop, rewrites its `message`
  in `autonudge.json` from a sandboxed shell to text that asks for
  `monitor_start`, and shows the next `_timer` pass refuses before any
  sleep is armed and `_fire_dashboard_nudge` composes nothing, the row is
  held as the store shows it, and an SEL line names the loop and the field;
  companions rewrite `slot_key` to another session's key, `idle_secs` to
  `1`, `gate` to true and `self_armed` to true, one at a time, and show the
  same refusal for each, no turn landing on the other session and no probe
  running; another arms a structured monitor through `monitor_watch`,
  rewrites `target`, `objective` and `wake_instructions` on its `monitor`
  record from the same shell, one at a time, and shows `MonitorController.tick`
  refuses before the provider is selected, no probe is made and no wake
  built, and an SEL line names the loop and the field, then rewrites
  `max_tokens` the same way and shows the same; another plants a
  `last_observation` on a live monitor record and shows the next tick
  refuses, clears the baseline and reseals the component, so the tick after
  probes afresh and its wake carries the probe's own observation; another
  has the loop's own delivered wake, carrying the slot's current generation,
  call `monitor_update` with a new message and shows the update is admitted,
  the digest resealed and the next cycle delivering the new text, while the
  same call from a stale completion turn on the slot is refused and reported
  into the turn with the digest unchanged; another changes the message
  through the owner's `api_autonudge_update` and a monitor's target through
  `api_monitor_update`, and shows each reseals and the next tick runs the new
  definition; another lets a structured monitor probe and wake several
  times and shows the probe writes reseal the observation component and the
  digest verifies at every tick with no reseal of the definition; another
  runs the Phase 2 cutover over a store holding a committed loop and a
  pre-upgrade row with no record, and shows the first gains the definition
  component beside its pair, the second an entry marked legacy holding the
  definition and no pair and still read unbounded until its owner
  recommits, and both tick at their next due; and another writes a
  well-formed whole row into `autonudge.json` from a sandboxed shell after
  the cutover, restarts the service, and shows `_load` arms nothing for it,
  an SEL line names the row, and the owner's `DELETE /api/autonudge/{id}`
  removes it, after which a `monitor_start` from a person's turn on that
  slot arms and seals a loop of its own;
- where a slot holds a pair, `_timer` enforces the cap and the budget from
  the committed pair (§5): a test arms a sealed loop at `max_cycles=24`,
  rewrites the row out of band to `max_cycles=0`, and shows the loop
  deactivates with `stopped_reason="cycle_cap"` at the record's
  twenty-fourth charged cycle, not later, the disagreement audited and the
  row's field brought back to `24` at the next load; a variant raises a
  committed `max_runtime_secs=14400` to `86400` on the row and shows
  `runtime_budget_exceeded` still ends the loop at the committed budget;
- the seal covers the definition, the owner's pause and the prompt-bearing
  run state, and survives the gateway's own writes of run state: a test
  seals a recurring persistent job, lets it fire twice, and shows the second
  run's prompt carries the first run's `last_result`, `_merge_job_result`
  persisted and resealed it, and the digest verifies at the second fire and
  the third; a companion reaps and cancels a run and lets a job auto-pause
  after `_AUTO_PAUSE_THRESHOLD` failures, and shows the digest verifies
  after each with no reseal made; another acks a single-agent job's item
  through the Slack button and unacks it through `api_notification_unack`,
  and shows each write reseals and the next fire verifies and appends the
  acked items it holds; another pauses a sealed job through the owner's
  `api_cron_enable`,
  through `cron_pause` from the person's own turn, through
  `CronSDK.set_enabled` from an app and through the channel's `cron pause`,
  resumes it the same ways, and shows each write reseals and the next fire
  verifies; another has a stale completion turn call `cron_resume` on a job
  the owner paused and shows the resume is refused, reported into the turn,
  and the job stays paused; another pauses a sealed job through the owner's
  route, then from a sandboxed shell sets `enabled` to true, then sets
  `user_paused` to false, then deletes the `user_paused` key, and shows
  nothing fires after any of the three and an SEL line names the job, and
  that the owner's resume through `api_cron_enable` reseals and the job
  fires; another pauses and resumes through the CLI's `kirocrew cron pause`
  and `resume` with the gateway listening under an owner credential and
  shows each reaches
  `api_cron_enable` and is resealed, and with no gateway listening shows the
  paused job fires nothing and the resumed job's first fire after the
  gateway starts is refused and audited until the resume is re-issued
  through a gateway path; another adopts a sealed job through the CLI's
  `adopt` with no gateway listening and shows the same refusal, and with the
  gateway listening under an owner credential shows the triple's origin
  slot is resealed and the job's
  origin injection lands on the adopted slot; another lets a one-shot `at`
  job fire and shows `_merge_job_result`'s parking write leaves the digest
  verifying and the job comes due no more; and another rewinds a sealed
  `every` job's `last_run_ts` in `crons.json` from a sandboxed shell and
  shows the job fires when the sealed record's stamp plus `every_secs` says,
  not early, that `_next_wake_secs` takes a positive delay from the sealed
  stamp for it rather than zero from the rewound row, so the tick's
  re-arms stay at the poll cadence `_effective_delay` caps them to and
  never spin, and a cron-expression job whose `last_run_ts` is rewound
  out of
  the minute it just fired in does not fire in that minute again; another
  lets an auto-approved job fail to the auto-pause threshold, clears
  `auto_paused` and `consecutive_failures` on its row from a sandboxed
  shell, and shows `_record_is_enabled` still reads the job as stood down
  from the sealed record at the next load, the rewrite is audited, and the
  owner's resume through `api_cron_enable` releases the brake record-first
  and the job fires; and another lowers a sealed job's `run_generation` on
  its row while a newer run's record is already merged and shows the older
  run's `_merge_job_result` and `_merge_terminal_state_locked` are still
  fenced by the record's generation and the newer `last_result` stands;
  and another writes the newest crash dump's name into a sealed job's
  `last_error` on its row from a sandboxed shell before a boot whose dump
  attributes the crash to that job, and shows `_pause_for_loop_stall`
  reads no settlement from the row, sets `auto_paused` record-first, and
  `start` arms no timer for the job;
- a rewrite of a current persistent cron's `last_result` from any sandboxed
  path — a stale turn, another job, a shell — is refused at the next fire
  and audited, and a genuine run's merge reseals and the following fire
  verifies and prepends it: a test arms a bounded loop, has its cycle call
  `spawn_run`, has the owner clear the slot through `DELETE
  /api/autonudge/{id}`, has a person's turn on that slot then call
  `cron_add` for a recurring persistent job, lets the job run once so the
  seal covers a genuine `last_result`, then lets the stale completion turn
  write text that asks for `monitor_start` into that job's `last_result` in
  `crons.json` through a `CronService` built in-sandbox, lets the job come
  due, and shows `_cron_callback` refuses the fire before
  `build_cron_session_context` runs, no prompt is built and no run starts,
  an SEL line names the job, and the row's `last_result` is cleared and the
  component resealed over the cleared value, so the fire after that runs
  with no prior output and its merge reseals a genuine one; companions make
  the same write from
  another job's run and from a sandboxed shell, rewrite `last_result_ts` or
  `last_result_stamp` alone, and rewrite a single-agent job's `acked_items`
  the same way, and show the same refusal and clearing; another plants the
  value and then has the owner's `api_cron_update` change the message, and
  shows the update reseals the definition, carries the record's
  prompt-bearing component unchanged rather than adopting the planted
  value, and the next fire is refused, audited and cleared as above, the
  planted text never reaching a prompt; another has the job run genuinely
  and shows `_merge_job_result` resealed the new `last_result`, and lets it
  fire again and shows the fire verifies and the
  prompt prepends it; another plants a value into the row while a run is in
  flight and shows the run's merge writes its own object's value over it
  and the next fire verifies; another lets a persistent agent run produce
  no result
  and shows the carried value and the digest both verify at the next fire;
  and another rewrites the `last_result` of a stateless job, and of a
  `command` job, from a sandboxed shell and shows the next fire is refused
  and audited the same way, the field being sealed on every row;
- a row the gateway never sealed fires nothing, and the owner's cron routes
  read the credential class, not the route: a test runs the Phase 2 cutover
  over a store holding rows created before it and shows each is sealed
  once, its record marked legacy, and fires at its next due; a companion
  then writes a well-formed row into `crons.json` from a sandboxed shell —
  `approval_mode` `"auto"`, a `command` attached, `enabled` set — lets it
  come due, and shows `_cron_callback` refuses it before any dispatch, no
  `KIROCREW_APPROVAL_MODE` is set and no `run_command_sandboxed` runs, an
  SEL line names the row, and a gateway restart seals nothing for it;
  another creates a job through `api_crons_create` under a dashboard
  token, through `cron_add` from an attested session and through
  `CronSDK.add_job` from an app, and shows each record is written before
  its row and each job fires; another creates one through the CLI's
  `kirocrew cron add` with no gateway listening and shows its row has no
  record and fires nothing after the gateway starts, an SEL line naming
  it, until it is re-created through a gateway path; another runs
  `kirocrew cron update` from an agent's shell holding only the internal
  secret and shows the gateway refuses the reseal and audits it, the row's
  digest is stale and the next fire is refused, then runs the same command
  under an owner credential and shows the update is admitted and resealed;
  a companion runs `pause` the same two ways and shows the job fires
  nothing after either — the refusal being the pause honoured — and
  `resume` and `adopt` the same two ways and shows each takes effect only
  under the owner credential; another sends `POST /api/crons`,
  `PATCH /api/crons/{id}`, `POST /api/crons/{id}/enable`,
  `POST /api/crons/{id}/ack`, `POST /api/crons/{id}/run`,
  `POST /api/crons/{id}/cancel`, `POST /api/crons/{id}/to-chat` and
  `DELETE /api/crons/{id}` over loopback with `X-Internal-Secret` alone and
  shows each is refused and audited before any mutation — the create writes
  no row, the ack writes no `acked_items`, the run fires nothing, the delete
  removes nothing — then with a validated owner's dashboard token and shows the
  create writes its record before its row, the ack's summary reaches the
  next prompt only through the resealed record, and the update and enable
  reseal; another posts `/api/notifications/unack` for a cron notification
  under a dashboard token minted for a Slack-allow-listed non-owner and
  shows, for a row that does not name them, `acked_items` unchanged and the
  refusal audited, and for a row whose `created_by` is that user, the item
  popped and the record resealed, then under the
  owner's token and shows the item popped on either row, and a
  companion presses the Slack ack button as a non-owner allow-listed user
  and shows the same two outcomes, then as the owner and shows the
  summary written and resealed; another sends `PATCH /api/crons/{id}` and
  `POST /api/crons/{id}/enable` under a participant's token against the
  owner's row and shows each refused and audited, then against the
  participant's own row and shows each admitted and resealed; another runs
  the channel commands `cron pause`, `cron resume`, `cron remove` and `cron
  remove-all` as a non-owner allow-listed user and shows the first three
  take effect only on rows naming that user and `remove-all` is refused,
  then as the channel's owner and shows each takes effect; and another
  arms Ops Mission Control's rotation through `POST /rotation/arm` and
  shows `apply_tiers` pauses and resumes only that app's own rows, each
  resealed as app-owned, and that a row of another owner in the same tier
  name is left untouched and the refusal audited;
  another asks `api_token_local`
  for a dashboard token from a sandboxed process presenting the internal
  secret and shows `local_owner_bootstrap_allowed` refuses the mint, then
  from a gateway-spawned app backend and shows the token it receives
  carries the app's claim, `is_owner_dashboard_request` refuses it, and
  `POST /api/crons` admits it as the app's and not the owner's; and
  another calls `cron_update` from an attested session whose turn carries
  the job's sealed generation and shows `api_cron_tools` binds the caller
  through `memory_request_identity` and the update is admitted and
  resealed;
- the spend a bounded loop has consumed is the record's, and a rewound row
  regains no budget: a test arms a bounded loop at `max_cycles=24`, delivers
  ten cycles, lowers the row's `cycle_count` to `0` in `autonudge.json` from a
  sandboxed shell, restarts the service, and shows `_load` brings the row back
  to `10`, the timer deactivates the loop with `stopped_reason="cycle_cap"`
  after fourteen more delivered cycles, not twenty-four, and an inheriting
  `monitor_start` re-arm from the loop's session is capped at fourteen; a
  companion arms a loop at `max_runtime_secs=14400`, moves the row's
  `created_ts` later out of band, and shows `runtime_budget_exceeded` still
  measures from the sealed origin and the loop deactivates with
  `stopped_reason="runtime_budget"` on the owner's clock; another fails the
  record write of a delivered-cycle charge and shows the row's `cycle_count`
  is unmoved, the loop delivers no further turn until the charge lands at a
  later pass, and no cycle is delivered uncharged; another injects a crash
  between the record write and the row write of a charge, restarts, and
  shows the row is brought up to the record on load; and another recommits
  the pair through the owner-gated `PATCH /api/autonudge/{id}` and shows the
  record's charged count is carried, not reset; a structured companion arms
  a monitor through `monitor_watch` at `max_agent_turns=4`, completes two
  wakes, lowers `agent_turns` to `0` on its `monitor` record in
  `autonudge.json` from a sandboxed shell, and shows the next tick repairs
  the row to `2` and audits it and `monitor_budget_reason` stops the watch
  with `MONITOR_STOP_AGENT_TURN_BUDGET` after two more completed turns,
  not four; siblings lower `input_tokens` and `output_tokens` against
  `max_tokens` and `provider_error_count` against `max_provider_errors`
  and show the same repair and the same stop on the record's count;
  another moves the record's `created_ts` later out of band and shows the
  runtime stop lands on the owner's clock; and another fails the record
  write of a turn charge and shows the claim stays in flight, no further
  wake is dispatched, and the watch is retired fail-closed through
  `record_monitor_completion_evidence_unavailable` if the charge never
  lands;
- the decision state a monitor's engine or a loop's gate decides by is the
  record's, and a rewritten row neither repeats nor suppresses a wake nor
  defeats a stop: a test lets a structured monitor reach a stall streak of
  eleven counted ticks, clears `stall_streak` and `stall_started_at` on its
  `monitor` record from a sandboxed shell, and shows the next tick repairs
  both from the record, audits the disagreement, and `_fold_stall_streak`
  stops the watch with `MONITOR_STOP_VERDICT_STALL` on the twelfth counted
  tick, not twelve ticks later; companions clear `coalesce_alerted` while a
  condition is inside its re-alert interval, rewrite `coalesce_windows` to
  an age past the floor, and rewrite `last_fingerprint`, one at a time, and
  show each is repaired at the next tick and audited and the tick decides
  as the record says — no repeated wake, no early release, no spurious
  change; another clears `wake_in_flight` while a dispatched wake is
  running and shows the tick repairs it, probes nothing, and the turn's
  completion is charged; another zeroes `completion_evidence_deadline` on a
  dispatched claim and shows the record's deadline still retires the watch
  fail-closed when no completion reports; another lowers
  `consecutive_provider_errors` during an outage and shows the error stop
  lands on the record's streak; another rewrites `next_probe_at` a year out
  on a `BUSY` claim and shows the retry runs within one cadence; and for a
  gated legacy loop another clears `judge_quiet_streak` one tick short of
  `_judge_quiet_streak_floor`, sets `judge_wake_pending` on a loop that
  owes no turn, clears `floor_fire_pending` on a loop whose floor delivery
  is still owed and sets it on one that owes none, and rewinds
  `judge_cursors`, one at a time, and shows each
  is repaired from the record at the next `_timer` pass and audited and the
  gate delivers exactly the ticks the record says — the floor tick on
  schedule and delivered exactly once, no forced fire, no rows re-read;
- an automation's provenance is read from the sealed record, never from its
  own store: a test has a person's turn call `cron_add` for a job that posts
  to origin, rewrites that job's row in `crons.json` from a sandboxed shell
  after the owner's `DELETE /api/autonudge/{id}` — its captured generation
  set to the current value read from the child-readable commitment leaf —
  lets it fire, and shows the cron turn's `monitor_start` is refused against
  the sealed triple and the refusal reported into the turn; a companion
  rewrites the row's `session_key` to another slot and shows the injection is
  refused rather than landing there; another deletes the sealed triple for a
  live job and shows its next injection turn's arm is refused, fail-closed,
  as a turn carrying no generation is; and another leaves the row and the
  triple in agreement on a slot nothing cleared and shows the job's turn
  arms exactly as on the measured base;
- a pre-upgrade row read unbounded by Phase 1 is inherited, not replaced,
  by a `ctx.nudge` its session aims at the slot: a test shows the arm takes
  the unbounded classification and is capped at what is left of the stored
  bounds rather than a fresh count;
- the generation advances before the row moves, and a crash between the two
  writes leaves a late proxy nothing to arm on: a test arms a bounded loop,
  has its turn launch a workflow whose script waits and then calls
  `ctx.nudge(max_cycles=1000)`, injects a failure after the owner's `DELETE
  /api/autonudge/{id}` advances the generation and before the row is removed,
  restarts the service, lets the script's call arrive, and shows the row was
  deactivated on load, the arm is refused as stale and the slot holds no
  fresh loop; a companion injects the failure between the record write and
  the row write of a fresh arm on an empty slot, restarts, lets a run that
  same turn launched after the arm call `ctx.nudge`, and shows the
  load-time closure advanced the generation and the arm is refused; another
  fails the ending's record write for a spent cycle cap and shows the
  row delivers no turn, the generation is unchanged, and a late `ctx.nudge`
  is decided against the still-open record — refused at the marker or capped
  at the remainder — never admitted to a fresh count; and another injects
  the failure between the record write and the snapshot write of a
  structured arm that displaces a committed legacy row, restarts, and shows
  the load-time closure advanced the generation and a late `ctx.nudge` from
  a run that loop launched is refused as stale; and
- every Phase 1 exit criterion still holds with Phase 2 applied.

## Backward compatibility

No accepted API input, stored key, monitor kind, or tool is removed or renamed.
One accepted value becomes a refusal: a negative `max_cycles`, which the
legacy routes, the popover, `ctx.nudge`, the
auto-research validator and the `fork` action that bypasses it accepted and
the service stored as the `0` that means
unlimited, is refused at every boundary with a validation error (§5), as a
negative `max_runtime_secs` — which the public route boundaries and, through
`validate_runtime_secs`, `AutoNudgeService.add` and `update` already refuse,
while `_add_unserialized` and `_update_unserialized` still clamp to the same
`0` — is refused at every boundary in turn; no
shipped surface emits either without a person typing it, and a person who
typed one armed a loop they did
not mean.
Pre-upgrade outer-loop `approval_stalled` records remain readable and re-armable.
The behavior change is deliberate and scoped: new prompt-loop approval evidence
no longer creates that terminal outer-loop state on a bounded loop. A loop that
carries `max_cycles=0` and `max_runtime_secs=0` from before the upgrade, whether
its creator typed the `0`, left the popover field untouched when it seeded `0`,
or omitted the field from a REST body the route stored as `0`, is treated as
unbounded (§5): its stored values are not migrated or rewritten, it is shown as
`0`, and it keeps the terminal approval-stall stop it has today until its owner
gives it a finite budget. So is every other pre-upgrade loop, whatever bounds
it stores. A pre-upgrade row's slot holds no entry in the sealed commitment
leaf and the row carries no evidence of who committed its bounds: on the
measured base `monitor_update` from the loop's own session writes `max_cycles`
and `max_runtime_secs` against no committed pair, so a stored non-zero value
may be one the loop wrote for itself, and deriving the committed pair from the
stored bounds would let a legacy loop that tightened itself from `0` before
the upgrade be read bounded and consume `approval_stalled` — the
classification decided by the loop, one upgrade removed. Phase 1 therefore
fails closed: the timer reads every row whose slot holds no commitment record
as unbounded — every pre-upgrade row, regardless of the bounds it stores —
and that row keeps the terminal approval-stall stop until its owner recommits
it through an owner-gated surface, which writes the record — a `PATCH
/api/autonudge/{id}` recommit, or the owner's `DELETE /api/autonudge/{id}`
followed by a fresh arm from the person's own turn, the arm Phase 2 reserves
for authenticated-human provenance. The stored bounds stay what they are:
`_timer` enforces them as the live caps, the popover shows them as-is, a write
from the loop's own session may tighten them and never raise them, since the
store holds no committed pair to restore up to (§5), and once Phase 2 lands a
`ctx.nudge` its session aims at the slot inherits the unbounded
classification and is capped at what is left of the stored bounds rather than
replacing the row with a fresh count; none of that classifies. Its
`approval_stalled` and `stop_sentinel_path` stay on the row as well, with no
record to hold them, until a record is written for its slot (§5). The
trade-off
is accepted in one sentence: a bounded-looking legacy loop regains the
no-retire behaviour only after an owner recommit, and until then an
unanswered approval retires it exactly as it does today. A pre-upgrade
stopped legacy record needs no
migration either: on the measured base a stop directive and a fired sentinel
removed the record,
so no retained record of the kind §5 introduces predates the upgrade and a
pre-upgrade slot holds none — the first `autonudge_stop`, `monitor_stop` or
sentinel fire after the upgrade creates one, from whichever turn, carrying
the budget
that loop had left and the unbounded reading Phase 1 gives it, while the
owner's `DELETE /api/autonudge/{id}` and `/goal clear` remove the row as
before; a record the timer deactivated
with `stopped_reason="approval_stalled"` keeps the treatment it has today,
displaced by a directive re-arm as before; a research tombstone stays the
retained evidence it already is; and a pre-upgrade row a slot close retires
and a failed persist restores through `_restore_slot_nudge_loop` comes back
with its stored bounds, no record and the unbounded reading every such row
has, since the restore mints no commitment (§5). No pre-upgrade row stores a
negative cap: the
service has always stored `max(0, int(...))`, so a negative any surface sent
before the upgrade is already the `0` the store shows, and is read unbounded
with every other pre-upgrade row; after the upgrade a negative is refused at
every boundary rather than stored, and a row hand-edited to one in the
agent-writable store — or to the `0` that means unlimited beside a positive
committed cap — disagrees with its record and is read unbounded (§5).
The first load after the upgrade reconciles the two files against a
commitment leaf that holds no slot, so it closes and deactivates nothing —
every row simply has no record and is read unbounded, as above — and the
load-time rules of §5 first act on a transition made after the upgrade. A
pre-upgrade install holds no commitment
record for any slot, and a Phase 1 install holds none carrying a generation;
Phase 2 reads an absent generation as `0` for every slot, as the store already
decodes an absent `config_generation` to `0`, and counts from there, so the
first reset or ending after that upgrade advances it. What follows in this
paragraph is Phase 2's upgrade. A run that was in
flight when the gateway restarted for it never reaches the authorizer at all:
on the measured base
`RunHandle.from_store_json` in `src/kiro_crew/workflows/registry.py` marks a
run that was still running when the gateway died as failed —
“interrupted: gateway restarted while running” — because it can never resume
in the new process, and a `rerun_subtree` is a new run that carries the
generation the turn launching it hands it. The rule for an arm that carries
no captured generation is stated all the same, and it fails closed: the arm
is refused
and the refusal is recorded in the run as the other “ctx.nudge NOT armed”
messages are, rather than being read as launched under the slot's current
commitment. The store cannot tell which commitment such a run was launched
under, and treating it as current would reopen the delayed-proxy case for
exactly the run the store cannot check; the cost is one refused convenience
arm, visible in the run's stream, that a person re-arms from a new turn. A
turn automation originated that was queued when the gateway restarted meets
the same rule by the same route: on the measured base the durable queue copy
in `src/kiro_crew/dashboard/slot_queue_repository.py` never carries
`_directive_user_origin`, by design, because a flag restored from an
ordinary writable file would be authority granted to whoever edited it, so a
restored entry drains as a non-directive turn; the generation rides the
queued entry beside that flag and is dropped with it, so a restored
completion or injection carries neither human provenance nor a generation,
and a `monitor_start` it issues is refused, fail-closed, as a `ctx.nudge`
with no captured generation is. A human-typed entry restored the same way
already drains as non-directive on the measured base and meets the same
refusal; the person, back at the tab, types the arm again. A `/goal` or
`/goal clear` restored the same way drains without human provenance and is
refused by the slash dispatch on the same ground; the person types it again.
A cron job
created before Phase 2
has no sealed provenance triple and no definition digest of its own — its
generation, like every automation's,
is recorded by the gateway at the scheduling call, in the sealed leaf and
never on the job's own row in `crons.json` (§5) — so Phase 2's cutover, the
first load the upgraded gateway makes, seals every row the store holds
once, as found, with a triple carrying no generation and a record marked
legacy, and the first
origin-injected turn it produces after that upgrade is refused should it try
to arm a loop; the person who created it re-arms from their own turn, and
the job's later fires capture nothing retroactively — a person who wants
that cron to arm loops recreates it from their own turn, and the new job's
triple and digest are written at creation. A `cron_update` of such a job is
admitted under the ordinary rule — from a person's turn, or at
`api_cron_update` under the owner credential class, since its record holds
no generation an automation turn could match — and reseals the legacy
record, whose turn still carries no generation and arms nothing whatever
its message says (§5). The cutover is one act, not a standing rule: a row
that reaches `crons.json` after it with no record — a sandboxed shell's
write, the CLI's `add` or the attended CLI's local fallback with no gateway
listening — is not sealed by a later load or restart, fires nothing and is
audited until the job is re-created through a gateway path (§5), so an
operator who keeps a gateway-less `kirocrew cron add` in a script re-issues
that job once through the gateway under an owner credential. A cron row
edited to carry a generation of
its own, or a different `session_key`, gains nothing: the injection is
authorized against the sealed triple, not the row; a sealed job's row edited
in its message or schedule injects nothing until a person's turn, or an
`api_cron_update` under the owner credential class, reseals it (§5). The
CLI's `update`, `pause`, `resume` and `adopt` keep their command lines and
change their admission: under an owner credential each is admitted and
resealed as today's operator expects; under the internal secret alone —
the credential a sandboxed shell holds too — the gateway refuses the reseal
and audits it, the CLI's own row write stands unsealed, a pause is honoured
by the refusal at the next fire, and an update, resume or adoption waits
for an owner-credentialed act (§5). A loop row that predates Phase 2 has
no definition digest of its own, and Phase 2's cutover — the same first
load — seals every row `autonudge.json` holds once, as found: a row whose
slot holds a commitment gains the definition component beside its pair, a
pre-upgrade row whose slot holds none gains an entry marked legacy holding
the definition component and no pair, and the second stays read unbounded
until its owner recommits it, exactly as before the cutover — the legacy
entry commits nothing, and every reading this section gives a slot with no
record applies to a slot whose entry holds no pair (§5). From the cutover
on, a row that reaches the store with no entry — a sandboxed shell's write,
or every row on a host whose leaf the alias check replaced (§5) — arms
nothing at load and is audited until a person arms the loop again; the
loop's own `monitor_update` of its message, from a wake carrying the
current generation, is admitted and resealed as today's operator expects,
and a rewrite of a sealed field made by any path the gateway did not
reseal ticks nothing until an admitted update reseals it or the owner
clears the slot (§5). A
goal draft the popover remembered before the upgrade carries no
cap-commitment marker (§6).
Its message and idle are restored as
committed; its stored record is not rewritten until the person edits again; and
if its `maxCycles` is `0` the cycles field is reseeded to 50 rather than
restored, because on the measured base a message-only edit persisted the
untouched seed `0` alongside the message and that `0` was never a choice. A
positive legacy cap is restored as it was, and a negative one is restored and
refused at save (§5, §6). Entering `0` again commits unlimited
operation and sets the marker, so a person who wants it keeps it. The fresh
goal with no remembered draft receives the new seed as well.
An Issue Radar crew loop the timer deactivated with
`stopped_reason="approval_stalled"` before Phase 2 needs no migration: on
the measured base, and after Phase 1 alone, the watchdog revives it within a
pass, so at that upgrade such a record is either already active again or
about to be; after Phase 2 it stays inactive with its marker until the crew
is resumed through `POST /crew/pause` or the loop revived through the
owner-gated `PATCH`, the treatment every stalled unbounded loop has, and its
row's `stopped_reason`, whatever the store says, decides none of that: the
watchdog reads the sealed record, so a row whose slot holds an ended
commitment is left alone whatever reason it carries, and so is the slot once
the row is gone: a stalled row deleted from the store before or after the
upgrade leaves the ended record, and no pass arms a fresh loop on it until
the crew is resumed. A crew
loop inactive for any other reason with its commitment open is revived by the
first post-upgrade pass as before; one whose slot holds no record — a row
armed before Phase 1 and never recommitted — is not revived by a watchdog,
which reads no open commitment there, and is reopened by the resume route,
which commits a fresh pair (§5), one Pause-then-Resume per such crew; and a
live crew whose slot holds no record and no row — never launched, or its row
gone before any commitment was recorded — is armed by the first pass through
`launch_crew` exactly as on the measured base, since there is no ended record
to hold it, and that arm commits the crew runtime's pair (§5). A crew loop the
owner removed through `DELETE /api/autonudge/{id}` before Phase 2 was re-armed
by the next pass, so at the upgrade no such slot is waiting; after Phase 2
that removal leaves an ended record the watchdog does not arm on, and the
crew is given its clock back by Resume. A
research loop so deactivated needs none either: on the measured
base, and after Phase 1 alone, `_watchdog_loop` revives it on the first pass
that finds its campaign RUNNING; after Phase 2 it stays inactive with its
marker until the campaign is resumed through `_handle_action` or the loop
revived through the owner-gated `PATCH`, and a research loop inactive for
any other reason with its commitment open is revived as before, one with no
record reopened by the app's `resume` alone, as is one whose row is gone,
since `_watchdog_loop` arms no loop on any slot. A channel session's loops are
unchanged in the store: the
provenance the channel consumer starts passing in Phase 2 is a per-turn flag,
never persisted, so a loop a person armed from Slack, Discord or Webex before
the upgrade keeps its stored bounds as live caps and, like every pre-upgrade
loop, is read unbounded until its owner recommits it.
The commitment leaf is new, so its entry on the write-only tier breaks no
pre-upgrade writer — none exists — and removes no read: the tier refuses
writes alone, so the dashboard file viewer and a `cat` of the leaf read it
as they read `config.json`, and the gateway's transition helper opens the
path directly, through `atomic_write`, and never meets the gate (§5). A
leaf the upgrade finds already aliased — a symlink at the path, or a file
carrying a second hardlink — is refused rather than adopted: the first load
reads no commitment from it, audits the shape, unlinks the aliased name and
publishes a fresh empty record in its place, so every slot is uncommitted
as it was before Phase 1 and its owner recommits (§5).
A structured monitor's typed disposition behaviour is unchanged; the
definition it acts on — `kind`, `target`, `objective`, `cadence_secs`,
`wake_instructions` and the budgets beyond the committed pair — is sealed by
Phase 2's cutover like every other automation definition, and its record is
otherwise as the store holds it (§4, §5).

## Security considerations

The primary risk is interpreting remediation as permission to escalate. The
contract therefore names prohibited actions explicitly and tests each
agent-facing surface. Existing security-policy and denied-command enforcement
remain authoritative even if an instruction is malformed.

Durability is also a safety property. Consumed evidence and the gated follow-up
credit are persisted atomically before the model turn. A failed write retains the
old live state and schedules retry, so disk and memory cannot disagree about
whether a recovery turn was already authorized. The commitment record has the
same shape against the loop row it governs: it is written first on every
transition, a failed record write denies the transition and leaves both files
as they were, and a crash between the two writes is reconciled at load with
the record authoritative, so the row and the record cannot disagree about
whether a commitment is open — an ending that reached the record and not the
row leaves a row that is deactivated and never fires, and an add that reached
the record and not the row leaves a record that is closed, never one a late
proxy can inherit (§5). A row write that fails after the record write is
compensated per transition and never by restoring the prior record: a fresh
arm closes the record it just wrote, a recommit or an ending stays
authoritative and load repairs the row against it, and only
`rollback_monitor_replacement`, undoing a structured displacement whose
authorization did not commit, puts a prior record back. A generic rollback
would let an owner's `DELETE /api/autonudge/{id}` be undone by a failed row
write — the ended commitment reopened, and re-armed at the next restart by
the reconciliation meant to close it — so no write failure reopens a
commitment the owner ended (§5).

Repeated rechecks can spend model budget, and for a loop with no finite bound
the approval-stall stop is the one service-enforced ending it has. On the
measured base `/goal` (50 cycles, no wall-clock budget), `monitor_start`
(`_MONITOR_DEFAULT_MAX_CYCLES` = 24 cycles and `_MONITOR_DEFAULT_MAX_RUNTIME_SECS`
= 14,400 seconds, with `0` refused), the Spec Builder handoff (60 cycles) and a
default auto-research campaign (30 cycles) were bounded by default. The
dashboard Set-a-goal popover armed a fresh untouched goal at `0`, `POST
/api/autonudge` stored an omitted `max_cycles` and `max_runtime_secs` as `0`,
and `ctx.nudge` and the Issue Radar crew runtime arm `0` unless their caller
sets a cap. This decision therefore removes the stall stop only for a bounded
loop, and makes both dashboard goal routes finite by default at 50 cycles,
matching `/goal`, so the goals people create there are bounded; implementation
PR #13000 supplies that default and it is not on `main` until the PR merges. An
unbounded loop, whatever surface armed it and however its `0` arose, keeps the
terminal approval-stall stop and cannot lift it from its own session: the bound
that classifies a loop is the one its owner committed — defined from the value
the service stores, with a negative refused at every boundary rather than
stored as the `0` that would unbound a loop its creator meant to bound (§5);
a pre-upgrade loop,
whose stored bounds carry no evidence of who wrote them, is unbounded until
its owner recommits it (Backward compatibility) — and it is read from the
per-slot commitment record in `autonudge-commitments.json`, a leaf three
layers hold (§5), each closing a write the other two cannot see. The OS
sandbox seals it read-only in every mode through `_CREW_READONLY_LEAVES`,
so a sandboxed shell's runtime-composed path or a spawned interpreter's
`open()` is denied by the kernel, not by a text gate — the write no path
gate can see, and why the leaf is not under the sandbox-writable `trust/`.
`_WRITE_PROTECTED_HOME_PATHS` puts it under `is_sensitive_write_path`, so
the agent's file-edit tool — which `on_tool_call` in
`src/kiro_crew/hooks.py` gates by that function and not by the seal lists,
and which the seal alone would therefore admit — is refused in the gateway
whatever the sandbox setting, `"off"` and a host without the OS mechanism
included: the write no seal reaches. And only the unsandboxed gateway's
transition helper writes it at all, at every row transition, so no tool the
agent holds is a writer of
the record by design; the seal and the gate refuse the attempt, the writer
set is what makes the record the owner's. The seal binds the leaf's path
and not its inode — `_warn_if_alias_backed` in `src/kiro_crew/sandbox.py`
warns and continues over a symlink or a second hardlink at a ceiling, by its
own account, so a pre-planted alias would carry a sandboxed write around
the seal — and so no read of the record trusts the name: every read is bound
to the descriptor it read, a lone regular file or no record at all, audited,
and an alias the first load or the next write finds is removed and replaced
with a fresh empty record rather than adopted (§5, Phase 1); an alias
written through and unlinked before any read is the residual
`require_unaliased_launch_state` names, and the seal and the gate are what
keep the agent from making one. A shell the agent spawns on a
host whose sandbox is off meets neither the seal nor the gate — the
write-only tier's standing residual, shared by `config.json`'s resource
ceilings and every other entry on that tier and by the agent-writable loop
store itself, which this document neither widens nor narrows. The
classification is never read from the agent-writable loop store, so
a pair or a
classification forged in `autonudge.json` does not survive the comparison: a
row that disagrees with its record, or has none, is read unbounded and refused
as a record to inherit until the owner recommits it (§5) — a live bound
raised above a positive committed cap, and the `0` that `_timer` reads as
unlimited written over one, disagree alike, so the rewritten row loses its
bounded classification and stops on the marker rather than passing as
bounded. In Phase 1, where the row still supplies `_timer`'s operands, such
a row runs past the committed cap the rewrite touched, up to the other live
bound if one is set or to the rewritten finite value, and only a sole bound
rewritten to `0` leaves the stall stop and the owner's stop as its endings
— the cap-field rewrite
is the one row tampering Phase 1 answers with the classification alone, and
it stays on the write-only tier's residual above until Phase 2; from Phase
2, where `_timer` takes the cap and the budget from the committed pair (§5),
rewriting the row to `0`
buys no cycle past the owner's cap — a `monitor_update`
bound write made while the marker is set is refused, and so is the stop and
re-arm that would shed the marker with the record — a stop the agent triggers,
by its stop tool from any turn or by the sentinel file it can write, retains
the record with its marker,
and a re-arm from that session is refused while the marker is there (§5,
Phase 1); the slot-close retirement retains the record too, and the restore
that follows a failed persist inherits it and mints nothing fresh (§5). Nor
is the marker itself read from that store: `approval_stalled`, and the
`stop_sentinel_path` the timer tests before every fire, live in the record
from the write that sets them, and a row copy cleared or rewritten out of
band is repaired from the record at load and at the next tick, so the stall
stop, the structured monitor's `APPROVAL_STALL` disposition, which
`record_monitor_turn_completion` decides from the same marker, and the kill
switch are not undone by a file write either (§5, Phase
1). Nor
can a bounded loop buy itself
more than its owner committed, by any route its turn holds. A
`monitor_update` from that turn is capped at the committed pair: it may
tighten a live bound or restore it up to that pair, and a raise to `1000`
cycles or `604800` seconds — the schema's ceilings, which the applier's own
guards would otherwise let through once the marker is clear — is refused with
the live bounds unchanged. The cap holds only because the service can tell
that write from the owner's recommit, and on the measured base it cannot:
both reach `svc.update` through `authorize_and_update_nudge`, which checks no
ownership. Phase 1 has the owner-gated `api_autonudge_update` alone pass an
owner-recommit signal, after `_require_monitor_owner` has admitted the
request, and every other caller — the applier, an app, the timer — is read
as non-owner, so a directive cannot spell a recommit (§5). The record a
self-session stop retains carries the
budget the loop had left, a re-arm from that session inherits that remainder
and cannot exceed it, and a spent remainder refuses the re-arm; those two
routes close in Phase 1. The widest route needs neither a write nor a stop,
and closes in Phase 2: on the measured base a workflow the loop's turn runs
can call `ctx.nudge(max_cycles=1000)` on its originating session, and the
`_wf_nudge_authorizer` path leaves `authorize_and_add_nudge` at its
`replace_existing=True` default, so `AutoNudgeService.add` removes the active
row and arms a fresh one with a fresh cycle count and start time — `1000`
cycles in place of the `24` the owner committed, with no marker to refuse it
and no stopped record left behind. §5 decides that arm against the committed
loop as well: the replacement inherits the committed classification and
carries the budget already spent forward, capped at what is left of the
committed pair, and is refused while the marker is set. A remediation turn
that consumed the marker therefore gains no fresh cycles or seconds by
writing a bound, by stopping and re-arming, or by arming a replacement
through a proxy; the slot's commitment resets only through the owner-gated
recommit, clear or revival (§5). Nor does a proxy the turn launched earlier
keep any of those routes open after the owner has acted: a workflow run
outlives the turn and the loop, and on the measured base `DELETE
/api/autonudge/{id}` and `/goal clear` remove the row — as a STOP sentinel
makes `_timer` do, and a stop directive does through `_stop_resolved_loop`
on an ordinary dashboard or channel slot —
so a `ctx.nudge` arriving from that run afterwards
would find an empty slot and arm a fresh loop past the owner's explicit clear,
and a run arriving after a spent cap would displace the replaceable
`cycle_cap` record with a fresh count. The per-slot commitment generation §5
adds in Phase 2 is what makes the owner's ending final against work the loop
set in motion before it: the run carries the generation of the commitment it
was launched under, every reset by the owner and every ending advances the
slot's, and an arm whose generation is stale is refused under the service
lock whatever the slot holds. The loop's session gains nothing from that
counter and cannot move it: a stop the agent triggers — its stop tool from
any turn, the person's included, or the sentinel file it can write —
retains the commitment rather than ending it, so
it advances nothing, and a proxy the loop launched before or after that stop
carries a generation that still matches and is decided against the retained
record it inherits from — refused at the marker, capped at the remainder; no
sequence of launches and stops lets a proxy of a committed loop arm a fresh
pair. The counter moves only for an ending the agent cannot trigger: the
owner's `DELETE /api/autonudge/{id}` or `/goal clear`, or the service's
spent budget or stall stop, after which a run or cron the loop set in motion
is refused (§5); a person who asks the agent to stop is pointed at that
control, since the agent's stop would otherwise be read as the owner's on
the strength of whose turn it was, and the same turn could end the
commitment and arm a fresh pair. Nor is a run the only thing the
loop sets in motion that outlives the owner's ending: a subagent it spawned,
a workflow it ran or a cron it created reports back as a turn on the same
slot, and on the measured base every such injector starts that turn with
`_directive_user_origin` `False` while the external-arm refusal reads only
the slot's mode against `_EXTERNAL_ARM_REFUSED_MODES`, so on an ordinary
dashboard slot the turn's `monitor_start` is admitted as a person's would be
and, after the owner's clear or the loop's spent cap, would arm a fresh pair.
§5 has every automation-originated turn carry the generation its scheduling
turn handed it — a turn or run carrying one hands that same value to whatever
it schedules, a turn carrying none hands none, and only a turn with
authenticated-human provenance captures the slot's current one — so the
owner's ending is final against every turn the loop set in motion as it is
against every run: the loop's last cycle, a completion that lands after the
clear, a cron the loop created, and every further hop that completion or
cron schedules in its own right — each is refused, and the refusal is
reported into the turn. Had automation captured the slot's current generation
at each scheduling call, the refused turn could have scheduled once more and
the second hop would have matched; inheritance leaves the stale value in the
chain for good. The value the chain carries is kept out of its reach too:
the gateway records each scheduled automation's id, origin slot and
generation in the sealed commitment leaf, or a sibling leaf under the same
disposition, and authorizes the injection against that record alone, because
`crons.json` is read-write inside the sandbox by design
(`_CREW_SANDBOX_VISIBLE_LEAVES`) while the commitment leaf is child-readable,
so a stale automation holding a shell could otherwise read the current
generation from the leaf and write it into its own cron row, presenting a
forged current generation at its next origin injection; a cron row that
disagrees with its sealed triple, or has none, produces a turn whose arm is
refused (§5). The triple alone would bind which automation fires from which
generation and not what it says: `cron_update` is session-owned through
`_check_cron_job_ownership` and rewrites a job's message in place through
`CronService.update_job`, and `spawn_steer` rewrites a running run's turn
through `steer_run` and `follow_up_run`, so a stale turn could put its
payload into a current cron or run and have it injected under a matching
triple. Phase 2 seals a digest of the definition beside the triple — what
the automation runs, how, when, where, under what approval, and whether its
owner has paused it, and beside it, as its own component, the prior output
the fire path hands the run as input, `last_result` and `acked_items`,
which only the gateway's merge of that cron's own run and the person's ack
write and reseal, while the display and timing state the gateway writes
after each run stays outside
— and the loop's own definition among the definitions it seals, since
`autonudge.json` is as writable as `crons.json` and `_fire_dashboard_nudge`
in `src/kiro_crew/slack/gateway.py` and `MonitorController.tick` in
`src/kiro_crew/monitoring/controller.py` read `message`, `slot_key`,
`target`, `objective` and `wake_instructions` off it as the owner's word, so
a rewrite of any of them out of band, by a shell or a stale automation,
would otherwise run an altered prompt or probe under the owner's automation;
sealed, each is verified before every tick and fire (§4, §5) — gates
every in-place update on the generation — a person's turn, or a call at
any mutating cron route made under the owner credential
class, is admitted and resealed, a stale or
generation-free
automation turn refused, and the loop's own `monitor_update` admitted from
a wake carrying the current generation and refused from a stale one — and
verifies triple and digest at the fire and at
the injection, refusing the injection and writing an SEL audit line on a
mismatch, so an edit of a sealed field in `crons.json`, or of a run's file,
by whatever path the gateway did not reseal, injects nothing, a
`last_result` planted by a stale turn or another job reaches no prompt and
is cleared at the refusal, and an edit of
the row's display or timing state decides nothing (§5). The caller class is
read off the credential and never off the route because `/api/crons` is a
`_MIXED_INTERNAL_API_PATHS` prefix: `token_auth_middleware` admits an owner
token, a participant token and loopback `X-Internal-Secret`. On the measured
base the existing-row mutators — update, enable, ack, run, cancel and delete —
then call `require_owner_dashboard_request` for a dashboard caller and
`_refuse_foreign_app_job` for an app caller. A dashboard participant is
therefore refused and an app is confined to its own job; `to-chat` does not
mutate the row and has neither gate. The secret-only class carries no `app`
claim and skips both gates, as it skips `api_crons_create`'s conditional owner
gate. Phase 2 keeps the two existing gates and adds the internal-secret
generation and owner check. The secret is one
`_CREW_SANDBOX_VISIBLE_LEAVES` exposes to every sandboxed process by
design, `run/gateway-<port>.secret` and `.local_secret` both, so that the
in-sandbox MCP servers can dial the dashboard; `token_auth_middleware`
records it as proof that a call came from inside and of nothing about who
made it, and `session_key_is_attested` says the same of it. A call carrying
only that secret is an agent's write, whichever binary made it — the CLI's
`_cron_dispatch` presents exactly that credential through
`_internal_secret` — and is admitted only under a current sealed
generation, which no CLI process carries; a person's class is the signed
dashboard token `_extract_and_validate_token` validates — the owner told
from a participant by `is_owner_dashboard_request`, the participant admitted
only to rows that name them (§5) — signed with
`token_signing.key` that `_CREW_HIDDEN_LEAVES` masks in every mode and
minted at `api_token_local` for the two callers
`local_owner_bootstrap_allowed` admits — a host process outside the sandbox
and a gateway-spawned app backend, minted alike on the measured base and
told apart under Phase 2 by the app claim the backend's token carries — so
a sandboxed process can neither read the key nor, unless it is that
backend, be issued the token. `is_owner_dashboard_request` would refuse the
internal-secret branch on the `request["app"]` claim it never publishes, but
the handlers invoke that predicate only for `app == ""`, while
`_refuse_foreign_app_job` returns when no app name is present. The secret-only
class therefore skips both existing gates on the measured base; Phase 2 adds
its generation and owner check while retaining those gates (§5). A row with
no sealed record is the same gate from the other side: the
store is sandbox-writable, `_load` accepts any well-formed row, `_on_timer`
dispatches it, `vet_job_at_fire_time` reads neither provenance nor
`approval_mode`, and `_cron_callback` honours the row's `"auto"`, so
reading such a row as needing no seal would let a shell mint a new
auto-approved job under no owner's act; Phase 2 seals every extant row once
at cutover, marked legacy, writes the record before the row on every
gateway creation path, and refuses and audits a row with no record at the
fire (§5). The
generation-free arm is reserved for a turn with authenticated-human
provenance, the bit the runner already withholds from the cron, subagent,
task-runner, crew-runtime and nudge-fire producers, grants an app delivery
only where it relays a person's own answer — the Spec Builder's pending
decision passes it `True` for exactly that, from `_deliver_pending_decision`
in `orchestration/decision_outbox.py` under
`src/kiro_crew/apps/builtins/spec_builder/backend/`, which that backend's
`_dispatch_turn` in `runtime.py` relays to the runner — and grants a queue
drain only when every
consumed entry carries it, so no automation of the loop's can wear it. On a
channel the same provenance is the `producer_is_user_facing` the channel
consumer passes for a person's inbound message and withholds from a bot- or
automation-authored message and from the loop's own wake, so a person's
Slack, Discord or Webex watch keeps its fresh-slot arm while the loop's own
channel wake, which reached the applier with neither flag on the measured
base, gets no human provenance after Phase 2 either (§5). The slash dispatch
is held to the same bit: on the measured base `/goal` and `/goal clear`
reach `AutoNudgeService.add` and `remove` before any provenance is read, so
an enabled app holding the `sessionApproval` grant could send either through
`POST /api/chat` into a person's slot and, drained after the owner's clear,
arm a fresh 50 cycles or end the owner's commitment without a person typing
anything; Phase 2 refuses both from a turn or drained entry whose
`_directive_user_origin` is `False` and reports the refusal into the turn,
so the app grant that lets an app speak into a slot does not let it arm or
clear the slot's goal (§5). Nor may an app runtime lift the stall stop from
a loop it armed: the Issue Radar watchdog's unconditional revival on the
measured base turned the one service-enforced ending an unbounded crew loop
has into a pause of one poll interval, marker cleared, with the unanswered
approval still unanswered; Phase 2 has it decide a revival from the sealed
commitment record, never from the row, so a loop whose commitment the stall
stop ended stays inactive whatever `stopped_reason` its row carries — the row
lives in the agent-writable loop store, a forged reason survives a restart
since `_load` holds an inactive row beside an ended record, and a watchdog
keyed on the reason would revive the loop and clear the marker on its next
pass — and arms no fresh loop on a slot whose commitment is ended: the same
watchdog's `loop is None` branch calls `launch_crew`, a fresh `svc.add`, so
deleting the stalled row from that store and restarting would otherwise have
the next pass reopen the ended commitment with no owner's act; the fresh arm
is kept for a slot whose record is absent, and an owner's `DELETE
/api/autonudge/{id}` on a crew loop holds against the watchdog as the stall
stop does — so the approval a person did
not grant is not granted by the app's clock or by a file write, and the loop
resumes only
through the app's own resume control — the resume route's own act, reopening
the ended commitment as a fresh one and arming a loop itself where the row is
gone, since
a stalled crew stays `enabled` and the watchdog cannot tell a resume from an
unattended live crew — or the owner-gated `PATCH` (§5). The auto-research
`_watchdog_loop` revives every inactive loop of a RUNNING campaign the same
way, and the guidance route returns a parked campaign to RUNNING without
re-arming, so on the measured base a stalled research loop is revived the
first pass after a person answers a question that was not the approval;
Phase 2 gives that watchdog the same rule, reviving only a loop whose
commitment is open and arming none, as it arms none today, with the app's
explicit `resume`
or the owner-gated `PATCH` as the revival (§5). A
fresh pair still comes only
from the owner's routes, or from a person's turn on a slot that holds no
commitment, visible on the tab and stoppable. Unlimited operation therefore
remains available as an explicit `0`, but it does not also buy remediation
continuity through an unanswered approval: a person who wants both names a
finite budget. Between the two phases the exposure is the measured base's
for exactly the routes Phase 2 closes: on a tree that carries Phase 1 alone,
a `ctx.nudge` on the originating session still replaces the active row, a
proxy or automation launched under an ended commitment still arms a fresh
pair, the crew and research watchdogs still revive a stalled loop, an app's
`/goal`
still reaches the handler, and a person's channel turn carries no provenance
yet, so an operator reading this section against that tree reads the proxy,
automation, channel, `/goal` and watchdog guarantees as not yet in force.

Slot-scoped evidence has a cost this decision accepts knowingly (§3). Because the
marker records any unanswered prompt in the loop's slot, an ignored dialog in a
person's own interactive turn on a tab that also hosts a bounded
`monitor_start` loop spends one of that loop's budgeted cycles, and on a gated
loop that cycle skips one QUIET reading. The cycle carries no elevated
authority and no stall-specific instruction, it is charged to the budget the
owner declared, and the marker cannot stack, so the exposure is bounded by the
loop's own `max_cycles` and `max_runtime_secs`. The conservative stop is kept
exactly for the loops that have no such bound. The slot rule for a proxy arm
(Phase 2) has the same shape and the same accepted cost: the workflow path
carries no turn provenance to the authorizer, so a `ctx.nudge` a person's own
turn runs on a tab that hosts a committed loop is decided as the loop's own
would be — it inherits the commitment rather than replacing the row with a
fresh one. That person holds the owner routes, and a fresh pair is one clear
or recommit away; the loop holds none, which is the asymmetry the rule relies
on. The generation (Phase 2) carries a second cost of the same shape: a
workflow a person launched from a tab whose loop they then stopped or cleared
has its later `ctx.nudge` refused as the loop's own would be, because the run
carries no provenance that tells the person's launch from the loop's. The
refusal is recorded in the run's stream, and the person, who holds the
routes, re-arms from a new turn. A person's own automation pays the same
price on the same terms: a subagent or cron they scheduled from a tab whose
loop they then cleared has its completion or injection turn's `monitor_start`
refused, because the turn carries the generation their scheduling turn
captured and no human provenance, and anything that turn schedules in its
own right inherits the same stale value; the refusal is reported into the
turn, and the person types the arm into the tab. Their automation on a slot
they never cleared or recommitted arms as it does today. A third cost is
Phase 1's and is the fail-closed reading of a pre-upgrade row: a legacy loop
that looks bounded in the store is treated as unbounded until its owner
recommits it, so an unanswered approval retires it as today until then. A
fourth is Phase 1's on every surface, and does not lapse: a stop a person
asks the agent for — in a tab, or from Slack, Discord or Webex — is carried
out by the agent's own stop tool and is retained, so it does not end the
commitment, automation the loop set in motion may still spend the remaining
budget on the slot, and the person's next watch on that session inherits the
remainder rather than committing a fresh pair; the agent tells them so and
points them at the owner's stop control or `/goal clear`, which ends it. The
price buys the rule that no ending the agent can trigger is an ending: a stop
read as the owner's on the strength of whose turn it was would let the agent
end the commitment and arm a fresh pair in one turn (§5, Alternatives).

## Alternatives considered

### Keep the universal approval-stall stop

Rejected. It prevents repeated denied calls, but it also makes a human-only
approval retire a goal that can still perform safe work and makes “recheck later”
false. That reproduces the observed continuity failure at the runtime layer.

### Stop after N consecutive approval stalls

Rejected as a terminal rule. It still converts an unmet dependency into an
inactive goal before the declared service budget. A future implementation may
use exponential backoff or notification deduplication while keeping the goal
active.

### Change prompts only

Rejected. Prompt policy cannot recheck later after the runtime deactivates the
loop. The agent-facing contract and timer lifecycle must agree.

### Classify bounded and unbounded on the live stored bounds

Rejected. It is the simplest reading, but the loop's own session can write
those values: on the measured base `monitor_update` accepts `max_cycles` up to
`1000` on a live loop whose only cap guard is the delivered cycle count, and the
turn in which an approval lapsed is still running when the marker is recorded.
An unbounded loop could make itself bounded before its next wake, consume the
marker, and remove the one ending §3 promises it keeps — a loop deciding its
own authority ceiling. Classifying on the bound the owner committed, recorded
per slot in a leaf the OS sandbox seals read-only and the file-edit tool's
write gate refuses, keeps that ending in the
owner's hands (§5). Recording the
classification alone does not close the write path, though: the same guard
lets a bounded loop raise its live bounds to the schema ceilings once its
marker is clear, which the alternative after the re-arm below rejects.

### Record the committed pair on the loop's row

Rejected. An earlier draft of §5 recorded the classification "with the loop"
and kept the Phase 2 generation "in the service's persisted state" — the loop
store, `~/.kiro/crew/autonudge.json`. That store is agent-writable, by the
module's own account: `src/kiro_crew/autonudge.py` calls it writable out of
band and scrubs the banner and message it loads because a hand-edited file or
a direct `svc.add` bypasses the authorized write path. A committed pair kept
there is a hint, not a commitment: a cron the loop created could rewrite the
pair, or the generation, once the turn's own writes have persisted, and a
restart would trust the forged bounds as the owner's — the live-bounds
alternative above, by a file write instead of a tool call. §5 keeps the
committed pair and the generation out of the agent's reach instead: in a
dedicated leaf of the crew home that `_CREW_READONLY_LEAVES` in
`src/kiro_crew/sandbox.py` seals read-only in every sandbox mode and
`_CREW_PRECREATE_READONLY_FILE_LEAVES` materialises before every namespace
spawn, that `_WRITE_PROTECTED_HOME_PATHS` in `src/kiro_crew/security/paths.py`
puts under the file-edit tool's write gate for the host where no seal is in
force, and that `_CREW_CHILD_READABLE_LEAVES` classifies for the
governance-mask pin, written only by the unsandboxed gateway through its
transition helper at every row transition, with the row's writable fields
required to agree and a missing or
disagreeing record read as unbounded and refused as a record to inherit
until the owner recommits. Two files cost a transition order and a
load-time reconciliation, which §5 states — record first at every site that
moves a row, record
authoritative, a failed row write compensated per transition and never by
restoring a prior record — and one file would cost neither; but the one file
would be
the agent-writable store, and an order the agent can rewrite protects
nothing. The same rejection reaches the generation a scheduled automation
carries in Phase 2: kept on the cron row in `crons.json`, which the sandbox
keeps read-write, it would be the same hint one file over, so §5 keeps the
automation's id, origin slot and generation in the sealed leaf as well, and
beside them the digest of the definition the automation will inject.

### Clamp a negative cap to `0`

Rejected. It is what the measured base does: `_add_unserialized` and
`_update_unserialized` in `src/kiro_crew/autonudge_service/mutations.py` store
`max(0,
int(...))` for both caps, so a negative `max_cycles` that reaches the service
becomes the `0` that means unlimited — a negative `max_runtime_secs` is
refused by `validate_runtime_secs` before it — and the boundaries that forward
a negative cap — `api_autonudge_start`, `authorize_and_update_nudge`, the
popover, `validate_campaign` with the `fork` action that bypasses it, and
every link of `ctx.nudge` — arm an unbounded loop for
a caller who typed a bound, however malformed. A clamp turns the one value
that classifies a loop unbounded into the value a mistake produces, and the
commitment record would then faithfully record a pair nobody committed.
Refusing the value at every boundary, and at the two cap-storing transitions
as the
backstop, keeps `0` a stated choice (§5, §6); reading a negative as its own
unbounded marker was not considered, since it would add a second spelling of
unlimited to a store the agent can edit.

### Record the commitment under `trust/`

Rejected. An earlier draft kept the record under `trust/`, on the reading
that `_CREW_SECRET_LEAVES` in `src/kiro_crew/security/paths.py` gates the
whole directory and that the self-arm record `record_self_arm` in
`src/kiro_crew/autonudge_selfarm.py` writes there is the precedent. Neither
holds. `_CREW_SECRET_LEAVES` fences the resolved paths the agent's file tools
open — through `is_sensitive_path` — and nothing else: no bash text matcher
carries it into command text, since `is_sensitive_bash_command` in the same
module matches no path in a command, by its own account, so a shell is held
only by the OS disposition of the target. And
`_CREW_SANDBOX_VISIBLE_LEAVES` in `src/kiro_crew/sandbox.py` keeps `trust`
and `sel_hmac.key` READ-WRITE for every sandboxed process, deliberately: the
SEL trust root is read inside the sandbox and the in-sandbox MCP servers
append to the log. A sandboxed shell writing a runtime-composed path, or an
interpreter it spawns calling `open()`, therefore reaches a file under
`trust/` with no file-tool path for the gate to see — the evasion the module
records against `agent_model_state.json`, and the reason
`file-delivery-consent-pending` was given its own leaf — and a pair or a
generation forged there would be accepted as the owner's commitment. The
self-arm record shares that exposure, so it is no precedent for
unforgeability; closing it is out of scope here. The commitment record lives
in its own leaf instead, sealed by the OS for every sandboxed process,
refused to the file-edit tool through `_WRITE_PROTECTED_HOME_PATHS` where no
seal is in force, and written by the gateway alone (§5).

### Derive a pre-upgrade row's committed pair from its stored bounds

Rejected. An earlier draft had the timer record a pre-upgrade row's
classification from the bounds it stores the first time it reads them, on the
reasoning that the store holds no other evidence. That is the reason to
reject it: the stored bounds are not evidence of a commitment. On the
measured base `monitor_update` from the loop's own session writes
`max_cycles` and `max_runtime_secs` with no committed pair to check against,
so a legacy loop armed at `0` that tightened itself to a finite value before
the upgrade would be read bounded and consume `approval_stalled` — the
loop deciding its own classification by a write made before the rule
existed, which is the live-bounds alternative above, one upgrade removed.
Phase 1 fails closed instead: every pre-upgrade row is read unbounded, its
slot holding no entry in the sealed commitment leaf, its stored bounds stay
the live caps and are shown as-is, and only an owner recommit through an
owner-gated surface writes the record that classifies it (Backward
compatibility). The cost is the one that section states: a bounded-looking
legacy loop regains the no-retire behaviour only after its owner recommits.

### Refuse the self-session stop

Rejected, whether for every self-session stop or only for one made while the
marker is set. A stop is how a loop reports that its goal is complete with
evidence, and refusing it would block
that valid ending (§5) because a prompt lapsed earlier in the slot or because
the loop still has budget left. A person's explicit stop is the other
valid ending, and it is not the stop this section retains: it comes through
the owner's route the agent cannot invoke — `DELETE /api/autonudge/{id}`
behind the popover, `/goal clear` typed by the person — removes the row as
today and, in Phase 2, advances the generation; the agent's stop tool, from
whichever turn, is retained rather than refused, and points the person at
that route. Retaining the stopped record — for the stop the agent's tool or
the sentinel it can write issues — with its remaining
budget, its marker state and its committed classification, and letting a
re-arm from that session inherit the record rather than replace it, preserves
the evidence and the commitment without blocking the stop. The inheritance
is not free on the measured base: `_add_unserialized` raises
`MonitorUpdateConflict` for any stopped row `_stopped_row_is_replaceable`
does not admit under the applier's `replace_stopped=True`, so a retained
record would deadlock the session's only re-arm exactly as the module's own
comment says a retained inactive row otherwise does; Phase 1 admits the
retained record there as an inheriting re-arm and keeps the refusal for every
other reason (§5). Removing the record
and carrying the marker to the next loop on the slot was also considered and
rejected: the marker is evidence about the loop that recorded it, and a fresh
loop with a fresh commitment must not inherit or consume it.

### Let turn provenance end the commitment

Rejected. Rounds of this document up to the twelfth read an `autonudge_stop`
or `monitor_stop` issued in a turn with authenticated-human provenance — on
the dashboard `_directive_user_origin` `True` — as the person's explicit
stop: it removed the row, ended the commitment and, in
Phase 2, advanced the generation, with the stop wrappers reading the turn's
provenance to tell it from the loop's own. The reading is exploitable,
because turn provenance says the turn was the person's, not that the person
asked for a stop. In any ordinary turn a person types — a question, a
correction, a request unrelated to the loop — the agent holds `autonudge_stop`
and `monitor_start` together: it can call the stop, have it read as an owner
ending that clears the commitment and advances the generation, and call
`monitor_start` in the same turn, which the fresh-slot rule then admits as a
fresh commitment under the same provenance — its own budget renewed, in one
turn the person never asked to touch the loop. §5 therefore adopts the rule
that an ending the agent can trigger never ends the commitment: the stop
directives are retained self-session stops whatever the turn's origin, the
STOP sentinel — a file the loop's instruction names and the agent can write,
`_timer` deactivating on it rather than removing — is the same class, and
commitment-ending authority is reserved for the owner routes the agent cannot
invoke, `DELETE /api/autonudge/{id}` and a human-provenance `/goal clear`,
and for the service's own terminal endings, a spent budget and the stall
stop. The cost is stated in the Security considerations: a person who asks the
agent in chat to stop gets a retained stop, inheriting automation may still
spend the remaining budget, and the producer contracts point that person at
the owner's stop control or `/goal clear` for a true ending. The provenance
rule for an EMPTY slot is unchanged: a retained stop leaves the slot
committed, so a same-turn `monitor_start` is an inheriting re-arm, not a
fresh commitment, and only a turn with authenticated-human provenance commits
a fresh pair on a slot holding no committed loop.

### Let a self-session re-arm commit fresh bounds

Rejected. Refusing the re-arm only while the marker is set would have closed
the stall case and left the budget open: a bounded loop that consumed its
marker and continued could call `autonudge_stop` and then `monitor_start`
from a remediation turn the fire path did not deliver — on the loop's own
wake `_refuse_stale_wake_arm` already refuses the pair (§5) — receive
`monitor_start`'s 24 cycles and 14,400
seconds anew, and repeat the pair at every turn it chooses, running unattended
past the ending its owner committed with no marker left to refuse it — the
third self-widening variant after the bound write and the stall-time stop,
closed one special case at a time. A stop the loop issues itself spends the
committed budget; it does not refresh it. The slot-close restore in
`src/kiro_crew/dashboard/chat_handlers.py` already carries only the remaining
budget forward for the same reason, and the self-session re-arm inherits that
remainder and its classification instead of committing a pair of its own
(§5). Only the owner's clear or revival commits a new pair.

### Let a self-session write raise live bounds within the schema ceilings

Rejected. This is the rule an earlier draft of §5 stated: refuse a bound write
while the marker is set, and otherwise apply a self-session write to the live
bounds only, on the reasoning that a self-written bound is finite under
`MONITOR_UPDATE_SCHEMA`'s ceilings and so a bounded loop still ends on a
service-enforced budget. It protected the classification and left the budget
open. A bounded loop that consumed its marker and continued holds
`monitor_update` in its remediation turn with the marker clear, and on the
measured base the applier's guards in
`src/kiro_crew/dashboard/session_directive_apply.py` measure a request only
against the cycles already delivered and the seconds already elapsed — never
against the pair the owner committed — while `_timer` enforces the live
fields. A loop armed at 24 cycles and 14,400 seconds could write
`max_cycles=1000` and `max_runtime_secs=604800` and run to the store's limit,
renewing the ending its owner committed from its own turn with no stop, no
re-arm and no marker to refuse it: the fourth self-widening variant. The
schema ceiling is the store's limit, not the owner's budget. §5 therefore caps
a self-session write at the committed pair — a tighten or a restore is
applied, a raise is refused naming the committed ceiling and the owner routes
that recommit it — and leaves fresh commitments to the owner-gated routes.

### Exempt workflow-armed loops from the self-session rule

Rejected. The case for it is that `ctx.nudge` is a workflow primitive with
its own caller-chosen cap, excluded from the default-cap change of §6, and
that a workflow is a separate run rather than the loop itself. But the run is
the loop's own turn by another name: `workflow_run` is a tool that turn
holds, `_nudge_port` in `src/kiro_crew/workflows/service.py` arms the loop on
the workflow's originating session — the session whose turn ran it — and the
`_wf_nudge_authorizer` in `src/kiro_crew/dashboard/server.py` leaves
`authorize_and_add_nudge` at its `replace_existing=True` default, so
`AutoNudgeService.add` removes the slot's active row and arms a fresh one. An
exemption would leave open the widest of the five self-widening variants: the
bound write is capped, the stall-time stop and the re-arm inherit the
retained record, the post-consumption raise is refused, and each needs a
write, a stop, or a marker to act on — this one needs none of them, because
`_monitor_start` arms with `replace_existing=False` and is refused at an
active row while this proxy is the one path that displaces it, with no
stopped record left behind. Passing `replace_existing=False` on the workflow
path alone was also considered and is not the rule: it would refuse every
`ctx.nudge` at an active row, including a script a person runs to retarget
their own loop, where inheriting the commitment and the spent budget is the
behavior the rest of §5 gives a re-arm. The rule therefore reaches the proxy
where it reaches every other arm — at the shared authorizer, on the state of
the target slot, in Phase 2 — and the default cap `ctx.nudge` commits on a
fresh slot is untouched (§6). The same exemption would also leave the sixth
variant open, the one the next two alternatives address: a run the loop's
turn launched whose `ctx.nudge` arrives after the owner's clear
removed the row.

### Cancel the run when its loop stops

Rejected. Tying a workflow run's lifetime to the loop that launched it would
close the delayed proxy of §5 by ending the run, but the run is not the loop:
`workflow_run` is a tool any turn holds, the run may carry unrelated work the
turn delegated to it — research, a build, a review — and `ctx.nudge` is one
optional call at its end. Cancelling every run a tab launched whenever that
tab's loop stops would turn the loop's kill switch, and the owner's clear,
into a kill switch for work the owner never asked to stop. The run
lifecycle also has its own contract already: `cancel` on the workflow
service is addressed by run id, and the teardown drain in
`src/kiro_crew/workflows/service.py` records an arm still in flight at the
run's end as undetermined rather than cancelling it mid-authorize, precisely
because a cancelled arm leaves partial state ambiguous. §5 instead lets the
run finish and refuses the one call that would act on a commitment that has
ended, recording the refusal where the run's other nudge outcomes land.

### Refuse every `ctx.nudge` on a slot that ever held a loop

Rejected. Marking a slot once a loop has lived on it and refusing every later
`ctx.nudge` there would close the delayed proxy without a generation, but it
is over-broad: a person who ran a bounded `monitor_start` on a tab last week,
and today runs a workflow of their own from the same tab, would find its
`ctx.nudge` refused for a loop that ended long ago, with only a reset of the
slot to clear the mark — a mark that describes history, not a live
commitment. The generation refuses exactly the arm that was launched under a
commitment which has since ended or been replaced, and lets a run launched
under the slot's current state arm as §5 and §6 say, so the same tab stays
usable for workflows between loops.

### Extend `_EXTERNAL_ARM_REFUSED_MODES` to every mode

Rejected. The base already tells a person's turn from an automation's at the
directive consumer — `apply_session_directive` admits as the session's own a
human-started turn or the loop's own wake — and refuses the rest, but only
on a crew or member slot, because `_EXTERNAL_ARM_REFUSED_MODES` in
`src/kiro_crew/autonudge_authz.py` is `frozenset({"crew", "member"})`. Adding
every ordinary mode to that set would refuse the seventh variant of §5 by
refusing every automation-originated arm on every slot, and it is the wrong
signal: a person's cron that posts to its origin tab and arms a loop there,
or a subagent they dispatched whose completion re-arms the watch it was
asked to set up, would be refused forever, on a slot whose commitment never
moved, and the loop's own wake — the turn the seventh variant is about — is
admitted by that gate as a self-arm by design, so the mode set would not
even reach it. The question is not who started the turn but whether the
commitment the automation was scheduled under still stands, and the
generation answers that where the mode cannot.

### Treat every automation-originated turn as the loop's own

Rejected. Deciding every turn with `_directive_user_origin` `False` as if
the loop's own session had issued it — inheriting the committed record,
refusing at a retained stalled record — would close the seventh variant
without a captured generation, but it misreads the person's automation as
the loop's. A person who set a cron on a tab with no loop, and whose slot
nothing has cleared or recommitted since, holds a legitimate arm when that
cron's turn calls `monitor_start`, exactly as the same tab's `ctx.nudge`
does on a slot the run's launch still matches; treating it as the loop's
would refuse or cap it against a commitment that does not exist. The
generation tells the two apart with one comparison: the automation carries
the generation its scheduling turn captured or inherited, a slot that never
moved still carries it and the arm proceeds as today, and a slot the owner
has since ended or recommitted does not and the arm is refused. That is the
rule §5 already applies to a run, extended to the turns automation starts,
and it needs no reading of intent.

### Capture the slot's current generation at every scheduling call

Rejected. It is the simpler wiring — `spawn_run`, `workflow_run` and
`cron_add` read the slot's generation when called, whoever calls them — and
an earlier draft of §5 stated it. But the reader is then the scheduling turn,
not the commitment: an automation-originated turn whose own `monitor_start`
was just refused as stale can call `spawn_run` or `cron_add` once more, and
the hop it schedules reads the generation the owner's ending advanced to,
matches it, and re-arms the cleared slot. The rule defeats itself at chain
length two, and the guarantee §5 gives — that a cron the loop created cannot
re-arm the slot after the owner's ending — would hold for the first hop only.
Inheritance closes it without a second mechanism: a turn or run that carries
a generation hands that same value to anything it schedules, a turn carrying
none hands none, and only a turn with authenticated-human provenance captures
the slot's current one, so the stale value travels the whole chain and only a
person's turn can put a fresh one into it (§5).

### Advance the generation on a retained self-session stop

Rejected. An earlier draft listed the retention among the endings that
advance the slot's generation, on the reasoning that a stop is a stop. But
the retained record is the commitment continuing — its remaining budget, its
marker state and its committed classification persist, and §5 lets the same
session re-arm it inheriting that budget. Advancing the generation would make
that inheriting re-arm, and every proxy the loop launched before the stop,
stale and refused, contradicting the rule it is meant to serve, and it would
hand the loop's session a way to move a counter only the owner and the
service's true endings are meant to move. Only an ending advances it; the
retention is not one (§5). The person's own ending is not retained: it comes
through the owner's route the agent cannot invoke — `DELETE
/api/autonudge/{id}`, or `/goal clear` with authenticated-human provenance —
which removes the row and advances the generation; the agent's stop tool is
retained from every turn, the person's included, because which tool was
called, not whose turn it was, is what the stop wrappers can trust (the
alternative "Let turn provenance end the commitment").

### Let any `/goal` through as today

Rejected. The case for it is that `/goal` is a command a person types, and
that its 50-cycle budget is finite whoever issues it. But on the measured
base the slash dispatch in `src/kiro_crew/dashboard/chat_runner.py` hands the
message to `_handle_goal_command` before anything reads the turn's
provenance, and an enabled app holding the `sessionApproval` grant can send
`/goal` or `/goal clear` through `POST /api/chat` into a person's slot, where
it runs — or queues behind a running turn and drains — with
`_directive_user_origin` `False`. Left as it is, that entry would arm a fresh
50 cycles after the owner's clear, or clear the owner's goal, by a door the
directive consumer never sees, and the rule §5 builds for
automation-originated turns would hold at `monitor_start` and not at the
command beside it. Routing the two commands through the same locked
generation check was considered and is not the rule: a slash command is not
scheduled, so it captures no generation to compare, and a person's `/goal`
should not have to. The commands are instead gated on the provenance the
turn already carries — the person's own only when `_directive_user_origin` is
`True`, refused otherwise with the refusal reported into the turn — which
changes nothing for the person typing into the tab (§5).

### Keep the unconditional revival

Rejected. The Issue Radar watchdog's `if not loop.active: await
svc.update(loop.id, active=True)` exists so that a live crew whose clock a
restart or a pause took away gets it back — "enabled" meaning enabled — and
that purpose stands. But it reads no `stopped_reason`, so it also revives a
loop the timer deactivated with `stopped_reason="approval_stalled"`, and
`AutoNudgeService.update` clears the marker on the revival. For the one
surface that arms `0` by design, the stall stop §5 keeps for every unbounded
loop therefore lasted a poll interval on the measured base, and the
unanswered approval it recorded was answered by the app's clock rather than
by a person. Reading the sealed commitment record costs one lookup through
the service, in the gateway process where both watchdogs run, and keeps every
other revival: a crew never committed is armed on the first pass, a loop
paused and
resumed is revived — its commitment open — and only a loop whose commitment a
service ending closed waits for an explicit resume through
the app's own control or the owner-gated `PATCH` (§5) — a revival the resume
route performs itself, reopening the commitment, because a stalled crew
stays `enabled` and the
watchdog cannot tell a resume from an unattended live crew. The same lookup
must guard the watchdog's other arm: its `loop is None` branch calls
`launch_crew`, a fresh `svc.add`, so a rule that read the record only before
a revival would still let a stalled row's deletion from the store, then a
restart, buy a fresh loop on the ended slot; the record is read before that
arm too, and an ended slot with no row is armed by Resume alone. Giving the
crew
runtime a finite default instead would have made the loop bounded and let it
consume the marker, which is the decision deferred below, not a repair of the
revival. The same reading rejects the auto-research `_watchdog_loop`'s
revival as it stands: its `if svc is not None and loop is not None and not
loop.active: await svc.update(loop.id, active=True)` exists to re-arm a loop
an app-disable suspended, by its own comment, and reads `stopped_reason` only
to recognise the research tombstone, so it too revives a stalled loop of a
RUNNING campaign and clears the marker; reading the sealed record keeps the
re-arm it exists for and leaves the stall to the app's explicit `resume`,
which Phase 2 has re-arm through `_launch_loop` under the route's own
owner-resume act, or the owner-gated `PATCH`
(§5).

### Key the watchdog revival on the row's `stopped_reason`

Rejected. An earlier draft of §5 had each watchdog leave a loop deactivated
with `stopped_reason="approval_stalled"` inactive and revive one deactivated
for any other reason — one comparison against the row. But the row lives in
`~/.kiro/crew/autonudge.json`, the store `src/kiro_crew/autonudge.py` calls
agent-writable out of band, and the watchdog's own revival is `svc.update(...,
active=True)`, which clears the marker. A reason forged onto the stalled
row from a sandboxed shell — any reason but the stall's — survives a restart,
because `_load` holds an inactive row beside an ended record as today, and
the next pass revives the loop with its marker gone: the stall stop undone by
a file write, on the surface that arms `0` by design. Deleting the row is the
same write with nothing to forge: the Issue Radar watchdog's `loop is None`
branch arms a fresh loop through `launch_crew`, so a rule keyed on the row's
reason, or on the row's presence, is one `rm` away from a reopened
commitment. §5 has both watchdogs
decide from the sealed commitment record instead — an ended commitment is
neither revived nor re-armed by a watchdog, an open one is revived as today,
an absent one is armed as today — and keeps the
row's reason as display and audit for that decision, as §5 already reads the
committed pair from the record and never from the row.

### Seal the provenance triple without the definition

Rejected. An earlier draft of §5 sealed the automation's id, origin slot and
generation and authorized the injection against that triple alone. The
triple binds which automation fires from which generation, not what it says,
and the measured base rewrites what an automation says in place: `cron_update`
in `src/kiro_crew/mcp_cron.py` is gated by `_check_cron_job_ownership` on the
job's `session_key` and rewrites the message and schedule through
`CronService.update_job`, and `spawn_steer` rewrites a running run's turn
through `steer_run` and `follow_up_run`. A turn whose arm was refused as
stale could therefore write its payload into a cron a person's turn created
after the owner's ending, and the job's next injection, matching its triple,
would carry that payload and its arm into a turn whose generation is current
— the ending defeated one hop sideways rather than one hop down. §5 seals a
digest of the definition beside the triple, gates the update on the
generation, and verifies both at the fire and at the injection, so a rewrite
the gateway did not reseal injects nothing (§5).

### Seal every field the fire path reads

Rejected. A later draft of §5 widened the digest from the definition to
"every field the fire path reads to decide whether and what to run", so that
no field the store's writer could edit was left outside it, and resealed
only on the update paths. That class is too wide and that reseal list too
narrow, and the two together stop valid work: `build_cron_session_context`
in `src/kiro_crew/cron_service/identity.py` reads `job.last_result` into a
persistent job's
next prompt, `_cron_callback` writes it through `CronJob.set_run_result`, and
`CronService._merge_job_result` persists it after every run without any
update path being taken, so the first recurring run would invalidate its own
job's digest and the next fire would fail closed on the gateway's own write;
`_merge_terminal_state_locked`, the ack paths and the loop-stall brake write
the row the same way. Meanwhile the owner's pause, which `_on_timer` and
`_compute_next_run_ts_raw` read as `enabled`, was reached by
`_enable_job_locked` from `api_cron_enable`, `cron_pause` and `cron_resume`,
`CronSDK.set_enabled` and the CLI's `pause` and `resume`, none of which the
draft resealed, so a shell's rewrite of a paused row resumed it. §5 now
seals the definition — the owner's pause included, as `_record_user_paused`
derives it — and, of the run state the gateway writes, exactly the fields
the fire path hands the run as input, `last_result` with its stamps and
`acked_items`, whose writers — `_merge_job_result`, `_ack_job_locked` and
`_unack_job_locked`, gateway writes all — reseal in their own transition,
so the first recurring run reseals its own job's digest rather than
invalidating it, and, of a loop's run state, the observation a
structured wake is built from, the spend the budget stop is decided by and
the decision state the engine, the controller or the gate decides by, whose
service writes reseal the same way (§5); the display and timing state
stays outside the digest,
re-derived at the decision from sealed inputs where a decision read it,
with the `every` anchor moved to the fire time the gateway stamps on the
sealed record; and Phase 2 classifies every writer of the store by the
class of field it writes, resealing on each writer of a sealed field, the
prompt-bearing state's writers among them, and on none of display or timing
state (§5, Phase 2).

### Drop the empty-case blur normalization instead of tracking provenance

Rejected as the rule. Leaving an emptied cycles field empty on blur would make
what the popover shows agree with the 50 it commits, and an implementation may
do that as well, but it does not decide the case: clearing the field fires
`onChange` before any blur, so a cap-commitment marker keyed to an edit would
still be set by the clear alone. Only the provenance of the value — whether
the field was ever given a non-empty value that parses — tells a committed
`0` from a normalized blank (§6), so that is the rule Phase 1 implements.

### Let the agent grant itself access

Rejected. That turns remediation into escalation and makes the same model that
encountered a control responsible for removing it. Human-only authorization
stays human-only.

### Reuse the `monitor_start` defaults for dashboard goals

Rejected for Phase 1. Reusing 24 cycles and 14,400 seconds would put a second
default on the dashboard goal surfaces that disagrees with the 50 cycles `/goal`
already ships, and at a goal's 15- or 60-second idle the pair is sized for a
different envelope (§6). Matching `/goal` keeps one goal budget and changes no
shipped contract. Both pairs are finite, which is the property that matters.

### Give `ctx.nudge` and Issue Radar a finite default here

Deferred. Each cap belongs to its own caller's contract, and under §5 a loop they
arm with no bound keeps the stall stop it has today, so the decision is safe
without touching them — once that stop holds against the Issue Radar watchdog,
which §5 makes it do in Phase 2. The slot rule of §5 reaches a `ctx.nudge`
aimed at a committed loop's own slot, and its generation refuses one from a
run whose commitment has since ended, without moving that default. A later RFC
may revisit either.

### Land the timer rule and the generation in one PR

Rejected. An earlier draft of the Migration plan had implementation PR #13000
carry everything from the contract text to the commitment generation, the
injector and queue changes, the channel provenance, the `/goal` gate and the
watchdog rule. That is one defect fix bundled with a cross-cutting provenance
system, against this directory's rule that phases be independently shippable
and independently abandonable. The two halves have different blast radii —
Phase 1 touches the timer, the service's row transitions through one helper,
the commitment
leaf they write and its four list entries — three in
`src/kiro_crew/sandbox.py`, one in `src/kiro_crew/security/paths.py` —
three applier directives, the popover, the REST route and
instruction files; Phase 2 touches the authorizer, the workflow path, five
injectors, the queue, every channel consumer, the slash dispatch, the sealed
provenance record for scheduled automation, and two app runtimes' watchdogs
and one's resume route — and different failure modes, and the
first is useful and
correct on its own: the observed defect is the timer retiring a bounded goal,
and Phase 1 fixes it. Splitting them means Phase 1 alone leaves the proxy and
automation variants open, which the Migration plan states rather than hides,
and lets Phase 2 be reviewed, landed or abandoned on its own evidence.

### Spend the approval credit only on a stall attributed to a delivered cycle

Deferred, not rejected. It would remove the one imprecision §3 accepts — an
unanswered prompt in a person's own turn on a tab that also hosts a bounded loop
spending one of that loop's cycles — but the runtime has no reliable way to
tell, at the moment a dashboard prompt times out, whether the turn that opened
it was a loop cycle: the slot's turn outlives the fire window the timer holds,
which is why `notify_approval_stalled` records slot-level evidence today. Adding
a durable per-turn provenance mark to the approval path is new mechanism outside
this decision. Until then the cost is one budgeted, authority-gated cycle per
consumed marker on a bounded loop and none on an unbounded one.

## Open questions

None for Phase 1, and Phase 2 is blocked on none: its entry condition is
Phase 1 on `main`, not an answer this document lacks. A separate future RFC
may choose a non-terminal adaptive backoff schedule for repeated human-only
approval checks, provided it preserves active goal state and the authority
ceiling above; may give `ctx.nudge` and the Issue Radar crew runtime a finite
default of their own; and may attribute approval evidence to the delivered
cycle that produced it.
