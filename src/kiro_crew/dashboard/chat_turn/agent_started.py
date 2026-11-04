"""Claim ACP agent-started turns into their owning dashboard slots."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from kiro_crew.acp.client import AcpClient
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_AGENT_STARTED_TURNS

logger = logging.getLogger("kiro_crew.dashboard.chat_runner")


def wire_agent_started_turn(state: Any, slot: Any, provider: Any, session_key: str) -> None:
    """Bind an ACP client's autonomous-turn handoff to this exact live slot/session."""
    acp = getattr(provider, "client", None)
    if not isinstance(acp, AcpClient) or acp.backend not in ACP_BACKENDS_AGENT_STARTED_TURNS:
        return

    # Every dashboard turn refreshes this closure, so a callback retained by a
    # prior binding cannot deliver into a slot after it has changed sessions.
    stop_generation = slot._stop_generation
    waiter: asyncio.Task[Any] | None = None
    owns_reservation = False

    def still_owned() -> bool:
        if state._slots.get(slot.key) is not slot or slot.is_closing:
            return False
        from kiro_crew.dashboard.chat_turn.turn_marker import _gateway_shutdown_requested
        from kiro_crew.dashboard.chat_utils_slots import effective_session_key

        if _gateway_shutdown_requested(state) or effective_session_key(slot) != session_key:
            return False
        return state.sessions.get_provider(session_key) is provider

    def on_agent_turn(observed: AcpClient) -> None:
        nonlocal waiter, owns_reservation, stop_generation
        if observed is not acp or not still_owned():
            return
        # Stop applies to this wake, not to later autonomous cycles in the same
        # session; capture its generation at the handoff boundary.
        stop_generation = slot._stop_generation
        if not still_owned():
            return
        # Synchronous on the serving loop: this fence stops a new dashboard send
        # from winning the turn between the ACP handoff and task scheduling.
        slot._agent_started_pending = True
        if not slot.running:
            slot._turn_admission_reserved = True
            owns_reservation = True
        if waiter is not None and not waiter.done():
            return

        async def deliver() -> None:
            nonlocal owns_reservation
            try:
                while still_owned() and slot._agent_started_pending:
                    if slot._stop_generation != stop_generation:
                        slot._agent_started_pending = False
                        return
                    if not owns_reservation and slot.running:
                        owner = slot.task
                        if owner is None or owner is asyncio.current_task():
                            await asyncio.sleep(0.05)
                        else:
                            await asyncio.wait({owner})
                        continue
                    if not owns_reservation:
                        # No suspension between the idle check and reservation.
                        if slot.running:
                            continue
                        slot._turn_admission_reserved = True
                        owns_reservation = True
                    if not still_owned():
                        slot._agent_started_pending = False
                        return

                    from kiro_crew.dashboard.chat_runner import _run_chat
                    from kiro_crew.dashboard.turn_dispatch import spawn_guarded_turn

                    slot._agent_started_pending = False
                    coro = state.run_background_turn(
                        slot,
                        _run_chat(
                            state,
                            slot,
                            "",
                            _agent_started=True,
                            _turn_actor="agent",
                        ),
                    )
                    try:
                        task = spawn_guarded_turn(state, slot, coro)
                    except BaseException:
                        slot._agent_started_pending = True
                        raise
                    slot.task = task
                    state.push_slots_update()
                    return
            finally:
                if slot._agent_started_pending:
                    slot._agent_started_pending = False
                if owns_reservation:
                    owns_reservation = False
                    slot._turn_admission_reserved = False

        try:
            waiter = asyncio.get_running_loop().create_task(deliver())
        except RuntimeError:
            slot._agent_started_pending = False
            owns_reservation = False
            slot._turn_admission_reserved = False
            return
        state._background_tasks.add(waiter)
        waiter.add_done_callback(state._background_tasks.discard)
        waiter.add_done_callback(_log_waiter_failure)

    acp.on_agent_turn = on_agent_turn


def _log_waiter_failure(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    with suppress(asyncio.CancelledError):
        error = task.exception()
        if error is not None:
            logger.warning(
                "ACP agent-started turn delivery failed",
                exc_info=(type(error), error, error.__traceback__),
            )
