"""A Bot's own limit on how many of the sessions it runs are open at once.

The owner sets ``max_workers`` on the Bot's settings page; no Bot tool writes it,
and 0 (the default) sets no limit. Every session-control verb that can leave one
more session open as a Bot asks :func:`worker_cap_refusal` with the execution
record the new slot will run under: ``session_create`` (from the Bot's chat, from
one of its workers, or from any session those opened), ``session_revive`` and
``session_fork``. A handoff moves a slot to another manager and opens nothing, so
it is not asked.

A session counts while its slot is live in ``state._slots`` and its key is either
one of the Bot's own workers (``members.crewmate_worker_member_id``) or in
:data:`_BOT_SESSIONS`: every slot one of those verbs opened under a record that
carries the Bot's member id, on any template and as the Bot itself. The Bot's own
thread and the chats a person opens are not minted by those verbs and are not
counted, nor is a session with no member id. Runs a session starts with
``spawn_run`` / ``spawn_sub_agents`` are not counted either; ``agent.max_subagents``
(a spawn past it waits in the queue) and the spawn depth limit bound them. A
finished session keeps its seat until it is closed. A new one is entered at its
mint, so two parallel creates cannot both pass, and the sessions a restart brought
back are entered from their saved records by :func:`count_restored_workers` before
a check.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.state_parts.dashboard_state import DashboardState

logger = logging.getLogger("kiro_crew.dashboard.state")

#: The member id of each slot a session-control verb opened as a Bot, by slot key
#: (``members._slot_part`` spelling). Read only for keys still in ``state._slots``.
_BOT_SESSIONS: dict[str, str] = {}

#: Restored slot keys whose saved record :func:`count_restored_workers` has read.
_RECORDS_READ: set[str] = set()


def own_worker_member(execution: Any) -> str:
    """The Bot's ``member_id`` when *execution* is one of its own workers, else ``""``."""
    from kiro_crew.mate_worker import is_own_worker_execution

    if execution is None or not is_own_worker_execution(execution):
        return ""
    return str(execution.member_id)


def bot_member(execution: Any) -> str:
    """The ``member_id`` *execution* runs as, else ``""``: what takes a Bot's seat."""
    member_id = getattr(execution, "member_id", None) if execution is not None else None
    return member_id if isinstance(member_id, str) else ""


def note_bot_session(slot_key: str, execution: Any) -> None:
    """Count *slot_key* toward the Bot *execution* runs as; no member id drops it."""
    from kiro_crew.members import _slot_part

    member_id = bot_member(execution)
    if member_id:
        _BOT_SESSIONS[_slot_part(slot_key)] = member_id
    else:
        _BOT_SESSIONS.pop(_slot_part(slot_key), None)


def refresh_bot_session(slot_key: str, execution: Any) -> None:
    """Follow an agent switch: a counted *slot_key* takes the seat its new record names.

    A slot not counted stays so: a person's own chat never takes a seat.
    """
    from kiro_crew.members import _slot_part

    if _slot_part(slot_key) in _BOT_SESSIONS:
        note_bot_session(slot_key, execution)


def bot_max_workers(member_id: str) -> int:
    """The ``max_workers`` its owner set for Bot *member_id*; 0 when none or unreadable."""
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.crewmate_harness import crew_for_member_id

    try:
        snap = live.snapshot()
        cfg = snap if snap is not None else KiroCrewConfig.load()
        record = cfg.agents.get(crew_for_member_id(cfg, member_id))
    except Exception:  # noqa: BLE001 -- an unreadable limit never refuses a create
        logger.warning("could not read max_workers for %s", member_id, exc_info=True)
        return 0
    cap = getattr(record, "max_workers", 0)
    return cap if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0 else 0


def bot_worker_cap_refusal(
    state: "DashboardState", member_id: str, cap: int, exclude: str = ""
) -> str | None:
    """Why Bot *member_id* may not open another session under *cap*, or ``None``.

    *exclude* is a slot key left out of the count: a fork's own just-minted child.
    """
    from kiro_crew.members import _slot_part, crewmate_worker_member_id

    if not member_id or cap <= 0:
        return None
    held = sum(
        1
        for key in list(state._slots)
        if (
            crewmate_worker_member_id(key) == member_id
            or _BOT_SESSIONS.get(_slot_part(key)) == member_id
        )
        and _slot_part(key) != _slot_part(exclude)
    )
    if held < cap:
        return None
    return (
        f"this Bot already has {held} sessions open and its owner allows {cap} "
        "at once. A session keeps its seat until it is closed: close one that has "
        "finished (session_close), then try again. Only the owner can change this "
        "limit, on the Bot's settings page."
    )


def worker_cap_refusal(state: "DashboardState", execution: Any, exclude: str = "") -> str | None:
    """Why a slot running *execution* may not open, or ``None`` when it may.

    ``None`` for a record with no member id, and for a Bot with no limit set.
    Synchronous: call it in the window that mints the slot.
    """
    member_id = bot_member(execution)
    if not member_id:
        return None
    return bot_worker_cap_refusal(state, member_id, bot_max_workers(member_id), exclude)


def read_worker_execution(session_key: str) -> Any:
    """*session_key*'s saved execution record, or ``None`` when it cannot be read.

    A record that cannot be decoded is not a counted worker: the cap never turns
    a read error into a refusal or an exception on a verb it guards. Blocking.
    """
    from kiro_crew.execution_context import read_session_execution

    try:
        return read_session_execution(session_key)
    except Exception:  # noqa: BLE001 -- see above
        logger.warning("could not read worker record %s", session_key, exc_info=True)
        return None


async def count_restored_workers(state: "DashboardState") -> None:
    """Enter the slots a restart brought back into their Bots' counts.

    A restored slot is otherwise counted only once a turn reads its execution
    record. Each live slot that is on the worker template, or that a verb opened
    (``_created_by``, as the restore sets it, but not a cron's ``cron:`` stamp,
    which a script cron's chat carries) on a member store, and that is not counted
    yet, has its saved record read off the loop. A record read is not read again;
    an unreadable one leaves only its own slot uncounted and is read again at the
    next pass. Both tables are first pruned to the live keys.
    """
    from kiro_crew.dashboard.chat_utils import slot_history_key
    from kiro_crew.dashboard.slot_persistence.metadata_codec import _CRON_CREATOR_PREFIX
    from kiro_crew.mate_worker import MATE_WORKER_AGENT, note_worker_session
    from kiro_crew.members import _slot_part, crewmate_worker_member_id

    live = list(state._slots.values())
    live_keys = {_slot_part(key) for key in list(state._slots)}
    live_keys.update(_slot_part(slot_history_key(slot)) for slot in live)
    _RECORDS_READ.intersection_update(live_keys)
    for gone in set(_BOT_SESSIONS) - live_keys:
        del _BOT_SESSIONS[gone]
    pending = [
        (key, slot)
        for slot in live
        if not crewmate_worker_member_id(key := slot_history_key(slot))
        and _slot_part(key) not in _BOT_SESSIONS
        and _slot_part(key) not in _RECORDS_READ
        and (
            getattr(slot, "agent", "") == MATE_WORKER_AGENT
            # A member record never sits on the default store (``MemoryStoreRef``).
            or (
                (creator := str(getattr(slot, "_created_by", "") or ""))
                and not creator.startswith(_CRON_CREATOR_PREFIX)
                and bool(getattr(slot, "memory_store", ""))
            )
        )
    ]
    if not pending:
        return

    def _read() -> list[tuple[str, Any, Any]]:
        # One record at a time: an unreadable one leaves only its own slot
        # uncounted, never the healthy workers read beside it.
        return [(key, slot, read_worker_execution(key)) for key, slot in pending]

    records = await asyncio.to_thread(_read)
    # The read ran off the loop, so the table may have moved meanwhile: a fork can
    # have counted its own child, or a slot closed or was made again under its key.
    # Apply a record only to the slot object it was read for.
    open_now = {slot_history_key(slot): slot for slot in list(state._slots.values())}
    for key, slot, execution in records:
        if execution is None or open_now.get(key) is not slot:
            continue
        _RECORDS_READ.add(_slot_part(key))
        if not crewmate_worker_member_id(key) and not note_worker_session(key, execution):
            note_bot_session(key, execution)


def note_new_worker(slot_key: str, execution: Any) -> None:
    """Enter a just-minted slot running *execution* into its Bot's count."""
    from kiro_crew.members import note_crewmate_worker_session

    member_id = own_worker_member(execution)
    if member_id:
        note_crewmate_worker_session(slot_key, member_id)
    note_bot_session(slot_key, execution)
