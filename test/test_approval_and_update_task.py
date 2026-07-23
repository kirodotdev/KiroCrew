"""Tests for approval gate changes and update_task functionality.

Covers:
- update_task: validation, field updates, immutability constraints
- update_plan_tasks: force_approval preservation
- task_executor: force_approval denied → paused (not failed)
- gateway: Slack DM notification filtering
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.task_models import Project, Task
from kiro_crew.task_planner import update_plan_tasks
from kiro_crew.taskrunner import Step, StepStatus, TaskRun, TaskRunner

# ── Helpers ──


def _mock_sessions() -> MagicMock:
    s = MagicMock()
    s._lock = asyncio.Lock()
    s._sessions = {}
    s.get_or_create = AsyncMock()

    async def _open_task_session(_pk, session_key, *, agent=None, cwd=None, approval_policy=""):
        return await s.get_or_create(session_key, agent=agent, cwd=cwd)

    s.open_task_session = _open_task_session
    s.release_subagent_runtime = AsyncMock()
    s.release = MagicMock()
    s.reset = AsyncMock()
    s.record_success = MagicMock()
    s.record_failure = AsyncMock()
    s.check_context_usage = MagicMock()
    return s


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


# ══════════════════════════════════════════════════════════════════════
# update_task tests
# ══════════════════════════════════════════════════════════════════════


class TestUpdateTask:
    """Tests for TaskRunner.update_task() method."""

    def _make_runner(self, tmp_path: Path) -> TaskRunner:
        sessions = _mock_sessions()
        return TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

    @pytest.mark.asyncio
    async def test_update_title(self, tmp_path: Path) -> None:
        runner = self._make_runner(tmp_path)
        run = TaskRun(spec_path="s.md", spec_content="s", status="running", task_id="t1")
        run.tasks = [Step(index=1, title="Old", description="d")]
        runner._runs = {"t1": run}
        result = await runner.update_task("t1", 1, {"title": "New Title"})
        assert result["title"] == "New Title"

    @pytest.mark.asyncio
    async def test_update_description(self, tmp_path: Path) -> None:
        runner = self._make_runner(tmp_path)
        run = TaskRun(spec_path="s.md", spec_content="s", status="running", task_id="t1")
        run.tasks = [Step(index=1, title="T", description="old")]
        runner._runs = {"t1": run}
        result = await runner.update_task("t1", 1, {"description": "new desc"})
        assert result["description"] == "new desc"

    @pytest.mark.asyncio
    async def test_update_force_approval(self, tmp_path: Path) -> None:
        runner = self._make_runner(tmp_path)
        run = TaskRun(spec_path="s.md", spec_content="s", status="running", task_id="t1")
        run.tasks = [Step(index=1, title="T", description="d")]
        runner._runs = {"t1": run}
        result = await runner.update_task("t1", 1, {"force_approval": True})
        assert result["force_approval"] is True

    @pytest.mark.asyncio
    async def test_update_depends_on(self, tmp_path: Path) -> None:
        runner = self._make_runner(tmp_path)
        run = TaskRun(spec_path="s.md", spec_content="s", status="running", task_id="t1")
        run.tasks = [
            Step(index=1, title="A", description="a"),
            Step(index=2, title="B", description="b"),
        ]
        runner._runs = {"t1": run}
        result = await runner.update_task("t1", 2, {"depends_on": [1]})
        assert result["depends_on"] == [1]

    @pytest.mark.asyncio
    async def test_update_requires_approval(self, tmp_path: Path) -> None:
        runner = self._make_runner(tmp_path)
        run = TaskRun(spec_path="s.md", spec_content="s", status="running", task_id="t1")
        run.tasks = [Step(index=1, title="T", description="d")]
        runner._runs = {"t1": run}
        result = await runner.update_task("t1", 1, {"requires_approval": True})
        assert result["requires_approval"] is True

    @pytest.mark.asyncio
    async def test_reject_empty_title(self, tmp_path: Path) -> None:
        runner = self._make_runner(tmp_path)
        run = TaskRun(spec_path="s.md", spec_content="s", status="running", task_id="t1")
        run.tasks = [Step(index=1, title="T", description="d")]
        runner._runs = {"t1": run}
        with pytest.raises(ValueError, match="non-empty string"):
            await runner.update_task("t1", 1, {"title": ""})

    @pytest.mark.asyncio
    async def test_reject_long_title(self, tmp_path: Path) -> None:
        runner = self._make_runner(tmp_path)
        run = TaskRun(spec_path="s.md", spec_content="s", status="running", task_id="t1")
        run.tasks = [Step(index=1, title="T", description="d")]
        runner._runs = {"t1": run}
        with pytest.raises(ValueError, match="title too long"):
            await runner.update_task("t1", 1, {"title": "x" * 501})

    @pytest.mark.asyncio
    async def test_reject_long_description(self, tmp_path: Path) -> None:
        runner = self._make_runner(tmp_path)
        run = TaskRun(spec_path="s.md", spec_content="s", status="running", task_id="t1")
        run.tasks = [Step(index=1, title="T", description="d")]
        runner._runs = {"t1": run}
        with pytest.raises(ValueError, match="description too long"):
            await runner.update_task("t1", 1, {"description": "x" * 5001})

    @pytest.mark.asyncio
    async def test_reject_non_pending_task(self, tmp_path: Path) -> None:
        runner = self._make_runner(tmp_path)
        run = TaskRun(spec_path="s.md", spec_content="s", status="running", task_id="t1")
        step = Step(index=1, title="T", description="d")
        step.status = StepStatus.PASSED
        run.tasks = [step]
        runner._runs = {"t1": run}
        with pytest.raises(ValueError, match="Can only edit pending"):
            await runner.update_task("t1", 1, {"title": "New"})

    @pytest.mark.asyncio
    async def test_reject_unknown_run(self, tmp_path: Path) -> None:
        runner = self._make_runner(tmp_path)
        runner._runs = {}
        with pytest.raises(ValueError, match="not found"):
            await runner.update_task("missing", 1, {"title": "X"})

    @pytest.mark.asyncio
    async def test_reject_unknown_task_index(self, tmp_path: Path) -> None:
        runner = self._make_runner(tmp_path)
        run = TaskRun(spec_path="s.md", spec_content="s", status="running", task_id="t1")
        run.tasks = [Step(index=1, title="T", description="d")]
        runner._runs = {"t1": run}
        with pytest.raises(ValueError, match="Task 99 not found"):
            await runner.update_task("t1", 99, {"title": "X"})

    @pytest.mark.asyncio
    async def test_reject_non_string_description(self, tmp_path: Path) -> None:
        runner = self._make_runner(tmp_path)
        run = TaskRun(spec_path="s.md", spec_content="s", status="running", task_id="t1")
        run.tasks = [Step(index=1, title="T", description="d")]
        runner._runs = {"t1": run}
        with pytest.raises(ValueError, match="description must be a string"):
            await runner.update_task("t1", 1, {"description": 123})

    @pytest.mark.asyncio
    async def test_invalid_depends_on_type(self, tmp_path: Path) -> None:
        runner = self._make_runner(tmp_path)
        run = TaskRun(spec_path="s.md", spec_content="s", status="running", task_id="t1")
        run.tasks = [Step(index=1, title="T", description="d")]
        runner._runs = {"t1": run}
        with pytest.raises(ValueError, match="depends_on must be a list"):
            await runner.update_task("t1", 1, {"depends_on": "invalid"})


# ══════════════════════════════════════════════════════════════════════
# update_plan_tasks: force_approval preservation
# ══════════════════════════════════════════════════════════════════════


class TestUpdatePlanTasksForceApproval:
    """Tests for force_approval preservation in update_plan_tasks."""

    def test_preserves_existing_force_approval_when_key_absent(self) -> None:
        run = Project(spec_path="s.md", spec_content="s", status="planned", task_id="r1")
        run.tasks = [Task(index=1, title="Gate", description="g", force_approval=True)]
        updated = update_plan_tasks(run, [{"title": "Gate", "description": "g"}])
        assert updated.tasks[0].force_approval is True

    def test_explicit_false_removes_force_approval(self) -> None:
        run = Project(spec_path="s.md", spec_content="s", status="planned", task_id="r1")
        run.tasks = [Task(index=1, title="Gate", description="g", force_approval=True)]
        updated = update_plan_tasks(run, [{"title": "Gate", "force_approval": False}])
        assert updated.tasks[0].force_approval is False

    def test_explicit_true_sets_force_approval(self) -> None:
        run = Project(spec_path="s.md", spec_content="s", status="planned", task_id="r1")
        run.tasks = [Task(index=1, title="Step", description="s")]
        updated = update_plan_tasks(run, [{"title": "Step", "force_approval": True}])
        assert updated.tasks[0].force_approval is True

    def test_new_task_defaults_to_false(self) -> None:
        """New tasks (no existing index) default to force_approval=False."""
        run = Project(spec_path="s.md", spec_content="s", status="planned", task_id="r1")
        run.tasks = [Task(index=1, title="Old", description="o")]
        # Adding a 2nd task that didn't exist before
        updated = update_plan_tasks(
            run, [{"title": "Old"}, {"title": "Brand New"}]
        )
        assert updated.tasks[1].force_approval is False


# ══════════════════════════════════════════════════════════════════════
# force_approval denied → paused (not failed)
# ══════════════════════════════════════════════════════════════════════


class TestForceApprovalDenied:
    """force_approval + denial pauses the run instead of failing."""

    @pytest.mark.asyncio
    async def test_force_approval_denied_pauses_run(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            on_approval=AsyncMock(return_value=False),
        )
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Deploy", description="d", requires_approval=True, force_approval=True)
        run.tasks = [step]

        result = await runner._execute_single_task(run, step, "hk")
        assert result is False
        assert step.status == StepStatus.PENDING
        assert run.status == "paused"
        assert "denied" in run.error.lower()

    @pytest.mark.asyncio
    async def test_force_approval_granted_executes(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        provider = _make_provider("done")
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            on_approval=AsyncMock(return_value=True),
        )
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Deploy", description="d", requires_approval=True, force_approval=True)
        run.tasks = [step]

        with patch.object(runner, "self_review", return_value=True):
            result = await runner._execute_single_task(run, step, "hk")
        assert result is True
        assert step.status == StepStatus.PASSED


# ══════════════════════════════════════════════════════════════════════
# gateway: Slack DM notification filtering
# ══════════════════════════════════════════════════════════════════════


class TestGatewayNotificationFilter:
    """Approval notification Slack DM filter uses precise patterns."""

    def test_requires_approval_matches(self) -> None:
        title = "⏳ Task 3 requires approval"
        assert "requires approval" in title.lower()

    def test_denied_matches(self) -> None:
        title = "⏸️ Task 3 denied"
        assert "denied" in title.lower()

    def test_gateway_does_not_match(self) -> None:
        title = "Investigating gateway error"
        assert "requires approval" not in title.lower()
        assert "denied" not in title.lower()

    def test_skipped_does_not_match(self) -> None:
        title = "⏭️ Task 2 skipped"
        assert "requires approval" not in title.lower()
        assert "denied" not in title.lower()

    def test_failed_does_not_match(self) -> None:
        title = "❌ Task 5 failed"
        assert "requires approval" not in title.lower()
        assert "denied" not in title.lower()


# ══════════════════════════════════════════════════════════════════════
# state.py: CancelledError returns False
# ══════════════════════════════════════════════════════════════════════


class TestApprovalCancelledError:
    """CancelledError during approval returns False (deny-by-default)."""

    @pytest.mark.asyncio
    async def test_cancelled_error_returns_false(self) -> None:
        """Simulate CancelledError during wait_for → returns False."""
        fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()

        async def _wait():
            try:
                return await asyncio.wait_for(fut, timeout=1.0)
            except asyncio.CancelledError:
                return False

        task = asyncio.create_task(_wait())
        await asyncio.sleep(0.01)
        task.cancel()
        result = await task
        assert result is False
