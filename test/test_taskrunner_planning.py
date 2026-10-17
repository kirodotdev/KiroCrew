"""The plan a task run adapts as it goes, and the limits it adapts within.

Working memory carries files, decisions and blockers into the next step prompt;
a failed step is re-planned (dependencies re-indexed) until the replan budget or
the total-step limit runs out; a self-review can fail a step or leave it
unverified; an approval gate runs the step, or pauses the run with the step
pending; the same error five times fails a step as a loop; circular dependencies
fall back to sequential order; and the progress file and the status view carry
tokens, skips and replans. The shared doubles live in ``taskrunner_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from taskrunner_helpers import (
    _make_mock_provider,
    _make_mock_sessions,
)

from kiro_crew.task_models import PROGRESS_FILE
from kiro_crew.taskrunner import (
    MAX_TOTAL_TASKS,
    Step,
    StepStatus,
    TaskRun,
    TaskRunner,
    WorkingMemory,
)

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
    async def testself_review_exception_fails_closed(self, tmp_path: Path) -> None:
        """Self-review exception → the step is not verified, so it does not pass."""
        sessions = _make_mock_sessions()
        sessions.get_or_create = AsyncMock(side_effect=RuntimeError("boom"))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s")
        step = Step(index=1, title="Test", description="d")
        run.tasks = [step]

        result = await runner.self_review(run, step)
        assert result is False
        assert step.error.startswith("Self-review could not verify the step")

    @pytest.mark.asyncio
    async def testself_review_no_diff_on_a_git_run_is_unverified(self, tmp_path: Path) -> None:
        """A git run whose step left no diff has nothing to review: not passed."""
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s")
        run.branch_name = "task/x"
        step = Step(index=1, title="Push", description="push the branch")
        run.tasks = [step]

        judge = AsyncMock(return_value={"ok": True})
        with (
            patch("kiro_crew.task_executor.git_coord.get_step_diff", AsyncMock(return_value="")),
            patch("kiro_crew.task_executor.stream_and_collect_json", judge),
        ):
            result = await runner.self_review(run, step)

        assert result is False
        assert "no diff" in step.error
        judge.assert_not_called()

    @pytest.mark.asyncio
    async def testself_review_no_diff_after_a_failed_commit_asks_the_reviewer(
        self, tmp_path: Path
    ) -> None:
        """A failed commit (non-fatal) explains the missing diff, so review still runs."""
        from kiro_crew.task_executor import self_review

        sessions = _make_mock_sessions()
        sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s")
        run.branch_name = "task/x"
        step = Step(index=1, title="Code", description="d")
        run.tasks = [step]

        with (
            patch("kiro_crew.task_executor.git_coord.get_step_diff", AsyncMock(return_value="")),
            patch("kiro_crew.task_executor.stream_and_collect_json", return_value={"ok": True}),
        ):
            assert await self_review(run, step, sessions, "", commit_failed=True) is True

    @pytest.mark.asyncio
    async def testself_review_no_diff_without_git_still_asks_the_reviewer(
        self, tmp_path: Path
    ) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s")
        step = Step(index=1, title="Research", description="d")
        run.tasks = [step]
        sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))

        with patch("kiro_crew.task_executor.stream_and_collect_json", return_value={"ok": True}):
            assert await runner.self_review(run, step) is True

    @pytest.mark.asyncio
    async def test_an_unverified_review_fails_the_step_without_a_rerun(
        self, tmp_path: Path
    ) -> None:
        sessions = _make_mock_sessions()
        sessions.get_or_create = AsyncMock(return_value=(_make_mock_provider("done"), True, False))
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Push", description="d")
        run.tasks = [step]

        async def _unverified(r, s, sessions, agent, session_key="", *, ctx=None):
            s.error = "Self-review could not verify the step: the review itself failed"
            return False

        with (
            patch("kiro_crew.task_executor.self_review", side_effect=_unverified),
            patch("kiro_crew.task_executor.execute_task", AsyncMock(return_value=True)) as ex,
        ):
            success = await runner._execute_single_task(run, step, "key")

        assert success is False
        assert step.status == StepStatus.FAILED
        assert ex.await_count == 1


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

        async def _review_once(r, s, sessions, agent, session_key="", *, ctx=None):
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

        # Step 1 denied → run pauses (denial does not skip)
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
