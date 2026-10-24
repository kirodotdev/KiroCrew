"""Verify that the dashboard Stop button broadcasts ``parent_stopped``."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web


# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------

class _FakeSlot:
    def __init__(self, key: str = "slot-1") -> None:
        self.key = key
        self._active_turn_session_key = "session-1"
        self._stop_state = "idle"
        self._stop_declined_at = 0.0
        self._stop_escalated_card_id: str | None = None
        self.linked_session_key = "session-1"
        self.messages: list[dict[str, Any]] = []
        self._pending_subagent_deliveries: list[str] = []
        self._approval_futures: dict[str, Any] = {}
        self._on_message = None
        self._dirty = False

    def take_pending_subagent_deliveries(self, _: list[str]) -> list[str]:
        return []

    def invalidate_source_links(self) -> None:
        pass


class _FakeState:
    def __init__(self, slot: _FakeSlot | None = None) -> None:
        self._slots = {slot.key: slot} if slot else {}
        self._broadcast_calls: list[tuple[str, dict]] = []

    def broadcast_ws(self, msg_type: str, data: Any) -> None:
        self._broadcast_calls.append((msg_type, data))

    def push_slots_update(self) -> None:
        pass


# Patch targets — turn_stop uses TYPE_CHECKING imports from chat_handlers,
# so at runtime they're globals on turn_stop's module only if we inject them.
TS = "kiro_crew.dashboard.chat_api.turn_stop"


@pytest.mark.asyncio
async def test_stop_broadcasts_parent_stopped() -> None:
    """api_chat_slot_stop broadcasts parent_stopped after stopping the turn."""
    from kiro_crew.dashboard.chat_api.turn_stop import api_chat_slot_stop

    slot = _FakeSlot()
    state = _FakeState(slot)

    request = MagicMock(spec=web.Request)
    request.match_info = {"slot": slot.key}
    request.query = {}
    request.get.return_value = ""
    request.app = {"state": state}

    with (
        patch(f"{TS}.deny_app_slot_access", return_value=None, create=True),
        patch(f"{TS}._app_cancel_denied", return_value=None, create=True),
        patch(f"{TS}.slot_not_found", return_value=web.json_response({"error": "not found"}, status=404), create=True),
        patch(f"{TS}.stop_slot_turn", new_callable=AsyncMock, return_value={"ok": True}, create=True),
    ):
        resp = await api_chat_slot_stop(request)

    assert resp.status == 200
    parent_stopped = [
        (t, d) for t, d in state._broadcast_calls if t == "parent_stopped"
    ]
    assert len(parent_stopped) == 1
    assert parent_stopped[0][1] == {"slot": slot.key}


@pytest.mark.asyncio
async def test_broadcast_failure_does_not_break_stop() -> None:
    """If broadcast_ws raises, the stop still succeeds."""
    from kiro_crew.dashboard.chat_api.turn_stop import api_chat_slot_stop

    slot = _FakeSlot()
    state = _FakeState(slot)

    def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("WS hub down")

    state.broadcast_ws = boom  # type: ignore[assignment]

    request = MagicMock(spec=web.Request)
    request.match_info = {"slot": slot.key}
    request.query = {}
    request.get.return_value = ""
    request.app = {"state": state}

    with (
        patch(f"{TS}.deny_app_slot_access", return_value=None, create=True),
        patch(f"{TS}._app_cancel_denied", return_value=None, create=True),
        patch(f"{TS}.slot_not_found", return_value=web.json_response({"error": "not found"}, status=404), create=True),
        patch(f"{TS}.stop_slot_turn", new_callable=AsyncMock, return_value={"ok": True}, create=True),
    ):
        resp = await api_chat_slot_stop(request)

    assert resp.status == 200


@pytest.mark.asyncio
async def test_no_broadcast_on_slot_not_found() -> None:
    """A missing slot returns 404 with no broadcast."""
    from kiro_crew.dashboard.chat_api.turn_stop import api_chat_slot_stop

    state = _FakeState()

    request = MagicMock(spec=web.Request)
    request.match_info = {"slot": "nonexistent"}
    request.query = {}
    request.get.return_value = ""
    request.app = {"state": state}

    with patch(f"{TS}.slot_not_found", return_value=web.json_response({"error": "not found"}, status=404), create=True):
        resp = await api_chat_slot_stop(request)

    assert resp.status == 404
    assert len(state._broadcast_calls) == 0
