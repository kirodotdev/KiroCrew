"""Tests for the mid-turn steer branch in POST /api/chat (api_chat handler).

Exercises the steer path that reaches the running turn's live AcpClient via
``slot._acp_client``:
  * steered success -> broadcasts ``steer_push`` and returns ``{steered: True}``;
  * steer unavailable (no live client) -> safe fall-through to the queue;
  * steer raises -> caught, falls through to the queue (message never dropped).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state


@pytest.fixture
def _patch_sel():
    """Patch sel() so the handler doesn't touch a real SecurityEventLog."""
    mock_sel = MagicMock()
    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=mock_sel):
        yield mock_sel


def _running_slot(state, key="test"):
    """Create a slot and make it look like a turn is in flight.

    ``running`` is ``task is not None and not task.done()``.
    """
    slot = state.get_or_create_slot(key)
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


class TestApiChatSteer:
    @pytest.mark.asyncio
    async def test_steer_injects_into_running_turn(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "go left", "steer": True}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data.get("steered") is True
            assert data.get("queued") is not True

        client_mock.steer.assert_awaited_once_with("go left")
        events = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "steer_push" in events
        assert "queue_push" not in events  # steered, not queued

    @pytest.mark.asyncio
    async def test_steer_unavailable_falls_back_to_queue(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        slot._acp_client = None  # no live client -> cannot steer

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "later", "steer": True}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data.get("queued") is True
            assert data.get("steered") is not True

        # message queued (not dropped) and broadcast as queue_push, not steer_push
        events = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "queue_push" in events
        assert "steer_push" not in events

    @pytest.mark.asyncio
    async def test_steer_error_falls_back_to_queue(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(side_effect=RuntimeError("boom"))
        slot._acp_client = client_mock

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "later", "steer": True}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data.get("queued") is True
            assert data.get("steered") is not True

        client_mock.steer.assert_awaited_once()
