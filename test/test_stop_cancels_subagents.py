"""Tests that the dashboard Stop button and Interrupt handler cascade
cancellation to in-flight subagents via ``cancel_for_parent``.

Regression tests for https://github.com/kirodotdev/KiroCrew/issues/18625
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fakes — minimal stand-ins matching test_stop_handler_idempotent.py
# ---------------------------------------------------------------------------

class _FakeSlot:
    """Minimal ChatSlot stand-in."""

    def __init__(self):
        self._stop_state = "idle"
        self._stop_generation = 0
        self._stop_event_id = None
        self._stop_escalated_card_id = None
        self._stop_declined_at = 0.0
        self._queue: list[dict] = []
        self._pending_steers: list = []
        self._steer_delivery_ids: dict = {}
        self._steer_send_ids: dict = {}
        self._steer_user_origin: dict = {}
        self._steer_channel_origin: dict = {}
        self._steer_admissions: dict = {}
        self._steer_attachment_meta: dict = {}
        self._steer_decision_strips: dict = {}
        self._steer_possibly_delivered: set = set()
        self.running = True
        self.key = "test-slot"
        self.linked_session_key = ""
        self._app = None
        self._active_turn_session_key = ""
        self.executor = "local"
        self.instance_id = ""
        self.remote_slot = ""
        self.agent = "kirocrew"
        self.messages: list[dict] = []
        self._dirty = False
        self.source_links_invalidated = 0
        self._lock = asyncio.Lock()

    def append(self, role, content, cls_meta):
        self.messages.append({"role": role, "content": content, "cls": cls_meta})

    def queue_promote_by_id(self, queue_id):
        return False

    def invalidate_source_links(self):
        self.source_links_invalidated += 1

    def take_pending_subagent_deliveries(self, contents):
        """Stub for hard-kill path that settles discarded stage deliveries."""
        return []


class _FakeState:
    """Minimal DashboardState stand-in with optional subagents."""

    def __init__(self, slot, *, subagents=None):
        self._slots = {"test-slot": slot}
        self.sessions = MagicMock()
        self.sessions.stop_turn = AsyncMock(return_value="soft")
        self.subagents = subagents
        self._push_count = 0

    def push_slots_update(self):
        self._push_count += 1

    def cancel_questions_for_slot(self, slot_key):
        return 0

    def get_slot(self, name):
        return self._slots.get(name)


def _make_subagents_mock():
    """Return a mock SubagentManager with a tracked cancel_for_parent."""
    mock = MagicMock()
    mock.cancel_for_parent = AsyncMock(return_value=(2, 1))  # 2 running, 1 queued
    return mock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _call_stop_slot_turn(state, slot, *, force=False):
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    with patch("kiro_crew.dashboard.chat_handlers.sel") as mock_sel:
        mock_sel.return_value.log_tool_invocation = MagicMock()
        mock_sel.return_value.log = MagicMock()
        with patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"):
            return await stop_slot_turn(state, slot, force=force)


# ---------------------------------------------------------------------------
# Tests: stop_slot_turn — soft stop path
# ---------------------------------------------------------------------------

class TestStopSlotTurnCancelsSubagents:
    """stop_slot_turn must call cancel_for_parent after stopping the turn."""

    @pytest.mark.asyncio
    async def test_soft_stop_calls_cancel_for_parent(self):
        """A cooperative stop cascades to subagents."""
        subs = _make_subagents_mock()
        slot = _FakeSlot()
        state = _FakeState(slot, subagents=subs)
        state.sessions.stop_turn = AsyncMock(return_value="soft")

        await _call_stop_slot_turn(state, slot)

        subs.cancel_for_parent.assert_awaited_once()
        # The cancel_key should be the slot's effective session key
        call_args = subs.cancel_for_parent.call_args
        assert call_args is not None

    @pytest.mark.asyncio
    async def test_soft_stop_no_subagents_manager(self):
        """Gracefully handles state without a subagents attribute."""
        slot = _FakeSlot()
        # _FakeState without subagents kwarg — subagents is None
        state = _FakeState(slot, subagents=None)
        state.sessions.stop_turn = AsyncMock(return_value="soft")

        # Should not raise
        result = await _call_stop_slot_turn(state, slot)
        assert result.get("ok") is True

    @pytest.mark.asyncio
    async def test_soft_stop_no_subagents_attr(self):
        """Gracefully handles state object that lacks subagents entirely."""
        slot = _FakeSlot()
        state = _FakeState(slot, subagents=None)
        # Remove the attribute entirely to simulate old _FakeState
        del state.subagents
        state.sessions.stop_turn = AsyncMock(return_value="soft")

        # Should not raise (getattr guard)
        result = await _call_stop_slot_turn(state, slot)
        assert result.get("ok") is True

    @pytest.mark.asyncio
    async def test_soft_stop_cancel_for_parent_exception_swallowed(self):
        """An exception in cancel_for_parent must not block the stop."""
        subs = _make_subagents_mock()
        subs.cancel_for_parent = AsyncMock(side_effect=RuntimeError("db gone"))
        slot = _FakeSlot()
        state = _FakeState(slot, subagents=subs)
        state.sessions.stop_turn = AsyncMock(return_value="soft")

        # Should not raise despite the exception
        result = await _call_stop_slot_turn(state, slot)
        assert result.get("ok") is True
        subs.cancel_for_parent.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_idle_outcome_still_cascades(self):
        """Even when the provider has no active turn, subagents are stopped.

        Subagents may outlive the parent turn that spawned them — a turn that
        completes normally still has running subagents.
        """
        subs = _make_subagents_mock()
        slot = _FakeSlot()
        state = _FakeState(slot, subagents=subs)
        state.sessions.stop_turn = AsyncMock(return_value="idle")

        await _call_stop_slot_turn(state, slot)

        subs.cancel_for_parent.assert_awaited_once()


# ---------------------------------------------------------------------------
# Tests: stop_slot_turn — hard kill escalation path
# ---------------------------------------------------------------------------

class TestHardStopCancelsSubagents:
    """The escalation (second press / force) path must also cascade."""

    @pytest.mark.asyncio
    async def test_hard_stop_calls_cancel_for_parent(self):
        """A hard kill cascades to subagents."""
        subs = _make_subagents_mock()
        slot = _FakeSlot()
        slot._stop_state = "soft_pending"  # simulate first press already done
        state = _FakeState(slot, subagents=subs)
        state.sessions.stop_turn = AsyncMock(return_value="hard")

        await _call_stop_slot_turn(state, slot)

        subs.cancel_for_parent.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hard_stop_cancel_exception_swallowed(self):
        """An exception in cancel_for_parent must not block the hard kill."""
        subs = _make_subagents_mock()
        subs.cancel_for_parent = AsyncMock(side_effect=RuntimeError("db gone"))
        slot = _FakeSlot()
        slot._stop_state = "soft_pending"
        state = _FakeState(slot, subagents=subs)
        state.sessions.stop_turn = AsyncMock(return_value="hard")

        result = await _call_stop_slot_turn(state, slot)
        assert result.get("ok") is True
