"""Forks capture member identity and strict privacy before copying conversation rows."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest
from aiohttp.streams import EMPTY_PAYLOAD
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from chat_test_helpers import _make_app, _make_state

from kiro_crew import execution_context as execution
from kiro_crew import memory_stores
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.sections import MemoryStoreConfig
from kiro_crew.dashboard import chat_fork, chat_persistence
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.testing.wait import await_until


@pytest.fixture
def fork_source(tmp_path, monkeypatch):
    cfg = KiroCrewConfig.load()
    cfg.agents["writer"] = KiroCrewAgentConfig()
    cfg.agents["peer"] = KiroCrewAgentConfig()
    store = memory_stores.provision_member_memory(cfg, "writer")
    peer_store = memory_stores.provision_member_memory(cfg, "peer")
    cfg.save()
    state = _make_state(tmp_path / "sessions")
    state.conversation_log = ConversationLog()
    parent = state.get_or_create_slot("source", agent="writer")
    parent.memory_store = store
    execution.bind_session_execution(
        effective_session_key(parent),
        execution.resolve_member_execution(cfg, "writer", validate_memory_files=False),
    )
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
async def test_member_fork_inherits_identity_before_copy_and_keeps_bodies_under_their_mode(
    fork_source, monkeypatch, memory_mode
):
    _cfg, state, parent, store, _peer_store = fork_source
    parent.memory_mode = memory_mode
    parent_key = effective_session_key(parent)
    captured = execution.read_session_execution(parent_key).with_mode(memory_mode)
    execution.bind_session_execution(parent_key, captured)
    await chat_persistence.save_slot_off_loop(state, parent)
    parent_bytes = state.conversation_log._path(parent_key).read_bytes()
    appended = []
    original_append = _ChatSlot.append

    def checked_append(slot, *args, **kwargs):
        if slot is not parent:
            assert execution.read_session_execution(effective_session_key(slot)) == captured
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
    assert state.conversation_log._path(parent_key).read_bytes().replace(
        b"\r\n", b"\n"
    ) == parent_bytes.replace(b"\r\n", b"\n")
    assert execution.read_session_execution(key) == captured
    if memory_mode == "persistent":
        assert state.conversation_log.get_metadata(key)["execution_context"] == captured.to_record()
        state._slots.pop(child.key)
        restored = chat_persistence._rehydrate_slot_from_history(state, child.key)
        assert restored is not None
        assert restored.memory_store == store
        assert restored.memory_mode == memory_mode
    else:
        # Both transcripts are kept -- the user reopens either from History --
        # under a line that records the restricted mode and names no memory
        # store, so nothing learns from them and a restart reads them as unbound
        # rather than as a legacy member record it must refuse.
        assert b"Keep the release checklist" in parent_bytes
        assert state.conversation_log.get_metadata(parent_key).get("memory_mode") == memory_mode
        assert "memory_store" not in state.conversation_log.get_metadata(parent_key)
        child_meta = state.conversation_log.get_metadata(key)
        assert child_meta.get("memory_mode") == memory_mode
        assert "memory_store" not in child_meta
        assert "execution_context" not in child_meta
        assert [row["content"] for row in state.conversation_log.read_messages(key)] == [
            "Keep the release checklist.",
            "Use the blue checklist.",
        ]
        state._slots.pop(child.key)
        execution.clear_session_execution(key)
        assert execution.read_session_execution(key) is None
        # The probe above checks the fork's identity-before-copy window; the
        # restart rehydrate below runs with no live carrier by construction.
        monkeypatch.setattr(_ChatSlot, "append", original_append)
        restored = chat_persistence._rehydrate_slot_from_history(state, child.key)
        assert restored is not None
        assert restored.memory_mode == memory_mode
        assert restored.agent == "writer"
        assert key in state._restricted_keys


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage", ["missing-identity", "malformed-identity", "changed-recorded-store"]
)
async def test_member_fork_refuses_inconsistent_parent_before_creating_child(fork_source, damage):
    _cfg, state, parent, _store, peer_store = fork_source
    await chat_persistence.save_slot_off_loop(state, parent)
    parent_key = effective_session_key(parent)
    if damage == "missing-identity":
        state.conversation_log.update_metadata(parent_key, {"execution_context": None})
    elif damage == "malformed-identity":
        state.conversation_log.update_metadata(parent_key, {"execution_context": {"member_id": 7}})
    else:
        parent.memory_store = peer_store
    before = set(state._slots)
    parent_bytes = state.conversation_log._path(parent_key).read_bytes()
    status, body = await _fork(state, parent)
    assert status == 503
    assert body["code"] == "store_unavailable"
    assert set(state._slots) == before
    assert state.conversation_log._path(parent_key).read_bytes().replace(
        b"\r\n", b"\n"
    ) == parent_bytes.replace(b"\r\n", b"\n")


@pytest.mark.asyncio
async def test_private_pin_failure_discards_empty_child_and_keeps_source(fork_source, monkeypatch):
    _cfg, state, parent, _store, _peer_store = fork_source
    parent.memory_mode = "temporary"
    await chat_persistence.save_slot_off_loop(state, parent)
    before = set(state._slots)
    restricted = set(state._restricted_keys)
    attempted = []

    def cannot_publish(key, captured):
        attempted.append(key)
        raise OSError("binding publication unavailable")

    monkeypatch.setattr(execution, "bind_session_execution", cannot_publish)
    status, body = await _fork(state, parent)

    assert status == 503
    assert body["code"] == "store_unavailable"
    assert len(attempted) == 1
    assert set(state._slots) == before
    assert state._restricted_keys == restricted
    assert not state.conversation_log.has_log(attempted[0])
    assert execution.read_session_execution(attempted[0]) is None


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
    original_bind = execution.bind_session_execution

    def blocked_bind(key, selected_store):
        attempted.append(key)
        loop.call_soon_threadsafe(entered.set)
        assert released.wait(10), "test did not release the binding worker"
        original_bind(key, selected_store)
        completed.set()

    monkeypatch.setattr(execution, "bind_session_execution", blocked_bind)
    request = make_mocked_request(
        "POST",
        f"/api/chat/slots/{parent.key}/fork",
        app=_make_app(state),
        match_info={"slot": parent.key},
        payload=EMPTY_PAYLOAD,
    )
    task = asyncio.create_task(chat_fork.api_chat_slot_fork(request))
    try:
        await await_until(entered, what="the fork entering the blocked bind")
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
            await await_until(task, what="the cancelled fork finishing")
    finally:
        released.set()
        await await_until(
            asyncio.gather(task, return_exceptions=True), what="the fork task settling"
        )

    assert completed.is_set()
    assert len(attempted) == 1
    assert set(state._slots) == before
    assert state._restricted_keys == restricted
    assert not state.conversation_log.has_log(attempted[0])
    assert execution.read_session_execution(attempted[0]) is None
    assert execution.read_session_execution(parent_key).store.legacy_name == store
    assert state.conversation_log._path(parent_key).read_bytes().replace(
        b"\r\n", b"\n"
    ) == parent_bytes.replace(b"\r\n", b"\n")


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
    assert execution.read_session_execution(effective_session_key(child)).member_id is None
    assert child.messages[0]["content"] == "Keep this V1 conversation"


@pytest.mark.asyncio
async def test_legacy_fork_keeps_the_identity_the_parent_ran_as(tmp_path):
    """A parent with no carrier must fork to the identity its own turns resolved to.

    A ``config.agents`` alias on a slot not stamped ``template`` runs as that
    crewmate: its template, model pin and store. Stamping the alias as a
    template id made the forked chat ask kiro-cli for an agent called
    ``default`` and fail on its first turn. A template-stamped slot, and a name
    that is no alias (possibly a template only the parent's project declares),
    stay template sessions under the name as written.
    """
    from kiro_crew.config.loader import resolve_agent_bindings
    from kiro_crew.session_agent_selection import resolve_session_agent_bindings

    cfg = KiroCrewConfig.load()
    cfg.agents["default"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    cfg.agents["builder"] = KiroCrewAgentConfig(kiro_agent="amzn-builder", model="sonnet")
    cfg.save()
    state = _make_state(tmp_path / "sessions")
    state.conversation_log = ConversationLog()
    for alias, agent_kind, kind, template, model in (
        ("default", "", "member", "kirocrew", ""),
        ("builder", "", "member", "amzn-builder", "sonnet"),
        ("builder", "member", "member", "amzn-builder", "sonnet"),
        ("builder", "template", "template", "builder", ""),
        ("project-only-template", "", "template", "project-only-template", ""),
    ):
        parent = state.get_or_create_slot(f"legacy-{alias}-{agent_kind or 'blank'}", agent=alias)
        parent.agent_kind = agent_kind
        parent.append("user", "An old conversation", "msg msg-u")
        parent.drain()
        assert execution.read_session_execution(effective_session_key(parent)) is None

        status, body = await _fork(state, parent)

        assert status == 200, body
        child = state._slots[body["key"]]
        child_key = effective_session_key(child)
        carrier = execution.read_session_execution(child_key)
        assert (carrier.selection_kind, carrier.template_id) == (kind, template)
        if kind == "member":
            assert carrier.selection_name == alias
            bindings = resolve_session_agent_bindings(
                resolve_agent_bindings, KiroCrewConfig.load(), child_key, child.agent
            )
            assert bindings.requested_resolved
            assert (bindings.kiro_agent, bindings.resolved_alias) == (template, alias)
            assert bindings.model == model


@pytest.mark.asyncio
async def test_legacy_fork_of_a_skill_view_name_resolves_its_source_agent(tmp_path, monkeypatch):
    """A stored skill-view name forks as the agent the view was built from.

    The parent's carrier-less turns map the view name back to its source before
    the alias check, so a view built from the ``builder`` crewmate alias ran as
    that crewmate. The fork makes the same mapping and records the crewmate,
    instead of a template carrier the child would launch without its pin.
    """
    from kiro_crew.agent_sdk.drivers import acp as acp_driver
    from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX

    view = f"{NATIVE_SKILL_ALIAS_PREFIX}0123abcd"
    monkeypatch.setattr(
        acp_driver, "skill_view_source_agent", lambda name: "builder" if name == view else name
    )
    cfg = KiroCrewConfig.load()
    cfg.agents["builder"] = KiroCrewAgentConfig(kiro_agent="amzn-builder", model="sonnet")
    cfg.save()
    state = _make_state(tmp_path / "sessions")
    state.conversation_log = ConversationLog()
    parent = state.get_or_create_slot("legacy-view", agent=view)
    parent.append("user", "An old conversation", "msg msg-u")
    parent.drain()
    assert execution.read_session_execution(effective_session_key(parent)) is None

    status, body = await _fork(state, parent)

    assert status == 200, body
    carrier = execution.read_session_execution(effective_session_key(state._slots[body["key"]]))
    assert (carrier.selection_kind, carrier.selection_name, carrier.template_id) == (
        "member",
        "builder",
        "amzn-builder",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded_store", ["", "default"])
async def test_legacy_fork_of_a_v2_crewmate_alias_is_refused(fork_source, recorded_store):
    """A record-less parent cannot prove it ran on a crewmate's private V2 store.

    With no recorded store the recorded-store V2 check passes on the default
    store, so the crewmate's resolved store must be checked too; the fork is
    refused before any child exists rather than bound to that private memory.
    """
    _cfg, state, _parent, _store, _peer_store = fork_source
    parent = state.get_or_create_slot("legacy-writer", agent="writer")
    parent.memory_store = recorded_store
    parent.append("user", "An old conversation", "msg msg-u")
    parent.drain()
    assert execution.read_session_execution(effective_session_key(parent)) is None
    before = set(state._slots)

    status, body = await _fork(state, parent)

    assert status == 503, body
    assert body["code"] == "store_unavailable"
    assert set(state._slots) == before
