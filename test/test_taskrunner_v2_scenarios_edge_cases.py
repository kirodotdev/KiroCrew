"""Scenario tests for TaskRunner V2: edge cases and use-case coverage.

A parallel group that loses one step; cycle detection on alternating, consecutive,
repeated test-failure and mixed errors; replans that recurse, fail, succeed, run out,
come back empty or re-index ``depends_on``; review retries; process crashes that cost
no retry and leave the cycle counter alone; git finalize and commit edge cases; token
budgets during a replan and between groups; checkpoint resume; approval denials;
multi-group execution; a step that fails its tests and then passes; empty and
missing specs; circular dependencies; and the retry and git context of a step
prompt. The bug-fix and core scenarios are in
``test_taskrunner_v2_scenarios.py``; the fakes come from
``taskrunner_v2_scenarios_helpers.py``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from taskrunner_v2_scenarios_helpers import _passthrough_sandbox  # noqa: F401
from taskrunner_v2_scenarios_helpers import _llm_event, _make_provider, _mock_sessions

from kiro_crew.taskrunner import MAX_TOTAL_TASKS, Step, StepStatus, TaskRun, TaskRunner

# ── Additional Scenario Tests: Edge Cases & Use Case Coverage ──


class TestScenarioParallelGroupPartialSuccess:
    """When step B fails in a [A, B, C] parallel group, A (already PASSED)
    must NOT be overwritten to SKIPPED. Only C (still PENDING) gets SKIPPED."""

    @pytest.mark.asyncio
    async def test_passed_step_not_overwritten_to_skipped(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")

        # 3 independent steps → single parallel group
        step_a = Step(index=1, title="A", description="d")
        step_b = Step(index=2, title="B", description="d")
        step_c = Step(index=3, title="C", description="d")
        run.tasks = [step_a, step_b, step_c]

        call_count = 0

        async def _mock_execute_single(r, step, hk, session_key=""):
            nonlocal call_count
            call_count += 1
            if step.index == 1:
                step.status = StepStatus.PASSED
                return True
            elif step.index == 2:
                step.status = StepStatus.FAILED
                step.error = "B broke"
                return False
            step.status = StepStatus.PASSED
            return True

        with (
            patch.object(runner, "_execute_single_task", side_effect=_mock_execute_single),
            patch.object(runner, "_try_replan", return_value=False),
        ):
            await runner._execute_tasks(run, "hk")

        assert step_a.status == StepStatus.PASSED, "A already passed — must not be overwritten"
        assert step_b.status == StepStatus.FAILED
        # With asyncio.gather, C runs concurrently and passes
        assert step_c.status == StepStatus.PASSED, "C ran via gather — should be PASSED"
        assert call_count == 3, "All 3 steps run concurrently via gather"


class TestScenarioCycleDetectionAlternatingErrors:
    """Errors A→B→A should NOT trigger cycle detection (not consecutive)."""

    @pytest.mark.asyncio
    async def test_alternating_errors_no_cycle(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        provider = MagicMock()
        call_num = 0

        async def _fail_alternating(msg: str):
            nonlocal call_num
            call_num += 1
            if call_num == 1:
                raise RuntimeError("error A")
            elif call_num == 2:
                raise RuntimeError("error B")
            else:
                raise RuntimeError("error A")
            yield  # type: ignore[misc]  # pragma: no cover

        provider.stream = _fail_alternating
        provider.approve_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Alternating", description="d")
        run.tasks = [step]

        success = await runner._execute_single_task(run, step)
        assert not success
        assert step.status == StepStatus.FAILED
        # Should NOT contain "Loop detected" — errors alternated, not consecutive
        assert "Loop detected" not in (step.error or "")


class TestScenarioCycleDetectionConsecutive:
    """Same error on every attempt → cycle detection warns then fails with Loop detected."""

    @pytest.mark.asyncio
    async def test_same_error_triggers_loop(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        provider = MagicMock()

        async def _always_same_error(msg: str):
            raise RuntimeError("identical error")
            yield  # type: ignore[misc]  # pragma: no cover

        provider.stream = _always_same_error
        provider.approve_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Loopy", description="d")
        run.tasks = [step]

        notified: list[str] = []
        runner._on_notify = AsyncMock(side_effect=lambda t, b, tid="": notified.append(t))

        success = await runner._execute_single_task(run, step)
        assert not success
        assert "Loop detected" in step.error
        assert any("Possible loop" in n for n in notified)


class TestScenarioReplanRecursiveStepLimit:
    """Recursive replan should be blocked by MAX_TOTAL_TASKS."""

    @pytest.mark.asyncio
    async def test_recursive_replan_hits_step_limit(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")

        # Fill to just under the limit
        run.tasks = [
            Step(index=i, title=f"S{i}", description="d", status=StepStatus.PASSED)
            for i in range(1, MAX_TOTAL_TASKS)
        ]
        # One failed step
        failed = Step(
            index=MAX_TOTAL_TASKS,
            title="Fail",
            description="d",
            error="boom",
            status=StepStatus.FAILED,
        )
        run.tasks.append(failed)
        run.replan_count = 0

        result = await runner._try_replan(run, failed)
        assert result is False
        assert "Task limit" in run.error


class TestScenarioReviewRetryNoSecondReview:
    """After review fails → revert → retry succeeds, the step should pass
    without a second review call (current design)."""

    @pytest.mark.asyncio
    async def test_no_second_review_after_retry(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.branch_name = "kirocrew/task/test"
        step = Step(index=1, title="Reviewed", description="d")
        run.tasks = [step]

        review_calls = 0

        async def _review_once(r, s, sessions, agent, session_key="", *, ctx=None):
            nonlocal review_calls
            review_calls += 1
            return review_calls > 1  # fail first, pass second would need 2 calls

        async def _exec_step(
            r,
            s,
            sessions,
            ctx,
            agent,
            on_tool_approval,
            auto_test,
            test_cmd,
            work_dir,
            on_notify,
            session_key="",
        ):
            s.status = StepStatus.PASSED
            return True

        with (
            patch("kiro_crew.task_executor.execute_task", side_effect=_exec_step),
            patch("kiro_crew.task_executor.self_review", side_effect=_review_once),
            patch("kiro_crew.task_executor.git_coord") as mock_git,
        ):
            mock_git.commit_step = AsyncMock(return_value="abc123")
            mock_git.revert_step = AsyncMock()
            result = await runner._execute_single_task(run, step, "hk")

        # Review called once (failed), then retry succeeded — no second review
        assert review_calls == 1
        assert result is True
        assert step.status == StepStatus.PASSED


class TestScenarioProcessCrashDoesNotConsumeRetry:
    """AcpProcessDied should not consume a logic retry attempt."""

    @pytest.mark.asyncio
    async def test_crash_then_success(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        from kiro_crew.acp.client import AcpProcessDied

        call_num = 0
        provider = MagicMock()

        async def _crash_then_succeed(msg: str):
            nonlocal call_num
            call_num += 1
            if call_num == 1:
                raise AcpProcessDied()
            yield _llm_event("text_chunk", "done")
            yield _llm_event("complete")

        provider.stream = _crash_then_succeed
        provider.approve_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Crashy", description="d")
        run.tasks = [step]

        success = await runner._execute_single_task(run, step)
        assert success
        assert step.status == StepStatus.PASSED
        # Only 1 logic attempt consumed (the crash didn't count)
        assert step.attempts == 1


class TestScenarioGitFinalizeOnFailedTask:
    """Git finalize should be called even when task fails."""

    @pytest.mark.asyncio
    async def test_finalize_called_on_failure(self, tmp_path: Path) -> None:
        spec = tmp_path / "TASK.md"
        spec.write_text("# Fail task", encoding="utf-8")

        sessions = _mock_sessions()
        sessions.get_or_create = AsyncMock(return_value=(_make_provider(), True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        finalize_called = False

        async def _mock_finalize(r):
            nonlocal finalize_called
            finalize_called = True
            return r.branch_name

        with (
            patch.object(
                runner,
                "_decompose",
                return_value=[
                    Step(index=1, title="Fail", description="d"),
                ],
            ),
            patch.object(runner, "_execute_tasks", side_effect=RuntimeError("boom")),
            patch("kiro_crew.taskrunner.git_coord") as mock_git,
        ):
            mock_git.init_workspace = AsyncMock(
                side_effect=lambda r: setattr(r, "branch_name", "kirocrew/task/test")
            )
            mock_git.finalize = _mock_finalize

            result = await runner.run(spec)

        assert result.status == "failed"
        assert finalize_called


class TestScenarioReplanNewStepsFailThenSecondReplan:
    """First replan succeeds with new steps, one fails, second replan is attempted."""

    @pytest.mark.asyncio
    async def test_double_replan(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")

        # Original step failed
        step1 = Step(
            index=1,
            title="Original",
            description="d",
            status=StepStatus.FAILED,
            error="original fail",
        )
        run.tasks = [step1]
        run.replan_count = 0

        decompose_count = 0

        async def _mock_decompose(spec, wd="", tid=""):
            nonlocal decompose_count
            decompose_count += 1
            if decompose_count == 1:
                return [Step(index=1, title="Replan1", description="d")]
            elif decompose_count == 2:
                return [Step(index=1, title="Replan2", description="d")]
            return []

        exec_count = 0

        async def _mock_exec(r, step, hk, session_key=""):
            nonlocal exec_count
            exec_count += 1
            if exec_count == 1:
                # First replan step fails
                step.status = StepStatus.FAILED
                step.error = "replan1 fail"
                return False
            # Second replan step succeeds
            step.status = StepStatus.PASSED
            return True

        with (
            patch.object(runner, "_decompose", side_effect=_mock_decompose),
            patch.object(runner, "_execute_single_task", side_effect=_mock_exec),
            patch("kiro_crew.task_executor.git_coord") as mock_git,
        ):
            mock_git.get_state_summary = AsyncMock(return_value="")
            result = await runner._try_replan(run, step1)

        assert result is True
        assert run.replan_count == 2
        assert decompose_count == 2


class TestScenarioTokenBudgetDuringReplan:
    """Token budget exceeded during replan execution should stop cleanly."""

    @pytest.mark.asyncio
    async def test_budget_exceeded_in_replan(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(
            sessions=sessions, auto_test=False, work_dir=tmp_path, token_budget=1000
        )
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.tokens_used = 999  # just under budget

        step1 = Step(index=1, title="Done", description="d", status=StepStatus.PASSED)
        failed = Step(
            index=2, title="Fail", description="d", status=StepStatus.FAILED, error="fail"
        )
        run.tasks = [step1, failed]

        async def _mock_decompose(spec, wd="", tid=""):
            return [
                Step(index=1, title="R1", description="d"),
                Step(index=1, title="R2", description="d"),
            ]

        async def _mock_exec(r, step, hk, session_key=""):
            r.tokens_used = 1001  # exceed budget after first step
            step.status = StepStatus.PASSED
            return True

        with (
            patch.object(runner, "_decompose", side_effect=_mock_decompose),
            patch.object(runner, "_execute_single_task", side_effect=_mock_exec),
            patch("kiro_crew.task_executor.git_coord") as mock_git,
        ):
            mock_git.get_state_summary = AsyncMock(return_value="")
            result = await runner._try_replan(run, failed)

        # Budget exceeded → replan returns False
        assert result is False
        assert "Token budget" in run.error


class TestScenarioCheckpointResumeWithGit:
    """Checkpoint resume skips steps, git init still happens, remaining steps execute."""

    @pytest.mark.asyncio
    async def test_checkpoint_resume_with_git(self, tmp_path: Path) -> None:
        spec = tmp_path / "TASK.md"
        spec.write_text("# Resume task", encoding="utf-8")

        sessions = _mock_sessions()
        sessions.get_or_create = AsyncMock(return_value=(_make_provider(), True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        steps_executed: list[int] = []

        async def _mock_exec_steps(r, hk):
            for s in r.tasks:
                if s.status == StepStatus.PENDING:
                    steps_executed.append(s.index)
                    s.status = StepStatus.PASSED

        git_init_called = False

        async def _mock_git_init(r):
            nonlocal git_init_called
            git_init_called = True
            r.branch_name = "kirocrew/task/test"

        with (
            patch.object(
                runner,
                "_decompose",
                return_value=[
                    Step(index=1, title="Already Done", description="d"),
                    Step(index=2, title="New Work", description="d"),
                ],
            ),
            patch.object(runner, "_load_checkpoint", return_value={"already done"}),
            patch.object(runner, "_execute_tasks", side_effect=_mock_exec_steps),
            patch("kiro_crew.taskrunner.git_coord") as mock_git,
        ):
            mock_git.init_workspace = _mock_git_init
            mock_git.finalize = AsyncMock(return_value="kirocrew/task/test")

            result = await runner.run(spec)

        assert git_init_called, "Git init should happen before checkpoint resume"
        assert result.tasks[0].status == StepStatus.PASSED  # from checkpoint
        assert result.status == "completed"


class TestScenarioApprovalDeniedInParallelGroup:
    """Approval denied should skip step (return True), not trigger replan."""

    @pytest.mark.asyncio
    async def test_approval_denied_no_replan(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        sessions.get_or_create = AsyncMock(return_value=(_make_provider(), True, False))

        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            on_approval=AsyncMock(return_value=False),
        )
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")

        step = Step(index=1, title="Needs Approval", description="d", requires_approval=True)
        run.tasks = [step]

        result = await runner._execute_single_task(run, step, "hk")
        assert result is False  # denied → paused, not failed
        assert step.status == StepStatus.PENDING
        assert run.status == "paused"


class TestScenarioMultiGroupExecution:
    """Steps with dependencies form multiple groups: [A,B] then [C depends on A]."""

    @pytest.mark.asyncio
    async def test_dependency_groups_execute_in_order(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")

        step_a = Step(index=1, title="A", description="d")
        step_b = Step(index=2, title="B", description="d")
        step_c = Step(index=3, title="C", description="d", depends_on=[1])
        run.tasks = [step_a, step_b, step_c]

        exec_order: list[int] = []

        async def _mock_exec(r, step, hk, session_key=""):
            exec_order.append(step.index)
            step.status = StepStatus.PASSED
            return True

        with patch.object(runner, "_execute_single_task", side_effect=_mock_exec):
            await runner._execute_tasks(run, "hk")

        # A and B in first group (order within group may vary), C after
        assert 3 in exec_order
        assert exec_order.index(3) > exec_order.index(1), "C must run after A"


class TestScenarioStepWithTestFailureThenPass:
    """Step passes LLM execution but tests fail, then succeeds on retry."""

    @pytest.mark.asyncio
    async def test_test_failure_retry_succeeds(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        provider = MagicMock()
        call_num = 0

        async def _stream(msg: str):
            nonlocal call_num
            call_num += 1
            yield _llm_event("text_chunk", f"attempt {call_num}")
            yield _llm_event("complete")

        provider.stream = _stream
        provider.approve_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        test_call = 0

        async def _run_tests(test_cmd, work_dir):
            nonlocal test_call
            test_call += 1
            if test_call == 1:
                return False, "AssertionError: expected 1 got 2"
            return True, "All tests passed"

        runner = TaskRunner(sessions=sessions, auto_test=True, work_dir=tmp_path)
        runner._test_cmd = ["make", "test"]
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Impl", description="d")
        run.tasks = [step]

        with patch("kiro_crew.task_executor.run_tests", side_effect=_run_tests):
            success = await runner._execute_single_task(run, step)

        assert success
        assert step.status == StepStatus.PASSED
        assert step.attempts == 2


class TestScenarioEmptySpecFile:
    """Empty spec file should raise ValueError."""

    @pytest.mark.asyncio
    async def test_empty_spec_raises(self, tmp_path: Path) -> None:
        spec = tmp_path / "EMPTY.md"
        spec.write_text("", encoding="utf-8")

        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        with pytest.raises(ValueError, match="empty"):
            await runner.run(spec)


class TestScenarioMissingSpecFile:
    """Missing spec file should raise FileNotFoundError."""

    @pytest.mark.asyncio
    async def test_missing_spec_raises(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        with pytest.raises(FileNotFoundError):
            await runner.run(tmp_path / "NONEXISTENT.md")


class TestScenarioReplanMaxReplanExhausted:
    """When replan_count >= _MAX_REPLAN, _try_replan should fail immediately."""

    @pytest.mark.asyncio
    async def test_max_replan_exhausted(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.replan_count = 2  # _MAX_REPLAN = 2

        failed = Step(
            index=1, title="Fail", description="d", error="boom", status=StepStatus.FAILED
        )
        run.tasks = [failed]

        result = await runner._try_replan(run, failed)
        assert result is False
        assert run.status == "failed"


class TestScenarioGroupParallelStepsDeadlock:
    """Steps with circular deps should fall back to sequential execution."""

    def test_circular_deps_sequential_fallback(self) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)

        # A depends on B, B depends on A → deadlock
        steps = [
            Step(index=1, title="A", description="d", depends_on=[2]),
            Step(index=2, title="B", description="d", depends_on=[1]),
        ]
        groups = runner._group_parallel_tasks(steps)
        # Should not hang — falls back to sequential
        assert len(groups) == 2
        assert len(groups[0]) == 1
        assert len(groups[1]) == 1


class TestScenarioGitCommitNoChanges:
    """When a step makes no file changes, commit_step returns empty string,
    and revert should not be called on review failure."""

    @pytest.mark.asyncio
    async def test_no_changes_no_revert_on_review_fail(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.branch_name = "kirocrew/task/test"
        step = Step(index=1, title="NoOp", description="d")
        run.tasks = [step]

        exec_count = 0

        async def _exec_side_effect(
            run,
            task,
            sessions,
            ctx,
            agent,
            on_tool_approval,
            auto_test,
            test_cmd,
            work_dir,
            on_notify,
            session_key="",
        ):
            nonlocal exec_count
            exec_count += 1
            task.status = StepStatus.PASSED
            return True

        with (
            patch("kiro_crew.task_executor.execute_task", side_effect=_exec_side_effect),
            patch("kiro_crew.task_executor.self_review", return_value=False),
            patch("kiro_crew.task_executor.git_coord") as mock_git,
        ):
            mock_git.commit_step = AsyncMock(return_value="")  # no changes
            mock_git.revert_step = AsyncMock()

            await runner._execute_single_task(run, step, "hk")

        # revert should NOT have been called (nothing was committed)
        mock_git.revert_step.assert_not_called()


class TestScenarioBuildStepPromptRetryContext:
    """Retry attempt should include error context in the prompt."""

    @pytest.mark.asyncio
    async def test_retry_prompt_includes_error(self) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        run = TaskRun(spec_path="/t.md", spec_content="spec")
        step = Step(index=1, title="Fix", description="fix the bug", error="TypeError: NoneType")
        run.tasks = [step]

        prompt = await runner._build_task_prompt(run, step, attempt=2)
        assert "retry attempt 2" in prompt.lower() or "attempt 2" in prompt.lower()
        assert "TypeError: NoneType" in prompt


class TestScenarioBuildStepPromptGitContext:
    """When branch_name is set, prompt should include git context."""

    @pytest.mark.asyncio
    async def test_prompt_includes_git_branch(self) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        run = TaskRun(spec_path="/t.md", spec_content="spec")
        run.branch_name = "kirocrew/task/my-task"
        step = Step(index=1, title="Code", description="write code")
        run.tasks = [step]

        with patch("kiro_crew.task_executor.git_coord") as mock_git:
            mock_git.get_state_summary = AsyncMock(return_value="## Git Log\nstep 1: setup")
            prompt = await runner._build_task_prompt(run, step, attempt=1)

        assert "kirocrew/task/my-task" in prompt
        assert "Git Log" in prompt


# ═══════════════════════════════════════════════════════════════════════
# Scenario: Test failure cycle detection (same test output 3 times)
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioTestFailureCycleDetection:
    """Same test output on every attempt should trigger loop detection."""

    @pytest.mark.asyncio
    async def test_same_test_failure_triggers_loop(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        provider = _make_provider("code changes")
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=True, work_dir=tmp_path)
        runner._test_cmd = ["fake-test"]
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Fix tests", description="d")
        run.tasks = [step]

        with patch("kiro_crew.task_executor.run_tests", return_value=(False, "FAIL: test_foo")):
            success = await runner._execute_single_task(run, step)

        assert not success
        assert "Loop detected" in step.error


# ═══════════════════════════════════════════════════════════════════════
# Scenario: Mixed exception then test failure — no cycle
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioMixedErrorTypes:
    """Exception on attempt 1, test failure on attempt 2 → different errors, no cycle."""

    @pytest.mark.asyncio
    async def test_exception_then_test_fail_no_cycle(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()

        call_count = 0

        async def _stream_or_fail(msg):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("connection timeout")
            yield _llm_event("text_chunk", "fixed code")
            yield _llm_event("complete")

        provider = MagicMock()
        provider.stream = _stream_or_fail
        provider.approve_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=True, work_dir=tmp_path)
        runner._test_cmd = ["fake-test"]
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Fix", description="d")
        run.tasks = [step]

        test_calls = 0

        async def _run_tests(test_cmd, work_dir):
            nonlocal test_calls
            test_calls += 1
            return (False, "FAIL: test_bar")

        with patch("kiro_crew.task_executor.run_tests", side_effect=_run_tests):
            success = await runner._execute_single_task(run, step)

        assert not success
        # Should NOT say "Loop detected" — exception and test failure are different
        assert "Loop detected" not in step.error


# ═══════════════════════════════════════════════════════════════════════
# Scenario: Successful replan completes task as "completed"
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioReplanSuccessCompletesTask:
    """When replan succeeds and new steps pass, task status is 'completed'."""

    @pytest.mark.asyncio
    async def test_replan_success_sets_completed(self, tmp_path: Path) -> None:
        spec = tmp_path / "task.md"
        spec.write_text("Build a widget")

        sessions = _mock_sessions()
        provider = _make_provider("done")
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        exec_count = 0

        async def _decompose(spec_text, work_dir="", task_id=""):
            return [Step(index=1, title="Task ", description="d")]

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        async def _exec_single(run, step, hk, session_key=""):
            nonlocal exec_count
            exec_count += 1
            if exec_count == 1:
                step.status = StepStatus.FAILED
                step.error = "oops"
                return False
            step.status = StepStatus.PASSED
            return True

        with (
            patch.object(runner, "_decompose", side_effect=_decompose),
            patch.object(runner, "_execute_single_task", side_effect=_exec_single),
            patch("kiro_crew.task_executor.git_coord") as mock_git,
        ):
            mock_git.init_workspace = AsyncMock()
            mock_git.finalize = AsyncMock()
            run = await runner.run(spec)

        assert run.status == "completed"
        assert run.replan_count == 1


# ═══════════════════════════════════════════════════════════════════════
# Scenario: Review retry also fails → step ends FAILED
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioReviewRetryAlsoFails:
    """Step passes, review fails, retry also fails → step is FAILED."""

    @pytest.mark.asyncio
    async def test_review_fail_retry_fail(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.branch_name = "kirocrew/task/test"
        step = Step(index=1, title="Code", description="d")
        run.tasks = [step]

        call_count = 0

        async def _exec_step(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                step.status = StepStatus.PASSED
                return True
            # Retry after review fail also fails
            step.status = StepStatus.FAILED
            step.error = "still broken"
            return False

        with (
            patch("kiro_crew.task_executor.execute_task", side_effect=_exec_step),
            patch("kiro_crew.task_executor.self_review", return_value=False),
            patch("kiro_crew.task_executor.git_coord") as mock_git,
        ):
            mock_git.commit_step = AsyncMock(return_value="abc123")
            mock_git.revert_step = AsyncMock()
            result = await runner._execute_single_task(run, step, "hk")

        assert not result
        assert step.status == StepStatus.FAILED
        mock_git.revert_step.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# Scenario: Replan re-indexes depends_on correctly
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioReplanDependsOnReindex:
    """New steps from replan have depends_on shifted by base index."""

    @pytest.mark.asyncio
    async def test_replan_reindexes_deps(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.tasks = [
            Step(index=1, title="A", description="d", status=StepStatus.PASSED),
            Step(index=2, title="B", description="d", status=StepStatus.FAILED),
        ]
        failed = run.tasks[1]
        failed.error = "broken"

        # Replan returns 2 new steps where step 2 depends on step 1
        new_steps = [
            Step(index=1, title="C", description="d"),
            Step(index=2, title="D", description="d", depends_on=[1]),
        ]

        async def _exec_single(r, s, hk, session_key=""):
            s.status = StepStatus.PASSED
            return True

        with (
            patch.object(runner, "_decompose", return_value=new_steps),
            patch.object(runner, "_execute_single_task", side_effect=_exec_single),
        ):
            result = await runner._try_replan(run, failed)

        assert result is True
        # Original 2 steps + 2 new = 4 total
        assert len(run.tasks) == 4
        # New step D (index 4) should depend on new step C (index 3)
        step_d = run.tasks[3]
        assert step_d.index == 4
        assert step_d.depends_on == [3]


# ═══════════════════════════════════════════════════════════════════════
# Scenario: Multiple sequential groups — first passes, second fails
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioMultiGroupFirstPassSecondFail:
    """Two sequential groups: group 1 passes, group 2 fails → task fails."""

    @pytest.mark.asyncio
    async def test_second_group_failure(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        # Step 2 depends on step 1 → two groups
        run.tasks = [
            Step(index=1, title="Setup", description="d"),
            Step(index=2, title="Build", description="d", depends_on=[1]),
        ]

        call_count = 0

        async def _exec_single(r, s, hk, session_key=""):
            nonlocal call_count
            call_count += 1
            if s.index == 1:
                s.status = StepStatus.PASSED
                return True
            s.status = StepStatus.FAILED
            s.error = "build failed"
            return False

        with (
            patch.object(runner, "_execute_single_task", side_effect=_exec_single),
            patch.object(runner, "_try_replan", return_value=False),
        ):
            await runner._execute_tasks(run, "hk")

        assert run.tasks[0].status == StepStatus.PASSED
        assert run.tasks[1].status == StepStatus.FAILED
        assert run.status == "failed"
        assert "Task 2 failed" in run.error


# ═══════════════════════════════════════════════════════════════════════
# Scenario: Process crash doesn't affect cycle detection counter
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioCrashDoesNotAffectCycleCounter:
    """AcpProcessDied should not set previous_error or increment cycle counter."""

    @pytest.mark.asyncio
    async def test_crash_does_not_pollute_previous_error(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()

        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("error A")
            if call_count == 2:
                from kiro_crew.acp.client import AcpProcessDied

                raise AcpProcessDied()
            # After crash recovery, different error — should not match "error A"
            raise RuntimeError("error B")
            yield  # type: ignore[misc]  # pragma: no cover

        provider = MagicMock()
        provider.stream = _stream
        provider.approve_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Work", description="d")
        run.tasks = [step]

        success = await runner._execute_single_task(run, step)
        assert not success
        # Crash between "error A" and "error B" means they're not consecutive
        # same errors, so no loop detection should fire
        assert "Loop detected" not in step.error


# ═══════════════════════════════════════════════════════════════════════
# Scenario: Approval denied returns True (skipped, not failed)
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioApprovalDeniedIsNotFailure:
    """Approval denied → step SKIPPED, returns True (not a failure)."""

    @pytest.mark.asyncio
    async def test_denied_step_skipped_not_failed(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            on_approval=AsyncMock(return_value=False),
        )
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Dangerous", description="d", requires_approval=True)
        run.tasks = [step]

        result = await runner._execute_single_task(run, step, "hk")
        assert result is False
        assert step.status == StepStatus.PENDING
        assert run.status == "paused"


# ═══════════════════════════════════════════════════════════════════════
# Scenario: Git commit failure is non-fatal
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioGitCommitFailureNonFatal:
    """If git commit throws, step still succeeds."""

    @pytest.mark.asyncio
    async def test_commit_exception_step_still_passes(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.branch_name = "kirocrew/task/test"
        step = Step(index=1, title="Code", description="d")
        run.tasks = [step]

        with (
            patch("kiro_crew.task_executor.execute_task", return_value=True),
            patch("kiro_crew.task_executor.self_review", return_value=True) as review,
            patch("kiro_crew.task_executor.git_coord") as mock_git,
        ):
            mock_git.commit_step = AsyncMock(side_effect=RuntimeError("git broken"))
            result = await runner._execute_single_task(run, step, "hk")

        assert result is True
        # The failed commit is why there is no diff: the no-diff refusal is skipped.
        assert review.call_args.kwargs.get("commit_failed") is True


# ═══════════════════════════════════════════════════════════════════════
# Scenario: Replan with empty decomposition returns False
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioReplanEmptyDecompFails:
    """If replan decomposition returns no steps, replan fails."""

    @pytest.mark.asyncio
    async def test_empty_replan_fails(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.tasks = [
            Step(index=1, title="A", description="d", status=StepStatus.FAILED),
        ]
        run.tasks[0].error = "broken"

        with patch.object(runner, "_decompose", return_value=[]):
            result = await runner._try_replan(run, run.tasks[0])

        assert result is False
        assert run.status == "failed"
        assert "Re-plan failed" in run.error


# ═══════════════════════════════════════════════════════════════════════
# Scenario: Token budget exceeded between groups stops execution
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioTokenBudgetBetweenGroups:
    """Token budget exceeded after group 1 → group 2 not started."""

    @pytest.mark.asyncio
    async def test_budget_stops_next_group(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path, token_budget=100)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.tasks = [
            Step(index=1, title="A", description="d"),
            Step(index=2, title="B", description="d", depends_on=[1]),
        ]

        async def _exec_single(r, s, hk, session_key=""):
            s.status = StepStatus.PASSED
            r.tokens_used = 200  # exceed budget
            return True

        with patch.object(runner, "_execute_single_task", side_effect=_exec_single):
            await runner._execute_tasks(run, "hk")

        assert run.status == "failed"
        assert "Token budget" in run.error
        assert run.tasks[0].status == StepStatus.PASSED
        assert run.tasks[1].status == StepStatus.PENDING
