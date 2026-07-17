"""Tests for _remove_slot_for_history_key in handlers.py."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard.handlers import _remove_slot_for_history_key


def _make_state(slots: dict) -> MagicMock:
    state = MagicMock()
    state._slots = dict(slots)
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    state.sessions.destroy = AsyncMock()
    return state


def _make_slot(key: str, running: bool = False) -> MagicMock:
    slot = MagicMock()
    slot.key = key
    slot.running = running
    if running:
        async def _hang():
            await asyncio.sleep(999)
        slot.task = asyncio.ensure_future(_hang())
    else:
        slot.task = None
    return slot


class TestRemoveSlotForHistoryKey:
    @pytest.mark.asyncio
    async def test_exact_key_match(self):
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert "dashboard_chat-1-100" not in state._slots

    @pytest.mark.asyncio
    async def test_stripped_key_match(self):
        slot = _make_slot("chat-1-100")
        state = _make_state({"chat-1-100": slot})
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert "chat-1-100" not in state._slots

    @pytest.mark.asyncio
    async def test_colon_prefix_stripped(self):
        slot = _make_slot("chat-2-200")
        state = _make_state({"chat-2-200": slot})
        await _remove_slot_for_history_key(state, "dashboard:chat-2-200")
        assert "chat-2-200" not in state._slots

    @pytest.mark.asyncio
    async def test_no_match_is_noop(self):
        state = _make_state({"chat-9-999": _make_slot("chat-9-999")})
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert "chat-9-999" in state._slots
        state.sessions.destroy.assert_not_called()

    @pytest.mark.asyncio
    async def test_running_task_cancelled(self):
        slot = _make_slot("dashboard_chat-1-100", running=True)
        state = _make_state({"dashboard_chat-1-100": slot})
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert slot.task.cancelled()
        state.sessions.destroy.assert_awaited_once_with("dashboard:chat-1-100")

    @pytest.mark.asyncio
    async def test_non_running_task_not_cancelled(self):
        slot = _make_slot("dashboard_chat-1-100", running=False)
        state = _make_state({"dashboard_chat-1-100": slot})
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert slot.task is None
        state.sessions.destroy.assert_awaited_once_with("dashboard:chat-1-100")

    @pytest.mark.asyncio
    async def test_stacked_dashboard_prefix(self):
        slot = _make_slot("chat-3-300")
        state = _make_state({"chat-3-300": slot})
        await _remove_slot_for_history_key(state, "dashboard_dashboard_chat-3-300")
        assert "chat-3-300" not in state._slots

    @pytest.mark.asyncio
    async def test_batch_clear_removes_multiple_slots(self):
        """Verify batch clear removes matched slots and leaves unmatched."""
        slot_a = _make_slot("chat-1-100")
        slot_b = _make_slot("chat-2-200", running=True)
        slot_c = _make_slot("chat-9-999")
        state = _make_state({
            "chat-1-100": slot_a,
            "chat-2-200": slot_b,
            "chat-9-999": slot_c,
        })
        # Simulate batch clear for two keys (one matched, one running)
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        await _remove_slot_for_history_key(state, "dashboard_chat-2-200")
        assert "chat-1-100" not in state._slots
        assert "chat-2-200" not in state._slots
        assert "chat-9-999" in state._slots  # unmatched stays
        assert state.sessions.destroy.await_count == 2

    @pytest.mark.asyncio
    async def test_reverse_prefix_lookup(self):
        """History key 'chat-1-100' finds slot stored as 'dashboard_chat-1-100'."""
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        await _remove_slot_for_history_key(state, "chat-1-100")
        assert "dashboard_chat-1-100" not in state._slots
        state.sessions.destroy.assert_awaited_once_with("dashboard:chat-1-100")

    @pytest.mark.asyncio
    async def test_sessions_remove_exception_does_not_propagate(self):
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        state.sessions.destroy = AsyncMock(side_effect=RuntimeError("already gone"))
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert "dashboard_chat-1-100" not in state._slots
