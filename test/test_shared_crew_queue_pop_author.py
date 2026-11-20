"""A teammate's queued send names its sender on the row every open tab rebuilds.

A send that lands on a busy session is queued, and no ``chat_message`` echo
follows its drained user row: each open tab rebuilds that row from the drain's
``queue_pop`` frame alone. The frame carries the entry's server-stamped author,
so the rebuilt row draws the same byline the stored row does after a reload.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.shared_crew.attribution import author_stamp
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    ActorContext,
    DisplaySnapshot,
    Principal,
    PrincipalKind,
    Provenance,
    Transport,
    principal_id_for,
)

BOB = Principal(
    id=principal_id_for("tailscale", "tailnet", "bob@example.test"),
    kind=PrincipalKind.HUMAN,
    provider="tailscale",
    issuer="tailnet",
    subject="bob@example.test",
    provenance=Provenance.TAILSCALE_SERVE,
    display=DisplaySnapshot(name="bob", login="bob@example.test"),
)


def _client(state, principal: Principal) -> TestClient:
    from kiro_crew.dashboard.chat_handlers import api_chat

    @web.middleware
    async def actor(request: web.Request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        request[ACTOR_REQUEST_KEY] = ActorContext(
            principal=principal, request_id="r", transport=Transport.HTTP
        )
        return await handler(request)

    app = web.Application(middlewares=[actor])
    app["state"] = state
    app.router.add_post("/api/chat", api_chat)
    return TestClient(TestServer(app))


@pytest.fixture
def busy(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.broadcast_ws_owners = MagicMock()
    state.push_slots_update = MagicMock()
    state.owner_id = ""
    state.subagents = None
    slot = state.get_or_create_slot("new-chat")
    slot.task = MagicMock(done=MagicMock(return_value=False))  # the busy queue path
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", MagicMock())
    return state, slot


async def _drain(state, slot) -> dict:
    from kiro_crew.dashboard import chat_runner

    slot.task = None
    with (
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
    ):
        assert await chat_runner._start_next_queued_turn(state, slot) is True
    return next(c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_pop")


@pytest.mark.asyncio
async def test_a_teammates_first_message_queued_on_a_new_session_names_them(busy):
    state, slot = busy
    with patch("kiro_crew.dashboard.chat_handlers._maybe_auto_title", new=AsyncMock()):
        async with _client(state, BOB) as client:
            resp = await client.post("/api/chat", json={"slot": "new-chat", "message": "hi"})
            assert resp.status == 200 and (await resp.json()).get("queued") is True
    pop = await _drain(state, slot)
    assert pop["meta"]["author"] == author_stamp(BOB)
    stored = [m for m in slot.messages if m.get("role") == "user"]
    assert stored[-1]["meta"]["author"]["id"] == BOB.id


@pytest.mark.asyncio
async def test_an_entry_with_no_author_rebuilds_a_row_with_none(busy):
    state, slot = busy
    slot.queue_append("restored from disk")
    pop = await _drain(state, slot)
    assert "author" not in (pop.get("meta") or {})
