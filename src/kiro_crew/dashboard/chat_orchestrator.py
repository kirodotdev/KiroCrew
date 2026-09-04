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
from kiro_crew.dashboard.state import DashboardState, _ChatSlot, append_and_surface
from kiro_crew.dashboard.turn_dispatch import _bounded_turn
from kiro_crew.hooks import safe_read_file
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import SecurityEvent, sel

logger = logging.getLogger(__name__)


async def _build_stage_context(
    slot: "_ChatSlot",
    tracker: "OrchestrationTracker",
    stage_idx: int,
) -> str:
    """Build a focused context message for a single stage.

    *stage_idx* is 0-based. Async because inlining the previous stages' results
    reads them off disk, which must not block the event loop the stage runs on.
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
    prev_paths = await _previous_result_paths(tracker, stage_idx)
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


def _read_previous_results(recorded: list[tuple[int, str]]) -> str:
    """Read each recorded stage result and compact it. Blocking.

    Split out so the reads can be handed to a worker thread as a unit. It takes
    an already-materialised ``(stage_num, path)`` list rather than the tracker,
    so nothing the event loop mutates is reachable from the worker.
    """
    _max_per_stage = 2000
    parts: list[str] = []
    for stage_num, path_str in recorded:
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


async def _previous_result_paths(
    tracker: "OrchestrationTracker",
    current_idx: int,
) -> str:
    """Return compacted previous stage results with paths for full details.

    Stage N inlines every earlier stage's result, so the read count grows with
    the plan and lands at each stage boundary. ``_stage_loop`` is async, so those
    reads are offloaded; the path list is snapshotted here first because
    ``tracker._stage_results`` is mutated on the loop by ``record_stage_result``
    as stages finish.
    """
    recorded: list[tuple[int, str]] = []
    for stage_num in range(1, current_idx + 1):
        path_str = tracker._stage_results.get(stage_num)
        if path_str:
            recorded.append((stage_num, path_str))
    if not recorded:
        # The first stage has nothing to inline; skip the worker hop entirely.
        return ""
    return await asyncio.to_thread(_read_previous_results, recorded)


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


def _completion_excerpts(result_paths: tuple[tuple[int, str], ...]) -> dict[int, str]:
    """Read captured stage results and return one summary excerpt per stage.

    Runs on a worker thread, so it takes an already-snapshotted sequence of
    ``(stage number, path)`` pairs rather than the live tracker: nothing mutable
    crosses the boundary in either direction. A stage whose result cannot be read
    is simply absent from the mapping, which is what makes the caller fall back
    to a plain "done" line for it.
    """
    excerpts: dict[int, str] = {}
    for stage_num, path_str in result_paths:
        try:
            text = safe_read_file(path_str).strip()
        except (OSError, PermissionError):
            continue
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("───"):
                excerpts[stage_num] = line[:120]
                break
    return excerpts


def _orchestration_stopped(slot: "_ChatSlot", tracker: OrchestrationTracker) -> bool:
    """True when the stage loop must not advance the plan any further.

    Two independent channels revoke a run, and they do not mean the same thing:

    * ``slot._stopping`` — session/ACP teardown. The slot itself is going away,
      so nothing on it may keep running.
    * ``tracker.stopped`` — the user revoked approval to keep ORCHESTRATING, via
      the plan Cancel control (``api_chat_plan_action``) or an orchestrator stop
      word. The slot stays alive and usable; only the plan ends.

    A plan cancel sets the second and deliberately not the first, so an
    advancement gate reading one flag observes only half the cancels. Every gate
    below therefore reads both. The inverse fix — having Cancel set
    ``slot._stopping`` — would hand a plan cancel the teardown semantics that
    flag carries for paths outside this loop, which is not what the user asked
    for by cancelling a plan.
    """
    return bool(slot._stopping) or bool(tracker.stopped)


def _is_plan_approval_entry(entry: dict) -> bool:
    """True when a queued entry is a plan-action Go approval.

    Matches ONLY the structural kind="plan_approval" tag (queue_append's
    classify-by-metadata contract). Deliberately NOT content: an untagged
    "go" in the queue is a plain user message (e.g. a linked Slack user's
    text) and dropping it is data loss (GPT CI finding, round 6). Nor is
    content matching needed for safety: a drained untagged entry dispatches
    through _run_chat as an ordinary turn — the queue drain never re-enters
    api_chat's typed-go branch, and _stage_loop's entry latch blocks any
    advancement on a cancelled plan regardless.
    """
    return entry.get("kind") == "plan_approval"


async def _exit_cancelled_plan(state: "DashboardState", slot: "_ChatSlot") -> None:
    """Terminal teardown for a ``_stage_loop`` entry whose plan is already cancelled.

    Mirrors the loop ``finally``'s exit sequence for the one path that cannot
    flow through it (the ``finally`` reads ``tracker.current_stage``, unbound
    when the latch check fires before tracker creation): tell the user WHY
    nothing ran, flush held notes under the same owner guard, hand off any real
    message the user queued while the Go was pending, and idle-close only when
    nothing started. Kept OUTSIDE ``_stage_loop`` so the loop retains exactly
    one flush seam in its ``finally`` (pinned structurally by
    test_gateway_appkit_endpoints); the queue-drain seam scan enforces this
    helper's own flush-above-drain ordering.
    """
    stop_msg = "🛑 This plan was already cancelled — ask for a new plan to continue."
    append_and_surface(state, slot, "assistant", stop_msg, "msg msg-a")
    # Same owner-guard as the loop finally: flush only when the registered task
    # is ours / absent / done, so a turn someone else owns keeps its notes.
    _own_task = slot.task
    if _own_task is None or _own_task is asyncio.current_task() or _own_task.done():
        try:
            slot.flush_deferred_notes()
        except Exception:
            logger.warning(
                "Stage loop: held-note delivery failed at cancelled exit for slot %s",
                slot.key,
                exc_info=True,
            )
        # Release our own registration so the handoff/idle checks below see the
        # slot as idle (in the loop finally, _run_chat's teardown has already
        # done this; no _run_chat ever ran on this path).
        slot.task = None
    _next_started = False
    # A queued plan approval is an approval of the very plan this exit is
    # refusing — the plan-action handler queues one (kind="plan_approval") when
    # the slot is busy (a pending loop counts as busy), so two Go clicks racing
    # a Cancel leave a second approval in the queue. Handing that entry to
    # _start_next_queued_turn would execute a revoked action through _run_chat
    # (GPT CI finding). Button approvals are classified by the structural kind
    # tag per queue_append's contract; a TYPED "go"/"go all" queued through
    # /api/chat carries no tag by definition, so those fall back to normalized
    # content (second GPT finding). Filtered here at drain time — not in the
    # cancel handler — so approvals queued AFTER the cancel but before this
    # pending loop ran are caught too.
    if slot._queue:
        slot._queue[:] = [e for e in slot._queue if not _is_plan_approval_entry(e)]
    if (
        not slot.running
        and not slot._last_turn_auth_required
        and state._slots.get(slot.key) is slot
        and slot._queue
        and not slot._stopping
    ):
        state.push_slots_update()
        _next_started = await _start_next_queued_turn(state, slot)
    if not _next_started and not slot.running:
        slot.append("done", "", "done")
        state.broadcast_ws("chat_done", {"slot": slot.key})
        slot.task = None
    state.push_slots_update()


async def _load_stage_budget(slot: "_ChatSlot", tracker: OrchestrationTracker) -> bool:
    """Apply the configured stage timeout to *tracker*. False to abandon the plan.

    The load stats and reads ``config.json`` plus any ``config.local.json``
    overlay, deep-merges them and runs the full schema validation, so it runs on
    a worker. Only the load crosses over: the value is applied and the slot read
    back on the loop, so no live orchestration state is handed to a thread. A
    failed load keeps the tracker's default budget, which is the same fallback
    the inline load had.

    Both cancellation channels can fire while that worker runs, and the check
    afterwards has to survive the fact that neither necessarily leaves state a
    plain re-read would see:

    * **Plan Cancel** stops the tracker. It is published before this is called
      precisely so that it does, which is why ``_orchestration_stopped`` is
      enough here and no separate record is kept.
    * **Dashboard Stop** has no ACP turn to cancel yet, so ``stop_turn`` answers
      "idle" and the handler releases ``_stop_state`` straight back to "idle" --
      re-reading ``slot._stopping`` afterwards would show an unstopped slot.
      ``slot._stop_generation`` counts stop INITIATIONS and is never rewound, so
      snapshotting it before the wait reports a Stop that fired AND resolved
      inside it. Same reading, and the same reason, as the poisoned-conversation
      canary in ``chat_runner``.

    Returning False rather than raising keeps the caller's exit on its normal
    path: the plan must not start, but the loop still owes the slot its cleanup.
    """
    _stop_generation = slot._stop_generation
    try:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        tracker.stage_timeout_seconds = cfg.orchestrator.stage_timeout_seconds
    except Exception:
        logger.debug(
            "Orchestrator config load failed for slot %s; keeping the default " "stage budget",
            slot.key,
            exc_info=True,
        )
    if slot._stop_generation != _stop_generation or _orchestration_stopped(slot, tracker):
        logger.info(
            "Stage loop for slot %s abandoned: a stop or plan cancel landed "
            "while the orchestrator config was loading",
            slot.key,
        )
        return False
    return True


async def _stage_loop(
    state: "DashboardState",
    slot: "_ChatSlot",
    auto_run: bool,
) -> None:
    """Python-controlled stage execution loop.

    Iterates through plan stages, calling ``_run_chat`` once per stage.
    Stage boundaries are enforced by Python code, not LLM prompts.
    """
    # Cancelled-plan latch (#6046): checked BEFORE the lazy tracker creation
    # below. A Cancel processed after the Go POST was accepted but before this
    # coroutine ran found no tracker to stop; without this check the loop would
    # build a fresh (unstopped) tracker and advance stage 1 against a revoked
    # approval. No await separates this check from the tracker assignment, so a
    # cancel can only interleave after the tracker exists — where tracker.stop()
    # and the gates below already observe it. The latch is cleared only when a
    # new plan is armed, so a later Go cannot resurrect a cancelled plan.
    if slot._plan_cancelled:
        logger.info(
            "Stage loop: plan already cancelled for slot %s; exiting without advancing",
            slot.key,
        )
        await _exit_cancelled_plan(state, slot)
        return

    tracker = slot._orch_tracker
    # Publish the tracker BEFORE the config load suspends below. That load is
    # this loop's first await, ahead of every stage gate, and a plan Cancel
    # landing in the window has to have something to stop: api_chat_plan_action
    # stops slot._orch_tracker, so against a None tracker it stops nothing while
    # still telling the user the plan was cancelled.
    #
    # Building it first is what lets `tracker.stopped` -- the canonical plan
    # cancel signal every advancement gate already reads through
    # `_orchestration_stopped` -- cover this window too, rather than a second
    # cancellation record kept alongside it that can drift from it.
    #
    # It starts on OrchestrationTracker's own default budget, the same 1800s
    # this loop fell back to when the load raised, and takes the configured
    # value below once that is known. Nothing reads the budget until a stage
    # records its first round, which cannot happen before the load returns.
    _bootstrapping = tracker is None
    if tracker is None:
        tracker = OrchestrationTracker()
        slot._orch_tracker = tracker

    total = slot._plan_stage_count
    titles = getattr(slot, "_stage_titles", [])

    # Determine starting stage (0-based index)
    start_idx = tracker.current_stage if tracker._stage_rounds else 0

    logger.info(
        "Stage loop start: slot=%s total=%d start_idx=%d auto_run=%s titles=%s",
        slot.key,
        total,
        start_idx,
        auto_run,
        titles,
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
        # Inside the try, so an abort here leaves through the same `finally` as
        # every other exit: the guard is cleared, a message the user queued
        # while this was loading is handed off, and the slot is closed out. A
        # bare `return` from the bootstrap would skip all of it and strand that
        # message behind a guard nothing clears.
        if _bootstrapping and not await _load_stage_budget(slot, tracker):
            return
        for stage_idx in range(start_idx, total):
            if _orchestration_stopped(slot, tracker):
                break

            stage_num = stage_idx + 1  # 1-based for display

            # Defensive clamp: never build or execute a stage beyond the CURRENT
            # plan size. `total` is captured once at range() creation; if the
            # live stage count ever shrank mid-run, continuing would emit a
            # phantom "Stage N of M" (N > M). Stop cleanly instead.
            if stage_idx >= slot._plan_stage_count:
                logger.warning(
                    "Stage loop clamp for slot %s: stage_idx=%d >= plan_stage_count=%d; stopping",
                    slot.key,
                    stage_idx,
                    slot._plan_stage_count,
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
            context = await _build_stage_context(slot, tracker, stage_idx)
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
                stage_num,
                total,
                len(context),
                len(slot.messages),
            )
            # NOT flushed here: a stage turn is automatic (`auto-go`), and a held
            # note is owed to the next USER turn, so feeding it to a stage would
            # spend it on a turn nobody asked for. The loop-exit flush below is
            # the delivery point -- it sits in this function's `finally`, where
            # `slot.task` is this loop's own task, so it fires on the completed,
            # paused and cancelled paths alike.
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
                    await _bounded_turn(
                        _run_chat(
                            state,
                            slot,
                            context,
                            _directive_user_origin=False,
                        ),
                        _turn_timeout,
                    )
                else:
                    await _run_chat(
                        state,
                        slot,
                        context,
                        _directive_user_origin=False,
                    )
            except (asyncio.TimeoutError, TimeoutError):
                # `_bounded_turn` raises builtin TimeoutError; on 3.10
                # asyncio.TimeoutError is a DIFFERENT class, so catch both (the
                # convention already used by _run_pending_synthesis).
                logger.error(
                    "Stage %d exceeded its %ds ceiling for slot %s",
                    stage_num,
                    tracker.stage_timeout_seconds,
                    slot.key,
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
                logger.exception(
                    "_run_chat failed during stage %d for slot %s", stage_num, slot.key
                )
                _err_msg = (
                    f"❌ Stage {stage_num} failed due to an internal error. Auto-run stopped."
                )
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

            if _orchestration_stopped(slot, tracker):
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
                    stage_num,
                    slot.key,
                )
                _fc_msg = (
                    f"⚠️ Stage {stage_num}: subagent manager unavailable. " "Auto-run stopped."
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
                        stage_num,
                        slot.key,
                    )
                    _fc_msg = f"⚠️ Stage {stage_num}: subagent check failed. " "Auto-run stopped."
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
                    and not _orchestration_stopped(slot, tracker)
                ):
                    _sa_rounds += 1
                    await asyncio.sleep(2)
                    _pending = state.subagents.running_agents_for(session_key)
                    # Update status every 10 polls (~20s)
                    if _pending and _sa_rounds % 10 == 0:
                        state.broadcast_ws(
                            "chat_status",
                            {
                                "slot": slot.key,
                                "status": f"Waiting for {len(_pending)} subagent(s)...",
                            },
                        )
                    if _pending is None:
                        logger.warning(
                            "Stage %d: running_agents_for returned None during"
                            " polling for slot %s — stopping auto-run",
                            stage_num,
                            slot.key,
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
                    stage_num,
                    _wait_secs,
                    _sa_rounds,
                    _sa_max_rounds,
                    slot.key,
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

            if _orchestration_stopped(slot, tracker):
                break

            # Capture result to disk
            try:
                result_path = _capture_stage_result(slot, stage_num)
                tracker.record_stage_result(stage_num, result_path)
            except OSError:
                logger.warning(
                    "Failed to capture stage %d result to disk", stage_num, exc_info=True
                )

            # Gate: if not auto_run, wait for user approval
            if not auto_run:
                # Emit completion message — user must click Go for next stage
                if stage_idx + 1 < total:
                    next_title = titles[stage_idx + 1] if stage_idx + 1 < len(titles) else ""
                    next_label = (
                        f"Stage {stage_idx + 2}: {next_title}"
                        if next_title
                        else f"Stage {stage_idx + 2}"
                    )
                    done_msg = (
                        f"✅ Stage {stage_num} complete. Click **Go** to proceed to {next_label}."
                        "\n\n[OPTION: Go | Go All | Cancel]"
                    )
                    done_msg, _ = redact_exfiltration_urls(done_msg)
                    done_msg, _ = redact_credentials(done_msg)
                    append_and_surface(state, slot, "assistant", done_msg, "msg msg-a")
                    _paused = True
                    return  # User's next "Go" click will re-enter _stage_loop
        else:
            # for loop completed without break — all stages done
            if not slot._stopping and start_idx < total:
                slot._auto_run = False
                # Snapshot the result paths on the loop thread — `_stage_results`
                # is live orchestration state the loop mutates — then read the
                # files on a worker: one read per completed stage, all of them
                # landing at once on the gateway's single event loop.
                _captured: list[tuple[int, str]] = []
                for s_idx in range(total):
                    _path = tracker._stage_results.get(s_idx + 1)
                    if _path:
                        _captured.append((s_idx + 1, _path))
                # Nothing captured means nothing to read: skip the worker hop.
                excerpts: dict[int, str] = {}
                if _captured:
                    excerpts = await asyncio.to_thread(_completion_excerpts, tuple(_captured))
                # Build execution summary from captured stage results
                summary_lines = [f"✅ All {total} stages complete."]
                for s_idx in range(total):
                    s_num = s_idx + 1
                    s_title = titles[s_idx] if s_idx < len(titles) else ""
                    excerpt = excerpts.get(s_num, "")
                    label = f"Stage {s_num}: {s_title}" if s_title else f"Stage {s_num}"
                    if excerpt:
                        summary_lines.append(f"  {label} — {excerpt}")
                    else:
                        summary_lines.append(f"  {label} — done")
                done_msg = "\n".join(summary_lines)
                done_msg, _ = redact_exfiltration_urls(done_msg)
                done_msg, _ = redact_credentials(done_msg)
                append_and_surface(state, slot, "assistant", done_msg, "msg msg-a")
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
            slot.key,
            tracker.current_stage,
            total,
            slot._stopping,
            slot._auto_run,
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
        # `slot.running` is asking whether a turn a stage STARTED is still
        # holding the slot: `_run_chat` publishes its own task on `slot.task`
        # and clears it when the turn ends, and this loop must defer to one that
        # is still live. It is not asking about this loop -- yet when no stage
        # turn ever ran, `slot.task` is still this very task, which is alive by
        # definition here, so the raw read reports a turn that does not exist.
        # That silently skipped BOTH the handoff and the idle close on exactly
        # the paths with nothing else to perform them: an abandoned bootstrap,
        # and a plan with no stages.
        _own_task = asyncio.current_task()
        _turn_live = slot.running and slot.task is not _own_task
        _next_started = False
        # Before _start_next_queued_turn, not after: a held note's context half
        # drains into that successor, so flushing later would let the note shape
        # a turn its visible line appears below. Skipped while a turn runs, since
        # that turn drains AFTER its task is assigned and would consume a note
        # written after it began; it flushes at its own completion instead.
        # ``slot.running`` cannot express that: inside this finally it names THIS
        # loop's own task, so defer only to a live task that is someone else's.
        _note_owner = slot.task
        if _note_owner is None or _note_owner is asyncio.current_task() or _note_owner.done():
            try:
                slot.flush_deferred_notes()
            except Exception:
                # Worst-placed of the flush seams: this is a ``finally``, so a raise
                # here both skips the rest of it -- the queued-work handoff, the
                # done row, chat_done, and clearing slot.task, leaving the slot
                # wedged with its spinner up -- AND replaces any exception the loop
                # was already unwinding, hiding the original failure. Held notes are
                # delivered by the next seam instead.
                logger.warning(
                    "Stage loop: held-note delivery failed at exit for slot %s",
                    slot.key,
                    exc_info=True,
                )
        # Same revoked-approval filter as _exit_cancelled_plan, at this drain:
        # a Go queued WHILE the plan ran, followed by a mid-loop cancel, would
        # otherwise drain an approval entry into _run_chat here — the
        # surviving residual both advisory lanes flagged. Only when the
        # plan is revoked; a paused plan's queued approval is still live.
        if slot._plan_cancelled and slot._queue:
            slot._queue[:] = [e for e in slot._queue if not _is_plan_approval_entry(e)]
        if (
            not _cancelled
            and not _turn_live
            and not slot._last_turn_auth_required
            and state._slots.get(slot.key) is slot
            and slot._queue
            and not slot._stopping
        ):
            state.push_slots_update()
            _next_started = await _start_next_queued_turn(state, slot)
        if not _next_started and not _turn_live:
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
        # Idempotence read taken BEFORE the latch is set. The latch alone is the
        # test: it is cleared only when a new plan is armed, so "latch set" means
        # this plan is already revoked. Deliberately NOT conjoined with tracker
        # state — the Slack gateway lazily creates a fresh UNSTOPPED tracker on
        # an orchestrator slot when a subagent result lands, so a result arriving
        # between two Cancels would make a tracker-based read report the plan as
        # live again and write a duplicate '🛑 Plan cancelled.' row (Opus review
        # finding). The unconditional tracker.stop() below still stops such a
        # gateway-created tracker on every cancel POST.
        already_cancelled = slot._plan_cancelled
        # Set unconditionally — NOT only when a tracker exists. The tracker is
        # created lazily inside _stage_loop, so a Cancel processed in the window
        # between a Go POST being accepted and its _stage_loop coroutine running
        # would otherwise no-op entirely and the plan would advance while the
        # transcript says cancelled (#6046). _stage_loop checks this latch
        # before creating a tracker.
        slot._plan_cancelled = True
        if tracker and not tracker.stopped:
            tracker.stop()
        slot._auto_run = False
        if state.subagents:
            session_key = f"dashboard:{slot.key}"
            for a in state.subagents.running_agents_for(session_key) or []:
                t = state.subagents._tasks.get(a["id"])
                if t and not t.done():
                    t.cancel()
        if not already_cancelled:
            stop_msg = "🛑 Plan cancelled."
            append_and_surface(state, slot, "assistant", stop_msg, "msg msg-a")
            state.broadcast_ws("chat_done", {"slot": slot.key})
        return web.json_response({"ok": True, "cancelled": True})

    # Go or Go All — use Python-controlled stage loop
    if slot.running:
        # circular import: session_control imports this package's modules at module level.
        from kiro_crew.dashboard.session_control import containment_meta

        # Provenance follows the CALLER — the same request-identity split as
        # api_chat and the manual continue. A human clicking Go on their own
        # busy session must not lose the approval if they link the session
        # before the drain; an app relaying a plan action never gains the
        # authenticated-human flag.
        # kind is a structural origin tag, not bare content: _exit_cancelled_plan
        # drops revoked approvals by this tag, and queue_append's contract names
        # metadata (never content equality) as the classification mechanism.
        slot.queue_append(
            "Go",
            kind="plan_approval",
            meta=containment_meta(state, slot),
            directive_user_origin=not bool(request.get("app", "")),
        )
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
    append_and_surface(state, slot, "user", _label, "msg msg-u", broadcast_user=True)
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
