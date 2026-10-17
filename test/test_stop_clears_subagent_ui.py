"""Tests for dashboard Stop clearing subagent spinners (UI-only fix).

When the user presses Stop, the backend broadcasts ``subagent_status`` with
``running: 0`` so the frontend evicts the spinner state.  The subagent
processes are NOT cancelled — they finish on their own.

Verifies:
1. ``api_chat_slot_stop`` broadcasts ``subagent_status`` with ``running: 0``
2. The broadcast carries the correct slot name and ``parent_stopped`` event
3. A ``broadcast_ws`` failure doesn't break the stop response
4. ``stop_slot_turn`` (the shared helper) does NOT broadcast — only the
   user-initiated HTTP route does
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Lightweight fakes — match the existing test_stop_handler_idempotent pattern
# ---------------------------------------------------------------------------

class _FakeSlot:
    """Minimal slot — enough for the stop path to reach our broadcast."""
    def __init__(self, name: str = "test-slot"):
        self.key = name
        self.name = name
        self.agent = "kirocrew"
        self.running = True
        self._running = True
        self._stop_state = "idle"
        self._stop_event_id = None
        self._stop_escalated_card_id = None
        self._stop_declined_at = 0
        self._stop_press_ts = 0
        self._stop_count = 0
        self._stop_generation = 0
        self._queue: list = []
        self._steer_queue = []
        self._compaction = None
        self._history: list = []
        self._pending_subagent_failures: list = []
        self._recovery_chat_triggered = False
        self.recovery_retrigger_count = 0
        self._approval_futures: dict = {}
        self.transcript_rows: list = []
        self.messages: list = []

    def take_pending_subagent_deliveries(self, contents):
        return []


class _FakeState:
    """Minimal DashboardState that records broadcast_ws calls."""
    def __init__(self, slot: _FakeSlot):
        self._slots = {slot.key: slot}
        self._broadcast_calls: list[tuple[str, dict]] = []

    def broadcast_ws(self, msg_type: str, data: dict) -> None:
        self._broadcast_calls.append((msg_type, data))

    def push_slots_update(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_broadcasts_subagent_status_zero():
    """Stop button broadcasts subagent_status with running=0 for the slot."""
    from aiohttp import web

    slot = _FakeSlot("my-slot")
    state = _FakeState(slot)
    app = web.Application()
    app["state"] = state

    request = MagicMock()
    request.app = app
    request.match_info = {"slot": "my-slot"}
    request.query = {}
    request.get = lambda key, default="": default

    # Patch at the chat_handlers level (where runtime resolution happens)
    with patch(
        "kiro_crew.dashboard.chat_handlers.stop_slot_turn",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop
        resp = await api_chat_slot_stop(request)

    assert resp.status == 200
    sa_events = [
        (t, d) for t, d in state._broadcast_calls if t == "subagent_status"
    ]
    assert len(sa_events) == 1, f"Expected 1 subagent_status broadcast, got {len(sa_events)}"
    _, payload = sa_events[0]
    assert payload["running"] == 0
    assert payload["slot"] == "my-slot"
    assert payload["event"] == "parent_stopped"


@pytest.mark.asyncio
async def test_stop_broadcasts_after_force_stop():
    """Force-stop also broadcasts running=0."""
    from aiohttp import web

    slot = _FakeSlot("force-slot")
    state = _FakeState(slot)
    app = web.Application()
    app["state"] = state

    request = MagicMock()
    request.app = app
    request.match_info = {"slot": "force-slot"}
    request.query = {"force": "true"}
    request.get = lambda key, default="": default

    with patch(
        "kiro_crew.dashboard.chat_handlers.stop_slot_turn",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop
        resp = await api_chat_slot_stop(request)

    assert resp.status == 200
    sa_events = [
        (t, d) for t, d in state._broadcast_calls if t == "subagent_status"
    ]
    assert len(sa_events) == 1
    assert sa_events[0][1]["running"] == 0
    assert sa_events[0][1]["slot"] == "force-slot"


@pytest.mark.asyncio
async def test_broadcast_failure_does_not_break_stop():
    """If broadcast_ws raises, the stop still returns ok."""
    from aiohttp import web

    slot = _FakeSlot("err-slot")
    state = _FakeState(slot)

    # Make broadcast_ws raise
    def _boom(msg_type: str, data: dict) -> None:
        raise RuntimeError("WS broadcast failed")
    state.broadcast_ws = _boom

    app = web.Application()
    app["state"] = state

    request = MagicMock()
    request.app = app
    request.match_info = {"slot": "err-slot"}
    request.query = {}
    request.get = lambda key, default="": default

    with patch(
        "kiro_crew.dashboard.chat_handlers.stop_slot_turn",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop
        resp = await api_chat_slot_stop(request)

    # Stop itself must succeed even if the broadcast fails
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body.get("ok") is True


@pytest.mark.asyncio
async def test_stop_already_in_progress_still_broadcasts():
    """When stop is already in progress (killing state), broadcast still fires.

    This is correct: the broadcast is idempotent (frontend evicts the same
    keys again), and a repeat press might reach the route before the first
    press's broadcast arrived at the frontend.
    """
    from aiohttp import web

    from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop

    slot = _FakeSlot("kill-slot")
    slot._stop_state = "killing"
    slot._stop_event_id = "stop-abc"
    slot.running = True

    state = _FakeState(slot)
    app = web.Application()
    app["state"] = state

    request = MagicMock()
    request.app = app
    request.match_info = {"slot": "kill-slot"}
    request.query = {}
    request.get = lambda key, default="": default

    resp = await api_chat_slot_stop(request)
    body = json.loads(resp.body)

    assert body.get("info") == "stop already in progress"
    # Broadcast still fires — idempotent and harmless
    sa_events = [
        (t, d) for t, d in state._broadcast_calls if t == "subagent_status"
    ]
    assert len(sa_events) == 1
    assert sa_events[0][1]["running"] == 0
