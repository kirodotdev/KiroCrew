"""Draining a dashboard slot's queue into its next turn: delivery settlement, the memory-
preparation parking, the stall drain and the next queued turn itself."""

from __future__ import annotations

import asyncio
import inspect
import json
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.chat_delivery import TURN_ACTOR_META_KEY as _TURN_ACTOR_META_KEY
from kiro_crew.dashboard.chat_delivery import (
    attachment_meta,
    queue_entry_is_user_origin,
    queued_text_for_display,
    quote_meta,
)
from kiro_crew.dashboard.chat_title import RESTORED_TURN_META_KEY
from kiro_crew.dashboard.chat_utils import (
    AUTOMATION_NOTICE_KIND,
    MCP_APP_MESSAGE_KIND,
    STEER_INTENT_META,
    STEER_POSSIBLY_DELIVERED_META,
    SUBAGENT_DELIVERY_KINDS,
    TURN_END_WIRE_CLS,
    _remove_queued_by_id,
    app_inject_row,
    is_synthetic_payload_item,
    is_synthetic_recovery_item,
    is_system_injection_item,
)
from kiro_crew.dashboard.chat_utils_injected_rows import AUTOMATION_STEER_META
from kiro_crew.dashboard.recovery_replays import replays_of
from kiro_crew.dashboard.session_directive_apply import (
    REFUSAL_SELF_WAKE_META_KEY,
    REFUSAL_WAKE_LOOP_ID_META_KEY,
)
from kiro_crew.dashboard.slot_queue_repository import RESTORED_QUEUE_KEY
from kiro_crew.dashboard.state import CRON_NOTIFY_RE, SUBAGENT_COMPLETION_PREFIXES
from kiro_crew.history import HUMAN_TURN_META_KEY

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        _PARKED_FOR_MEMORY,
        COMMANDS_OFF_META_KEY,
        CRON_NOTIFICATION_KIND,
        CRON_NOTIFY_PREFIX,
        SUBAGENT_COMPLETION_KIND,
        DashboardState,
        KiroCrewConfig,
        _ChatSlot,
        _dequeue_next_message,
        _dequeue_next_system_message,
        _run_chat,
        append_and_surface,
        effective_session_key,
        logger,
        redact_credentials,
        redact_exfiltration_urls,
        shutdown_event,
        spawn_guarded_turn,
    )
    from kiro_crew.dashboard.chat_turn.recipient import (
        MIRROR_ECHO_OWED_META_KEY,
        _batch_recovery_echo_owed,
    )
    from kiro_crew.dashboard.chat_turn.recovery import (
        _drop_revoked_replays,
        _forget_swept_replays,
        _purge_superseded_continuations,
    )
    from kiro_crew.dashboard.chat_turn.regenerate_swap import adopt_regenerate_target
    from kiro_crew.dashboard.chat_turn.steer_queue import (
        _actor_for_queue_items,
        _drop_stale_admissions,
    )
    from kiro_crew.dashboard.chat_turn.turn_marker import _gateway_shutdown_requested


def _arm_queued_delivery_settlement(
    state: DashboardState,
    slot: _ChatSlot,
    task: "asyncio.Task",
    contents: list[str],
    consumed: list[bool],
) -> None:
    """Open the retention window on a drained completion once its turn has RUN.

    The gateway records the owed agent ids on the slot when it has to QUEUE a
    sub-agent completion (``GatewayOrchestrator._defer_queued_delivery`` in
    ``slack/gateway_runtime/subagent_injection.py``), keyed on the announce content, which is what keeps each ``result.txt`` alive for as long as
    the row waits. This is the other half — but it deliberately does not fire at
    dispatch.

    A ``delivered`` tombstone is durable and excludes the folder from restart
    orphan reconciliation, while a dispatched turn is not yet durable: the
    injected row is still being persisted and the model has not necessarily
    consumed the prompt. Writing the tombstone at dispatch would mean a crash in
    that window loses the completion for good (no queue row left, no folder to
    recover, and the result pruned one TTL later). Waiting for the task to finish
    makes the failure mode fail-safe instead: nothing is written, so the next
    start's reconciliation still sees the folder and re-delivers it.

    Nothing is settled until the model has CONSUMED the prompt, and that is the
    only condition. ``_run_chat`` handles a signed-out CLI, a dead provider,
    exhausted prompt-busy retries, a transient backend error and a stall by
    rendering a card and RETURNING NORMALLY, several of them after re-queueing the
    prompt itself for a later retry — so the task's outcome says nothing either way.
    *consumed* is the evidence that does: ``_on_complete`` (turn_stats) sets it on the provider's
    turn-complete event for a real end-of-turn, ``_run_chat`` on the first streamed token, and
    ``_on_tool_call`` (tool_rows) on a fired tool call. Those triggers are exactly the states
    in which the announce will NOT be replayed. An EMPTY response splits: the FIRST one
    re-queues this same announce verbatim and RETRACTS the report, so the clock
    waits for the replay that lands; the second re-queues a continuation instead
    and stays consumed.

    Consumption is one-way, so a turn cancelled or failed after it still settles:
    the result is in the model's context either way, and withholding the tombstone
    would have the next start re-announce a completion the parent already read.

    *consumed* is a cell owned by THIS armed turn, never slot-wide state: a turn's
    tail-drain starts its successor before the predecessor's callback runs, so a
    shared field would be reset by the successor and leave the earlier — already
    consumed — completion unsettled and re-injected after a restart.

    An unconsumed turn settles nothing, which is the fail-safe side: the folder
    survives for the replay to read and the next start's reconciliation recovers
    it, where a premature tombstone would have the reaper delete a result the
    parent never saw.
    """

    def _on_turn_done(finished: "asyncio.Task") -> None:  # type: ignore[type-arg]
        # Consumption is the whole predicate, and it is one-way: once the model has
        # the prompt, nothing later un-delivers it. A turn cancelled or failed AFTER
        # that point (session close, shutdown, the turn ceiling) does not replay the
        # announce -- ``build_recovery_requeue`` switches to a continuation once
        # anything was emitted, a stop suppresses the requeue outright, and the
        # guarded-turn error path only renders a card -- so skipping settlement
        # there would leave a consumed result to be re-announced as an orphan by
        # the next start. The task's own outcome is therefore not consulted.
        if not consumed[0]:
            return
        try:
            owed = slot.take_pending_subagent_deliveries(contents)
        except Exception:
            logger.debug("Could not claim queued sub-agent delivery marks", exc_info=True)
            return
        if not owed:
            return
        # The manager owns the write: it holds each tombstone until that run's
        # teardown has finished, so one can never hide a child that is still being
        # killed from restart reconciliation. There is no second path -- the debt
        # only ever exists because the manager's own completion callback created
        # it, so a state without a manager cannot have one to settle. If the call
        # does not hand back a coroutine (a stubbed manager), skip rather than
        # invent a write that would bypass the teardown gate; the folder stays
        # recoverable by the next start's reconciliation.
        mgr = getattr(state, "subagents", None)
        settle = getattr(mgr, "settle_queued_delivery", None) if mgr is not None else None
        work = None
        if settle is not None:
            try:
                candidate = settle(owed)
            except Exception:
                logger.debug("Manager-side delivery settlement refused", exc_info=True)
            else:
                work = candidate if asyncio.iscoroutine(candidate) else None
        if work is None:
            logger.debug(
                "No sub-agent manager to settle queued delivery for %s; "
                "leaving the folder for restart reconciliation",
                owed,
            )
            return
        writer = asyncio.create_task(work)
        bg = getattr(state, "_background_tasks", None)
        if isinstance(bg, set):
            bg.add(writer)
            writer.add_done_callback(bg.discard)

    try:
        task.add_done_callback(_on_turn_done)
    except Exception:
        logger.debug("Could not arm queued sub-agent delivery settlement", exc_info=True)


def settle_drained_subagent_notices(state: Any, slot: Any, notices: list[str]) -> None:
    """Settle the delivery debt of notices drained from ``_pending_subagent_failures``.

    A notice whose run owes a ``delivered`` tombstone is recorded on the slot's
    ledger under its own text when it is queued, and that run's folder stays
    untombstoned until this drain, so a restart before it leaves the run to the
    next start's orphan reconciliation. The parent's next turn calls this where
    it takes the list. The manager writes the tombstones on a tracked task,
    behind the same teardown gate as a queued announce.
    """
    try:
        owed = slot.take_pending_subagent_deliveries(notices)
    except Exception:
        logger.debug("Could not claim drained sub-agent notice deliveries", exc_info=True)
        return
    if not owed:
        return
    mgr = getattr(state, "subagents", None)
    settle = getattr(mgr, "settle_queued_delivery", None) if mgr is not None else None
    work = None
    if settle is not None:
        try:
            candidate = settle(owed)
        except Exception:
            logger.debug("Manager-side notice settlement refused", exc_info=True)
        else:
            work = candidate if asyncio.iscoroutine(candidate) else None
    if work is None:
        logger.debug(
            "No sub-agent manager to settle drained notices for %s; "
            "leaving the folder for restart reconciliation",
            owed,
        )
        return
    writer = asyncio.create_task(work)
    bg = getattr(state, "_background_tasks", None)
    if isinstance(bg, set):
        bg.add(writer)
        writer.add_done_callback(bg.discard)


#: Where a requeue stamps the actor of the turn it is retrying. Re-exported from
#: ``chat_delivery``, which assembles queue-entry meta, so the producers that stamp
#: it and the drain below that reads it cannot drift onto two spellings.
TURN_ACTOR_META_KEY = _TURN_ACTOR_META_KEY


MEMORY_PREPARATION_PARKED_TEXT = (
    "ℹ️ Memory is still being prepared after the gateway restart. "
    "Queued messages will run when it is ready."
)


def _memory_preparation_pending(state: DashboardState) -> bool:
    """Whether the gateway's shared memory preparation is still running.

    A finished task (completed, failed or cancelled) is not pending: a failed
    or cancelled preparation is refused by the turn itself with its own error,
    which is a permanent answer rather than a wait.
    """
    task = getattr(state, "memory_startup_task", None)
    return task is not None and not task.done()


def _arm_parked_queue_drain(state: DashboardState, slot: _ChatSlot, *, notice: bool) -> None:
    """Record *slot* as parked on memory preparation and arm the drain.

    The first call per task adds the done-callback; later calls only record
    the slot. Only recorded slots are drained, so a queue this mechanism did
    not park (one restored from disk at boot, say) waits for its user's
    next send. With *notice*, the slot gets one notice row per
    task so the user can see why its queued cards are not running.
    """
    task = getattr(state, "memory_startup_task", None)
    if task is None or task.done():
        return
    parked = _PARKED_FOR_MEMORY.get(task)
    if parked is None:
        parked = {}
        _PARKED_FOR_MEMORY[task] = parked

        def _on_prepared(_done: Any) -> None:
            if not _parked_queue_drain_admitted():
                return
            drain = asyncio.ensure_future(_drain_parked_queues(state, list(parked)))
            state._background_tasks.add(drain)
            drain.add_done_callback(state._background_tasks.discard)

        task.add_done_callback(_on_prepared)
    noticed = parked.get(slot.key, False)
    parked[slot.key] = noticed or notice
    if notice and not noticed:
        slot.append("notice", MEMORY_PREPARATION_PARKED_TEXT, "msg msg-info")
        state.push_slots_update()


def _parked_queue_drain_admitted() -> bool:
    """Whether a parked queue may be dequeued now that preparation has ended.

    The task ending is not the same as memory being ready: a shutdown, a
    stopped startup or a recorded recovery failure all end it too, and each
    makes `_run_chat` refuse the turn after it has been popped. In those cases
    the queue stays as it is, so the history save keeps it for the next start.
    """
    if shutdown_event.is_set():
        return False
    from kiro_crew.memory_startup import MemoryStartupUnavailable, require_memory_prepared

    try:
        require_memory_prepared()
    except MemoryStartupUnavailable:
        return False
    return True


# Recovery turns and memory-ready callbacks inherit only their accepted card ids.
_RESTORED_DRAIN_IDS: ContextVar[dict[str, frozenset[str]]] = ContextVar(
    "restored_drain_ids", default={}
)


async def _drain_parked_queues(state: DashboardState, slot_keys: list[str]) -> None:
    """Start the next queued turn on each parked slot that is still idle.

    Runs once when memory preparation finishes. Each slot is re-checked under
    its own lock, the same guard the dispatch routes take, so a send or a
    Continue that landed in the meantime is never doubled: a running slot
    drains its own queue at its turn end.
    """
    for key in slot_keys:
        slot = state._slots.get(key)
        if slot is None or not slot._queue or slot.executor == "remote":
            continue
        try:
            async with slot._lock:
                if (
                    state._slots.get(slot.key) is not slot
                    or not slot._queue
                    or slot.running
                    or slot._stop_state != "idle"
                ):
                    continue
                if not _memory_preparation_pending(state) and not _parked_queue_drain_admitted():
                    return
                started = await _start_next_queued_turn(state, slot)
            if started:
                state.push_slots_update()
        except Exception:
            logger.warning(
                "Could not drain the parked queue for slot %s after memory preparation",
                slot.key,
                exc_info=True,
            )


async def drain_idle_parent_queue_for_stall(state: DashboardState, parent_session_key: str) -> None:
    """Drain one idle parent slot's queue after its only live child stalled.

    ``subagents_hold_user_messages`` already lets a stalled child's queue drain,
    but that rule only fires when something *starts* a drain: the next user send,
    or a child's completion turn. A message queued BEFORE the reaper flagged the
    child ``stalled`` has neither — it sits until the next send or the 30-minute
    wall-clock reap. The ``subagent_stalled`` event is the missing trigger: when
    the flag is set, kick the parent slot's drain so the parked message starts.

    Resolves the parent slot and hands it to :func:`_drain_parked_queues`, which
    owns the lock-guarded, admission-checked drain: a send or Continue that
    landed meanwhile is never doubled, a running slot drains its own queue at its
    turn end, ``_start_next_queued_turn`` re-reads the hold so a live sibling
    keeps the queue parked, and a stall arriving mid-shutdown leaves the message
    queued rather than popping it into a refused turn.
    """
    from kiro_crew.dashboard.handlers.messaging import _slot_for_parent

    slot = _slot_for_parent(state, parent_session_key)
    if slot is None or not slot._queue or slot.executor == "remote":
        return
    await _drain_parked_queues(state, [slot.key])


def _mark_turn_end(slot: _ChatSlot) -> None:
    """Mark on ``slot._pending`` that the turn that just ended is over.

    Pushed at a turn's end before the queue drain or the cycle's end writes
    anything (a held note, a drop notice, a successor's first row), never before
    a recovery the ending turn queued for itself. The end of the whole queue
    cycle is ``done``; this boundary is where an app's stream on a user's
    session stops (``chat_handlers.api_chat``), and every other reader skips it,
    so a repeated boundary is harmless.
    """
    slot.push_wire_frame(TURN_END_WIRE_CLS, "")


_RECOVERY_TURN_META_KEY = "recoveryTurnId"


def _is_own_recovery(items: "list[dict]", predecessor_actor: str, predecessor_turn_id: str) -> bool:
    """Only a retry stamped by the exact ending turn continues its stream.

    Actor equality alone also admits an older, held retry of the same app.
    Missing or restored provenance cannot attest to this turn's identity.
    """
    return (
        bool(predecessor_actor)
        and bool(predecessor_turn_id)
        and len(items) == 1
        and is_synthetic_recovery_item(items[0])
        and not items[0].get(RESTORED_QUEUE_KEY)
        and isinstance(items[0].get("meta"), dict)
        and items[0]["meta"].get(_RECOVERY_TURN_META_KEY) == predecessor_turn_id
        and _actor_for_queue_items(items) == predecessor_actor
    )


#: Process-local key on a synthetic-recovery queue entry that is a verbatim
#: requeue of a sub-agent completion. Never persisted: a kinded entry is not
#: durable (``slot_queue_repository._is_durable_queue_entry``).
_REPLAYS_COMPLETION_KEY = "_replays_completion"


def idle_slot_admits_drain(state: DashboardState, slot: _ChatSlot) -> bool:
    """Whether an injector may start the queued head of an idle *slot* now.

    Read under ``slot._lock``. The slot is still the one registered under its
    key, owns no turn (its task, when set, is done), holds no send's admission
    reservation, is not closing, is not held for sign-in and has no Stop in
    flight. Shutdown and memory preparation are refused by
    :func:`_start_next_queued_turn` itself.
    """
    task = slot.task
    return (
        state.get_slot(slot.key) is slot
        and not slot.running
        and (task is None or task.done())
        and not slot._turn_admission_reserved
        and not slot.is_closing
        and not slot._last_turn_auth_required
        and not slot._stopping
        and slot._stop_state == "idle"
    )


def subagents_hold_user_messages(state: DashboardState, session_key: str) -> bool:
    """Whether a live child of *session_key* should keep user messages queued.

    A child the reaper has flagged ``stalled`` (see
    ``_maybe_flag_stall_impl``) does not hold the queue: a child that died
    before its completion path ran never sets ``done``, so counting it parks
    every user message until the wall-clock timeout. The flag clears when the
    child streams again, and the hold with it.
    """
    subs = state.subagents
    if subs is None:
        return False
    agents = subs.running_agents_for(session_key) or []
    return any(not (isinstance(a, dict) and a.get("stalled")) for a in agents)


def _record_queued_crew_steers(state: DashboardState, consumed: list[dict]) -> None:
    """Record the person's queued steers into a Crew Mode worker as they are delivered.

    A send the person typed into a busy worker is queued with ``(root, worker)``
    on its entry (``chat_send.mark_crew_steer``). The ``steer`` event is written
    HERE, when the drain hands the entry to the worker, from the entry's text as it
    is now (an edit while queued is what the worker gets), so a cancelled entry
    records nothing. Off the drain's path: a tracked task, and a fault never
    blocks the turn. The marker is in memory only, so an entry a restart restores
    is delivered and never recorded.
    """
    from kiro_crew.dashboard.chat_handlers import CREW_STEER_QUEUE_KEY

    for item in consumed:
        marker = item.pop(CREW_STEER_QUEUE_KEY, None)
        if not (isinstance(marker, tuple) and len(marker) == 2):
            continue
        root, worker = marker
        text = item.get("content")
        if not isinstance(text, str) or not text:
            continue
        try:
            from kiro_crew.dashboard.handlers import crew_workers

            task = asyncio.get_running_loop().create_task(
                crew_workers.person_steer(state, root, worker, text)
            )
            tasks = getattr(state, "_background_tasks", None)
            if isinstance(tasks, set):
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except Exception:
            logger.warning("crew steer: a queued steer was not recorded", exc_info=True)


async def _start_next_queued_turn(
    state: DashboardState,
    slot: _ChatSlot,
    *,
    allow_user_during_subagents: bool = False,
    required_queue_id: str | None = None,
    predecessor_actor: str = "",
    predecessor_turn_id: str = "",
    end_batch: bool = False,
) -> bool:
    """Dequeue and start one ready Kiro turn, preserving queue semantics.

    ``allow_user_during_subagents`` is the explicit queued-card Run-now path. It
    bypasses only the child-work hold; an active stage still owns dispatch.
    ``required_queue_id`` binds the action to the selected card after admission
    revalidation, so a stale click never starts a different queued message.
    ``predecessor_actor`` and ``predecessor_turn_id`` identify the ending turn,
    so only a recovery stamped by that exact turn avoids a new-turn boundary
    (:func:`_mark_turn_end`). ``end_batch`` says the caller read the synthesis
    fire gate as ``clear`` with the floor held, so a user message drained here
    also zeroes the batch's completion-turn count; the drain itself reads no
    store.
    """

    # Nothing starts while the process is going away. The successor would write
    # its own in-flight marker before the session gate refuses it, and that marker
    # is what the next start reads in place of the ending turn's: the restart would
    # show the recovery's row, not the cut reply. The queue stays as it is, so the
    # history save keeps it for the next start, as the parked drain already does.
    if _gateway_shutdown_requested(state):
        return False

    # A drain that follows a turn's end marks that end before it writes anything:
    # the drop notices, the held-note flush and the cancellation notices below
    # belong to what comes next, not to the turn that ended. Held back only while
    # the queue head is that turn's own recovery; the dequeue re-decides then.
    turn_ended = False
    if predecessor_actor and not _is_own_recovery(
        slot._queue[:1], predecessor_actor, predecessor_turn_id
    ):
        _mark_turn_end(slot)
        turn_ended = True

    # Ahead of any dequeue: while the gateway's memory preparation is still
    # running, a dispatched turn only waits at `_run_chat`'s admission seam and,
    # past its grace period, is refused. Popping the entry and writing its row
    # first would consume input that never runs, so leave the queue as it is
    # (cards stay visible, order unchanged) and let the preparation task's
    # completion drain it (`_arm_parked_queue_drain`).
    if slot._queue and _memory_preparation_pending(state):
        _arm_parked_queue_drain(state, slot, notice=True)
        return False

    # FIRST, before anything reads the queue: re-assert each entry's
    # admission-time containment and drop every entry that has stopped
    # qualifying.
    # Everything below — the note flush peeking at queue[0], the user-intervention
    # purge, the dequeue itself — must see only entries that may still deliver.
    _drop_stale_admissions(state, slot)
    # The sweep may have dropped that recovery, leaving a new turn at the head.
    if (
        predecessor_actor
        and not turn_ended
        and not _is_own_recovery(slot._queue[:1], predecessor_actor, predecessor_turn_id)
    ):
        _mark_turn_end(slot)
        turn_ended = True

    # The admission sweep above (and any other queue removal) can drop a
    # replay's entry WITHOUT touching the ledger, and the empty-queue early return
    # below would skip the re-checks entirely: forget such a record here, before
    # the queue is read for dispatch, so a later entry cannot inherit it.
    _forget_swept_replays(slot)

    # Above the dequeue, so a held note's visible line lands before this turn's
    # user row: its context half drains inside _turn_build_prompt via drain_pending_context.
    # Withheld when the next queued item carries a structural origin tag -- a cron
    # notification or a runner-injected recovery prompt -- for the same reason
    # _finish_queue_cycle withholds from synthesis: a note is owed to the next
    # USER turn, and that cycle's own flush delivers it afterwards.
    if not (slot._queue and slot._queue[0].get("kind")):
        try:
            slot.flush_deferred_notes()
        except Exception:
            # Everything below this point is the successor handoff -- the dequeue,
            # the row append and spawn_guarded_turn. A raise here would return
            # without dispatching, leaving the queued work stranded, so degrade to
            # "the held note waits for the next seam" and carry on.
            logger.warning(
                "flush_deferred_notes failed before the queue drain for slot %s",
                slot.key,
                exc_info=True,
            )

    if not slot._queue:
        return False

    # The promise-only recovery continuation is queue_insert(0)'d at
    # the promising turn's completion, but a Stop, a queued user follow-up, or a
    # late steer can intervene afterward. `_requeue_unconsumed_steers` degrades an
    # unconsumed steer to a queue card at the HEAD in _run_chat's finally BEFORE
    # this drain, pushing the continuation to position 1: the steer dequeues and
    # runs first, and the orphaned continuation would then dispatch the announced
    # action on a LATER drain, when the intervention signal is already gone. So
    # purge every promise-only continuation from the queue UP FRONT whenever ANY
    # user intervention is present — a stop, a pending steer, or any non-synthetic
    # (user-authored) queue item — BEFORE the dequeue, so an orphaned continuation
    # can never survive a turn to dispatch later. No await before the decision =>
    # atomic on the single event loop.
    #
    # Identity is STRUCTURAL, never content alone. Fixed runner-authored
    # continuations require a synthetic payload plus one of their fixed bodies. The
    # dynamic false-blocker replay carries ORIGINAL user text, so its unforgeable
    # dedicated kind is the purge marker. A user pasting either body or choosing
    # the same words carries neither structural marker and is never purged.
    #
    # A Stop pressed AND resolved back to idle in the post-turn awaits (between the
    # continuation's enqueue and this drain) is invisible to `_should_suppress_requeue`
    # / `_stopping` (both snap back to idle), so compare the monotonic stop counter
    # against its value AT ENQUEUE (the continuation record in `slot.replays`): any
    # increment means a Stop happened while the continuation waited, and the
    # announced action must not be dispatched.
    # Same comparison for the session-scoped count: a stop issued on a linked
    # channel surface moves only that one.
    if await _purge_superseded_continuations(state, slot):
        return False

    # The self-queued replays (the model-access swap, the lost-session reconnect,
    # the image-history recovery and the content-filter retry) carry the same
    # hazard on their own records: each was queued at index 0 BEFORE any Stop or
    # correction that landed while it waited, and the awaits that follow some of
    # their enqueues (a session reset, a conversation discard) are exactly the
    # window a soft Stop lands in, one that preserves the queue and leaves
    # `_stopping` back at idle. A mid-episode session rebind is not among the
    # stop/user-input signals the continuation guard above gates on, so this
    # runs at the top level of the drain. Each is identified by its queue id --
    # the content is the user's own words or a recovery prompt, never a fixed
    # text -- and dropped when the ledger revokes it.
    if await _drop_revoked_replays(state, slot):
        return False

    from kiro_crew.shared_crew.admission_entry import queued_work_ids
    from kiro_crew.shared_crew.admission_gate import installed_service

    service = installed_service()
    if service is not None:
        for item in list(slot._queue):
            work_ids = queued_work_ids([item])
            if work_ids and await service._cancelled_parked(work_ids) is not None:
                slot.queue_remove_by_id(item["id"])
                state.broadcast_ws("queue_pop", {"slot": slot.key, "queue_id": item["id"]})

    try:
        merge = KiroCrewConfig.load().dashboard.merge_queued_messages
    except Exception:
        logger.warning(
            "Failed to load config; falling back to sequential dequeue",
            exc_info=True,
        )
        merge = False

    from kiro_crew.dashboard import responsive_chat

    priority_id = responsive_chat.first_priority_id(slot)
    if priority_id and (slot._stopping or slot._stop_state != "idle"):
        return False
    # Picked here rather than asked for by a card: the person's waiting sends
    # are one conversation, so a leading run of them drains as one turn.
    auto_priority = bool(priority_id and not required_queue_id)
    if priority_id and not required_queue_id:
        required_queue_id = priority_id
        allow_user_during_subagents = True

    hold_users = bool(
        not allow_user_during_subagents
        and subagents_hold_user_messages(state, effective_session_key(slot))
    )
    restored_ids = _RESTORED_DRAIN_IDS.get().get(slot.key)
    if restored_ids is not None and not required_queue_id and not hold_users:
        required_queue_id = next(
            (
                item["id"]
                for item in slot._queue
                if not item.get(RESTORED_QUEUE_KEY) or item["id"] in restored_ids
            ),
            None,
        )
        if required_queue_id is None:
            return False

    if required_queue_id and hold_users:
        # The hold dequeues only system entries, so it could start a different
        # entry than the one card asked for. That card stays queued instead.
        return False
    if required_queue_id and not slot.queue_promote_by_id(required_queue_id):
        return False
    if hold_users:
        # Sub-agent completions / recovery still flow while user messages wait.
        next_msg, consumed = _dequeue_next_system_message(slot)
    elif auto_priority and (run := responsive_chat.dequeue_waiting_run(slot)) is not None:
        next_msg, consumed = run
    else:
        # ``required_queue_id`` is an explicit request to run exactly one card.
        # Promotion selects it; merging here would consume unrelated queued user
        # prompts in the same turn and falsely acknowledge work the user did not
        # choose.
        # Restored entries have no trustworthy actor. Drain sequentially while
        # any remain, so their title exclusion never swallows fresh user words
        # and a fresh row never launders restored text into a title prompt.
        next_msg, consumed = _dequeue_next_message(
            slot,
            merge_enabled=merge
            and not required_queue_id
            and not any(item.get(RESTORED_QUEUE_KEY) for item in slot._queue),
        )
    if next_msg is None:
        return False
    _record_queued_crew_steers(state, consumed)

    # A successor turn is now certain to dispatch (every no-successor path above
    # already returned False), so the predecessor turn's assistant bubble must be
    # finalized on the clients NOW, before the successor's row and first chunk
    # reach them. The end-of-turn flush suppresses ``chat_segment``
    # (``broadcast=False``), deferring the finalize to the ``chat_done`` that
    # ``_finish_queue_cycle`` emits -- but on this path no ``chat_done`` follows.
    # The flush's ``slot.append`` does emit a ``chat_message{role:assistant}``
    # frame whose reducer branch also finalizes, but that frame is CONDITIONAL:
    # suppressed while an HTTP SSE reader drains the slot (``_has_reader``),
    # absent when the turn's final segment is empty (text already flushed at a
    # tool boundary), and droppable client-side by the mid-keyed redelivery
    # guard. Without an unconditional finalize the successor's chunks append
    # into the still-open ``streaming`` row: two turns render as one bubble,
    # and a line-final ``[OPTIONS: ...]`` marker in the first turn loses its
    # end-of-line anchor and degrades to prose. ``chat_segment`` is that
    # unconditional finalize, and it is idempotent on the reducer (no live
    # ``streaming`` row -> no-op), so clients that already finalized are
    # unaffected. The queue-empty and dropped-entry paths keep
    # ``_finish_queue_cycle``'s ``chat_done`` as their sole finalizer -- no
    # double finalize on any path.
    state.broadcast_ws("chat_segment", {"slot": slot.key})

    is_recovery = any(is_synthetic_recovery_item(item) for item in consumed)
    # Orthogonal to `is_recovery`, which decides how the row renders: this decides
    # whether the runner may mirror the text to a linked thread as user speech.
    # They diverge on a recovery that replays the user's own message.
    synthetic_payload = any(is_synthetic_payload_item(item) for item in consumed)
    is_system_injection = any(is_system_injection_item(item) for item in consumed)
    # Before the successor writes its first row, unless the drain's top already
    # marked it. A recovery the ending turn queued for itself is the same turn
    # retried, so its reply belongs before the boundary.
    if not turn_ended and not _is_own_recovery(consumed, predecessor_actor, predecessor_turn_id):
        _mark_turn_end(slot)
    directive_user_origin = bool(consumed) and all(
        item.get("_directive_user_origin") is True for item in consumed
    )
    # Channel authority is the narrower credential boundary. If batching combines
    # channel and dashboard entries, the whole turn must retain that boundary so a
    # directive derived from either message cannot inherit dashboard-owner secrets.
    directive_channel_origin = bool(consumed) and any(
        item.get("_directive_channel_origin") is True for item in consumed
    )
    if slot._stopping and not is_system_injection:
        slot.append(
            "error",
            "⟳ Session reset — processing next message with conversation history",
            "msg msg-err",
        )
        slot._stopping = False

    # The session's own human's queued text is delivered AS TYPED, the same rule
    # an ordinary idle send and a steer already follow: a `role == "user"` row is
    # stored and served unredacted (chat_persistence), and here the drained entry
    # becomes BOTH that row and the turn's LLM input, so redacting it would strip
    # a link the human pasted from the model that a straight send would receive.
    # `queue_entry_is_user_origin` is the exact discriminator the pending card
    # uses: user-stamped, not channel-stamped, and carrying no
    # producer `kind` -- so a cron / subagent / MCP-app / peer / disk-restored
    # entry (none of them the reader's own words) never reaches this arm and is
    # redacted as before. A merge folds only user sends together, so requiring
    # EVERY consumed entry to qualify keeps a mixed batch on the redacted path.
    deliver_as_typed = bool(consumed) and all(queue_entry_is_user_origin(item) for item in consumed)
    if not deliver_as_typed:
        next_msg, _ = redact_exfiltration_urls(next_msg)
        next_msg, _ = redact_credentials(next_msg)
    # An entry an app sent is never a cron or sub-agent event, whatever its text
    # opens with: the actor comes from the enqueue-time kind or stamp, the same
    # structural source the MCP-App case below reads. Nor is a restored entry:
    # only plain prompts persist (`slot_queue_repository._is_durable_queue_entry`),
    # and the restore drops the actor stamp, so the text is all that is left and
    # it cannot name an event.
    _not_an_event = (
        _actor_for_queue_items(consumed) == "app"
        or any(item.get(RESTORED_QUEUE_KEY) for item in consumed)
        or any(responsive_chat.priority_entry(item) for item in consumed)
    )
    is_cron = not _not_an_event and next_msg.startswith(CRON_NOTIFY_PREFIX)
    is_subagent = not _not_an_event and next_msg.startswith(SUBAGENT_COMPLETION_PREFIXES)
    # STRUCTURAL, not prefix: an MCP-App message's text is app-authored, so
    # deriving its row from the text would let (and did let) it fall through to
    # role "user" — persisted and broadcast as human speech — while the
    # idle-slot twin writes an `inject` row with `injectKind: mcp_app`. The
    # enqueue-time kind is the unforgeable source (a user typing the banner
    # text has no kind tag and still correctly drains as user speech). An app
    # message never merges (it is a system-injection kind, so the merge stops
    # at it), so `consumed` holds it alone.
    is_app_message = any(item.get("kind") == MCP_APP_MESSAGE_KIND for item in consumed)
    # A re-routed automation refusal (``session_directive_apply``): structural
    # kind, like the app message above, so it drains as an ``inject`` row the
    # crewmate's turn carries rather than a ``user`` row shown as the person's
    # own bubble. Never merges (a system-injection kind), so ``consumed`` holds
    # it alone.
    is_automation_notice = any(item.get("kind") == AUTOMATION_NOTICE_KIND for item in consumed)
    # A requeued automation steer (a work-ledger wake the turn never took): its
    # entry names the `meta.nudge` its row carries, so it drains as the same
    # nudge row it was steered as, never as the person's `user` row. Never
    # merges (`_dequeue_next_message` stops at it), so `consumed` holds it alone.
    automation_nudge = next(
        (
            (item.get("meta") or {}).get(AUTOMATION_STEER_META)
            for item in consumed
            if isinstance((item.get("meta") or {}).get(AUTOMATION_STEER_META), dict)
        ),
        None,
    )
    is_automation_steer = automation_nudge is not None
    if not (
        is_cron
        or is_subagent
        or is_recovery
        or is_app_message
        or is_automation_notice
        or is_automation_steer
    ):
        # A user message after the last completion takes over from the armed
        # synthesis and ends its batch, so its count starts over. Only the
        # caller that read the fire gate as `clear` with the floor held says so
        # (`_hand_off_queue`, `_run_pending_synthesis`); every other drain
        # keeps the count. A `held` or `unknown` verdict means a child may
        # still be out -- running, delivering, pending in memory, or queued in
        # the store -- and that batch's later results still earn the synthesis
        # they would without the message. A stale count costs at most one extra
        # synthesis; clearing a live batch's count loses its one.
        if end_batch:
            slot._synthesis_completion_turns = 0
        slot._pending_synthesis = False
        slot._synthesis_rechecks = 0

    for item in consumed:
        content, _ = redact_exfiltration_urls(item["content"])
        content, _ = redact_credentials(content)
        # The client rebuilds this entry as a user row from the frame alone (no
        # `chat_message` echo follows for a user row), so the attachment lists
        # the entry carries travel with it -- without them the rebuilt row
        # resolves `[attached_file N]` markers by whitespace and a spaced path
        # is truncated until the next reload. The text follows the row this
        # drain writes from `next_msg`: as typed when this is the session's own
        # human's send (`deliver_as_typed`, matching the pending card and the
        # row/input the turn receives), redacted otherwise.
        _pop: dict = {
            "slot": slot.key,
            "content": queued_text_for_display(
                item["content"] if deliver_as_typed else content,
                user_origin=deliver_as_typed,
            ),
            "queue_id": item["id"],
            # The DRAIN's own verdict rides the pop: True when this drain
            # writes its own row for the turn these entries become (inject /
            # subagent), so the reducer must NOT rebuild the popped entry as
            # a user row — doing so shows the text twice, once attributed to
            # the human. Computed here from the same classification the row
            # write uses, so the client cannot drift from the server (an
            # EDITED cron card whose text lacks the cron prefix drains as
            # a real user row, and this flag correctly says "rebuild").
            "drain_writes_row": is_cron
            or is_subagent
            or is_recovery
            or is_app_message
            or is_automation_notice
            or is_automation_steer
            or responsive_chat.reusable_row(slot, consumed, next_msg) is not None,
        }
        # The client rebuilds the drained entry as a user row from this frame
        # alone (no `chat_message` echo follows for a user row), so the lists
        # AND the quote ride it: without the quote the rebuilt row shows the
        # blockquote as text until a reload.
        _pop_meta = {
            **attachment_meta(item.get("meta")),
            **quote_meta(item.get("meta"), user_origin=queue_entry_is_user_origin(item)),
        }
        from kiro_crew.dashboard.chat_delivery import normalize_send_id

        _pop_send_id = normalize_send_id((item.get("meta") or {}).get("sendId"))
        if _pop_send_id:
            _pop_meta["sendId"] = _pop_send_id
        if _pop_meta:
            _pop["meta"] = _pop_meta
        state.broadcast_ws("queue_pop", _pop)
        _remove_queued_by_id(slot.messages, item["id"])

    match = CRON_NOTIFY_RE.match(next_msg) if is_cron else None
    cron_label = match.group(1) if match else "cron"
    cron_label, _ = redact_exfiltration_urls(cron_label)
    cron_label, _ = redact_credentials(cron_label)
    # The app label for a drained MCP-App message: read from the meta the
    # producer stamped on the entry — the banner is written in one place and
    # parsed in none (display only; classification stayed structural above).
    app_label = ""
    if is_app_message:
        for item in consumed:
            if item.get("kind") == MCP_APP_MESSAGE_KIND:
                _lbl = (item.get("meta") or {}).get("appLabel")
                if isinstance(_lbl, str):
                    app_label = _lbl
                break
        app_label, _ = redact_exfiltration_urls(app_label)
        app_label, _ = redact_credentials(app_label)
    if is_automation_steer:
        row_role = "nudge"
    elif is_subagent:
        row_role = "subagent"
    elif is_cron or is_recovery or is_app_message or is_automation_notice:
        row_role = "inject"
    else:
        row_role = "user"
    if is_automation_steer:
        row_cls = "msg msg-nudge"
    elif is_cron:
        # A cron row's `cls` slot carries a JSON payload, not a CSS class name:
        # `cronLabel` is structured data the frontend reads off the row.
        row_cls = json.dumps({"cronLabel": cron_label})
    elif is_app_message:
        # The ONE row shape both delivery paths share (`app_inject_row`):
        # plain CSS cls, identity in meta — a JSON cls would make
        # `_prepare_messages` replace the stored meta on HTTP rebuild,
        # dropping `injectKind` and the row's collapse/fold exemptions.
        row_cls = app_inject_row(app_label or "app")[1]
    elif is_recovery or is_automation_notice:
        row_cls = "msg msg-inject"
    else:
        row_cls = "msg msg-u"
    # Provenance a producer attached to the queue entry belongs on the row the
    # drain writes, not only on the entry that is about to disappear.
    _drained_meta: dict = {}
    # Delivery ids ACCUMULATE; everything else is last-writer-wins. A merge folds
    # several queued messages into one row, and each may carry its own steer's id —
    # a plain `update` would keep only the last, and every other caller would see
    # no row for its delivery and append a duplicate. The row stands for all of
    # them, so it has to name all of them.
    #
    # Accumulating generally does not undo the narrow rule above: per-entry
    # subagent facts would be meaningless on a merged row, but a subagent
    # completion never merges (it drains alone and breaks any user-message
    # merge), so a merged row cannot carry them in the first place.
    _drained_ids: list[str] = []
    # Client send-correlation ids accumulate for the same reason: a merged row
    # stands for every queued send folded into it, and a sender proving its own
    # delivery by identity must be able to find its id on that row even when it
    # was not the last writer. The single-entry case (the overwhelmingly common
    # one) keeps the plain-send shape -- `sendId` alone -- so a client reading
    # only that key sees exactly what a dispatched send's row carries.
    _drained_send_ids: list[str] = []
    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard.session_control import (
        CHANNEL_RECIPIENT_META_KEY,
        QUEUED_CONTAINMENT_META_KEY,
        SEND_ORIGIN_META_KEY,
        audit_queued_allow,
    )

    # The ALLOW side of the drain's permission decision: these entries
    # passed re-validation and are now becoming a turn. Audited at consumption —
    # not per sweep pass — so an entry that waits across several drains yields
    # one row when it actually executes. Exempt kinds were never subject to the
    # decision, so they are not counted as one.
    _revalidated_ids = [
        item["id"]
        for item in consumed
        if item.get("kind") not in (CRON_NOTIFICATION_KIND, SUBAGENT_COMPLETION_KIND)
    ]
    if _revalidated_ids:
        audit_queued_allow(slot, _revalidated_ids)

    for item in consumed:
        _item_meta = item.get("meta")
        if isinstance(_item_meta, dict):
            _one = _item_meta.get("steer_delivery_id")
            if isinstance(_one, str) and _one:
                _drained_ids.append(_one)
            _many = _item_meta.get("steer_delivery_ids")
            if isinstance(_many, list):
                _drained_ids.extend(x for x in _many if isinstance(x, str) and x)
            _sid = _item_meta.get("sendId")
            if isinstance(_sid, str) and _sid and _sid not in _drained_send_ids:
                _drained_send_ids.append(_sid)
            # The admission-time containment snapshot is queue plumbing,
            # consumed by _drop_stale_admissions above; it says nothing about the
            # ROW, so it must not ride into the persisted transcript meta.
            _drained_meta.update(
                (k, v) for k, v in _item_meta.items() if k != QUEUED_CONTAINMENT_META_KEY
            )
    # A drop-notice address names the one sender whose text the notice quotes.
    # A merged row's text is every entry's, so the row keeps an address only
    # when every consumed entry carries that same address; otherwise the union's
    # last writer would be quoted another sender's words.
    for _address_key in (CHANNEL_RECIPIENT_META_KEY, SEND_ORIGIN_META_KEY, "author"):
        _addresses = [
            (
                (item.get("meta") or {}).get(_address_key)
                if isinstance(item.get("meta"), dict)
                else None
            )
            for item in consumed
        ]
        if any(_address != _addresses[0] for _address in _addresses):
            _drained_meta.pop(_address_key, None)
    # Model input only: the row keeps the user's text as typed.
    _possibly_delivered_steer = bool(_drained_meta.pop(STEER_POSSIBLY_DELIVERED_META, False))
    # Queue plumbing for the sub-agent hold only; says nothing about the row.
    _drained_meta.pop(STEER_INTENT_META, None)
    # Queue plumbing too: the row says it as `meta.nudge`, the key the chip reads.
    _drained_meta.pop(AUTOMATION_STEER_META, None)
    if automation_nudge is not None:
        _drained_meta["nudge"] = dict(automation_nudge)
    # Read off the consumed entries (``_batch_recovery_echo_owed``); the merged
    # row's union says nothing about each entry, so it does not ride along.
    _drained_meta.pop(MIRROR_ECHO_OWED_META_KEY, None)
    # Queue plumbing like the steer mark above, read the way the channel origin is:
    # ANY consumed entry a channel hand-off stamped keeps the merged text content,
    # so a batch cannot smuggle a channel's "/clear" into a command by sitting
    # behind a dashboard entry. Popped so it does not ride into the row's meta.
    _commands_off = bool(_drained_meta.pop(COMMANDS_OFF_META_KEY, False)) or any(
        (item.get("meta") or {}).get(COMMANDS_OFF_META_KEY) is True for item in consumed
    )
    if "quote" in _drained_meta:
        # The row's quote record follows the row's text: as typed only when
        # every consumed entry is the human's own (`deliver_as_typed`), else
        # redacted -- a restored entry carries no origin stamp and the user-row
        # emit path keeps `meta.quote` as stored, so this is where its form
        # is decided. A record the bound refuses is dropped whole.
        _drained_meta.update(
            quote_meta({"quote": _drained_meta.pop("quote")}, user_origin=deliver_as_typed)
        )
    if _drained_ids:
        _drained_meta.pop("steer_delivery_id", None)
        _drained_meta["steer_delivery_ids"] = _drained_ids
    if len(_drained_send_ids) > 1:
        # A merged row: `sendId` stays as the union left it (last writer) so the
        # key is never absent when any entry had one, and `sendIds` names every
        # send the row stands for. Membership in `sendIds` is the proof a client
        # should read on a merged row; on a plain row `sendId` is the whole story.
        _drained_meta["sendIds"] = _drained_send_ids
    # Durable provenance for every `inject` row. `cls` is NOT persisted for this
    # role (chat_persistence only keeps it for `role == "system"`), and the
    # frontend's `meta.cronLabel` exists on the wire only because parse_cls_meta
    # synthesizes it at emit time — so anything keyed on it silently disappears
    # after a flush + rehydrate. `meta` IS persisted and restored, so the render
    # side can ask what a row IS instead of guessing from what its text is not.
    #
    # The recovery split matters: build_recovery_requeue replays the USER'S OWN
    # message verbatim when the turn emitted nothing, and that row must keep
    # rendering as speech. `synthetic_payload` is the existing answer to exactly
    # that question, so reuse it rather than inventing a second signal.
    #
    # Folded into the drained meta rather than a separate `_row_meta`: this drain
    # now unions the meta of EVERY consumed entry (a merged row names all of its
    # steer delivery ids), so the drained mapping is the one the row write reads.
    # An `inject` row and a `subagent` row are mutually exclusive by `row_role`,
    # so these two provenance blocks can never both fire on one row.
    if row_role == "inject":
        if is_cron:
            _inject_kind = "cron"
        elif is_app_message:
            # Before the synthetic_payload arm: an app entry IS a synthetic
            # payload (that is what suppresses the channel mirror), but its
            # inject identity is its own, matching the direct-dispatch twin.
            _inject_kind = "mcp_app"
        elif is_automation_notice:
            _inject_kind = "automation_notice"
        elif synthetic_payload:
            _inject_kind = "recovery"
        else:
            _inject_kind = "user_replay"
        _inject_meta: dict = {"injectKind": _inject_kind}
        if is_cron:
            _inject_meta["cronLabel"] = cron_label
        elif is_app_message:
            _inject_meta["appLabel"] = app_label or "app"
        _drained_meta.update(_inject_meta)
    elif row_role == "user" and directive_user_origin:
        # The direct-send path stamps HUMAN_TURN_META_KEY on a user row whenever
        # the send is human-authored (chat_handlers: ``if not request_app``). A
        # message that arrives while the slot is busy is QUEUED and later drained
        # here instead, so without this it would write an UNMARKED user row and
        # the last-human-turn ranking gate (chat_persistence, keyed on this
        # marker) would skip it, staling ``last_user_at`` for a genuine human
        # turn. ``directive_user_origin`` is that same ``not request_app`` signal
        # carried on the queue entry (see ``_directive_user_origin``), so stamp
        # it here on exactly the human-origin drain. It fails closed: a drain
        # with any non-human (app) entry, or the ``inject`` branch above, never
        # reaches here and stays unmarked.
        _drained_meta[HUMAN_TURN_META_KEY] = True
    if row_role == "user" and any(item.get(RESTORED_QUEUE_KEY) for item in consumed):
        # This is a loss-of-provenance marker, never authority recovered from disk.
        # Persist it on the row so auto-titling and refresh rebase both exclude it.
        _drained_meta[RESTORED_TURN_META_KEY] = True
    current_row = responsive_chat.reusable_row(slot, consumed, next_msg)
    if current_row is None:
        if is_automation_steer:
            current_row = append_and_surface(
                state, slot, row_role, next_msg, row_cls, meta=_drained_meta or None
            )
        else:
            current_row = slot.append(
                row_role,
                next_msg,
                row_cls,
                meta=_drained_meta or None,
            )

    # Per-turn consumption cell for a drained sub-agent completion (see
    # _arm_queued_delivery_settlement): owned by THIS turn, so a successor turn
    # started by this one's tail-drain cannot reset it. The hook is passed ONLY
    # for a completion row, so every other row's turn is dispatched exactly as
    # before.
    #
    # Settleable rows are selected by their STRUCTURAL kind, never by the text
    # prefix ``is_subagent`` reads: the delivery ledger is content-keyed, so a row
    # whose text merely LOOKS like an announce (a user pasting one back) would
    # otherwise claim the genuine row's debt and start its retention clock early.
    # ``chat_utils.is_system_injection_item`` documents the kind tag as the
    # unforgeable classifier for exactly this reason -- a user-typed row cannot
    # carry one. Recovery rows are included because a completion that failed before
    # the model consumed it is re-queued verbatim under that kind.
    _consumed: list[bool] = [False]
    _settleable = [
        item["content"] for item in consumed if item.get("kind") in SUBAGENT_DELIVERY_KINDS
    ]

    if _settleable and not slot.owes_subagent_delivery(_settleable):
        # Owes nothing (every ordinary recovery replay, and any completion whose
        # debt was already settled): dispatch this row unchanged.
        _settleable = []

    _delivery_callbacks = [
        callback for item in consumed if callable(callback := item.get("_on_consumed"))
    ]
    _irreversible_delivery_callbacks = [
        callback for item in consumed if callable(callback := item.get("_on_irreversibly_consumed"))
    ]

    def _note_consumed(consumed: bool = True) -> None:
        # False is a RETRACTION: the runner re-queued this exact announce verbatim
        # (first empty response), so the delivery that counts has not happened yet.
        _consumed[0] = consumed
        for callback in _delivery_callbacks:
            callback(consumed)

    async def _note_irreversibly_consumed() -> None:
        for callback in _irreversible_delivery_callbacks:
            result = callback()
            if inspect.isawaitable(result):
                await result

    _run_kwargs: dict[str, Any] = {
        "_current_message": current_row,
        "_synthetic_payload": synthetic_payload,
        "_directive_user_origin": directive_user_origin,
        "_directive_channel_origin": directive_channel_origin,
    }
    if any(responsive_chat.priority_entry(item) for item in consumed):
        _run_kwargs["_responsive_human"] = True
        _run_kwargs["_responsive_protected"] = any(
            item.get("_responsive_protected") is True for item in consumed
        )
    if _commands_off:
        _run_kwargs["_commands_off"] = True
    # A verbatim replay of a dashboard prompt that died before its mirror echo:
    # set only when every consumed entry carries the stamp, so it is echoed once.
    if _batch_recovery_echo_owed(consumed):
        _run_kwargs["_recovery_echo_owed"] = True
    # Provenance for the session's log, from the enqueue-time ``kind`` tag — the
    # same unforgeable source ``is_system_injection_item`` classifies on, and for
    # the same reason: the banner these injections wrap their text in is something
    # a user can type. Left unset when no consumed entry carries a mapped kind,
    # which the crew log records as a user turn.
    _queue_actor = _actor_for_queue_items(consumed)
    if _queue_actor:
        _run_kwargs["_turn_actor"] = _queue_actor
    # Provenance this process did not establish. A restored entry has no actor by
    # construction (`sanitize_restored_queue` drops the stamp with the directive
    # flags), and an absent actor reads as `user` below -- so without this the one
    # arm that spends on an actor of `user` would take a turn whose author is
    # whoever could write the session file.
    if any(item.get(RESTORED_QUEUE_KEY) for item in consumed):
        _run_kwargs["_turn_provenance_restored"] = True
    if _settleable or _delivery_callbacks:
        _run_kwargs["_on_consumed"] = _note_consumed
    if _irreversible_delivery_callbacks:
        _run_kwargs["_on_irreversibly_consumed"] = _note_irreversibly_consumed
    # The row this turn runs carried its attachment lists through the queue, and this
    # is the site that has the item, so the crew log's ids come from here. Collected
    # across `consumed` because a row that carries attachments drains ALONE
    # (`carries_attachments`), so there is exactly one such row to read.
    _drained_attachment_meta: dict[str, list[str]] = {}
    for item in consumed:
        for _meta_key, _meta_paths in attachment_meta(item.get("meta")).items():
            _drained_attachment_meta.setdefault(_meta_key, []).extend(_meta_paths)
    _drained_attachments = [path for paths in _drained_attachment_meta.values() for path in paths]
    if _drained_attachments:
        _run_kwargs["_attachments"] = _drained_attachments
        # The typed form rides along so the refusal replay can rebuild the
        # entry's meta without retyping ``dirs`` entries as files.
        _run_kwargs["_attachment_meta"] = _drained_attachment_meta
    # Replay identity rides as a parameter, matched by queue-entry id at the one
    # site that still has the entry. The replay must have drained ALONE: a merge
    # folding user input into the same dispatch is a correction, not the retry.
    _claimed_replays = replays_of(slot).claim(consumed)
    if _claimed_replays:
        _run_kwargs["_replay"] = _claimed_replays
    if is_recovery:
        _run_kwargs["_synthetic_recovery_turn"] = True
        if len(consumed) == 1 and consumed[0].get(_REPLAYS_COMPLETION_KEY) is True:
            _run_kwargs["_replays_completion"] = True
    if _possibly_delivered_steer:
        _run_kwargs["_steer_possibly_delivered"] = True
    # A re-routed automation refusal carries the originating wake's provenance
    # on its (non-durable, live-only) entry meta, so the successor turn it opens
    # inherits `_directive_self_wake` / `_directive_loop_id`. Without it, a
    # crewmate that retries `monitor_stop` from this turn would present an empty
    # wake id, skip the stale-wake guard, and remove a paused loop record a
    # person kept. Read from the live entry only (the entry carries a `kind`, so
    # it never persists), so forged wake authority cannot ride in from disk.
    if is_automation_notice:
        for item in consumed:
            _meta = item.get("meta")
            if isinstance(_meta, dict) and _meta.get(REFUSAL_SELF_WAKE_META_KEY) is True:
                _run_kwargs["_directive_self_wake"] = True
                _loop_id = _meta.get(REFUSAL_WAKE_LOOP_ID_META_KEY)
                if isinstance(_loop_id, str) and _loop_id:
                    _run_kwargs["_directive_loop_id"] = _loop_id
                break
    # Shared Crew: the drained turn is charged to the person who queued it, read
    # from the server-stamped author on the entries. Entries restored from disk
    # or written by different people name no one, so the turn falls to the
    # bounded Unknown or automation budget (shared_crew.turn_gate).
    from kiro_crew.shared_crew.turn_gate import (
        independent_turn,
        initiated_by,
        queued_initiator,
        queued_work,
    )

    with independent_turn(), initiated_by(queued_initiator(consumed)), queued_work(consumed):
        task = spawn_guarded_turn(
            state,
            slot,
            _run_chat(state, slot, next_msg, **_run_kwargs),
        )
    slot.task = task
    # A recovery replay of a Regenerate's message takes over its target now
    # that ``slot.task`` gave it the slot's generation.
    adopt_regenerate_target(slot, consumed, task)
    if _settleable:
        # Open the retention clock on the result files this row promises — but
        # only once the turn has actually run and the model has consumed the
        # prompt, since a "delivered" tombstone is durable and hides the folder
        # from restart recovery. The gateway deliberately left them un-tombstoned
        # while the row waited in the queue, because that clock would otherwise
        # expire before the row was ever consumed. Claimed by the
        # row's CONTENT, which is what a pre-consumption retry re-queues: its
        # queue-entry id is freshly minted and would match no debt.
        _arm_queued_delivery_settlement(state, slot, task, _settleable, _consumed)
    return True
