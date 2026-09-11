"""Private member identity survives scheduling, retries and process restarts."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from member_memory_helpers import patch_private_memory_supported

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.cron import CronJob, CronService, resolve_cron_memory
from kiro_crew.history import ConversationLog
from kiro_crew.member_memory_auth import bind_private_session_store
from kiro_crew.memory_stores import UnknownMemoryStore, provision_member_memory
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_persistence import (
    _run_memory_identity_path,
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
    await asyncio.to_thread(log.update_metadata, parent, {"memory_store": reviewer})
    builder = SimpleNamespace(conversation_log=log, ensure_store=AsyncMock())
    with pytest.raises(UnknownMemoryStore, match="protected member binding"):
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
    run = Project(spec_path="spec.md", spec_content="private task", task_id="failed-private")
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
    await asyncio.to_thread(log.update_metadata, runtime, {"memory_store": reviewer})
    sessions = MagicMock(open_task_session=AsyncMock())
    run = Project(spec_path="spec.md", spec_content="task", task_id="broken-review")
    builder = SimpleNamespace(conversation_log=log, ensure_store=AsyncMock())
    with pytest.raises(UnknownMemoryStore, match="protected member binding"):
        await self_review(
            run, Task(index=1, title="task", description="work"), sessions, "kirocrew", ctx=builder
        )
    sessions.open_task_session.assert_not_called()
    builder.ensure_store.assert_not_called()


@pytest.mark.asyncio
async def test_registered_private_hook_prepares_its_member_on_later_delivery(member_stores):
    from kiro_crew.context import store_of_session
    from kiro_crew.dashboard.handlers.hooks import _run_hook_inner
    from kiro_crew.mcp_caller import CallerContext
    from kiro_crew.mcp_tools import control

    writer, _ = member_stores
    origin, hook_key = "dashboard:hook-owner", f"hook:{writer}:private-report"
    bind_private_session_store(origin, writer)
    from kiro_crew.member_memory_auth import issue_member_session_proof, publish_member_session_pid

    await asyncio.to_thread(ConversationLog().update_metadata, origin, {"memory_store": writer})
    publish_member_session_pid(os.getpid(), origin, memory_store=writer)
    proof = issue_member_session_proof(origin, os.getpid())
    assert proof
    caller = CallerContext(session_key=origin, from_gateway=True, member_memory_proof=proof)
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
    assert store_of_session(log, hook_key) == writer
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
    sessions.get_or_create.assert_awaited_once()


def test_private_hook_cannot_be_registered_by_an_unsigned_backend(member_stores):
    from kiro_crew.history import ConversationLog
    from kiro_crew.mcp_tools import control
    from kiro_crew.member_memory_auth import read_private_session_store

    writer, _ = member_stores
    origin = "dashboard:unsigned-hook-owner"
    bind_private_session_store(origin, writer)
    # A persisted member assignment does not authenticate the process making
    # this request, even when the transcript names the same store.
    ConversationLog().update_metadata(origin, {"memory_store": writer})
    hooks_file = control.mcp_core.config_dir() / "hooks.json"
    hooks_before = hooks_file.read_bytes() if hooks_file.exists() else None
    with (
        patch.object(control.mcp_core, "_resolve_session_key_strict", return_value=origin),
        patch("kiro_crew.mcp_caller.current_caller", return_value=None),
        patch("kiro_crew.member_memory_auth.protected_member_session_for_pid", return_value=None),
    ):
        result = control.register_hook(
            "register_hook", {"hook_id": "refused", "context_summary": "report"}
        )
    assert result == (
        "Error: the hook's protected member binding is unavailable; global memory was not used"
    )
    assert read_private_session_store(origin) == writer
    assert read_private_session_store("hook:refused") is None
    assert read_private_session_store(f"hook:{writer}:refused") is None
    assert (hooks_file.read_bytes() if hooks_file.exists() else None) == hooks_before


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
    from kiro_crew.dashboard.chat_runner import _bind_private_slot_memory
    from kiro_crew.dashboard.chat_utils import effective_session_key
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
        _bind_private_slot_memory(key, writer, restored=slot._memory_assignment_from_history)
        assert read_private_session_store(key) == writer
    else:
        with pytest.raises(UnknownMemoryStore, match="no verified assignment"):
            _bind_private_slot_memory(key, writer, restored=slot._memory_assignment_from_history)
        assert read_private_session_store(key) is None


@pytest.mark.asyncio
async def test_http_resume_cannot_authorize_private_transcript(tmp_path, member_stores):
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app_with_agent_routes, _make_state
    from dashboard_owner_helpers import as_owner

    from kiro_crew.dashboard.chat_runner import _bind_private_slot_memory
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
    slot = state._slots["resume-private"]
    with pytest.raises(UnknownMemoryStore, match="no verified assignment"):
        _bind_private_slot_memory(key, writer, restored=slot._memory_assignment_from_history)
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

    from kiro_crew.dashboard.chat_runner import _bind_private_slot_memory
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
                assert response.status == 200, await response.text()
    key = "dashboard:owner-pick"
    # The selection itself is rollback-able; the actual turn publishes the pin.
    assert read_private_session_store(key) is None
    with pytest.raises(UnknownMemoryStore, match="no verified assignment"):
        _bind_private_slot_memory(
            key,
            writer,
            restored=slot._memory_assignment_from_history,
            conversation_log=state.conversation_log,
        )
    assert read_private_session_store(key) is None


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

    from kiro_crew.dashboard.chat_runner import _bind_private_slot_memory
    from kiro_crew.member_memory_auth import read_private_session_store
    from kiro_crew.members import DM_SLOT_MODE, member_slot_key, write_dm_binding

    writer, reviewer = member_stores
    state = _make_state(tmp_path)
    legacy_slot_key = member_slot_key("writer")
    slot_key = member_slot_key("writer", "" if prior == "private" else writer)
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
        expected = (
            409 if prior in {"redirected", "collision"} else 503 if prior == "foreign" else 200
        )
        assert response.status == expected, await response.text()
    if prior in {"fresh", "legacy", "private"}:
        slot = state._slots[slot_key]
        assert slot.memory_store == writer
        _bind_private_slot_memory(key, writer, restored=slot._memory_assignment_from_history)
        assert read_private_session_store(key) == writer
    else:
        assert read_private_session_store(key) == (reviewer if prior == "foreign" else None)
        assert read_private_session_store("dashboard:another-conversation") is None


@pytest.mark.parametrize("private_job", [False, True])
def test_cron_followup_uses_job_authority_not_provider_template_alias(
    tmp_path, member_stores, private_job
):
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_runner import _bind_private_slot_memory
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.dashboard.cron_inject import _bind_cron_slot
    from kiro_crew.member_memory_auth import read_private_session_store

    writer, _ = member_stores
    job = CronJob(id="followup", name="follow up", message="task", agent_id="writer")
    key = "cron:followup"
    if private_job:
        job.member_id = "writer"
        job.memory_store = writer
        bind_private_session_store(key, writer)
    else:
        assert resolve_cron_memory(job) == ("", "writer")
    slot = _bind_cron_slot(_make_state(tmp_path), job, [])
    assert effective_session_key(slot) == key
    if private_job:
        _bind_private_slot_memory(key, writer, restored=slot._memory_assignment_from_history)
        assert read_private_session_store(key) == writer
    else:
        with pytest.raises(UnknownMemoryStore, match="no verified assignment"):
            _bind_private_slot_memory(key, writer, restored=slot._memory_assignment_from_history)
        assert read_private_session_store(key) is None


@pytest.mark.parametrize("scope_member", ["writer", "reviewer", "global"])
def test_history_tools_round_trip_only_protected_filename_aliases(
    member_stores, monkeypatch, scope_member
):
    from kiro_crew import mcp_core
    from kiro_crew.history import transcript_stem
    from kiro_crew.mcp_tools import sessions

    writer, reviewer = member_stores
    entries = [
        ("dashboard:writer:project_one", writer, "Writer confidential"),
        ("dashboard:reviewer:project_one", reviewer, "Reviewer confidential"),
        ("dashboard:global", "", "Global confidential"),
    ]
    log = ConversationLog()
    for key, store, label in entries:
        if store:
            bind_private_session_store(key, store)
        log.append(key, "user", label + " history")
        log.update_metadata(key, {"memory_store": store, "title": label})
    scope = {"writer": writer, "reviewer": reviewer, "global": ""}[scope_member]
    caller = next(key for key, store, _ in entries if store == scope)
    # Authentication is separately exercised with kernel/proof fixtures. Keep
    # the index, protected records, metadata and content readers real here.
    monkeypatch.setattr("kiro_crew.member_memory_auth.mcp_memory_scope", lambda key: scope)
    monkeypatch.setattr(mcp_core, "require_strict_session_key", lambda error: (caller, ""))
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: caller)
    listed = sessions.list_sessions("list_sessions", {"all_workspaces": True})
    searched = sessions.search_chat_history(
        "search_chat_history", {"query": "confidential", "all_workspaces": True}
    )
    for key, store, label in entries:
        stem = transcript_stem(key)
        assert (stem in listed) == (store == scope)
        assert (label in searched) == (store == scope)
        fetched = sessions.get_chat_session(
            "get_chat_session", {"session_key": stem, "all_workspaces": True}
        )
        assert (label + " history" in fetched) == (store == scope)
        if store != scope:
            assert fetched.startswith("Access denied:")


def test_private_history_index_rechecks_records_and_rejects_cross_store_collisions(member_stores):
    from kiro_crew.history import transcript_stem
    from kiro_crew.mcp_tools.sessions import _history_memory_visible
    from kiro_crew.member_memory_auth import _session_binding_path, private_history_session_index

    writer, reviewer = member_stores
    key = "dashboard:collision:part"
    alias = "dashboard:collision_part"
    assert transcript_stem(key) == transcript_stem(alias)
    log = ConversationLog()
    bind_private_session_store(key, writer)
    log.append(key, "user", "writer history")
    log.update_metadata(key, {"memory_store": writer})
    index = private_history_session_index()
    stem = transcript_stem(key)
    assert _history_memory_visible(stem, writer, index)
    # An unsigned canonical-key claim cannot redirect a protected lookup.
    log.update_metadata(key, {"session_key": "dashboard:someone-else"})
    assert _history_memory_visible(stem, writer, index)
    bind_private_session_store(alias, reviewer)
    collision_index = private_history_session_index()
    assert not _history_memory_visible(stem, writer, collision_index)
    assert not _history_memory_visible(stem, reviewer, collision_index)
    assert not _history_memory_visible(stem, "", collision_index)
    assert not _history_memory_visible(key, writer, collision_index)
    assert not _history_memory_visible(alias, reviewer, collision_index)
    # A once-valid snapshot never permits a removed committed record.
    _session_binding_path(key).unlink()
    assert not _history_memory_visible(stem, writer, index)
    with pytest.raises(ValueError, match="committed private history binding"):
        private_history_session_index()


@pytest.fixture
def member_stores(monkeypatch):
    # Provider calls in this file are doubles; model the supported WSL runtime.
    patch_private_memory_supported(monkeypatch)
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
    _run_memory_identity_path("run1").unlink()
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    with patch.object(manager, "spawn") as spawn:
        result = manager.continue_conversation("run1", "continue")
    assert result.done and result.error.startswith("memory_unavailable:")
    spawn.assert_not_called()


def test_legacy_resume_without_member_binding_remains_global():
    assert read_run_memory_store("legacy") == ""


@pytest.mark.parametrize("replacement", [None, "", "default", "other"])
def test_private_channel_binding_survives_restart_and_refuses_metadata_downgrade(
    member_stores, replacement
):
    from kiro_crew.context import store_of_session
    from kiro_crew.member_memory_auth import bind_private_session_store

    writer, reviewer = member_stores
    key = "slack:private-channel-thread"
    log = ConversationLog()
    log.init()
    log.update_metadata(key, {"memory_store": writer})
    bind_private_session_store(key, writer)
    assert store_of_session(ConversationLog(), key) == writer
    record = (
        {}
        if replacement is None
        else {"memory_store": reviewer if replacement == "other" else replacement}
    )
    with pytest.raises(UnknownMemoryStore, match="protected member binding"):
        store_of_session(SimpleNamespace(get_metadata=lambda _: record), key)
    assert store_of_session(SimpleNamespace(get_metadata=lambda _: {}), "slack:legacy") == ""


def test_private_session_cannot_be_rebound_or_lose_its_protected_record(member_stores):
    from kiro_crew.context import store_of_session
    from kiro_crew.member_memory_auth import _session_binding_path, bind_private_session_store

    writer, reviewer = member_stores
    key = "dashboard:pinned-writer"
    bind_private_session_store(key, writer)
    with pytest.raises(ValueError, match="already bound"):
        bind_private_session_store(key, reviewer)
    _session_binding_path(key).unlink()
    with pytest.raises(UnknownMemoryStore, match="protected member session binding"):
        store_of_session(SimpleNamespace(get_metadata=lambda _: {}), key)


@pytest.mark.parametrize("target", ["default", "legacy", "peer"])
def test_private_caller_cannot_delegate_into_legacy_or_peer_memory(member_stores, target):
    from kiro_crew.context import require_memory_delegation
    from kiro_crew.history import ConversationLog
    from kiro_crew.member_memory_auth import bind_private_session_store

    writer, peer = member_stores
    key = "dashboard:private-delegator"
    log = ConversationLog()
    log.update_metadata(key, {"memory_store": writer})
    bind_private_session_store(key, writer)
    selected = peer if target == "peer" else target
    with pytest.raises(UnknownMemoryStore, match="must retain"):
        require_memory_delegation(log, key, selected)
    require_memory_delegation(log, key, writer)


@pytest.mark.asyncio
async def test_provider_factory_derives_private_fence_from_persisted_identity(
    member_stores, tmp_path, monkeypatch
):
    from kiro_crew import member_memory_auth as auth

    writer, _ = member_stores
    log = ConversationLog()
    log.init()
    log.update_metadata("dashboard:member-writer", {"memory_store": writer})
    bind_private_session_store("dashboard:member-writer", writer)
    create_agent_folder("private-worker", memory_store=writer)
    resolve = auth.private_memory_store_for_session
    loop_thread = threading.get_ident()
    resolved = []

    def read_identity(key):
        resolved.append((key, threading.get_ident()))
        return resolve(key)

    monkeypatch.setattr(auth, "private_memory_store_for_session", read_identity)
    factory = KiroCrewConfig.load().create_provider_factory()
    for index, (key, claimed_private, expected) in enumerate(
        (
            ("dashboard:member-writer", False, True),
            ("subagent:private-worker", False, True),
            ("dashboard:unowned", True, False),
        )
    ):
        provider = factory(key, private_memory=claimed_private, cwd=str(tmp_path / str(index)))
        assert len(resolved) == index, "synchronous construction must not read private identity"
        await asyncio.wait_for(provider.prepare_private_memory(), timeout=5)
        assert provider._private_memory is expected
        assert provider._client._private_memory is expected
        await asyncio.wait_for(provider.prepare_private_memory(), timeout=5)
        assert len(resolved) == index + 1
    assert all(thread != loop_thread for _, thread in resolved)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["", "kas", "claude"])
@pytest.mark.parametrize("private", [False, True])
async def test_direct_provider_start_prepares_private_flags_and_mcp_routing(
    tmp_path, monkeypatch, backend, private
):
    from kiro_crew import member_memory_auth as auth
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.providers.acp import AcpProvider

    loop_thread = threading.get_ident()
    reads = []

    def read_identity(key):
        reads.append((key, threading.get_ident()))
        return "member-writer" if private else ""

    monkeypatch.setattr(auth, "private_memory_store_for_session", read_identity)
    overlay, socket = tmp_path / "overlay", tmp_path / "gateway.sock"
    provider = AcpProvider(
        work_dir=tmp_path,
        session_key="dashboard:direct-private",
        acp_backend=backend,
        mcp_gateway_overlay=overlay,
        mcp_gateway_socket=socket,
    )
    started = []

    async def at_launch(_instance):
        assert threading.get_ident() == loop_thread
        assert provider._private_memory is private
        assert provider._client._private_memory is private
        assert provider._client._mcp_gateway_overlay == (None if private else str(overlay))
        assert provider._client._mcp_gateway_socket == (None if private else str(socket))
        assert provider._client._private_mcp_gateway_socket == str(socket)
        started.append(backend)

    monkeypatch.setattr(AcpProvider, "_start_kiro_runtime", at_launch)
    monkeypatch.setattr(AcpClient, "ensure_ready", at_launch)
    monkeypatch.setattr(AcpProvider, "_apply_initial_effort", AsyncMock())
    await asyncio.wait_for(provider.start(), timeout=5)
    # A runtime replacement may not yet carry a session key. Repeated start
    # preserves the prepared original identity without reading that wrapper.
    provider._client._session_key = ""
    await asyncio.wait_for(provider.start(), timeout=5)
    assert started == [backend, backend]
    assert len(reads) == 1 and reads[0][0] == "dashboard:direct-private"
    assert reads[0][1] != loop_thread


@pytest.mark.asyncio
async def test_private_provider_preparation_refuses_unsupported_backend_before_start(
    tmp_path, monkeypatch
):
    from kiro_crew import member_memory_auth as auth
    from kiro_crew.acp.types import ACP_BACKEND_CODEX
    from kiro_crew.providers.acp import AcpProvider

    monkeypatch.setattr(auth, "private_memory_store_for_session", lambda key: "member-writer")
    provider = AcpProvider(
        work_dir=tmp_path, session_key="dashboard:private", acp_backend=ACP_BACKEND_CODEX
    )
    launch = AsyncMock()
    monkeypatch.setattr(provider._client, "ensure_ready", launch)
    with pytest.raises(UnknownMemoryStore, match="Global Memory V1 was not used"):
        await asyncio.wait_for(provider.start(), timeout=5)
    assert not provider._private_memory_prepared
    assert not provider._private_memory
    assert not provider._client._private_memory
    launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_preparation_failed_read_can_retry_without_publication(tmp_path, monkeypatch):
    from kiro_crew import member_memory_auth as auth
    from kiro_crew.providers.acp import AcpProvider

    resolver = MagicMock(side_effect=UnknownMemoryStore("private binding unreadable"))
    monkeypatch.setattr(auth, "private_memory_store_for_session", resolver)
    socket = tmp_path / "gateway.sock"
    provider = AcpProvider(
        work_dir=tmp_path, session_key="dashboard:retry", mcp_gateway_socket=socket
    )
    launch = AsyncMock()
    monkeypatch.setattr(provider, "_start_kiro_runtime", launch)
    with pytest.raises(UnknownMemoryStore, match="private binding unreadable"):
        await asyncio.wait_for(provider.start(), timeout=5)
    assert not provider._private_memory_prepared
    assert not provider._private_memory and not provider._client._private_memory
    assert provider._client._mcp_gateway_socket == str(socket)
    launch.assert_not_awaited()

    resolver.side_effect = None
    resolver.return_value = "member-writer"
    await asyncio.wait_for(provider.start(), timeout=5)
    assert provider._private_memory_prepared and provider._private_memory
    assert provider._client._private_memory
    assert provider._client._mcp_gateway_socket is None
    assert provider._client._private_mcp_gateway_socket == str(socket)
    launch.assert_awaited_once()
    assert resolver.call_count == 2


@pytest.mark.asyncio
async def test_cancelled_private_preparation_does_not_publish_worker_result(tmp_path, monkeypatch):
    from kiro_crew import member_memory_auth as auth
    from kiro_crew.providers.acp import AcpProvider

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release, finished = threading.Event(), threading.Event()

    def read_identity(key):
        loop.call_soon_threadsafe(entered.set)
        try:
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release identity read")
            return "member-writer"
        finally:
            finished.set()

    monkeypatch.setattr(auth, "private_memory_store_for_session", read_identity)
    provider = AcpProvider(work_dir=tmp_path, session_key="dashboard:cancelled")
    launch = AsyncMock()
    monkeypatch.setattr(provider, "_start_kiro_runtime", launch)
    task = asyncio.create_task(provider.start())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert not task.done(), "the loop remains responsive while the identity read waits"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)
        assert await asyncio.to_thread(finished.wait, 5)
    assert not provider._private_memory_prepared
    assert not provider._private_memory
    assert not provider._client._private_memory
    launch.assert_not_awaited()

    monkeypatch.setattr(auth, "private_memory_store_for_session", lambda key: "member-writer")
    await asyncio.wait_for(provider.start(), timeout=5)
    assert provider._private_memory_prepared and provider._private_memory
    launch.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_binding", [False, True])
async def test_session_allocation_prepares_real_factory_before_private_comparison(
    member_stores, tmp_path, monkeypatch, changed_binding
):
    from kiro_crew import member_memory_auth as auth
    from kiro_crew import session_allocation
    from kiro_crew.providers.acp import AcpProvider
    from kiro_crew.session import SessionManager

    writer, _ = member_stores
    key = "cron:private-allocation"
    bind_private_session_store(key, writer)
    ConversationLog().update_metadata(key, {"memory_store": writer})
    resolve = auth.private_memory_store_for_session
    loop_thread = threading.get_ident()
    reads = []

    def read_identity(candidate):
        reads.append((candidate, threading.get_ident()))
        return "" if changed_binding and len(reads) == 2 else resolve(candidate)

    monkeypatch.setattr(auth, "private_memory_store_for_session", read_identity)
    monkeypatch.setattr(session_allocation, "private_memory_store_for_session", read_identity)
    cfg = KiroCrewConfig.load()
    cfg.session.pool_size = 1
    manager = SessionManager(cfg, provider_factory=cfg.create_provider_factory())
    claim = AsyncMock()
    monkeypatch.setattr(manager, "_drain_and_claim", claim)
    monkeypatch.setattr(manager, "_dispatch_hard_kill", MagicMock())

    async def at_launch(provider):
        assert provider._private_memory and provider._client._private_memory
        raise RuntimeError("test reached private launch boundary")

    launch = AsyncMock(side_effect=at_launch)

    # Patch on the instance boundary via a real async method so binding is kept.
    async def observe_launch(provider):
        await launch(provider)

    monkeypatch.setattr(AcpProvider, "_start_kiro_runtime", observe_launch)
    expected = "Provider does not match" if changed_binding else "test reached private launch"
    try:
        with pytest.raises(RuntimeError, match=expected):
            await asyncio.wait_for(
                manager.get_or_create(key, agent="kirocrew", model="auto", cwd=str(tmp_path)),
                timeout=5,
            )
        assert key not in manager._sessions
        claim.assert_not_awaited()
        if changed_binding:
            launch.assert_not_awaited()
        else:
            launch.assert_awaited_once()
        assert len(reads) == 2
        assert all(candidate == key and thread != loop_thread for candidate, thread in reads)
    finally:
        await manager.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_provider_preserves_private_fence_when_constructing_runtime(tmp_path, private):
    from kiro_crew.providers.acp import AcpProvider

    with patch("kiro_crew.providers.acp.AcpClient") as client_type:
        provider = AcpProvider(private_memory=private)
    assert client_type.call_args.kwargs.get("private_memory", False) is private
    provider._client = SimpleNamespace(
        _work_dir=tmp_path,
        _agent="kirocrew",
        _sandbox_mode="auto",
        _extra_env={},
        _mcp_gateway_overlay=None,
        _mcp_gateway_socket=None,
        _private_mcp_gateway_socket=str(tmp_path / "custom-broker.sock"),
        _resume_session_id="",
        _model="auto",
        backend="",
    )
    runtime = MagicMock(spawn=AsyncMock(side_effect=RuntimeError("stop before process launch")))
    with patch("kiro_crew.providers.acp.AcpRuntime", return_value=runtime) as runtime_type:
        with pytest.raises(RuntimeError, match="stop before process launch"):
            await provider._start_kiro_runtime_impl({}, {})
    assert runtime_type.call_args.kwargs.get("private_memory", False) is private
    assert runtime_type.call_args.kwargs["mcp_gateway_socket"] == (
        str(tmp_path / "custom-broker.sock") if private else None
    )


@pytest.mark.asyncio
async def test_private_consolidation_uses_separate_bound_process_and_cleans_it_up(member_stores):
    from kiro_crew.llm_helpers import background_turn
    from kiro_crew.member_memory_auth import (
        _session_binding_path,
        private_memory_store_for_session,
    )
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
        assert not _session_binding_path(key).parent.exists()
    assert len(set(keys)) == 2
    sessions.recycle_background.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_consolidation_acquire_failure_removes_generated_artifacts(member_stores):
    from kiro_crew.llm_helpers import background_turn
    from kiro_crew.member_memory_auth import _session_binding_path

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
    assert not _session_binding_path(key).parent.exists()


@pytest.mark.asyncio
async def test_private_consolidation_preserves_authority_when_retirement_fails(member_stores):
    from kiro_crew.llm_helpers import background_turn
    from kiro_crew.member_memory_auth import _session_binding_path

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
    assert _session_binding_path(key).is_file()
    sessions.destroy.assert_not_awaited()


@pytest.mark.parametrize("contents", ["{truncated", "[]", "null"])
def test_unreadable_run_without_protected_identity_cannot_become_global(contents):
    from kiro_crew import subagent_persistence as persistence

    folder = persistence._agent_dir("broken-identity")
    folder.mkdir(parents=True)
    (folder / "state.json").write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match="run metadata is unreadable"):
        read_run_memory_store("broken-identity")


def test_unprotected_old_record_cannot_authorize_a_private_resume(member_stores):
    from kiro_crew import subagent_persistence as persistence

    writer, _ = member_stores
    old = persistence._cleanup_identities_path("old-identity").parent / "memory.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"version": 2, "memory_store": writer}), encoding="utf-8")
    with pytest.raises(ValueError, match="protected memory record"):
        read_run_memory_store("old-identity")


def test_missing_private_record_cannot_be_hidden_by_editing_run_state(member_stores):
    writer, _ = member_stores
    folder = create_agent_folder("edited-state", memory_store=writer)
    _run_memory_identity_path("edited-state").unlink()
    (folder / "state.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="protected memory record"):
        read_run_memory_store("edited-state")


def test_schedule_member_survives_reload_without_origin_chat(tmp_path, member_stores):
    writer, _ = member_stores
    service = CronService(base_dir=tmp_path / "cron")
    job = service.add_job("daily", "write report", every_secs=60, member_id="writer")
    reloaded = CronService(base_dir=tmp_path / "cron").get_job(job.id)
    assert reloaded.member_id == "writer"
    assert reloaded.memory_store == writer
    assert resolve_cron_memory(reloaded) == (writer, "kirocrew")


@pytest.mark.parametrize("mode", ["command", "script"])
@pytest.mark.parametrize("inherit", [False, True])
def test_private_deterministic_schedule_refused_before_persistence(
    tmp_path, member_stores, mode, inherit
):
    writer, _ = member_stores
    ConversationLog().update_metadata("dashboard:writer", {"memory_store": writer})
    bind_private_session_store("dashboard:writer", writer)
    identity = {"session_key": "dashboard:writer"} if inherit else {"member_id": "writer"}
    body = "echo hello" if mode == "command" else "report.py:run"
    service = CronService(base_dir=tmp_path / "cron")
    with pytest.raises(ValueError, match="require an agent task"):
        service.add_job("daily", "", every_secs=60, **identity, **{mode: body})
    assert service.list_jobs() == []
    assert CronService(base_dir=tmp_path / "cron").list_jobs() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["command", "script"])
async def test_imported_private_deterministic_schedule_never_dispatches(member_stores, mode):
    from test_cron_gateway_integration import _make_gw_for_llm, _run_llm_callback

    writer, _ = member_stores
    gateway = _make_gw_for_llm()
    job = CronJob(
        id="private-import", name="daily", message="", member_id="writer", memory_store=writer
    )
    setattr(job, mode, "echo hello" if mode == "command" else "report.py:run")
    with (
        patch("kiro_crew.slack.gateway.run_command_sandboxed") as command,
        patch("kiro_crew.slack.gateway.run_script_sandboxed") as script,
        pytest.raises(ValueError, match="require an agent task"),
    ):
        await _run_llm_callback(gateway, job)
    command.assert_not_called()
    script.assert_not_called()
    gateway.sessions.get_or_create.assert_not_called()


@pytest.mark.parametrize("bad_identity", [None, False, 0, [], {}])
@pytest.mark.parametrize("field", ["member_id", "memory_store"])
def test_malformed_schedule_identity_never_means_global(field, bad_identity):
    job = CronJob(id="damaged", name="damaged", message="task")
    setattr(job, field, bad_identity)
    with pytest.raises(ValueError, match="memory identity is malformed"):
        resolve_cron_memory(job)


@pytest.mark.parametrize("bad_identity", [None, False, 0, [], {}])
def test_malformed_spawn_identity_is_refused_before_queueing(bad_identity):
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    with patch("kiro_crew.subagent.check_memory_available") as host_check:
        result = manager.spawn("task", memory_store=bad_identity)
    assert result.done and result.error.startswith("memory_unavailable:")
    host_check.assert_not_called()
    assert not manager._agents


@pytest.mark.asyncio
async def test_native_windows_private_execution_refuses_before_preparing_provider(
    member_stores, monkeypatch
):
    from kiro_crew.context import prepare_store_vectors

    writer, _ = member_stores
    monkeypatch.setattr("kiro_crew.member_memory_auth.sys", SimpleNamespace(platform="win32"))
    patch_private_memory_supported(monkeypatch, value=False)
    builder = MagicMock(ensure_store=AsyncMock())
    with pytest.raises(UnknownMemoryStore, match="WSL/Linux gateway"):
        await prepare_store_vectors(builder, writer)
    builder.ensure_store.assert_not_called()
    await prepare_store_vectors(builder, "default")
    builder.ensure_store.assert_not_called()


def test_schedule_inherits_creator_member_once(tmp_path, member_stores):
    writer, reviewer = member_stores
    log = ConversationLog()
    log.update_metadata("dashboard:writer", {"memory_store": writer, "agent": "writer"})
    bind_private_session_store("dashboard:writer", writer)
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


def test_sandbox_crew_selection_does_not_open_private_memory(member_stores):
    from kiro_crew.mcp_core import _do_select_crew

    writer, _ = member_stores
    with patch(
        "kiro_crew.memory_stores._named_store_dir", side_effect=PermissionError("sandbox hidden")
    ):
        selected = json.loads(_do_select_crew("writer"))
    assert selected["bound"]["memory_store"] == writer


@pytest.mark.parametrize("origin", ["dashboard:writer", "subagent:writer-run"])
def test_sandbox_schedule_inherits_binding_without_opening_private_memory(
    tmp_path, member_stores, origin
):
    writer, _ = member_stores
    if origin.startswith("subagent:"):
        create_agent_folder("writer-run", memory_store=writer)
    else:
        ConversationLog().update_metadata(origin, {"memory_store": writer})
        bind_private_session_store(origin, writer)
    with patch(
        "kiro_crew.memory_stores._named_store_dir", side_effect=PermissionError("sandbox hidden")
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
    with pytest.raises(UnknownMemoryStore, match="global memory was not used"):
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
    log.update_metadata(key, {"memory_store": writer, "agent": recorded_agent})
    bind_private_session_store(key, writer)
    expected = "custom-template" if recorded_agent == "custom-template" else "kirocrew"
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
    with pytest.raises(UnknownMemoryStore):
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
    assert "missing-private" in refusal and "not declared" in refusal
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

    with patch("kiro_crew.context.prepare_store_vectors", new=AsyncMock()) as prepare:
        await _run_llm_callback(gateway, job, get_or_create_side_effect=acquire)
    assert seen == [writer]
    prepare.assert_awaited_once_with(
        gateway.ctx_builder, writer, session_key=gateway.sessions.get_or_create.call_args.args[0]
    )
    assert gateway.sessions.get_or_create.call_args.kwargs["agent"] == "kirocrew"


@pytest.mark.asyncio
async def test_deleted_scheduled_member_never_starts_global_provider():
    from test_cron_gateway_integration import _make_gw_for_llm, _run_llm_callback

    gateway = _make_gw_for_llm()
    job = CronJob(id="member-job", name="daily", message="report", member_id="deleted")
    with pytest.raises(ValueError, match="unknown Crew Member"):
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
    log.update_metadata(key, {"memory_store": reviewer})
    bind_private_session_store(key, reviewer)
    original = log._path(key).read_bytes()
    with pytest.raises(ValueError, match="another private store"):
        log.delete_memory_consolidation_session(key, writer)
    assert log._path(key).read_bytes() == original
