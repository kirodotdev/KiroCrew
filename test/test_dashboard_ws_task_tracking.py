"""Regression: WS broadcast tasks must be retained, not fire-and-forgotten.

``_send_ws_all`` / ``broadcast_ws_subagent_subscribers`` used
``asyncio.ensure_future(ws.send_str(msg))`` and discarded the returned task. Per the
asyncio docs the event loop keeps only a WEAK reference to such a task, so it can be
garbage-collected mid-send — silently dropping a websocket message (a lost dashboard
update). The fix routes both through ``_spawn_ws_send``, which retains a strong
reference in ``_background_tasks`` (the existing pattern in this module) until the task
completes. These tests assert the structural contract (the task is tracked), not the
GC race itself, so they are deterministic.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


class _FakeWS:
    """Minimal stand-in for web.WebSocketResponse with an awaitable send_str."""

    def __init__(self) -> None:
        self.closed = False
        self.sent: list[str] = []
        self._gate = asyncio.Event()  # held closed so the send task stays pending
        # These tests target the task-tracking guarantee, not the per-app
        # scope filter — flag as a dashboard user so _ws_client_allowed
        # returns True unconditionally.
        self._flags: dict = {"_is_dashboard_user": True}

    def get(self, key: str, default=None):
        return self._flags.get(key, default)

    async def send_str(self, msg: str) -> None:
        await self._gate.wait()  # stay pending until released
        self.sent.append(msg)


@pytest.mark.asyncio
async def test_send_ws_all_retains_task(tmp_path) -> None:
    state = _make_state(tmp_path)
    ws = _FakeWS()
    state._ws_clients.append(ws)

    assert len(state._background_tasks) == 0
    state._send_ws_all("ping", {}, '{"type": "ping"}')

    # The in-flight send task must be retained (strong ref) while pending.
    assert len(state._background_tasks) == 1, (
        "WS send task was not retained — it can be GC'd mid-send and drop the message"
    )

    # Release the gate; the task completes and is discarded via done-callback.
    ws._gate.set()
    await asyncio.sleep(0)  # let the task run + done-callback fire
    await asyncio.gather(*list(state._background_tasks), return_exceptions=True)
    await asyncio.sleep(0)
    assert ws.sent == ['{"type": "ping"}']
    assert len(state._background_tasks) == 0  # cleaned up after completion


@pytest.mark.asyncio
async def test_subagent_broadcast_retains_task(tmp_path) -> None:
    state = _make_state(tmp_path)
    ws = _FakeWS()
    state._ws_subagent_subscribers.add(ws)

    assert len(state._background_tasks) == 0
    state.broadcast_ws_subagent_subscribers("chunk", {"x": 1})
    assert len(state._background_tasks) == 1, "subagent WS broadcast task was not retained"

    ws._gate.set()
    await asyncio.gather(*list(state._background_tasks), return_exceptions=True)
    await asyncio.sleep(0)
    assert len(state._background_tasks) == 0


class _RaisingWS:
    """A WS whose send_str raises (client disconnected mid-send)."""

    def __init__(self) -> None:
        self.closed = False
        self._gate = asyncio.Event()

        # These tests target the task-tracking guarantee, not the per-app
        # scope filter — flag as a dashboard user so _ws_client_allowed
        # returns True unconditionally.
        self._flags: dict = {"_is_dashboard_user": True}

    def get(self, key: str, default=None):
        return self._flags.get(key, default)

    async def send_str(self, msg: str) -> None:
        await self._gate.wait()
        raise ConnectionResetError("client disconnected")


@pytest.mark.asyncio
async def test_failed_send_still_self_cleans(tmp_path) -> None:
    # A send that raises (peer disconnect) must NOT leak in _background_tasks — the
    # done-callback discards regardless of outcome and consumes the exception (so it
    # isn't surfaced as an unretrieved-task-exception warning).
    state = _make_state(tmp_path)
    ws = _RaisingWS()
    state._ws_clients.append(ws)
    state._send_ws_all("ping", {}, '{"type": "ping"}')
    assert len(state._background_tasks) == 1  # retained while pending

    ws._gate.set()
    await asyncio.gather(*list(state._background_tasks), return_exceptions=True)
    await asyncio.sleep(0)
    assert len(state._background_tasks) == 0  # self-cleaned despite the failure
