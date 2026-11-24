"""A step whose output says it is waiting on someone pauses its run, never passes.

``task_step_ask.step_ask`` reads a step's final result for an ``[OPTIONS:]``
trailer or a last paragraph that declares the step blocked, incomplete or
stopping; ``execute_single_task`` then pauses the run with the question instead
of reviewing and passing the step, and ``_try_replan`` plans nothing around it.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from taskrunner_helpers import _make_mock_sessions

from conftest import requires_git
from kiro_crew import git_coord
from kiro_crew.task_step_ask import step_ask
from kiro_crew.taskrunner import Step, StepStatus, TaskRun, TaskRunner

_BLOCKED = (
    "I can't finish this step because production access is missing. "
    "I'm stopping here.\n"
    "[OPTIONS: Access granted, continue | Skip this step]"
)

_PASSED_BUT_BLOCKED = "Tests passed, but this step is blocked until production access is approved."
_PASSED_AND_BLOCKED = "Tests passed and this step is blocked until production access is approved."
_BLOCKED_UNTIL_APPROVED = (
    "This step is blocked, but I can finish once you approve production access."
)
_BLOCKED_NOT_PASSED = (
    "This step is blocked because production access was not passed to this session."
)
_BLOCKED_BUT_TESTS_PASSED = (
    "This step is blocked on missing production access, but local tests passed."
)
_BLOCKED_BUT_OTHER_TASK_DONE = "Task 4 is blocked on production access, but task 3 is complete."
_BLOCKED_DONE_ONLY_WHEN = (
    "This step is blocked, but the task is complete only when you approve production access."
)
_BLOCKED_DONE_IF = (
    "This step is blocked, but if you approve production access the task is complete."
)
_BLOCKED_DONE_ONLY_AFTER = (
    "This step is blocked, but the task is complete only after production access is approved."
)


def _runner(tmp_path: Path) -> TaskRunner:
    return TaskRunner(sessions=_make_mock_sessions(), auto_test=False, work_dir=tmp_path)


class TestStepAsk:
    def test_options_trailer_is_a_question(self) -> None:
        ask = step_ask(_BLOCKED)
        assert "production access is missing" in ask
        assert "Access granted, continue | Skip this step" in ask

    def test_bare_options_trailer_names_its_choices(self) -> None:
        assert step_ask("[OPTIONS: Yes | No]") == "Yes | No"

    @pytest.mark.parametrize(
        "text",
        [
            "Checked the config.\n\nStep 3 is incomplete: the AWS access is missing.",
            "Read the logs.\n\nI'm stopping here until the request is approved.",
            "I cannot proceed without the credentials.",
            "This step remains blocked on the approval.",
            "Task 4 is still incomplete.",
            "I can't continue without the token, so I'm stopping here.",
            "This step is blocked until the permissions are fixed.",
            "This step is blocked until the access request is resolved.",
            "I cannot continue unless you approve the access request.",
            "This step is blocked pending approval, but I can continue once you approve.",
            _PASSED_BUT_BLOCKED,
            "The build succeeded, however I cannot continue without the token.",
            "I can't proceed with the old API, but this step is blocked until production "
            "access is approved.",
            _PASSED_AND_BLOCKED,
            _BLOCKED_UNTIL_APPROVED,
            _BLOCKED_NOT_PASSED,
            "This step is blocked until access is granted, but the build passed.",
            "This step is blocked, but the token was not fixed.",
            "This step is blocked, but the token has not been fixed.",
            "This step is blocked, but the access request hasn't been resolved.",
            _BLOCKED_BUT_TESTS_PASSED,
            _BLOCKED_BUT_OTHER_TASK_DONE,
            _BLOCKED_DONE_ONLY_WHEN,
            _BLOCKED_DONE_IF,
            _BLOCKED_DONE_ONLY_AFTER,
        ],
    )
    def test_declared_stop_in_the_last_paragraph_is_a_question(self, text: str) -> None:
        assert step_ask(text)

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Done. Created handler.py and routes.py.",
            "The old job was blocked on a lock, so I removed the lock.\n\nAll tests pass.",
            "The old job was blocked on a lock, so I removed the lock. All tests pass.",
            "It is incomplete only if verification fails; verification passed.",
            "The step is incomplete only if the check fails; the check passed.",
            "I'm stopping the dev server now that the tests pass.",
            "I couldn't finish initially, but I fixed the permissions and completed the step. "
            "All tests pass.",
            "If verification fails, this step is blocked; verification passed.",
            "Once the cache is cold the step is blocked; it was warm and the step is done.",
            "I can't proceed with the old API, but the new one worked and the step is done.",
            "See the [OPTIONS section of the guide.",
            "This step was blocked on the lock, but the lock is now fixed and the step is done.",
            "This step is blocked on the lock, but that is now resolved; all tests pass.",
        ],
    )
    def test_a_finished_step_asks_nothing(self, text: str) -> None:
        assert step_ask(text) == ""

    def test_the_question_is_bounded(self) -> None:
        assert len(step_ask("I'm stopping here. " + "x" * 1000)) == 300


class TestRunPausesOnAQuestion:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "result",
        [
            _BLOCKED,
            _PASSED_BUT_BLOCKED,
            _PASSED_AND_BLOCKED,
            _BLOCKED_UNTIL_APPROVED,
            _BLOCKED_NOT_PASSED,
            _BLOCKED_BUT_TESTS_PASSED,
            _BLOCKED_BUT_OTHER_TASK_DONE,
            _BLOCKED_DONE_ONLY_WHEN,
            _BLOCKED_DONE_IF,
            _BLOCKED_DONE_ONLY_AFTER,
        ],
    )
    async def test_a_blocked_step_pauses_the_run_unreviewed(
        self, tmp_path: Path, result: str
    ) -> None:
        runner = _runner(tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Read prod", description="d")
        run.tasks = [step, Step(index=2, title="Use it", description="d", depends_on=[1])]

        async def _blocked(_run, task, *args, **kwargs) -> bool:
            task.result = result
            return True

        review = AsyncMock(return_value=True)
        with (
            patch("kiro_crew.task_executor.execute_task", side_effect=_blocked),
            patch("kiro_crew.task_executor.self_review", review),
        ):
            success = await runner._execute_single_task(run, step, "key")

        assert success is False
        assert step.status == StepStatus.PENDING
        assert step.error.startswith("Waiting on input: ")
        assert run.status == "paused"
        assert "production access" in run.error
        assert review.await_count == 0
        assert await runner._try_replan(run, step) is False
        assert run.status == "paused"
        assert run.replan_count == 0

    @pytest.mark.asyncio
    async def test_a_paused_step_commits_its_work_first(self, tmp_path: Path) -> None:
        """The finalizer removes the worktree, so the paused step's edits must be on the branch."""
        runner = _runner(tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.branch_name = "kirocrew/run"
        step = Step(index=1, title="Read prod", description="d")
        run.tasks = [step]

        async def _blocked(_run, task, *args, **kwargs) -> bool:
            task.result = _BLOCKED
            return True

        commit = AsyncMock(return_value="abc123")
        with (
            patch("kiro_crew.task_executor.execute_task", side_effect=_blocked),
            patch("kiro_crew.task_executor.self_review", AsyncMock(return_value=True)),
            patch("kiro_crew.git_coord.commit_step", commit),
        ):
            success = await runner._execute_single_task(run, step, "key")

        assert success is False
        assert run.status == "paused"
        commit.assert_awaited_once_with(run, step)

    @pytest.mark.asyncio
    async def test_a_failed_preservation_commit_fails_the_run_and_keeps_the_worktree(
        self, tmp_path: Path
    ) -> None:
        """No Resume is offered, and finalize has no worktree path left to remove."""
        runner = _runner(tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.branch_name = "kirocrew/run"
        run.worktree_path = str(tmp_path / "wt")
        step = Step(index=1, title="Read prod", description="d")
        run.tasks = [step]

        async def _blocked(_run, task, *args, **kwargs) -> bool:
            task.result = _BLOCKED
            return True

        with (
            patch("kiro_crew.task_executor.execute_task", side_effect=_blocked),
            patch("kiro_crew.task_executor.self_review", AsyncMock(return_value=True)),
            patch("kiro_crew.git_coord.commit_step", AsyncMock(side_effect=RuntimeError("lock"))),
        ):
            success = await runner._execute_single_task(run, step, "key")

        assert success is False
        assert step.status == StepStatus.FAILED
        assert run.status == "failed"
        assert str(tmp_path / "wt") in run.error
        assert run.worktree_path == run.work_dir == ""
        assert await git_coord.workspace_is_valid(run) is False
        assert await runner._try_replan(run, step) is False

    @pytest.mark.asyncio
    async def test_a_queued_sibling_never_starts_after_a_pause(self, tmp_path: Path) -> None:
        """A sibling waiting on the parallel bound does not run once the run stopped."""
        runner = TaskRunner(
            sessions=_make_mock_sessions(),
            auto_test=False,
            work_dir=tmp_path,
            max_parallel_steps=1,
        )
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.tasks = [Step(index=i, title=f"S{i}", description="d") for i in (1, 2)]
        turns: list[int] = []

        async def _blocked(_run, task, *args, **kwargs) -> bool:
            turns.append(task.index)
            task.result = _BLOCKED
            return True

        with patch("kiro_crew.task_executor.execute_task", side_effect=_blocked):
            await runner._execute_tasks(run, "key")

        assert turns == [1]
        assert run.status == "paused"
        assert run.tasks[1].status == StepStatus.PENDING

    @pytest.mark.asyncio
    async def test_a_rejected_step_reverts_only_its_own_commit(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.branch_name = "kirocrew/run"
        step = Step(index=1, title="Edit", description="d")
        run.tasks = [step]

        revert = AsyncMock()
        with (
            patch("kiro_crew.task_executor.execute_task", AsyncMock(return_value=True)),
            patch("kiro_crew.git_coord.commit_step", AsyncMock(return_value="sha1")),
            patch("kiro_crew.git_coord.revert_step", revert),
            patch("kiro_crew.task_executor.self_review", AsyncMock(return_value=False)),
        ):
            await runner._execute_single_task(run, step, "key")

        revert.assert_awaited_once_with(run, "sha1")

    @pytest.mark.asyncio
    async def test_a_review_retry_that_is_blocked_pauses_the_run(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Read prod", description="d")
        run.tasks = [step]
        results = iter(["Done.", "I cannot continue without credentials."])

        async def _turn(_run, task, *args, **kwargs) -> bool:
            task.result = next(results)
            return True

        with (
            patch("kiro_crew.task_executor.execute_task", side_effect=_turn),
            patch("kiro_crew.task_executor.self_review", AsyncMock(return_value=False)),
        ):
            success = await runner._execute_single_task(run, step, "key")

        assert success is False
        assert step.status == StepStatus.PENDING
        assert step.error.startswith("Waiting on input: ")
        assert run.status == "paused"

    @pytest.mark.asyncio
    async def test_a_finished_step_still_passes(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Print", description="d")
        run.tasks = [step]

        async def _done(_run, task, *args, **kwargs) -> bool:
            task.result = "DONE"
            return True

        with (
            patch("kiro_crew.task_executor.execute_task", side_effect=_done),
            patch("kiro_crew.task_executor.self_review", AsyncMock(return_value=True)),
        ):
            success = await runner._execute_single_task(run, step, "key")

        assert success is True
        assert step.status == StepStatus.PASSED
        assert run.status == "running"


@requires_git
class TestRevertKeepsASiblingCommit:
    """Parallel steps share a worktree: reverting one never drops another's commit."""

    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, _floor_monkeypatch):
        """Real git runs unwrapped: these tests are about the revert, not the sandbox."""
        _floor_monkeypatch.setattr(
            git_coord,
            "sandboxed_spawn_argv",
            lambda argv, *a, **k: (list(argv), dict(os.environ), None),
        )

    async def _run_with_commit(self, tmp_path: Path) -> tuple[TaskRun, str]:
        repo = tmp_path / "repo"
        repo.mkdir()
        await git_coord._git(str(repo), "init")
        (repo / "seed.txt").write_text("seed")
        await git_coord._git(str(repo), "add", "-A")
        await git_coord._git(str(repo), "commit", "-m", "init")
        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "cr_ask"
        run.work_dir = str(repo)
        await git_coord.init_workspace(run)
        (Path(run.work_dir) / "rejected.py").write_text("x")
        sha = await git_coord.commit_step(run, Step(index=1, title="A", description="d"))
        return run, sha

    async def _sibling_commits(self, run: TaskRun) -> str:
        (Path(run.work_dir) / "checkpoint.py").write_text("y")
        return await git_coord.commit_step(run, Step(index=2, title="B", description="d"))

    @pytest.mark.asyncio
    async def test_a_sibling_commit_on_top_is_kept(self, tmp_path: Path) -> None:
        run, sha = await self._run_with_commit(tmp_path)
        try:
            sibling = await self._sibling_commits(run)
            await git_coord.revert_step(run, sha)
            head = (await git_coord._git(run.work_dir, "rev-parse", "HEAD")).strip()
            assert head == sibling
            assert run.commit_hashes == [sha, sibling]
        finally:
            await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_a_sibling_committing_mid_revert_is_kept(self, tmp_path: Path) -> None:
        """A sibling committing while the ref moves waits for the revert to finish."""
        run, sha = await self._run_with_commit(tmp_path)
        real_git = git_coord._git
        sibling: list[asyncio.Task[str]] = []

        async def _git(work_dir: str, *args: str) -> str:
            if args[:1] == ("update-ref",) and not sibling:
                sibling.append(asyncio.create_task(self._sibling_commits(run)))
            return await real_git(work_dir, *args)

        try:
            with patch("kiro_crew.git_coord._git", side_effect=_git):
                await git_coord.revert_step(run, sha)
                sibling_sha = await asyncio.wait_for(sibling[0], 30)
            head = (await real_git(run.work_dir, "rev-parse", "HEAD")).strip()
            assert head == sibling_sha
            assert run.commit_hashes == [sibling_sha]
            assert (Path(run.work_dir) / "checkpoint.py").exists()
            assert not (Path(run.work_dir) / "rejected.py").exists()
        finally:
            await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_nothing_is_restored_while_a_sibling_runs(self, tmp_path: Path) -> None:
        """A running sibling's agent may rewrite any path: a revert then keeps the commit."""
        run, sha = await self._run_with_commit(tmp_path)
        work = Path(run.work_dir)
        (work / "seed.txt").unlink()
        deleted = await git_coord.commit_step(run, Step(index=1, title="A", description="d"))
        try:
            async with git_coord.step_running(run, 1), git_coord.step_running(run, 2):
                (work / "seed.txt").write_text("recreated by the sibling")
                (work / "rejected.py").write_text("edited by the sibling")
                await git_coord.revert_step(run, deleted)
            assert (work / "seed.txt").read_text() == "recreated by the sibling"
            assert (work / "rejected.py").read_text() == "edited by the sibling"
            assert run.commit_hashes == [sha, deleted]
        finally:
            await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_a_sibling_waits_for_a_revert_to_start(self, tmp_path: Path) -> None:
        run, sha = await self._run_with_commit(tmp_path)
        real_git = git_coord._git
        started: list[asyncio.Task[None]] = []
        order: list[str] = []

        async def _sibling() -> None:
            async with git_coord.step_running(run, 2):
                order.append("sibling")

        async def _git(work_dir: str, *args: str) -> str:
            if args[:1] == ("update-ref",) and not started:
                started.append(asyncio.create_task(_sibling()))
            out = await real_git(work_dir, *args)
            if "restore" in args:
                order.append("restored")
            return out

        try:
            async with git_coord.step_running(run, 1):
                with patch("kiro_crew.git_coord._git", side_effect=_git):
                    await git_coord.revert_step(run, sha)
                await asyncio.wait_for(started[0], 30)
            assert order == ["restored", "sibling"]
            assert run.commit_hashes == []
        finally:
            await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_a_sibling_writing_after_the_ref_moved_keeps_its_files(
        self, tmp_path: Path
    ) -> None:
        """Only the rejected commit's paths are restored: no reset erases a sibling's edits."""
        run, sha = await self._run_with_commit(tmp_path)
        real_git = git_coord._git

        async def _git(work_dir: str, *args: str) -> str:
            if "restore" in args:
                (Path(run.work_dir) / "checkpoint.py").write_text("y")
            return await real_git(work_dir, *args)

        try:
            with patch("kiro_crew.git_coord._git", side_effect=_git):
                await git_coord.revert_step(run, sha)
            assert (Path(run.work_dir) / "checkpoint.py").read_text() == "y"
            assert not (Path(run.work_dir) / "rejected.py").exists()
            assert run.commit_hashes == []
        finally:
            await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_a_sibling_committing_during_the_restore_stays_recorded(
        self, tmp_path: Path
    ) -> None:
        run, sha = await self._run_with_commit(tmp_path)
        real_git = git_coord._git
        sibling: list[asyncio.Task[str]] = []

        async def _git(work_dir: str, *args: str) -> str:
            if "restore" in args and not sibling:
                sibling.append(asyncio.create_task(self._sibling_commits(run)))
            return await real_git(work_dir, *args)

        try:
            with patch("kiro_crew.git_coord._git", side_effect=_git):
                await git_coord.revert_step(run, sha)
                sibling_sha = await asyncio.wait_for(sibling[0], 30)
            assert run.commit_hashes == [sibling_sha]
        finally:
            await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_a_failed_restore_puts_the_commit_back(self, tmp_path: Path) -> None:
        run, sha = await self._run_with_commit(tmp_path)
        real_git = git_coord._git

        async def _git(work_dir: str, *args: str) -> str:
            if "restore" in args:
                raise RuntimeError("index.lock")
            return await real_git(work_dir, *args)

        try:
            with patch("kiro_crew.git_coord._git", side_effect=_git):
                await git_coord.revert_step(run, sha)
            head = (await real_git(run.work_dir, "rev-parse", "HEAD")).strip()
            assert head == sha
            assert run.commit_hashes == [sha]
        finally:
            await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_its_own_commit_on_top_is_reverted(self, tmp_path: Path) -> None:
        run, sha = await self._run_with_commit(tmp_path)
        try:
            await git_coord.revert_step(run, sha)
            assert run.commit_hashes == []
            assert not (Path(run.work_dir) / "rejected.py").exists()
        finally:
            await git_coord.finalize(run)
