"""Workflow admission owns one off-loop capture, independent of parent lifetime."""

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef, bind_session_execution
from kiro_crew.history import ConversationLog
from kiro_crew.workflow_memory import WorkflowScope
from kiro_crew.workflows.service import SOURCE_FORMAT_TASK_PLAN, WorkflowService
from kiro_crew.workflows.store import WorkflowRunStore

SCRIPT = 'META = {"name": "capture"}\nasync def workflow(ctx):\n    return "done"\n'


def gateway_context():
    from kiro_crew.dashboard.handlers._shared import (
        live_session_memory_mode,
        resolve_session_memory_mode,
    )

    context = SimpleNamespace(_session_memory_modes={})
    state = SimpleNamespace(
        context_builder=context, _slots={}, _restricted_keys=set(), subagents=None
    )
    context.memory_mode_for_session = lambda key: resolve_session_memory_mode(state, key)
    context.live_memory_mode_for_session = lambda key: live_session_memory_mode(state, key)
    return context, state


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied", [False, True])
@pytest.mark.parametrize("entry", ["scope", "host", "script", "intent", "definition", "task-plan"])
async def test_workflow_capture_reads_off_loop_once_and_outlives_parent(
    tmp_path, monkeypatch, supplied, entry
):
    parent = "dashboard:workflow-capture-parent"
    execution = ExecutionContext(
        "owner-a", MemoryStoreRef("member-a", "owner-a"), "member", "template-a", app="app-a"
    )
    await asyncio.to_thread(bind_session_execution, parent, execution)
    log = ConversationLog()
    original_read = ConversationLog.get_metadata_status
    loop_thread = threading.get_ident()
    reads = []

    def read(self, key):
        if key == parent:
            reads.append(threading.get_ident())
            assert reads[-1] != loop_thread, "persistent metadata read blocked the gateway loop"
            assert not supplied, "a supplied carrier must not re-read its parent"
            assert len(reads) == 1, "admission reinterpreted its captured parent"
        return original_read(self, key)

    monkeypatch.setattr(ConversationLog, "get_metadata_status", read)
    removed = []

    def remove_parent():
        log.delete_session(parent)
        removed.append(parent)

    from kiro_crew import workflow_memory

    original_capture = workflow_memory.capture_execution

    def capture(*keys):
        captured = original_capture(*keys)
        remove_parent()
        return captured

    monkeypatch.setattr(workflow_memory, "capture_execution", capture)
    if supplied:
        await asyncio.to_thread(remove_parent)
    context, _state = gateway_context()
    options = {"execution_context": execution} if supplied else {}
    if entry == "scope":
        scope = await WorkflowScope.admit("wf_000001", context, parent, **options)
        assert scope.execution_context == execution
    else:
        store = WorkflowRunStore(tmp_path / "runs")
        svc = await WorkflowService.create(
            sessions=SimpleNamespace(), context_builder=context, store=store
        )
        carried = []

        async def run_background(_source, **kwargs):
            carried.append(kwargs["execution_context"])
            return True

        def runner(_run_id, **kwargs):
            assert kwargs["memory_scope"].execution_context == execution
            return SimpleNamespace(run_background=run_background)

        monkeypatch.setattr(svc, "_runner", runner)
        if entry == "host":
            run_id = await svc.begin_host_run(
                name="capture", source_format="python", driver="test", session_key=parent, **options
            )
            assert svc.registry.get(run_id).execution_context == execution
            saved = await asyncio.to_thread(store.load_all)
            assert saved[0]["execution_context"] == execution.to_record()
        elif entry == "script":
            assert "run_id" in await svc.start(SCRIPT, session_key=parent, **options)
        elif entry == "intent":
            assert "run_id" in await svc.start_from_intent("do work", session_key=parent, **options)
        else:

            def definition(_ref):
                # The definition read is another suspension after capture.
                log.delete_session(parent)
                removed.append(parent)
                return dict(
                    id="definition-a",
                    slug="capture",
                    revision=1,
                    source=SCRIPT,
                    format=SOURCE_FORMAT_TASK_PLAN if entry == "task-plan" else "python",
                )

            async def task_definition(_definition, **kwargs):
                carried.append(kwargs["execution_context"])
                return {"run_id": "wf_000002"}

            monkeypatch.setattr(svc, "get_definition", definition)
            svc._task_runner = SimpleNamespace(start_workflow_definition=task_definition)
            assert "run_id" in await svc.start_definition("capture", session_key=parent, **options)
        if entry != "host":
            assert carried == [execution]
    assert len(reads) == (0 if supplied else 1)
    assert removed
    assert not await asyncio.to_thread(log._path(parent).exists)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["persistent", "incognito", "temporary"])
async def test_captured_scope_survives_absent_parent_with_real_gateway_resolver(mode):
    parent = "dashboard:closed-parent"
    context, _state = gateway_context()
    execution = ExecutionContext(
        "owner-a", MemoryStoreRef("member-a", "owner-a"), "member", "a", mode, "app-a"
    )
    with pytest.raises(ValueError, match="memory mode is unavailable"):
        await context.memory_mode_for_session(parent)
    scope = await WorkflowScope.admit("wf_closed", context, parent, execution_context=execution)
    assert scope.execution_context == execution


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original,live,inherited,expected",
    [
        ("incognito", "persistent", (), "incognito"),
        ("temporary", "persistent", (), "temporary"),
        ("persistent", "incognito", (), "incognito"),
        ("incognito", "temporary", (), "temporary"),
        ("persistent", "not-ready", (), "temporary"),
        ("persistent", "persistent", ("incognito",), "incognito"),
        ("incognito", "persistent", ("temporary",), "temporary"),
    ],
)
async def test_scope_combines_captured_live_and_inherited_restrictions(
    original, live, inherited, expected
):
    parent = "dashboard:live-parent"
    context, state = gateway_context()
    if live == "not-ready":
        state.subagents = SimpleNamespace(
            running=[SimpleNamespace(conversation_key=parent, _memory_mode_ready=False)]
        )
    else:
        state._slots["live-parent"] = SimpleNamespace(
            blocks_reads=live == "temporary", is_restricted=live != "persistent"
        )
    execution = ExecutionContext(
        "owner-a", MemoryStoreRef("member-a", "owner-a"), "member", "a", original, "app-a"
    )
    scope = await WorkflowScope.admit(
        "wf_live", context, parent, execution_context=execution, inherited_modes=inherited
    )
    assert scope.execution_context == replace(execution, memory_mode=expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("live", ["incognito", "temporary"])
@pytest.mark.parametrize("entry", ["scope", "host", "script", "intent", "definition", "author"])
async def test_admission_snapshots_live_restriction_before_capture_yields(
    tmp_path, monkeypatch, live, entry
):
    from kiro_crew import workflow_memory

    parent = "dashboard:closing-parent"
    context, state = gateway_context()
    state._slots["closing-parent"] = SimpleNamespace(
        blocks_reads=live == "temporary", is_restricted=True
    )
    execution = ExecutionContext(
        "owner-a", MemoryStoreRef("member-a", "owner-a"), "member", "a", app="app-a"
    )
    await asyncio.to_thread(bind_session_execution, parent, execution)
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_capture = workflow_memory.capture_execution

    def capture(*keys):
        captured = original_capture(*keys)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5), "capture was not released"
        return captured

    monkeypatch.setattr(workflow_memory, "capture_execution", capture)
    carried = []
    if entry == "scope":

        async def invoke():
            scope = await WorkflowScope.admit("wf_closing", context, parent)
            carried.append(scope.execution_context)

    else:
        svc = await WorkflowService.create(
            sessions=SimpleNamespace(),
            context_builder=context,
            store=WorkflowRunStore(tmp_path / "runs"),
        )

        async def run_background(_source, **kwargs):
            carried.append(kwargs["execution_context"])
            return True

        monkeypatch.setattr(
            svc, "_runner", lambda *args, **kwargs: SimpleNamespace(run_background=run_background)
        )
        if entry == "definition":
            monkeypatch.setattr(
                svc,
                "get_definition",
                lambda ref: dict(
                    id="saved", slug="saved", revision=1, source=SCRIPT, format="python"
                ),
            )
        if entry == "author":

            class AuthorReached(Exception):
                pass

            async def prepare(scope, _context, _key):
                carried.append(scope.execution_context)
                raise AuthorReached

            monkeypatch.setattr(WorkflowScope, "prepare", prepare)
            svc._sessions.destroy = AsyncMock()

        async def invoke():
            if entry == "host":
                run_id = await svc.begin_host_run(
                    name="closing", source_format="python", driver="test", session_key=parent
                )
                carried.append(svc.registry.get(run_id).execution_context)
            elif entry == "script":
                assert "run_id" in await svc.start(SCRIPT, session_key=parent)
            elif entry == "intent":
                assert "run_id" in await svc.start_from_intent("work", session_key=parent)
            elif entry == "definition":
                assert "run_id" in await svc.start_definition("saved", session_key=parent)
            else:
                with pytest.raises(AuthorReached):
                    await svc.author("work", author=parent)

    pending = asyncio.create_task(invoke())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        state._slots.clear()
    finally:
        release.set()
        await asyncio.wait_for(pending, timeout=5)
    assert carried == [replace(execution, memory_mode=live)]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["live-port", "live-map", "inherited"])
async def test_scope_refuses_invalid_retention_inputs(source):
    from kiro_crew.memory_stores import UnknownMemoryStore

    parent = "dashboard:invalid-mode"
    context, _state = gateway_context()
    if source == "live-port":
        context.live_memory_mode_for_session = lambda key: "invalid"
    elif source == "live-map":
        context._session_memory_modes[parent] = "invalid"
    execution = ExecutionContext(None, MemoryStoreRef("default"), "template", "a")
    with pytest.raises(UnknownMemoryStore, match="invalid privacy mode"):
        await WorkflowScope.admit(
            "wf_invalid",
            context,
            parent,
            execution_context=execution,
            inherited_modes=("invalid",) if source == "inherited" else (),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["conflicting-parent", "unreadable-parent", "wrong-store"])
async def test_workflow_capture_refuses_invalid_parent_without_global_fallback(
    tmp_path, monkeypatch, failure
):
    from kiro_crew.workflow_memory import WorkflowMemoryError

    parent = "dashboard:workflow-invalid-parent"
    author = "dashboard:workflow-author"
    execution = ExecutionContext("owner-a", MemoryStoreRef("member-a", "owner-a"), "member", "a")
    other = ExecutionContext("owner-b", MemoryStoreRef("member-b", "owner-b"), "member", "b")
    await asyncio.to_thread(bind_session_execution, parent, execution)
    await asyncio.to_thread(
        bind_session_execution, author, other if failure == "conflicting-parent" else execution
    )
    if failure == "unreadable-parent":
        await asyncio.to_thread(
            ConversationLog()._path(parent).write_bytes, b"not valid metadata\n"
        )
    svc = await WorkflowService.create(
        sessions=SimpleNamespace(),
        context_builder=SimpleNamespace(),
        store=WorkflowRunStore(tmp_path / "runs"),
    )

    def no_runner(*args, **kwargs):
        raise AssertionError("refused admission started a runtime")

    monkeypatch.setattr(svc, "_runner", no_runner)
    options = {"expected_store": "member-b"} if failure == "wrong-store" else {}
    result = await svc.start(SCRIPT, session_key=parent, author=author, **options)
    assert result["code"] == "workflow_memory_unavailable"
    assert result["admission_rejected"]
    assert "Global fallback" in result["error"]
    if failure == "conflicting-parent":
        with pytest.raises(WorkflowMemoryError, match="must match"):
            await WorkflowScope.admit("wf_000001", SimpleNamespace(), parent, author)
