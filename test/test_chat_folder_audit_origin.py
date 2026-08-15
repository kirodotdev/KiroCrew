"""SEL attribution for chat-folder writes comes from CALLER IDENTITY.

The regression these pin: the audit ``source`` on a folder mutation used to be a
constant (``"dashboard"``), and the natural next step — inferring ``"mcp"``
from the presence of ``X-Internal-Secret`` — is an inference about the transport,
not the caller. Either way the FIRST internal caller that is not the MCP server
gets attributed to something that did not make the write, and nothing fails.

So the tests assert the identity the auth middleware verified is what reaches the
log, and specifically that an internal caller which does NOT name itself lands as
``internal:unknown`` rather than being guessed at.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat_folders import (
    INTERNAL_UNKNOWN,
    api_chat_folder_create,
    api_chat_folder_delete,
    api_chat_folder_update,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog


class _RecordingSel:
    """Captures ``log_api_access`` kwargs instead of writing the SEL chain."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log_api_access(self, **kw: Any) -> None:
        self.events.append(kw)

    def folder_events(self) -> list[dict[str, Any]]:
        return [e for e in self.events if str(e.get("operation", "")).startswith("chat.folder")]


@pytest.fixture
def recorded_sel(monkeypatch) -> _RecordingSel:
    rec = _RecordingSel()
    # Patch the canonical accessor the handlers call through.
    monkeypatch.setattr("kiro_crew.dashboard.chat_folders.sel", lambda: rec)
    return rec


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.recycle_background = AsyncMock()
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    return state


def _make_app(state: DashboardState, *, request_keys: dict[str, Any]) -> web.Application:
    """Folder routes with the middleware-verified identity pre-seeded.

    Production sets ``request["app"]`` / ``request["internal_auth"]`` in
    ``token_auth``; seeding them directly exercises the real handlers without
    standing up the auth middleware.
    """

    @web.middleware
    async def _identity(request: web.Request, handler):  # type: ignore[no-untyped-def]
        for k, v in request_keys.items():
            request[k] = v
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = state
    app.router.add_post("/api/chat/folders", api_chat_folder_create)
    app.router.add_patch("/api/chat/folders/{id}", api_chat_folder_update)
    app.router.add_delete("/api/chat/folders/{id}", api_chat_folder_delete)
    return app


async def _client(app: web.Application) -> TestClient:
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_browser_caller_is_dashboard(tmp_path, recorded_sel):
    """Cookie-auth (no app claim, no internal auth) stays ``dashboard``."""
    client = await _client(_make_app(_make_state(tmp_path), request_keys={"app": ""}))
    try:
        resp = await client.post("/api/chat/folders", json={"name": "Design"})
        assert resp.status == 201
    finally:
        await client.close()

    ev = recorded_sel.folder_events()[-1]
    assert ev["caller"] == "dashboard"
    assert ev["source"] == "dashboard"


@pytest.mark.asyncio
async def test_app_token_caller_is_the_app(tmp_path, recorded_sel):
    """An app-token write is attributed to that app, not to the dashboard."""
    client = await _client(_make_app(_make_state(tmp_path), request_keys={"app": "issue-radar"}))
    try:
        resp = await client.post("/api/chat/folders", json={"name": "Triage"})
        assert resp.status == 201
    finally:
        await client.close()

    ev = recorded_sel.folder_events()[-1]
    assert ev["caller"] == "issue-radar"
    assert ev["source"] == "app"


@pytest.mark.asyncio
async def test_internal_caller_naming_itself_is_recorded_by_name(tmp_path, recorded_sel):
    """An internal caller that names itself in ``X-Internal-Caller`` is believed."""
    client = await _client(_make_app(_make_state(tmp_path), request_keys={"internal_auth": True}))
    try:
        resp = await client.post(
            "/api/chat/folders", json={"name": "Agent work"}, headers={"X-Internal-Caller": "mcp"}
        )
        assert resp.status == 201
    finally:
        await client.close()

    ev = recorded_sel.folder_events()[-1]
    assert ev["caller"] == "mcp"
    assert ev["source"] == "mcp"


@pytest.mark.asyncio
async def test_unnamed_internal_caller_is_not_guessed_as_mcp(tmp_path, recorded_sel):
    """THE regression: holding the internal secret does not make you the MCP server.

    A second internal caller — present-day or future — that does not name itself
    must be recorded as explicitly unknown. Attributing it to ``mcp`` would put a
    write in the log under a component that did not make it.
    """
    client = await _client(_make_app(_make_state(tmp_path), request_keys={"internal_auth": True}))
    try:
        # No X-Internal-Caller at all, then an unrecognized one.
        assert (await client.post("/api/chat/folders", json={"name": "A"})).status == 201
        resp = await client.post(
            "/api/chat/folders",
            json={"name": "B"},
            headers={"X-Internal-Caller": "some-future-component"},
        )
        assert resp.status == 201
    finally:
        await client.close()

    events = recorded_sel.folder_events()
    assert len(events) == 2
    for ev in events:
        assert ev["source"] == INTERNAL_UNKNOWN
        assert ev["caller"] == "internal"
        assert ev["source"] != "mcp"


@pytest.mark.asyncio
async def test_update_and_delete_carry_the_same_origin(tmp_path, recorded_sel):
    """Every folder-write route attributes through the same helper."""
    state = _make_state(tmp_path)
    client = await _client(_make_app(state, request_keys={"app": "spec-builder"}))
    try:
        created = await client.post("/api/chat/folders", json={"name": "Specs"})
        fid = (await created.json())["id"]
        assert (await client.patch(f"/api/chat/folders/{fid}", json={"name": "Specs v2"})).status == 200
        assert (await client.delete(f"/api/chat/folders/{fid}")).status == 200
    finally:
        await client.close()

    ops = [(e["operation"], e["caller"], e["source"]) for e in recorded_sel.folder_events()]
    assert ops == [
        ("chat.folder_create", "spec-builder", "app"),
        ("chat.folder_update", "spec-builder", "app"),
        ("chat.folder_delete", "spec-builder", "app"),
    ]
