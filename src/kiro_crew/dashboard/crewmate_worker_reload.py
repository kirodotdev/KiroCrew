"""Hot-reload a crewmate's own sessions when what they may mount changes.

A live agent process mounts its MCP servers and skills once, at session start.
A worker mounts every capability its crewmate's file approves
(option A: nothing is picked per hand-off), so a capability approved mid-task --
a ``request_capabilities`` card applied, or the user adding it in the crewmate's
advanced settings -- lands in the file while the RUNNING worker still lacks it.
Here the gateway reloads that same worker: the same teardown the tab menu's
Reload session runs (``chat_api.session_relaunch.reload_slot_session``), so the
process is relaunched, re-reads its spec (``mate_worker.worker_session_spec``
merges the updated file) and resumes the SAME conversation. No new worker is
opened.

Only an idle session is reloaded. A worker that reported a gap has ended its
turn, so it is idle by then. A busy one (a turn in flight, a queued message,
attached sub-agents) is marked, and reloaded the next time one of its turns
ends (:func:`note_turn_end`, from ``chat_turn.queue_cycle._end_turn_tail``): a
few short tries, each re-checking that it is idle, since the turn end itself
runs while the turn still holds the slot.

The same reload turns a crewmate's conductor onto its new tool list when the
user applies the card that lets it ask for tools (``mate_tool_access``): the
conductor then mounts ``request_capabilities`` and is woken to raise that card
for what its worker was missing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The state attribute holding the slot keys owed a reload once they are idle.
#: Kept on the state, not the slot: ``_ChatSlot`` declares ``__slots__``.
OWED_ATTR = "_crewmate_reload_owed"
#: Tries after a turn end before giving up until the next one, and their spacing.
RETRIES = 6
RETRY_SECS = 0.5

#: Reload outcomes.
RELOADED = "reloaded"
BUSY = "busy"
NOT_RUNNING = "not_running"
GONE = "gone"

_NOTICE = (
    "Session reloaded so it picks up capabilities added to this crewmate's worker. "
    "The conversation is kept."
)


def _owed(state: Any) -> set[str]:
    owed = getattr(state, OWED_ATTR, None)
    if not isinstance(owed, set):
        owed = set()
        setattr(state, OWED_ATTR, owed)
    return owed


def is_owed(state: Any, slot: Any) -> bool:
    """Whether *slot* still owes a reload onto its updated spec."""
    return getattr(slot, "key", "") in _owed(state)


def _mark(state: Any, slot: Any, owed: bool) -> None:
    key = getattr(slot, "key", "")
    if owed:
        _owed(state).add(key)
    else:
        _owed(state).discard(key)


def _is_busy(state: Any, slot: Any, session_key: str) -> bool:
    from kiro_crew.dashboard.chat_handlers import _switch_target_busy

    provider = state.sessions.get_provider(session_key)
    if provider is not None and provider.has_active_turn():
        return True
    return bool(getattr(slot, "_queue", None)) or bool(
        _switch_target_busy(state, slot, session_key, provider)
    )


async def reload_if_idle(state: Any, slot: Any) -> str:
    """Reload *slot*'s agent process now when it is idle; mark it pending when busy.

    Returns :data:`RELOADED`, :data:`BUSY` (marked, reloaded at its next turn
    end), :data:`NOT_RUNNING` (no live process: its next spawn reads the new
    spec anyway) or :data:`GONE`. Never raises.
    """
    from kiro_crew.dashboard.chat_handlers import reload_slot_session
    from kiro_crew.dashboard.chat_utils import effective_session_key

    key = getattr(slot, "key", "")
    try:
        if state._slots.get(key) is not slot:
            return GONE
        session_key = effective_session_key(slot)
        # Busy first: a worker still cold-starting has no provider yet, but its
        # turn is in flight (``slot.running``) and spawns on the kit it already
        # projected, so it keeps the reload owed for its turn end.
        if _is_busy(state, slot, session_key):
            _mark(state, slot, True)
            return BUSY
        if state.sessions.get_provider(session_key) is None:
            _mark(state, slot, False)
            return NOT_RUNNING
        response = await reload_slot_session(
            state,
            slot,
            key,
            still_ours=lambda _after: state._slots.get(key) is slot,
            denied=lambda _session_key: None,
            busy=lambda sk: _is_busy(state, slot, sk),
            notice=_NOTICE,
        )
    except Exception:  # noqa: BLE001 - a missed reload leaves the next spawn to read it
        logger.warning("crewmate reload of %s failed", key, exc_info=True)
        return GONE
    if response.status == 409:
        _mark(state, slot, True)
        return BUSY
    if response.status >= 400:
        return GONE
    _mark(state, slot, False)
    logger.info("crewmate session %s reloaded onto its updated spec", key)
    return RELOADED


def _workers_of(state: Any, member: str) -> list[Any]:
    """The live slots that are crewmate *member*'s own workers. Blocking (record reads)."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.chat_utils import slot_history_key
    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.mate_worker import MATE_WORKER_AGENT

    record = KiroCrewConfig.load().agents.get(member)
    member_id = str(getattr(record, "member_id", "") or "") if record is not None else ""
    if not member_id:
        return []
    out = []
    for slot in list(state._slots.values()):
        try:
            rec = read_session_execution(slot_history_key(slot))
        except Exception:  # noqa: BLE001 - an unreadable record is not a worker
            continue
        if (
            rec is not None
            and getattr(rec, "template_id", "") == MATE_WORKER_AGENT
            and str(getattr(rec, "member_id", "")) == member_id
        ):
            out.append(slot)
    return out


async def reload_workers(state: Any, member: str) -> dict[str, str]:
    """Reload every live worker of crewmate *member* (idle now, busy at turn end).

    Returns ``{slot key: outcome}``. Never raises.
    """
    try:
        workers = await asyncio.to_thread(_workers_of, state, member)
    except Exception:  # noqa: BLE001 - no roster names no worker
        logger.debug("crewmate reload: workers of %s unreadable", member, exc_info=True)
        return {}
    return {slot.key: await reload_if_idle(state, slot) for slot in workers}


def schedule_reload_workers(state: Any, member: str) -> "asyncio.Task[dict[str, str]] | None":
    """:func:`reload_workers` as a task, so the route that changed the file answers first."""
    if state is None or not member:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(reload_workers(state, member))
    tasks = getattr(state, "_crewmate_reload_tasks", None)
    if not isinstance(tasks, set):
        tasks = set()
        state._crewmate_reload_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


async def _retry(state: Any, slot: Any) -> None:
    for attempt in range(RETRIES):
        await asyncio.sleep(RETRY_SECS * (attempt + 1))
        outcome = await reload_if_idle(state, slot)
        if outcome != BUSY:
            return


def note_turn_end(state: Any, slot: Any) -> None:
    """At a top-level turn end: run a reload this slot owes once it is idle. Never raises."""
    if not is_owed(state, slot):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_retry(state, slot))
    tasks = getattr(state, "_crewmate_reload_tasks", None)
    if not isinstance(tasks, set):
        tasks = set()
        state._crewmate_reload_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)
