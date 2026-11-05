"""A tool approval decided by request id never settles another session's request.

The agent mints a tool-approval request id itself (its JSON-RPC id, a counter
per process), so two live sessions can each hold a pending request under the
same id. ``POST /api/approvals/{id}/{action}`` names no session, so it refuses
such an id with 409 rather than settling whichever session comes first. A
caller that knows its session and card row decides on
``POST /api/chat/slots/{slot}/approve`` with ``origin: "native"`` and the row's
``request_mid``, which resolves only that session's request.

Every test drives the real handlers through an aiohttp ``TestClient`` as the
dashboard user's browser session.
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
    request["app"] = ""
    request["user"] = "local-app"
    return await handler(request)


def _app(state) -> web.Application:
    app = web.Application(middlewares=[_dashboard_user])
    app["state"] = state
    app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    return app


def _pending(state, name: str, request_id: str):
    slot = state.get_or_create_slot(name, origin=SlotOrigin.USER)
    fut = asyncio.get_running_loop().create_future()
    row = slot.append("permission", "Tool", f'{{"request_id": "{request_id}"}}')
    slot.register_approval(request_id, fut, row)
    return slot, fut


@pytest.mark.asyncio
async def test_id_pending_in_two_sessions_is_refused_by_the_id_route(state):
    # B is registered first, so it is first in slot order.
    _slot_b, fut_b = _pending(state, "session-b", "1")
    _slot_a, fut_a = _pending(state, "session-a", "1")
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/approvals/1/approve", json={"response_id": "r-1"})
        body = await resp.json()
    assert resp.status == 409
    assert body["code"] == "approval_ambiguous"
    assert "decision" not in body
    assert not fut_a.done()
    assert not fut_b.done()


@pytest.mark.asyncio
async def test_unique_id_on_the_id_route_still_resolves(state):
    _slot, fut = _pending(state, "session-a", "7")
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/approvals/7/reject", json={})
    assert resp.status == 200
    assert fut.done()


@pytest.mark.asyncio
async def test_slot_route_with_the_card_row_resolves_only_that_session(state):
    _slot_b, fut_b = _pending(state, "session-b", "1")
    slot_a, fut_a = _pending(state, "session-a", "1")
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/session-a/approve",
            json={
                "action": "approved",
                "request_id": "1",
                "response_id": "r-a-1",
                "origin": "native",
                "request_mid": slot_a.approval_instance("1"),
            },
        )
    assert resp.status == 200
    assert fut_a.done()
    assert not fut_b.done()
