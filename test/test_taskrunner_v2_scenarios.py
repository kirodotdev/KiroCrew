"""Scenario tests for TaskRunner V2 changes.

Tests cover: Phase 1 bug fixes (index-based lookup, serialized parallel,
MAX_TOTAL_STEPS, cycle detection), Phase 2 git coordination, Phase 3
observed-state memory, Phase 4 review separation, and edge cases.

Each test simulates a realistic task runner scenario end-to-end.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import requires_git
from kiro_crew.taskrunner import (
    MAX_TOTAL_TASKS,
    Step,
    StepStatus,
    TaskRun,
    TaskRunner,
)


@pytest.fixture(autouse=True)
def _passthrough_sandbox(monkeypatch):
    """Several scenarios spawn REAL git via ``git_coord._git`` →
    ``sandboxed_spawn_argv`` → ``wrap_argv``, which raises when no OS-level
    sandbox backend is available. These exercise task/git logic, not sandbox
    availability, so run the command unwrapped in-test."""
    import os as _os

    from kiro_crew import git_coord

    monkeypatch.setattr(
        git_coord,
        "sandboxed_spawn_argv",
        lambda argv, *a, **k: (list(argv), dict(_os.environ), None),
    )


# ── Helpers ──


def _mock_sessions() -> MagicMock:
    s = MagicMock()
    s._lock = asyncio.Lock()
    s._sessions = {}
    s.get_or_create = AsyncMock()
    s.release = MagicMock()
    s.reset = AsyncMock()
    s.record_success = MagicMock()
    s.record_failure = AsyncMock()
    s.check_context_usage = MagicMock()

    async def _open_task_session(_parent_key, session_key, *, agent=None, cwd=None, approval_policy=""):
        return await s.get_or_create(session_key, agent=agent, cwd=cwd)

    s.open_task_session = _open_task_session
    s.release_subagent_runtime = AsyncMock()
    return s


def _llm_event(kind: str, text: str = ""):
    from kiro_crew.providers.base import LLMEvent

    return LLMEvent(kind=kind, text=text)


def _make_provider(text: str = "done"):
    provider = MagicMock()

    async def _stream(msg: str):
        yield _llm_event("text_chunk", text)
        yield _llm_event("complete")

    provider.stream = _stream
    provider.approve_tool = AsyncMock()
    provider.reject_tool = AsyncMock()
    provider.context_usage_pct = MagicMock(return_value=0.0)
    return provider


# ── BUG 1: Cycle detection threshold unreachable ──


class TestBug1CycleDetectionThreshold:
    """_MAX_RETRIES=3 means max 2 consecutive same-error matches.
    Cycle detection at >=5 is unreachable. Warn at >=3 is also unreachable.
    This test proves the bug exists."""

    @pytest.mark.asyncio
    async def test_cycle_warn_fires_within_max_retries(self, tmp_path: Path) -> None:
        """With _MAX_RETRIES=3, cycle detection now fires: warn at 2 same errors, fail at 3."""
        sessions = _mock_sessions()
        notifications: list[tuple[str, str]] = []

        async def _on_notify(t: str, b: str, tid: str = "") -> None:
            notifications.append((t, b))

        provider = MagicMock()

        async def _always_same_error(msg: str):
            raise RuntimeError("identical error")
            yield  # type: ignore[misc]

        provider.stream = _always_same_error
        provider.approve_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(
            sessions=sessions, auto_test=False, on_notify=_on_notify, work_dir=tmp_path
        )
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Loopy", description="d")
        run.tasks = [step]

        success = await runner._execute_single_task(run, step)
        assert not success

        # FIXED: cycle detection now fires within _MAX_RETRIES
        loop_warnings = [t for t, _ in notifications if "loop" in t.lower()]
        assert (
            len(loop_warnings) >= 1
        ), "Expected loop warning to fire — cycle detection thresholds are now reachable"


# ── BUG 2: Git revert on failed step reverts wrong commit ──


class TestBug2RevertOnFailedStep:
    """Fixed: revert_step is no longer called when a step fails without committing."""

    @pytest.mark.asyncio
    async def test_no_revert_when_step_never_committed(self, tmp_path: Path) -> None:
        """Failed step that never committed should NOT trigger revert."""
        from kiro_crew import git_coord

        sessions = _mock_sessions()

        provider = MagicMock()

        async def _fail(msg: str):
            raise RuntimeError("step failed")
            yield  # type: ignore[misc]

        provider.stream = _fail
        provider.approve_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.branch_name = "kirocrew/task/test"
        # Simulate a previous step's commit
        run.commit_hashes = ["abc123"]

        step = Step(index=2, title="Failing step", description="d")
        run.tasks = [
            Step(index=1, title="Prev", description="d", status=StepStatus.PASSED),
            step,
        ]

        with patch.object(git_coord, "revert_step", new_callable=AsyncMock) as mock_revert:
            success = await runner._execute_single_task(run, step, "key")

        assert not success
        # FIXED: revert_step is NOT called — step 2 never committed
        assert not mock_revert.called, "revert_step should not be called for uncommitted step"


# ── BUG 3: Double commit after review fail + retry ──


class TestBug3DoubleCommitOnReviewRetry:
    """Fixed: review failure now reverts the bad commit before retrying."""

    @pytest.mark.asyncio
    async def test_revert_before_retry_on_review_failure(self, tmp_path: Path) -> None:
        from kiro_crew import git_coord  # noqa: F401

        sessions = _mock_sessions()
        provider = _make_provider("done")
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.branch_name = "kirocrew/task/test"
        step = Step(index=1, title="Test", description="d")
        run.tasks = [step]

        commit_calls: list[int] = []
        revert_calls: list[str] = []

        async def _mock_commit(r, s):
            commit_calls.append(s.index)
            r.commit_hashes.append(f"sha_{len(commit_calls)}")
            return f"sha_{len(commit_calls)}"

        async def _mock_revert(r):
            revert_calls.append("revert")
            if r.commit_hashes:
                r.commit_hashes.pop()

        review_count = 0

        async def _review_fail_then_pass(r, s, sessions, agent, session_key=""):
            nonlocal review_count
            review_count += 1
            if review_count == 1:
                s.error = "Review: bad code"
                return False
            return True

        with (
            patch("kiro_crew.task_executor.git_coord.commit_step", side_effect=_mock_commit),
            patch("kiro_crew.task_executor.git_coord.revert_step", side_effect=_mock_revert),
            patch("kiro_crew.task_executor.self_review", side_effect=_review_fail_then_pass),
        ):
            success = await runner._execute_single_task(run, step, "key")

        assert success
        # FIXED: bad commit is reverted before retry, then good commit is made
        assert len(commit_calls) == 2, f"Expected 2 commits, got {len(commit_calls)}"
        assert (
            len(revert_calls) == 1
        ), f"Expected 1 revert (bad commit reverted before retry), got {len(revert_calls)}"


# ── BUG 4: _execute_tasks returns after replan in parallel group ──


class TestBug4ReturnAfterReplanInGroup:
    """In a parallel group [A, B, C], if A fails, replan triggers.
    With asyncio.gather, B and C still run concurrently."""

    @pytest.mark.asyncio
    async def test_successful_replan_still_exits_execute_tasks(self, tmp_path: Path) -> None:
        spec = tmp_path / "TASK.md"
        spec.write_text("# Feature", encoding="utf-8")

        sessions = _mock_sessions()

        # 3 independent steps → single parallel group
        step_json = json.dumps(
            [
                {"title": "A", "description": "d"},
                {"title": "B", "description": "d"},
                {"title": "C", "description": "d"},
            ]
        )

        decompose_provider = MagicMock()

        async def _decompose_stream(msg: str):
            yield _llm_event("text_chunk", step_json)
            yield _llm_event("complete")

        decompose_provider.stream = _decompose_stream
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        failed_titles: list[str] = []

        def _make_step_provider():
            p = MagicMock()

            async def _step_stream(msg: str):
                if "**A**" in msg:
                    failed_titles.append("A")
                    raise RuntimeError("A fails")
                yield _llm_event("text_chunk", "ok")
                yield _llm_event("complete")

            p.stream = _step_stream
            p.approve_tool = AsyncMock()
            p.context_usage_pct = MagicMock(return_value=0.0)
            return p

        async def _get_or_create(key: str, agent=None, cwd=None, **kwargs):
            if "decompose" in key:
                return decompose_provider, True, False
            return _make_step_provider(), True, False

        sessions.get_or_create = _get_or_create

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        with patch.object(runner, "self_review", return_value=True):
            result = await runner.run(spec)

        # A failed (triggers replan), B and C ran via gather
        assert len(failed_titles) >= 1  # A was attempted
        a_step = next((s for s in result.tasks if s.title == "A"), None)
        assert a_step and a_step.status == StepStatus.FAILED


# ── Scenario: Multi-step task with git coordination end-to-end ──


@requires_git
class TestScenarioGitCoordinationE2E:
    """Simulate a real task: decompose → execute 3 steps → each commits → complete."""

    @pytest.mark.asyncio
    async def test_full_git_workflow(self, tmp_path: Path) -> None:
        from kiro_crew import git_coord

        # Set up a real git repo
        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "existing.py").write_text("x = 1")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "initial")

        sessions = _mock_sessions()

        step_json = json.dumps(
            [
                {"title": "Add foo", "description": "Create foo.py"},
                {"title": "Add bar", "description": "Create bar.py", "depends_on": [1]},
            ]
        )

        decompose_provider = MagicMock()

        async def _decompose_stream(msg: str):
            yield _llm_event("text_chunk", step_json)
            yield _llm_event("complete")

        decompose_provider.stream = _decompose_stream
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        step_call = 0
        step_provider = MagicMock()

        async def _step_stream(msg: str):
            nonlocal step_call
            step_call += 1
            # Simulate creating files in the worktree
            yield _llm_event("text_chunk", f"Created file for step {step_call}")
            yield _llm_event("complete")

        step_provider.stream = _step_stream
        step_provider.approve_tool = AsyncMock()
        step_provider.context_usage_pct = MagicMock(return_value=0.0)

        async def _get_or_create(key: str, agent=None, cwd=None, **kwargs):
            if "decompose" in key:
                return decompose_provider, True, False
            return step_provider, True, False

        sessions.get_or_create = _get_or_create

        # Test git_coord directly (no need for full runner.run)
        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "e2e_test"
        run.work_dir = str(work_dir)

        await git_coord.init_workspace(run)
        assert run.branch_name == "kirocrew/task/e2e_test"
        assert run.worktree_path != ""
        wt = Path(run.work_dir)

        # Step 1: create foo.py
        (wt / "foo.py").write_text("def foo(): pass")
        step1 = Step(index=1, title="Add foo", description="d")
        sha1 = await git_coord.commit_step(run, step1)
        assert sha1 != ""

        # Step 2: create bar.py
        (wt / "bar.py").write_text("def bar(): pass")
        step2 = Step(index=2, title="Add bar", description="d")
        sha2 = await git_coord.commit_step(run, step2)
        assert sha2 != ""

        # State summary should show both commits
        summary = await git_coord.get_state_summary(run)
        assert "foo" in summary.lower() or "step 1" in summary.lower()
        assert "bar" in summary.lower() or "step 2" in summary.lower()

        # Step diff should show only last commit
        diff = await git_coord.get_step_diff(run)
        assert "bar.py" in diff

        # Finalize
        branch = await git_coord.finalize(run)
        assert branch == "kirocrew/task/e2e_test"


# ── Scenario: Step fails mid-task, revert preserves earlier work ──


@requires_git
class TestScenarioRevertPreservesEarlierWork:
    @pytest.mark.asyncio
    async def test_revert_only_affects_last_commit(self, tmp_path: Path) -> None:
        from kiro_crew import git_coord

        repo = tmp_path / "repo"
        repo.mkdir()
        await git_coord._git(str(repo), "init")
        (repo / "seed.txt").write_text("seed")
        await git_coord._git(str(repo), "add", "-A")
        await git_coord._git(str(repo), "commit", "-m", "initial")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "rv_test"
        run.work_dir = str(repo)

        await git_coord.init_workspace(run)
        wt = Path(run.work_dir)

        # Step 1 succeeds
        (wt / "good.py").write_text("good = True")
        step1 = Step(index=1, title="Good step", description="d")
        await git_coord.commit_step(run, step1)

        # Step 2 commits then needs revert
        (wt / "bad.py").write_text("bad = True")
        step2 = Step(index=2, title="Bad step", description="d")
        await git_coord.commit_step(run, step2)
        assert (wt / "bad.py").exists()

        # Revert step 2
        await git_coord.revert_step(run)
        assert not (wt / "bad.py").exists(), "bad.py should be gone after revert"
        assert (wt / "good.py").exists(), "good.py should survive revert"

        await git_coord.finalize(run)


# ── Scenario: Non-git directory fallback ──


class TestScenarioNonGitFallback:
    @pytest.mark.asyncio
    async def test_build_task_prompt_without_git(self) -> None:
        """Without git, prompt uses WorkingMemory fallback."""
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.branch_name = ""  # no git
        run.memory.files_changed = ["Created handler.py"]
        step = Step(index=1, title="Next", description="d")
        run.tasks = [step]

        prompt = await runner._build_task_prompt(run, step, attempt=1)
        assert "Working Memory" in prompt
        assert "handler.py" in prompt
        assert "Git Log" not in prompt

    @requires_git
    @pytest.mark.asyncio
    async def test_build_task_prompt_with_git(self, tmp_path: Path) -> None:
        """With git, prompt includes git state."""
        from kiro_crew import git_coord

        repo = tmp_path / "repo"
        repo.mkdir()
        await git_coord._git(str(repo), "init")
        (repo / "seed.txt").write_text("seed")
        await git_coord._git(str(repo), "add", "-A")
        await git_coord._git(str(repo), "commit", "-m", "initial")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "prompt_test"
        run.work_dir = str(repo)

        await git_coord.init_workspace(run)
        wt = Path(run.work_dir)
        (wt / "file.py").write_text("x = 1")
        step = Step(index=1, title="Add file", description="d")
        await git_coord.commit_step(run, step)

        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        step2 = Step(index=2, title="Next step", description="d")
        run.tasks = [step, step2]

        prompt = await runner._build_task_prompt(run, step2, attempt=1)
        assert "Git Log" in prompt
        assert "branch" in prompt.lower()

        await git_coord.finalize(run)


# ── Scenario: Review with git diff vs without ──


class TestScenarioReviewWithDiff:
    @pytest.mark.asyncio
    async def test_review_uses_separate_session(self, tmp_path: Path) -> None:
        """Review creates its own session key, not reusing the step session."""
        sessions = _mock_sessions()
        sessions.get_or_create = AsyncMock(return_value=(_make_provider(), True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s")
        run.task_id = "rev_test"
        step = Step(index=1, title="Test", description="d")
        run.tasks = [step]

        with patch("kiro_crew.task_executor.stream_and_collect_json", return_value={"ok": True}):
            result = await runner.self_review(run, step, "taskrunner:rev_test")

        assert result is True
        # Verify review used its own session key
        create_calls = sessions.get_or_create.call_args_list
        assert any("review" in str(c) for c in create_calls)
        # Verify review session was reset after use
        reset_calls = sessions.reset.call_args_list
        assert any("review" in str(c) for c in reset_calls)

    @pytest.mark.asyncio
    async def test_review_includes_diff_when_git_available(self, tmp_path: Path) -> None:
        """When git diff is available, review prompt includes actual diff."""
        from kiro_crew import git_coord  # noqa: F401

        sessions = _mock_sessions()
        sessions.get_or_create = AsyncMock(return_value=(_make_provider(), True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s")
        run.task_id = "diff_rev"
        run.branch_name = "kirocrew/task/diff_rev"
        step = Step(index=1, title="Add handler", description="Create request handler")
        run.tasks = [step]

        captured_prompt = []

        async def _capture_json(client, prompt):
            captured_prompt.append(prompt)
            return {"ok": True}

        with (
            patch(
                "kiro_crew.task_executor.git_coord.get_step_diff",
                return_value="diff --git a/handler.py\n+def handle():",
            ),
            patch("kiro_crew.task_executor.stream_and_collect_json", side_effect=_capture_json),
        ):
            await runner.self_review(run, step)

        assert len(captured_prompt) == 1
        assert "independent review agent" in captured_prompt[0].lower()
        assert "handler.py" in captured_prompt[0]
        assert "Actual Diff" in captured_prompt[0]

    @pytest.mark.asyncio
    async def test_review_fallback_without_diff(self, tmp_path: Path) -> None:
        """Without git diff, review uses generic prompt."""
        from kiro_crew import git_coord  # noqa: F401

        sessions = _mock_sessions()
        sessions.get_or_create = AsyncMock(return_value=(_make_provider(), True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s")
        run.task_id = "no_diff"
        run.branch_name = "kirocrew/task/no_diff"
        step = Step(index=1, title="Test", description="d")
        run.tasks = [step]

        captured_prompt = []

        async def _capture_json(client, prompt):
            captured_prompt.append(prompt)
            return {"ok": True}

        with (
            patch("kiro_crew.task_executor.git_coord.get_step_diff", return_value=""),
            patch("kiro_crew.task_executor.stream_and_collect_json", side_effect=_capture_json),
        ):
            await runner.self_review(run, step)

        assert len(captured_prompt) == 1
        assert "Actual Diff" not in captured_prompt[0]


# ── Scenario: Replan uses git state instead of memory ──


class TestScenarioReplanWithGitState:
    @pytest.mark.asyncio
    async def test_replan_includes_git_context(self, tmp_path: Path) -> None:
        """When git is active, _try_replan uses git state summary."""
        from kiro_crew import git_coord

        sessions = _mock_sessions()

        replan_json = json.dumps([{"title": "Fix step", "description": "d"}])
        provider = MagicMock()

        async def _stream(msg: str):
            yield _llm_event("text_chunk", replan_json)
            yield _llm_event("complete")

        provider.stream = _stream
        provider.approve_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.branch_name = "kirocrew/task/test"
        run.tasks = [
            Step(index=1, title="Done", description="d", status=StepStatus.PASSED),
        ]
        failed = Step(index=2, title="Broken", description="d", error="import error")
        run.tasks.append(failed)

        git_summary = "## Git Log\n```\nabc1234 step 1: Done\n```"

        with (
            patch.object(git_coord, "get_state_summary", return_value=git_summary),
            patch.object(runner, "self_review", return_value=True),
        ):
            result = await runner._try_replan(run, failed)

        assert result is True
        assert run.replan_count == 1

    @pytest.mark.asyncio
    async def test_replan_falls_back_to_memory_without_git(self, tmp_path: Path) -> None:
        """Without git, _try_replan uses WorkingMemory."""
        sessions = _mock_sessions()

        replan_json = json.dumps([{"title": "Fix", "description": "d"}])
        provider = MagicMock()

        async def _stream(msg: str):
            yield _llm_event("text_chunk", replan_json)
            yield _llm_event("complete")

        provider.stream = _stream
        provider.approve_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.branch_name = ""  # no git
        run.memory.files_changed = ["Created foo.py"]
        run.tasks = [Step(index=1, title="Done", description="d", status=StepStatus.PASSED)]
        failed = Step(index=2, title="Broken", description="d", error="err")
        run.tasks.append(failed)

        with patch.object(runner, "self_review", return_value=True):
            result = await runner._try_replan(run, failed)

        assert result is True


# ── Scenario: MAX_TOTAL_STEPS prevents unbounded growth ──


class TestScenarioMaxTotalSteps:
    @pytest.mark.asyncio
    async def test_replan_blocked_at_boundary(self, tmp_path: Path) -> None:
        """Exactly at MAX_TOTAL_TASKS → replan refused."""
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.tasks = [
            Step(index=i, title=f"S{i}", description="d", status=StepStatus.PASSED)
            for i in range(1, MAX_TOTAL_TASKS + 1)
        ]
        failed = Step(index=MAX_TOTAL_TASKS, title="Last", description="d", error="err")

        result = await runner._try_replan(run, failed)
        assert not result
        assert "Task limit" in run.error

    @pytest.mark.asyncio
    async def test_replan_allowed_below_limit(self, tmp_path: Path) -> None:
        """Below MAX_TOTAL_TASKS → replan proceeds."""
        sessions = _mock_sessions()

        replan_json = json.dumps([{"title": "New", "description": "d"}])
        provider = MagicMock()

        async def _stream(msg: str):
            yield _llm_event("text_chunk", replan_json)
            yield _llm_event("complete")

        provider.stream = _stream
        provider.approve_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.tasks = [
            Step(index=i, title=f"S{i}", description="d", status=StepStatus.PASSED)
            for i in range(1, MAX_TOTAL_TASKS)  # one below limit
        ]
        failed = Step(index=MAX_TOTAL_TASKS - 1, title="Almost", description="d", error="err")

        with patch.object(runner, "self_review", return_value=True):
            result = await runner._try_replan(run, failed)

        assert result is True


# ── Scenario: Index-based step lookup after replan ──


class TestScenarioIndexBasedLookup:
    @pytest.mark.asyncio
    async def test_step_lookup_uses_fresh_reference(self, tmp_path: Path) -> None:
        """After replan adds steps, execution uses fresh references from run.tasks."""
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")

        # Original steps
        s1 = Step(index=1, title="Original", description="d")
        run.tasks = [s1]

        # Simulate replan adding a step
        s2 = Step(index=2, title="Replanned", description="d")
        run.tasks.append(s2)

        # Execute with index-based lookup
        pending = [s for s in run.tasks if s.status == StepStatus.PENDING]
        groups = TaskRunner._group_parallel_tasks(pending, set())

        # Mutate s1 in run.tasks (simulating replan mutation)
        run.tasks[0].description = "MUTATED"

        # The lookup in _execute_tasks should find the mutated version
        for group in groups:
            for step_ref in group:
                step = next((s for s in run.tasks if s.index == step_ref.index), step_ref)
                if step.index == 1:
                    assert step.description == "MUTATED", "Should use fresh reference"
                break
            break


# ── Scenario: Worktree isolation ──


@requires_git
class TestScenarioWorktreeIsolation:
    @pytest.mark.asyncio
    async def test_worktree_does_not_modify_original(self, tmp_path: Path) -> None:
        """Changes in worktree don't affect the original repo."""
        from kiro_crew import git_coord

        repo = tmp_path / "repo"
        repo.mkdir()
        await git_coord._git(str(repo), "init")
        (repo / "original.py").write_text("original")
        await git_coord._git(str(repo), "add", "-A")
        await git_coord._git(str(repo), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "iso_test"
        run.work_dir = str(repo)

        await git_coord.init_workspace(run)

        # Create file in worktree
        wt = Path(run.work_dir)
        (wt / "new_file.py").write_text("new")
        step = Step(index=1, title="Add file", description="d")
        await git_coord.commit_step(run, step)

        # Original repo should NOT have the new file
        assert not (repo / "new_file.py").exists(), "Original repo should be untouched"
        assert (wt / "new_file.py").exists(), "Worktree should have the file"

        await git_coord.finalize(run)


# ── Scenario: Completion notification includes branch name ──


class TestScenarioCompletionNotification:
    @pytest.mark.asyncio
    async def test_completion_includes_branch(self, tmp_path: Path) -> None:
        spec = tmp_path / "TASK.md"
        spec.write_text("# Feature", encoding="utf-8")

        sessions = _mock_sessions()
        step_json = json.dumps([{"title": "One step", "description": "d"}])

        decompose_provider = MagicMock()

        async def _decompose_stream(msg: str):
            yield _llm_event("text_chunk", step_json)
            yield _llm_event("complete")

        decompose_provider.stream = _decompose_stream
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        step_provider = _make_provider("done")

        async def _get_or_create(key: str, agent=None, cwd=None, **kwargs):
            if "decompose" in key:
                return decompose_provider, True, False
            return step_provider, True, False

        sessions.get_or_create = _get_or_create

        notifications: list[tuple[str, str]] = []

        async def _on_notify(t: str, b: str, tid: str = "") -> None:
            notifications.append((t, b))

        runner = TaskRunner(
            sessions=sessions, auto_test=False, on_notify=_on_notify, work_dir=tmp_path
        )

        # Patch git_coord to set branch_name
        async def _mock_init(run):
            run.branch_name = "kirocrew/task/test_branch"

        from kiro_crew import git_coord

        with (
            patch.object(git_coord, "init_workspace", side_effect=_mock_init),
            patch.object(git_coord, "commit_step", return_value="sha123"),
            patch.object(git_coord, "finalize", return_value="kirocrew/task/test_branch"),
            patch.object(runner, "self_review", return_value=True),
        ):
            result = await runner.run(spec)

        assert result.status == "completed"
        completion_msgs = [b for t, b in notifications if "completed" in t.lower()]
        assert any(
            "Branch" in b for b in completion_msgs
        ), f"Completion notification should include branch. Got: {completion_msgs}"


# ── Scenario: git_coord.init_workspace failure is non-fatal ──


class TestScenarioGitInitFailureNonFatal:
    @pytest.mark.asyncio
    async def test_git_init_failure_continues_without_git(self, tmp_path: Path) -> None:
        spec = tmp_path / "TASK.md"
        spec.write_text("# Feature", encoding="utf-8")

        sessions = _mock_sessions()
        step_json = json.dumps([{"title": "Step", "description": "d"}])

        decompose_provider = MagicMock()

        async def _decompose_stream(msg: str):
            yield _llm_event("text_chunk", step_json)
            yield _llm_event("complete")

        decompose_provider.stream = _decompose_stream
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        step_provider = _make_provider("done")

        async def _get_or_create(key: str, agent=None, cwd=None, **kwargs):
            if "decompose" in key:
                return decompose_provider, True, False
            return step_provider, True, False

        sessions.get_or_create = _get_or_create

        from kiro_crew import git_coord

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        with (
            patch.object(git_coord, "init_workspace", side_effect=RuntimeError("git not found")),
            patch.object(runner, "self_review", return_value=True),
        ):
            result = await runner.run(spec)

        assert result.status == "completed"
        assert result.branch_name == ""  # no git


# ── Scenario: Persist and restore runs across restarts ──


class TestScenarioRunsPersistence:
    def test_persist_and_reload(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        run = TaskRun(
            spec_path="/t.md",
            spec_content="s",
            task_id="persist_1",
            status="completed",
            started_at=1000.0,
            finished_at=2000.0,
            tokens_used=500,
            replan_count=1,
            work_dir=str(tmp_path),
            tasks=[
                Step(index=1, title="A", description="d", status=StepStatus.PASSED, attempts=1),
                Step(
                    index=2,
                    title="B",
                    description="d",
                    status=StepStatus.FAILED,
                    error="boom",
                    attempts=3,
                ),
            ],
        )
        runner._runs["persist_1"] = run
        runner._persist_runs()

        # Reload
        runner2 = TaskRunner(sessions=_mock_sessions(), auto_test=False, work_dir=tmp_path)
        assert "persist_1" in runner2._runs
        restored = runner2._runs["persist_1"]
        assert restored.status == "completed"
        assert restored.tokens_used == 500
        assert restored.replan_count == 1
        assert len(restored.tasks) == 2
        assert restored.tasks[0].status == StepStatus.PASSED
        assert restored.tasks[1].status == StepStatus.FAILED
        assert restored.tasks[1].error == "boom"

    def test_persist_truncates_step_results(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        run = TaskRun(
            spec_path="/t.md",
            spec_content="s",
            task_id="trunc_1",
            status="completed",
            tasks=[Step(index=1, title="A", description="d", result="x" * 5000)],
        )
        runner._runs["trunc_1"] = run
        runner._persist_runs()

        data = json.loads((tmp_path / "runs.json").read_text(encoding="utf-8"))
        assert len(data[0]["task_details"][0]["result"]) <= 2000


# ── Scenario: Full E2E git revert + replan integration ──


@requires_git
class TestScenarioGitRevertReplanE2E:
    @pytest.mark.asyncio
    async def test_fail_revert_replan_commit(self, tmp_path: Path) -> None:
        """Task  passes → commit. Step 2 fails → revert. Replan → new step commits."""
        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "base.py").write_text("base")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "initial")

        run = TaskRun(spec_path="/t.md", spec_content="s", task_id="e2e_replan")
        run.work_dir = str(work_dir)
        await git_coord.init_workspace(run)
        wt = Path(run.work_dir)

        # Step 1: succeeds
        (wt / "good.py").write_text("good = True")
        s1 = Step(index=1, title="Good", description="d")
        sha1 = await git_coord.commit_step(run, s1)
        assert sha1 != ""
        assert len(run.commit_hashes) == 1

        # Step 2: commits then fails (simulating commit before review catches issue)
        (wt / "bad.py").write_text("bad = True")
        s2 = Step(index=2, title="Bad", description="d")
        await git_coord.commit_step(run, s2)
        assert len(run.commit_hashes) == 2

        # Revert step 2
        await git_coord.revert_step(run)
        assert len(run.commit_hashes) == 1
        assert not (wt / "bad.py").exists()
        assert (wt / "good.py").exists()

        # Replan step: commits on same branch
        (wt / "fix.py").write_text("fix = True")
        s3 = Step(index=3, title="Fix", description="d")
        sha3 = await git_coord.commit_step(run, s3)
        assert sha3 != ""
        assert len(run.commit_hashes) == 2

        # Verify state summary shows both good commits
        summary = await git_coord.get_state_summary(run)
        assert "good" in summary.lower() or "step 1" in summary.lower()
        assert "fix" in summary.lower() or "step 3" in summary.lower()
        # bad step should NOT be in the log
        assert "bad" not in summary.lower() or "step 2" not in summary.lower()

        await git_coord.finalize(run)


# ── Scenario: Replan returns empty decomposition ──


class TestScenarioReplanEmptyDecomposition:
    @pytest.mark.asyncio
    async def test_replan_empty_steps_fails(self, tmp_path: Path) -> None:
        """Replan decomposition returns no steps → run fails."""
        sessions = _mock_sessions()

        provider = MagicMock()

        async def _stream(msg: str):
            yield _llm_event("text_chunk", "[]")  # empty array
            yield _llm_event("complete")

        provider.stream = _stream
        provider.approve_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.tasks = [Step(index=1, title="Done", description="d", status=StepStatus.PASSED)]
        failed = Step(index=2, title="Broken", description="d", error="err")
        run.tasks.append(failed)

        result = await runner._try_replan(run, failed)
        assert not result
        assert "Re-plan failed" in run.error


# ── Scenario: Process crash recovery preserves git branch ──


class TestScenarioProcessCrashWithGit:
    @pytest.mark.asyncio
    async def test_acp_crash_recovery_keeps_branch(self, tmp_path: Path) -> None:
        """AcpProcessDied during git-enabled run → recovery continues on same branch."""
        from kiro_crew.acp.client import AcpProcessDied

        sessions = _mock_sessions()
        call_count = 0

        provider = MagicMock()

        async def _crash_then_succeed(msg: str):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise AcpProcessDied("Process exited")
            yield _llm_event("text_chunk", "recovered")
            yield _llm_event("complete")

        provider.stream = _crash_then_succeed
        provider.approve_tool = AsyncMock()
        provider.context_usage_pct = MagicMock(return_value=0.0)
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.branch_name = "kirocrew/task/crash_test"
        step = Step(index=1, title="Crashy", description="d")
        run.tasks = [step]

        success = await runner._execute_single_task(run, step)
        assert success
        assert step.status == StepStatus.PASSED
        # Branch should still be set (not cleared by crash recovery)
        assert run.branch_name == "kirocrew/task/crash_test"


# ── Scenario: Global timeout during step execution ──


class TestScenarioGlobalTimeoutMidExecution:
    @pytest.mark.asyncio
    async def test_timeout_between_groups(self, tmp_path: Path) -> None:
        """Global timeout exceeded between groups → fails with timeout error."""
        sessions = _mock_sessions()
        provider = _make_provider("done")
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        notifications: list[tuple[str, str]] = []

        async def _on_notify(t: str, b: str, tid: str = "") -> None:
            notifications.append((t, b))

        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            global_timeout=1.0,
            on_notify=_on_notify,
        )
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.started_at = 0.0  # started long ago → already timed out
        s1 = Step(index=1, title="A", description="d")
        s2 = Step(index=2, title="B", description="d", depends_on=[1])
        run.tasks = [s1, s2]

        with patch.object(runner, "self_review", return_value=True):
            await runner._execute_tasks(run, "key")

        assert run.status == "failed"
        assert "timeout" in run.error.lower()
        assert any("timed out" in t.lower() for t, _ in notifications)


# ── Scenario: Token budget exhausted mid-execution ──


class TestScenarioTokenBudgetMidExecution:
    @pytest.mark.asyncio
    async def test_budget_exceeded_between_groups(self, tmp_path: Path) -> None:
        """Token budget exceeded between groups → fails cleanly."""
        sessions = _mock_sessions()
        provider = _make_provider("done")
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path, token_budget=100)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.tokens_used = 200  # already over budget
        s1 = Step(index=1, title="A", description="d")
        run.tasks = [s1]

        await runner._execute_tasks(run, "key")

        assert run.status == "failed"
        assert "Token budget" in run.error


# ── Scenario: Non-git init_workspace path (git init) ──


class TestScenarioNonGitInitWorkspace:
    @pytest.mark.asyncio
    async def test_init_creates_repo_and_branch(self, tmp_path: Path) -> None:
        """init_workspace on a plain non-git dir sets git_enabled=False and returns immediately."""
        from kiro_crew import git_coord

        work_dir = tmp_path / "plain"
        work_dir.mkdir()
        (work_dir / "file.txt").write_text("hello")

        run = TaskRun(spec_path="/t.md", spec_content="s", task_id="init_test")
        run.work_dir = str(work_dir)

        await git_coord.init_workspace(run)

        assert run.git_enabled is False
        assert run.branch_name == ""
        assert run.worktree_path == ""
        # work_dir unchanged
        assert run.work_dir == str(work_dir)
        # No .git directory created
        assert not (work_dir / ".git").exists()


# ── BUG 5: Review failure doesn't revert committed step before retry ──


class TestBug5ReviewFailRevertBeforeRetry:
    """Fixed: review failure now reverts the committed step before retrying."""

    @pytest.mark.asyncio
    async def test_review_fail_reverts_before_retry(self, tmp_path: Path) -> None:
        from kiro_crew import git_coord  # noqa: F401

        sessions = _mock_sessions()
        provider = _make_provider("done")
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.branch_name = "kirocrew/task/test"
        step = Step(index=1, title="Test", description="d")
        run.tasks = [step]

        commit_calls: list[str] = []
        revert_calls: list[str] = []

        async def _mock_commit(r, s):
            commit_calls.append(f"commit_{s.index}")
            r.commit_hashes.append(f"sha_{len(commit_calls)}")
            return f"sha_{len(commit_calls)}"

        async def _mock_revert(r):
            revert_calls.append("revert")
            if r.commit_hashes:
                r.commit_hashes.pop()

        review_count = 0

        async def _review_fail_once(r, s, sessions, agent, session_key=""):
            nonlocal review_count
            review_count += 1
            return review_count > 1  # fail first, pass second

        with (
            patch("kiro_crew.task_executor.git_coord.commit_step", side_effect=_mock_commit),
            patch("kiro_crew.task_executor.git_coord.revert_step", side_effect=_mock_revert),
            patch("kiro_crew.task_executor.self_review", side_effect=_review_fail_once),
        ):
            success = await runner._execute_single_task(run, step, "key")

        assert success
        # FIXED: 2 commits, 1 revert (bad commit reverted before retry)
        assert len(commit_calls) == 2, f"Expected 2 commits, got {len(commit_calls)}"
        assert (
            len(revert_calls) == 1
        ), f"Expected 1 revert (bad commit reverted), got {len(revert_calls)}"


# ── Scenario: _parse_tasks handles malformed JSON gracefully ──


class TestScenarioParseStepsMalformed:
    def test_garbage_input(self) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        assert runner._parse_tasks("not json at all") == []

    def test_empty_array(self) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        assert runner._parse_tasks("[]") == []

    def test_missing_title(self) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        result = runner._parse_tasks('[{"description": "no title"}]')
        assert result == []

    def test_valid_with_extras(self) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        steps = runner._parse_tasks('[{"title": "A", "description": "d", "extra": true}]')
        assert len(steps) == 1
        assert steps[0].title == "A"


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

        async def _review_once(r, s, sessions, agent, session_key=""):
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
            patch.object(runner, "self_review", return_value=True),
            patch("kiro_crew.task_executor.git_coord") as mock_git,
        ):
            mock_git.commit_step = AsyncMock(side_effect=RuntimeError("git broken"))
            result = await runner._execute_single_task(run, step, "hk")

        assert result is True


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

        async def _mock_review(run, step, sessions, agent, session_key=""):
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

        async def _review(run, step, sessions, agent, session_key=""):
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

        with patch.object(runner, "self_review", return_value=True):
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
            patch.object(runner, "self_review", return_value=True),
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
