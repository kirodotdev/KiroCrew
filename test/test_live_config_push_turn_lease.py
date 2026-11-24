"""A live effort or model push holds the session's turn lease across its config call.

The switch handlers check that the session is idle, then await the harness's reply to
a config call. A turn another surface drives (a Slack, Discord, Telegram or Teams
message, a cron run) takes the session's turn lease before its prompt is in flight,
which neither ``slot.running`` nor ``provider.has_active_turn()`` can see, and that
prompt would read the stdout the config call's reply wait is reading. Each live path
therefore takes the lease without waiting and reads a held lease as a turn in flight.

The lease here is a real ``SessionManager``'s, over a registered session entry, so a
held lease is the same semaphore a channel turn holds.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.acp.types import ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.providers.acp import AcpProvider


def _provider() -> MagicMock:
    """An idle kiro-cli provider whose live model switch and effort push succeed."""
    provider = MagicMock(spec=AcpProvider)
    provider.capabilities = capabilities_for(ACP_BACKEND_KIRO)
    provider.has_active_turn.return_value = False
    provider.available_models.return_value = [{"modelId": "auto"}]
    provider.supports_effort.return_value = True
    provider.accepts_effort_level.return_value = True
    provider.change_effort = AsyncMock(return_value=True)
    provider.clear_effort = AsyncMock(return_value=True)
    provider.reapply_live_effort = AsyncMock(return_value=True)
    provider.client = MagicMock()
    provider.client.set_model = AsyncMock()
    provider.model_pin_refused = ""
    return provider


def _real_lease(state, key: str, provider):
    """Give *state* a real SessionManager lease over one registered session at *key*."""
    from kiro_crew.session import SessionManager
    from kiro_crew.session_parts.record import _Session

    cfg = MagicMock()
    cfg.default_agent = ""
    cfg.model = "auto"
    cfg.session.pool_size = 0
    cfg.session.pool_agent = ""
    cfg.session.pool_ttl_secs = 0
    manager = SessionManager(cfg)
    manager._sessions[key] = _Session(provider=provider)
    state.sessions._fold_key = manager._fold_key
    state.sessions.try_acquire = manager.try_acquire
    state.sessions.release = manager.release
    state.sessions.is_busy = manager.is_busy
    state.sessions.has_open_replay_gap = manager.has_open_replay_gap
    return manager


def _held(manager, key: str) -> bool:
    return manager._sessions[key].semaphore.locked()


def _app(state) -> web.Application:
    from kiro_crew.dashboard.chat import api_chat_slot_model, api_chat_slot_reasoning_effort

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/model", api_chat_slot_model)
    app.router.add_post("/api/chat/slots/{slot}/reasoning-effort", api_chat_slot_reasoning_effort)
    return app


def _setup(tmp_path, monkeypatch):
    from kiro_crew.dashboard import chat_handlers

    # The effort route writes a restore marker first; that file write is not under test.
    monkeypatch.setattr(chat_handlers, "_remember_reasoning_effort_for_restore", lambda _e: None)
    state = _make_state(tmp_path)
    state.sessions.reset = AsyncMock(return_value=True)
    provider = _provider()
    state.sessions.get_provider = MagicMock(return_value=provider)
    state.get_or_create_slot("a", model="claude-opus-4.8")
    state.push_slots_update = MagicMock()
    slot = state._slots["a"]
    key = effective_session_key(slot)
    manager = _real_lease(state, key, provider)
    return state, slot, provider, manager, key


class TestTheEffortRoute:
    @pytest.mark.asyncio
    async def test_a_held_lease_defers_the_level_without_a_config_call(self, tmp_path, monkeypatch):
        state, slot, provider, manager, key = _setup(tmp_path, monkeypatch)
        assert await manager.try_acquire(key)  # a channel turn, before its prompt

        async with TestClient(TestServer(_app(state), host="127.0.0.1")) as client:
            resp = await client.post(
                "/api/chat/slots/a/reasoning-effort", json={"reasoning_effort": "high"}
            )
            data = await resp.json()

        assert resp.status == 200, data
        assert data.get("deferred") is True
        assert slot.reasoning_effort == "high"
        provider.change_effort.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()
        assert _held(manager, key)  # still the channel turn's

    @pytest.mark.asyncio
    async def test_the_live_push_runs_under_the_lease_and_gives_it_back(
        self, tmp_path, monkeypatch
    ):
        state, _slot, provider, manager, key = _setup(tmp_path, monkeypatch)
        during: list[bool] = []

        async def change_effort(_level):
            during.append(_held(manager, key))
            return True

        provider.change_effort = AsyncMock(side_effect=change_effort)

        async with TestClient(TestServer(_app(state), host="127.0.0.1")) as client:
            resp = await client.post(
                "/api/chat/slots/a/reasoning-effort", json={"reasoning_effort": "high"}
            )

        assert resp.status == 200
        assert during == [True]
        assert not _held(manager, key)


class TestTheModelPicker:
    @pytest.mark.asyncio
    async def test_a_held_lease_defers_the_pick_to_the_turn_boundary(self, tmp_path, monkeypatch):
        state, slot, provider, manager, key = _setup(tmp_path, monkeypatch)
        assert await manager.try_acquire(key)

        async with TestClient(TestServer(_app(state), host="127.0.0.1")) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "auto"})
            data = await resp.json()

        assert resp.status == 200, data
        assert data.get("deferred") is True
        assert slot.model == "auto"
        assert slot._deferred_model_switch_key == key
        provider.client.set_model.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()
        assert _held(manager, key)

    @pytest.mark.asyncio
    async def test_the_live_switch_runs_under_the_lease_and_gives_it_back(
        self, tmp_path, monkeypatch
    ):
        state, slot, provider, manager, key = _setup(tmp_path, monkeypatch)
        during: list[bool] = []

        async def set_model(_wire):
            during.append(_held(manager, key))

        provider.client.set_model = AsyncMock(side_effect=set_model)

        async with TestClient(TestServer(_app(state), host="127.0.0.1")) as client:
            resp = await client.post("/api/chat/slots/a/model", json={"model": "auto"})
            data = await resp.json()

        assert resp.status == 200, data
        assert during == [True]
        assert slot.model == "auto"
        assert not _held(manager, key)


class TestTheDeferredPickAtADashboardBoundary:
    """``apply_deferred_model_switch`` from a dashboard turn's tail or admission."""

    @pytest.mark.asyncio
    async def test_a_held_lease_keeps_the_pick_deferred(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.session_control import apply_deferred_model_switch

        state, slot, provider, manager, key = _setup(tmp_path, monkeypatch)
        slot.model = "auto"
        slot._deferred_model_switch_key = key
        assert await manager.try_acquire(key)  # the channel turn that took over

        armed = await apply_deferred_model_switch(state, slot)

        assert armed is False
        provider.client.set_model.assert_not_awaited()
        assert slot._deferred_model_switch_key == key
        assert _held(manager, key)

    @pytest.mark.asyncio
    async def test_the_live_switch_runs_under_the_lease_and_gives_it_back(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard.session_control import apply_deferred_model_switch

        state, slot, provider, manager, key = _setup(tmp_path, monkeypatch)
        slot.model = "auto"
        slot._deferred_model_switch_key = key
        during: list[bool] = []

        async def set_model(_wire):
            during.append(_held(manager, key))

        provider.client.set_model = AsyncMock(side_effect=set_model)

        armed = await apply_deferred_model_switch(state, slot)

        assert armed is False
        assert during == [True]
        assert slot._deferred_model_switch_key == ""
        assert not _held(manager, key)


class TestTheBulkEffortOnlySwitch:
    """``POST /api/chat/slots/model`` on a slot already on the picked model."""

    @staticmethod
    async def _post_bulk(state, body: dict) -> tuple[int, dict]:
        from kiro_crew.dashboard.chat import api_chat_slots_model

        @web.middleware
        async def dashboard_caller(request, handler):
            request["app"] = ""
            return await handler(request)

        app = web.Application(middlewares=[dashboard_caller])
        app["state"] = state
        app.router.add_post("/api/chat/slots/model", api_chat_slots_model)
        async with TestClient(TestServer(app, host="127.0.0.1")) as client:
            resp = await client.post("/api/chat/slots/model", json=body)
            return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_a_held_lease_skips_or_defers_the_session(self, tmp_path, monkeypatch):
        state, slot, provider, manager, key = _setup(tmp_path, monkeypatch)
        slot.reasoning_effort = "low"
        assert await manager.try_acquire(key)

        status, data = await self._post_bulk(
            state, {"model": "claude-opus-4.8", "reasoning_effort": "high"}
        )
        assert status == 200, data
        assert data["skipped_running"] == ["a"]
        assert slot.reasoning_effort == "low"

        status, data = await self._post_bulk(
            state,
            {"model": "claude-opus-4.8", "reasoning_effort": "high", "skip_running": False},
        )
        assert status == 200, data
        assert data["effort_deferred"] == ["a"]
        assert slot.reasoning_effort == "high"
        provider.change_effort.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()
        assert _held(manager, key)

    @pytest.mark.asyncio
    async def test_the_live_push_runs_under_the_lease_and_gives_it_back(
        self, tmp_path, monkeypatch
    ):
        state, slot, provider, manager, key = _setup(tmp_path, monkeypatch)
        slot.reasoning_effort = "low"
        during: list[bool] = []

        async def change_effort(_level):
            during.append(_held(manager, key))
            return True

        provider.change_effort = AsyncMock(side_effect=change_effort)

        status, data = await self._post_bulk(
            state, {"model": "claude-opus-4.8", "reasoning_effort": "high"}
        )

        assert status == 200, data
        assert data["switched"] == ["a"]
        assert during == [True]
        assert not _held(manager, key)


class TestAReplayGap:
    """A cron or channel turn that reset its session to re-claim it holds the key.

    ``SessionManager.open_replay_gap``: from the reset's pop to the re-claim the key
    has no session, and every other claim waits for that turn. The tab reads the turn
    as running throughout (``linked_session_busy``).
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("level", ["high", ""])
    async def test_a_popped_session_defers_the_level_without_a_reset(
        self, tmp_path, monkeypatch, caplog, level
    ):
        state, slot, _provider_, manager, key = _setup(tmp_path, monkeypatch)
        slot.reasoning_effort = "low"
        del manager._sessions[key]
        state.sessions.get_provider = MagicMock(return_value=None)
        manager.open_replay_gap(key)
        try:
            with caplog.at_level("INFO", logger="kiro_crew.dashboard.chat_handlers"):
                async with TestClient(TestServer(_app(state), host="127.0.0.1")) as client:
                    resp = await client.post(
                        "/api/chat/slots/a/reasoning-effort", json={"reasoning_effort": level}
                    )
                    data = await resp.json()
        finally:
            manager.close_replay_gap(key)

        assert resp.status == 200, data
        assert data.get("deferred") is True
        assert slot.reasoning_effort == level
        # A Default nothing live could forget is cleared at the next spawn.
        assert slot._effort_overlay_clear is (level == "")
        state.sessions.reset.assert_not_awaited()
        assert any(
            "deferred live effort push" in r.getMessage()
            and "live_session=False lease_held=False replay_gap=True" in r.getMessage()
            for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    @pytest.mark.asyncio
    async def test_the_effort_route_does_not_take_a_lease_the_gap_holds(
        self, tmp_path, monkeypatch
    ):
        state, slot, provider, manager, key = _setup(tmp_path, monkeypatch)
        manager.open_replay_gap(key)  # a gap whose turn does not hold the permit
        try:
            async with TestClient(TestServer(_app(state), host="127.0.0.1")) as client:
                resp = await client.post(
                    "/api/chat/slots/a/reasoning-effort", json={"reasoning_effort": "high"}
                )
                data = await resp.json()
        finally:
            manager.close_replay_gap(key)

        assert resp.status == 200, data
        assert data.get("deferred") is True
        assert slot.reasoning_effort == "high"
        provider.change_effort.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()
        assert not _held(manager, key)

    @pytest.mark.asyncio
    async def test_the_model_picker_does_not_take_a_lease_the_gap_holds(
        self, tmp_path, monkeypatch
    ):
        state, slot, provider, manager, key = _setup(tmp_path, monkeypatch)
        manager.open_replay_gap(key)
        try:
            async with TestClient(TestServer(_app(state), host="127.0.0.1")) as client:
                resp = await client.post("/api/chat/slots/a/model", json={"model": "auto"})
                data = await resp.json()
        finally:
            manager.close_replay_gap(key)

        assert resp.status == 200, data
        assert data.get("deferred") is True
        provider.client.set_model.assert_not_awaited()
        state.sessions.reset.assert_not_awaited()
        assert not _held(manager, key)


class TestASessionStillBeingRegistered:
    """A brand-new session, between its registration and its starting turn's claim.

    ``get_or_create`` registers the session and then awaits (the scratch index, the
    start record) before it takes the turn permit. A live config push or a channel
    command that took the free permit there would act on a session another turn is
    starting, so ``try_acquire`` refuses it until that turn has claimed it.
    """

    @pytest.mark.asyncio
    async def test_its_lease_is_not_free_until_the_starting_turn_claims_it(self, monkeypatch):
        import asyncio

        from real_session_manager import real_sessions

        from kiro_crew import session_allocation as allocation
        from kiro_crew.testing.wait import await_until

        sessions = real_sessions()
        registered, proceed = asyncio.Event(), asyncio.Event()
        record_started = allocation.record_session_started

        async def held_start(key, provider, *args, **kwargs):
            registered.set()
            await proceed.wait()
            return await record_started(key, provider, *args, **kwargs)

        monkeypatch.setattr(allocation, "record_session_started", held_start)
        key = sessions._fold_key("dashboard:b031")
        starting = asyncio.create_task(sessions.get_or_create("dashboard:b031"))
        try:
            await await_until(registered, what="the new session's registration")
            assert sessions.get_provider("dashboard:b031") is not None
            took = await sessions.try_acquire(key)
            if took:
                # Hand it back, or the starting turn would wait on it forever.
                sessions.release(key)
            assert took is False

            proceed.set()
            await await_until(starting, what="the starting turn's claim")
            assert await sessions.try_acquire(key) is False  # the starting turn's now
            sessions.release(key)
            assert await sessions.try_acquire(key) is True
            sessions.release(key)
        finally:
            proceed.set()
            await await_until(
                asyncio.gather(starting, return_exceptions=True), what="the start to end"
            )
            await await_until(asyncio.ensure_future(sessions.close_all()), what="close_all")
