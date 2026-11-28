"""A slot switch pressed while a cron or channel turn holds its key in a replay gap.

A cron turn whose KAS batch is stale re-projects it
(``spec_hooks_reproject.reproject_unattended_claim``): it opens a replay gap,
resets its session and claims a successor, which takes seconds to start. The
job's tab reads that window as running (``linked_session_busy``), but no
provider is registered and no slot runs the turn. ``_switch_target_busy`` reads
the gap as a turn in flight, so a switch from the tab is refused or deferred: it
never resets a session that is not there, and never tells the tab that the next
reply runs a value the successor does not receive.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard.chat import (
    api_chat_slot_agent,
    api_chat_slot_model,
    api_chat_slot_reasoning_effort,
    api_chat_slot_workspace,
)
from kiro_crew.dashboard.chat_api.slot_guards import _switch_target_busy
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.dashboard.system_notices import SESSION_SWITCH_KIND
from kiro_crew.session import SessionManager
from kiro_crew.testing.wait import await_until

CRON_KEY = "cron:job-1"
TAB = "cron-job-1"
# Registry aliases the model guard accepts (tests are exempt from the hardcoded-model gate).
_MODEL_B = "gpt-5.6-sol"


class _CronGapTurn:
    """A cron turn driven through a real ``SessionManager`` re-projection.

    It claims the key, opens the replay gap, resets its session and claims a
    successor whose ``start`` waits on ``release_start``. Once that start is
    released it holds the lease until ``finish`` is set, then releases and closes
    its gap, all from its own task, which owns the gap.
    """

    def __init__(self) -> None:
        self.release_start = asyncio.Event()
        self.starting = asyncio.Event()
        self.finish = asyncio.Event()
        self.done = asyncio.Event()
        self.providers: list[Any] = []
        self.manager = SessionManager(KiroCrewConfig(), provider_factory=self._factory)
        self.task: asyncio.Task[None] | None = None

    def _factory(self, session_key=None, agent=None, channel_id=None, **kwargs):
        provider = AsyncMock()
        provider.context_usage_pct = lambda: 0.0
        provider.has_active_turn = lambda: False
        successor = bool(self.providers)
        self.providers.append(provider)

        async def start() -> None:
            if successor:
                self.starting.set()
                await self.release_start.wait()

        provider.start = start
        return provider

    async def _run(self) -> None:
        manager = self.manager
        await manager.get_or_create(CRON_KEY)
        manager.open_replay_gap(CRON_KEY)
        await manager.reset(CRON_KEY)
        await manager.get_or_create(CRON_KEY)
        manager.adopt_turn(CRON_KEY)
        await self.finish.wait()
        manager.release(CRON_KEY)
        manager.close_replay_gap(CRON_KEY)
        self.done.set()

    async def enter_gap(self) -> None:
        """Run the turn up to its successor's start, inside the gap."""
        self.task = asyncio.create_task(self._run())
        await await_until(self.starting, what="the successor's start")
        assert self.manager.has_open_replay_gap(CRON_KEY)
        assert self.manager.get_provider(CRON_KEY) is None

    async def end(self) -> None:
        self.release_start.set()
        self.finish.set()
        await await_until(self.done, what="the cron turn's release")
        assert self.task is not None
        await self.task


@web.middleware
async def _dashboard_user(request, handler):
    request["app"] = ""
    request["user"] = "local-app"
    return await handler(request)


def _app(state: Any) -> web.Application:
    app = web.Application(middlewares=[_dashboard_user])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/agent", api_chat_slot_agent)
    app.router.add_post("/api/chat/slots/{slot}/model", api_chat_slot_model)
    app.router.add_post("/api/chat/slots/{slot}/reasoning-effort", api_chat_slot_reasoning_effort)
    app.router.add_post("/api/chat/slots/{slot}/workspace", api_chat_slot_workspace)
    return app


def _cron_tab(state: Any, manager: SessionManager) -> _ChatSlot:
    state.sessions = manager
    slot = _ChatSlot(TAB)
    slot.linked_session_key = CRON_KEY
    slot.append("user", "hello")
    slot.append("assistant", "hi there")
    state._slots[slot.key] = slot
    return slot


def _switch_rows(slot: _ChatSlot) -> list[dict]:
    return [m for m in slot.messages if (m.get("meta") or {}).get("kind") == SESSION_SWITCH_KIND]


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return _make_state(tmp_path)


@pytest.mark.asyncio
async def test_an_effort_pick_in_the_gap_resets_nothing_and_claims_nothing(state) -> None:
    # The route may refuse the pick or defer it; either way nothing is reset and
    # nothing tells the tab that the next reply runs the new level.
    turn = _CronGapTurn()
    slot = _cron_tab(state, turn.manager)
    await turn.enter_gap()
    generation = turn.manager.session_generation(CRON_KEY)
    try:
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.post(
                f"/api/chat/slots/{TAB}/reasoning-effort", json={"reasoning_effort": "high"}
            )
            answer = await resp.json()
        assert answer.get("deferred") is True or answer.get("code") == "turn_in_flight", answer
        assert _switch_rows(slot) == [], "no row says the next reply runs the new level"
        assert turn.manager.session_generation(CRON_KEY) == generation, "no reset ran"
    finally:
        await turn.end()
    successor = turn.providers[-1]
    assert turn.manager.get_provider(CRON_KEY) is successor, "the job's session was not replaced"
    successor.change_effort.assert_not_awaited()
    successor.clear_effort.assert_not_awaited()
    await turn.manager.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "body", "field"),
    [("agent", {"agent": "new-agent"}, "agent"), ("workspace", {"workspace": "ws2"}, "workspace")],
    ids=["agent", "workspace"],
)
async def test_a_switch_in_the_gap_is_refused_and_resets_nothing(state, route, body, field) -> None:
    turn = _CronGapTurn()
    slot = _cron_tab(state, turn.manager)
    before = getattr(slot, field)
    await turn.enter_gap()
    assert state.linked_session_busy(CRON_KEY), "the tab reads the gap as running"
    generation = turn.manager.session_generation(CRON_KEY)
    try:
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{TAB}/{route}", json=body)
            assert resp.status == 409
            assert (await resp.json())["code"] == "turn_in_flight"
        assert getattr(slot, field) == before, "a refused switch commits nothing"
        assert _switch_rows(slot) == [], "no row says the next reply runs the new value"
        assert turn.manager.session_generation(CRON_KEY) == generation, "no reset ran"
    finally:
        await turn.end()

    # Once the turn has released its key, the same switch lands.
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(f"/api/chat/slots/{TAB}/{route}", json=body)
        assert resp.status == 200
    assert getattr(slot, field) == body[field]
    await turn.manager.close_all()


@pytest.mark.asyncio
async def test_a_model_pick_in_the_gap_waits_for_the_turn_boundary(state) -> None:
    turn = _CronGapTurn()
    slot = _cron_tab(state, turn.manager)
    await turn.enter_gap()
    generation = turn.manager.session_generation(CRON_KEY)
    try:
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{TAB}/model", json={"model": _MODEL_B})
            assert resp.status == 200
            assert (await resp.json()).get("deferred") is True
        assert _switch_rows(slot) == []
        assert turn.manager.session_generation(CRON_KEY) == generation, "no reset ran"
    finally:
        await turn.end()
    await turn.manager.close_all()


class _GapSessions:
    def __init__(self, answer: object) -> None:
        self.answer = answer

    def has_open_replay_gap(self, key: str) -> object:
        return self.answer if key == CRON_KEY else False


def _predicate_state(sessions: object) -> Any:
    state = MagicMock()
    state._slots = {}
    state.sessions = sessions
    return state


@pytest.mark.parametrize(
    ("answer", "busy"),
    [(True, True), (False, False), (MagicMock(), False)],
    ids=["gap-open", "no-gap", "truthy-double"],
)
def test_the_predicate_reads_a_replay_gap_as_a_turn_in_flight(answer, busy) -> None:
    slot = _ChatSlot(TAB)
    slot.linked_session_key = CRON_KEY
    state = _predicate_state(_GapSessions(answer))
    assert _switch_target_busy(state, slot, CRON_KEY, None) is busy
