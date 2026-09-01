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
(`_escalated = ["knowledge", "orchestrated", "board"]`, `manager.py:1621`), and
the frontend keeps `/orchestrated/:slug?` only as a redirect to `/chat`
(`website/src/App.tsx:560`, `:2337`).

**Terminology.** "Autopilot" is the user-facing name (nav, WelcomeView, session
menu). The slot `mode` value, the config section, and the system-prompt filename
keep the internal name `orchestrator`, because the mode value is persisted in
session history metadata (`dashboard/chat_persistence.py:1398`) and renaming it
would break restored sessions. The prompt states the binding explicitly ("This
is Autopilot") so the model recognizes user references to *autopilot* /
*autopilot plan* / *autopilot this*; that line is pinned by
`test/test_prompt_autopilot_binding_rule.py`.

## Key Files

| File | Role |
|------|------|
| `dashboard/chat_orchestrator.py` | `_stage_loop` (the stage driver), `_build_stage_context`, `_capture_stage_result`, `api_chat_plan_action` |
| `context_management.py` | `OrchestrationTracker`, plan-format validation (`validate_plan_format`, `looks_like_plan`, `ensure_go_all_option`, `strip_plan_markers`, `rephrase_plan`, `extract_plan_metadata`), and all size caps |
| `dashboard/chat_runner.py` | `_run_chat` (one LLM turn) plus the end-of-turn plan detector that arms the gate |
| `dashboard/chat_title.py` | `_reset_auto_run_for_new_plan`, `_extract_and_redact_plan_metadata`, `_rephrase_plan_lite` |
| `dashboard/chat_handlers.py` | `api_chat` typed-`go` / typed-stop detection, post-escalation guidance reset |
| `dashboard/chat_folders.py` | `api_chat_slot_mode` (`_VALID_MODES = ("", "orchestrator")`) |
| `dashboard/state.py` | `_ChatSlot` plan state and the `mode` / `surface` wire fields |
| `config/prompt-orchestrator.md` | System prompt: plan format, stage execution, delegation, escalation |
| `slack/gateway.py` | `_subagent_done` orchestration guard: per-task failures, per-stage rounds, escalation text |
| `session_workspace.py` | `~/.kiro/crew/sessions/<id>/` layout for sub-agent result files |
| `conductor_skill.py` | Always-on delegation skill (`agent.conductor_skill`, default `false`); independent of Autopilot |
| `website/src/pages/chat/AssistantMessage.tsx` | `parseOptions` turns `[OPTION: …]` into buttons and sets `isPlan` |
| `website/src/pages/chat/useChatPageActionsController.ts` | Routes a plan-option click to `api.planAction()` |

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
(`chat_runner.py:2411`). A stage-execution turn whose output happens to look
plan-shaped must never re-arm or re-count the plan, since that corrupts the
stage total and produces "Stage N of M" overruns.

On a planning turn, at end of turn (`chat_runner.py:4834` onward):

1. `validate_plan_format(text)` checks three things: the `📋 Plan for:` header,
   `Stage N:` lines with strictly sequential numbering, and the `[OPTION: Go |
   … | Cancel]` footer (`context_management.py:255`).
2. No header but `looks_like_plan(text)` matches (at least two
   `Phase|Step|Stage|Part N:` style lines, `context_management.py:238`):
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

`_rephrase_plan_lite` (`chat_title.py:546`) runs on the shared cheap background
session rather than the slot's own, releases it in a `finally`, and calls
`sessions.recycle_background()`: repeated rephrases would otherwise bloat that
child until a mid-stream recycle killed an in-flight call and blocked every
chat queued behind it.

**Fallback arm.** `assistant_text` is reset at each tool-call boundary, so a
plan emitted before further tool calls is gone by the final segment. A separate
whole-turn buffer `_orch_plan_buf` is never reset, and if the final-segment path
did not arm (`_armed_final` false) the gate is armed from that buffer instead
(`chat_runner.py:4981`). Without it, a model that plans and then keeps working
appears to skip the gate entirely.

### Frontend rendering

`parseOptions` (`website/src/pages/chat/AssistantMessage.tsx:49`) takes the
**last** `[OPTION(S): …]` marker for the button list, strips **every** marker
from the displayed text so a stray earlier marker cannot leak as raw syntax, and
sets `isPlan` when both a plan header and a stage marker are present. Every
plan-chip gesture in an orchestrator slot — single click, double-click, and the
Send-now segment — goes straight to `api.planAction(slot, action)` from
`useChatPageActionsController.ts` instead of filling the composer or sending
the label as chat text. The send gestures pass the row identity captured on the
first click of the gesture, so a footer that replaces the reused chip between
the two clicks of a double-click is refused rather than approving a stage the
user never saw. A typed `Cancel` is not special-cased server-side, so routing
those two send gestures through the same gate is what makes the stop control
actually stop the plan.

## Stage Gates

`POST /api/chat/slots/{slot}/plan-action` (`api_chat_plan_action`,
`chat_orchestrator.py:534`) accepts `go`, `go all`, or `cancel`, and requires
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
(`chat_handlers.py:433`).

**Widget-origin refusal.** `go`/`go all` is the only privilege escalation
reachable from chat *text* (it flips the slot into unattended per-stage
auto-approval), and a `<mcwidget>` iframe can pre-fill the input and socially
engineer a human keypress. So a turn whose `user_meta["origin"] == "widget"` has
its `go`/`go all` refused, logged as `auto_run_denied`, and falls through to a
normal fully-gated turn (`chat_handlers.py:409`). Mode changes and tool
approvals live on separate endpoints an iframe cannot reach.

## Execution: the stage loop

`_stage_loop` (`chat_orchestrator.py:140`) owns stage boundaries in Python, not
in the prompt. It creates the tracker if absent (timeout from
`orchestrator.stage_timeout_seconds`), resumes at `tracker.current_stage` when
rounds already exist, and for each stage index:

1. Break if `_orchestration_stopped(slot, tracker)` — see
   [Stop and Cancel](#stop-and-cancel) for why both flags are read.
2. **Clamp**: break if `stage_idx >= slot._plan_stage_count`. `total` is
   captured once when the range is built, so a plan that shrank mid-run would
   otherwise emit a phantom "Stage N of M" with N > M.
3. Check `tracker.is_stage_timed_out()` **before** recording the round, because
   `record_round` restarts the stage clock. On timeout: clear `_auto_run`, post
   the elapsed notice, log `auto_run_timeout`, break.
4. `tracker.record_round(stage_num)` and append a `───── Stage N: Title ─────`
   separator (class `stage-sep`).
5. `_build_stage_context` composes the goal, a `status_summary` checklist
   (completed / execute-now / pending), previous stage results, the current
   stage's title and bullets, and an explicit "execute Stage N of M now"
   instruction. It is appended as a hidden user message (`auto-go` class) and
   passed to `_run_chat`. An exception from `_run_chat` clears `_auto_run`,
   posts a stage-error notice, logs `auto_run_stage_error`, and breaks.
6. **Wait for the stage's sub-agents.** Polls
   `state.subagents.running_agents_for("dashboard:<slot>")` every 2s, up to 150
   rounds (5 minutes), broadcasting a `chat_status` count every 10 polls. This
   is **fail-closed**: a missing manager, or `running_agents_for` returning
   `None` either before or during polling, stops auto-run with a notice and a
   `auto_run_subagent_check_failed` SEL event rather than silently skipping
   verification. Exhausting the 150 rounds stops auto-run with
   `auto_run_subagent_timeout`.
7. `_capture_stage_result` concatenates the assistant messages back to this
   stage's separator and writes
   `~/.kiro/crew/sessions/<slot>/stage_<n>_result.md`; the path is recorded on
   the tracker. Redaction is re-applied here even though both upstream sources
   are already clean, because this writes a NEW file outside the history log's
   own redaction pass (redaction is idempotent, so the common case is a no-op).
8. If not `auto_run` and another stage remains: post
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

`OrchestrationTracker` (`context_management.py:65`) enforces limits the prompt
cannot talk its way past.

| Limit | Value | Scope | Effect |
|-------|-------|-------|--------|
| `MAX_TASK_FAILURES` | 3 | per `task_key` (first 80 chars of the task) | System text: must ask the user for guidance before retrying |
| `MAX_STAGE_ROUNDS` | 3 | per stage | System text: must ask the user for guidance before spawning more |
| `MAX_STAGE_ESCALATIONS` | 2 | per stage | `is_force_failed()` becomes true: must stop and report, no retry |

Sub-agent outcomes feed the tracker from `slack/gateway.py`'s `_subagent_done`
(`gateway.py:3586`), which resolves the tracker from the parent's **dashboard
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
(`chat_handlers.py:506`).

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
`slot._stopping`: the slot is being torn down, so nothing on it may keep running.
Cancel and the typed stop words set `tracker.stopped` and leave `_stopping`
alone: the slot stays alive and usable, and only the plan ends. A gate reading
one flag therefore observes only half the stops, and the window that matters is
`_run_chat` — the loop's longest await, so the likeliest place for a cancel to
land, and the point it would otherwise resume from straight into the next stage
against a revoked approval. `_orchestration_stopped(slot, tracker)` is the single
predicate all four gates (top-of-iteration, post-`_run_chat`, the sub-agent poll
condition, and pre-capture) call, so the two channels cannot drift apart again.

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
| `agent.conductor_skill` | `false` | Emits the always-on delegation skill. Independent of Autopilot: it changes routing knowledge, not the prompt. |

Frontend-side, `defaultAutopilot` in the browser-local chat config
(`localStorage` key `mc-chat-config`, `website/src/pages/chat/ChatSettings.tsx`)
makes newly created sessions start in `orchestrator` mode. It is a per-browser
preference, not backend config.

Sub-agent guards that bound a stage (`agent.max_subagents`,
`agent.subagent_spawn_stagger_secs`, `_TIMEOUT_SECS`, `_TURN_LIMIT`) are owned by
the subagent module: see `subagent.md`.

## Prompt Selection

`agent._prompt_path(mode="orchestrator")` (`agent.py:567`) resolves the
orchestrator prompt in order: `~/.kiro/crew/prompt-orchestrator.md`, then
`<project>/agents/prompt-orchestrator.md`, then the bundled
`src/kiro_crew/config/prompt-orchestrator.md`; it falls back to the normal
prompt if none exists. `ContextBuilder` passes the slot's mode through on the
first message of a session (`context.py:1890`, `chat_runner.py:3047`), so
switching mode takes effect on the next fresh session, and
`{{MAX_SUBAGENTS}}` in the prompt is substituted with the live resolved
concurrency cap.

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

- **Plan progress is not persisted.** `_orch_tracker`, `_stage_titles`,
  `_plan_goal`, `_stage_descriptions`, and `_auto_run` are in-memory `_ChatSlot`
  attributes and are absent from both `to_dict()` and the persisted history meta
  line (only `mode` is written, `chat_persistence.py:1398`), so a gateway
  restart or crash loses stage position and the plan must be re-approved. The
  `stage_*_result.md` files on disk survive, but nothing reloads them.
- Mode cannot be switched while the slot is running: `api_chat_slot_mode`
  returns `409`.
- Sub-agent wait is capped at 5 minutes per stage; a longer fan-out stops
  auto-run with a possibly-incomplete-results notice rather than waiting.

## Testing

| Area | Location |
|------|----------|
| Tracker limits, timeout, `timeout_human`, caps, stale-session cleanup | `test/test_context_management.py` |
| Stage loop guard lifetime, shrink clamp, plan-action routing, plan detection scoped to planning turns, widget-origin `go all` refusal | `test/test_dashboard_chat.py` |
| Prompt binds the "Autopilot" name | `test/test_prompt_autopilot_binding_rule.py` |
| `parseOptions` marker/plan parsing | `website/src/test/AssistantMessage.test.tsx` |
