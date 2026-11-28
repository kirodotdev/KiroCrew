"""Only a row's author may edit it or rewind to it, and the edited row names them.

The request's actor is published by a middleware in the request's own task,
as ``identity.publish_actor`` does; the real ``api_chat_slot_edit_resend`` and
``api_chat_slot_rewind`` then judge and write the row. ``_run_chat`` is patched,
so no backend session starts. The rule itself is pinned by
``test_shared_crew_edit_rule.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_regenerate import (
    api_chat_slot_edit_resend,
    api_chat_slot_regenerate,
)
from kiro_crew.dashboard.chat_rewind import api_chat_slot_rewind
from kiro_crew.shared_crew import attribution, preview
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


def _person(login: str) -> Principal:
    return Principal(
        id=principal_id_for("tailscale", "tailnet", login),
        kind=PrincipalKind.HUMAN,
        provider="tailscale",
        issuer="tailnet",
        subject=login,
        provenance=Provenance.TAILSCALE_SERVE,
        display=DisplaySnapshot(name=login.split("@")[0], login=login),
    )


ALICE = _person("alice@example.test")
BOB = _person("bob@example.test")


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.sessions.discard_conversation = AsyncMock(return_value=True)
    st.sessions.aflush = AsyncMock()
    st.sessions._session_map.get = MagicMock(return_value="")
    return st


def _client(state, actor: Principal) -> TestClient:
    @web.middleware
    async def as_actor(request: web.Request, handler):
        context = ActorContext(principal=actor, request_id="r", transport=Transport.HTTP)
        request[ACTOR_REQUEST_KEY] = context
        attribution.bind_request_author(context)
        return await handler(request)

    app = web.Application(middlewares=[as_actor])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/edit-resend", api_chat_slot_edit_resend)
    app.router.add_post("/api/chat/slots/{slot}/regenerate", api_chat_slot_regenerate)
    app.router.add_post("/api/chat/slots/{slot}/rewind", api_chat_slot_rewind)
    return TestClient(TestServer(app))


def _author_id(row: dict) -> str | None:
    return ((row.get("meta") or {}).get("author") or {}).get("id")


@pytest.fixture
def preview_on(monkeypatch):
    monkeypatch.setattr(preview, "preview_enabled", lambda: True)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["edit-resend", "rewind"])
async def test_bob_cannot_edit_or_rewind_to_alices_row(state, preview_on, route) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "alice asks", meta={"author": ALICE.to_public_dict()})
    slot.append("assistant", "answer")
    slot.drain()
    run_chat = AsyncMock()

    with (
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run_chat),
        patch("kiro_crew.dashboard.chat_rewind._run_chat", new=run_chat),
    ):
        async with _client(state, BOB) as client:
            resp = await client.post(
                f"/api/chat/slots/s1/{route}", json={"index": 0, "content": "bob edits"}
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "not_message_author"

    assert [m["content"] for m in slot.messages] == ["alice asks", "answer"]
    run_chat.assert_not_called()
    state.sessions.discard_conversation.assert_not_called()


@pytest.mark.asyncio
async def test_bob_editing_his_own_row_is_attributed_to_bob(state, preview_on) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "bob asks", meta={"author": BOB.to_public_dict()})
    slot.append("assistant", "answer")
    slot.drain()

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state, BOB) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "bob edits"}
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    rows = [m for m in slot.messages if m["role"] == "user"]
    assert [m["content"] for m in rows] == ["bob edits"]
    assert _author_id(rows[0]) == BOB.id


@pytest.mark.asyncio
async def test_regenerate_keeps_the_original_author(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "alice asks", meta={"author": ALICE.to_public_dict()})
    slot.append("assistant", "answer")
    slot.drain()

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state, BOB) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            await asyncio.sleep(0)

    rows = [m for m in slot.messages if m["role"] == "user"]
    assert [_author_id(m) for m in rows] == [ALICE.id]
