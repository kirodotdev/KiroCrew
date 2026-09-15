"""Restart I/O must settle off-loop before the dashboard exposes workflows."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.workflows.registry import RunHandle, RunRegistry
from kiro_crew.workflows.service import WorkflowService
from kiro_crew.workflows.store import WorkflowRunStore


async def _create(**kwargs):
    # Check thread affinity through either startup entry point, including the
    # complete synchronous constructor path when no factory is exposed.
    factory = getattr(WorkflowService, "create", None)
    if factory is None:
        return WorkflowService(**kwargs)
    return await factory(**kwargs)


def _seed(store):
    handle = RunHandle(
        run_id="wf_000042",
        name="restart",
        source="META = {}\nasync def workflow(ctx):\n    return 1\n",
        args={"input": "kept"},
        agent_results={0: {"answer": "kept"}, 1: None},
        agent_errors={1: "no result"},
        workflow_id="wfd_saved",
        workflow_revision=3,
        driver="taskrunner",
        task_id="task-1",
    )
    store.save(handle.run_id, handle.to_store_json())


# A hang guard, not the assertion: ``_await_restart_write`` settles on causality
# and this only turns a lost run into a failed test, well under pytest-timeout.
_HANG_GUARD_SECS = 60.0


async def _await_restart_write(entered: asyncio.Event, startup: asyncio.Task) -> None:
    """Wait for the restart write to begin, or for startup to be unable to reach it.

    The write is scheduled only after the real ``start_dashboard`` has bound and
    kicked workflow initialization, which then loads config, constructs the
    service and hydrates the store off-loop. A fixed wall-clock budget across
    all of that measured runner speed, not the bind-before-write ordering under
    test. Settle on causality instead: ``entered`` fires, a failed boot raises
    its own error, or the initializer finishes without ever writing, which is a
    deterministic failure.
    """
    give_up_at = time.monotonic() + _HANG_GUARD_SECS
    initializer = None
    while not entered.is_set():
        if initializer is None and startup.done():
            _, state, _ = startup.result()
            initializer = state.workflow_startup_task
            assert initializer is not None, "startup finished without kicking initialization"
        if initializer is not None and initializer.done():
            initializer.result()
            raise AssertionError("workflow initialization settled without the restart write")
        assert time.monotonic() < give_up_at, "the restart write never began"
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_restart_write_barrier_is_causal_not_wall_clock():
    entered = asyncio.Event()

    async def failed_boot():
        raise RuntimeError("boot failed before the write")

    with pytest.raises(RuntimeError, match="boot failed"):
        await _await_restart_write(entered, asyncio.create_task(failed_boot()))

    settled = asyncio.create_task(asyncio.sleep(0))
    await settled

    async def booted():
        return None, SimpleNamespace(workflow_startup_task=settled), None

    with pytest.raises(AssertionError, match="settled without the restart write"):
        await _await_restart_write(entered, asyncio.create_task(booted()))

    booting = asyncio.create_task(asyncio.sleep(3600))
    try:
        entered.set()
        await _await_restart_write(entered, booting)  # returns on the signal alone
    finally:
        booting.cancel()


@pytest.mark.asyncio
async def test_restart_io_off_loop_retains_payload_and_is_idempotent(tmp_path, monkeypatch):
    store = WorkflowRunStore(base_dir=tmp_path)
    await asyncio.to_thread(_seed, store)
    loop_thread = threading.get_ident()
    reads, writes, hydration = [], [], []
    real_load, real_save = store.load_all, store.save
    real_restore = RunHandle.from_store_json

    def load():
        reads.append(threading.get_ident())
        return real_load()

    def save(*args):
        writes.append(threading.get_ident())
        return real_save(*args)

    def restore(obj):
        hydration.append(threading.get_ident())
        return real_restore(obj)

    monkeypatch.setattr(store, "load_all", load)
    monkeypatch.setattr(store, "save", save)
    monkeypatch.setattr(RunHandle, "from_store_json", staticmethod(restore))
    svc = await _create(sessions=None, store=store)
    assert writes and all(t != loop_thread for t in writes), "restart write blocked loop"
    assert reads and all(t != loop_thread for t in reads), "restart read blocked loop"
    assert hydration == [loop_thread]
    disk = (await asyncio.to_thread(real_load))[0]
    handle = svc.registry.get("wf_000042")
    assert handle.status == disk["status"] == "failed"
    assert "interrupted" in disk["error"]
    assert disk == handle.to_store_json()
    assert disk["agent_results"] == {"0": {"answer": "kept"}, "1": None}
    assert disk["agent_errors"] == {"1": "no result"}
    assert disk["source_is_original"] is True
    assert disk["workflow_id"] == "wfd_saved" and disk["workflow_revision"] == 3
    assert disk["args"] == {"input": "kept"}
    writes.clear()
    again = await _create(sessions=None, store=store)
    assert writes == []
    assert await again._new_run_id() == "wf_000043"
    assert again.registry.reopen_host_run("wf_000042", task_id="task-1", persist=False)
    await again.registry.persist_async("wf_000042")
    assert (await asyncio.to_thread(real_load))[0]["status"] == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_dashboard_binds_before_restart_write_and_owns_initialization(
    tmp_path, monkeypatch, cancel
):
    from aiohttp.test_utils import make_mocked_request
    from test_dashboard_server_startup_coverage import (
        _cancel_stray_tasks,
        _release_process_handles,
        _start_dashboard,
    )

    from kiro_crew.dashboard import server
    from kiro_crew.dashboard.handlers.workflows import api_workflow_runs
    from kiro_crew.workflows import service

    store = WorkflowRunStore(base_dir=tmp_path)
    await asyncio.to_thread(_seed, store)
    bound, entered = asyncio.Event(), asyncio.Event()
    release, finished = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    save_threads = []
    real_save = store.save

    def save(*args):
        save_threads.append(threading.get_ident())
        loop.call_soon_threadsafe(entered.set)
        try:
            if not release.wait(10):
                raise TimeoutError("test did not release restart write")
            real_save(*args)
        finally:
            finished.set()

    monkeypatch.setattr(store, "save", save)
    monkeypatch.setattr(service, "WorkflowRunStore", lambda: store)
    # The shared startup harness stubs the socket bind and all external work;
    # this marker is called by actual startup wiring immediately AFTER the bind.
    monkeypatch.setattr(server, "_export_bound_port", lambda *args: bound.set())
    from kiro_crew.taskrunner import TaskRunner

    driver = TaskRunner(sessions=MagicMock(), context_builder=MagicMock(), work_dir=tmp_path)
    attach = MagicMock(wraps=driver.attach_workflow_service)
    monkeypatch.setattr(driver, "attach_workflow_service", attach)
    startup = asyncio.create_task(_start_dashboard(tmp_path, monkeypatch, task_runner=driver))
    runner = None
    state = None
    shutdown = None
    try:
        await _await_restart_write(entered, startup)
        assert bound.is_set(), "restart write delayed socket readiness"
        runner, state, _ = await asyncio.wait_for(asyncio.shield(startup), 5)
        assert state.ready
        assert len(save_threads) == 1 and save_threads[0] != loop_thread
        assert not finished.is_set()
        assert state.workflow_service is None
        assert state.task_runner is driver  # no task may start without its publication port
        driver.attach_workflow_service.assert_not_called()
        request = make_mocked_request("GET", "/api/workflows/runs", app=runner.app)
        assert (await api_workflow_runs(request)).status == 503
        initializer = next(
            t for t in state._background_tasks if t.get_name() == "workflow-initialization"
        )
        if cancel:
            shutdown = asyncio.create_task(runner.cleanup())

            async def cancellation_delivered():
                while not initializer.cancelling():
                    await asyncio.sleep(0)

            await asyncio.wait_for(cancellation_delivered(), 5)
            shutdown.cancel()
            await asyncio.sleep(0)
            shutdown.cancel()
            await asyncio.sleep(0)
            assert not shutdown.done()
            assert not initializer.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(initializer, return_exceptions=True), 5)
        if shutdown is not None:
            await asyncio.wait_for(shutdown, 5)
        assert finished.is_set()
        assert (await asyncio.to_thread(store.load_all))[0]["status"] == "failed"
        if cancel:
            assert state.workflow_service is None
            assert state.task_runner is driver
            driver.attach_workflow_service.assert_not_called()
            assert (await api_workflow_runs(request)).status == 503
        else:
            published = state.workflow_service
            driver.attach_workflow_service.assert_called_once_with(published)
            assert state.task_runner is driver
            assert published.registry.get("wf_000042").status == "failed"
            assert (await api_workflow_runs(request)).status == 200
            await asyncio.sleep(0)
            assert state.workflow_service is published
            driver.attach_workflow_service.assert_called_once()
    finally:
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(startup, return_exceptions=True), 15)
        if runner is None and isinstance(outcomes[0], tuple):
            runner, state = outcomes[0][0], outcomes[0][1]
        if shutdown is not None:
            await asyncio.wait_for(asyncio.gather(shutdown, return_exceptions=True), 5)
        if runner is not None:
            await runner.cleanup()
        await _cancel_stray_tasks()
        if state is not None:
            _release_process_handles(state)


@pytest.mark.asyncio
async def test_async_reload_evicts_off_loop_and_skips_bad_records(tmp_path, monkeypatch):
    store = WorkflowRunStore(base_dir=tmp_path)
    await asyncio.to_thread(_seed, store)
    loop_thread = threading.get_ident()
    deleted = []
    real_delete = store.delete

    def delete(rid):
        deleted.append(threading.get_ident())
        real_delete(rid)

    records = await asyncio.to_thread(store.load_all)
    monkeypatch.setattr(store, "load_all", lambda: [{"run_id": "bad", "events": [None]}, *records])
    monkeypatch.setattr(store, "delete", delete)
    registry = RunRegistry(store=store, max_runs=0)
    assert await registry.load_persisted_async() == 1
    assert registry.list() == []
    assert deleted and all(t != loop_thread for t in deleted)
    assert not list(tmp_path.joinpath("runs").iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["load_all", "save", "delete"])
async def test_async_reload_refuses_inventory_failure_but_keeps_write_compatibility(
    tmp_path, monkeypatch, operation
):
    store = WorkflowRunStore(base_dir=tmp_path)
    await asyncio.to_thread(_seed, store)

    def fail(*args):
        raise OSError("test storage unavailable")

    monkeypatch.setattr(store, operation, fail)
    registry = RunRegistry(store=store, max_runs=0 if operation == "delete" else 200)
    done, event = MagicMock(), MagicMock()
    registry.set_on_done(done)
    registry.set_on_event(event)
    if operation == "load_all":
        with pytest.raises(OSError, match="test storage unavailable"):
            await registry.load_persisted_async()
        assert registry.list() == []
    else:
        assert await registry.load_persisted_async() == 1
        if operation == "save":
            assert registry.get("wf_000042").status == "failed"
        else:
            assert registry.list() == []
    done.assert_not_called()
    event.assert_not_called()


@pytest.mark.asyncio
async def test_async_service_without_persistence_does_not_construct_store(monkeypatch):
    from kiro_crew.workflows import service

    store_factory = MagicMock(side_effect=AssertionError("unexpected store construction"))
    monkeypatch.setattr(service, "WorkflowRunStore", store_factory)
    svc = await WorkflowService.create(sessions=None, persist=False)
    assert svc.list_runs() == []
    assert await svc._new_run_id() == "wf_000001"
    store_factory.assert_not_called()


@pytest.mark.asyncio
async def test_dashboard_initialization_failure_keeps_workflows_unavailable(
    tmp_path, monkeypatch, caplog
):
    from aiohttp.test_utils import make_mocked_request
    from test_dashboard_server_startup_coverage import _dashboard

    from kiro_crew.dashboard.handlers.workflows import api_workflow_runs
    from kiro_crew.taskrunner import TaskRunner, WorkflowInitializing

    async def fail(**kwargs):
        raise RuntimeError("test initialization failure")

    monkeypatch.setattr(WorkflowService, "create", fail)
    driver = TaskRunner(sessions=MagicMock(), work_dir=tmp_path / "tasks")
    attach = MagicMock(wraps=driver.attach_workflow_service)
    monkeypatch.setattr(driver, "attach_workflow_service", attach)
    async with _dashboard(tmp_path, monkeypatch, task_runner=driver) as (runner, state, _):
        pending = [t for t in state._background_tasks if t.get_name() == "workflow-initialization"]
        await asyncio.wait_for(asyncio.gather(*pending), 5)
        assert state.ready and state.workflow_service is None
        assert state.task_runner is driver
        assert state.workflow_startup_status == "failed"
        attach.assert_not_called()  # Construction failed before either port was attached.
        with pytest.raises(WorkflowInitializing, match="restart the gateway") as error:
            await driver._reserve_start("failed-init")
        assert error.value.code == "workflow_initialization_failed"
        assert driver._runs == {} and driver._tasks == {}
        assert not driver._start_ids_in_flight
        request = make_mocked_request("GET", "/api/workflows/runs", app=runner.app)
        assert (await api_workflow_runs(request)).status == 503
        assert "WorkflowService unavailable" in caplog.text


@pytest.mark.asyncio
async def test_shutdown_fences_a_factory_that_returns_after_cancellation(tmp_path, monkeypatch):
    from test_dashboard_server_startup_coverage import _dashboard

    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    service = MagicMock()

    async def delayed(**kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return service

    monkeypatch.setattr(WorkflowService, "create", delayed)
    driver = SimpleNamespace(
        attach_workflow_service=MagicMock(), defer_workflow_attachment=MagicMock()
    )
    async with _dashboard(tmp_path, monkeypatch, task_runner=driver) as (runner, state, _):
        shutdown = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            shutdown = asyncio.create_task(runner.cleanup())
            await asyncio.wait_for(cancelled.wait(), 5)
            assert not shutdown.done()
            release.set()
            await asyncio.wait_for(shutdown, 5)
            assert state.workflow_service is None and state.task_runner is driver
            service.attach_task_runner.assert_not_called()
            driver.attach_workflow_service.assert_not_called()
        finally:
            release.set()
            if shutdown is not None:
                await asyncio.wait_for(asyncio.gather(shutdown, return_exceptions=True), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_during_tunnel", [False, True])
async def test_tunnel_stops_before_workflow_drain_with_early_publication_fence(
    tmp_path, monkeypatch, finish_during_tunnel
):
    from test_dashboard_server_startup_coverage import _dashboard

    entered, release = asyncio.Event(), asyncio.Event()
    tunnel_entered, tunnel_release = asyncio.Event(), asyncio.Event()
    cancelled = asyncio.Event()
    service = MagicMock()

    async def delayed(**kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return service

    async def stop_tunnel():
        tunnel_entered.set()
        await tunnel_release.wait()

    monkeypatch.setattr(WorkflowService, "create", delayed)
    async with _dashboard(tmp_path, monkeypatch) as (runner, state, _):
        state.tunnel_manager = SimpleNamespace(stop=stop_tunnel)
        shutdown = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            initializer = next(
                t for t in state._background_tasks if t.get_name() == "workflow-initialization"
            )
            shutdown = asyncio.create_task(runner.cleanup())
            await asyncio.wait_for(tunnel_entered.wait(), 5)
            assert not cancelled.is_set(), "workflow drain preceded tunnel teardown"
            if finish_during_tunnel:
                release.set()
                await asyncio.wait_for(initializer, 5)
                assert state.workflow_service is None
                assert not shutdown.done()
            tunnel_release.set()
            if not finish_during_tunnel:
                await asyncio.wait_for(cancelled.wait(), 5)
                assert not shutdown.done(), "shutdown abandoned workflow initialization"
                release.set()
            await asyncio.wait_for(shutdown, 5)
            assert state.workflow_service is None
            assert initializer.done()
        finally:
            release.set()
            tunnel_release.set()
            if shutdown is not None:
                await asyncio.wait_for(asyncio.gather(shutdown, return_exceptions=True), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("init_fails", [False, True])
async def test_channel_task_admission_waits_for_workflow_attachment(
    tmp_path, monkeypatch, init_fails
):
    from test_dashboard_server_startup_coverage import _dashboard

    from kiro_crew.messaging.commands import task_arg_reply
    from kiro_crew.taskrunner import TaskRunner

    entered, release = asyncio.Event(), asyncio.Event()
    original_create = WorkflowService.create

    async def delayed(**kwargs):
        entered.set()
        await release.wait()
        if init_fails:
            raise RuntimeError("test workflow initialization failed")
        return await original_create(**kwargs, persist=False)

    monkeypatch.setattr(WorkflowService, "create", delayed)
    driver = TaskRunner(sessions=MagicMock(), work_dir=tmp_path / "tasks")
    orch = SimpleNamespace(task_runner=driver)  # same reference retained by the channel gateway
    spec = tmp_path / "task.txt"
    spec.write_text("A synthetic task", encoding="utf-8")
    executed = asyncio.Event()

    async def no_model_run(*args, **kwargs):
        executed.set()

    # Keep actual command dispatch, admission, registration and durable linkage;
    # replace only execution after the placeholder has been committed.
    monkeypatch.setattr(driver, "run", no_model_run)
    async with _dashboard(tmp_path, monkeypatch, task_runner=driver) as (_, state, _):
        initializer = next(
            t for t in state._background_tasks if t.get_name() == "workflow-initialization"
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            reply = await task_arg_reply(str(spec), orch.task_runner)
            assert "initializing" in reply
            assert driver._runs == {} and driver._tasks == {}
            assert not executed.is_set()
            release.set()
            await asyncio.wait_for(initializer, 5)
            reply = await task_arg_reply(str(spec), orch.task_runner)
            if init_fails:
                from kiro_crew.taskrunner import WorkflowInitializing

                assert state.workflow_service is None
                assert state.task_runner is driver
                assert state.workflow_startup_status == "failed"
                assert "Task started" not in reply
                assert "restart the gateway" in reply
                with pytest.raises(WorkflowInitializing, match="restart the gateway") as error:
                    await driver._reserve_start("failed-init")
                assert error.value.code == "workflow_initialization_failed"
                assert driver._runs == {} and driver._tasks == {}
                assert not driver._start_ids_in_flight
                assert not executed.is_set()
                standalone = TaskRunner(sessions=MagicMock(), work_dir=tmp_path / "standalone")
                monkeypatch.setattr(standalone, "run", no_model_run)
                try:
                    standalone_reply = await task_arg_reply(str(spec), standalone)
                    assert "Task started" in standalone_reply
                    await asyncio.wait_for(executed.wait(), 5)
                    standalone_run = next(iter(standalone._runs.values()))
                    assert standalone_run.workflow_run_id == ""
                finally:
                    await asyncio.wait_for(
                        asyncio.gather(*list(standalone._tasks.values()), return_exceptions=True), 5
                    )
            else:
                assert "Task started" in reply
                await asyncio.wait_for(executed.wait(), 5)
                run = next(iter(driver._runs.values()))
                assert run.workflow_run_id
                assert (
                    state.workflow_service.registry.get(run.workflow_run_id).task_id == run.task_id
                )
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(initializer, return_exceptions=True), 5)
            await asyncio.wait_for(
                asyncio.gather(*list(driver._tasks.values()), return_exceptions=True), 5
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry", ["plan", "run", "start_background", "execute_plan", "retry_from_task"]
)
async def test_shared_task_admission_refuses_before_mutating_state(tmp_path, entry):
    from kiro_crew.taskrunner import TaskRunner

    driver = TaskRunner(sessions=MagicMock(), work_dir=tmp_path)
    driver.defer_workflow_attachment()
    args = ("missing", 0) if entry == "retry_from_task" else ("missing",)
    with pytest.raises(RuntimeError, match="initializing workflows"):
        await getattr(driver, entry)(*args)
    assert driver._runs == {} and driver._tasks == {}
    assert not driver._start_ids_in_flight
    driver.attach_workflow_service(None)
    driver._require_workflow_ready()  # explicit None releases standalone mode


@pytest.mark.asyncio
async def test_async_factory_moves_constructor_io_off_loop(tmp_path, monkeypatch):
    from kiro_crew.workflows import library, service, store

    loop_thread = threading.get_ident()
    observed = []
    real_load = store.KiroCrewConfig.load
    real_library_dir = library.default_workflow_library_dir
    real_bind = service.live.bind

    def config_load(*args, **kwargs):
        observed.append(("config", threading.get_ident()))
        return real_load(*args, **kwargs)

    def library_dir():
        observed.append(("library", threading.get_ident()))
        return real_library_dir()

    def bind(*args, **kwargs):
        observed.append(("bind", threading.get_ident()))
        return real_bind(*args, **kwargs)

    monkeypatch.setattr(store.KiroCrewConfig, "load", config_load)
    monkeypatch.setattr(library, "default_workflow_library_dir", library_dir)
    monkeypatch.setattr(service.live, "bind", bind)
    svc = await WorkflowService.create(sessions=None)
    assert svc.registry is not None
    assert {name for name, _ in observed} == {"config", "library", "bind"}
    assert all(thread != loop_thread for _, thread in observed)


@pytest.mark.asyncio
@pytest.mark.parametrize("constructor_fails", [False, True])
async def test_cancelled_factory_drains_constructor_before_return(
    tmp_path, monkeypatch, constructor_fails
):
    from kiro_crew.workflows import service

    entered = asyncio.Event()
    release, finished = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    real_constructor = service.WorkflowDefinitionLibrary
    restored = []

    def library_constructor():
        loop.call_soon_threadsafe(entered.set)
        try:
            if not release.wait(10):
                raise TimeoutError("test constructor was not released")
            if constructor_fails:
                raise RuntimeError("test constructor failure")
            return real_constructor(base_dir=tmp_path)
        finally:
            finished.set()

    async def restore(self):
        restored.append(self)
        return 0

    monkeypatch.setattr(service, "WorkflowDefinitionLibrary", library_constructor)
    monkeypatch.setattr(RunRegistry, "load_persisted_async", restore)
    creating = asyncio.create_task(WorkflowService.create(sessions=None, persist=False))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        creating.cancel()
        await asyncio.sleep(0)
        creating.cancel()
        await asyncio.sleep(0)
        assert not creating.done()
        assert not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(creating, 5)
        assert finished.is_set()
        assert restored == []
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(creating, return_exceptions=True), 5)


@pytest.mark.asyncio
async def test_dashboard_task_reads_and_cancel_remain_available_during_init(tmp_path, monkeypatch):
    from test_dashboard_server_startup_coverage import _dashboard
    from test_handlers_taskrunner_coverage import _body, _request

    from kiro_crew.dashboard.handlers.taskrunner import (
        api_taskrunner_cancel,
        api_taskrunner_start,
        api_taskrunner_status,
    )
    from kiro_crew.task_models import Project
    from kiro_crew.taskrunner import TaskRunner

    entered, release = asyncio.Event(), asyncio.Event()
    real_create = WorkflowService.create

    async def delayed(**kwargs):
        entered.set()
        await release.wait()
        return await real_create(**kwargs, persist=False)

    monkeypatch.setattr(WorkflowService, "create", delayed)
    driver = TaskRunner(sessions=MagicMock(), work_dir=tmp_path / "tasks")
    driver._runs["existing"] = Project(
        spec_path="", spec_content="", task_id="existing", source="dashboard", status="running"
    )
    active = asyncio.create_task(asyncio.Event().wait())
    driver._tasks["existing"] = active
    spec = tmp_path / "new-task.txt"
    spec.write_text("Synthetic task", encoding="utf-8")
    try:
        async with _dashboard(tmp_path, monkeypatch, task_runner=driver) as (_, state, _):
            initializer = next(
                t for t in state._background_tasks if t.get_name() == "workflow-initialization"
            )
            try:
                await asyncio.wait_for(entered.wait(), 5)
                response = await api_taskrunner_status(_request(state, "GET"))
                assert response.status == 200
                assert _body(response)["available"] is True
                assert _body(response)["runs"][0]["task_id"] == "existing"
                response = await api_taskrunner_start(
                    _request(state, json_body={"spec": str(spec)})
                )
                assert response.status == 503
                assert "initializing workflows" in _body(response)["error"]
                assert set(driver._runs) == {"existing"}
                response = await api_taskrunner_cancel(
                    _request(state, json_body={"task_id": "existing"})
                )
                assert response.status == 200 and _body(response)["ok"]
                await asyncio.gather(active, return_exceptions=True)
                assert active.cancelled()
                release.set()
                await asyncio.wait_for(initializer, 5)
                assert state.task_runner is driver
                assert state.workflow_service is not None
                assert _body(await api_taskrunner_status(_request(state, "GET")))["available"]
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(initializer, return_exceptions=True), 5)
    finally:
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["delete", "project_delete", "update_plan", "update_task"])
@pytest.mark.parametrize("init_fails", [False, True])
async def test_restored_link_mutations_wait_for_attachment(tmp_path, operation, init_fails):
    from test_handlers_taskrunner_coverage import _body, _request

    from kiro_crew.dashboard.handlers.taskrunner import (
        api_taskrunner_delete,
        api_taskrunner_update_plan,
        api_taskrunner_update_task,
    )
    from kiro_crew.task_models import Project, Task
    from kiro_crew.task_planner import plan_to_yaml
    from kiro_crew.taskrunner import TaskRunner

    store = WorkflowRunStore(tmp_path / "workflows")
    workflows = WorkflowService(sessions=None, store=store)
    driver = TaskRunner(sessions=MagicMock(), work_dir=tmp_path / "tasks")
    run = Project(
        spec_path="", spec_content="", task_id="existing", status="planned", source="dashboard"
    )
    run.tasks = [Task(index=1, title="Original", description="Synthetic step")]
    run.workflow_run_id = await workflows.begin_host_run(
        name="Existing",
        source=plan_to_yaml(run.tasks),
        source_format="task-plan",
        driver="taskrunner",
        task_id=run.task_id,
    )
    await workflows.pause(run.workflow_run_id)
    driver._runs[run.task_id] = run
    await driver._apersist_runs()
    driver = TaskRunner(sessions=MagicMock(), work_dir=tmp_path / "tasks")
    driver.defer_workflow_attachment()
    state = SimpleNamespace(task_runner=driver)
    task_before = await asyncio.to_thread(driver._runs_path().read_bytes)
    workflow_before = await asyncio.to_thread(store.load_all)
    with pytest.raises(RuntimeError, match="initializing workflows"):
        if operation in {"delete", "project_delete"}:
            await driver.delete_run("existing")
        elif operation == "update_plan":
            await driver.update_plan("existing", [{"index": 1, "title": "Changed"}])
        else:
            await driver.update_task("existing", 1, {"title": "Changed"})

    async def mutate():
        if operation == "project_delete":
            from kiro_crew.dashboard.handlers_project import api_project_delete

            return await api_project_delete(
                _request(state, "DELETE", match_info={"id": "existing"})
            )
        match = {"task_id": "existing", "index": "1"}
        if operation == "delete":
            return await api_taskrunner_delete(_request(state, "DELETE", match_info=match))
        if operation == "update_plan":
            return await api_taskrunner_update_plan(
                _request(
                    state,
                    "PUT",
                    match_info=match,
                    json_body={
                        "steps": [{"index": 1, "title": "Changed", "description": "Changed step"}]
                    },
                )
            )
        return await api_taskrunner_update_task(
            _request(state, "PATCH", match_info=match, json_body={"title": "Changed"})
        )

    response = await mutate()
    assert response.status == 503
    assert _body(response)["code"] == "workflow_initializing"
    assert "initializing workflows" in _body(response)["error"]
    assert driver._runs["existing"].tasks[0].title == "Original"
    assert await asyncio.to_thread(driver._runs_path().read_bytes) == task_before
    assert await asyncio.to_thread(store.load_all) == workflow_before

    restored = await WorkflowService.create(sessions=None, store=store)
    driver.attach_workflow_service(None if init_fails else restored)
    response = await mutate()
    assert response.status == 200, _body(response)
    disk = await asyncio.to_thread(store.load_all)
    if operation in {"delete", "project_delete"}:
        assert "existing" not in driver._runs
    else:
        assert driver._runs["existing"].tasks[0].title == "Changed"
    if init_fails:
        assert disk == workflow_before  # documented standalone fallback
    elif operation in {"delete", "project_delete"}:
        assert disk == []
    else:
        assert "Changed" in disk[0]["source"]


@pytest.mark.asyncio
async def test_from_chat_refuses_before_creating_an_unlinked_plan(tmp_path):
    from test_handlers_taskrunner_coverage import _body, _request

    from kiro_crew.dashboard.handlers.taskrunner import api_taskrunner_from_chat
    from kiro_crew.taskrunner import TaskRunner

    driver = TaskRunner(sessions=MagicMock(), work_dir=tmp_path / "tasks")
    driver.defer_workflow_attachment()
    before = list(driver._work_dir.iterdir()) if driver._work_dir.exists() else []
    response = await api_taskrunner_from_chat(
        _request(
            SimpleNamespace(task_runner=driver),
            json_body={"steps": [{"index": 1, "title": "Synthetic"}]},
        )
    )
    assert response.status == 503
    assert "initializing workflows" in _body(response)["error"]
    assert driver._runs == {}
    assert (list(driver._work_dir.iterdir()) if driver._work_dir.exists() else []) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route,method",
    [
        ("start", "start_background"),
        ("start_inline", "start_background"),
        ("plan", "plan"),
        ("retry", "retry_from_task"),
        ("execute_plan", "execute_plan"),
        ("delete", "delete_run"),
        ("update_plan", "update_plan"),
        ("update_task", "update_task"),
        ("from_chat", "update_plan"),
    ],
)
@pytest.mark.parametrize("unrelated_error", [False, True])
async def test_task_handler_readiness_errors_are_typed(
    tmp_path, monkeypatch, route, method, unrelated_error
):
    from unittest.mock import AsyncMock

    from test_handlers_taskrunner_coverage import _body, _request

    from kiro_crew.dashboard.handlers import taskrunner as handlers
    from kiro_crew.task_models import Project, Task
    from kiro_crew.taskrunner import TaskRunner

    driver = TaskRunner(sessions=MagicMock(), work_dir=tmp_path / "tasks")
    run = Project(spec_path="", spec_content="", task_id="existing", status="planned")
    run.tasks = [Task(index=1, title="Original", description="Synthetic")]
    driver._runs[run.task_id] = run
    state = SimpleNamespace(task_runner=driver)
    spec = tmp_path / "input.txt"
    spec.write_text("Synthetic task", encoding="utf-8")
    body = {
        "spec": "__inline__:Synthetic task" if route == "start_inline" else str(spec),
        "input": "Synthetic task",
        "steps": [{"index": 1, "title": "Changed"}],
        "title": "Changed",
        "task_id": "existing",
    }
    request = _request(state, match_info={"task_id": "existing", "index": "1"}, json_body=body)
    handler = getattr(handlers, "api_taskrunner_" + ("start" if route == "start_inline" else route))
    if unrelated_error:
        monkeypatch.setattr(driver, method, AsyncMock(side_effect=RuntimeError("storage boom")))
        if route in {"start", "start_inline"}:
            response = await handler(request)
            assert response.status == 400  # preserve the existing broad start-error contract
            assert "storage boom" in _body(response)["error"]
        else:
            with pytest.raises(RuntimeError, match="storage boom"):
                await handler(request)
    else:
        driver.defer_workflow_attachment()
        response = await handler(request)
        assert response.status == 503
        assert _body(response)["code"] == "workflow_initializing"
        assert "initializing workflows" in _body(response)["error"]
        assert driver._runs["existing"].tasks[0].title == "Original"
        assert driver._tasks == {}
    assert driver._plan_task is None
    assert not list(driver._work_dir.glob("TASK_*.md"))


@pytest.mark.asyncio
async def test_project_delete_preserves_unrelated_errors():
    from unittest.mock import AsyncMock

    from test_project_alias import _make_app, _make_request

    from kiro_crew.dashboard.handlers_project import api_project_delete

    runner = SimpleNamespace(delete_run=AsyncMock(side_effect=RuntimeError("storage failure")))
    request = _make_request(_make_app(runner), "DELETE", match_info={"id": "existing"})
    with pytest.raises(RuntimeError, match="storage failure"):
        await api_project_delete(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_factory", [False, True])
async def test_requested_persistence_never_falls_back_when_store_cannot_open(
    monkeypatch, async_factory
):
    from kiro_crew.workflows import service

    factory = MagicMock(side_effect=OSError("store unavailable"))
    monkeypatch.setattr(service, "WorkflowRunStore", factory)
    with pytest.raises(OSError, match="store unavailable"):
        if async_factory:
            await WorkflowService.create(sessions=None)
        else:
            WorkflowService(sessions=None)
    factory.assert_called_once_with()
