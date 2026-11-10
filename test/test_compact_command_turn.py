"""A manual ``/compact`` is the compaction it runs, from the moment it is sent.

The chat shows the compacting state for the whole command rather than
"Thinking…", and a message sent while it runs waits in the queue: steered into
the command it would reach no prompt.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import steer_requeue_helpers
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state
from dashboard_chat_helpers import _provider_mock
from steer_requeue_helpers import _running_slot

from kiro_crew.acp.types import ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

_patch_sel = steer_requeue_helpers._patch_sel


def _compacting_frames(state) -> list[dict]:
    return [
        c.args[1]
        for c in state.broadcast_ws.call_args_list
        if c.args and c.args[0] == "chat_message" and c.args[1].get("role") == "compacting"
    ]


@pytest.mark.asyncio
async def test_a_compact_turn_says_compacting_before_the_command_is_sent(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    slot = state.get_or_create_slot("s1")
    client = _provider_mock()
    client.context_usage_pct = MagicMock(return_value=0.0)
    client.context_window_tokens = MagicMock(return_value=0)
    client.context_used_tokens = MagicMock(return_value=0)
    client.wait_for_compaction = AsyncMock(return_value={"type": "completed", "summary": ""})
    client.capabilities = capabilities_for(ACP_BACKEND_KIRO)
    seen_when_sent: dict = {}

    async def _stream_command(msg):
        seen_when_sent["frames"] = _compacting_frames(state)
        seen_when_sent["marked"] = slot._compact_command_turn
        yield LLMEvent(kind=EVENT_COMPLETE)

    client.stream_command = _stream_command
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    from kiro_crew.dashboard.chat import _run_chat

    await _run_chat(state, slot, "/compact")

    assert seen_when_sent == {
        "frames": [{"slot": slot.key, "role": "compacting", "content": ""}],
        "marked": True,
    }
    assert slot._compact_command_turn is False


@pytest.mark.asyncio
async def test_a_send_during_a_compact_turn_is_queued_not_steered(
    tmp_path, monkeypatch, _patch_sel
):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = _running_slot(state)
    client = MagicMock(spec=["supports_steer", "steer_needs_loss_recovery", "steer"])
    client.supports_steer = True
    client.steer_needs_loss_recovery = False
    client.steer = AsyncMock(return_value=True)
    client.has_active_turn = MagicMock(return_value=True)
    slot._acp_client = client
    slot._compact_command_turn = True

    async with TestClient(TestServer(_make_app(state))) as http:
        resp = await http.post(
            "/api/chat", json={"slot": "test", "message": "/compact", "steer": True}
        )
        assert resp.status == 200
        body = await resp.json()

    client.steer.assert_not_awaited()
    assert body.get("queued") is True and not body.get("steered")
    assert [e.get("content") for e in slot._queue] == ["/compact"]
