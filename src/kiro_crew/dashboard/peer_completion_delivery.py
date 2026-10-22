"""Deliver creator-owned dashboard turn completions through the durable slot queue."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping

from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log.projection_kernel import projection_of
from kiro_crew.crew_log.projection_session import fold_session
from kiro_crew.crew_log.session_tree_projection import projection
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.peer_completion import (
    PEER_TURN_COMPLETION_ATTESTED_KEY,
    PEER_TURN_COMPLETION_HELD_KEY,
    PEER_TURN_COMPLETION_KIND,
    PEER_TURN_COMPLETION_META_KEY,
    PEER_TURN_COMPLETION_PREPARING_KEY,
    build_event,
    entry_is_attested,
    event_from_entry,
    prompt_for_event,
)
from kiro_crew.dashboard.slot_queue_repository import (
    MAX_LIVE_QUEUE_ENTRIES,
    durable_queue_entries,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)
_EVENT_ID_SCAN_ROWS = 500
_PEER_WAKE_DEFERRED_KEY = "_peer_completion_wake_deferred"


def _pending_approval(state: "DashboardState", slot: "_ChatSlot") -> bool:
    futures = getattr(slot, "_approval_futures", None) or {}
    try:
        if any(not future.done() for future in futures.values()):
            return True
    except Exception:
        return True
    pending = getattr(state, "pending_coordinator_approvals", None)
    if not callable(pending):
        return False
    try:
        return bool(pending(slot.key))
    except Exception:
        return True


def _event_id_in_rows(slot: "_ChatSlot", identity: str) -> bool:
    for row in slot.messages[-_EVENT_ID_SCAN_ROWS:]:
        event = event_from_entry(row)
        if event is not None and event["event_id"] == identity:
            return True
    return False


def _queued_event(slot: "_ChatSlot", identity: str) -> dict[str, Any] | None:
    for entry in slot._queue:
        event = event_from_entry(entry)
        if event is not None and event["event_id"] == identity:
            return entry
    return None


def pending_child_sessions(creator: "_ChatSlot") -> set[str]:
    """Children with an attested completion still waiting in *creator*'s queue."""
    out: set[str] = set()
    for entry in creator._queue:
        if not entry_is_attested(entry):
            continue
        event = event_from_entry(entry)
        if event is not None:
            out.add(event["child_session"])
    return out


def _turn_is_completed(session_id: str, child_turn_id: int) -> bool | None:
    """Whether the bounded protected outline proves this exact turn completed."""
    try:
        folded = fold_session(session_id, ("outline",))
        outline = projection_of(folded.checkpoints["outline"]).value
    except Exception:
        logger.debug("peer completion could not fold child log %s", session_id, exc_info=True)
        return None
    turns = outline.get("turns") if isinstance(outline, Mapping) else None
    if not isinstance(turns, list):
        return None
    row = next(
        (row for row in turns if isinstance(row, Mapping) and row.get("turn") == child_turn_id),
        None,
    )
    if row is None:
        return None
    return row.get("completed") is True


def attest_event(event: Mapping[str, Any]) -> bool | None:
    """Prove the event's creator and completed turn from protected crew-log state."""
    child = str(event["child_session"])
    child_session_id = str(event["child_session_id"])
    creator = str(event["creator_session"])
    turn = int(event["child_turn_id"])
    try:
        tree = projection()
        if not tree.seeded_for_current_store:
            return None
        reading = tree.reading()
        if reading.incomplete:
            return None
        record = next(
            (
                record
                for record in reading.records
                if record.slot == child and record.sid == child_session_id
            ),
            None,
        )
        if record is None:
            return False
        if tree.trusted_creator(child, child_session_id) != creator:
            return False
    except Exception:
        logger.debug("peer completion could not read session lineage", exc_info=True)
        return None
    return _turn_is_completed(child_session_id, turn)


async def _persist_event(
    state: "DashboardState",
    creator: "_ChatSlot",
    *,
    required_queue_id: str = "",
) -> bool:
    """Persist the queue entry before any creator turn can consume it."""
    history_key = slot_history_key(creator)
    try:
        saved = await save_slot_off_loop(
            state,
            creator,
            force=True,
            best_effort=False,
            expected_history_key=history_key,
            expected_slot_name=creator.key,
        )
    except Exception:
        logger.warning(
            "peer completion for creator %s could not be persisted; leaving it queued",
            creator.key,
            exc_info=True,
        )
        creator._dirty = True
        return False
    if not saved or creator.queue_persist_pending:
        logger.warning(
            "peer completion for creator %s is queued but not durably committed yet",
            creator.key,
        )
        creator._dirty = True
        return False
    if required_queue_id and not any(
        entry.get("id") == required_queue_id for entry in durable_queue_entries(creator._queue)
    ):
        logger.warning(
            "peer completion %s for creator %s was outside durable queue bounds",
            required_queue_id,
            creator.key,
        )
        creator._dirty = True
        return False
    return True


async def _wake_if_idle(state: "DashboardState", creator: "_ChatSlot", queue_id: str) -> bool:
    if creator.running or creator.is_closing or state.get_slot(creator.key) is not creator:
        return False
    memory_task = getattr(state, "memory_startup_task", None)
    if memory_task is not None and not memory_task.done():
        entry = next((item for item in creator._queue if item.get("id") == queue_id), None)
        if entry is None or entry.get(_PEER_WAKE_DEFERRED_KEY) is True:
            return False
        entry[_PEER_WAKE_DEFERRED_KEY] = True

        async def _retry_after_memory() -> None:
            try:
                await memory_task
            except BaseException:
                return
            finally:
                entry.pop(_PEER_WAKE_DEFERRED_KEY, None)
            await _wake_if_idle(state, creator, queue_id)

        retry = asyncio.create_task(_retry_after_memory())
        state._background_tasks.add(retry)
        retry.add_done_callback(state._background_tasks.discard)
        return False
    from kiro_crew.dashboard.chat_runner import _start_next_queued_turn

    return await _start_next_queued_turn(state, creator, required_queue_id=queue_id)


async def _drain_if_idle(state: "DashboardState", creator: "_ChatSlot") -> bool:
    if creator.running or creator.is_closing or state.get_slot(creator.key) is not creator:
        return False
    from kiro_crew.dashboard.chat_runner import _start_next_queued_turn

    return await _start_next_queued_turn(state, creator)


async def enqueue_turn_completed(
    state: "DashboardState",
    child: "_ChatSlot",
    *,
    child_session_id: str,
    child_turn_id: int,
    wake: bool = True,
) -> tuple[str, str] | None:
    """Persist one completion pointer for the attested creator, then optionally wake it.

    ``None`` means no safe creator callback was available.  The child is never
    restarted and no result verdict is inferred here.
    """
    if (
        not child_session_id
        or child_turn_id < 1
        or not str(getattr(child, "_created_by", "") or "")
        or _pending_approval(state, child)
    ):
        return None
    try:
        await asyncio.to_thread(crew_log_emit.flush)
    except Exception:
        logger.debug("peer completion could not settle crew-log writes", exc_info=True)
        return None
    try:
        tree = projection()
        if not tree.seeded_for_current_store:
            return None
        creator_key = tree.trusted_creator(child.key, child_session_id)
    except Exception:
        logger.debug("peer completion creator lookup failed", exc_info=True)
        return None
    if not creator_key or not await asyncio.to_thread(
        _turn_is_completed, child_session_id, child_turn_id
    ):
        return None
    creator = state.get_slot(creator_key)
    if creator is None or creator is child or creator.is_closing:
        return None
    event = build_event(
        creator_session=creator.key,
        child_session=child.key,
        child_session_id=child_session_id,
        child_turn_id=child_turn_id,
        transcript_index=child_turn_id,
        ended_at=datetime.now(timezone.utc).isoformat(),
    )
    identity = event["event_id"]
    if _event_id_in_rows(creator, identity):
        return None
    existing = _queued_event(creator, identity)
    if existing is not None and entry_is_attested(existing):
        queue_id = str(existing.get("id") or "")
        if wake:
            await _wake_if_idle(state, creator, queue_id)
        return creator.key, queue_id
    if existing is not None:
        entry = existing
        queue_id = str(existing.get("id") or "")
    else:
        if len(creator._queue) >= MAX_LIVE_QUEUE_ENTRIES:
            logger.warning(
                "peer completion for creator %s could not enter the full queue",
                creator.key,
            )
            return None
        queue_id = creator.queue_append(
            prompt_for_event(event),
            kind=PEER_TURN_COMPLETION_KIND,
            meta={PEER_TURN_COMPLETION_META_KEY: event},
        )
        queued_entry = _queued_event(creator, identity)
        if queued_entry is None:
            return None
        entry = queued_entry
    entry.pop(PEER_TURN_COMPLETION_ATTESTED_KEY, None)
    entry[PEER_TURN_COMPLETION_PREPARING_KEY] = True
    persisted = False
    try:
        persisted = await _persist_event(state, creator, required_queue_id=queue_id)
    except asyncio.CancelledError:
        creator.queue_remove_by_id(queue_id)
        if creator._queue:
            await _drain_if_idle(state, creator)
        raise
    finally:
        entry.pop(PEER_TURN_COMPLETION_PREPARING_KEY, None)
    if not persisted:
        creator.queue_remove_by_id(queue_id)
        if creator._queue:
            await _drain_if_idle(state, creator)
        return None
    entry[PEER_TURN_COMPLETION_ATTESTED_KEY] = True
    state.push_slots_update()
    if wake:
        await _wake_if_idle(state, creator, queue_id)
    return creator.key, queue_id


async def wake_prepared(state: "DashboardState", prepared: tuple[str, str] | None) -> bool:
    """Wake the exact durable callback prepared before the child became idle."""
    if prepared is None:
        return False
    creator_key, queue_id = prepared
    creator = state.get_slot(creator_key)
    if creator is None:
        return False
    entry = next((item for item in creator._queue if item.get("id") == queue_id), None)
    if not entry_is_attested(entry):
        return False
    oldest = next(
        (str(item.get("id") or "") for item in creator._queue if entry_is_attested(item)),
        "",
    )
    return bool(oldest) and await _wake_if_idle(state, creator, oldest)


async def recover_pending(state: "DashboardState") -> int:
    """Re-attest restored events and wake each idle creator at most once."""
    pending = [
        entry
        for creator in state._slots.values()
        for entry in creator._queue
        if entry.get("kind") == PEER_TURN_COMPLETION_KIND
    ]
    if not pending:
        return 0
    for entry in pending:
        entry[PEER_TURN_COMPLETION_HELD_KEY] = True
    try:
        tree = projection()
        await asyncio.to_thread(tree.ensure_seeded)
        if not tree.seeded_for_current_store:
            return 0
    except Exception:
        logger.warning("peer completion recovery could not seed session lineage", exc_info=True)
        return 0

    recovered = 0
    for creator in list(state._slots.values()):
        invalid: list[str] = []
        ready: list[str] = []
        for entry in list(creator._queue):
            if entry.get("kind") != PEER_TURN_COMPLETION_KIND:
                continue
            event = event_from_entry(entry)
            queue_id = str(entry.get("id") or "")
            if event is None or event["creator_session"] != creator.key:
                invalid.append(queue_id)
                continue
            if _event_id_in_rows(creator, event["event_id"]):
                invalid.append(queue_id)
                continue
            attested = await asyncio.to_thread(attest_event, event)
            if attested is True:
                entry.pop(PEER_TURN_COMPLETION_HELD_KEY, None)
                entry[PEER_TURN_COMPLETION_ATTESTED_KEY] = True
                ready.append(queue_id)
            elif attested is False:
                invalid.append(queue_id)
            else:
                entry[PEER_TURN_COMPLETION_HELD_KEY] = True
        for queue_id in invalid:
            if queue_id:
                creator.queue_remove_by_id(queue_id)
        if invalid:
            await _persist_event(state, creator)
        if ready and not creator.running:
            if await _wake_if_idle(state, creator, ready[0]):
                recovered += 1
    if recovered:
        logger.info(
            "Recovered %d pending peer session turn completion callback(s)",
            recovered,
        )
    return recovered
