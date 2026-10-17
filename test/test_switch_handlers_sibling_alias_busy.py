"""Slot-switch handlers refuse while a SIBLING alias cold-starts on the shared session.

Two alias slots can drive one session: a channel-linked slot and its dashboard
twin both resolve to the same ``effective_session_key``. A switch issued
through alias A while alias B is cold-starting is invisible to the two
per-slot signals -- A's ``slot.running`` is False (the turn was dispatched
through B's task) and ``get_provider(session_key)`` is None until B's
multi-second ``provider.start()`` registers a session. Without a third signal
the switch commits, resets nothing, and reports success while the session
comes up on B's captured (old) bindings.

The third signal scans every running slot's TURN key (``_cancel_target``: the
identity its in-flight turn published, else its routing), so it sees B's cold
start under the shared key -- and keeps seeing it after a mid-turn rebind moves
B's routing elsewhere. These tests model exactly that window -- B's ``task``
set, no registered provider -- and drive it through each of the five switch
handlers via A. Each must refuse (409 ``turn_in_flight``; the bulk handler
lists A in ``skipped_running``), never await the reset, and leave A's bindings
untouched. Control cases assert the same switch proceeds once B's turn is done.
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
_ALIAS_A = "alias-a"
_ALIAS_B = "alias-b"
# The one session both aliases run on (a channel-born key, as in production).
_SHARED_SESSION_KEY = "slack:1700000000.000100"


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


def _alias(name: str) -> _ChatSlot:
    s = _ChatSlot(name)
    s.model = _MODEL_OLD
    s.agent = "old-agent"
    s.workspace = "default"
    s.reasoning_effort = ""
    s.linked_session_key = _SHARED_SESSION_KEY
    return s


def _mock_state(*slots: _ChatSlot) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {s.key: s for s in slots}
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock(return_value=True)
    # The window under test: B's provider.start() has not registered yet.
    state.sessions.get_provider = MagicMock(return_value=None)
    # No transcript store: the control cases run the commit path to its 200,
    # and the metadata persist is skipped when there is nothing to write to.
    state.conversation_log = None
    return state


@pytest.fixture
def alias_a() -> _ChatSlot:
    return _alias(_ALIAS_A)


@pytest.fixture
def alias_b() -> _ChatSlot:
    return _alias(_ALIAS_B)


@pytest.fixture
def state(alias_a: _ChatSlot, alias_b: _ChatSlot) -> DashboardState:
    return _mock_state(alias_a, alias_b)


@contextlib.asynccontextmanager
async def _sibling_cold_start(alias_b: _ChatSlot) -> AsyncIterator[None]:
    """B is mid-dispatch: its task is live, its provider is not registered.

    An ``async with`` helper rather than an async fixture, by this repo's
    convention (no async-fixture plugin is wired for these tests).
    """
    gate = asyncio.Event()

    async def _cold_start() -> None:
        await gate.wait()

    task = asyncio.create_task(_cold_start())
    alias_b.task = task
    assert alias_b.running
    try:
        yield
    finally:
        gate.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.fixture(autouse=True)
def _no_side_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", MagicMock(return_value=None))
    monkeypatch.setattr(chat_handlers, "_broadcast_context_reset", MagicMock(return_value=None))
    monkeypatch.setattr(chat_handlers, "sel", lambda: MagicMock())


# (route, body, slot attribute the handler commits, its pre-switch value).
_SINGLE_SLOT_SWITCHES = [
    pytest.param(
        f"/api/chat/slots/{_ALIAS_A}/agent",
        {"agent": "new-agent"},
        "agent",
        "old-agent",
        id="agent",
    ),
    pytest.param(
        f"/api/chat/slots/{_ALIAS_A}/model",
        {"model": _MODEL_NEW},
        "model",
        _MODEL_OLD,
        id="model",
    ),
    pytest.param(
        f"/api/chat/slots/{_ALIAS_A}/reasoning-effort",
        {"reasoning_effort": "high"},
        "reasoning_effort",
        "",
        id="effort",
    ),
    pytest.param(
        f"/api/chat/slots/{_ALIAS_A}/workspace",
        {"workspace": "other"},
        "workspace",
        "default",
        id="workspace",
    ),
]


class TestSingleSlotSwitchesRefuseASiblingColdStart:
    """agent / model / effort / workspace through A: 409, no reset, nothing committed."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("route", "body", "field", "old_value"), _SINGLE_SLOT_SWITCHES)
    async def test_refuses_while_sibling_alias_cold_starts(
        self, state, alias_a, alias_b, route, body, field, old_value
    ):
        async with _sibling_cold_start(alias_b):
            assert not alias_a.running, "the switching alias itself must look idle"
            assert state.sessions.get_provider(_SHARED_SESSION_KEY) is None
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(route, json=body)
                assert resp.status == 409, await resp.text()
                payload = await resp.json()
        assert payload["code"] == "turn_in_flight"
        state.sessions.reset.assert_not_awaited()
        assert getattr(alias_a, field) == old_value

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("route", "body", "field", "old_value"), _SINGLE_SLOT_SWITCHES)
    async def test_proceeds_once_sibling_turn_is_done(
        self, state, alias_a, alias_b, route, body, field, old_value
    ):
        """Control: a finished sibling task is not 'running', so the same switch goes through."""
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        done.set_result(None)
        alias_b.task = done
        assert not alias_b.running
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(route, json=body)
            assert resp.status == 200, await resp.text()
        assert getattr(alias_a, field) != old_value


class TestBulkModelSwitchSkipsASiblingColdStart:
    """The bulk handler skips A (skip_running default) rather than reset the shared session."""

    @pytest.mark.asyncio
    async def test_skips_while_sibling_alias_cold_starts(self, state, alias_a, alias_b):
        async with _sibling_cold_start(alias_b):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_NEW})
                assert resp.status == 200, await resp.text()
                payload = await resp.json()
        assert _ALIAS_A in payload["skipped_running"]
        assert _ALIAS_A not in payload["switched"]
        assert _ALIAS_A not in payload["failed"]
        state.sessions.reset.assert_not_awaited()
        assert alias_a.model == _MODEL_OLD

    @pytest.mark.asyncio
    async def test_switches_once_sibling_turn_is_done(self, state, alias_a, alias_b):
        """Control: with B idle the same request switches A."""
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        done.set_result(None)
        alias_b.task = done
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_NEW})
            assert resp.status == 200, await resp.text()
            payload = await resp.json()
        assert _ALIAS_A in payload["switched"]
        assert alias_a.model == _MODEL_NEW


class TestSiblingColdStartDuringTheLastAwait:
    """The sibling's task appears AFTER the pre-commit check, during the last await before reset.

    The pre-commit check runs before the handler's remaining awaits (the
    agent's resolution warm-up, the model's live-switch RPCs, and every
    handler's children probe). A sibling alias that begins cold-starting
    during one of those is invisible to a check that already ran, so each
    handler re-probes the same predicate in the no-await window right before
    its reset -- and, where it has already committed, unwinds the commit.
    The children probe is the last await they all share, so the sibling's
    task is started from inside it.
    """

    @staticmethod
    def _start_sibling_inside_children_probe(
        monkeypatch: pytest.MonkeyPatch, alias_b: _ChatSlot, gate: asyncio.Event
    ) -> None:
        async def _probe_then_cold_start(*_args, **_kwargs) -> bool:
            async def _cold_start() -> None:
                await gate.wait()

            if alias_b.task is None:
                alias_b.task = asyncio.create_task(_cold_start())
            await asyncio.sleep(0)
            return False

        monkeypatch.setattr(chat_handlers, "subagents_attached_async", _probe_then_cold_start)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("route", "body", "field", "old_value"),
        # Workspace is not listed: its check already sits after its last await
        # (the children probe precedes it), so this window does not exist there.
        [p for p in _SINGLE_SLOT_SWITCHES if p.id != "workspace"],
    )
    async def test_refuses_and_restores_when_sibling_appears_before_reset(
        self, state, alias_a, alias_b, monkeypatch, route, body, field, old_value
    ):
        gate = asyncio.Event()
        self._start_sibling_inside_children_probe(monkeypatch, alias_b, gate)
        try:
            assert not alias_b.running, "B must be idle when the pre-commit check runs"
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(route, json=body)
                assert resp.status == 409, await resp.text()
                payload = await resp.json()
            assert payload["code"] == "turn_in_flight"
            assert alias_b.running, "the probe stub did not start the sibling"
            state.sessions.reset.assert_not_awaited()
            assert getattr(alias_a, field) == old_value
        finally:
            gate.set()
            if alias_b.task is not None:
                await asyncio.gather(alias_b.task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_bulk_skips_when_sibling_appears_before_reset(
        self, state, alias_a, alias_b, monkeypatch
    ):
        gate = asyncio.Event()
        self._start_sibling_inside_children_probe(monkeypatch, alias_b, gate)
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_NEW})
                assert resp.status == 200, await resp.text()
                payload = await resp.json()
            assert _ALIAS_A in payload["skipped_running"]
            assert _ALIAS_A not in payload["switched"]
            state.sessions.reset.assert_not_awaited()
            assert alias_a.model == _MODEL_OLD
        finally:
            gate.set()
            if alias_b.task is not None:
                await asyncio.gather(alias_b.task, return_exceptions=True)


class TestSiblingRebindsAfterPublishingItsTurnKey:
    """B's routing moves off the shared session mid-turn; its TURN still runs there.

    A cron injection rebinds ``linked_session_key`` on a live slot with no
    ``running`` gate. B's cold-starting turn published the shared key as its
    identity before the rebind, and that turn -- not B's new routing -- is what
    a reset of the shared session would tear down. A busy scan keyed on each
    running slot's routing drops B the moment it rebinds; the scan must follow
    the published turn key instead.
    """

    @staticmethod
    def _rebind_after_publish(alias_b: _ChatSlot) -> None:
        alias_b._active_turn_session_key = _SHARED_SESSION_KEY
        alias_b.linked_session_key = "cron:job-elsewhere"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("route", "body", "field", "old_value"), _SINGLE_SLOT_SWITCHES)
    async def test_refuses_when_sibling_rebound_but_its_turn_runs_here(
        self, state, alias_a, alias_b, route, body, field, old_value
    ):
        async with _sibling_cold_start(alias_b):
            self._rebind_after_publish(alias_b)
            assert chat_handlers.effective_session_key(alias_b) != _SHARED_SESSION_KEY
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(route, json=body)
                assert resp.status == 409, await resp.text()
                payload = await resp.json()
        assert payload["code"] == "turn_in_flight"
        state.sessions.reset.assert_not_awaited()
        assert getattr(alias_a, field) == old_value

    @pytest.mark.asyncio
    async def test_bulk_skips_when_sibling_rebound_but_its_turn_runs_here(
        self, state, alias_a, alias_b
    ):
        async with _sibling_cold_start(alias_b):
            self._rebind_after_publish(alias_b)
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/model", json={"model": _MODEL_NEW})
                assert resp.status == 200, await resp.text()
                payload = await resp.json()
        assert _ALIAS_A in payload["skipped_running"]
        assert _ALIAS_A not in payload["switched"]
        assert alias_a.model == _MODEL_OLD

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("route", "body", "field", "old_value"), _SINGLE_SLOT_SWITCHES)
    async def test_proceeds_when_sibling_turn_runs_elsewhere(
        self, state, alias_a, alias_b, route, body, field, old_value
    ):
        """Control: B's turn published a DIFFERENT key, so the shared session is not busy."""
        async with _sibling_cold_start(alias_b):
            alias_b._active_turn_session_key = "cron:job-elsewhere"
            alias_b.linked_session_key = "cron:job-elsewhere"
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(route, json=body)
                assert resp.status == 200, await resp.text()
        assert getattr(alias_a, field) != old_value


_LEGACY_BARE_KEY = _SHARED_SESSION_KEY.removeprefix("slack:")


class TestLegacyBareSlackKeyIsTheSameSession:
    """A bare legacy ``thread_ts`` and its ``slack:`` sibling name ONE session.

    A slot restored from an old transcript carries the bare key; a channel-born
    sibling carries the canonical one. ``SessionManager`` folds the two onto one
    live session, so the busy scan must compare them as equal and the switch
    lock must hand both spellings the same lock -- otherwise the two aliases
    serialize against nobody and the scan's comparison fails on spelling.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("switching_key", "sibling_key"),
        [
            pytest.param(
                _LEGACY_BARE_KEY, _SHARED_SESSION_KEY, id="switch-legacy_sibling-canonical"
            ),
            pytest.param(
                _SHARED_SESSION_KEY, _LEGACY_BARE_KEY, id="switch-canonical_sibling-legacy"
            ),
        ],
    )
    @pytest.mark.parametrize(("route", "body", "field", "old_value"), _SINGLE_SLOT_SWITCHES)
    async def test_refuses_across_spellings_and_shares_one_lock(
        self, state, alias_a, alias_b, switching_key, sibling_key, route, body, field, old_value
    ):
        from kiro_crew.llm_helpers import slot_switch_session_lock

        alias_a.linked_session_key = switching_key
        alias_b.linked_session_key = sibling_key
        assert slot_switch_session_lock(switching_key) is slot_switch_session_lock(
            sibling_key
        ), "the two spellings of one session took different switch locks"
        async with _sibling_cold_start(alias_b):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(route, json=body)
                assert resp.status == 409, await resp.text()
                payload = await resp.json()
        assert payload["code"] == "turn_in_flight"
        state.sessions.reset.assert_not_awaited()
        assert getattr(alias_a, field) == old_value
