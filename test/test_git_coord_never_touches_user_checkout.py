"""A task run never runs git in the user's own checkout.

``git_coord`` gives a run an isolated worktree on ``kirocrew/task/<id>`` and runs its
step commits and reverts there. Two cases can leave a run with git enabled while
``work_dir`` still names the user's checkout, where ``commit_step``'s ``git add -A``
would commit the user's uncommitted work and ``revert_step``'s ``reset --hard`` would
discard it:

* ``init_workspace`` failing for a git repository (the run keeps the default
  ``git_enabled = True``), and
* a restart after the worktree was added but before the run recorded it, whose
  re-run of ``init_workspace`` failed on the existing branch.

Every test here runs against a scratch repository under ``tmp_path`` and ends by
checking that the user's HEAD, branch and uncommitted files are byte-identical. The
only seam replaced is the OS-sandbox wrapper, which becomes a plain ``git`` call.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from kiro_crew import git_coord
from kiro_crew.task_models import Project, Task

_USER_EDIT = "my uncommitted work\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))

    async def _plain(argv, **_k):
        return list(argv), dict(os.environ), None

    monkeypatch.setattr(git_coord, "sandboxed_spawn_argv_async", _plain)
    root = Path(os.path.realpath(tmp_path)) / "user-repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "user.txt").write_text("committed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "user base")
    # The user's own uncommitted work: a modified tracked file and an untracked one.
    (root / "user.txt").write_text(_USER_EDIT)
    (root / "notes.txt").write_text("untracked notes\n")
    return root


def _read(path: Path) -> bytes:
    return path.read_bytes() if path.exists() else b"<deleted>"


def _user_state(repo: Path) -> tuple[str, str, str, bytes, bytes]:
    return (
        _git(repo, "rev-parse", "HEAD").strip(),
        _git(repo, "branch", "--show-current").strip(),
        _git(repo, "status", "--porcelain"),
        _read(repo / "user.txt"),
        _read(repo / "notes.txt"),
    )


def _run(work_dir: Path, task_id: str = "t-run") -> Project:
    return Project(spec_path="", spec_content="", task_id=task_id, work_dir=str(work_dir))


def _step(run: Project) -> None:
    """One step as the executor runs it: edit, commit, then a revert of that commit."""
    (Path(run.work_dir) / "agent.txt").write_text("agent edit\n")
    asyncio.run(git_coord.commit_step(run, Task(index=1, title="step", description="edit")))
    asyncio.run(git_coord.revert_step(run))


def test_a_restart_after_an_unrecorded_worktree_add_never_touches_the_users_checkout(repo):
    before = _user_state(repo)
    # First start: the worktree is added, then the process stops before the run is recorded.
    asyncio.run(git_coord.init_workspace(_run(repo, "t-crash")))
    # Restart: the reloaded run has no branch_name, so init_workspace runs again; the
    # task runner swallows an error from it and continues.
    run = _run(repo, "t-crash")
    try:
        asyncio.run(git_coord.init_workspace(run))
    except Exception:
        pass
    _step(run)
    assert _user_state(repo) == before, (
        "a restarted task run ran git in the user's own checkout: their HEAD, branch or "
        "uncommitted files changed"
    )
    assert Path(run.work_dir) != repo
    assert run.git_enabled and run.branch_name == "kirocrew/task/t-crash"


def test_a_cancelled_recovery_leaves_nothing_for_finalize_to_remove(repo, monkeypatch):
    """A restart cancelled while recovery probes the leftover worktree.

    ``init_workspace`` names the leftover in ``run.worktree_path`` before it awaits
    ``reinit_workspace_for_retry``. The cancellation propagates, so the task runner's
    ``finalize`` runs next with ``workspace_lost`` still false, and it removes
    ``run.worktree_path`` with ``--force``. The fields must be cleared on the way out,
    or the cancel deletes the leftover and the uncommitted work inside it.
    """
    before = _user_state(repo)
    asyncio.run(git_coord.init_workspace(_run(repo, "t-cancel")))
    leftover = Path(repo).parent / ".kirocrew-work" / "t-cancel"
    (leftover / "draft.txt").write_text("uncommitted work inside the leftover\n")
    real = git_coord._leftover_dir_is_ours

    async def _cancelled_mid_probe(run, path=None):
        # The pause or cancel lands while recovery awaits a git probe.
        raise asyncio.CancelledError()

    monkeypatch.setattr(git_coord, "_leftover_dir_is_ours", _cancelled_mid_probe)
    run = _run(repo, "t-cancel")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(git_coord.init_workspace(run))
    monkeypatch.setattr(git_coord, "_leftover_dir_is_ours", real)

    assert not run.git_enabled
    assert run.worktree_path == "" and run.branch_name == "" and run.repo_root == "", (
        "a cancelled recovery left tentative worktree fields on the run: "
        f"{run.worktree_path!r} {run.branch_name!r} {run.repo_root!r}"
    )
    # What the task runner does next.
    asyncio.run(git_coord.finalize(run))
    assert (leftover / "draft.txt").exists(), "finalize removed the leftover worktree"
    assert _user_state(repo) == before


def test_a_failed_init_leaves_git_disabled(repo, tmp_path):
    before = _user_state(repo)
    # `.kirocrew-work` beside the repository is a file, so `worktree add` cannot create its dir.
    (repo.parent / ".kirocrew-work").write_text("not a directory\n")
    run = _run(repo, "t-noroom")
    with pytest.raises(RuntimeError):
        asyncio.run(git_coord.init_workspace(run))
    assert run.git_enabled is False and Path(run.work_dir) == repo
    assert run.worktree_path == "" and run.branch_name == ""
    _step(run)
    # With git disabled the step's own edit stays as a plain uncommitted file, as for a
    # non-git folder; nothing was committed or reset.
    (repo / "agent.txt").unlink()
    assert _user_state(repo) == before


def test_git_steps_refuse_the_users_checkout_even_with_git_enabled(repo):
    before = _user_state(repo)
    head = before[0]
    run = _run(repo, "t-guard")
    run.git_enabled = True  # the state an earlier failure left behind
    run.commit_hashes = [head]
    (repo / "agent.txt").write_text("agent edit\n")
    sha = asyncio.run(git_coord.commit_step(run, Task(index=1, title="step", description="edit")))
    asyncio.run(git_coord.revert_step(run))
    assert sha == ""
    assert asyncio.run(git_coord.get_step_diff(run)) == ""
    assert asyncio.run(git_coord.get_state_summary(run)) == ""
    (repo / "agent.txt").unlink()
    assert _user_state(repo) == before


def test_control_a_first_start_runs_in_an_isolated_worktree(repo):
    before = _user_state(repo)
    run = _run(repo, "t-first")
    asyncio.run(git_coord.init_workspace(run))
    assert run.git_enabled and Path(run.work_dir) != repo and Path(run.work_dir).is_dir()
    (Path(run.work_dir) / "agent.txt").write_text("agent edit\n")
    sha = asyncio.run(git_coord.commit_step(run, Task(index=1, title="step", description="edit")))
    assert sha and _git(Path(run.work_dir), "rev-parse", "HEAD").strip() == sha
    assert _user_state(repo) == before


def test_control_a_recorded_worktree_that_was_lost_is_recovered(repo):
    before = _user_state(repo)
    run = _run(repo, "t-lost")
    asyncio.run(git_coord.init_workspace(run))
    shutil.rmtree(run.worktree_path)  # the scratch worktree vanishes; the run recorded it
    assert not asyncio.run(git_coord.workspace_is_valid(run))
    assert asyncio.run(git_coord.reinit_workspace_for_retry(run))
    assert Path(run.work_dir) == Path(run.worktree_path) and Path(run.work_dir).is_dir()
    assert _git(Path(run.work_dir), "branch", "--show-current").strip() == run.branch_name
    _step(run)
    assert _user_state(repo) == before


def test_control_a_non_git_project_is_unchanged(tmp_path, monkeypatch):
    async def _plain(argv, **_k):
        return list(argv), dict(os.environ), None

    monkeypatch.setattr(git_coord, "sandboxed_spawn_argv_async", _plain)
    folder = Path(os.path.realpath(tmp_path)) / "plain-folder"
    folder.mkdir()
    (folder / "file.txt").write_text("content\n")
    run = _run(folder, "t-plain")
    asyncio.run(git_coord.init_workspace(run))
    assert run.git_enabled is False and Path(run.work_dir) == folder
    assert asyncio.run(git_coord.commit_step(run, Task(index=1, title="s", description="d"))) == ""
    assert not (folder / ".git").exists()
    assert (folder / "file.txt").read_text() == "content\n"
