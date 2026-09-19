"""Async subagent admission captures records without moving loop-owned gates."""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from test_subagent_continuable import _manager

from kiro_crew import execution_context as execution
from kiro_crew import subagent, subagent_persistence
from kiro_crew.history import ConversationLog
from kiro_crew.subagent import SubagentInfo


@pytest.fixture
def legacy_v1_run(tmp_path, monkeypatch):
    """Write the pre-canonical state and sidecar shared by Global and named V1."""
    from kiro_crew.config import paths

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(paths, "_resolved_home", None)
    monkeypatch.setattr(subagent_persistence, "_SUBAGENTS_DIR", tmp_path / "subagents")
    (tmp_path / "config.json").write_text(
        json.dumps({"memory_stores": {"default": {}, "archive": {}}}), encoding="utf-8"
    )

    def write(store, *, mode="persistent", app=""):
        run_id = "legacy-run"
        directory = tmp_path / "subagents" / run_id
        directory.mkdir(parents=True)
        state_path = directory / "state.json"
        state_path.write_text(
            json.dumps(
                {
                    "id": run_id,
                    "task": "original",
                    "agent": "kirocrew",
                    "parent_session": "dashboard:closed",
                    "status": "done",
                    "session_id": "original-sid",
                    "provider": "acp",
                    "cwd": "",
                    "context_groups": "memory,lessons,project",
                    "memory_store": store,
                    "memory_binding_version": 2,
                }
            ),
            encoding="utf-8",
        )
        sidecar = tmp_path / "member-memory-bindings" / run_id / "memory.json"
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text(
            json.dumps({"memory_store": store, "memory_mode": mode, "app": app, "version": 2}),
            encoding="utf-8",
        )
        return run_id, state_path, sidecar

    return write


@pytest.mark.parametrize("store", ["", "archive"])
@pytest.mark.parametrize("mode", ["persistent", "incognito", "temporary"])
@pytest.mark.parametrize("app", ["", "owner-app"])
def test_legacy_v1_run_reopens_with_original_store_retention_and_app(
    legacy_v1_run, store, mode, app
):
    run_id, state_path, sidecar = legacy_v1_run(store, mode=mode, app=app)
    before = state_path.read_bytes(), sidecar.read_bytes()
    for _ in range(2):
        restored = subagent_persistence.read_run_execution(run_id)
        assert restored.member_id is None
        assert restored.store.legacy_name == store
        assert restored.memory_mode == mode
        assert restored.app == app
        assert restored.template_id == "kirocrew"
    assert (state_path.read_bytes(), sidecar.read_bytes()) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["", "archive"])
@pytest.mark.parametrize(
    "mode,app", [("persistent", ""), ("incognito", "owner-app"), ("temporary", "owner-app")]
)
async def test_cold_legacy_v1_continuation_publishes_original_execution(
    legacy_v1_run, monkeypatch, store, mode, app
):
    run_id, _state_path, sidecar = legacy_v1_run(store, mode=mode, app=app)
    before = sidecar.read_bytes()
    manager = _manager()
    manager._sessions.resumable_sid.return_value = "original-sid"
    dispatched = []

    async def spawn(**kwargs):
        captured = execution.execution_from_record(
            {"execution_context": kwargs["_execution_context"]}
        )
        await asyncio.to_thread(
            subagent_persistence.create_agent_folder,
            "follow-up",
            execution_context=captured,
            memory_mode=captured.memory_mode,
        )
        dispatched.append(kwargs)
        return SubagentInfo(id="follow-up", task="continued", execution_context=captured)

    monkeypatch.setattr(manager, "spawn_async", spawn)
    info = await manager.continue_conversation_async(run_id, "follow up", _memory_mode="persistent")
    assert info.id == "follow-up", info.error
    assert dispatched[0]["conversation_key"] == "subagent:legacy-run"
    assert dispatched[0]["app"] == app
    restored = await asyncio.to_thread(subagent_persistence.read_run_execution, info.id)
    assert restored.store.legacy_name == store
    assert restored.member_id is None
    assert restored.memory_mode == mode
    assert restored.app == app
    assert sidecar.read_bytes() == before


@pytest.mark.parametrize(
    "defect", ["missing", "malformed", "app", "mode", "mode_object", "store", "version"]
)
def test_legacy_v1_run_refuses_missing_or_invalid_ownership(legacy_v1_run, defect):
    run_id, state_path, sidecar = legacy_v1_run("archive", mode="incognito", app="owner-app")
    before = state_path.read_bytes()
    if defect == "missing":
        sidecar.unlink()
    elif defect == "malformed":
        sidecar.write_text("{", encoding="utf-8")
    else:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        field = {
            "app": "app",
            "mode": "memory_mode",
            "mode_object": "memory_mode",
            "store": "memory_store",
            "version": "version",
        }[defect]
        payload[field] = {
            "app": None,
            "mode": "invalid",
            "mode_object": [],
            "store": "",
            "version": 1,
        }[defect]
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="memory_unavailable"):
        subagent_persistence.read_run_execution(run_id)
    assert state_path.read_bytes() == before


def test_legacy_v2_run_cannot_infer_member_identity(legacy_v1_run):
    from member_memory_helpers import write_member_home

    run_id, state_path, sidecar = legacy_v1_run("member-alice")
    write_member_home(sidecar.parents[2], "alice")
    before = state_path.read_bytes(), sidecar.read_bytes()
    with pytest.raises(ValueError, match="no canonical execution"):
        subagent_persistence.read_run_execution(run_id)
    assert (state_path.read_bytes(), sidecar.read_bytes()) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied", [False, True])
@pytest.mark.parametrize("durable", [False, True])
async def test_async_spawn_captures_parent_off_loop_before_policy_gates(
    monkeypatch, supplied, durable
):
    manager = _manager()
    monkeypatch.setattr(
        type(manager._admission), "taskq_store", lambda self: object() if durable else None
    )
    parent = "dashboard:spawn-capture"
    captured = execution.ExecutionContext(
        None, execution.MemoryStoreRef("default"), "template", "kirocrew", app="owner-app"
    )
    await asyncio.to_thread(execution.bind_session_execution, parent, captured)
    manager._memory_mode_for_session = lambda _key: "persistent"
    loop_thread = threading.get_ident()
    reads = []
    original = ConversationLog.get_metadata_status

    def read(log, key):
        if key == parent:
            assert not supplied, "supplied execution re-read its parent"
            assert threading.get_ident() != loop_thread, "parent metadata read on gateway loop"
            reads.append(key)
        result = original(log, key)
        if key == parent:
            log.delete_session(parent)
        return result

    monkeypatch.setattr(ConversationLog, "get_metadata_status", read)
    policy_calls = []

    def governance(_parent, _agent, *, app):
        assert threading.get_ident() == loop_thread
        policy_calls.append(app)
        return "synthetic policy refusal"

    monkeypatch.setattr(subagent, "_vet_spawn_governance", governance)
    options = {"_execution_context": captured.to_record()} if supplied else {}
    info = await manager.spawn_async("synthetic", parent_session_key=parent, **options)
    assert info.done and "synthetic policy refusal" in info.error
    assert policy_calls == ["owner-app"]
    assert reads == ([] if supplied else [parent])


@pytest.mark.asyncio
@pytest.mark.parametrize("becomes_busy", [False, True])
async def test_cold_continuation_reads_one_snapshot_off_loop_and_rechecks_busy(
    monkeypatch, becomes_busy
):
    manager = _manager()
    conv_id = "cold-capture"
    captured = execution.ExecutionContext(
        None, execution.MemoryStoreRef("default"), "template", "kirocrew", "persistent", "owner-app"
    )
    await asyncio.to_thread(
        subagent_persistence.create_agent_folder,
        conv_id,
        task="original",
        execution_context=captured,
    )
    await asyncio.to_thread(subagent_persistence.update_state, conv_id, session_id="original-sid")
    manager._sessions.resumable_sid.return_value = "original-sid"
    loop_thread = threading.get_ident()
    original = subagent_persistence.read_state
    loop = asyncio.get_running_loop()
    reads = []

    def read(agent_id):
        if agent_id == conv_id:
            assert threading.get_ident() != loop_thread, "cold run read on gateway loop"
            reads.append(agent_id)
            if becomes_busy:
                loop.call_soon_threadsafe(
                    manager._agents.__setitem__,
                    conv_id,
                    SubagentInfo(id=conv_id, task="concurrent turn"),
                )
        return original(agent_id)

    monkeypatch.setattr(subagent_persistence, "read_state", read)
    monkeypatch.setattr(subagent, "read_state", read)
    promotions = []

    def promote(*args):
        assert threading.get_ident() == loop_thread
        promotions.append(args)

    monkeypatch.setattr(manager, "_promote_conversation", promote)
    dispatched = []

    async def spawn(**kwargs):
        assert threading.get_ident() == loop_thread
        dispatched.append(kwargs)
        return SubagentInfo(id="follow-up", task="synthetic")

    monkeypatch.setattr(manager, "spawn_async", spawn)
    info = await manager.continue_conversation_async(conv_id, "follow up", _memory_mode="incognito")
    if becomes_busy:
        assert info.done and info.error.startswith("conversation_busy")
        assert not promotions and not dispatched
        assert reads == [conv_id]
        return
    assert info.id == "follow-up"
    assert promotions
    assert reads == [conv_id]
    restored = execution.execution_from_record(
        {"execution_context": dispatched[0]["_execution_context"]}
    )
    assert restored == captured.with_mode("incognito")
    assert dispatched[0]["app"] == "owner-app"


@pytest.mark.asyncio
@pytest.mark.parametrize("cold", [False, True])
async def test_retry_retains_original_member_app_and_mode_after_parent_closes(monkeypatch, cold):
    from kiro_crew.dashboard.handlers import messaging

    parent = "dashboard:retry-parent"
    captured = execution.ExecutionContext(
        "owner-a",
        execution.MemoryStoreRef("member-a", "owner-a"),
        "member",
        "template-a",
        "incognito",
        "owner-app",
        "original-member",
    )
    await asyncio.to_thread(execution.bind_session_execution, parent, captured)
    await asyncio.to_thread(
        subagent_persistence.create_agent_folder,
        "retry-original",
        task="original",
        execution_context=captured,
    )
    old = SubagentInfo(
        id="retry-original",
        task="original",
        parent_session_key=parent,
        agent="template-a",
        done=True,
        error="original failure",
        memory_store="member-a",
        execution_context=None if cold else captured,
    )
    request = SimpleNamespace(
        get=lambda key, default=None: default,
        app={"state": SimpleNamespace(subagents=SimpleNamespace(get=lambda _id: old))},
        match_info={"agent_id": old.id},
    )
    loop_thread = threading.get_ident()
    original_read = subagent_persistence.read_state
    reads = []

    def read(agent_id):
        assert threading.get_ident() != loop_thread
        reads.append(agent_id)
        return original_read(agent_id)

    monkeypatch.setattr(subagent_persistence, "read_state", read)

    async def warm(*args):
        await asyncio.to_thread(ConversationLog().delete_session, parent)

    async def spawn(_state, task, **kwargs):
        assert kwargs["_execution_context"] == captured.to_record()
        assert kwargs["app"] == "owner-app"
        assert kwargs["_memory_mode"] == "incognito"
        return SubagentInfo(id="retried", task=task)

    monkeypatch.setattr(messaging, "warm_project_agents_for_spawn", warm)
    monkeypatch.setattr(messaging, "_spawn_on_loop", spawn)
    result = await messaging.api_spawn_retry(request)
    assert result.status == 200
    assert reads == ([old.id] if cold else [])


@pytest.mark.asyncio
async def test_async_spawn_closed_admission_never_reads_parent(monkeypatch):
    manager = _manager()
    manager._sessions.admission_closed = True

    def unreadable(*args):
        raise AssertionError("closed admission touched parent metadata")

    monkeypatch.setattr(ConversationLog, "get_metadata_status", unreadable)
    info = await manager.spawn_async("synthetic", parent_session_key="dashboard:closed")
    assert info.done and "admission is closed" in info.error


@pytest.mark.asyncio
async def test_async_capture_error_refuses_without_global_and_keeps_batch_accounting(monkeypatch):
    manager = _manager()
    manager._memory_mode_for_session = lambda _key: "incognito"
    loop_thread = threading.get_ident()

    def unreadable(*args):
        assert threading.get_ident() != loop_thread
        return {}, False

    monkeypatch.setattr(ConversationLog, "get_metadata_status", unreadable)
    info = await manager.spawn_async(
        "synthetic",
        parent_session_key="dashboard:unreadable",
        batch_id="capture-batch",
        batch_total=1,
    )
    assert info.done and info.error.startswith("memory_unavailable")
    assert "Global was not used" in info.error
    assert info.memory_mode == "incognito"
    assert manager._batch_submitted["capture-batch"] == [1, 1]
    assert not manager._agents and not manager._queue
