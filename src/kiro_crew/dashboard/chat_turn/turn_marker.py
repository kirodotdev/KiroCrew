"""The in-flight turn marker a dashboard turn writes so a restart can offer Resume."""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.slot_queue_repository import ALL_ATTACHMENT_META_KEYS

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        DashboardState,
        _ChatSlot,
        _is_turn_inject,
        local_turn_prompt_within_bounds,
        logger,
        register_guarded_history_write,
        run_to_completion,
        save_slot_off_loop,
        shutdown_event,
        slot_history_key,
    )


def _local_turn_generation_for(slot: _ChatSlot) -> int:
    """The generation the marker for this dispatch is written under.

    ``slot.task`` is installed before ``_run_chat`` gets its first loop step, so
    ``_turn_generation`` already names this exact dispatch. Read into a local
    BEFORE the marker save is awaited: a cancellation landing inside that await
    (a tab close) must still leave the finally a generation it can retire, or
    the save commits after the cancel and nothing ever clears it.
    """
    return max(1, slot._turn_generation)


# Keys of the opening row copied into the marker: the row's identity, its
# attachment lists -- every list of ``ALL_ATTACHMENT_META_KEYS``, ``images``
# included, since the picture a send attached rides ``meta.images`` and nothing
# else, and a restored opener without it is a row whose later regenerate or
# edit-resend cannot replay the image -- and, for an inject, the kind that
# makes the classifier count it as a turn opener. Everything else about the row
# is recomputed by ``_ChatSlot.append`` or belongs to the process that wrote
# it. Mirrors ``chat_persistence._LOCAL_TURN_PROMPT_META_KEYS``.
_LOCAL_TURN_PROMPT_META_KEYS = ("mid", *ALL_ATTACHMENT_META_KEYS, "injectKind")


#: Roles whose row opens a turn by itself. ``inject`` opens one only with a
#: dispatching ``injectKind`` (``_is_turn_inject``). Mirrors
#: ``_LOCAL_TURN_PROMPT_ROLES`` in ``slot_persistence/turn_marker.py`` minus ``inject``. A
#: drained sub-agent completion opens its own turn too; without its copy a crash
#: before the flush loses the row, and the interrupted-turn restore would walk
#: past it to the previous, already-answered request.
_LOCAL_TURN_OPENER_ROLES = frozenset({"user", "nudge", "subagent"})


def _local_turn_opening_row(slot: _ChatSlot) -> "dict[str, Any] | None":
    """A durable copy of the row that opened the turn being admitted.

    Walks back from the window tail to the newest row that opens a turn: a
    ``user`` row, a ``nudge`` row (a monitor loop's cycle, which always
    dispatches) or a dispatching ``inject`` (``_is_turn_inject``). Stops at
    the first conversational assistant row, since a turn whose opener already
    has an answer is not the one being admitted. Returns ``None`` when the
    window holds no such row (a slot whose opener was consumed by an earlier
    save-and-trim, or a test stub with an empty window).

    The copy carries what ``_ChatSlot.append`` needs to re-create the row as
    the loader would: role, content, the ordering ``ts`` and the identity
    ``mid`` plus the attachment lists. It does NOT carry the
    generation, the turn actor or any directive flag -- the restored row is a
    transcript row again, not an admission. A copy outside
    ``local_turn_prompt_within_bounds`` (serialized size, attachment count,
    per-field length) is not carried at all.
    """
    for row in reversed(slot.messages):
        role = row.get("role")
        if role == "assistant" and row.get("content"):
            return None
        meta = row.get("meta")
        if role in _LOCAL_TURN_OPENER_ROLES or (role == "inject" and _is_turn_inject(meta)):
            kept_meta = {
                key: meta[key]
                for key in _LOCAL_TURN_PROMPT_META_KEYS
                if isinstance(meta, dict) and key in meta
            }
            # No ``cls``: the transcript never persists one for a ``user`` or
            # ``inject`` row, and a cron inject's in-memory ``cls`` is JSON that
            # the emit path would parse into ``meta`` over the row's real
            # ``mid`` / ``injectKind``. The restore assigns the loader's default.
            copy = {
                "role": role,
                "content": str(row.get("content") or ""),
                "ts": str(row.get("ts") or ""),
                "meta": kept_meta,
            }
            # Bounded as a whole, never truncated: a shortened copy would come
            # back as the user's own transcript row. Over the bound, the
            # generation alone marks the turn.
            return copy if local_turn_prompt_within_bounds(copy) else None
    return None


async def _begin_local_turn_marker(state: DashboardState, slot: _ChatSlot, generation: int) -> None:
    """Persist this turn's generation on the slot's metadata line before dispatch.

    The transcript cannot record a process death: a turn cut by a force exit
    leaves partial assistant prose and finished tool rows, the same shape as a
    clean answer, and the restore heuristic reads it as finished. Writing the
    generation BEFORE any provider work begins means no output can outrun it,
    and every restore path turns a leftover value into the interruption row.

    A metadata-only merge, not a window save: the rows stay with the periodic
    flush, so admitting a turn changes nothing about what the prompt builder
    reads off disk. Only a transcript that does not exist yet (a newborn slot's
    first turn) takes the full forced save, which is the one writer that can
    create it. Both writes are awaited to completion under cancellation, however
    many times it is delivered (``run_to_completion``: a graceful shutdown
    escalating after its timeout cancels the runner more than once), so the
    teardown clear that follows a cancelled turn is always ordered after them.
    Best-effort like the queue's own durable copy: a
    failure leaves the value in memory for the next save and never refuses the
    turn -- the marker is a recovery hint, not the user's words.

    Both writes are fenced to THIS slot incarnation. A tab closed mid-turn and
    reopened under the same key (``close_slot`` pops the slot, then waits up to
    2 s for the cancelled runner) resumes the same transcript, so the routing
    pin alone cannot tell the two apart; the merge's guard and the forced
    save's ``expected_slot_name`` recheck ``state._slots`` under the owner lock
    and refuse once the map holds a different object, so a cancelled runner
    that outlives its replacement never writes over the replacement's line or
    rebuilds the window over its rows.
    A restricted session (``memory_mode`` other than ``persistent``) gets the
    marker like any other: the transcript save writes every mode's rows and
    records the strictest ``memory_mode`` it can see, so the teardown clear
    lands and the restart reads the marker. The prompt copy on the metadata
    line adds no exposure the transcript rows do not already have -- every
    reader that learns gates on the line's ``memory_mode``.
    """
    slot._turn_in_flight_generation = generation
    # The opening row travels with the generation. The row itself waits for the
    # periodic flush (an immediate window save would put it on disk before the
    # prompt build, where the builder's recent-context projection would read
    # the current prompt as history), so a death inside that window loses it
    # and the restore would judge the previous turn's tail instead. With the
    # copy on the metadata line the restore puts the row back first.
    slot._turn_in_flight_prompt = _local_turn_opening_row(slot)
    log = state.conversation_log
    if log is None:
        return
    history_key = slot_history_key(slot)
    slot_name = slot.key
    # Both keys every time. The merge cannot delete a key, so a copy the
    # previous turn's clear failed to remove would otherwise stay paired with
    # this generation and come back as this turn's opener; ``None`` is the
    # cleared value the restore reads as "no copy" (same rule as the merge
    # save in ``_save_slot_to_history``).
    marker_fields: dict[str, Any] = {
        "turn_in_flight_generation": generation,
        "turn_in_flight_prompt": slot._turn_in_flight_prompt,
    }

    def _still_this_incarnation(_meta: dict) -> bool:
        return state._slots.get(slot_name) is slot

    # Same fence and same registry as every truncating save. A close raises
    # ``is_closing``, waits for the futures in ``_guarded_history_writes``, and
    # only then pops the name; a write dispatched after the fence has nothing
    # left to order against it, and a write not registered is invisible to the
    # wait, so a close-and-same-key-reopen could land this line onto the
    # replacement's transcript. Read the fence here with no suspension before
    # the registration so the two interleavings are the only ones: fence first
    # and this refuses, or registration first and the retraction waits.
    if getattr(slot, "is_closing", False):
        slot._dirty = True
        return
    merge = asyncio.get_running_loop().run_in_executor(
        None,
        functools.partial(
            log.update_metadata_if,
            history_key,
            marker_fields,
            _still_this_incarnation,
            require_existing=True,
        ),
    )
    register_guarded_history_write(slot, merge)
    try:
        merged = await run_to_completion(merge)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Could not persist the turn marker for slot %s", slot.key, exc_info=True)
        slot._dirty = True
        return
    if not merged and state._slots.get(slot_name) is slot:
        await run_to_completion(
            save_slot_off_loop(
                state,
                slot,
                force=True,
                expected_history_key=history_key,
                expected_slot_name=slot_name,
            )
        )


def _retire_local_turn_marker(slot: _ChatSlot, generation: int) -> bool:
    """Clear the in-memory marker for one turn without touching a successor's.

    Returns whether this call cleared it. A successor can be admitted in the
    same slot before this turn's teardown finishes (the queue drain installs a
    new task with its own generation), so only the generation that wrote the
    marker may retire it.
    """
    if generation <= 0 or slot._turn_in_flight_generation != generation:
        return False
    slot._turn_in_flight_generation = 0
    slot._turn_in_flight_prompt = None
    return True


async def _clear_local_turn_marker(state: DashboardState, slot: _ChatSlot, generation: int) -> None:
    """Durably clear one turn's marker on an exit that wrote no other save.

    The landed path retires the marker in memory right before its own transcript
    save, so the omission rides that write. Every other exit -- an error card,
    a Stop, a recovery re-queue -- reaches here and pays one forced save, since
    a restart before the periodic flush would otherwise read the stale value
    and flag a turn that ended in plain sight as interrupted.
    """
    if not _retire_local_turn_marker(slot, generation):
        return
    # ``best_effort`` re-arms ``_dirty`` on failure so the flush retries the
    # omission; until then a stale on-disk value fails safe to one extra
    # recovery prompt rather than a missed interruption. The identity pins
    # refuse the write when a same-key replacement now owns the transcript
    # (see ``_begin_local_turn_marker``): the replacement's own saves carry
    # its marker state, and the live window is the replacement's, not this one.
    await save_slot_off_loop(
        state,
        slot,
        force=True,
        expected_history_key=slot_history_key(slot),
        expected_slot_name=slot.key,
    )


def _gateway_shutdown_requested(state: DashboardState) -> bool:
    """Whether the process is going away, read without retaining the event.

    A signal shutdown sets ``shutdown_event``. The in-app restart (dashboard
    restart and update apply) never does: it drains with ``close_all()`` and
    re-execs. Both end turns because the process is ending, so both count.
    """
    return shutdown_event.is_set() or state.sessions.final_drain_started is True
