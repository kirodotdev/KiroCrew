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
2. No header but `looks_like_plan(text)` matches
   (`context_management.looks_like_plan`): `_rephrase_plan_lite(...,
   might_not_be_plan=True)` asks the model to either reformat it or answer
   `NOT_A_PLAN`, in which case nothing is armed.

   The pre-filter is a RUN of numbered lines, not a match count, and
   `_ordered_plan_runs` takes THREE readings of the text: the stage-line shape
   alone, the bold-item shape alone, and the two merged in document order.

   No single reading serves every real plan. A plan states its stages and numbers
   its substeps under them — `Stage 1: Setup` / `1. **Install**` /
   `2. **Configure**` / `Stage 2: Build` — and in the merged reading those
   substeps carry the count past 2, so `Stage 2` no longer continues its own run
   and a plan plainly written as a plan scores 1. Each shape therefore keeps a
   reading where the other's numbering cannot reach it. The merged reading is for
   the case neither isolated one sees: a model writing ONE sequence in both
   shapes, `Stage 1: Survey` then `2. **Build**`.

   A run qualifies at `_PLAN_STAGE_RUN_MIN` (2) when it is stage lines, or is
   merged and holds at least one stage line; a run of bold items alone needs
   `_PLAN_BOLD_LIST_MIN` (3). The bold-only shape carries no stage vocabulary at
   all, and two bold items is the commonest shape of ordinary prose
   (`1. **Yes** …` / `2. **No** …`), which is why the merged reading is offered
   the shorter threshold only when a stage line is in it.

   Both patterns are `re.IGNORECASE`: a model writes `1. **setup the repo**` as
   readily as `1. **Setup the repo**`, and a case-sensitive `[A-Z]` made every
   lowercase-led bold plan invisible.

   The run taken is the LONGEST one starting at 1, anywhere in the text, not the
   run at its head — one stray `Step 3:`-shaped sentence above the plan (a line
   about the code, a quoted log) otherwise zeroed the score of the plan below it.

   It stays deliberately loose — genuinely plan-shaped prose is the LLM's call —
   but a false positive costs a 2–8 s round trip on the background session, so
   three shapes that used to count as two matches do not: an excerpt starting
   mid-list (`3. **Alpha**` / `4. **Beta**`), the same line repeated in two worked
   examples (`Step 1:` … `Step 1:`), and an unordered enumeration. Same
   sequential-from-1 reading `validate_plan_format` already applies to a real
   plan, one stage earlier.
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

`rephrase_plan` caps its input at `REPHRASE_INPUT_MAX_CHARS` (4000) before either
prompt is built. The `[...truncated N chars...]` marker is budgeted INSIDE that
cap and the remaining 25 % head + 75 % tail split is taken from what is left, so
the number in the name is the ceiling on what the model actually receives rather
than on the source text alone. The turn handed to it can be a whole long answer that merely ENDS in
something plan-shaped, and the call's only job is to reshape a header and a stage
list. Tail-heavy because a plan appended to an explanation sits at the end. The
cap is far above any real plan on purpose: the result REPLACES the turn text when
it validates, so a cap a plan could reach would silently shorten the transcript.

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
- **Cancel** stops the tracker, clears `_auto_run`, cancels this boundary's exact
  `(parent, owner)` sub-agent work—including spawn-approval waits—across every
  captured parent, appends `🛑 Plan cancelled.` and broadcasts `chat_done`. It
  never invokes the LLM.
- If the slot is already running, `Go`/`Go All` are queued
  (`{"ok": true, "queued": true}`).

Typing `go` / `go all` in the chat box reaches the same loop through `api_chat`
(`dashboard/chat_handlers.api_chat`). The OpenAI-compatible
`/v1/chat/completions` path uses `slot.running` for named-slot admission, so it
refuses unrelated requests throughout stage settlement even while no child turn
occupies `slot.task`. Executing and cancelling conflicts return `409` with
`error.type: slot_busy` and `code: slot_busy`. A paused gate has different
remediation, so it returns `409` with `error.type: slot_busy` and
`code: stage_gate_paused`; its message directs the client to continue from the
dashboard with Go because the OpenAI-compatible endpoint cannot submit that
action. The top-level `code` mirrors `error.code` in both cases.

| Named-slot state | HTTP | `error.type` | `code` | Client action |
|---|---:|---|---|---|
| Turn executing or cancellation settling | 409 | `slot_busy` | `slot_busy` | Retry after the slot becomes idle. |
| Autopilot stage gate paused | 409 | `slot_busy` | `stage_gate_paused` | Continue from the dashboard with Go. |

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
7. **Wait for the stage's sub-agents.** Registers one
   `SubagentManager.completion_event(parent_key)` for every immutable parent key
   captured by the boundary, pulsed once per terminal report from
   `_subagent_done`, and re-reads `running_agents_for` on each wake. Each event is
   a PULSE, not state: the loop CLEARS every event before re-reading, so a
   completion landing between the read and the wait still returns at once
   instead of being dropped. `_SA_FALLBACK_SECS` (5 s) bounds each wait because
   an event is explicitly not a guarantee — a run can
   reach a terminal state on a path that never announces (shutdown's
   `cancel_all`) — and the plan-Cancel handler pulses the event itself so a cancel
   is not waiting out that interval. Registration is released in a `finally`;
   the manager's waiter table is fused at `_MAX_COMPLETION_WAITERS` (64), past
   which a caller gets a detached event and degrades to its own fallback rather
   than growing the table.

   This replaces a 2 s poll, which cost the wave up to two seconds of latency and
   ran the O(n) `running_agents_for` scan on a timer whether anything had happened
   or not. The ceiling is unchanged in value and now stated in wall clock rather
   than rounds: `min(stage_timeout // 2, _SA_MAX_WAIT_SECS)` — half the stage
   budget, capped at 15 minutes — so it no longer moves when the poll interval
   does. The `chat_status` count is re-broadcast every `_SA_STATUS_EVERY_SECS`
   (20 s). Still **fail-closed**: a missing manager, or `running_agents_for`
   returning `None` either before or during the wait, stops auto-run with a notice
   and an `auto_run_subagent_check_failed` SEL event rather than silently skipping
   verification. Exhausting the ceiling stops auto-run with
   `auto_run_subagent_timeout`.

   **Settlement invariants.** These ids are stable; code comments and review
   findings cite them bare, and the named tests decide if prose and behavior
   disagree.

   | Id | Guarantees | Pinned by | Constrains |
   |---|---|---|---|
   | S1 | Only completion or recovery rows tagged with the active `StageBoundary.owner` run inside that stage. A tagged completion routes status, stage delivery, and delivery settlement by its exact captured `(parent, owner-generation)`, independent of alias arm time. A retry preserves its prior owner only while that exact boundary remains active; after release it joins the current parent route and never revives the stale owner. Only untagged runs use the parent/latest compatibility fallback; canonical is used when no alias has an active owner. Foreign rows remain queued until stage execution ends. | `test_autopilot_stage_completion_handoff.py::test_stage_settlement_consumes_only_owned_completion`, `::test_active_stage_holds_foreign_completion_until_boundary_exit`, `test_handlers_messaging_coverage.py::TestApiSpawnRetry::test_stage_boundary_owner_prefers_active_alias_over_inactive_canonical`, `::test_stage_boundary_slot_falls_back_to_canonical_without_active_owner`, `test_slack_gateway.py::TestSubagentDone::test_dashboard_completion_routes_to_exact_run_owner`, `::test_retry_after_released_boundary_routes_to_live_canonical_slot` | `chat_utils.py` (`owned_stage_delivery_entry`), `chat_orchestrator.py` (`_settle_stage_delivery`), `chat_runner.py` (`_start_next_queued_turn`), `dashboard/handlers/messaging.py` (`_stage_boundary_slot_for_parent`, `api_spawn_retry`), `state.py` (`StageBoundary.generation`), `slack/gateway.py` (`_subagent_done`) |
   | S2 | Consumed but interrupted stage work retains its exact retry or continuation obligation until successful settlement and capture; queueing or prompt consumption alone never discharges it. | `test_autopilot_stage_completion_handoff.py::test_unrelated_completion_does_not_suppress_consumed_stage_continuation`, `::test_exact_preconsumption_recovery_finishes_before_stage_advance` | `state.py` (`StageBoundary.continuation_required`, `preserve`), `chat_orchestrator.py` (`_queue_consumed_stage_resume`, `_settle_stage_delivery`) |
   | S3 | Each `(parent, owner-generation)` keeps its own bounded failed-report payload bucket. Rows are frozen compact snapshots—never live `SubagentInfo` records—and all retained rows share one `_REPORT_FAILURE_BYTE_BUDGET`; each bucket admits at most `_REPORT_FAILURES_PER_PARENT_CAP` rows and variable delivery text is capped at 64 KiB. A source-derived test enumerates every `SubagentInfo` field read by `_report_terminal_impl`, `_subagent_done`, and their report helpers, then requires the snapshot field set to match exactly. If the row cap or process budget refuses a snapshot, the exact live `StageBoundary.report_retention_refused` stores `row_cap` or `byte_budget`; no global sentinel, refusal count, scope map, or collapse record exists. That boundary stays failed closed until its own discard clears the flag, while an unrelated boundary discard changes nothing. Retained snapshots can redeliver. Slot teardown discards only exact parent/owner pairs from the closing boundary generation. Finished-run deletion fences an active report task, then redelivers debt for the matching live boundary or discards only that exact inactive boundary before removing the record. | `test_subagent_reap_race.py::test_retained_report_payload_caps_text_bytes_and_redelivers`, `::test_report_failure_snapshot_fields_match_terminal_consumers`, `::test_saturated_report_scope_blocks_only_itself_until_discard`, `::test_report_failure_byte_budget_rejects_only_the_new_row`, `::test_report_retention_refusal_is_boundary_local`, `::test_report_failure_refusals_are_boundary_local`, `::test_slot_teardown_discards_exact_failure_scopes_for_its_boundary`, `::test_slot_teardown_preserves_sibling_alias_failure_scope`, `::test_aborted_slot_teardown_preserves_failure_scopes`, `::test_saturated_report_payload_boundary_stays_blocked_until_discard`, `::test_settle_before_delete_waits_for_inflight_terminal_report`, `::test_settle_before_delete_keeps_run_until_report_delivery_succeeds`, `test_autopilot_stage_completion_handoff.py::test_failed_delivery_pause_reaches_transcript_and_linked_channel`, `test_handlers_messaging_coverage.py::TestApiSpawnDelete::test_managed_delete_settlement_uses_one_public_manager_seam`, `::test_delete_scopes_settlement_to_the_deleted_runs_owner` | `state.py` (`StageBoundary.report_retention_refused`, `StageBoundary.clear`), `subagent.py` (`_ReportFailureSnapshot`, `_REPORT_FAILURE_BYTE_BUDGET`, `_truncate_report_failure_text`, `_admit_report_failure`, `_report_retention_refusal`, `discard_report_failure_scopes`, `settle_before_delete`), `slack/gateway.py` (`_report_failure_boundary`, `_subagent_done`), `chat_handlers.py` (`close_slot`), `chat_orchestrator.py` (`_settle_stage_delivery`, `_halt_plan`), `chat_runner.py` (`_deliver_linked_slack_message`, `_deliver_cross_surface_reply`), `dashboard/handlers/messaging.py` (`api_spawn_delete`) |
   | S4 | One accepted Go owns one three-retrigger recovery budget across all of its stages; stage arm and clear preserve the count and the next accepted Go resets it. | `test_autopilot_stage_completion_handoff.py::test_recovery_retriggers_accumulate_across_stages_for_one_go` | `state.py` (`StageBoundary.recovery_retrigger_count`), `chat_handlers.py` and `chat_orchestrator.py` (Go admission), `slack/gateway.py` (retrigger gate) |
   | S5 | Plan cancellation captures every exact `(parent, StageBoundary.owner)` scope and atomically reserves the full parent set, or records a boundary-local cap refusal, before controller teardown or any store await. In the same synchronous decision, every matching live, completed-unrouted, or watcher-held record becomes `user_stopped` and `_stage_boundary_cancelled`; retained report debt and queued follow-ups are removed, and owned follow-up watchers are cancelled. Parent-end teardown uses the same delivery classification: `_stage_boundary_cancelled` is `PARKS_WHEN_SET`, while `pending_followups` is `NOT_DELIVERY_STATE` and is instead cleared with its watcher synchronously before the retired session key can be reused. The gateway still accounts a racing batch member but re-checks revocation at each completion handoff, so no digest, parent route, channel injection, continuation dispatch, or watcher re-arm survives cancellation. It cancels work across every captured parent, including children parked on spawn approval, without touching another owner under the same parent. Durable queued-row reads and writes run on `TaskStore.run`. Within one process, if the store is unavailable, that exact scope remains held after the UI boundary releases: refill and fair-pick refuse its rows while sibling owners remain eligible, the halt is visible in the transcript and mirrored to linked channels, and the next pump settlement pass retries the durable cancel in insertion order. The manager retains at most `_PENDING_BOUNDARY_CANCELLATION_SCOPE_CAP` exact scopes and caps each stored failure at `_PENDING_BOUNDARY_CANCELLATION_FAILURE_MAX_CHARS`; it reserves every parent alias for one stage atomically. When the set cannot fit, `StageBoundary.cancellation_hold_refused` records `pending_scope_cap` with the overflow count, matching live owners are revoked, and the stage boundary remains closed until a later Cancel can reserve the full set. This hold is process-local. After restart, the TaskStore boot reconciler returns an ownerless `ADMITTED` row to `QUEUED`; before the first refill inserts any stage-tagged persisted row, refill resolves its `(parent, owner)` against live boundaries and durably cancels the row when that exact boundary no longer exists. Untagged rows retain ordinary restart redispatch. A boundary-owned claim revalidates its durable generation and exact cancellation authority immediately before registration, with no intervening await on the successful path. If that post-claim store step is unavailable, the process-local `_retained_claims` map keeps the admitted generation and reserved slot; the next pump settlement pass retries it before ordinary refill, and only registration or a durable refusal consumes or releases the reservation. If cancellation refuses a still-owned admitted generation, the writer thread cancels it, publishes the neutral queued-stop report, and returns its reservation; no admitted-but-unregistered task remains. It publishes one terminal stop and hands the oldest surviving queued turn to exactly one owner only after active stage work has stopped. | `test_session.py::TestParentEndCancelsItsChildren::test_parent_end_cancels_owned_followup_without_recreating_session`, `::test_the_delivery_parked_states_are_enumerated_from_the_producers`, `::test_every_parked_state_makes_the_delivery_parked`, `test_taskq_admission_integration.py::test_boundary_cancel_scope_cap_bounds_failures_and_retries_in_order`, `::test_boundary_cancel_scope_reservation_is_atomic_across_parent_aliases`, `test_plan_cancel_race.py::test_active_stage_cancel_scope_cap_keeps_boundary_and_blocks_dispatch`, `test_taskq_admission_integration.py::test_claim_revalidation_outage_retains_generation_and_slot_until_retry`, `::test_boundary_cancel_refuses_completion_before_store_settlement`, `::test_boundary_cancel_revokes_completed_unrouted_owner_before_store_settlement`, `test_slack_gateway.py::TestSubagentDoneStoppedClassification::test_boundary_cancelled_completion_is_not_routed`, `::test_completed_owner_revoked_while_report_waits_is_not_routed`, `test_spawn_followup.py::TestFollowUpDelivery::test_boundary_cancel_drops_completed_owners_queued_followup`, `test_plan_cancel_race.py::test_plan_cancel_revokes_every_captured_parent_before_boundary_clear`, `::test_stage_cancel_store_failure_surfaces_mirrored_halt_notice`, `::test_active_stage_cancel_releases_boundary_and_hands_off_once`, `::test_concurrent_cancels_start_only_one_queued_turn`, `test_subagent_scale.py::TestBatchIdentity::test_stop_boundary_includes_approval_waiters_and_preserves_sibling`, `test_taskq_admission_integration.py::test_cancel_for_boundary_reaches_only_its_store_rows`, `::test_cancel_for_boundary_store_io_runs_off_the_loop_thread`, `::test_boundary_cancel_store_failure_blocks_dispatch_until_retry_tick`, `::test_boundary_cancel_after_claim_before_registration_refuses_start`, `::test_boundary_cancel_marker_after_claim_releases_and_stops_row`, `::test_restart_refill_cancels_row_from_gone_stage_boundary` | `chat_orchestrator.py` (`_cancel_stage_subagents`, `_release_cancelled_plan_boundary`, `_cancel_release_owns_handoff`), `subagent.py` (`DELIVERY_ROUTING_FIELDS`, `delivery_is_parked`, `cancel_for_boundary`, `_pending_boundary_cancellations`, `_retained_claims`, `_stage_boundary_cancelled`, `_followup_watcher_infos`), `subagent_manager/cancellation.py` (`snapshot_teardown_children_impl`, `cancel_for_boundary_impl`, `retry_pending_boundary_cancellations_impl`), `subagent_manager/continuation.py` (`_arm_followup_watcher_impl`, `_deliver_followups_impl`), `subagent_manager/admission/pump.py` (`claim_and_start`, `retry_retained_claims`), `subagent_manager/admission/taskq_bridge.py` (`taskq_cancel_boundary_async`, `_reconcile_refill_boundaries_async`), `subagent_manager/admission/fairness.py` (`pick_window_index`), `slack/gateway.py` (`_subagent_done`) |

   Every Autopilot halt appends through the same transcript-feed seam the dashboard renders and mirrors to a linked Slack or non-Slack channel.

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

`_previous_result_paths` inlines up to 2000 bytes for each of the last
`_PREV_FULL_STAGES` (3) prior stages (30% head, 70% tail, split in **binary** mode
so head and tail budgets are in the same units as the size check). Every EARLIER
stage contributes one headline instead — its first non-separator line, read from
the first `_PREV_HEADLINE_BYTES` (512) of the file. Each stage always emits its
full path, so nothing the model could reach before is out of reach; it opens an
older result with its file tools.

Inlining every prior stage made the context grow with the stage index — ~18 KB by
stage 10 — and re-read every earlier file at each boundary, on the worker the
`asyncio.to_thread` hop exists to protect. A result file whose path is sensitive
(`security.is_sensitive_path`) contributes its path only, never its content or its
headline.

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
`{{MAX_SUBAGENTS}}` in the prompt is substituted with the execution cap in
force (`resource_status.adaptive_exec_cap`), or with the configured ceiling
labelled as one when no controller runs in the process.

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
- Sub-agent wait is capped at half the stage budget, 15 minutes at most; a
  longer fan-out stops auto-run with a possibly-incomplete-results notice rather
  than waiting.
- A stage advances when its turn returns without raising. Whether the stage
  actually produced work is NOT judged: deciding it needs a signal that survives
  a tool-only turn, a transient empty turn, a refused permission-gated call, an
  unrelated successor turn and an unanswered question, and no such signal exists
  yet. Tracked as P2-1 on #1783.

## Testing

| Area | Location |
|------|----------|
| Tracker limits, timeout, `timeout_human`, caps, stale-session cleanup | `test/test_context_management.py` |
| Round cap enforced on the dashboard path; stage entry spends no round | `test/test_stage_round_cap_enforced.py` |
| The plan pre-filter still sees lowercase, mixed-shape and stray-numbered plans | `test/test_plan_detection_breadth.py` |
| The wave wait is event-driven, its fallback still bounds it, and the waiter table is fused | `test/test_autopilot_wave_wait_event.py` |
| Only the last three prior stages are inlined | `test/test_autopilot_previous_stage_context.py` |
| Whole-plan watchdog, the 75% notice, budget loading for a tracker the loop did not build | `test/test_plan_duration_watchdog.py` |
| Config load off the loop thread, and the cancel/stop windows it opens | `test/test_orchestrator_config_load_off_loop.py` |
| A plan with no stages is refused out loud rather than silently skipped | `test/test_expired_plan_is_refused.py` |
| Stage loop guard lifetime, shrink clamp, plan-action routing, plan detection scoped to planning turns, widget-origin `go all` refusal | `test/test_dashboard_chat.py` |
| Prompt binds the "Autopilot" name | `test/test_prompt_autopilot_binding_rule.py` |
| `parseOptions` marker/plan parsing | `website/src/test/AssistantMessage.test.tsx` |
