"""What a task run releases, saves and recovers when a step ends, fails or dies.

Every lifecycle path (success, failure, cancel, a parallel group) resets the
sessions it opened; progress is saved beside the spec; a checkpoint resumes the
completed steps; a step whose process died, looped or overflowed its context is
recovered within its budgets with a resume hint and a redacted result; a failing
step leaves a lesson, and each finished step a line in the conversation history.
The shared doubles live in ``taskrunner_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from taskrunner_helpers import (
    _make_mock_provider,
    _make_mock_sessions,
)

from kiro_crew.task_models import PROGRESS_FILE
from kiro_crew.taskrunner import Step, StepStatus, TaskRun, TaskRunner
from kiro_crew.testing.wait import async_wait_until, await_until

# ── Resource management ──


class TestResourceManagement:
    """Verify sessions are released for all task lifecycle paths."""

    @pytest.mark.asyncio
    async def test_single_task_session_reset_after_success(self, tmp_path: Path) -> None:
        """After a single task succeeds, its session must be reset."""
        sessions = _make_mock_sessions()
        sessions.reset = AsyncMock()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

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
    async def test_parallel_tasks_all_sessions_reset(self, tmp_path: Path) -> None:
        """After a parallel group completes, ALL task sessions must be reset."""
        sessions = _make_mock_sessions()
        sessions.reset = AsyncMock()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

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
    async def test_parallel_failure_still_resets_all_sessions(self, tmp_path: Path) -> None:
        """If one task in a parallel group fails, ALL sessions in the group must still be reset."""
        sessions = _make_mock_sessions()
        sessions.reset = AsyncMock()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

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

        async def track_reset(key, **kwargs):
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
    async def test_pending_tasks_hold_no_sessions(self, tmp_path: Path) -> None:
        """Tasks that never execute (PENDING) should not create any sessions."""
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

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
    async def test_cancel_running_project_cleans_up_sessions(self, tmp_path: Path) -> None:
        """cancel() on a running project must trigger _cleanup_run_sessions."""
        sessions = _make_mock_sessions()
        sessions.cancel_current = AsyncMock()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        # Simulate a long-running task that blocks until cancelled
        blocked = asyncio.Event()

        async def mock_execute(run, task, hk="", session_key=""):
            # Register the session so cleanup can find it
            sessions._sessions[session_key] = MagicMock()
            try:
                # flake-ok: pre-existing unbounded Event.wait in the block-until-cancelled stub, moved verbatim; tracked for a de-flake pass
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
        # flake-ok: pre-existing 50 ms wait for the background run to block before the cancel, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 50 ms wait for the background run to block before the cancel, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0.05)

        # Cancel it
        runner.cancel("cancel1")

        # Wait for the task to fully complete (cancel + finally block)
        await await_until(task)

        # Verify: task is cancelled, sessions were cleaned up
        assert run.status == "cancelled"
        assert run.tasks[0].status == StepStatus.CANCELLED
        # _cleanup_run_sessions should have called cancel_current + release + reset
        assert sessions.cancel_current.call_count >= 1
        assert sessions.reset.call_count >= 1

    @pytest.mark.asyncio
    async def test_cancel_mid_parallel_group_resets_all(self, tmp_path: Path) -> None:
        """Cancelling during a parallel group must clean up all sessions in the group."""
        sessions = _make_mock_sessions()
        sessions.cancel_current = AsyncMock()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        started = asyncio.Event()

        async def mock_execute(run, task, hk="", session_key=""):
            sessions._sessions[session_key] = MagicMock()
            if task.index == 1:
                started.set()
                # flake-ok: pre-existing block-until-cancelled stub sleep, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing block-until-cancelled stub sleep, moved verbatim; tracked for a de-flake pass
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

        # flake-ok: pre-existing unbounded Event.wait on a stub step the run starts itself, moved verbatim; tracked for a de-flake pass
        await started.wait()
        runner.cancel("cancel2")
        # Allow cancellation to fully propagate — CI machines may be slower
        await async_wait_until(
            lambda: run.status == "cancelled",
            describe=lambda: f"cancellation did not propagate; status={run.status}",
        )

        assert run.status == "cancelled"
        # All 3 sessions should have been targeted by cleanup
        cleaned_keys = {c.args[0] for c in sessions.cancel_current.call_args_list}
        for i in range(1, 4):
            key = f"taskrunner:cancel2:task{i}"
            if key in sessions._sessions:
                assert key in cleaned_keys, f"Session {key} not cleaned up"

    @pytest.mark.asyncio
    async def test_cancel_mid_parallel_group_reset_completes(self, tmp_path: Path) -> None:
        """reset() must run to completion under CancelledError (shield fix)."""
        sessions = _make_mock_sessions()
        sessions.cancel_current = AsyncMock()
        reset_completed = []

        async def slow_reset(key: str) -> None:
            # flake-ok: pre-existing 100 ms slow-reset stub, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 100 ms slow-reset stub, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.1)  # simulates provider.shutdown()
            reset_completed.append(key)

        sessions.reset = slow_reset
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        started = asyncio.Event()

        async def mock_execute(run, task, hk="", session_key=""):
            sessions._sessions[session_key] = MagicMock()
            if task.index == 1:
                started.set()
                # flake-ok: pre-existing block-until-cancelled stub sleep, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing block-until-cancelled stub sleep, moved verbatim; tracked for a de-flake pass
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

        # flake-ok: pre-existing unbounded Event.wait on a stub step the run starts itself, moved verbatim; tracked for a de-flake pass
        await started.wait()
        runner.cancel("shield1")
        # Wait for the cancel to actually settle (status reaches 'cancelled' and the
        # shielded resets finish) instead of guessing a fixed sleep. The old
        # `await asyncio.sleep(1.5)` was a timing guess that the slower aarch64
        # CPython 3.10 build worker exceeded under parallel xdist load, leaving the
        # state at 'cancelling' when asserted -- a flaky, worker-speed-dependent
        # failure. Polling the observable end-state is order/speed-independent.
        await async_wait_until(
            lambda: run.status == "cancelled" and len(reset_completed) >= 3,
        )

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

    def test_save_progress_without_spec_does_not_write_to_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        runner = TaskRunner(sessions=_make_mock_sessions(), auto_test=False)
        run = TaskRun(spec_path="", spec_content="ad-hoc", started_at=1000.0)
        runner._save_progress(run)
        assert not (tmp_path / PROGRESS_FILE).exists()


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
    async def test_ambiguous_delivery_death_steers_retry_to_resume_not_restart(
        self, tmp_path: Path
    ) -> None:
        """A drain-stall death carries ambiguous_delivery: the buffered step prompt
        may already have run, so the retry must resume from current state instead
        of re-stating the task verbatim (which would re-run its tools)."""
        from kiro_crew.acp.client import AcpProcessDied
        from kiro_crew.providers.base import LLMEvent

        sessions = _make_mock_sessions()
        prompts: list[str] = []
        call_count = 0

        provider = MagicMock()

        async def _ambiguous_then_succeed(msg: str):
            nonlocal call_count
            call_count += 1
            prompts.append(msg)
            if call_count == 1:
                raise AcpProcessDied("stdin stalled", ambiguous_delivery=True)
            yield LLMEvent(kind="text_chunk", text="Resumed!")
            yield LLMEvent(kind="complete")

        provider.stream = _ambiguous_then_succeed
        provider.approve_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        async def _on_notify(title: str, body: str, task_id: str = "") -> None:
            pass

        runner = TaskRunner(
            sessions=sessions, auto_test=False, on_notify=_on_notify, work_dir=tmp_path
        )
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Create the user table", description="run the migration")
        run.tasks = [step]

        success = await runner._execute_single_task(run, step)

        assert success
        assert step.status == StepStatus.PASSED
        sessions.reset.assert_called()
        # The retry prompt (second stream call) steers to resume from current
        # state, not re-run the step. (A later verification turn may also stream.)
        assert len(prompts) >= 2
        retry_prompt = prompts[1].lower()
        assert "do not restart" in retry_prompt
        assert "already have started" in retry_prompt or "already started" in retry_prompt
        assert step.resume_hint == "", "a normally completed attempt clears the hint"

    @pytest.mark.asyncio
    async def test_resume_hint_survives_a_failure_before_delivery(self, tmp_path: Path) -> None:
        """The hint must clear only AFTER the hint-bearing prompt is delivered: if a
        later turn fails before it streams, the next retry must still carry the
        resume hint rather than fall back to a verbatim replay."""
        from kiro_crew.acp.client import AcpProcessDied
        from kiro_crew.providers.base import LLMEvent

        sessions = _make_mock_sessions()
        prompts: list[str] = []
        call_count = 0

        provider = MagicMock()

        async def _ambiguous_then_fail_then_succeed(msg: str):
            nonlocal call_count
            call_count += 1
            prompts.append(msg)
            if call_count == 1:
                raise AcpProcessDied("stdin stalled", ambiguous_delivery=True)
            if call_count == 2:
                # A plain (non-ambiguous) failure BEFORE any output is streamed:
                # the hint must still be set for the next retry.
                raise RuntimeError("transient failure before delivery")
                yield  # pragma: no cover - unreachable, makes this an async gen
            yield LLMEvent(kind="text_chunk", text="Resumed!")
            yield LLMEvent(kind="complete")

        provider.stream = _ambiguous_then_fail_then_succeed
        provider.approve_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        async def _on_notify(title: str, body: str, task_id: str = "") -> None:
            pass

        runner = TaskRunner(
            sessions=sessions, auto_test=False, on_notify=_on_notify, work_dir=tmp_path
        )
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Create the user table", description="run the migration")
        run.tasks = [step]

        await runner._execute_single_task(run, step)

        # The 3rd attempt's prompt still carries the resume hint, because the 2nd
        # attempt raised before delivery and the hint was NOT cleared at render.
        assert len(prompts) >= 3
        assert "do not restart" in prompts[2].lower()

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

    @pytest.mark.parametrize("ambiguous", [False, True])
    @pytest.mark.asyncio
    async def test_process_died_exceeds_budget(self, tmp_path: Path, ambiguous: bool) -> None:
        """AcpProcessDied > _MAX_RECOVERIES → step fails. A give-up after an
        ambiguous death KEEPS the resume hint: a Resume or retry restarts the
        step at attempt 1 in the same work dir, where its prompt may already
        have run, so the restarted prompt still opens with "do not restart"."""
        from kiro_crew.acp.client import AcpProcessDied

        sessions = _make_mock_sessions()

        fail_provider = MagicMock()

        async def _always_die(msg: str):
            raise AcpProcessDied("Process exited", ambiguous_delivery=ambiguous)
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
        restarted = await runner._build_task_prompt(run, step, attempt=1)
        assert ("## Resume (do not restart)" in restarted) is ambiguous

    @pytest.mark.asyncio
    async def test_a_loop_detected_give_up_keeps_the_resume_hint(self, tmp_path: Path) -> None:
        """Only a normally completed attempt clears the hint, so a step that
        dies ambiguously and then fails on a repeated error still carries it."""
        from kiro_crew.acp.client import AcpProcessDied

        sessions = _make_mock_sessions()
        provider = MagicMock()
        calls = 0

        async def _die_then_repeat(msg: str):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise AcpProcessDied("stdin stalled", ambiguous_delivery=True)
            raise RuntimeError("the same failure")
            yield  # type: ignore[misc]  # pragma: no cover

        provider.stream = _die_then_repeat
        provider.approve_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Tag the release", description="desc")
        run.tasks = [step]

        assert not await runner._execute_single_task(run, step)
        assert step.status == StepStatus.FAILED
        assert "do not restart" in step.resume_hint.lower()

    @pytest.mark.asyncio
    async def test_a_death_after_output_and_a_tool_call_resumes_the_retry(
        self, tmp_path: Path
    ) -> None:
        """A plain death after the attempt streamed text and called a tool keeps
        the attempt number, so the "continue" error text never renders; the
        retry must still open by inspecting state instead of restating a step
        whose tool (a push) may already have run."""
        from kiro_crew.acp.client import AcpProcessDied
        from kiro_crew.providers.base import LLMEvent

        sessions = _make_mock_sessions()
        prompts: list[str] = []
        provider = _make_mock_provider("pushed")

        async def _emit_then_die_then_succeed(msg: str):
            prompts.append(msg)
            if len(prompts) == 1:
                yield LLMEvent(kind="text_chunk", text="Pushing now.")
                yield LLMEvent(kind="tool_call", title="git push origin main")
                raise AcpProcessDied("Process exited during prompt")
            yield LLMEvent(kind="text_chunk", text="Done.")
            yield LLMEvent(kind="complete")

        provider.stream = _emit_then_die_then_succeed
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Push the branch", description="git push")
        run.tasks = [step]

        assert await runner._execute_single_task(run, step)
        assert "## Resume (do not restart)" not in prompts[0]
        assert "## Resume (do not restart)" in prompts[1]
        assert step.resume_hint == "", "a normally completed attempt clears the hint"

    @pytest.mark.asyncio
    async def test_a_replan_after_a_possibly_run_step_says_so(self, tmp_path: Path) -> None:
        """The automatic replan after a give-up sees the resume hint, so the new
        tasks do not restate possibly-executed work as fresh."""
        from kiro_crew.task_models import RESUME_HINT

        runner = TaskRunner(sessions=_make_mock_sessions(), auto_test=False, work_dir=tmp_path)
        runner._decompose = AsyncMock(return_value=[])
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Tag the release", description="desc")
        step.status = StepStatus.FAILED
        step.error = "Process died 3 times — giving up"
        step.resume_hint = RESUME_HINT
        run.tasks = [step]

        assert await runner._try_replan(run, step) is False
        spec = runner._decompose.await_args.args[0]
        assert "may already have run" in spec

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
    async def test_volatile_lesson_is_not_reported_as_learned(self, tmp_path: Path) -> None:
        """A refused JSONL write must not notify or update the run ledger."""
        from kiro_crew.learn import LessonStore

        store = LessonStore(base_dir=tmp_path)
        sessions = _make_mock_sessions()
        runner = TaskRunner(
            sessions=sessions, auto_test=False, work_dir=tmp_path, lesson_store=store
        )
        runner._notify = AsyncMock()
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s")
        step = Step(index=1, title="X", description="d", error="boom")

        with patch.object(
            runner,
            "_call_llm_for_lesson",
            return_value={
                "rule": "use automatic model selection",
                "negative": "use gpt-5.6-sol",
                "category": "preference",
            },
        ):
            await runner._extract_lesson(step, run)

        assert store.load_all() == []
        assert run.lessons_learned == []
        runner._notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_private_volatile_lesson_is_not_reported_as_learned(self, tmp_path: Path) -> None:
        """A refused private vector write must not notify or update the run ledger."""
        from kiro_crew.vector_memory import LessonWriteOutcome, LessonWriteResult

        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        runner._notify = AsyncMock()
        context = MagicMock()
        context.ensure_store = AsyncMock(return_value=MagicMock())
        runner._ctx = context
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", task_id="private")
        step = Step(index=1, title="X", description="d", error="boom")

        with (
            patch(
                "kiro_crew.member_memory_auth.read_private_session_store",
                return_value="member-store",
            ),
            patch("kiro_crew.context.inherit_session_memory", new=AsyncMock()),
            patch.object(
                runner,
                "_call_llm_for_lesson",
                return_value={
                    "rule": "use automatic model selection",
                    "negative": "use gpt-5.6-sol",
                    "category": "preference",
                },
            ),
            patch(
                "kiro_crew.taskrunner.run_in_embed_pool",
                new=AsyncMock(
                    return_value=LessonWriteResult(
                        LessonWriteOutcome.REFUSED,
                        "volatile_session_fact",
                    )
                ),
            ),
        ):
            await runner._extract_lesson(step, run)

        assert run.lessons_learned == []
        runner._notify.assert_not_awaited()

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
