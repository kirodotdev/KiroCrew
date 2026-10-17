"""A task run's git coordination: the run's git fields and the ``git_coord`` module.

A run starts with empty git fields; ``git_coord`` initialises the workspace (a
real repository, or a directory git must not treat as one), says whether a run's
worktree is still that worktree, re-initialises an orphaned one for a retry
without deleting a tree it cannot prove is the run's, commits a step's work and
reverts it (retrying a Windows sharing violation). The ``git_coord`` tests need a
real ``git`` and carry ``requires_git``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from conftest import requires_git
from kiro_crew.taskrunner import Step, TaskRun


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


# ── Phase 2: TaskRun git fields ──


class TestTaskRunGitFields:
    def test_default_git_fields(self) -> None:
        run = TaskRun(spec_path="/t.md", spec_content="s")
        assert run.branch_name == ""
        assert run.base_branch == ""
        assert run.commit_hashes == []
        assert run.worktree_path == ""


# ── Phase 2: git_coord module ──


@requires_git
class TestGitCoord:
    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- class-scoped autouse fixture moved verbatim; no test in its class calls monkeypatch.undo()
    def _passthrough_sandbox(self, monkeypatch):
        """TestGitCoord spawns REAL git via ``git_coord._git`` →
        ``sandboxed_spawn_argv`` → ``wrap_argv``, which raises when no OS-level
        sandbox backend is available. These tests exercise git coordination, not
        sandbox availability, so run the command unwrapped in-test."""
        import os as _os

        from kiro_crew import git_coord

        monkeypatch.setattr(
            git_coord,
            "sandboxed_spawn_argv",
            lambda argv, *a, **k: (list(argv), dict(_os.environ), None),
        )

    @pytest.mark.asyncio
    async def test_init_workspace_no_repo(self, tmp_path: Path, monkeypatch) -> None:
        """init_workspace in a non-git dir → run in place, no git init."""
        from kiro_crew import git_coord

        work_dir = _non_git_dir(tmp_path, monkeypatch, "work")
        (work_dir / "hello.txt").write_text("hi")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "test_123"
        run.work_dir = str(work_dir)

        await git_coord.init_workspace(run)

        # The task runner must NOT impose git on a non-repo folder.
        assert run.git_enabled is False
        assert run.branch_name == ""
        assert run.worktree_path == ""
        assert run.work_dir == str(work_dir)  # runs in place
        assert not (work_dir / ".git").exists()  # no git init

    @pytest.mark.asyncio
    async def test_init_workspace_missing_git_binary(self, tmp_path: Path, monkeypatch) -> None:
        """No ``git`` binary on the host → treated as non-git (run in place), not a crash.

        Locks the git-optional guarantee independently of whether the test host
        actually has git installed, by simulating the missing binary.
        """
        from kiro_crew import git_coord

        async def _no_git(*_a, **_k):
            raise FileNotFoundError(2, "No such file or directory", "git")

        monkeypatch.setattr(git_coord.asyncio, "create_subprocess_exec", _no_git)

        # The probe swallows the missing binary and reports "not a repo".
        assert await git_coord._is_git_repo(str(tmp_path)) is False

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "nogit_123"
        run.work_dir = str(work_dir)

        await git_coord.init_workspace(run)

        assert run.git_enabled is False
        assert run.work_dir == str(work_dir)  # unchanged — no worktree
        assert run.worktree_path == ""

    @pytest.mark.asyncio
    async def test_init_workspace_existing_repo(self, tmp_path: Path) -> None:
        """init_workspace in existing git repo → worktree created."""
        from kiro_crew import git_coord

        # Set up a real git repo
        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "file.txt").write_text("content")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "wt_test"
        run.work_dir = str(work_dir)

        await git_coord.init_workspace(run)

        assert run.branch_name == "kirocrew/task/wt_test"
        assert run.worktree_path != ""
        assert Path(run.worktree_path).exists()
        # work_dir should have been updated to the worktree
        assert run.work_dir == run.worktree_path

        # Cleanup
        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_workspace_is_valid_true_for_a_live_worktree(self, tmp_path: Path) -> None:
        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "file.txt").write_text("content")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "valid_test"
        run.work_dir = str(work_dir)
        await git_coord.init_workspace(run)

        assert await git_coord.workspace_is_valid(run) is True

        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_workspace_is_valid_false_for_a_legacy_run_without_repo_root(
        self, tmp_path: Path
    ) -> None:
        """A run persisted before ``repo_root`` was recorded cannot be fully
        verified or recovered. Path identity alone would accept a DIFFERENT
        repository at the expected path, so validation fails closed and the
        retry is refused with a clear error instead of resuming on a
        workspace whose identity cannot be established."""
        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "file.txt").write_text("content")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "legacy_no_root"
        run.work_dir = str(work_dir)
        await git_coord.init_workspace(run)
        assert await git_coord.workspace_is_valid(run) is True

        # Simulate the legacy persisted shape: everything intact except the
        # recorded repo_root.
        saved_root = run.repo_root
        run.repo_root = ""
        assert await git_coord.workspace_is_valid(run) is False

        run.repo_root = saved_root
        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_workspace_is_valid_true_for_a_run_with_no_worktree(self, tmp_path: Path) -> None:
        """A run that never had a worktree (non-git-folder mode) is trivially
        valid -- there is nothing broken to detect."""
        from kiro_crew import git_coord

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "nogit_valid"
        run.work_dir = str(tmp_path)
        assert run.branch_name == ""

        assert await git_coord.workspace_is_valid(run) is True

    @pytest.mark.asyncio
    async def test_reinit_recovers_an_orphaned_worktree(self, tmp_path: Path) -> None:
        """Reproduce the reported state precisely -- the worktree
        directory still exists on disk, but its registration under the main
        repo's ``.git/worktrees/`` was removed out from under it (what an
        interrupted ``git worktree remove`` leaves behind: deregistration and
        directory deletion are separate steps). ``workspace_is_valid`` must
        catch this (directory-exists alone would miss it), and
        ``reinit_workspace_for_retry`` must recover using the ORIGINAL repo
        (``run.repo_root``) and the run's EXISTING branch -- not fail with
        "branch already exists", which a naive re-``init_workspace()`` call
        would hit."""
        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "file.txt").write_text("content")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "orphan_test"
        run.work_dir = str(work_dir)
        await git_coord.init_workspace(run)
        assert run.git_enabled is True
        worktree_dir = run.worktree_path
        branch = run.branch_name

        # Simulate the orphaned state: deregister the worktree from the main
        # repo's admin metadata directly, WITHOUT removing the directory --
        # the exact "dir exists, `git status` says not a git repo" shape
        # the issue reports, distinct from the directory being deleted.
        worktree_name = Path(worktree_dir).name
        admin_dir = Path(run.repo_root) / ".git" / "worktrees" / worktree_name
        assert admin_dir.exists()
        import shutil

        shutil.rmtree(admin_dir)
        assert Path(worktree_dir).exists()  # directory itself is untouched

        assert await git_coord.workspace_is_valid(run) is False

        recovered = await git_coord.reinit_workspace_for_retry(run)

        assert recovered is True
        assert run.git_enabled is True
        assert run.branch_name == branch  # reused, not a fresh branch
        assert await git_coord.workspace_is_valid(run) is True
        assert (Path(run.work_dir) / "file.txt").exists()  # branch content intact

        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_reinit_fails_closed_when_the_original_repo_is_also_gone(
        self, tmp_path: Path
    ) -> None:
        """If even run.repo_root cannot be recovered from, reinit must
        report failure rather than silently disabling git."""
        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "file.txt").write_text("content")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "gone_test"
        run.work_dir = str(work_dir)
        await git_coord.init_workspace(run)

        import shutil

        from kiro_crew.platform_compat import rmtree_force

        shutil.rmtree(run.worktree_path, ignore_errors=True)
        # Plain shutil.rmtree(..., ignore_errors=False) fails closed on Windows:
        # git writes loose objects under .git/objects read-only, and Windows
        # (unlike POSIX) checks the file's own read-only attribute rather than
        # the parent directory's write bit, so os.unlink raises WinError 5
        # before the repo is actually gone. rmtree_force clears the attribute
        # and retries, and its return value confirms the ORIGINAL repo really
        # is gone -- which this test's premise depends on.
        assert rmtree_force(run.repo_root) is True

        recovered = await git_coord.reinit_workspace_for_retry(run)

        assert recovered is False

    @pytest.mark.asyncio
    async def test_workspace_is_valid_false_when_only_an_enclosing_repo_answers(
        self, tmp_path: Path
    ) -> None:
        """A deregistered worktree whose directory survives INSIDE another
        repository must not read as valid. ``git rev-parse`` walks UP from the
        directory it is run in, so an ordinary directory nested anywhere inside
        an enclosing repo still reports "inside a work tree" -- and resuming on
        that answer would commit every remaining step into the enclosing
        checkout while reporting those steps completed. Validity therefore has
        to be identity (is this MY worktree?), not mere repo-ness."""
        from kiro_crew import git_coord

        enclosing = tmp_path / "enclosing"
        enclosing.mkdir()
        await git_coord._git(str(enclosing), "init")
        (enclosing / "file.txt").write_text("content")
        await git_coord._git(str(enclosing), "add", "-A")
        await git_coord._git(str(enclosing), "commit", "-m", "init")

        # The run's worktree was deregistered and its contents cleaned up, but
        # the directory itself survived -- and it sits inside `enclosing`.
        stale = enclosing / ".kirocrew-work" / "nested_test"
        stale.mkdir(parents=True)

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "nested_test"
        run.work_dir = str(stale)
        run.worktree_path = str(stale)
        run.repo_root = str(enclosing)
        run.branch_name = "kirocrew/task/nested_test"
        run.git_enabled = True

        # Precondition, and the whole point: git DOES report this directory as
        # being inside a work tree -- the ENCLOSING one. A repo-ness check
        # answers True here, which is why it cannot be the validity test.
        assert await git_coord._is_git_repo(str(stale)) is True

        assert await git_coord.workspace_is_valid(run) is False

    @pytest.mark.asyncio
    async def test_workspace_is_valid_false_for_a_different_repo_at_the_same_path(
        self, tmp_path: Path
    ) -> None:
        """A DIFFERENT repository standing at the run's worktree path must not
        read as valid.

        Matching paths are not identity: something else can create a repo
        exactly where the lost worktree stood, and then
        ``rev-parse --show-toplevel`` answers with that path -- the expected
        one. Resuming on that would commit the run's remaining steps into a
        repository that is not the run's own. A linked worktree shares its main
        repository's git directory, so the common git dir is what actually
        distinguishes them."""
        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "file.txt").write_text("content")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "impostor_test"
        run.work_dir = str(work_dir)
        await git_coord.init_workspace(run)
        worktree_dir = run.worktree_path
        assert await git_coord.workspace_is_valid(run) is True

        # Replace the worktree with an unrelated repository at the SAME path.
        await git_coord.finalize(run)
        from kiro_crew.platform_compat import rmtree_force

        rmtree_force(worktree_dir)
        Path(worktree_dir).mkdir(parents=True)
        await git_coord._git(worktree_dir, "init")
        (Path(worktree_dir) / "impostor.txt").write_text("not ours")
        await git_coord._git(worktree_dir, "add", "-A")
        await git_coord._git(worktree_dir, "commit", "-m", "impostor")

        # Precondition, and the whole point: the path check alone is satisfied
        # -- git reports this very directory as the repository root -- so only
        # the git-common-dir comparison can tell the impostor apart.
        toplevel = (await git_coord._git(worktree_dir, "rev-parse", "--show-toplevel")).strip()
        assert git_coord._same_dir(toplevel, worktree_dir) is True

        assert await git_coord.workspace_is_valid(run) is False

    @pytest.mark.asyncio
    async def test_workspace_is_valid_false_for_a_worktree_on_the_wrong_branch(
        self, tmp_path: Path
    ) -> None:
        """Repository identity is not worktree identity: a worktree of THIS
        repo, checked out on a DIFFERENT branch at the run's own path, must
        not read as valid -- resuming would commit the run's remaining steps
        onto somebody else's branch while reporting success."""
        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "file.txt").write_text("content")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "wrong_branch_test"
        run.work_dir = str(work_dir)
        await git_coord.init_workspace(run)
        assert await git_coord.workspace_is_valid(run) is True

        # Move the worktree onto a different branch of the SAME repo -- path
        # and repo identity both still match; only the branch differs.
        await git_coord._git(run.work_dir, "checkout", "-b", "someone-elses-branch")

        assert await git_coord.workspace_is_valid(run) is False

        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_git_probe_propagates_a_transient_spawn_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A TRANSIENT spawn failure must propagate, not be read as "non-git".

        ``EMFILE`` / ``ENOMEM`` / ``EAGAIN`` are ``OSError`` too, but they say
        nothing about whether the directory is a repository. Answering False to
        them would report a real repo as non-git, and ``init_workspace`` would
        then set ``git_enabled = False`` -- running every step directly against
        the user's own checkout, with no worktree isolation and no per-step
        commits. So the guard names only the two "it is not there" errors and
        lets the rest fail the run."""
        import errno

        from kiro_crew import git_coord

        async def exhausted(*args: object, **kwargs: object) -> object:
            raise OSError(errno.EMFILE, "Too many open files")

        monkeypatch.setattr(git_coord, "create_subprocess_limited", exhausted)

        with pytest.raises(OSError) as excinfo:
            await git_coord._is_git_repo(str(tmp_path))
        assert excinfo.value.errno == errno.EMFILE

    @pytest.mark.asyncio
    async def test_git_probe_fails_closed_when_the_directory_itself_is_gone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Probing a directory that does not exist must read as "not a git
        repo", never raise.

        The two platforms disagree on how the spawn fails: POSIX reports a
        missing cwd as ``FileNotFoundError``, while Windows raises
        ``NotADirectoryError`` (WinError 267) out of ``CreateProcess``. Guarding
        only ``FileNotFoundError`` therefore failed closed on POSIX and
        propagated on Windows -- exactly the lost-worktree state the retry path
        exists to detect. Both are ``OSError``, which is what the guard tests.
        """
        from kiro_crew import git_coord

        async def refuse_to_spawn(*args: object, **kwargs: object) -> object:
            raise NotADirectoryError(267, "The directory name is invalid")

        monkeypatch.setattr(git_coord, "create_subprocess_limited", refuse_to_spawn)

        assert await git_coord._is_git_repo(str(tmp_path / "gone")) is False

        # And the retry path built on it fails closed rather than propagating.
        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "spawn_gone"
        run.repo_root = str(tmp_path / "gone")
        run.work_dir = str(tmp_path / "gone")
        run.branch_name = "kirocrew/task/spawn_gone"

        assert await git_coord.reinit_workspace_for_retry(run) is False

    @pytest.mark.asyncio
    async def test_recovery_revalidates_the_captured_tree_before_deleting_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ownership must hold for the tree that is actually deleted.

        The proof runs against the saved path; the capture then renames whatever
        occupies that path at rename time. Those are two different moments, so a
        directory swapped in between them would be captured and destroyed on the
        strength of a proof about a different tree. Recovery therefore proves
        ownership a SECOND time, against the captured name, and this pins that:
        the swap is injected in exactly that gap, and its data must survive.
        """
        import shutil as _shutil

        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "file.txt").write_text("content")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "capture_swap"
        run.work_dir = str(work_dir)
        await git_coord.init_workspace(run)
        worktree_dir = Path(run.worktree_path)

        # The orphaned state: deregistered, checkout still on disk, so the first
        # proof legitimately passes and recovery proceeds to capture.
        admin_dir = Path(run.repo_root) / ".git" / "worktrees" / worktree_dir.name
        assert admin_dir.exists()
        _shutil.rmtree(admin_dir)

        real_proof = git_coord._leftover_dir_is_ours
        treasure = worktree_dir / "not-ours.txt"
        swapped = False

        async def proving_then_swapping(run_arg: object, path: object = None) -> bool:
            """Pass the first proof, then swap the path out from under it."""
            nonlocal swapped
            verdict = await real_proof(run_arg, path)
            if not swapped and path is None:
                swapped = True
                if worktree_dir.exists():
                    _shutil.rmtree(worktree_dir)
                worktree_dir.mkdir(parents=True)
                treasure.write_text("someone else's data")
            return verdict

        monkeypatch.setattr(git_coord, "_leftover_dir_is_ours", proving_then_swapping)

        recovered = await git_coord.reinit_workspace_for_retry(run)

        assert swapped, "the post-proof gap was never exercised"
        assert treasure.exists(), (
            "recovery deleted the tree it captured without re-proving ownership "
            "of THAT tree -- a post-proof swap was destroyed"
        )
        assert treasure.read_text() == "someone else's data"
        assert recovered is False
        # The stranger's directory is put back, not left under a stale name.
        assert not list(worktree_dir.parent.glob("*.kirocrew-stale-*"))

    @pytest.mark.asyncio
    async def test_recovery_never_deletes_a_tree_swapped_in_after_the_proof(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The destructive step must act on a CAPTURED tree, not on a path it
        re-resolves after awaiting git.

        Ownership is proved against ``run.worktree_path``, and a ``git``
        subprocess is awaited before anything is deleted. A delete that
        re-resolved that path would destroy whatever occupied it by then -- not
        the tree the proof actually validated. This plants an unrelated
        directory at the saved path from INSIDE that awaited git call, which is
        the exact window, and asserts its contents survive.
        """
        import shutil as _shutil

        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "file.txt").write_text("content")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "swap_race"
        run.work_dir = str(work_dir)
        await git_coord.init_workspace(run)
        worktree_dir = Path(run.worktree_path)

        # The orphaned state: deregistered, but the checkout survives on disk, so
        # the ownership proof passes and recovery proceeds to delete.
        admin_dir = Path(run.repo_root) / ".git" / "worktrees" / worktree_dir.name
        assert admin_dir.exists()
        _shutil.rmtree(admin_dir)
        assert worktree_dir.exists()

        real_git = git_coord._git
        treasure = worktree_dir / "not-ours.txt"
        planted = False

        async def racing_git(cwd: str, *args: str) -> str:
            """Swap an unrelated directory onto the saved path mid-recovery --
            after the capture (and its proofs), immediately before the
            ``worktree add`` resolves the saved path again."""
            nonlocal planted
            if not planted and args[:2] == ("worktree", "add"):
                planted = True
                if worktree_dir.exists():
                    _shutil.rmtree(worktree_dir)
                worktree_dir.mkdir(parents=True)
                treasure.write_text("someone else's data")
            return await real_git(cwd, *args)

        monkeypatch.setattr(git_coord, "_git", racing_git)

        recovered = await git_coord.reinit_workspace_for_retry(run)

        assert planted, "the race window was never exercised"
        assert treasure.exists(), (
            "recovery deleted a directory that only appeared AFTER the "
            "ownership proof -- the delete re-resolved the saved path"
        )
        assert treasure.read_text() == "someone else's data"
        # The saved path is occupied by something that is not ours, so the
        # re-add must fail rather than clobber it.
        assert recovered is False

    @pytest.mark.asyncio
    async def test_reinit_parks_the_stale_checkout_instead_of_deleting_it(
        self, tmp_path: Path
    ) -> None:
        """Recovery PARKS the captured leftover; nothing deletes it.

        The only evidence an orphaned checkout can offer is a plain-text
        ``.git`` pointer file a forgery reproduces exactly, so no leftover
        that reaches the destructive step carries evidence strong enough to
        authorize destruction. The rename is what frees the path for the
        re-add; the tree survives on disk under the set-aside name.
        """
        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "file.txt").write_text("content")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "park_test"
        run.work_dir = str(work_dir)
        await git_coord.init_workspace(run)
        worktree_dir = Path(run.worktree_path)
        (worktree_dir / "scratch.txt").write_text("uncommitted scratch")

        # The orphaned state: deregistered, directory intact.
        import shutil

        admin_dir = Path(run.repo_root) / ".git" / "worktrees" / worktree_dir.name
        assert admin_dir.exists()
        shutil.rmtree(admin_dir)

        assert await git_coord.reinit_workspace_for_retry(run) is True

        parked = list(worktree_dir.parent.glob(f"{worktree_dir.name}.kirocrew-stale-*"))
        assert parked, "the captured leftover was deleted instead of parked"
        assert (parked[0] / "scratch.txt").read_text() == "uncommitted scratch"

        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_orphaned_gitdir_reader_rejects_unc_and_uncontained_targets(
        self, tmp_path: Path
    ) -> None:
        """The pointer target is attacker-writable text: no spelling of it may
        reach a filesystem probe unless it is lexically an entry directly
        under the repository's own ``worktrees`` directory. On Windows, a mere
        ``exists()`` on a UNC target performs outbound SMB authentication --
        a credential exposure -- so UNC/device forms are rejected on the raw
        string before any syscall."""
        from kiro_crew import git_coord

        worktrees_dir = str(tmp_path / "repo" / ".git" / "worktrees")
        tree = tmp_path / "leftover"
        tree.mkdir()

        def forge(target: str) -> str:
            (tree / ".git").write_text(f"gitdir: {target}\n")
            return git_coord._orphaned_worktree_gitdir(str(tree), worktrees_dir)

        # UNC and device spellings: rejected on the raw string.
        assert forge(r"\\attacker-host\share\worktrees\x") == ""
        assert forge("//attacker-host/share/worktrees/x") == ""
        assert forge(r"\\.\pipe\evil") == ""
        assert forge(r"\\?\C:\anything") == ""
        # Local but outside the repo's worktrees dir: rejected lexically.
        assert forge(str(tmp_path / "elsewhere" / "wt")) == ""
        # Dot-dot escape normalizing outside the worktrees dir: rejected.
        assert forge(f"{worktrees_dir}/../../../elsewhere") == ""
        # A genuine dangling entry directly under the worktrees dir: accepted.
        # Compare normalized: on Windows the reader returns backslash paths.
        import os as _os

        genuine = f"{worktrees_dir}/gone-entry"
        assert _os.path.normcase(_os.path.normpath(forge(genuine))) == _os.path.normcase(
            _os.path.normpath(genuine)
        )

    @pytest.mark.asyncio
    async def test_recovery_metadata_readers_never_follow_links(self, tmp_path: Path) -> None:
        """The recovery-path invariant: no filesystem operation on the
        worktree-recovery path ever follows a link. A planted symlink -- as
        the ``.git`` pointer leaf, as a ``worktrees`` entry, or as the
        pointer's target leaf -- is refused or skipped, never dereferenced.
        (On Windows the same applies to junctions/reparse points.)"""
        import os as _os

        from kiro_crew import git_coord

        if not hasattr(_os, "symlink"):
            pytest.skip("no symlink support on this platform")

        # 1. A symlinked .git leaf is refused by the no-follow open even
        #    though a symlink can masquerade via follow-based reads.
        tree = tmp_path / "leftover"
        tree.mkdir()
        real_pointer = tmp_path / "elsewhere.txt"
        worktrees_dir = str(tmp_path / "repo" / ".git" / "worktrees")
        real_pointer.write_text(f"gitdir: {worktrees_dir}/gone\n")
        try:
            _os.symlink(str(real_pointer), str(tree / ".git"))
        except OSError:
            pytest.skip("cannot create symlinks in this environment")
        assert git_coord._orphaned_worktree_gitdir(str(tree), worktrees_dir) == ""

        # 2. A symlinked entry inside the worktrees dir is skipped, not
        #    traversed, by _run_worktree_admin_dir.
        wt_dir = tmp_path / "wt"
        real_entry_home = tmp_path / "outside-entry"
        real_entry_home.mkdir()
        (real_entry_home / "gitdir").write_text(f"{wt_dir}/.git\n")
        admin = tmp_path / "admin" / "worktrees"
        admin.mkdir(parents=True)
        _os.symlink(str(real_entry_home), str(admin / "linked-entry"))
        assert git_coord._run_worktree_admin_dir(str(admin), str(wt_dir)) == ""

        # 3. A symlink LEAF at the pointer target reads as existing via the
        #    no-follow probe, so the pointer is rejected as evidence -- the
        #    link is never dereferenced.
        tree2 = tmp_path / "leftover2"
        tree2.mkdir()
        real_admin = tmp_path / "repo2" / ".git" / "worktrees"
        real_admin.mkdir(parents=True)
        _os.symlink(str(tmp_path / "no-such-place"), str(real_admin / "link-leaf"))
        (tree2 / ".git").write_text(f"gitdir: {real_admin}/link-leaf\n")
        assert git_coord._orphaned_worktree_gitdir(str(tree2), str(real_admin)) == ""

        # 4. Validation happens on the OPENED handle (atomic -- no
        #    check-then-open window): a non-regular file is refused by the
        #    fstat of the handle itself, and a symlink leaf is refused by the
        #    open, never dereferenced.
        with pytest.raises(OSError):
            with git_coord._open_nofollow_text(str(tmp_path)):  # a directory
                pass
        link_file = tmp_path / "leaf-link"
        _os.symlink(str(real_pointer), str(link_file))
        with pytest.raises(OSError):
            with git_coord._open_nofollow_text(str(link_file)):
                pass

    @pytest.mark.asyncio
    async def test_reinit_refuses_to_delete_a_foreign_repo_at_the_worktree_path(
        self, tmp_path: Path
    ) -> None:
        """A completely different, unrelated repository sitting at the run's
        worktree path must not be recursively deleted during recovery --
        that would be silent, unrecoverable data loss for whatever that
        repository holds. Recovery must refuse and fail the run instead."""
        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "file.txt").write_text("content")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "foreign_repo_test"
        run.work_dir = str(work_dir)
        await git_coord.init_workspace(run)
        worktree_dir = run.worktree_path

        # Replace the worktree with an unrelated, independent repository at
        # the SAME path -- exactly the shape a foreign occupant would take.
        await git_coord.finalize(run)
        from kiro_crew.platform_compat import rmtree_force

        rmtree_force(worktree_dir)
        Path(worktree_dir).mkdir(parents=True)
        await git_coord._git(worktree_dir, "init")
        (Path(worktree_dir) / "precious.txt").write_text("someone else's data")
        await git_coord._git(worktree_dir, "add", "-A")
        await git_coord._git(worktree_dir, "commit", "-m", "unrelated")

        recovered = await git_coord.reinit_workspace_for_retry(run)

        assert recovered is False
        assert (
            Path(worktree_dir) / "precious.txt"
        ).exists(), "recovery deleted a repository it does not own"

    @pytest.mark.asyncio
    async def test_reinit_refuses_to_delete_an_arbitrary_non_git_directory(
        self, tmp_path: Path
    ) -> None:
        """A non-Git directory is not evidence of an orphaned worktree.

        The retry path may only remove a broken checkout that still carries
        Git's pointer back to this run's original repository.  A replacement
        directory with unrelated files must survive byte-for-byte.
        """
        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "file.txt").write_text("content")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "non_git_replacement"
        run.work_dir = str(work_dir)
        await git_coord.init_workspace(run)
        worktree_dir = Path(run.worktree_path)

        await git_coord.finalize(run)
        worktree_dir.mkdir(parents=True)
        precious = worktree_dir / "precious.txt"
        precious.write_bytes(b"someone else's bytes\x00")

        assert await git_coord.reinit_workspace_for_retry(run) is False
        assert precious.read_bytes() == b"someone else's bytes\x00"

    @pytest.mark.asyncio
    async def test_reinit_refuses_to_delete_the_same_repo_on_another_branch(
        self, tmp_path: Path
    ) -> None:
        """Repository identity alone does not authorize destructive recovery."""
        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "file.txt").write_text("content")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "wrong_branch_replacement"
        run.work_dir = str(work_dir)
        await git_coord.init_workspace(run)
        worktree_dir = Path(run.worktree_path)
        await git_coord._git(run.work_dir, "checkout", "-b", "someone-elses-branch")
        precious = worktree_dir / "precious.txt"
        precious.write_text("branch owner's data")

        assert await git_coord.workspace_is_valid(run) is False
        assert await git_coord.reinit_workspace_for_retry(run) is False
        assert precious.read_text() == "branch owner's data"

        await git_coord._git(str(worktree_dir), "checkout", run.branch_name)
        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_reinit_never_deletes_a_tree_with_a_forged_worktree_pointer(
        self, tmp_path: Path
    ) -> None:
        """A forged ``.git`` pointer must not authorize deletion.

        The stale-checkout proof accepts a regular ``.git`` file naming a
        MISSING entry under the repository's ``worktrees`` directory -- but
        that file is plain text anyone can write, and a forged pointer to a
        nonexistent entry is indistinguishable from a genuine one (a missing
        target is exactly what makes a genuine pointer stale). Evidence that
        weak may move the tree aside so recovery can proceed; it must never
        destroy the tree's contents. This forges such a pointer over an
        unrelated directory at the run's worktree path and asserts the
        contents survive recovery on disk.
        """
        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "file.txt").write_text("content")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "forged_pointer"
        run.work_dir = str(work_dir)
        await git_coord.init_workspace(run)
        worktree_dir = Path(run.worktree_path)

        # Replace the worktree with an unrelated directory carrying a FORGED
        # pointer: a regular ``.git`` file naming a nonexistent entry under
        # the real repository's ``worktrees`` directory. The ownership checks
        # accept it as a stale checkout, which on its own would authorize deletion.
        await git_coord.finalize(run)
        worktree_dir.mkdir(parents=True)
        forged_target = Path(run.repo_root) / ".git" / "worktrees" / "no-such-entry"
        assert not forged_target.exists()
        (worktree_dir / ".git").write_text(f"gitdir: {forged_target}\n")
        precious = worktree_dir / "precious.txt"
        precious.write_text("someone else's data")

        recovered = await git_coord.reinit_workspace_for_retry(run)

        # The forged tree may be parked aside so recovery can proceed, but
        # its contents must survive SOMEWHERE on disk -- at the saved path or
        # under the private set-aside name.
        survivors = [
            candidate / "precious.txt"
            for candidate in (
                [worktree_dir]
                + list(worktree_dir.parent.glob(f"{worktree_dir.name}.kirocrew-stale-*"))
            )
            if (candidate / "precious.txt").exists()
        ]
        assert survivors, (
            "recovery deleted a tree whose only ownership evidence was a "
            "forged .git pointer to a nonexistent worktrees entry"
        )
        assert survivors[0].read_text() == "someone else's data"
        if recovered:
            await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_workspace_is_valid_false_when_the_branch_lost_the_runs_commits(
        self, tmp_path: Path
    ) -> None:
        """Same branch NAME is not the same HISTORY.

        A branch force-moved after the run's steps committed keeps its name
        while dropping those commits. Resuming on it would leave the earlier
        steps marked passed although their work is gone, so every remaining
        step builds on the wrong tree and reports success. The run's own
        commit ledger is the ground truth the branch tip must still descend
        from.
        """
        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "file.txt").write_text("content")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "rewritten_branch"
        run.work_dir = str(work_dir)
        await git_coord.init_workspace(run)

        (Path(run.work_dir) / "step.py").write_text("print('step')")
        step = Step(index=1, title="Add step.py", description="d")
        assert await git_coord.commit_step(run, step) != ""
        assert await git_coord.workspace_is_valid(run) is True

        # Rewind the branch past the run's recorded commit -- the shape a
        # force-move leaves behind: same name, the run's commit gone.
        await git_coord._git(run.work_dir, "reset", "--hard", "HEAD~1")

        assert await git_coord.workspace_is_valid(run) is False

        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_reinit_refuses_to_re_add_a_branch_that_lost_the_runs_commits(
        self, tmp_path: Path
    ) -> None:
        """Recovery must not re-add a branch whose history was rewritten.

        ``worktree add`` by branch NAME would resume the run on whatever the
        branch now points at; with the run's recorded commits gone, earlier
        steps stay marked passed while their output is absent. Recovery must
        refuse -- and refuse BEFORE touching the leftover tree on disk.
        """
        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "file.txt").write_text("content")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "rewritten_reinit"
        run.work_dir = str(work_dir)
        await git_coord.init_workspace(run)
        worktree_dir = Path(run.worktree_path)

        (Path(run.work_dir) / "step.py").write_text("print('step')")
        step = Step(index=1, title="Add step.py", description="d")
        assert await git_coord.commit_step(run, step) != ""

        # Rewind the branch past the run's commit, then orphan the checkout
        # (deregistered admin entry) so retry lands in recovery.
        await git_coord._git(run.work_dir, "reset", "--hard", "HEAD~1")
        marker = worktree_dir / "leftover-marker.txt"
        marker.write_text("still here")
        admin_dir = Path(run.repo_root) / ".git" / "worktrees" / worktree_dir.name
        assert admin_dir.exists()
        import shutil

        shutil.rmtree(admin_dir)

        assert await git_coord.reinit_workspace_for_retry(run) is False
        # Refused before any capture: the leftover sits untouched at its path.
        assert marker.exists()
        assert marker.read_text() == "still here"

    @pytest.mark.asyncio
    async def test_reinit_rejects_a_branch_rewritten_during_the_re_add(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The re-added worktree is validated AFTER ``worktree add``.

        Every history check before the add reasons about the branch as it was
        THEN; a branch force-updated inside that window is checked out by the
        add with the run's earlier commits gone, and those steps would stay
        marked passed. Recovery must revalidate the checkout it actually
        produced and fail closed on mismatch.
        """
        import shutil as _shutil

        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "file.txt").write_text("content")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")
        base_sha = (await git_coord._git(str(work_dir), "rev-parse", "HEAD")).strip()

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "midadd_rewrite"
        run.work_dir = str(work_dir)
        await git_coord.init_workspace(run)
        worktree_dir = Path(run.worktree_path)

        (Path(run.work_dir) / "step.py").write_text("print('step')")
        step = Step(index=1, title="Add step.py", description="d")
        assert await git_coord.commit_step(run, step) != ""

        # The orphaned state: checkout on disk, admin entry gone. The
        # branch is still intact here, so every PRE-add check passes.
        admin_dir = Path(run.repo_root) / ".git" / "worktrees" / worktree_dir.name
        assert admin_dir.exists()
        _shutil.rmtree(admin_dir)

        real_git = git_coord._git
        rewrote = False

        async def racing_git(cwd: str, *args: str) -> str:
            """Force-move the branch from INSIDE the worktree-add call --
            after every precheck, before the checkout lands."""
            nonlocal rewrote
            if not rewrote and args[:2] == ("worktree", "add"):
                rewrote = True
                await real_git(
                    run.repo_root,
                    "update-ref",
                    f"refs/heads/{run.branch_name}",
                    base_sha,
                )
            return await real_git(cwd, *args)

        monkeypatch.setattr(git_coord, "_git", racing_git)

        recovered = await git_coord.reinit_workspace_for_retry(run)

        assert rewrote, "the add-window race was never exercised"
        assert recovered is False, (
            "recovery accepted a worktree whose branch was force-updated "
            "between the prechecks and the add"
        )

    @pytest.mark.asyncio
    async def test_commit_and_revert(self, tmp_path: Path) -> None:
        """commit_step creates commit, revert_step undoes it."""
        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "seed.txt").write_text("seed")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "cr_test"
        run.work_dir = str(work_dir)

        await git_coord.init_workspace(run)  # git repo → worktree, git_enabled=True

        # Create a file and commit
        (Path(run.work_dir) / "new.py").write_text("print('hello')")
        step = Step(index=1, title="Add new.py", description="d")
        sha = await git_coord.commit_step(run, step)
        assert sha != ""
        assert len(run.commit_hashes) == 1

        # Revert
        await git_coord.revert_step(run)
        assert len(run.commit_hashes) == 0
        assert not (Path(run.work_dir) / "new.py").exists()

        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_commit_no_changes(self, tmp_path: Path) -> None:
        """commit_step with no changes → empty string."""
        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "seed.txt").write_text("seed")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "nc_test"
        run.work_dir = str(work_dir)

        await git_coord.init_workspace(run)

        step = Step(index=1, title="No-op", description="d")
        sha = await git_coord.commit_step(run, step)
        assert sha == ""

        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_non_git_workspace_git_ops_are_noops(self, tmp_path: Path, monkeypatch) -> None:
        """A non-git workspace runs in place; all git helpers are safe no-ops."""
        from kiro_crew import git_coord

        work_dir = _non_git_dir(tmp_path, monkeypatch, "plain")
        (work_dir / "a.py").write_text("x = 1")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "plain_test"
        run.work_dir = str(work_dir)

        await git_coord.init_workspace(run)
        assert run.git_enabled is False

        step = Step(index=1, title="Edit", description="d")
        assert await git_coord.commit_step(run, step) == ""  # no commit
        await git_coord.revert_step(run)  # no-op, no raise
        assert await git_coord.get_state_summary(run) == ""
        assert await git_coord.get_step_diff(run) == ""
        assert await git_coord.finalize(run) == ""  # nothing to clean
        # The file the "step" would have created is untouched (no git reset).
        assert (work_dir / "a.py").exists()

        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_get_state_summary(self, tmp_path: Path) -> None:
        """get_state_summary returns git log + diff stat."""
        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        (work_dir / "seed.txt").write_text("seed")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "ss_test"
        run.work_dir = str(work_dir)

        await git_coord.init_workspace(run)

        (Path(run.work_dir) / "foo.py").write_text("x = 1")
        step = Step(index=1, title="Add foo", description="d")
        await git_coord.commit_step(run, step)

        summary = await git_coord.get_state_summary(run)
        assert "Git Log" in summary
        assert "foo.py" in summary

        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_revert_no_commits(self, tmp_path: Path) -> None:
        """revert_step with no commits → no-op."""
        from kiro_crew import git_coord

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.work_dir = str(tmp_path)
        run.commit_hashes = []

        await git_coord.revert_step(run)  # should not raise

    @pytest.mark.asyncio
    async def test_revert_success_pops_one_and_reverts_worktree(
        self, tmp_path: Path, caplog
    ) -> None:
        """Baseline unchanged: a successful reset reverts the tree, pops exactly
        one commit hash, and emits no log line at default (non-debug) levels."""
        import logging

        from kiro_crew import git_coord

        work_dir = tmp_path / "repo"
        work_dir.mkdir()
        await git_coord._git(str(work_dir), "init")
        tracked = work_dir / "tracked.txt"
        tracked.write_text("v1")
        await git_coord._git(str(work_dir), "add", "-A")
        await git_coord._git(str(work_dir), "commit", "-m", "init")

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.task_id = "revert_ok"
        run.work_dir = str(work_dir)
        await git_coord.init_workspace(run)

        # Commit a second version of the tracked file through commit_step.
        (Path(run.work_dir) / "tracked.txt").write_text("v2")
        step = Step(index=1, title="change", description="d")
        sha = await git_coord.commit_step(run, step)
        assert sha
        assert run.commit_hashes == [sha]
        assert (Path(run.work_dir) / "tracked.txt").read_text() == "v2"

        with caplog.at_level(logging.WARNING, logger="kiro_crew.git_coord"):
            await git_coord.revert_step(run)

        assert run.commit_hashes == []  # exactly one popped
        assert (Path(run.work_dir) / "tracked.txt").read_text() == "v1"  # reverted
        assert caplog.records == []  # silent at default levels

        await git_coord.finalize(run)

    @pytest.mark.asyncio
    async def test_revert_retries_a_windows_sharing_violation_then_succeeds(
        self, monkeypatch, caplog
    ) -> None:
        """On Windows a held-handle unlink failure is transient; revert_step
        retries the bounded window and, once the handle clears, pops normally."""
        import logging

        from kiro_crew import git_coord

        monkeypatch.setattr(git_coord.platform_compat, "IS_WINDOWS", True)
        # flake-ok: pre-existing rebind of the stdlib asyncio.sleep through git_coord.asyncio for one test, undone at teardown, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(git_coord.asyncio, "sleep", AsyncMock())  # no real delay

        calls = {"n": 0}

        async def _flaky_git(work_dir, *args):
            if args[:2] == ("reset", "--hard"):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise RuntimeError(
                        "git reset --hard HEAD~1 failed: error: unable to unlink old "
                        "'tracked.txt': Invalid argument\nfatal: Could not reset index "
                        "file to revision 'HEAD~1'."
                    )
                return ""
            return ""

        monkeypatch.setattr(git_coord, "_git", _flaky_git)

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.git_enabled = True
        run.work_dir = "/w"
        run.worktree_path = "/w"
        run.commit_hashes = ["aaa", "bbb"]

        with caplog.at_level(logging.WARNING, logger="kiro_crew.git_coord"):
            await git_coord.revert_step(run)

        assert calls["n"] == 3  # two contended attempts, third succeeds
        assert run.commit_hashes == ["aaa"]  # exactly one popped after success
        # No WARNING because the revert ultimately succeeded.
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    @pytest.mark.asyncio
    async def test_revert_exhausts_retries_warns_and_does_not_pop(
        self, monkeypatch, caplog
    ) -> None:
        """A sharing violation that never clears: after the bounded retries the
        failure surfaces at WARNING and commit_hashes is left untouched so it
        stays consistent with the un-reverted worktree."""
        import logging

        from kiro_crew import git_coord

        monkeypatch.setattr(git_coord.platform_compat, "IS_WINDOWS", True)
        # flake-ok: pre-existing rebind of the stdlib asyncio.sleep through git_coord.asyncio for one test, undone at teardown, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(git_coord.asyncio, "sleep", AsyncMock())

        calls = {"n": 0}

        async def _always_locked(work_dir, *args):
            if args[:2] == ("reset", "--hard"):
                calls["n"] += 1
                raise RuntimeError(
                    "git reset --hard HEAD~1 failed: error: unable to unlink old "
                    "'tracked.txt': Invalid argument"
                )
            return ""

        monkeypatch.setattr(git_coord, "_git", _always_locked)

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.git_enabled = True
        run.work_dir = "/w"
        run.worktree_path = "/w"
        run.commit_hashes = ["aaa", "bbb"]

        with caplog.at_level(logging.WARNING, logger="kiro_crew.git_coord"):
            await git_coord.revert_step(run)

        assert calls["n"] == git_coord._REVERT_MAX_ATTEMPTS  # tried the whole budget
        assert run.commit_hashes == ["aaa", "bbb"]  # NOT popped — stays consistent
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert "git revert failed" in warnings[0].getMessage()

    @pytest.mark.asyncio
    async def test_revert_non_sharing_violation_failure_warns_without_retry(
        self, monkeypatch, caplog
    ) -> None:
        """A failure that is not a transient Windows sharing violation (e.g. on
        POSIX, or an unrelated git error) is NOT retried into a false success:
        it surfaces at WARNING on the first attempt and does not pop."""
        import logging

        from kiro_crew import git_coord

        # POSIX: a reset failure is a genuine fault — no retry window.
        monkeypatch.setattr(git_coord.platform_compat, "IS_WINDOWS", False)
        sleep_mock = AsyncMock()
        # flake-ok: pre-existing rebind of the stdlib asyncio.sleep through git_coord.asyncio for one test, undone at teardown, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(git_coord.asyncio, "sleep", sleep_mock)

        calls = {"n": 0}

        async def _hard_fail(work_dir, *args):
            if args[:2] == ("reset", "--hard"):
                calls["n"] += 1
                raise RuntimeError("git reset --hard HEAD~1 failed: fatal: bad revision")
            return ""

        monkeypatch.setattr(git_coord, "_git", _hard_fail)

        run = TaskRun(spec_path="/t.md", spec_content="s")
        run.git_enabled = True
        run.work_dir = "/w"
        run.worktree_path = "/w"
        run.commit_hashes = ["aaa"]

        with caplog.at_level(logging.WARNING, logger="kiro_crew.git_coord"):
            await git_coord.revert_step(run)

        assert calls["n"] == 1  # no retry
        sleep_mock.assert_not_awaited()
        assert run.commit_hashes == ["aaa"]  # not popped
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert "git revert failed" in warnings[0].getMessage()
