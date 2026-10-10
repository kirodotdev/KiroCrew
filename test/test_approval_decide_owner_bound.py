"""A tool-approval decide by request id alone is owner-bound to the dashboard user.

Both decide routes, ``POST /api/approvals/{id}/{action}`` and
``POST /api/chat/slots/{slot}/approve``, accept an owner-bound target that names
the exact request a card showed (``origin=coordinator`` + slot + instance, or
``origin: "native"`` + ``request_mid``). A decide that names only the request id
resolves whichever request holds that id at that moment, so it is accepted only
from the dashboard user's own browser session. A loopback ``X-Internal-Secret``
caller (an agent, MCP tool or cron) must send the owner-bound target.

Every test drives the real handler through an aiohttp ``TestClient``; the auth
middleware is stood in by one of the two middlewares below.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_handlers import api_chat_slot_approve
from kiro_crew.dashboard.handlers.sessions import api_approval_resolve
from kiro_crew.dashboard.state import SlotOrigin

SLOT = "dashboard:selected"


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    return st


@web.middleware
async def _dashboard_user(request: web.Request, handler):
    """The dashboard user's browser session: a present, empty app claim."""
    request["app"] = ""
    request["user"] = "local-app"
    return await handler(request)


@web.middleware
async def _internal_caller(request: web.Request, handler):
    """A loopback ``X-Internal-Secret`` caller: no app claim, ``internal_auth``."""
    request["internal_auth"] = True
    return await handler(request)


def _app(state, middleware) -> web.Application:
    app = web.Application(middlewares=[middleware])
    app["state"] = state
    app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    return app


async def _wait_pending(state, approval_id: str) -> dict:
    for _ in range(200):
        pending = state._pending_approvals.get(approval_id)
        if pending is not None:
            return pending
        await asyncio.sleep(0.01)
    raise AssertionError(f"approval {approval_id!r} never registered")


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


class _Req(dict):
    """A request stand-in: the predicate reads only the request's mapping."""


def test_predicate_admits_only_the_dashboard_user():
    from kiro_crew.dashboard.chat_handlers import bare_approval_decide_allowed

    assert bare_approval_decide_allowed(_Req(app="", user="local-app")) is True
    assert bare_approval_decide_allowed(_Req(internal_auth=True)) is False
    assert bare_approval_decide_allowed(_Req(app="", internal_auth=True)) is False
    assert bare_approval_decide_allowed(_Req()) is False
    assert bare_approval_decide_allowed(_Req(app="some-app")) is False


# -- POST /api/approvals/{id}/{action} -------------------------------------


@pytest.mark.asyncio
async def test_internal_caller_bare_id_coordinator_decide_is_refused(state):
    task = asyncio.get_running_loop().create_task(
        state.request_approval("rid-1", "subagent", "fs_write", slot=SLOT)
    )
    try:
        await _wait_pending(state, "rid-1")
        async with TestClient(TestServer(_app(state, _internal_caller))) as client:
            resp = await client.post("/api/approvals/rid-1/approve", json={})
            body = await resp.json()
        assert resp.status == 404
        assert body["code"] == "approval_target_required"
        assert "rid-1" in state._pending_approvals
        assert not task.done()
    finally:
        await _cancel(task)


@pytest.mark.asyncio
async def test_internal_caller_owner_bound_coordinator_decide_resolves(state):
    task = asyncio.get_running_loop().create_task(
        state.request_approval("rid-2", "subagent", "fs_write", slot=SLOT)
    )
    try:
        pending = await _wait_pending(state, "rid-2")
        query = {"origin": "coordinator", "slot": SLOT, "instance": pending["instance"]}
        async with TestClient(TestServer(_app(state, _internal_caller))) as client:
            resp = await client.post("/api/approvals/rid-2/approve", params=query, json={})
        assert resp.status == 200
        assert await asyncio.wait_for(task, 2) is True
    finally:
        await _cancel(task)


@pytest.mark.asyncio
async def test_dashboard_user_bare_id_coordinator_decide_still_resolves(state):
    task = asyncio.get_running_loop().create_task(
        state.request_approval("rid-3", "subagent", "fs_write", slot=SLOT)
    )
    try:
        await _wait_pending(state, "rid-3")
        async with TestClient(TestServer(_app(state, _dashboard_user))) as client:
            resp = await client.post("/api/approvals/rid-3/approve", json={})
        assert resp.status == 200
        assert await asyncio.wait_for(task, 2) is True
    finally:
        await _cancel(task)


# -- POST /api/chat/slots/{slot}/approve -----------------------------------


def _slot_with_pending(state, request_id: str):
    slot = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    fut = asyncio.get_running_loop().create_future()
    row = slot.append("permission", "Tool", f'{{"request_id": "{request_id}"}}')
    slot.register_approval(request_id, fut, row)
    return slot, fut


@pytest.mark.asyncio
async def test_internal_caller_bare_id_slot_approve_is_refused(state):
    _slot, fut = _slot_with_pending(state, "rid-7")
    async with TestClient(TestServer(_app(state, _internal_caller))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/approve",
            json={"action": "approved", "request_id": "rid-7"},
        )
        body = await resp.json()
    assert resp.status == 404
    assert body["code"] == "approval_target_required"
    assert not fut.done()


@pytest.mark.asyncio
async def test_internal_caller_slot_approve_with_no_request_id_is_refused(state):
    # The route picks the slot's single pending request when no id is named;
    # that is a bare decide too.
    _slot, fut = _slot_with_pending(state, "rid-8")
    async with TestClient(TestServer(_app(state, _internal_caller))) as client:
        resp = await client.post("/api/chat/slots/selected/approve", json={"action": "approved"})
    assert resp.status == 404
    assert not fut.done()


@pytest.mark.asyncio
async def test_internal_caller_owner_bound_slot_approve_resolves(state):
    slot, fut = _slot_with_pending(state, "rid-9")
    mid = slot.approval_instance("rid-9")
    assert mid
    async with TestClient(TestServer(_app(state, _internal_caller))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/approve",
            json={
                "action": "approved",
                "request_id": "rid-9",
                "origin": "native",
                "request_mid": mid,
            },
        )
    assert resp.status == 200
    assert fut.done()


@pytest.mark.asyncio
async def test_dashboard_user_bare_id_slot_approve_still_resolves(state):
    _slot, fut = _slot_with_pending(state, "rid-10")
    async with TestClient(TestServer(_app(state, _dashboard_user))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/approve",
            json={"action": "approved", "request_id": "rid-10"},
        )
    assert resp.status == 200
    assert fut.done()
