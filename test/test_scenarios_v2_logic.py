"""Scenario tests part 2 — TaskRunner logic: step ordering, replan, cycle detection.

These test the taskrunner control flow without real LLM calls.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.task_models import MAX_RETRIES
from kiro_crew.taskrunner import (
    MAX_TOTAL_TASKS,
    Step,
    StepStatus,
    TaskRun,
    TaskRunner,
    WorkingMemory,
)


def _make_mock_sessions() -> MagicMock:
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
    sessions.recycle_background = AsyncMock()
    return sessions


def _make_provider(text: str = "done"):
    from kiro_crew.providers.base import LLMEvent

    provider = MagicMock()

    async def _stream(msg: str):
        yield LLMEvent(kind="text_chunk", text=text)
        yield LLMEvent(kind="complete")

    provider.stream = _stream
    provider.approve_tool = AsyncMock()
    provider.reject_tool = AsyncMock()
    provider.context_usage_pct = MagicMock(return_value=0.0)
    return provider


# ═══════════════════════════════════════════════════════════════════════
# Scenario 8: Step ordering — sequential deps never run out of order
# Simulates: "create DB schema → seed data → run migrations → test"
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioStepOrdering:
    @pytest.mark.asyncio
    async def test_sequential_steps_execute_in_order(self, tmp_path: Path) -> None:
        """Steps with chain deps execute strictly in order."""
        (tmp_path / "TASK.md").write_text("# Sequential task")
        sessions = _make_mock_sessions()

        step_json = json.dumps(
            [
                {"title": "Create schema", "description": "d"},
                {"title": "Seed data", "description": "d", "depends_on": [1]},
                {"title": "Run migrations", "description": "d", "depends_on": [2]},
                {"title": "Test", "description": "d", "depends_on": [3]},
            ]
        )

        from kiro_crew.providers.base import LLMEvent

        decompose_provider = MagicMock()

        async def _decompose(msg):
            yield LLMEvent(kind="text_chunk", text=step_json)
            yield LLMEvent(kind="complete")

        decompose_provider.stream = _decompose
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        execution_order: list[int] = []
        step_provider = MagicMock()

        async def _step(msg):
            # Extract step index from the "## Current Step (N/M)" header
            import re

            m = re.search(r"## Current Task \((\d+)/\d+\)", msg)
            if m and "Acceptance Check" not in msg.split("## Current Task")[1][:50]:
                execution_order.append(int(m.group(1)))
            yield LLMEvent(kind="text_chunk", text="done")
            yield LLMEvent(kind="complete")

        step_provider.stream = _step
        step_provider.approve_tool = AsyncMock()
        step_provider.context_usage_pct = MagicMock(return_value=0.0)

        async def _get(key, agent=None):
            if "decompose" in key:
                return decompose_provider, True, False
            return step_provider, True, False

        sessions.get_or_create = _get

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        with patch.object(runner, "self_review", return_value=True):
            result = await runner.run(tmp_path / "TASK.md")

        # Verify strict ordering
        assert execution_order == [1, 2, 3, 4]
        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_diamond_deps_execute_correctly(self, tmp_path: Path) -> None:
        """Diamond: A → (B, C parallel) → D. B and C run before D."""
        (tmp_path / "TASK.md").write_text("# Diamond task")
        sessions = _make_mock_sessions()

        step_json = json.dumps(
            [
                {"title": "A", "description": "d"},
                {"title": "B", "description": "d", "depends_on": [1]},
                {"title": "C", "description": "d", "depends_on": [1]},
                {"title": "D", "description": "d", "depends_on": [2, 3]},
            ]
        )

        from kiro_crew.providers.base import LLMEvent

        decompose_provider = MagicMock()

        async def _decompose(msg):
            yield LLMEvent(kind="text_chunk", text=step_json)
            yield LLMEvent(kind="complete")

        decompose_provider.stream = _decompose
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        execution_order: list[str] = []
        step_provider = MagicMock()

        async def _step(msg):
            for t in ("A", "B", "C", "D"):
                if f"**{t}**" in msg:
                    execution_order.append(t)
                    break
            yield LLMEvent(kind="text_chunk", text="done")
            yield LLMEvent(kind="complete")

        step_provider.stream = _step
        step_provider.approve_tool = AsyncMock()
        step_provider.context_usage_pct = MagicMock(return_value=0.0)

        async def _get(key, agent=None):
            if "decompose" in key:
                return decompose_provider, True, False
            return step_provider, True, False

        sessions.get_or_create = _get

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        with patch.object(runner, "self_review", return_value=True):
            result = await runner.run(tmp_path / "TASK.md")

        assert result.status == "completed"
        # A must be first, D must be last
        assert execution_order[0] == "A"
        assert execution_order[-1] == "D"
        # B and C must both come before D
        d_idx = execution_order.index("D")
        assert "B" in execution_order[:d_idx]
        assert "C" in execution_order[:d_idx]

    @pytest.fixture(autouse=True)
    def _create_spec(self, tmp_path: Path) -> None:
        (tmp_path / "TASK.md").write_text("# Test task")


# ═══════════════════════════════════════════════════════════════════════
# Scenario 9: Replan after failure — new steps get correct indices
# Simulates: "deploy service" — step 2 fails, replan adds 2 new steps
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioReplanIndexing:
    def test_replan_indices_dont_collide(self) -> None:
        """After replan, new step indices don't overlap with existing."""
        run = TaskRun(spec_path="/s.md", spec_content="s", status="running")
        run.tasks = [
            Step(index=1, title="A", description="d", status=StepStatus.PASSED),
            Step(index=2, title="B", description="d", status=StepStatus.FAILED, error="boom"),
        ]

        # Simulate what _try_replan does with new steps
        new_steps = [
            Step(index=1, title="B-fix", description="d", depends_on=[]),
            Step(index=2, title="C-new", description="d", depends_on=[1]),
        ]
        base_idx = len(run.tasks)  # 2
        for i, step in enumerate(new_steps, 1):
            step.depends_on = [d + base_idx for d in step.depends_on]
            step.index = base_idx + i
        run.tasks.extend(new_steps)

        # Verify no index collision
        indices = [s.index for s in run.tasks]
        assert len(indices) == len(set(indices)), f"Duplicate indices: {indices}"
        assert indices == [1, 2, 3, 4]

        # Verify deps were shifted
        assert run.tasks[3].depends_on == [3]  # was [1], shifted by 2


# ═══════════════════════════════════════════════════════════════════════
# Scenario 10: MAX_TOTAL_STEPS prevents infinite replan loops
# Simulates: pathological task that keeps failing and replanning
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioMaxStepsGuard:
    @pytest.mark.asyncio
    async def test_step_limit_prevents_runaway(self) -> None:
        """Task with 50 steps can't replan (would exceed limit)."""
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)

        run = TaskRun(spec_path="/s.md", spec_content="s", status="running")
        run.tasks = [
            Step(index=i, title=f"Step {i}", description="d", status=StepStatus.PASSED)
            for i in range(1, MAX_TOTAL_TASKS + 1)
        ]
        failed = Step(index=MAX_TOTAL_TASKS, title="Last", description="d", error="fail")

        result = await runner._try_replan(run, failed)
        assert result is False
        assert "Task limit" in run.error

    @pytest.mark.asyncio
    async def test_step_limit_at_boundary(self) -> None:
        """49 steps → replan allowed. 50 steps → blocked."""
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)

        # 49 steps — should be allowed (but will fail for other reasons in mock)
        run49 = TaskRun(spec_path="/s.md", spec_content="s", status="running")
        run49.tasks = [
            Step(index=i, title=f"S{i}", description="d", status=StepStatus.PASSED)
            for i in range(1, 50)
        ]
        failed49 = Step(index=49, title="X", description="d", error="fail")
        # This will try to decompose (and fail because mock), but won't hit step limit
        await runner._try_replan(run49, failed49)
        assert "Task limit" not in (run49.error or "")


# ═══════════════════════════════════════════════════════════════════════
# Scenario 11: Cycle detection — same error repeating
# Simulates: "fix linting" — same lint error keeps coming back
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioCycleDetection:
    @pytest.mark.asyncio
    async def test_different_errors_no_cycle(self, tmp_path: Path) -> None:
        """Different errors on each retry → no cycle detection triggered."""
        sessions = _make_mock_sessions()

        call_count = 0
        provider = MagicMock()

        async def _varying_errors(msg):
            nonlocal call_count
            call_count += 1
            raise RuntimeError(f"error variant {call_count}")
            yield  # type: ignore[misc]

        provider.stream = _varying_errors
        provider.approve_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Lint fix", description="d")
        run.tasks = [step]

        success = await runner._execute_single_task(run, step)
        assert not success
        # Should have used all 3 retries (no early cycle exit)
        assert step.attempts == MAX_RETRIES
        assert "Loop detected" not in step.error


# ═══════════════════════════════════════════════════════════════════════
# Scenario 12: Review with git diff vs without
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioReviewModes:
    @pytest.mark.asyncio
    async def test_review_uses_diff_when_branch_exists(self, tmp_path: Path) -> None:
        """When run.branch_name is set, review prompt includes actual diff."""
        sessions = _make_mock_sessions()
        provider = MagicMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s")
        run.branch_name = "kirocrew/task/test"
        step = Step(index=1, title="Fix bug", description="Fix the auth bug")
        run.tasks = [step]

        fake_diff = "--- a/auth.py\n+++ b/auth.py\n-old\n+new"

        with patch(
            "kiro_crew.task_executor.stream_and_collect_json", return_value={"ok": True}
        ) as mock_json, patch(
            "kiro_crew.task_executor.git_coord.get_step_diff", return_value=fake_diff
        ):
            result = await runner.self_review(run, step)

        assert result is True
        # Verify the prompt sent to LLM contained the diff
        call_args = mock_json.call_args
        prompt = call_args[0][1]  # second positional arg
        assert "```diff" in prompt
        assert "auth.py" in prompt

    @pytest.mark.asyncio
    async def test_review_fallback_without_branch(self, tmp_path: Path) -> None:
        """When no branch_name, review uses old-style self-review prompt."""
        sessions = _make_mock_sessions()
        provider = MagicMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s")
        run.branch_name = ""  # no git
        step = Step(index=1, title="Fix", description="d")
        run.tasks = [step]

        with patch(
            "kiro_crew.task_executor.stream_and_collect_json", return_value={"ok": True}
        ) as mock_json:
            result = await runner.self_review(run, step)

        assert result is True
        prompt = mock_json.call_args[0][1]
        assert "review agent" in prompt.lower()
        assert "```diff" not in prompt


# ═══════════════════════════════════════════════════════════════════════
# Scenario 13: _build_task_prompt includes git context
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioStepPromptWithGit:
    @pytest.mark.asyncio
    async def test_prompt_includes_git_log(self, tmp_path: Path) -> None:
        """When branch exists, step prompt includes git log and diff stat."""
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.branch_name = "kirocrew/task/test"
        run.tasks = [
            Step(index=1, title="Done", description="d", status=StepStatus.PASSED),
            Step(index=2, title="Current", description="do the thing"),
        ]

        fake_summary = (
            "## Git Log\n```\nabc1234 step 1: Done\n```\n## Files\n```\nfoo.py | 5 +\n```"
        )

        with patch("kiro_crew.git_coord.get_state_summary", return_value=fake_summary):
            prompt = await runner._build_task_prompt(run, run.tasks[1], attempt=1)

        assert "Git Log" in prompt
        assert "foo.py" in prompt
        assert "do the thing" in prompt

    @pytest.mark.asyncio
    async def test_prompt_without_git(self) -> None:
        """Without branch, prompt uses WorkingMemory fallback."""
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.branch_name = ""
        run.memory.files_changed = ["Created handler.py"]
        step = Step(index=1, title="Next", description="d")
        run.tasks = [step]

        prompt = await runner._build_task_prompt(run, step, attempt=1)
        assert "handler.py" in prompt
        assert "Git Log" not in prompt


# ═══════════════════════════════════════════════════════════════════════
# Scenario 14: Failure in parallel group triggers replan
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioFailureInParallelGroup:
    @pytest.mark.asyncio
    async def test_failure_in_parallel_group_triggers_replan(self, tmp_path: Path) -> None:
        """If step B fails in group [B, C], the group fails (both run via gather)."""
        sessions = _make_mock_sessions()

        from kiro_crew.providers.base import LLMEvent

        step_json = json.dumps(
            [
                {"title": "B", "description": "d"},
                {"title": "C", "description": "d"},
            ]
        )

        decompose_provider = MagicMock()

        async def _decompose(msg):
            yield LLMEvent(kind="text_chunk", text=step_json)
            yield LLMEvent(kind="complete")

        decompose_provider.stream = _decompose
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        executed: list[str] = []
        step_provider = MagicMock()

        async def _step(msg):
            if "**B**" in msg:
                executed.append("B")
                raise RuntimeError("B fails")
            executed.append("C")
            yield LLMEvent(kind="text_chunk", text="done")
            yield LLMEvent(kind="complete")

        step_provider.stream = _step
        step_provider.approve_tool = AsyncMock()
        step_provider.context_usage_pct = MagicMock(return_value=0.0)

        async def _get(key, agent=None):
            if "decompose" in key:
                return decompose_provider, True, False
            return step_provider, True, False

        sessions.get_or_create = _get

        spec = tmp_path / "TASK.md"
        spec.write_text("# Test")

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        with patch.object(runner, "_try_replan", return_value=False):
            result = await runner.run(spec)

        assert result.status == "failed"
        # With asyncio.gather, both B and C run concurrently.
        # B fails (after retries), but C still executes.
        assert "B" in executed
        assert "C" in executed


# ═══════════════════════════════════════════════════════════════════════
# Scenario 15: WorkingMemory still works as fallback
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioWorkingMemoryFallback:
    def test_update_from_result_still_works(self) -> None:
        """WorkingMemory.update_from_result still parses file paths."""
        mem = WorkingMemory()
        mem.update_from_result(
            "Created src/handler.py\n"
            "Modified src/routes.py\n"
            "Some random text\n"
            "Deleted old_file.py"
        )
        assert len(mem.files_changed) == 3
        assert any("handler.py" in f for f in mem.files_changed)
        assert any("routes.py" in f for f in mem.files_changed)
        assert any("old_file.py" in f for f in mem.files_changed)

    def test_summary_truncation(self) -> None:
        """Memory caps at 20 files, 10 decisions, 5 blockers."""
        mem = WorkingMemory(
            files_changed=[f"f{i}" for i in range(30)],
            decisions=[f"d{i}" for i in range(15)],
            blockers=[f"b{i}" for i in range(10)],
        )
        text = mem.summary()
        # Last 20 files
        assert "f10" in text
        assert "f9" not in text
        # Last 10 decisions
        assert "d5" in text
        assert "d4" not in text
        # Last 5 blockers
        assert "b5" in text
        assert "b4" not in text


# ═══════════════════════════════════════════════════════════════════════
# Scenario 16: Parallel groups use asyncio.gather (not sequential)
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioParallelExecution:
    @pytest.mark.asyncio
    async def test_parallel_group_runs_concurrently(self, tmp_path: Path) -> None:
        """Steps B and C (no deps) must run via asyncio.gather, not sequentially."""
        (tmp_path / "TASK.md").write_text("# Parallel task")
        sessions = _make_mock_sessions()

        step_json = json.dumps(
            [
                {"title": "B", "description": "d"},
                {"title": "C", "description": "d"},
            ]
        )

        from kiro_crew.providers.base import LLMEvent

        decompose_provider = MagicMock()

        async def _decompose(msg):
            yield LLMEvent(kind="text_chunk", text=step_json)
            yield LLMEvent(kind="complete")

        decompose_provider.stream = _decompose
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        # Barrier: both steps must reach this point before either can finish.
        # If execution is sequential, the second step never starts while the
        # first is waiting → deadlock → timeout → test fails.
        barrier = asyncio.Event()
        arrived: list[str] = []

        def _make_step_provider():
            p = MagicMock()

            async def _step(msg):
                for t in ("B", "C"):
                    if f"**{t}**" in msg:
                        arrived.append(t)
                        if len(arrived) >= 2:
                            barrier.set()
                        else:
                            await asyncio.wait_for(barrier.wait(), timeout=2.0)
                        break
                yield LLMEvent(kind="text_chunk", text="done")
                yield LLMEvent(kind="complete")

            p.stream = _step
            p.approve_tool = AsyncMock()
            p.context_usage_pct = MagicMock(return_value=0.0)
            return p

        async def _get(key, agent=None):
            if "decompose" in key:
                return decompose_provider, True, False
            return _make_step_provider(), True, False

        sessions.get_or_create = _get

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        with patch.object(runner, "self_review", return_value=True):
            result = await runner.run(tmp_path / "TASK.md")

        assert result.status == "completed"
        assert set(arrived) == {"B", "C"}
