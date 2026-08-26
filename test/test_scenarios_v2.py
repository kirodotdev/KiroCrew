"""Scenario tests for TaskRunner V2 — simulates real task execution patterns.

Tests git_coord directly (no mocking) and taskrunner logic through
carefully constructed scenarios that mirror real user tasks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import requires_git
from kiro_crew import git_coord
from kiro_crew.taskrunner import (
    Step,
    TaskRun,
)

pytestmark = requires_git


@pytest.fixture(autouse=True)
def _passthrough_sandbox(monkeypatch):
    """These scenarios spawn REAL git via ``git_coord._git`` →
    ``sandboxed_spawn_argv`` → ``wrap_argv``, which raises when no OS-level
    sandbox backend is available (e.g. macOS where sandbox-exec is absent, or a
    host with the probe disabled). The scenarios exercise git/taskrunner logic,
    not sandbox availability, so run the command unwrapped in-test. On hosts with
    a real backend this is a no-op relative to what production does."""
    import os as _os

    monkeypatch.setattr(
        git_coord,
        "sandboxed_spawn_argv",
        lambda argv, *a, **k: (list(argv), dict(_os.environ), None),
    )

# ═══════════════════════════════════════════════════════════════════════
# Scenario 1: Multi-step code task in existing git repo (worktree path)
# User runs: "implement a REST API with 3 endpoints"
# Expected: worktree created, 3 commits, worktree cleaned up
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioMultiStepCodeTask:
    @pytest.mark.asyncio
    async def test_full_lifecycle(self, tmp_path: Path) -> None:
        """Simulate: init repo → worktree → 3 step commits → state summary → finalize."""
        repo = tmp_path / "myproject"
        repo.mkdir()
        await git_coord._git(str(repo), "init")
        (repo / "README.md").write_text("# My Project")
        await git_coord._git(str(repo), "add", "-A")
        await git_coord._git(str(repo), "commit", "-m", "initial")

        run = TaskRun(spec_path="/spec.md", spec_content="s")
        run.task_id = "rest_api_task"
        run.work_dir = str(repo)

        # Init — should create worktree
        await git_coord.init_workspace(run)
        assert run.worktree_path != ""
        assert Path(run.work_dir).exists()
        assert run.branch_name == "kirocrew/task/rest_api_task"
        # Original repo should be untouched
        assert (repo / "README.md").read_text(encoding="utf-8") == "# My Project"

        # Step 1: Create models.py
        (Path(run.work_dir) / "models.py").write_text("class User: pass")
        step1 = Step(index=1, title="Create models", description="d")
        sha1 = await git_coord.commit_step(run, step1)
        assert sha1 != ""
        assert len(run.commit_hashes) == 1

        # Step 2: Create routes.py
        (Path(run.work_dir) / "routes.py").write_text("def get_users(): pass")
        step2 = Step(index=2, title="Create routes", description="d")
        sha2 = await git_coord.commit_step(run, step2)
        assert sha2 != ""
        assert sha2 != sha1
        assert len(run.commit_hashes) == 2

        # Step 3: Create tests
        (Path(run.work_dir) / "test_routes.py").write_text("def test_get(): pass")
        step3 = Step(index=3, title="Add tests", description="d")
        sha3 = await git_coord.commit_step(run, step3)  # noqa: F841
        assert len(run.commit_hashes) == 3

        # State summary should show all 3 commits
        summary = await git_coord.get_state_summary(run)
        assert "models.py" in summary
        assert "routes.py" in summary
        assert "test_routes.py" in summary
        assert "Git Log" in summary

        # Step diff should show only last commit
        diff = await git_coord.get_step_diff(run)
        assert "test_routes.py" in diff
        assert "models.py" not in diff  # not in last commit

        # Finalize — worktree removed, branch name returned
        branch = await git_coord.finalize(run)
        assert branch == "kirocrew/task/rest_api_task"
        # Worktree dir should be gone
        assert not Path(run.worktree_path).exists()

        # Branch should exist in original repo
        branches = await git_coord._git(str(repo), "branch", "--list")
        assert "kirocrew/task/rest_api_task" in branches

    @pytest.mark.asyncio
    async def test_user_workdir_untouched_during_task(self, tmp_path: Path) -> None:
        """User's working directory must not be modified by task execution."""
        repo = tmp_path / "userproject"
        repo.mkdir()
        await git_coord._git(str(repo), "init")
        (repo / "app.py").write_text("original content")
        await git_coord._git(str(repo), "add", "-A")
        await git_coord._git(str(repo), "commit", "-m", "init")

        run = TaskRun(spec_path="/s.md", spec_content="s")
        run.task_id = "isolation_test"
        run.work_dir = str(repo)

        await git_coord.init_workspace(run)

        # Modify file in worktree
        (Path(run.work_dir) / "app.py").write_text("MODIFIED BY TASK")
        (Path(run.work_dir) / "new_file.py").write_text("new")

        # User's original should be untouched
        assert (repo / "app.py").read_text(encoding="utf-8") == "original content"
        assert not (repo / "new_file.py").exists()

        await git_coord.finalize(run)


# ═══════════════════════════════════════════════════════════════════════
# Scenario 2: Step fails mid-task → revert → retry succeeds
# User runs: "refactor auth module" — step 2 breaks tests, gets reverted
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioStepFailureAndRevert:
    @pytest.mark.asyncio
    async def test_revert_restores_previous_state(self, tmp_path: Path) -> None:
        """Step 2 fails → revert → file from step 2 gone, step 1 intact."""
        repo = tmp_path / "work"
        repo.mkdir()
        await git_coord._git(str(repo), "init")
        (repo / "seed.txt").write_text("seed")
        await git_coord._git(str(repo), "add", "-A")
        await git_coord._git(str(repo), "commit", "-m", "initial")

        run = TaskRun(spec_path="/s.md", spec_content="s")
        run.task_id = "revert_test"
        run.work_dir = str(repo)

        await git_coord.init_workspace(run)

        # Step 1 succeeds: create auth.py
        (Path(run.work_dir) / "auth.py").write_text("def login(): pass")
        step1 = Step(index=1, title="Create auth", description="d")
        await git_coord.commit_step(run, step1)

        # Step 2 "fails": create broken middleware
        (Path(run.work_dir) / "middleware.py").write_text("BROKEN CODE")
        step2 = Step(index=2, title="Add middleware", description="d")
        await git_coord.commit_step(run, step2)

        # Revert step 2
        await git_coord.revert_step(run)

        # middleware.py should be gone, auth.py should remain
        assert not (Path(run.work_dir) / "middleware.py").exists()
        assert (Path(run.work_dir) / "auth.py").exists()
        assert (Path(run.work_dir) / "auth.py").read_text(encoding="utf-8") == "def login(): pass"
        assert len(run.commit_hashes) == 1

        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_revert_then_new_commit(self, tmp_path: Path) -> None:
        """After revert, a new step can commit cleanly."""
        repo = tmp_path / "work"
        repo.mkdir()
        await git_coord._git(str(repo), "init")
        (repo / "seed.txt").write_text("seed")
        await git_coord._git(str(repo), "add", "-A")
        await git_coord._git(str(repo), "commit", "-m", "initial")

        run = TaskRun(spec_path="/s.md", spec_content="s")
        run.task_id = "revert_retry"
        run.work_dir = str(repo)

        await git_coord.init_workspace(run)

        # Step 1
        (Path(run.work_dir) / "a.py").write_text("a")
        await git_coord.commit_step(run, Step(index=1, title="A", description="d"))

        # Step 2 fails
        (Path(run.work_dir) / "b.py").write_text("broken")
        await git_coord.commit_step(run, Step(index=2, title="B broken", description="d"))
        await git_coord.revert_step(run)

        # Step 2 retry succeeds with different content
        (Path(run.work_dir) / "b.py").write_text("fixed")
        sha = await git_coord.commit_step(run, Step(index=2, title="B fixed", description="d"))
        assert sha != ""
        assert len(run.commit_hashes) == 2
        assert (Path(run.work_dir) / "b.py").read_text(encoding="utf-8") == "fixed"

        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_multiple_reverts(self, tmp_path: Path) -> None:
        """Multiple consecutive reverts work correctly."""
        repo = tmp_path / "work"
        repo.mkdir()
        await git_coord._git(str(repo), "init")
        (repo / "seed.txt").write_text("seed")
        await git_coord._git(str(repo), "add", "-A")
        await git_coord._git(str(repo), "commit", "-m", "initial")

        run = TaskRun(spec_path="/s.md", spec_content="s")
        run.task_id = "multi_revert"
        run.work_dir = str(repo)

        await git_coord.init_workspace(run)

        # 3 commits
        for i in range(1, 4):
            (Path(run.work_dir) / f"file{i}.py").write_text(f"content {i}")
            await git_coord.commit_step(run, Step(index=i, title=f"Step {i}", description="d"))

        assert len(run.commit_hashes) == 3

        # Revert all 3
        await git_coord.revert_step(run)
        assert len(run.commit_hashes) == 2
        assert not (Path(run.work_dir) / "file3.py").exists()

        await git_coord.revert_step(run)
        assert len(run.commit_hashes) == 1
        assert not (Path(run.work_dir) / "file2.py").exists()

        await git_coord.revert_step(run)
        assert len(run.commit_hashes) == 0
        assert not (Path(run.work_dir) / "file1.py").exists()

        # Revert with nothing left — no-op
        await git_coord.revert_step(run)
        assert len(run.commit_hashes) == 0

        await git_coord.finalize(run)


# ═══════════════════════════════════════════════════════════════════════
# Scenario 3: Task in non-git directory (greenfield project)
# User runs: "create a new CLI tool from scratch"
# Expected: git_enabled=False, no git init, runs in place without versioning
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioGreenfieldProject:
    @pytest.mark.asyncio
    async def test_git_init_in_empty_dir(self, tmp_path: Path) -> None:
        """Non-git dir → git_enabled=False, no git init, runs in place."""
        work = tmp_path / "newproject"
        work.mkdir()

        run = TaskRun(spec_path="/s.md", spec_content="s")
        run.task_id = "greenfield"
        run.work_dir = str(work)

        await git_coord.init_workspace(run)

        assert run.git_enabled is False
        assert run.branch_name == ""
        assert run.worktree_path == ""
        assert run.work_dir == str(work)  # unchanged
        assert not (work / ".git").exists()  # no git init

    @pytest.mark.asyncio
    async def test_git_init_with_existing_files(self, tmp_path: Path) -> None:
        """Non-git dir with existing files → git_enabled=False, no git init, files untouched."""
        work = tmp_path / "existing"
        work.mkdir()
        (work / "config.yaml").write_text("key: value")
        (work / "data.json").write_text("{}")

        run = TaskRun(spec_path="/s.md", spec_content="s")
        run.task_id = "existing_files"
        run.work_dir = str(work)

        await git_coord.init_workspace(run)

        assert run.git_enabled is False
        assert run.branch_name == ""
        assert run.worktree_path == ""
        assert run.work_dir == str(work)  # unchanged
        assert not (work / ".git").exists()  # no git init
        # Existing files are untouched
        assert (work / "config.yaml").read_text(encoding="utf-8") == "key: value"
        assert (work / "data.json").read_text(encoding="utf-8") == "{}"


# ═══════════════════════════════════════════════════════════════════════
# Scenario 4: Step produces no file changes (API call, config check)
# User runs: "verify all endpoints return 200"
# Expected: commit_step returns "" (no-op), no empty commits
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioNoFileChanges:
    @pytest.mark.asyncio
    async def test_no_op_steps_dont_create_commits(self, tmp_path: Path) -> None:
        work = tmp_path / "work"
        work.mkdir()

        run = TaskRun(spec_path="/s.md", spec_content="s")
        run.task_id = "noop"
        run.work_dir = str(work)

        await git_coord.init_workspace(run)

        # Step that doesn't change any files
        step = Step(index=1, title="Verify endpoints", description="d")
        sha = await git_coord.commit_step(run, step)
        assert sha == ""
        assert len(run.commit_hashes) == 0

        # State summary should be empty (no changes from base)
        summary = await git_coord.get_state_summary(run)
        assert summary == ""

    @pytest.mark.asyncio
    async def test_mixed_noop_and_real_steps(self, tmp_path: Path) -> None:
        """Some steps change files, some don't — only real changes get commits."""
        repo = tmp_path / "work"
        repo.mkdir()
        await git_coord._git(str(repo), "init")
        (repo / "seed.txt").write_text("seed")
        await git_coord._git(str(repo), "add", "-A")
        await git_coord._git(str(repo), "commit", "-m", "initial")

        run = TaskRun(spec_path="/s.md", spec_content="s")
        run.task_id = "mixed"
        run.work_dir = str(repo)

        await git_coord.init_workspace(run)

        # Step 1: no-op
        sha1 = await git_coord.commit_step(run, Step(index=1, title="Check", description="d"))
        assert sha1 == ""

        # Step 2: real change
        (Path(run.work_dir) / "fix.py").write_text("fixed")
        sha2 = await git_coord.commit_step(run, Step(index=2, title="Fix", description="d"))
        assert sha2 != ""

        # Step 3: no-op
        sha3 = await git_coord.commit_step(run, Step(index=3, title="Verify", description="d"))
        assert sha3 == ""

        assert len(run.commit_hashes) == 1  # only step 2

        await git_coord.finalize(run)


# ═══════════════════════════════════════════════════════════════════════
# Scenario 5: Concurrent tasks on same repo (two worktrees)
# User runs two tasks simultaneously on the same codebase
# Expected: each gets its own worktree, no conflicts
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioConcurrentTasks:
    @pytest.mark.asyncio
    async def test_two_worktrees_same_repo(self, tmp_path: Path) -> None:
        repo = tmp_path / "shared_repo"
        repo.mkdir()
        await git_coord._git(str(repo), "init")
        (repo / "base.py").write_text("base")
        await git_coord._git(str(repo), "add", "-A")
        await git_coord._git(str(repo), "commit", "-m", "init")

        run1 = TaskRun(spec_path="/s.md", spec_content="s")
        run1.task_id = "task_a"
        run1.work_dir = str(repo)

        run2 = TaskRun(spec_path="/s.md", spec_content="s")
        run2.task_id = "task_b"
        run2.work_dir = str(repo)

        await git_coord.init_workspace(run1)
        await git_coord.init_workspace(run2)

        # Both should have separate worktrees
        assert run1.worktree_path != run2.worktree_path
        assert Path(run1.work_dir).exists()
        assert Path(run2.work_dir).exists()

        # Changes in one don't affect the other
        (Path(run1.work_dir) / "task_a.py").write_text("a")
        (Path(run2.work_dir) / "task_b.py").write_text("b")

        assert not (Path(run1.work_dir) / "task_b.py").exists()
        assert not (Path(run2.work_dir) / "task_a.py").exists()

        # Both can commit independently
        await git_coord.commit_step(run1, Step(index=1, title="A work", description="d"))
        await git_coord.commit_step(run2, Step(index=1, title="B work", description="d"))

        assert len(run1.commit_hashes) == 1
        assert len(run2.commit_hashes) == 1

        # Cleanup both
        await git_coord.finalize(run1)
        await git_coord.finalize(run2)


# ═══════════════════════════════════════════════════════════════════════
# Scenario 6: Large file modifications across steps
# User runs: "refactor entire module — rename classes, update imports"
# Expected: each step's diff is isolated, state summary grows
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioLargeRefactor:
    @pytest.mark.asyncio
    async def test_incremental_state_summary(self, tmp_path: Path) -> None:
        repo = tmp_path / "work"
        repo.mkdir()
        await git_coord._git(str(repo), "init")
        (repo / "seed.txt").write_text("seed")
        await git_coord._git(str(repo), "add", "-A")
        await git_coord._git(str(repo), "commit", "-m", "initial")

        run = TaskRun(spec_path="/s.md", spec_content="s")
        run.task_id = "refactor"
        run.work_dir = str(repo)

        await git_coord.init_workspace(run)

        # Step 1: Create 5 files
        for i in range(5):
            (Path(run.work_dir) / f"module{i}.py").write_text(f"class Mod{i}: pass")
        await git_coord.commit_step(run, Step(index=1, title="Create modules", description="d"))

        summary1 = await git_coord.get_state_summary(run)
        assert "module0.py" in summary1

        # Step 2: Modify 3 of them
        for i in range(3):
            (Path(run.work_dir) / f"module{i}.py").write_text(f"class Renamed{i}: pass")
        await git_coord.commit_step(run, Step(index=2, title="Rename classes", description="d"))

        summary2 = await git_coord.get_state_summary(run)
        # Should show both commits in log
        assert "Create modules" in summary2
        assert "Rename classes" in summary2

        # Step diff should only show the rename changes
        diff = await git_coord.get_step_diff(run)
        assert "Renamed" in diff
        assert "module3.py" not in diff  # wasn't modified in step 2

        await git_coord.finalize(run)


# ═══════════════════════════════════════════════════════════════════════
# Scenario 7: Edge cases in git_coord
# ═══════════════════════════════════════════════════════════════════════


class TestScenarioEdgeCases:
    @pytest.mark.asyncio
    async def test_binary_files(self, tmp_path: Path) -> None:
        """Binary files (images, compiled) can be committed."""
        repo = tmp_path / "work"
        repo.mkdir()
        await git_coord._git(str(repo), "init")
        (repo / "seed.txt").write_text("seed")
        await git_coord._git(str(repo), "add", "-A")
        await git_coord._git(str(repo), "commit", "-m", "initial")

        run = TaskRun(spec_path="/s.md", spec_content="s")
        run.task_id = "binary"
        run.work_dir = str(repo)

        await git_coord.init_workspace(run)

        # Write a binary file
        (Path(run.work_dir) / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        sha = await git_coord.commit_step(run, Step(index=1, title="Add image", description="d"))
        assert sha != ""

        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_deeply_nested_files(self, tmp_path: Path) -> None:
        """Files in deep directory structures are tracked."""
        repo = tmp_path / "work"
        repo.mkdir()
        await git_coord._git(str(repo), "init")
        (repo / "seed.txt").write_text("seed")
        await git_coord._git(str(repo), "add", "-A")
        await git_coord._git(str(repo), "commit", "-m", "initial")

        run = TaskRun(spec_path="/s.md", spec_content="s")
        run.task_id = "nested"
        run.work_dir = str(repo)

        await git_coord.init_workspace(run)

        deep = Path(run.work_dir) / "src" / "main" / "java" / "com" / "example"
        deep.mkdir(parents=True)
        (deep / "App.java").write_text("public class App {}")
        sha = await git_coord.commit_step(run, Step(index=1, title="Add App", description="d"))
        assert sha != ""

        summary = await git_coord.get_state_summary(run)
        assert "App.java" in summary

        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_file_deletion_tracked(self, tmp_path: Path) -> None:
        """Deleting files is captured in commits."""
        repo = tmp_path / "work"
        repo.mkdir()
        await git_coord._git(str(repo), "init")
        (repo / "seed.txt").write_text("seed")
        await git_coord._git(str(repo), "add", "-A")
        await git_coord._git(str(repo), "commit", "-m", "initial")

        run = TaskRun(spec_path="/s.md", spec_content="s")
        run.task_id = "delete"
        run.work_dir = str(repo)

        await git_coord.init_workspace(run)

        # Create then delete
        f = Path(run.work_dir) / "temp.py"
        f.write_text("temporary")
        await git_coord.commit_step(run, Step(index=1, title="Create temp", description="d"))

        f.unlink()
        sha = await git_coord.commit_step(run, Step(index=2, title="Delete temp", description="d"))
        assert sha != ""

        diff = await git_coord.get_step_diff(run)
        assert "temp.py" in diff

        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_special_chars_in_commit_message(self, tmp_path: Path) -> None:
        """Step titles with special chars don't break git commit."""
        repo = tmp_path / "work"
        repo.mkdir()
        await git_coord._git(str(repo), "init")
        (repo / "seed.txt").write_text("seed")
        await git_coord._git(str(repo), "add", "-A")
        await git_coord._git(str(repo), "commit", "-m", "initial")

        run = TaskRun(spec_path="/s.md", spec_content="s")
        run.task_id = "special"
        run.work_dir = str(repo)

        await git_coord.init_workspace(run)

        (Path(run.work_dir) / "f.py").write_text("x")
        step = Step(index=1, title='Fix "quotes" & <angles> (parens)', description="d")
        sha = await git_coord.commit_step(run, step)
        assert sha != ""

        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_empty_dir_state_summary(self, tmp_path: Path) -> None:
        """State summary on fresh branch with no commits returns empty."""
        work = tmp_path / "work"
        work.mkdir()

        run = TaskRun(spec_path="/s.md", spec_content="s")
        run.task_id = "empty_summary"
        run.work_dir = str(work)

        await git_coord.init_workspace(run)

        summary = await git_coord.get_state_summary(run)
        assert summary == ""

    @pytest.mark.asyncio
    async def test_get_step_diff_no_commits(self, tmp_path: Path) -> None:
        """get_step_diff with no commits → empty string (not error)."""
        work = tmp_path / "work"
        work.mkdir()

        run = TaskRun(spec_path="/s.md", spec_content="s")
        run.task_id = "no_diff"
        run.work_dir = str(work)

        await git_coord.init_workspace(run)

        diff = await git_coord.get_step_diff(run)
        assert diff == ""  # graceful, no exception

    @pytest.mark.asyncio
    async def test_finalize_without_worktree(self, tmp_path: Path) -> None:
        """finalize on non-worktree run → just returns branch name."""
        run = TaskRun(spec_path="/s.md", spec_content="s")
        run.branch_name = "kirocrew/task/test"
        run.worktree_path = ""

        branch = await git_coord.finalize(run)
        assert branch == "kirocrew/task/test"

    @pytest.mark.asyncio
    async def test_finalize_already_cleaned(self, tmp_path: Path) -> None:
        """finalize when worktree dir already deleted → no error."""
        run = TaskRun(spec_path="/s.md", spec_content="s")
        run.branch_name = "kirocrew/task/test"
        run.worktree_path = str(tmp_path / "nonexistent_worktree")

        branch = await git_coord.finalize(run)
        assert branch == "kirocrew/task/test"  # graceful
