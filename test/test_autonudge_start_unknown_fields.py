"""``POST /api/autonudge`` refuses an unknown body key, as the monitor tools do.

The ``monitor_start`` / ``monitor_watch`` tools answer an extra field with
``<field>: unknown field for tool ...`` and arm nothing. The REST route answers
the same input the same way: a 400 naming the field, and no loop armed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.autonudge import NudgeLoop
from kiro_crew.validation import ValidationError
from kiro_crew.validation_parts.core_agent_schemas import MONITOR_START_SCHEMA
from kiro_crew.validation_parts.field_engine import validate_tool_args


def _owner_dashboard_identity():
    from aiohttp import web

    @web.middleware
    async def middleware(request, handler):
        state = request.app.get("state")
        request["user"] = str(getattr(state, "owner_id", "") or "") or "local-app"
        request["app"] = ""
        return await handler(request)

    return middleware


def _svc():
    svc = MagicMock()
    loop = NudgeLoop(id="loop-1", slot_key="chat-1-123", message="go")
    svc.add = AsyncMock(return_value=loop)
    svc.list_all = lambda: [loop]
    svc.get_by_id = lambda _id: loop if _id == "loop-1" else None
    return svc


def _app(monkeypatch, svc):
    from aiohttp import web

    from kiro_crew.dashboard.handlers import autonudge as _handler

    monkeypatch.setattr(_handler, "_autonudge_get", lambda: svc)
    state = MagicMock()
    state._slots = {
        "chat-1-123": MagicMock(workspace="default", memory_mode="persistent", mode="chat")
    }
    app = web.Application(middlewares=[_owner_dashboard_identity()])
    app["state"] = state
    app.router.add_post("/api/autonudge", _handler.api_autonudge_start)
    return app


def test_the_tool_refuses_the_field_the_route_must_refuse() -> None:
    """The reference behaviour the route is matched against."""
    with pytest.raises(ValidationError) as exc:
        validate_tool_args({"message": "go", "wake_on_green": True}, MONITOR_START_SCHEMA)
    assert exc.value.field == "wake_on_green"
    assert exc.value.message.startswith("unknown field for ")


@pytest.mark.asyncio
async def test_an_unknown_field_is_400_and_arms_nothing(monkeypatch) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    svc = _svc()
    async with TestClient(TestServer(_app(monkeypatch, svc))) as client:
        resp = await client.post(
            "/api/autonudge",
            json={"slot_key": "chat-1-123", "message": "go", "wake_on_green": True},
        )
        assert resp.status == 400
        body = await resp.json()
    assert body["code"] == "unknown_field"
    assert body["error"].startswith("wake_on_green: unknown field for ")
    svc.add.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_first_unknown_field_in_body_order_is_named(monkeypatch) -> None:
    """``validate_tool_args`` names the first unknown key it meets; so does the route."""
    from aiohttp.test_utils import TestClient, TestServer

    svc = _svc()
    async with TestClient(TestServer(_app(monkeypatch, svc))) as client:
        resp = await client.post(
            "/api/autonudge",
            json={"zzz": 1, "slot_key": "chat-1-123", "message": "go", "aaa": 2},
        )
        assert resp.status == 400
        assert (await resp.json())["error"].startswith("zzz: ")
    svc.add.assert_not_awaited()


@pytest.mark.asyncio
async def test_every_field_the_route_reads_is_still_accepted(monkeypatch) -> None:
    """Negative control: the closed set must not refuse a key the route documents."""
    from aiohttp.test_utils import TestClient, TestServer

    svc = _svc()
    async with TestClient(TestServer(_app(monkeypatch, svc))) as client:
        resp = await client.post(
            "/api/autonudge",
            json={
                "slot_key": "chat-1-123",
                "session_key": "chat-1-123",
                "message": "go",
                "idle_secs": 60,
                "max_cycles": 0,
                "max_runtime_secs": 0,
                "stop_sentinel_path": "",
                "gate": False,
                "banner": "b",
                "judge": {},
            },
        )
        assert resp.status == 200, await resp.text()
    svc.add.assert_awaited_once()
