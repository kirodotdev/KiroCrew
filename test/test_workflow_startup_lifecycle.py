"""Post-bind workflow restoration and fail-closed admission, without a gateway."""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import server
from kiro_crew.taskrunner import TaskRunner, WorkflowInitializing
from kiro_crew.workflows.service import WorkflowService
from kiro_crew.workflows.store import WorkflowRunStore


def _state():
    runner = object.__new__(TaskRunner)
    runner._sessions = SimpleNamespace(admission_closed=False)
    runner._workflow_service = None
    runner._workflow_initializing = False
    runner._runs = {}
    runner._tasks = {}
    runner._start_lock = asyncio.Lock()
    runner._start_ids_in_flight = set()
    return SimpleNamespace(
        sessions=runner._sessions,
        context_builder=None,
        task_runner=runner,
        workflow_service=None,
        workflow_startup_status="pending",
        workflow_startup_stopping=False,
        workflow_startup_task=None,
        _background_tasks=set(),
        broadcast_ws=Mock(),
    )


def _app(state):
    @web.middleware
    async def auth(request, handler):
        if request.headers.get("X-Test-Auth") != "yes":
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def unrelated(request):
        return web.json_response({"ok": True})

    async def dependent(request):
        return web.json_response({"runs": state.workflow_service.list_runs()})

    async def mutation(request):
        try:
            await state.task_runner._reserve_start("http-start")
        except WorkflowInitializing as exc:
            return web.json_response({"error": str(exc), "code": exc.code}, status=503)
        state.task_runner._release_start("http-start")
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[auth])
    app.router.add_get("/api/health", unrelated)
    app.router.add_get("/api/workflows/runs", dependent)
    app.router.add_post("/api/taskrunner", mutation)
    server._register_workflow_lifecycle(app, state)
    return app


@pytest.mark.asyncio
async def test_listener_serves_during_full_restore_then_publishes(monkeypatch, tmp_path):
    state = _state()
    store = WorkflowRunStore(tmp_path / "runs")
    original = WorkflowService(sessions=state.sessions, store=store)
    run_id = await original.begin_host_run(
        name="restored", task_id="task-1", source_format="task-plan", driver="taskrunner"
    )
    await original.pause(run_id)
    create = WorkflowService.create
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_create(**kwargs):
        entered.set()
        await release.wait()
        return await create(store=store, **kwargs)

    monkeypatch.setattr(WorkflowService, "create", delayed_create)
    async with TestClient(TestServer(_app(state), host="127.0.0.1")) as client:
        assert state.workflow_startup_task is None
        server._kick_workflow_initialization(state)
        task = state.workflow_startup_task
        server._kick_workflow_initialization(state)
        assert state.workflow_startup_task is task
        await asyncio.wait_for(entered.wait(), 3)
        headers = {"X-Test-Auth": "yes"}
        assert (await client.get("/api/health", headers=headers)).status == 200
        assert (await client.get("/api/workflows/runs")).status == 401
        assert (await client.get("/api/workflows/runs", headers=headers)).status == 503
        assert (await client.post("/api/taskrunner", headers=headers)).status == 503
        assert state.workflow_service is None
        with pytest.raises(WorkflowInitializing, match="retry shortly") as error:
            await state.task_runner._reserve_start("too-early")
        assert error.value.code == "workflow_initializing"
        assert state.task_runner._runs == {} and state.task_runner._tasks == {}
        assert not state.task_runner._start_ids_in_flight
        release.set()
        await asyncio.wait_for(task, 3)
        assert state.workflow_startup_status == "ready"
        assert state.task_runner._workflow_service is state.workflow_service
        response = await client.get("/api/workflows/runs", headers=headers)
        assert response.status == 200
        assert any(run["run_id"] == run_id for run in (await response.json())["runs"])
        assert (await client.post("/api/taskrunner", headers=headers)).status == 200
        await state.task_runner._reserve_start("ready")
        state.task_runner._release_start("ready")
        assert not state.task_runner._start_ids_in_flight


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["create", "attach"])
async def test_initialization_failure_never_half_publishes(monkeypatch, failure):
    state = _state()
    service = Mock(timeout_secs=1800)
    if failure == "create":
        create = AsyncMock(side_effect=RuntimeError("restore failed"))
    else:
        create = AsyncMock(return_value=service)
        real_attach = state.task_runner.attach_workflow_service

        def broken_attach(value):
            real_attach(value)
            if value is not None:
                raise RuntimeError("attachment failed")

        state.task_runner.attach_workflow_service = broken_attach
    driver = state.task_runner
    attach = Mock(wraps=driver.attach_workflow_service)
    monkeypatch.setattr(driver, "attach_workflow_service", attach)
    monkeypatch.setattr(WorkflowService, "create", create)
    async with TestClient(TestServer(_app(state), host="127.0.0.1")) as client:
        server._kick_workflow_initialization(state)
        await state.workflow_startup_task
        assert state.task_runner is driver
        assert state.workflow_startup_status == "failed"
        assert state.workflow_service is None
        assert state.task_runner._workflow_service is None
        with pytest.raises(WorkflowInitializing, match="restart the gateway") as error:
            await state.task_runner._reserve_start("failed-init")
        assert error.value.code == "workflow_initialization_failed"
        assert state.task_runner._runs == {} and state.task_runner._tasks == {}
        assert not state.task_runner._start_ids_in_flight
        response = await client.post("/api/taskrunner", headers={"X-Test-Auth": "yes"})
        assert response.status == 503
        payload = await response.json()
        assert payload["code"] == "workflow_initialization_failed"
        assert "restart the gateway" in payload["error"]
        assert "retry" not in payload["error"]
        assert not state.task_runner._start_ids_in_flight
        if failure == "attach":
            service.attach_task_runner.assert_called_with(None)
            assert [call.args for call in service.attach_task_runner.call_args_list] == [
                (driver,),
                (None,),
            ]
            assert [call.args for call in attach.call_args_list] == [(service,), (None,)]
        else:
            service.attach_task_runner.assert_not_called()
            attach.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("suppress_cancel", [False, True])
async def test_shutdown_cancels_recovery_and_prevents_late_publication(
    monkeypatch, suppress_cancel
):
    state = _state()
    entered = asyncio.Event()
    service = Mock(timeout_secs=1800)

    async def delayed_create(**kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not suppress_cancel:
                raise
        return service

    monkeypatch.setattr(WorkflowService, "create", delayed_create)
    app = _app(state)
    runner = web.AppRunner(app)
    await runner.setup()
    server._kick_workflow_initialization(state)
    await asyncio.wait_for(entered.wait(), 3)
    await asyncio.wait_for(runner.cleanup(), 3)
    assert state.workflow_startup_task.done()
    assert state.workflow_startup_stopping
    assert state.workflow_startup_status == "stopped"
    assert state.workflow_service is None
    assert state.task_runner._workflow_service is None
    with pytest.raises(WorkflowInitializing):
        await state.task_runner._reserve_start("after-shutdown")
    assert state.task_runner._runs == {} and state.task_runner._tasks == {}
    assert not state.task_runner._start_ids_in_flight
    task = state.workflow_startup_task
    server._kick_workflow_initialization(state)
    assert state.workflow_startup_task is task
    service.attach_task_runner.assert_not_called()


def test_both_entrypoints_kick_after_credentials_not_during_setup():
    for entry in (server.start_dashboard, server.start_api_server):
        source = inspect.getsource(entry)
        assert source.index("_start_site(site, port)") < source.index(
            "_write_instance_credentials,"
        )
        assert source.index("_write_instance_credentials,") < source.index(
            "_kick_workflow_initialization(state)"
        )
        assert source.index("_register_workflow_lifecycle(app, state)") < source.index(
            "await runner.setup()"
        )
        assert "WorkflowService.create" not in source


@pytest.mark.asyncio
async def test_standalone_runner_does_not_require_workflow_service():
    state = _state()
    await state.task_runner._reserve_start("standalone")
    assert state.task_runner._start_ids_in_flight == {"standalone"}
    state.task_runner._release_start("standalone")
    state.task_runner.defer_workflow_attachment()
    with pytest.raises(WorkflowInitializing):
        await state.task_runner._reserve_start("deferred")
    assert not state.task_runner._start_ids_in_flight
    assert state.task_runner._runs == {} and state.task_runner._tasks == {}
    state.task_runner.attach_workflow_service(None)
    await state.task_runner._reserve_start("explicit-fallback")
    assert state.task_runner._start_ids_in_flight == {"explicit-fallback"}
    state.task_runner._release_start("explicit-fallback")
    assert not state.task_runner._start_ids_in_flight


@pytest.mark.asyncio
async def test_shared_shutdown_gate_prevents_publication_before_app_shutdown(monkeypatch):
    state = _state()
    service = Mock(timeout_secs=1800)

    async def closing_create(**kwargs):
        state.sessions.admission_closed = True
        return service

    monkeypatch.setattr(WorkflowService, "create", closing_create)
    _app(state)
    server._kick_workflow_initialization(state)
    await state.workflow_startup_task
    assert state.workflow_startup_status == "stopped"
    assert state.workflow_service is None
    assert state.task_runner._workflow_service is None
    assert state.task_runner._admission_closed()
    with pytest.raises(WorkflowInitializing):
        await state.task_runner._reserve_start("closed-host")
    assert state.task_runner._runs == {} and state.task_runner._tasks == {}
    assert not state.task_runner._start_ids_in_flight
    service.attach_task_runner.assert_not_called()


@pytest.mark.asyncio
async def test_api_only_gateway_forwards_existing_context_builder(monkeypatch):
    import kiro_crew.dashboard as dashboard
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.slack.gateway import GatewayOrchestrator

    builder = object()
    state = SimpleNamespace()
    start = AsyncMock(return_value=(None, state))
    monkeypatch.setattr(dashboard, "start_api_server", start)
    host = SimpleNamespace(
        sessions=object(),
        cron_svc=object(),
        ctx_builder=builder,
        _cfg=KiroCrewConfig(),
        _port_override=None,
        _slack_enabled=False,
        subagent_mgr=None,
        task_runner=None,
        slack=None,
        _owner_id="",
        _test_mode=True,
        conv_log=None,
        _schedule_memory_preparation=lambda: None,
        _no_crons=True,
    )
    await GatewayOrchestrator._init_api_server(host)
    assert start.await_args.kwargs["context_builder"] is builder


@pytest.mark.asyncio
async def test_api_only_server_keeps_context_before_any_initialization(monkeypatch):
    builder = object()
    captured = {}

    class StateCaptured(Exception):
        pass

    def capture_state(**kwargs):
        captured.update(kwargs)
        raise StateCaptured

    monkeypatch.setattr(server, "DashboardState", capture_state)
    with pytest.raises(StateCaptured):
        await server.start_api_server(
            sessions=object(),
            crons=object(),
            lessons=object(),
            context_builder=builder,
        )
    assert captured["context_builder"] is builder


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "create", "attach", "cancel"])
async def test_canonical_gate_preserves_reads_and_cancel_and_reports_failure(
    tmp_path, monkeypatch, failure
):
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.handlers import taskrunner as handlers
    from kiro_crew.task_models import Project, Task
    from kiro_crew.taskrunner import WorkflowInitializing

    state = _state()
    driver = TaskRunner(sessions=state.sessions, work_dir=tmp_path / "tasks")
    state.task_runner = driver
    run = Project(
        spec_path="", spec_content="", task_id="existing", status="planned", source="dashboard"
    )
    run.tasks = [Task(index=1, title="Original", description="Synthetic")]
    driver._runs[run.task_id] = run
    service = MagicMock(timeout_secs=1800)
    entered, release = asyncio.Event(), asyncio.Event()

    async def create(**kwargs):
        entered.set()
        await release.wait()
        if failure == "create":
            raise RuntimeError("synthetic restore failure")
        return service

    if failure == "attach":
        attach = driver.attach_workflow_service

        def fail_after_attach(value):
            attach(value)
            if value is not None:
                raise RuntimeError("synthetic attachment failure")

        monkeypatch.setattr(driver, "attach_workflow_service", fail_after_attach)
    monkeypatch.setattr(WorkflowService, "create", create)
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/taskrunner", handlers.api_taskrunner_status)
    app.router.add_post("/api/taskrunner/cancel", handlers.api_taskrunner_cancel)
    app.router.add_delete("/api/taskrunner/{task_id}", handlers.api_taskrunner_delete)
    app.router.add_put("/api/taskrunner/{task_id}/plan", handlers.api_taskrunner_update_plan)

    async def workflow_runs(request):
        return web.json_response({"runs": []})

    app.router.add_get("/api/workflows/runs", workflow_runs)
    server._register_workflow_lifecycle(app, state)
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        server._kick_workflow_initialization(state)
        await asyncio.wait_for(entered.wait(), 3)
        assert state.workflow_startup_task.get_name() == "workflow-initialization"
        assert (await client.get("/api/taskrunner")).status == 200
        assert (await client.post("/api/taskrunner/cancel", json={})).status == 200
        response = await client.delete("/api/taskrunner/existing")
        assert response.status == 503
        assert (await response.json())["code"] == "workflow_initializing"
        with pytest.raises(WorkflowInitializing, match="retry shortly"):
            await driver._reserve_start("pending")
        if failure == "cancel":
            state.workflow_startup_task.cancel()
            await asyncio.gather(state.workflow_startup_task, return_exceptions=True)
        else:
            release.set()
            await state.workflow_startup_task
        if failure is None:
            assert state.workflow_startup_status == "ready"
            assert state.workflow_service is service
            assert driver._workflow_service is service
            driver._require_workflow_ready()
            service.attach_task_runner.assert_called_once_with(driver)
        else:
            assert state.workflow_startup_status == "failed"
            assert state.workflow_service is None
            assert driver._workflow_service is None
            for path in ("/api/workflows/runs", "/api/taskrunner/existing"):
                response = await (client.get(path) if "workflows" in path else client.delete(path))
                assert response.status == 503
                payload = await response.json()
                assert payload["code"] == "workflow_initialization_failed"
                assert "restart the gateway" in payload["error"]
                assert "retry" not in payload["error"]
            response = await client.put(
                "/api/taskrunner/existing/plan", json={"steps": [{"index": 1, "title": "Changed"}]}
            )
            assert response.status == 503
            assert (await response.json())["code"] == "workflow_initialization_failed"
            assert driver._runs["existing"].tasks[0].title == "Original"
            with pytest.raises(WorkflowInitializing, match="restart the gateway"):
                await driver._reserve_start("failed")
            assert not driver._start_ids_in_flight
            assert (await client.get("/api/taskrunner")).status == 200
            assert (await client.post("/api/taskrunner/cancel", json={})).status == 200


def test_canonical_explicit_none_releases_standalone_fallback(tmp_path):
    from kiro_crew.taskrunner import WorkflowInitializing

    driver = TaskRunner(sessions=None, work_dir=tmp_path)
    driver._require_workflow_ready()
    driver.defer_workflow_attachment(failed=True)
    with pytest.raises(WorkflowInitializing, match="restart the gateway"):
        driver._require_workflow_ready()
    driver.attach_workflow_service(None)
    driver._require_workflow_ready()
    assert not hasattr(driver, "_workflow_recovery_pending")
