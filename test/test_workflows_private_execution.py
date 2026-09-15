"""Real workflow/service/session/store integration with only model I/O replaced.

OS capability is pinned in this in-process suite. The namespace-enabled gateway
E2E is a separate requirement; these tests do not claim kernel/MCP proof coverage.
"""

import asyncio
from types import SimpleNamespace

import pytest
from member_memory_helpers import patch_private_memory_supported

from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.context import ContextBuilder, release_cached_memory_store, store_of_session
from kiro_crew.history import ConversationLog
from kiro_crew.member_memory_auth import (
    bind_private_session_store,
    private_memory_store_for_session,
)
from kiro_crew.memory_stores import persist_member_config, provision_member_memory
from kiro_crew.session import SessionManager
from kiro_crew.workflow_memory import (
    WorkflowMemoryError,
    WorkflowScope,
    authorize_run,
    binding_path,
)
from kiro_crew.workflows import agent_exec, agent_pool, service
from kiro_crew.workflows.store import WorkflowRunStore

SCRIPT = """META = {"name": "private execution"}
async def workflow(ctx):
    first = await ctx.parallel([lambda: ctx.agent("left"), lambda: ctx.agent("right")])
    second = await ctx.agent("next")
    named = await ctx.agent("chain1", session="dashboard:bob")
    last = await ctx.agent("chain2", session="dashboard:bob")
    return [first, second, named, last]
"""


class Model:
    def __init__(self, key):
        self.key = key
        self._private_memory = bool(private_memory_store_for_session(key))
        self.prompts = []
        self.alive = True
        self.cwd = ""

    async def start(self):
        pass

    async def shutdown(self):
        self.alive = False

    async def new_conversation(self):
        self.prompts.clear()

    def is_process_alive(self):
        return self.alive

    def context_usage_pct(self):
        return 0.0

    def has_active_turn(self):
        return False


@pytest.fixture
def world(monkeypatch, event_loop, tmp_path):
    from pathlib import Path

    from kiro_crew import context

    patch_private_memory_supported(monkeypatch)
    home = tmp_path / "host-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("KIRO_HOME", str(home / ".kiro"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # Workflow routing is the subject, not the installed persona/skill catalog.
    # Keep a real prompt file and assert delivery instead of repeatedly scanning
    # the operator's steering and the growing product prompt under coverage.
    prompt = home / "workflow-persona.txt"
    prompt.write_text("WORKFLOW_SYSTEM_MARKER", encoding="utf-8")
    monkeypatch.setattr(context, "_prompt_path", lambda **kwargs: prompt)
    templates = home / ".kiro" / "agents"
    templates.mkdir(parents=True)
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", templates)
    (templates / "kirocrew-lite.json").write_text(
        '{"name": "kirocrew-lite", "prompt": "Author a valid workflow."}',
        encoding="utf-8",
    )
    from kiro_crew.skills import SkillsLoader

    skill_root = home / "skills"
    skill = skill_root / "workflow-fixture"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: workflow-fixture\ndescription: WORKFLOW_SKILL_MARKER\n---\n"
        "Return the owning member's marker.\n",
        encoding="utf-8",
    )
    log = ConversationLog()
    builder = ContextBuilder(
        conversation_log=log, skills=SkillsLoader(skill_root, install_builtins=False)
    )
    builder.memory.init()
    stores = {}
    for member in ("alice", "bob"):
        cfg = KiroCrewConfig.load()
        cfg.agents[member] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        stores[member] = provision_member_memory(cfg, member)
        persist_member_config(cfg, member, create=True)
        key = f"dashboard:{member}"
        bind_private_session_store(key, stores[member])
        log.update_metadata(key, {"memory_store": stores[member]})
        vectors = event_loop.run_until_complete(builder.ensure_store(stores[member]))
        vectors.write_lesson(f"{member.upper()}_PRIVATE_MARKER", "tool", None, "test")
    models = []

    def factory(key, **kwargs):
        model = Model(key)
        models.append(model)
        return model

    sessions = SessionManager(KiroCrewConfig.load(), provider_factory=factory)

    async def complete(model, prompt, **kwargs):
        model.prompts.append(prompt)
        store = store_of_session(log, model.key)
        if store:
            owner = next(name for name, value in stores.items() if value == store)
            history = "\n".join(model.prompts)
            if not model.key.startswith("wf-author:"):
                assert "WORKFLOW_SYSTEM_MARKER" in history
                assert "WORKFLOW_SKILL_MARKER" in history
            assert f"{owner.upper()}_PRIVATE_MARKER" in history
            other = "BOB" if owner == "alice" else "ALICE"
            assert f"{other}_PRIVATE_MARKER" not in history
        await asyncio.sleep(0)
        return SCRIPT if model.key.startswith("wf-author:") else store or "V1"

    for module in (agent_exec, agent_pool, service):
        monkeypatch.setattr(module, "stream_and_collect", complete)
    try:
        yield SimpleNamespace(
            builder=builder, log=log, stores=stores, sessions=sessions, models=models
        )
    finally:
        event_loop.run_until_complete(sessions.close_all())
        for store in stores.values():
            release_cached_memory_store(store)


async def finished(svc, started):
    assert "run_id" in started, started
    handle = svc.registry.get(started["run_id"])
    done, _ = await asyncio.wait({handle.task}, timeout=10)
    if not done:
        # Capture only code locations, never coroutine locals or private payloads.
        waits = []
        for task in asyncio.all_tasks():
            chain = []
            awaitable = task.get_coro()
            while awaitable is not None:
                code = getattr(awaitable, "cr_code", None)
                if code is not None:
                    chain.append(code.co_name)
                awaitable = getattr(awaitable, "cr_await", None)
            waits.append(chain)
        from kiro_crew.testing.workflow_memory_scenario import progress_summary

        progress = progress_summary(handle.snapshot(include_events=True))
        handle.task.cancel()
        await asyncio.gather(handle.task, return_exceptions=True)
        pytest.fail(f"Workflow exceeded 10s: progress={progress}; waits={waits}")
    await handle.task
    assert handle.status == "finished", handle.error
    return handle


@pytest.mark.asyncio
@pytest.mark.parametrize("pooled", [False, True])
@pytest.mark.parametrize("entry", ["source", "intent", "definition"])
async def test_private_execution_keeps_scope_on_every_worker(world, pooled, entry):
    svc = service.WorkflowService(
        sessions=world.sessions,
        context_builder=world.builder,
        pool_agents=pooled,
        persist=False,
    )
    if entry == "source":
        started = await svc.start(SCRIPT, session_key="dashboard:alice")
    elif entry == "intent":
        started = await svc.start_from_intent("private work", session_key="dashboard:alice")
    else:
        saved = svc.save_definition(SCRIPT)
        started = await svc.start_definition(
            saved["definition"]["id"], session_key="dashboard:alice"
        )
    handle = await finished(svc, started)
    assert handle.result == [[world.stores["alice"]] * 2] + [world.stores["alice"]] * 3
    assert world.models
    assert all(
        store_of_session(world.log, model.key) == world.stores["alice"] for model in world.models
    )
    assert not world.sessions.has_session("dashboard:bob")
    with pytest.raises(WorkflowMemoryError):
        await authorize_run(handle.run_id, "dashboard:bob")
    with pytest.raises(WorkflowMemoryError):
        await authorize_run(handle.run_id, "dashboard:global")


@pytest.mark.asyncio
async def test_private_author_and_restart_rerun_use_protected_binding(world, tmp_path):
    store = WorkflowRunStore(tmp_path / "workflow-records")
    svc = service.WorkflowService(
        sessions=world.sessions, context_builder=world.builder, store=store
    )
    assert (await svc.author("private author", author="dashboard:alice"))["ok"]
    first = await finished(svc, await svc.start(SCRIPT, session_key="dashboard:alice"))
    assert not list(store.runs_dir.glob("*.json")), "private content leaked into ordinary runs"
    restored = service.WorkflowService(
        sessions=world.sessions, context_builder=world.builder, store=store
    )
    assert restored.registry.get(first.run_id) is not None
    denied = await restored.rerun_subtree(first.run_id, caller_session="dashboard:bob")
    assert denied["code"] == "workflow_memory_unavailable"
    rerun = await finished(
        restored, await restored.rerun_subtree(first.run_id, 2, caller_session="dashboard:alice")
    )
    assert rerun.result == first.result
    binding_path(first.run_id).unlink()
    denied = await restored.rerun_subtree(first.run_id, caller_session="dashboard:alice")
    assert denied["code"] == "workflow_memory_unavailable"


@pytest.mark.asyncio
async def test_published_scope_cannot_rebind_and_missing_record_refuses(world):
    scope = await WorkflowScope.admit("wf_test", world.builder, "dashboard:alice")
    with pytest.raises(WorkflowMemoryError):
        await WorkflowScope.admit("wf_test", world.builder, "dashboard:bob")
    binding_path(scope.run_id).write_text("broken", encoding="utf-8")
    before = len(world.models)
    with pytest.raises(WorkflowMemoryError):
        await scope.prepare(world.builder, scope.worker_key("unused"))
    assert len(world.models) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("mid_run", [False, True])
@pytest.mark.parametrize("pooled", [False, True])
async def test_invalidated_run_fails_without_global_worker(world, monkeypatch, mid_run, pooled):
    svc = service.WorkflowService(
        sessions=world.sessions, context_builder=world.builder, pool_agents=pooled, persist=False
    )
    started = await svc.start(SCRIPT, session_key="dashboard:alice")
    assert "run_id" in started, started
    run_id = started["run_id"]
    path = binding_path(run_id)
    if mid_run:

        async def revoke(model, prompt, **kwargs):
            path.write_text("invalid", encoding="utf-8")
            return "already completed private work"

        for module in (agent_exec, agent_pool):
            monkeypatch.setattr(module, "stream_and_collect", revoke)
    else:
        path.unlink()
    handle = svc.registry.get(run_id)
    await asyncio.wait_for(handle.task, 10)
    assert handle.status == "failed"
    assert "Global V1 was not used" in handle.error
    if not mid_run:
        assert world.models == []
    assert all(model._private_memory for model in world.models)


@pytest.mark.asyncio
async def test_authenticated_scope_cannot_change_before_service_admission(world):
    svc = service.WorkflowService(
        sessions=world.sessions, context_builder=world.builder, persist=False
    )
    for expected in ("", world.stores["bob"]):
        result = await svc.start(SCRIPT, session_key="dashboard:alice", expected_store=expected)
        assert result["code"] == "workflow_memory_unavailable"
    assert not world.models
    assert not svc.list_runs()


@pytest.mark.asyncio
async def test_private_persistence_acl_and_eviction_run_off_loop(world, tmp_path, monkeypatch):
    import json
    import threading

    from kiro_crew import platform_compat
    from kiro_crew.workflow_memory import private_payload_path, read_binding
    from kiro_crew.workflows.store import WorkflowRunStore

    loop_thread = threading.get_ident()
    seen = []
    acl_threads = []
    store = WorkflowRunStore(tmp_path / "public-workflows")
    save = store.save
    delete = store.delete
    restrict = platform_compat.restrict_dir_to_owner

    def observed_save(rid, payload):
        seen.append(("save", threading.get_ident(), payload["status"], len(payload["events"])))
        return save(rid, payload)

    def observed_delete(rid):
        seen.append(("delete", threading.get_ident(), "", 0))
        return delete(rid)

    def observed_restrict(path):
        if path == private_payload_path("probe").parent:
            acl_threads.append(threading.get_ident())
        return restrict(path)

    monkeypatch.setattr(store, "save", observed_save)
    monkeypatch.setattr(store, "delete", observed_delete)
    monkeypatch.setattr(platform_compat, "restrict_dir_to_owner", observed_restrict)
    svc = service.WorkflowService(
        sessions=world.sessions, context_builder=world.builder, store=store
    )
    svc.registry._max_runs = 1
    script = """META = {"name": "private checkpoint"}
async def workflow(ctx):
    ctx.log("one")
    ctx.log("two")
    ctx.log("three")
    ctx.log("four")
    return "private result"
"""
    first = await svc.start(script, session_key="dashboard:alice")
    rid = first["run_id"]
    await svc.registry.get(rid).task
    assert read_binding(rid, required=True)["memory_store"] == world.stores["alice"]
    payload = json.loads(private_payload_path(rid).read_text())
    assert payload["result"] == "private result"
    assert not store.runs_dir.exists()
    second = await svc.start(script, session_key="dashboard:alice")
    await svc.registry.get(second["run_id"]).task
    assert not private_payload_path(rid).exists()
    assert any(op == "delete" for op, *_ in seen)
    assert any(status == "running" and count >= 5 for _, _, status, count in seen)
    assert all(thread != loop_thread for _, thread, _, _ in seen)
    assert acl_threads and all(thread != loop_thread for thread in acl_threads)
