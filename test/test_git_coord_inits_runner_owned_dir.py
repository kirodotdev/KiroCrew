"""A runner-owned non-git work dir is ``git init``'d IN PLACE so verification runs.

The task runner's content-aware gate (per-step commit + diff review) is entirely
gated on git. A plain "upload a spec, generate tasks, no workspace override" run
executes in a per-run ``plan_<id>/`` scratch directory the runner created itself
and that is NOT a git repo. Without this behavior a step passes with no diff
review and no test command -- a stub file (a comment and no code) reaching
``PASSED``.

``init_workspace`` ``git init``s such a *runner-owned* directory
(``run.owns_task_dir``) and runs git directly in it (``git_in_place``): the task
branch is checked out IN PLACE, with NO separate worktree, so the deliverable
stays in ``work_dir`` where the completion summary names it and ``finalize``
(which only ever removes a worktree) leaves it alone. A *user-supplied* non-git
folder is never git-init'd (the ``test_git_coord_never_touches_user_checkout``
contract covers that).

The only seam replaced is the OS-sandbox wrapper, which becomes a plain ``git``
call.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from kiro_crew import git_coord
from kiro_crew.task_models import Project, Task


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False
    ).stdout


@pytest.fixture
def plain_sandbox(monkeypatch) -> None:
    async def _plain(argv, **_k):
        return list(argv), dict(os.environ), None

    monkeypatch.setattr(git_coord, "sandboxed_spawn_argv_async", _plain)


@pytest.fixture
def scratch(tmp_path, monkeypatch) -> Path:
    """A runner-owned per-run scratch dir that is NOT a git repo (like plan_<id>/)."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    d = Path(os.path.realpath(tmp_path)) / "plan_run1"
    d.mkdir()
    return d


def _owned_run(work_dir: Path, task_id: str = "t-owned") -> Project:
    return Project(
        spec_path="",
        spec_content="",
        task_id=task_id,
        work_dir=str(work_dir),
        owns_task_dir=True,
    )


def test_a_runner_owned_non_git_dir_is_initialized_in_place(plain_sandbox, scratch):
    run = _owned_run(scratch)
    asyncio.run(git_coord.init_workspace(run))

    assert run.git_enabled, "git coordination should be ON for a runner-owned dir"
    assert run.git_in_place, "a runner-owned dir runs git in place, not in a worktree"
    assert run.worktree_path == "", "an in-place run must create no worktree"
    assert Path(run.work_dir) == scratch, "work_dir must stay the scratch dir (no worktree move)"
    assert run.branch_name == "kirocrew/task/t-owned"
    assert (scratch / ".git").exists()
    assert _git(scratch, "branch", "--show-current").strip() == "kirocrew/task/t-owned"
    assert _git(scratch, "rev-parse", "HEAD").strip(), "no base commit was created"


def test_a_step_commit_lands_in_place_and_is_diff_reviewable(plain_sandbox, scratch):
    run = _owned_run(scratch)
    asyncio.run(git_coord.init_workspace(run))

    (scratch / "module.py").write_text("def real():\n    return 42\n")
    sha = asyncio.run(git_coord.commit_step(run, Task(index=1, title="impl", description="x")))
    assert sha, "a substantive step produced no commit"

    diff = asyncio.run(git_coord.get_step_diff(run))
    assert "def real()" in diff, "the committed content is not visible to review"


def test_finalize_does_not_delete_the_deliverable(plain_sandbox, scratch):
    """The defect this rework fixes: an in-place run's committed output must
    survive ``finalize`` on disk, at the path the run reports."""
    run = _owned_run(scratch)
    asyncio.run(git_coord.init_workspace(run))
    (scratch / "module.py").write_text("def real():\n    return 42\n")
    asyncio.run(git_coord.commit_step(run, Task(index=1, title="impl", description="x")))

    asyncio.run(git_coord.finalize(run))

    assert scratch.exists(), "finalize removed the runner-owned work dir"
    assert (scratch / "module.py").exists(), "finalize deleted the committed deliverable"
    assert (scratch / "module.py").read_text() == "def real():\n    return 42\n"
    assert Path(run.work_dir) == scratch, "the reported work_dir is where the files still are"


def test_an_empty_stub_step_leaves_no_diff_for_review_to_fail_closed_on(plain_sandbox, scratch):
    """A stub-only step adds nothing over the empty base, so there is no diff --
    which is exactly what the self_review empty-diff fail-closed gate keys off."""
    run = _owned_run(scratch)
    asyncio.run(git_coord.init_workspace(run))

    sha = asyncio.run(git_coord.commit_step(run, Task(index=1, title="stub", description="x")))
    assert sha == "", "an empty step must not produce a commit"
    assert asyncio.run(git_coord.get_step_diff(run)) == ""


def test_an_intact_in_place_repo_is_valid_to_resume(plain_sandbox, scratch):
    """A restart re-validates the in-place repo directly (no worktree recovery)."""
    run = _owned_run(scratch)
    asyncio.run(git_coord.init_workspace(run))
    (scratch / "module.py").write_text("x = 1\n")
    sha = asyncio.run(git_coord.commit_step(run, Task(index=1, title="impl", description="x")))

    assert asyncio.run(git_coord.workspace_is_valid(run)) is True
    # Recovery is a worktree operation and must refuse an in-place run.
    assert asyncio.run(git_coord.reinit_workspace_for_retry(run)) is False
    assert sha  # the step did commit


def test_a_user_supplied_non_git_folder_is_never_initialized(plain_sandbox, scratch):
    """owns_task_dir=False (a user workspace override) is left untouched: no git."""
    run = Project(
        spec_path="",
        spec_content="",
        task_id="t-user",
        work_dir=str(scratch),
        owns_task_dir=False,
    )
    asyncio.run(git_coord.init_workspace(run))
    assert run.git_enabled is False
    assert run.git_in_place is False
    assert not (scratch / ".git").exists(), "a user folder must never be git-init'd"
    assert Path(run.work_dir) == scratch


def test_a_failed_init_falls_back_to_no_git(plain_sandbox, scratch, monkeypatch):
    """git init failure is non-fatal: the run continues without git coordination."""

    async def _boom(path, branch):
        return False

    monkeypatch.setattr(git_coord, "_git_init_runner_dir", _boom)
    run = _owned_run(scratch)
    asyncio.run(git_coord.init_workspace(run))
    assert run.git_enabled is False
    assert run.git_in_place is False
    assert Path(run.work_dir) == scratch
