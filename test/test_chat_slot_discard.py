"""``POST /api/chat/slots/{slot}/discard`` removes a session nothing was written in.

The artifact companion chat creates its session when its panel opens and discards
it, slot and history row, when the panel closes unused. The page's "unused" reading
comes from one window's copy of the slot, so the server decides on the live slot,
and it deletes the history row only when the close it performed left the key free.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_handlers


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    return st


def _client(state) -> TestClient:
    app = _make_app(state)
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    return TestClient(TestServer(app))


async def _create(client) -> str:
    """A companion session as the artifact page creates it: pinned title, bound slug,
    and the titled History row a live gateway lists for it."""
    resp = await client.post(
        "/api/chat/slots",
        json={"title": "Artifact: Notes", "artifact": "notes", "no_session_folder": True},
    )
    assert resp.status == 200
    name = (await resp.json())["key"]
    state = client.server.app["state"]
    state.conversation_log.set_title(f"dashboard:{name}", "Artifact: Notes")
    assert _history_row_exists(state, name)
    return name


async def _discard(client, name: str):
    resp = await client.post(f"/api/chat/slots/{name}/discard")
    return resp.status, await resp.json()


def _history_row_exists(state, name: str) -> bool:
    return state.conversation_log._path(f"dashboard:{name}").exists()


@pytest.mark.asyncio
async def test_an_empty_slot_is_closed_and_its_history_row_deleted(state):
    async with _client(state) as client:
        name = await _create(client)
        status, body = await _discard(client, name)
    assert status == 200 and body == {"ok": True}
    assert name not in state._slots
    assert not _history_row_exists(state, name)


@pytest.mark.asyncio
async def test_a_slot_with_a_message_is_refused(state):
    async with _client(state) as client:
        name = await _create(client)
        state._slots[name].append("user", "hello", "msg msg-u")
        status, body = await _discard(client, name)
    assert status == 409 and body["code"] == "slot_not_empty"
    assert name in state._slots
    assert _history_row_exists(state, name)


@pytest.mark.asyncio
async def test_a_slot_with_a_running_turn_is_refused(state):
    async with _client(state) as client:
        name = await _create(client)
        turn = asyncio.get_running_loop().create_future()
        state._slots[name].task = turn
        try:
            status, body = await _discard(client, name)
        finally:
            turn.cancel()
    assert status == 409 and body["code"] == "turn_in_flight"
    assert name in state._slots


@pytest.mark.asyncio
async def test_a_slot_with_a_queued_turn_is_refused(state):
    async with _client(state) as client:
        name = await _create(client)
        state._slots[name]._queue.append({"id": "q1", "content": "next"})
        status, body = await _discard(client, name)
    assert status == 409 and body["code"] == "slot_not_empty"
    assert name in state._slots


@pytest.mark.asyncio
async def test_a_message_that_lands_while_the_close_awaits_is_kept(state, monkeypatch):
    retire = chat_handlers._retire_slot_nudge_loop
    async with _client(state) as client:
        name = await _create(client)
        slot = state._slots[name]

        async def retire_while_a_message_lands(key):
            slot.append("user", "hello", "msg msg-u")
            return await retire(key)

        monkeypatch.setattr(chat_handlers, "_retire_slot_nudge_loop", retire_while_a_message_lands)
        status, body = await _discard(client, name)
    assert status == 409 and body["code"] == "slot_not_empty"
    assert state._slots.get(name) is slot
    assert _history_row_exists(state, name)


@pytest.mark.asyncio
async def test_a_replacement_created_while_the_close_saves_keeps_its_history(state, monkeypatch):
    save = chat_handlers.save_slot_off_loop
    replacement = None
    async with _client(state) as client:
        name = await _create(client)
        original = state._slots[name]

        async def save_while_a_send_recreates_the_key(*args, **kwargs):
            nonlocal replacement
            replacement = state.get_or_create_slot(name)
            replacement.append("user", "hello", "msg msg-u")
            return await save(*args, **kwargs)

        monkeypatch.setattr(
            chat_handlers, "save_slot_off_loop", save_while_a_send_recreates_the_key
        )
        status, body = await _discard(client, name)
    assert status == 200 and body == {"ok": True}
    assert replacement is not None and replacement is not original
    assert state._slots.get(name) is replacement
    assert _history_row_exists(state, name)


@pytest.mark.asyncio
async def test_a_linked_slot_is_refused(state):
    async with _client(state) as client:
        name = await _create(client)
        slot = state._slots[name]
        slot.linked_session_key = "slack:C1:1.0"
        status, body = await _discard(client, name)
    assert status == 409 and body["code"] == "not_discardable"
    assert state._slots.get(name) is slot


@pytest.mark.asyncio
async def test_an_unknown_slot_is_not_found(state):
    async with _client(state) as client:
        status, _ = await _discard(client, "missing")
    assert status == 404
