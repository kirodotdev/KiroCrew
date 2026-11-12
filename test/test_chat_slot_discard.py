"""``POST /api/chat/slots/{slot}/discard`` removes a session nothing was written in.

The artifact companion chat creates its session when its panel opens and discards
it, slot and history row, when the panel closes unused; the routed chat does the
same for the session an app's draft launch created, when the user leaves it unsent. The page's "unused" reading
comes from one window's copy of the slot, so the server decides on the live slot,
and it deletes the history row only when the close it performed left the key free.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.cron_service.store import CronStoreBusy
from kiro_crew.dashboard import chat_handlers, slot_retention
from kiro_crew.dashboard.handlers import sessions


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        slot_retention, "live_loop_slot_keys", lambda *, include_paused=False: set()
    )
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    return st


def _client(state, discard_user: str = "") -> TestClient:
    app = _make_app(state)
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    if discard_user:

        @web.middleware
        async def _discard_as(request, handler):
            if request.path.endswith("/discard"):
                request["user"] = discard_user
            return await handler(request)

        app.middlewares.append(_discard_as)
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
async def test_a_draft_launch_session_is_closed_and_leaves_no_history_row(state):
    # The routed chat creates an app's draft launch session with the launch's memory
    # mode and no session folder, and discards it when the user leaves it unsent.
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots", json={"memory_mode": "persistent", "no_session_folder": True}
        )
        assert resp.status == 200
        name = (await resp.json())["key"]
        status, _ = await _discard(client, name)
    # An untitled empty session saves no History row, so the delete finds none.
    assert status == 200
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
async def test_saved_messages_the_live_window_does_not_hold_keep_the_history_row(state):
    # A slot recreated on an archived key opens empty over the saved transcript.
    async with _client(state) as client:
        name = await _create(client)
        state.conversation_log.append(f"dashboard:{name}", "user", "an earlier turn")
        status, body = await _discard(client, name)
    assert status == 409 and body["code"] == "slot_not_empty"
    assert state.conversation_log.has_messages(f"dashboard:{name}")


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
@pytest.mark.parametrize("active", [True, False], ids=["armed", "paused"])
async def test_a_slot_with_a_goal_loop_or_monitor_is_refused(state, monkeypatch, active):
    async with _client(state) as client:
        name = await _create(client)
        loop = SimpleNamespace(active=active, slot_key=name)
        monkeypatch.setattr(
            slot_retention,
            "live_loop_slot_keys",
            lambda *, include_paused=False: slot_retention.loop_slot_keys(
                [loop], include_paused=include_paused
            ),
        )
        status, body = await _discard(client, name)
    assert status == 409 and body["code"] == "automation_active"
    assert name in state._slots
    assert _history_row_exists(state, name)


@pytest.mark.asyncio
async def test_unknown_loop_bindings_keep_the_slot_and_its_history_row(state, monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.slot_retention.live_loop_slot_keys",
        lambda *, include_paused=False: None,
    )
    async with _client(state) as client:
        name = await _create(client)
        slot = state._slots[name]
        status, body = await _discard(client, name)
    assert status == 409 and body["code"] == "automation_unknown"
    assert state._slots.get(name) is slot
    assert _history_row_exists(state, name)


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
async def test_a_replacement_created_while_the_close_awaits_is_kept(state, monkeypatch):
    retire = chat_handlers._retire_slot_nudge_loop
    replacement = None
    turn = asyncio.get_running_loop().create_future()
    try:
        async with _client(state) as client:
            name = await _create(client)
            original = state._slots[name]

            async def retire_while_the_slot_is_replaced(key):
                nonlocal replacement
                assert state._slots.pop(name) is original
                replacement = state.get_or_create_slot(name)
                replacement.append("user", "hello", "msg msg-u")
                state.conversation_log.append(f"dashboard:{name}", "user", "hello")
                replacement.task = turn
                return await retire(key)

            monkeypatch.setattr(
                chat_handlers, "_retire_slot_nudge_loop", retire_while_the_slot_is_replaced
            )
            status, body = await _discard(client, name)
        assert replacement is not None and replacement is not original
        assert state._slots.get(name) is replacement
        assert status == 409 and body["code"] == "slot_replaced"
        assert not turn.done()
        assert _history_row_exists(state, name)
        assert state.conversation_log.has_messages(f"dashboard:{name}")
    finally:
        turn.cancel()


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
async def test_an_app_owned_slot_is_refused(state):
    async with _client(state) as client:
        name = await _create(client)
        slot = state._slots[name]
        slot._app = "demo-app"
        status, body = await _discard(client, name)
    assert status == 409 and body["code"] == "not_discardable"
    assert state._slots.get(name) is slot
    assert _history_row_exists(state, name)


@pytest.mark.asyncio
async def test_an_unknown_slot_is_not_found(state):
    async with _client(state) as client:
        status, _ = await _discard(client, "missing")
    assert status == 404


@pytest.mark.asyncio
async def test_a_non_owner_dashboard_session_keeps_the_history_row(state):
    # A channel allowlist link: an empty app claim, a subject that is not the owner.
    async with _client(state, discard_user="U0NONOWNER") as client:
        name = await _create(client)
        status, body = await _discard(client, name)
    assert status == 200 and body == {"ok": True}
    assert name not in state._slots
    assert _history_row_exists(state, name)


@pytest.mark.asyncio
async def test_a_refused_history_delete_is_the_answer(state, monkeypatch):
    async def busy_store(_crons, _keys):
        raise CronStoreBusy("the cron store is busy")

    monkeypatch.setattr(sessions, "_owner_keys_bound_to_transcript", busy_store)
    async with _client(state) as client:
        name = await _create(client)
        status, body = await _discard(client, name)
    assert status == 409 and body["code"] == "cron_store_busy"
    assert name not in state._slots
    assert _history_row_exists(state, name)


@pytest.mark.asyncio
async def test_a_send_while_the_history_delete_runs_is_refused(state, monkeypatch):
    scan = sessions._owner_keys_bound_to_transcript
    sent: dict = {}
    async with _client(state) as client:
        name = await _create(client)

        async def scan_while_another_window_sends(crons, keys):
            resp = await client.post("/api/chat", json={"slot": name, "message": "hello"})
            sent.update(status=resp.status, body=await resp.json())
            return await scan(crons, keys)

        monkeypatch.setattr(
            sessions, "_owner_keys_bound_to_transcript", scan_while_another_window_sends
        )
        status, _ = await _discard(client, name)
    assert status == 200
    assert sent["status"] == 409 and sent["body"]["code"] == "delete_in_flight"
    assert name not in state._slots
    assert not _history_row_exists(state, name)


@pytest.mark.asyncio
async def test_a_named_create_while_the_history_delete_runs_is_refused(state, monkeypatch):
    scan = sessions._owner_keys_bound_to_transcript
    created: dict = {}
    async with _client(state) as client:
        name = await _create(client)

        async def scan_while_another_window_creates(crons, keys):
            resp = await client.post(
                "/api/chat/slots", json={"name": name, "no_session_folder": True}
            )
            created.update(status=resp.status, body=await resp.json())
            return await scan(crons, keys)

        monkeypatch.setattr(
            sessions, "_owner_keys_bound_to_transcript", scan_while_another_window_creates
        )
        status, _ = await _discard(client, name)
    assert status == 200
    assert created["status"] == 409 and created["body"]["code"] == "delete_in_flight"
    assert name not in state._slots
    assert not _history_row_exists(state, name)
