"""An idle interrupt that names a card not in the queue must not stop a channel turn.

``api_chat_slot_interrupt`` cancels the channel-path turn a linked tab is bound to
before it dispatches the selected card. A refused interrupt (the card was sent,
cancelled or edited away meanwhile) must leave that live turn running.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat import api_chat_slot_interrupt
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def _app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/interrupt", api_chat_slot_interrupt)
    return app


@pytest.mark.asyncio
async def test_unknown_card_leaves_the_linked_channel_turn_running():
    slot = _ChatSlot("test")
    slot.linked_session_key = "slack:C1:1700000000.000100"
    slot._linked_session_busy = lambda _key: True
    slot.queue_append("still queued")
    state = MagicMock(spec=DashboardState)
    state._slots = {"test": slot}
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    state.sessions.stop_turn = AsyncMock(return_value="soft")
    state.broadcast_ws = MagicMock()

    async def _start(_state, s, *, allow_user_during_subagents, required_queue_id):
        return any(item["id"] == required_queue_id for item in s._queue)

    with (
        patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()),
        patch(
            "kiro_crew.dashboard.chat_handlers._start_next_queued_turn",
            new=AsyncMock(side_effect=_start),
        ),
    ):
        assert slot.linked_turn_running and not slot.running
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/interrupt", json={"queue_id": "q-gone"})
            data = await resp.json()

    assert resp.status == 409
    assert data["code"] == "queue_item_unavailable"
    # The refusal changed nothing: the channel turn was never asked to stop.
    assert state.sessions.stop_turn.await_count == 0
