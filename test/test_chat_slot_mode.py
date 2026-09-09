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
    async def test_busy_check_asks_about_the_linked_session(self):
        """Subagents spawn under the slot's LINKED session, and
        `has_pending_work_for` matches `parent_session_key` exactly. Asking about
        `dashboard:<tab>` for a channel-linked slot reports idle while that
        slot's subagents are still running, flipping the execution model out from
        under them."""
        slot = _ChatSlot("test")
        slot.linked_session_key = "slack:1785370133.085469"
        state = _mock_state(slot)
        asked: list[str] = []
        state.subagents = MagicMock()
        state.subagents.has_pending_work_for = MagicMock(
            side_effect=lambda k: bool(asked.append(k)) or k == slot.linked_session_key
        )
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch(
                    "/api/chat/slots/test/mode",
                    json={"mode": "orchestrator"},
                )
        assert asked == ["slack:1785370133.085469"]
        # And the answer is honoured: pending work refuses the switch.
        assert resp.status == 409
        assert slot.mode == ""

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

    @pytest.mark.asyncio
    async def test_crew_is_no_longer_a_switchable_mode(self):
        """Crew Mode retired: the switch endpoint refuses it like any unknown
        mode, so no session can be steered back into a dispatch path that no
        longer exists."""
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop"):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.patch("/api/chat/slots/test/mode", json={"mode": "crew"})
        assert resp.status == 400
        assert slot.mode == ""


class TestRetiredModeRestore:
    """A slot persisted under a retired mode comes back as plain chat.

    The transcript is the user's record and still renders; only the
    mode-specific dispatch went with the mode. No migration, no deletion.
    """

    def test_crew_restores_as_plain_chat(self):
        from kiro_crew.dashboard.chat_persistence import _restored_mode

        assert _restored_mode("crew") == ""

    @pytest.mark.parametrize("raw", ["orchestrator", "design-critique", "member"])
    def test_live_modes_pass_through(self, raw):
        from kiro_crew.dashboard.chat_persistence import _restored_mode

        assert _restored_mode(raw) == raw

    @pytest.mark.parametrize("raw", ["", None, 42, ["crew"]])
    def test_non_string_or_empty_is_plain_chat(self, raw):
        from kiro_crew.dashboard.chat_persistence import _restored_mode

        assert _restored_mode(raw) == ""
