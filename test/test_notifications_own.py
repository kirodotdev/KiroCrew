"""An app reads and acknowledges its own bell notifications, and nobody else's."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.notifications_push import (
    api_own_notification_ack,
    api_own_notifications,
)
from kiro_crew.dashboard.notification_coordinator import NotificationCoordinator


class _FakeState:
    owner_id = "owner"

    def __init__(self) -> None:
        self._notification_log = [
            {"ts": "t1", "source": "app:ledger", "title": "a", "priority": "default"},
            {"ts": "t2", "source": "app:other", "title": "b", "priority": "default"},
            {"ts": "t3", "source": "system", "title": "c", "priority": "default"},
            {"ts": "t4", "source": "app:ledger", "title": "d", "priority": "default"},
        ]
        self.rewrites = 0
        self.broadcasts: list[tuple[str, dict]] = []

    async def _rewrite_notifications_async(self) -> None:
        self.rewrites += 1

    async def ack_notification(self, ts: str) -> bool:
        return await NotificationCoordinator.set_acknowledged(self, ts, acknowledged=True)

    def broadcast_ws(self, event: str, data: dict) -> None:
        self.broadcasts.append((event, data))

    def acked(self) -> dict[str, bool]:
        return {n["ts"]: bool(n.get("acked")) for n in self._notification_log}


def _app(state: _FakeState, *, app: str, user: str = "") -> web.Application:
    @web.middleware
    async def fake_auth(request, handler):
        request["app"] = app
        if user:
            request["user"] = user
        return await handler(request)

    application = web.Application(middlewares=[fake_auth])
    application["state"] = state
    application.router.add_get("/api/notifications/own", api_own_notifications)
    application.router.add_post("/api/notifications/own/ack", api_own_notification_ack)
    return application


@pytest.mark.asyncio
async def test_an_app_token_reads_and_acks_only_its_own_notes():
    state = _FakeState()
    async with TestClient(TestServer(_app(state, app="ledger"))) as client:
        # The name in the query is not the app's identity: the token's is.
        resp = await client.get("/api/notifications/own?app=other")
        assert resp.status == 200
        listed = await resp.json()
        assert [n["ts"] for n in listed["notifications"]] == ["t1", "t4"]
        assert listed["unread"] == 2

        for foreign in ("t2", "t3"):
            resp = await client.post("/api/notifications/own/ack", json={"ts": foreign})
            assert resp.status == 404
        resp = await client.post("/api/notifications/own/ack", json={"ts": "t1"})
        assert resp.status == 200
        assert state.acked() == {"t1": True, "t2": False, "t3": False, "t4": False}

        resp = await client.post("/api/notifications/own/ack", json={"ts": "t4"})
        assert resp.status == 200
    assert state.acked() == {"t1": True, "t2": False, "t3": False, "t4": True}
    assert state.rewrites == 2
    assert state.broadcasts == [
        ("notification_ack", {"ts": "t1"}),
        ("notification_ack", {"ts": "t4"}),
    ]


@pytest.mark.asyncio
async def test_the_dashboard_names_the_app_and_must_be_the_owner():
    state = _FakeState()
    async with TestClient(TestServer(_app(state, app="", user="owner"))) as client:
        resp = await client.get("/api/notifications/own?app=other")
        assert [n["ts"] for n in (await resp.json())["notifications"]] == ["t2"]
        resp = await client.get("/api/notifications/own")
        assert resp.status == 400
        resp = await client.post("/api/notifications/own/ack", json={"app": "other", "ts": "t2"})
        assert resp.status == 200
    assert state.acked() == {"t1": False, "t2": True, "t3": False, "t4": False}

    async with TestClient(TestServer(_app(state, app="", user="teammate"))) as client:
        resp = await client.get("/api/notifications/own?app=other")
        assert resp.status == 403


@pytest.mark.asyncio
async def test_every_refusal_leaves_an_audit_record():
    state = _FakeState()
    with patch("kiro_crew.dashboard.handlers.notifications_push.sel") as sel_mock:
        async with TestClient(TestServer(_app(state, app="ledger"))) as client:
            resp = await client.post("/api/notifications/own/ack", json={"ts": "t2"})
            assert resp.status == 404
        async with TestClient(TestServer(_app(state, app="", user="owner"))) as client:
            resp = await client.get("/api/notifications/own")
            assert resp.status == 400
            resp = await client.post(
                "/api/notifications/own/ack", json={"app": "ledger", "ts": "t2"}
            )
            assert resp.status == 404
    denials = [
        (c.kwargs["caller"], c.kwargs["operation"], c.kwargs.get("resources", ""))
        for c in sel_mock.return_value.log_api_access.call_args_list
        if c.kwargs["outcome"] == "denied"
    ]
    assert denials == [
        ("ledger", "notifications_own_ack", "t2"),
        ("dashboard", "notifications_own", ""),
        ("dashboard", "notifications_own_ack", "t2"),
    ]
