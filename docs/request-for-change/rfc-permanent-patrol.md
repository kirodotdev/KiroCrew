---
title: A loop the model arms picks no cycle cap; it ends itself with one final report
status: accepted
author: kirocrew agent session, directed by iamwhatever
created: 2026-11-08
last-audited: 2026-11-08
audited-at: 664feb41be
doc-pr: 33382
implementation-prs: [33385]
tracking-issues: [33381]
supersedes: []
superseded-by: []
---

# RFC: A loop the model arms picks no cycle cap; it ends itself with one final report

Status: accepted. The repository owner (iamwhatever) made this decision on
2026-11-08 in the conductor chat that dispatched the work, in four rulings
listed in section 10. It takes effect when this document merges. The
implementation is [#33385](https://github.com/kirodotdev/KiroCrew/pull/33385),
which rebases onto the base that carries this document. Every citation below
was read on `origin/main` at `664feb41be`. Citations name a file and the symbol
in it, not a line number, because the docs lint refuses line citations in prose
(a line number moves with the next refactor; a symbol name does not). Paths are
under `src/kiro_crew/` unless they start with `skills/`.

This document lands on its own because it changes what `monitor_start` and the
two conductors (Bot and Ultra Mode) do by default, and the First Principles lane
reads that decision from the base branch.

## 1. Problem

A model that arms a loop today also picks when the loop dies.

- `monitor_start` defaults to a 24-cycle cap and a 14,400 s runtime
  (`mcp_tools/_limits.py` `_MONITOR_DEFAULT_MAX_CYCLES` and
  `_MONITOR_DEFAULT_MAX_RUNTIME_SECS`, read in
  `mcp_tools/control_parts/monitor.py` `monitor_start`). A loop that hits either
  stops whether or not its work is done.
- A work-ledger patrol makes the model check and renew its own bounds with the
  `patrol_budget` tool (`mcp_work.py` `_patrol_budget`; the rule is the
  `patrol_budget` key of `agent_parts/spec_prompts.py`
  `_LEDGER_CONDUCTOR_RULES`). A missed renewal ends the patrol with items open.
  The gateway's default patrol arms 300 cycles and 86,400 s
  (`conductor_patrol.py` `PATROL_MAX_CYCLES`, `PATROL_MAX_RUNTIME_SECS`).
- Neither conductor sends the user a closing message when it stops its patrol
  (the `patrol_ends` section of `agent_parts/spec_prompts.py`
  `_CONDUCTOR_PROMPT_SECTIONS`; the `conductor.patrol` piece of
  `crewmate_prompts.json`).

## 2. Current design: conductor and workers through the work ledger

- **Dispatch.** A conductor records an item with `work_ledger_record`
  `action=create`, opens a worker session, binds it with `action=bind`, then
  seeds it (the `work_ledger_record` tool in `mcp_work.py`; the bind branch in
  `dashboard/handlers/work_ledger_conductor.py`). `work_dispatch` runs the same
  four steps in one call and seeds only after the bind
  (`mcp_dashboard_session_runs.py` `_run_work_dispatch`).
- **Worker side.** The worker reads its item with `work_brief` and reports with
  `work_report` (both tools in `mcp_work.py`): `progress`, `blocked`, `question`
  or `done`, plus `reason` `approval` or `needs_human`.
- **Conductor side.** `work_ledger_read` returns the board; `compact=true`
  returns status columns and derived flags, including `unpatrolled` when no
  work-ledger watch is active (`dashboard/handlers/work_ledger_read.py`, through
  `conductor_patrol.py` `has_active_patrol`). The conductor answers a question
  with `action=decide`, records the `accept_eval` answer with `action=verdict`,
  and ends an item with `action=close`.
- **Bot vs Ultra Mode.** Both share one rule set,
  `agent_parts/spec_prompts.py` `_LEDGER_CONDUCTOR_RULES`. The root conductor
  charter is assembled from it (`_CONDUCTOR_PROMPT_SECTIONS`, joined into
  `_CONDUCTOR_SYSTEM_PROMPT`). Ultra Mode runs the team lead
  (`crew_mode.py` `DEFAULT_CREW_MODE_AGENT`), which follows the
  `goal-conductor` skill. The Bot's `conductor.patrol` piece in
  `crewmate_prompts.json` fills the same rules in as placeholders.

## 3. Wake and poll

- `monitor_start` arms a prompt loop on the calling session: every
  `interval_secs` the message is re-injected as the next turn
  (`mcp_tools/control_parts/monitor.py` `monitor_start`). `monitor_update`
  revises it; `monitor_stop` and `autonudge_stop` end it (same module).
- Every arm goes through one chokepoint,
  `autonudge_authz.py` `authorize_and_add_nudge`.
- When a conductor binds a worker and holds no active loop,
  `conductor_patrol.py` `ensure_patrol`, called from the bind route in
  `dashboard/handlers/work_ledger_conductor.py`, arms a gated
  `watch="work-ledger"` patrol every 300 s (`PATROL_INTERVAL_SECS`,
  `PATROL_WATCH`).
- Restart survival: on `AutoNudgeService.start` (`autonudge.py`), active loops
  are reloaded (`autonudge_service/store_loader.py` `_load`) and their timers
  re-armed from their persisted deadline (`_arm_from_deadline`).
- A spent bound on a work-ledger loop with open items is raised, up to the
  `monitoring.max_runtime_secs` ceiling (`autonudge_service/firing.py`
  `_extend_for_open_ledger`), 7 days as shipped (`monitoring/limits.py`
  `DEFAULT_RUNTIME_CEILING_SECS`).

## 4. Event-driven wakes

- **Work ledger.** A worker report pulls the conductor's work-ledger loop
  forward within seconds through `AutoNudgeService.fire_now`, called from
  `conductor_wake.py`. Only the waking statuses do: `done`, `blocked`,
  `question` (`work_vocab.py` `WORK_WAKE_STATUSES`, read as `ledger_wake.py`
  `WAKE_STATUSES`; `reply` is admitted only under the remote-dispatch Feature
  Preview, `work_vocab.py` `wake_statuses`). A `progress` report wakes nothing.
  A report whose status repeats the event right before it wakes nothing
  either (`ledger_wake.py` `repeats_status`, read by `conductor_wake.py` and
  `probes/work_ledger.py`): five `blocked` reports in a row wake once. The
  first report, a new status, and any report after a `progress` or after the
  conductor's own `decision` still wake, and so does every `question` or
  `reply`, since each is a new ask (only `blocked` and `done` repeats are
  suppressed). A new summary or pr on the same
  status does not; the next read of the board shows it.
- **Pull request.** A gated loop naming one PR is read each tick against its own
  wake criteria, or the shipped default brief (`autonudge_judge.py`
  `DEFAULT_WAKE_WHEN`): a blocker, a failing check, a settled board, a question,
  a review asking for change, a terminal state, or the loop's own exit
  condition.

## 5. Wake filter

- A gated loop's quiet tick costs no turn. Silence is still bounded: one forced
  delivery after `_MAX_QUIET_STREAK = 10` quiet ticks, or after
  `_IDLE_LEDGER_QUIET_FLOOR = 40` on an idle work ledger or on a pull request
  that only waits on a reviewer (`autonudge_service/gate.py`, chosen by
  `_quiet_floor`; the PR case is `_pr_waits_only_on_review`: open, not a
  draft, mergeable, every check finished with none failed, and
  `REVIEW_REQUIRED`).
- `gate` defaults to on (`mcp_tools/control_parts/monitor.py` `monitor_start`).
  A `gate=false` loop is re-injected every interval (the `gate` field of the
  `monitor_start` schema in `mcp_tools/control_parts/schemas.py`).
- The `monitor_update` schema in the same file has no `gate` field, so a
  revision cannot turn the gate off; ungating needs a fresh arm (the comment on
  `watch` in `mcp_tools/control_parts/monitor.py` `monitor_update`).

## 6. Decision

1. **A gated loop the model arms has no cycle cap by default.** Omitting
   `max_cycles` on `monitor_start` arms no cap when the loop is gated and has a
   subject to observe.
2. **A loop that wakes every interval keeps the 24-cycle default:** `gate=false`,
   or a gated loop with nothing to observe.
3. **The runtime defaults to the operator ceiling**, `monitoring.max_runtime_secs`
   (7 days as shipped): the one runaway backstop, kept on purpose.
4. **Explicit values are honoured**, except on a `watch="work-ledger"` patrol,
   which takes no bound from the model: the 300 s patrol interval, no cycle cap
   and the ceiling runtime, on `monitor_start`, on a `monitor_update` that adds
   the watch or touches a bound or the interval, and on the gateway's default
   patrol.
5. **The model ends the loop itself.** It checks live state (for example
   `gh pr view` for a PR watch). When the exit condition holds, it sends the user
   ONE final report (what landed, what is left, links), then, in a later step,
   stops the loop. Both conductors use one shared rule and `autonudge_stop`,
   which both are granted.
6. **System and app callers are unchanged.** Code that arms loops with its own
   bounds, and skills that pass explicit bounds, keep them.

## 7. Why permanent and gated

- **No renewal ladder.** The model no longer has to act on the one cycle whose
  budget line says 10% is left (the "Renew the loop yourself" step of the
  `goal-conductor` skill's Patrol section).
- **No loop dies with work open.** A cap the model picked can run out mid-goal;
  the gateway already extends an open patrol to the same ceiling
  (`_extend_for_open_ledger`), so the model's bound added only a way to fail.
- **Cost stays bounded by the gate.** Quiet ticks are free, and delivered quiet
  turns are capped by the floors in section 5.
- **7-day backstop.** Every loop still ends at `monitoring.max_runtime_secs`.

## 8. Instruction simplification

The conductor no longer runs `patrol_budget` check or renew, and no longer picks
counts. #33385 removes or replaces this text:

- `agent_parts/spec_prompts.py`, rule `patrol_budget`: the check, the exit-21
  runtime, and the renewal on `10% or less left`, replaced by "pass no
  `max_cycles` and no `max_runtime_secs`".
- `agent_parts/spec_prompts.py`, section `round_close_reset`: `patrol_base` in
  the round-close record.
- `agent_parts/spec_prompts.py`, section `patrol_ends`: "Call `autonudge_stop`
  only then. `max_cycles` is a runaway backstop, not a stop signal.", replaced
  by the final-report-then-stop rule.
- `crewmate_prompts.json`, `conductor.patrol`: "When no item is open any more,
  stop the loop with monitor_stop.", replaced by the same shared rule.
- `builtin_skills/goal-conductor/SKILL.md`, Patrol section: "pass explicit
  positive `interval_secs`, `max_cycles` and `max_runtime_secs`"; "The bounds
  come from the script" with its exit codes; "Renew the loop yourself" with its
  exit table; `patrol_base` in the item-cap notice and round-close record. Stop
  conditions: the renew and 7-day-ask sentence, and "Call `autonudge_stop` only
  on one of the two signals" without a report.
- `conductor_patrol.py` `PATROL_MESSAGE`: "Tune this loop with monitor_update".
- `mcp_tools/control_parts/schemas.py` (the `monitor_start` description and its
  `max_cycles` field) and the two arm acks (`mcp_tools/control_parts/monitor.py`
  `monitor_start`, `dashboard/session_directive_apply_monitor.py`
  `_monitor_start`): "finite" loop and "call autonudge_stop" wording, replaced
  by "decide from live state, one final report, then stop".
- `builtin_skills/kirocrew-dev/babysit/SKILL.md` (the comment-aware recipe's
  `max_cycles: 48` and `max_runtime_secs: 14400`, "All three limits must be
  positive and finite") and `skills/self-nudge-loop/SKILL.md` (the fixed
  30-cycle cap for a gated `monitor_start` loop): fixed cycle caps the model
  was told to pick.

## 9. Out of scope

- Removing the `patrol_budget` tool and script. It still checks a capped loop;
  removing a published tool is a separate cleanup.
- Changing `monitor_watch` (structured monitors) or the REST/UI arming surface.

## 10. Record of the rulings

All four came from the owner through the dispatching conductor on 2026-11-08:
(a) patrols are permanent by default, and end with one final report then a
stop; (b) keep the 7-day `monitoring.max_runtime_secs` backstop as the only
bound on a patrol; (c) widen it to every `monitor_start` loop a model arms,
with the model deciding when it is done from live state; (d) the no-cap
default applies only to gated loops, and a `gate=false` loop keeps the
24-cycle default.

## 11. Risks

This decision deliberately reverses the premise behind the 24-cycle default
(the comment above the cap in `mcp_tools/control_parts/monitor.py`
`monitor_start`: a loop with no bound stops only when the model volunteers a
stop, and loop stores show that is not reliable). It does not claim models now
stop reliably. It bounds the cost of one that does not instead: a gated loop's
quiet tick costs no turn, the quiet floors limit delivered turns, every loop
still ends at the 7-day ceiling, and a loop that wakes every interval keeps the
24-cycle cap.

- A model that never stops a finished patrol leaves it running until the 7-day
  backstop. On an idle ledger that costs one floor turn every 40 quiet ticks
  (about 3.3 hours at the 300 s patrol interval).
- The tool's directive replay reads no session log, so it cannot resolve a bare
  `PR <n>`. The implementation marks such a default cap and the applier decides
  it again with the log, so the cap follows the gate the loop actually gets.
