"""A Channels room seat that runs a Bot gets that Bot's own memory store.

A seat runs under ``channel:<room>:<seat>``. When its agent is a Bot with its own
named memory store, the seat's session is bound to that Bot's execution before
it starts, and again before each turn, so its memory and lesson tools read and
write the store the Bot has now, the way a person joining a discussion brings
their own memory. A seat on an agent template, or on a crewmate whose store is
the Global one, binds nothing and has no memory tools that work: its key is never
recognized as a session's.

The memory routes recognize a seat only while it is still in its room, its
session is live, and its record holds the binding its Bot resolves to now
(:func:`seat_is_established`). A seat removed from its room, or whose Bot was
renamed, deleted, moved to another store or to Global, is refused rather than
reaching a store that is not its Bot's now.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.config import KiroCrewConfig

if TYPE_CHECKING:
    from kiro_crew.execution_context import ExecutionContext

logger = logging.getLogger(__name__)

#: The session-key prefix every room seat runs under (``Channel.add_agent``).
ROOM_SEAT_KEY_PREFIX = "channel:"


def _session_agent_kwargs(agent_name: str) -> dict[str, Any]:
    """``get_or_create``'s agent arguments: a crewmate (``default``) runs its template.

    Passed raw, a crewmate name reached the skill view as an agent with no spec.
    The crewmate's memory store is bound separately, by :func:`bind_seat_memory`.
    Blocking (config read): run it off the event loop.
    """
    if not agent_name:
        return {"agent": None}
    from kiro_crew.cli_chat import _resolve_cli_agent

    kiro_agent, crew_agent, _model, _store = _resolve_cli_agent(KiroCrewConfig.load(), agent_name)
    return {"agent": kiro_agent, "crew_agent": crew_agent}


async def start_seat_memory(agent: Any, channel: Any) -> bool:
    """Bind *agent*'s seat to its Bot's own store; ``False`` means the seat must not start.

    A Bot whose store cannot be bound is reported in the room and marked failed
    rather than started without its memory.
    """
    try:
        await asyncio.to_thread(bind_seat_memory, agent.agent_name, agent.session_key)
    except ValueError as unbound:
        logger.warning("Room seat %s: %s", agent.session_key, unbound)
        await channel.post(
            agent.id,
            f"Error: {agent.agent_name!r} could not join with its own memory: {unbound}",
            from_role=agent.role,
            msg_type="system",
        )
        agent.state = "failed"
        return False
    return True


async def refresh_seat_memory(agent: Any) -> None:
    """Re-bind a running seat to its Bot's store as config names it now, before a turn.

    A store the Bot moved to is bound for the turn. A Bot without a store of its
    own binds nothing, and the memory routes then refuse the seat. Never raises:
    the turn runs either way.
    """
    try:
        await asyncio.to_thread(bind_seat_memory, agent.agent_name, agent.session_key)
    except (OSError, ValueError) as unbound:
        logger.warning("Room seat %s has no memory this turn: %s", agent.session_key, unbound)


def seat_execution(config: KiroCrewConfig, agent_name: str) -> ExecutionContext | None:
    """The binding a seat running *agent_name* gets now, or ``None`` when it gets none.

    ``None`` for a template seat, a crewmate on the Global store, and a name
    config does not hold. Raises the execution layer's ``ValueError`` (or
    ``UnknownMemoryStore``) when the Bot's own store cannot be resolved.
    """
    if not agent_name or agent_name not in config.agents:
        return None
    from kiro_crew.cli_chat import _resolve_cli_agent
    from kiro_crew.execution_context import resolve_member_execution
    from kiro_crew.memory_stores import named_store_or_empty

    _kiro_agent, _crew_agent, _model, store = _resolve_cli_agent(config, agent_name)
    if not named_store_or_empty(store):
        return None
    return resolve_member_execution(config, agent_name)


def _same_binding(bound: ExecutionContext | None, expected: ExecutionContext) -> bool:
    """Whether *bound* names the store and member *expected* does."""
    return (
        bound is not None
        and bound.store == expected.store
        and bound.member_id == expected.member_id
    )


def bind_seat_memory(agent_name: str, session_key: str) -> bool:
    """Bind *session_key* to *agent_name*'s own store; whether there was one to bind.

    Writes only when the record does not already name that store. Raises the
    execution layer's ``ValueError`` (or ``UnknownMemoryStore``) when the Bot's
    identity or store cannot be resolved: a seat must then not start, because
    starting it would run the Bot with no binding. Blocking: call it off the
    event loop.
    """
    from kiro_crew.execution_context import bind_session_execution, read_session_execution

    execution = seat_execution(KiroCrewConfig.load(), agent_name)
    if execution is None:
        return False
    if not _same_binding(read_session_execution(session_key), execution):
        # The seat key is minted per seat, so this record is the seat's alone. Not
        # vouched: a seat is never the caller slot of an own-store dispatch (a room
        # seat may not spawn), which is the only reader of the vouched map.
        bind_session_execution(session_key, execution, replace_existing=True)
    return True


def seat_has_own_store(session_key: str, agent_name: str) -> bool:
    """Whether the seat *session_key*, running *agent_name*, holds its Bot's current binding.

    The record must name the store and member :func:`seat_execution` resolves
    now, so a binding its Bot does not have now is refused rather than trusted. The
    STORE is compared, not only a member id: a Bot on a legacy V1 named store has
    no member id and is still bound to its own store. Any resolution or read
    failure answers ``False``, so an unreadable record never opens memory.
    """
    if not session_key.startswith(ROOM_SEAT_KEY_PREFIX):
        return False
    from kiro_crew.execution_context import read_session_execution

    try:
        expected = seat_execution(KiroCrewConfig.load(), agent_name)
        bound = read_session_execution(session_key)
    except (OSError, ValueError):
        return False
    return expected is not None and _same_binding(bound, expected)


def _seated_agent(manager: Any, session_key: str) -> Any | None:
    """The member still seated under *session_key* in its room, or ``None``."""
    parts = session_key.split(":")
    if manager is None or len(parts) != 3 or parts[0] + ":" != ROOM_SEAT_KEY_PREFIX:
        return None
    channel = manager.get(parts[1])
    seat = channel.members.get(parts[2]) if channel is not None else None
    return seat if seat is not None and seat.session_key == session_key else None


async def seat_is_established(state: Any, session_key: str) -> bool:
    """Whether *session_key* is a live room seat bound to its Bot's current store.

    The memory routes' recognition for a ``channel:`` key: the seat is still in
    its room, its session is live, and its record holds the binding its Bot
    resolves to now (:func:`seat_has_own_store`).
    """
    seat = _seated_agent(getattr(state, "channel_manager", None), session_key)
    sessions = getattr(state, "sessions", None)
    if seat is None or sessions is None or sessions.has_session(session_key) is not True:
        return False
    return await asyncio.to_thread(seat_has_own_store, session_key, seat.agent_name)
