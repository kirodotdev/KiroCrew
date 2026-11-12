"""An all-slots ``trust`` or a ``yolo`` grant never answers a held KAS plan handoff.

``api_chat_mode`` sweeps every pending background approval
(``state._approval_futures``) and every channel card (``agent._approval_future``)
when the grant is not scoped to one slot. A KAS plan handoff waits on the person
in every mode (``kas_workflows.chat.needs_a_person``), so the sweep must leave it
pending while it still settles an ordinary request parked beside it. Each test
parks one of each through the real hold site, makes the grant through the real
route, and checks both. The ordinary request proves the sweep ran.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_mode_security_helpers import (  # noqa: F401 -- fixtures are requested by name
    _hermetic_home,
    _isolate_safety_override,
    state_fixture,
)

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, AcpEvent
from kiro_crew.dashboard.chat_handlers import api_chat_mode
from kiro_crew.kas_workflows.person_hold import held_for_person, hold_for_person
from kiro_crew.testing.wait import async_wait_until, await_until

_GRANTS = [
    pytest.param({"mode": "trust"}, id="all-slots-trust"),
    pytest.param({"mode": "yolo"}, id="yolo"),
]


@web.middleware
async def _owner(request: web.Request, handler):
    request["app"] = ""
    request["user"] = "local-app"
    return await handler(request)


def _mode_client(state) -> TestClient:
    app = web.Application(middlewares=[_owner])
    app["state"] = state
    app.router.add_post("/api/chat/mode", api_chat_mode)
    return TestClient(TestServer(app))


async def _settle(*tasks: asyncio.Task) -> None:
    """End every turn still parked (a failed assert leaves one) and collect them all."""
    for task in tasks:
        if not task.done():
            task.cancel()
    await await_until(asyncio.gather(*tasks, return_exceptions=True), what="the parked turns")


def _plan(request_id: str) -> AcpEvent:
    """A KAS workflow plan handoff (``update_workflow``)."""
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=request_id,
        title="Approve the workflow plan",
        tool_kind="read",
        options=[{"optionId": "ok", "kind": "allow_once", "name": "Approve"}],
        harness_tool_id="update_workflow",
        kas_builtin_ids=True,
    )


def _ordinary(request_id: str) -> AcpEvent:
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=request_id,
        title="fs_write",
        tool_kind="edit",
        options=[{"optionId": "allow", "kind": "allow_once", "name": "Allow"}],
    )


class TestTheMarker:
    def test_only_a_marked_future_is_held(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            held, plain = loop.create_future(), loop.create_future()
            hold_for_person(held)
            assert held_for_person(held) and not held_for_person(plain)
            assert not held_for_person(None)
        finally:
            loop.close()


@pytest.mark.parametrize("grant", _GRANTS)
@pytest.mark.asyncio
async def test_a_bulk_grant_leaves_a_held_background_handoff_pending(state, grant) -> None:
    loop = asyncio.get_running_loop()
    held = loop.create_task(
        state.request_approval("bg-plan", "subagent", "Approve the workflow plan", person_only=True)
    )
    plain = loop.create_task(state.request_approval("bg-plain", "subagent", "fs_write"))
    try:
        await async_wait_until(
            lambda: {"bg-plan", "bg-plain"} <= set(state._approval_futures),
            describe=lambda: "both registrations",
        )
        async with _mode_client(state) as client:
            resp = await client.post("/api/chat/mode", json=grant)
            assert resp.status == 200
        assert (
            await await_until(plain, what="the ordinary request") is True
        ), "the sweep settles an ordinary request"
        assert not held.done(), "a bulk grant answered a plan handoff nobody clicked"
        assert not state._approval_futures["bg-plan"].done()
    finally:
        state.resolve_approval("bg-plan", False)
        await _settle(held, plain)


def _member(name: str):
    return SimpleNamespace(
        id=name,
        role="dev",
        agent_name=name,
        session_key=f"channel:{name}",
        _approval_future=None,
        _trusted_commands=set(),
        _trusted_bases=set(),
        _pending_approval_command="",
    )


def _parked_channel(members):
    """A channel whose approval card waits for an answer instead of giving one."""
    ch = SimpleNamespace(id="c1", trusted=False, members={m.id: m for m in members})
    ch._broadcast = MagicMock()
    ch._save = MagicMock()  # an all-slots trust also trusts the channel and saves it
    ch.post = AsyncMock()
    return ch


def _client(event):
    client = SimpleNamespace()

    async def _stream(message):
        yield event
        yield SimpleNamespace(kind=EVENT_COMPLETE)

    client.stream = _stream
    client.approve_tool = AsyncMock()
    client.reject_tool = AsyncMock()
    return client


@pytest.mark.parametrize("grant", _GRANTS)
@pytest.mark.asyncio
async def test_a_bulk_grant_leaves_a_held_channel_handoff_pending(
    state, grant, _floor_monkeypatch
) -> None:
    from kiro_crew.channel import _stream_task

    _floor_monkeypatch.setattr("kiro_crew.permission_floor.refusal_for", lambda event, **kw: None)
    planner, worker = _member("planner"), _member("worker")
    ch = _parked_channel([planner, worker])
    state.channel_manager = SimpleNamespace(_channels={"c1": ch})
    plan_client, plain_client = _client(_plan("rq-plan")), _client(_ordinary("rq-plain"))
    loop = asyncio.get_running_loop()
    plan_turn = loop.create_task(
        _stream_task(planner, ch, plan_client, "go", is_yolo=lambda: False)
    )
    plain_turn = loop.create_task(
        _stream_task(worker, ch, plain_client, "go", is_yolo=lambda: False)
    )
    try:
        await async_wait_until(
            lambda: planner._approval_future is not None and worker._approval_future is not None,
            describe=lambda: "both channel cards",
        )
        held = planner._approval_future
        async with _mode_client(state) as client:
            resp = await client.post("/api/chat/mode", json=grant)
            assert resp.status == 200
        await await_until(plain_turn, what="the ordinary turn")
        plain_client.approve_tool.assert_awaited_once_with("rq-plain")
        assert not held.done(), "a bulk grant answered a plan handoff nobody clicked"
        plan_client.approve_tool.assert_not_awaited()
    finally:
        if planner._approval_future is not None and not planner._approval_future.done():
            planner._approval_future.set_result("rejected")
        await _settle(plan_turn, plain_turn)
    plan_client.approve_tool.assert_not_awaited()
