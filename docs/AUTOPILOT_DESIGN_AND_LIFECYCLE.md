# KiroCrew Autopilot — Design, Lifecycle & Research

**Date:** 2026-04-09
**Status:** Implemented (Beta)

> Covers: orchestrated chat design, Go/Go All/Cancel lifecycle, execution, failure handling, known issues, and optimization roadmap.

---

## Table of Contents

- [1. Overview](#1-overview)
- [2. Architecture](#2-architecture)
- [3. Planning](#3-planning)
- [4. Stage Gates: Go / Go All / Cancel](#4-stage-gates)
- [5. Execution](#5-execution)
- [6. Auto-Run Continuation](#6-auto-run-continuation)
- [7. Failure Handling and Escalation](#7-failure-handling-and-escalation)
- [8. Stop and Cancel](#8-stop-and-cancel)
- [9. Known Issues](#9-known-issues)
- [10. Proposed Optimizations](#10-proposed-optimizations)
- [11. TODO](#11-todo)

---

## 1. Overview

Orchestrated Chat (Autopilot) consolidates Chat mode and Task Runner into a single interactive surface.

Autopilot is **not** a separate app or page — it is a per-slot mode of the unified Chat surface, enabled when `_ChatSlot.mode == "orchestrator"` and toggled via `PATCH /api/chat/slots/{slot}/mode` (`chat_folders.py`; `_VALID_MODES = ("", "orchestrator")`, registered in `server.py`). Switching mode returns `409` while the slot is running. The standalone `orchestrated` builtin app was removed — it is deleted on startup via `manager.py`'s `_escalated` list — and there is no `/orchestrated` page or backend route.

- **Simple tasks** (majority): behaves like chat — direct response, no planning overhead.
- **Complex tasks**: structured plan, stage-by-stage execution, specialist agents — with user interaction between stages.
- **Over time**: learns from user guidance and past plans, needs less human support for longer autonomous runs.

```
                    Simple tasks          Complex tasks         Long autonomous
                    ─────────────         ─────────────         ──────────────
Chat mode           ✅ Direct response    ❌ No structure       ❌ No planning
Task Runner         ❌ Overkill           ✅ Plan + execute     ✅ Hands-off
Autopilot           ✅ Direct response    ✅ Plan + execute     ✅ Learns to need
                                          + user interaction    less human support
```

### Impact on Existing Features

| Feature | Impact |
|---------|--------|
| Chat mode (default) | No change. Standard prompt, fire-and-forget sub-agents. |
| Chat mode (conductor_skill=true) | Adds agent routing knowledge. Same prompt, same UX. |
| Autopilot (Chat β) | Orchestrator prompt loaded. Plan lessons injected. OrchestrationTracker active. |
| Task Runner | No changes. Future: `task_run` opens orchestrated session with spec pre-loaded. |
| Cron / Heartbeat | Plan lesson consolidation added to daily cycle. Stale session cleanup added. |
| Sub-Agent System | Disk streaming, spawn queuing (2s stagger), Activity Viewer events, resource caps. |
| Memory | New plan memory (`~/.kirocrew/plan_memory/`). Plan lessons consolidated periodically. |

---

## 2. Architecture

### Key Files

| File | Role |
|------|------|
| `chat.py` | Main execution loop (`_run_chat`), plan validation, auto-run, stage gates, plan-action endpoint |
| `context_management.py` | `OrchestrationTracker`, `validate_plan_format`, `ensure_go_all_option`, `looks_like_plan`, `strip_plan_markers`, `rephrase_plan` |
| `gateway.py` | Subagent result handling, round counting, failure escalation (Slack path) |
| `agents/prompt-orchestrator.md` | LLM instructions for plan format, stage execution, failure handling |
| `conductor_skill.py` | Agent roster + delegation guidelines (opt-in skill) |
| `session_workspace.py` | Per-session directory for history + result files |
| `agent_metadata.py` | Per-agent routing metadata |
| `frontend/.../AssistantMessage.tsx` | Parses `[OPTION:]` tags into clickable buttons |
| `frontend/.../ChatPage.tsx` | Wires button clicks to `api.planAction()` |
| `frontend/.../ActivityViewer.tsx` | Tools/Subagents/Logs tabs |

### State on `_ChatSlot`

| Attribute | Type | Purpose |
|-----------|------|---------|
| `mode` | `str` | `"orchestrator"` enables all plan machinery |
| `_orch_tracker` | `OrchestrationTracker` | Tracks stages, rounds, failures, escalation |
| `_stage_titles` | `list[str]` | Stage titles extracted from plan text |
| `_plan_goal` | `str` | Goal from `📋 Plan for:` header |
| `_plan_stage_count` | `int` (property) | `len(_stage_titles)` — total stages |
| `_auto_run` | `bool` | When True, auto-advances past stage gates |

### Execution Guards

| Guard | Value | Mechanism |
|-------|-------|-----------|
| Concurrent sub-agents | 3 | `SubagentManager._max_concurrent` |
| Staggered start | 2s | `_drain_queue()` via `call_later` |
| Per-agent timeout | 30 min | `_TIMEOUT_SECS`, reaper loop |
| Per-agent tool calls | 100 | `_TURN_LIMIT` |
| Failure escalation | 3 failures | `OrchestrationTracker` (code-enforced) |
| Session round limit | 80% context | Same as chat mode |
| Result file cap | 500 KB | `cap_result_file()` |
| Streaming buffer cap | 50K chars | `cap_streaming_text()` |
| Completed agent eviction | 50 entries | `evict_completed_agents()` |
| Stage timeout          | 30 min  | `OrchestrationTracker.is_stage_timed_out()`, configurable via `orchestrator.stage_timeout_seconds` |

### Context Injection

Standard chat context (55K budget) plus:
- **Plan lessons** from `plan_lessons.md` (all orchestrator sessions)
- **Conversation history** includes sub-agent completion events (200-word summaries)
- **Full result files** on disk — LLM reads via tools when needed

### Flow

```
User request
  → LLM generates plan with [OPTION: Go | Go All | Cancel]
  → User clicks Go/Go All/Cancel
  → Backend executes stage (LLM makes tool calls, spawns subagents)
  → Stage completes → checkpoint → next stage gate (or auto-advance)
  → All stages done → plan complete
```

---

## 3. Planning

### 3.1 LLM Generates Plan

When `slot.mode == "orchestrator"`, the prompt instructs the LLM to output:

```
📋 Plan for: "<description>"

Stage 1: <Title>
  - task
  - task

Stage 2: <Title>
  - task

[OPTION: Go | Go All | Cancel]
```

### 3.2 Plan Validation Pipeline

After the LLM response completes, `_run_chat` runs a 3-tier validation:

```
validate_plan_format(text)
  → has 📋 header? has Stage N: lines? has [OPTION:] footer?

If no header but looks_like_plan() regex matches (≥2 Stage/Phase/Step lines):
  → _rephrase_plan_lite(might_not_be_plan=True)
  → LLM reformats or returns NOT_A_PLAN: prefix

If has_plan but invalid (missing stages or footer):
  → _rephrase_plan_lite() → LLM fixes format
  → If still invalid → strip_plan_markers() + has_plan=False (degrade to chat)

If valid:
  → ensure_go_all_option() patches [OPTION: Go | Cancel] → [OPTION: Go | Go All | Cancel]
  → _plan_stage_count = count of "Stage N" lines
  → Plan event logged
```

`_rephrase_plan_lite` uses the cheap background session (Sonnet) — not the main Opus session.

### 3.3 Frontend Rendering

`AssistantMessage.tsx` parses `[OPTION: Go | Go All | Cancel]` via regex:
- Detects `isPlan` = true when both `📋 Plan for:` header and `Stage N:` lines are present
- Renders each option as a clickable button
- Plan buttons → `onPlanAction(action)` → `POST /api/chat/slots/{slot}/plan-action`
- Non-plan buttons → `onOption(text)` → sends as regular chat message

---

## 4. Stage Gates

### Go

1. Validates slot exists, mode is `"orchestrator"`, action is valid
2. `_ensure_tracker_and_sep()` — creates tracker if needed, injects `───── Stage N ─────` separator
3. Appends `"Go"` as user message, broadcasts to UI
4. Creates `asyncio.create_task(_run_chat(state, slot, "Go"))` — LLM executes the stage
5. After stage completes, LLM outputs checkpoint + `[OPTION: Go | Go All | Cancel]` → waits for next click

Alternative: typing "go" in the chat box triggers the same flow via text detection.

### Go All

1. Same as Go, plus: sets `slot._auto_run = True`
2. Displays "Go All" in the UI but passes "Go" to `_run_chat` (LLM only sees "Go")
3. After each stage, auto-run block checks:
   - `_auto_run` is True
   - `tracker.current_stage < _plan_stage_count`
   - `tracker.stopped` is False
4. If conditions met: sends `"Continue the plan. Execute Stage N of M."` (hidden from UI)
5. Queues continuation via `_auto_run_pending` — `finally` block creates new task, keeping call stack flat
6. When stages exhausted or stopped: clears `_auto_run = False`

**Auto-run stop conditions:** all stages completed, tracker stopped, no tracker, `_plan_stage_count` is 0.

### Cancel

1. `tracker.stop()` if tracker exists
2. `slot._auto_run = False`
3. Cancels all running subagent tasks
4. Appends "🛑 Plan cancelled." as assistant message
5. Broadcasts `chat_done` — no LLM invocation

### Queuing

If the slot is already running when Go/Go All is clicked:
- Message appended to `slot._queue`
- Returns `{"ok": True, "queued": True}`
- When current `_run_chat` finishes, `finally` block pops from queue and starts new `_run_chat`

---

## 5. Execution

### 5.1 Stage Execution

Each stage is a single `_run_chat` invocation where the LLM:
1. Reads the plan and current stage from conversation context
2. Makes tool calls (file reads, edits, bash commands)
3. Optionally spawns subagents for parallel work
4. Outputs a checkpoint summary + `[OPTION:]` footer

### 5.2 Subagent Handling

In `gateway.py`, subagent results are tracked per-stage:
- Success: `tracker.record_success(task_key)` — resets failure count
- Failure: `tracker.record_failure(task_key)` — increments count
- When all pending agents complete: `tracker.record_round(stage)` — counts as one round

### 5.3 Stage Separators

`_ensure_tracker_and_sep()` injects visual separators:
```
───── Stage 2 ─────
```
- Created on first "Go" and each subsequent stage transition
- Caps `next_stage` at `_plan_stage_count`
- Only records a round when entering a new stage

---

## 6. Auto-Run Continuation

After each `_run_chat` completes, the auto-run block:

```python
if _should_auto_continue(slot):
    _tracker = slot._orch_tracker
    _max = slot._plan_stage_count
    if _tracker.is_stage_timed_out():
        slot._auto_run = False  # Timeout — stop auto-run
    else:
        _next = _tracker.current_stage + 1
        _cont = f"Continue the plan. Execute Stage {_next} of {_max}."
        slot.append("user", _cont, "msg msg-u auto-go")
        slot._auto_run_pending = _cont
        return
elif slot._auto_run:
    slot._auto_run = False  # Terminal
```

Design decisions:
- **Tracker-based detection** — uses `current_stage < _plan_stage_count` instead of scanning LLM output for `[OPTION:]` tags
- **Descriptive messages** — `"Continue the plan. Execute Stage N of M."` gives LLM clear context
- **Hidden from UI** — `auto-go` CSS class hides auto-continue messages
- **Iterative via task scheduling** — `finally` block picks up `_auto_run_pending` and creates a new `asyncio.create_task`, keeping the call stack flat

---

## 7. Failure Handling and Escalation

### 7.1 OrchestrationTracker Limits

| Limit | Value | Scope | What Happens |
|-------|-------|-------|-------------|
| `MAX_TASK_FAILURES` | 3 | Per task_key | System injects "ask user for guidance" |
| `MAX_STAGE_ROUNDS` | 3 | Per stage | System injects "ask user for guidance" |
| `MAX_STAGE_ESCALATIONS` | 2 | Per stage | `is_force_failed()` → "MUST stop, do NOT retry" |

### 7.2 Escalation Flow

```
Round 1-3: Normal execution
  → Round limit hit → has_escalated = True
  → System: "⚠️ Stage N has used 3/3 rounds. Ask user for guidance."
  → LLM asks user → user provides guidance
  → reset_after_guidance(): clears rounds, increments escalation count
  → Rounds 4-6: Fresh budget
  → Round limit hit again → escalation count = 2
  → is_force_failed() = True
  → System: "🛑 Stage N failed after 2 escalations. MUST stop."
```

### 7.3 Error Handling in `_run_chat`

| Exception | Behavior |
|-----------|----------|
| `CancelledError` | Preserves partial `assistant_text` (stop button) |
| `AcpError` | Preserves partial text + appends error |
| Generic `Exception` | Logs traceback + records failure |

The `finally` block always: releases session lock, processes queued messages, marks slot idle if no queue.

---

## 8. Stop and Cancel

### Stop via Text

When user types "stop"/"cancel"/"abort" while `tracker.has_escalated` is True:
1. `tracker.stop()` → `stopped = True`
2. `slot._auto_run = False`
3. Running subagent tasks cancelled
4. System: "🛑 [SYSTEM] Orchestration stopped by user."

### Stop via Cancel Button

Always works regardless of escalation state:
1. `tracker.stop()`, clear auto-run, cancel subagents
2. Message: "🛑 Plan cancelled."
3. No LLM invocation

### User Guidance After Escalation

If `tracker.has_escalated` but user sends a non-stop message:
- `reset_after_guidance()` gives fresh round/failure budgets
- Escalation counter increments (can't reset forever)

### Entry Points

| Entry Point | Trigger | Handler |
|-------------|---------|---------|
| Plan-action button | Frontend button click | `api_chat_plan_action` |
| Text "go" / "go all" | User types in chat box | `api_chat` |
| Text "stop" / "cancel" | User types during escalation | `api_chat` |
| Auto-run continuation | After stage with `_auto_run=True` | `_run_chat` |
| Subagent result | Subagent completes | `gateway.py` |

---

## 9. Known Issues

### ✅ Resolved / Not an Issue

| # | Issue | Resolution |
|---|-------|------------|
| ~~1~~ | ~~Recursive call stack for auto-run~~ | Fixed in CR-266105689: converted to iterative via `slot._auto_run_pending` + `asyncio.create_task` in `finally` block. Call stack stays flat regardless of plan size. |
| ~~2~~ | ~~No stage timeout~~ | Fixed in CR-266105689: 30 min default, configurable via `~/.kirocrew/config.json` `orchestrator.stage_timeout_seconds`. |
| ~~3~~ | ~~Context overflow~~ | Already handled: `_MAX_HISTORY_CHARS=8000`, per-message truncation at 500 chars, `build_stage_context()` uses stage summaries not full transcripts. Only current stage messages are unbounded (single stage rarely gets huge). |
| ~~4~~ | ~~No replanning~~ | By design. Human-in-the-loop: fail → retry with error → escalate to user → user guides → learning improves future plans. |
| ~~5~~ | ~~Escalation not code-enforced~~ | Already 2-tier: tier 1 is prompt-based (soft nudge), tier 2 is Python-enforced via `is_force_failed()` after 2 escalations (9 rounds). Acceptable. |
| ~~6~~ | ~~No partial stage recovery~~ | Already smart: `_collect_subagent_results()` sends full pass/fail summary to LLM which decides what to retry. Not blind retry. |
| ~~7~~ | ~~No cross-stage failure memory~~ | Not needed: stages are sequential, next stage only starts when previous completes. `tracker.stage_results` captures status. |
| ~~8~~ | ~~Stop button doesn't work for orchestrator~~ | Already works: `api_chat_slot_stop` kills kiro-cli process and cancels `slot.task`, which stops everything including auto-run. |
| ~~10~~ | ~~"Prompt already in progress" drops silently~~ | Fixed: `AcpPromptBusy(AcpError)` is now raised on "already in progress" (`acp/client.py`), and the Slack path auto-resets the wedged session (`slack/handler.py` → `sessions.reset`) so the next message cold-starts cleanly instead of hitting the same wall. |

### 🔵 Deferred

| # | Issue | Notes |
|---|-------|-------|
| 9 | No progress persistence | Gateway crash loses tracker state. Future feature. |

---

## 10. Proposed Optimizations

### P0 — Important for Robustness

| # | Optimization | Effort | Impact |
|---|-------------|--------|--------|
| 3 | **Progress checkpointing** — serialize tracker to disk per stage | M | Survives gateway crashes |
| 4 | **Prompt-in-progress retry** — queue or retry dropped prompts | S | Prevents stuck sessions |

### P1 — Nice to Have

| # | Optimization | Effort | Impact |
|---|-------------|--------|--------|
| 5 | Cross-stage context summary | M | Compact inter-stage memory (currently not needed due to existing caps) |
| 6 | Token prediction per stage | M | Estimate cost before spawning |

---

## 11. TODO

### Plan Memory
- [ ] Wire LLM call from HistoryConsolidator into `consolidate_plan_lessons()`
- [ ] Similarity search: embed plan descriptions, find top-5 related past plans

### Cost Control
- [ ] Token prediction per stage — estimate cost before spawning

### UX
- [ ] Stage progress indicator in Activity Viewer
- [ ] Plan approval as structured card with OPTIONS buttons

### Migration (Step 2)
- [ ] Feature flag to make autopilot the default page
- [ ] Make `task_run` open an autopilot session with spec pre-loaded
- [ ] Deprecate separate task runner UI
