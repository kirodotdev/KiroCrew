"""The Sessions search marks a hit ``open`` when a live dashboard slot owns it.

The server owns the slot-to-transcript mapping (``slot_history_key``), so the
client reads the flag instead of guessing from slot names. Each case below is a
key shape a name-based guess gets wrong: an ordinary tab, a cron tab (live
``cron-<id>``, stored ``cron_<id>``) and a channel-linked tab.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_sessions_search
from kiro_crew.history import ConversationLog


def _slot(key: str, linked: str = "", channel_origin: bool = False) -> SimpleNamespace:
    return SimpleNamespace(key=key, linked_session_key=linked, channel_origin=channel_origin)


def _app(log: ConversationLog, slots: dict) -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(conversation_log=log, _slots=slots)
    app.router.add_get("/api/sessions/search", api_sessions_search)
    return app


@pytest.mark.asyncio
async def test_open_tabs_of_every_key_shape_are_marked(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    for key in ("dashboard:chat-1-1", "cron:job7", "slack:1785.12", "dashboard:chat-old"):
        log.append(key, "user", "grid planning notes")
    slots = {
        "chat-1-1": _slot("chat-1-1"),
        "cron-job7": _slot("cron-job7", linked="cron:job7"),
        "slack_1785.12": _slot("slack_1785.12", linked="slack:1785.12", channel_origin=True),
    }
    async with TestClient(TestServer(_app(log, slots))) as client:
        rows = (await (await client.get("/api/sessions/search?q=grid")).json())["sessions"]
    marked = {row["key"]: row.get("open") for row in rows}
    assert marked == {
        "dashboard_chat-1-1": True,
        "cron_job7": True,
        "slack_1785.12": True,
        "dashboard_chat-old": None,
    }


@pytest.mark.asyncio
async def test_a_closed_tab_is_not_marked(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.append("dashboard:chat-1-1", "user", "grid planning notes")
    async with TestClient(TestServer(_app(log, {}))) as client:
        rows = (await (await client.get("/api/sessions/search?q=grid")).json())["sessions"]
    assert [row["key"] for row in rows] == ["dashboard_chat-1-1"]
    assert "open" not in rows[0]


@pytest.mark.asyncio
async def test_an_app_caller_gets_no_open_flag(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.append("dashboard:chat-1-1", "user", "grid planning notes")

    @web.middleware
    async def as_app(request, handler):
        request["app"] = "some-app"
        return await handler(request)

    app = web.Application(middlewares=[as_app])
    app["state"] = SimpleNamespace(conversation_log=log, _slots={"chat-1-1": _slot("chat-1-1")})
    app.router.add_get("/api/sessions/search", api_sessions_search)
    async with TestClient(TestServer(app)) as client:
        rows = (await (await client.get("/api/sessions/search?q=grid")).json())["sessions"]
    assert all("open" not in row for row in rows)


def test_a_state_without_slots_marks_nothing():
    from kiro_crew.dashboard.session_open_marks import mark_open_sessions

    rows = [{"key": "dashboard_chat-1-1"}]
    mark_open_sessions(rows, SimpleNamespace(conversation_log=None))
    assert rows == [{"key": "dashboard_chat-1-1"}]
