"""Discovery cannot change the namespace selected by an existing conversation."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from chat_test_helpers import _make_app_with_agent_routes, _make_state, drain_background_tasks
from dashboard_owner_helpers import as_owner
from test_members_dm_thread import _make_members_app

from kiro_crew.agent_discovery import AgentInfo
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, resolve_agent_bindings
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard import chat_handlers, chat_runner
from kiro_crew.dashboard.chat_persistence import (
    _apply_recent_session,
    _rehydrate_slot_from_history,
    rehydrate_slot_from_history_async,
)
from kiro_crew.dashboard.handlers import agents
from kiro_crew.execution_context import read_session_execution
from kiro_crew.member_memory_auth import read_private_session_store
from kiro_crew.memory import MemoryStore
from kiro_crew.memory_stores import (
    UnknownMemoryStore,
    persist_member_config,
    provision_member_memory,
)
from kiro_crew.providers.base import (
    EVENT_AGENT_SWITCHED,
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    LLMEvent,
)
from kiro_crew.session_agent_selection import (
    record_agent_selection,
    resolve_session_agent_bindings,
    session_agent_selection_kind,
)
from kiro_crew.skills import SkillsLoader

TEMPLATE = "kirocrew-conductor"


def test_global_session_binding_matches_equivalent_alias(monkeypatch):
    from kiro_crew import execution_context, session_agent_selection

    cfg = KiroCrewConfig()
    cfg.agents = {
        "original": KiroCrewAgentConfig(kiro_agent="kirocrew"),
        "equivalent": KiroCrewAgentConfig(kiro_agent="kirocrew"),
    }
    cfg.default_agent = "original"
    captured = execution_context.ExecutionContext(
        None,
        execution_context.MemoryStoreRef("default"),
        "member",
        "kirocrew",
        selection_name="original",
    )
    monkeypatch.setattr(session_agent_selection, "read_session_execution", lambda _: captured)
    stored = resolve_session_agent_bindings(
        resolve_agent_bindings, cfg, "dashboard:global-alias", "original"
    )
    requested = resolve_agent_bindings(cfg, "equivalent")
    assert requested.requested_resolved
    assert stored.same_dispatch_binding(requested)
    assert stored.memory_store_name == "default"
    assert stored.execution_context is captured
    assert not stored.same_dispatch_binding(replace(requested, memory_store_name="other-store"))


def _turn_state(tmp_path, monkeypatch):
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "workspace"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    state = _make_state(tmp_path, context_builder=builder)
    state.context_builder.build_message = MagicMock(return_value=("task", None))
    state.context_builder.ensure_store = AsyncMock(return_value=object())
    provider = MagicMock()

    async def stream(*args, **kwargs):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="The task is complete.")
        yield LLMEvent(kind=EVENT_COMPLETE)

    provider.stream = stream
    state.sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    state.sessions.consume_replay_suppression = MagicMock(return_value=False)
    state.sessions.record_failure = AsyncMock()
    monkeypatch.setattr(chat_runner, "_maybe_auto_title", AsyncMock())
    monkeypatch.setattr(chat_runner, "generate_session_summary", AsyncMock())
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *a, **kw: None)
    return state


async def _template_chat(tmp_path, monkeypatch, *, first_turn=True):
    cfg = KiroCrewConfig.load()
    assert TEMPLATE not in cfg.agents
    cfg.save()
    # Keep discovery and the actual sync/config/private-store publication real;
    # the installed template catalog and provider are the external boundaries.
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: TEMPLATE if name == TEMPLATE else "",
    )
    monkeypatch.setattr(
        agents,
        "list_agents",
        lambda: [
            AgentInfo(
                name=TEMPLATE,
                filename=f"{TEMPLATE}.json",
                description="",
                model="auto",
                source="builtin",
            )
        ],
    )
    state = _turn_state(tmp_path, monkeypatch)
    app = _make_app_with_agent_routes(state)
    app.router.add_post("/api/agents/sync", agents.api_kirocrew_agents_sync)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat/slots", json={"name": "template-chat", "agent": TEMPLATE}
        )
        assert response.status == 200, await response.text()
        slot = state._slots["template-chat"]
        if first_turn:
            slot.append("user", "Complete the assigned task.")
            await asyncio.wait_for(chat_runner._run_chat(state, slot, "Complete the task."), 10)
            await asyncio.wait_for(drain_background_tasks(state), 10)
            state.sessions.record_failure.assert_not_awaited()
            assert state.sessions.get_or_create.await_count == 1

        response = await client.post("/api/agents/sync", json={})
        assert response.status == 200, await response.text()
        assert TEMPLATE in (await response.json())["synced"]
    synced = KiroCrewConfig.load()
    assert synced.agents[TEMPLATE].memory_store == "default"
    # The namespace collision requires an owner-created private member.
    private_store = provision_member_memory(synced, TEMPLATE)
    synced.save()
    assert synced.memory_stores[private_store].memory_version == 2
    assert read_private_session_store("dashboard:template-chat") is None
    return state, slot, private_store


@pytest.mark.asyncio
async def test_slot_create_cannot_overwrite_later_same_name_member(tmp_path, monkeypatch):
    """A delayed template create cannot replace an explicit member selection."""
    cfg = KiroCrewConfig.load()
    assert TEMPLATE not in cfg.agents
    cfg.save()
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: TEMPLATE if name == TEMPLATE else "",
    )
    monkeypatch.setattr(
        agents,
        "list_agents",
        lambda: [
            AgentInfo(
                name=TEMPLATE,
                filename=f"{TEMPLATE}.json",
                description="",
                model="auto",
                source="builtin",
            )
        ],
    )
    state = _turn_state(tmp_path, monkeypatch)
    state.sessions.reset = AsyncMock(return_value=True)
    monkeypatch.setattr("kiro_crew.llm_helpers._slot_switch_session_locks", {})
    key = "dashboard:create-owner-race"
    writer_started, release_writer, writer_finished = (threading.Event() for _ in range(3))
    later_lock_attempted = asyncio.Event()
    publications = {}
    get_or_create = state.get_or_create_slot

    class ObservedSlotLock(asyncio.Lock):
        async def acquire(self):
            if writer_started.is_set():
                later_lock_attempted.set()
            return await super().acquire()

    def create_with_observed_lock(*args, **kwargs):
        slot = get_or_create(*args, **kwargs)
        slot._lock = ObservedSlotLock()
        return slot

    def gated_publication(session_key, agent_name, bindings, **kwargs):
        assert session_key == key
        assert kwargs.get("replace") is True
        kind = bindings.selection_kind
        try:
            if kind == "template":
                writer_started.set()
                assert release_writer.wait(15), "Template create writer was not released"
            change = record_agent_selection(session_key, agent_name, bindings, **kwargs)
            assert change is not None
            publications[kind] = change[1]
            return change
        finally:
            if kind == "template":
                writer_finished.set()

    monkeypatch.setattr(state, "get_or_create_slot", create_with_observed_lock)
    monkeypatch.setattr(chat_handlers, "record_agent_selection", gated_publication)
    app = _make_app_with_agent_routes(state)
    app.router.add_post("/api/agents/sync", agents.api_kirocrew_agents_sync)
    async with TestClient(TestServer(as_owner(app))) as client:
        create_task = asyncio.create_task(
            client.post("/api/chat/slots", json={"name": "create-owner-race", "agent": TEMPLATE})
        )
        later_task = None
        try:
            assert await asyncio.wait_for(asyncio.to_thread(writer_started.wait, 10), 11)
            slot = state._slots["create-owner-race"]
            creation_agent, creation_project = slot.agent, slot.project
            create_holds_lock = slot._lock.locked()
            response = await asyncio.wait_for(client.post("/api/agents/sync", json={}), 10)
            assert response.status == 200, await response.text()
            assert TEMPLATE in (await response.json())["synced"]

            def opt_in():
                cfg = KiroCrewConfig.load()
                assert cfg.agents[TEMPLATE].memory_store == "default"
                store = provision_member_memory(cfg, TEMPLATE)
                cfg.save()
                return store

            private_store = await asyncio.to_thread(opt_in)

            later_task = asyncio.create_task(
                client.post(f"/api/chat/slots/{slot.key}/agent", json={"agent": TEMPLATE})
            )
            await asyncio.wait_for(later_lock_attempted.wait(), 10)
            if not create_holds_lock:
                # Prove the actual overwrite on the unlocked handler. A fixed
                # create may serialize B; do not require B to finish then.
                later_response = await asyncio.wait_for(asyncio.shield(later_task), 10)
                assert later_response.status == 200, await later_response.text()
                assert slot.agent == creation_agent
                assert slot.agent is not creation_agent
                assert slot.project == creation_project
                assert (
                    await asyncio.to_thread(session_agent_selection_kind, key, TEMPLATE) == "member"
                )
            release_writer.set()
            responses = await asyncio.wait_for(asyncio.gather(create_task, later_task), 15)
            assert [response.status for response in responses] == [200, 200]
            assert await asyncio.wait_for(asyncio.to_thread(writer_finished.wait, 10), 11)
            assert slot.agent == creation_agent == TEMPLATE
            assert slot.project == creation_project
            assert slot.memory_store == private_store

            def final_selection():
                return read_session_execution(key).to_record()

            selected = await asyncio.wait_for(asyncio.to_thread(final_selection), 10)
            assert selected == publications["member"], {
                "selected": selected,
                "publications": publications,
                "create_status": responses[0].status,
                "later_status": responses[1].status,
            }
        finally:
            release_writer.set()
            tasks = [task for task in (create_task, later_task) if task is not None]
            try:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 15)
            finally:
                if writer_started.is_set():
                    assert await asyncio.wait_for(asyncio.to_thread(writer_finished.wait, 10), 11)
                await asyncio.wait_for(drain_background_tasks(state), 10)


@pytest.mark.asyncio
async def test_slot_create_can_replace_existing_protected_history_selection(tmp_path, monkeypatch):
    state, slot, _ = await asyncio.wait_for(_template_chat(tmp_path, monkeypatch), 20)
    key = "dashboard:template-chat"
    later_agent = "kirocrew-worker"
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in (TEMPLATE, later_agent) else "",
    )
    assert await asyncio.to_thread(session_agent_selection_kind, key, TEMPLATE) == "template"
    state._slots.pop(slot.key)
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await asyncio.wait_for(
            client.post("/api/chat/slots", json={"name": slot.key, "agent": later_agent}), 10
        )
        assert response.status == 200, await response.text()
    assert state._slots[slot.key].agent == later_agent
    assert await asyncio.to_thread(session_agent_selection_kind, key, later_agent) == "template"
    assert await asyncio.to_thread(read_private_session_store, key) is None
    await asyncio.wait_for(drain_background_tasks(state), 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("restore", ["live", "open", "recent", "resume"])
async def test_discovery_keeps_existing_template_conversation(tmp_path, monkeypatch, restore):
    state, slot, private_store = await _template_chat(tmp_path, monkeypatch)
    if restore != "live":
        state = _turn_state(tmp_path, monkeypatch)
        if restore == "open":
            slot = _rehydrate_slot_from_history(state, "template-chat")
        elif restore == "recent":
            key = "dashboard:template-chat"
            _apply_recent_session(
                state,
                key,
                "template-chat",
                {},
                state.conversation_log.get_metadata(key),
                state.conversation_log._read_messages(key),
                conv_log=state.conversation_log,
                kiro_model_map={},
                restore_cfg=KiroCrewConfig.load(),
            )
            slot = state._slots["template-chat"]
        else:
            async with TestClient(
                TestServer(as_owner(_make_app_with_agent_routes(state)))
            ) as client:
                response = await client.post(
                    "/api/chat/slots/template-chat/resume",
                    json={"key": "dashboard:template-chat"},
                )
                assert response.status == 200, await response.text()
            slot = state._slots["template-chat"]
        assert slot is not None
    state.sessions.get_or_create.reset_mock()
    state.sessions.record_failure.reset_mock()
    state.context_builder.build_message.reset_mock()
    # Completion callbacks enter the same runner with the already selected slot.
    await asyncio.wait_for(
        chat_runner._run_chat(
            state,
            slot,
            "[Subagent completion event] The worker finished.",
            _directive_user_origin=False,
        ),
        10,
    )
    await asyncio.wait_for(drain_background_tasks(state), 10)
    assert state.sessions.get_or_create.await_count == 1
    state.sessions.record_failure.assert_not_awaited()
    assert (
        state.context_builder.build_message.call_args.kwargs["execution_context"].store.store_id
        == "default"
    )
    assert state.sessions.get_or_create.call_args.kwargs["agent"] == TEMPLATE
    assert read_private_session_store("dashboard:template-chat") is None

    async with TestClient(TestServer(_make_members_app(state))) as client:
        response = await client.post(f"/api/members/{TEMPLATE}/thread")
        assert response.status == 200, await response.text()
        member_slot = state._slots[(await response.json())["slot_key"]]
    assert member_slot.key != slot.key
    assert member_slot.memory_store == private_store
    assert read_private_session_store(f"dashboard:{member_slot.key}") == private_store


@pytest.mark.asyncio
async def test_live_provider_switch_preserves_selection_after_restore(tmp_path, monkeypatch):
    state, slot, _ = await asyncio.wait_for(_template_chat(tmp_path, monkeypatch), 20)
    key = "dashboard:template-chat"
    later_agent = "kirocrew-worker"
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in (TEMPLATE, later_agent) else "",
    )
    provider = state.sessions.get_or_create.return_value[0]
    state.sessions.reset = AsyncMock(return_value=True)

    async def switch(*args, **kwargs):
        yield LLMEvent(kind=EVENT_AGENT_SWITCHED, text=later_agent)
        yield LLMEvent(kind=EVENT_COMPLETE)

    provider.stream = switch
    await asyncio.wait_for(chat_runner._run_chat(state, slot, "switch the template"), 10)
    await asyncio.wait_for(drain_background_tasks(state), 10)
    assert slot.agent == later_agent
    assert await asyncio.to_thread(session_agent_selection_kind, key, later_agent) == "template"

    state = _turn_state(tmp_path, monkeypatch)
    slot = _rehydrate_slot_from_history(state, "template-chat")
    assert slot is not None
    assert slot.agent == later_agent
    await asyncio.wait_for(chat_runner._run_chat(state, slot, "continue"), 10)
    await asyncio.wait_for(drain_background_tasks(state), 10)
    assert state.sessions.get_or_create.call_args.kwargs["agent"] == later_agent
    state.sessions.record_failure.assert_not_awaited()
    assert await asyncio.to_thread(read_private_session_store, key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interruption", "cancel_count"),
    [
        ("cancel", 1),
        ("cancel", 2),
        ("rebound", 0),
        ("closed", 0),
        ("rollback", 1),
        ("rollback", 2),
    ],
)
async def test_interrupted_provider_switch_restores_selection(
    tmp_path, monkeypatch, interruption, cancel_count
):
    """A provider publication cannot outlive its turn or strand old history."""
    state, slot, _ = await asyncio.wait_for(_template_chat(tmp_path, monkeypatch), 20)
    key = "dashboard:template-chat"
    later_agent = "kirocrew-worker"
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in (TEMPLATE, later_agent) else "",
    )
    before = read_session_execution(key).to_record()
    provider = state.sessions.get_or_create.return_value[0]
    state.sessions.reset = AsyncMock(return_value=True)
    writer_started, release_writer, writer_finished = (threading.Event() for _ in range(3))
    rollback_started, release_rollback, rollback_finished = (threading.Event() for _ in range(3))
    retired_after_writer = []
    publish = chat_runner.record_provider_agent_switch
    restore = chat_runner.restore_agent_selection

    def gated_publication(*args):
        try:
            change = publish(*args)
            writer_started.set()
            assert release_writer.wait(15), "Provider publication was not released"
            return change
        finally:
            writer_finished.set()

    monkeypatch.setattr(chat_runner, "record_provider_agent_switch", gated_publication)

    def observed_restore(*args):
        try:
            rollback_started.set()
            if interruption == "rollback":
                assert release_rollback.wait(15), "Provider rollback was not released"
            return restore(*args)
        finally:
            rollback_finished.set()

    monkeypatch.setattr(chat_runner, "restore_agent_selection", observed_restore)

    async def switch(*args, **kwargs):
        yield LLMEvent(kind=EVENT_AGENT_SWITCHED, text=later_agent)
        yield LLMEvent(kind=EVENT_COMPLETE)

    provider.stream = switch

    async def turn():
        try:
            await chat_runner._run_chat(state, slot, "switch the template")
        finally:
            retired_after_writer.append((writer_finished.is_set(), rollback_finished.is_set()))

    task = asyncio.create_task(turn())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(writer_started.wait, 10), 11)
        assert not writer_finished.is_set()
        if interruption in ("rebound", "rollback"):
            slot.linked_session_key = "dashboard:other-session"
        elif interruption == "closed":
            assert state._slots.pop(slot.key) is slot
        if interruption == "rollback":
            release_writer.set()
            assert await asyncio.wait_for(asyncio.to_thread(rollback_started.wait, 10), 11)
            assert not rollback_finished.is_set()
        for _ in range(cancel_count):
            task.cancel()
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 10)
        release_writer.set()
        release_rollback.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15)
        assert await asyncio.wait_for(asyncio.to_thread(writer_finished.wait, 10), 11)
        assert read_session_execution(key).to_record() == before
        assert retired_after_writer == [(True, True)]
        assert slot.agent == TEMPLATE
        metadata = await asyncio.to_thread(state.conversation_log.get_metadata, key)
        assert metadata["agent"] == TEMPLATE

        # Real restored history must still allocate under the original template.
        restored = _turn_state(tmp_path, monkeypatch)
        reopened = _rehydrate_slot_from_history(restored, slot.key)
        assert reopened is not None
        await asyncio.wait_for(chat_runner._run_chat(restored, reopened, "continue"), 10)
        await asyncio.wait_for(drain_background_tasks(restored), 10)
        restored.sessions.record_failure.assert_not_awaited()
        assert restored.sessions.get_or_create.call_args.kwargs["agent"] == TEMPLATE
    finally:
        release_writer.set()
        release_rollback.set()
        try:
            if not task.done():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15)
        finally:
            if writer_started.is_set():
                assert await asyncio.wait_for(asyncio.to_thread(writer_finished.wait, 10), 11)
            if rollback_started.is_set():
                assert await asyncio.wait_for(asyncio.to_thread(rollback_finished.wait, 10), 11)
            await asyncio.wait_for(drain_background_tasks(state), 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("restore", ["open", "recent", "resume"])
async def test_restored_agent_conflict_cannot_replace_protected_selection(
    tmp_path, monkeypatch, restore
):
    state, _, _ = await asyncio.wait_for(_template_chat(tmp_path, monkeypatch), 20)
    key = "dashboard:template-chat"
    later_agent = "kirocrew-worker"
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in (TEMPLATE, later_agent) else "",
    )
    protected = read_session_execution(key).to_record()
    # A crash between the history write and owner-selection publication leaves
    # these two records disagreeing. History must not become replacement authority.
    await asyncio.to_thread(state.conversation_log.update_metadata, key, {"agent": later_agent})
    state = _turn_state(tmp_path, monkeypatch)
    if restore == "open":
        slot = _rehydrate_slot_from_history(state, "template-chat")
    elif restore == "recent":
        _apply_recent_session(
            state,
            key,
            "template-chat",
            {},
            state.conversation_log.get_metadata(key),
            state.conversation_log._read_messages(key),
            conv_log=state.conversation_log,
            kiro_model_map={},
            restore_cfg=KiroCrewConfig.load(),
        )
        slot = state._slots["template-chat"]
    else:
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            response = await asyncio.wait_for(
                client.post("/api/chat/slots/template-chat/resume", json={"key": key}), 10
            )
            assert response.status == 200, await response.text()
        slot = state._slots["template-chat"]
    assert slot is not None
    assert slot.agent == TEMPLATE
    await asyncio.wait_for(
        chat_runner._run_chat(
            state,
            slot,
            "[Subagent completion event] The worker finished.",
            _directive_user_origin=False,
        ),
        10,
    )
    await asyncio.wait_for(drain_background_tasks(state), 10)
    state.sessions.record_failure.assert_not_awaited()
    state.sessions.get_or_create.assert_awaited_once()
    assert state.sessions.get_or_create.call_args.kwargs["agent"] == TEMPLATE
    assert (
        state.context_builder.build_message.call_args.kwargs["execution_context"].store.store_id
        == "default"
    )
    assert read_private_session_store(key) is None
    assert read_session_execution(key).to_record() == protected
    assert not any(row["role"] == "error" for row in slot.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", [{}, {"member_id": "missing"}])
async def test_malformed_canonical_selection_refuses_before_provider_start(
    tmp_path, monkeypatch, damage
):
    state, slot, _ = await _template_chat(tmp_path, monkeypatch)
    key = "dashboard:template-chat"
    from kiro_crew.history import ConversationLog

    await asyncio.to_thread(ConversationLog().update_metadata, key, {"execution_context": damage})
    state.sessions.get_or_create.reset_mock()
    state.context_builder.build_message.reset_mock()
    await asyncio.wait_for(chat_runner._run_chat(state, slot, "continue"), 10)
    await asyncio.wait_for(drain_background_tasks(state), 10)
    state.sessions.get_or_create.assert_not_awaited()
    state.context_builder.build_message.assert_not_called()
    assert any(row["role"] == "error" for row in slot.messages)


@pytest.mark.asyncio
async def test_owner_reselection_cannot_promote_existing_template_context_to_member(
    tmp_path, monkeypatch
):
    state, slot, _ = await _template_chat(tmp_path, monkeypatch)
    key = "dashboard:template-chat"
    state.sessions.reset = AsyncMock(return_value=True)
    previous = read_session_execution(key)
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await client.post(
            "/api/chat/slots/template-chat/agent", json={"agent": TEMPLATE}
        )
        assert response.status == 503, await response.text()
    assert read_session_execution(key) == previous
    assert session_agent_selection_kind(key, TEMPLATE) == "template"
    assert read_private_session_store(key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("allocation", ["turn", "eager"])
async def test_discovery_before_first_allocation_preserves_owner_template_pick(
    tmp_path, monkeypatch, allocation
):
    state, slot, _ = await _template_chat(tmp_path, monkeypatch, first_turn=False)
    if allocation == "eager":
        monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)
        monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: 1)
        monkeypatch.setattr(chat_runner, "_armed_prefetches", {})
        await asyncio.wait_for(chat_runner._eager_spawn(state, slot), 10)
        assert state.sessions.get_or_create.call_args.kwargs["speculative"] is True
    else:
        await asyncio.wait_for(chat_runner._run_chat(state, slot, "Complete the task."), 10)
        assert (
            state.context_builder.build_message.call_args.kwargs["execution_context"].store.store_id
            == "default"
        )
    await asyncio.wait_for(drain_background_tasks(state), 10)
    state.sessions.get_or_create.assert_awaited_once()
    assert state.sessions.get_or_create.call_args.kwargs["agent"] == TEMPLATE
    assert read_private_session_store("dashboard:template-chat") is None


@pytest.mark.asyncio
async def test_late_template_resolution_cannot_replace_owner_member_pick(tmp_path, monkeypatch):
    await _template_chat(tmp_path, monkeypatch)
    key = "dashboard:template-chat"
    cfg = KiroCrewConfig.load()
    prior = resolve_session_agent_bindings(resolve_agent_bindings, cfg, key, TEMPLATE)
    selected = resolve_agent_bindings(cfg, TEMPLATE)
    await asyncio.to_thread(record_agent_selection, key, TEMPLATE, selected, replace=True)
    with pytest.raises(UnknownMemoryStore, match="changed during preparation"):
        await asyncio.to_thread(record_agent_selection, key, TEMPLATE, prior)
    assert session_agent_selection_kind(key, TEMPLATE) == "member"


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_pick", ["before_write", "after_write"])
async def test_prewarm_publication_retains_newer_owner_agent_pick(
    tmp_path, monkeypatch, owner_pick
):
    state, slot, _ = await _template_chat(tmp_path, monkeypatch, first_turn=False)
    later_agent = "kirocrew-worker"
    key = "dashboard:template-chat"
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in (TEMPLATE, later_agent) else "",
    )
    state.sessions.reset = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)
    monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: 1)
    monkeypatch.setattr(chat_runner, "_armed_prefetches", {})
    to_thread = asyncio.to_thread
    interleaved = False

    async def choose_later_agent():
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            response = await client.post(
                "/api/chat/slots/template-chat/agent", json={"agent": later_agent}
            )
            assert response.status == 200, await response.text()
        assert await to_thread(session_agent_selection_kind, key, later_agent) == "template"

    async def owner_pick_during_publication(func, *args, **kwargs):
        nonlocal interleaved
        if func is record_agent_selection and not kwargs.get("replace") and not interleaved:
            interleaved = True
            if owner_pick == "before_write":
                await choose_later_agent()
            change = await to_thread(func, *args, **kwargs)
            if owner_pick == "after_write":
                await choose_later_agent()
            return change
        return await to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", owner_pick_during_publication)
    await asyncio.wait_for(chat_runner._eager_spawn(state, slot), 10)
    await asyncio.wait_for(drain_background_tasks(state), 10)
    assert interleaved
    assert slot.agent == later_agent
    state.sessions.get_or_create.assert_not_awaited()
    assert await to_thread(session_agent_selection_kind, key, later_agent) == "template"


@pytest.mark.asyncio
async def test_cancelled_prewarm_cannot_overwrite_owner_pick(tmp_path, monkeypatch):
    state, slot, _ = await _template_chat(tmp_path, monkeypatch, first_turn=False)
    later_agent = "kirocrew-worker"
    key = "dashboard:template-chat"
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in (TEMPLATE, later_agent) else "",
    )
    cfg = KiroCrewConfig.load()
    cfg.session.eager_spawn = True
    cfg.save()
    state.sessions.reset = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)
    monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: 1)
    monkeypatch.setattr(chat_runner, "_armed_prefetches", {})
    started, release, finished = (threading.Event() for _ in range(3))

    def delayed_write(*args, **kwargs):
        started.set()
        try:
            assert release.wait(10), "Test did not release the automatic publication"
            return record_agent_selection(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(chat_runner, "record_agent_selection", delayed_write)
    task = asyncio.create_task(chat_runner._eager_spawn(state, slot))
    slot._eager_spawn_task = task
    try:
        assert await asyncio.to_thread(started.wait, 10)
        # Exercise the real replacement scheduler; only its successor's
        # provider work is outside this cancelled-publication assertion.
        monkeypatch.setattr(chat_runner, "_eager_spawn", AsyncMock())
        monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", chat_runner.schedule_eager_spawn)
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            response = await client.post(
                "/api/chat/slots/template-chat/agent", json={"agent": later_agent}
            )
            assert response.status == 200, await response.text()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert await asyncio.to_thread(finished.wait, 10)
        await asyncio.wait_for(slot._eager_spawn_task, 10)
        state.sessions.get_or_create.assert_not_awaited()
        assert await asyncio.to_thread(session_agent_selection_kind, key, later_agent) == "template"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15)
        await asyncio.wait_for(drain_background_tasks(state), 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_live_agent_change_requires_observed_selection_revision(
    tmp_path, monkeypatch, changed
):
    await _template_chat(tmp_path, monkeypatch, first_turn=False)
    key = "dashboard:template-chat"
    later_agent = "kirocrew-worker"
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in (TEMPLATE, later_agent) else "",
    )
    cfg = KiroCrewConfig.load()
    # A live event carries the previous selection's revision into publication;
    # resolving a different name from history cannot provide this observation.
    prior = resolve_session_agent_bindings(resolve_agent_bindings, cfg, key, TEMPLATE)
    binding = resolve_agent_bindings(cfg, later_agent, selection_kind="template")
    binding.selection_revision = prior.selection_revision
    if changed:
        # Even an identical owner pick supersedes the earlier observation.
        owner_binding = resolve_session_agent_bindings(resolve_agent_bindings, cfg, key, TEMPLATE)
        await asyncio.to_thread(record_agent_selection, key, TEMPLATE, owner_binding, replace=True)
        with pytest.raises(UnknownMemoryStore, match="changed during preparation"):
            await asyncio.to_thread(record_agent_selection, key, later_agent, binding)
        assert session_agent_selection_kind(key, TEMPLATE) == "template"
    else:
        await asyncio.to_thread(record_agent_selection, key, later_agent, binding)
        assert session_agent_selection_kind(key, later_agent) == "template"


@pytest.mark.asyncio
async def test_recorded_member_cannot_become_template_after_member_removal(tmp_path, monkeypatch):
    await _template_chat(tmp_path, monkeypatch)
    cfg = KiroCrewConfig.load()
    key = "dashboard:member-chat"
    binding = resolve_agent_bindings(cfg, TEMPLATE)
    await asyncio.to_thread(record_agent_selection, key, TEMPLATE, binding)
    del cfg.agents[TEMPLATE]
    # The template still exists in the real discovery catalog stub. Only the
    # member namespace disappeared, so the old selection is unresolved.
    resolved = await asyncio.to_thread(
        resolve_session_agent_bindings, resolve_agent_bindings, cfg, key, TEMPLATE
    )
    assert resolved.requested_resolved is False


@pytest.mark.asyncio
async def test_default_model_ignores_new_same_name_member_pin(tmp_path, monkeypatch):
    _, slot, _ = await _template_chat(tmp_path, monkeypatch)
    cfg = KiroCrewConfig.load()
    cfg.agents[TEMPLATE].model = "member-model-pin"
    monkeypatch.setattr(cfg, "_resolve_named_agent_model", lambda *a: "template-model-pin")
    assert await asyncio.to_thread(chat_runner._default_session_model, cfg, slot, "") == (
        "template-model-pin"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("restore", ["open", "recent", "resume"])
async def test_failed_agent_history_write_keeps_prior_selection(tmp_path, monkeypatch, restore):
    """A history lock failure cannot publish an agent that restart cannot use."""
    state, slot, _ = await asyncio.wait_for(_template_chat(tmp_path, monkeypatch), 20)
    key = "dashboard:template-chat"
    later_agent = "kirocrew-worker"
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in (TEMPLATE, later_agent) else "",
    )
    state.sessions.reset = AsyncMock(return_value=True)
    protected = read_session_execution(key).to_record()
    prior = (slot.agent, slot.workspace, slot.project, slot.memory_store)
    publish = MagicMock(wraps=record_agent_selection)
    monkeypatch.setattr(chat_handlers, "record_agent_selection", publish)
    monkeypatch.setattr(
        state.conversation_log, "_locked", MagicMock(side_effect=TimeoutError("history lock"))
    )
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await asyncio.wait_for(
            client.post("/api/chat/slots/template-chat/agent", json={"agent": later_agent}), 10
        )
        assert response.status == 503, await response.text()
    assert (slot.agent, slot.workspace, slot.project, slot.memory_store) == prior
    assert slot._dirty
    publish.assert_not_called()
    assert read_session_execution(key).to_record() == protected

    # A fresh dashboard and ConversationLog read the durable state, not the
    # rolled-back slot or its metadata cache.
    restored = _turn_state(tmp_path, monkeypatch)
    if restore == "open":
        slot = _rehydrate_slot_from_history(restored, "template-chat")
    elif restore == "recent":
        _apply_recent_session(
            restored,
            key,
            "template-chat",
            {},
            restored.conversation_log.get_metadata(key),
            restored.conversation_log._read_messages(key),
            conv_log=restored.conversation_log,
            kiro_model_map={},
            restore_cfg=KiroCrewConfig.load(),
        )
        slot = restored._slots["template-chat"]
    else:
        async with TestClient(
            TestServer(as_owner(_make_app_with_agent_routes(restored)))
        ) as client:
            response = await asyncio.wait_for(
                client.post("/api/chat/slots/template-chat/resume", json={"key": key}), 10
            )
            assert response.status == 200, await response.text()
        slot = restored._slots["template-chat"]
    assert slot is not None
    assert slot.agent == TEMPLATE
    await asyncio.wait_for(chat_runner._run_chat(restored, slot, "Continue the task."), 10)
    await asyncio.wait_for(drain_background_tasks(restored), 10)
    restored.sessions.record_failure.assert_not_awaited()
    restored.sessions.get_or_create.assert_awaited_once()
    assert not any(row["role"] == "error" for row in slot.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["lookup_error", "unresolved"])
async def test_failed_agent_lookup_preserves_existing_selection(tmp_path, monkeypatch, failure):
    state, slot, _ = await asyncio.wait_for(_template_chat(tmp_path, monkeypatch), 20)
    key = "dashboard:template-chat"
    protected = read_session_execution(key).to_record()
    metadata = state.conversation_log.get_metadata(key)
    prior = (slot.agent, slot.workspace, slot.project, slot.memory_store)
    state.sessions.reset = AsyncMock(return_value=True)
    with monkeypatch.context() as patch:
        if failure == "lookup_error":
            patch.setattr(
                chat_handlers,
                "resolve_agent_bindings",
                MagicMock(side_effect=OSError("agent lookup unavailable")),
            )
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            response = await asyncio.wait_for(
                client.post(
                    f"/api/chat/slots/{slot.key}/agent", json={"agent": "missing-template"}
                ),
                10,
            )
            assert response.status == 503, await response.text()
    state.sessions.reset.assert_not_awaited()
    assert (slot.agent, slot.workspace, slot.project, slot.memory_store) == prior
    assert read_session_execution(key).to_record() == protected
    assert state.conversation_log.get_metadata(key) == metadata
    state.sessions.get_or_create.reset_mock()
    await asyncio.wait_for(chat_runner._run_chat(state, slot, "Continue."), 10)
    await asyncio.wait_for(drain_background_tasks(state), 10)
    state.sessions.record_failure.assert_not_awaited()
    state.sessions.get_or_create.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_agent_lookup_preserves_existing_selection(tmp_path, monkeypatch):
    state, slot, _ = await asyncio.wait_for(_template_chat(tmp_path, monkeypatch), 20)
    key = "dashboard:template-chat"
    protected = read_session_execution(key).to_record()
    metadata = state.conversation_log.get_metadata(key)
    prior = (slot.agent, slot.workspace, slot.project, slot.memory_store)
    state.sessions.reset = AsyncMock(return_value=True)
    body = json.dumps({"agent": "kirocrew-worker"}).encode()
    payload = MagicMock()

    async def chunks(_size):
        yield body

    payload.iter_chunked = chunks
    request = make_mocked_request(
        "POST",
        f"/api/chat/slots/{slot.key}/agent",
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        match_info={"slot": slot.key},
        app=_make_app_with_agent_routes(state),
        payload=payload,
    )
    request["user"] = "local-app"
    request["app"] = ""
    with monkeypatch.context() as patch:
        patch.setattr(
            chat_handlers, "warm_project_agent_names", AsyncMock(side_effect=asyncio.CancelledError)
        )
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(chat_handlers.api_chat_slot_agent(request), 10)
    state.sessions.reset.assert_not_awaited()
    assert (slot.agent, slot.workspace, slot.project, slot.memory_store) == prior
    assert read_session_execution(key).to_record() == protected
    assert state.conversation_log.get_metadata(key) == metadata
    state.sessions.get_or_create.reset_mock()
    await asyncio.wait_for(chat_runner._run_chat(state, slot, "Continue."), 10)
    await asyncio.wait_for(drain_background_tasks(state), 10)
    state.sessions.record_failure.assert_not_awaited()
    state.sessions.get_or_create.assert_awaited_once()


@pytest.mark.asyncio
async def test_default_agent_switch_replaces_protected_selection(tmp_path, monkeypatch):
    state, slot, _ = await asyncio.wait_for(_template_chat(tmp_path, monkeypatch), 20)
    state.sessions.reset = AsyncMock(return_value=True)
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await asyncio.wait_for(
            client.post(f"/api/chat/slots/{slot.key}/agent", json={"agent": ""}), 10
        )
        assert response.status == 200, await response.text()
    state.sessions.get_or_create.reset_mock()
    await asyncio.wait_for(chat_runner._run_chat(state, slot, "Continue."), 10)
    await asyncio.wait_for(drain_background_tasks(state), 10)
    state.sessions.record_failure.assert_not_awaited()
    state.sessions.get_or_create.assert_awaited_once()
    restored = _turn_state(tmp_path, monkeypatch)
    restored_slot = _rehydrate_slot_from_history(restored, slot.key)
    assert restored_slot is not None
    await asyncio.wait_for(chat_runner._run_chat(restored, restored_slot, "Continue."), 10)
    await asyncio.wait_for(drain_background_tasks(restored), 10)
    restored.sessions.record_failure.assert_not_awaited()
    restored.sessions.get_or_create.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["template", "legacy_member", "default"])
async def test_non_owner_switch_keeps_durable_selection(tmp_path, monkeypatch, selection):
    state, slot, _ = await asyncio.wait_for(_template_chat(tmp_path, monkeypatch), 20)
    cfg = KiroCrewConfig.load()
    default_agent = cfg.default_agent
    cfg.agents["legacy-worker"] = replace(cfg.agents[default_agent])
    cfg.save()
    later_agent = {
        "template": "kirocrew-worker",
        "legacy_member": "legacy-worker",
        "default": "",
    }[selection]
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in (TEMPLATE, later_agent) else "",
    )
    state.sessions.reset = AsyncMock(return_value=True)
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await asyncio.wait_for(
            client.post(
                f"/api/chat/slots/{slot.key}/agent",
                json={"agent": later_agent},
                headers={"X-Test-User": "allowed-chat-user"},
            ),
            10,
        )
        assert response.status == 200, await response.text()
    state.sessions.get_or_create.reset_mock()
    await asyncio.wait_for(chat_runner._run_chat(state, slot, "Continue."), 10)
    await asyncio.wait_for(drain_background_tasks(state), 10)
    state.sessions.record_failure.assert_not_awaited()
    state.sessions.get_or_create.assert_awaited_once()
    restored = _turn_state(tmp_path, monkeypatch)
    restored_slot = _rehydrate_slot_from_history(restored, slot.key)
    assert restored_slot is not None and restored_slot.agent == (later_agent or default_agent)
    await asyncio.wait_for(chat_runner._run_chat(restored, restored_slot, "Continue."), 10)
    await asyncio.wait_for(drain_background_tasks(restored), 10)
    restored.sessions.record_failure.assert_not_awaited()
    restored.sessions.get_or_create.assert_awaited_once()
    assert not any(row["role"] == "error" for row in restored_slot.messages)
    if selection == "legacy_member":
        # A permitted V1 choice is not authority to admit private memory after
        # that member is upgraded. Exercise the live slot, not only a restore.
        cfg = KiroCrewConfig.load()
        provision_member_memory(cfg, later_agent)
        cfg.save()
        state.sessions.get_or_create.reset_mock()
        await asyncio.wait_for(chat_runner._run_chat(state, slot, "Continue."), 10)
        await asyncio.wait_for(drain_background_tasks(state), 10)
        state.sessions.get_or_create.assert_awaited_once()
        state.sessions.record_failure.assert_not_awaited()
        assert read_session_execution("dashboard:template-chat").store.store_id == "default"
        assert read_private_session_store("dashboard:template-chat") is None


@pytest.mark.asyncio
async def test_non_owner_member_upgraded_during_lookup_is_refused(tmp_path, monkeypatch):
    state, slot, _ = await asyncio.wait_for(_template_chat(tmp_path, monkeypatch), 20)
    cfg = KiroCrewConfig.load()
    cfg.agents["legacy-worker"] = replace(cfg.agents[cfg.default_agent])
    cfg.save()
    key = "dashboard:template-chat"
    protected = read_session_execution(key).to_record()
    metadata = state.conversation_log.get_metadata(key)
    prior = (slot.agent, slot.workspace, slot.project, slot.memory_store)
    state.sessions.reset = AsyncMock(return_value=True)
    calls = 0

    def resolve_then_upgrade(config, name, *args, **kwargs):
        nonlocal calls
        result = resolve_agent_bindings(config, name, *args, **kwargs)
        calls += 1
        if calls == 1:
            upgraded = KiroCrewConfig.load()
            provision_member_memory(upgraded, "legacy-worker")
            upgraded.save()
        return result

    monkeypatch.setattr(chat_handlers, "resolve_agent_bindings", resolve_then_upgrade)
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await asyncio.wait_for(
            client.post(
                f"/api/chat/slots/{slot.key}/agent",
                json={"agent": "legacy-worker"},
                headers={"X-Test-User": "allowed-chat-user"},
            ),
            10,
        )
        assert response.status == 403, await response.text()
    assert calls == 2
    state.sessions.reset.assert_not_awaited()
    assert (slot.agent, slot.workspace, slot.project, slot.memory_store) == prior
    assert read_session_execution(key).to_record() == protected
    assert state.conversation_log.get_metadata(key) == metadata


@pytest.mark.asyncio
async def test_non_owner_member_switch_is_refused_before_mutation(tmp_path, monkeypatch):
    state, slot, _ = await asyncio.wait_for(_template_chat(tmp_path, monkeypatch), 20)
    key = "dashboard:template-chat"
    protected = read_session_execution(key).to_record()
    metadata = state.conversation_log.get_metadata(key)
    prior = (slot.agent, slot.workspace, slot.project, slot.memory_store)
    state.sessions.reset = AsyncMock(return_value=True)
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await asyncio.wait_for(
            client.post(
                f"/api/chat/slots/{slot.key}/agent",
                json={"agent": TEMPLATE},
                headers={"X-Test-User": "allowed-chat-user"},
            ),
            10,
        )
        assert response.status == 403, await response.text()
    state.sessions.reset.assert_not_awaited()
    assert (slot.agent, slot.workspace, slot.project, slot.memory_store) == prior
    assert read_session_execution(key).to_record() == protected
    assert state.conversation_log.get_metadata(key) == metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2])
@pytest.mark.parametrize("phase", ["write", "rebound_restore"])
async def test_cancelled_agent_history_write_drains_before_restore(
    tmp_path, monkeypatch, cancel_count, phase
):
    state, slot, _ = await asyncio.wait_for(_template_chat(tmp_path, monkeypatch), 20)
    key = "dashboard:template-chat"
    later_agent = "kirocrew-worker"
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in (TEMPLATE, later_agent) else "",
    )
    state.sessions.reset = AsyncMock(return_value=True)
    protected = read_session_execution(key).to_record()
    started, release, finished = (threading.Event() for _ in range(3))
    restore_started, release_restore, restore_finished = (threading.Event() for _ in range(3))
    update_metadata = state.conversation_log.update_metadata
    writes = []

    def gated_write(history_key, fields):
        if fields.get("agent") == later_agent:
            try:
                started.set()
                assert release.wait(15), "Agent metadata writer was not released"
                update_metadata(history_key, fields)
                writes.append(later_agent)
            finally:
                finished.set()
        else:
            try:
                if phase == "rebound_restore":
                    restore_started.set()
                    assert release_restore.wait(15), "Agent metadata rollback was not released"
                update_metadata(history_key, fields)
                writes.append(fields.get("agent"))
            finally:
                restore_finished.set()

    monkeypatch.setattr(state.conversation_log, "update_metadata", gated_write)
    body = json.dumps({"agent": later_agent}).encode()
    payload = MagicMock()

    async def chunks(_size):
        yield body

    payload.iter_chunked = chunks
    request = make_mocked_request(
        "POST",
        f"/api/chat/slots/{slot.key}/agent",
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        match_info={"slot": slot.key},
        app=_make_app_with_agent_routes(state),
        payload=payload,
    )
    request["user"] = "local-app"
    request["app"] = ""
    task = asyncio.create_task(chat_handlers.api_chat_slot_agent(request))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 10), 11)
        if phase == "rebound_restore":
            slot.linked_session_key = "task:rebound"
            release.set()
            assert await asyncio.wait_for(asyncio.to_thread(restore_started.wait, 10), 11)
        for _ in range(cancel_count):
            assert task.cancel()
            # Cross the scheduled cancellation boundary with the writer held.
            delivered = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(delivered.set_result, None)
            await asyncio.wait_for(delivered, 10)
            assert not task.done()
            assert slot._lock.locked()
        release.set()
        release_restore.set()
        result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15)
        assert isinstance(result[0], asyncio.CancelledError)
        assert finished.is_set()
        assert restore_finished.is_set()
        assert writes == [later_agent, TEMPLATE]
        assert slot.agent == TEMPLATE
        assert read_session_execution(key).to_record() == protected
        restored = _turn_state(tmp_path, monkeypatch)
        restored_slot = _rehydrate_slot_from_history(restored, "template-chat")
        assert restored_slot is not None and restored_slot.agent == TEMPLATE
        await asyncio.wait_for(chat_runner._run_chat(restored, restored_slot, "Continue."), 10)
        await asyncio.wait_for(drain_background_tasks(restored), 10)
        restored.sessions.record_failure.assert_not_awaited()
        restored.sessions.get_or_create.assert_awaited_once()
    finally:
        release.set()
        release_restore.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15)
        if started.is_set():
            assert await asyncio.wait_for(asyncio.to_thread(finished.wait, 10), 11)
        if restore_started.is_set():
            assert await asyncio.wait_for(asyncio.to_thread(restore_finished.wait, 10), 11)
        await asyncio.wait_for(drain_background_tasks(state), 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("restore", ["open", "recent", "resume", "async"])
async def test_cancelled_agent_switch_with_failed_history_restore(tmp_path, monkeypatch, restore):
    """A failed rollback must not leave an empty conversation unresumable."""
    from kiro_crew.history import HistoryLockTimeout

    state, slot, _ = await asyncio.wait_for(
        _template_chat(tmp_path, monkeypatch, first_turn=False), 20
    )
    later_agent = "kirocrew-worker"
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in (TEMPLATE, later_agent) else "",
    )
    state.sessions.reset = AsyncMock(return_value=True)
    key = "dashboard:template-chat"
    protected = read_session_execution(key).to_record()
    started, release, finished = (threading.Event() for _ in range(3))
    update_metadata = state.conversation_log.update_metadata

    def fail_restore(history_key, fields):
        if fields.get("agent") != later_agent:
            raise HistoryLockTimeout("history rollback lock")
        try:
            started.set()
            assert release.wait(15), "Agent metadata writer was not released"
            update_metadata(history_key, fields)
        finally:
            finished.set()

    monkeypatch.setattr(state.conversation_log, "update_metadata", fail_restore)
    body = json.dumps({"agent": later_agent}).encode()
    payload = MagicMock()

    async def chunks(_size):
        yield body

    payload.iter_chunked = chunks
    request = make_mocked_request(
        "POST",
        f"/api/chat/slots/{slot.key}/agent",
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        match_info={"slot": slot.key},
        app=_make_app_with_agent_routes(state),
        payload=payload,
    )
    request["user"] = "local-app"
    request["app"] = ""
    task = asyncio.create_task(chat_handlers.api_chat_slot_agent(request))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 10), 11)
        assert task.cancel()
        delivered = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(delivered.set_result, None)
        await asyncio.wait_for(delivered, 10)
        release.set()
        result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15)
        assert isinstance(result[0], asyncio.CancelledError)
        assert finished.is_set()
        assert not slot.messages
        await asyncio.wait_for(drain_background_tasks(state), 10)

        restored = _turn_state(tmp_path, monkeypatch)
        if restore == "open":
            restored_slot = _rehydrate_slot_from_history(restored, slot.key)
        elif restore == "async":
            restored_slot = await asyncio.wait_for(
                rehydrate_slot_from_history_async(restored, slot.key), 10
            )
        elif restore == "recent":
            _apply_recent_session(
                restored,
                key,
                slot.key,
                {},
                restored.conversation_log.get_metadata(key),
                restored.conversation_log._read_messages(key),
                conv_log=restored.conversation_log,
                kiro_model_map={},
                restore_cfg=KiroCrewConfig.load(),
            )
            restored_slot = restored._slots[slot.key]
        else:
            async with TestClient(
                TestServer(as_owner(_make_app_with_agent_routes(restored)))
            ) as client:
                response = await asyncio.wait_for(
                    client.post(f"/api/chat/slots/{slot.key}/resume", json={"key": key}), 10
                )
                assert response.status == 200, await response.text()
            restored_slot = restored._slots[slot.key]
        assert restored_slot is not None
        assert restored_slot.agent == TEMPLATE
        assert read_session_execution(key).to_record() == protected
        await asyncio.wait_for(chat_runner._run_chat(restored, restored_slot, "Continue."), 10)
        await asyncio.wait_for(drain_background_tasks(restored), 10)
        restored.sessions.record_failure.assert_not_awaited()
        restored.sessions.get_or_create.assert_awaited_once()
        assert not any(row["role"] == "error" for row in restored_slot.messages)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15)
        if started.is_set():
            assert await asyncio.wait_for(asyncio.to_thread(finished.wait, 10), 11)
        await asyncio.wait_for(drain_background_tasks(state), 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("interleaving", ["rebound", "write_error", "later_pick"])
async def test_rebound_owner_switch_restores_selection_provenance(
    tmp_path, monkeypatch, interleaving
):
    state, slot, _ = await _template_chat(tmp_path, monkeypatch, first_turn=False)
    key = "dashboard:template-chat"
    state.sessions.reset = AsyncMock(return_value=True)
    to_thread = asyncio.to_thread

    async def rebind_after_selection(func, *args, **kwargs):
        if (
            func is record_agent_selection
            and kwargs.get("replace")
            and interleaving == "write_error"
        ):
            raise OSError("selection write unavailable")
        result = await to_thread(func, *args, **kwargs)
        if func is record_agent_selection and kwargs.get("replace"):
            if interleaving == "later_pick":
                await to_thread(func, *args, **kwargs)
            slot.linked_session_key = "task:rebound"
        return result

    monkeypatch.setattr(chat_handlers.asyncio, "to_thread", rebind_after_selection)
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await client.post(
            "/api/chat/slots/template-chat/agent", json={"agent": TEMPLATE}
        )
        assert response.status == (
            503 if interleaving == "write_error" else 409
        ), await response.text()
    assert session_agent_selection_kind(key, TEMPLATE) == (
        "member" if interleaving == "later_pick" else "template"
    )
    assert (read_private_session_store(key) is not None) is (interleaving == "later_pick")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2], ids=["one_cancel", "two_cancels"])
@pytest.mark.parametrize("later_choice", ["same_name_template", "different_name_template"])
async def test_cancelled_owner_writer_cannot_overwrite_later_choice(
    tmp_path, monkeypatch, later_choice, cancel_count
):
    """A cancelled owner still owns its running publication until it settles."""
    state, slot, _ = await asyncio.wait_for(
        _template_chat(tmp_path, monkeypatch, first_turn=False), 20
    )
    key = "dashboard:template-chat"
    later_agent = TEMPLATE if later_choice == "same_name_template" else "kirocrew-worker"
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in (TEMPLATE, later_agent) else "",
    )
    monkeypatch.setattr("kiro_crew.llm_helpers._slot_switch_session_locks", {})
    state.sessions.reset = AsyncMock(return_value=True)
    app = _make_app_with_agent_routes(state)
    loop_thread = threading.get_ident()
    writer_started, release_writer, writer_finished = (threading.Event() for _ in range(3))
    later_lock_attempted = asyncio.Event()
    publications = {}
    writer_threads = {}
    retired_after_drain = []
    later_task = None

    class ObservedSlotLock(asyncio.Lock):
        async def acquire(self):
            if asyncio.current_task() is later_task:
                # Observe the actual lock attempt, without requiring acquisition:
                # a corrected handler may still hold this lock while draining.
                later_lock_attempted.set()
            return await super().acquire()

    monkeypatch.setattr(slot, "_lock", ObservedSlotLock())

    def gated_publication(*args, **kwargs):
        assert kwargs.get("replace") is True
        owner = "later" if writer_started.is_set() else "cancelled"
        writer_threads[owner] = threading.get_ident()
        try:
            if owner == "cancelled":
                # The real to_thread worker has started, so cancelling its
                # asyncio waiter cannot stop this publication. Pause before the
                # real writer acquires its file lock, not inside the file lock.
                writer_started.set()
                assert release_writer.wait(15), "Cancelled owner writer was not released"
            change = record_agent_selection(*args, **kwargs)
            assert change is not None
            publications[owner] = change[1]
            return change
        finally:
            if owner == "cancelled":
                writer_finished.set()

    monkeypatch.setattr(chat_handlers, "record_agent_selection", gated_publication)

    def owner_request(agent):
        body = json.dumps({"agent": agent}).encode()
        payload = MagicMock()

        async def chunks(_size):
            yield body

        payload.iter_chunked = chunks
        request = make_mocked_request(
            "POST",
            f"/api/chat/slots/{slot.key}/agent",
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
            match_info={"slot": slot.key},
            app=app,
            payload=payload,
        )
        # The same authenticated bootstrap owner used by as_owner; retain the
        # real handler's authorization, JSON decoding, resolution and writes.
        request["user"] = "local-app"
        request["app"] = ""
        return request

    async def first_owner():
        try:
            return await chat_handlers.api_chat_slot_agent(owner_request(TEMPLATE))
        finally:
            retired_after_drain.append(writer_finished.is_set())

    old_task = asyncio.create_task(first_owner())

    async def cancel_blocked_owner():
        assert not release_writer.is_set()
        assert not writer_finished.is_set()
        assert old_task.cancel()
        # cancel() schedules the handler's wakeup before this callback. Cross
        # that loop boundary with the worker still gated, so the second cancel
        # is a separate delivery during the drain, not two coalesced requests.
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await asyncio.wait_for(checkpoint.wait(), 10)
        assert not writer_finished.is_set()

    try:
        assert await asyncio.wait_for(asyncio.to_thread(writer_started.wait, 10), 11)
        assert not writer_finished.is_set()
        assert not old_task.done()

        if later_choice == "same_name_template":
            # A already resolved the discovered member. Removing that member
            # makes B's explicit same-name choice resolve the available template.
            def remove_member():
                cfg = KiroCrewConfig.load()
                del cfg.agents[TEMPLATE]
                cfg.save()

            await asyncio.wait_for(asyncio.to_thread(remove_member), 10)

        await cancel_blocked_owner()
        later_task = asyncio.create_task(
            chat_handlers.api_chat_slot_agent(owner_request(later_agent))
        )
        await asyncio.wait_for(later_lock_attempted.wait(), 10)
        if cancel_count == 2:
            await cancel_blocked_owner()
        if old_task.done():
            # The broken handler abandoned its worker and released serialization.
            # Make the later real request commit before releasing that worker.
            # A handler retaining its lock while draining takes the other path;
            # it need not finish B while A's writer is deliberately paused.
            await asyncio.wait_for(asyncio.shield(later_task), 10)
        release_writer.set()
        results = await asyncio.wait_for(
            asyncio.gather(old_task, later_task, return_exceptions=True), 15
        )
        assert await asyncio.wait_for(asyncio.to_thread(writer_finished.wait, 10), 11)
        assert old_task.cancelled(), "The real owner handler must propagate cancellation"
        assert isinstance(results[0], asyncio.CancelledError)
        assert not isinstance(results[1], BaseException), results[1]
        assert results[1].status == 200, results[1].text
        assert set(writer_threads) == {"cancelled", "later"}
        assert all(thread != loop_thread for thread in writer_threads.values())
        assert publications["cancelled"]["selection_kind"] == "member"
        assert publications["later"]["selection_kind"] == "template"

        def final_identity():
            selection = read_session_execution(key).to_record()
            metadata = state.conversation_log.get_metadata(key)
            return {
                "protected_agent": selection["selection_name"],
                "protected_namespace": session_agent_selection_kind(key, later_agent),
                "protected_revision": selection["selection_revision"],
                "slot_agent": str(slot.agent),
                "history_agent": metadata["agent"],
            }

        observed = await asyncio.wait_for(asyncio.to_thread(final_identity), 10)
        observed["cancelled_handler_retired_after_writer"] = retired_after_drain
        assert observed == {
            "protected_agent": later_agent,
            "protected_namespace": "template",
            "protected_revision": publications["later"]["selection_revision"],
            "slot_agent": later_agent,
            "history_agent": later_agent,
            "cancelled_handler_retired_after_writer": [True],
        }
    finally:
        release_writer.set()
        tasks = [task for task in (old_task, later_task) if task is not None]
        try:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 15)
        finally:
            if writer_started.is_set():
                assert await asyncio.wait_for(asyncio.to_thread(writer_finished.wait, 10), 11)
            await asyncio.wait_for(drain_background_tasks(state), 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2], ids=["one_cancel", "two_cancels"])
@pytest.mark.parametrize("cleanup_phase", ["selection", "history"])
async def test_cancelled_rebound_cleanup_finishes_before_later_owner(
    tmp_path, monkeypatch, cleanup_phase, cancel_count
):
    """Neither rollback write may outlive the handler or skip the other write."""
    state, slot, _ = await asyncio.wait_for(
        _template_chat(tmp_path, monkeypatch, first_turn=False), 20
    )
    history_key = "dashboard:template-chat"
    rebound_key = "task:rebound-cleanup"
    later_agent = "kirocrew-worker"
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in (TEMPLATE, later_agent) else "",
    )
    monkeypatch.setattr("kiro_crew.llm_helpers._slot_switch_session_locks", {})
    state.sessions.reset = AsyncMock(return_value=True)
    app = _make_app_with_agent_routes(state)
    loop_thread = threading.get_ident()
    rebound = threading.Event()
    cleanup_started, release_cleanup = threading.Event(), threading.Event()
    selection_finished, history_finished = threading.Event(), threading.Event()
    later_lock_attempted = asyncio.Event()
    cleanup_threads = {}
    retired_after_cleanup = []
    cancel_results = []
    later_task = None
    record_owner = chat_handlers._record_explicit_agent_selection
    restore_selection = chat_handlers.restore_agent_selection
    update_metadata = state.conversation_log.update_metadata

    class ObservedSlotLock(asyncio.Lock):
        async def acquire(self):
            if asyncio.current_task() is later_task:
                later_lock_attempted.set()
            return await super().acquire()

    monkeypatch.setattr(slot, "_lock", ObservedSlotLock())

    async def rebind_after_publication(*args, **kwargs):
        change = await record_owner(*args, **kwargs)
        if not rebound.is_set():
            # Exercise the real post-publication rebound branch. Its history
            # rollback still names the dashboard transcript, while B selects
            # an agent for the newly linked session.
            slot.linked_session_key = rebound_key
            rebound.set()
        return change

    def pause_cleanup(phase):
        cleanup_threads[phase] = threading.get_ident()
        if cleanup_phase == phase:
            cleanup_started.set()
            assert release_cleanup.wait(15), "Rollback worker was not released"

    def restore_protected(key, change):
        if key != history_key or change is None:
            return restore_selection(key, change)
        try:
            pause_cleanup("selection")
            return restore_selection(key, change)
        finally:
            selection_finished.set()

    def restore_history(key, metadata):
        if key != history_key or not rebound.is_set() or metadata.get("agent") != TEMPLATE:
            return update_metadata(key, metadata)
        try:
            pause_cleanup("history")
            return update_metadata(key, metadata)
        finally:
            history_finished.set()

    monkeypatch.setattr(chat_handlers, "_record_explicit_agent_selection", rebind_after_publication)
    monkeypatch.setattr(chat_handlers, "restore_agent_selection", restore_protected)
    monkeypatch.setattr(state.conversation_log, "update_metadata", restore_history)

    def owner_request(agent):
        body = json.dumps({"agent": agent}).encode()
        payload = MagicMock()

        async def chunks(_size):
            yield body

        payload.iter_chunked = chunks
        request = make_mocked_request(
            "POST",
            f"/api/chat/slots/{slot.key}/agent",
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
            match_info={"slot": slot.key},
            app=app,
            payload=payload,
        )
        request["user"] = "local-app"
        request["app"] = ""
        return request

    async def first_owner():
        try:
            return await chat_handlers.api_chat_slot_agent(owner_request(TEMPLATE))
        finally:
            retired_after_cleanup.append(selection_finished.is_set() and history_finished.is_set())

    old_task = asyncio.create_task(first_owner())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(cleanup_started.wait, 10), 11)
        assert rebound.is_set()
        assert not old_task.done()
        for index in range(cancel_count):
            assert not release_cleanup.is_set()
            cancel_results.append(old_task.cancel())
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 10)
            if index == 0:
                later_task = asyncio.create_task(
                    chat_handlers.api_chat_slot_agent(owner_request(later_agent))
                )
                await asyncio.wait_for(later_lock_attempted.wait(), 10)
        assert cancel_results[0]
        if old_task.done():
            # The unfixed path releases the locks with cleanup still blocked.
            # Force the later metadata to land before that abandoned write.
            # A correct drain retains the locks; never wait for B here then.
            await asyncio.wait_for(asyncio.shield(later_task), 10)
        release_cleanup.set()
        results = await asyncio.wait_for(
            asyncio.gather(old_task, later_task, return_exceptions=True), 15
        )
        gated_finished = selection_finished if cleanup_phase == "selection" else history_finished
        assert await asyncio.wait_for(asyncio.to_thread(gated_finished.wait, 10), 11)
        assert old_task.cancelled()
        assert isinstance(results[0], asyncio.CancelledError)
        assert not isinstance(results[1], BaseException), results[1]
        assert results[1].status == 200, results[1].text
        assert all(thread != loop_thread for thread in cleanup_threads.values())

        def final_identity():
            metadata = state.conversation_log.get_metadata(history_key)
            return {
                "original_namespace": session_agent_selection_kind(history_key, TEMPLATE),
                "later_namespace": session_agent_selection_kind(rebound_key, later_agent),
                "slot_agent": str(slot.agent),
                "history_agent": metadata["agent"],
            }

        observed = await asyncio.wait_for(asyncio.to_thread(final_identity), 10)
        observed.update(
            protected_rollback_finished=selection_finished.is_set(),
            history_rollback_finished=history_finished.is_set(),
            handler_retired_after_full_cleanup=retired_after_cleanup,
            cancellations_accepted=cancel_results,
        )
        assert observed == {
            "original_namespace": "template",
            "later_namespace": "template",
            "slot_agent": later_agent,
            "history_agent": later_agent,
            "protected_rollback_finished": True,
            "history_rollback_finished": True,
            "handler_retired_after_full_cleanup": [True],
            "cancellations_accepted": [True] * cancel_count,
        }
    finally:
        release_cleanup.set()
        tasks = [task for task in (old_task, later_task) if task is not None]
        try:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 15)
        finally:
            if cleanup_started.is_set():
                gated_finished = (
                    selection_finished if cleanup_phase == "selection" else history_finished
                )
                assert await asyncio.wait_for(asyncio.to_thread(gated_finished.wait, 10), 11)
            await asyncio.wait_for(drain_background_tasks(state), 10)


@pytest.mark.asyncio
async def test_failed_empty_chat_private_grant_restores_protected_selection(tmp_path, monkeypatch):
    """A failed first private grant must not commit the same-name member choice."""
    state, slot, private_store = await asyncio.wait_for(
        _template_chat(tmp_path, monkeypatch, first_turn=False), 20
    )
    key = "dashboard:template-chat"
    protected = read_session_execution(key).to_record()
    prior = (slot.agent, slot.memory_store, slot._memory_assignment_from_history)
    state.sessions.reset = AsyncMock(return_value=True)

    async def fail_grant(_state, session_key, agent_name, cfg):
        assert session_key == key
        assert agent_name == TEMPLATE
        assert cfg.agents[agent_name].memory_store == private_store
        assert session_agent_selection_kind(key, TEMPLATE) == "member"
        raise OSError("private grant unavailable")

    pin = AsyncMock(side_effect=fail_grant)
    monkeypatch.setattr(chat_handlers, "pin_private_agent_store", pin)
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await asyncio.wait_for(
            client.post(f"/api/chat/slots/{slot.key}/agent", json={"agent": TEMPLATE}), 10
        )
        assert response.status == 503, await response.text()

    pin.assert_awaited_once()
    assert (slot.agent, slot.memory_store, slot._memory_assignment_from_history) == prior
    assert read_session_execution(key).to_record() == protected
    assert session_agent_selection_kind(key, TEMPLATE) == "template"
    assert state.conversation_log.get_metadata(key)["agent"] == TEMPLATE
    assert read_private_session_store(key) is None


def _prewarmed_member_state(tmp_path, monkeypatch, *, sid: str | None):
    """An empty new chat whose pre-warm left ``sid`` behind, plus a member.

    The eager pre-warm allocates while the chat is still on the default agent,
    and the switch handler's own reset preserves the persistence entry, so what
    survives into the pin is a resume pointer with no live provider.
    """
    cfg = KiroCrewConfig.load()
    cfg.agents["reviewer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    store = provision_member_memory(cfg, "reviewer")
    persist_member_config(cfg, "reviewer", create=True)
    state = _make_state(tmp_path)
    state.sessions.reset = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *a, **kw: None)
    monkeypatch.setattr(chat_runner, "_maybe_auto_title", AsyncMock())
    holder = {"sid": sid}
    state.sessions.resumable_sid = MagicMock(side_effect=lambda _key: holder["sid"])

    def _forget(_key):
        dropped, holder["sid"] = holder["sid"], None
        return dropped

    state.sessions.forget_conversation = MagicMock(side_effect=_forget)
    return state, store


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", [None, "acp-prewarm-sid"])
async def test_new_chat_member_pick_binds_private_memory_over_a_prewarm(tmp_path, monkeypatch, sid):
    """ "+ New conversation" then picking a member must bind its private store.

    Regression test. A pre-warm's surviving resume pointer is not V1 context:
    the chat has never been sent a message, so the pick is granted and the turn
    runs on the member's private store instead of reporting no verified private
    assignment while the member's name is displayed. Both parametrizations
    assert the same outcome, so the pre-warm is what varies and the binding is
    what does not.
    """
    state, store = await asyncio.to_thread(_prewarmed_member_state, tmp_path, monkeypatch, sid=sid)
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await asyncio.wait_for(client.post("/api/chat/slots", json={"name": "new"}), 10)
        assert response.status == 200, await response.text()
        slot = state._slots["new"]
        key = f"dashboard:{slot.key}"
        assert await asyncio.to_thread(read_private_session_store, key) is None
        response = await asyncio.wait_for(
            client.post(f"/api/chat/slots/{slot.key}/agent", json={"agent": "reviewer"}), 10
        )
        assert response.status == 200, await response.text()
    assert slot.agent == "reviewer"
    assert slot.memory_store == store
    assert await asyncio.to_thread(read_private_session_store, key) == store
    # Nothing can resume the default agent's pre-warmed process into the
    # member's private store: the pointer is dropped, not merely ignored.
    assert state.sessions.resumable_sid(key) is None
    assert state.sessions.forget_conversation.called is bool(sid)


@pytest.mark.asyncio
async def test_new_chat_v1_pick_keeps_its_prewarmed_session(tmp_path, monkeypatch):
    """A V1 pick has nothing to grant and must keep its resumable session."""
    state, _ = await asyncio.to_thread(
        _prewarmed_member_state, tmp_path, monkeypatch, sid="acp-prewarm-sid"
    )
    cfg = KiroCrewConfig.load()
    cfg.agents["plain"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    cfg.save()
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await asyncio.wait_for(client.post("/api/chat/slots", json={"name": "v1"}), 10)
        assert response.status == 200, await response.text()
        slot = state._slots["v1"]
        response = await asyncio.wait_for(
            client.post(f"/api/chat/slots/{slot.key}/agent", json={"agent": "plain"}), 10
        )
        assert response.status == 200, await response.text()
    state.sessions.forget_conversation.assert_not_called()
    assert state.sessions.resumable_sid(f"dashboard:{slot.key}") == "acp-prewarm-sid"


@pytest.mark.asyncio
@pytest.mark.parametrize("obstacle", ["transcript_rows", "unreadable_transcript", "live_provider"])
async def test_prewarmed_session_is_kept_when_the_chat_is_not_provably_empty(
    tmp_path, monkeypatch, obstacle
):
    """Only a provably empty chat may lose its pre-warm; each check fails closed."""
    from kiro_crew.dashboard.chat_persistence import release_prewarmed_session

    state, _ = await asyncio.to_thread(
        _prewarmed_member_state, tmp_path, monkeypatch, sid="acp-prewarm-sid"
    )
    key = "dashboard:not-empty"
    if obstacle == "transcript_rows":
        await asyncio.to_thread(state.conversation_log.append, key, "user", "earlier V1 turn")
    elif obstacle == "unreadable_transcript":
        state.conversation_log.has_messages = MagicMock(side_effect=OSError("unreadable"))
    else:
        state.sessions.get_provider = MagicMock(return_value=MagicMock())
    released = await asyncio.wait_for(
        release_prewarmed_session(state, key, "reviewer", KiroCrewConfig.load()), 10
    )
    assert released is False
    state.sessions.forget_conversation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_restricted_member_selection_and_turn_never_persist_transcript(
    tmp_path, monkeypatch, mode
):
    from kiro_crew.history import ConversationLog

    cfg = KiroCrewConfig.load()
    cfg.agents["writer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    member_store = provision_member_memory(cfg, "writer")
    cfg.save()
    state = _turn_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("restricted", agent="default", memory_mode=mode)
    state.sessions.reset = AsyncMock(return_value=True)
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await client.post("/api/chat/slots/restricted/agent", json={"agent": "writer"})
        assert response.status == 200, await response.text()
    key = "dashboard:restricted"
    captured = read_session_execution(key)
    assert captured.memory_mode == mode
    assert captured.store.store_id == member_store
    slot.append("user", "restricted body sentinel")
    await asyncio.wait_for(chat_runner._run_chat(state, slot, "restricted body sentinel"), 10)
    await asyncio.wait_for(drain_background_tasks(state), 10)
    state.sessions.get_or_create.assert_awaited_once()
    assert not state.conversation_log.has_log(key)
    assert not ConversationLog().has_log(key)
    if mode == "temporary":
        state.context_builder.ensure_store.assert_not_awaited()
