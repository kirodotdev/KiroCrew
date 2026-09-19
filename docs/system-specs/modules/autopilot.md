# Autopilot Module

## Overview

Autopilot is a per-slot mode of the unified Chat surface: the model presents a
staged plan, the user approves it, and a **Python-controlled stage loop** drives
execution one stage at a time. Simple requests still behave like ordinary chat
(the prompt tells the model to answer directly); only work that warrants a
checkpointed plan gets the plan / approve / execute flow.

Autopilot is not a separate app or page. It is enabled when
`_ChatSlot.mode == "orchestrator"` and toggled via `PATCH
/api/chat/slots/{slot}/mode`. The `orchestrated` builtin app no longer exists:
`apps/manager.py` deletes stale installs of it on startup
(the `_escalated` list in `apps/manager.py`'s startup reconcile), and
the frontend keeps `/orchestrated/:slug?` only as a redirect to `/chat`
(`OrchestratedRedirect` in `website/src/App.tsx`).

**Terminology.** "Autopilot" is the user-facing name (nav, WelcomeView, session
menu). The slot `mode` value, the config section, and the system-prompt filename
keep the internal name `orchestrator`, because the mode value is persisted in
session history metadata (`_save_slot_to_history` writes it; `_rehydrate_slot_from_history` reads it back) and renaming it
would break restored sessions. The prompt states the binding explicitly ("This
is Autopilot") so the model recognizes user references to *autopilot* /
*autopilot plan* / *autopilot this*; that line is pinned by
`test/test_prompt_autopilot_binding_rule.py`.

## Key Files

| File | Role |
|------|------|
| `dashboard/chat_orchestrator.py` | `_stage_loop` (the stage driver), `_build_stage_context`, `_collect_stage_result_parts` + `_write_stage_result`, `api_chat_plan_action` |
| `context_management.py` | `OrchestrationTracker`, plan-format validation (`validate_plan_format`, `looks_like_plan`, `ensure_go_all_option`, `strip_plan_markers`, `rephrase_plan`, `extract_plan_metadata`), and all size caps |
| `dashboard/chat_runner.py` | `_run_chat` (one LLM turn) plus the end-of-turn plan detector that arms the gate |
| `dashboard/chat_title.py` | `_reset_auto_run_for_new_plan`, `_extract_and_redact_plan_metadata`, `_rephrase_plan_lite` |
| `dashboard/chat_handlers.py` | `api_chat` typed-`go` / typed-stop detection, post-escalation guidance reset |
| `dashboard/chat_folders.py` | `api_chat_slot_mode`, whose `_VALID_MODES` also admits `"crew"` — see Slot modes below |
| `dashboard/state.py` | `_ChatSlot` plan state and the `mode` / `surface` wire fields |
| `config/prompt-orchestrator.md` | System prompt: plan format, stage execution, delegation, escalation |
| `slack/gateway.py` | `_subagent_done` orchestration guard: per-task failures, per-stage rounds, escalation text |
| `session_workspace.py` | `~/.kiro/crew/sessions/<id>/` layout for sub-agent result files |
| `website/src/app-sdk/protocol/options.ts` | `parseOptions` — turns `[OPTION: …]` into buttons and sets `isPlan`. `website/src/pages/chat/AssistantMessage.tsx` is its consumer |
| `website/src/pages/ChatPage.tsx` | Routes a plan-option click to `api.planAction()` |

## Slot modes

`api_chat_slot_mode`'s `_VALID_MODES` admits three values, and Autopilot owns
exactly one of them:

| `mode` | Meaning |
|---|---|
| `""` | Ordinary chat. No plan machinery. |
| `"orchestrator"` | Autopilot — everything in this spec. |

A third value, `"crew"` (Crew Mode), existed until it retired in favour of the
Crew Members page; a slot persisted under it is restored as `""`. Its record is
in [crew-mode.md](crew-mode.md) § "Retired: Crew Mode".

## Slot State

All of these live on `_ChatSlot` (`dashboard/state.py`) and are **in-memory
only**; none is serialized by `to_dict()` or written to the history meta line.

| Attribute | Type | Purpose |
|-----------|------|---------|
| `mode` | `str` | `"orchestrator"` enables the plan machinery. Persisted. |
| `_orch_tracker` | `OrchestrationTracker \| None` | Rounds, failures, escalations, stage result paths |
| `_stage_titles` | `list[str]` | Stage titles parsed from the plan |
| `_stage_descriptions` | `list[list[str]]` | Bullet tasks per stage, replayed into the stage context |
| `_plan_goal` | `str` | Goal text from the `📋 Plan for:` header |
| `_plan_stage_count` | `int` (property) | `len(_stage_titles)` |
| `_auto_run` | `bool` | "Go All" was chosen: stage gates are skipped |
| `_in_stage_execution` | `bool` | True only while `_stage_loop` drives a turn; gates the plan detector |
| `stage_boundary` | `StageBoundary` | Atomically owns the pending stage, provider consumption, exact retry, continuation obligation, Stop-preservation generation, parent keys, recovery counters, and queue-ownership generation |

`surface` is emitted alongside `mode` in the slots payload as a forward-compat
alias (identical today) so a future backend can split nav destination from mode
without a wire change; the frontend reads `slot.surface ?? slot.mode`.

## Planning

### Plan format

The prompt instructs the model to emit exactly:

```
📋 Plan for: "<description>"

Stage 1: <Title>
  - task
  - task

Stage 2: <Title>
  - task

[OPTION: Go | Go All | Cancel]
```

The prompt also requires the last stage to be verification, requires the
`[OPTION: …]` line to be last and to appear exactly once, and requires the turn
to END once the plan is on screen (no tool calls in the planning turn), because
nothing has been approved yet.

### Detection and validation

Plan detection runs only on a **planning turn**:
`mode == "orchestrator"` AND `not _in_stage_execution`
(the `_in_stage_execution` guard in `dashboard/chat_runner.py`). A stage-execution turn whose output happens to look
plan-shaped must never re-arm or re-count the plan, since that corrupts the
stage total and produces "Stage N of M" overruns.

On a planning turn, at end of turn (the plan-detector block in `dashboard/chat_runner.py`):

1. `validate_plan_format(text)` checks three things: the `📋 Plan for:` header,
   `Stage N:` lines with strictly sequential numbering, and the `[OPTION: Go |
   … | Cancel]` footer (`context_management.validate_plan_format`).
2. No header but `looks_like_plan(text)` matches (at least two
   `Phase|Step|Stage|Part N:` style lines, `context_management.looks_like_plan`):
   `_rephrase_plan_lite(..., might_not_be_plan=True)` asks the model to either
   reformat it or answer `NOT_A_PLAN`, in which case nothing is armed.
3. Header present but invalid: `_rephrase_plan_lite` retries the format once.
   If the result is still invalid, `strip_plan_markers` removes the markers and
   the turn degrades to ordinary chat.
4. Valid: `ensure_go_all_option` patches a two-option footer up to
   `[OPTION: Go | Go All | Cancel]`, `_reset_auto_run_for_new_plan` clears the
   previous tracker and deletes stale `stage_*_result.md` files, and
   `_extract_and_redact_plan_metadata` fills `_stage_titles` / `_plan_goal` /
   `_stage_descriptions` (credential- and exfiltration-URL-redacted).

`_rephrase_plan_lite` (`dashboard/chat_title.py`) runs on the shared cheap background
session rather than the slot's own, releases it in a `finally`, and calls
`sessions.recycle_background()`: repeated rephrases would otherwise bloat that
child until a mid-stream recycle killed an in-flight call and blocked every
chat queued behind it.

**Fallback arm.** `assistant_text` is reset at each tool-call boundary, so a
plan emitted before further tool calls is gone by the final segment. A separate
whole-turn buffer `_orch_plan_buf` is never reset, and if the final-segment path
did not arm (`_armed_final` false) the gate is armed from that buffer instead
(the `_orch_plan_buf` fallback arm in `dashboard/chat_runner.py`). Without it, a model that plans and then keeps working
appears to skip the gate entirely.

### Frontend rendering

`parseOptions` (`website/src/app-sdk/protocol/options.ts`, called from
`AssistantMessage.tsx`) takes the
**last** `[OPTION(S): …]` marker for the button list, strips **every** marker
from the displayed text so a stray earlier marker cannot leak as raw syntax, and
sets `isPlan` when both a plan header and a stage marker are present. Every
plan-chip gesture in an orchestrator slot — single click, double-click, and the
Send-now segment — goes straight to `api.planAction(slot, action)` instead of
filling the composer or sending the label as chat text. The send gestures pass
the row identity captured on the first click of the gesture, so a footer that
replaces the reused chip between the two clicks of a double-click is refused
rather than approving a stage the user never saw. A typed `Cancel` is not
special-cased server-side, so routing those two send gestures through the same
gate is what makes the stop control actually stop the plan.

## Stage Gates

`POST /api/chat/slots/{slot}/plan-action` (`api_chat_plan_action`,
`dashboard/chat_orchestrator.api_chat_plan_action`) accepts `go`, `go all`, or `cancel`, and requires
`mode == "orchestrator"` (otherwise `400`). Every action is SEL-audited.

- **Go** appends the `Go` label to the transcript and starts
  `_stage_loop(state, slot, auto_run=False)`.
- **Go All** additionally sets `slot._auto_run = True`, logs an
  `auto_run_enabled` SEL event, and starts the loop with `auto_run=True`.
- **Cancel** stops the tracker, clears `_auto_run`, cancels this slot's running
  sub-agent tasks, appends `🛑 Plan cancelled.` and broadcasts `chat_done`. It
  never invokes the LLM.
- If the slot is already running, `Go`/`Go All` are queued
  (`{"ok": true, "queued": true}`).

Typing `go` / `go all` in the chat box reaches the same loop through `api_chat`
(`dashboard/chat_handlers.api_chat`). The OpenAI-compatible
`/v1/chat/completions` path uses `slot.running` for named-slot admission, so it
refuses unrelated requests throughout stage settlement even while no child turn
occupies `slot.task`.

**Widget-origin refusal.** `go`/`go all` is the only privilege escalation
reachable from chat *text* (it flips the slot into unattended per-stage
auto-approval), and a `<mcwidget>` iframe can pre-fill the input and socially
engineer a human keypress. So a turn whose `user_meta["origin"] == "widget"` has
its `go`/`go all` refused, logged as `auto_run_denied`, and falls through to a
normal fully-gated turn (the widget-origin refusal in `dashboard/chat_handlers.api_chat`, audited as `go_typed_widget_origin`). Mode changes and tool
approvals live on separate endpoints an iframe cannot reach.

## Execution: the stage loop

`_stage_loop` (`dashboard/chat_orchestrator.py`) owns stage boundaries in Python, not
in the prompt. It creates the tracker if absent, loads the budgets
(`orchestrator.stage_timeout_seconds` and `orchestrator.max_plan_duration_seconds`)
whenever `tracker.budgets_unset` says this tracker has never had them applied,
resumes at `tracker.current_stage` when rounds already exist, and for each stage
index:

**A plan whose stages are gone is refused, before anything else.** If
`slot._plan_stage_count` is 0 the loop posts `⚠️ This plan is no longer active …`,
logs `auto_run_plan_expired` / `plan_shape_absent`, closes the turn out
(`chat_done`, `slot.task = None`) and returns — no tracker, no config load, no
turn. `mode` is persisted and the transcript keeps the plan turn's
`[OPTION: Go | Go All | Cancel]` row, so a restored slot renders buttons over a
plan that no longer exists; pressing one used to run zero stages and return in
total silence (`range(start_idx, 0)` is empty and the completion message is gated
on `start_idx < total`), which is indistinguishable from a hang. The same gate
covers a planning turn that parsed no stages, so the message names the state
rather than a cause. See [Limitations](#limitations) for why the plan is not
persisted instead.

The budget load is gated on the TRACKER, not on whether this loop created it.
Gating on `tracker is None` meant a tracker the loop did not build — one created
lazily by `slack/gateway.py` when a subagent result landed — ran the whole plan on
constructor defaults, with the plan watchdog sitting at `0` (disabled). A tracker
constructed WITH an explicit budget answers `budgets_unset == False`, so a paused
plan's later Go still pays for no load, and `mark_budgets_loaded()` is recorded
even when the load raised so one bad config read cannot become one per stage-loop
entry.

1. Break if `_orchestration_stopped(slot, tracker)` — see
   [Stop and Cancel](#stop-and-cancel) for why both flags are read.
2. **Clamp**: break if `stage_idx >= slot._plan_stage_count`. `total` is
   captured once when the range is built, so a plan that shrank mid-run would
   otherwise emit a phantom "Stage N of M" with N > M.
3. **Whole-plan watchdog.** Break if `tracker.is_plan_timed_out()`
   (`orchestrator.max_plan_duration_seconds`, default 2 h), clearing `_auto_run`
   and logging `auto_run_timeout` / `plan_duration_exceeded`. Checked at the
   boundary rather than mid-turn: the running stage has its own ceiling, and
   cutting between stages leaves every finished stage captured and resumable.
   `tracker.plan_warning_due()` posts one notice — latched in the tracker — once
   the run passes `PLAN_WARN_FRACTION` (75%) of that budget. **Enforced under
   `auto_run` only**: a stage-gated plan spends its wall-clock at approval
   prompts, and the user clicking each stage is the ceiling. The clock is not
   re-armed when a plan that was stepped through attended is later switched to
   Go All, so attended time does count in that one mixed case. Deliberate: the
   budget is a property of the plan, not of the mode, and a re-arm would let
   Go/Go All alternation refresh the ceiling indefinitely.
4. Check `tracker.is_stage_timed_out()` **before** entering the stage, because
   `start_stage` restarts the stage clock. On timeout: clear `_auto_run`, post
   the elapsed notice, log `auto_run_timeout`, break.
5. `tracker.start_stage(stage_num)` and append a `───── Stage N: Title ─────`
   separator (class `stage-sep`). `start_stage` registers the stage at **zero
   rounds** and restarts the stage clock; it deliberately spends no round, because
   a round is one spawn wave and entering a stage is not one. The loop used to
   enter through `record_round` — inert while nothing here read the cap, but once
   the cap is enforced that tick left only 2 waves before the cut on this path
   while the Slack path still got 3.
6. `_build_stage_context` composes the goal, a `status_summary` checklist
   (completed / execute-now / pending), previous stage results, the current
   stage's title and bullets, and an explicit "execute Stage N of M now"
   instruction. It is appended as a hidden user message (`auto-go` class) and
   passed to `_run_chat`. An exception from `_run_chat` clears `_auto_run`,
   posts a stage-error notice, logs `auto_run_stage_error`, and breaks.
7. **Settle the stage boundary.** `_settle_stage_delivery` polls every 2s
   across every immutable parent key captured by the boundary. Its wait budget
   derives from the configured stage timeout and is bounded by
   `_STAGE_SUBAGENT_POLL_ROUND_CAP`; a disabled timeout uses that same cap. A
   missing manager, a missing queue-aware capability, or a `running_agents_for`
   result of `None` fails closed with `auto_run_subagent_check_failed`; exhausting
   the poll cap stops auto-run with `auto_run_subagent_timeout`.

   **Settlement invariants.** These ids are stable; code comments and review
   findings cite them bare, and the named tests decide if prose and behavior
   disagree.

   | Id | Guarantees | Pinned by | Constrains |
   |---|---|---|---|
   | S1 | Only completion or recovery rows tagged with the active `StageBoundary.owner` run inside that stage. A tagged completion routes status, stage delivery, and delivery settlement by its exact captured `(parent, owner-generation)`, independent of alias arm time. Only untagged runs use the parent/latest compatibility fallback; canonical is used when no alias has an active owner. Foreign rows remain queued until stage execution ends. | `test_autopilot_stage_completion_handoff.py::test_stage_settlement_consumes_only_owned_completion`, `::test_active_stage_holds_foreign_completion_until_boundary_exit`, `test_handlers_messaging_coverage.py::TestApiSpawnRetry::test_stage_boundary_owner_prefers_active_alias_over_inactive_canonical`, `::test_stage_boundary_slot_falls_back_to_canonical_without_active_owner`, `test_slack_gateway.py::TestSubagentDone::test_dashboard_completion_routes_to_exact_run_owner` | `chat_utils.py` (`owned_stage_delivery_entry`), `chat_orchestrator.py` (`_settle_stage_delivery`), `chat_runner.py` (`_start_next_queued_turn`), `dashboard/handlers/messaging.py` (`_stage_boundary_slot_for_parent`), `state.py` (`StageBoundary.generation`), `slack/gateway.py` (`_subagent_done`) |
   | S2 | Consumed but interrupted stage work retains its exact retry or continuation obligation until successful settlement and capture; queueing or prompt consumption alone never discharges it. | `test_autopilot_stage_completion_handoff.py::test_unrelated_completion_does_not_suppress_consumed_stage_continuation`, `::test_exact_preconsumption_recovery_finishes_before_stage_advance` | `state.py` (`StageBoundary.continuation_required`, `preserve`), `chat_orchestrator.py` (`_queue_consumed_stage_resume`, `_settle_stage_delivery`) |
   | S3 | Failed-report buckets retain frozen compact snapshots—never live `SubagentInfo` rows—with exact run/parent/owner-generation identities, timing scalars, and only result, task, raw-task, and error delivery text, each UTF-8 byte-capped with a visible omitted-byte marker. The same snapshot restores terminal outcome, partial state, and result location verbatim on temporary redelivery records. Payload and saturation-marker rows share bounded exact buckets plus one global bucket. At exact-scope saturation, the saturating scope enters the global bucket without displacing an arbitrary existing scope; the first collapse emits one SEL event naming that producer. The global bucket intentionally fails closed gateway-wide, and every bystander pause notice names the same producer parent/owner next to the existing discard/Cancel remedy. Marker identity lets exact boundary discard remove its debt. Finished-run deletion fences any active report task before reading debt or removing the record. | `test_subagent_reap_race.py::test_retained_report_payload_caps_text_bytes_and_redelivers`, `::test_report_failure_scope_cap_overflow_fails_closed_globally`, `::test_report_failure_saturation_markers_share_payload_caps`, `::test_saturated_report_payload_boundary_stays_blocked_until_discard`, `::test_settle_before_delete_waits_for_inflight_terminal_report`, `::test_settle_before_delete_keeps_run_until_report_delivery_succeeds`, `test_autopilot_stage_completion_handoff.py::test_stage_failure_notice_distinguishes_global_overflow_from_own_debt`, `::test_failed_delivery_pause_reaches_transcript_and_linked_channel`, `test_handlers_messaging_coverage.py::TestApiSpawnDelete::test_managed_delete_settlement_uses_one_public_manager_seam`, `::test_delete_scopes_settlement_to_the_deleted_runs_owner` | `subagent.py` (`_ReportFailureSnapshot`, `_truncate_report_failure_text`, `_admit_report_failure`, `_global_report_failure_active`, `_peek_report_failures`, `settle_before_delete`), `chat_orchestrator.py` (`_settle_stage_delivery`, `_halt_plan`), `chat_runner.py` (`_deliver_linked_slack_message`, `_deliver_cross_surface_reply`), `dashboard/handlers/messaging.py` (`api_spawn_delete`) |
   | S4 | One accepted Go owns one three-retrigger recovery budget across all of its stages; stage arm and clear preserve the count and the next accepted Go resets it. | `test_autopilot_stage_completion_handoff.py::test_recovery_retriggers_accumulate_across_stages_for_one_go` | `state.py` (`StageBoundary.recovery_retrigger_count`), `chat_handlers.py` and `chat_orchestrator.py` (Go admission), `slack/gateway.py` (retrigger gate) |
   | S5 | Plan cancellation releases its boundary, publishes one terminal stop, and hands the oldest surviving queued turn to exactly one owner only after active stage work has stopped. | `test_plan_cancel_race.py::test_active_stage_cancel_releases_boundary_and_hands_off_once`, `::test_concurrent_cancels_start_only_one_queued_turn` | `chat_orchestrator.py` (`_release_cancelled_plan_boundary`, `_cancel_release_owns_handoff`) |

   Every Autopilot halt appends through the same transcript-feed seam the dashboard renders and mirrors to a linked Slack or non-Slack channel. Own debt and gateway-wide report overflow have distinct notice text.

   The queue-aware probe includes accepted spawns not yet registered as running,
   completed inner runs whose outer report-registration task remains live, active
   report tasks, retained report debt, owned delivery rows and delivery counters.
   Capture requires two clean event-loop passes over those sources. The provider
   consumption callback decides whether an interrupted stage reruns or continues;
   auth and refusal retries preserve their enqueue-time system provenance. A
   pending boundary contributes to `slot.running` until guarded Go or Cancel
   releases it. Execution-only consumers use `turn_running`, while destructive
   history edits (regenerate, variant switching, and edit-resend) use `running`
   so they cannot rewrite the transcript reserved for the next stage, as
   specified in [session.md](session.md).
8. **Capture the stage result**, split across the thread boundary.
   `_collect_stage_result_parts` walks the assistant messages back to this
   stage's separator **on the loop**, because `slot.messages` is live state the
   loop mutates; it returns an immutable tuple of raw strings, which
   `_write_stage_result` then redacts and writes to
   `~/.kiro/crew/sessions/<slot>/stage_<n>_result.md` **on a worker**. The path
   is recorded on the tracker. Redaction is re-applied here even though both
   upstream sources are already clean, because
   this writes a NEW file outside the history log's own redaction pass
   (redaction is idempotent, so the common case is a no-op).
9. **Round cap after the wave — auto-run only.** Break if the stage has spent
   `MAX_STAGE_ROUNDS`, clearing `_auto_run` and logging `auto_run_round_cap` /
   `stage_round_cap` — a request for guidance, not a terminal verdict. Gated on
   `auto_run` like the watchdog: an attended stage that spent exactly its
   allowed waves and finished falls through to the normal Go prompt rather than
   being told "Auto-run stopped" with no Go row. Every
   round is recorded in one place, `_subagent_done` against
   `tracker.current_stage` as each spawn wave finishes, which is why this gate is
   placed after the wave rather than on entry. Placed **after** the capture too,
   so a stage that genuinely finished keeps its result on disk.
10. If not `auto_run` and another stage remains: post
   `✅ Stage N complete. Click **Go** to proceed to …` plus a fresh
   `[OPTION: Go | Go All | Cancel]`, mark the loop paused, and return. The
   user's next Go re-enters `_stage_loop`.

When the `for` completes without breaking, the loop posts an all-stages-complete
summary built from the captured stage files (first non-separator line of each,
truncated to 120 chars, read through `hooks.safe_read_file`), clears `_auto_run`,
and logs `auto_run_completed`.

The `finally` clears `_in_stage_execution` exactly once on loop exit (pause,
completion, break, or error). The guard deliberately spans any recovery turn a
stage queued (empty-response re-queue, stale or tool-stall recovery): a
per-`_run_chat` clear would drop it before that recovery ran and let its
plan-shaped output re-arm the plan. Clearing it on exit also lets a later Cancel
plus re-plan arm again. Unless the loop paused, it appends `done` and broadcasts
`chat_done`, then always releases `slot.task`.

### Previous-stage context

`_previous_result_paths` inlines up to 2000 bytes per prior stage (30% head,
70% tail, split in **binary** mode so head and tail budgets are in the same
units as the size check) and always emits the full path so the model can read
the rest with its file tools. A result file whose path is sensitive
(`security.is_sensitive_path`) contributes its path only, never its content.

## Failure Handling and Escalation

`OrchestrationTracker` (`context_management.py`) enforces limits the prompt
cannot talk its way past.

| Limit | Value | Scope | Effect |
|-------|-------|-------|--------|
| `MAX_TASK_FAILURES` | 3 | per `task_key` (first 80 chars of the task) | System text: must ask the user for guidance before retrying |
| `MAX_STAGE_ROUNDS` | 3 | per stage | Slack: system text to ask for guidance. Dashboard: `_stage_loop` halts the plan after the stage's wave (`auto_run_round_cap`). All 3 belong to spawn waves — stage entry spends none |
| `MAX_STAGE_ESCALATIONS` | 2 | per stage | `is_force_failed()` becomes true: must stop and report, no retry. Enforced in `_subagent_done` only |

`MAX_STAGE_ESCALATIONS` is deliberately **not** checked by `_stage_loop`, and that
is a reachability fact rather than a preference. Escalations are only recorded by
`reset_after_guidance`, which zeroes the capped stage's rounds while KEEPING its
key — so `current_stage` (the highest key) does not move, the loop's next entry
starts at the stage after it, and an escalated stage is never re-entered. Nothing
on that path can observe `is_force_failed`, so a check there would be dead code.

Sub-agent outcomes feed the tracker from `slack/gateway.py`'s `_subagent_done`
(`_subagent_done` in `slack/gateway.py`), which resolves the tracker from the parent's **dashboard
slot** rather than the session key, because stage limits belong to the tab the
run lives in and not to where the conversation started:

- Error: `record_failure(task_key)`; at the limit the completion event carries
  the ask-for-guidance guard text.
- Success: `record_success(task_key)` clears that task's failure count.
- User-stopped: recorded as **neither**. Success would let the plan advance on
  work the user killed and would skew success stats; failure would fire
  retry-guidance guards for a deliberate act.
- When no sub-agents remain pending, the batch counts as one round via
  `record_round(stage)`, which appends either the round-budget warning or, once
  `is_force_failed`, the stop-and-report directive.

`reset_after_guidance()` gives a fresh round and failure budget after the user
weighs in, increments that stage's escalation count, and resets the stage clock,
so the budget cannot be refreshed indefinitely. `api_chat` calls it whenever a
non-stop message arrives while `has_escalated` is true
(`tracker.reset_after_guidance()` in `dashboard/chat_handlers.api_chat`).

Escalation is therefore two-tier by construction: tier 1 is prompt text the
model may ignore, tier 2 is `is_force_failed()` in Python.

## Stop and Cancel

| Path | Trigger | Behavior |
|------|---------|----------|
| Cancel button | `plan-action` with `cancel` | Always available: stop tracker, clear `_auto_run`, cancel sub-agent tasks, post `🛑 Plan cancelled.`, no LLM call |
| Typed `stop` / `cancel` / `abort` | `api_chat`, only while `tracker.has_escalated` and not already stopped | Same teardown, posts `🛑 [SYSTEM] Orchestration stopped by user.` |
| Stop button | `POST /api/chat/slots/{slot}/stop` | Generic cooperative stop with hard-kill escalation on a second press; sets `_stopping` |

Typed stop words are gated on `has_escalated` so an ordinary "cancel that idea"
mid-plan is not read as a control command; the Cancel and Stop buttons are
unconditional.

**Two flags, two meanings — every advancement gate reads both.** Stop sets
`slot._stopping` for session teardown; plan Cancel and typed stop set
`tracker.stopped` while leaving the slot usable. `_orchestration_stopped` reads
both flags plus the monotonic stop generation at every advancement and settlement
gate, so a Stop that resolves back to idle still revokes the controller entry that
observed it.

Settlement invariant S2 decides whether interrupted stage input reruns or first
settles its retained continuation. Settlement invariant S5 owns plan-cancellation
release and successor handoff. Before that release, Cancel revokes subagents under
the legacy `dashboard:<slot>` key and every parent key captured by the boundary,
then drops only tagged Go approvals, the exact retry and delivery rows owned by
the cancelled generation. Foreign-generation completions and ordinary queued user
messages survive for the one successor handoff.

The inverse — having Cancel set `slot._stopping` — is deliberately **not** what
this does: that flag carries teardown semantics for paths outside the stage loop,
and cancelling a plan is not a request to tear the session down.

The all-stages-complete summary still reads `slot._stopping` alone. A cancel that
lands after the final stage's gate has already passed leaves a plan whose stages
all genuinely ran, and suppressing a truthful completion summary there would be
the wrong trade.

## Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `orchestrator.stage_timeout_seconds` | `1800` | Wall-clock budget per stage before auto-run stops. `0` disables the check. |
| `orchestrator.max_plan_duration_seconds` | `7200` | Wall-clock budget for the WHOLE plan, checked at each stage boundary, with one warning at 75%. `0` disables the check. |

Frontend-side, `defaultAutopilot` in the browser-local chat config
(`localStorage` key `mc-chat-config`, `website/src/pages/chat/ChatSettings.tsx`)
makes newly created sessions start in `orchestrator` mode. It is a per-browser
preference, not backend config.

Sub-agent guards that bound a stage (`agent.max_subagents`,
`agent.subagent_spawn_stagger_secs`, `_TIMEOUT_SECS`, `_TURN_LIMIT`) are owned by
the subagent module: see `subagent.md`.

## Prompt Selection

`agent._prompt_path(mode="orchestrator")` resolves the
orchestrator prompt in order: `~/.kiro/crew/prompt-orchestrator.md`, then
`<project>/agents/prompt-orchestrator.md`, then the bundled
`src/kiro_crew/config/prompt-orchestrator.md`; it falls back to the normal
prompt if none exists. `ContextBuilder` passes the slot's mode through on the
first message of a session, so
switching mode takes effect on the next fresh session, and
`{{MAX_SUBAGENTS}}` in the prompt is substituted with the live resolved
concurrency cap.

The bundled prompt is self-contained and replaces, rather than appends to, the
normal prompt. Its planning contract is explicit: a plan request in any language
wins over complexity heuristics; otherwise dependent phases, multiple files or
systems, and useful intermediate checkpoints must all be present. A plan has one
approval footer, ends the planning turn, and is not re-presented during execution.
Go pauses between stages; Go All continues after checkpoints but stops on failure
or escalation; Cancel aborts. Stages retain verification, independent fan-out,
direct-work exceptions, the wall-clock/start gate and three-round limit. Reversible
in-scope decisions continue without interruption; missing access, unsanctioned
destructive work, repeated failure and conflicts without a safe default escalate.
`test/test_prompt_compact_contract.py` validates the worked plan with the real
parser and guards the prompt's byte budget and operational clauses; it does not
replace the Python stage and permission gates.

## Size and Retention Caps

Defined once in `context_management.py` so they can be tuned in one place.

| Constant | Value | Applies to |
|----------|-------|------------|
| `RESULT_FILE_MAX_BYTES` | 512000 | Per sub-agent result file; `cap_result_file` keeps 20% head + 80% tail so both task context and final output survive |
| `STREAMING_TEXT_MAX_CHARS` | 50000 | In-memory streaming buffer per sub-agent (Activity Viewer); keeps the most recent tail |
| `RESULT_SUMMARY_WORDS` | 200 | Completion-event preview (first + last half), enough to plan next steps without reading the file |
| `SESSION_MAX_BYTES` | 5000000 | Total `agent-*.md` bytes in one session workspace (`check_session_budget`) |
| `HISTORY_MAX_ENTRIES` | 500 | Session `history.jsonl` entries |
| `SESSION_MAX_AGE_SECS` | 604800 | Session workspace age before `cleanup_stale_sessions` removes it |
| `MAX_RETAINED_AGENTS` | 50 | Completed sub-agents retained in `SubagentManager._agents` (`evict_completed_agents`) |

Per-stage inline context is separately bounded at 2000 bytes per prior stage in
`_previous_result_paths`, so the stage context stays roughly constant in size
however long the plan runs.

## Security Properties

- Every plan-action, stage advance, timeout, sub-agent-check failure, and
  completion emits a SEL event, so an unattended "Go All" run is fully
  reconstructible from the audit log.
- Credential and exfiltration-URL redaction is applied at every new sink the
  loop introduces: the stage separator, the stage context, the pause and
  completion messages, the extracted plan metadata, and the stage result file.
- Sub-agent verification is fail-closed: an unavailable or erroring subagent
  manager stops auto-run instead of advancing on unverified work.
- `go`/`go all` from a widget-origin turn is refused, so a prompt-injected
  widget cannot escalate a session into unattended auto-approval.

## Limitations

- **Plan progress is not persisted, and a restart ends the plan.**
  `_orch_tracker`, `_stage_titles`, `_plan_goal`, `_stage_descriptions` and
  `_auto_run` are in-memory `_ChatSlot` attributes, absent from both `to_dict()`
  and the persisted history meta line (only `mode` is written, by
  `_save_slot_to_history`), so a gateway restart or crash loses the plan. The
  `stage_*_result.md` files on disk survive; nothing reloads them.

  This is a choice, not an omission. Autopilot is a lightweight executor of a plan
  the user is watching, not a task runner that owns work across process lifetimes:
  resuming means restoring an execution ledger (which stage ran, how many rounds it
  spent, which results are real), and every one of those restored facts is a way to
  re-run a completed stage's side effects or to skip a stage that never ran. A
  plan is cheap to re-ask for; a mis-resumed plan is not.

  What the module owes the user is therefore honesty rather than continuity: the
  restored slot's `[OPTION: …]` row still renders, and pressing Go gets
  `⚠️ This plan is no longer active …` (`auto_run_plan_expired`) instead of the
  silence it used to get. See [the stage loop](#execution-the-stage-loop).
- Mode cannot be switched while the slot is running: `api_chat_slot_mode`
  returns `409`.
- Sub-agent wait is capped at 5 minutes per stage; a longer fan-out stops
  auto-run with a possibly-incomplete-results notice rather than waiting.

## Testing

| Area | Location |
|------|----------|
| Tracker limits, timeout, `timeout_human`, caps, stale-session cleanup | `test/test_context_management.py` |
| Round cap enforced on the dashboard path; stage entry spends no round | `test/test_stage_round_cap_enforced.py` |
| Whole-plan watchdog, the 75% notice, budget loading for a tracker the loop did not build | `test/test_plan_duration_watchdog.py` |
| Config load off the loop thread, and the cancel/stop windows it opens | `test/test_orchestrator_config_load_off_loop.py` |
| A plan with no stages is refused out loud rather than silently skipped | `test/test_expired_plan_is_refused.py` |
| Stage loop guard lifetime, shrink clamp, plan-action routing, plan detection scoped to planning turns, widget-origin `go all` refusal | `test/test_dashboard_chat.py` |
| Prompt binds the "Autopilot" name | `test/test_prompt_autopilot_binding_rule.py` |
| `parseOptions` marker/plan parsing | `website/src/test/AssistantMessage.test.tsx` |
