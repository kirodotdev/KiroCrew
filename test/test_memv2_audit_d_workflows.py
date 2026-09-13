"""Workflow leases survive every exit; private parents never become global workers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from member_memory_helpers import patch_private_memory_supported

from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.history import ConversationLog
from kiro_crew.member_memory_auth import bind_private_session_store
from kiro_crew.memory_stores import persist_member_config, provision_member_memory
from kiro_crew.workflows import agent_exec, agent_pool, service
from kiro_crew.workflows.library import WorkflowDefinitionLibrary
from kiro_crew.workflows.registry import RunHandle

SCRIPT = 'META = {"name": "lease"}\nasync def workflow(ctx):\n    return await ctx.agent("work")\n'
PARENT = "dashboard:private-workflow-parent"


class LeaseSessions:
    """Faithful semaphore lease fake: release preserves the provider unless cleanup is requested."""

    def __init__(self):
        self.providers = {}
        self.leases = {}
        self.released = []
        self.acquired = []
        self.waiting = asyncio.Event()

    async def get_or_create(self, key, **kwargs):
        semaphore = self.leases.setdefault(key, asyncio.Semaphore(1))
        if semaphore.locked():
            self.waiting.set()
        await semaphore.acquire()
        self.acquired.append(key)
        provider = self.providers.setdefault(key, SimpleNamespace(key=key, history=[]))
        return provider, len(provider.history) == 0, False

    def release(self, key, *, cleanup=False):
        assert self.leases[key].locked(), "a lease must be held before it is returned"
        self.released.append((key, cleanup))
        self.leases[key].release()
        if cleanup:
            self.providers.pop(key, None)

    async def destroy(self, key):
        self.providers.pop(key, None)
        semaphore = self.leases.get(key)
        if semaphore is not None and semaphore.locked():
            semaphore.release()


def adapter(sessions, pooled):
    if pooled:
        return agent_pool.build_pooled_agent_fn(sessions, run_id="audit-d")
    return agent_exec.build_agent_fn(sessions, run_id="audit-d"), None


@pytest.mark.asyncio
@pytest.mark.parametrize("pooled", [False, True], ids=["unpooled", "pooled"])
@pytest.mark.parametrize("exit_kind", ["success", "exception", "cancel"])
async def test_named_lease_is_returned_and_second_call_keeps_history(
    monkeypatch, pooled, exit_kind
):
    sessions = LeaseSessions()
    fn, pool = adapter(sessions, pooled)
    entered = asyncio.Event()

    async def stream(provider, prompt, **kwargs):
        provider.history.append(prompt)
        entered.set()
        if prompt == "first":
            if exit_kind == "exception":
                raise RuntimeError("step failed")
            if exit_kind == "cancel":
                await asyncio.Event().wait()
        return "reply"

    monkeypatch.setattr(agent_exec, "stream_and_collect", stream)
    monkeypatch.setattr(agent_pool, "stream_and_collect", stream)
    first = asyncio.create_task(fn("first", {"session": "chain"}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if exit_kind == "cancel":
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(first, 2)
        elif exit_kind == "exception":
            with pytest.raises(RuntimeError, match="step failed"):
                await asyncio.wait_for(first, 2)
        else:
            assert await asyncio.wait_for(first, 2) == "reply"
        provider = sessions.providers["chain"]
        assert not sessions.leases["chain"].locked()
        assert sessions.released == [("chain", False)]
        assert await asyncio.wait_for(fn("second", {"session": "chain"}), 2) == "reply"
        assert sessions.providers["chain"] is provider
        assert provider.history == ["first", "second"]
        assert sessions.released == [("chain", False), ("chain", False)]
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        if pool is not None:
            await pool.shutdown()
        await sessions.destroy("chain")


@pytest.mark.asyncio
@pytest.mark.parametrize("pooled", [False, True])
async def test_cancelled_waiter_cannot_release_another_calls_lease(monkeypatch, pooled):
    sessions = LeaseSessions()
    fn, pool = adapter(sessions, pooled)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def stream(provider, prompt, **kwargs):
        entered.set()
        await finish.wait()
        return "done"

    monkeypatch.setattr(agent_exec, "stream_and_collect", stream)
    monkeypatch.setattr(agent_pool, "stream_and_collect", stream)
    first = asyncio.create_task(fn("first", {"session": "chain"}))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(fn("second", {"session": "chain"}))
        await asyncio.wait_for(sessions.waiting.wait(), 2)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(second, 2)
        assert sessions.leases["chain"].locked()
        assert sessions.released == []
        finish.set()
        assert await asyncio.wait_for(first, 2) == "done"
        assert sessions.released == [("chain", False)]
    finally:
        finish.set()
        tasks = [first] + ([second] if second is not None else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if pool is not None:
            await pool.shutdown()
        await sessions.destroy("chain")


@pytest.fixture
def private_parent(monkeypatch):
    patch_private_memory_supported(monkeypatch)
    cfg = KiroCrewConfig.load()
    cfg.agents["workflow-owner"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    store = provision_member_memory(cfg, "workflow-owner")
    persist_member_config(cfg, "workflow-owner", create=True)
    ConversationLog().update_metadata(PARENT, {"memory_store": store})
    bind_private_session_store(PARENT, store)
    return store


async def invoke(svc, entry, parent):
    if entry == "author":
        return await svc.author("draft work", author=parent)
    if entry == "source":
        return await svc.start(SCRIPT, session_key=parent)
    if entry == "intent":
        return await svc.start_from_intent("draft work", session_key=parent)
    if entry in {"saved", "saved-task"}:
        task_plan = entry == "saved-task"
        saved = svc.save_definition(
            "agents:\n  worker:\n    prompt: work\n" if task_plan else SCRIPT,
            source_format="task-plan" if task_plan else "python",
        )
        assert saved["ok"], saved
        return await svc.start_definition(saved["definition"]["id"], session_key=parent)
    prior = RunHandle(
        run_id="wf_000100",
        name="prior",
        source=SCRIPT,
        session_key=parent,
        status="finished",
        agent_results={0: "prior answer"},
    )
    svc.registry.register(prior)
    return await svc.rerun_subtree(
        prior.run_id,
        from_index=1,
        source=SCRIPT.replace('"work"', '"new work"') if entry == "edited-rerun" else None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("pooled", [False, True], ids=["unpooled", "pooled"])
@pytest.mark.parametrize(
    "entry", ["author", "source", "intent", "saved", "saved-task", "rerun", "edited-rerun"]
)
async def test_private_parent_refuses_before_author_or_worker_creation(
    monkeypatch, tmp_path, private_parent, pooled, entry
):
    sessions = LeaseSessions()
    task_runner = SimpleNamespace(
        start_workflow_definition=AsyncMock(return_value={"run_id": "task"})
    )
    svc = service.WorkflowService(
        sessions=sessions,
        pool_agents=pooled,
        persist=False,
        definition_library=WorkflowDefinitionLibrary(tmp_path / "library"),
        task_runner=task_runner,
    )
    stream = AsyncMock(return_value=SCRIPT)
    for module in (service, agent_exec, agent_pool):
        monkeypatch.setattr(module, "stream_and_collect", stream)
    result = await invoke(svc, entry, PARENT)
    # Drain any incorrectly admitted run too, so a red test never leaks work.
    for snapshot in svc.list_runs():
        task = svc.registry.get(snapshot["run_id"]).task
        if task is not None:
            await asyncio.wait_for(task, 3)
    assert "run_id" not in result
    assert result["code"] == "workflow_private_memory_unsupported"
    assert "private memory" in result["error"]
    assert not sessions.acquired
    assert stream.await_count == 0
    task_runner.start_workflow_definition.assert_not_awaited()
    assert len(svc.list_runs()) == (1 if "rerun" in entry else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("pooled", [False, True])
@pytest.mark.parametrize(
    "entry", ["author", "source", "intent", "saved", "saved-task", "rerun", "edited-rerun"]
)
async def test_global_parent_still_runs_when_private_members_exist(
    monkeypatch, tmp_path, private_parent, pooled, entry
):
    sessions = LeaseSessions()
    task_runner = SimpleNamespace(
        start_workflow_definition=AsyncMock(return_value={"run_id": "task"})
    )
    svc = service.WorkflowService(
        sessions=sessions,
        pool_agents=pooled,
        persist=False,
        definition_library=WorkflowDefinitionLibrary(tmp_path / "library"),
        task_runner=task_runner,
    )
    stream = AsyncMock(return_value=SCRIPT)
    for module in (service, agent_exec, agent_pool):
        monkeypatch.setattr(module, "stream_and_collect", stream)
    result = await invoke(svc, entry, "dashboard:global-workflow-parent")
    for snapshot in svc.list_runs():
        task = svc.registry.get(snapshot["run_id"]).task
        if task is not None:
            await asyncio.wait_for(task, 3)
    assert "error" not in result
    assert result.get("ok") or result.get("run_id")


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["corrupt-binding", "missing-binding", "metadata-downgrade"])
async def test_unreadable_private_identity_never_falls_back(private_parent, damage):
    from kiro_crew.member_memory_auth import _session_binding_path

    path = _session_binding_path(PARENT)
    if damage == "corrupt-binding":
        path.write_text("invalid", encoding="utf-8")
    elif damage == "missing-binding":
        path.unlink()
    else:
        await asyncio.to_thread(
            ConversationLog().update_metadata, PARENT, {"memory_store": "default"}
        )
    sessions = LeaseSessions()
    svc = service.WorkflowService(sessions=sessions, persist=False)
    result = await svc.start(SCRIPT, session_key=PARENT)
    assert result["code"] == "workflow_memory_unavailable"
    assert not sessions.acquired
    assert svc.list_runs() == []


@pytest.mark.asyncio
async def test_author_identity_cannot_be_hidden_by_global_result_route(private_parent):
    svc = service.WorkflowService(sessions=LeaseSessions(), persist=False)
    result = await svc.start(SCRIPT, author=PARENT, session_key="dashboard:global")
    assert result["code"] == "workflow_private_memory_unsupported"
    assert svc.list_runs() == []


@pytest.mark.asyncio
async def test_restored_private_run_cannot_resume_on_global_workers(tmp_path, private_parent):
    from kiro_crew.workflows.store import WorkflowRunStore

    store = WorkflowRunStore(tmp_path / "workflows")
    before = service.WorkflowService(sessions=LeaseSessions(), store=store)
    before.registry.register(
        RunHandle(
            run_id="wf_000100",
            name="old-private-run",
            source=SCRIPT,
            session_key=PARENT,
            status="finished",
            agent_results={0: "private-result"},
        )
    )
    sessions = LeaseSessions()
    after = service.WorkflowService(sessions=sessions, store=store)
    result = await after.rerun_subtree("wf_000100", from_index=1)
    assert result["code"] == "workflow_private_memory_unsupported"
    assert not sessions.acquired
    assert after.registry.get("wf_000100").agent_results == {0: "private-result"}
    assert len(after.list_runs()) == 1


@pytest.mark.asyncio
async def test_binding_resolution_runs_off_the_event_loop(monkeypatch):
    import threading

    loop_thread = threading.get_ident()
    called = []

    def resolve(key):
        called.append((key, threading.get_ident()))
        return "private-store"

    monkeypatch.setattr(service, "private_memory_store_for_session", resolve)
    svc = service.WorkflowService(sessions=LeaseSessions(), persist=False)
    result = await svc.start(SCRIPT, session_key=PARENT)
    assert result["code"] == "workflow_private_memory_unsupported"
    assert called and called[0][0] == PARENT
    assert called[0][1] != loop_thread
