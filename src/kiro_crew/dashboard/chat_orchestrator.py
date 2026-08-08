"""Orchestrator stage loop — Python-controlled plan execution."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.context_management import OrchestrationTracker
from kiro_crew.dashboard.chat_runner import _run_chat, _start_next_queued_turn
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.dashboard.turn_dispatch import _bounded_turn
from kiro_crew.hooks import safe_read_file
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import SecurityEvent, sel

logger = logging.getLogger(__name__)


def _build_stage_context(
    slot: "_ChatSlot",
    tracker: "OrchestrationTracker",
    stage_idx: int,
) -> str:
    """Build a focused context message for a single stage.

    *stage_idx* is 0-based.
    """
    titles = getattr(slot, "_stage_titles", [])
    goal = getattr(slot, "_plan_goal", "")
    total = slot._plan_stage_count

    parts: list[str] = []
    if goal:
        parts.append(f"🎯 Goal: {goal}")
    parts.append("Plan Status:")
    parts.append(tracker.status_summary(stage_idx, total, titles))

    # Previous stage result paths (LLM can read details via file tools)
    prev_paths = _previous_result_paths(tracker, stage_idx)
    if prev_paths:
        parts.append(f"## Previous Stage Results\n{prev_paths}")

    title = titles[stage_idx] if stage_idx < len(titles) else ""
    label = f"Stage {stage_idx + 1}: {title}" if title else f"Stage {stage_idx + 1}"
    parts.append(f"## Current Stage — {label}")
    # Include task bullets from the original plan
    descriptions = getattr(slot, "_stage_descriptions", [])
    if stage_idx < len(descriptions) and descriptions[stage_idx]:
        parts.append("\n".join(descriptions[stage_idx]))
    parts.append(
        f"Execute Stage {stage_idx + 1} of {total} now. "
        "When you have fully completed all work for this stage "
        "(including waiting for any subagent results), "
        "your turn will end and the orchestrator will advance to the next stage."
    )
    return "\n\n".join(parts)


def _previous_result_paths(
    tracker: "OrchestrationTracker",
    current_idx: int,
) -> str:
    """Return compacted previous stage results with paths for full details."""
    _max_per_stage = 2000
    parts: list[str] = []
    for stage_num in range(1, current_idx + 1):
        path_str = tracker._stage_results.get(stage_num)
        if not path_str:
            continue
        p = Path(path_str)
        content = ""
        if p.exists() and not is_sensitive_path(str(p)):
            try:
                file_size = p.stat().st_size
                if file_size <= _max_per_stage:
                    content = p.read_bytes().decode("utf-8", errors="replace")
                else:
                    # Read only head + tail in binary mode (consistent byte units)
                    head_bytes = _max_per_stage * 3 // 10  # 30%
                    tail_bytes = _max_per_stage - head_bytes  # 70%
                    with open(p, "rb") as f:
                        head_raw = f.read(head_bytes)
                        f.seek(max(0, file_size - tail_bytes))
                        tail_raw = f.read()
                    content = (
                        head_raw.decode("utf-8", errors="replace")
                        + "\n...[truncated]...\n"
                        + tail_raw.decode("utf-8", errors="replace")
                    )
            except (OSError, ValueError):
                pass
        header = f"### Stage {stage_num}"
        if content:
            parts.append(f"{header}\n{content}\nFull result: `{path_str}`")
        else:
            parts.append(f"{header}\nFull result: `{path_str}`")
    return "\n\n".join(parts)


def _capture_stage_result(
    slot: "_ChatSlot",
    stage_num: int,
) -> str:
    """Extract assistant messages since stage start and write to disk.

    Returns the path to the result file.
    """
    # Collect assistant text from the most recent messages (since last stage separator)
    result_parts: list[str] = []
    for m in reversed(slot.messages):
        role = m.get("role", "")
        cls = m.get("cls", "")
        if isinstance(cls, str) and "stage-sep" in cls:
            break  # hit the separator for this stage
        if role == "assistant":
            # Defence in depth before this reaches disk. Both upstream sources are
            # already clean — live turns via chat_runner._flush_segment, restored
            # turns via the load-time content pass — but this writes a NEW file
            # outside the history log's own redaction, so it does not depend on
            # that. Redaction is idempotent, so the common case is a no-op.
            text = m.get("content", "")
            text, _ = redact_exfiltration_urls(text)
            text, _ = redact_credentials(text)
            result_parts.append(text)
    result_parts.reverse()
    result_text = "\n\n".join(result_parts)

    session_dir = config_dir() / "sessions" / slot.key
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / f"stage_{stage_num}_result.md"
    path.write_text(result_text, encoding="utf-8")
    return str(path)


async def _stage_loop(
    state: "DashboardState",
    slot: "_ChatSlot",
    auto_run: bool,
) -> None:
    """Python-controlled stage execution loop.

    Iterates through plan stages, calling ``_run_chat`` once per stage.
    Stage boundaries are enforced by Python code, not LLM prompts.
    """
    tracker = slot._orch_tracker
    if tracker is None:
        try:
            _timeout = KiroCrewConfig.load().orchestrator.stage_timeout_seconds
        except Exception:
            _timeout = 1800
        tracker = OrchestrationTracker(stage_timeout_seconds=_timeout)
        slot._orch_tracker = tracker

    total = slot._plan_stage_count
    titles = getattr(slot, "_stage_titles", [])

    # Determine starting stage (0-based index)
    start_idx = tracker.current_stage if tracker._stage_rounds else 0

    logger.info(
        "Stage loop start: slot=%s total=%d start_idx=%d auto_run=%s titles=%s",
        slot.key, total, start_idx, auto_run, titles,
    )

    _paused = False
    _cancelled = False
    # Mark the ENTIRE stage-execution lifetime, not each _run_chat call. A
    # stage turn can queue a recovery/continue turn (empty-response re-queue,
    # stale/tool-stall recovery) that runs slightly later on the same slot; a
    # per-call clear would drop the guard before that recovery ran, letting its
    # plan-shaped output re-arm/re-count the plan (GPT finding). The flag is
    # cleared once in the outer `finally` when the loop actually exits (pause,
    # completion, break, or error) — so a later Cancel + re-plan can arm again.
    #
    # It ALSO gates mid-plan message handling: while set, api_chat queues a user
    # message (chip card) even when slot.task is momentarily idle between stages,
    # and _start_next_queued_turn HOLDS user messages (recovery/system still
    # drain) so they never run concurrently with the plan — handed off in the
    # finally once the plan ends.
    slot._in_stage_execution = True
    try:
        for stage_idx in range(start_idx, total):
            if slot._stopping:
                break

            stage_num = stage_idx + 1  # 1-based for display

            # Defensive clamp: never build or execute a stage beyond the CURRENT
            # plan size. `total` is captured once at range() creation; if the
            # live stage count ever shrank mid-run, continuing would emit a
            # phantom "Stage N of M" (N > M). Stop cleanly instead.
            if stage_idx >= slot._plan_stage_count:
                logger.warning(
                    "Stage loop clamp for slot %s: stage_idx=%d >= plan_stage_count=%d; stopping",
                    slot.key, stage_idx, slot._plan_stage_count,
                )
                break

            # Check timeout BEFORE recording new round (record_round resets timer)
            if tracker.is_stage_timed_out():
                slot._auto_run = False
                _timeout_msg = (
                    f"⏱️ Stage {stage_num} timed out after {tracker.timeout_human}. "
                    "Auto-run stopped."
                )
                slot.append("assistant", _timeout_msg, "msg msg-a")
                state.broadcast_ws(
                    "chat_append",
                    {"slot": slot.key, "html": _timeout_msg, "cls": "msg msg-a"},
                )
                sel().log(
                    SecurityEvent(
                        event_id=uuid.uuid4().hex,
                        timestamp=datetime.now(tz=timezone.utc).isoformat(),
                        event_type="auto_run_timeout",
                        caller_identity=f"dashboard:{slot.key}",
                        agent=getattr(slot, "agent", ""),
                        source="dashboard",
                        operation="stage_timeout",
                        outcome="stopped",
                        resources=f"slot={slot.key},stage={stage_num}",
                    )
                )
                break

            # Record round and emit separator (after timeout check)
            tracker.record_round(stage_num)
            title = titles[stage_idx] if stage_idx < len(titles) else ""
            label = f"Stage {stage_num}: {title}" if title else f"Stage {stage_num}"
            sep = f"\n\n───── {label} ─────\n"
            sep, _ = redact_exfiltration_urls(sep)
            sep, _ = redact_credentials(sep)
            slot.append("assistant", sep, "msg msg-a stage-sep")
            state.broadcast_ws(
                "chat_append",
                {"slot": slot.key, "html": sep, "cls": "msg msg-a stage-sep"},
            )

            # Build focused context and execute
            context = _build_stage_context(slot, tracker, stage_idx)
            context, _ = redact_exfiltration_urls(context)
            context, _ = redact_credentials(context)
            sel().log(
                SecurityEvent(
                    event_id=uuid.uuid4().hex,
                    timestamp=datetime.now(tz=timezone.utc).isoformat(),
                    event_type="auto_run_continue",
                    caller_identity=f"dashboard:{slot.key}",
                    agent=getattr(slot, "agent", ""),
                    source="dashboard",
                    operation="stage_auto_advance",
                    outcome="approved",
                    resources=f"slot={slot.key},stage={stage_num},total={total}",
                )
            )

            # Inject as hidden user message and run LLM turn
            logger.info(
                "Stage %d/%d: context=%d chars, messages=%d",
                stage_num, total, len(context), len(slot.messages),
            )
            slot.append("user", context, "msg msg-u auto-go")
            try:
                # `_bounded_turn`, NOT `asyncio.wait_for`. `_run_chat` CATCHES
                # CancelledError (it flushes the partial assistant output and
                # returns), so wait_for would absorb its own deadline: the inner
                # task completes "normally", wait_for hands back a value instead
                # of raising, and a half-finished stage would advance as if it
                # had succeeded. `_bounded_turn` records that its own timer
                # fired and raises on that observed fact, so a swallowed
                # cancellation still surfaces. See its docstring in
                # turn_dispatch.py -- it exists for exactly this trap.
                #
                # A falsy stage_timeout_seconds means "disabled" everywhere else
                # in the tracker, so skip the ceiling entirely rather than
                # passing 0, which would cut every stage instantly.
                _turn_timeout = tracker.stage_timeout_seconds
                if _turn_timeout:
                    await _bounded_turn(_run_chat(state, slot, context), _turn_timeout)
                else:
                    await _run_chat(state, slot, context)
            except (asyncio.TimeoutError, TimeoutError):
                # `_bounded_turn` raises builtin TimeoutError; on 3.10
                # asyncio.TimeoutError is a DIFFERENT class, so catch both (the
                # convention already used by _run_pending_synthesis).
                logger.error(
                    "Stage %d exceeded its %ds ceiling for slot %s",
                    stage_num, tracker.stage_timeout_seconds, slot.key,
                )
                _timeout_msg = (
                    f"⏱️ Stage {stage_num} timed out after {tracker.timeout_human}. "
                    "Auto-run stopped."
                )
                slot._auto_run = False
                slot.append("assistant", _timeout_msg, "msg msg-a")
                state.broadcast_ws(
                    "chat_append",
                    {"slot": slot.key, "html": _timeout_msg, "cls": "msg msg-a"},
                )
                sel().log(
                    SecurityEvent(
                        event_id=uuid.uuid4().hex,
                        timestamp=datetime.now(tz=timezone.utc).isoformat(),
                        event_type="auto_run_timeout",
                        caller_identity=f"dashboard:{slot.key}",
                        agent=getattr(slot, "agent", ""),
                        source="dashboard",
                        operation="stage_turn_ceiling",
                        outcome="stopped",
                        resources=f"slot={slot.key},stage={stage_num}",
                    )
                )
                break
            except Exception:
                logger.exception("_run_chat failed during stage %d for slot %s", stage_num, slot.key)
                _err_msg = f"❌ Stage {stage_num} failed due to an internal error. Auto-run stopped."
                slot._auto_run = False
                slot.append("assistant", _err_msg, "msg msg-a")
                state.broadcast_ws(
                    "chat_append",
                    {"slot": slot.key, "html": _err_msg, "cls": "msg msg-a"},
                )
                sel().log(
                    SecurityEvent(
                        event_id=uuid.uuid4().hex,
                        timestamp=datetime.now(tz=timezone.utc).isoformat(),
                        event_type="auto_run_stage_error",
                        caller_identity=f"dashboard:{slot.key}",
                        agent=getattr(slot, "agent", ""),
                        source="dashboard",
                        operation="stage_error",
                        outcome="error",
                        resources=f"slot={slot.key},stage={stage_num}",
                    )
                )
                break

            if slot._stopping:
                break

            # Wait for pending subagents spawned during this stage
            _sa_rounds = 0
            # Dynamic poll cap. Each poll sleeps 2s, so `stage_timeout // 4`
            # rounds ≈ half the stage timeout in wall-clock, hard-capped at 450
            # rounds (15 min). This replaces a fixed 150 (5 min), which was far
            # shorter than a subagent's own 30-min budget and abandoned
            # legitimate long-running analysis agents mid-flight.
            # A falsy stage timeout means "disabled", so fall back to the 15-min
            # ceiling rather than 0 (which would skip the wait entirely).
            # Worst case per stage is therefore turn-timeout + subagent-wait;
            # the total-plan watchdog (separate follow-up) bounds the run.
            if tracker.stage_timeout_seconds:
                _sa_max_rounds = min(tracker.stage_timeout_seconds // 4, 450)
            else:
                _sa_max_rounds = 450
            session_key = f"dashboard:{slot.key}"
            if state.subagents is None:
                # Fail-closed: subagent manager missing — stop auto-run
                logger.warning(
                    "Stage %d: subagents manager is None for slot %s"
                    " — stopping auto-run (fail-closed)",
                    stage_num, slot.key,
                )
                _fc_msg = (
                    f"⚠️ Stage {stage_num}: subagent manager unavailable. "
                    "Auto-run stopped."
                )
                slot._auto_run = False
                slot.append("assistant", _fc_msg, "msg msg-a")
                state.broadcast_ws(
                    "chat_append",
                    {"slot": slot.key, "html": _fc_msg, "cls": "msg msg-a"},
                )
                sel().log(
                    SecurityEvent(
                        event_id=uuid.uuid4().hex,
                        timestamp=datetime.now(tz=timezone.utc).isoformat(),
                        event_type="auto_run_subagent_check_failed",
                        caller_identity=f"dashboard:{slot.key}",
                        agent=getattr(slot, "agent", ""),
                        source="dashboard",
                        operation="subagent_manager_missing",
                        outcome="stopped",
                        resources=f"slot={slot.key},stage={stage_num}",
                    )
                )
                break
            else:
                _pending = state.subagents.running_agents_for(session_key)
                # Fail-closed: if running_agents_for returns None (error),
                # stop auto-run rather than silently skipping verification
                if _pending is None:
                    logger.warning(
                        "Stage %d: running_agents_for returned None for slot %s"
                        " — stopping auto-run (fail-closed)",
                        stage_num, slot.key,
                    )
                    _fc_msg = (
                        f"⚠️ Stage {stage_num}: subagent check failed. "
                        "Auto-run stopped."
                    )
                    slot._auto_run = False
                    slot.append("assistant", _fc_msg, "msg msg-a")
                    state.broadcast_ws(
                        "chat_append",
                        {"slot": slot.key, "html": _fc_msg, "cls": "msg msg-a"},
                    )
                    sel().log(
                        SecurityEvent(
                            event_id=uuid.uuid4().hex,
                            timestamp=datetime.now(tz=timezone.utc).isoformat(),
                            event_type="auto_run_subagent_check_failed",
                            caller_identity=f"dashboard:{slot.key}",
                            agent=getattr(slot, "agent", ""),
                            source="dashboard",
                            operation="running_agents_for_none",
                            outcome="stopped",
                            resources=f"slot={slot.key},stage={stage_num}",
                        )
                    )
                    break
                # Emit initial status so user knows we're waiting
                state.broadcast_ws(
                    "chat_status",
                    {"slot": slot.key, "status": f"Waiting for {len(_pending)} subagent(s)..."},
                )
                while (
                    _pending
                    and _sa_rounds < _sa_max_rounds
                    and not slot._stopping
                ):
                    _sa_rounds += 1
                    await asyncio.sleep(2)
                    _pending = state.subagents.running_agents_for(session_key)
                    # Update status every 10 polls (~20s)
                    if _pending and _sa_rounds % 10 == 0:
                        state.broadcast_ws(
                            "chat_status",
                            {"slot": slot.key, "status": f"Waiting for {len(_pending)} subagent(s)..."},
                        )
                    if _pending is None:
                        logger.warning(
                            "Stage %d: running_agents_for returned None during"
                            " polling for slot %s — stopping auto-run",
                            stage_num, slot.key,
                        )
                        slot._auto_run = False
                        break
            if _pending is None and not slot._auto_run:
                # Fail-closed: running_agents_for returned None during polling
                _fc_msg = (
                    f"⚠️ Stage {stage_num}: subagent check failed during polling. "
                    "Auto-run stopped."
                )
                slot.append("assistant", _fc_msg, "msg msg-a")
                state.broadcast_ws(
                    "chat_append",
                    {"slot": slot.key, "html": _fc_msg, "cls": "msg msg-a"},
                )
                sel().log(
                    SecurityEvent(
                        event_id=uuid.uuid4().hex,
                        timestamp=datetime.now(tz=timezone.utc).isoformat(),
                        event_type="auto_run_subagent_check_failed",
                        caller_identity=f"dashboard:{slot.key}",
                        agent=getattr(slot, "agent", ""),
                        source="dashboard",
                        operation="running_agents_for_none_during_poll",
                        outcome="stopped",
                        resources=f"slot={slot.key},stage={stage_num}",
                    )
                )
                break
            if _sa_rounds >= _sa_max_rounds:
                _wait_secs = _sa_rounds * 2
                logger.warning(
                    "Stage %d: subagent wait exhausted after %ds (%d rounds, cap %d) for slot %s",
                    stage_num, _wait_secs, _sa_rounds, _sa_max_rounds, slot.key,
                )
                slot._auto_run = False
                _sa_msg = (
                    f"⚠️ Stage {stage_num}: subagent wait exhausted after "
                    f"{_wait_secs // 60} minutes. "
                    "Auto-run stopped — some results may be incomplete."
                )
                slot.append("assistant", _sa_msg, "msg msg-a")
                state.broadcast_ws(
                    "chat_append",
                    {"slot": slot.key, "html": _sa_msg, "cls": "msg msg-a"},
                )
                sel().log(
                    SecurityEvent(
                        event_id=uuid.uuid4().hex,
                        timestamp=datetime.now(tz=timezone.utc).isoformat(),
                        event_type="auto_run_subagent_timeout",
                        caller_identity=f"dashboard:{slot.key}",
                        agent=getattr(slot, "agent", ""),
                        source="dashboard",
                        operation="subagent_wait_exhausted",
                        outcome="stopped",
                        resources=f"slot={slot.key},stage={stage_num}",
                    )
                )
                break

            if slot._stopping:
                break

            # Capture result to disk
            try:
                result_path = _capture_stage_result(slot, stage_num)
                tracker.record_stage_result(stage_num, result_path)
            except OSError:
                logger.warning("Failed to capture stage %d result to disk", stage_num, exc_info=True)

            # Gate: if not auto_run, wait for user approval
            if not auto_run:
                # Emit completion message — user must click Go for next stage
                if stage_idx + 1 < total:
                    next_title = titles[stage_idx + 1] if stage_idx + 1 < len(titles) else ""
                    next_label = f"Stage {stage_idx + 2}: {next_title}" if next_title else f"Stage {stage_idx + 2}"
                    done_msg = (
                        f"✅ Stage {stage_num} complete. Click **Go** to proceed to {next_label}."
                        "\n\n[OPTION: Go | Go All | Cancel]"
                    )
                    done_msg, _ = redact_exfiltration_urls(done_msg)
                    done_msg, _ = redact_credentials(done_msg)
                    slot.append("assistant", done_msg, "msg msg-a")
                    state.broadcast_ws(
                        "chat_message",
                        {"slot": slot.key, "role": "assistant", "content": done_msg},
                    )
                    _paused = True
                    return  # User's next "Go" click will re-enter _stage_loop
        else:
            # for loop completed without break — all stages done
            if not slot._stopping and start_idx < total:
                slot._auto_run = False
                # Build execution summary from captured stage results
                summary_lines = [f"✅ All {total} stages complete."]
                for s_idx in range(total):
                    s_num = s_idx + 1
                    s_title = titles[s_idx] if s_idx < len(titles) else ""
                    result_path = tracker._stage_results.get(s_num)
                    excerpt = ""
                    if result_path:
                        try:
                            text = safe_read_file(result_path).strip()
                            for line in text.splitlines():
                                line = line.strip()
                                if line and not line.startswith("───"):
                                    excerpt = line[:120]
                                    break
                        except (OSError, PermissionError):
                            pass
                    label = f"Stage {s_num}: {s_title}" if s_title else f"Stage {s_num}"
                    if excerpt:
                        summary_lines.append(f"  {label} — {excerpt}")
                    else:
                        summary_lines.append(f"  {label} — done")
                done_msg = "\n".join(summary_lines)
                done_msg, _ = redact_exfiltration_urls(done_msg)
                done_msg, _ = redact_credentials(done_msg)
                slot.append("assistant", done_msg, "msg msg-a")
                state.broadcast_ws(
                    "chat_message",
                    {"slot": slot.key, "role": "assistant", "content": done_msg},
                )
                sel().log(
                    SecurityEvent(
                        event_id=uuid.uuid4().hex,
                        timestamp=datetime.now(tz=timezone.utc).isoformat(),
                        event_type="auto_run_completed",
                        caller_identity=f"dashboard:{slot.key}",
                        agent=getattr(slot, "agent", ""),
                        source="dashboard",
                        operation="auto_run_terminal",
                        outcome="completed",
                        resources=f"slot={slot.key},stages={total}",
                    )
                )
    except asyncio.CancelledError:
        # Hard stop / slot deletion: do NOT hand off queued work below (the slot
        # is being torn down and a started turn would run orphaned). Mark it and
        # re-raise so the task ends cancelled.
        _cancelled = True
        raise
    finally:
        # Clear the stage-execution guard exactly once, when the loop exits
        # (pause / completion / break / error). This spans any queued recovery
        # turns a stage started, and lets a later Cancel + re-plan arm again.
        slot._in_stage_execution = False
        logger.info(
            "Stage loop end: slot=%s current_stage=%s/%s stopping=%s auto_run=%s",
            slot.key, tracker.current_stage, total, slot._stopping, slot._auto_run,
        )
        # Hand off any messages the user queued while the plan ran (held via the
        # _in_stage_execution gate in _start_next_queued_turn — now cleared above).
        # If one starts it owns slot.task, so skip the idle-close; a cancelled loop
        # skips the handoff entirely (queue preserved for the torn-down slot).
        # ``state._slots.get(...) is slot`` guards a slot DELETED mid-plan (slot.task
        # is None between stages, so deletion isn't blocked): never launch a turn on
        # a slot that is no longer registered. ``not slot._last_turn_auth_required``
        # mirrors _run_chat's own guard: a signed-out CLI holds the queue for
        # post-login resume instead of popping it into another auth failure.
        # ``not slot.running`` defers entirely to a turn a stage's _run_chat may
        # have already started (e.g. a refusal-recovery continuation): that live
        # task owns slot.task and will drain the queue + emit chat_done itself, so
        # we must not start a second turn or clobber/idle-close over it.
        _next_started = False
        if (
            not _cancelled
            and not slot.running
            and not slot._last_turn_auth_required
            and state._slots.get(slot.key) is slot
            and slot._queue
            and not slot._stopping
        ):
            state.push_slots_update()
            _next_started = await _start_next_queued_turn(state, slot)
        if not _next_started and not slot.running:
            if not _paused:
                slot.append("done", "", "done")
                state.broadcast_ws("chat_done", {"slot": slot.key})
            # Clean up task so the slot is available for the next "Go" click
            # (paused) or new messages (completed).
            slot.task = None
        state.push_slots_update()


async def api_chat_plan_action(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/plan-action — execute Go/Go All/Cancel on a plan.

    Unlike /api/chat, this does NOT re-invoke the LLM for Cancel.
    Go/Go All inject "Go" into the chat to advance the plan.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    action = (body.get("action") or "").strip().lower()
    if action not in ("go", "go all", "cancel"):
        return web.json_response({"error": "action must be go, go all, or cancel"}, status=400)
    if getattr(slot, "mode", "") != "orchestrator":
        return web.json_response(
            {"error": "plan actions only available in orchestrator mode"}, status=400
        )

    try:
        sel().log_api_access(
            caller=f"dashboard:{name}",
            operation=f"plan_action:{action}",
            outcome="ok",
            resources=slot.key,
        )
    except Exception:
        logger.warning("SEL audit failed for plan action %s", action, exc_info=True)

    if action == "cancel":
        tracker = slot._orch_tracker
        if tracker and not tracker.stopped:
            tracker.stop()
        slot._auto_run = False
        if state.subagents:
            session_key = f"dashboard:{slot.key}"
            for a in (state.subagents.running_agents_for(session_key) or []):
                t = state.subagents._tasks.get(a["id"])
                if t and not t.done():
                    t.cancel()
        stop_msg = "🛑 Plan cancelled."
        slot.append("assistant", stop_msg, "msg msg-a")
        state.broadcast_ws(
            "chat_message", {"slot": slot.key, "role": "assistant", "content": stop_msg}
        )
        state.broadcast_ws("chat_done", {"slot": slot.key})
        return web.json_response({"ok": True, "cancelled": True})

    # Go or Go All — use Python-controlled stage loop
    if slot.running:
        slot.queue_append("Go")
        return web.json_response({"ok": True, "queued": True})

    is_auto = action == "go all"
    if is_auto:
        slot._auto_run = True
        logger.info("Auto-run enabled for slot %s via plan-action", slot.key)
        sel().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex,
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="auto_run_enabled",
                caller_identity=f"dashboard:{slot.key}",
                agent=getattr(slot, "agent", ""),
                source="dashboard",
                operation="go_all",
                outcome="approved",
                resources=f"slot={slot.key}",
            )
        )

    _label = "Go All" if is_auto else "Go"
    slot.append("user", _label, "msg msg-u")
    state.broadcast_ws("chat_message", {"slot": slot.key, "role": "user", "content": _label})
    if not is_auto:
        sel().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex,
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="stage_approved",
                caller_identity=f"dashboard:{slot.key}",
                agent=getattr(slot, "agent", ""),
                source="dashboard",
                operation="go",
                outcome="approved",
                resources=f"slot={slot.key}",
            )
        )
    task = asyncio.create_task(_stage_loop(state, slot, auto_run=is_auto))
    slot.task = task
    slot._recovery_retrigger_count = 0
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    state.push_slots_update()
    return web.json_response({"ok": True})
