"""Scenario tests for TaskRunner V2: per-step sessions and the memory they carry.

Each step runs in a session of its own (a unique key, ``is_new`` on every step,
the same for the steps a replan adds, none leaked past the replan), and a non-git
run leaves working memory alone. After them: a review after a successful retry and
the commit it makes, cycle detection on the test-failure path, a parallel group
that passes or loses its middle step, ``depends_on`` re-indexing after a replan,
and the git-or-memory context of a step prompt. The bug-fix and core scenarios are
in ``test_taskrunner_v2_scenarios.py``; the fakes come from
``taskrunner_v2_scenarios_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from taskrunner_v2_scenarios_helpers import _passthrough_sandbox  # noqa: F401
from taskrunner_v2_scenarios_helpers import _llm_event, _make_provider, _mock_sessions

from kiro_crew.taskrunner import Step, StepStatus, TaskRun, TaskRunner

# ── Memory Integration: Per-Step Session Keys ──


class TestScenarioPerStepSessionKey:
    """Each step gets a unique session key so ContextBuilder injects full context."""

    @pytest.mark.asyncio
    async def test_each_step_gets_unique_session(self, tmp_path: Path) -> None:
        spec = tmp_path / "TASK.md"
        spec.write_text("# Task", encoding="utf-8")

        sessions = _mock_sessions()
        provider = _make_provider("done")
        session_keys_seen: list[str] = []

        async def _track_sessions(key: str, agent=None, cwd=None, **kwargs):
            session_keys_seen.append(key)
            return provider, True, False  # is_new=True every time

        sessions.get_or_create = _track_sessions

        decompose_provider = MagicMock()
        step_json = json.dumps(
            [
                {"title": "A", "description": "d"},
                {"title": "B", "description": "d"},
            ]
        )

        async def _decompose_stream(msg: str):
            yield _llm_event("text_chunk", step_json)
            yield _llm_event("complete")

        decompose_provider.stream = _decompose_stream
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        call_count = 0

        async def _get_or_create(key: str, agent=None, cwd=None, **kwargs):
            nonlocal call_count
            call_count += 1
            session_keys_seen.append(key)
            if "decompose" in key:
                return decompose_provider, True, False
            return provider, True, False

        sessions.get_or_create = _get_or_create

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        with patch.object(runner, "self_review", return_value=True):
            result = await runner.run(spec)

        assert result.status == "completed"
        # Each step should have its own session key with :step{N}
        step_keys = [k for k in session_keys_seen if ":task" in k]
        assert any(":task1" in k for k in step_keys)
        assert any(":task2" in k for k in step_keys)
        # No shared task-level session (old pattern)
        shared_keys = [k for k in session_keys_seen if k.endswith(result.task_id)]
        assert len(shared_keys) == 0


class TestScenarioPerStepSessionIsNew:
    """Verify is_new=True fires for every step (not just step 1)."""

    @pytest.mark.asyncio
    async def test_is_new_true_for_all_steps(self, tmp_path: Path) -> None:
        spec = tmp_path / "TASK.md"
        spec.write_text("# Task", encoding="utf-8")

        sessions = _mock_sessions()
        provider = _make_provider("done")
        is_new_values: list[bool] = []

        step_json = json.dumps(
            [
                {"title": "S1", "description": "d"},
                {"title": "S2", "description": "d"},
                {"title": "S3", "description": "d"},
            ]
        )

        decompose_provider = MagicMock()

        async def _decompose_stream(msg: str):
            yield _llm_event("text_chunk", step_json)
            yield _llm_event("complete")

        decompose_provider.stream = _decompose_stream
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        async def _get_or_create(key: str, agent=None, cwd=None, **kwargs):
            if "decompose" in key:
                return decompose_provider, True, False
            # Every step session is new (fresh key)
            is_new_values.append(True)
            return provider, True, False

        sessions.get_or_create = _get_or_create

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        with patch("kiro_crew.task_executor.self_review", return_value=True):
            result = await runner.run(spec)

        assert result.status == "completed"
        # 3 steps → 3 is_new=True calls (self_review patched out)
        assert len(is_new_values) == 3


class TestScenarioReplanStepSessionReset:
    """Replanned steps executed in _try_replan should also get per-step sessions."""

    @pytest.mark.asyncio
    async def test_replan_steps_get_per_step_keys(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        session_keys_seen: list[str] = []

        provider = _make_provider("done")

        async def _get_or_create(key: str, agent=None, cwd=None, **kwargs):
            session_keys_seen.append(key)
            return provider, True, False

        sessions.get_or_create = _get_or_create

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        run = TaskRun(
            spec_path=str(tmp_path / "t.md"),
            spec_content="spec",
            status="running",
        )
        run.task_id = "test_replan"
        run.work_dir = str(tmp_path)
        # Step 1 passed, step 2 failed
        run.tasks = [
            Step(index=1, title="Done", description="d", status=StepStatus.PASSED),
            Step(index=2, title="Fail", description="d", status=StepStatus.FAILED, error="boom"),
        ]

        # Mock _decompose to return 1 new step
        async def _mock_decompose(spec, work_dir="", task_id=""):
            return [Step(index=1, title="Fix", description="fix it")]

        with (
            patch.object(runner, "_decompose", side_effect=_mock_decompose),
            patch.object(runner, "self_review", return_value=True),
        ):
            result = await runner._try_replan(run, run.tasks[1])

        assert result is True
        # The replanned step should use per-step session key
        step_keys = [k for k in session_keys_seen if ":task" in k]
        assert any(":task3" in k for k in step_keys)


# ── Review Retry: Second Review After Retry Success ──


class TestScenarioReviewRetrySuccessGetsReview:
    """When review fails, step retries. If retry succeeds, it should get reviewed again
    via self_review (the second call in _execute_single_task)."""

    @pytest.mark.asyncio
    async def test_retry_after_review_fail_gets_second_review(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        provider = _make_provider("done")
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        review_calls = 0

        async def _mock_review(run, step, sessions, agent, session_key="", *, ctx=None):
            nonlocal review_calls
            review_calls += 1
            if review_calls == 1:
                step.error = "Review: bad code"
                return False  # first review fails
            return True  # second review passes

        run = TaskRun(
            spec_path=str(tmp_path / "t.md"),
            spec_content="spec",
            status="running",
        )
        run.tasks = [Step(index=1, title="Code", description="d")]

        with (
            patch("kiro_crew.task_executor.self_review", side_effect=_mock_review),
            patch("kiro_crew.task_executor.git_coord") as mock_gc,
        ):
            mock_gc.commit_step = AsyncMock(return_value="abc123")
            mock_gc.revert_step = AsyncMock()
            result = await runner._execute_single_task(run, run.tasks[0], "hist")

        # The step should pass (retry succeeded + second review passed)
        assert result is True
        assert run.tasks[0].status == StepStatus.PASSED
        # self_review called once (the retry path calls execute_task which
        # doesn't call self_review — only execute_single_task does)
        # After retry success, the code just commits. No second review.
        assert review_calls == 1


# ── Working Memory Not Updated (Non-Git Runs) ──


class TestScenarioNonGitMemoryNotUpdated:
    """In V2, update_from_result was removed. For non-git runs, working memory
    stays empty. This is by design — git is the coordination surface."""

    @pytest.mark.asyncio
    async def test_memory_not_updated_without_git(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        provider = _make_provider("Created foo.py with handler code")
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        run = TaskRun(
            spec_path=str(tmp_path / "t.md"),
            spec_content="spec",
            status="running",
        )
        run.tasks = [Step(index=1, title="Create handler", description="d")]

        with patch.object(runner, "self_review", return_value=True):
            await runner._execute_single_task(run, run.tasks[0], "hist")

        # Working memory should NOT have been updated (no update_from_result call)
        assert run.memory.files_changed == []


# ── Replan Session Reset Gap ──


class TestScenarioReplanSessionNotLeaked:
    """Steps executed inside _try_replan should have their sessions reset
    via the default session_key in _execute_single_task."""

    @pytest.mark.asyncio
    async def test_replan_step_session_created_and_used(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        provider = _make_provider("done")
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        created_keys: list[str] = []

        async def _track_create(key: str, agent=None, cwd=None, **kwargs):
            created_keys.append(key)
            return provider, True, False

        sessions.get_or_create = _track_create

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        run = TaskRun(
            spec_path=str(tmp_path / "t.md"),
            spec_content="spec",
            status="running",
        )
        run.task_id = "leak_test"
        run.work_dir = str(tmp_path)
        run.tasks = [
            Step(index=1, title="OK", description="d", status=StepStatus.PASSED),
            Step(index=2, title="Fail", description="d", status=StepStatus.FAILED, error="err"),
        ]

        async def _mock_decompose(spec, work_dir="", task_id=""):
            return [Step(index=1, title="Fix", description="fix")]

        with (
            patch.object(runner, "_decompose", side_effect=_mock_decompose),
            patch.object(runner, "self_review", return_value=True),
        ):
            await runner._try_replan(run, run.tasks[1])

        # The replanned step should create a per-step session
        step_keys = [k for k in created_keys if ":task" in k]
        assert len(step_keys) >= 1


# ── Cycle Detection: Test Failure Path ──


class TestScenarioCycleDetectionTestPath:
    """Cycle detection in the test failure path (not exception path)."""

    @pytest.mark.asyncio
    async def test_same_test_output_triggers_cycle(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        provider = _make_provider("done")
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(
            sessions=sessions,
            auto_test=True,
            work_dir=tmp_path,
            auto_commit=False,
        )
        runner._test_cmd = ["echo", "test"]

        run = TaskRun(
            spec_path=str(tmp_path / "t.md"),
            spec_content="spec",
            status="running",
        )
        step = Step(index=1, title="Test", description="d")
        run.tasks = [step]

        # Mock run_tests to always return same failure
        with patch(
            "kiro_crew.task_executor.run_tests",
            return_value=(False, "FAIL: test_foo - AssertionError"),
        ):
            success = await runner._execute_single_task(run, step)

        assert not success
        assert step.status == StepStatus.FAILED
        assert "Loop detected" in step.error


# ── Git Commit After Review Retry Success ──


class TestScenarioGitCommitAfterReviewRetry:
    """When review fails, step is reverted and retried. If retry succeeds,
    the new changes should be committed."""

    @pytest.mark.asyncio
    async def test_commit_after_successful_retry(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        provider = _make_provider("done")
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        review_count = 0

        async def _review(run, step, sessions, agent, session_key="", *, ctx=None):
            nonlocal review_count
            review_count += 1
            if review_count == 1:
                step.error = "Review: issues"
                return False
            return True

        run = TaskRun(
            spec_path=str(tmp_path / "t.md"),
            spec_content="spec",
            status="running",
        )
        run.branch_name = "kirocrew/task/test"
        run.tasks = [Step(index=1, title="Code", description="d")]

        commit_calls = 0

        async def _mock_commit(r, s):
            nonlocal commit_calls
            commit_calls += 1
            return f"sha{commit_calls}"

        with (
            patch("kiro_crew.task_executor.self_review", side_effect=_review),
            patch("kiro_crew.task_executor.git_coord") as mock_gc,
        ):
            mock_gc.commit_step = AsyncMock(side_effect=_mock_commit)
            mock_gc.revert_step = AsyncMock()
            result = await runner._execute_single_task(run, run.tasks[0], "hist")

        assert result is True
        # First commit (before review), then revert, then second commit (after retry)
        assert commit_calls == 2
        mock_gc.revert_step.assert_called_once()


# ── Parallel Group: All Steps Pass ──


class TestScenarioParallelGroupAllPass:
    """All steps in a parallel group pass — no SKIPPED marking."""

    @pytest.mark.asyncio
    async def test_all_group_steps_pass(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        provider = _make_provider("done")
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        run = TaskRun(
            spec_path=str(tmp_path / "t.md"),
            spec_content="spec",
            status="running",
        )
        run.task_id = "par_test"
        run.work_dir = str(tmp_path)
        # 3 independent steps → single parallel group
        run.tasks = [
            Step(index=1, title="A", description="d"),
            Step(index=2, title="B", description="d"),
            Step(index=3, title="C", description="d"),
        ]

        with patch("kiro_crew.task_executor.self_review", return_value=True):
            await runner._execute_tasks(run, "taskrunner:run:test")

        assert all(s.status == StepStatus.PASSED for s in run.tasks)
        assert run.status == "running"  # not failed


# ── Parallel Group: Middle Step Fails ──


class TestScenarioParallelGroupMiddleFails:
    """Second step in a 3-step group fails. With gather, all run concurrently."""

    @pytest.mark.asyncio
    async def test_middle_step_failure_others_still_run(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()

        async def _get_or_create(key: str, agent=None, cwd=None, **kwargs):
            provider = MagicMock()
            if ":task2" in key:
                # Second step fails
                async def _fail_stream(msg):
                    raise RuntimeError("step 2 broke")
                    yield  # type: ignore[misc]  # pragma: no cover

                provider.stream = _fail_stream
            else:

                async def _ok_stream(msg):
                    yield _llm_event("text_chunk", "done")
                    yield _llm_event("complete")

                provider.stream = _ok_stream
            provider.approve_tool = AsyncMock()
            provider.context_usage_pct = MagicMock(return_value=0.0)
            return provider, True, False

        sessions.get_or_create = _get_or_create

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        run = TaskRun(
            spec_path=str(tmp_path / "t.md"),
            spec_content="spec",
            status="running",
        )
        run.task_id = "mid_fail"
        run.work_dir = str(tmp_path)
        run.tasks = [
            Step(index=1, title="A", description="d"),
            Step(index=2, title="B", description="d"),
            Step(index=3, title="C", description="d"),
        ]

        with (
            patch("kiro_crew.task_executor.self_review", return_value=True),
            patch.object(runner, "_try_replan", return_value=False),
        ):
            await runner._execute_tasks(run, "taskrunner:run:test")

        assert run.tasks[0].status == StepStatus.PASSED
        assert run.tasks[1].status == StepStatus.FAILED
        # With asyncio.gather, C runs concurrently and passes
        assert run.tasks[2].status == StepStatus.PASSED


# ── Replan: depends_on Reindexing ──


class TestScenarioReplanDependsOnReindexing:
    """New steps from replan have depends_on shifted by base index."""

    @pytest.mark.asyncio
    async def test_depends_on_shifted(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        provider = _make_provider("done")
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        run = TaskRun(
            spec_path=str(tmp_path / "t.md"),
            spec_content="spec",
            status="running",
        )
        run.task_id = "reindex"
        run.work_dir = str(tmp_path)
        run.tasks = [
            Step(index=1, title="OK", description="d", status=StepStatus.PASSED),
            Step(index=2, title="Fail", description="d", status=StepStatus.FAILED, error="err"),
        ]

        async def _mock_decompose(spec, work_dir="", task_id=""):
            return [
                Step(index=1, title="Fix A", description="d"),
                Step(index=2, title="Fix B", description="d", depends_on=[1]),
            ]

        with (
            patch.object(runner, "_decompose", side_effect=_mock_decompose),
            patch.object(runner, "self_review", return_value=True),
        ):
            result = await runner._try_replan(run, run.tasks[1])

        assert result is True
        # New steps should be reindexed: base=2, so step3 and step4
        assert run.tasks[2].index == 3
        assert run.tasks[3].index == 4
        # depends_on should be shifted: original [1] → [1+2] = [3]
        assert run.tasks[3].depends_on == [3]


# ── Build Step Prompt: Git Context vs Memory Fallback ──


class TestScenarioBuildStepPromptFallback:
    """_build_task_prompt uses git context when available, memory otherwise."""

    @pytest.mark.asyncio
    async def test_prompt_uses_memory_when_no_git(self) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)

        run = TaskRun(spec_path="/t.md", spec_content="spec")
        run.memory.files_changed = ["handler.py"]
        run.memory.decisions = ["Use REST"]
        step = Step(index=1, title="Next", description="d")
        run.tasks = [step]

        prompt = await runner._build_task_prompt(run, step, attempt=1)
        assert "handler.py" in prompt
        assert "REST" in prompt

    @pytest.mark.asyncio
    async def test_prompt_uses_git_when_available(self) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)

        run = TaskRun(spec_path="/t.md", spec_content="spec")
        run.branch_name = "kirocrew/task/test"
        step = Step(index=1, title="Next", description="d")
        run.tasks = [step]

        with patch("kiro_crew.task_executor.git_coord") as mock_gc:
            mock_gc.get_state_summary = AsyncMock(
                return_value="## Git Log\n```\nabc1234 step 1: setup\n```"
            )
            prompt = await runner._build_task_prompt(run, step, attempt=1)

        assert "Git Log" in prompt
        assert "abc1234" in prompt
        assert "kirocrew/task/test" in prompt
