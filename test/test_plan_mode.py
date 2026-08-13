"""Tests for Plan Mode — plan(), update_plan(), execute_plan(), enhanced prompts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.providers.base import LLMEvent
from kiro_crew.taskrunner import Step, StepStatus, TaskRun, TaskRunner


def _make_mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions._lock = asyncio.Lock()
    sessions._sessions = {}
    sessions.get_or_create = AsyncMock()

    async def _open_task_session(_pk, session_key, *, agent=None, cwd=None, approval_policy=""):
        return await sessions.get_or_create(session_key, agent=agent, cwd=cwd)

    sessions.open_task_session = _open_task_session
    sessions.release_subagent_runtime = AsyncMock()
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.record_failure = AsyncMock()
    sessions.check_context_usage = MagicMock()
    sessions.close_all = AsyncMock()
    return sessions


def _make_mock_provider(text: str = "done") -> MagicMock:
    provider = MagicMock()

    async def _stream(message: str):
        yield LLMEvent(kind="text_chunk", text=text)
        yield LLMEvent(kind="complete")

    provider.stream = _stream
    provider.approve_tool = AsyncMock()
    provider.reject_tool = AsyncMock()
    provider.context_usage_pct = MagicMock(return_value=0.0)
    return provider


def _make_runner(tmp_path: Path, steps_json: list[dict] | None = None) -> TaskRunner:
    """Create a TaskRunner with mocked _decompose returning given steps."""
    sessions = _make_mock_sessions()
    provider = _make_mock_provider()
    sessions.get_or_create.return_value = (provider, True, False)
    runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

    if steps_json is not None:

        async def _mock_decompose(spec, work_dir="", task_id=""):
            return runner._parse_tasks(json.dumps(steps_json))

        runner._decompose = _mock_decompose  # type: ignore[assignment]
    return runner


def _planned_run(
    runner: TaskRunner, task_id: str = "plan_test", tasks: list[Step] | None = None
) -> TaskRun:
    """Insert a planned TaskRun directly into runner._runs."""
    run = TaskRun(
        spec_path="",
        spec_content="test spec",
        original_input="test input",
        source="text",
        status="planned",
        task_id=task_id,
        work_dir=str(runner._work_dir / task_id),
    )
    run.tasks = tasks or [
        Step(index=1, title="Step A", description="Do A"),
        Step(index=2, title="Step B", description="Do B", depends_on=[1]),
    ]
    TaskRunner._normalize_cross_group_deps(run.tasks)
    runner._runs[task_id] = run
    return run


# ── TestPlan ──


class TestPlan:
    @pytest.mark.asyncio
    async def test_plan_from_text(self, tmp_path: Path) -> None:
        runner = _make_runner(
            tmp_path, [{"title": "S1", "description": "D1"}, {"title": "S2", "description": "D2"}]
        )
        run = await runner.plan("add health endpoint", source="text")
        assert run.status == "planned"
        assert len(run.tasks) == 2  # 2 decomposed steps
        assert run.original_input == "add health endpoint"
        assert run.spec_content == ""
        assert run.source == "text"
        assert all(s.status == StepStatus.PENDING for s in run.tasks)

    @pytest.mark.asyncio
    async def test_plan_from_spec(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path, [{"title": "S1"}])
        run = await runner.plan("# Task\n## Goal\nAdd endpoint", source="spec")
        assert run.spec_content == "# Task\n## Goal\nAdd endpoint"
        assert run.original_input == run.spec_content

    @pytest.mark.asyncio
    async def test_plan_from_file(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# File Spec\nDo things")
        runner = _make_runner(tmp_path, [{"title": "S1"}])
        run = await runner.plan("", source="file", spec_path=str(spec_file))
        assert run.spec_content == "# File Spec\nDo things"

    @pytest.mark.asyncio
    async def test_plan_empty_input_raises(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path, [{"title": "S1"}])
        with pytest.raises(ValueError, match="empty"):
            await runner.plan("", source="text")

    @pytest.mark.asyncio
    async def test_plan_file_not_found_raises(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path, [{"title": "S1"}])
        with pytest.raises(FileNotFoundError):
            await runner.plan("", source="file", spec_path="/nonexistent/file.md")

    @pytest.mark.asyncio
    async def test_plan_decompose_failure_raises(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path, [])  # empty steps = decompose failure
        with pytest.raises(ValueError, match="Could not generate a plan"):
            await runner.plan("do something", source="text")


# ── TestAcceptanceCriteria ──


class TestAcceptanceCriteriaIgnored:
    """acceptance_criteria in LLM output is silently ignored after removal."""

    def test_parse_tasks_ignores_criteria(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        text = json.dumps(
            {
                "tasks": [{"title": "A", "description": "do A"}],
                "acceptance_criteria": ["File A exists", "Tests pass"],
            }
        )
        steps = runner._parse_tasks(text)
        assert len(steps) == 1


# ── TestUpdatePlan ──


class TestUpdatePlan:
    @pytest.mark.asyncio
    async def test_update_replaces_steps(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        _planned_run(
            runner,
            tasks=[
                Step(index=1, title="Old1", description="O1"),
                Step(index=2, title="Old2", description="O2"),
                Step(index=3, title="Old3", description="O3"),
            ],
        )
        run = await runner.update_plan(
            "plan_test",
            [
                {"title": "New1", "description": "N1"},
                {"title": "New2", "description": "N2"},
            ],
        )
        assert len(run.tasks) == 2  # 2 new steps
        assert run.tasks[0].index == 1
        assert run.tasks[0].title == "New1"
        assert run.tasks[1].index == 2

    @pytest.mark.asyncio
    async def test_update_validates_deps(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        _planned_run(runner)
        run = await runner.update_plan(
            "plan_test",
            [
                {"title": "A", "depends_on": []},
                {"title": "B", "depends_on": [1, 5]},  # 5 is invalid (future)
            ],
        )
        assert run.tasks[1].depends_on == [1]

    @pytest.mark.asyncio
    async def test_update_rejects_non_planned(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        run = _planned_run(runner)
        run.status = "running"
        with pytest.raises(ValueError, match="Cannot update plan while"):
            await runner.update_plan("plan_test", [{"title": "X"}])

    @pytest.mark.asyncio
    async def test_update_rejects_not_found(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            await runner.update_plan("nonexistent", [{"title": "X"}])

    @pytest.mark.asyncio
    async def test_update_cancelled_project_resets_to_planned(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        run = _planned_run(runner)
        run.status = "cancelled"
        run.error = "old error"
        run.replan_count = 3
        await runner.update_plan("plan_test", [{"title": "New Step"}])
        assert run.status == "planned"
        assert run.error == ""
        assert run.replan_count == 0
        assert len(run.tasks) == 1
        assert run.tasks[0].title == "New Step"

    @pytest.mark.asyncio
    async def test_update_failed_project_resets_to_planned(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        run = _planned_run(runner)
        run.status = "failed"
        run.error = "old error"
        run.replan_count = 2
        await runner.update_plan("plan_test", [{"title": "Retry Step"}])
        assert run.status == "planned"
        assert run.error == ""
        assert run.replan_count == 0
        assert len(run.tasks) == 1
        assert run.tasks[0].title == "Retry Step"

    @pytest.mark.asyncio
    async def test_update_rejects_empty_steps(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        _planned_run(runner)
        with pytest.raises(ValueError, match="No valid tasks"):
            await runner.update_plan("plan_test", [{"no_title": True}])


# ── TestExecutePlan ──


class TestExecutePlan:
    @pytest.mark.asyncio
    async def test_execute_starts_running(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        _planned_run(runner)
        with patch("kiro_crew.taskrunner.git_coord"):
            task_id = await runner.execute_plan("plan_test")
        assert task_id == "plan_test"
        # Give the async task a tick to start
        await asyncio.sleep(0.05)
        run = runner._runs["plan_test"]
        assert run.status in ("running", "completed", "failed")

    @pytest.mark.asyncio
    async def test_execute_rejects_non_planned(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        run = _planned_run(runner)
        run.status = "completed"
        with pytest.raises(ValueError, match="not in a startable state"):
            await runner.execute_plan("plan_test")

    @pytest.mark.asyncio
    async def test_execute_cancelled_project_resets_pending(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        run = _planned_run(runner)
        run.status = "cancelled"
        run.tasks[0].status = StepStatus.PASSED
        run.tasks[1].status = StepStatus.FAILED
        run.tasks[1].error = "old error"
        run.tasks[1].result = "old result"
        run.error = "Shutdown signal received"
        with patch("kiro_crew.taskrunner.git_coord"):
            await runner.execute_plan("plan_test")
        # Reset happens synchronously before async execution starts
        assert run.tasks[0].status == StepStatus.PASSED  # preserved
        assert run.tasks[1].status == StepStatus.PENDING
        assert run.tasks[1].error == ""
        assert run.tasks[1].result == ""
        assert run.error == ""

    @pytest.mark.asyncio
    async def test_execute_failed_project_resets_pending(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        run = _planned_run(runner)
        run.status = "failed"
        run.tasks[0].status = StepStatus.PASSED
        run.tasks[1].status = StepStatus.FAILED
        run.tasks[1].error = "previous failure"
        run.tasks[1].result = "partial output"
        run.error = "Task 2 failed"
        with patch("kiro_crew.taskrunner.git_coord"):
            await runner.execute_plan("plan_test")
        # Reset happens synchronously before async execution starts
        assert run.tasks[0].status == StepStatus.PASSED  # preserved
        assert run.tasks[1].status == StepStatus.PENDING
        assert run.tasks[1].error == ""
        assert run.tasks[1].result == ""
        assert run.error == ""

    @pytest.mark.asyncio
    async def test_execute_fresh_resets_all_tasks(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        run = _planned_run(runner)
        run.status = "cancelled"
        run.tasks[0].status = StepStatus.PASSED
        run.tasks[0].result = "old output"
        run.tasks[1].status = StepStatus.FAILED
        run.tasks[1].error = "old error"
        with patch("kiro_crew.taskrunner.git_coord"):
            await runner.execute_plan("plan_test", fresh=True)
        # fresh=True resets ALL tasks including PASSED
        assert run.tasks[0].status == StepStatus.PENDING
        assert run.tasks[0].result == ""
        assert run.tasks[1].status == StepStatus.PENDING
        assert run.tasks[1].error == ""

    @pytest.mark.asyncio
    async def test_execute_runs_all_steps(self, tmp_path: Path) -> None:
        sessions = _make_mock_sessions()
        provider = _make_mock_provider("step done")
        sessions.get_or_create.return_value = (provider, True, False)
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        _planned_run(
            runner,
            tasks=[
                Step(index=1, title="S1", description="D1"),
                Step(index=2, title="S2", description="D2", depends_on=[1]),
            ],
        )
        with patch("kiro_crew.taskrunner.git_coord"):
            await runner.execute_plan("plan_test")
            # Wait for completion
            task = runner._tasks.get("plan_test")
            if task:
                await asyncio.wait_for(task, timeout=10)
        run = runner._runs["plan_test"]
        assert run.status == "completed"
        assert all(s.status == StepStatus.PASSED for s in run.tasks)

    @pytest.mark.asyncio
    async def test_execute_retries_on_step_failure(self, tmp_path: Path) -> None:
        sessions = _make_mock_sessions()
        call_count = 0

        async def _stream_with_failure(message: str):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient error")
            yield LLMEvent(kind="text_chunk", text="fixed")
            yield LLMEvent(kind="complete")

        provider = MagicMock()
        provider.stream = _stream_with_failure
        provider.approve_tool = AsyncMock()
        provider.reject_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create.return_value = (provider, True, False)
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        _planned_run(
            runner,
            tasks=[
                Step(index=1, title="Flaky", description="May fail"),
                Step(index=2, title="S2", description="D2", depends_on=[1]),
            ],
        )
        with patch("kiro_crew.taskrunner.git_coord"):
            await runner.execute_plan("plan_test")
            task = runner._tasks.get("plan_test")
            if task:
                await asyncio.wait_for(task, timeout=10)
        run = runner._runs["plan_test"]
        assert run.tasks[0].attempts >= 2
        assert run.tasks[0].status == StepStatus.PASSED
        assert run.status == "completed"

    @pytest.mark.asyncio
    async def test_concurrent_guard_preserves_state_on_rejection(self, tmp_path: Path) -> None:
        """If MAX_CONCURRENT_TASKS is hit, state must NOT be mutated."""
        from kiro_crew.taskrunner import _MAX_CONCURRENT_TASKS

        runner = _make_runner(tmp_path)
        run = _planned_run(runner)
        run.status = "failed"
        run.tasks[0].status = StepStatus.PASSED
        run.tasks[0].result = "important output"
        run.tasks[1].status = StepStatus.FAILED
        run.error = "Task 2 failed"

        # Fill up concurrent slots with fake running tasks
        for i in range(_MAX_CONCURRENT_TASKS):
            mock_task = MagicMock()
            mock_task.done.return_value = False
            runner._tasks[f"fake_{i}"] = mock_task  # type: ignore[assignment]

        with pytest.raises(ValueError, match="Too many concurrent tasks"):
            await runner.execute_plan("plan_test", fresh=True)

        # State must be unchanged — guard fired before mutation
        assert run.status == "failed"
        assert run.tasks[0].status == StepStatus.PASSED
        assert run.tasks[0].result == "important output"
        assert run.tasks[1].status == StepStatus.FAILED
        assert run.error == "Task 2 failed"

        # Cleanup fake tasks
        for i in range(_MAX_CONCURRENT_TASKS):
            runner._tasks.pop(f"fake_{i}", None)


# ── TestPlanToChat ──


class TestPlanToChat:
    def test_includes_original_input(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        _planned_run(runner)
        output = runner.plan_to_chat_context("plan_test")
        assert "## Original Input" in output
        assert "test input" in output

    def test_includes_grouped_steps(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        _planned_run(
            runner,
            tasks=[
                Step(index=1, title="A", description="DA"),
                Step(index=2, title="B", description="DB"),
                Step(index=3, title="C", description="DC", depends_on=[1, 2]),
            ],
        )
        output = runner.plan_to_chat_context("plan_test")
        assert "Group 1 (parallel)" in output
        assert "Group 2" in output

    def test_includes_json_block(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        _planned_run(runner)
        output = runner.plan_to_chat_context("plan_test")
        assert "```json" in output
        # Extract and parse JSON
        start = output.index("```json") + 7
        end = output.index("```", start)
        parsed = json.loads(output[start:end])
        assert isinstance(parsed, list)
        assert len(parsed) == 2  # 2 original steps

    @pytest.mark.asyncio
    async def test_round_trip_json(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        _planned_run(runner)
        output = runner.plan_to_chat_context("plan_test")
        start = output.index("```json") + 7
        end = output.index("```", start)
        parsed = json.loads(output[start:end])
        run = await runner.update_plan("plan_test", parsed)
        assert len(run.tasks) == 2  # 2 from JSON
        assert run.tasks[0].title == "Step A"

    # ── TestEnhancedStepPrompt ──

    def test_includes_plan_task_id_marker(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        _planned_run(runner)
        output = runner.plan_to_chat_context("plan_test")
        assert "<!-- plan_task_id:plan_test -->" in output

    def test_includes_agent_instruction(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        _planned_run(runner)
        output = runner.plan_to_chat_context("plan_test")
        assert "```json code block" in output


class TestPlanCleanup:
    @pytest.mark.asyncio
    async def test_plan_cleans_up_on_decompose_failure(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path, [])  # empty = decompose failure
        with pytest.raises(ValueError):
            await runner.plan("do something", source="text")
        # Verify no orphan run left in _runs
        assert len(runner._runs) == 0


class TestStatusGroups:
    def test_status_includes_groups(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        _planned_run(
            runner,
            tasks=[
                Step(index=1, title="A", description="DA"),
                Step(index=2, title="B", description="DB"),
                Step(index=3, title="C", description="DC", depends_on=[1, 2]),
            ],
        )
        s = runner.status()
        run_data = s["runs"][0]
        assert "groups" in run_data
        assert run_data["groups"] == [[1, 2], [3]]

    def test_status_includes_new_fields(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        _planned_run(runner)
        s = runner.status()
        run_data = s["runs"][0]
        assert run_data["original_input"] == "test input"
        assert run_data["source"] == "text"
        assert run_data["task_details"][0]["depends_on"] == []
        assert run_data["task_details"][1]["depends_on"] == [1]


class TestEnhancedStepPrompt:
    @pytest.mark.asyncio
    async def test_includes_full_plan(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        run = TaskRun(spec_path="t.md", spec_content="spec")
        run.tasks = [Step(index=i, title=f"Step {i}", description=f"Desc {i}") for i in range(1, 6)]
        run.tasks[0].status = StepStatus.PASSED
        run.tasks[1].status = StepStatus.PASSED
        prompt = await runner._build_task_prompt(run, run.tasks[2], attempt=1)
        for i in range(1, 6):
            assert f"Step {i}" in prompt
            assert f"Desc {i}" in prompt

    @pytest.mark.asyncio
    async def test_includes_dependency_info(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        run = TaskRun(spec_path="t.md", spec_content="spec")
        run.tasks = [
            Step(index=1, title="A", description="DA"),
            Step(index=2, title="B", description="DB"),
            Step(index=3, title="C", description="DC", depends_on=[1, 2]),
        ]
        prompt = await runner._build_task_prompt(run, run.tasks[2], attempt=1)
        assert "depends on: 1, 2" in prompt

    @pytest.mark.asyncio
    async def test_includes_you_are_here_marker(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        run = TaskRun(spec_path="t.md", spec_content="spec")
        run.tasks = [
            Step(index=1, title="A", description="DA", status=StepStatus.PASSED),
            Step(index=2, title="B", description="DB"),
        ]
        prompt = await runner._build_task_prompt(run, run.tasks[1], attempt=1)
        assert "← YOU ARE HERE" in prompt

    @pytest.mark.asyncio
    async def test_includes_parallel_grouping(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        run = TaskRun(spec_path="t.md", spec_content="spec")
        run.tasks = [
            Step(index=1, title="A", description="DA"),
            Step(index=2, title="B", description="DB"),
            Step(index=3, title="C", description="DC", depends_on=[1, 2]),
        ]
        prompt = await runner._build_task_prompt(run, run.tasks[0], attempt=1)
        assert "Group 1 (parallel)" in prompt

    @pytest.mark.asyncio
    async def test_includes_original_context(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        run = TaskRun(spec_path="t.md", spec_content="spec", original_input="my original request")
        run.tasks = [Step(index=1, title="A", description="DA")]
        prompt = await runner._build_task_prompt(run, run.tasks[0], attempt=1)
        assert "## Original Context" in prompt
        assert "my original request" in prompt

    @pytest.mark.asyncio
    async def test_includes_strict_instruction(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        run = TaskRun(spec_path="t.md", spec_content="spec")
        run.tasks = [Step(index=1, title="A", description="DA")]
        prompt = await runner._build_task_prompt(run, run.tasks[0], attempt=1)
        assert "follow strictly" in prompt or "ONLY what" in prompt.lower()

    @pytest.mark.asyncio
    async def test_spec_content_used_when_no_original_input(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        run = TaskRun(spec_path="t.md", spec_content="my spec content", original_input="")
        run.tasks = [Step(index=1, title="A", description="DA")]
        prompt = await runner._build_task_prompt(run, run.tasks[0], attempt=1)
        assert "## Original Spec" in prompt
        assert "my spec content" in prompt


# ── TestPlanPersistence ──


class TestPlanPersistence:
    def test_planned_run_persisted(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        run = _planned_run(runner)
        run.tasks[0].description = "Custom desc"
        run.tasks[1].requires_approval = True
        runner._persist_runs()

        runner2 = _make_runner(tmp_path)
        runner2._load_runs()
        restored = runner2._runs.get("plan_test")
        assert restored is not None
        assert restored.status == "planned"
        assert restored.original_input == "test input"
        assert restored.source == "text"

    def test_task_details_preserved(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        run = _planned_run(runner)
        run.tasks[0].description = "Custom desc"
        run.tasks[1].depends_on = [1]
        run.tasks[1].requires_approval = True
        runner._persist_runs()

        runner2 = _make_runner(tmp_path)
        runner2._load_runs()
        restored = runner2._runs["plan_test"]
        assert restored.tasks[0].description == "Custom desc"
        assert restored.tasks[1].depends_on == [1]
        assert restored.tasks[1].requires_approval is True


class TestPlanRecoveryAfterTabSwitch:
    """Tests that planned/in_progress runs survive persistence and are discoverable
    via status() — the backend contract the frontend recovery polling relies on."""

    def test_planned_run_discoverable_via_status(self, tmp_path: Path) -> None:
        """After persist+reload, status() still returns the planned run."""
        runner = _make_runner(tmp_path)
        _planned_run(runner)
        runner._persist_runs()

        runner2 = _make_runner(tmp_path)
        runner2._load_runs()
        s = runner2.status()
        planned = [r for r in s["runs"] if r["status"] == "planned"]
        assert len(planned) == 1
        assert planned[0]["task_id"] == "plan_test"

    def test_running_run_persisted_and_recovered_as_paused(self, tmp_path: Path) -> None:
        """Running runs ARE persisted for crash recovery (recovered as paused)."""
        runner = _make_runner(tmp_path)
        run = _planned_run(runner)
        run.status = "running"
        runner._persist_runs()

        runner2 = _make_runner(tmp_path)
        runner2._load_runs()
        assert "plan_test" in runner2._runs
        assert runner2._runs["plan_test"].status == "paused"

    @pytest.mark.asyncio
    async def test_update_plan_preserves_steps_for_applied(self, tmp_path: Path) -> None:
        """update_plan changes steps; status() returns updated steps (Use as Plan flow)."""
        runner = _make_runner(tmp_path)
        _planned_run(
            runner,
            tasks=[
                Step(index=1, title="Old A", description="OA"),
                Step(index=2, title="Old B", description="OB"),
            ],
        )
        await runner.update_plan(
            "plan_test",
            [
                {"title": "New X", "description": "NX"},
                {"title": "New Y", "description": "NY"},
                {"title": "New Z", "description": "NZ"},
            ],
        )
        s = runner.status()
        run_data = next(r for r in s["runs"] if r["task_id"] == "plan_test")
        assert len(run_data["task_details"]) == 3  # 3 new steps
        assert run_data["task_details"][0]["title"] == "New X"
        assert run_data["task_details"][2]["title"] == "New Z"

    @pytest.mark.asyncio
    async def test_status_finds_correct_run_by_task_id(self, tmp_path: Path) -> None:
        """With multiple planned runs, status() returns all so frontend can filter by task_id."""
        runner = _make_runner(tmp_path)
        _planned_run(runner, task_id="plan_old")
        _planned_run(runner, task_id="plan_new")
        await runner.update_plan("plan_new", [{"title": "Updated", "description": "U"}])
        s = runner.status()
        ids = {r["task_id"] for r in s["runs"] if r["status"] == "planned"}
        assert ids == {"plan_old", "plan_new"}
        new_run = next(r for r in s["runs"] if r["task_id"] == "plan_new")
        assert new_run["task_details"][0]["title"] == "Updated"


class TestCrossGroupDependencyNormalization:
    """Steps depending on partial members of a prior group must be expanded
    to depend on ALL members of that group."""

    def test_partial_dep_expanded_to_full_group(self, tmp_path: Path) -> None:
        """Task  depends on step 1 only, but 1 and 2 are in the same group.
        After normalization, step 3 must depend on both 1 and 2."""
        runner = _make_runner(tmp_path)
        steps = runner._parse_tasks(
            json.dumps(
                [
                    {"title": "A", "description": "a", "depends_on": []},
                    {"title": "B", "description": "b", "depends_on": []},
                    {"title": "C", "description": "c", "depends_on": [1]},
                ]
            )
        )
        assert steps[2].depends_on == [1, 2]

    def test_already_complete_deps_unchanged(self, tmp_path: Path) -> None:
        """If step already depends on all group members, no change."""
        runner = _make_runner(tmp_path)
        steps = runner._parse_tasks(
            json.dumps(
                [
                    {"title": "A", "depends_on": []},
                    {"title": "B", "depends_on": []},
                    {"title": "C", "depends_on": [1, 2]},
                ]
            )
        )
        assert steps[2].depends_on == [1, 2]

    def test_sequential_chain_unchanged(self, tmp_path: Path) -> None:
        """Purely sequential steps (each depends on previous) stay unchanged."""
        runner = _make_runner(tmp_path)
        steps = runner._parse_tasks(
            json.dumps(
                [
                    {"title": "A", "depends_on": []},
                    {"title": "B", "depends_on": [1]},
                    {"title": "C", "depends_on": [2]},
                ]
            )
        )
        assert steps[0].depends_on == []
        assert steps[1].depends_on == [1]
        assert steps[2].depends_on == [2]

    def test_multi_group_partial_deps(self, tmp_path: Path) -> None:
        """Step depends on one member from each of two prior groups."""
        runner = _make_runner(tmp_path)
        steps = runner._parse_tasks(
            json.dumps(
                [
                    {"title": "A", "depends_on": []},
                    {"title": "B", "depends_on": []},
                    {"title": "C", "depends_on": [1, 2]},
                    {"title": "D", "depends_on": [1, 2]},
                    {"title": "E", "depends_on": [3]},  # partial dep on group {3,4}
                ]
            )
        )
        assert steps[4].depends_on == [3, 4]

    @pytest.mark.asyncio
    async def test_update_plan_preserves_user_deps(self, tmp_path: Path) -> None:
        """update_plan respects user's explicit dependency edits (no normalization)."""
        runner = _make_runner(tmp_path)
        _planned_run(runner)
        await runner.update_plan(
            "plan_test",
            [
                {"title": "X", "depends_on": []},
                {"title": "Y", "depends_on": []},
                {"title": "Z", "depends_on": [1]},  # partial — should stay [1], not expand to [1,2]
            ],
        )
        run = runner._runs["plan_test"]
        assert run.tasks[2].depends_on == [1]

    def test_empty_steps_no_crash(self, tmp_path: Path) -> None:
        runner = _make_runner(tmp_path)
        result = runner._normalize_cross_group_deps([])
        assert result == []
