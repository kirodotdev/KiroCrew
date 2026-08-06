"""Tests for the TaskRunner module."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import requires_git
from kiro_crew.task_models import PROGRESS_FILE
from kiro_crew.taskrunner import (
    _STALL_CANCEL_TIMEOUT,
    _STALL_TIMEOUT,
    MAX_TOTAL_TASKS,
    Step,
    StepStatus,
    TaskRun,
    TaskRunner,
    WorkingMemory,
)

# ── Fixtures ──


def _make_mock_sessions() -> MagicMock:
    """Create a mock SessionManager with the methods TaskRunner uses."""
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions._lock = asyncio.Lock()
    sessions._sessions = {}
    sessions.get_or_create = AsyncMock()
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.record_failure = AsyncMock()
    sessions.check_context_usage = MagicMock()
    sessions.close_all = AsyncMock()

    async def _open_task_session(_parent_key, session_key, *, agent=None, cwd=None, approval_policy=""):
        # Fake: the run-scoped shared runtime is mocked away; forward to whatever
        # get_or_create is set to (preserves per-step key/call assertions).
        return await sessions.get_or_create(session_key, agent=agent, cwd=cwd)

    sessions.open_task_session = _open_task_session
    sessions.release_subagent_runtime = AsyncMock()
    return sessions


def _make_mock_provider(text: str = "done") -> MagicMock:
    """Create a mock provider that yields a text chunk + complete event."""
    from kiro_crew.providers.base import LLMEvent

    provider = MagicMock()

    async def _stream(message: str):
        yield LLMEvent(kind="text_chunk", text=text)
        yield LLMEvent(kind="complete")

    provider.stream = _stream
    provider.approve_tool = AsyncMock()
    provider.reject_tool = AsyncMock()
    provider.context_usage_pct = MagicMock(return_value=0.0)
    return provider


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

        with patch.object(runner, "self_review", return_value=True):
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


# ── Resource management ──


class TestResourceManagement:
    """Verify sessions are released for all task lifecycle paths."""

    @pytest.mark.asyncio
    async def test_single_task_session_reset_after_success(self) -> None:
        """After a single task succeeds, its session must be reset."""
        sessions = _make_mock_sessions()
        sessions.reset = AsyncMock()
        runner = TaskRunner(sessions=sessions, auto_test=False)

        async def mock_execute(run, task, hk="", session_key=""):
            task.status = StepStatus.PASSED
            return True

        runner._execute_single_task = mock_execute
        runner._notify = AsyncMock()

        run = TaskRun(task_id="r1", spec_path="", spec_content="test")
        run.status = "running"
        run.started_at = __import__("time").time()
        run.tasks = [Step(index=1, title="T1", description="d")]

        await runner._execute_tasks(run, "hk")

        reset_keys = [c.args[0] for c in sessions.reset.call_args_list]
        assert "taskrunner:r1:task1" in reset_keys

    @pytest.mark.asyncio
    async def test_parallel_tasks_all_sessions_reset(self) -> None:
        """After a parallel group completes, ALL task sessions must be reset."""
        sessions = _make_mock_sessions()
        sessions.reset = AsyncMock()
        runner = TaskRunner(sessions=sessions, auto_test=False)

        async def mock_execute(run, task, hk="", session_key=""):
            task.status = StepStatus.PASSED
            return True

        runner._execute_single_task = mock_execute
        runner._notify = AsyncMock()

        run = TaskRun(task_id="r2", spec_path="", spec_content="test")
        run.status = "running"
        run.started_at = __import__("time").time()
        run.tasks = [Step(index=i, title=f"T{i}", description="d") for i in range(1, 4)]

        await runner._execute_tasks(run, "hk")

        reset_keys = [c.args[0] for c in sessions.reset.call_args_list]
        for i in range(1, 4):
            assert f"taskrunner:r2:task{i}" in reset_keys

    @pytest.mark.asyncio
    async def test_parallel_failure_still_resets_all_sessions(self) -> None:
        """If one task in a parallel group fails, ALL sessions in the group must still be reset."""
        sessions = _make_mock_sessions()
        sessions.reset = AsyncMock()
        runner = TaskRunner(sessions=sessions, auto_test=False)

        call_count = 0

        async def mock_execute(run, task, hk="", session_key=""):
            nonlocal call_count
            call_count += 1
            if task.index == 2:
                task.status = StepStatus.FAILED
                task.error = "boom"
                return False
            task.status = StepStatus.PASSED
            return True

        runner._execute_single_task = mock_execute
        runner._notify = AsyncMock()
        runner._try_replan = AsyncMock(return_value=False)

        run = TaskRun(task_id="r3", spec_path="", spec_content="test")
        run.status = "running"
        run.started_at = __import__("time").time()
        run.tasks = [Step(index=i, title=f"T{i}", description="d") for i in range(1, 4)]

        await runner._execute_tasks(run, "hk")

        reset_keys = [c.args[0] for c in sessions.reset.call_args_list]
        for i in range(1, 4):
            assert (
                f"taskrunner:r3:task{i}" in reset_keys
            ), f"Session for task{i} not reset after parallel failure"

    @pytest.mark.asyncio
    async def test_cancel_releases_semaphores_before_reset(self) -> None:
        """Cancellation must release() before reset() to avoid semaphore leaks."""
        sessions = _make_mock_sessions()
        sessions.cancel_current = AsyncMock()
        sessions._sessions = {
            "taskrunner:x:task0": MagicMock(),
            "taskrunner:x:task1": MagicMock(),
        }
        call_order: list[str] = []

        def track_release(key):
            call_order.append(f"release:{key}")

        async def track_reset(key):
            call_order.append(f"reset:{key}")

        sessions.release = track_release
        sessions.reset = track_reset

        runner = TaskRunner(sessions=sessions, auto_test=False)
        run = TaskRun(task_id="x", spec_path="", spec_content="")
        await runner._cleanup_run_sessions(run)

        # For each key, release must come before reset
        for key in ["taskrunner:x:task0", "taskrunner:x:task1"]:
            ri = call_order.index(f"release:{key}")
            si = call_order.index(f"reset:{key}")
            assert ri < si, f"release must precede reset for {key}: {call_order}"

    @pytest.mark.asyncio
    async def test_pending_tasks_hold_no_sessions(self) -> None:
        """Tasks that never execute (PENDING) should not create any sessions."""
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)

        async def mock_execute(run, task, hk="", session_key=""):
            task.status = StepStatus.FAILED
            task.error = "fail"
            return False

        runner._execute_single_task = mock_execute
        runner._notify = AsyncMock()
        runner._try_replan = AsyncMock(return_value=False)

        run = TaskRun(task_id="r4", spec_path="", spec_content="test")
        run.status = "running"
        run.started_at = __import__("time").time()
        # Task 1 fails, tasks 2-3 depend on it so should never execute
        run.tasks = [
            Step(index=1, title="T1", description="d"),
            Step(index=2, title="T2", description="d", depends_on=[1]),
            Step(index=3, title="T3", description="d", depends_on=[2]),
        ]

        await runner._execute_tasks(run, "hk")

        # Only task1 should have had its session reset (it was the only one that ran)
        reset_keys = [c.args[0] for c in sessions.reset.call_args_list]
        assert "taskrunner:r4:task1" in reset_keys
        assert "taskrunner:r4:task2" not in reset_keys
        assert "taskrunner:r4:task3" not in reset_keys

    @pytest.mark.asyncio
    async def test_cancel_running_project_cleans_up_sessions(self) -> None:
        """cancel() on a running project must trigger _cleanup_run_sessions."""
        sessions = _make_mock_sessions()
        sessions.cancel_current = AsyncMock()
        runner = TaskRunner(sessions=sessions, auto_test=False)

        # Simulate a long-running task that blocks until cancelled
        blocked = asyncio.Event()

        async def mock_execute(run, task, hk="", session_key=""):
            # Register the session so cleanup can find it
            sessions._sessions[session_key] = MagicMock()
            try:
                await blocked.wait()  # blocks forever until cancelled
            except asyncio.CancelledError:
                raise
            return True

        runner._execute_single_task = mock_execute
        runner._notify = AsyncMock()

        run = TaskRun(task_id="cancel1", spec_path="", spec_content="test")
        run.status = "running"
        run.started_at = __import__("time").time()
        run.tasks = [Step(index=1, title="T1", description="d")]
        runner._runs["cancel1"] = run

        # Start execution in background
        async def _bg():
            try:
                await runner._execute_tasks(run, "hk")
            except asyncio.CancelledError:
                run.status = "cancelling"
                runner._reset_incomplete_tasks(run)
            finally:
                await runner._cleanup_run_sessions(run)
                if run.status == "cancelling":
                    run.status = "cancelled"

        task = asyncio.create_task(_bg())
        runner._tasks["cancel1"] = task

        # Let the task start and block
        await asyncio.sleep(0.05)

        # Cancel it
        runner.cancel("cancel1")

        # Wait for the task to fully complete (cancel + finally block)
        await asyncio.wait_for(task, timeout=5.0)

        # Verify: task is cancelled, sessions were cleaned up
        assert run.status == "cancelled"
        assert run.tasks[0].status == StepStatus.CANCELLED
        # _cleanup_run_sessions should have called cancel_current + release + reset
        assert sessions.cancel_current.call_count >= 1
        assert sessions.reset.call_count >= 1

    @pytest.mark.asyncio
    async def test_cancel_mid_parallel_group_resets_all(self) -> None:
        """Cancelling during a parallel group must clean up all sessions in the group."""
        sessions = _make_mock_sessions()
        sessions.cancel_current = AsyncMock()
        runner = TaskRunner(sessions=sessions, auto_test=False)

        started = asyncio.Event()

        async def mock_execute(run, task, hk="", session_key=""):
            sessions._sessions[session_key] = MagicMock()
            if task.index == 1:
                started.set()
                await asyncio.sleep(10)  # block until cancelled
            task.status = StepStatus.PASSED
            return True

        runner._execute_single_task = mock_execute
        runner._notify = AsyncMock()

        run = TaskRun(task_id="cancel2", spec_path="", spec_content="test")
        run.status = "running"
        run.started_at = __import__("time").time()
        # 3 independent tasks = 1 parallel group
        run.tasks = [Step(index=i, title=f"T{i}", description="d") for i in range(1, 4)]
        runner._runs["cancel2"] = run

        async def _bg():
            try:
                await runner._execute_tasks(run, "hk")
            except asyncio.CancelledError:
                run.status = "cancelling"
                runner._reset_incomplete_tasks(run)
            finally:
                await runner._cleanup_run_sessions(run)
                if run.status == "cancelling":
                    run.status = "cancelled"

        task = asyncio.create_task(_bg())
        runner._tasks["cancel2"] = task

        await started.wait()
        runner.cancel("cancel2")
        # Allow cancellation to fully propagate — CI machines may be slower
        for _ in range(20):
            await asyncio.sleep(0.1)
            if run.status == "cancelled":
                break
        else:
            pytest.fail(f"Cancellation did not propagate within 2s; status={run.status}")

        assert run.status == "cancelled"
        # All 3 sessions should have been targeted by cleanup
        cleaned_keys = {c.args[0] for c in sessions.cancel_current.call_args_list}
        for i in range(1, 4):
            key = f"taskrunner:cancel2:task{i}"
            if key in sessions._sessions:
                assert key in cleaned_keys, f"Session {key} not cleaned up"

    @pytest.mark.asyncio
    async def test_cancel_mid_parallel_group_reset_completes(self) -> None:
        """reset() must run to completion under CancelledError (shield fix)."""
        sessions = _make_mock_sessions()
        sessions.cancel_current = AsyncMock()
        reset_completed = []

        async def slow_reset(key: str) -> None:
            await asyncio.sleep(0.1)  # simulates provider.shutdown()
            reset_completed.append(key)

        sessions.reset = slow_reset
        runner = TaskRunner(sessions=sessions, auto_test=False)

        started = asyncio.Event()

        async def mock_execute(run, task, hk="", session_key=""):
            sessions._sessions[session_key] = MagicMock()
            if task.index == 1:
                started.set()
                await asyncio.sleep(10)
            task.status = StepStatus.PASSED
            return True

        runner._execute_single_task = mock_execute
        runner._notify = AsyncMock()

        run = TaskRun(task_id="shield1", spec_path="", spec_content="test")
        run.status = "running"
        run.started_at = __import__("time").time()
        run.tasks = [Step(index=i, title=f"T{i}", description="d") for i in range(1, 4)]
        runner._runs["shield1"] = run

        async def _bg():
            try:
                await runner._execute_tasks(run, "hk")
            except asyncio.CancelledError:
                run.status = "cancelling"
                runner._reset_incomplete_tasks(run)
            finally:
                await runner._cleanup_run_sessions(run)
                if run.status == "cancelling":
                    run.status = "cancelled"

        task = asyncio.create_task(_bg())
        runner._tasks["shield1"] = task

        await started.wait()
        runner.cancel("shield1")
        # Wait for the cancel to actually settle (status reaches 'cancelled' and the
        # shielded resets finish) instead of guessing a fixed sleep. The old
        # `await asyncio.sleep(1.5)` was a timing guess that the slower aarch64
        # CPython 3.10 build worker exceeded under parallel xdist load, leaving the
        # state at 'cancelling' when asserted -- a flaky, worker-speed-dependent
        # failure. Polling the observable end-state is order/speed-independent.
        for _ in range(200):  # up to ~10s
            if run.status == "cancelled" and len(reset_completed) >= 3:
                break
            await asyncio.sleep(0.05)

        assert run.status == "cancelled"
        # Without shield, slow_reset would be interrupted and reset_completed would be empty
        assert (
            len(reset_completed) >= 3
        ), f"Only {len(reset_completed)} resets completed: {reset_completed}"


# ── Save progress ──


class TestSaveProgress:
    def test_save_progress(self, tmp_path: Path) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        run = TaskRun(
            spec_path=str(tmp_path / "TASK.md"),
            spec_content="spec",
            started_at=1000.0,
            status="running",
            tasks=[
                Step(index=1, title="Step A", description="a", status=StepStatus.PASSED),
                Step(
                    index=2,
                    title="Step B",
                    description="b",
                    status=StepStatus.FAILED,
                    error="boom",
                    attempts=2,
                ),
            ],
        )
        runner._save_progress(run)
        progress = tmp_path / PROGRESS_FILE
        assert progress.exists()
        content = progress.read_text(encoding="utf-8")
        assert "Step A" in content
        assert "✅" in content
        assert "❌" in content
        assert "boom" in content
        assert "(attempts: 2)" in content


# ── 12.1a: Checkpoint Resume ──


class TestCheckpointResume:
    def test_load_checkpoint_with_completed_steps(self, tmp_path: Path) -> None:
        """Checkpoint file with ✅ steps → returns set of titles."""
        spec = tmp_path / "TASK.md"
        spec.write_text("# Test", encoding="utf-8")
        progress = tmp_path / PROGRESS_FILE
        progress.write_text(
            "# Task Progress\n"
            "## Steps\n"
            "- ✅ **Step 1:** Create handler\n"
            "- ✅ **Step 2:** Add route (attempts: 2)\n"
            "- ❌ **Step 3:** Write tests\n",
            encoding="utf-8",
        )

        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        result = runner._load_checkpoint(spec)
        assert result is not None
        assert "create handler" in result
        assert "add route" in result
        assert len(result) == 2  # only ✅ steps

    def test_load_checkpoint_no_file(self, tmp_path: Path) -> None:
        """No TASK_PROGRESS.md → returns None."""
        spec = tmp_path / "TASK.md"
        spec.write_text("# Test", encoding="utf-8")

        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        assert runner._load_checkpoint(spec) is None

    def test_load_checkpoint_no_completed(self, tmp_path: Path) -> None:
        """Checkpoint with no ✅ steps → returns None."""
        spec = tmp_path / "TASK.md"
        spec.write_text("# Test", encoding="utf-8")
        progress = tmp_path / PROGRESS_FILE
        progress.write_text(
            "# Task Progress\n" "## Steps\n" "- ❌ **Step 1:** Broken\n",
            encoding="utf-8",
        )

        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        assert runner._load_checkpoint(spec) is None

    @pytest.mark.asyncio
    async def test_resume_skips_completed_steps(self, tmp_path: Path) -> None:
        """Resume with 2/3 completed → starts at step 3."""
        spec = tmp_path / "TASK.md"
        spec.write_text("# Feature", encoding="utf-8")

        # Write checkpoint showing 2 completed steps
        progress = tmp_path / PROGRESS_FILE
        progress.write_text(
            "# Task Progress\n"
            "## Steps\n"
            "- ✅ **Step 1:** Create file\n"
            "- ✅ **Step 2:** Add tests\n"
            "- ⬜ **Step 3:** Update docs\n",
            encoding="utf-8",
        )

        sessions = _make_mock_sessions()
        step_json = json.dumps(
            [
                {"title": "Create file", "description": "Create foo.py"},
                {"title": "Add tests", "description": "Add test_foo.py"},
                {"title": "Update docs", "description": "Update README"},
            ]
        )

        from kiro_crew.providers.base import LLMEvent

        decompose_provider = MagicMock()

        async def _decompose_stream(msg: str):
            yield LLMEvent(kind="text_chunk", text=step_json)
            yield LLMEvent(kind="complete")

        decompose_provider.stream = _decompose_stream
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        step_provider = MagicMock()

        async def _step_stream(msg: str):
            yield LLMEvent(kind="text_chunk", text="Done!")
            yield LLMEvent(kind="complete")

        step_provider.stream = _step_stream
        step_provider.approve_tool = AsyncMock()
        step_provider.context_usage_pct = MagicMock(return_value=0.0)

        execute_count = 0

        async def _get_or_create(key: str, agent=None, cwd=None, **kwargs):
            nonlocal execute_count
            if "decompose" in key:
                return decompose_provider, True, False
            execute_count += 1
            return step_provider, True, False

        sessions.get_or_create = _get_or_create

        notifications: list[tuple[str, str]] = []

        async def _on_notify(title: str, body: str, task_id: str = "") -> None:
            notifications.append((title, body))

        runner = TaskRunner(
            sessions=sessions, auto_test=False, on_notify=_on_notify, work_dir=tmp_path
        )

        with patch("kiro_crew.task_executor.self_review", return_value=True):
            result = await runner.run(spec)

        assert result.status == "completed"
        # Only step 3 should have been executed (1 execute + no self-review)
        assert execute_count == 1
        # Steps 1 & 2 should be PASSED from checkpoint
        assert result.tasks[0].status == StepStatus.PASSED
        assert result.tasks[1].status == StepStatus.PASSED
        assert result.tasks[2].status == StepStatus.PASSED
        # Resume notification should have fired
        assert any("Resuming" in t for t, _ in notifications)

    @pytest.mark.asyncio
    async def test_fresh_flag_ignores_checkpoint(self, tmp_path: Path) -> None:
        """--fresh flag → ignore checkpoint, start over."""
        spec = tmp_path / "TASK.md"
        spec.write_text("# Feature", encoding="utf-8")

        # Write checkpoint showing all steps completed
        progress = tmp_path / PROGRESS_FILE
        progress.write_text(
            "# Task Progress\n" "## Steps\n" "- ✅ **Step 1:** Create file\n",
            encoding="utf-8",
        )

        sessions = _make_mock_sessions()
        step_json = json.dumps([{"title": "Create file", "description": "Create foo.py"}])

        from kiro_crew.providers.base import LLMEvent

        decompose_provider = MagicMock()

        async def _decompose_stream(msg: str):
            yield LLMEvent(kind="text_chunk", text=step_json)
            yield LLMEvent(kind="complete")

        decompose_provider.stream = _decompose_stream
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        step_provider = MagicMock()

        async def _step_stream(msg: str):
            yield LLMEvent(kind="text_chunk", text="Done!")
            yield LLMEvent(kind="complete")

        step_provider.stream = _step_stream
        step_provider.approve_tool = AsyncMock()
        step_provider.context_usage_pct = MagicMock(return_value=0.0)

        execute_count = 0

        async def _get_or_create(key: str, agent=None, cwd=None, **kwargs):
            nonlocal execute_count
            if "decompose" in key:
                return decompose_provider, True, False
            execute_count += 1
            return step_provider, True, False

        sessions.get_or_create = _get_or_create

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path, fresh=True)

        with patch("kiro_crew.task_executor.self_review", return_value=True):
            result = await runner.run(spec)

        assert result.status == "completed"
        # Step should have been executed despite checkpoint
        assert execute_count == 1

    def test_build_resume_context(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        completed = [
            Step(index=1, title="Create file", description="d"),
            Step(index=2, title="Add route", description="d"),
        ]
        ctx = runner._build_resume_context(completed)
        assert "Previously Completed" in ctx
        assert "Create file" in ctx
        assert "Add route" in ctx
        assert "already on disk" in ctx


# ── 12.1b: Session Recovery ──


class TestSessionRecovery:
    @pytest.mark.asyncio
    async def test_process_died_recovers(self, tmp_path: Path) -> None:
        """AcpProcessDied → recovers and completes step."""
        from kiro_crew.acp.client import AcpProcessDied
        from kiro_crew.providers.base import LLMEvent

        sessions = _make_mock_sessions()
        call_count = 0

        fail_provider = MagicMock()

        async def _fail_then_succeed(msg: str):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise AcpProcessDied("Process exited")
            yield LLMEvent(kind="text_chunk", text="Recovered!")
            yield LLMEvent(kind="complete")

        fail_provider.stream = _fail_then_succeed
        fail_provider.approve_tool = AsyncMock()
        fail_provider.context_usage_pct = MagicMock(return_value=0.0)

        sessions.get_or_create = AsyncMock(return_value=(fail_provider, True, False))

        notifications: list[tuple[str, str]] = []

        async def _on_notify(title: str, body: str, task_id: str = "") -> None:
            notifications.append((title, body))

        runner = TaskRunner(
            sessions=sessions, auto_test=False, on_notify=_on_notify, work_dir=tmp_path
        )
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Test step", description="desc")
        run.tasks = [step]

        success = await runner._execute_single_task(run, step)

        assert success
        assert step.status == StepStatus.PASSED
        # Session should have been reset after crash
        sessions.reset.assert_called()
        # Notification about process death
        assert any("process died" in t for t, _ in notifications)

    @pytest.mark.asyncio
    async def test_process_death_does_not_consume_logic_retry(self, tmp_path: Path) -> None:
        """Process dies once then logic fails — should still get full 3 logic attempts."""
        from kiro_crew.acp.client import AcpProcessDied

        sessions = _make_mock_sessions()
        call_count = 0

        provider = MagicMock()

        async def _die_then_fail(msg: str):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise AcpProcessDied("Process exited")
            # Logic failures on all subsequent attempts
            raise RuntimeError("logic error")
            yield  # type: ignore[misc]  # pragma: no cover

        provider.stream = _die_then_fail
        provider.approve_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)

        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Test", description="desc")
        run.tasks = [step]

        success = await runner._execute_single_task(run, step)

        assert not success
        # 1 process death (not counted) + 3 logic retries = 4 total calls
        assert call_count == 4
        # Step should report 3 attempts (logic only)
        assert step.attempts == 3

    @pytest.mark.asyncio
    async def test_process_died_exceeds_budget(self, tmp_path: Path) -> None:
        """AcpProcessDied > _MAX_RECOVERIES → step fails."""
        from kiro_crew.acp.client import AcpProcessDied

        sessions = _make_mock_sessions()

        fail_provider = MagicMock()

        async def _always_die(msg: str):
            raise AcpProcessDied("Process exited")
            yield  # type: ignore[misc]  # pragma: no cover

        fail_provider.stream = _always_die
        fail_provider.approve_tool = AsyncMock()
        fail_provider.context_usage_pct = MagicMock(return_value=0.0)

        sessions.get_or_create = AsyncMock(return_value=(fail_provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Doomed", description="desc")
        run.tasks = [step]

        success = await runner._execute_single_task(run, step)

        assert not success
        assert step.status == StepStatus.FAILED
        assert "Process died" in step.error

    @pytest.mark.asyncio
    async def test_session_reset_between_retries(self, tmp_path: Path) -> None:
        """Session is reset between retry attempts to prevent StreamReader corruption."""
        sessions = _make_mock_sessions()
        call_count = 0

        provider = MagicMock()

        async def _fail_twice_then_succeed(msg: str):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise RuntimeError("readuntil() called while another coroutine is waiting")
            yield LLMEvent(kind="text_chunk", text="ok")
            yield LLMEvent(kind="complete")

        from kiro_crew.providers.base import LLMEvent

        provider.stream = _fail_twice_then_succeed
        provider.approve_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)

        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Retry test", description="desc")
        run.tasks = [step]

        success = await runner._execute_single_task(run, step)

        assert success
        # Session should have been reset between each failed attempt
        reset_calls = [c.args[0] for c in sessions.reset.call_args_list]
        assert len([k for k in reset_calls if "task1" in k]) >= 2

    @pytest.mark.asyncio
    async def test_mid_stream_context_overflow_compacts(self, tmp_path: Path) -> None:
        """Context ≥90% during tool call → compact and retry without burning attempt."""
        from kiro_crew.providers.base import LLMEvent

        sessions = _make_mock_sessions()
        call_count = 0

        provider = MagicMock()

        async def _overflow_then_succeed(msg: str):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: emit a tool request while context is high
                yield LLMEvent(
                    kind="permission_request",
                    title="read",
                    text="",
                    request_id="req-1",
                    tool_kind="tool",
                )
            else:
                yield LLMEvent(kind="text_chunk", text="done after compact")
                yield LLMEvent(kind="complete")

        provider.stream = _overflow_then_succeed
        provider.approve_tool = AsyncMock()
        provider.reject_tool = AsyncMock()
        # Return 92% on first check (triggers overflow), 40% after compact
        provider.context_usage_pct = MagicMock(side_effect=[92.0, 40.0, 40.0])
        provider.compact = AsyncMock()
        provider.wait_for_compaction = AsyncMock(return_value={"type": "completed"})

        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Overflow test", description="desc")
        run.tasks = [step]

        success = await runner._execute_single_task(run, step)

        assert success
        provider.reject_tool.assert_called_once_with("req-1")
        provider.compact.assert_called_once()
        assert "Context window" in (step.result or "")

    @pytest.mark.asyncio
    async def test_mid_stream_context_overflow_exceeds_max_recoveries(self, tmp_path: Path) -> None:
        """Compaction exceeds MAX_RECOVERIES → task fails."""
        from kiro_crew.providers.base import LLMEvent
        from kiro_crew.task_executor import MAX_RECOVERIES

        sessions = _make_mock_sessions()

        provider = MagicMock()

        async def _always_overflow(msg: str):
            yield LLMEvent(
                kind="permission_request",
                title="read",
                text="",
                request_id="req-1",
                tool_kind="tool",
            )

        provider.stream = _always_overflow
        provider.approve_tool = AsyncMock()
        provider.reject_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=95.0)
        provider.compact = AsyncMock()
        provider.wait_for_compaction = AsyncMock(return_value={"type": "completed"})

        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Overflow exhaust", description="desc")
        run.tasks = [step]

        success = await runner._execute_single_task(run, step)

        assert not success
        assert step.status == StepStatus.FAILED
        assert "Context overflow" in step.error
        assert provider.reject_tool.call_count >= MAX_RECOVERIES

    @pytest.mark.asyncio
    async def test_mid_stream_compaction_failure_falls_back_to_reset(self, tmp_path: Path) -> None:
        """Compaction raises → falls back to session reset, then retries."""
        from kiro_crew.providers.base import LLMEvent

        sessions = _make_mock_sessions()
        call_count = 0

        provider = MagicMock()

        async def _overflow_then_succeed(msg: str):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield LLMEvent(
                    kind="permission_request",
                    title="read",
                    text="",
                    request_id="req-1",
                    tool_kind="tool",
                )
            else:
                yield LLMEvent(kind="text_chunk", text="recovered")
                yield LLMEvent(kind="complete")

        provider.stream = _overflow_then_succeed
        provider.approve_tool = AsyncMock()
        provider.reject_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(side_effect=[92.0, 30.0, 30.0])
        provider.compact = AsyncMock(side_effect=RuntimeError("compact broke"))

        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Compact fail", description="desc")
        run.tasks = [step]

        success = await runner._execute_single_task(run, step)

        assert success
        sessions.reset.assert_called()  # fell back to reset
        assert "recovered" in (step.result or "")

    @pytest.mark.asyncio
    async def test_task_result_redacts_credentials(self, tmp_path: Path) -> None:
        """Final task.result must have credentials redacted."""
        from kiro_crew.providers.base import LLMEvent

        sessions = _make_mock_sessions()
        provider = MagicMock()

        async def _stream_with_cred(msg: str):
            yield LLMEvent(kind="text_chunk", text="key is AKIAIOSFODNN7EXAMPLE here")
            yield LLMEvent(kind="complete")

        provider.stream = _stream_with_cred
        provider.approve_tool = AsyncMock()
        provider.reject_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Cred test", description="desc")
        run.tasks = [step]

        success = await runner._execute_single_task(run, step)

        assert success
        assert "AKIAIOSFODNN7EXAMPLE" not in (step.result or "")
        assert "[REDACTED" in (step.result or "")


# ── 12.1c: Learn from Failures ──


class TestExtractLesson:
    @pytest.mark.asyncio
    async def test_extract_lesson_saves(self, tmp_path: Path) -> None:
        """Failed step → lesson extracted and saved."""
        from kiro_crew.learn import LessonStore

        store = LessonStore(base_dir=tmp_path)
        sessions = _make_mock_sessions()

        runner = TaskRunner(sessions=sessions, auto_test=False, lesson_store=store)

        step = Step(
            index=1,
            title="Add route",
            description="d",
            status=StepStatus.FAILED,
            error="flake8 N806: variable 'MyVar' should be lowercase",
        )

        bedrock_response = json.dumps(
            {
                "rule": "Use lowercase variable names in functions",
                "negative": "Do not use CamelCase for local variables",
                "category": "tool",
            }
        )

        with patch.object(
            runner, "_call_llm_for_lesson", return_value=json.loads(bedrock_response)
        ):
            await runner._extract_lesson(step)

        lessons = store.load_all()
        assert len(lessons) == 1
        assert "lowercase" in lessons[0].rule.lower()

    @pytest.mark.asyncio
    async def test_extract_lesson_no_store(self) -> None:
        """No lesson_store → does nothing."""
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, lesson_store=None)
        step = Step(index=1, title="X", description="d", error="boom")
        # Should not raise
        await runner._extract_lesson(step)

    @pytest.mark.asyncio
    async def test_extract_lesson_invalid_response(self, tmp_path: Path) -> None:
        """Invalid Bedrock response → no lesson saved."""
        from kiro_crew.learn import LessonStore

        store = LessonStore(base_dir=tmp_path)
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, lesson_store=store)

        step = Step(index=1, title="X", description="d", error="boom")

        with patch.object(runner, "_call_llm_for_lesson", return_value=None):
            await runner._extract_lesson(step)

        assert store.load_all() == []

    @pytest.mark.asyncio
    async def test_extract_lesson_missing_rule_key(self, tmp_path: Path) -> None:
        """LLM returns dict without 'rule' → no lesson saved."""
        from kiro_crew.learn import LessonStore

        store = LessonStore(base_dir=tmp_path)
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, lesson_store=store)

        step = Step(index=1, title="X", description="d", error="boom")

        with patch.object(runner, "_call_llm_for_lesson", return_value={"category": "tool"}):
            await runner._extract_lesson(step)

        assert store.load_all() == []


# ── 12.1d: History Integration ──


class TestHistoryIntegration:
    def test_log_task(self, tmp_path: Path) -> None:
        """Completed step → entries in ConversationLog."""
        from kiro_crew.history import ConversationLog

        conv_log = ConversationLog(base_dir=tmp_path)
        conv_log.init()

        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, conversation_log=conv_log)

        run = TaskRun(spec_path=str(tmp_path / "TASK.md"), spec_content="spec")
        step = Step(
            index=1,
            title="Create handler",
            description="d",
            status=StepStatus.PASSED,
            result="Created handler.py with 50 lines",
        )
        run.tasks = [step]

        runner._log_task("taskrunner:run:TASK", run, step)

        messages = conv_log.read_messages("taskrunner:run:TASK")
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert "Create handler" in messages[0]["content"]
        assert messages[1]["role"] == "assistant"
        assert "handler.py" in messages[1]["content"]

    def test_log_task_no_log(self) -> None:
        """No conversation_log → does nothing."""
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, conversation_log=None)
        run = TaskRun(spec_path="/t.md", spec_content="s")
        step = Step(index=1, title="X", description="d", result="ok")
        run.tasks = [step]
        # Should not raise
        runner._log_task("key", run, step)

    @pytest.mark.asyncio
    async def test_consolidation_triggered(self, tmp_path: Path) -> None:
        """Task completion → consolidator.maybe_consolidate() called."""
        sessions = _make_mock_sessions()

        step_json = json.dumps([{"title": "One step", "description": "d"}])

        from kiro_crew.providers.base import LLMEvent

        decompose_provider = MagicMock()

        async def _decompose_stream(msg: str):
            yield LLMEvent(kind="text_chunk", text=step_json)
            yield LLMEvent(kind="complete")

        decompose_provider.stream = _decompose_stream
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        step_provider = MagicMock()

        async def _step_stream(msg: str):
            yield LLMEvent(kind="text_chunk", text="Done!")
            yield LLMEvent(kind="complete")

        step_provider.stream = _step_stream
        step_provider.approve_tool = AsyncMock()
        step_provider.context_usage_pct = MagicMock(return_value=0.0)

        async def _get_or_create(key: str, agent=None, cwd=None, **kwargs):
            if "decompose" in key:
                return decompose_provider, True, False
            return step_provider, True, False

        sessions.get_or_create = _get_or_create

        consolidator = MagicMock()
        consolidator.maybe_consolidate = MagicMock()

        spec = tmp_path / "TASK.md"
        spec.write_text("# Test", encoding="utf-8")

        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            consolidator=consolidator,
            work_dir=tmp_path,
        )

        with patch.object(runner, "self_review", return_value=True):
            result = await runner.run(spec)

        assert result.status == "completed"
        consolidator.maybe_consolidate.assert_called_once()
        call_args = consolidator.maybe_consolidate.call_args[0]
        assert "taskrunner:run:TASK" in call_args[0]


# ── 12.1e: Task Watchdog ──


class TestWatchdog:
    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch):
        """TestWatchdog runs the real ``TaskRunner.run()`` which reaches
        ``git_coord.init_workspace`` → ``sandboxed_spawn_argv`` → ``wrap_argv``,
        raising on hosts without an OS-level sandbox backend (e.g. the dry-run
        build fleet). These tests exercise watchdog/timeout logic, not sandbox
        availability, so run commands unwrapped in-test (same pattern as
        TestGitCoord)."""
        import os as _os

        from kiro_crew import git_coord

        monkeypatch.setattr(
            git_coord,
            "sandboxed_spawn_argv",
            lambda argv, *a, **k: (list(argv), dict(_os.environ), None),
        )

    @pytest.mark.asyncio
    async def test_watchdog_stall_notification(self) -> None:
        """Watchdog sends notification when no progress in _STALL_TIMEOUT."""
        import time

        sessions = _make_mock_sessions()

        notifications: list[tuple[str, str]] = []

        async def _on_notify(title: str, body: str, task_id: str = "") -> None:
            notifications.append((title, body))

        runner = TaskRunner(sessions=sessions, auto_test=False, on_notify=_on_notify)
        run = TaskRun(spec_path="/t.md", spec_content="s", status="running")
        run.current_task = 3
        run.started_at = time.time()
        run.last_task_time = time.time() - _STALL_TIMEOUT - 60

        # Run watchdog for one tick then stop
        with patch("kiro_crew.taskrunner.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = [None, asyncio.CancelledError()]
            try:
                await runner._watchdog_loop(run)
            except asyncio.CancelledError:
                pass

        assert any("stalled" in t.lower() for t, _ in notifications)

    @pytest.mark.asyncio
    async def test_watchdog_exits_when_not_running(self) -> None:
        """Watchdog exits when run status changes from running."""
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        run = TaskRun(spec_path="/t.md", spec_content="s", status="completed")

        # Should exit immediately
        await runner._watchdog_loop(run)

    @pytest.mark.asyncio
    async def test_watchdog_no_stall_when_recent_progress(self) -> None:
        """Watchdog does NOT notify when last_task_time is recent."""
        import time

        sessions = _make_mock_sessions()
        notifications: list[tuple[str, str]] = []

        async def _on_notify(title: str, body: str, task_id: str = "") -> None:
            notifications.append((title, body))

        runner = TaskRunner(sessions=sessions, auto_test=False, on_notify=_on_notify)
        run = TaskRun(spec_path="/t.md", spec_content="s", status="running")
        run.started_at = time.time()
        run.last_task_time = time.time()  # just now

        with patch("kiro_crew.taskrunner.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = [None, asyncio.CancelledError()]
            try:
                await runner._watchdog_loop(run)
            except asyncio.CancelledError:
                pass

        assert not any("stalled" in t.lower() for t, _ in notifications)

    @pytest.mark.asyncio
    async def test_watchdog_resets_correct_session_key(self) -> None:
        """Watchdog must reset the session key matching the current task index."""
        import time

        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, on_notify=AsyncMock())
        run = TaskRun(spec_path="/t.md", spec_content="s", status="running")
        run.task_id = "plan_123"
        run.current_task = 3
        run.started_at = time.time()
        run.last_task_time = time.time() - _STALL_CANCEL_TIMEOUT - 60

        with patch("kiro_crew.taskrunner.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = [None, asyncio.CancelledError()]
            try:
                await runner._watchdog_loop(run)
            except asyncio.CancelledError:
                pass

        sessions.reset.assert_called_with("taskrunner:plan_123:task3")

    @pytest.mark.asyncio
    async def test_watchdog_uses_run_fields_not_runner(self) -> None:
        """Watchdog reads last_task_time and current_task from run, not runner."""
        import time

        sessions = _make_mock_sessions()
        notifications: list[tuple[str, str]] = []

        async def _on_notify(title: str, body: str, task_id: str = "") -> None:
            notifications.append((title, body))

        runner = TaskRunner(sessions=sessions, auto_test=False, on_notify=_on_notify)
        run = TaskRun(spec_path="/t.md", spec_content="s", status="running")
        run.started_at = time.time()
        run.last_task_time = time.time()  # recent — should NOT stall

        # Set decoy on runner (the old bug) — watchdog must ignore this
        runner._last_task_time = 0.0  # type: ignore[attr-defined]

        with patch("kiro_crew.taskrunner.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = [None, asyncio.CancelledError()]
            try:
                await runner._watchdog_loop(run)
            except asyncio.CancelledError:
                pass

        # If watchdog read runner._last_task_time (0.0), it would stall-notify.
        # It should read run.last_task_time (recent) and NOT notify.
        assert not any("stalled" in t.lower() for t, _ in notifications)


# ── Phase 12.2: Working Memory ──


class TestWorkingMemory:
    def test_empty_summary(self) -> None:
        mem = WorkingMemory()
        assert mem.summary() == ""

    def test_summary_with_files(self) -> None:
        mem = WorkingMemory(files_changed=["Created handler.py", "Modified routes.py"])
        text = mem.summary()
        assert "handler.py" in text
        assert "routes.py" in text
        assert "Files Changed" in text

    def test_summary_with_decisions(self) -> None:
        mem = WorkingMemory(decisions=["Use asyncio over threading"])
        text = mem.summary()
        assert "asyncio" in text
        assert "Key Decisions" in text

    def test_summary_with_blockers(self) -> None:
        mem = WorkingMemory(blockers=["Missing API key"])
        text = mem.summary()
        assert "Missing API key" in text
        assert "Blockers" in text

    def test_update_from_result(self) -> None:
        mem = WorkingMemory()
        mem.update_from_result("Created handler.py\nModified routes.py\nSome other text")
        assert len(mem.files_changed) == 2
        assert "Created handler.py" in mem.files_changed[0]

    def test_update_from_result_no_matches(self) -> None:
        mem = WorkingMemory()
        mem.update_from_result("Just some text with no file operations")
        assert mem.files_changed == []

    @pytest.mark.asyncio
    async def test_memory_in_step_prompt(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.memory.files_changed = ["Created foo.py"]
        run.memory.decisions = ["Use REST API"]
        step = Step(index=1, title="Next", description="d")
        run.tasks = [step]
        prompt = await runner._build_task_prompt(run, step, attempt=1)
        assert "Working Memory" in prompt
        assert "foo.py" in prompt
        assert "REST API" in prompt


# ── Phase 12.2: Plan Revision ──


class TestPlanRevision:
    @pytest.mark.asyncio
    async def test_replan_on_step_failure(self, tmp_path: Path) -> None:
        """Step fails → replan decomposes new steps and executes them."""
        spec = tmp_path / "TASK.md"
        spec.write_text("# Feature", encoding="utf-8")

        sessions = _make_mock_sessions()

        from kiro_crew.providers.base import LLMEvent

        original_steps = json.dumps([{"title": "Broken step", "description": "Will fail"}])
        replan_steps = json.dumps([{"title": "Fixed step", "description": "Will work"}])

        decompose_call = 0

        decompose_provider = MagicMock()

        async def _decompose_stream(msg: str):
            nonlocal decompose_call
            decompose_call += 1
            text = original_steps if decompose_call == 1 else replan_steps
            yield LLMEvent(kind="text_chunk", text=text)
            yield LLMEvent(kind="complete")

        decompose_provider.stream = _decompose_stream
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        step_call = 0
        step_provider = MagicMock()

        async def _step_stream(msg: str):
            nonlocal step_call
            step_call += 1
            if step_call <= 3:  # first 3 calls = original step retries (all fail)
                raise RuntimeError("logic error")
            yield LLMEvent(kind="text_chunk", text="Fixed!")
            yield LLMEvent(kind="complete")

        step_provider.stream = _step_stream
        step_provider.approve_tool = AsyncMock()
        step_provider.context_usage_pct = MagicMock(return_value=0.0)

        async def _get_or_create(key: str, agent=None, cwd=None, **kwargs):
            if "decompose" in key:
                return decompose_provider, True, False
            return step_provider, True, False

        sessions.get_or_create = _get_or_create

        notifications: list[tuple[str, str]] = []

        async def _on_notify(title: str, body: str, task_id: str = "") -> None:
            notifications.append((title, body))

        runner = TaskRunner(
            sessions=sessions, auto_test=False, on_notify=_on_notify, work_dir=tmp_path
        )

        with patch.object(runner, "self_review", return_value=True):
            result = await runner.run(spec)

        assert result.status == "completed"
        assert result.replan_count == 1
        assert any("Re-planning" in t for t, _ in notifications)

    @pytest.mark.asyncio
    async def test_replan_exhausted(self, tmp_path: Path) -> None:
        """Replan limit exceeded → task fails."""
        spec = tmp_path / "TASK.md"
        spec.write_text("# Feature", encoding="utf-8")

        sessions = _make_mock_sessions()

        from kiro_crew.providers.base import LLMEvent

        step_json = json.dumps([{"title": "Always fails", "description": "d"}])

        decompose_provider = MagicMock()

        async def _decompose_stream(msg: str):
            yield LLMEvent(kind="text_chunk", text=step_json)
            yield LLMEvent(kind="complete")

        decompose_provider.stream = _decompose_stream
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        fail_provider = MagicMock()

        async def _fail_stream(msg: str):
            raise RuntimeError("always fails")
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
        result = await runner.run(spec)

        assert result.status == "failed"
        assert result.replan_count == 2  # both replans attempted, all steps fail

    @pytest.mark.asyncio
    async def test_replan_reindexes_depends_on(self, tmp_path: Path) -> None:
        """Replanned steps have depends_on shifted by base index."""
        spec = tmp_path / "TASK.md"
        spec.write_text("# Feature", encoding="utf-8")

        sessions = _make_mock_sessions()

        from kiro_crew.providers.base import LLMEvent

        original_steps = json.dumps([{"title": "Broken", "description": "d"}])
        replan_steps = json.dumps(
            [
                {"title": "Fix A", "description": "d"},
                {"title": "Fix B", "description": "d", "depends_on": [1]},
            ]
        )

        decompose_call = 0
        decompose_provider = MagicMock()

        async def _decompose_stream(msg: str):
            nonlocal decompose_call
            decompose_call += 1
            text = original_steps if decompose_call == 1 else replan_steps
            yield LLMEvent(kind="text_chunk", text=text)
            yield LLMEvent(kind="complete")

        decompose_provider.stream = _decompose_stream
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        step_call = 0
        step_provider = MagicMock()

        async def _step_stream(msg: str):
            nonlocal step_call
            step_call += 1
            if step_call <= 3:  # original step retries
                raise RuntimeError("fail")
            yield LLMEvent(kind="text_chunk", text="ok")
            yield LLMEvent(kind="complete")

        step_provider.stream = _step_stream
        step_provider.approve_tool = AsyncMock()
        step_provider.context_usage_pct = MagicMock(return_value=0.0)

        async def _get_or_create(key: str, agent=None, cwd=None, **kwargs):
            if "decompose" in key:
                return decompose_provider, True, False
            return step_provider, True, False

        sessions.get_or_create = _get_or_create

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        with patch.object(runner, "self_review", return_value=True):
            result = await runner.run(spec)

        assert result.status == "completed"
        # Replanned steps should have re-indexed depends_on
        # Original: 1 step. Replan adds 2 more at index 2, 3.
        # Fix B's depends_on [1] should become [1 + 1] = [2]
        fix_b = result.tasks[2]
        assert fix_b.title == "Fix B"
        assert fix_b.depends_on == [2]

    @pytest.mark.asyncio
    async def test_replan_recurses_on_second_failure(self, tmp_path: Path) -> None:
        """First replan's steps fail → second replan fires and succeeds."""
        spec = tmp_path / "TASK.md"
        spec.write_text("# Feature", encoding="utf-8")

        sessions = _make_mock_sessions()

        from kiro_crew.providers.base import LLMEvent

        original = json.dumps([{"title": "Task ", "description": "d"}])
        replan1 = json.dumps([{"title": "Replan1 step", "description": "d"}])
        replan2 = json.dumps([{"title": "Replan2 step", "description": "d"}])

        decompose_call = 0
        decompose_provider = MagicMock()

        async def _decompose_stream(msg: str):
            nonlocal decompose_call
            decompose_call += 1
            text = [original, replan1, replan2][min(decompose_call - 1, 2)]
            yield LLMEvent(kind="text_chunk", text=text)
            yield LLMEvent(kind="complete")

        decompose_provider.stream = _decompose_stream
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        step_call = 0
        step_provider = MagicMock()

        async def _step_stream(msg: str):
            nonlocal step_call
            step_call += 1
            # Calls 1-3: original step retries (fail)
            # Calls 4-6: replan1 step retries (fail)
            # Call 7: replan2 step (succeed)
            if step_call <= 6:
                raise RuntimeError("fail")
            yield LLMEvent(kind="text_chunk", text="success!")
            yield LLMEvent(kind="complete")

        step_provider.stream = _step_stream
        step_provider.approve_tool = AsyncMock()
        step_provider.context_usage_pct = MagicMock(return_value=0.0)

        async def _get_or_create(key: str, agent=None, cwd=None, **kwargs):
            if "decompose" in key:
                return decompose_provider, True, False
            return step_provider, True, False

        sessions.get_or_create = _get_or_create

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        with patch.object(runner, "self_review", return_value=True):
            result = await runner.run(spec)

        assert result.status == "completed"
        assert result.replan_count == 2  # both replans used


# ── Phase 12.2: Self-Review ──


class TestSelfReview:
    @pytest.mark.asyncio
    async def testself_review_passes(self, tmp_path: Path) -> None:
        """Self-review returns ok:true → step passes."""
        sessions = _make_mock_sessions()
        provider = MagicMock()

        from kiro_crew.providers.base import LLMEvent

        async def _stream(msg: str):
            yield LLMEvent(kind="text_chunk", text='{"ok": true}')
            yield LLMEvent(kind="complete")

        provider.stream = _stream
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s")
        step = Step(index=1, title="Test", description="d", status=StepStatus.PASSED)
        run.tasks = [step]

        with patch("kiro_crew.task_executor.stream_and_collect_json", return_value={"ok": True}):
            result = await runner.self_review(run, step)

        assert result is True

    @pytest.mark.asyncio
    async def testself_review_fails(self, tmp_path: Path) -> None:
        """Self-review returns ok:false → step marked for retry."""
        sessions = _make_mock_sessions()
        provider = MagicMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s")
        step = Step(index=1, title="Test", description="d", status=StepStatus.PASSED)
        run.tasks = [step]

        with patch(
            "kiro_crew.task_executor.stream_and_collect_json",
            return_value={"ok": False, "issue": "wrong file modified"},
        ):
            result = await runner.self_review(run, step)

        assert result is False
        assert "wrong file" in step.error
        assert len(run.memory.blockers) == 1

    @pytest.mark.asyncio
    async def testself_review_exception_passes(self, tmp_path: Path) -> None:
        """Self-review exception → doesn't block step."""
        sessions = _make_mock_sessions()
        sessions.get_or_create = AsyncMock(side_effect=RuntimeError("boom"))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s")
        step = Step(index=1, title="Test", description="d")
        run.tasks = [step]

        result = await runner.self_review(run, step)
        assert result is True  # graceful fallback


# ── Phase 12.2: Approval Gates ──


class TestApprovalGates:
    def test_parse_requires_approval(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        text = json.dumps(
            [
                {"title": "Safe step", "description": "d"},
                {"title": "Dangerous step", "description": "d", "requires_approval": True},
            ]
        )
        steps = runner._parse_tasks(text)
        assert not steps[0].requires_approval
        assert steps[1].requires_approval

    @pytest.mark.asyncio
    async def test_approval_denied_skips_step(self, tmp_path: Path) -> None:
        """Approval denied → step paused for editing (not failed)."""
        sessions = _make_mock_sessions()
        provider = _make_mock_provider("done")
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        async def _deny(_step: Step) -> bool:
            return False

        runner = TaskRunner(
            sessions=sessions, auto_test=False, on_approval=_deny, work_dir=tmp_path
        )
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Delete DB", description="d", requires_approval=True)
        run.tasks = [step]

        success = await runner._execute_single_task(run, step, "key")
        assert success is False
        assert step.status == StepStatus.PENDING
        assert run.status == "paused"

    @pytest.mark.asyncio
    async def test_approval_granted_executes(self, tmp_path: Path) -> None:
        """Approval granted → step executes normally."""
        sessions = _make_mock_sessions()
        provider = _make_mock_provider("done")
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        async def _approve(_step: Step) -> bool:
            return True

        runner = TaskRunner(
            sessions=sessions, auto_test=False, on_approval=_approve, work_dir=tmp_path
        )
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Delete DB", description="d", requires_approval=True)
        run.tasks = [step]

        with patch.object(runner, "self_review", return_value=True):
            success = await runner._execute_single_task(run, step, "key")

        assert success is True
        assert step.status == StepStatus.PASSED


# ── Phase 12.2: Active Stall Recovery ──


class TestActiveStallRecovery:
    @pytest.mark.asyncio
    async def test_watchdog_cancels_stalled_step(self) -> None:
        """Watchdog resets session after _STALL_CANCEL_TIMEOUT."""
        import time

        sessions = _make_mock_sessions()

        notifications: list[tuple[str, str]] = []

        async def _on_notify(title: str, body: str, task_id: str = "") -> None:
            notifications.append((title, body))

        runner = TaskRunner(sessions=sessions, auto_test=False, on_notify=_on_notify)
        run = TaskRun(spec_path="/t.md", spec_content="s", status="running")
        run.current_task = 1
        run.started_at = time.time()
        run.last_task_time = time.time() - _STALL_CANCEL_TIMEOUT - 60

        with patch("kiro_crew.taskrunner.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = [None, asyncio.CancelledError()]
            try:
                await runner._watchdog_loop(run)
            except asyncio.CancelledError:
                pass

        assert run.task_id in runner._stall_cancelled_ids
        sessions.reset.assert_called()
        assert any("cancelling" in t.lower() for t, _ in notifications)

    @pytest.mark.asyncio
    async def test_watchdog_heartbeat_detects_dead_process(self) -> None:
        """Watchdog resets session when ACP process is dead (heartbeat check)."""
        import time

        sessions = _make_mock_sessions()
        # is_provider_alive returns False → process is dead
        sessions.is_provider_alive = AsyncMock(return_value=False)

        notifications: list[tuple[str, str]] = []

        async def _on_notify(title: str, body: str, task_id: str = "") -> None:
            notifications.append((title, body))

        runner = TaskRunner(sessions=sessions, auto_test=False, on_notify=_on_notify)
        run = TaskRun(spec_path="/t.md", spec_content="s", status="running")
        run.task_id = "plan_42"
        run.current_task = 2
        run.started_at = time.time()
        run.last_task_time = time.time()  # recent — no stall

        # Run watchdog for 3 ticks (need 2 consecutive dead checks to trigger reset)
        tick = 0

        async def _tick_sleep(_interval: float) -> None:
            nonlocal tick
            tick += 1
            if tick >= 3:
                raise asyncio.CancelledError()

        with patch("kiro_crew.taskrunner.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = _tick_sleep
            try:
                await runner._watchdog_loop(run)
            except asyncio.CancelledError:
                pass

        sessions.reset.assert_called_with("taskrunner:plan_42:task2")
        assert any("process died" in t.lower() for t, _ in notifications)

    @pytest.mark.asyncio
    async def test_watchdog_heartbeat_resets_count_on_task_change(self) -> None:
        """dead_process_count resets when current_task changes between ticks."""
        import time

        sessions = _make_mock_sessions()
        # Always report dead
        sessions.is_provider_alive = AsyncMock(return_value=False)

        runner = TaskRunner(sessions=sessions, auto_test=False, on_notify=AsyncMock())
        run = TaskRun(spec_path="/t.md", spec_content="s", status="running")
        run.task_id = "plan_99"
        run.current_task = 1
        run.started_at = time.time()
        run.last_task_time = time.time()

        tick = 0

        async def _tick_sleep(_interval: float) -> None:
            nonlocal tick
            tick += 1
            if tick == 2:
                run.current_task = 2  # change after task1's check completes
            if tick >= 3:
                raise asyncio.CancelledError()  # stop after task2's first check

        with patch("kiro_crew.taskrunner.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = _tick_sleep
            try:
                await runner._watchdog_loop(run)
            except asyncio.CancelledError:
                pass

        # Should NOT have triggered reset — each task only got 1 dead check (< threshold of 2)
        sessions.reset.assert_not_called()


# ── Phase 12.2: Token Budget ──


class TestTokenBudget:
    @pytest.mark.asyncio
    async def test_token_budget_exceeded(self, tmp_path: Path) -> None:
        """Token budget exceeded → task fails."""
        spec = tmp_path / "TASK.md"
        spec.write_text("# Feature", encoding="utf-8")

        sessions = _make_mock_sessions()

        from kiro_crew.providers.base import LLMEvent

        step_json = json.dumps(
            [
                {"title": "Task ", "description": "d"},
                {"title": "Task ", "description": "d", "depends_on": [1]},
            ]
        )

        decompose_provider = MagicMock()

        async def _decompose_stream(msg: str):
            yield LLMEvent(kind="text_chunk", text=step_json)
            yield LLMEvent(kind="complete")

        decompose_provider.stream = _decompose_stream
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        # Step provider that generates lots of text (tokens)
        step_provider = MagicMock()

        async def _step_stream(msg: str):
            yield LLMEvent(kind="text_chunk", text="x" * 4000)  # ~1000 tokens
            yield LLMEvent(kind="complete")

        step_provider.stream = _step_stream
        step_provider.approve_tool = AsyncMock()
        step_provider.context_usage_pct = MagicMock(return_value=0.0)

        async def _get_or_create(key: str, agent=None, cwd=None, **kwargs):
            if "decompose" in key:
                return decompose_provider, True, False
            return step_provider, True, False

        sessions.get_or_create = _get_or_create

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path, token_budget=500)

        with patch.object(runner, "self_review", return_value=True):
            result = await runner.run(spec)

        assert result.status == "failed"
        assert "Token budget" in result.error

    def test_status_includes_tokens(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        run = TaskRun(spec_path="/t.md", spec_content="s", status="running", tokens_used=500)
        run.task_id = "test_1"
        runner._runs["test_1"] = run
        s = runner.status()
        assert s["runs"][0]["tokens_used"] == 500


# ── Phase 12.2: Parallel Step Grouping ──


class TestParallelStepGrouping:
    def test_no_deps_all_parallel(self) -> None:
        """Steps with no dependencies → single parallel group."""
        steps = [
            Step(index=1, title="A", description="d"),
            Step(index=2, title="B", description="d"),
            Step(index=3, title="C", description="d"),
        ]
        groups = TaskRunner._group_parallel_tasks(steps)
        assert len(groups) == 1
        assert len(groups[0]) == 3

    def test_sequential_deps(self) -> None:
        """Each step depends on previous → all sequential."""
        steps = [
            Step(index=1, title="A", description="d"),
            Step(index=2, title="B", description="d", depends_on=[1]),
            Step(index=3, title="C", description="d", depends_on=[2]),
        ]
        groups = TaskRunner._group_parallel_tasks(steps)
        assert len(groups) == 3
        assert len(groups[0]) == 1
        assert groups[0][0].title == "A"
        assert groups[1][0].title == "B"
        assert groups[2][0].title == "C"

    def test_diamond_deps(self) -> None:
        """Diamond: A → B,C → D."""
        steps = [
            Step(index=1, title="A", description="d"),
            Step(index=2, title="B", description="d", depends_on=[1]),
            Step(index=3, title="C", description="d", depends_on=[1]),
            Step(index=4, title="D", description="d", depends_on=[2, 3]),
        ]
        groups = TaskRunner._group_parallel_tasks(steps)
        assert len(groups) == 3
        assert len(groups[0]) == 1  # A
        assert len(groups[1]) == 2  # B, C in parallel
        assert len(groups[2]) == 1  # D

    def test_empty_steps(self) -> None:
        groups = TaskRunner._group_parallel_tasks([])
        assert groups == []

    def test_parse_depends_on(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        text = json.dumps(
            [
                {"title": "A", "description": "d"},
                {"title": "B", "description": "d", "depends_on": [1]},
            ]
        )
        steps = runner._parse_tasks(text)
        assert steps[0].depends_on == []
        assert steps[1].depends_on == [1]

    def test_parse_invalid_depends_on(self) -> None:
        """Non-list depends_on → treated as empty."""
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        text = json.dumps([{"title": "A", "description": "d", "depends_on": "invalid"}])
        steps = runner._parse_tasks(text)
        assert steps[0].depends_on == []


# ── Edge Cases ──


class TestEdgeCases:
    def test_working_memory_truncation(self) -> None:
        """Working memory caps files at 20, decisions at 10, blockers at 5."""
        mem = WorkingMemory(
            files_changed=[f"Created file{i}.py" for i in range(25)],
            decisions=[f"Decision {i}" for i in range(15)],
            blockers=[f"Blocker {i}" for i in range(8)],
        )
        text = mem.summary()
        # Shows last 20 files (indices 5-24), last 10 decisions (5-14), last 5 blockers (3-7)
        assert "file4.py" not in text  # index 4 truncated
        assert "file5.py" in text  # index 5 kept
        assert "file24.py" in text
        assert "Decision 4" not in text
        assert "Decision 5" in text
        assert "Decision 14" in text
        assert "Blocker 2" not in text
        assert "Blocker 3" in text
        assert "Blocker 7" in text

    @pytest.mark.asyncio
    async def test_approval_no_handler_auto_approves(self, tmp_path: Path) -> None:
        """Step requires approval but no handler → auto-approve with warning."""
        sessions = _make_mock_sessions()
        provider = _make_mock_provider("done")
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, on_approval=None, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Dangerous", description="d", requires_approval=True)
        run.tasks = [step]

        with patch.object(runner, "self_review", return_value=True):
            success = await runner._execute_single_task(run, step, "key")

        assert success is True
        assert step.status == StepStatus.PASSED  # auto-approved, not skipped

    @pytest.mark.asyncio
    async def testself_review_fail_then_retry_succeeds(self, tmp_path: Path) -> None:
        """Self-review fails → step retried → succeeds without second review."""
        sessions = _make_mock_sessions()
        provider = _make_mock_provider("done")
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Test", description="d")
        run.tasks = [step]

        review_calls = 0

        async def _review_once(r, s, sessions, agent, session_key=""):
            nonlocal review_calls
            review_calls += 1
            if review_calls == 1:
                s.error = "Self-review: wrong file"
                return False
            return True

        with patch("kiro_crew.task_executor.self_review", side_effect=_review_once):
            success = await runner._execute_single_task(run, step, "key")

        assert success is True
        # Self-review called once (not called again after retry)
        assert review_calls == 1

    def test_group_parallel_tasks_deadlock(self) -> None:
        """Steps with circular deps → fallback to sequential."""
        steps = [
            Step(index=1, title="A", description="d", depends_on=[2]),
            Step(index=2, title="B", description="d", depends_on=[1]),
        ]
        groups = TaskRunner._group_parallel_tasks(steps)
        # Both are blocked → deadlock fallback: each runs sequentially
        assert len(groups) == 2
        assert len(groups[0]) == 1
        assert len(groups[1]) == 1

    def test_group_parallel_tasks_missing_dep(self) -> None:
        """Step depends on non-existent index → treated as satisfied."""
        steps = [
            Step(index=1, title="A", description="d", depends_on=[99]),
        ]
        groups = TaskRunner._group_parallel_tasks(steps)
        # dep 99 not in completed_indices → step is blocked
        # deadlock fallback: runs sequentially
        assert len(groups) == 1

    @pytest.mark.asyncio
    async def test_skipped_step_not_counted_as_failure(self, tmp_path: Path) -> None:
        """Skipped step doesn't trigger replan or fail the task."""
        spec = tmp_path / "TASK.md"
        spec.write_text("# Feature", encoding="utf-8")

        sessions = _make_mock_sessions()

        from kiro_crew.providers.base import LLMEvent

        step_json = json.dumps(
            [
                {"title": "Dangerous", "description": "d", "requires_approval": True},
                {"title": "Safe", "description": "d", "depends_on": [1]},
            ]
        )

        decompose_provider = MagicMock()

        async def _decompose_stream(msg: str):
            yield LLMEvent(kind="text_chunk", text=step_json)
            yield LLMEvent(kind="complete")

        decompose_provider.stream = _decompose_stream
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        step_provider = MagicMock()

        async def _step_stream(msg: str):
            yield LLMEvent(kind="text_chunk", text="done")
            yield LLMEvent(kind="complete")

        step_provider.stream = _step_stream
        step_provider.approve_tool = AsyncMock()
        step_provider.context_usage_pct = MagicMock(return_value=0.0)

        async def _get_or_create(key: str, agent=None, cwd=None, **kwargs):
            if "decompose" in key:
                return decompose_provider, True, False
            return step_provider, True, False

        sessions.get_or_create = _get_or_create

        async def _deny(_step: Step) -> bool:
            return False

        runner = TaskRunner(
            sessions=sessions, auto_test=False, on_approval=_deny, work_dir=tmp_path
        )

        with patch.object(runner, "self_review", return_value=True):
            result = await runner.run(spec)

        # Step 1 denied → run pauses (denial no longer skips)
        assert result.tasks[0].status == StepStatus.PENDING
        assert result.status == "paused"


# ── Phase 12.2: Progress File Updates ──


class TestProgressFileUpdates:
    def test_progress_includes_tokens_and_replans(self, tmp_path: Path) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        run = TaskRun(
            spec_path=str(tmp_path / "TASK.md"),
            spec_content="spec",
            started_at=1000.0,
            status="running",
            tokens_used=1234,
            replan_count=1,
            tasks=[Step(index=1, title="A", description="a", status=StepStatus.PASSED)],
        )
        runner._save_progress(run)
        content = (tmp_path / PROGRESS_FILE).read_text(encoding="utf-8")
        assert "1234" in content
        assert "Replans" in content


# ── Phase 12.2: Status Includes New Fields ──


class TestStatusNewFields:
    def test_status_includes_skipped_and_replan(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        run = TaskRun(
            spec_path="/t.md",
            spec_content="s",
            status="running",
            replan_count=1,
            tokens_used=999,
            tasks=[
                Step(index=1, title="A", description="a", status=StepStatus.SKIPPED),
                Step(index=2, title="B", description="b", status=StepStatus.PASSED),
            ],
        )
        run.task_id = "test_1"
        runner._runs["test_1"] = run
        s = runner.status()
        r = s["runs"][0]
        assert r["skipped"] == 1
        assert r["replan_count"] == 1
        assert r["tokens_used"] == 999


# ── Phase 1c: MAX_TOTAL_STEPS ──


class TestMaxTotalSteps:
    @pytest.mark.asyncio
    async def test_replan_blocked_by_step_limit(self, tmp_path: Path) -> None:
        """_try_replan refuses when step count >= MAX_TOTAL_TASKS."""
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        # Fill steps to the limit
        run.tasks = [
            Step(index=i, title=f"Step {i}", description="d", status=StepStatus.PASSED)
            for i in range(1, MAX_TOTAL_TASKS + 1)
        ]
        failed = Step(index=MAX_TOTAL_TASKS, title="Last", description="d", error="boom")
        result = await runner._try_replan(run, failed)
        assert result is False
        assert run.status == "failed"
        assert "Task limit" in run.error


# ── Phase 1d: Cycle Detection ──


class TestCycleDetection:
    @pytest.mark.asyncio
    async def test_cycle_detection_fails_at_5(self, tmp_path: Path) -> None:
        """Same error 5 times → step fails with loop detection."""
        sessions = _make_mock_sessions()

        provider = MagicMock()

        async def _always_fail(msg: str):
            raise RuntimeError("same error every time")
            yield  # type: ignore[misc]  # pragma: no cover

        provider.stream = _always_fail
        provider.approve_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Loopy", description="d")
        run.tasks = [step]

        success = await runner._execute_single_task(run, step)
        assert not success
        assert step.status == StepStatus.FAILED


# ── Parallel Groups ──


class TestParallelGroups:
    @pytest.mark.asyncio
    async def test_parallel_group_runs_all_steps(self, tmp_path: Path) -> None:
        """Steps in a parallel group all execute (via asyncio.gather)."""
        spec = tmp_path / "TASK.md"
        spec.write_text("# Feature", encoding="utf-8")

        sessions = _make_mock_sessions()

        from kiro_crew.providers.base import LLMEvent

        # 3 independent steps (no deps → single parallel group)
        step_json = json.dumps(
            [
                {"title": "A", "description": "d"},
                {"title": "B", "description": "d"},
                {"title": "C", "description": "d"},
            ]
        )

        decompose_provider = MagicMock()

        async def _decompose_stream(msg: str):
            yield LLMEvent(kind="text_chunk", text=step_json)
            yield LLMEvent(kind="complete")

        decompose_provider.stream = _decompose_stream
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        execution_order: list[str] = []
        step_provider = MagicMock()

        async def _step_stream(msg: str):
            # Track execution order via the step title in the prompt
            for title in ("A", "B", "C"):
                if f"**{title}**" in msg:
                    execution_order.append(title)
                    break
            yield LLMEvent(kind="text_chunk", text="done")
            yield LLMEvent(kind="complete")

        step_provider.stream = _step_stream
        step_provider.approve_tool = AsyncMock()
        step_provider.context_usage_pct = MagicMock(return_value=0.0)

        async def _get_or_create(key: str, agent=None, cwd=None, **kwargs):
            if "decompose" in key:
                return decompose_provider, True, False
            return step_provider, True, False

        sessions.get_or_create = _get_or_create

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        with patch.object(runner, "self_review", return_value=True):
            result = await runner.run(spec)

        assert result.status == "completed"
        # All 3 should have run (order may vary but all present)
        assert len(execution_order) == 3


# ── Phase 2: TaskRun git fields ──


class TestTaskRunGitFields:
    def test_default_git_fields(self) -> None:
        run = TaskRun(spec_path="/t.md", spec_content="s")
        assert run.branch_name == ""
        assert run.base_branch == ""
        assert run.commit_hashes == []
        assert run.worktree_path == ""


# ── Phase 2: git_coord module ──


@requires_git
class TestGitCoord:
    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch):
        """TestGitCoord spawns REAL git via ``git_coord._git`` →
        ``sandboxed_spawn_argv`` → ``wrap_argv``, which raises when no OS-level
        sandbox backend is available. These tests exercise git coordination, not
        sandbox availability, so run the command unwrapped in-test."""
        import os as _os

        from kiro_crew import git_coord

        monkeypatch.setattr(
            git_coord,
            "sandboxed_spawn_argv",
            lambda argv, *a, **k: (list(argv), dict(_os.environ), None),
        )

    @pytest.mark.asyncio
    async def test_init_workspace_no_repo(self, tmp_path: Path) -> None:
        """init_workspace in a non-git dir → run in place, no git init."""
        from kiro_crew import git_coord

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        (work_dir / "hello.txt").write_text("hi")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "test_123"
        run.work_dir = str(work_dir)

        await git_coord.init_workspace(run)

        # The task runner must NOT impose git on a non-repo folder.
        assert run.git_enabled is False
        assert run.branch_name == ""
        assert run.worktree_path == ""
        assert run.work_dir == str(work_dir)  # runs in place
        assert not (work_dir / ".git").exists()  # no git init

    @pytest.mark.asyncio
    async def test_init_workspace_missing_git_binary(self, tmp_path: Path, monkeypatch) -> None:
        """No ``git`` binary on the host → treated as non-git (run in place), not a crash.

        Locks the git-optional guarantee independently of whether the test host
        actually has git installed, by simulating the missing binary.
        """
        from kiro_crew import git_coord

        async def _no_git(*_a, **_k):
            raise FileNotFoundError(2, "No such file or directory", "git")

        monkeypatch.setattr(git_coord.asyncio, "create_subprocess_exec", _no_git)

        # The probe swallows the missing binary and reports "not a repo".
        assert await git_coord._is_git_repo(str(tmp_path)) is False

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "nogit_123"
        run.work_dir = str(work_dir)

        await git_coord.init_workspace(run)

        assert run.git_enabled is False
        assert run.work_dir == str(work_dir)  # unchanged — no worktree
        assert run.worktree_path == ""

    @pytest.mark.asyncio
    async def test_init_workspace_existing_repo(self, tmp_path: Path) -> None:
        """init_workspace in existing git repo → worktree created."""
        from kiro_crew import git_coord

        # Set up a real git repo
        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "file.txt").write_text("content")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "wt_test"
        run.work_dir = str(work_dir)

        await git_coord.init_workspace(run)

        assert run.branch_name == "kirocrew/task/wt_test"
        assert run.worktree_path != ""
        assert Path(run.worktree_path).exists()
        # work_dir should have been updated to the worktree
        assert run.work_dir == run.worktree_path

        # Cleanup
        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_commit_and_revert(self, tmp_path: Path) -> None:
        """commit_step creates commit, revert_step undoes it."""
        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "seed.txt").write_text("seed")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "cr_test"
        run.work_dir = str(work_dir)

        await git_coord.init_workspace(run)  # git repo → worktree, git_enabled=True

        # Create a file and commit
        (Path(run.work_dir) / "new.py").write_text("print('hello')")
        step = Step(index=1, title="Add new.py", description="d")
        sha = await git_coord.commit_step(run, step)
        assert sha != ""
        assert len(run.commit_hashes) == 1

        # Revert
        await git_coord.revert_step(run)
        assert len(run.commit_hashes) == 0
        assert not (Path(run.work_dir) / "new.py").exists()

        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_commit_no_changes(self, tmp_path: Path) -> None:
        """commit_step with no changes → empty string."""
        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "seed.txt").write_text("seed")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "nc_test"
        run.work_dir = str(work_dir)

        await git_coord.init_workspace(run)

        step = Step(index=1, title="No-op", description="d")
        sha = await git_coord.commit_step(run, step)
        assert sha == ""

        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_non_git_workspace_git_ops_are_noops(self, tmp_path: Path) -> None:
        """A non-git workspace runs in place; all git helpers are safe no-ops."""
        from kiro_crew import git_coord

        work_dir = tmp_path / "plain"
        work_dir.mkdir()
        (work_dir / "a.py").write_text("x = 1")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "plain_test"
        run.work_dir = str(work_dir)

        await git_coord.init_workspace(run)
        assert run.git_enabled is False

        step = Step(index=1, title="Edit", description="d")
        assert await git_coord.commit_step(run, step) == ""  # no commit
        await git_coord.revert_step(run)  # no-op, no raise
        assert await git_coord.get_state_summary(run) == ""
        assert await git_coord.get_step_diff(run) == ""
        assert await git_coord.finalize(run) == ""  # nothing to clean
        # The file the "step" would have created is untouched (no git reset).
        assert (work_dir / "a.py").exists()

        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_get_state_summary(self, tmp_path: Path) -> None:
        """get_state_summary returns git log + diff stat."""
        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "seed.txt").write_text("seed")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "ss_test"
        run.work_dir = str(work_dir)

        await git_coord.init_workspace(run)

        (Path(run.work_dir) / "foo.py").write_text("x = 1")
        step = Step(index=1, title="Add foo", description="d")
        await git_coord.commit_step(run, step)

        summary = await git_coord.get_state_summary(run)
        assert "Git Log" in summary
        assert "foo.py" in summary

        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_revert_no_commits(self, tmp_path: Path) -> None:
        """revert_step with no commits → no-op."""
        from kiro_crew import git_coord

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.work_dir = str(tmp_path)
        run.commit_hashes = []

        await git_coord.revert_step(run)  # should not raise


class TestTaskNaming:
    """Tests for the task naming feature."""

    def test_auto_name_from_task_header(self) -> None:
        assert TaskRunner._auto_name("# Task: Deploy new service\nStep 1") == "Deploy new service"

    def test_auto_name_task_header_after_generic_heading(self) -> None:
        assert TaskRunner._auto_name("# Introduction\n# Task: Deploy Service") == "Deploy Service"

    def test_auto_name_from_h1(self) -> None:
        assert TaskRunner._auto_name("# My Cool Task\ndo stuff") == "My Cool Task"

    def test_auto_name_from_spec_path(self) -> None:
        assert TaskRunner._auto_name("no heading here", "deploy_service.md") == "Deploy Service"

    def test_auto_name_fallback_first_line(self) -> None:
        assert TaskRunner._auto_name("no heading here") == "no heading here"

    def test_auto_name_empty(self) -> None:
        assert TaskRunner._auto_name("") == ""

    def test_auto_name_truncates_at_60(self) -> None:
        long = "# Task: " + "A" * 100
        assert len(TaskRunner._auto_name(long)) == 60

    def test_taskrun_name_field_default(self) -> None:
        run = TaskRun(
            spec_path="x", spec_content="x", started_at=0, last_task_time=0, status="planned"
        )
        assert run.name == ""

    def test_resolve_task_by_name(self) -> None:
        runner = TaskRunner.__new__(TaskRunner)
        runner._runs = {}
        run = TaskRun(
            spec_path="x", spec_content="x", started_at=0, last_task_time=0, status="running"
        )
        run.task_id = "abc_123"
        run.name = "deploy prod"
        runner._runs["abc_123"] = run
        assert runner._resolve_task("deploy prod") is run

    def test_resolve_task_by_id(self) -> None:
        runner = TaskRunner.__new__(TaskRunner)
        runner._runs = {}
        run = TaskRun(
            spec_path="x", spec_content="x", started_at=0, last_task_time=0, status="running"
        )
        run.task_id = "abc_123"
        runner._runs["abc_123"] = run
        assert runner._resolve_task("abc_123") is run

    def test_resolve_task_not_found(self) -> None:
        runner = TaskRunner.__new__(TaskRunner)
        runner._runs = {}
        assert runner._resolve_task("nope") is None

    @pytest.mark.asyncio
    async def test_run_uses_explicit_name(self, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# Task: Auto Name\n## Steps\n1. Do thing\n   - run: echo hi")
        runner = TaskRunner(sessions=_make_mock_sessions(), work_dir=tmp_path)
        with patch.object(runner, "_execute_tasks", new_callable=AsyncMock, return_value=True):
            run = await runner.run(spec, name="My Custom Name")
        assert run.name == "My Custom Name"

    @pytest.mark.asyncio
    async def test_run_auto_derives_name(self, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# Task: Auto Derived\n## Steps\n1. Do thing\n   - run: echo hi")
        runner = TaskRunner(sessions=_make_mock_sessions(), work_dir=tmp_path)
        with patch.object(runner, "_execute_tasks", new_callable=AsyncMock, return_value=True):
            run = await runner.run(spec)
        assert run.name == "Auto Derived"

    @pytest.mark.asyncio
    async def test_start_background_passes_name(self, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# Bg Task\n## Steps\n1. Do thing\n   - run: echo hi")
        runner = TaskRunner(sessions=_make_mock_sessions(), work_dir=tmp_path)
        with patch.object(runner, "run", new_callable=AsyncMock) as mock_run:
            task_id = await runner.start_background(spec, name="bg-task")
            assert task_id
            await asyncio.sleep(0)  # yield to let the background task run
            mock_run.assert_called_once()
            assert mock_run.call_args.kwargs.get("name") == "bg-task"


class TestWorkspaceDirValidation:
    """taskrunner.workspace_dir must reject credential/secret paths (security)."""

    def test_sensitive_workspace_dir_rejected(self):
        with pytest.raises(ValueError, match="sensitive"):
            TaskRunner(sessions=_make_mock_sessions(), workspace_dir="~/.aws")

    def test_sensitive_ssh_dir_rejected(self):
        with pytest.raises(ValueError, match="sensitive"):
            TaskRunner(sessions=_make_mock_sessions(), workspace_dir="~/.ssh")

    def test_normal_workspace_dir_accepted(self, tmp_path):
        tr = TaskRunner(sessions=_make_mock_sessions(), workspace_dir=str(tmp_path))
        assert tr._workspace_dir == str(Path(tmp_path).resolve())

    def test_traversal_into_sensitive_dir_rejected(self):
        # A `..`-traversal spelling that resolves into ~/.aws must be caught
        # AFTER canonicalization, not slip past the raw-string check.
        aws = Path("~/.aws").expanduser().resolve()
        sneaky = str(aws.parent / "x" / ".." / ".aws")
        with pytest.raises(ValueError, match="sensitive"):
            TaskRunner(sessions=_make_mock_sessions(), workspace_dir=sneaky)

    def test_symlink_to_sensitive_dir_rejected(self, tmp_path):
        # A symlink pointing at a credential dir must be rejected once resolved.
        target = Path("~/.ssh").expanduser().resolve()
        link = tmp_path / "innocent"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("symlinks not supported on this platform")
        with pytest.raises(ValueError, match="sensitive"):
            TaskRunner(sessions=_make_mock_sessions(), workspace_dir=str(link))

    def test_resolve_helper_blank_and_normal(self, tmp_path):
        from kiro_crew.taskrunner import _resolve_workspace_dir

        assert _resolve_workspace_dir("") == ""
        assert _resolve_workspace_dir("   ") == ""
        assert _resolve_workspace_dir(str(tmp_path)) == str(Path(tmp_path).resolve())

    def test_resolve_helper_rejects_sensitive(self):
        from kiro_crew.taskrunner import _resolve_workspace_dir

        with pytest.raises(ValueError, match="sensitive"):
            _resolve_workspace_dir("~/.aws")

    @pytest.mark.asyncio
    async def test_start_background_rejects_sensitive_workspace(self, tmp_path):
        # The per-run override is validated synchronously at the top of
        # start_background so the HTTP handler surfaces a 400 immediately —
        # a bad path must raise before any background task is spawned.
        runner = TaskRunner(sessions=_make_mock_sessions(), auto_test=False, work_dir=tmp_path)
        with pytest.raises(ValueError, match="sensitive"):
            await runner.start_background("anything.md", workspace_dir="~/.ssh")

    @pytest.mark.asyncio
    async def test_plan_per_run_workspace_override(self, tmp_path):
        # A per-run workspace_dir overrides the runner's default base dir: the
        # planned run operates directly in the chosen (resolved) folder.
        runner = TaskRunner(sessions=_make_mock_sessions(), auto_test=False, work_dir=tmp_path)
        runner._decompose = AsyncMock(return_value=[Step(index=1, title="step", description="desc")])
        override = tmp_path / "custom-root"
        run = await runner.plan(input_text="do X", source="text", workspace_dir=str(override))
        assert run.work_dir == str(override.resolve())


class TestMaxParallelStepsClamp:
    """`compute_max_subagents` is the host-safe ceiling; a positive
    `max_parallel_steps` may only lower it, never raise it above the ceiling."""

    def _cap(self, value):
        sessions = _make_mock_sessions()
        # Pin the computed host-safe ceiling to a known value (9).
        with patch("kiro_crew.taskrunner.compute_max_subagents", return_value=9):
            runner = TaskRunner(sessions=sessions, auto_test=False, max_parallel_steps=value)
        return runner._max_parallel_steps

    def test_auto_zero_uses_computed_ceiling(self):
        assert self._cap(0) == 9

    def test_none_uses_computed_ceiling(self):
        assert self._cap(None) == 9

    def test_positive_below_ceiling_is_honored(self):
        # Intentional throttle below the ceiling is respected.
        assert self._cap(2) == 2

    def test_positive_above_ceiling_is_clamped(self):
        # An aggressive value can never exceed the host-safe ceiling.
        assert self._cap(50) == 9

    def test_compute_failure_falls_back_to_legacy_default(self):
        sessions = _make_mock_sessions()
        with patch("kiro_crew.taskrunner.compute_max_subagents", side_effect=RuntimeError("boom")):
            runner = TaskRunner(sessions=sessions, auto_test=False, max_parallel_steps=0)
        # Falls back to _MAX_PARALLEL_TASKS (3) when the ceiling can't be computed.
        assert runner._max_parallel_steps == 3


# ── Semaphore-based parallel scheduling (replaces batch loop) ──


class TestSemaphoreParallelScheduling:
    """Pin: _execute_tasks uses asyncio.Semaphore so that a finished slot is
    refilled immediately -- a slow step must NOT block idle slots.

    Regression: the old fixed-size batch loop caused batch-stall where one slow
    step blocked the entire batch even when other slots were free.
    """

    @staticmethod
    def _make_run(tmp_path: Path, n: int, task_id: str) -> TaskRun:
        tasks = [Step(index=i, title=f"t{i}", description="d") for i in range(1, n + 1)]
        run = TaskRun(
            spec_path=str(tmp_path / "spec.md"),
            spec_content="# spec",
            tasks=tasks,
            status="running",
            task_id=task_id,
            work_dir=str(tmp_path),
        )
        run.started_at = run.last_task_time = 1.0
        return run

    def _peak_tracker(self):
        state = {"active": 0, "peak": 0}
        lock = asyncio.Lock()

        async def _fake_exec(run, task, history_key="", session_key="") -> bool:
            async with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            await asyncio.sleep(0.02)  # hold slot so concurrency overlaps
            async with lock:
                state["active"] -= 1
            task.status = StepStatus.PASSED
            task.result = "ok"
            return True

        return state, _fake_exec

    @pytest.mark.asyncio
    async def test_semaphore_caps_concurrency_at_knob(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Peak concurrency == max_parallel_steps (3) when 8 tasks are dispatched.

        The host-safe ceiling is pinned high on purpose. A small CI runner
        computes a ceiling of 3, which equals the knob under test — the
        assertion would then hold even if the knob were ignored entirely, so
        without this the test proves nothing.
        """
        monkeypatch.setattr("kiro_crew.taskrunner.compute_max_subagents", lambda _cfg: 64)
        sessions = _make_mock_sessions()
        runner = TaskRunner(
            sessions=sessions, auto_test=False, work_dir=tmp_path, max_parallel_steps=3
        )
        state, fake_exec = self._peak_tracker()
        runner._execute_single_task = fake_exec  # type: ignore[assignment]

        run = self._make_run(tmp_path, n=8, task_id="cap_test")
        await runner._execute_tasks(run, history_key="")

        assert state["peak"] == 3, f"expected peak concurrency 3, got {state['peak']}"
        assert all(t.status == StepStatus.PASSED for t in run.tasks)

    @pytest.mark.asyncio
    async def test_host_ceiling_still_caps_the_knob(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The host-safe ceiling is the upper bound; the knob may only lower it.

        Counterpart to the two tests above, which pin the ceiling out of the way
        to isolate the knob. This one pins it BELOW the knob to prove the OOM
        guard still wins — the property those tests deliberately stop covering.
        """
        monkeypatch.setattr("kiro_crew.taskrunner.compute_max_subagents", lambda _cfg: 2)
        runner = TaskRunner(
            sessions=_make_mock_sessions(),
            auto_test=False,
            work_dir=tmp_path,
            max_parallel_steps=6,
        )
        assert runner._max_parallel_steps == 2

    @pytest.mark.asyncio
    async def test_knob_lifts_above_legacy_cap_of_3(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A knob of 6 allows peak concurrency of 6, proving the legacy cap is gone.

        The host-safe ceiling is pinned high because it is derived from the
        machine's memory/CPU headroom: a small CI runner computes 3 — exactly the
        legacy value this test exists to disprove — so leaving it live makes the
        assertion depend on the runner's size rather than on the knob. The
        ceiling's own authority is covered by
        ``test_host_ceiling_still_caps_the_knob``.
        """
        monkeypatch.setattr("kiro_crew.taskrunner.compute_max_subagents", lambda _cfg: 64)
        sessions = _make_mock_sessions()
        runner = TaskRunner(
            sessions=sessions, auto_test=False, work_dir=tmp_path, max_parallel_steps=6
        )
        state, fake_exec = self._peak_tracker()
        runner._execute_single_task = fake_exec  # type: ignore[assignment]

        run = self._make_run(tmp_path, n=8, task_id="lift_test")
        await runner._execute_tasks(run, history_key="")

        assert state["peak"] == 6, f"expected peak concurrency 6, got {state['peak']}"
        assert all(t.status == StepStatus.PASSED for t in run.tasks)

    @pytest.mark.asyncio
    async def test_no_batch_stall_slot_refilled_immediately(self, tmp_path: Path) -> None:
        """KEY REGRESSION TEST: With limit=2 and 4 tasks where task 1 is slow,
        task 3 must start BEFORE task 1 finishes (it fills the slot vacated by
        task 2). The old batch loop would block because task 1 and 2 were in
        the same batch, and task 3 could only start after that batch completed.
        """
        sessions = _make_mock_sessions()
        runner = TaskRunner(
            sessions=sessions, auto_test=False, work_dir=tmp_path, max_parallel_steps=2
        )

        # Events to sequence and observe ordering
        events: list[str] = []
        task2_done = asyncio.Event()
        task3_started = asyncio.Event()
        task1_may_finish = asyncio.Event()

        async def _fake_exec(run, task, history_key="", session_key="") -> bool:
            idx = task.index
            events.append(f"start:{idx}")
            if idx == 1:
                # Slow task: waits until told to finish
                await task1_may_finish.wait()
            elif idx == 2:
                # Fast task: signals done quickly
                await asyncio.sleep(0.01)
                task2_done.set()
            elif idx == 3:
                # Must start while task 1 is still running (refills task 2's slot)
                task3_started.set()
                await asyncio.sleep(0.01)
            else:
                await asyncio.sleep(0.01)
            events.append(f"end:{idx}")
            task.status = StepStatus.PASSED
            task.result = "ok"
            return True

        runner._execute_single_task = _fake_exec  # type: ignore[assignment]
        run = self._make_run(tmp_path, n=4, task_id="stall_test")

        async def _orchestrate():
            # Wait for task 2 to finish, then verify task 3 starts before task 1 ends
            await task2_done.wait()
            await asyncio.sleep(0.05)  # give task 3 time to acquire semaphore
            assert task3_started.is_set(), (
                "BATCH STALL: task 3 did not start after task 2 freed its slot; "
                "this means the scheduler is still using fixed batches"
            )
            # Now let task 1 finish
            task1_may_finish.set()

        # Run both concurrently
        await asyncio.gather(
            runner._execute_tasks(run, history_key=""),
            _orchestrate(),
        )

        assert all(t.status == StepStatus.PASSED for t in run.tasks)
        # task 3 started before task 1 ended -- proving slot was refilled immediately
        start3_idx = events.index("start:3")
        end1_idx = events.index("end:1")
        assert start3_idx < end1_idx, (
            f"task 3 started at index {start3_idx} but task 1 ended at {end1_idx}; "
            "expected task 3 to start BEFORE task 1 finishes (slot refill)"
        )
