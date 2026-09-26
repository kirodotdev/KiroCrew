---
title: Goal blocker lifecycle — remediate without retiring autonomous work
status: accepted
author: rubencu
created: 2026-09-23
last-audited: 2026-09-25
audited-at: f6d8b7f8c8
doc-pr:
implementation-prs: [13000]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Goal blocker lifecycle — remediate without retiring autonomous work

- Status: accepted when this document lands. Maintainer review and merge of this
  RFC is the decision; implementation PR #13000 remains blocked until then.
- Measured against `f6d8b7f8c8`: legacy prompt loops use `AutoNudgeService` in
  `src/kiro_crew/autonudge.py`, whose `add` defaults both `max_cycles` and
  `max_runtime_secs` to `0`, and a `NudgeLoop` carrying `0` for both is
  unlimited. Eight surfaces arm such loops. `/goal` composes its recurring
  instruction in `src/kiro_crew/dashboard/chat_runner.py` and caps its loop at
  50 cycles with no wall-clock budget. The dashboard Set-a-goal popover
  (`website/src/components/AutoNudgePopover.tsx`) always serializes
  `max_cycles`: its field is seeded from the live loop, from a remembered draft,
  or with `0`, and `parseCycles` turns an empty field into `0`, so a fresh goal
  whose creator never touched the field is armed at `0` and the popover labels
  that `0` as infinite. `POST /api/autonudge` (`api_autonudge_start` in
  `src/kiro_crew/dashboard/handlers/autonudge.py`) stores an omitted
  `max_cycles` and `max_runtime_secs` as `0`; its only shipped caller is that
  popover, which never omits the field. `monitor_start` in
  `src/kiro_crew/mcp_tools/control.py` is the one surface that already bounds by
  default: an omitted `max_cycles` becomes `_MONITOR_DEFAULT_MAX_CYCLES` (24)
  and an omitted `max_runtime_secs` becomes `_MONITOR_DEFAULT_MAX_RUNTIME_SECS`
  (14,400 seconds), both from `src/kiro_crew/mcp_tools/_limits.py`, and its
  schema refuses either field below `1`, so at the tool boundary it cannot
  request an unbounded loop. The tool only encodes a directive; the loop is
  created by the session-directive applier `_monitor_start` in
  `src/kiro_crew/dashboard/session_directive_apply.py`, which reads
  `int(args.get("max_cycles") or 0)` and `int(args.get("max_runtime_secs") or
  0)` and so would arm `0` for a field a directive lacks. The tool writes both
  fields into every payload it emits, so that fallback has no shipped producer,
  but the bound lives at the tool, not at the applier. The Spec Builder handoff
  (`src/kiro_crew/apps/builtins/spec_builder/backend/handlers.py`) arms its own
  finite `_EXEC_MAX_CYCLES` (60) with no wall-clock budget. Auto-research
  (`src/kiro_crew/apps/builtins/auto_research/handlers.py`) arms the campaign
  row's `max_cycles`, whose schema column is `NOT NULL DEFAULT 30` and whose
  insert path writes `config.get("max_cycles", 30)`, so a default campaign is
  bounded at 30 cycles; only a creator who explicitly submits `0` arms an
  unbounded worker loop (the cap is validated against `MAX_CYCLES_HARD_CAP`
  only, and `_reserve_cycles` treats `0` as unbounded). That `0` unbounds the
  loop record, not the campaign: the same module's watchdog completes a RUNNING
  campaign when `count >= row["max_cycles"]`, which a `0` cap satisfies on the
  first recorded cycle result, so the campaign lifecycle ends there while the
  AutoNudge record it armed stays classified unbounded under §5. The worker
  slot's tools are auto-approved under a 24-hour trust grant
  (`_TRUST_TTL_SECS`).
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
  `max_cycles` to `0` and has no runtime parameter at any link of that chain,
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
`ctx.nudge` arrives after the owner's sentinel or clear removed the loop, or
a subagent completion or cron injection that starts a turn on the slot after
that ending — arms nothing (§5).
Structured monitors retain their typed terminal outcome when an accepted
action cannot be delivered.

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
§5 instead of an unconditional stall stop.

### 2. Authority ceiling

“Fix the owning permission/configuration” means changing an application-owned
policy or configuration only when the current authorization already allows that
change and the result is least privilege. It never means granting the agent
itself access, changing the approval mechanism that refused it, weakening a
governance rule, bypassing a safety control, or widening its own loop: a bound
the loop's session writes for itself neither reclassifies the loop nor raises a
live bound above the pair its owner committed — it may tighten, or restore up
to that pair, never past it — a stop the loop's session issues retains the
record with its remaining budget, its marker state and its committed
classification rather than opening the slot to a fresh self-armed loop, a
re-arm from that session inherits what the record retains instead of
committing new bounds, an arm that session directs at its own slot through a
proxy — a workflow's `ctx.nudge` on its originating session, which replaces
the active row — carries the commitment and the budget already spent forward
rather than resetting them, a proxy that session launched under a commitment
its owner has since ended or replaced arms nothing at all, a turn that
automation the session set in motion — its own loop's cycle, a subagent or
workflow completion, a cron's origin injection — starts on the slot arms only
under the commitment that automation was scheduled under, never past the
owner's ending of it (§5), and the ending its owner committed is not the
agent's to remove or renew.

When that ceiling leaves only a human action, the loop records the condition,
reports it once, and rechecks later. The agent may perform other safe work in the
same goal between checks.

### 3. Prompt-loop approval evidence

An unanswered prompt records `approval_stalled` in memory and schedules its
persistence: `notify_approval_stalled` in `src/kiro_crew/autonudge.py` sets the
loop's flag and calls `_persist_soon`, a supervised fire-and-forget write whose
failure is only logged. The marker is therefore not durable by itself. If that
write is lost, a restarted service sees no marker, the loop wakes on its
schedule, and the next unanswered prompt records the marker again; nothing was
spent, because no budget is charged and no authority is exercised by recording.
That is why Phase 1 protects the consumption write below, not the recording.
On a legacy prompt or goal loop’s next wake, the runtime:

1. reads the loop's recorded classification — bounded when the committed
   `max_cycles` or `max_runtime_secs` is non-zero, the rule of §5 — a separate
   reading from the `cycle_cap` and `runtime_budget` exhaustion checks the
   timer already runs on the live fields before it reaches approval evidence,
   and one that neither a bound the loop wrote for itself, nor a stop and
   re-arm from its own session, nor an arm that session directed at its own
   slot by proxy can move;
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
`src/kiro_crew/autonudge.py` resolves the loop by slot, and every approval
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
  already spent forward instead of starting a fresh count (§5).
- An unbounded loop still stops on the marker (§5), and neither a cap the loop
  writes for itself in the turn that let the prompt lapse, nor a stop and
  re-arm it issues from that turn, nor a replacement it arms by proxy from
  that turn changes that, so the direction the code
  chose for the case with no other ending — a conservative stop — is
  preserved exactly where it matters.

Requiring attribution to a delivered cycle before spending the credit is
recorded as a future refinement, not a Phase 1 requirement (Alternatives).

### 4. Structured monitors

A structured monitor does not enter the legacy prompt timer branch. Its accepted
action completion is typed. Its approval evidence is the same slot-scoped
boolean §3 describes: when `record_monitor_turn_completion` in
`src/kiro_crew/autonudge.py` charges a completed action turn while the loop's
`approval_stalled` flag is set — set by any unanswered prompt in the slot,
whether or not it belonged to the accepted action — it forces the disposition
to `APPROVAL_STALL` and, unless a spent budget stops the monitor first, records
`approval_stall` on `MonitorState.stopped_reason` with outcome `BLOCKED` and
deactivates the monitor. The monitor does not correlate the stall to the
specific action, and this RFC preserves that behavior unchanged rather than
adding a correlation requirement. That record
is inspectable and restartable, and it remains separate from legacy outer-loop
`approval_stalled` values retained only for pre-upgrade store compatibility.

### 5. Stop conditions

A prompt/goal loop stops only when one of these holds:

- the objective or Definition of Done is complete with concrete evidence;
- the user explicitly asks to stop;
- a configured STOP sentinel fires;
- a host/tooling failure remains unrecoverable after bounded retries and no safe
  work remains; or
- a service-enforced finite cycle/runtime budget is spent.

The last item is a backstop, not success. A human-only approval is absent from the
list because waiting for a person does not satisfy the goal.

The list assumes a finite budget exists to be spent, so the lifecycle change
applies only where one does. One rule decides it, per loop, at every wake that
reads approval evidence, and it reads the loop's **committed bounds** — the
`max_cycles` and `max_runtime_secs` as committed by the surface that armed the
loop or later revised by its owner — not the live stored values:

- A loop is **bounded** when its committed `max_cycles` or its committed
  `max_runtime_secs` is non-zero. A bounded loop consumes the
  `approval_stalled` marker and continues (§3); the budget its owner committed
  is the service-enforced ending.
- A loop is **unbounded** when both committed values are `0`. An unbounded loop
  keeps the terminal approval-stall stop unchanged: it deactivates with
  `stopped_reason="approval_stalled"`, emits `expired`, and stays inspectable
  and re-armable. This is not temporal and does not lapse once Phase 1 lands:
  a loop with no finite bound behaves this way before and after the change.

The stall stop is terminal for every unbounded loop, whatever armed it, and
an app runtime that arms one does not get to undo it. On the measured base
the Issue Radar crew runtime does: `watchdog_cycle` in
`src/kiro_crew/apps/builtins/issue_radar/backend/crew_runtime.py`
re-activates every inactive loop of a live crew on every pass — `if not
loop.active: await svc.update(loop.id, active=True)` — reading no
`stopped_reason`, and `AutoNudgeService.update` in `src/kiro_crew/autonudge.py`
clears `approval_stalled` on an actual revival. A crew loop the timer
deactivated with `stopped_reason="approval_stalled"` is therefore running
again by the next watchdog pass with its marker gone, on an approval nobody
answered, and the one service-enforced ending an unbounded loop has lasts a
poll interval for exactly the surface that arms `0` by design. Phase 1 has
the watchdog leave a loop deactivated with `stopped_reason="approval_stalled"`
inactive, marker intact; only an explicit resume revives it — the app's own
resume control, `POST /crew/pause` with `paused` false, which on the measured
base leaves the re-arm to the watchdog and after Phase 1 revives the stalled
loop as the person's act, or the owner-gated `PATCH /api/autonudge/{id}` —
and the watchdog keeps re-arming a loop a restart lost and reviving one
deactivated for any other reason, as today.

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
`604800` (`MONITOR_UPDATE_SCHEMA` in `src/kiro_crew/validation.py`). The
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
`src/kiro_crew/autonudge.py` enforces the live fields the write lands in. A
loop armed at 24 cycles and 14,400 seconds would run to a thousand cycles and
seven days — the schema ceiling is the store's limit, not the owner's budget —
renewing from its own turn the ending its owner committed, with no stop, no
re-arm and no marker left to refuse it. Phase 1 therefore records the
classification with the loop when a bound is committed and decides every
`max_cycles` or `max_runtime_secs` write from the loop's own session —
`monitor_update`, or any other bound write the session directs at its own
loop — against that record, completing the principle of this section that a
loop's session may spend the committed budget but never enlarge it:

- A self-session write is capped at the committed pair. It may tighten a live
  bound, or restore one up to its committed value, never above it; the cap is
  read per field, so a field the owner committed at `0` has no ceiling of its
  own, and the recorded classification does not move.
- A write asking for more than the committed pair is refused, in the manner
  and voice the applier already refuses a cap at or below the delivered count:
  the refusal names the committed ceiling and the owner routes that recommit
  it, and the live bounds are unchanged. An unbounded request — a `0`, which
  `MONITOR_UPDATE_SCHEMA` already refuses at the tool — asks for more than any
  finite pair and is refused on the same ground should a directive carry it.
- A committed pair of `0` and `0` has no ceiling to exceed. A finite
  self-written bound only tightens the live values; the classification stays
  unbounded, and the loop still stops on the marker at its next wake whatever
  its live cap says. The record exists for exactly this loop.
- The refusal while `approval_stalled` is set stays as the special case: while
  the marker is set, no bound write from the loop's own session is applied at
  all.

Fresh commitments come only from the owner, through `PATCH /api/autonudge/{id}`
behind the popover and the other owner routes this section names below.

A bound write is not the only self-session path that would erase the
commitment. On the measured base a stop issued from the loop's own session —
`autonudge_stop` and `monitor_stop` share `_stop_resolved_loop` in
`src/kiro_crew/dashboard/session_directive_apply.py` — removes an ordinary
dashboard or channel legacy loop's record outright; only a research-owned
slot's loop is deactivated with a retained tombstone, and only a structured
monitor retains a terminal record. In the same module `_monitor_start` arms
with `replace_existing=False` and `replace_stopped=True`, the one path allowed
to displace a retained stopped row, and `_stopped_row_is_replaceable` in
`src/kiro_crew/autonudge.py` lets it displace any system-imposed stop —
`_REPLACEABLE_LOOP_STOP_REASONS`, which includes `approval_stalled` — while
failing closed, preserving the row, for a manual pause, a tombstone or a
reason it does not know. Read together, the turn that let the prompt lapse
can call `autonudge_stop` and then `monitor_start`: the record, the marker and
the committed classification vanish with the removal, a fresh finite loop arms
in their place, and the stall stop is taken by neither. The marker is not the
only thing the removal erases. A bounded loop that consumed its marker and
continued holds the same two tools in its remediation turn, and the same stop
and re-arm would hand it a fresh 24 cycles and 14,400 seconds —
`monitor_start`'s defaults — with the marker already consumed and nothing left
to refuse: a loop renewing its own budget, unattended, past the ending its
owner committed, at every turn it chooses. A rule that refused only the
marker-set case would close one variant and leave that one open.

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
Phase 1 reads is therefore not who issued the arm but what the target slot
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
is the sixth self-widening variant, and the owner's own stop opens it. When a
configured STOP sentinel fires, `_timer` in `src/kiro_crew/autonudge.py`
removes the row — `await self.remove(loop.id)`, a removal, not the
deactivation a spent cap receives — and `DELETE /api/autonudge/{id}` and
`/goal clear` remove it the same way. The run's later `ctx.nudge` then finds
an empty slot, and the fresh-slot rule (c) below, read alone, arms a fresh
loop of a thousand cycles: the owner's explicit stop and the budget the
owner committed, bypassed by a proxy launched before the stop, with no bound
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
tell the two apart. Phase 1 therefore keeps, per slot, a commitment
generation: a counter in the service's persisted state, keyed by slot rather
than carried on the row — `config_generation` on `NudgeLoop` is the module's
existing per-row fence, a removed row takes it along, and an empty slot has
no row at all — advanced by every owner reset or recommit (`DELETE
/api/autonudge/{id}`, a `PATCH /api/autonudge/{id}` recommit, `/goal`, `/goal
clear`), by every fresh commitment an arming surface makes on a slot holding
none, and by every ending of a commitment: the sentinel removal, an explicit
user stop, a spent cycle cap or runtime budget, the timer's stall stop and
its other terminal stops, and the retention of a self-session stop. A
replacement or re-arm that inherits the commitment under the rules below
advances nothing, because the commitment continues. A workflow run captures
its originating slot's generation when it is launched; `_nudge_port` passes
it with the arm, `_wf_nudge_authorizer` hands it to `authorize_and_add_nudge`
beside `slot_key`, `message`, `idle_secs` and `max_cycles`, and the
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
originating slot, the task-runner stage turns in
`src/kiro_crew/dashboard/chat_orchestrator.py` and
`src/kiro_crew/dashboard/handlers/taskrunner.py`, the Issue Radar crew turn
in `src/kiro_crew/apps/builtins/issue_radar/backend/crew_runtime.py`, and
the loop's own delivered wake. On the measured base every one of them passes
`_run_chat` a `_directive_user_origin` of `False`. That flag is the runner's
provenance bit, which `src/kiro_crew/dashboard/chat_runner.py` describes as
authenticated-human provenance — the author typed into the session's own
surface — and which a queue drain grants a turn only when every entry it
consumed carries it. A turn so started holds `monitor_start` as a person's
would, and if the owner's sentinel or clear has removed the loop meanwhile,
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
ended from a person's automation on a slot that never moved. Phase 1
therefore reserves the generation-free arm for a turn with authenticated
human provenance and has every automation-originated turn carry the slot's
generation as a run does: the nudge fire captures it at dispatch, beside the
`config_generation` snapshot `_fire_dashboard_nudge` already takes, and
`spawn_run`, `workflow_run` and `cron_add` capture it at the scheduling call
and hand it to the completion or injection they produce.

Phase 1 decides the stop as it decides the write, and decides every
arm or replacement the loop's session directs at its own slot, by whatever
proxy — a workflow's `ctx.nudge` on its originating session included — and
by whatever turn automation it scheduled starts there, against the committed
record and the generation the proxy was launched or the automation scheduled
under, not only the one made while the marker is set:

- A stop issued from the loop's own session deactivates the loop and retains
  its record — its remaining cycle and runtime budget, its marker state and
  its committed classification — instead of removing it. Its stop reason lies
  outside the system-imposed re-armable set, so the fail-closed rule already
  in `_stopped_row_is_replaceable` refuses to displace it as a fresh
  commitment.
- A `monitor_start`, a workflow's `ctx.nudge` on its originating session, or
  any other arm or replacement the loop's session directs at a slot that
  holds a committed loop — an active row, or a retained self-stopped record —
  commits no fresh bounds. The loop it produces inherits the committed
  classification and the remaining budget: the bounds the arm requests are
  capped at what is left of the committed pair — the cycles not yet delivered
  and the seconds not yet spent — which is the computation the slot-close
  restore `_restore_slot_nudge_loop` in
  `src/kiro_crew/dashboard/chat_handlers.py` already performs when it re-arms
  a retiring loop with its remaining budget, and which, by its own account,
  must not buy cycles the person never granted. A replacement of the active
  row carries the commitment and the budget already spent forward — the
  delivered cycles and the elapsed seconds — rather than resetting them to a
  fresh count. The arm is refused outright while the committed loop carries
  `approval_stalled`, active or retained, because the stalled loop's marker is
  evidence a fresh loop must not inherit or consume, and it is refused when
  the remaining budget is exhausted, as the slot-close restore declines to
  restore a loop whose cap or wall-clock budget is already spent. A committed
  pair of `0` and `0` has no remaining budget to cap against: the loop the arm
  produces takes the requested live bounds and the unbounded classification,
  and stops on the marker at its next wake as §3 says. The arm goes through
  the same gateway authorizer as every other arm a session can direct at its
  own slot, so the rule is decided there, once, for every proxy — and a
  workflow's `ctx.nudge`, or an automation-originated turn's `monitor_start`,
  reaches it only under the commitment its run was launched or its automation
  scheduled under: an arm whose captured generation is no longer the slot's
  is refused before the slot's state is read, so the record this rule
  inherits from is the one the launching or scheduling turn was authorized
  under, never one the owner committed since.
- A fresh slot, one holding no committed loop, keeps each surface's own
  commitment as today, and who may reach it generation-free is the decision
  of this rule. A turn with authenticated-human provenance — the person typed
  into the session's own surface: on the dashboard `_directive_user_origin`
  `True`, which the chat runner hands the directive consumer as
  `producer_is_user_facing`; on a channel, the `producer_is_user_facing` the
  channel consumer passes for a person's inbound message (Phase 1, below) —
  commits a fresh pair with no generation to compare:
  `monitor_start`'s defaults are its committed pair, as today. Every turn
  automation originates — the loop's own cycles, subagent and workflow
  completions, task-runner stage turns, cron-to-origin injections,
  app-driven turns that relay no person's own answer — carries the slot's
  generation captured when the
  automation was scheduled or dispatched, as a workflow run carries the one
  it captured at launch, and its arm is decided as a proxy's is. A matching
  generation reaches this rule and the one above: a person's cron on a slot
  whose generation never moved arms as today, and a `ctx.nudge` or a
  matching turn's `monitor_start` on such a slot commits exactly what §6
  records for its surface — for `ctx.nudge`, `0` unless its caller passes
  `max_cycles`, with no runtime parameter. A mismatch is refused and
  reported into the turn or the run, whether the slot is empty again or holds
  a loop the owner armed meanwhile, and that loop is not displaced. So a
  loop's cap-ending cycle cannot re-arm its slot — its arm meets the active
  row with no budget left, or a generation the cap's ending has advanced
  past — and a cron the loop created cannot re-arm it after the owner's
  ending, while nothing changes for the person typing into the tab. The
  default-cap decision for the two programmatic callers (§6) does not move;
  only the displacement of a committed loop from its own slot, and the arm
  of a proxy or an automation-originated turn whose commitment has ended or
  been replaced, do.
- Only the owner resets a slot's commitment, through the routes
  `_require_monitor_owner` already gates: `DELETE /api/autonudge/{id}`
  (`api_autonudge_delete`, behind the popover's Stop/Clear control) removes
  the record so an arming surface can commit a new pair, and `PATCH
  /api/autonudge/{id}` revives it, the owner write that clears the marker
  (`AutoNudgeService.update` clears it on an actual revival) and commits the
  bounds it carries. `/goal` and `/goal clear` in
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
  Phase 1 has the slash dispatch refuse `/goal` and `/goal clear` from a turn
  or drained entry without authenticated-human provenance, reporting the
  refusal into the turn as the handler reports its other outcomes; a `/goal`
  the person types arms 50 as today. Each of these owner acts advances the
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
`src/kiro_crew/dashboard/session_directive_apply.py` is `False`; all nine
channel transports build their consumer through it, and the Slack gateway's
own nudge-fire turn does too. So `_monitor_start`'s
`initiator_slot_key=binding if self_arm_ok else ""` resolves to `""` for
every channel turn, a person's included — harmless today, because the flag
feeds only the crew/member rule, which refuses an external arm only where the
slot's mode is in `_EXTERNAL_ARM_REFUSED_MODES`, but the fresh-slot rule
would read it as an automation's turn: a person's Slack, Discord or Webex
watch request — a surface `monitor_start` in
`src/kiro_crew/mcp_tools/control.py` documents as supported, and the channel
legacy loop this section names — would be refused on an empty slot. Phase 1
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
to the new default, it is shown as `0`, and it keeps the stall stop. The owner
of such a loop opts into remediation continuity by giving it any finite budget
through an arming surface or the owner-gated `PATCH` route; the loop cannot
give itself one, whether by writing a bound, by stopping and re-arming
itself, by arming a replacement on its own slot through a proxy, by a
proxy it launched before the owner cleared the slot, or by a turn that
automation it scheduled starts on the slot after that clear, since a
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
omits the field that the REST default reads. Two other rows change without
their cap moving: `/goal` gains the provenance gate of §5, and the Issue Radar
crew runtime's watchdog stops reviving a stalled loop (§5).

| Surface | Default cap today | Explicit `0` | Runtime budget | Phase 1 | Unanswered approval after Phase 1 |
|---|---|---|---|---|---|
| `/goal` (`src/kiro_crew/dashboard/chat_runner.py`) | 50 cycles; `--max N` clamps to 1–50 | not expressible | none | cap unchanged; `/goal` and `/goal clear` are dispatched only from a turn with authenticated-human provenance — an app-sent or drained entry without it is refused and the refusal reported into the turn (§5) | bounded: continues |
| Set-a-goal popover (`website/src/components/AutoNudgePopover.tsx`) | field seeded `0` for a fresh goal, empty field parses to `0`, always sent; `0` labelled infinite | unlimited | not exposed; sends none | fresh goal seeds 50; empty or unparseable field commits 50, including the `0` its blur normalization writes into such a field; typed `0` commits `0`; live loop shown as-is; remembered draft restored verbatim only when its cap-commitment marker is present; a legacy or uncommitted-cap draft at `0` restores message and idle and reseeds 50 | bounded: continues; typed or committed-and-remembered `0`: stall stop |
| `POST /api/autonudge` (`api_autonudge_start`) | omitted `max_cycles` stored `0` | unlimited | omitted stays `0`; explicit value accepted up to `MAX_RUNTIME_SECS_CEILING` | omitted `max_cycles` → 50; explicit `0` stays unlimited; no runtime default | bounded: continues; explicit `0` with no runtime budget: stall stop |
| `monitor_start` (`src/kiro_crew/mcp_tools/control.py`) | `_MONITOR_DEFAULT_MAX_CYCLES` = 24, written into every payload; the applier `_monitor_start` in `src/kiro_crew/dashboard/session_directive_apply.py` that arms the loop would read an absent field as `0`, which no tool payload has | refused (schema minimum `1`) | `_MONITOR_DEFAULT_MAX_RUNTIME_SECS` = 14,400 s when omitted, written into every payload; `0` refused | unchanged | bounded on every tool-produced payload: continues |
| Spec Builder handoff (`src/kiro_crew/apps/builtins/spec_builder/backend/handlers.py`) | `_EXEC_MAX_CYCLES` = 60 | not expressible | `0` | unchanged | bounded: continues |
| auto-research (`src/kiro_crew/apps/builtins/auto_research/handlers.py`) | campaign row, `NOT NULL DEFAULT 30`; insert writes `config.get("max_cycles", 30)` | accepted (no lower bound below `MAX_CYCLES_HARD_CAP`); unlimited loop record, but the watchdog completes the campaign on its first recorded cycle (`count >= row["max_cycles"]`) | `0` | unchanged | default campaign is bounded: continues within its remaining cycles; explicit `0` campaign: loop record keeps the stall stop, as today, while the campaign itself completes on its first recorded cycle |
| `ctx.nudge` (`src/kiro_crew/workflows/runner.py`) | `0` (signature default) | unlimited | no parameter at any link (`_nudge_port`, `_wf_nudge_authorizer`); `0` | default excluded; on a slot holding a committed loop the arm inherits that loop's classification and remaining budget instead of replacing the row with a fresh count, and a run launched under a commitment that has since ended or been replaced is refused (§5) | default `0`: stall stop; a script that passes a finite `max_cycles`: continues |
| Issue Radar crew runtime (`src/kiro_crew/apps/builtins/issue_radar/backend/crew_runtime.py`) | arms `0` by design | unlimited | `0` | default cap excluded; `watchdog_cycle` no longer revives a loop deactivated with `stopped_reason="approval_stalled"` — only the app's own resume (`POST /crew/pause` with `paused` false) or the owner-gated `PATCH` does; other inactive loops are revived as today (§5) | stall stop, and it holds: on the measured base the watchdog revived the loop and cleared the marker on its next pass |

The popover change is what makes a dashboard goal finite, because `save()`
serializes `max_cycles` on every submit and `api_autonudge_start`'s omission
default therefore never fires for it. Phase 1 seeds a fresh goal — no live loop
on the slot and no remembered draft — with 50, and an empty or unparseable field
commits as 50 instead of `0`. A typed `0` is a value, not a blank, and commits
as `0`. A live loop's field shows its stored `max_cycles`, `0` included, and is
not rewritten to the default.

The committed value alone cannot carry that distinction, because the field
rewrites itself. Its `onBlur` writes `parseCycles(maxCyclesInput)` — `parseInt`
falling back to `0` — back into the field, so a field a person emptied, or one
whose text does not parse, shows the literal `0` before `save()` reads the raw
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
and `save()` would send it, arming an unbounded goal from a cap nobody
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
  again, which commits the cap and sets the marker. A non-zero legacy cap is
  restored as it was; it is finite either way.

The pre-upgrade message-only draft is the case this closes: without the marker,
Phase 1 would seed a fresh goal at 50 and still arm the next goal on that slot
at `0`. The REST omission default is the backstop for a caller other than the
popover that leaves the field out of its body; with the popover fixed it has no
shipped caller that reaches it, and that is the point: both routes into a
dashboard goal must be finite by default independently.

Auto-research is not changed by Phase 1 and its consequence is the one the rule
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
deactivate the loop record.

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
not the applier's. The slot-close restore `_restore_slot_nudge_loop` in
`src/kiro_crew/dashboard/chat_handlers.py` re-arms an existing loop with its
remaining cycle and runtime budget and inherits its recorded classification;
it is the computation a self-session re-arm of a retained record reuses (§5).
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
on every pass, stall stop included, clearing the marker with the revival. §5
makes that stop hold there as it holds everywhere: the watchdog leaves a loop
deactivated with `stopped_reason="approval_stalled"` inactive, and only an
explicit resume revives it. A later RFC may give either a finite default. The
exclusion is of the default cap, not of the slot rule: a
`ctx.nudge` aimed at a slot that already holds a committed loop is an arm the
loop's session directs at its own slot, and §5 decides it as it decides a
`monitor_start` there — it inherits the committed classification and the
remaining budget rather than replacing the row with a fresh count. On a fresh
slot it commits exactly what the row above records, and it does so only while
the slot's commitment generation is the one its run captured at launch; a
run that outlived the loop, or the clear, it was launched under arms nothing
(§5).

## Migration plan

### Phase 0 — decision record

Land this RFC independently of implementation PR #13000.

Exit criteria:

- the RFC is on `main` with status `accepted`;
- maintainers have explicitly accepted the blocker lifecycle and authority
  ceiling through normal RFC review; and
- the implementation PR references the merged document.

### Phase 1 — implementation

Implementation PR #13000 aligns all instruction producers named in §1, including
the bundled babysit and prepare-pr skills and the repo-checkout goal-loop
skill, fixes the generated self-nudge template, splits prompt-loop and
structured-monitor approval behavior, applies the bounded/unbounded rule of §5
at the legacy timer's approval-evidence check on the loop's committed bounds —
recording the classification with the loop when a bound is committed,
refusing a `max_cycles` or `max_runtime_secs` write from the loop's own session
while `approval_stalled` is set, capping every other such write from that
session at the committed pair — a tighten or a restore up to the committed
value is applied, a raise above it is refused with the committed ceiling and
the owner routes named and the live bounds unchanged — retaining the record of
every loop stopped from its own session with its remaining budget, its marker
state and its committed classification, and deciding every arm or replacement
the loop's session directs at its own slot, by whatever proxy — a
`monitor_start` at a retained record, a workflow's `ctx.nudge` on its
originating session, which on the measured base replaces the active row —
against that record at the gateway authorizer `authorize_and_add_nudge` they
all reach: the signal is the target slot already holding a committed loop,
not the arm's provenance, which the workflow path does not carry; the loop it
produces inherits the committed classification and remaining budget rather
than a fresh pair, a replacement of the active row carries the commitment and
the budget already spent forward, and the arm is refused while the committed
loop carries the marker or its budget is exhausted — keeping a per-slot
commitment generation in the service's persisted state, advanced by every
owner reset or recommit and by every ending of a commitment, captured by a
workflow run at launch, carried by `_nudge_port` and `_wf_nudge_authorizer`
to `authorize_and_add_nudge`, and compared under the service `_lock` where
the slot's row is read, so a `ctx.nudge` from a run launched under a
commitment that has since ended or been replaced is refused whatever the slot
now holds and the refusal is recorded in the run's stream — reserving the
generation-free fresh-slot arm for a turn whose `_directive_user_origin` is
`True`, and having every automation-originated turn carry the slot's
generation to the same authorizer: the nudge fire captures it at dispatch
beside the `config_generation` snapshot `_fire_dashboard_nudge` already
takes, `spawn_run`, `workflow_run` and `cron_add` capture it at the
scheduling call and hand it to the completion or origin injection they
produce, the task-runner and app injectors capture it when they dispatch,
and when the injector queues rather than starts the turn the generation
rides the queued entry beside `_directive_user_origin`, process-local as that
flag is, so the drain hands `_run_chat` both, the session-directive consumer
passes the generation to `authorize_and_add_nudge` where it already passes
`initiator_slot_key`, and a mismatch is refused and reported into the turn
as the run's is into its stream — having the channel directive consumer
`build_directive_consumer` in `src/kiro_crew/messaging/dispatch.py` pass
`producer_is_user_facing` to `apply_session_directive` for a turn a person's
inbound message started, and withhold it from a bot- or automation-authored
channel message and from a channel loop's own wake, before that flag becomes
the fresh-slot arming gate, so a person's Slack, Discord or Webex watch
request arms a fresh slot as today — gating the `/goal` slash dispatch in
`src/kiro_crew/dashboard/chat_runner.py` on the same provenance, so `/goal`
and `/goal clear` are dispatched only from a turn whose
`_directive_user_origin` is `True` and are refused, with the refusal
reported into the turn, from a turn or drained entry without it, an app's
`POST /api/chat` send included — having the Issue Radar `watchdog_cycle`
leave a loop deactivated with `stopped_reason="approval_stalled"` inactive
with its marker, reviving it only on the app's own resume (`POST /crew/pause`
with `paused` false) or the owner-gated `PATCH /api/autonudge/{id}`, while it
keeps re-arming a loop a restart lost and reviving one deactivated for any
other reason — makes a dashboard
goal finite by
default on both of its routes — the Set-a-goal popover seeds and commits 50 for
a fresh untouched goal, for an emptied or unparseable field whatever its blur
normalization shows, and for a remembered draft whose `0` cap was never
committed (§6), and `POST /api/autonudge` stores an omitted `max_cycles`
as 50 — matching the `/goal` budget, with an explicit `0` still meaning
unlimited and `max_runtime_secs` staying opt-in, leaves the bounds that
`monitor_start`, the Spec Builder handoff, auto-research, `ctx.nudge` and the
Issue Radar crew runtime commit on a fresh slot unchanged, and adds durable
transition, write-failure, gate-bypass, and contract tests. None of this
behavior is on `main` until that PR merges; the measured base above remains
the current runtime state.

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
- permission remediation text prohibits self-grant and governance weakening;
- a bounded prompt loop (non-zero committed `max_cycles` or `max_runtime_secs`)
  remains active after consumed approval evidence;
- an unbounded prompt loop (committed `max_cycles=0` and `max_runtime_secs=0`)
  still deactivates with `stopped_reason="approval_stalled"` and emits
  `expired`, whether the `0` was typed explicitly, armed by a pre-upgrade
  popover for an untouched field or stored by a REST body for an omitted one,
  written by the owner through the loop's `PATCH` route, armed by `ctx.nudge`
  or the Issue Radar crew runtime, or submitted as an auto-research campaign
  cap; a test pins both branches;
- the classification is recorded with the loop when its bound is committed,
  and neither a bound written from the loop's own session nor an arm that
  session directs at its own slot by proxy moves it;
  `monitor_update` refuses a `max_cycles` or `max_runtime_secs` write from the
  loop's own session while `approval_stalled` is set; a test arms an unbounded
  loop, records the marker, has the loop's own session call `monitor_update`
  with `max_cycles=1000`, shows the call is refused, and shows the loop still
  deactivates with `stopped_reason="approval_stalled"` at its next wake;
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
- a stop issued from the loop's own session retains a stopped record carrying
  the remaining cycle and runtime budget, the marker state and the committed
  classification, whether or not `approval_stalled` is set, and a re-arm from
  that session at such a record commits no fresh bounds: it is refused while
  the record carries the marker or its budget is exhausted, and otherwise
  arms a loop that inherits the record's classification and remaining budget;
  three tests pin it: one arms an unbounded loop, records the marker, has the
  loop's own session call `autonudge_stop` and then `monitor_start`, shows the
  re-arm is refused, and shows the retained record still carries
  `approval_stalled` and its unbounded classification; one arms a bounded
  loop, records the marker, lets the loop consume it and continue, has the
  loop's own session call `autonudge_stop` and then `monitor_start` with
  `max_cycles=24` and `max_runtime_secs=14400`, and shows the new loop carries
  only the cycles and seconds the retained record had left and stops when
  they are spent; and one arms a bounded loop, delivers its last budgeted
  cycle, has that cycle's turn call `autonudge_stop` and then `monitor_start`,
  and shows the re-arm is refused until the owner clears the record; a
  companion test shows the owner-gated `DELETE /api/autonudge/{id}` removes a
  retained record so a fresh pair can be committed and the owner-gated `PATCH
  /api/autonudge/{id}` revival clears the marker;
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
  `ctx.nudge(max_cycles=1000)` on the originating session, fires the loop's
  configured STOP sentinel so `_timer` removes the row, lets the script's
  call arrive, and shows the arm is refused, the refusal lands in the run's
  stream as a “ctx.nudge NOT armed” message and the slot stays empty;
  companions replace the sentinel with the owner-gated `DELETE
  /api/autonudge/{id}` and show the same refusal, let the loop spend its cap
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
  marker and stop reason; a companion resumes the crew through `POST
  /crew/pause` with `paused` false and shows the loop revives with the marker
  cleared; another revives it through the owner-gated `PATCH
  /api/autonudge/{id}`; and another deactivates a live crew's loop for a
  reason other than the stall, or removes it as a restart would, and shows
  the next watchdog pass revives or re-arms it exactly as on the measured
  base;
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
  holds no committed loop; a `ctx.nudge` aimed at a slot that holds one, or
  issued by a run whose commitment has since ended or been replaced, and a
  `monitor_start` from a turn automation originated, are decided by §5, as
  the tests above pin;
- finite bounds and explicit stop controls still pass their existing tests; and
- hand-written and scaffolded self-nudge instructions match.

## Backward compatibility

No accepted API input, stored key, monitor kind, or tool is removed or renamed.
Pre-upgrade outer-loop `approval_stalled` records remain readable and re-armable.
The behavior change is deliberate and scoped: new prompt-loop approval evidence
no longer creates that terminal outer-loop state on a bounded loop. A loop that
carries `max_cycles=0` and `max_runtime_secs=0` from before the upgrade, whether
its creator typed the `0`, left the popover field untouched when it seeded `0`,
or omitted the field from a REST body the route stored as `0`, is treated as
unbounded (§5): its stored values are not migrated or rewritten, it is shown as
`0`, and it keeps the terminal approval-stall stop it has today until its owner
gives it a finite budget. A pre-upgrade loop carries no recorded classification;
Phase 1 records one from its stored bounds the first time the timer reads them
and treats those values as the committed pair, because the store holds no other
evidence of what was committed, so a stored `0` for both is recorded unbounded,
any non-zero pair is recorded bounded, and a later write from the loop's own
session neither moves it nor raises a live bound above that recorded pair, and
a `ctx.nudge` its session aims at the slot inherits that recorded pair and
what the loop has spent of it rather than replacing the row with a fresh
count. A pre-upgrade stopped legacy record needs no
migration either: on the measured base a self-session stop removed the record,
so no retained record of the kind §5 introduces predates the upgrade and a
pre-upgrade slot holds none — the first self-session stop after the upgrade
creates one, carrying the budget that loop had left and the classification
Phase 1 recorded for it; a record the timer deactivated with
`stopped_reason="approval_stalled"` keeps the treatment it has today,
displaced by a directive re-arm as before; and a research tombstone stays the
retained evidence it already is. A pre-upgrade store holds no per-slot
commitment generation; Phase 1 reads an absent generation as `0` for every
slot, as the store already decodes an absent `config_generation` to `0`, and
counts from there, so the first reset or ending after the upgrade advances
it. A run that was in flight when the gateway restarted for the upgrade
never reaches the authorizer at all: on the measured base
`RunHandle.from_store_json` in `src/kiro_crew/workflows/registry.py` marks a
run that was still running when the gateway died as failed —
“interrupted: gateway restarted while running” — because it can never resume
in the new process, and a `rerun_subtree` is a new run that captures a
generation at launch. The rule for an arm that carries no captured
generation is stated all the same, and it fails closed: the arm is refused
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
created before the upgrade
carries no captured generation on its record either, so the first
origin-injected turn it produces after the upgrade is refused should it try
to arm a loop; the person who created it re-arms from their own turn, and
the job's later fires capture nothing retroactively — a person who wants
that cron to arm loops recreates it, and the new job captures its generation
at creation. A
goal draft the popover remembered before the upgrade carries no
cap-commitment marker (§6).
Its message and idle are restored as
committed; its stored record is not rewritten until the person edits again; and
if its `maxCycles` is `0` the cycles field is reseeded to 50 rather than
restored, because on the measured base a message-only edit persisted the
untouched seed `0` alongside the message and that `0` was never a choice. A
non-zero legacy cap is restored as it was. Entering `0` again commits unlimited
operation and sets the marker, so a person who wants it keeps it. The fresh
goal with no remembered draft receives the new seed as well.
An Issue Radar crew loop the timer deactivated with
`stopped_reason="approval_stalled"` before the upgrade needs no migration: on
the measured base the watchdog revived it within a pass, so at the upgrade
such a record is either already active again or about to be; after the
upgrade it stays inactive with its marker until the crew is resumed through
`POST /crew/pause` or the loop revived through the owner-gated `PATCH`, the
treatment every stalled unbounded loop has. A crew loop inactive for any
other reason is revived by the first post-upgrade pass as before. A channel
session's loops are unchanged in the store: the provenance the channel
consumer starts passing is a per-turn flag, never persisted, so a loop a
person armed from Slack, Discord or Webex before the upgrade keeps its bounds
and its classification is recorded from them as any pre-upgrade loop's is.
Structured-monitor records are unchanged.

## Security considerations

The primary risk is interpreting remediation as permission to escalate. The
contract therefore names prohibited actions explicitly and tests each
agent-facing surface. Existing security-policy and denied-command enforcement
remain authoritative even if an instruction is malformed.

Durability is also a safety property. Consumed evidence and the gated follow-up
credit are persisted atomically before the model turn. A failed write retains the
old live state and schedules retry, so disk and memory cannot disagree about
whether a recovery turn was already authorized.

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
that classifies a loop is the one its owner committed, a `monitor_update`
bound write made while the marker is set is refused, and so is the stop and
re-arm that would shed the marker with the record — a self-session stop
retains the record with its marker, and a re-arm from that session is refused
while the marker is there (§5). Nor can a bounded loop buy itself more than
its owner committed, by any route its turn holds. A `monitor_update` from
that turn is capped at the committed pair: it may tighten a live bound or
restore it up to that pair, and a raise to `1000` cycles or `604800` seconds —
the schema's ceilings, which the applier's own guards would otherwise let
through once the marker is clear — is refused with the live bounds unchanged.
The record a self-session stop retains carries the budget the loop had left, a
re-arm from that session inherits that remainder and cannot exceed it, and a
spent remainder refuses the re-arm. The widest route needs neither a write
nor a stop: on the measured base a workflow the loop's turn runs can call
`ctx.nudge(max_cycles=1000)` on its originating session, and the
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
outlives the turn and the loop, and on the measured base a STOP sentinel
makes `_timer` remove the row while `DELETE /api/autonudge/{id}` and `/goal
clear` remove it too, so a `ctx.nudge` arriving from that run afterwards
would find an empty slot and arm a fresh loop past the owner's explicit stop,
and a run arriving after a spent cap would displace the replaceable
`cycle_cap` record with a fresh count. The per-slot commitment generation §5
adds is what makes the owner's stop final against work the loop set in
motion before it: the run carries the generation of the commitment it was
launched under, every reset by the owner and every ending advances the
slot's, and an arm whose generation is stale is refused under the service
lock whatever the slot holds. The loop's session gains nothing from that
counter. The only way it moves the generation is by ending its own
commitment — a self-session stop — and a proxy launched after that is
decided against the retained record it inherits from, while every proxy
launched before it is refused; no sequence of launches and stops lets a
proxy of a committed loop arm a fresh pair. Nor is a run the only thing the
loop sets in motion that outlives the owner's ending: a subagent it spawned,
a workflow it ran or a cron it created reports back as a turn on the same
slot, and on the measured base every such injector starts that turn with
`_directive_user_origin` `False` while the external-arm refusal reads only
the slot's mode against `_EXTERNAL_ARM_REFUSED_MODES`, so on an ordinary
dashboard slot the turn's `monitor_start` is admitted as a person's would be
and, after the owner's clear or the loop's spent cap, would arm a fresh pair.
§5 has every automation-originated turn carry the generation captured when
the automation was scheduled or dispatched, so the owner's ending is final
against every turn the loop set in motion as it is against every run: the
loop's last cycle, a completion that lands after the clear, a cron the loop
created — each is refused, and the refusal is reported into the turn. The
generation-free arm is reserved for a turn with authenticated-human
provenance, the bit the runner already withholds from the cron, subagent,
task-runner, crew-runtime and nudge-fire producers, grants an app delivery
only where it relays a person's own answer — the Spec Builder's pending
decision in `src/kiro_crew/apps/builtins/spec_builder/backend/runtime.py`
passes it `True` for exactly that — and grants a queue drain only when every
consumed entry carries it, so no automation of the loop's can wear it. On a
channel the same provenance is the `producer_is_user_facing` the channel
consumer passes for a person's inbound message and withholds from a bot- or
automation-authored message and from the loop's own wake, so a person's
Slack, Discord or Webex watch keeps its fresh-slot arm while the loop's own
channel wake, which reached the applier with neither flag on the measured
base, gets no human provenance after Phase 1 either (§5). The slash dispatch
is held to the same bit: on the measured base `/goal` and `/goal clear`
reach `AutoNudgeService.add` and `remove` before any provenance is read, so
an enabled app holding the `sessionApproval` grant could send either through
`POST /api/chat` into a person's slot and, drained after the owner's clear,
arm a fresh 50 cycles or end the owner's commitment without a person typing
anything; Phase 1 refuses both from a turn or drained entry whose
`_directive_user_origin` is `False` and reports the refusal into the turn,
so the app grant that lets an app speak into a slot does not let it arm or
clear the slot's goal (§5). Nor may an app runtime lift the stall stop from
a loop it armed: the Issue Radar watchdog's unconditional revival on the
measured base turned the one service-enforced ending an unbounded crew loop
has into a pause of one poll interval, marker cleared, with the unanswered
approval still unanswered; Phase 1 has it leave a loop deactivated with
`stopped_reason="approval_stalled"` inactive, so the approval a person did
not grant is not granted by the app's clock, and the loop resumes only
through the app's own resume control or the owner-gated `PATCH` (§5). A
fresh pair still comes only
from the owner's routes, or from a person's turn on a slot that holds no
commitment, visible on the tab and stoppable. Unlimited operation therefore
remains available as an explicit `0`, but it does not also buy remediation
continuity through an unanswered approval: a person who wants both names a
finite budget.

Slot-scoped evidence has a cost this decision accepts knowingly (§3). Because the
marker records any unanswered prompt in the loop's slot, an ignored dialog in a
person's own interactive turn on a tab that also hosts a bounded
`monitor_start` loop spends one of that loop's budgeted cycles, and on a gated
loop that cycle skips one QUIET reading. The cycle carries no elevated
authority and no stall-specific instruction, it is charged to the budget the
owner declared, and the marker cannot stack, so the exposure is bounded by the
loop's own `max_cycles` and `max_runtime_secs`. The conservative stop is kept
exactly for the loops that have no such bound. The slot rule for a proxy arm
has the same shape and the same accepted cost: the workflow path carries no
turn provenance to the authorizer, so a `ctx.nudge` a person's own turn runs
on a tab that hosts a committed loop is decided as the loop's own would be —
it inherits the commitment rather than replacing the row with a fresh one.
That person holds the owner routes, and a fresh pair is one clear or recommit
away; the loop holds none, which is the asymmetry the rule relies on. The
generation carries a second cost of the same shape: a workflow a person
launched from a tab whose loop they then stopped or cleared has its later
`ctx.nudge` refused as the loop's own would be, because the run carries no
provenance that tells the person's launch from the loop's. The refusal is
recorded in the run's stream, and the person, who holds the routes, re-arms
from a new turn. A person's own automation pays the same price on the same
terms: a subagent or cron they scheduled from a tab whose loop they then
cleared has its completion or injection turn's `monitor_start` refused,
because the turn carries the generation of that scheduling and no human
provenance; the refusal is reported into the turn, and the person types the
arm into the tab. Their automation on a slot they never cleared or recommitted
arms as it does today.

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
with the loop, keeps that ending in the owner's hands (§5). Recording the
classification alone does not close the write path, though: the same guard
lets a bounded loop raise its live bounds to the schema ceilings once its
marker is clear, which the alternative after the re-arm below rejects.

### Refuse the self-session stop

Rejected, whether for every self-session stop or only for one made while the
marker is set. A person's explicit stop request delivered in the same turn is
a valid ending (§5) and must not be refused because a prompt lapsed earlier in
the slot or because the loop still has budget left. Retaining the stopped
record with its remaining budget, its marker state and its committed
classification, and letting a re-arm from that session inherit the record
rather than replace it, preserves the evidence and the commitment without
blocking the stop. Removing the record and carrying the marker to the next
loop on the slot was also considered and rejected: the marker is evidence
about the loop that recorded it, and a fresh loop with a fresh commitment must
not inherit or consume it.

### Let a self-session re-arm commit fresh bounds

Rejected. Refusing the re-arm only while the marker is set would have closed
the stall case and left the budget open: a bounded loop that consumed its
marker and continued could call `autonudge_stop` and then `monitor_start`
from its remediation turn, receive `monitor_start`'s 24 cycles and 14,400
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
the target slot — and the default cap `ctx.nudge` commits on a fresh slot is
untouched (§6). The same exemption would also leave the sixth variant open,
the one the next two alternatives address: a run the loop's turn launched
whose `ctx.nudge` arrives after the owner's sentinel or clear removed the row.

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
generation tells the two apart with one comparison: the automation captured
the slot's generation when it was scheduled, a slot that never moved still
carries it and the arm proceeds as today, and a slot the owner has since
ended or recommitted does not and the arm is refused. That is the rule §5
already applies to a run, extended to the turns automation starts, and it
needs no reading of intent.

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
by a person. Reading the stop reason costs one comparison and keeps every
other revival: a loop lost to a restart is re-armed, a loop paused and
resumed is revived, and only the stall waits for an explicit resume through
the app's own control or the owner-gated `PATCH` (§5). Giving the crew
runtime a finite default instead would have made the loop bounded and let it
consume the marker, which is the decision deferred below, not a repair of the
revival.

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
which §5 makes it do. The slot rule of §5 reaches a `ctx.nudge` aimed at a
committed loop's own slot, and its generation refuses one from a run whose
commitment has since ended, without moving that default. A later RFC may
revisit either.

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

None for Phase 1. A separate future RFC may choose a non-terminal adaptive
backoff schedule for repeated human-only approval checks, provided it preserves
active goal state and the authority ceiling above; may give `ctx.nudge` and the
Issue Radar crew runtime a finite default of their own; and may attribute
approval evidence to the delivered cycle that produced it.
