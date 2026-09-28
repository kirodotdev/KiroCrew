"""Remaining private workflow paths with real schedulers and owned stores.

Only model output and the unit fixture's OS availability are synthetic.
"""

import asyncio

import pytest
from test_workflows_private_execution import world as _world
from test_workflows_receipts import ReceiptModel

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.config import KiroCrewConfig
from kiro_crew.context import store_of_session
from kiro_crew.member_memory_auth import private_memory_store_for_session
from kiro_crew.providers.base import LLMEvent
from kiro_crew.session import SessionManager
from kiro_crew.taskrunner import TaskRunner
from kiro_crew.workflow_memory import WorkflowScope
from kiro_crew.workflows.service import WorkflowService

world = _world


@pytest.mark.asyncio
@pytest.mark.parametrize("author_only", [False, True])
async def test_saved_task_plan_executes_on_real_private_taskrunner(
    world, tmp_path_factory, author_only, caplog, monkeypatch
):
    import logging

    caplog.set_level(logging.INFO)
    providers = []

    class TaskModel(ReceiptModel):
        def __init__(self, key):
            super().__init__()
            self.key = key
            self._private_memory = bool(private_memory_store_for_session(key))
            self.alive = True

        def is_process_alive(self):
            return self.alive

        def has_active_turn(self):
            return False

        async def shutdown(self):
            self.alive = False

        async def raw(self, message):
            self.sent.append(message)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text='{"ok": true}')
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    def factory(key, **kwargs):
        model = TaskModel(key)
        providers.append(model)
        return model

    from kiro_crew.config.sections import WorkspaceConfig

    project_dir = tmp_path_factory.mktemp("workflow-project")
    config = KiroCrewConfig.load()
    config.workspaces["workflow-project"] = WorkspaceConfig(dir=str(project_dir))
    config.save()
    sessions = SessionManager(config, provider_factory=factory)

    async def model_task_session(parent_key, key, **kwargs):
        # Replace native runtime allocation only; real session creation, member
        # context construction, scheduler execution and review remain exercised.
        assert store_of_session(world.log, key) == store_of_session(world.log, parent_key)
        return await sessions.get_or_create(key, **kwargs)

    monkeypatch.setattr(sessions, "open_task_session", model_task_session)
    service = WorkflowService(sessions=sessions, context_builder=world.builder, persist=False)
    runner = TaskRunner(
        sessions=sessions,
        context_builder=world.builder,
        conversation_log=world.log,
        auto_test=False,
        auto_commit=False,
        work_dir=project_dir,
        max_parallel_steps=1,
        workflow_service=service,
    )
    service.attach_task_runner(runner)
    try:
        saved = service.save_definition(
            "agents:\n  PRIVATE_DIAGNOSTIC_TITLE:\n    prompt: Return a confirmation without editing files\n",
            source_format="task-plan",
        )
        started = await service.start_definition(
            saved["definition"]["id"],
            author="dashboard:alice",
            session_key="" if author_only else "dashboard:alice",
        )
        assert "task_id" in started, started
        await asyncio.wait_for(runner._tasks[started["task_id"]], 20)
        run = runner._runs[started["task_id"]]
        assert run.status == "completed", run.error
        assert providers and any(provider.sent for provider in providers)
        assert all(
            store_of_session(world.log, provider.key) == world.stores["alice"]
            for provider in providers
        )
        scope = await WorkflowScope.restore(
            started["run_id"], record=service.registry.get(started["run_id"]).to_store_json()
        )
        assert scope.store == world.stores["alice"]
        assert scope.execution_context == run.execution_context
    finally:
        for task in list(runner._tasks.values()):
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await sessions.close_all()


@pytest.mark.asyncio
async def test_private_pool_overflow_and_replacement_keep_scope(world, monkeypatch):
    from kiro_crew.workflows.agent_pool import build_pooled_agent_fn

    scope = await WorkflowScope.admit("wf_overflow", world.builder, "dashboard:alice")
    execute, pool = build_pooled_agent_fn(
        world.sessions,
        run_id=scope.run_id,
        memory_scope=scope,
        context_builder=world.builder,
        max_workers=1,
        max_identities=1,
    )
    try:
        assert await execute("first", {"model": "model-one"}) == scope.store
        first = world.models[-1]
        first.alive = False
        assert await execute("replacement", {"model": "model-one"}) == scope.store
        replacement = world.models[-1]
        assert replacement is not first
        assert await execute("overflow", {"model": "model-two"}) == scope.store
        assert any(model.key.startswith("wf-unpooled:") for model in world.models)

        async def failed_reset():
            raise RuntimeError("deterministic model reset failure")

        monkeypatch.setattr(replacement, "new_conversation", failed_reset)
        assert await execute("hard reset", {"model": "model-one"}) == scope.store
        assert world.models[-1] is not replacement
        assert all(store_of_session(world.log, model.key) == scope.store for model in world.models)
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("pooled", [False, True])
async def test_unavailable_learned_memory_keeps_member_persona_and_routing(world, pooled):
    from test_workflows_private_execution import SCRIPT, finished

    from kiro_crew.context import release_cached_memory_store
    from kiro_crew.memory_stores import resolve_store_path

    store = world.stores["alice"]
    await asyncio.to_thread(release_cached_memory_store, store)
    path = await asyncio.to_thread(resolve_store_path, store)
    await asyncio.to_thread(path.unlink)
    service = WorkflowService(
        sessions=world.sessions, context_builder=world.builder, pool_agents=pooled, persist=False
    )
    run = await finished(service, await service.start(SCRIPT, session_key="dashboard:alice"))
    assert run.status == "finished"
    assert run.execution_context.store.store_id == store
    assert world.models and all(
        store_of_session(world.log, model.key) == store for model in world.models
    )
    assert all(
        "ALICE_PRIVATE_MARKER" in prompt for model in world.models for prompt in model.prompts
    )


@pytest.mark.asyncio
async def test_http_controls_keep_original_execution_under_ordinary_auth(world, monkeypatch):
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from test_workflows_private_execution import SCRIPT, finished

    from kiro_crew import member_memory_auth
    from kiro_crew.dashboard.handlers import workflows
    from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS, _STRICT_INTERNAL_API_PATHS
    from kiro_crew.dashboard.token_auth import token_auth_middleware

    # A loopback TCP caller proves the key it declares with the signed token its
    # launcher published. The stand-in token names its own session, so the
    # verifier answers without a trust root on disk.
    monkeypatch.setattr(
        member_memory_auth,
        "verify_session_token",
        lambda token: token.removeprefix("signed:"),
    )

    service = WorkflowService(sessions=world.sessions, context_builder=world.builder, persist=False)
    run = await finished(service, await service.start(SCRIPT, session_key="dashboard:alice"))
    state = SimpleNamespace(
        workflow_service=service,
        context_builder=world.builder,
        conversation_log=world.log,
        owner_id="owner",
    )
    app = web.Application(
        middlewares=[
            token_auth_middleware(
                internal_paths=_STRICT_INTERNAL_API_PATHS,
                mixed_internal_paths=_MIXED_INTERNAL_API_PATHS,
                internal_secret="workflow-test-secret",
            )
        ]
    )
    app["state"] = state
    app.router.add_get("/api/workflows/runs", workflows.api_workflow_runs)
    app.router.add_get("/api/workflows/runs/{run_id}", workflows.api_workflow_run_get)
    app.router.add_post("/api/workflows/runs/{run_id}/rerun", workflows.api_workflow_run_rerun)
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        for member in ("bob", "global", "alice"):
            headers = {
                "X-Internal-Secret": "workflow-test-secret",
                "X-Session-Key": f"dashboard:{member}",
                "X-Session-Token": f"signed:dashboard:{member}",
            }
            detail = await client.get(f"/api/workflows/runs/{run.run_id}", headers=headers)
            assert detail.status == 200, await detail.text()
            listing = await client.get("/api/workflows/runs", headers=headers)
            assert listing.status == 200 and (await listing.json())["runs"]
            response = await client.post(
                f"/api/workflows/runs/{run.run_id}/rerun", json={}, headers=headers
            )
            assert response.status == 200, await response.text()
            rerun = await finished(service, await response.json())
            assert rerun.execution_context.member_id == run.execution_context.member_id
            assert rerun.execution_context.store == run.execution_context.store
        refused = await client.get(
            f"/api/workflows/runs/{run.run_id}",
            headers={"X-Internal-Secret": "wrong", "X-Session-Key": "dashboard:alice"},
        )
        assert refused.status in (401, 403)
