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


def _compose_identity_trace(observer, previous):
    """Observe selected frames while preserving the installed coverage tracer."""

    def dispatch(frame, event, arg):
        prior_local = previous(frame, event, arg) if previous is not None else None
        observed_local = observer(frame, event, arg)
        if observed_local is None:
            return prior_local

        def combined(frame, event, arg):
            nonlocal prior_local, observed_local
            if prior_local is not None:
                prior_local = prior_local(frame, event, arg)
            if observed_local is not None:
                observed_local = observed_local(frame, event, arg)
            return combined if prior_local is not None or observed_local is not None else None

        return combined

    return dispatch


def _safe_identity_branch(values):
    """Project only branch facts, never record contents, paths or proof bytes."""
    facts = {}
    if "raw" in values:
        facts["record_readable"] = values["raw"] is not None
    if "row" in values:
        record = values["row"]
        facts["record_is_object"] = isinstance(record, dict)
        if isinstance(record, dict):
            facts["version_supported"] = record.get("version") in (1, 2)
            facts["version_is_global_capable"] = record.get("version") == 2
            facts["session_valid"] = isinstance(record.get("session_key"), str) and bool(
                record["session_key"]
            )
            facts["store_valid"] = isinstance(record.get("memory_store"), str) and (
                record["memory_store"] != "default"
            )
            facts["store_empty"] = record.get("memory_store") == ""
            facts["start_present"] = bool(values.get("start"))
            facts["start_matches_record"] = values.get("start") == record.get("process_start")
    if "no_report" in values:
        facts["no_report_loaded"] = True
    if type(values.get("result")) is int:
        facts["native_result"] = values["result"]
    return facts


def _require_identity_listing(status, member, trace):
    if status != 200:
        import json

        pytest.fail(
            "workflow identity listing: "
            + json.dumps({"member": member, "status": status, "trace": trace}, sort_keys=True),
            pytrace=False,
        )


def test_identity_failure_trace_is_complete_and_payload_free():
    import json

    private = "SECRET_PATH_PROOF_OR_RECORD_CONTENT"
    facts = _safe_identity_branch(
        {
            "raw": private.encode(),
            "path": private,
            "proof": private,
            "row": {
                "version": 2,
                "session_key": private,
                "memory_store": private,
                "process_start": private,
            },
            "start": private,
        }
    )
    trace = [facts for _ in range(100)]
    with pytest.raises(pytest.fail.Exception) as error:
        _require_identity_listing(403, "global", trace)
    message = str(error.value)
    assert private not in message
    payload = json.loads(message.removeprefix("workflow identity listing: "))
    assert payload == {"member": "global", "status": 403, "trace": trace}
    assert len(payload["trace"]) == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("author_only", [False, True])
async def test_saved_task_plan_executes_on_real_private_taskrunner(
    world, tmp_path_factory, author_only, caplog
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
        scope = await WorkflowScope.restore(started["run_id"])
        assert scope.store == world.stores["alice"]
        assert "PRIVATE_DIAGNOSTIC_TITLE" not in caplog.text
        assert "Private task" in caplog.text
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
@pytest.mark.parametrize("damage", ["archived", "database-deleted"])
@pytest.mark.parametrize("mid_run", [False, True])
@pytest.mark.parametrize("pooled", [False, True])
async def test_retired_or_deleted_memory_never_allocates_global(
    world, monkeypatch, damage, mid_run, pooled
):
    from test_workflows_private_execution import SCRIPT

    from kiro_crew.memory_stores import archive_member_memory_store, resolve_store_path
    from kiro_crew.workflows import agent_exec, agent_pool

    refusal: OSError | None = None

    def invalidate() -> bool:
        """Apply the damage; ``True`` once it has actually landed.

        Windows refuses to delete a file that still has a live handle. Two things
        can hold one: the fixture's own cached store, retired here explicitly,
        and a workflow step reading memory for itself. The caller below only
        injects once NO step is in flight, so the second holder cannot exist and
        the unlink needs no retry -- a refusal here is a real one and is
        reported, because ``ctx.parallel`` turns a raising branch into ``None``
        and never re-raises (GATE A5), so an injection that fails inside one is
        otherwise invisible and the run goes on to finish against an INTACT
        store.

        Synchronous on purpose: it runs with no await between the last step
        finishing and the damage landing, so no other step can open a handle in
        the middle of it.
        """
        nonlocal refusal
        if damage == "archived":
            archive_member_memory_store(world.stores["alice"], expected_owner="alice")
            return True

        from kiro_crew.context import release_cached_memory_store

        path = resolve_store_path(world.stores["alice"])
        release_cached_memory_store(world.stores["alice"])
        try:
            path.unlink()
        except OSError as exc:
            refusal = exc
            return False
        assert not path.exists()
        return True

    svc = WorkflowService(
        sessions=world.sessions,
        context_builder=world.builder,
        pool_agents=pooled,
        persist=False,
    )
    if not mid_run:
        assert invalidate(), f"pre-run {damage} was never applied: {refusal!r}"
        rejected = await svc.start(SCRIPT, session_key="dashboard:alice")
        assert rejected["code"] == "workflow_memory_unavailable"
        assert not world.models
        assert not svc.list_runs()
        return
    base_stream = agent_pool.stream_and_collect
    invalidated = False
    inflight = 0

    async def complete_then_invalidate(provider, prompt, **kwargs):
        nonlocal invalidated, inflight
        inflight += 1
        try:
            result = await base_stream(provider, prompt, **kwargs)
        finally:
            inflight -= 1
        # Inject when THIS step is the last one out, which is the signal the
        # production path actually resolves: every other branch of the parallel
        # group has returned, so no step holds a memory handle and the unlink
        # cannot be refused for sharing. SCRIPT runs three more steps after that
        # group, so the damage still lands mid-run. Synchronous from here to the
        # unlink, so no step can open a handle in between.
        if inflight == 0 and not invalidated:
            invalidated = invalidate()
        return result

    for module in (agent_exec, agent_pool):
        monkeypatch.setattr(module, "stream_and_collect", complete_then_invalidate)
    started = await svc.start(SCRIPT, session_key="dashboard:alice")
    run = svc.registry.get(started["run_id"])
    await asyncio.wait_for(run.task, 10)
    # The subject is what the run does with destroyed memory, so an injection
    # that never landed is a harness fact and is reported as one. Asserting the
    # status first would blame the run for a store this test left intact.
    assert invalidated, f"mid-run {damage} was never applied: {refusal!r}"
    assert run.status == "failed", run.result
    assert world.models and all(model._private_memory for model in world.models)


@pytest.fixture
def global_vector(world, event_loop):
    """The HTTP world needs the Global DB normally initialized at gateway boot."""
    from kiro_crew.config.paths import config_dir
    from kiro_crew.vector_memory import VectorMemoryStore

    vectors = VectorMemoryStore(db_path=config_dir() / "memory.db")
    try:
        event_loop.run_until_complete(asyncio.to_thread(vectors.init))
        yield vectors
    finally:
        event_loop.run_until_complete(asyncio.to_thread(vectors.close))


@pytest.mark.asyncio
async def test_http_private_run_read_list_cancel_rerun_scope(world, monkeypatch, global_vector):
    import ctypes
    import os
    import sys
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from test_workflows_private_execution import SCRIPT, finished

    from kiro_crew import member_memory_auth, platform_compat
    from kiro_crew.dashboard.handlers import workflows
    from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS, _STRICT_INTERNAL_API_PATHS
    from kiro_crew.dashboard.token_auth import token_auth_middleware
    from kiro_crew.member_memory_auth import (
        PROOF_HEADER,
        issue_member_session_proof,
        publish_member_session_pid,
    )

    identity_trace = []
    traced_codes = {
        member_memory_auth._protected_member_binding_for_pid.__code__,
        platform_compat.process_can_read_under_sandbox.__code__,
    }

    def branch_trace(frame, event, arg):
        if frame.f_code not in traced_codes:
            return None
        values = frame.f_locals
        row = {"probe": frame.f_code.co_name, "line": frame.f_lineno, "event": event}
        if event == "exception":
            row["exception"] = arg[0].__name__
        elif event == "return":
            row.update(_safe_identity_branch(values))
        elif event == "line" and type(values.get("result")) is int:
            # Capture errno on the first Python line after the real variadic
            # sandbox_check call, before a later process-start query changes it.
            if not any(item.get("native_query") for item in identity_trace):
                row.update(
                    native_query=True,
                    native_result=values["result"],
                    native_errno=ctypes.get_errno(),
                    no_report=values.get("no_report"),
                )
            else:
                return branch_trace
        else:
            return branch_trace
        identity_trace.append(row)
        return branch_trace

    def record_probe(module, name):
        original = getattr(module, name)

        def observed(*args, **kwargs):
            # to_thread executes this wrapper on the same thread as the real
            # identity check. Restore its prior tracer even on an exception.
            previous = sys.gettrace()
            tracing = name == "_protected_member_binding_for_pid"
            if tracing:
                sys.settrace(_compose_identity_trace(branch_trace, previous))
            try:
                result = original(*args, **kwargs)
            except Exception as exc:
                identity_trace.append({"probe": name, "exception": type(exc).__name__})
                raise
            else:
                safe = result
                if name == "_protected_member_binding_for_pid" and result is not None:
                    safe = {"session_present": bool(result[0]), "store_present": bool(result[1])}
                elif name == "get_process_start_id":
                    safe = bool(result)
                identity_trace.append({"probe": name, "result": safe})
                return result
            finally:
                if tracing:
                    sys.settrace(previous)

        monkeypatch.setattr(module, name, observed)

    # Observe the exact checks used by the request, never re-probe after a
    # refusal: a second OS query could hide a transient failure in the first.
    record_probe(platform_compat, "get_tcp_peer_pid")
    record_probe(platform_compat, "_macos_tcp_peer_pid")
    record_probe(platform_compat, "get_process_start_id")
    record_probe(platform_compat, "process_can_read_under_sandbox")
    record_probe(member_memory_auth, "_protected_member_binding_for_pid")

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
    app.router.add_post("/api/workflows/runs/{run_id}/cancel", workflows.api_workflow_run_cancel)
    app.router.add_post("/api/workflows/runs/{run_id}/rerun", workflows.api_workflow_run_rerun)
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        for member in ("bob", "global", "alice"):
            store = world.stores.get(member, "")
            key = f"dashboard:{member}"
            publish_member_session_pid(os.getpid(), key, memory_store=store)
            headers = {"X-Internal-Secret": "workflow-test-secret", "X-Session-Key": key}
            if store:
                proof = issue_member_session_proof(key, os.getpid())
                assert proof
                headers[PROOF_HEADER] = proof
            allowed = member == "alice"
            detail = await client.get(f"/api/workflows/runs/{run.run_id}", headers=headers)
            assert detail.status == (200 if allowed else 403), (member, await detail.text())
            if not store:
                assert await asyncio.to_thread(store_of_session, world.log, key) == ""
            identity_trace.clear()
            from kiro_crew.config.paths import config_dir

            target = config_dir().resolve() / "memory.db"
            identity_trace.append(
                {
                    "fixture_global_db_exists": await asyncio.to_thread(target.exists),
                    "fixture_global_parent_exists": await asyncio.to_thread(target.parent.exists),
                }
            )
            listing = await client.get("/api/workflows/runs", headers=headers)
            _require_identity_listing(listing.status, member, identity_trace)
            assert bool((await listing.json())["runs"]) == allowed
            for operation in ("cancel", "rerun"):
                response = await client.post(
                    f"/api/workflows/runs/{run.run_id}/{operation}", json={}, headers=headers
                )
                assert response.status == (200 if allowed else 403), await response.text()
                if allowed and operation == "rerun":
                    await finished(service, await response.json())


def test_identity_observer_preserves_prior_local_tracer_changes():
    events = []
    selected, unrelated = object(), object()

    def prior_second(frame, event, arg):
        events.append(("prior_second", event))
        return prior_second

    def prior_first(frame, event, arg):
        events.append(("prior_first", event))
        return prior_second

    def prior(frame, event, arg):
        events.append(("prior", event))
        return prior_first

    def observer(frame, event, arg):
        if frame is not selected:
            return None
        events.append(("observer", event))
        return observer

    trace = _compose_identity_trace(observer, prior)
    local = trace(selected, "call", None)
    local = local(selected, "line", None)
    local(selected, "return", None)
    assert events == [
        ("prior", "call"),
        ("observer", "call"),
        ("prior_first", "line"),
        ("observer", "line"),
        ("prior_second", "return"),
        ("observer", "return"),
    ]
    assert trace(unrelated, "call", None) is prior_first


@pytest.mark.asyncio
async def test_native_seatbelt_global_identity_requires_existing_probe_target():
    """Use the real native query on both sides of normal Global DB startup."""
    import os
    import sys

    if sys.platform != "darwin":
        pytest.skip("Native Seatbelt probe runs only on macOS")
    from kiro_crew import member_memory_auth, platform_compat
    from kiro_crew.config.paths import config_dir
    from kiro_crew.vector_memory import VectorMemoryStore

    target = config_dir().resolve() / "memory.db"
    assert not target.exists()
    key = "dashboard:global-probe"
    await asyncio.to_thread(member_memory_auth.publish_member_session_pid, os.getpid(), key)
    assert (
        await asyncio.to_thread(platform_compat.process_can_read_under_sandbox, os.getpid(), target)
        is False
    )
    assert await asyncio.to_thread(
        member_memory_auth._protected_member_binding_for_pid, os.getpid()
    ) == ("", "")
    vectors = VectorMemoryStore(db_path=target)
    try:
        await asyncio.to_thread(vectors.init)
        assert target.is_file()
        assert (
            await asyncio.to_thread(
                platform_compat.process_can_read_under_sandbox, os.getpid(), target
            )
            is True
        )
        assert await asyncio.to_thread(
            member_memory_auth._protected_member_binding_for_pid, os.getpid()
        ) == (key, "")
    finally:
        await asyncio.to_thread(vectors.close)
