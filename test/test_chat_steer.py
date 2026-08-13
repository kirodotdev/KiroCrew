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

    @pytest.mark.asyncio
    async def test_steer_cuts_segment_before_user_append(self, tmp_path, monkeypatch, _patch_sel):
        """The segment cut runs BEFORE the steer user message is persisted, so
        the flushed pre-steer assistant text lands ABOVE the steer bubble."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        roles_at_cut: list[str] = []

        def _cut() -> None:
            # Snapshot the transcript at cut time, then flush like the real
            # closure does (persist the accumulated segment as assistant).
            roles_at_cut.extend(m["role"] for m in slot.messages)
            slot.append("assistant", "pre-steer text", "msg msg-a")

        slot._steer_segment_cut = _cut

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "go left", "steer": True}
            )
            assert resp.status == 200
            assert (await resp.json()).get("steered") is True

        # Cut ran before the user append: no user message in the snapshot.
        assert "user" not in roles_at_cut
        # Persisted order: pre-steer assistant ABOVE the steer user bubble.
        roles = [m["role"] for m in slot.messages]
        assert roles.index("assistant") < roles.index("user")
        steer_msg = next(m for m in slot.messages if m["role"] == "user")
        assert steer_msg.get("meta", {}).get("steer") is True

    @pytest.mark.asyncio
    async def test_steer_cut_failure_does_not_lose_steer(self, tmp_path, monkeypatch, _patch_sel):
        """A raising cut closure is best-effort: the steer user message is
        still persisted and steer_push still broadcast."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock
        slot._steer_segment_cut = MagicMock(side_effect=RuntimeError("boom"))

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "go left", "steer": True}
            )
            assert resp.status == 200
            assert (await resp.json()).get("steered") is True

        slot._steer_segment_cut.assert_called_once()
        assert any(m["role"] == "user" and m.get("meta", {}).get("steer") for m in slot.messages)
        events = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "steer_push" in events


class TestFlushSegmentQuietPersist:
    """Pin the steer-cut persistence contract: quiet_persist suppresses the
    per-message chat_message broadcast slot.append emits for the finalized
    assistant message. At the cut boundary every client has already frozen its
    streaming message, so that broadcast would render a duplicate copy of the
    pre-steer text below the steer bubble."""

    def _slot_with_chunks(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        slot._on_message = MagicMock()
        slot.append("chunk", "pre-steer", "chunk", broadcast=False)
        return state, slot

    def test_quiet_persist_suppresses_message_broadcast(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _flush_segment

        state, slot = self._slot_with_chunks(tmp_path, monkeypatch)
        _flush_segment(state, slot, "pre-steer text", broadcast=False, quiet_persist=True)
        # Persisted (chunks collapsed into the assistant message)…
        roles = [m["role"] for m in slot.messages]
        assert "assistant" in roles and "chunk" not in roles
        # …but with NO chat_message broadcast and NO chat_segment event.
        slot._on_message.assert_not_called()
        events = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "chat_segment" not in events

    def test_default_flush_still_broadcasts_message(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _flush_segment

        state, slot = self._slot_with_chunks(tmp_path, monkeypatch)
        _flush_segment(state, slot, "normal segment", broadcast=False)
        slot._on_message.assert_called_once()
