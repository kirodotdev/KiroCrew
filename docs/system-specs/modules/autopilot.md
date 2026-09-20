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

   The pre-filter is a RUN of numbered lines, not a match count: at least two
   `Phase|Step|Stage|Part N:` lines numbered 1, 2, 3 …, or at least
   `_PLAN_BOLD_LIST_MIN` (3) bold-led ordered items numbered the same way. It
   stays deliberately loose — genuinely plan-shaped prose is the LLM's call — but
   a false positive costs a 2–8 s round trip on the background session, so three
   shapes that used to count as two matches no longer do: an excerpt starting
   mid-list (`3. **Alpha**` / `4. **Beta**`), the same line repeated in two worked
   examples (`Step 1:` … `Step 1:`), and an unordered enumeration. The bold-list
   shape needs a longer run than the stage-line shape because it carries no stage
   vocabulary at all, and two bold items is the commonest shape of ordinary prose
   (`1. **Yes** …` / `2. **No** …`). Same sequential-from-1 reading
   `validate_plan_format` already applies to a real plan, one stage earlier.
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
- **Cancel** stops the tracker, clears `_auto_run`, cancels this slot's running
  sub-agent tasks, appends `🛑 Plan cancelled.` and broadcasts `chat_done`. It
  never invokes the LLM.
- If the slot is already running, `Go`/`Go All` are queued
  (`{"ok": true, "queued": true}`).

Typing `go` / `go all` in the chat box reaches the same loop through `api_chat`
(`dashboard/chat_handlers.api_chat`).

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
resumes at `tracker.current_stage` when rounds already exist — unless
`tracker.retry_stage` names a stage, which wins (see the empty-result gate
below) — and for each stage index:

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
7. **Wait for the stage's sub-agents.** Waits on
   `SubagentManager.completion_event("dashboard:<slot>")`, pulsed once per
   terminal report from `_subagent_done`, and re-reads
   `running_agents_for` on each wake. The event is a PULSE, not a state: the loop
   CLEARS it before re-reading, so a completion landing between the read and the
   wait still returns at once instead of being dropped. `_SA_FALLBACK_SECS` (5 s)
   bounds each wait because the event is explicitly not a guarantee — a run can
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
10. **A stage that produced nothing is not complete.** If the captured text is
   empty after stripping — the same snapshot `_write_stage_result` just wrote, so
   the verdict matches the artifact rather than a second opinion about it — the
   loop records a round for the stage, latches it with
   `tracker.mark_stage_for_retry(stage_num)`, clears `_auto_run`, posts
   `⚠️ Stage N produced no output …` with a fresh
   `[OPTION: Go | Go All | Cancel]`, logs `auto_run_empty_stage` /
   `stage_produced_nothing`, and pauses. Before this the loop advanced on
   "`_run_chat` returned without raising", so an empty or refused turn moved the
   plan on, showed the stage as `✅ completed` in the next `status_summary`, and
   let a Go All run reach "✅ All N stages complete" having produced none of it.

   The latch is what makes "do not advance" real: the resume point is
   `tracker.current_stage`, the highest stage key, which the empty stage
   registered on ENTRY — so without it the next Go would run the stage after the
   one that failed. `tracker.retry_stage` is a pure READ and
   `tracker.start_stage` is what spends it, so exactly one re-entry is bought and
   nothing buys it early: the gates between the loop's read and the stage it names
   — the whole-plan watchdog and the stage-timeout check — both break before
   `start_stage`, and a latch spent at the read would be gone on those paths while
   `current_stage` still pointed past the failed stage. The stage keeps its rounds
   and its ledger place because it did run. The rounds are the existing budget: three empty attempts reach
   `MAX_STAGE_ROUNDS` exactly as three fruitless spawn waves do, and the notice
   says so — **one round per attempt, whoever records it**. The stage's tally is
   snapshotted at entry (after `start_stage`, which registers the stage without
   resetting it), and the gate records a round only when that tally has not moved
   since: a delegated stage that spawned a wave and then emitted no assistant text
   is the ordinary shape of this case, and `_subagent_done` has already charged
   that wave against the same tracker. Charging again would make one attempt cost
   two of three rounds, so two attempts would exhaust a budget of three.
   Auto-run stops rather than retrying by itself, like every other guard
   here (timeout, round cap, subagent failure): the next attempt is only worth
   spending if a human still wants it. The message walk sits OUTSIDE the capture's
   `try` so a failed WRITE is never read as a stage that idled.
11. If not `auto_run` and another stage remains: post
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
- An empty stage is detected by the text it captured, so a stage whose entire
  turn was tool calls with no assistant output reads as "produced nothing". That
  is deliberate — it handed the next stage nothing either — but it is a
  mechanical test, not a judgement about whether work happened.

## Testing

| Area | Location |
|------|----------|
| Tracker limits, timeout, `timeout_human`, caps, stale-session cleanup | `test/test_context_management.py` |
| Round cap enforced on the dashboard path; stage entry spends no round | `test/test_stage_round_cap_enforced.py` |
| An empty stage does not advance, spends a round, and re-enters itself on the next Go | `test/test_autopilot_empty_stage.py` |
| The wave wait is event-driven, its fallback still bounds it, and the waiter table is fused | `test/test_autopilot_wave_wait_event.py` |
| Only the last three prior stages are inlined | `test/test_autopilot_previous_stage_context.py` |
| Whole-plan watchdog, the 75% notice, budget loading for a tracker the loop did not build | `test/test_plan_duration_watchdog.py` |
| Config load off the loop thread, and the cancel/stop windows it opens | `test/test_orchestrator_config_load_off_loop.py` |
| A plan with no stages is refused out loud rather than silently skipped | `test/test_expired_plan_is_refused.py` |
| Stage loop guard lifetime, shrink clamp, plan-action routing, plan detection scoped to planning turns, widget-origin `go all` refusal | `test/test_dashboard_chat.py` |
| Prompt binds the "Autopilot" name | `test/test_prompt_autopilot_binding_rule.py` |
| `parseOptions` marker/plan parsing | `website/src/test/AssistantMessage.test.tsx` |
