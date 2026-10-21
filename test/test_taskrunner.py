"""Tests for the TaskRunner module.

This module pins the step and run models, a task plan's shared workflow run (admission,
cancellation rollback, rebinds, saved plans), step parsing, step prompts, the status view,
the foreground run and cancel. What a run releases, saves and recovers when a step ends,
fails or dies lives in ``test_taskrunner_recovery.py``; the watchdog, the global timeout,
stall recovery, the token budget and parallel scheduling in ``test_taskrunner_run_steps.py``;
working memory, plan revision, self-review, approval gates and the plan's limits in
``test_taskrunner_planning.py``; the git coordination of a run in
``test_taskrunner_git_coord.py``; a run's name, workspace, parallel cap and originating
session key in ``test_taskrunner_run_inputs.py``; the shared doubles in
``taskrunner_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from taskrunner_helpers import (
    _GENEROUS_DEADLINE,
    _make_mock_provider,
    _make_mock_sessions,
)

import kiro_crew.taskrunner as taskrunner_module
from kiro_crew.task_models import PROGRESS_FILE
from kiro_crew.taskrunner import Step, StepStatus, TaskRun, TaskRunner
from kiro_crew.testing.wait import await_until
from kiro_crew.workflows.service import WorkflowService
from kiro_crew.workflows.store import WorkflowRunStore

# ── Fixtures ──


@asynccontextmanager
async def _at_workflow_checkpoint(runner, operation, release, checkpoint="_workflow_begin"):
    """Finish real workflow setup before timing the cancellation trigger.

    Registration allocates a protected ID and persists off-loop, including ACL
    work on Windows. That setup is not the persistence/publication cancellation
    being tested. Keep it real, but hold its last checkpoint until the test is
    ready to start its one-second entry-event wait.
    """
    ready = asyncio.get_running_loop().create_future()
    proceed = asyncio.Event()
    original = getattr(runner, checkpoint)

    async def prepared(run, *args, **kwargs):
        await original(run, *args, **kwargs)
        assert run.workflow_run_id
        assert runner._workflow_service.status(run.workflow_run_id) is not None
        ready.set_result(None)
        await proceed.wait()

    with patch.object(runner, checkpoint, prepared):
        task = asyncio.create_task(operation)
        try:
            done, _ = await asyncio.wait(
                (ready, task), timeout=_GENEROUS_DEADLINE, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                task.result()  # Surface setup exceptions instead of an unrelated event timeout.
                pytest.fail("Operation ended before reaching the workflow checkpoint")
            assert ready in done, f"Workflow setup did not reach {checkpoint}"
            proceed.set()
            yield task
        finally:
            proceed.set()
            release.set()
            ready.cancel()
            if not task.done():
                task.cancel()
            done, _ = await asyncio.wait((task,), timeout=_GENEROUS_DEADLINE)
            assert task in done, "Cancellation test left its operation running"
            if not task.cancelled():
                task.exception()  # Retrieve failures even when the entry-event wait failed.


# ── Step / TaskRun dataclass tests ──


class TestStep:
    def test_defaults(self) -> None:
        step = Step(index=1, title="test", description="desc")
        assert step.status == StepStatus.PENDING
        assert step.attempts == 0
        assert step.error == ""

    def test_status_values(self) -> None:
        assert StepStatus.PASSED.value == "passed"
        assert StepStatus.FAILED.value == "failed"
        assert StepStatus.IN_PROGRESS.value == "in_progress"


class TestTaskRun:
    def test_defaults(self) -> None:
        run = TaskRun(spec_path="/tmp/test.md", spec_content="# Test")
        assert run.status == "pending"
        assert run.tasks == []
        assert run.error == ""


class TestWorkflowRunIntegration:
    @pytest.mark.asyncio
    async def test_closed_gateway_admission_rejects_background_start(self, tmp_path: Path) -> None:
        sessions = _make_mock_sessions()
        sessions.admission_closed = True
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        spec_path = tmp_path / "blocked.md"
        spec_path.write_text("# Blocked task\n", encoding="utf-8")

        with pytest.raises(ValueError, match="gateway admission is closed"):
            await runner.start_background(spec_path)

        assert runner._runs == {}
        assert runner._tasks == {}

    def test_in_flight_planning_counts_as_running(self, tmp_path: Path) -> None:
        runner = TaskRunner(sessions=_make_mock_sessions(), auto_test=False, work_dir=tmp_path)

        runner._start_ids_in_flight.add("plan-racing")

        assert runner.running is True

    @pytest.mark.asyncio
    async def test_closed_gateway_admission_rejects_planning(self, tmp_path: Path) -> None:
        sessions = _make_mock_sessions()
        sessions.admission_closed = True
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        with pytest.raises(ValueError, match="gateway admission is closed"):
            await runner.plan("draft a plan")

        assert runner._start_ids_in_flight == set()
        assert runner._runs == {}

    @pytest.mark.asyncio
    async def test_pause_during_background_preparation_stays_visible(self, tmp_path: Path) -> None:
        sessions = _make_mock_sessions()
        sessions.admission_closed = False
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        spec_path = tmp_path / "racing.md"
        spec_path.write_text("# Racing task\n", encoding="utf-8")
        visible_during_pause: list[bool] = []

        async def close_admission_during_persist() -> None:
            sessions.admission_closed = True
            visible_during_pause.append(runner.running)

        runner._apersist_runs = close_admission_during_persist  # type: ignore[method-assign]
        execute = AsyncMock()
        with patch.object(runner, "run", execute):
            task_id = await runner.start_background(spec_path)
            assert visible_during_pause == [True]
            assert task_id in runner._tasks
            assert runner._start_ids_in_flight == set()
            await runner._tasks[task_id]

        execute.assert_awaited_once()
        assert runner._tasks == {}

    @pytest.mark.asyncio
    async def test_cancelled_plan_memory_inheritance_releases_reservation(
        self, tmp_path: Path
    ) -> None:
        runner = TaskRunner(sessions=_make_mock_sessions(), auto_test=False, work_dir=tmp_path)

        with patch.object(
            runner, "_bind_run_execution", AsyncMock(side_effect=asyncio.CancelledError())
        ):
            with pytest.raises(asyncio.CancelledError):
                await runner.plan("cancel during inherited memory")

        assert runner._start_ids_in_flight == set()
        assert runner.running is False
        assert runner._runs == {}

    @pytest.mark.asyncio
    async def test_cancelled_workflow_begin_rolls_back_background_start(
        self, tmp_path: Path
    ) -> None:
        runner = TaskRunner(sessions=_make_mock_sessions(), auto_test=False, work_dir=tmp_path)
        spec_path = tmp_path / "workflow-cancel.md"
        spec_path.write_text("# Cancel during workflow publication\n", encoding="utf-8")
        delete_link = AsyncMock()
        persist = AsyncMock()

        async def cancel_after_link(run) -> None:
            run.workflow_run_id = "wf-partial"
            raise asyncio.CancelledError()

        runner._workflow_begin = cancel_after_link  # type: ignore[method-assign]
        runner._workflow_delete_link = delete_link  # type: ignore[method-assign]
        runner._apersist_runs = persist  # type: ignore[method-assign]

        with pytest.raises(asyncio.CancelledError):
            await runner.start_background(spec_path, session_key="dashboard:test")

        delete_link.assert_awaited_once()
        persist.assert_awaited_once()
        assert runner._runs == {}
        assert runner._run_session_keys == {}
        assert runner._start_ids_in_flight == set()
        assert runner.running is False

    @pytest.mark.asyncio
    async def test_cancelled_background_start_removes_unowned_workflow_run(
        self, tmp_path: Path
    ) -> None:
        sessions = _make_mock_sessions()
        workflow_store = WorkflowRunStore(tmp_path / "workflow-store")
        workflows = WorkflowService(sessions=sessions, store=workflow_store)
        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            workflow_service=workflows,
        )
        spec_path = tmp_path / "background.md"
        spec_path.write_text("# Background task\n", encoding="utf-8")
        persistence_started = asyncio.Event()
        persistence_release = asyncio.Event()
        persist_calls = 0

        async def block_placeholder_persistence() -> None:
            nonlocal persist_calls
            persist_calls += 1
            if persist_calls == 1:
                persistence_started.set()
                await persistence_release.wait()

        runner._apersist_runs = block_placeholder_persistence  # type: ignore[method-assign]
        async with _at_workflow_checkpoint(
            runner, runner.start_background(spec_path), persistence_release
        ) as starting:
            await await_until(persistence_started)
            starting.cancel()
            persistence_release.set()

            with pytest.raises(asyncio.CancelledError):
                await await_until(starting)

        assert persist_calls == 2

        assert runner._runs == {}
        assert runner._tasks == {}
        assert runner._start_ids_in_flight == set()
        assert runner.running is False
        assert workflows.list_runs() == []
        assert WorkflowService(sessions=sessions, store=workflow_store).list_runs() == []

    @pytest.mark.asyncio
    async def test_cancelled_plan_publication_removes_unowned_workflow_run(
        self, tmp_path: Path
    ) -> None:
        sessions = _make_mock_sessions()
        workflow_store = WorkflowRunStore(tmp_path / "workflow-store")
        workflows = WorkflowService(sessions=sessions, store=workflow_store)
        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            workflow_service=workflows,
        )
        runner._decompose = AsyncMock(
            return_value=[Step(index=1, title="Implement", description="make the change")]
        )
        publication_started = asyncio.Event()
        publication_release = asyncio.Event()

        async def block_plan_source(_run_id: str, _source: str, *, source_format: str = "") -> bool:
            del source_format
            publication_started.set()
            await publication_release.wait()
            return True

        workflows.set_source = block_plan_source  # type: ignore[method-assign]
        async with _at_workflow_checkpoint(
            runner, runner.plan("implement the feature"), publication_release
        ) as planning:
            await await_until(publication_started)
            planning.cancel()

            with pytest.raises(asyncio.CancelledError):
                await await_until(planning)

        assert runner._runs == {}
        assert workflows.list_runs() == []
        assert WorkflowService(sessions=sessions, store=workflow_store).list_runs() == []
        assert list(tmp_path.glob("plan_*")) == []

    @pytest.mark.asyncio
    async def test_cancelled_plan_drains_persist_before_rollback(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        sessions = _make_mock_sessions()
        workflow_store = WorkflowRunStore(tmp_path / "workflow-store")
        workflows = WorkflowService(sessions=sessions, store=workflow_store)
        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            workflow_service=workflows,
        )
        runner._decompose = AsyncMock(
            return_value=[Step(index=1, title="Implement", description="make the change")]
        )
        # The hook runs on the persist worker thread, so it reports back to the
        # loop with call_soon_threadsafe rather than parking a second executor
        # thread on a threading.Event.
        loop = asyncio.get_running_loop()
        persist_started = asyncio.Event()
        allow_persist = threading.Event()
        persist_finished = asyncio.Event()
        lifecycle: list[str] = []
        original_atomic_write = taskrunner_module.atomic_write
        first_write = True

        def block_first_write(path, content, *, fsync=False):  # type: ignore[no-untyped-def]
            nonlocal first_write
            if first_write and path == runner._runs_path():
                first_write = False
                lifecycle.append("persist_started")
                loop.call_soon_threadsafe(persist_started.set)
                assert allow_persist.wait(timeout=_GENEROUS_DEADLINE)
                lifecycle.append("persist_finished")
                loop.call_soon_threadsafe(persist_finished.set)
            original_atomic_write(path, content, fsync=fsync)

        original_delete = runner._workflow_delete_link

        async def observe_delete(run: TaskRun) -> None:
            lifecycle.append("workflow_deleted")
            await original_delete(run)

        monkeypatch.setattr(taskrunner_module, "atomic_write", block_first_write)
        runner._workflow_delete_link = observe_delete  # type: ignore[method-assign]

        # _workflow_set_plan is the last workflow-store write before the runs.json
        # persist, so the entry wait below covers only the to_thread hop into the
        # hook, not the fsync-backed setup whose duration the host controls.
        async with _at_workflow_checkpoint(
            runner,
            runner.plan("implement the feature"),
            allow_persist,
            checkpoint="_workflow_set_plan",
        ) as planning:
            await asyncio.wait_for(persist_started.wait(), timeout=_GENEROUS_DEADLINE)
            planning.cancel()

            async def release_after_rollback_gets_one_turn() -> None:
                await asyncio.sleep(0)
                allow_persist.set()

            release = asyncio.create_task(release_after_rollback_gets_one_turn())
            with pytest.raises(asyncio.CancelledError):
                await planning
            await release
            await asyncio.wait_for(persist_finished.wait(), timeout=_GENEROUS_DEADLINE)

        assert lifecycle.index("persist_finished") < lifecycle.index("workflow_deleted")
        assert runner._runs == {}
        assert workflows.list_runs() == []
        assert WorkflowService(sessions=sessions, store=workflow_store).list_runs() == []
        assert json.loads((tmp_path / "runs.json").read_text(encoding="utf-8")) == []
        assert list(tmp_path.glob("plan_*")) == []

    @pytest.mark.asyncio
    async def test_cancellation_during_initial_persist_finalizes_both_run_views(
        self, tmp_path: Path
    ) -> None:
        sessions = _make_mock_sessions()
        workflows = WorkflowService(sessions=sessions, persist=False)
        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            workflow_service=workflows,
        )
        spec_path = tmp_path / "cancel-during-setup.yaml"
        spec_path.write_text("agents:\n  test:\n    prompt: run tests\n", encoding="utf-8")
        persist_started = asyncio.Event()
        persist_release = asyncio.Event()
        persist_calls = 0

        async def cancel_first_persist() -> None:
            nonlocal persist_calls
            persist_calls += 1
            if persist_calls == 1:
                persist_started.set()
                await persist_release.wait()

        runner._apersist_runs = cancel_first_persist  # type: ignore[method-assign]
        async with _at_workflow_checkpoint(
            runner,
            runner.run(spec_path, task_id="cancelled_setup", source="yaml"),
            persist_release,
            checkpoint="_workflow_rebind",
        ) as task:
            await await_until(persist_started)
            task.cancel()

            run = await await_until(task)

        assert run.status == "cancelled"
        assert runner._runs[run.task_id].status == "cancelled"
        assert workflows.status(run.workflow_run_id)["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_plan_registers_one_paused_task_plan_run_and_persists_the_link(
        self, tmp_path: Path
    ) -> None:
        sessions = _make_mock_sessions()
        workflows = WorkflowService(sessions=sessions, persist=False)
        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            workflow_service=workflows,
        )
        runner._decompose = AsyncMock(
            return_value=[Step(index=1, title="Implement", description="make the change")]
        )

        run = await runner.plan("implement the feature")

        assert run.workflow_run_id
        snap = workflows.result(run.workflow_run_id)
        assert snap["status"] == "paused"
        assert snap["driver"] == "taskrunner"
        assert snap["task_id"] == run.task_id
        assert snap["source_format"] == "task-plan"
        assert "agents:" in snap["source"]

        restored = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            workflow_service=workflows,
        )
        assert restored._runs[run.task_id].workflow_run_id == run.workflow_run_id

    @pytest.mark.asyncio
    async def test_execute_plan_rebinds_and_finishes_the_same_workflow_run(
        self, tmp_path: Path
    ) -> None:
        sessions = _make_mock_sessions()
        workflows = WorkflowService(sessions=sessions, persist=False)
        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            workflow_service=workflows,
        )
        runner._decompose = AsyncMock(
            return_value=[Step(index=1, title="Implement", description="make the change")]
        )
        run = await runner.plan("implement the feature")
        workflow_run_id = run.workflow_run_id

        async def execute_existing_pipeline(project, history_key):
            project.tasks[0].status = StepStatus.PASSED
            project.tasks[0].result = "done"

        runner._execute_tasks = AsyncMock(side_effect=execute_existing_pipeline)

        await runner.execute_plan(run.task_id)
        await runner._tasks[run.task_id]

        snap = workflows.result(workflow_run_id)
        assert run.status == "completed"
        assert run.workflow_run_id == workflow_run_id
        assert snap["status"] == "finished"
        assert snap["result"]["task_id"] == run.task_id
        assert runner._execute_tasks.await_count == 1

    @pytest.mark.asyncio
    async def test_execute_plan_reopens_a_failed_projection_without_a_second_run(
        self, tmp_path: Path
    ) -> None:
        sessions = _make_mock_sessions()
        workflows = WorkflowService(sessions=sessions, persist=False)
        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            workflow_service=workflows,
        )
        runner._decompose = AsyncMock(
            return_value=[Step(index=1, title="Implement", description="make the change")]
        )
        run = await runner.plan("implement the feature")
        workflow_run_id = run.workflow_run_id
        await workflows.fail(workflow_run_id, "interrupted")

        async def execute_existing_pipeline(project, history_key):
            project.tasks[0].status = StepStatus.PASSED

        runner._execute_tasks = AsyncMock(side_effect=execute_existing_pipeline)
        await runner.execute_plan(run.task_id)
        await runner._tasks[run.task_id]

        assert run.workflow_run_id == workflow_run_id
        assert [item["run_id"] for item in workflows.list_runs()] == [workflow_run_id]
        assert workflows.status(workflow_run_id)["status"] == "finished"

    @pytest.mark.asyncio
    async def test_terminal_projection_follows_taskrunner_persistence(self, tmp_path: Path) -> None:
        sessions = _make_mock_sessions()
        workflows = WorkflowService(sessions=sessions, persist=False)
        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            workflow_service=workflows,
        )
        runner._decompose = AsyncMock(
            return_value=[Step(index=1, title="Implement", description="make the change")]
        )
        run = await runner.plan("implement the feature")
        timeline: list[str] = []
        runner._apersist_runs = AsyncMock(side_effect=lambda: timeline.append("persist"))
        original_finish = workflows.finish

        async def finish(run_id, result):
            timeline.append("finish")
            await original_finish(run_id, result)

        workflows.finish = AsyncMock(side_effect=finish)

        async def execute_existing_pipeline(project, history_key):
            project.tasks[0].status = StepStatus.PASSED

        runner._execute_tasks = AsyncMock(side_effect=execute_existing_pipeline)
        await runner.execute_plan(run.task_id)
        await runner._tasks[run.task_id]

        assert timeline[-2:] == ["persist", "finish"]

    @pytest.mark.asyncio
    async def test_execute_plan_backfills_workflow_link_for_legacy_run(
        self, tmp_path: Path
    ) -> None:
        sessions = _make_mock_sessions()
        workflows = WorkflowService(sessions=sessions, persist=False)
        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            workflow_service=workflows,
        )
        runner._decompose = AsyncMock(
            return_value=[Step(index=1, title="Implement", description="make the change")]
        )
        run = await runner.plan("implement the feature")
        previous_workflow_run_id = run.workflow_run_id
        run.workflow_run_id = ""

        async def execute_existing_pipeline(project, history_key):
            project.tasks[0].status = StepStatus.PASSED
            project.tasks[0].result = "done"

        runner._execute_tasks = AsyncMock(side_effect=execute_existing_pipeline)

        await runner.execute_plan(run.task_id)
        await runner._tasks[run.task_id]

        assert run.workflow_run_id
        assert run.workflow_run_id != previous_workflow_run_id
        assert workflows.status(run.workflow_run_id)["status"] == "finished"

    @pytest.mark.asyncio
    async def test_rebind_persists_replacement_for_evicted_workflow_run(
        self, tmp_path: Path
    ) -> None:
        sessions = _make_mock_sessions()
        workflows = WorkflowService(sessions=sessions, persist=False)
        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            workflow_service=workflows,
        )
        runner._decompose = AsyncMock(
            return_value=[Step(index=1, title="Implement", description="make the change")]
        )
        run = await runner.plan("implement the feature")
        evicted_workflow_run_id = run.workflow_run_id
        assert await workflows.delete_run(evicted_workflow_run_id) is True
        original_phase = workflows.phase

        async def phase_after_durable_link(run_id: str, title: str) -> None:
            restored = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
            assert restored._runs[run.task_id].workflow_run_id == run_id
            await original_phase(run_id, title)

        workflows.phase = phase_after_durable_link  # type: ignore[method-assign]

        await runner._workflow_rebind(run)

        assert run.workflow_run_id != evicted_workflow_run_id
        restored = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        assert restored._runs[run.task_id].workflow_run_id == run.workflow_run_id

    @pytest.mark.asyncio
    async def test_rebind_persists_replacement_for_rejected_workflow_run(
        self, tmp_path: Path
    ) -> None:
        sessions = _make_mock_sessions()
        workflows = WorkflowService(sessions=sessions, persist=False)
        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            workflow_service=workflows,
        )
        runner._decompose = AsyncMock(
            return_value=[Step(index=1, title="Implement", description="make the change")]
        )
        run = await runner.plan("implement the feature")
        rejected_workflow_run_id = run.workflow_run_id
        handle = workflows.registry.get(rejected_workflow_run_id)
        assert handle is not None
        handle.driver = "workflow"
        await workflows.fail(rejected_workflow_run_id, "cannot resume as a host run")

        await runner._workflow_rebind(run)

        assert run.workflow_run_id != rejected_workflow_run_id
        restored = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        assert restored._runs[run.task_id].workflow_run_id == run.workflow_run_id

    @pytest.mark.asyncio
    async def test_saved_task_plan_invocation_uses_exact_yaml_and_records_provenance(
        self, tmp_path: Path
    ) -> None:
        sessions = _make_mock_sessions()
        workflows = WorkflowService(sessions=sessions, persist=False)
        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            workflow_service=workflows,
        )
        workflows.attach_task_runner(runner)
        source = "agents:\n  test:\n    prompt: run tests\n    force_approval: true\n"
        saved = workflows.save_definition(
            source,
            name="Test project",
            slug="test-project",
            source_format="task-plan",
        )["definition"]

        async def execute_existing_pipeline(project, history_key):
            project.tasks[0].status = StepStatus.PASSED
            project.tasks[0].result = "done"

        runner._execute_tasks = AsyncMock(side_effect=execute_existing_pipeline)

        started = await workflows.start_definition(saved["slug"], input_text="from slash")
        await runner._tasks[started["task_id"]]
        run = runner._runs[started["task_id"]]

        assert run.spec_content == source
        assert run.original_input == "from slash"
        assert run.workflow_id == saved["id"]
        assert run.workflow_slug == saved["slug"]
        assert run.workflow_revision == saved["revision"]
        assert run.tasks[0].force_approval is True
        assert started["run_id"] == run.workflow_run_id
        snapshot = workflows.result(started["run_id"])
        assert snapshot["status"] == "finished"
        assert snapshot["source"] == source
        assert snapshot["workflow_id"] == saved["id"]

    @pytest.mark.asyncio
    async def test_editing_saved_task_plan_clears_revision_and_keeps_lineage(
        self, tmp_path: Path
    ) -> None:
        sessions = _make_mock_sessions()
        workflow_store = WorkflowRunStore(tmp_path / "workflow-store")
        workflows = WorkflowService(sessions=sessions, store=workflow_store)
        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            workflow_service=workflows,
        )
        source = "agents:\n  test:\n    prompt: run tests\n"
        saved = workflows.save_definition(
            source,
            name="Test project",
            slug="test-project",
            source_format="task-plan",
        )["definition"]
        run = await runner.plan(
            source,
            source="yaml",
            workflow_name=saved["name"],
            workflow_id=saved["id"],
            workflow_slug=saved["slug"],
            workflow_revision=saved["revision"],
            workflow_source=source,
        )

        await runner.update_task(run.task_id, 1, {"description": run.tasks[0].description})
        assert run.workflow_id == saved["id"]

        await runner.update_task(run.task_id, 1, {"description": "run the full test suite"})

        assert run.workflow_id == ""
        assert run.workflow_slug == ""
        assert run.workflow_revision == 0
        snapshot = workflows.result(run.workflow_run_id)
        assert snapshot["workflow_id"] == ""
        assert snapshot["workflow_slug"] == ""
        assert snapshot["workflow_revision"] == 0
        assert snapshot["derived_from"] == {
            "workflow_id": saved["id"],
            "revision": saved["revision"],
        }
        restored_run = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)._runs[
            run.task_id
        ]
        assert restored_run.workflow_id == ""
        assert restored_run.derived_from_workflow_id == saved["id"]
        assert restored_run.derived_from_revision == saved["revision"]
        restored_snapshot = WorkflowService(sessions=sessions, store=workflow_store).result(
            run.workflow_run_id
        )
        assert restored_snapshot["workflow_id"] == ""
        assert restored_snapshot["derived_from"] == {
            "workflow_id": saved["id"],
            "revision": saved["revision"],
        }

        promoted = await workflows.promote_run_definition(
            run.workflow_run_id,
            name="Adapted test project",
            slug="adapted-test-project",
        )
        assert promoted["ok"] is True
        assert promoted["definition"]["derived_from"] == {
            "workflow_id": saved["id"],
            "revision": saved["revision"],
        }

    @pytest.mark.asyncio
    async def test_saved_task_plan_capacity_rejection_removes_created_run(
        self, tmp_path: Path
    ) -> None:
        sessions = _make_mock_sessions()
        workflows = WorkflowService(sessions=sessions, persist=False)
        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            workflow_service=workflows,
        )
        blockers = [asyncio.create_task(asyncio.Event().wait()) for _ in range(3)]
        runner._tasks.update({f"active_{index}": task for index, task in enumerate(blockers)})
        definition = {
            "id": "wfd_test",
            "slug": "test-project",
            "name": "Test project",
            "revision": 1,
            "source": "agents:\n  test:\n    prompt: run tests\n",
        }

        try:
            started = await runner.start_workflow_definition(definition)
        finally:
            for task in blockers:
                task.cancel()
            await asyncio.gather(*blockers, return_exceptions=True)

        assert started == {
            "error": "Too many concurrent tasks (3/3). "
            "Cancel or wait for a running task to finish."
        }
        assert runner._runs == {}
        assert workflows.list_runs() == []
        restored = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        assert restored._runs == {}

    @pytest.mark.asyncio
    async def test_concurrent_plans_get_distinct_ids_when_the_clock_repeats(
        self, tmp_path: Path
    ) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        runner._decompose = AsyncMock(
            return_value=[Step(index=1, title="Implement", description="make the change")]
        )

        with patch("kiro_crew.taskrunner.time.time_ns", return_value=123):
            first, second = await asyncio.gather(runner.plan("first"), runner.plan("second"))

        # plan() awaits before it takes _start_lock, so either call may reach
        # the id allocation first. Only distinctness is the contract here.
        assert first.task_id != second.task_id
        assert {first.task_id, second.task_id} == {"plan_123", "plan_124"}
        assert set(runner._runs) == {"plan_123", "plan_124"}

    @pytest.mark.asyncio
    async def test_delete_run_removes_its_linked_workflow_run(self, tmp_path: Path) -> None:
        sessions = _make_mock_sessions()
        workflows = WorkflowService(sessions=sessions, persist=False)
        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            workflow_service=workflows,
        )
        runner._decompose = AsyncMock(
            return_value=[Step(index=1, title="Implement", description="make the change")]
        )
        run = await runner.plan("implement the feature")
        workflow_run_id = run.workflow_run_id

        assert await runner.delete_run(run.task_id) is True
        assert workflows.status(workflow_run_id) is None


# ── Parse steps ──


class TestParseSteps:
    def test_valid_json(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        text = json.dumps(
            [
                {"title": "Task ", "description": "Do thing 1"},
                {"title": "Task ", "description": "Do thing 2"},
            ]
        )
        steps = runner._parse_tasks(text)
        assert len(steps) == 2
        assert steps[0].title == "Task "
        assert steps[0].index == 1
        assert steps[1].index == 2

    def test_markdown_fenced_json(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        text = '```json\n[{"title": "A"}]\n```'
        steps = runner._parse_tasks(text)
        assert len(steps) == 1
        assert steps[0].title == "A"

    def test_invalid_json(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        steps = runner._parse_tasks("not json at all")
        assert steps == []

    def test_non_list(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        steps = runner._parse_tasks('{"title": "not a list"}')
        assert steps == []

    def test_items_without_title(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        text = json.dumps(
            [
                {"description": "no title"},
                {"title": "has title"},
            ]
        )
        steps = runner._parse_tasks(text)
        assert len(steps) == 1
        assert steps[0].title == "has title"

    def test_prose_preamble_before_json_object(self) -> None:
        # Reporter's real shape: a one-line preamble before a complete
        # {"steps": [...]} object, despite the prompt demanding bare JSON.
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        body = json.dumps(
            {
                "steps": [
                    {"title": "Set up scaffolding", "description": "Init the repo"},
                    {"title": "Implement feature", "description": "Write the code"},
                ]
            }
        )
        text = "Here is the decomposed task plan you asked for:\n" + body
        steps = runner._parse_tasks(text)
        assert len(steps) == 2
        assert steps[0].title == "Set up scaffolding"
        assert steps[1].index == 2

    def test_prose_preamble_and_suffix_around_json_array(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        text = 'Sure! Plan below.\n[{"title": "A"}, {"title": "B"}]\nLet me know if you need more.'
        steps = runner._parse_tasks(text)
        assert [s.title for s in steps] == ["A", "B"]

    def test_preamble_with_stray_brace_still_finds_body(self) -> None:
        # A brace in the preamble prose must not mask the real JSON body.
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        text = 'Note: use {placeholder} syntax.\n{"steps": [{"title": "A"}]}'
        steps = runner._parse_tasks(text)
        assert len(steps) == 1
        assert steps[0].title == "A"

    def test_preamble_json_with_braces_inside_strings(self) -> None:
        # Braces/brackets inside string values must not end the span early.
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        body = json.dumps({"steps": [{"title": 'Fix "{}" rendering', "description": "x]}"}]})
        steps = runner._parse_tasks("Plan:\n" + body)
        assert len(steps) == 1
        assert steps[0].title == 'Fix "{}" rendering'

    def test_genuinely_unparseable_returns_empty(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        assert runner._parse_tasks("no json here at all") == []
        assert runner._parse_tasks("broken { not json [ anywhere") == []

    def test_json_like_token_in_preamble_does_not_mask_body(self) -> None:
        # A trivial parseable span in the preamble (here "[1]") must not win
        # over the plan-shaped body that follows it.
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        text = 'Steps use depends_on: [1] for ordering.\n{"steps": [{"title": "Real task"}]}'
        steps = runner._parse_tasks(text)
        assert len(steps) == 1
        assert steps[0].title == "Real task"

    def test_empty_plan_snippet_in_preamble_does_not_mask_body(self) -> None:
        # An empty-plan example in the preamble (e.g. '{"steps": []}') must not
        # be selected over the real title-bearing plan that follows it.
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        text = 'The shape is {"steps": []} with items like:\n{"steps": [{"title": "Real task"}]}'
        steps = runner._parse_tasks(text)
        assert len(steps) == 1
        assert steps[0].title == "Real task"

    def test_nested_example_snippet_in_preamble_does_not_starve_scan(self) -> None:
        # A deeply nested example plus stray prose braces before the body must
        # not exhaust the extractor (raw_decode skips past each parsed value).
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        text = (
            'Example: {"a": {"b": {"c": [1, 2, {"d": []}]}}} and tokens {x} {y} {z}.\n'
            '{"steps": [{"title": "Real task"}]}'
        )
        steps = runner._parse_tasks(text)
        assert len(steps) == 1
        assert steps[0].title == "Real task"

    def test_two_different_plans_is_ambiguous_and_fails_safe(self) -> None:
        # A title-bearing worked EXAMPLE before the real plan is ambiguous: the
        # parser cannot know which plan is real, and executing a guess is worse
        # than failing. Two different plan-shaped values -> [] (logged).
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        text = (
            'For example {"steps": [{"title": "Example task"}]} — here is the plan:\n'
            '{"steps": [{"title": "Real task"}]}'
        )
        assert runner._parse_tasks(text) == []

    def test_identical_restated_plan_is_not_ambiguous(self) -> None:
        # A model restating the SAME payload twice is not ambiguity.
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        body = '{"steps": [{"title": "Real task"}]}'
        steps = runner._parse_tasks(f"Plan:\n{body}\nAgain, the plan is:\n{body}")
        assert len(steps) == 1
        assert steps[0].title == "Real task"

    def test_unparseable_error_log_is_bounded_and_redacted(self, caplog) -> None:
        import logging

        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        secret = "ghp_" + "a" * 36
        text = "totally not json " + secret + " " + "x" * 5000
        with caplog.at_level(logging.ERROR, logger="kiro_crew.task_planner"):
            assert runner._parse_tasks(text) == []
        record = next(r for r in caplog.records if "Failed to parse tasks JSON" in r.getMessage())
        message = record.getMessage()
        assert secret not in message
        assert str(len(text)) in message
        # Payload slice is bounded so one record cannot evict the log window.
        assert len(message) < 700


# ── Build step prompt ──


class TestBuildStepPrompt:
    @pytest.mark.asyncio
    async def test_first_step(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        run = TaskRun(spec_path="/tmp/t.md", spec_content="spec")
        run.tasks = [
            Step(index=1, title="First", description="desc1"),
            Step(index=2, title="Second", description="desc2"),
        ]
        prompt = await runner._build_task_prompt(run, run.tasks[0], attempt=1)
        assert "First" in prompt
        assert "desc1" in prompt
        assert "Previous Attempt" not in prompt

    @pytest.mark.asyncio
    async def test_retry_includes_error(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        run = TaskRun(spec_path="/tmp/t.md", spec_content="spec")
        step = Step(index=1, title="Broken", description="desc", error="import failed")
        run.tasks = [step]
        prompt = await runner._build_task_prompt(run, step, attempt=2)
        assert "Previous Attempt Failed" in prompt
        assert "import failed" in prompt

    @pytest.mark.asyncio
    async def test_completed_steps_shown(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        run = TaskRun(spec_path="/tmp/t.md", spec_content="spec")
        run.tasks = [
            Step(index=1, title="Done", description="d", status=StepStatus.PASSED),
            Step(index=2, title="Current", description="c"),
        ]
        prompt = await runner._build_task_prompt(run, run.tasks[1], attempt=1)
        assert "✅ 1. Done" in prompt


# ── Status ──


class TestStatus:
    def test_no_run(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        assert runner.status() == {"running": False, "agent": "", "runs": []}

    def test_with_run(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        run = TaskRun(
            spec_path="/tmp/t.md",
            spec_content="spec",
            status="running",
            tasks=[
                Step(index=1, title="A", description="a", status=StepStatus.PASSED),
                Step(index=2, title="B", description="b"),
            ],
            current_task=2,
        )
        run.task_id = "test_1"
        runner._runs["test_1"] = run
        s = runner.status()
        r = s["runs"][0]
        assert r["status"] == "running"
        assert r["tasks"] == 2
        assert r["completed"] == 1
        assert r["current_task"] == 2


# ── Run integration ──


class TestRun:
    @pytest.mark.asyncio
    async def test_file_not_found(self, tmp_path: Path) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        with pytest.raises(FileNotFoundError):
            await runner.run(tmp_path / "nonexistent.md")

    @pytest.mark.asyncio
    async def test_empty_spec(self, tmp_path: Path) -> None:
        spec = tmp_path / "TASK.md"
        spec.write_text("", encoding="utf-8")
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        with pytest.raises(ValueError, match="empty"):
            await runner.run(spec)

    @pytest.mark.asyncio
    async def test_decompose_failure(self, tmp_path: Path) -> None:
        """If decomposition returns no steps, run fails."""
        spec = tmp_path / "TASK.md"
        spec.write_text("# Build something", encoding="utf-8")

        sessions = _make_mock_sessions()
        provider = _make_mock_provider("not valid json")
        sessions.get_or_create.return_value = (provider, True, False)

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        result = await runner.run(spec)
        assert result.status == "failed"
        assert "decompose" in result.error.lower()

    @pytest.mark.asyncio
    async def test_successful_run(self, tmp_path: Path) -> None:
        """Happy path: decompose + execute all steps."""
        spec = tmp_path / "TASK.md"
        spec.write_text("# Add a feature", encoding="utf-8")

        sessions = _make_mock_sessions()

        step_json = json.dumps(
            [
                {"title": "Create file", "description": "Create foo.py"},
                {"title": "Add tests", "description": "Add test_foo.py"},
            ]
        )
        call_count = 0

        # First call = decompose (returns JSON), subsequent = step execution
        def _make_stream(text: str):
            from kiro_crew.providers.base import LLMEvent

            async def _stream(message: str):
                yield LLMEvent(kind="text_chunk", text=text)
                yield LLMEvent(kind="complete")

            return _stream

        decompose_provider = MagicMock()
        decompose_provider.stream = _make_stream(step_json)
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        step_provider = MagicMock()
        step_provider.stream = _make_stream("Done!")
        step_provider.approve_tool = AsyncMock()
        step_provider.context_usage_pct = MagicMock(return_value=0.0)

        async def _get_or_create(key: str, agent=None, cwd=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if "decompose" in key:
                return decompose_provider, True, False
            return step_provider, call_count == 2, False  # first step = new session

        sessions.get_or_create = _get_or_create

        notifications: list[tuple[str, str]] = []

        async def _on_notify(title: str, body: str, task_id: str = "") -> None:
            notifications.append((title, body))

        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            on_notify=_on_notify,
            work_dir=tmp_path,
        )

        with patch("kiro_crew.task_executor.self_review", return_value=True):
            result = await runner.run(spec)

        assert result.status == "completed"
        assert len(result.tasks) == 2  # 2 decomposed steps
        assert all(s.status == StepStatus.PASSED for s in result.tasks)

        # Check progress file was written
        progress = tmp_path / PROGRESS_FILE
        assert progress.exists()
        content = progress.read_text(encoding="utf-8")
        assert "completed" in content
        assert "Create file" in content

        # Check notifications were sent
        assert any("Task started" in t for t, _ in notifications)
        assert any("Task completed" in t for t, _ in notifications)

    @pytest.mark.asyncio
    async def test_step_failure_with_retries(self, tmp_path: Path) -> None:
        """Step fails all retries → run fails."""
        spec = tmp_path / "TASK.md"
        spec.write_text("# Build something", encoding="utf-8")

        sessions = _make_mock_sessions()

        step_json = json.dumps([{"title": "Broken step", "description": "Will fail"}])

        from kiro_crew.providers.base import LLMEvent

        decompose_provider = MagicMock()

        async def _decompose_stream(message: str):
            yield LLMEvent(kind="text_chunk", text=step_json)
            yield LLMEvent(kind="complete")

        decompose_provider.stream = _decompose_stream
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        # Step provider that always raises
        fail_provider = MagicMock()

        async def _fail_stream(message: str):
            raise RuntimeError("kiro-cli crashed")
            yield  # type: ignore[misc]  # pragma: no cover

        fail_provider.stream = _fail_stream
        fail_provider.approve_tool = AsyncMock()
        fail_provider.context_usage_pct = MagicMock(return_value=0.0)

        async def _get_or_create(key: str, agent=None, cwd=None, **kwargs):
            if "decompose" in key:
                return decompose_provider, True, False
            return fail_provider, True, False

        sessions.get_or_create = _get_or_create

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        with patch.object(runner, "_try_replan", return_value=False):
            result = await runner.run(spec)

        assert result.status == "failed"
        assert result.tasks[0].attempts == 3  # hit max retries
        assert result.tasks[0].status == StepStatus.FAILED


# ── Cancel ──


class TestCancel:
    def test_cancel_not_running(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        runner.cancel()  # should not raise

    def test_running_property(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        assert not runner.running

    def test_reset_incomplete_tasks(self) -> None:
        """Cancel should mark pending/in_progress/reviewing tasks as cancelled, leave passed/failed alone."""
        run = TaskRun(
            task_id="t1",
            spec_path="",
            spec_content="",
            tasks=[
                Step(index=1, title="done", description="", status=StepStatus.PASSED),
                Step(index=2, title="running", description="", status=StepStatus.IN_PROGRESS),
                Step(index=3, title="waiting", description="", status=StepStatus.PENDING),
                Step(index=4, title="review", description="", status=StepStatus.REVIEWING),
                Step(index=5, title="broke", description="", status=StepStatus.FAILED),
            ],
        )
        TaskRunner._reset_incomplete_tasks(run)
        assert run.tasks[0].status == StepStatus.PASSED
        assert run.tasks[1].status == StepStatus.CANCELLED
        assert run.tasks[2].status == StepStatus.CANCELLED
        assert run.tasks[3].status == StepStatus.CANCELLED
        assert run.tasks[4].status == StepStatus.FAILED

    @pytest.mark.asyncio
    async def test_cleanup_run_sessions_only_targets_run_prefix(self) -> None:
        """Session cleanup must only touch sessions matching taskrunner:{task_id}:* prefix."""
        sessions = _make_mock_sessions()
        sessions.cancel_current = AsyncMock()
        sessions._sessions = {
            "taskrunner:abc:task0": MagicMock(),
            "taskrunner:abc:task1": MagicMock(),
            "taskrunner:other:task0": MagicMock(),
            "dashboard:chat1": MagicMock(),
            "background": MagicMock(),
        }
        runner = TaskRunner(sessions=sessions, auto_test=False)
        run = TaskRun(task_id="abc", spec_path="", spec_content="")
        await runner._cleanup_run_sessions(run)
        # Only abc sessions should be cancelled, released, and reset
        cancelled_keys = [c.args[0] for c in sessions.cancel_current.call_args_list]
        released_keys = [c.args[0] for c in sessions.release.call_args_list]
        reset_keys = [c.args[0] for c in sessions.reset.call_args_list]
        assert sorted(cancelled_keys) == ["taskrunner:abc:task0", "taskrunner:abc:task1"]
        assert sorted(released_keys) == ["taskrunner:abc:task0", "taskrunner:abc:task1"]
        assert sorted(reset_keys) == ["taskrunner:abc:task0", "taskrunner:abc:task1"]
