"""The app and state doubles shared by the ``test_chat_slot_reasoning_effort*.py`` modules."""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web

from kiro_crew.dashboard.chat import api_chat_slot_reasoning_effort
from kiro_crew.dashboard.chat_handlers import api_chat_slot_selection_capabilities
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/reasoning-effort", api_chat_slot_reasoning_effort)
    app.router.add_get(
        "/api/chat/slots/{slot}/selection-capabilities", api_chat_slot_selection_capabilities
    )
    return app


def _mock_state(slot: _ChatSlot | None = None, provider: object = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {}
    if slot:
        state._slots[slot.key] = slot
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.aflush = AsyncMock()
    # The explicit-Default intent the handler records per session key, kept in
    # a dict so a test reads the value a request finally leaves.
    intents: dict[str, bool] = {}
    state.sessions.default_intents = intents
    # Every intent write and row hold in order, so a test can read when a hold
    # began and ended relative to the writes it covers.
    events: list[tuple[object, ...]] = []
    state.sessions.effort_events = events

    def set_explicit_effort_default(key: str, pending: bool) -> bool:
        events.append(("set", key, pending))
        intents[key] = pending
        return True

    @contextlib.contextmanager
    def hold_explicit_effort_default(key: str):
        events.append(("hold", key))
        try:
            yield
        finally:
            events.append(("release", key))

    state.sessions.set_explicit_effort_default = MagicMock(side_effect=set_explicit_effort_default)
    state.sessions.hold_explicit_effort_default = MagicMock(
        side_effect=hold_explicit_effort_default
    )
    state.sessions.explicit_effort_default_pending = MagicMock(
        side_effect=lambda key: intents.get(key, False)
    )
    # No cold start in flight under any key, so the handler's intent writes are
    # not refused; a test that holds a start sets this per key.
    state.sessions.effort_basis_locked = MagicMock(return_value=False)
    # The handler's intent writes are counted per key from the in-memory flag
    # through the save, so a cold start reading the basis can wait for them.
    # ``in_flight_effort_intent_writes`` is the live count; a test reads it
    # from inside a save to see that the write is still counted there.
    in_flight_writes: dict[str, int] = {}
    state.sessions.in_flight_effort_intent_writes = in_flight_writes

    @contextlib.contextmanager
    def effort_intent_write(key: str):
        in_flight_writes[key] = in_flight_writes.get(key, 0) + 1
        try:
            yield
        finally:
            in_flight_writes[key] -= 1
            if in_flight_writes[key] == 0:
                del in_flight_writes[key]

    state.sessions.effort_intent_write = MagicMock(side_effect=effort_intent_write)
    # No live AcpProvider by default → handler falls back to session reset
    # (matches prior behaviour for the "no session yet" path).
    state.sessions.get_provider = MagicMock(return_value=provider)
    return state
