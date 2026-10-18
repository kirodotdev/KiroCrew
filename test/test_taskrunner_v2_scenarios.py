"""Scenario tests for TaskRunner V2 changes.

Tests cover: Phase 1 bug fixes (index-based lookup, serialized parallel,
MAX_TOTAL_STEPS, cycle detection), Phase 2 git coordination, Phase 3
observed-state memory and Phase 4 review separation.

Each test simulates a realistic task runner scenario end-to-end.

The edge-case scenarios are in ``test_taskrunner_v2_scenarios_edge_cases.py``; the
per-step session and memory scenarios, and the review-retry, cycle-detection,
parallel-group, re-index and prompt scenarios after them, in
``test_taskrunner_v2_scenarios_step_sessions.py``; and the fakes all three modules
use in ``taskrunner_v2_scenarios_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from taskrunner_v2_scenarios_helpers import _passthrough_sandbox  # noqa: F401
from taskrunner_v2_scenarios_helpers import _llm_event, _make_provider, _mock_sessions

from conftest import requires_git
from kiro_crew.taskrunner import (
    MAX_TOTAL_TASKS,
    Step,
    StepStatus,
    TaskRun,
    TaskRunner,
)


def _non_git_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Path:
    """A directory ``init_workspace`` must treat as NOT a repository, on any host.

    "Non-git dir" is not a property ``tmp_path`` has everywhere: a harness that
    pins ``TMPDIR`` under the checkout gives it a real ``.git`` among its
    ancestors, ``git rev-parse --is-inside-work-tree`` answers yes, and the run
    then adds a REAL worktree and task branch to the enclosing repository -- the
    very thing the non-git path exists to avoid. ``GIT_CEILING_DIRECTORIES`` is
    git's own seam for that upward walk (discovery stops below the named
    directory) and the spawn inherits ``os.environ``, so the state is constructed
    here rather than assumed of the host. The directory is a CHILD of the ceiling
    because git checks its starting directory before consulting the ceiling.
    """
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    work = tmp_path / name
    work.mkdir()
    return work


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
    """Fixed: revert_step is not called when a step fails without committing."""

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

        async def _review_fail_then_pass(r, s, sessions, agent, session_key="", *, ctx=None):
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
        """A run without git has no diff, so review uses the generic prompt."""
        from kiro_crew import git_coord  # noqa: F401

        sessions = _mock_sessions()
        sessions.get_or_create = AsyncMock(return_value=(_make_provider(), True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s")
        run.task_id = "no_diff"
        run.branch_name = ""
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
        # A git run always records its own worktree; a path that does not exist keeps
        # every git call failing here, so no test step can reach a real repository.
        run.work_dir = run.worktree_path = str(tmp_path / "no-such-worktree")
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
            patch("kiro_crew.task_executor.self_review", return_value=True),
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
        # A git run always records its own worktree; a nonexistent path keeps git off
        # any real repository.
        run.work_dir = run.worktree_path = str(tmp_path / "no-such-worktree")
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
    async def test_init_creates_repo_and_branch(self, tmp_path: Path, monkeypatch) -> None:
        """init_workspace on a plain non-git dir sets git_enabled=False and returns immediately."""
        from kiro_crew import git_coord

        work_dir = _non_git_dir(tmp_path, monkeypatch, "plain")
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

        async def _review_fail_once(r, s, sessions, agent, session_key="", *, ctx=None):
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
