"""Private member identity survives scheduling, retries and process restarts."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.cron import CronJob, CronService, resolve_cron_memory
from kiro_crew.history import ConversationLog
from kiro_crew.member_memory_auth import bind_private_session_store
from kiro_crew.memory_stores import UnknownMemoryStore, provision_member_memory
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_persistence import (
    create_agent_folder,
    read_run_memory_store,
)

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.mark.asyncio
async def test_task_continuations_keep_protected_member_after_restart(member_stores):
    from kiro_crew.context import inherit_session_memory, store_of_session
    from kiro_crew.member_memory_auth import read_private_session_store

    writer, reviewer = member_stores
    origin = "dashboard:task-owner"
    runtime = "task:durable-private:runtime"
    log = ConversationLog()
    bind_private_session_store(origin, writer)
    await asyncio.to_thread(log.update_metadata, origin, {"memory_store": writer})
    builder = SimpleNamespace(conversation_log=log, ensure_store=AsyncMock(return_value=object()))
    assert await inherit_session_memory(builder, origin, runtime) == writer

    # Restart with only durable records; a change to the origin cannot retarget
    # the task's fixed runtime or any later planner/reviewer/history session.
    await asyncio.to_thread(log.update_metadata, origin, {"memory_store": reviewer})
    builder.conversation_log = ConversationLog()
    for child in (
        "task:durable-private:decompose",
        "task:durable-private:task1",
        "task:durable-private:review",
        "taskrunner:run:durable-private",
    ):
        assert await inherit_session_memory(builder, runtime, child) == writer
        assert read_private_session_store(child) == writer
        assert store_of_session(ConversationLog(), child) == writer


@pytest.mark.asyncio
async def test_task_continuation_refuses_tampered_parent_before_child_binding(member_stores):
    from kiro_crew.context import inherit_session_memory
    from kiro_crew.member_memory_auth import read_private_session_store

    writer, reviewer = member_stores
    parent, child = "task:tampered:runtime", "task:tampered:task1"
    bind_private_session_store(parent, writer)
    log = ConversationLog()
    await asyncio.to_thread(log.update_metadata, parent, {"execution_context": {"broken": True}})
    builder = SimpleNamespace(conversation_log=log, ensure_store=AsyncMock())
    with pytest.raises(UnknownMemoryStore, match="malformed execution context"):
        await inherit_session_memory(builder, parent, child)
    assert read_private_session_store(child) is None
    builder.ensure_store.assert_not_called()


@pytest.mark.asyncio
async def test_unbound_legacy_task_continuation_keeps_global_memory():
    from kiro_crew.context import inherit_session_memory
    from kiro_crew.member_memory_auth import read_private_session_store

    builder = SimpleNamespace(conversation_log=ConversationLog(), ensure_store=AsyncMock())
    child = "task:legacy:task1"
    assert await inherit_session_memory(builder, "task:legacy:runtime", child) == ""
    assert read_private_session_store(child) is None
    builder.ensure_store.assert_not_called()


@pytest.mark.asyncio
async def test_private_task_failure_lesson_never_uses_global_provider_or_store(member_stores):
    from kiro_crew.context import store_of_session
    from kiro_crew.task_models import Project, Task
    from kiro_crew.taskrunner import TaskRunner

    writer, _ = member_stores
    runtime = "taskrunner:failed-private:runtime"
    bind_private_session_store(runtime, writer)
    log = ConversationLog()
    await asyncio.to_thread(log.update_metadata, runtime, {"memory_store": writer})
    private_vectors, global_vectors, global_lessons = MagicMock(), MagicMock(), MagicMock()
    builder = SimpleNamespace(
        conversation_log=log, ensure_store=AsyncMock(return_value=private_vectors)
    )
    runner = TaskRunner(
        sessions=MagicMock(),
        context_builder=builder,
        lesson_store=global_lessons,
        consolidator=SimpleNamespace(_vector_store=global_vectors),
    )
    from kiro_crew.execution_context import read_session_execution

    run = Project(
        spec_path="spec.md",
        spec_content="private task",
        task_id="failed-private",
        execution_context=read_session_execution(runtime),
    )
    history_key = await runner._bound_history_key(run, "taskrunner:run:spec")
    assert history_key == "taskrunner:run:failed-private"
    assert store_of_session(ConversationLog(), history_key) == writer
    task = Task(index=1, title="private task", description="work", error="private error")
    with (
        patch.object(
            runner, "_call_llm_for_lesson", AsyncMock(return_value={"rule": "check inputs"})
        ) as llm,
        patch.object(runner, "_notify", AsyncMock()),
    ):
        await runner._extract_lesson(task, run)
    assert llm.await_args.kwargs == {"runtime_key": runtime}
    private_vectors.write_lesson.assert_called_once_with(
        "check inputs", "tool", None, "task_runner"
    )
    global_vectors.write_lesson.assert_not_called()
    global_lessons.save.assert_not_called()
    assert run.lessons_learned == ["check inputs"]


@pytest.mark.asyncio
async def test_private_task_review_refuses_corruption_before_provider(member_stores):
    from kiro_crew.task_executor import self_review
    from kiro_crew.task_models import Project, Task

    writer, reviewer = member_stores
    runtime = "taskrunner:broken-review:runtime"
    bind_private_session_store(runtime, writer)
    log = ConversationLog()
    await asyncio.to_thread(log.update_metadata, runtime, {"execution_context": {"broken": True}})
    sessions = MagicMock(open_task_session=AsyncMock())
    run = Project(spec_path="spec.md", spec_content="task", task_id="broken-review")
    builder = SimpleNamespace(conversation_log=log, ensure_store=AsyncMock())
    with pytest.raises(UnknownMemoryStore, match="malformed execution context"):
        await self_review(
            run, Task(index=1, title="task", description="work"), sessions, "kirocrew", ctx=builder
        )
    sessions.open_task_session.assert_not_called()
    builder.ensure_store.assert_not_called()


@pytest.mark.asyncio
async def test_registered_private_hook_prepares_its_member_on_later_delivery(member_stores):
    from dataclasses import replace

    from kiro_crew.context import store_of_session
    from kiro_crew.dashboard.handlers.hooks import _load_hook_execution, _run_hook_inner
    from kiro_crew.execution_context import (
        bind_session_execution,
        clear_session_execution,
        read_session_execution,
    )
    from kiro_crew.mcp_caller import CallerContext
    from kiro_crew.mcp_tools import control

    writer, _ = member_stores
    origin, hook_key = "dashboard:hook-owner", f"hook:{writer}:private-report"
    bind_private_session_store(origin, writer)
    captured = replace(
        read_session_execution(origin, required=True),
        template_id="captured-template",
        app="synthetic-app",
    )
    bind_session_execution(origin, captured, replace_existing=True)

    await asyncio.to_thread(ConversationLog().update_metadata, origin, {"memory_store": writer})
    caller = CallerContext(session_key=origin, from_gateway=True)
    with (
        patch.object(control.mcp_core, "_resolve_session_key_strict", return_value=origin),
        patch("kiro_crew.mcp_caller.current_caller", return_value=caller),
        patch.object(control.mcp_core, "_api_base", return_value="http://127.0.0.1:7788"),
        patch.object(control.mcp_core, "sel", return_value=MagicMock()),
    ):
        result = await asyncio.to_thread(
            control.register_hook,
            "register_hook",
            {"hook_id": "private-report", "context_summary": "report"},
        )
    assert result.startswith("Hook registered:")
    log = ConversationLog()
    admitted = read_session_execution(origin, required=True)
    assert _load_hook_execution(hook_key) == admitted
    assert not log._path(hook_key).exists()
    log.delete_session(origin)
    clear_session_execution(origin)
    config = KiroCrewConfig.load()
    config.agents["writer"].kiro_agent = "later-template"
    config.save()
    from kiro_crew.member_memory_auth import read_private_session_store

    assert read_private_session_store("hook:private-report") is None
    builder = SimpleNamespace(conversation_log=log, ensure_store=AsyncMock(return_value=object()))
    sessions = SimpleNamespace(
        get_or_create=AsyncMock(side_effect=RuntimeError("provider reached"))
    )
    with pytest.raises(RuntimeError, match="provider reached"):
        await _run_hook_inner(
            SimpleNamespace(context_builder=builder, sessions=sessions), hook_key, "go", None
        )
    builder.ensure_store.assert_awaited_once_with(writer)
    sessions.get_or_create.assert_awaited_once_with(hook_key, agent=admitted.template_id)
    assert store_of_session(log, hook_key) == writer
    assert read_session_execution(hook_key) == admitted


@pytest.mark.parametrize("mode", ["incognito", "temporary"])
@pytest.mark.parametrize("member", [True, False])
def test_restricted_hook_registration_never_persists_summary(member_stores, mode, member):
    from kiro_crew.execution_context import bind_session_execution, execution_for_store
    from kiro_crew.mcp_tools import control

    writer, _ = member_stores
    key = "dashboard:restricted-hook"
    bind_session_execution(key, execution_for_store(writer if member else "", memory_mode=mode))
    path = control.mcp_core.config_dir() / "hooks.json"
    before = path.read_bytes() if path.exists() else None
    with patch.object(control.mcp_core, "_resolve_session_key_strict", return_value=key):
        result = control.register_hook(
            "register_hook", {"hook_id": "restricted", "context_summary": "DO NOT PERSIST"}
        )
    assert result.startswith("Error: hook registration is disabled")
    assert (path.read_bytes() if path.exists() else None) == before
    assert not (path.parent / "hooks.json.lock").exists()
    assert not ConversationLog()._path(f"hook:{writer}:restricted").exists()


@pytest.mark.parametrize("restore_path", ["open", "recent", "channel"])
@pytest.mark.parametrize("recorded_agent", ["writer", ""])
@pytest.mark.parametrize("protected", [False, True])
def test_history_fields_cannot_grant_private_assignment(
    tmp_path, member_stores, restore_path, recorded_agent, protected
):
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.channel_slots import surface_channel_session
    from kiro_crew.dashboard.chat_persistence import (
        _apply_recent_session,
        _rehydrate_slot_from_history,
    )
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.member_memory_auth import read_private_session_store

    writer, _ = member_stores
    cfg = KiroCrewConfig.load()
    cfg.default_agent = "writer"
    cfg.save()
    state = _make_state(tmp_path)
    key = "slack:1234567890.123456" if restore_path == "channel" else "dashboard:restored"
    meta = {"agent": recorded_agent, "memory_store": writer}
    messages = [{"role": "user", "content": "ordinary history"}]
    state.conversation_log.append(key, "user", "ordinary history")
    state.conversation_log.update_metadata(key, meta)
    if protected:
        bind_private_session_store(key, writer)
    if restore_path == "open":
        slot = _rehydrate_slot_from_history(state, "restored")
    elif restore_path == "recent":
        _apply_recent_session(
            state,
            key,
            "restored",
            {},
            meta,
            messages,
            conv_log=state.conversation_log,
            kiro_model_map={},
            restore_cfg=cfg,
        )
        slot = state._slots["restored"]
    else:
        slot = surface_channel_session(state, {"key": key}, meta, messages, session_key=key)
    assert slot is not None
    assert effective_session_key(slot) == key
    if protected:
        read_session_execution(key, required=True)
        assert read_private_session_store(key) == writer
    else:
        with pytest.raises(
            UnknownMemoryStore,
            match="canonical execution identity|canonical member identity|missing or malformed execution context",
        ):
            read_session_execution(key, required=True)
        assert read_private_session_store(key) is None


@pytest.mark.asyncio
async def test_http_resume_cannot_authorize_private_transcript(tmp_path, member_stores):
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app_with_agent_routes, _make_state
    from dashboard_owner_helpers import as_owner

    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.member_memory_auth import read_private_session_store

    writer, _ = member_stores
    state = _make_state(tmp_path)
    key = "dashboard:resume-private"
    state.conversation_log.append(key, "user", "ordinary V1 history")
    state.conversation_log.update_metadata(key, {"agent": "writer"})
    with patch("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn"):
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            response = await client.post("/api/chat/slots/resume-private/resume", json={"key": key})
            assert response.status == 200, await response.text()
    with pytest.raises(
        UnknownMemoryStore,
        match="canonical execution identity|canonical member identity|missing or malformed execution context",
    ):
        read_session_execution(key, required=True)
    assert read_private_session_store(key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_owner_create_pins_private_selection_before_history_save(
    tmp_path, member_stores, explicit
):
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app_with_agent_routes, _make_state
    from dashboard_owner_helpers import as_owner

    from kiro_crew.member_memory_auth import read_private_session_store

    writer, _ = member_stores
    cfg = KiroCrewConfig.load()
    cfg.default_agent = "writer"
    cfg.save()
    state = _make_state(tmp_path)
    payload = {"name": "owner-private"}
    if explicit:
        payload["agent"] = "writer"
    with patch("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn"):
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            response = await client.post("/api/chat/slots", json=payload)
            assert response.status == 200, await response.text()
    assert state._slots["owner-private"].memory_store == writer
    assert read_private_session_store("dashboard:owner-private") == writer


@pytest.mark.asyncio
async def test_owner_direct_first_send_pins_private_memory_before_user_history(
    tmp_path, member_stores, monkeypatch
):
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app, _make_state, drain_background_tasks

    from kiro_crew.dashboard import chat_handlers, chat_persistence
    from kiro_crew.member_memory_auth import read_private_session_store

    writer, _ = member_stores
    cfg = KiroCrewConfig.load()
    cfg.default_agent = "writer"
    cfg.save()
    state = _make_state(tmp_path)
    original_pin = chat_persistence._pin_private_agent_assignment
    pin_observations = []

    def observed_pin(key, agent, config, **kwargs):
        pin_observations.append(state.conversation_log.has_log(key))
        return original_pin(key, agent, config, **kwargs)

    run = AsyncMock()
    monkeypatch.setattr(chat_persistence, "_pin_private_agent_assignment", observed_pin)
    monkeypatch.setattr(chat_handlers, "_run_chat", run)
    monkeypatch.setattr(chat_handlers, "_maybe_auto_title", AsyncMock())
    monkeypatch.setattr(chat_handlers, "maybe_auto_tag", AsyncMock())
    async with TestClient(TestServer(_make_app(state))) as client:
        response = await client.post(
            "/api/chat?ws=1", json={"slot": "owner-direct", "message": "Remember this task"}
        )
        assert response.status == 200, await response.text()
        await asyncio.wait_for(drain_background_tasks(state), timeout=5)
    assert pin_observations == [False]
    assert state._slots["owner-direct"].memory_store == writer
    assert read_private_session_store("dashboard:owner-direct") == writer
    run.assert_awaited_once()


@pytest.mark.asyncio
async def test_roster_does_not_present_missing_private_declarations_as_v1(tmp_path, member_stores):
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_state
    from test_members_dm_thread import _make_members_app

    writer, reviewer = member_stores
    cfg = KiroCrewConfig.load()
    cfg.agents["writer"].memory_store = "default"
    del cfg.memory_stores[reviewer]
    cfg.agents["legacy"] = KiroCrewAgentConfig()
    cfg.save()
    async with TestClient(TestServer(_make_members_app(_make_state(tmp_path)))) as client:
        response = await client.get("/api/members")
        assert response.status == 200, await response.text()
        rows = {row["name"]: row for row in (await response.json())["members"]}
    assert rows["writer"]["memory_version"] is None
    assert rows["reviewer"]["memory_version"] is None
    assert rows["legacy"]["memory_version"] == 1
    assert cfg.memory_stores[writer].owner_member == "writer"


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [False, True])
async def test_agent_pick_cannot_promote_an_existing_v1_transcript(tmp_path, member_stores, owner):
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app_with_agent_routes, _make_state
    from dashboard_owner_helpers import as_owner

    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.member_memory_auth import read_private_session_store

    writer, _ = member_stores
    state = _make_state(tmp_path)
    state.sessions.get_provider.return_value = None
    state.sessions.reset = AsyncMock(return_value=True)
    slot = state.get_or_create_slot("owner-pick", agent="default")
    slot._memory_assignment_from_history = True
    await asyncio.to_thread(
        state.conversation_log.append, "dashboard:owner-pick", "assistant", "V1 context"
    )
    with patch("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn"):
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            response = await client.post(
                "/api/chat/slots/owner-pick/agent",
                json={"agent": "writer"},
                headers={} if owner else {"X-Test-User": "other-user"},
            )
            if owner:
                assert response.status == 503, await response.text()
                assert (await response.json())["code"] == "store_unavailable"
                assert slot.agent == "default"
                assert (
                    state.conversation_log.get_metadata("dashboard:owner-pick").get("agent")
                    == "default"
                )
    key = "dashboard:owner-pick"
    # Neither an owner pick nor a turn can promote V1 transcript history.
    assert read_private_session_store(key) is None
    with pytest.raises(
        UnknownMemoryStore,
        match="canonical execution identity|canonical member identity|missing or malformed execution context",
    ):
        read_session_execution(key, required=True)
    assert read_private_session_store(key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [False, True])
async def test_owner_agent_pick_on_empty_chat_pins_private_memory(tmp_path, member_stores, owner):
    """The agent menu on an empty chat is a grant door, like create-with-agent.

    The transcript file already exists (the slot's metadata was flushed when it
    was created) but holds no message row: that is an EMPTY chat, and the
    owner's pick must pin the member's store so the first turn is admitted
    rather than refused with "no verified assignment". A non-owner pick
    grants nothing.
    """
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app_with_agent_routes, _make_state
    from dashboard_owner_helpers import as_owner

    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.member_memory_auth import read_private_session_store

    writer, _ = member_stores
    state = _make_state(tmp_path)
    state.sessions.reset = AsyncMock(return_value=True)
    state.get_or_create_slot("empty-pick", agent="default")
    key = "dashboard:empty-pick"
    # A metadata-only transcript: born by the slot's first flush, no messages.
    await asyncio.to_thread(
        state.conversation_log.update_metadata, key, {"agent": "default", "title": "New"}
    )
    assert state.conversation_log.has_log(key)
    assert not state.conversation_log.has_messages(key)
    with patch("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn"):
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            response = await client.post(
                "/api/chat/slots/empty-pick/agent",
                json={"agent": "writer"},
                headers={} if owner else {"X-Test-User": "other-user"},
            )
            if owner:
                assert response.status == 200, await response.text()
    slot = state._slots["empty-pick"]
    if owner:
        assert read_private_session_store(key) == writer
        assert slot.memory_store == writer
        # The first turn confirms the grant instead of refusing it.
        read_session_execution(key, required=True)
        assert read_private_session_store(key) == writer
    else:
        assert read_private_session_store(key) is None
        with pytest.raises(
            UnknownMemoryStore,
            match="canonical execution identity|canonical member identity|missing or malformed execution context",
        ):
            read_session_execution(key, required=True)
        assert read_private_session_store(key) is None


@pytest.mark.asyncio
async def test_unreadable_transcript_pick_is_refused_not_silently_committed(
    tmp_path, member_stores
):
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app_with_agent_routes, _make_state
    from dashboard_owner_helpers import as_owner

    from kiro_crew.member_memory_auth import read_private_session_store

    state = _make_state(tmp_path)
    state.sessions.reset = AsyncMock(return_value=True)
    slot = state.get_or_create_slot("unreadable-pick", agent="default")
    key = "dashboard:unreadable-pick"
    with (
        patch("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn"),
        patch.object(ConversationLog, "has_messages", side_effect=PermissionError("denied")),
    ):
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            response = await client.post(
                "/api/chat/slots/unreadable-pick/agent", json={"agent": "writer"}
            )
            assert response.status == 503, await response.text()
            assert (await response.json())["code"] == "store_unavailable"
    assert slot.agent == "default"
    assert state.conversation_log.get_metadata(key).get("agent") == "default"
    assert read_private_session_store(key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("slot_kind", ["linked", "channel"])
async def test_member_pick_on_unused_linked_or_channel_slot_captures_identity(
    tmp_path, member_stores, slot_kind
):
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app_with_agent_routes, _make_state
    from dashboard_owner_helpers import as_owner

    from kiro_crew.member_memory_auth import read_private_session_store

    writer, _ = member_stores
    state = _make_state(tmp_path)
    state.sessions.reset = AsyncMock(return_value=True)
    slot = state.get_or_create_slot("native-pick", agent="default")
    linked_key = "slack:123.456"
    if slot_kind == "linked":
        slot.linked_session_key = linked_key
    else:
        slot.channel_origin = True
    with patch("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn"):
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            response = await client.post(
                "/api/chat/slots/native-pick/agent", json={"agent": "writer"}
            )
            assert response.status == 200, await response.text()
    assert read_private_session_store("dashboard:native-pick") == (
        None if slot_kind == "linked" else writer
    )
    assert read_private_session_store(linked_key) == (writer if slot_kind == "linked" else None)


@pytest.mark.asyncio
async def test_owner_agent_pick_accepts_message_racing_with_private_pin(tmp_path, member_stores):
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app_with_agent_routes, _make_state
    from dashboard_owner_helpers import as_owner

    from kiro_crew.dashboard.chat_persistence import pin_private_agent_store
    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.member_memory_auth import read_private_session_store

    writer, _ = member_stores
    state = _make_state(tmp_path)
    state.sessions.reset = AsyncMock(return_value=True)
    slot = state.get_or_create_slot("raced-pick", agent="default")
    key = "dashboard:raced-pick"

    async def pin_with_message(state, session_key, agent, config, **kwargs):
        assigned_store = await pin_private_agent_store(state, session_key, agent, config, **kwargs)
        if kwargs.get("validate_only"):
            return assigned_store
        assert slot.agent == "writer"
        slot.messages.append({"role": "user", "content": "first private message"})
        read_session_execution(session_key, required=True)
        return assigned_store

    with (
        patch("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn"),
        patch("kiro_crew.dashboard.chat_handlers.pin_private_agent_store", pin_with_message),
    ):
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            response = await client.post(
                "/api/chat/slots/raced-pick/agent", json={"agent": "writer"}
            )
            assert response.status == 200, await response.text()
    assert slot.messages
    assert slot.agent == "writer"
    assert slot.memory_store == writer
    assert read_private_session_store(key) == writer


@pytest.mark.asyncio
async def test_owner_agent_pick_pin_failure_rolls_the_switch_back(tmp_path, member_stores):
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app_with_agent_routes, _make_state
    from dashboard_owner_helpers import as_owner

    from kiro_crew.member_memory_auth import read_private_session_store

    state = _make_state(tmp_path)
    state.sessions.reset = AsyncMock(return_value=True)
    slot = state.get_or_create_slot("failed-pick", agent="default")
    prior_memory_store = slot.memory_store
    key = "dashboard:failed-pick"
    with (
        patch("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn"),
        patch(
            "kiro_crew.dashboard.chat_handlers.pin_private_agent_store",
            AsyncMock(side_effect=UnknownMemoryStore("boom")),
        ),
    ):
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            response = await client.post(
                "/api/chat/slots/failed-pick/agent", json={"agent": "writer"}
            )
            assert response.status == 503, await response.text()
            assert (await response.json())["code"] == "store_unavailable"
    assert slot.agent == "default"
    assert slot.memory_store == prior_memory_store
    assert state.conversation_log.get_metadata(key).get("agent") == "default"
    assert read_private_session_store(key) is None


@pytest.mark.asyncio
async def test_owner_second_member_pick_on_pinned_empty_chat_is_refused(tmp_path, member_stores):
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app_with_agent_routes, _make_state
    from dashboard_owner_helpers import as_owner

    from kiro_crew.member_memory_auth import read_private_session_store

    writer, _ = member_stores
    state = _make_state(tmp_path)
    state.sessions.reset = AsyncMock(return_value=True)
    slot = state.get_or_create_slot("pinned-pick", agent="default")
    key = "dashboard:pinned-pick"
    with patch("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn"):
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            response = await client.post(
                "/api/chat/slots/pinned-pick/agent", json={"agent": "writer"}
            )
            assert response.status == 200, await response.text()
            assert slot.agent == "writer"
            assert read_private_session_store(key) == writer
            for agent in ("reviewer", "default"):
                response = await client.post(
                    "/api/chat/slots/pinned-pick/agent", json={"agent": agent}
                )
                assert response.status == 409, await response.text()
                assert (await response.json())["code"] == "member_session_pinned"
                assert slot.agent == "writer"
                assert read_private_session_store(key) == writer


@pytest.mark.asyncio
async def test_owner_agent_pick_to_v1_member_or_default_pins_nothing(tmp_path, member_stores):
    """Only a V2 member's pick writes a grant; V1 picks leave no binding behind."""
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app_with_agent_routes, _make_state
    from dashboard_owner_helpers import as_owner

    from kiro_crew.member_memory_auth import read_private_session_store

    cfg = KiroCrewConfig.load()
    cfg.agents["legacy"] = KiroCrewAgentConfig(kiro_agent="kirocrew", triggers="legacy")
    cfg.save()
    state = _make_state(tmp_path)
    state.sessions.reset = AsyncMock(return_value=True)
    state.get_or_create_slot("v1-pick", agent="default")
    key = "dashboard:v1-pick"
    with patch("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn"):
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            for agent in ("legacy", "default"):
                response = await client.post("/api/chat/slots/v1-pick/agent", json={"agent": agent})
                assert response.status == 200, await response.text()
                assert read_private_session_store(key) is None


def test_metadata_only_transcript_is_not_v1_history_for_a_private_bind(tmp_path, member_stores):
    """A transcript that holds only a metadata line is an empty conversation."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_persistence import _pin_private_agent_assignment
    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.member_memory_auth import read_private_session_store

    writer, _ = member_stores
    cfg = KiroCrewConfig.load()
    state = _make_state(tmp_path)
    key = "dashboard:metadata-only"
    state.conversation_log.update_metadata(key, {"agent": "writer", "title": "Empty"})
    assert state.conversation_log.has_log(key)
    assert not state.conversation_log.has_messages(key)
    with pytest.raises(
        UnknownMemoryStore,
        match="canonical execution identity|canonical member identity|missing or malformed execution context",
    ):
        read_session_execution(key, required=True)
    assert read_private_session_store(key) is None
    assert (
        _pin_private_agent_assignment(key, "writer", cfg, conversation_log=state.conversation_log)
        == writer
    )
    assert read_private_session_store(key) == writer

    # One message row is history, and history is never promoted.
    other = "dashboard:has-a-row"
    state.conversation_log.update_metadata(other, {"agent": "writer"})
    state.conversation_log.append(other, "user", "said something on V1")
    assert state.conversation_log.has_messages(other)
    with pytest.raises(UnknownMemoryStore, match="retains its existing history"):
        _pin_private_agent_assignment(other, "writer", cfg, conversation_log=state.conversation_log)
    assert read_private_session_store(other) is None


def test_unverifiable_transcript_never_reads_as_empty_for_a_private_bind(tmp_path, member_stores):
    """Fail closed: a corrupt line is history, and an unreadable file refuses."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_persistence import _pin_private_agent_assignment
    from kiro_crew.member_memory_auth import read_private_session_store

    cfg = KiroCrewConfig.load()
    state = _make_state(tmp_path)
    log = state.conversation_log

    corrupt = "dashboard:corrupt-row"
    log.update_metadata(corrupt, {"agent": "writer"})
    with open(log._path(corrupt), "ab") as handle:
        handle.write(b"{this is not json\n")
    assert log.has_messages(corrupt)
    with pytest.raises(UnknownMemoryStore, match="retains its existing history"):
        _pin_private_agent_assignment(corrupt, "writer", cfg, conversation_log=log)
    assert read_private_session_store(corrupt) is None

    unreadable = "dashboard:unreadable"
    log.update_metadata(unreadable, {"agent": "writer"})
    with patch("builtins.open", side_effect=PermissionError("denied")):
        with pytest.raises(OSError):
            log.has_messages(unreadable)
        with pytest.raises(UnknownMemoryStore, match="unreadable"):
            _pin_private_agent_assignment(unreadable, "writer", cfg, conversation_log=log)
    assert read_private_session_store(unreadable) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prior", ["fresh", "legacy", "private", "redirected", "collision", "foreign"]
)
async def test_owner_member_open_pins_only_its_unambiguous_canonical_session(
    tmp_path, member_stores, prior
):
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_state
    from test_members_dm_thread import _make_members_app

    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.member_memory_auth import read_private_session_store
    from kiro_crew.members import DM_SLOT_MODE, member_slot_key, write_dm_binding

    writer, reviewer = member_stores
    state = _make_state(tmp_path)
    legacy_slot_key = member_slot_key("writer")
    slot_key = member_slot_key("writer", writer)
    key = f"dashboard:{slot_key}"
    if prior != "fresh":
        write_dm_binding("writer", member="writer", slot_key=legacy_slot_key)
    if prior == "legacy":
        state.conversation_log.append(
            f"dashboard:{legacy_slot_key}", "user", "existing member discussion"
        )
        state.conversation_log.update_metadata(
            f"dashboard:{legacy_slot_key}", {"agent": "reviewer"}
        )
    elif prior == "private":
        bind_private_session_store(key, writer)
    elif prior == "redirected":
        slot = state.get_or_create_slot(slot_key, agent="writer", mode=DM_SLOT_MODE)
        slot.linked_session_key = "dashboard:another-conversation"
        slot._memory_assignment_from_history = True
    elif prior == "collision":
        cfg = KiroCrewConfig.load()
        cfg.agents["Writer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        provision_member_memory(cfg, "Writer")
        cfg.save()
    elif prior == "foreign":
        bind_private_session_store(key, reviewer)
    async with TestClient(TestServer(_make_members_app(state))) as client:
        response = await client.post("/api/members/writer/thread")
        expected = 409 if prior == "redirected" else 503 if prior == "foreign" else 200
        assert response.status == expected, await response.text()
    if prior in {"fresh", "legacy", "private", "collision"}:
        slot = state._slots[slot_key]
        assert slot.memory_store == writer
        read_session_execution(key, required=True)
        assert read_private_session_store(key) == writer
    else:
        assert read_private_session_store(key) == (reviewer if prior == "foreign" else None)
        assert read_private_session_store("dashboard:another-conversation") is None


@pytest.mark.parametrize("private_job", [False, True])
def test_cron_followup_uses_job_authority_not_provider_template_alias(
    tmp_path, member_stores, private_job
):
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.dashboard.cron_inject import _bind_cron_slot
    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.member_memory_auth import read_private_session_store

    writer, _ = member_stores
    job = CronJob(id="followup", name="follow up", message="task", agent_id="writer")
    key = "cron:followup"
    if private_job:
        job.member_id = "writer"
        job.memory_store = writer
        bind_private_session_store(key, writer)
        job.execution_context = read_session_execution(key).to_record()
    else:
        assert resolve_cron_memory(job) == ("", "writer")
    slot = _bind_cron_slot(_make_state(tmp_path), job, [])
    assert effective_session_key(slot) == key
    if private_job:
        read_session_execution(key, required=True)
        assert read_private_session_store(key) == writer
    else:
        with pytest.raises(
            UnknownMemoryStore,
            match="canonical execution identity|canonical member identity|missing or malformed execution context",
        ):
            read_session_execution(key, required=True)
        assert read_private_session_store(key) is None


@pytest.fixture
def member_stores(monkeypatch):
    # Provider calls in this file are doubles; model the supported WSL runtime.
    pass  # Member routing does not depend on OS isolation.
    cfg = KiroCrewConfig.load()
    cfg.agents["writer"] = KiroCrewAgentConfig(kiro_agent="kirocrew", triggers="write")
    cfg.agents["reviewer"] = KiroCrewAgentConfig(kiro_agent="kirocrew", triggers="review")
    writer = provision_member_memory(cfg, "writer")
    reviewer = provision_member_memory(cfg, "reviewer")
    cfg.save()
    return writer, reviewer


def test_restarted_run_restores_protected_identity_not_agent_editable_state(member_stores):
    writer, reviewer = member_stores
    folder = create_agent_folder("run1", memory_store=writer)
    state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
    state["memory_store"] = reviewer
    (folder / "state.json").write_text(json.dumps(state), encoding="utf-8")
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    assert not manager._agents
    assert manager._inherited_memory_store("run1") == writer


def test_missing_private_resume_record_fails_instead_of_global(member_stores):
    writer, _ = member_stores
    create_agent_folder("run1", memory_store=writer)
    folder = create_agent_folder("run1", memory_store=writer)
    row = json.loads((folder / "state.json").read_text(encoding="utf-8"))
    row.pop("execution_context")
    (folder / "state.json").write_text(json.dumps(row), encoding="utf-8")
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    with patch.object(manager, "spawn") as spawn:
        result = manager.continue_conversation("run1", "continue")
    assert result.done and result.error.startswith("memory_unavailable:")
    spawn.assert_not_called()


def test_legacy_resume_without_member_binding_remains_global():
    from kiro_crew.subagent_persistence import create_agent_folder

    create_agent_folder("legacy")
    assert read_run_memory_store("legacy") == ""


@pytest.mark.parametrize("replacement", [None, "", "default", "other"])
def test_canonical_channel_identity_outranks_legacy_metadata(member_stores, replacement):
    from kiro_crew.context import store_of_session

    writer, reviewer = member_stores
    key = "slack:member-channel-thread"
    bind_private_session_store(key, writer)
    assert store_of_session(ConversationLog(), key) == writer
    record = (
        {}
        if replacement is None
        else {"memory_store": reviewer if replacement == "other" else replacement}
    )
    assert store_of_session(SimpleNamespace(get_metadata=lambda _: record), key) == writer
    assert store_of_session(SimpleNamespace(get_metadata=lambda _: {}), "slack:legacy") == ""


def test_member_session_cannot_be_rebound_and_malformed_carrier_refuses(member_stores):
    from kiro_crew.context import store_of_session

    writer, reviewer = member_stores
    key = "dashboard:pinned-writer"
    bind_private_session_store(key, writer)
    with pytest.raises(ValueError, match="another memory binding"):
        bind_private_session_store(key, reviewer)
    ConversationLog().update_metadata(key, {"execution_context": {"member_id": 7}})
    with pytest.raises(UnknownMemoryStore, match="canonical|identity|malformed"):
        store_of_session(ConversationLog(), key)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["persistent", "incognito", "temporary"])
async def test_session_allocation_sets_member_context_and_mode_before_native_start(
    member_stores, tmp_path, monkeypatch, mode
):
    from kiro_crew.execution_context import bind_session_execution, resolve_member_execution
    from kiro_crew.providers.acp import AcpProvider
    from kiro_crew.session import SessionManager

    key = "cron:member-allocation"
    cfg = KiroCrewConfig.load()
    captured = resolve_member_execution(
        cfg, "writer", memory_mode=mode, validate_memory_files=False
    )
    bind_session_execution(key, captured)
    cfg.session.pool_size = 1
    manager = SessionManager(cfg, provider_factory=cfg.create_provider_factory())
    claim = AsyncMock()
    monkeypatch.setattr(manager, "_drain_and_claim", claim)
    monkeypatch.setattr(manager, "_dispatch_hard_kill", MagicMock())
    launches = []

    async def at_launch(provider):
        assert provider.member_context is True
        assert provider.memory_mode == mode
        launches.append(provider)
        raise RuntimeError("observed admitted native launch")

    monkeypatch.setattr(AcpProvider, "_start_kiro_runtime", at_launch)
    try:
        with pytest.raises(RuntimeError, match="observed admitted native launch"):
            await asyncio.wait_for(
                manager.get_or_create(key, agent="kirocrew", model="auto", cwd=str(tmp_path)), 5
            )
        assert key not in manager._sessions
        claim.assert_not_awaited()
        assert len(launches) == 1
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_private_consolidation_uses_separate_bound_process_and_cleans_it_up(member_stores):
    from kiro_crew.llm_helpers import background_turn
    from kiro_crew.member_memory_auth import private_memory_store_for_session
    from kiro_crew.session import BACKGROUND_KEY

    client = SimpleNamespace(last_prompt_stats=None)
    sessions = MagicMock(
        get_or_create=AsyncMock(return_value=(client, True, False)),
        remove=AsyncMock(),
        destroy=AsyncMock(),
        recycle_background=AsyncMock(),
    )
    keys = []
    for store in member_stores:
        async with background_turn(
            sessions, task="consolidation", agent="kirocrew-lite", memory_store=store
        ):
            key = sessions.get_or_create.call_args.args[0]
            keys.append(key)
            assert key != BACKGROUND_KEY
            assert private_memory_store_for_session(key) == store
        sessions.release.assert_called_with(key)
        sessions.remove.assert_awaited_with(key)
        sessions.destroy.assert_awaited_with(key)
        log = ConversationLog()
        assert not log._path(key).exists()
        assert not log._lock_path(key).exists()
    assert len(set(keys)) == 2
    sessions.recycle_background.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_consolidation_acquire_failure_removes_generated_artifacts(member_stores):
    from kiro_crew.llm_helpers import background_turn

    writer, _ = member_stores
    sessions = MagicMock(
        get_or_create=AsyncMock(side_effect=RuntimeError("provider unavailable")),
        remove=AsyncMock(),
        destroy=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="provider unavailable"):
        async with background_turn(
            sessions, task="consolidation", agent="kirocrew-lite", memory_store=writer
        ):
            pass
    key = sessions.get_or_create.call_args.args[0]
    sessions.remove.assert_awaited_once_with(key)
    sessions.destroy.assert_awaited_once_with(key)
    log = ConversationLog()
    assert not log._path(key).exists()
    assert not log._lock_path(key).exists()


@pytest.mark.asyncio
async def test_private_consolidation_preserves_authority_when_retirement_fails(member_stores):
    from kiro_crew.llm_helpers import background_turn

    writer, _ = member_stores
    client = SimpleNamespace(last_prompt_stats=None)
    sessions = MagicMock(
        get_or_create=AsyncMock(return_value=(client, True, False)),
        remove=AsyncMock(side_effect=OSError("provider still live")),
        destroy=AsyncMock(),
    )
    async with background_turn(
        sessions, task="consolidation", agent="kirocrew-lite", memory_store=writer
    ):
        key = sessions.get_or_create.call_args.args[0]
    assert ConversationLog()._path(key).exists()
    from kiro_crew.execution_context import read_session_execution

    assert read_session_execution(key).store.store_id == writer
    sessions.destroy.assert_not_awaited()


@pytest.mark.parametrize("contents", ["{truncated", "[]", "null"])
def test_unreadable_run_without_protected_identity_cannot_become_global(contents):
    from kiro_crew import subagent_persistence as persistence

    folder = persistence._agent_dir("broken-identity")
    folder.mkdir(parents=True)
    (folder / "state.json").write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match="run record is unavailable"):
        read_run_memory_store("broken-identity")


def test_schedule_member_survives_reload_without_origin_chat(tmp_path, member_stores):
    writer, _ = member_stores
    service = CronService(base_dir=tmp_path / "cron")
    job = service.add_job("daily", "write report", every_secs=60, member_id="writer")
    reloaded = CronService(base_dir=tmp_path / "cron").get_job(job.id)
    assert reloaded.member_id == "writer"
    assert reloaded.memory_store == writer
    assert resolve_cron_memory(reloaded) == (writer, "kirocrew")


@pytest.mark.parametrize("bad_identity", [None, False, 0, [], {}])
@pytest.mark.parametrize("field", ["member_id", "memory_store"])
def test_malformed_schedule_identity_never_means_global(field, bad_identity):
    job = CronJob(id="damaged", name="damaged", message="task")
    setattr(job, field, bad_identity)
    with pytest.raises(ValueError, match="malformed schedule identity"):
        resolve_cron_memory(job)


@pytest.mark.parametrize("bad_identity", [None, False, 0, [], {}])
def test_malformed_spawn_identity_is_refused_before_queueing(bad_identity):
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    with patch("kiro_crew.subagent.check_memory_available") as host_check:
        result = manager.spawn("task", memory_store=bad_identity)
    assert result.done and result.error.startswith("memory_unavailable:")
    host_check.assert_not_called()
    assert not manager._agents


def test_schedule_inherits_creator_member_once(tmp_path, member_stores):
    writer, reviewer = member_stores
    log = ConversationLog()
    bind_private_session_store("dashboard:writer", writer)
    log.update_metadata("dashboard:writer", {"memory_store": writer, "agent": "writer"})
    service = CronService(base_dir=tmp_path / "cron")
    job = service.add_job("daily", "write report", every_secs=60, session_key="dashboard:writer")
    log.update_metadata("dashboard:writer", {"memory_store": reviewer, "agent": "reviewer"})
    assert job.member_id == "writer"
    assert resolve_cron_memory(job)[0] == writer


def test_v1_provider_template_does_not_become_a_member(tmp_path, member_stores):
    job = CronService(base_dir=tmp_path / "cron").add_job(
        "legacy", "task", every_secs=60, agent_id="writer"
    )
    assert job.member_id == job.memory_store == ""
    assert resolve_cron_memory(job) == ("", "writer")


@pytest.mark.parametrize("origin", ["dashboard:writer", "subagent:writer-run"])
def test_sandbox_schedule_inherits_binding_without_opening_private_memory(
    tmp_path, member_stores, origin
):
    writer, _ = member_stores
    if origin.startswith("subagent:"):
        create_agent_folder("writer-run", memory_store=writer)
    else:
        bind_private_session_store(origin, writer)
        ConversationLog().update_metadata(origin, {"memory_store": writer})
    with patch(
        "kiro_crew.vector_memory.read_member_database_identity",
        side_effect=PermissionError("database unavailable"),
    ):
        job = CronService(base_dir=tmp_path / "cron").add_job(
            "scheduled", "write", every_secs=60, session_key=origin
        )
    assert job.member_id == "writer"
    assert job.memory_store == writer
    assert resolve_cron_memory(job) == (writer, "kirocrew")


def test_corrupt_existing_transcript_refuses_memory_resolution_and_scheduling(tmp_path):
    from kiro_crew.context import store_of_session

    log = ConversationLog()
    log.update_metadata("dashboard:broken", {"memory_store": "private-identity"})
    log._path("dashboard:broken").write_text("{truncated metadata\n", encoding="utf-8")
    with pytest.raises(UnknownMemoryStore, match="Global was not used"):
        store_of_session(log, "dashboard:broken")
    service = CronService(base_dir=tmp_path / "cron")
    with pytest.raises(ValueError, match="unreadable"):
        service.add_job("scheduled", "task", every_secs=60, session_key="dashboard:broken")
    assert service.list_jobs() == []


def test_schedule_refuses_rebinding_without_partial_changes(tmp_path, member_stores):
    service = CronService(base_dir=tmp_path / "cron")
    job = service.add_job("daily", "task", every_secs=60, member_id="writer")
    with pytest.raises(ValueError, match="fixed"):
        service.update_job(job.id, name="changed", member_id="reviewer")
    stored = service.get_job(job.id)
    assert stored.name == "daily"
    assert stored.member_id == "writer"


@pytest.mark.parametrize("recorded_agent", ["writer", "", "default", "custom-template"])
@pytest.mark.asyncio
async def test_linked_member_hydrates_provider_template_and_keeps_its_memory(
    member_stores, recorded_agent, monkeypatch
):
    from kiro_crew.context import store_of_session
    from kiro_crew.messaging.session_resume import persisted_session_agent
    from kiro_crew.slack import handler

    # Hydration's seen set and result map form one cache. Isolate both so a
    # preceding parameter on the same xdist worker cannot skip this hydration.
    monkeypatch.setattr(handler, "_hydrated_sessions", set())
    monkeypatch.setattr(handler, "_thread_agents", {})
    writer, _ = member_stores
    log = ConversationLog()
    key = "dashboard:linked-writer"
    bind_private_session_store(key, writer)
    log.update_metadata(key, {"memory_store": writer, "agent": recorded_agent})
    expected = "kirocrew"
    assert persisted_session_agent(log, key) == expected
    await handler._hydrate_thread_overrides(key, log)
    assert handler._thread_agents[key] == expected
    assert store_of_session(log, key) == writer


def test_unowned_v1_agent_name_is_not_reinterpreted_as_member(member_stores):
    from kiro_crew.messaging.session_resume import persisted_session_agent

    log = ConversationLog()
    log.update_metadata("slack:legacy", {"agent": "writer"})
    assert persisted_session_agent(log, "slack:legacy") == "writer"


@pytest.mark.asyncio
async def test_unavailable_private_run_refused_before_provider_allocation():
    sessions = MagicMock()
    sessions.get_or_create = AsyncMock()
    manager = SubagentManager(sessions=sessions, ctx_builder=MagicMock())
    info = SubagentInfo(id="run1", task="task", memory_store="missing-private")
    with pytest.raises(ValueError, match="no captured execution context"):
        await asyncio.wait_for(manager._run_inner(info, "subagent:run1"), 5)
    sessions.get_or_create.assert_not_called()


@pytest.mark.asyncio
async def test_linked_channel_refuses_private_memory_before_provider_and_displays_reason(
    monkeypatch,
):
    from test_messaging_dispatch import _CtxBuilder, _patch_pipeline, _Sessions, _turn

    from kiro_crew.messaging.dispatch import drive_turn

    _patch_pipeline(monkeypatch)
    sessions = _Sessions()
    sessions.get_or_create = AsyncMock()
    renderer = MagicMock()
    renderer.on_turn_start = AsyncMock()
    renderer.on_text_chunk = AsyncMock()
    renderer.on_done = AsyncMock()
    renderer.close = AsyncMock()
    turn = _turn(renderer)
    builder = _CtxBuilder()
    builder.conversation_log = ConversationLog()
    builder.conversation_log.update_metadata(turn.session_key, {"memory_store": "missing-private"})

    await drive_turn(turn, sessions=sessions, ctx_builder=builder)

    sessions.get_or_create.assert_not_called()
    renderer.on_text_chunk.assert_awaited_once()
    refusal = renderer.on_text_chunk.call_args.args[0]
    assert "Memory store declaration is unavailable" in refusal
    renderer.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_binding_publication_blocks_an_already_scheduled_run(member_stores):
    writer, _ = member_stores
    sessions = MagicMock()
    sessions.get_or_create = AsyncMock()
    manager = SubagentManager(sessions=sessions, ctx_builder=MagicMock())
    info = SubagentInfo(id="run1", task="task", memory_store=writer)
    with patch("kiro_crew.subagent.create_agent_folder", side_effect=OSError("disk full")):
        manager._log_spawned(info)
    assert info.error.startswith("memory_unavailable:")
    with pytest.raises(RuntimeError, match="could not persist"):
        await asyncio.wait_for(manager._run_inner(info, "subagent:run1"), 5)
    sessions.get_or_create.assert_not_called()


@pytest.mark.asyncio
async def test_scheduled_member_passes_private_store_to_context(member_stores):
    from test_cron_gateway_integration import _make_gw_for_llm, _run_llm_callback

    writer, _ = member_stores
    gateway = _make_gw_for_llm()
    gateway.ctx_builder.conversation_log = ConversationLog()
    gateway.ctx_builder.ensure_store = AsyncMock()
    job = CronJob(
        id="member-job", name="daily", message="report", member_id="writer", memory_store=writer
    )
    seen = []

    async def acquire(*args, **kwargs):
        seen.append(gateway.ctx_builder.conversation_log.get_metadata(args[0])["memory_store"])
        return MagicMock(), True, False

    from kiro_crew.execution_context import execution_for_store

    job.execution_context = execution_for_store(writer).to_record()
    with patch("kiro_crew.context.prepare_store_vectors", new=AsyncMock()) as prepare:
        await _run_llm_callback(gateway, job, get_or_create_side_effect=acquire)
    assert seen == [writer]
    prepare.assert_not_called()
    assert gateway.sessions.get_or_create.call_args.kwargs["agent"] == "kirocrew"


@pytest.mark.asyncio
async def test_deleted_scheduled_member_never_starts_global_provider():
    from test_cron_gateway_integration import _make_gw_for_llm, _run_llm_callback

    gateway = _make_gw_for_llm()
    job = CronJob(id="member-job", name="daily", message="report", member_id="deleted")
    with pytest.raises(ValueError, match="no canonical execution context"):
        await _run_llm_callback(gateway, job)
    gateway.sessions.get_or_create.assert_not_called()


@pytest.mark.asyncio
async def test_structural_startup_failure_keeps_memory_closed_and_explains_recovery(monkeypatch):
    from test_slack_gateway import _make_orchestrator

    from kiro_crew.memory_startup import (
        MemoryStartup,
        MemoryStartupUnavailable,
        require_memory_ready,
    )

    gateway = _make_orchestrator()
    gateway._memory_startup = MemoryStartup.begin()
    stopping = asyncio.Event()
    monkeypatch.setattr("kiro_crew.slack.gateway.shutdown_event", stopping)
    memory, vectors = MagicMock(), MagicMock()
    gateway.ctx_builder = SimpleNamespace(memory=memory)
    gateway.vector_memory = vectors
    try:
        with (
            patch(
                "kiro_crew.memory_backup.apply_pending_member_restores",
                side_effect=ValueError("memory configuration unreadable; previous data preserved"),
            ),
            patch("kiro_crew.context.reset_memory_caches"),
        ):
            assert await asyncio.to_thread(gateway._initialize_memory_worker) is False
        memory.init.assert_not_called()
        vectors.init.assert_not_called()
        with pytest.raises(MemoryStartupUnavailable, match="memory configuration unreadable"):
            require_memory_ready()
        from kiro_crew.dashboard.handlers._shared import memory_startup_refusal

        refusal = memory_startup_refusal()
        assert refusal.status == 503
        assert json.loads(refusal.text)["code"] == "store_unavailable"
        assert "previous data preserved" in json.loads(refusal.text)["error"]

        gateway._memory_startup_task = asyncio.get_running_loop().create_future()
        gateway._memory_startup_task.set_result(False)
        recovery_shell = asyncio.Event()
        with patch(
            "kiro_crew.slack.gateway.logger.error",
            side_effect=lambda *args: recovery_shell.set(),
        ):
            supervisor = asyncio.create_task(gateway._wait_for_memory_preparation())
            try:
                await asyncio.wait_for(recovery_shell.wait(), 1)
                assert not supervisor.done()
                memory.init.assert_not_called()
                vectors.init.assert_not_called()
                stopping.set()
                assert await asyncio.wait_for(supervisor, 1) is False
            finally:
                stopping.set()
                await supervisor
    finally:
        await asyncio.to_thread(gateway._stop_memory_startup)


def test_transient_history_cleanup_refuses_a_mismatched_protected_store(member_stores):
    from uuid import uuid4

    from kiro_crew.member_memory_auth import bind_private_session_store

    writer, reviewer = member_stores
    key = f"memory-consolidation:{writer}:{uuid4().hex}"
    log = ConversationLog()
    bind_private_session_store(key, reviewer)
    log.update_metadata(key, {"memory_store": reviewer})
    original = log._path(key).read_bytes()
    with pytest.raises(ValueError, match="another memory store"):
        log.delete_memory_consolidation_session(key, writer)
    assert log._path(key).read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["persistent", "incognito", "temporary"])
async def test_task_plan_policy_survives_fresh_gateway_context(tmp_path, monkeypatch, mode):
    from kiro_crew.context import ContextBuilder, inherit_session_memory
    from kiro_crew.execution_context import (
        ExecutionContext,
        MemoryStoreRef,
        bind_session_execution,
        read_session_execution,
    )
    from kiro_crew.taskrunner import TaskRunner

    execution = ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew", mode)
    bind_session_execution("dashboard:parent", execution)
    builder = ContextBuilder(conversation_log=ConversationLog())
    sessions = MagicMock()
    runner = TaskRunner(sessions=sessions, context_builder=builder, work_dir=tmp_path)
    run = await runner.plan(
        input_text="agents:\n  inspect:\n    prompt: Inspect the input\n",
        source="yaml",
        session_key="dashboard:parent",
    )
    assert run.execution_context == execution
    root = f"taskrunner:{run.task_id}:runtime"
    assert read_session_execution(root) == execution
    fresh = ContextBuilder(conversation_log=ConversationLog())
    runner._ctx = fresh
    child = f"taskrunner:{run.task_id}:task1"
    assert await inherit_session_memory(fresh, root, child) == ""
    assert read_session_execution(child) == execution
    assert fresh._session_memory_modes[child] == mode
    rows = json.loads(runner._runs_path().read_text(encoding="utf-8"))
    assert bool(rows) == (mode == "persistent")
    if mode != "persistent":
        assert not fresh.conversation_log._path(child).exists()
    runner._lesson_store = MagicMock()
    runner._call_llm_for_lesson = AsyncMock(return_value={"rule": "use bounded waits"})
    await runner._extract_lesson(run.tasks[0], run)
    assert runner._call_llm_for_lesson.call_count == (1 if mode == "persistent" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "corrupt", "mode"])
async def test_runtime_policy_damage_refuses_even_with_persistent_history(
    tmp_path, monkeypatch, damage
):
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
    from kiro_crew.task_models import Project
    from kiro_crew.taskrunner import TaskRunner
    from kiro_crew.workflow_memory import TaskSnapshotError

    runner = TaskRunner(sessions=MagicMock(), work_dir=tmp_path)
    execution = ExecutionContext(
        "alice", MemoryStoreRef("alice-store", "alice"), "member", "kirocrew"
    )
    run = Project(
        "", "retained body", task_id="damaged", status="planned", execution_context=execution
    )
    runner._runs[run.task_id] = run
    rows = json.loads(runner._serialize_runs())
    if damage == "missing":
        rows[0]["execution_context"].pop("member_id")
    elif damage == "corrupt":
        rows[0]["execution_context"] = "invalid"
    else:
        rows[0]["execution_context"]["memory_mode"] = "invalid"
    runner._runs_path().write_text(json.dumps(rows), encoding="utf-8")
    before = runner._runs_path().read_bytes()
    restored = TaskRunner(sessions=MagicMock(), work_dir=tmp_path)
    assert not restored._runs
    assert restored._snapshot_recovery_incomplete
    with pytest.raises(TaskSnapshotError):
        await restored._apersist_runs()
    assert runner._runs_path().read_bytes() == before
