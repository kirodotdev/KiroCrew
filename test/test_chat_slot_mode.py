"""Tests for PATCH /api/chat/slots/{slot}/mode endpoint."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat import api_chat_slot_mode
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_patch("/api/chat/slots/{slot}/mode", api_chat_slot_mode)
    return app


def _mock_state(slot: _ChatSlot | None = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {}
    if slot:
        state._slots[slot.key] = slot
    state.push_slots_update = MagicMock()
    return state


class TestChatSlotMode:
    @pytest.mark.asyncio
    async def test_switch_to_orchestrator(self):
        slot = _ChatSlot("test")
        assert slot.mode == ""
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    "/api/chat/slots/test/mode",
                    json={"mode": "orchestrator"},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data == {"ok": True, "mode": "orchestrator"}
                assert slot.mode == "orchestrator"
                state.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_switch_back_to_default(self):
        slot = _ChatSlot("test")
        slot.mode = "orchestrator"
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    "/api/chat/slots/test/mode",
                    json={"mode": ""},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data == {"ok": True, "mode": ""}
                assert slot.mode == ""

    @pytest.mark.asyncio
    async def test_invalid_mode_rejected(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    "/api/chat/slots/test/mode",
                    json={"mode": "invalid"},
                )
                assert resp.status == 400
                data = await resp.json()
                assert data["error"] == "invalid mode"
                assert slot.mode == ""  # unchanged

    @pytest.mark.asyncio
    async def test_slot_not_found(self):
        state = _mock_state()  # no slot
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    "/api/chat/slots/nonexistent/mode",
                    json={"mode": "orchestrator"},
                )
                assert resp.status == 404

    @pytest.mark.asyncio
    async def test_missing_mode_defaults_to_empty(self):
        slot = _ChatSlot("test")
        slot.mode = "orchestrator"
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    "/api/chat/slots/test/mode",
                    json={},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["mode"] == ""
                assert slot.mode == ""

    @pytest.mark.asyncio
    async def test_reject_mode_switch_while_running(self):
        slot = _ChatSlot("test")
        # Simulate a running session by giving it an undone task
        import asyncio
        slot.task = asyncio.ensure_future(asyncio.sleep(999))
        assert slot.running
        state = _mock_state(slot)
        try:
            with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"):
                async with TestClient(TestServer(_make_app(state))) as client:
                    resp = await client.patch(
                        "/api/chat/slots/test/mode",
                        json={"mode": "orchestrator"},
                    )
                    assert resp.status == 409
                    assert slot.mode == ""  # unchanged
        finally:
            slot.task.cancel()
            try:
                await slot.task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_auto_run_cleared_on_leaving_orchestrator(self):
        slot = _ChatSlot("test")
        slot.mode = "orchestrator"
        slot._auto_run = True
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    "/api/chat/slots/test/mode",
                    json={"mode": ""},
                )
                assert resp.status == 200
                assert slot.mode == ""
                assert slot._auto_run is False
