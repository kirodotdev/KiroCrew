"""App and state builders shared by test_chat_slot_switch_atomicity.py and its parts.

An aiohttp app that routes every slot switch endpoint behind the dashboard-user
marker, a mocked ``DashboardState`` holding one slot, and the two registry model
aliases the switch tests pick between.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from aiohttp import web

from kiro_crew.dashboard.chat import (
    api_chat_slot_agent,
    api_chat_slot_model,
    api_chat_slot_project,
    api_chat_slot_reasoning_effort,
    api_chat_slot_workspace,
    api_chat_slots_model,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

# Valid registry aliases the model guard accepts (tests are exempt from the
# hardcoded-model-literal gate; these mirror the ids the existing model-switch
# tests use).
_MODEL_A = "claude-opus-4.8"
_MODEL_B = "gpt-5.6-sol"


def _make_app(state: DashboardState) -> web.Application:
    # Mirror production: token_auth middleware sets request["app"] on every
    # authenticated path ("" = dashboard user); the bulk handler fails closed
    # without it.
    @web.middleware
    async def dashboard_auth_marker(request, handler):
        if "app" not in request:
            request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[dashboard_auth_marker])
    app["state"] = state
    app.router.add_post("/api/chat/slots/model", api_chat_slots_model)
    app.router.add_post("/api/chat/slots/{slot}/model", api_chat_slot_model)
    app.router.add_post("/api/chat/slots/{slot}/workspace", api_chat_slot_workspace)
    app.router.add_post("/api/chat/slots/{slot}/agent", api_chat_slot_agent)
    app.router.add_post("/api/chat/slots/{slot}/reasoning-effort", api_chat_slot_reasoning_effort)
    app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
    return app


def _mock_state(slot: _ChatSlot, provider: object = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.push_slots_update = MagicMock()
    state.broadcast_context_usage = MagicMock()
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock()
    # No live AcpProvider by default → the model handler takes the reset path.
    state.sessions.get_provider = MagicMock(return_value=provider)
    # Not a channel-resumed session by default, so a mid-turn pick on a pure
    # dashboard key defers; a bare MagicMock would answer truthy and 409.
    state.sessions.mirror_accepts_inbound = MagicMock(return_value=False)
    return state
