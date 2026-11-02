"""GET /api/notifications reports ``unread`` from the notes it returns.

The count is the notes that are not read and not passive, the bell badge's
rule. Acknowledging notes lowers it, and a gateway that restarts over a
persisted feed reports the true number rather than zero.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import messaging
from kiro_crew.dashboard.notification_coordinator import unread_count
from kiro_crew.dashboard.state import DashboardState


def _state(monkeypatch, tmp_path) -> DashboardState:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def _app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/notifications", messaging.api_notifications)
    app.router.add_post("/api/notifications/ack", messaging.api_notification_ack)
    app.router.add_post("/api/notifications/ack-all", messaging.api_notifications_ack_all)
    return app


async def _unread(client: TestClient) -> int:
    resp = await client.get("/api/notifications")
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert set(body) == {"notifications", "unread"}
    return int(body["unread"])


def _deliver(state: DashboardState, ts: str, **extra: object) -> None:
    state._deliver_note({"ts": ts, "kind": "cron", "title": ts, "body": "b", **extra})


@pytest.mark.asyncio
async def test_acknowledging_notes_lowers_unread(monkeypatch, tmp_path):
    state = _state(monkeypatch, tmp_path)
    _deliver(state, "t1")
    _deliver(state, "t2")
    _deliver(state, "t3")
    async with TestClient(TestServer(_app(state))) as client:
        assert await _unread(client) == 3

        resp = await client.post("/api/notifications/ack", json={"ts": "t1"})
        assert resp.status == 200
        assert await _unread(client) == 2

        resp = await client.post("/api/notifications/ack-all")
        assert resp.status == 200
        assert await _unread(client) == 0


@pytest.mark.asyncio
async def test_a_restarted_gateway_reports_the_true_unread_count(monkeypatch, tmp_path):
    first = _state(monkeypatch, tmp_path)
    _deliver(first, "t1")
    _deliver(first, "t2")
    await first._rewrite_notifications_async()

    restarted = _state(monkeypatch, tmp_path)
    assert [note["ts"] for note in restarted._notification_log] == ["t1", "t2"]
    async with TestClient(TestServer(_app(restarted))) as client:
        assert await _unread(client) == 2


def test_passive_silenced_and_read_notes_are_not_unread():
    notes = [
        {"ts": "1"},
        {"ts": "2", "acked": True},
        {"ts": "3", "priority": "passive"},
        {"ts": "4", "silenced": True},
        {"ts": "5", "priority": "critical", "acked": False},
    ]
    assert unread_count(notes) == 2
