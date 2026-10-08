"""Dashboard Stop against a turn whose provider reports no active turn.

A turn wedged inside a dispatched, mid-execution tool call that never acks a
cooperative cancel reaches ``stop_turn`` as a non-acked outcome, which that
method escalates to a hard reset itself. The separate ``"idle"`` outcome means
the provider holds no active turn to cancel: the model stream reached its done
boundary, or no session is registered for the key. When the slot still reads
running at that point the turn ended at the provider but the slot has not seen
the terminal event settle it.

The handler resolves the orphaned card and names the honest state for that
case: ``{"ok": True, "info": "no active turn"}`` rather than a bare
``{"ok": True}`` that reads as "stopped a running turn". It does NOT force a
hard reset -- there is no live provider turn to kill, so a reset would
cold-restart a healthy backend that is merely finishing its post-stream tail.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _FakeSlot:
    """Minimal ChatSlot stand-in, mirroring test_stop_handler_idempotent."""

    def __init__(self):
        self._stop_state = "idle"
        self._stop_generation = 0
        self._stop_event_id = None
        self._stop_escalated_card_id = None
        self.task = None
        self._stop_teardown_task = None
        self._queue: list[dict] = []
        self._pending_steers: list = []
        self._steer_delivery_ids: dict = {}
        self._steer_send_ids: dict = {}
        self._steer_user_origin: dict = {}
        self._steer_channel_origin: dict = {}
        self._steer_admissions: dict = {}
        self._steer_attachment_meta: dict = {}
        self._steer_decision_strips: dict = {}
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

    def append(self, role, content, cls_meta):
        self.messages.append({"role": role, "content": content, "cls": cls_meta})

    def invalidate_source_links(self):
        self.source_links_invalidated += 1


class _FakeState:
    """Minimal DashboardState stand-in."""

    def __init__(self, slot):
        self._slots = {"test-slot": slot}
        self.sessions = MagicMock()
        self._push_count = 0

    def push_slots_update(self):
        self._push_count += 1

    def cancel_questions_for_slot(self, slot_key):
        return 0


def _request(state):
    from aiohttp import web

    app = web.Application()
    app["state"] = state
    request = MagicMock()
    request.get = lambda key, default="": default
    request.app = app
    request.match_info = {"slot": "test-slot"}
    request.query = {}
    return request


class TestStopIdleWhileRunning:
    @pytest.mark.asyncio
    async def test_idle_on_a_running_slot_reports_honestly_without_a_reset(self):
        """A cooperative stop that reports idle while the slot still reads
        running names the honest state and does not force a hard reset.

        The provider has no active turn to cancel (the model stream ended), so
        the reply must read ``info: "no active turn"`` instead of a bare
        success that claims a running turn was stopped, and no forced reset is
        issued against the still-settling backend.
        """
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

        slot = _FakeSlot()
        slot.running = True

        state = _FakeState(slot)
        calls: list[dict] = []

        async def _stop_turn(_key, force=False, preserve_queue=False, on_soft=None, on_hard=None):
            calls.append({"force": force})
            # The provider holds no active turn to cancel.
            return "idle"

        state.sessions.stop_turn = AsyncMock(side_effect=_stop_turn)

        with patch("kiro_crew.dashboard.chat_handlers.sel") as mock_sel:
            mock_sel.return_value.log_tool_invocation = MagicMock()
            mock_sel.return_value.log = MagicMock()
            with patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"):
                resp = await api_chat_slot_stop(_request(state))

        body = json.loads(resp.body)

        # No forced reset: an idle provider turn is nothing to hard-kill, and a
        # reset would cold-restart a healthy backend finishing its tail.
        assert not any(c["force"] for c in calls), (
            "an idle provider turn must not be force-reset -- there is no live " "turn to kill"
        )
        # The reply reflects reality rather than claiming a running turn stopped.
        assert body.get("ok") is True
        assert body.get("info") == "no active turn"

    @pytest.mark.asyncio
    async def test_idle_on_a_settled_slot_keeps_the_plain_reply(self):
        """A turn that settled during the cooperative cancel keeps the plain
        reply.

        The terminal event flips the slot to not-running before idle lands, so
        the ``"no active turn"`` info -- reserved for a slot that still reads
        running -- is not attached.
        """
        from kiro_crew.dashboard.chat_handlers import stop_slot_turn

        slot = _FakeSlot()
        slot.running = True

        state = _FakeState(slot)
        calls: list[dict] = []

        async def _stop_turn(_key, force=False, preserve_queue=False, on_soft=None, on_hard=None):
            calls.append({"force": force})
            # The turn settles during the cooperative cancel: the slot reads
            # not-running by the time idle lands.
            slot.running = False
            return "idle"

        state.sessions.stop_turn = AsyncMock(side_effect=_stop_turn)

        with patch("kiro_crew.dashboard.chat_handlers.sel") as mock_sel:
            mock_sel.return_value.log_tool_invocation = MagicMock()
            mock_sel.return_value.log = MagicMock()
            with patch("kiro_crew.dashboard.chat_handlers._reject_pending_approvals"):
                body = await stop_slot_turn(state, slot)

        assert not any(c["force"] for c in calls), "a settled turn must not be force-reset"
        assert body.get("ok") is True
        assert body.get("info") != "no active turn"
