"""Slot-switch handlers refuse a slot recreated under the same name while queued.

Every switch handler reads ``state._slots.get(name)`` before its first await,
then queues on ``slot._lock`` and on the session-keyed switch lock (the model
paths add ``slot._model_pick_lock``). Slot removal and same-name re-registration
take neither lock, so the name can belong to a DIFFERENT slot object -- possibly
a different app's -- by the time the request resumes. The app-isolation check
that follows reads the STALE object, so without an identity re-check a request
authorized against the old slot lands its reset on the new slot's session.

``api_chat_slot_reload`` carries that re-check (pinned by
``test_slot_reset_interleaving_seam.py``). These tests drive the same window
through the five pre-existing switch handlers that share reload's queueing
shape -- agent, model, bulk model, reasoning effort, workspace -- one test per
lock-acquisition await, and assert each refuses without ever touching the
replacement's session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat import (
    api_chat_slot_agent,
    api_chat_slot_model,
    api_chat_slot_reasoning_effort,
    api_chat_slot_workspace,
    api_chat_slots_model,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

_MODEL_OLD = "claude-opus-4.8"
_MODEL_NEW = "gpt-5.6-sol"
_SLOT = "s1"
_SESSION_KEY = f"dashboard:{_SLOT}"

# Scheduler turns a bounded yield will spend before giving up -- see the
# sibling seam tests for why turns, not seconds.
_MAX_TURNS = 500


async def _yield_until(predicate: Callable[[], bool]) -> bool:
    for _ in range(_MAX_TURNS):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


def _make_app(state: DashboardState) -> web.Application:
    # Mirror production: token_auth sets request["app"] on every authenticated
    # path ("" = dashboard user); the isolation guards fail closed without it.
    @web.middleware
    async def dashboard_auth_marker(request, handler):
        if "app" not in request:
            request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[dashboard_auth_marker])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/agent", api_chat_slot_agent)
    app.router.add_post("/api/chat/slots/{slot}/model", api_chat_slot_model)
    app.router.add_post("/api/chat/slots/{slot}/reasoning-effort", api_chat_slot_reasoning_effort)
    app.router.add_post("/api/chat/slots/{slot}/workspace", api_chat_slot_workspace)
    app.router.add_post("/api/chat/slots/model", api_chat_slots_model)
    return app


def _idle_provider() -> MagicMock:
    provider = MagicMock()
    provider.has_active_turn = MagicMock(return_value=False)
    return provider


def _mock_state(slot: _ChatSlot) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock(return_value=True)
    state.sessions.get_provider = MagicMock(return_value=_idle_provider())
    return state


@pytest.fixture
def slot() -> _ChatSlot:
    s = _ChatSlot(_SLOT)
    s.model = _MODEL_OLD
    s.agent = "old-agent"
    s.workspace = "default"
    s.reasoning_effort = ""
    return s


@pytest.fixture
def state(slot: _ChatSlot) -> DashboardState:
    return _mock_state(slot)


@pytest.fixture
def sel_log(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake = MagicMock()
    monkeypatch.setattr(chat_handlers, "sel", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def _no_side_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", MagicMock(return_value=None))
    monkeypatch.setattr(chat_handlers, "_broadcast_context_reset", MagicMock(return_value=None))


def _replacement() -> _ChatSlot:
    r = _ChatSlot(_SLOT)
    r.model = _MODEL_OLD
    r.agent = "old-agent"
    r.workspace = "default"
    r.reasoning_effort = ""
    return r


# (route, body, SEL operation name) for the four single-slot switch handlers.
_SINGLE_SLOT_SWITCHES = [
    pytest.param(
        f"/api/chat/slots/{_SLOT}/agent", {"agent": "new-agent"}, "chat.slot_agent", id="agent"
    ),
    pytest.param(
        f"/api/chat/slots/{_SLOT}/model", {"model": _MODEL_NEW}, "chat.slot_model", id="model"
    ),
    pytest.param(
        f"/api/chat/slots/{_SLOT}/reasoning-effort",
        {"reasoning_effort": "high"},
        "chat.slot_reasoning_effort",
        id="effort",
    ),
    pytest.param(
        f"/api/chat/slots/{_SLOT}/workspace",
        {"workspace": "other"},
        "chat.slot_workspace",
        id="workspace",
    ),
]


async def _race_recreate_while_queued(
    state: DashboardState,
    held_lock: asyncio.Lock,
    route: str,
    body: dict,
) -> tuple[int, dict, _ChatSlot]:
    """POST *route* while *held_lock* is held, swap the slot, release, collect.

    Holding the lock externally is what parks the request on exactly one
    lock-acquisition await: it has already read the (still-current) slot and
    passed every check before that await. The swap then lands while it is
    queued, and whatever the handler does next runs against the STALE object.
    """
    replacement = _replacement()
    async with TestClient(TestServer(_make_app(state))) as client:
        async with held_lock:
            task = asyncio.create_task(client.post(route, json=body))
            stuck = not await _yield_until(lambda: task.done())
            assert stuck, f"{route} completed without ever contending for the held lock"
            # Same shape a delete-then-recreate under one name produces.
            state._slots[_SLOT] = replacement
        try:
            finished = await _yield_until(lambda: task.done())
            assert finished, f"{route} never completed after the lock was released"
            resp = task.result()
            status = resp.status
            payload = await resp.json()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    return status, payload, replacement


def _assert_denied_row(sel_log: MagicMock, operation: str) -> None:
    rows = [
        c.kwargs
        for c in sel_log.log_api_access.call_args_list
        if c.kwargs.get("outcome") == "denied" and c.kwargs.get("operation") == operation
    ]
    assert rows, f"no SEL api_access denied row for {operation}"
    assert rows[-1]["resources"] == f"slot={_SLOT}"


class TestSingleSlotSwitchesRefuseARecreatedSlot:
    """agent / model / effort / workspace: 404 + no reset, at each await."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("route", "body", "operation"), _SINGLE_SLOT_SWITCHES)
    async def test_refuses_when_recreated_while_queued_on_slot_lock(
        self, state, slot, sel_log, route, body, operation
    ):
        status, payload, replacement = await _race_recreate_while_queued(
            state, slot._lock, route, body
        )
        assert status == 404
        assert payload["code"] == "slot_not_found"
        state.sessions.reset.assert_not_awaited()
        assert state._slots[_SLOT] is replacement
        _assert_denied_row(sel_log, operation)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("route", "body", "operation"), _SINGLE_SLOT_SWITCHES)
    async def test_refuses_when_recreated_while_queued_on_session_lock(
        self, state, slot, sel_log, route, body, operation
    ):
        """The SECOND await: past slot._lock and its re-check, parked on the session lock."""
        session_lock = chat_handlers._slot_switch_session_lock(_SESSION_KEY)
        status, payload, replacement = await _race_recreate_while_queued(
            state, session_lock, route, body
        )
        assert status == 404
        assert payload["code"] == "slot_not_found"
        state.sessions.reset.assert_not_awaited()
        assert state._slots[_SLOT] is replacement
        _assert_denied_row(sel_log, operation)

    @pytest.mark.asyncio
    async def test_model_refuses_when_recreated_while_queued_on_model_pick_lock(
        self, state, slot, sel_log
    ):
        """The model handler's THIRD await, ``slot._model_pick_lock``."""
        status, payload, replacement = await _race_recreate_while_queued(
            state, slot._model_pick_lock, f"/api/chat/slots/{_SLOT}/model", {"model": _MODEL_NEW}
        )
        assert status == 404
        assert payload["code"] == "slot_not_found"
        state.sessions.reset.assert_not_awaited()
        assert state._slots[_SLOT] is replacement
        _assert_denied_row(sel_log, "chat.slot_model")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("route", "body", "operation"), _SINGLE_SLOT_SWITCHES)
    async def test_uncontended_switch_still_proceeds(
        self, state, slot, sel_log, route, body, operation
    ):
        """Control: the re-check is a no-op when the slot is still the registered one."""
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(route, json=body)
            assert resp.status != 404, await resp.text()
        denied = [
            c
            for c in sel_log.log_api_access.call_args_list
            if c.kwargs.get("outcome") == "denied" and c.kwargs.get("operation") == operation
        ]
        assert denied == []


class TestBulkModelSwitchSkipsARecreatedSlot:
    """The bulk handler iterates a snapshot; a slot replaced mid-iteration is skipped, not reset."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("which", ["slot_lock", "session_lock", "model_pick_lock"])
    async def test_skips_when_recreated_while_queued(self, state, slot, sel_log, which):
        held = {
            "slot_lock": slot._lock,
            "session_lock": chat_handlers._slot_switch_session_lock(_SESSION_KEY),
            "model_pick_lock": slot._model_pick_lock,
        }[which]
        status, payload, replacement = await _race_recreate_while_queued(
            state, held, "/api/chat/slots/model", {"model": _MODEL_NEW}
        )
        assert status == 200
        # Reported like the rebound case: skipped so the caller retries against
        # whatever the name resolves to now, not switched, not failed.
        assert _SLOT not in payload["switched"]
        assert _SLOT not in payload["failed"]
        assert _SLOT in payload["skipped_running"]
        state.sessions.reset.assert_not_awaited()
        assert state._slots[_SLOT] is replacement
        assert replacement.model == _MODEL_OLD
        _assert_denied_row(sel_log, "chat.slots_model")
