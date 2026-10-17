"""Reload and reset-conversation refuse while a SIBLING alias cold-starts on the session.

Two alias slots can drive one session: a channel-linked slot and its dashboard
twin both resolve to the same ``effective_session_key``. While alias B's first
turn is still inside ``provider.start()``, a teardown issued through alias A
sees neither per-slot signal: A's ``slot.running`` is False (the turn runs on
B's task) and ``get_provider(session_key)`` is None until B's session registers.

The five switch handlers refuse that window through ``_switch_target_busy``,
which also scans every running slot's TURN key. The two teardown routes did not
use it, so through A:

* ``reset-conversation`` dropped the resume pointer and answered ``reset: true``
  while B's start, which had already read that pointer, brought the old
  conversation back;
* ``reload`` found no registered session, answered ``ok`` with a "Reloading
  session" notice, and eagerly spawned a second start beside B's.

These tests hold B in exactly that window -- a live ``task``, no registered
provider -- and drive each route through A. Each must refuse with 409
``turn_in_flight`` and tear nothing down. Control cases assert the same request
goes through once B's turn is done, or when B's turn runs on another session.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat import api_chat_slot_reload, api_chat_slot_reset_conversation
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

_ALIAS_A = "alias-a"
_ALIAS_B = "alias-b"
# The one session both aliases run on (a channel-born key, as in production).
_SHARED_SESSION_KEY = "slack:1700000000.000100"
_LEGACY_BARE_KEY = _SHARED_SESSION_KEY.removeprefix("slack:")
_ELSEWHERE = "cron:job-elsewhere"

_RELOAD = f"/api/chat/slots/{_ALIAS_A}/reload"
_RESET = f"/api/chat/slots/{_ALIAS_A}/reset-conversation"


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
    app.router.add_post("/api/chat/slots/{slot}/reload", api_chat_slot_reload)
    app.router.add_post(
        "/api/chat/slots/{slot}/reset-conversation", api_chat_slot_reset_conversation
    )
    return app


def _alias(name: str) -> _ChatSlot:
    slot = _ChatSlot(name)
    slot.linked_session_key = _SHARED_SESSION_KEY
    return slot


@pytest.fixture
def alias_a() -> _ChatSlot:
    return _alias(_ALIAS_A)


@pytest.fixture
def alias_b() -> _ChatSlot:
    return _alias(_ALIAS_B)


@pytest.fixture
def state(alias_a: _ChatSlot, alias_b: _ChatSlot) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {alias_a.key: alias_a, alias_b.key: alias_b}
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock(return_value=True)
    state.sessions.discard_conversation = AsyncMock(return_value=True)
    # The window under test: B's provider.start() has not registered yet.
    state.sessions.get_provider = MagicMock(return_value=None)
    return state


@pytest.fixture(autouse=True)
def _isolate(_floor_monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    eager = MagicMock(return_value=None)
    _floor_monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", eager)
    # No children attached: the sub-agent gate is not what these tests are about.
    _floor_monkeypatch.setattr(
        chat_handlers, "subagents_attached_async", AsyncMock(return_value=False)
    )
    _floor_monkeypatch.setattr(chat_handlers, "sel", lambda: MagicMock())
    return eager


@contextlib.asynccontextmanager
async def _cold_start(slot: _ChatSlot) -> AsyncIterator[None]:
    """*slot* is mid-dispatch: its task is live, its provider is not registered.

    An ``async with`` helper rather than an async fixture, by this repo's
    convention (no async-fixture plugin is wired for these tests).
    """
    gate = asyncio.Event()

    async def _starting() -> None:
        await gate.wait()

    task = asyncio.create_task(_starting())
    slot.task = task
    assert slot.running
    try:
        yield
    finally:
        gate.set()
        await asyncio.gather(task, return_exceptions=True)


def _finished(slot: _ChatSlot) -> None:
    done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    done.set_result(None)
    slot.task = done
    assert not slot.running


async def _post(state: DashboardState, route: str) -> tuple[int, dict]:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(route)
        return resp.status, await resp.json()


def _assert_nothing_torn_down(state: DashboardState, alias_a: _ChatSlot, eager: MagicMock):
    state.sessions.reset.assert_not_awaited()
    state.sessions.discard_conversation.assert_not_awaited()
    eager.assert_not_called()
    assert alias_a.messages == []


class TestTeardownsRefuseASiblingColdStart:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", [_RELOAD, _RESET], ids=["reload", "reset-conversation"])
    async def test_refuses_while_sibling_alias_cold_starts(
        self, state, alias_a, alias_b, _isolate, route
    ):
        async with _cold_start(alias_b):
            assert not alias_a.running, "the alias the request names must look idle"
            assert state.sessions.get_provider(_SHARED_SESSION_KEY) is None
            status, body = await _post(state, route)
        assert status == 409, body
        assert body["code"] == "turn_in_flight"
        _assert_nothing_torn_down(state, alias_a, _isolate)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("switching_key", "sibling_key"),
        [
            pytest.param(_LEGACY_BARE_KEY, _SHARED_SESSION_KEY, id="legacy-vs-canonical"),
            pytest.param(_SHARED_SESSION_KEY, _LEGACY_BARE_KEY, id="canonical-vs-legacy"),
        ],
    )
    @pytest.mark.parametrize("route", [_RELOAD, _RESET], ids=["reload", "reset-conversation"])
    async def test_refuses_across_legacy_and_canonical_slack_spellings(
        self, state, alias_a, alias_b, _isolate, route, switching_key, sibling_key
    ):
        """A bare legacy ``thread_ts`` and its ``slack:`` sibling are ONE session."""
        alias_a.linked_session_key = switching_key
        alias_b.linked_session_key = sibling_key
        async with _cold_start(alias_b):
            status, body = await _post(state, route)
        assert status == 409, body
        assert body["code"] == "turn_in_flight"
        _assert_nothing_torn_down(state, alias_a, _isolate)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", [_RELOAD, _RESET], ids=["reload", "reset-conversation"])
    async def test_refuses_when_sibling_rebound_but_its_turn_runs_here(
        self, state, alias_a, alias_b, _isolate, route
    ):
        """B's routing moved off the session mid-turn; its published TURN key did not.

        A cron injection rebinds ``linked_session_key`` on a live slot with no
        ``running`` gate. The turn B started on the shared session is still
        what a teardown of that session would hit.
        """
        async with _cold_start(alias_b):
            alias_b._active_turn_session_key = _SHARED_SESSION_KEY
            alias_b.linked_session_key = _ELSEWHERE
            status, body = await _post(state, route)
        assert status == 409, body
        assert body["code"] == "turn_in_flight"
        _assert_nothing_torn_down(state, alias_a, _isolate)


class TestReloadRefusesItsOwnColdStart:
    @pytest.mark.asyncio
    async def test_refuses_while_this_slot_cold_starts(self, state, alias_a, _isolate):
        """The reservation half of the same predicate, which reload also lacked.

        A first turn dispatched through THIS slot has no registered provider for
        the whole of ``provider.start()``. The menu already disables Reload
        while the slot runs; the route must refuse the same window rather than
        spawn a second start beside the one in flight.
        """
        async with _cold_start(alias_a):
            status, body = await _post(state, _RELOAD)
        assert status == 409, body
        assert body["code"] == "turn_in_flight"
        _assert_nothing_torn_down(state, alias_a, _isolate)


class TestSiblingColdStartDuringTheChildrenProbe:
    """B starts cold AFTER the route's early checks, while the children probe awaits.

    The children probe is the last await before each teardown, and message
    dispatch takes none of the locks the routes hold, so a sibling's turn can
    begin inside it. The reset's ``skip_if_busy`` sees only a REGISTERED
    session, so a busy read taken before that await would let the teardown
    through. The read must come after it.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", [_RELOAD, _RESET], ids=["reload", "reset-conversation"])
    async def test_refuses_when_sibling_starts_inside_the_probe(
        self, state, alias_a, alias_b, _isolate, monkeypatch, route
    ):
        gate = asyncio.Event()

        async def _probe_then_cold_start(*_args, **_kwargs) -> bool:
            async def _starting() -> None:
                await gate.wait()

            if alias_b.task is None:
                alias_b.task = asyncio.create_task(_starting())
            await asyncio.sleep(0)
            return False

        monkeypatch.setattr(chat_handlers, "subagents_attached_async", _probe_then_cold_start)
        try:
            assert not alias_b.running, "B must be idle when the early checks run"
            status, body = await _post(state, route)
            assert alias_b.running, "the probe stub did not start the sibling"
        finally:
            gate.set()
            if alias_b.task is not None:
                await asyncio.gather(alias_b.task, return_exceptions=True)
        assert status == 409, body
        assert body["code"] == "turn_in_flight"
        _assert_nothing_torn_down(state, alias_a, _isolate)


class TestTeardownsProceedWithoutASiblingTurn:
    @pytest.mark.asyncio
    async def test_reload_proceeds_once_sibling_turn_is_done(
        self, state, alias_a, alias_b, _isolate
    ):
        _finished(alias_b)
        status, body = await _post(state, _RELOAD)
        assert status == 200, body
        state.sessions.reset.assert_awaited_once_with(_SHARED_SESSION_KEY, skip_if_busy=True)
        _isolate.assert_called_once()

    @pytest.mark.asyncio
    async def test_reset_proceeds_once_sibling_turn_is_done(self, state, alias_a, alias_b):
        _finished(alias_b)
        status, body = await _post(state, _RESET)
        assert status == 200, body
        assert body["reset"] is True
        state.sessions.discard_conversation.assert_awaited_once_with(
            _SHARED_SESSION_KEY, replay=True, skip_if_busy=True
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", [_RELOAD, _RESET], ids=["reload", "reset-conversation"])
    async def test_proceeds_when_sibling_turn_runs_on_another_session(
        self, state, alias_a, alias_b, route
    ):
        """Another slot running is not this session being busy."""
        async with _cold_start(alias_b):
            alias_b._active_turn_session_key = _ELSEWHERE
            alias_b.linked_session_key = _ELSEWHERE
            status, body = await _post(state, route)
        assert status == 200, body
        awaited = state.sessions.reset if route == _RELOAD else state.sessions.discard_conversation
        awaited.assert_awaited_once()
        assert awaited.await_args.args[0] == _SHARED_SESSION_KEY
