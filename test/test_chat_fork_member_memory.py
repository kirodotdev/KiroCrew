"""Forks inherit verified private authority before copying any conversation rows."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest
from aiohttp.streams import EMPTY_PAYLOAD
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from chat_test_helpers import _make_app, _make_state

from kiro_crew import member_memory_auth as auth
from kiro_crew import memory_stores
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.sections import MemoryStoreConfig
from kiro_crew.dashboard import chat_fork, chat_persistence, chat_runner
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.state import _ChatSlot


@pytest.fixture
def fork_source(tmp_path, monkeypatch):
    cfg = KiroCrewConfig.load()
    cfg.agents["writer"] = KiroCrewAgentConfig()
    cfg.agents["peer"] = KiroCrewAgentConfig()
    store = memory_stores.provision_member_memory(cfg, "writer")
    peer_store = memory_stores.provision_member_memory(cfg, "peer")
    cfg.save()
    state = _make_state(tmp_path / "sessions")
    parent = state.get_or_create_slot("source", agent="writer")
    parent.memory_store = store
    auth.bind_private_session_store(effective_session_key(parent), store)
    parent.append("user", "Keep the release checklist.", "msg msg-u")
    parent.append("assistant", "Use the blue checklist.", "msg msg-a")
    parent.drain()
    monkeypatch.setattr(chat_fork, "sel", lambda: MagicMock())
    return cfg, state, parent, store, peer_store


async def _fork(state, parent):
    async with TestClient(TestServer(_make_app(state))) as client:
        response = await client.post(f"/api/chat/slots/{parent.key}/fork", json={})
        return response.status, await response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_mode", ["persistent", "incognito", "temporary"])
async def test_private_fork_is_pinned_before_copy_and_can_continue_after_restore(
    fork_source, monkeypatch, memory_mode
):
    _cfg, state, parent, store, _peer_store = fork_source
    parent.memory_mode = memory_mode
    await chat_persistence.save_slot_off_loop(state, parent)
    parent_key = effective_session_key(parent)
    parent_bytes = state.conversation_log._path(parent_key).read_bytes()
    appended = []
    original_append = _ChatSlot.append

    def checked_append(slot, *args, **kwargs):
        if slot is not parent:
            assert auth.read_private_session_store(effective_session_key(slot)) == store
            assert slot.memory_store == store
            assert slot.memory_mode == memory_mode
            appended.append(args[0])
        return original_append(slot, *args, **kwargs)

    monkeypatch.setattr(_ChatSlot, "append", checked_append)
    status, body = await _fork(state, parent)

    assert status == 200, body
    child = state._slots[body["key"]]
    key = effective_session_key(child)
    assert key != parent_key
    assert appended == ["user", "assistant"]
    assert child.agent == "writer"
    assert child.memory_store == store
    assert body["memory_mode"] == memory_mode
    assert child.is_restricted is (memory_mode != "persistent")
    assert child.blocks_reads is (memory_mode == "temporary")
    assert (key in state._restricted_keys) is (memory_mode != "persistent")
    assert [row["content"] for row in child.messages] == [
        "Keep the release checklist.",
        "Use the blue checklist.",
    ]
    assert state.conversation_log._path(parent_key).read_bytes() == parent_bytes
    assert auth.read_private_session_store(parent_key) == store
    assert auth.read_private_session_store(key) == store
    assert state.conversation_log.get_metadata(key)["memory_store"] == store
    # The real continuation guard sees a stored transcript and native-looking
    # assistant rows; only the protected pin lets this fork retain its member.
    await asyncio.to_thread(
        chat_runner._bind_private_slot_memory,
        key,
        store,
        restored=True,
        conversation_log=state.conversation_log,
        native_context=True,
    )
    state._slots.pop(child.key)
    restored = chat_persistence._rehydrate_slot_from_history(state, child.key)
    assert restored is not None
    assert restored.memory_store == store
    assert restored.memory_mode == memory_mode
    await asyncio.to_thread(
        chat_runner._bind_private_slot_memory,
        key,
        store,
        restored=restored._memory_assignment_from_history,
        conversation_log=state.conversation_log,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage", ["missing-proof", "corrupt-proof", "peer-proof", "changed-member"]
)
async def test_private_fork_refuses_unproven_or_changed_parent_before_creating_child(
    fork_source, damage
):
    cfg, state, parent, store, peer_store = fork_source
    if damage == "missing-proof":
        # An old V1 transcript can name today's private member without possessing
        # its immutable authority. A new key supplies that genuinely absent case.
        parent = state.get_or_create_slot("unprotected-source", agent="writer")
        parent.memory_store = store
        parent.append("user", "Old V1 conversation", "msg msg-u")
        parent.append("assistant", "Old V1 answer", "msg msg-a")
        parent.drain()
    elif damage == "corrupt-proof":
        auth._session_binding_path(effective_session_key(parent)).unlink()
    elif damage == "peer-proof":
        parent = state.get_or_create_slot("peer-source", agent="writer")
        parent.memory_store = store
        auth.bind_private_session_store(effective_session_key(parent), peer_store)
        parent.append("user", "Foreign conversation", "msg msg-u")
        parent.drain()
    else:
        cfg.agents["writer"].memory_store = "default"
        cfg.save()
    await chat_persistence.save_slot_off_loop(state, parent)
    before = set(state._slots)
    parent_bytes = state.conversation_log._path(effective_session_key(parent)).read_bytes()

    status, body = await _fork(state, parent)

    assert status == 503
    assert body["code"] == "store_unavailable"
    assert set(state._slots) == before
    assert state.conversation_log._path(effective_session_key(parent)).read_bytes() == parent_bytes


@pytest.mark.asyncio
async def test_private_pin_failure_discards_empty_child_and_keeps_source(fork_source, monkeypatch):
    _cfg, state, parent, _store, _peer_store = fork_source
    parent.memory_mode = "temporary"
    await chat_persistence.save_slot_off_loop(state, parent)
    before = set(state._slots)
    restricted = set(state._restricted_keys)
    attempted = []

    def cannot_publish(key, store):
        attempted.append(key)
        raise OSError("binding publication unavailable")

    monkeypatch.setattr(auth, "bind_private_session_store", cannot_publish)
    status, body = await _fork(state, parent)

    assert status == 503
    assert body["code"] == "store_unavailable"
    assert len(attempted) == 1
    assert set(state._slots) == before
    assert state._restricted_keys == restricted
    assert not state.conversation_log.has_log(attempted[0])
    assert auth.read_private_session_store(attempted[0]) is None


@pytest.mark.asyncio
async def test_cancelled_private_fork_drains_binding_and_keeps_landed_proof(
    fork_source, monkeypatch
):
    _cfg, state, parent, store, _peer_store = fork_source
    parent.memory_mode = "temporary"
    await chat_persistence.save_slot_off_loop(state, parent)
    parent_key = effective_session_key(parent)
    parent_bytes = state.conversation_log._path(parent_key).read_bytes()
    before = set(state._slots)
    restricted = set(state._restricted_keys)
    entered = asyncio.Event()
    released = threading.Event()
    completed = threading.Event()
    attempted = []
    loop = asyncio.get_running_loop()
    original_bind = auth.bind_private_session_store

    def blocked_bind(key, selected_store):
        attempted.append(key)
        loop.call_soon_threadsafe(entered.set)
        assert released.wait(10), "test did not release the binding worker"
        original_bind(key, selected_store)
        completed.set()

    monkeypatch.setattr(auth, "bind_private_session_store", blocked_bind)
    request = make_mocked_request(
        "POST",
        f"/api/chat/slots/{parent.key}/fork",
        app=_make_app(state),
        match_info={"slot": parent.key},
        payload=EMPTY_PAYLOAD,
    )
    task = asyncio.create_task(chat_fork.api_chat_slot_fork(request))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert task.cancel()
        # The cancelled await runs before this queued checkpoint. It must keep
        # draining the worker, rather than remove a child the worker still owns.
        checkpoint = asyncio.Event()
        loop.call_soon(checkpoint.set)
        await checkpoint.wait()
        assert not task.done()
        assert not completed.is_set()
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
    finally:
        released.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)

    assert completed.is_set()
    assert len(attempted) == 1
    assert set(state._slots) == before
    assert state._restricted_keys == restricted
    assert not state.conversation_log.has_log(attempted[0])
    assert auth.read_private_session_store(attempted[0]) == store
    assert auth.read_private_session_store(parent_key) == store
    assert state.conversation_log._path(parent_key).read_bytes() == parent_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["default", "legacy-store"])
async def test_existing_v1_forks_keep_their_unprotected_memory_contract(tmp_path, store):
    cfg = KiroCrewConfig.load()
    cfg.agents["legacy"] = KiroCrewAgentConfig(memory_store=store)
    if store != "default":
        cfg.memory_stores[store] = MemoryStoreConfig()
        (memory_stores.memory_stores_root() / store).mkdir(parents=True)
    cfg.save()
    state = _make_state(tmp_path / "sessions")
    parent = state.get_or_create_slot("legacy-source", agent="legacy", memory_mode="incognito")
    parent.memory_store = store
    parent.append("user", "Keep this V1 conversation", "msg msg-u")
    parent.drain()

    status, body = await _fork(state, parent)

    assert status == 200, body
    child = state._slots[body["key"]]
    assert child.agent == "legacy"
    assert child.memory_mode == "incognito"
    assert child.is_restricted is True
    assert auth.read_private_session_store(effective_session_key(child)) is None
    assert child.messages[0]["content"] == "Keep this V1 conversation"
