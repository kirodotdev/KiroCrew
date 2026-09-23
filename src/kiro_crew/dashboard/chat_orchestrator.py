"""Orchestrator stage loop — Python-controlled plan execution."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.config.sections import OrchestratorConfig
from kiro_crew.context_management import MAX_STAGE_ROUNDS, OrchestrationTracker
from kiro_crew.dashboard.chat_runner import (
    _deliver_cross_surface_reply,
    _deliver_linked_slack_message,
    _run_chat,
    _start_next_queued_turn,
)
from kiro_crew.dashboard.chat_utils import (
    _MANUAL_RESUME_MSG,
    STAGE_DELIVERY_KINDS,
    SYNTHETIC_RECOVERY_KIND,
    chat_done_payload,
    effective_session_key,
    owned_stage_delivery_entry,
)
from kiro_crew.dashboard.state import (
    DashboardState,
    _ChatSlot,
    _log_task_exception,
    append_and_surface,
    stage_boundary_for,
)
from kiro_crew.dashboard.turn_dispatch import _bounded_turn
from kiro_crew.hooks import safe_read_file
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import SecurityEvent, sel
from kiro_crew.subagent import SubagentReportDeliveryError

logger = logging.getLogger(__name__)

# ── Previous-stage context budget ──
# How many of the most recent prior stages are inlined in FULL. Inlining every
# earlier result at up to 2000 bytes each makes the context grow linearly in the
# stage index (~18 KB by stage 10) and re-reads all of those files at every later
# stage boundary. An older stage therefore contributes its path plus one headline,
# which is what the model needs to decide whether to open it with its file tools;
# the path is emitted for every stage either way.
_PREV_FULL_STAGES = 3
# Bytes read from an older stage's file to find that headline.
_PREV_HEADLINE_BYTES = 512

# ── Subagent wave wait ──
# Coarse fallback for the event-driven wave wait. The completion event is a
# PULSE, and a run can reach a terminal state on a path that never announces
# (shutdown's ``cancel_all``), so a wait that woke only on the event could hold a
# stage for its whole budget. Five seconds keeps a lost pulse cheap and still
# cuts the O(n) ``running_agents_for`` scan rate to a quarter of the 2s tick it
# replaces.
_SA_FALLBACK_SECS = 5.0
# Ceiling on the whole wave wait when the stage budget does not imply a smaller
# one: the same 15 minutes the old 450-round cap expressed at 2s per round.
_SA_MAX_WAIT_SECS = 900
# How often the "waiting for N subagent(s)" status line is re-broadcast.
_SA_STATUS_EVERY_SECS = 20.0


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


def _result_headline(p: Path) -> str:
    """The first non-empty, non-separator line of a stage result file.

    Reads only the first :data:`_PREV_HEADLINE_BYTES`, because this is what an
    OLDER stage contributes to a later stage's context: the whole point of
    summarising it is not to read the file.
    """
    try:
        with open(p, "rb") as f:
            raw = f.read(_PREV_HEADLINE_BYTES)
    except (OSError, ValueError):
        return ""
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("─"):
            return line[:120]
    return ""


def _read_previous_results(recorded: list[tuple[int, str]]) -> str:
    """Read each recorded stage result and compact it. Blocking.

    Split out so the reads can be handed to a worker thread as a unit. It takes
    an already-materialised ``(stage_num, path)`` list rather than the tracker,
    so nothing the event loop mutates is reachable from the worker.

    Only the last :data:`_PREV_FULL_STAGES` entries are inlined in full; every
    earlier one contributes its headline and its path. ``recorded`` is ordered
    oldest-first by its caller, which is what makes "the last three" the three
    most recent.
    """
    _max_per_stage = 2000
    parts: list[str] = []
    _full_from = max(0, len(recorded) - _PREV_FULL_STAGES)
    for _pos, (stage_num, path_str) in enumerate(recorded):
        p = Path(path_str)
        content = ""
        if _pos < _full_from:
            # An older stage: headline plus path, never the body.
            header = f"### Stage {stage_num}"
            headline = ""
            if p.exists() and not is_sensitive_path(str(p)):
                headline = _result_headline(p)
            if headline:
                parts.append(f"{header}\n{headline}\nFull result: `{path_str}`")
            else:
                parts.append(f"{header}\nFull result: `{path_str}`")
            continue
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


def _collect_stage_result_parts(slot: "_ChatSlot") -> tuple[str, ...]:
    """Snapshot the assistant text this stage produced, newest separator backwards.

    Runs on the event loop because it walks ``slot.messages``, which the loop
    mutates. Returns an immutable tuple of RAW text so the write half can be
    handed to a worker without any live slot state crossing the boundary -- the
    same split as ``_previous_result_paths`` / ``_read_previous_results``.
    """
    result_parts: list[str] = []
    for m in reversed(slot.messages):
        role = m.get("role", "")
        cls = m.get("cls", "")
        if isinstance(cls, str) and "stage-sep" in cls:
            break  # hit the separator for this stage
        if role == "assistant":
            result_parts.append(m.get("content", ""))
    result_parts.reverse()
    return tuple(result_parts)


def _write_stage_result(
    slot_key: str,
    stage_num: int,
    raw_parts: tuple[str, ...],
) -> str:
    """Redact *raw_parts* and write the stage result file. Returns its path.

    Blocking: ``mkdir`` plus a file write, which is why the caller hands this to
    a worker. It takes only strings, so nothing the event loop mutates is
    reachable from that worker.
    """
    parts: list[str] = []
    for text in raw_parts:
        # Defence in depth before this reaches disk. Both upstream sources are
        # already clean — live turns via chat_runner._flush_segment, restored
        # turns via the load-time content pass — but this writes a NEW file
        # outside the history log's own redaction, so it does not depend on
        # that. Redaction is idempotent, so the common case is a no-op.
        text, _ = redact_exfiltration_urls(text)
        text, _ = redact_credentials(text)
        parts.append(text)
    result_text = "\n\n".join(parts)

    session_dir = config_dir() / "sessions" / slot_key
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


async def _deliver_halt_notice_to_channels(
    state: "DashboardState",
    slot: "_ChatSlot",
    message: str,
) -> None:
    """Mirror a stage halt to every linked channel surface, best-effort."""
    session_key = effective_session_key(slot)
    await _deliver_linked_slack_message(
        state,
        slot,
        state.sessions,
        session_key,
        message,
    )
    await _deliver_cross_surface_reply(state, session_key, message)


def _halt_plan(
    state: "DashboardState",
    slot: "_ChatSlot",
    message: str,
    *,
    event_type: str,
    operation: str,
    stage_num: int,
) -> None:
    """Stop auto-run, tell the user why, and audit it.

    Not redacted: every caller builds *message* from a stage number and a
    humanized duration, never from model-authored text, which is the same
    footing as the pre-existing stage-timeout notice beside it.
    """
    slot._auto_run = False
    append_and_surface(state, slot, "assistant", message, "msg msg-a")
    delivery = asyncio.create_task(_deliver_halt_notice_to_channels(state, slot, message))
    state._background_tasks.add(delivery)
    delivery.add_done_callback(state._background_tasks.discard)
    delivery.add_done_callback(_log_task_exception)
    sel().log(
        SecurityEvent(
            event_id=uuid.uuid4().hex,
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            event_type=event_type,
            caller_identity=f"dashboard:{slot.key}",
            agent=getattr(slot, "agent", ""),
            source="dashboard",
            operation=operation,
            outcome="stopped",
            resources=f"slot={slot.key},stage={stage_num}",
        )
    )


def _round_cap_message(
    tracker: OrchestrationTracker,
    stage_num: int,
) -> str | None:
    """The halt notice when *stage_num* has spent its round budget, else ``None``.

    ``MAX_STAGE_ROUNDS`` enforces the "max 3 rounds per stage" the orchestrator
    prompt promises: recording a round without consulting it here would leave that
    promise unenforced. Rounds are recorded in ONE place -- the
    subagent-completion handler in the Slack gateway, once per completed wave on
    this same tracker -- which is how a dashboard stage reaches the cap at all.
    The loop itself enters a stage through ``start_stage``, which spends no round,
    so all three the prompt promises are available to actual spawn waves.

    ``MAX_STAGE_ESCALATIONS`` is deliberately NOT checked here, and that is a
    reachability fact rather than a preference. An escalation is only recorded by
    ``reset_after_guidance``, which zeroes that stage's rounds while KEEPING its
    key -- so ``current_stage`` (the highest key) does not move, the loop's next
    entry starts at the stage after it, and an escalated stage is never re-entered.
    Nothing on this path can therefore observe ``is_force_failed``. It stays
    enforced in the Slack gateway, where the tracker is not driven by a stage loop
    and the check IS reachable.
    """
    if not tracker.round_limit_reached(stage_num):
        return None
    rounds = tracker.round_count(stage_num)
    return (
        f"⚠️ Stage {stage_num} has used all {MAX_STAGE_ROUNDS} of its spawn rounds "
        f"({rounds}). Auto-run stopped — send guidance to continue."
    )


def _orchestration_stopped(
    slot: "_ChatSlot",
    tracker: OrchestrationTracker,
    *,
    stop_generation: int | None = None,
) -> bool:
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
    ``stop_generation`` is the controller's entry snapshot. A soft Stop can
    return ``_stop_state`` to idle before cancellation unwinds, so a changed
    generation remains the durable revocation signal.
    """
    return (
        bool(slot._stopping)
        or bool(tracker.stopped)
        or (stop_generation is not None and slot._stop_generation != stop_generation)
    )


def _is_plan_approval_entry(entry: dict) -> bool:
    """True when a queued entry is a plan-action Go approval.

    Matches ONLY the structural kind="plan_approval" tag (queue_append's
    classify-by-metadata contract). Deliberately NOT content: an untagged
    "go" in the queue is a plain user message (e.g. a linked Slack user's
    text) and dropping it is data loss. Nor is
    content matching needed for safety: a drained untagged entry dispatches
    through _run_chat as an ordinary turn — the queue drain never re-enters
    api_chat's typed-go branch, and _stage_loop's entry latch blocks any
    advancement on a cancelled plan regardless.
    """
    return entry.get("kind") == "plan_approval"


def _discard_cancelled_plan_queue_entries(slot: "_ChatSlot") -> list[str]:
    """Drop only queue work owned by the revoked plan boundary."""
    boundary = stage_boundary_for(slot)
    retry_id = boundary.retry_queue_id
    owner = boundary.owner or (boundary.generation if slot._plan_cancelled else None)
    removed: list[dict] = []
    kept: list[dict] = []
    for entry in slot._queue:
        if (
            entry.get("id") == retry_id
            or _is_plan_approval_entry(entry)
            or boundary.owns_entry(entry, owner=owner)
        ):
            removed.append(entry)
        else:
            kept.append(entry)
    slot._queue[:] = kept
    return [
        str(entry.get("content", ""))
        for entry in removed
        if entry.get("kind") in STAGE_DELIVERY_KINDS
    ]


async def _settle_discarded_stage_deliveries(
    state: "DashboardState",
    slot: "_ChatSlot",
    contents: list[str],
) -> None:
    """Settle queued completion and boundary report debt through one seam."""
    manager = getattr(state, "subagents", None)
    if manager is None:
        return
    owed = slot.take_pending_subagent_deliveries(contents)
    if owed:
        try:
            settlement = manager.settle_queued_delivery(owed)
            if asyncio.iscoroutine(settlement):
                await settlement
        except Exception:
            logger.warning(
                "Could not settle discarded stage deliveries for slot %s",
                slot.key,
                exc_info=True,
            )
    boundary = stage_boundary_for(slot)
    owner = boundary.owner or boundary.generation
    parents = tuple(boundary.parent_session_keys) or (effective_session_key(slot),)
    discard_failures = getattr(manager, "discard_report_failures", None)
    if owner and callable(discard_failures):
        for parent in parents:
            discard_failures(parent, owner)


def _cancel_release_owns_handoff(
    slot: "_ChatSlot",
    *,
    controller_active: bool,
) -> bool:
    """Whether release, rather than a stage controller, owns handoff (S5)."""
    controller = slot._stage_controller_task
    turn = slot.task
    return not (
        controller_active
        or slot._in_stage_execution
        or (controller is not None and not controller.done())
        or (turn is not None and not turn.done())
    )


async def _release_cancelled_plan_boundary(
    state: "DashboardState",
    slot: "_ChatSlot",
    *,
    terminal_message: str = "",
    controller_active: bool = False,
) -> None:
    """Settle cancellation, publish its stop, then release admission (S5)."""
    release_generation = stage_boundary_for(slot).generation
    async with slot._lock:
        boundary = stage_boundary_for(slot)
        if not slot._plan_cancelled or boundary.generation != release_generation:
            return
        # A scope-cap refusal has no retained manager entry to block durable
        # dispatch after this boundary disappears. Keep the live boundary armed
        # until a later Cancel atomically reserves every captured parent.
        if boundary.cancellation_hold_refused:
            return
        removed_contents = _discard_cancelled_plan_queue_entries(slot)
        await _settle_discarded_stage_deliveries(state, slot, removed_contents)
        if terminal_message:
            append_and_surface(state, slot, "assistant", terminal_message, "msg msg-a")
            state.broadcast_ws("chat_done", await chat_done_payload(state, slot))
        boundary.clear()
        if (
            _cancel_release_owns_handoff(
                slot,
                controller_active=controller_active,
            )
            and not slot._last_turn_auth_required
            and state._slots.get(slot.key) is slot
            and slot._queue
            and not slot._stopping
        ):
            state.push_slots_update()
            await _start_next_queued_turn(state, slot)


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
    # _start_next_queued_turn would execute a revoked action through _run_chat.
    # Filter at drain time so approvals queued after Cancel but before this
    # pending loop ran are caught too. Releasing through one seam also settles
    # retention debt for every owned completion row it removes.
    await _release_cancelled_plan_boundary(
        state,
        slot,
        controller_active=True,
    )

    def _turn_running() -> bool:
        task = slot.task
        return task is not None and not task.done()

    if (
        not _turn_running()
        and not slot._last_turn_auth_required
        and state._slots.get(slot.key) is slot
        and slot._queue
        and not slot._stopping
    ):
        state.push_slots_update()
        _next_started = await _start_next_queued_turn(state, slot)
    if not _next_started and not _turn_running():
        slot.append("done", "", "done")
        state.broadcast_ws("chat_done", await chat_done_payload(state, slot))
        slot.task = None
    state.push_slots_update()


async def _load_plan_budgets(slot: "_ChatSlot", tracker: OrchestrationTracker) -> bool:
    """Apply the configured stage and whole-plan budgets. False to abandon the plan.

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
        tracker.max_plan_duration_seconds = cfg.orchestrator.max_plan_duration_seconds
    except Exception:
        # Both budgets are set to the dataclass defaults, not left as they are.
        # The tracker constructs with ``_plan_timeout = 0``, and 0 means DISABLED
        # everywhere it is read -- so leaving them alone on an unreadable or invalid
        # config.json would remove the whole-plan ceiling entirely while the stage
        # budget quietly fell back to its own default. A failed load lands on exactly
        # the budgets a default config would have produced.
        tracker.stage_timeout_seconds = OrchestratorConfig.stage_timeout_seconds
        tracker.max_plan_duration_seconds = OrchestratorConfig.max_plan_duration_seconds
        logger.debug(
            "Orchestrator config load failed for slot %s; falling back to the "
            "default stage and plan budgets",
            slot.key,
            exc_info=True,
        )
    # Recorded whether or not the load raised: the fallback budgets ARE the
    # documented outcome of a failed load, and leaving the tracker asking for one
    # would re-attempt a bad config read at every later stage-loop entry.
    tracker.mark_budgets_loaded()
    if _orchestration_stopped(
        slot,
        tracker,
        stop_generation=_stop_generation,
    ):
        logger.info(
            "Stage loop for slot %s abandoned: a stop or plan cancel landed "
            "while the orchestrator config was loading",
            slot.key,
        )
        return False
    return True


def _exact_stage_delivery_retry(slot: "_ChatSlot") -> dict | None:
    retry_id = stage_boundary_for(slot).retry_queue_id
    if not retry_id:
        return None
    return next(
        (
            entry
            for entry in slot._queue
            if entry.get("id") == retry_id and entry.get("kind") in STAGE_DELIVERY_KINDS
        ),
        None,
    )


def _has_exact_stage_delivery_retry(slot: "_ChatSlot") -> bool:
    return _exact_stage_delivery_retry(slot) is not None


def _queue_consumed_stage_resume(
    state: "DashboardState",
    slot: "_ChatSlot",
    *,
    directive_user_origin: bool,
) -> bool:
    """Queue the continuation owed by consumed interrupted stage work (S2)."""
    if (
        stage_boundary_for(slot).stage is None
        or not stage_boundary_for(slot).consumed
        or not (slot._last_turn_auth_required or stage_boundary_for(slot).continuation_required)
    ):
        return False

    # circular import: session_control imports dashboard modules at module level.
    from kiro_crew.dashboard.session_control import containment_meta

    if _exact_stage_delivery_retry(slot) is None:
        stage_boundary_for(slot).retry_queue_id = slot.queue_insert(
            0,
            _MANUAL_RESUME_MSG,
            kind=SYNTHETIC_RECOVERY_KIND,
            meta=stage_boundary_for(slot).tag_meta(containment_meta(state, slot)),
            directive_user_origin=directive_user_origin,
        )
    # Queueing and prompt consumption are not successful recovery. Keep both the
    # retry identity and continuation obligation until settlement finishes and
    # captures the pending stage. A hard Stop may discard the queued row; the
    # stale id then fails _has_exact_stage_delivery_retry and the next Go queues
    # it again.
    # Clear only the auth hold before the controller starts so
    # _settle_stage_delivery drains exactly that turn. The pending boundary and
    # continuation obligation remain until the turn finishes successfully.
    slot._last_turn_auth_required = False
    state.push_slots_update()
    return True


def _stage_parent_session_keys(slot: "_ChatSlot") -> tuple[str, ...]:
    """Every immutable parent key captured by a turn in the pending stage."""
    keys = tuple(sorted(stage_boundary_for(slot).parent_session_keys))
    return keys or (effective_session_key(slot),)


async def _wait_for_stage_pulse(events: tuple[asyncio.Event, ...], timeout: float) -> None:
    """Wait for any completion pulse, or only the coarse fallback timeout."""
    if timeout <= 0:
        return
    waiters = [asyncio.create_task(event.wait()) for event in events]
    if not waiters:
        waiters.append(asyncio.create_task(asyncio.Event().wait()))
    try:
        await asyncio.wait(
            waiters,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for waiter in waiters:
            if not waiter.done():
                waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


def _capture_stage_cancellation_scope(
    slot: "_ChatSlot",
) -> tuple[str, tuple[str, ...], int] | None:
    """Freeze the exact child scope before controller teardown can clear it."""
    boundary = stage_boundary_for(slot)
    owner = boundary.owner
    if owner is None:
        return None
    parent_keys = set(boundary.parent_session_keys)
    parent_keys.add(f"dashboard:{slot.key}")
    return owner, tuple(sorted(parent_keys)), boundary.stage or 0


def _reserve_stage_cancellation_scopes(
    state: "DashboardState",
    scope: tuple[str, tuple[str, ...], int] | None,
) -> str:
    """Reserve every captured parent before controller teardown can clear it."""
    manager = state.subagents
    if manager is None or scope is None:
        return ""
    owner, parent_keys, _stage_num = scope
    reserve = getattr(manager, "reserve_boundary_cancellation_scopes", None)
    if not callable(reserve):
        return ""
    reservation = reserve(parent_keys, owner)
    return reservation if isinstance(reservation, str) else ""


async def _cancel_stage_subagents(
    state: "DashboardState",
    slot: "_ChatSlot",
    *,
    scope: tuple[str, tuple[str, ...], int] | None,
    reservation_reason: str | None = None,
) -> bool:
    """Cancel captured stage children; false keeps an unretained scope closed."""
    manager = state.subagents
    if manager is None or scope is None:
        return True
    owner, parent_keys, stage_num = scope
    pending: list[str] = []
    overflow: list[str] = []
    pending_reason = getattr(manager, "boundary_cancellation_pending_reason", None)
    refused = getattr(manager, "boundary_cancellation_refused", None)
    if reservation_reason is None:
        reservation_reason = _reserve_stage_cancellation_scopes(state, scope)
    if reservation_reason:
        overflow.append(reservation_reason)
    for parent_key in parent_keys:
        if reservation_reason:
            cancellation = manager.cancel_for_boundary(
                parent_key,
                owner,
                retain_scope=False,
            )
        else:
            cancellation = manager.cancel_for_boundary(parent_key, owner)
        if asyncio.iscoroutine(cancellation):
            await cancellation
        reason = ""
        if callable(pending_reason):
            current = pending_reason(parent_key, owner)
            if isinstance(current, str) and current:
                reason = current
                pending.append(parent_key)
        if callable(refused) and refused(parent_key, owner) is True:
            overflow.append(reason)
    if pending or overflow:
        if overflow:
            latest = next((reason for reason in reversed(overflow) if reason), "scope cap reached")
            notice = (
                "⚠️ Plan cancellation could not enter the bounded durable task queue "
                f"hold ({latest}). Its stage remains blocked. Retry Cancel after "
                "earlier durable cancellations settle."
            )
        else:
            notice = (
                "⚠️ Plan cancellation could not finish writing to the durable task "
                "queue. Its queued stage work remains blocked, and cancellation "
                "will retry automatically."
            )
        _halt_plan(
            state,
            slot,
            notice,
            event_type="auto_run_cancel_settlement_failed",
            operation="stage_cancel_store_unavailable",
            stage_num=stage_num,
        )
    return not overflow


def _running_stage_agents(manager: object, slot: "_ChatSlot") -> list[dict] | None:
    """Combine live agents across all parent keys used by the pending stage."""
    pending: list[dict] = []
    for parent_key in _stage_parent_session_keys(slot):
        current = manager.running_agents_for(parent_key)  # type: ignore[attr-defined]
        if current is None:
            return None
        pending.extend(current)
    return pending


async def _queued_stage_work_pending(manager: object, slot: "_ChatSlot") -> bool | None:
    """Whether accepted-but-unregistered stage work still exists."""
    for parent_key in _stage_parent_session_keys(slot):
        try:
            if await manager.has_pending_work_for_async(parent_key):  # type: ignore[attr-defined]
                return True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Stage pending-work probe failed for parent %s",
                parent_key,
            )
            return None
    return False


async def _settle_stage_delivery(
    state: "DashboardState",
    slot: "_ChatSlot",
    tracker: OrchestrationTracker,
    stage_num: int,
    *,
    require_registered_slot: bool = False,
    stop_generation: int | None = None,
) -> bool:
    """Wait until parent agents, reports, and completion turns are stably quiescent."""
    manager = state.subagents
    if manager is None:
        _halt_plan(
            state,
            slot,
            f"⚠️ Stage {stage_num}: subagent manager unavailable. Auto-run stopped.",
            event_type="auto_run_subagent_check_failed",
            operation="subagent_manager_missing",
            stage_num=stage_num,
        )
        return False

    max_wait = (
        min(tracker.stage_timeout_seconds // 2, _SA_MAX_WAIT_SECS)
        if tracker.stage_timeout_seconds
        else float(_SA_MAX_WAIT_SECS)
    )
    wait_started = time.monotonic()
    last_status_at: float | None = None
    clean_passes = 0
    seen_parent_keys: frozenset[str] = frozenset()
    attempted_deliveries: set[tuple[str, str]] = set()
    own_task = asyncio.current_task()

    def _delivery_failed(reason: str = "") -> bool:
        detail = f" {reason}." if reason else ""
        _halt_plan(
            state,
            slot,
            f"⚠️ Stage {stage_num} finished its background work, but its "
            f"completion event could not be processed.{detail} Auto-run paused "
            "before the next stage. Resolve the session error, then send Go to resume.",
            event_type="auto_run_stage_error",
            operation="stage_completion_delivery_failed",
            stage_num=stage_num,
        )
        return False

    while not _orchestration_stopped(
        slot,
        tracker,
        stop_generation=stop_generation,
    ):
        if require_registered_slot and state._slots.get(slot.key) is not slot:
            return False
        parent_keys = _stage_parent_session_keys(slot)
        current_parent_keys = frozenset(parent_keys)
        if current_parent_keys != seen_parent_keys:
            seen_parent_keys = current_parent_keys
            clean_passes = 0
        if slot._last_turn_auth_required:
            return _delivery_failed()

        # The first clean pass establishes that the live roster is empty. The second
        # stable pass still rechecks durable queued work, reports, delivery turns,
        # owned queue entries and boundary counters, but does not repeat that O(n)
        # roster scan: the stage turn has ended, so those are the only paths that can
        # publish later work for this boundary. A changed parent-key set resets
        # ``clean_passes`` above and therefore scans the new scope.
        pending = [] if clean_passes else _running_stage_agents(manager, slot)
        queued_pending = await _queued_stage_work_pending(manager, slot)
        if pending is None or queued_pending is None:
            _halt_plan(
                state,
                slot,
                f"⚠️ Stage {stage_num}: subagent check failed. Auto-run stopped.",
                event_type="auto_run_subagent_check_failed",
                operation="subagent_pending_probe_failed",
                stage_num=stage_num,
            )
            return False
        if pending or queued_pending:
            clean_passes = 0
            now = time.monotonic()
            elapsed = now - wait_started
            if elapsed >= max_wait:
                _halt_plan(
                    state,
                    slot,
                    f"⚠️ Stage {stage_num}: subagent wait exhausted after "
                    f"{int(elapsed) // 60} minutes. Auto-run stopped — some "
                    "results may be incomplete.",
                    event_type="auto_run_subagent_timeout",
                    operation="subagent_wait_exhausted",
                    stage_num=stage_num,
                )
                return False
            if last_status_at is None or now - last_status_at >= _SA_STATUS_EVERY_SECS:
                last_status_at = now
                status = (
                    f"Waiting for {len(pending)} subagent(s)..."
                    if pending
                    else "Waiting for queued subagent work..."
                )
                state.broadcast_ws(
                    "chat_status",
                    {"slot": slot.key, "status": status},
                )

            completion_event = getattr(manager, "completion_event", None)
            release_completion_event = getattr(manager, "release_completion_event", None)
            registered: list[tuple[str, asyncio.Event]] = []
            if callable(completion_event):
                for parent_key in parent_keys:
                    event = completion_event(parent_key)
                    if isinstance(event, asyncio.Event):
                        registered.append((parent_key, event))
            for _parent_key, event in registered:
                event.clear()
            try:
                # Clear before this re-read: a completion landing between the
                # probe and wait sets its pulse and cannot be lost.
                pending = _running_stage_agents(manager, slot)
                queued_pending = await _queued_stage_work_pending(manager, slot)
                if pending is None or queued_pending is None:
                    _halt_plan(
                        state,
                        slot,
                        f"⚠️ Stage {stage_num}: subagent check failed. Auto-run stopped.",
                        event_type="auto_run_subagent_check_failed",
                        operation="subagent_pending_probe_failed",
                        stage_num=stage_num,
                    )
                    return False
                if not pending and not queued_pending:
                    continue
                budget = max_wait - (time.monotonic() - wait_started)
                await _wait_for_stage_pulse(
                    tuple(event for _parent_key, event in registered),
                    min(_SA_FALLBACK_SECS, max(0.0, budget)),
                )
            finally:
                if callable(release_completion_event):
                    for parent_key, _event in registered:
                        release_completion_event(parent_key)
            continue

        try:
            reports_observed = False
            for parent_key in parent_keys:
                reports_observed = (
                    bool(
                        await manager.wait_for_parent_reports(  # type: ignore[attr-defined]
                            parent_key,
                            stage_boundary_for(slot).owner or "",
                        )
                    )
                    or reports_observed
                )
            if reports_observed:
                clean_passes = 0
                continue
        except asyncio.CancelledError:
            raise
        except SubagentReportDeliveryError as exc:
            logger.exception(
                "Stage %d: terminal-report handoff failed for slot %s",
                stage_num,
                slot.key,
            )
            return _delivery_failed(str(exc))
        except Exception:
            logger.exception(
                "Stage %d: terminal-report handoff failed for slot %s",
                stage_num,
                slot.key,
            )
            return _delivery_failed()

        delivery_task = slot.task
        # ``slot.task`` names real turn Tasks in production. Focused callers may
        # use a bare Future only as a busy sentinel; it has no coroutine that can
        # finish it, so joining it here would wedge the stage boundary forever.
        if (
            isinstance(delivery_task, asyncio.Task)
            and delivery_task is not own_task
            and not delivery_task.done()
        ):
            clean_passes = 0
            try:
                await delivery_task
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Stage %d: completion turn failed for slot %s",
                    stage_num,
                    slot.key,
                )
                return _delivery_failed()
            continue

        delivery_entry = owned_stage_delivery_entry(stage_boundary_for(slot), slot._queue)
        if delivery_entry is not None:
            clean_passes = 0
            delivery_key = (
                str(delivery_entry.get("kind", "")),
                str(delivery_entry.get("content", "")),
            )
            if delivery_key in attempted_deliveries:
                return _delivery_failed()
            attempted_deliveries.add(delivery_key)
            try:
                if not await _start_next_queued_turn(state, slot):
                    return _delivery_failed()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Stage %d: completion queue drain failed for slot %s",
                    stage_num,
                    slot.key,
                )
                return _delivery_failed()
            continue

        if (
            slot._subagent_deliveries_inflight
            or stage_boundary_for(slot).synthetic_recovery_inflight
        ):
            clean_passes = 0
            continue
        if clean_passes == 0:
            clean_passes = 1
            continue
        return True

    return False


async def _settle_and_capture_stage(
    state: "DashboardState",
    slot: "_ChatSlot",
    tracker: OrchestrationTracker,
    stage_num: int,
    *,
    auto_run: bool,
    require_registered_slot: bool,
    stop_generation: int,
) -> tuple[bool, bool]:
    """Settle and capture one boundary; return ``(captured, continue)``."""
    if not await _settle_stage_delivery(
        state,
        slot,
        tracker,
        stage_num,
        require_registered_slot=require_registered_slot,
        stop_generation=stop_generation,
    ):
        if _orchestration_stopped(slot, tracker, stop_generation=stop_generation):
            stage_boundary_for(slot).preserve(
                stage_num,
                consumed=stage_boundary_for(slot).consumed,
            )
        return False, False
    if _orchestration_stopped(slot, tracker, stop_generation=stop_generation):
        stage_boundary_for(slot).preserve(
            stage_num,
            consumed=stage_boundary_for(slot).consumed,
        )
        return False, False
    raw_parts = _collect_stage_result_parts(slot)
    try:
        result_path = await asyncio.to_thread(
            _write_stage_result,
            slot.key,
            stage_num,
            raw_parts,
        )
        tracker.record_stage_result(stage_num, result_path)
    except OSError:
        logger.warning(
            "Failed to capture stage %d result to disk",
            stage_num,
            exc_info=True,
        )

    boundary = stage_boundary_for(slot)
    boundary.clear()
    cap = _round_cap_message(tracker, stage_num) if auto_run else None
    if cap:
        _halt_plan(
            state,
            slot,
            cap,
            event_type="auto_run_round_cap",
            operation="stage_round_cap",
            stage_num=stage_num,
        )
        return True, False
    return True, True


async def _stage_loop(
    state: "DashboardState",
    slot: "_ChatSlot",
    auto_run: bool,
) -> None:
    """Python-controlled stage execution loop.

    Iterates through plan stages, calling ``_run_chat`` once per stage.
    Stage boundaries are enforced by Python code, not LLM prompts.
    """
    controller_managed = slot._stage_controller_task is asyncio.current_task()
    _stage_stop_generation = slot._stop_generation
    # Direct internal callers use an unattached slot in focused tests and tools.
    # Production entry points track the controller before the task can run, so
    # only those managed loops require the slot to remain in the live registry.
    # Cancelled-plan latch: checked BEFORE the lazy tracker creation
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
        # stage-boundary-exit: cancelled-before-start clear
        return

    # A restart erased the plan. `_stage_titles` (and so `_plan_stage_count`) live
    # only in memory: nothing persists them, deliberately -- the autopilot is a
    # lightweight executor, not a task runner, and a plan nobody was watching is
    # not resumed across a restart. But `mode` IS persisted and the transcript
    # keeps its `[OPTION: Go | Go All | Cancel]` row, so a restored slot offers
    # buttons with no plan behind them. Clicking one would run zero stages and
    # return in silence -- the loop's range is empty, and the completion message is
    # gated on `start_idx < total`, so the user would get no response at all.
    #
    # Say so instead. Also covers a plan turn that parsed no stages, which reaches
    # this the same way, so the wording names the state rather than a cause.
    if not slot._plan_stage_count:
        _dead_msg = (
            "⚠️ This plan is no longer active — its stages are not in memory, "
            "usually because the gateway restarted since it was created. Nothing "
            "was run. Send the request again to plan it afresh; any stage results "
            "that did complete are still on disk under this session's directory."
        )
        append_and_surface(state, slot, "assistant", _dead_msg, "msg msg-a")
        sel().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex,
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="auto_run_plan_expired",
                caller_identity=f"dashboard:{slot.key}",
                agent=getattr(slot, "agent", ""),
                source="dashboard",
                operation="plan_shape_absent",
                outcome="refused",
                resources=f"slot={slot.key}",
            )
        )
        state.broadcast_ws("chat_done", await chat_done_payload(state, slot))
        slot.task = None
        stage_boundary_for(slot).clear()
        state.push_slots_update()
        # stage-boundary-exit: expired-plan clear
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
    if tracker is None:
        tracker = OrchestrationTracker()
        slot._orch_tracker = tracker

    total = slot._plan_stage_count
    titles = getattr(slot, "_stage_titles", [])

    # Determine the starting stage. An armed delivery boundary wins;
    # otherwise resume from ordinary tracker progress.
    pending_stage = stage_boundary_for(slot).stage
    pending_consumed = stage_boundary_for(slot).consumed
    if pending_stage is not None and not 1 <= pending_stage <= total:
        pending_stage = None
        stage_boundary_for(slot).clear()
    start_idx = (
        pending_stage - 1
        if pending_stage is not None
        else (tracker.current_stage if tracker._stage_rounds else 0)
    )
    # A consumed final-stage boundary still has to emit the one completion
    # summary; an ordinary Go after completion must not emit it again.
    plan_had_work = start_idx < total

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
    _active_stage_num: int | None = None
    _active_stage_consumed = False

    def _preserve_interrupted_stage() -> None:
        """Keep an entered-but-uncaptured stage from becoming an implicit success."""
        if _active_stage_num is None:
            return
        stage_boundary_for(slot).preserve(
            _active_stage_num,
            consumed=_active_stage_consumed,
        )

    # Mark the ENTIRE stage-execution lifetime, not each _run_chat call. A
    # stage turn can queue a recovery/continue turn (empty-response re-queue,
    # stale/tool-stall recovery) that runs slightly later on the same slot; a
    # per-call clear would drop the guard before that recovery ran, letting its
    # plan-shaped output re-arm/re-count the plan. The flag is
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
        # Asked of the TRACKER, not of whether this loop created it. A slot the
        # Slack gateway touched first arrives with a tracker it created lazily
        # when a subagent result landed; that tracker has never seen the config,
        # and gating on "did I just build this" left it running the whole plan
        # on constructor defaults -- the plan watchdog disabled at 0 and the
        # stage budget ignoring config. A tracker that already has its budgets
        # answers False, so a paused plan's later Go still pays for nothing.
        if tracker.budgets_unset and not await _load_plan_budgets(slot, tracker):
            # stage-boundary-exit: budget-load-aborted owned
            return
        if pending_stage is not None:
            if not pending_consumed and _has_exact_stage_delivery_retry(slot):
                if not await _settle_stage_delivery(
                    state,
                    slot,
                    tracker,
                    pending_stage,
                    require_registered_slot=controller_managed,
                    stop_generation=_stage_stop_generation,
                ):
                    # stage-boundary-exit: pending-retry-settle-failed owned
                    return
                pending_consumed = stage_boundary_for(slot).consumed
            if not pending_consumed:
                # The stage prompt never reached the model. The loop below
                # re-enters this same stage with a fresh boundary generation.
                stage_boundary_for(slot).clear()
                slot._last_turn_auth_required = False
            else:
                captured, can_continue = await _settle_and_capture_stage(
                    state,
                    slot,
                    tracker,
                    pending_stage,
                    auto_run=auto_run,
                    require_registered_slot=controller_managed,
                    stop_generation=_stage_stop_generation,
                )
                if not can_continue:
                    if captured:
                        # stage-boundary-exit: pending-round-cap clear
                        return
                    # stage-boundary-exit: pending-capture-failed owned
                    return
                start_idx = pending_stage
        for stage_idx in range(start_idx, total):
            if controller_managed and state._slots.get(slot.key) is not slot:
                logger.info(
                    "Stage loop for slot %s stopped because the slot is no longer registered",
                    slot.key,
                )
                # stage-boundary-exit: slot-unregistered clear
                break
            if _orchestration_stopped(
                slot,
                tracker,
                stop_generation=_stage_stop_generation,
            ):
                # stage-boundary-exit: stopped-before-stage clear
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
                # stage-boundary-exit: plan-shrank clear
                break

            # Whole-plan watchdog. The per-stage timeout below bounds ONE stage;
            # multiplied by stage count it bounds nothing useful, so a long plan
            # could run unattended for hours. Checked at the stage
            # boundary rather than mid-turn: the stage that is already running has
            # its own ceiling, and cutting a plan between stages leaves the work
            # so far captured on disk and resumable.
            #
            # AUTO-RUN ONLY. The budget bounds UNATTENDED runtime, and the clock is
            # wall-clock from the plan's first round, so a stage-gated plan spends
            # most of it sitting at an approval prompt: enforcing it there would cut
            # a plan the user is actively stepping through, counting their own
            # review time between Go clicks against them. A plan that advances only
            # when the user asks it to needs no ceiling, because the user is the
            # ceiling.
            if auto_run and tracker.is_plan_timed_out():
                _halt_plan(
                    state,
                    slot,
                    f"⏱️ Plan exceeded its total budget of "
                    f"{tracker.plan_timeout_human} (elapsed "
                    f"{tracker.plan_elapsed_human}) before Stage {stage_num}. "
                    "Auto-run stopped.",
                    event_type="auto_run_timeout",
                    operation="plan_duration_exceeded",
                    stage_num=stage_num,
                )
                # stage-boundary-exit: plan-timeout clear
                break
            # One warning per plan, latched inside the tracker, so the user can
            # intervene before the cut rather than only learning of it after.
            # Gated with the cut it warns about: an attended plan is never cut, so
            # a notice there would announce a ceiling that does not apply.
            if auto_run and tracker.plan_warning_due():
                _warn_msg = (
                    f"⏳ Plan has used {tracker.plan_elapsed_human} of its "
                    f"{tracker.plan_timeout_human} total budget. It will stop at "
                    "the first stage boundary past the budget."
                )
                slot.append("assistant", _warn_msg, "msg msg-a")
                state.broadcast_ws(
                    "chat_append",
                    {"slot": slot.key, "html": _warn_msg, "cls": "msg msg-a"},
                )

            # Check the timeout BEFORE entering the stage: `start_stage` restarts
            # the per-stage clock, so reading it afterwards would always be 0.
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
                # stage-boundary-exit: stage-timeout-before-entry clear
                break

            # Enter the stage and emit the separator (after the timeout check).
            # NOT `record_round`: a round is a spawn wave, and the cap this PR
            # makes real is the wave budget -- see `OrchestrationTracker.start_stage`.
            tracker.start_stage(stage_num)
            stage_boundary_for(slot).arm(stage_num)
            _active_stage_num = stage_num
            _active_stage_consumed = False
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
            # the delivery point for the completed, paused and cancelled paths.
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
                stage_boundary_for(slot).parent_session_keys.add(effective_session_key(slot))
                _stage_turn_consumed = False

                def _record_stage_turn_consumed(consumed: bool) -> None:
                    nonlocal _active_stage_consumed, _stage_turn_consumed
                    _stage_turn_consumed = consumed
                    _active_stage_consumed = consumed
                    if stage_boundary_for(slot).stage == stage_num:
                        stage_boundary_for(slot).mark_consumed(consumed)

                _stage_turn_coro = _run_chat(
                    state,
                    slot,
                    context,
                    _directive_user_origin=False,
                    _on_consumed=_record_stage_turn_consumed,
                    # Stage context assembled by the orchestrator, so the
                    # ledger records the gateway rather than a user.
                    _turn_actor="gateway",
                )
                if _turn_timeout:
                    _stage_turn_coro = _bounded_turn(_stage_turn_coro, _turn_timeout)
                # ``slot.task`` must name the ACTIVE LLM turn, not this outer
                # stage controller. A subagent terminal report waits for that
                # task before injecting its completion. Pointing it at the
                # controller made the report wait for every remaining stage,
                # while the controller saw no running agents and advanced before
                # the report reached the conversation.
                _stage_turn_task = asyncio.create_task(
                    _stage_turn_coro,
                    name=f"dashboard-stage-turn:{slot.key}:{stage_num}",
                )
                slot.task = _stage_turn_task
                try:
                    await _stage_turn_task
                finally:
                    # A completion report may already have claimed the slot with
                    # its own turn after this one ended. Never clear that newer
                    # claim.
                    if slot.task is _stage_turn_task:
                        slot.task = None
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
                _preserve_interrupted_stage()
                # stage-boundary-exit: stage-turn-timeout owned
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
                _preserve_interrupted_stage()
                # stage-boundary-exit: stage-turn-error owned
                break

            if _orchestration_stopped(
                slot,
                tracker,
                stop_generation=_stage_stop_generation,
            ):
                _preserve_interrupted_stage()
                # stage-boundary-exit: stopped-after-turn owned
                break

            stage_boundary_for(slot).mark_consumed(_stage_turn_consumed)
            if not _stage_turn_consumed and _has_exact_stage_delivery_retry(slot):
                # Recovery belongs to this exact unconsumed stage turn. Keep the
                # same controller and stage guard alive while settlement drains
                # it; its preserved consumption callback updates both the local
                # and persistent stage state before this function continues.
                if not await _settle_stage_delivery(
                    state,
                    slot,
                    tracker,
                    stage_num,
                    require_registered_slot=controller_managed,
                    stop_generation=_stage_stop_generation,
                ):
                    if _orchestration_stopped(
                        slot,
                        tracker,
                        stop_generation=_stage_stop_generation,
                    ):
                        _preserve_interrupted_stage()
                    # stage-boundary-exit: retry-settle-failed owned
                    return
            if not _stage_turn_consumed:
                # No exact successor completed this turn. Preserve the boundary
                # for one guarded retry on the next Go rather than capturing an
                # unexecuted stage.
                slot._auto_run = False
                _paused = True
                # stage-boundary-exit: unconsumed-stage-paused owned
                return

            if _orchestration_stopped(
                slot,
                tracker,
                stop_generation=_stage_stop_generation,
            ):
                _preserve_interrupted_stage()
                # stage-boundary-exit: stopped-before-capture owned
                break

            captured, can_continue = await _settle_and_capture_stage(
                state,
                slot,
                tracker,
                stage_num,
                auto_run=auto_run,
                require_registered_slot=controller_managed,
                stop_generation=_stage_stop_generation,
            )
            if not can_continue:
                if captured:
                    # stage-boundary-exit: stage-round-cap clear
                    break
                # stage-boundary-exit: stage-capture-failed owned
                break
            _active_stage_num = None
            _active_stage_consumed = False

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
                    # stage-boundary-exit: manual-stage-pause clear
                    return
        else:
            # for loop completed without break — all stages done
            if not slot._stopping and plan_had_work:
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
        # Preserve the entered stage before hard stop / slot deletion tears down
        # this controller. Deleted slots discard the state with the slot; a live
        # slot's next Go must retry or settle this stage rather than infer success
        # from ``tracker.current_stage`` and skip it.
        _preserve_interrupted_stage()
        # Hard stop / slot deletion: do NOT hand off queued work below (the slot
        # is being torn down and a started turn would run orphaned). Mark it and
        # re-raise so the task ends cancelled.
        _cancelled = True
        # stage-boundary-exit: controller-cancelled owned
        raise
    finally:
        # Clear the stage-execution guard exactly once, when the loop exits
        # (pause / completion / break / error). This spans any queued recovery
        # turns a stage started, and lets a later Cancel + re-plan arm again.
        slot._in_stage_execution = False
        # A session Stop preserves the interrupted stage for a later Go. An
        # explicit PLAN cancel does the opposite: the plan is revoked, so no
        # stage remains to resume and its marker must not block ordinary queued
        # input from the final handoff below.
        if slot._plan_cancelled:
            await _release_cancelled_plan_boundary(
                state,
                slot,
                controller_active=True,
            )
            _active_stage_num = None
            _active_stage_consumed = False
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
        # The public ``slot.running`` includes this outer controller so Stop and
        # plan-action arbitration stay busy between stages. Final handoff needs
        # the narrower child-turn question: only another live ``slot.task`` owns
        # queue draining and ``chat_done``.
        _own_task = asyncio.current_task()

        def _child_turn_live() -> bool:
            turn_owner = slot.task
            return bool(
                turn_owner is not None and turn_owner is not _own_task and not turn_owner.done()
            )

        _turn_live = _child_turn_live()
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
        # Build the terminal payload before the final queue decision. This is the
        # controller's last await; a message accepted while it suspends sees the
        # controller as running and enters the queue, then the arbitration below
        # starts it before the controller releases ownership.
        done_payload: dict | None = None
        if not _turn_live:
            done_payload = await chat_done_payload(state, slot)
            if _paused:
                done_payload["needs_input"] = True
            _turn_live = _child_turn_live()

        # Same revoked-approval filter as _exit_cancelled_plan, at this drain:
        # a Go queued WHILE the plan ran, followed by a mid-loop cancel, would
        # otherwise drain an approval entry into _run_chat here — the
        # surviving residual both advisory lanes flagged. Only when the
        # plan is revoked; a paused plan's queued approval is still live.
        if slot._plan_cancelled and slot._queue:
            await _release_cancelled_plan_boundary(
                state,
                slot,
                controller_active=True,
            )
        # A pending stage boundary owns its completion retry. Leave that entry
        # queued for the next Go, which re-enters with `_in_stage_execution` set;
        # the generic handoff below has already cleared that guard.
        if (
            not _cancelled
            and not _turn_live
            and not slot._last_turn_auth_required
            and stage_boundary_for(slot).stage is None
            and state._slots.get(slot.key) is slot
            and slot._queue
            and not slot._stopping
        ):
            state.push_slots_update()
            _next_started = await _start_next_queued_turn(state, slot)
        if not _next_started and not _turn_live:
            if not _paused:
                slot.append("done", "", "done")
            assert done_payload is not None
            state.broadcast_ws("chat_done", done_payload)
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
        scope = _capture_stage_cancellation_scope(slot)
        tracker = slot._orch_tracker
        # Idempotence read taken BEFORE the latch is set. The latch alone is the
        # test: it is cleared only when a new plan is armed, so "latch set" means
        # this plan is already revoked. Deliberately NOT conjoined with tracker
        # state — the Slack gateway lazily creates a fresh UNSTOPPED tracker on
        # an orchestrator slot when a subagent result lands, so a result arriving
        # between two Cancels would make a tracker-based read report the plan as
        # live again and write a duplicate '🛑 Plan cancelled.' row. The
        # unconditional tracker.stop() below still stops such a
        # gateway-created tracker on every cancel POST.
        already_cancelled = slot._plan_cancelled
        # Set unconditionally — NOT only when a tracker exists. The tracker is
        # created lazily inside _stage_loop, so a Cancel processed in the window
        # between a Go POST being accepted and its _stage_loop coroutine running
        # would otherwise no-op entirely and the plan would advance while the
        # transcript says cancelled. _stage_loop checks this latch
        # before creating a tracker.
        slot._plan_cancelled = True
        if tracker and not tracker.stopped:
            tracker.stop()
        slot._auto_run = False
        reservation_reason = _reserve_stage_cancellation_scopes(state, scope)
        signal_completion = getattr(state.subagents, "signal_completion", None)
        if callable(signal_completion):
            parent_keys = set(_stage_parent_session_keys(slot))
            parent_keys.add(f"dashboard:{slot.key}")
            for parent_key in sorted(parent_keys):
                signal_completion(parent_key)
        from kiro_crew.dashboard.chat_handlers import (  # circular import: handlers import orchestrator
            _cancel_stage_controller,
        )

        await _cancel_stage_controller(slot)
        release_boundary = await _cancel_stage_subagents(
            state,
            slot,
            scope=scope,
            reservation_reason=reservation_reason,
        )
        # A live stage controller owns cleanup in its ``finally`` after the
        # current turn unwinds. A paused boundary has no such owner, so Cancel
        # settles its owned rows and terminal frame before admission reopens.
        stop_msg = "" if already_cancelled else "🛑 Plan cancelled."
        if release_boundary and not slot._in_stage_execution:
            await _release_cancelled_plan_boundary(
                state,
                slot,
                terminal_message=stop_msg,
            )
        elif release_boundary and stop_msg:
            # The shared release seam publishes the terminal frame only after
            # controller teardown and exact-scope child cancellation finish.
            await _release_cancelled_plan_boundary(
                state,
                slot,
                terminal_message=stop_msg,
            )
        return web.json_response({"ok": True, "cancelled": True})

    # Go or Go All — use Python-controlled stage loop. ``running`` includes an
    # uncancelled pending boundary so destructive/background paths stay blocked;
    # that boundary alone is exactly what validated Go resumes. A live turn or
    # controller remains ``turn_running`` and queues this approval.
    if slot.turn_running:
        # circular import: session_control imports this package's modules at module level.
        from kiro_crew.dashboard.chat_delivery import TURN_ACTOR_META_KEY
        from kiro_crew.dashboard.session_control import containment_meta

        # Provenance follows the CALLER — the same request-identity split as
        # api_chat and the manual continue. A human clicking Go on their own
        # busy session must not lose the approval if they link the session
        # before the drain; an app relaying a plan action never gains the
        # authenticated-human flag.
        # kind is a structural origin tag, not bare content: _exit_cancelled_plan
        # drops revoked approvals by this tag, and queue_append's contract names
        # metadata (never content equality) as the classification mechanism.
        _go_meta = containment_meta(state, slot)
        if request.get("app", ""):
            # The actor, not only the origin flag. `plan_approval` maps to nothing in
            # `_QUEUE_KIND_ACTORS`, so without this stamp the drain falls through to
            # `user` and an app's relayed approval runs as the person's turn -- which
            # is the same fallback every consumer of that field then reads.
            _go_meta[TURN_ACTOR_META_KEY] = "app"
        slot.queue_append(
            "Go",
            kind="plan_approval",
            meta=_go_meta,
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
    if stage_boundary_for(slot).stage is not None:
        _queue_consumed_stage_resume(
            state,
            slot,
            directive_user_origin=not bool(request.get("app", "")),
        )
        slot._last_turn_auth_required = False
    task = asyncio.create_task(
        _stage_loop(state, slot, auto_run=is_auto),
        name=f"dashboard-stage:{slot.key}",
    )
    slot.track_stage_controller(task)
    slot.task = task
    # S4: one accepted Go resets the recovery budget shared by its stages.
    stage_boundary_for(slot).recovery_retrigger_count = 0
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    state.push_slots_update()
    return web.json_response({"ok": True})
