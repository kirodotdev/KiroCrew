"""Idempotency + orphaned-stop-card regression tests for the dashboard stop /
interrupt handlers (provider-agnostic — ported from the upstream project,
defect 3). The CC-provider-specific classes in the upstream file are dropped:
KiroCrew is KiroACP-only and providers/claude_code.py does not exist here."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _FakeSlot:
    """Minimal ChatSlot stand-in for handler tests."""

    def __init__(self):
        self._stop_state = "idle"
        self._stop_event_id = None
        self._queue: list[dict] = []
        self._auto_run = False
        self.running = True
        self.key = "test-slot"
        self.agent = "kirocrew"
        self.messages: list[dict] = []
        self._dirty = False
        self.source_links_invalidated = 0

    def append(self, role, content, cls_meta):
        self.messages.append({"role": role, "content": content, "cls": cls_meta})

    def invalidate_source_links(self):
        self.source_links_invalidated += 1


class _FakeState:
    """Minimal DashboardState stand-in."""

    def __init__(self, slot):
        self._slots = {"test-slot": slot}
        self.sessions = MagicMock()
        self.sessions.stop_turn = AsyncMock(return_value="idle")
        self._push_count = 0

    def push_slots_update(self):
        self._push_count += 1

    def cancel_questions_for_slot(self, slot_key):
        """No pending ask_question cards in this fixture.

        Present because the stop path releases BOTH blocking waits (approvals
        and agent questions) through `_unblock_pending_waits`.
        """
        return 0


class TestStopHandlerIdempotent:
    """Repeat /stop press returns info without creating another card."""

    @pytest.mark.asyncio
    async def test_repeat_stop_no_new_card(self):
        """Second non-force stop press while soft_pending returns info."""
        from aiohttp import web

        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot()
        # Simulate a stop already in progress (first press completed the guard
        # at line 727 and would reach the escalation path, but the escalation
        # path only fires when _stop_state == "soft_pending". We test the new
        # idempotent guard for states like "killing".)
        slot._stop_state = "killing"
        slot._stop_event_id = "stop-abc"
        slot.running = True

        state = _FakeState(slot)
        app = web.Application()
        app["state"] = state

        request = MagicMock()
        request.app = app
        request.match_info = {"slot": "test-slot"}
        request.query = {}  # no force flag

        resp = await api_chat_slot_stop(request)
        body = json.loads(resp.body)

        assert body.get("info") == "stop already in progress"
        # No new messages appended (no new card created)
        assert len(slot.messages) == 0

    @pytest.mark.asyncio
    async def test_idle_outcome_resolves_card(self):
        """When stop_turn returns 'idle', the stop card is resolved."""
        from aiohttp import web

        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot()
        slot.running = True
        state = _FakeState(slot)
        state.sessions.stop_turn = AsyncMock(return_value="idle")

        app = web.Application()
        app["state"] = state

        request = MagicMock()
        request.app = app
        request.match_info = {"slot": "test-slot"}
        request.query = {}

        # Mock SEL logging and _reject_pending_approvals
        with patch("kiro_crew.dashboard.chat_handlers.sel") as mock_sel:
            mock_sel.return_value.log_tool_invocation = MagicMock()
            mock_sel.return_value.log = MagicMock()
            with patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"):
                await api_chat_slot_stop(request)

        # After the handler, stop state should be back to idle and event_id cleared
        assert slot._stop_state == "idle"
        assert slot._stop_event_id is None
        assert slot.source_links_invalidated == 1


class TestInterruptHandlerIdempotent:
    """Repeat /interrupt press returns info without creating another card."""

    @pytest.mark.asyncio
    async def test_repeat_interrupt_no_new_card(self):
        """Interrupt while already stopping returns info."""
        from aiohttp import web

        from kiro_crew.dashboard.chat_handlers import api_chat_slot_interrupt

        slot = _FakeSlot()
        slot._stop_state = "soft_pending"
        slot._stop_event_id = "stop-xyz"
        slot.running = True
        slot._queue = [{"queue_id": "q1", "content": "hello"}]

        state = _FakeState(slot)
        app = web.Application()
        app["state"] = state

        request = MagicMock()
        request.app = app
        request.match_info = {"slot": "test-slot"}
        request.content_length = 0

        resp = await api_chat_slot_interrupt(request)
        body = json.loads(resp.body)

        assert body.get("info") == "stop already in progress"
        # Queue unchanged
        assert len(slot._queue) == 1
