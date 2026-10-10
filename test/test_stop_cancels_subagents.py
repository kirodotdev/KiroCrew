"""Tests that the dashboard Stop button cascades cancellation to in-flight
subagents via ``cancel_for_parent``, and that other callers of
``stop_slot_turn`` (steer-containment, session-control, work-ledger) do NOT.

Regression tests for https://github.com/kirodotdev/KiroCrew/issues/18625
"""

from __future__ import annotations

import asyncio
import json
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
        self._turn_generation = 0

    def append(self, role, content, cls_meta):
        self.messages.append({"role": role, "content": content, "cls": cls_meta})

    def queue_promote_by_id(self, queue_id):
        return False

    def invalidate_source_links(self):
        self.source_links_invalidated += 1

    def take_pending_subagent_deliveries(self, contents):
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


def _make_request(state, *, force=False):
    """Build a minimal mock request for api_chat_slot_stop."""
    from aiohttp import web

    app = web.Application()
    app["state"] = state

    request = MagicMock()
    # Dashboard user press, not an app token.
    request.get = lambda key, default="": default
    request.app = app
    request.match_info = {"slot": "test-slot"}
    request.query = {"force": "true"} if force else {}
    return request


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _call_stop_slot_turn(state, slot, *, force=False, source="dashboard"):
    """Call stop_slot_turn directly (the shared helper)."""
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    with patch("kiro_crew.dashboard.chat_handlers.sel") as mock_sel:
        mock_sel.return_value.log_tool_invocation = MagicMock()
        mock_sel.return_value.log = MagicMock()
        with patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"):
            return await stop_slot_turn(state, slot, force=force, source=source)


async def _call_api_chat_slot_stop(state, *, force=False):
    """Call the HTTP route handler directly."""
    from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

    request = _make_request(state, force=force)
    with patch("kiro_crew.dashboard.chat_handlers.sel") as mock_sel:
        mock_sel.return_value.log_tool_invocation = MagicMock()
        mock_sel.return_value.log = MagicMock()
        with patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"):
            return await api_chat_slot_stop(request)


# ---------------------------------------------------------------------------
# Tests: api_chat_slot_stop — the cascade lives HERE
# ---------------------------------------------------------------------------

class TestStopButtonCascadesToSubagents:
    """The dashboard Stop button route must call cancel_for_parent."""

    @pytest.mark.asyncio
    async def test_soft_stop_calls_cancel_for_parent(self):
        """A cooperative stop cascades to subagents."""
        subs = _make_subagents_mock()
        slot = _FakeSlot()
        state = _FakeState(slot, subagents=subs)
        state.sessions.stop_turn = AsyncMock(return_value="soft")

        resp = await _call_api_chat_slot_stop(state)
        body = json.loads(resp.body)

        assert body.get("ok") is True
        subs.cancel_for_parent.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_idle_outcome_still_cascades(self):
        """Even when the provider has no active turn, subagents are stopped.

        Subagents may outlive the parent turn that spawned them.
        """
        subs = _make_subagents_mock()
        slot = _FakeSlot()
        state = _FakeState(slot, subagents=subs)
        state.sessions.stop_turn = AsyncMock(return_value="idle")

        await _call_api_chat_slot_stop(state)

        subs.cancel_for_parent.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hard_stop_calls_cancel_for_parent(self):
        """A hard kill (second press) cascades to subagents."""
        subs = _make_subagents_mock()
        slot = _FakeSlot()
        slot._stop_state = "soft_pending"  # simulate first press already done
        state = _FakeState(slot, subagents=subs)
        state.sessions.stop_turn = AsyncMock(return_value="hard")

        await _call_api_chat_slot_stop(state, force=True)

        subs.cancel_for_parent.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_subagents_manager(self):
        """Gracefully handles state.subagents being None."""
        slot = _FakeSlot()
        state = _FakeState(slot, subagents=None)
        state.sessions.stop_turn = AsyncMock(return_value="soft")

        resp = await _call_api_chat_slot_stop(state)
        body = json.loads(resp.body)
        assert body.get("ok") is True

    @pytest.mark.asyncio
    async def test_no_subagents_attr(self):
        """Gracefully handles state object that lacks subagents entirely."""
        slot = _FakeSlot()
        state = _FakeState(slot, subagents=None)
        del state.subagents
        state.sessions.stop_turn = AsyncMock(return_value="soft")

        resp = await _call_api_chat_slot_stop(state)
        body = json.loads(resp.body)
        assert body.get("ok") is True

    @pytest.mark.asyncio
    async def test_cancel_for_parent_exception_swallowed(self):
        """An exception in cancel_for_parent must not block the stop."""
        subs = _make_subagents_mock()
        subs.cancel_for_parent = AsyncMock(side_effect=RuntimeError("db gone"))
        slot = _FakeSlot()
        state = _FakeState(slot, subagents=subs)
        state.sessions.stop_turn = AsyncMock(return_value="soft")

        resp = await _call_api_chat_slot_stop(state)
        body = json.loads(resp.body)
        assert body.get("ok") is True
        subs.cancel_for_parent.assert_awaited_once()


# ---------------------------------------------------------------------------
# Tests: stop_slot_turn — the shared helper must NOT cascade
# ---------------------------------------------------------------------------

class TestStopSlotTurnDoesNotCascade:
    """stop_slot_turn is called by steer-containment, session-control, and
    work-ledger board — none of which should cancel subagents.  The cascade
    must only happen in api_chat_slot_stop."""

    @pytest.mark.asyncio
    async def test_steer_containment_stop_does_not_cancel_subagents(self):
        """A steer-containment stop must NOT cancel subagents.

        The steer-containment code says: 'Stopping now would cancel work that
        never received this steer, which is worse than the exposure being
        narrowed.'  If stop_slot_turn cascaded, steer containment would
        violate that contract.
        """
        subs = _make_subagents_mock()
        slot = _FakeSlot()
        state = _FakeState(slot, subagents=subs)
        state.sessions.stop_turn = AsyncMock(return_value="soft")

        await _call_stop_slot_turn(
            state, slot, source="session_send_steer_containment"
        )

        subs.cancel_for_parent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_session_control_stop_does_not_cancel_subagents(self):
        """An agent-driven session_control stop must NOT cancel subagents."""
        subs = _make_subagents_mock()
        slot = _FakeSlot()
        state = _FakeState(slot, subagents=subs)
        state.sessions.stop_turn = AsyncMock(return_value="soft")

        await _call_stop_slot_turn(state, slot, source="session_control")

        subs.cancel_for_parent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_work_ledger_stop_does_not_cancel_subagents(self):
        """A work-ledger board item stop must NOT cancel subagents."""
        subs = _make_subagents_mock()
        slot = _FakeSlot()
        state = _FakeState(slot, subagents=subs)
        state.sessions.stop_turn = AsyncMock(return_value="soft")

        await _call_stop_slot_turn(state, slot, source="work_ledger_board")

        subs.cancel_for_parent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dashboard_source_stop_does_not_cascade_from_helper(self):
        """Even a dashboard-source stop through the helper does not cascade.

        The cascade lives in the HTTP route, not the helper — so a direct call
        to stop_slot_turn(source="dashboard") does NOT trigger it.
        """
        subs = _make_subagents_mock()
        slot = _FakeSlot()
        state = _FakeState(slot, subagents=subs)
        state.sessions.stop_turn = AsyncMock(return_value="soft")

        await _call_stop_slot_turn(state, slot, source="dashboard")

        subs.cancel_for_parent.assert_not_awaited()
