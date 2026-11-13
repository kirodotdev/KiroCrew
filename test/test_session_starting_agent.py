"""The agent of a cold start in flight is readable until the start registers or ends.

A session's own MCP servers start, and ask the gateway for their tool list, while the
start that spawned them is still waiting for them, so before the session is registered
and ``get_agent`` can answer. ``SessionManager.starting_agent`` answers in that window
from a row the start itself writes (``SessionRegistryState.starting_agents``). These
tests hold who writes the row and that it is gone at every exit of the start.
"""

from __future__ import annotations

import asyncio

import pytest
from session_helpers import (  # noqa: F401 -- the fixture is requested by name
    _mock_provider_factory,
    cfg_fixture,
)

from kiro_crew.session import SessionManager
from kiro_crew.testing.wait import async_wait_until, await_until

KEY = "dashboard:starting"


class _Starts:
    """A provider factory whose providers wait at ``start`` until the test lets them go."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.entered: list[object] = []
        self.fail: BaseException | None = None
        base = _mock_provider_factory()

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            provider = base(session_key, agent, channel_id, **kwargs)

            async def _start() -> None:
                self.entered.append(provider)
                await self.gate.wait()
                if self.fail is not None:
                    raise self.fail

            provider.start = _start
            return provider

        self.factory = factory

    async def spawning(self) -> None:
        await async_wait_until(lambda: len(self.entered) == 1, describe=lambda: self.entered)


@pytest.mark.asyncio
async def test_a_start_answers_its_agent_until_the_session_is_registered(cfg) -> None:
    starts = _Starts()
    mgr = SessionManager(cfg, provider_factory=starts.factory)
    assert mgr.starting_agent(KEY) == ""

    start = asyncio.create_task(mgr.get_or_create(KEY, agent="scout"))
    await starts.spawning()
    assert mgr.starting_agent(KEY) == "scout"
    assert mgr.get_agent(KEY) == ""  # not registered yet: the window this row is for
    assert mgr.starting_agent("dashboard:another") == ""

    starts.gate.set()
    await await_until(start, what="the start")
    assert mgr.get_agent(KEY) == "scout"
    assert mgr.starting_agent(KEY) == ""
    assert mgr._allocation_state.starting_agents == {}
    mgr.release(KEY)
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_start_that_fails_leaves_no_row(cfg) -> None:
    starts = _Starts()
    starts.fail = RuntimeError("the agent process did not start")
    mgr = SessionManager(cfg, provider_factory=starts.factory)

    start = asyncio.create_task(mgr.get_or_create(KEY, agent="scout"))
    await starts.spawning()
    assert mgr.starting_agent(KEY) == "scout"
    starts.gate.set()
    with pytest.raises(RuntimeError, match="did not start"):
        await await_until(start, what="the failed start")
    assert mgr.starting_agent(KEY) == ""
    assert mgr._allocation_state.starting_agents == {}
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_cancelled_start_leaves_no_row(cfg) -> None:
    starts = _Starts()
    mgr = SessionManager(cfg, provider_factory=starts.factory)

    start = asyncio.create_task(mgr.get_or_create(KEY, agent="scout"))
    await starts.spawning()
    assert mgr.starting_agent(KEY) == "scout"
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await await_until(start, what="the cancelled start")
    assert mgr.starting_agent(KEY) == ""
    assert mgr._allocation_state.starting_agents == {}
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_start_that_names_no_agent_writes_no_row(cfg) -> None:
    starts = _Starts()
    mgr = SessionManager(cfg, provider_factory=starts.factory)

    start = asyncio.create_task(mgr.get_or_create(KEY))
    await starts.spawning()
    assert mgr._allocation_state.starting_agents == {}
    starts.gate.set()
    await await_until(start, what="the start")
    mgr.release(KEY)
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_row_without_its_starts_reservation_answers_nothing(cfg) -> None:
    """Only the start path writes a row, with the token of the reservation it holds. A
    row that names any other token is not a start in flight and is never read."""
    mgr = SessionManager(cfg, provider_factory=_Starts().factory)
    mgr._allocation_state.starting_agents[KEY] = (object(), "scout")
    assert mgr.starting_agent(KEY) == ""
    await mgr.close_all()
