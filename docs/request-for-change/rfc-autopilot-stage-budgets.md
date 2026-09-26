---
title: Autopilot stage budgets admit work; they do not kill productive turns
status: draft
kind: decision
author: junjiequ (incident owner; drafted with Kiro Crew)
created: 2026-09-26
last-audited: 2026-09-26
audited-at: 7f1f4fb1d
revision: 1
doc-pr: 14043
implementation-prs: [13992]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Autopilot stage budgets admit work; they do not kill productive turns

Status: draft. This document asks maintainers to decide how Autopilot bounds a
productive stage and how the dashboard recovers when a real turn ceiling fires.
It must land before the implementation in
[#13992](https://github.com/kirodotdev/KiroCrew/pull/13992) is made ready again.
The implementation PR is a prototype, not the decision source.

- Measured against `main` at `7f1f4fb1d` on 2026-09-26.
- Incident evidence: [sanitized timeout evidence](assets/autopilot-stage-timeout-evidence.md).
- Existing contracts: `docs/system-specs/modules/autopilot.md`,
  `docs/system-specs/modules/session.md`, and
  `docs/system-specs/modules/acp-client.md`.

## Summary

Keep the configured 30-minute stage budget, but apply it only at an Autopilot
stage boundary: once a stage has started, it may finish under the ordinary chat
turn and tool-liveness safeguards. When the completed stage has spent its
30-minute automatic budget, Go All pauses before admitting the next stage and
asks the user to choose **Go** or **Cancel**.

A real chat-turn ceiling remains a hard stop. When it fires, the deadline owner
must request native cancellation while the exact leased ACP turn is still marked
active, before cancelling and unwinding the dashboard stream. It then waits a
bounded time for acknowledgement, preserves streamed output and the exact
interrupted-stage boundary, and warns visibly if the native turn may still be
running. An armed stage boundary must never admit an ordinary planning turn that
could replace the plan or delete captured stage results.

This separates three concerns that current `main` couples to one clock:

| Concern | Bound | Enforcement point |
|---|---:|---|
| Automatic stage progression | `orchestrator.stage_timeout_seconds`, default 30 min | Before Go All admits the next stage |
| Whole unattended plan | `orchestrator.max_plan_duration_seconds`, default 2 h | Between stages |
| One running chat turn | `agent.chat_turn_timeout_secs`, default 4 h | Around the active dashboard turn |
| Unknown in-flight tool stall | 90-minute suspect window, 2-hour unknown hard cap | ACP liveness oracle |

## Motivation

### The failure is repeated, not hypothetical

A bounded scan of the incident owner's dashboard records found six distinct
`Stage N timed out after 30m` events between 2026-09-17 and 2026-09-25. Session
copies repeated some transcript rows, so the count was deduplicated by each
message's stable id and timestamp.

Two independent incidents were traced through both the dashboard transcript and
the native ACP transcript:

1. The dashboard reported Stage 2 stopped while a detached build continued. The
   native turn later recorded 3,424 passing tests on each of Python 3.10 and
   Python 3.12 and `BUILD SUCCEEDED`. The dashboard had already discarded its
   ownership of that result, and the only build log was later reclaimed with the
   session scratch directory.
2. The dashboard reported Stage 1 stopped at 20:44:15 UTC. The native ACP record
   continued until at least 20:44:45 UTC and began three additional source-file
   writes that were absent from the dashboard transcript. The user then had to
   inspect the worktree for writes made after the UI claimed the stage stopped.

The visible signature was consistent across the incidents:

- a productive stage reached a fixed wall-clock age;
- the dashboard posted `Auto-run stopped`;
- unfinished tool rows rendered as stopped;
- ordinary user messages queued behind the preserved stage boundary;
- the native turn could continue after the dashboard had stopped observing it.

### Current behavior on `main`

At `7f1f4fb1d`:

- `OrchestrationTracker.start_stage` in
  `src/kiro_crew/context_management.py` starts an absolute stage clock.
- `_stage_loop` in `src/kiro_crew/dashboard/chat_orchestrator.py` checks that
  clock before entering a stage.
- The same loop then passes `tracker.stage_timeout_seconds` to `_bounded_turn`
  around `_run_chat`. The 30-minute progression budget therefore also kills the
  running turn.
- `_bounded_turn` in `src/kiro_crew/dashboard/turn_dispatch.py` deliberately
  cancels the inner task and raises even when `_run_chat` absorbs
  `CancelledError`.
- `_run_chat`'s `CancelledError` branch in
  `src/kiro_crew/dashboard/chat_runner.py` currently persists the partial
  dashboard reply only. It does not send native `session/cancel`.
- The native ACP turn can therefore continue after the dashboard controller
  reports that it stopped.
- The interrupted `StageBoundary` remains armed, correctly preserving retry and
  capture obligations, but the old timeout card provides no control that safely
  reconciles that boundary.

The hard cancel is internally consistent with today's system spec. The problem is
that the configured stage clock is doing two incompatible jobs: limiting
unattended progression and limiting one already-admitted turn. The incidents show
that 30 minutes is routinely shorter than productive build, test, and review
work, while the product already has independent safeguards for a genuinely
stalled turn.

## Decision requested

Accept the following contract:

1. `stage_timeout_seconds` is an **automatic admission budget**. It controls
   whether Go All may start another stage; it never cancels the stage already
   running.
2. Every stage turn uses the ordinary hard chat-turn ceiling and existing ACP
   liveness safeguards.
3. A hard turn deadline requests cancellation of the exact leased native turn
   while its ACP stream is still active, before cancelling the dashboard task.
4. A timeout is not reported as complete until native cancellation is
   acknowledged. `no_turn` is not acknowledgement when the deadline still owns
   unfinished native work; a missing acknowledgement produces a visible
   unsafe-to-resume warning.
5. The exact interrupted-stage boundary survives a hard timeout. **Go** resumes
   that obligation; **Cancel** ends it. Ordinary text cannot bypass the boundary
   into planning.
6. The UI names the stage that spent the budget and the stage whose admission is
   being withheld. It never says an unstarted stage timed out.

## Goals

- Let legitimate builds, tests, and model turns finish without an unrelated
  30-minute Autopilot kill.
- Keep a finite unattended progression budget and the two-hour whole-plan budget.
- Make dashboard and native ACP lifecycle agree after a real hard timeout.
- Preserve partial output, exact retry ownership, stage results, and queued user
  input.
- Prevent a timeout recovery turn from replacing the active plan or deleting
  captured stage artifacts.
- Make an unacknowledged native cancellation visible and fail closed.

## Non-goals

- Removing Autopilot budgets.
- Raising the four-hour chat-turn default.
- Changing the ACP liveness oracle or its WORKING/UNKNOWN classifications.
- Persisting an entire Autopilot plan across a gateway restart.
- Redesigning session scratch retention generally.
- Making free-form guidance execute concurrently with an armed stage boundary.
- Weakening session identity, lease ownership, tool approval, or sandbox policy.

## Design

### 1. The stage clock becomes an admission clock

`start_stage(stage)` still starts the stage clock. The loop measures that clock
only when deciding whether to admit another automatic stage turn.

After a stage's turn and owned subagent work have settled, the loop captures the
stage result exactly as it does today. Before Go All enters the next stage, it
checks the elapsed stage budget:

- within budget: admit the next automatic stage;
- over budget: set auto-run false and pause before the next stage;
- no next stage: finish normally; no pointless timeout card after the plan is
  already complete.

The pause card names both sides of the boundary:

> Stage 1 used its 30m auto-run budget. Auto-run paused before Stage 2.
> Choose **Go** to continue, or **Cancel** to end the plan.

The clock resets only when a new stage is admitted. The pause does not reset
failure counts, round counts, escalation counts, or the whole-plan clock.

An explicit human **Go** is attended work. It may admit the next stage even when
the previous automatic budget was spent. **Go All** re-enables automatic
progression from that attended boundary but does not reset the whole-plan budget.

`stage_timeout_seconds = 0` retains its current meaning: disable this automatic
stage-admission pause. It does not disable the independent chat-turn ceiling.

### 2. A running stage uses the ordinary turn ceiling

Every stage turn runs under `agent.chat_turn_timeout_secs`, through the same
`_bounded_turn` mechanism as an ordinary dashboard turn. The current default is
four hours and the loader clamps it to 5 minutes through 24 hours; it cannot be
disabled.

The existing ACP watchdog remains independent:

- a WORKING tool with matched process or CPU evidence is allowed to continue;
- an UNKNOWN in-flight tool reaches the 90-minute suspect window;
- UNKNOWN forbearance is hard-capped at two hours by default;
- the chat-turn ceiling is the final backstop for a productive turn.

This RFC removes no runaway protection. It stops the shorter progression budget
from pre-empting the safeguards that already classify whether a turn is working
or stalled.

### 3. Native cancellation precedes dashboard unwind and is lease-owned

The implementation prototype in #13992 first put native cancellation in
`_run_chat`'s `CancelledError` handler. Current-head GPT and Opus review showed
that placement is too late: cancelling the dashboard task unwinds the ACP stream
first, its finalizer clears active-turn state, and a later `cancel_current` can
return `no_turn` without sending native `session/cancel`. That recreates the
original incident on the exact silent-tool path the fix is meant to close.

The hard deadline therefore needs an owned pre-unwind cancellation phase:

1. After `_run_chat` acquires the shared session lease, it publishes an exact
   cancellation target: provider/session identity plus the current turn
   generation or equivalent handle. A key alone is insufficient because aliases
   can wait behind another turn on the same folded key.
2. When the hard deadline fires, the deadline owner requests native cancellation
   through that exact target **before** cancelling the dashboard coroutine that
   owns the ACP stream.
3. Only after the native request has been sent does the deadline cancel/unwind
   the dashboard task and persist its partial reply.
4. Wait a bounded time for native terminal acknowledgement, reusing
   `agent.soft_stop_budget_secs` unless maintainers choose a dedicated setting.
5. Retain and shield the native-cancel operation so a second dashboard
   cancellation cannot abandon an already-started cancel send.
6. Treat `acked` as settled. Treat `no_turn` as settled only when the exact owned
   turn is independently known terminal; it is not acknowledgement merely
   because local stream unwind cleared an active flag.
7. For `timeout`, `error`, an unexplained `no_turn`, or a cancelled native-cancel
   operation, append a visible warning:

   > The native agent did not acknowledge the stop request. Its previous tool may
   > still be finishing; do not resume this stage until the session becomes idle.

8. Keep the stage boundary closed while cancellation is unacknowledged. A false
   `stopped` claim is worse than an explicit uncertain state.

This can be implemented by extending the deadline wrapper with a pre-cancel
callback, by moving timeout ownership to a turn driver that already has the exact
lease, or by adding an exact-turn cancellation handle. The RFC fixes ordering and
identity, not one internal API shape.

The session lifecycle's existing `prev_turn_cancelled` flag remains the source of
the next-turn cancellation preamble. It is set only after the exact native
cancellation is acknowledged, not merely when the dashboard asks.

### 4. The interrupted-stage boundary remains authoritative

A hard turn timeout can happen before or after the native provider consumed the
stage prompt. Recovery must preserve that distinction:

- unconsumed prompt: retry the exact stage prompt;
- consumed prompt with incomplete settlement: continue or capture against the
  same stage owner;
- successfully settled stage: never re-run it.

The existing `StageBoundary` carries those obligations and remains armed until
reconciliation succeeds or the user cancels the plan.

While it is armed:

- **Go** enters the existing boundary reconciliation path;
- **Cancel** invokes plan cancellation and releases the boundary only after owned
  subagents and durable queue rows are settled;
- ordinary text remains visibly queued and cannot start `_run_chat` in planning
  mode;
- destructive history operations continue to treat the slot as busy.

This deliberately rejects the tempting implementation where an
`awaiting_guidance` flag makes the pending boundary appear absent. That path lets
a normal orchestrator turn parse a plan-shaped reply, reset the plan, and unlink
captured `stage_*_result.md` files while the old boundary is still owed.

A future dedicated "add guidance to paused stage" action could store text on the
boundary for the subsequent Go. It is not needed to close this incident and is
outside this RFC.

### 5. Timeout cards state what is known

Two pauses have different wording:

- **Automatic admission budget spent:** the completed stage ran long; nothing was
  killed. The card names the spent stage and the next stage.
- **Hard chat-turn ceiling fired:** cancellation was requested. The card names the
  hard turn limit and says Autopilot is paused in the active stage. If native
  acknowledgement fails, the warning above follows and blocks safe resume.

Neither path uses the old blanket `Auto-run stopped` wording.

### 6. Evidence and metrics

Keep the existing SEL event family but distinguish causes:

- `operation=stage_admission_budget` for a boundary pause;
- `operation=stage_turn_ceiling` for a hard running-turn cancellation;
- include `budget_stage`, `pending_stage`, and cancellation outcome where
  applicable.

Tests, not production logs, own the detailed interleaving evidence. Production
logging must not include prompt bodies, tool arguments, credentials, or raw
transcript content.

## Migration plan

The RFC lands before any admission-only behavior ships. The native cancellation
bugfix is independent: it may land first while preserving the current 30-minute
hard stage ceiling. This keeps corrupted sessions from accumulating while the
product-shape decision is reviewed.

### Phase 0: immediate native-cancellation convergence

Ship a focused bugfix that keeps today's 30-minute hard stage ceiling but makes
its firing honest:

- request native cancellation through the exact leased turn before ACP stream
  unwind;
- wait for acknowledgement and do not treat an unexplained `no_turn` as success;
- retain the cancel send through repeated dashboard cancellation;
- preserve partial output and warn visibly on non-acknowledgement;
- retain the existing stage boundary and expose safe Go/Cancel recovery without
  admitting ordinary planning text.

**Exit criteria:** a silent in-flight fake tool receives native `session/cancel`
before local turn-active state clears; pre-lease cancellation leaves the current
lease holder untouched; repeated cancellation cannot abandon the send or partial
reply; non-acknowledgement remains visibly unsafe to resume.

This phase is a bug fix, not acceptance of the stage-admission proposal. It can be
a separate PR cut from the cancellation portion of #13992.

### Phase 1: stage admission semantics

- Update the config label/help text without changing the stored key.
- Remove `stage_timeout_seconds` as the hard wrapper around a running stage.
- Wrap stage turns in the ordinary chat-turn ceiling.
- Pause only before another automatic stage begins.
- Make the card name the spent and pending stages.

**Exit criteria:** a test stage that crosses its short automatic budget completes
and captures its result; the next stage does not start automatically; explicit
Go starts it; the final stage completes without a spurious pause.

### Phase 2: owned cancellation and boundary recovery

- Propagate hard dashboard cancellation to native ACP only after lease ownership.
- Wait for acknowledgement and retain the cancel task through repeated
  cancellation.
- Surface non-acknowledgement.
- Preserve partial output and the exact stage boundary.
- Admit only Go/Cancel while the boundary is armed.

**Exit criteria:** tests prove pre-lease cancellation leaves the current lease
holder untouched; repeated cancellation cannot abandon the native send or partial
reply; an acknowledged cancellation resumes with cancelled-turn context; an
unacknowledged cancellation warns and remains unsafe to resume; a plan-shaped
ordinary message cannot reset the plan while the boundary is armed.

### Phase 3: evidence, documentation, and cross-platform verification

- Update Autopilot, session, and configuration specs.
- Add cause-specific SEL assertions.
- Run the scoped dashboard tests on Linux, Windows, and macOS.
- Reproduce with a low test budget and a tool that remains demonstrably WORKING
  past it.

**Exit criteria:** all scoped tests pass on the three supported desktop
platforms; the evidence appendix's reproduction no longer produces dashboard/
native split-brain; the implementation PR references the accepted RFC number.

## Backward compatibility

- The config key remains `orchestrator.stage_timeout_seconds`; existing values
  need no migration.
- The value changes meaning from "kill this running stage" to "pause before the
  next automatic stage." Settings help and system specs must state that change.
- `0` still disables the stage-specific check.
- `max_plan_duration_seconds`, failure counts, round caps, approvals, and manual
  Go/Cancel behavior remain.
- No persisted session or transcript schema changes are required. Any new
  `StageBoundary` field is runtime-only and must reset on arm/clear.
- Rollback restores the old hard stage wrapper, but would also restore the
  reproduced data-loss risk; rollback is therefore an emergency compatibility
  action, not the preferred response to an implementation defect.

## Security considerations

- A productive unattended stage may now run beyond 30 minutes, up to the
  independent chat-turn ceiling. This is an intentional behavior change and the
  main cost of the decision.
- The whole-plan two-hour budget is also boundary-enforced, not a mid-turn kill.
  It prevents another automatic stage from starting after expiry, but one stage
  admitted just before that boundary can finish under the four-hour turn ceiling.
  Maintainers are therefore accepting a worst-case overshoot of one admitted
  turn, rather than treating the two-hour value as a strict wall-clock maximum.
- The liveness oracle still cancels UNKNOWN stalls; WORKING evidence, not elapsed
  time alone, earns forbearance.
- Native cancellation is ownership-gated, preventing one alias waiting for a
  lease from cancelling the sibling turn that owns it.
- Cancellation failure is fail-closed for resume and visible to the user.
- No incident transcript or customer content is added to the repository. The
  evidence appendix contains only redacted lifecycle facts and hashes.

## Alternatives considered

### Raise the stage timeout

Rejected. A larger arbitrary wall clock moves the false-positive boundary but
keeps one setting responsible for both progression and turn safety. Legitimate
workloads vary by orders of magnitude.

### Reset the stage clock on every progress event

Rejected. Text tokens, tool starts, file writes, and subagent completions are not
equivalent progress, and an unhealthy loop can emit them forever. Kiro Crew
already has an evidence-based liveness oracle for this decision.

### Make the stage timeout an inactivity timer

Rejected as duplicate machinery. The ACP watchdog already distinguishes WORKING,
UNKNOWN, dead, and stuck-input states and has separate suspect and hard-cap
windows.

### Keep the 30-minute hard kill but add native cancellation

Rejected as incomplete. It closes the split-brain continuation but still aborts
normal builds and tests, which caused all six observed timeout cards.

### Remove the stage budget entirely

Rejected. Go All still needs a bounded point where control returns to the user,
and a many-stage plan otherwise multiplies its unattended runtime.

### Admit ordinary guidance while the stage boundary is armed

Rejected. An ordinary orchestrator turn may produce a plan-shaped reply, invoking
plan reset and deleting completed stage-result files. Safe free-form guidance
needs a dedicated boundary-owned input path, not a bypass around the boundary.

## Open questions

1. **Config name.** Keep `stage_timeout_seconds` for compatibility now, or add a
   clearer `stage_auto_advance_budget_seconds` alias and deprecate the old key?
   Recommendation: keep the stored key for this change and rename only the UI
   label/help text.
2. **Cancellation acknowledgement budget.** Reuse
   `agent.soft_stop_budget_secs`, or add an Autopilot-specific value?
   Recommendation: reuse the existing stop budget; cancellation semantics should
   not depend on who initiated the stop.
3. **Unacknowledged native turn.** Is visible fail-closed blocking sufficient, or
   must the dashboard also retain the native ACP session pointer until terminal
   evidence arrives? Recommendation: retain it where the session manager already
   can, but do not make a new durable transcript store part of this fix.
4. **Queued guidance UX.** Is a visible queued row plus Go/Cancel sufficient for
   v1? Recommendation: yes. A dedicated boundary-owned guidance action can be a
   later change if users need to amend the interrupted stage before Go.
5. **Implementation split.** Should #13992 remain one change after this RFC is
   accepted? Recommendation: no. Extract Phase 0 into an immediate lifecycle
   bugfix that preserves current timeout semantics. Keep the admission-only
   behavior draft until this RFC lands, then rebase that implementation on the
   independently fixed cancellation path.

## Maintainer decision

Accepting this RFC means the 30-minute setting governs **automatic stage
admission**, not the lifetime of an active turn, and hard timeout recovery becomes
native-cancel-aware and lease-owned. Rejecting it should state which clock is
allowed to abort a demonstrably WORKING stage and how dashboard/native lifecycle
must converge afterward.
