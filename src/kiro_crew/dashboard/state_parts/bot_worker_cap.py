"""A Bot's own limit on how many of its worker sessions are open at once.

The owner sets ``max_workers`` on the Bot's settings page; no Bot tool writes it,
and 0 (the default) sets no limit. Every session-control verb that can leave one
more of a Bot's workers open asks :func:`worker_cap_refusal` with the execution
record the new slot will run under: ``session_create`` (from the Bot's chat or
from one of its workers), ``session_revive`` and ``session_fork``. A handoff
moves a slot to another manager and opens nothing, so it is not asked.

A worker counts while its slot is live in ``state._slots``, by the member id in
``members.crewmate_worker_member_id``: the Bot's own worker sessions, on its
worker template. A session a worker opens on another template keeps the Bot's
member id but is not counted here; the per-creator cap bounds it. A finished
worker keeps its seat until it is closed. A new worker is entered at its mint,
so two parallel creates cannot both pass, and the workers a restart brought
back are entered from their saved records by :func:`count_restored_workers`
before a check.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.state_parts.dashboard_state import DashboardState

logger = logging.getLogger("kiro_crew.dashboard.state")


def own_worker_member(execution: Any) -> str:
    """The Bot's ``member_id`` when *execution* is one of its own workers, else ``""``."""
    from kiro_crew.mate_worker import is_own_worker_execution

    if execution is None or not is_own_worker_execution(execution):
        return ""
    return str(execution.member_id)


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


def bot_worker_cap_refusal(state: "DashboardState", member_id: str, cap: int) -> str | None:
    """Why Bot *member_id* may not open another worker under *cap*, or ``None``."""
    from kiro_crew.members import crewmate_worker_member_id

    if not member_id or cap <= 0:
        return None
    held = sum(1 for key in list(state._slots) if crewmate_worker_member_id(key) == member_id)
    if held < cap:
        return None
    return (
        f"this Bot already has {held} worker sessions open and its owner allows {cap} "
        "at once. A worker keeps its seat until it is closed: close one that has "
        "finished (session_close), then try again. Only the owner can change this "
        "limit, on the Bot's settings page."
    )


def worker_cap_refusal(state: "DashboardState", execution: Any) -> str | None:
    """Why a slot running *execution* may not open, or ``None`` when it may.

    ``None`` for any record that is not a Bot's own worker, and for a Bot with no
    limit set. Synchronous: call it in the window that mints the slot.
    """
    member_id = own_worker_member(execution)
    if not member_id:
        return None
    return bot_worker_cap_refusal(state, member_id, bot_max_workers(member_id))


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
    """Enter the worker slots a restart brought back into their Bots' worker counts.

    A restored slot is otherwise counted only once a turn reads its execution
    record. Each live worker slot not yet counted has its saved record read off
    the loop, once; a record that is not a Bot's own worker leaves it uncounted.
    """
    from kiro_crew.dashboard.chat_utils import slot_history_key
    from kiro_crew.mate_worker import MATE_WORKER_AGENT, note_worker_session
    from kiro_crew.members import crewmate_worker_member_id

    keys = [
        slot_history_key(slot)
        for slot in list(state._slots.values())
        if getattr(slot, "agent", "") == MATE_WORKER_AGENT
        and not crewmate_worker_member_id(slot_history_key(slot))
    ]
    if not keys:
        return

    def _read() -> dict[str, Any]:
        # One record at a time: an unreadable one leaves only its own slot
        # uncounted, never the healthy workers read beside it.
        return {key: read_worker_execution(key) for key in keys}

    records = await asyncio.to_thread(_read)
    # The read ran off the loop, so the table may have moved meanwhile: a fork can
    # have counted its own child, or a slot closed. Apply only what is still news.
    open_now = {slot_history_key(slot) for slot in list(state._slots.values())}
    for key, execution in records.items():
        if execution is None or key not in open_now or crewmate_worker_member_id(key):
            continue
        note_worker_session(key, execution)


def note_new_worker(slot_key: str, execution: Any) -> None:
    """Enter a just-minted slot running *execution* into its Bot's worker count."""
    from kiro_crew.members import note_crewmate_worker_session

    member_id = own_worker_member(execution)
    if member_id:
        note_crewmate_worker_session(slot_key, member_id)
