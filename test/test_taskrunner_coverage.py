"""Coverage-focused tests for the :mod:`kiro_crew.taskrunner` orchestrator.

Exercises the surfaces the existing task-runner suites leave untouched: the
workspace-dir security gate, plan/update_task validation, ``execute_plan``'s
lifecycle (success, failure, cancellation), run teardown (cleanup, trust
revocation, delete/cancel/pause/retry), lesson extraction, and the runs-registry
persistence + crash-recovery paths.

Every boundary that would need a real host is faked: git (``git_coord``), the
provider/session layer (``SessionManager``), the test runner (``run_tests``),
and the embedding pool. Nothing here spawns a subprocess, touches the sandbox,
or writes outside ``tmp_path``, so the file behaves identically on a CI runner.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import taskrunner as tr
from kiro_crew.safety_override import safety_override
from kiro_crew.task_models import Project, Task, TaskStatus
from kiro_crew.taskrunner import TaskRunner, _auto_approve_scope, _resolve_workspace_dir

# ── Fixtures / helpers ──


def _sessions() -> MagicMock:
    """A SessionManager double covering every method TaskRunner touches."""
    sessions = MagicMock()
    sessions._sessions = {}
    sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.cancel_current = AsyncMock()
    sessions.release_subagent_runtime = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.is_provider_alive = AsyncMock(return_value=None)
    return sessions


def _runner(tmp_path: Path, **kwargs) -> TaskRunner:
    kwargs.setdefault("sessions", _sessions())
    return TaskRunner(auto_test=False, work_dir=Path(tmp_path), **kwargs)


def _seed_run(
    runner: TaskRunner,
    tmp_path: Path,
    *,
    status: str = "planned",
    task_id: str = "plan_1",
    name: str = "demo",
    tasks: list[Task] | None = None,
) -> Project:
    if tasks is None:
        tasks = [Task(index=1, title="One", description="d")]
    run = Project(
        spec_path=str(tmp_path / "spec.md"),
        spec_content="# spec",
        status=status,
        task_id=task_id,
        name=name,
        work_dir=str(tmp_path),
        tasks=tasks,
    )
    runner._runs[task_id] = run
    return run


def _busy_task() -> MagicMock:
    """A stand-in for an in-flight asyncio.Task (done() is False)."""
    task = MagicMock()
    task.done = MagicMock(return_value=False)
    return task


_YAML_SPEC = (
    "agents:\n"
    "  first:\n"
    "    prompt: do a thing\n"
    "  second:\n"
    "    prompt: do another\n"
    "    depends_on: [first]\n"
)


def _scripted_sleep(steps: list):
    """``asyncio.sleep`` stand-in that runs one callback per tick, then cancels.

    Lets a watchdog iteration be driven deterministically without a real delay:
    tick *i* runs ``steps[i]``, and the tick after the script is exhausted
    raises ``CancelledError`` exactly as a cancelled watchdog task would.
    """
    state = {"n": 0}

    async def _sleep(_delay=0, *_args, **_kwargs):
        index = state["n"]
        state["n"] += 1
        if index >= len(steps):
            raise asyncio.CancelledError()
        steps[index]()

    return _sleep


def _noop() -> None:
    return None


# ── _resolve_workspace_dir ──


class TestResolveWorkspaceDir:
    def test_blank_returns_empty_string(self) -> None:
        assert _resolve_workspace_dir("   ") == ""
        assert _resolve_workspace_dir("") == ""

    def test_traversal_is_canonicalized(self, tmp_path: Path) -> None:
        target = tmp_path / "ws"
        target.mkdir()
        raw = str(tmp_path / "ws" / ".." / "ws")
        assert _resolve_workspace_dir(raw) == str(target.resolve())

    def test_sensitive_path_rejected_and_audited(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tr, "is_sensitive_path", lambda p: True)
        audit = MagicMock()
        monkeypatch.setattr(tr, "sel", lambda: audit)
        with pytest.raises(ValueError, match="sensitive/credential path"):
            _resolve_workspace_dir(str(tmp_path))
        kwargs = audit.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["metadata"]["reason"] == "sensitive_path"

    def test_rejection_survives_audit_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dead SEL must not turn a denial into an acceptance."""
        monkeypatch.setattr(tr, "is_sensitive_path", lambda p: True)
        monkeypatch.setattr(tr, "sel", MagicMock(side_effect=RuntimeError("sel down")))
        with pytest.raises(ValueError, match="rejected"):
            _resolve_workspace_dir(str(tmp_path))

    def test_acceptance_survives_audit_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tr, "sel", MagicMock(side_effect=RuntimeError("sel down")))
        assert _resolve_workspace_dir(str(tmp_path)) == str(tmp_path.resolve())


# ── YAML decomposition audit wrapper ──


class TestDecomposeYamlWithAudit:
    def test_success_logs_task_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        audit = MagicMock()
        monkeypatch.setattr(tr, "sel", lambda: audit)
        tasks = tr._decompose_yaml_with_audit(_YAML_SPEC, "plan_1")
        assert [t.index for t in tasks] == [1, 2]
        assert tasks[1].depends_on == [1]
        kwargs = audit.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "ok"
        assert kwargs["metadata"] == {"task_id": "plan_1", "task_count": 2}

    def test_failure_logs_error_and_reraises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        audit = MagicMock()
        monkeypatch.setattr(tr, "sel", lambda: audit)
        with pytest.raises(ValueError):
            tr._decompose_yaml_with_audit("not: a workflow\n", "plan_2")
        kwargs = audit.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "error"
        assert kwargs["metadata"]["task_id"] == "plan_2"


# ── current_run ──


class TestCurrentRun:
    def test_none_when_registry_empty(self, tmp_path: Path) -> None:
        assert _runner(tmp_path).current_run is None

    def test_returns_most_recently_registered(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        _seed_run(runner, tmp_path, task_id="a", name="a")
        last = _seed_run(runner, tmp_path, task_id="b", name="b")
        assert runner.current_run is last


# ── plan() ──


class TestPlan:
    @pytest.mark.asyncio
    async def test_missing_spec_file(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        with pytest.raises(FileNotFoundError):
            await runner.plan(source="file", spec_path=str(tmp_path / "nope.md"))

    @pytest.mark.asyncio
    async def test_empty_spec_file(self, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("   \n", encoding="utf-8", newline="\n")
        runner = _runner(tmp_path)
        with pytest.raises(ValueError, match="empty"):
            await runner.plan(source="file", spec_path=str(spec))

    @pytest.mark.asyncio
    async def test_file_source_decomposes_content(self, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# Build the thing\n", encoding="utf-8", newline="\n")
        runner = _runner(tmp_path)
        with patch.object(
            TaskRunner,
            "_decompose",
            AsyncMock(return_value=[Task(index=1, title="T", description="d")]),
        ) as dec:
            run = await runner.plan(source="file", spec_path=str(spec))
        assert dec.await_args.args[0] == "# Build the thing"
        assert run.status == "planned"
        assert run.spec_content == "# Build the thing"
        assert runner._runs[run.task_id] is run

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["spec", "text"])
    async def test_empty_input_text(self, tmp_path: Path, source: str) -> None:
        runner = _runner(tmp_path)
        with pytest.raises(ValueError, match="Input text is empty"):
            await runner.plan(input_text="  ", source=source)

    @pytest.mark.asyncio
    async def test_text_source_leaves_spec_content_blank(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        with patch.object(
            TaskRunner,
            "_decompose",
            AsyncMock(return_value=[Task(index=1, title="T", description="d")]),
        ):
            run = await runner.plan(input_text="ship it", source="text")
        assert run.spec_content == ""
        assert run.original_input == "ship it"
        assert Path(run.work_dir).is_dir()

    @pytest.mark.asyncio
    async def test_yaml_source_bypasses_llm(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        with patch.object(TaskRunner, "_decompose", AsyncMock()) as dec:
            run = await runner.plan(input_text=_YAML_SPEC, source="yaml")
        dec.assert_not_awaited()
        assert [t.index for t in run.tasks] == [1, 2]

    @pytest.mark.asyncio
    async def test_workspace_override_becomes_work_dir(self, tmp_path: Path) -> None:
        override = tmp_path / "override"
        override.mkdir()
        runner = _runner(tmp_path)
        with patch.object(
            TaskRunner,
            "_decompose",
            AsyncMock(return_value=[Task(index=1, title="T", description="d")]),
        ):
            run = await runner.plan(
                input_text="go", source="text", workspace_dir=str(override)
            )
        assert run.work_dir == str(override.resolve())

    @pytest.mark.asyncio
    async def test_decompose_timeout_becomes_value_error(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        with patch.object(
            TaskRunner, "_decompose", AsyncMock(side_effect=asyncio.TimeoutError())
        ):
            with pytest.raises(ValueError, match="timed out"):
                await runner.plan(input_text="go", source="text")
        assert runner._runs == {}

    @pytest.mark.asyncio
    async def test_decompose_cancelled_becomes_value_error(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        with patch.object(
            TaskRunner, "_decompose", AsyncMock(side_effect=asyncio.CancelledError())
        ):
            with pytest.raises(ValueError, match="cancelled"):
                await runner.plan(input_text="go", source="text")

    @pytest.mark.asyncio
    async def test_empty_plan_rejected(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        with patch.object(TaskRunner, "_decompose", AsyncMock(return_value=[])):
            with pytest.raises(ValueError, match="Could not generate a plan"):
                await runner.plan(input_text="go", source="text")

    def test_cancel_plan_cancels_live_task_only(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner.cancel_plan()  # no plan task — must not raise
        live = _busy_task()
        runner._plan_task = live
        runner.cancel_plan()
        live.cancel.assert_called_once()
        done = MagicMock()
        done.done = MagicMock(return_value=True)
        runner._plan_task = done
        runner.cancel_plan()
        done.cancel.assert_not_called()


# ── update_plan / update_task ──


class TestUpdatePlan:
    @pytest.mark.asyncio
    async def test_unknown_run(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            await runner.update_plan("nope", [])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["running", "cancelling"])
    async def test_refuses_while_active(self, tmp_path: Path, status: str) -> None:
        runner = _runner(tmp_path)
        _seed_run(runner, tmp_path, status=status)
        with pytest.raises(ValueError, match=f"while {status}"):
            await runner.update_plan("plan_1", [])


class TestUpdateTask:
    @pytest.mark.asyncio
    async def test_unknown_run(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            await runner.update_task("nope", 1, {})

    @pytest.mark.asyncio
    async def test_unknown_task_index(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        _seed_run(runner, tmp_path)
        with pytest.raises(ValueError, match="Task 9 not found"):
            await runner.update_task("plan_1", 9, {})

    @pytest.mark.asyncio
    async def test_refuses_non_pending_task(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        _seed_run(
            runner,
            tmp_path,
            tasks=[Task(index=1, title="One", description="d", status=TaskStatus.PASSED)],
        )
        with pytest.raises(ValueError, match="status=passed"):
            await runner.update_task("plan_1", 1, {"title": "x"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "updates,message",
        [
            ({"title": "  "}, "title must be a non-empty string"),
            ({"title": 42}, "title must be a non-empty string"),
            ({"title": "x" * 501}, "title too long"),
            ({"description": 7}, "description must be a string"),
            ({"description": "d" * 5001}, "description too long"),
            ({"depends_on": "1,2"}, "depends_on must be a list"),
        ],
    )
    async def test_validation_errors(
        self, tmp_path: Path, updates: dict, message: str
    ) -> None:
        runner = _runner(tmp_path)
        _seed_run(runner, tmp_path)
        with pytest.raises(ValueError, match=message):
            await runner.update_task("plan_1", 1, updates)

    @pytest.mark.asyncio
    async def test_invalid_field_leaves_task_untouched(self, tmp_path: Path) -> None:
        """Validation happens before any mutation, so a late error rolls nothing back."""
        runner = _runner(tmp_path)
        _seed_run(runner, tmp_path)
        with pytest.raises(ValueError, match="description too long"):
            await runner.update_task(
                "plan_1", 1, {"title": "new", "description": "d" * 5001}
            )
        assert runner._runs["plan_1"].tasks[0].title == "One"

    @pytest.mark.asyncio
    async def test_applies_changes_and_filters_deps(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        _seed_run(
            runner,
            tmp_path,
            tasks=[
                Task(index=1, title="One", description="a"),
                Task(index=2, title="Two", description="b"),
            ],
        )
        result = await runner.update_task(
            "demo",  # resolvable by name too
            2,
            {
                "title": "  Renamed  ",
                "description": "longer",
                # 0 and 5 are out of range (must be 0 < d < index); "x" is not numeric
                "depends_on": [1, 0, 5, "x"],
                "requires_approval": 1,
                "force_approval": 0,
            },
        )
        assert result == {
            "index": 2,
            "title": "Renamed",
            "description": "longer",
            "depends_on": [1],
            "requires_approval": True,
            "force_approval": False,
        }
        assert (tmp_path / "runs.json").exists()


# ── execute_plan ──


class TestExecutePlan:
    @pytest.mark.asyncio
    async def test_unknown_run(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            await runner.execute_plan("nope")

    @pytest.mark.asyncio
    async def test_refuses_non_startable_status(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        _seed_run(runner, tmp_path, status="running")
        with pytest.raises(ValueError, match="not in a startable state"):
            await runner.execute_plan("plan_1")

    @pytest.mark.asyncio
    async def test_concurrency_cap_checked_before_mutation(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = _seed_run(runner, tmp_path, status="failed")
        run.tasks[0].status = TaskStatus.FAILED
        runner._tasks = {f"other{i}": _busy_task() for i in range(3)}
        with pytest.raises(ValueError, match="Too many concurrent tasks"):
            await runner.execute_plan("plan_1")
        # state untouched: the guard runs before the resume reset
        assert run.status == "failed"
        assert run.tasks[0].status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_resume_resets_only_unfinished_tasks(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = _seed_run(
            runner,
            tmp_path,
            status="failed",
            tasks=[
                Task(index=1, title="ok", description="d", status=TaskStatus.PASSED),
                Task(
                    index=2,
                    title="bad",
                    description="d",
                    status=TaskStatus.FAILED,
                    error="boom",
                    attempts=3,
                ),
            ],
        )
        run.error = "boom"
        with patch.object(TaskRunner, "_execute_tasks", AsyncMock()), patch.object(
            TaskRunner, "_watchdog_loop", AsyncMock()
        ), patch.object(tr.git_coord, "init_workspace", AsyncMock()):
            task_id = await runner.execute_plan("plan_1")
            await runner._tasks[task_id]
        assert run.tasks[0].status == TaskStatus.PASSED
        assert run.tasks[1].attempts == 0
        assert run.tasks[1].error == ""
        assert run.status == "completed"

    @pytest.mark.asyncio
    async def test_fresh_resets_passed_tasks_too(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = _seed_run(
            runner,
            tmp_path,
            status="paused",
            tasks=[Task(index=1, title="ok", description="d", status=TaskStatus.PASSED)],
        )
        with patch.object(TaskRunner, "_execute_tasks", AsyncMock()), patch.object(
            TaskRunner, "_watchdog_loop", AsyncMock()
        ), patch.object(tr.git_coord, "init_workspace", AsyncMock()):
            task_id = await runner.execute_plan("plan_1", fresh=True)
            await runner._tasks[task_id]
        assert run.tasks[0].attempts == 0
        assert run.status == "completed"

    @pytest.mark.asyncio
    async def test_workspace_override_only_for_planned_runs(self, tmp_path: Path) -> None:
        override = tmp_path / "elsewhere"
        override.mkdir()
        runner = _runner(tmp_path)
        run = _seed_run(runner, tmp_path, status="paused")
        original = run.work_dir
        with patch.object(TaskRunner, "_execute_tasks", AsyncMock()), patch.object(
            TaskRunner, "_watchdog_loop", AsyncMock()
        ), patch.object(tr.git_coord, "init_workspace", AsyncMock()):
            task_id = await runner.execute_plan("plan_1", workspace_dir=str(override))
            await runner._tasks[task_id]
        assert run.work_dir == original

    @pytest.mark.asyncio
    async def test_happy_path_grants_then_revokes_trust(self, tmp_path: Path) -> None:
        notify = AsyncMock()
        consolidator = MagicMock()
        runner = _runner(tmp_path, on_notify=notify, consolidator=consolidator)
        run = _seed_run(runner, tmp_path, task_id="plan_trust")
        run.branch_name = "feat/x"
        scope = _auto_approve_scope("plan_trust")
        finalize = AsyncMock()
        with patch.object(TaskRunner, "_execute_tasks", AsyncMock()) as exec_tasks, patch.object(
            TaskRunner, "_watchdog_loop", AsyncMock()
        ), patch.object(tr.git_coord, "init_workspace", AsyncMock()) as init_ws, patch.object(
            tr.git_coord, "finalize", finalize
        ):
            task_id = await runner.execute_plan("plan_trust", auto_approve=True)
            assert safety_override().is_scope_active(scope) is True
            await runner._tasks[task_id]
        exec_tasks.assert_awaited_once()
        init_ws.assert_awaited_once()
        finalize.assert_awaited_once()
        consolidator.maybe_consolidate.assert_called_once_with("taskrunner:run:plan_trust")
        assert run.status == "completed"
        assert run.finished_at > 0
        assert task_id not in runner._tasks
        # trust never outlives the run
        assert safety_override().is_scope_active(scope) is False

    @pytest.mark.asyncio
    async def test_git_init_failure_does_not_abort_run(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = _seed_run(runner, tmp_path)
        with patch.object(TaskRunner, "_execute_tasks", AsyncMock()), patch.object(
            TaskRunner, "_watchdog_loop", AsyncMock()
        ), patch.object(
            tr.git_coord, "init_workspace", AsyncMock(side_effect=RuntimeError("no git"))
        ):
            task_id = await runner.execute_plan("plan_1")
            await runner._tasks[task_id]
        assert run.status == "completed"

    @pytest.mark.asyncio
    async def test_execution_error_marks_run_failed(self, tmp_path: Path) -> None:
        notify = AsyncMock()
        runner = _runner(tmp_path, on_notify=notify)
        run = _seed_run(runner, tmp_path)
        with patch.object(
            TaskRunner, "_execute_tasks", AsyncMock(side_effect=RuntimeError("kaboom"))
        ), patch.object(TaskRunner, "_watchdog_loop", AsyncMock()), patch.object(
            tr.git_coord, "init_workspace", AsyncMock()
        ):
            task_id = await runner.execute_plan("plan_1")
            await runner._tasks[task_id]
        assert run.status == "failed"
        assert run.error == "kaboom"
        assert any("error" in call.args[0].lower() for call in notify.await_args_list)

    @pytest.mark.asyncio
    async def test_cancellation_finalizes_as_cancelled(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = _seed_run(
            runner,
            tmp_path,
            tasks=[
                Task(index=1, title="a", description="d", status=TaskStatus.IN_PROGRESS),
                Task(index=2, title="b", description="d"),
            ],
        )
        with patch.object(
            TaskRunner, "_execute_tasks", AsyncMock(side_effect=asyncio.CancelledError())
        ), patch.object(TaskRunner, "_watchdog_loop", AsyncMock()), patch.object(
            tr.git_coord, "init_workspace", AsyncMock()
        ):
            task_id = await runner.execute_plan("plan_1")
            await runner._tasks[task_id]
        assert run.status == "cancelled"
        assert run.tasks[0].status == TaskStatus.CANCELLED
        assert run.tasks[1].status == TaskStatus.CANCELLED


class TestPlanToChatContext:
    def test_unknown_run(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            runner.plan_to_chat_context("nope")

    def test_renders_known_run(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        _seed_run(runner, tmp_path)
        assert "One" in runner.plan_to_chat_context("plan_1")


# ── Teardown: sessions, trust, runtime ──


class TestCleanupRunSessions:
    @pytest.mark.asyncio
    async def test_no_sessions_still_releases_runtime(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = _seed_run(runner, tmp_path)
        await runner._cleanup_run_sessions(run)
        runner._sessions.cancel_current.assert_not_awaited()
        runner._sessions.release_subagent_runtime.assert_awaited_once_with(
            "taskrunner:plan_1:runtime"
        )

    @pytest.mark.asyncio
    async def test_cancels_releases_and_resets_run_sessions_only(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = _seed_run(runner, tmp_path)
        runner._sessions._sessions = {
            "taskrunner:plan_1:task1": object(),
            "taskrunner:other:task1": object(),
            "dashboard": object(),
        }
        with patch("asyncio.sleep", AsyncMock()):
            await runner._cleanup_run_sessions(run)
        assert [c.args[0] for c in runner._sessions.cancel_current.await_args_list] == [
            "taskrunner:plan_1:task1"
        ]
        assert [c.args[0] for c in runner._sessions.reset.await_args_list] == [
            "taskrunner:plan_1:task1"
        ]
        assert run.error == ""

    @pytest.mark.asyncio
    async def test_reset_failure_is_recorded_on_the_run(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = _seed_run(runner, tmp_path)
        runner._sessions._sessions = {"taskrunner:plan_1:task1": object()}
        runner._sessions.cancel_current = AsyncMock(side_effect=RuntimeError("nope"))
        runner._sessions.release = MagicMock(side_effect=RuntimeError("nope"))
        runner._sessions.reset = AsyncMock(side_effect=RuntimeError("stuck"))
        with patch("asyncio.sleep", AsyncMock()):
            await runner._cleanup_run_sessions(run)
        assert run.error == "Cancel cleanup failed for 1 session(s)"
        runner._sessions.release_subagent_runtime.assert_awaited_once()


class TestRunTrust:
    def test_grant_and_revoke_keep_flag_and_scope_in_sync(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = _seed_run(runner, tmp_path, task_id="trust_sync")
        scope = _auto_approve_scope("trust_sync")
        runner._grant_run_trust(run, True)
        assert run.auto_approve is True
        assert safety_override().is_scope_active(scope) is True
        runner._grant_run_trust(run, False)
        assert run.auto_approve is False
        assert safety_override().is_scope_active(scope) is False

    @pytest.mark.asyncio
    async def test_release_runtime_revokes_scope(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = _seed_run(runner, tmp_path, task_id="trust_release")
        runner._grant_run_trust(run, True)
        await runner._release_run_runtime(run)
        assert safety_override().is_scope_active(_auto_approve_scope("trust_release")) is False

    @pytest.mark.asyncio
    async def test_release_runtime_swallows_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _runner(tmp_path)
        run = _seed_run(runner, tmp_path)
        runner._sessions.release_subagent_runtime = AsyncMock(side_effect=RuntimeError("x"))
        monkeypatch.setattr(
            tr, "safety_override", MagicMock(side_effect=RuntimeError("override down"))
        )
        await runner._release_run_runtime(run)  # must not raise


# ── delete / cancel / pause ──


class TestDeleteRun:
    @pytest.mark.asyncio
    async def test_unknown_run_returns_false(self, tmp_path: Path) -> None:
        assert await _runner(tmp_path).delete_run("nope") is False

    @pytest.mark.asyncio
    async def test_running_run_is_cancelled_and_dropped(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        _seed_run(runner, tmp_path, status="running")
        live = _busy_task()
        runner._tasks["plan_1"] = live
        runner._stall_cancelled_ids.add("plan_1")
        assert await runner.delete_run("plan_1") is True
        live.cancel.assert_called_once()
        assert "plan_1" not in runner._runs
        assert "plan_1" not in runner._tasks
        assert "plan_1" not in runner._stall_cancelled_ids

    @pytest.mark.asyncio
    async def test_audit_failure_does_not_fail_delete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _runner(tmp_path)
        _seed_run(runner, tmp_path)
        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", MagicMock(side_effect=RuntimeError("sel down")))
        assert await runner.delete_run("plan_1") is True
        assert runner._runs == {}


class TestCancelAndPause:
    def test_cancel_by_name_resolves_every_match(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        _seed_run(runner, tmp_path, task_id="a", name="shared", status="running")
        _seed_run(runner, tmp_path, task_id="b", name="shared", status="running")
        tasks = {key: _busy_task() for key in ("a", "b")}
        runner._tasks.update(tasks)
        runner.cancel("shared")
        assert [r.status for r in runner._runs.values()] == ["cancelling", "cancelling"]
        for task in tasks.values():
            task.cancel.assert_called_once()

    def test_cancel_unknown_id_is_a_noop(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        _seed_run(runner, tmp_path, status="running")
        runner.cancel("ghost")
        assert runner._runs["plan_1"].status == "running"

    def test_cancel_all_touches_running_runs_only(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        _seed_run(runner, tmp_path, task_id="a", name="a", status="running")
        _seed_run(runner, tmp_path, task_id="b", name="b", status="completed")
        live, done = _busy_task(), MagicMock()
        done.done = MagicMock(return_value=True)
        runner._tasks.update({"a": live, "b": done})
        runner.cancel()
        assert runner._runs["a"].status == "cancelling"
        assert runner._runs["b"].status == "completed"
        live.cancel.assert_called_once()
        done.cancel.assert_not_called()

    def test_pause_running_run_by_name(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        _seed_run(runner, tmp_path, status="running")
        live = _busy_task()
        runner._tasks["plan_1"] = live
        runner.pause("demo")
        assert runner._runs["plan_1"].status == "pausing"
        live.cancel.assert_called_once()

    def test_pause_ignores_unknown_and_idle_runs(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        _seed_run(runner, tmp_path, status="paused")
        runner.pause("ghost")
        runner.pause("plan_1")
        assert runner._runs["plan_1"].status == "paused"


# ── retry_from_task ──


class TestRetryFromTask:
    @pytest.mark.asyncio
    async def test_unknown_run(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not found"):
            await _runner(tmp_path).retry_from_task("nope", 1)

    @pytest.mark.asyncio
    async def test_refuses_running_run(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        _seed_run(runner, tmp_path, status="running")
        with pytest.raises(ValueError, match="Cannot retry a running task"):
            await runner.retry_from_task("plan_1", 1)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["cancelling", "pausing"])
    async def test_refuses_mid_cancel(self, tmp_path: Path, status: str) -> None:
        runner = _runner(tmp_path)
        _seed_run(runner, tmp_path, status=status)
        with pytest.raises(ValueError, match="cancel is in progress"):
            await runner.retry_from_task("plan_1", 1)

    @pytest.mark.asyncio
    async def test_resets_from_index_and_completes(self, tmp_path: Path) -> None:
        notify = AsyncMock()
        runner = _runner(tmp_path, on_notify=notify)
        run = _seed_run(
            runner,
            tmp_path,
            status="failed",
            tasks=[
                Task(index=1, title="a", description="d", status=TaskStatus.PASSED),
                Task(
                    index=2,
                    title="b",
                    description="d",
                    status=TaskStatus.FAILED,
                    error="boom",
                    attempts=2,
                ),
            ],
        )
        run.finished_at = 123.0
        with patch.object(TaskRunner, "_execute_tasks", AsyncMock()) as exec_tasks, patch.object(
            TaskRunner, "_watchdog_loop", AsyncMock()
        ):
            task_id = await runner.retry_from_task("plan_1", 2)
            await runner._tasks[task_id]
        exec_tasks.assert_awaited_once()
        assert run.tasks[0].status == TaskStatus.PASSED
        assert run.tasks[1].status == TaskStatus.PENDING
        assert run.tasks[1].attempts == 0
        assert run.status == "completed"
        assert run.finished_at > 123.0

    @pytest.mark.asyncio
    async def test_reinits_git_when_work_dir_is_missing(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = _seed_run(runner, tmp_path, status="failed")
        run.branch_name = "feat/x"
        run.work_dir = str(tmp_path / "gone")
        init_ws = AsyncMock(side_effect=RuntimeError("git absent"))
        with patch.object(TaskRunner, "_execute_tasks", AsyncMock()), patch.object(
            TaskRunner, "_watchdog_loop", AsyncMock()
        ), patch.object(tr.git_coord, "init_workspace", init_ws):
            task_id = await runner.retry_from_task("plan_1", 1)
            await runner._tasks[task_id]
        init_ws.assert_awaited_once()
        assert run.status == "completed"  # git failure is non-fatal

    @pytest.mark.asyncio
    async def test_failure_marks_run_failed(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = _seed_run(runner, tmp_path, status="failed")
        with patch.object(
            TaskRunner, "_execute_tasks", AsyncMock(side_effect=RuntimeError("nope"))
        ), patch.object(TaskRunner, "_watchdog_loop", AsyncMock()):
            task_id = await runner.retry_from_task("plan_1", 1)
            await runner._tasks[task_id]
        assert run.status == "failed"
        assert run.error == "nope"

    @pytest.mark.asyncio
    async def test_pausing_mid_retry_finalizes_as_paused(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = _seed_run(
            runner,
            tmp_path,
            status="failed",
            tasks=[Task(index=1, title="a", description="d")],
        )

        async def _pause_then_cancel(*_args, **_kwargs):
            run.status = "pausing"
            run.tasks[0].status = TaskStatus.IN_PROGRESS
            raise asyncio.CancelledError()

        with patch.object(
            TaskRunner, "_execute_tasks", AsyncMock(side_effect=_pause_then_cancel)
        ), patch.object(TaskRunner, "_watchdog_loop", AsyncMock()):
            task_id = await runner.retry_from_task("plan_1", 1)
            await runner._tasks[task_id]
        assert run.status == "paused"
        assert run.tasks[0].status == TaskStatus.PENDING


# ── start_background failure paths ──


class TestStartBackground:
    @pytest.mark.asyncio
    async def test_run_failure_marks_placeholder_failed(self, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# do it\n", encoding="utf-8", newline="\n")
        runner = _runner(tmp_path)
        with patch.object(TaskRunner, "run", AsyncMock(side_effect=RuntimeError("bang"))):
            task_id = await runner.start_background(str(spec))
            await runner._tasks[task_id]
        run = runner._runs[task_id]
        assert run.status == "failed"
        assert run.error == "bang"
        assert run.spec_content == "# do it"
        assert task_id not in runner._tasks

    @pytest.mark.asyncio
    async def test_persist_failure_rolls_back_placeholder(self, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# do it\n", encoding="utf-8", newline="\n")
        runner = _runner(tmp_path)
        with patch.object(
            TaskRunner, "_apersist_runs", AsyncMock(side_effect=OSError("disk full"))
        ):
            with pytest.raises(OSError):
                await runner.start_background(str(spec))
        assert runner._runs == {}
        assert runner._tasks == {}

    @pytest.mark.asyncio
    async def test_prunes_cron_runs_and_caps_history(self, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# do it\n", encoding="utf-8", newline="\n")
        runner = _runner(tmp_path)
        cron = _seed_run(runner, tmp_path, task_id="cron_done", name="c", status="completed")
        cron.source = "cron"
        for i in range(12):
            _seed_run(
                runner, tmp_path, task_id=f"old{i:02d}", name=f"o{i}", status="completed"
            )
        with patch.object(TaskRunner, "run", AsyncMock()):
            task_id = await runner.start_background(str(spec))
            await runner._tasks[task_id]
        assert "cron_done" not in runner._runs
        # oldest 2 of the 12 pruned, last 10 kept, plus the new placeholder
        assert "old00" not in runner._runs and "old01" not in runner._runs
        assert "old11" in runner._runs
        assert len(runner._runs) == 11


# ── Lessons ──


class TestLessonExtraction:
    @pytest.mark.asyncio
    async def test_no_store_is_a_noop(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        with patch.object(TaskRunner, "_call_llm_for_lesson", AsyncMock()) as call:
            await runner._extract_lesson(Task(index=1, title="t", description="d"))
        call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_rule_writes_nothing(self, tmp_path: Path) -> None:
        store = MagicMock()
        runner = _runner(tmp_path, lesson_store=store)
        with patch.object(
            TaskRunner, "_call_llm_for_lesson", AsyncMock(return_value={"category": "tool"})
        ):
            await runner._extract_lesson(Task(index=1, title="t", description="d"))
        store.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_saves_to_lesson_store_and_records_on_run(self, tmp_path: Path) -> None:
        store = MagicMock()
        notify = AsyncMock()
        runner = _runner(tmp_path, lesson_store=store, on_notify=notify)
        run = _seed_run(runner, tmp_path)
        task = Task(index=3, title="t", description="d", error="E" * 600)
        with patch.object(
            TaskRunner,
            "_call_llm_for_lesson",
            AsyncMock(return_value={"rule": "R", "category": "tool", "negative": "N"}),
        ) as call:
            await runner._extract_lesson(task, run=run)
        store.save.assert_called_once()
        lesson = store.save.call_args.args[0]
        assert (lesson.rule, lesson.category, lesson.negative) == ("R", "tool", "N")
        # the failure text handed to the LLM is truncated
        assert 'Error: "' + "E" * 500 + '"' in call.await_args.args[0]
        assert run.lessons_learned == ["R"]
        notify.assert_awaited()

    @pytest.mark.asyncio
    async def test_vector_store_path_uses_embed_pool(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = MagicMock()
        consolidator = MagicMock()
        runner = _runner(tmp_path, lesson_store=store, consolidator=consolidator)
        pool = AsyncMock()
        monkeypatch.setattr(tr, "run_in_embed_pool", pool)
        with patch.object(
            TaskRunner, "_call_llm_for_lesson", AsyncMock(return_value={"rule": "R"})
        ):
            await runner._extract_lesson(Task(index=1, title="t", description="d"))
        store.save.assert_not_called()
        assert pool.await_args.args == (
            consolidator._vector_store.write_lesson,
            "R",
            "tool",
            None,
            "task_runner",
        )

    @pytest.mark.asyncio
    async def test_llm_failure_is_swallowed(self, tmp_path: Path) -> None:
        store = MagicMock()
        runner = _runner(tmp_path, lesson_store=store)
        with patch.object(
            TaskRunner, "_call_llm_for_lesson", AsyncMock(side_effect=RuntimeError("down"))
        ):
            await runner._extract_lesson(Task(index=1, title="t", description="d"))
        store.save.assert_not_called()


class TestCallLlmForLesson:
    @pytest.mark.asyncio
    async def test_returns_parsed_json_and_releases_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _runner(tmp_path)
        monkeypatch.setattr(
            tr, "stream_and_collect_json", AsyncMock(return_value={"rule": "R"})
        )
        assert await runner._call_llm_for_lesson("p") == {"rule": "R"}
        runner._sessions.release.assert_called_once()
        runner._sessions.recycle_background.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_session_failure_returns_none_and_still_recycles(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner._sessions.get_or_create = AsyncMock(side_effect=RuntimeError("no provider"))
        assert await runner._call_llm_for_lesson("p") is None
        runner._sessions.recycle_background.assert_awaited_once()


# ── History logging ──


class TestLogTask:
    def test_without_conversation_log_is_a_noop(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = _seed_run(runner, tmp_path)
        runner._log_task("hist", run, Task(index=1, title="T", description="d"))

    def test_off_loop_appends_user_then_assistant(self, tmp_path: Path) -> None:
        log = MagicMock()
        runner = _runner(tmp_path, conversation_log=log)
        run = _seed_run(runner, tmp_path)
        task = Task(index=2, title="T", description="d", result="R" * 2500)
        runner._log_task("hist", run, task)
        roles = [call.args[1] for call in log.append.call_args_list]
        assert roles == ["user", "assistant"]
        assert log.append.call_args_list[0].args[2] == "[Task: spec.md] Task 2: T"
        assert log.append.call_args_list[1].args[2] == "R" * 2000

    def test_off_loop_uses_task_id_when_spec_path_is_blank(self, tmp_path: Path) -> None:
        log = MagicMock()
        runner = _runner(tmp_path, conversation_log=log)
        run = _seed_run(runner, tmp_path)
        run.spec_path = ""
        runner._log_task("hist", run, Task(index=1, title="T", description="d"))
        assert log.append.call_args_list[0].args[2] == "[Task: plan_1] Task 1: T"
        assert log.append.call_args_list[1].args[2] == "Task completed."

    def test_off_loop_append_failure_is_swallowed(self, tmp_path: Path) -> None:
        log = MagicMock()
        log.append = MagicMock(side_effect=RuntimeError("history locked"))
        runner = _runner(tmp_path, conversation_log=log)
        run = _seed_run(runner, tmp_path)
        runner._log_task("hist", run, Task(index=1, title="T", description="d"))
        log.append.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_loop_offloads_the_write(self, tmp_path: Path) -> None:
        log = MagicMock()
        runner = _runner(tmp_path, conversation_log=log)
        run = _seed_run(runner, tmp_path)
        runner._log_task("hist", run, Task(index=1, title="T", description="d"))
        for _ in range(100):
            if log.append.call_count >= 2:
                break
            await asyncio.sleep(0.005)
        assert [call.args[1] for call in log.append.call_args_list] == ["user", "assistant"]

    @pytest.mark.asyncio
    async def test_on_loop_failure_is_reported_not_raised(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        log = MagicMock()
        log.append = MagicMock(side_effect=RuntimeError("history locked"))
        runner = _runner(tmp_path, conversation_log=log)
        run = _seed_run(runner, tmp_path)
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.taskrunner"):
            runner._log_task("hist", run, Task(index=1, title="T", description="d"))
            for _ in range(100):
                if any("history locked" in rec.getMessage() for rec in caplog.records):
                    break
                await asyncio.sleep(0.005)
        assert any("history locked" in rec.getMessage() for rec in caplog.records)


# ── Watchdog ──


class TestWatchdogLoop:
    @pytest.mark.asyncio
    async def test_returns_when_run_leaves_running(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = _seed_run(runner, tmp_path, status="running")

        def _finish() -> None:
            run.status = "completed"

        with patch("asyncio.sleep", _scripted_sleep([_finish])):
            await runner._watchdog_loop(run)
        runner._sessions.is_provider_alive.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dead_process_resets_session_at_threshold(self, tmp_path: Path) -> None:
        notify = AsyncMock()
        runner = _runner(tmp_path, on_notify=notify)
        run = _seed_run(runner, tmp_path, status="running")
        run.started_at = run.last_task_time = time.time()
        run.current_task = 4
        runner._sessions.is_provider_alive = AsyncMock(return_value=False)
        runner._sessions.reset = AsyncMock(side_effect=RuntimeError("stuck"))
        with patch("asyncio.sleep", _scripted_sleep([_noop, _noop])):
            await runner._watchdog_loop(run)
        # _DEAD_THRESHOLD consecutive dead checks before the reset fires
        assert runner._sessions.reset.await_count == 1
        assert runner._sessions.reset.await_args.args == ("taskrunner:plan_1:task4",)
        assert any("process died" in call.args[0] for call in notify.await_args_list)

    @pytest.mark.asyncio
    async def test_global_timeout_stops_the_watchdog(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path, global_timeout=1.0)
        run = _seed_run(runner, tmp_path, status="running")
        run.started_at = time.time() - 30
        run.last_task_time = time.time()
        sleeper = _scripted_sleep([_noop, _noop])
        with patch("asyncio.sleep", sleeper):
            await runner._watchdog_loop(run)
        # returned on the first tick rather than consuming the second
        runner._sessions.is_provider_alive.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stall_cancel_resets_once_per_run(self, tmp_path: Path) -> None:
        notify = AsyncMock()
        runner = _runner(tmp_path, on_notify=notify)
        run = _seed_run(runner, tmp_path, status="running")
        run.started_at = time.time()
        run.last_task_time = time.time() - tr._STALL_CANCEL_TIMEOUT - 60
        runner._sessions.reset = AsyncMock(side_effect=RuntimeError("stuck"))
        with patch("asyncio.sleep", _scripted_sleep([_noop, _noop])):
            await runner._watchdog_loop(run)
        assert "plan_1" in runner._stall_cancelled_ids
        # second tick sees the id already recorded and does not reset again
        assert runner._sessions.reset.await_count == 1
        assert any("stalled task" in call.args[0] for call in notify.await_args_list)


# ── Tests hook ──


class TestRunTests:
    @pytest.mark.asyncio
    async def test_no_command_configured(self, tmp_path: Path) -> None:
        ok, out = await _runner(tmp_path)._run_tests()
        assert (ok, out) == (True, "no test command configured")

    @pytest.mark.asyncio
    async def test_delegates_to_run_tests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _runner(tmp_path)
        runner._test_cmd = ["pytest", "-q"]
        fake = AsyncMock(return_value=(False, "1 failed"))
        monkeypatch.setattr(tr, "run_tests", fake)
        assert await runner._run_tests() == (False, "1 failed")
        assert fake.await_args.args == (["pytest", "-q"], Path(tmp_path))


# ── Registry persistence ──


class TestPersistence:
    def test_cron_and_transient_runs_are_not_serialized(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        keep = _seed_run(runner, tmp_path, task_id="keep", name="keep", status="completed")
        cron = _seed_run(runner, tmp_path, task_id="cron", name="cron", status="completed")
        cron.source = "cron"
        _seed_run(runner, tmp_path, task_id="tmp", name="tmp", status="pending")
        runner._persist_runs()
        data = json.loads((tmp_path / "runs.json").read_text(encoding="utf-8"))
        assert [item["task_id"] for item in data] == [keep.task_id]

    def test_write_failure_does_not_advance_the_watermark(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _runner(tmp_path)
        _seed_run(runner, tmp_path, status="completed")
        monkeypatch.setattr(tr, "atomic_write", MagicMock(side_effect=OSError("full")))
        runner._persist_runs()
        assert runner._persist_written == 0
        assert not (tmp_path / "runs.json").exists()

    def test_stale_snapshot_never_clobbers_a_newer_one(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner._commit_snapshot(5, '["new"]')
        runner._commit_snapshot(2, '["stale"]')
        assert (tmp_path / "runs.json").read_text(encoding="utf-8") == '["new"]'
        assert runner._persist_written == 5


def _registry_item(**overrides) -> dict:
    item = {
        "task_id": "r1",
        "name": "r1",
        "spec_path": "spec.md",
        "status": "running",
        "source": "text",
        "auto_approve": True,
        "task_details": [
            {
                "index": 1,
                "title": "one",
                "description": "d",
                "status": "in_progress",
                "attempts": 2,
            }
        ],
    }
    item.update(overrides)
    return item


class TestLoadRuns:
    def test_missing_file_starts_empty(self, tmp_path: Path) -> None:
        assert _runner(tmp_path)._runs == {}

    def test_corrupt_registry_is_preserved_as_sidecar(self, tmp_path: Path) -> None:
        (tmp_path / "runs.json").write_text("{not json", encoding="utf-8")
        runner = _runner(tmp_path)
        assert runner._runs == {}
        assert (tmp_path / "runs.json.corrupt").exists()
        assert not (tmp_path / "runs.json").exists()

    def test_sidecar_failure_still_starts_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "runs.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(
            Path, "replace", MagicMock(side_effect=OSError("cannot rename"))
        )
        runner = _runner(tmp_path)
        assert runner._runs == {}
        assert (tmp_path / "runs.json").exists()

    def test_running_run_recovers_as_resumable_without_trust(self, tmp_path: Path) -> None:
        (tmp_path / "runs.json").write_text(
            json.dumps([_registry_item()]), encoding="utf-8"
        )
        runner = _runner(tmp_path)
        run = runner._runs["r1"]
        assert run.status == "paused"
        assert "crashed" in run.error
        assert run.tasks[0].status == TaskStatus.PENDING
        assert run.tasks[0].attempts == 1
        assert run.auto_approve is False
        assert safety_override().is_scope_active(_auto_approve_scope("r1")) is False

    def test_running_run_without_tasks_recovers_as_failed(self, tmp_path: Path) -> None:
        (tmp_path / "runs.json").write_text(
            json.dumps([_registry_item(task_details=[])]), encoding="utf-8"
        )
        run = _runner(tmp_path)._runs["r1"]
        assert run.status == "failed"
        assert "decomposition" in run.error

    def test_planning_run_recovers_as_failed(self, tmp_path: Path) -> None:
        (tmp_path / "runs.json").write_text(
            json.dumps([_registry_item(status="planning", task_details=[])]),
            encoding="utf-8",
        )
        run = _runner(tmp_path)._runs["r1"]
        assert run.status == "failed"
        assert "re-plan" in run.error

    def test_cancelling_run_recovers_as_cancelled(self, tmp_path: Path) -> None:
        details = [
            {"index": 1, "title": "a", "description": "", "status": "in_progress", "attempts": 2},
            {"index": 2, "title": "b", "description": "", "status": "pending", "attempts": 0},
            {"index": 3, "title": "c", "description": "", "status": "passed", "attempts": 1},
        ]
        (tmp_path / "runs.json").write_text(
            json.dumps([_registry_item(status="cancelling", task_details=details)]),
            encoding="utf-8",
        )
        run = _runner(tmp_path)._runs["r1"]
        assert run.status == "cancelled"
        assert [t.status for t in run.tasks] == [
            TaskStatus.CANCELLED,
            TaskStatus.CANCELLED,
            TaskStatus.PASSED,
        ]
        assert run.tasks[0].attempts == 1

    def test_structural_error_keeps_earlier_runs(self, tmp_path: Path) -> None:
        good = _registry_item(task_id="good", status="completed", task_details=[])
        bad = _registry_item(task_id="bad", status="completed", task_details=[{"title": "x"}])
        (tmp_path / "runs.json").write_text(json.dumps([good, bad]), encoding="utf-8")
        runner = _runner(tmp_path)
        assert list(runner._runs) == ["good"]
