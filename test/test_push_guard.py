"""Tests for the kirocrew-prepare-pr push_guard.py stale-base detection.

Verifies that the push_guard script (src/kiro_crew/builtin_skills/kirocrew-dev/
kirocrew-prepare-pr/scripts/push_guard.py) correctly refuses to push when:
- The branch has no common history with origin/<base> (orphan / disconnected)
- The commit count exceeds --max-ahead (implausibly many commits for a PR)
- The fetch of origin/<base> fails (network error → fail closed)

And allows push when the branch is a normal single-commit PR (1 commit ahead
of a fresh origin/<base> with shared history).

A force-push from a
worktree branched off kiki-trunk can carry 114 duplicate commits, which the guard rejects.
"""

from __future__ import annotations

import io
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from installer_test_helpers import run_bounded
from skill_script_helpers import load_skill_script

from kiro_crew import platform_compat
from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS

# Resolve the push_guard.py script path relative to the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
PUSH_GUARD_DIR = (
    REPO_ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "kirocrew-prepare-pr"
    / "scripts"
)
PUSH_GUARD = str(PUSH_GUARD_DIR / "push_guard.py")


def _load_push_guard():
    """A private copy of push_guard.py: not in sys.modules, no sys.path entry."""
    return load_skill_script("push_guard_under_test", PUSH_GUARD)


@pytest.fixture(autouse=True)
def _no_inherited_git_c(monkeypatch):
    """The guard honours its caller's ``git -c`` (GIT_CONFIG_PARAMETERS) by design.

    A test is not that caller: an ancestor's ``-c core.hooksPath`` would run
    host hooks inside the run.  A test that needs the channel sets it itself.
    """
    monkeypatch.delenv("GIT_CONFIG_PARAMETERS", raising=False)


# Well under the per-test ``--timeout``, so a hung git fails the test by name
# instead of costing the worker (testing-conventions, determinism class 6).
_SUBPROCESS_TIMEOUT_S = 30
# The guard's own bounds in an in-process run, for the same reason.
_INPROCESS_TIMEOUT_S = 20


def _run_push_guard(
    cwd: str, extra_args: list[str] | None = None, env_extra: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run push_guard.py in the given directory; return (rc, stdout, stderr).

    Bounded, and on a timeout the guard's whole process tree is reaped
    (``run_bounded``). The environment passes through as is -- including the
    rootdir conftest's ``GIT_CEILING_DIRECTORIES`` fence -- because the guard
    scrubs the variables that would retarget git itself.
    """
    args = [sys.executable, PUSH_GUARD, "--base", "main", *(extra_args or [])]
    proc = run_bounded(
        args, {**os.environ, **(env_extra or {})}, timeout=_SUBPROCESS_TIMEOUT_S, cwd=cwd
    )
    return proc.returncode, proc.stdout, proc.stderr


def _fixture_git_env() -> dict[str, str]:
    """Env for a fixture git call: no host config, templates, hooks, or identity bleed.

    The session/module-scoped template builders below run BEFORE the function-scoped
    ``_git_identity`` autouse fixture in ``test/conftest.py`` has pinned anything, so
    they would otherwise read the developer's real ``~/.gitconfig`` -- a
    ``commit.gpgSign`` aborts the whole template, and a ``core.hooksPath`` or
    ``init.templateDir`` would EXECUTE host hooks from inside the test run. The
    ``GIT_DIR`` location family is dropped (the production list, so an exported
    ``GIT_DIR`` from a hook or ``rebase --exec`` cannot retarget the fixture), both
    template channels are emptied, and identity is supplied. Deliberately NOT
    ``git_command_env()``: that pins ``diff.external`` empty for commands that never
    diff, and these fixtures run ``git diff``.
    """
    # GIT_CONFIG_PARAMETERS is an ancestor's ``git -c``: it would load a host
    # core.hooksPath past the file channels emptied below.
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _GIT_LOCATION_VARS and k != "GIT_CONFIG_PARAMETERS"
    }
    env.update(
        {
            "GIT_TEMPLATE_DIR": "",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "init.templateDir",
            "GIT_CONFIG_VALUE_0": "",
        }
    )
    return env


def _git(cwd: str, *args: str) -> str:
    """Run a git command in cwd with the scrubbed fixture env; raise on failure."""
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env=_fixture_git_env(),
        timeout=_SUBPROCESS_TIMEOUT_S,
    )
    return proc.stdout.strip()


@pytest.fixture(scope="session")
def _repo_pair_template(tmp_path_factory) -> tuple[str, str]:
    """Build the bare origin + initial clone once per session; ``repo_pair`` copies it.

    Six git subprocesses (~1-1.6s) would otherwise be paid on every one of the ~40
    tests below. Session scope is safe because the template directories are
    never handed to a test, only copied from via ``shutil.copytree`` -- so a
    test that pushes, branches, or clones ``work2`` off its own copy of
    ``origin_dir`` cannot reach another test's copy.
    """
    root = tmp_path_factory.mktemp("push-guard-seed")
    origin_dir = str(root / "origin.git")
    clone_dir = str(root / "work")

    os.makedirs(origin_dir)
    _git(origin_dir, "init", "--bare")
    _git(origin_dir, "symbolic-ref", "HEAD", "refs/heads/main")

    _git(str(root), "clone", origin_dir, "work")
    _git(clone_dir, "checkout", "-b", "main")

    Path(clone_dir, "README.md").write_text("initial\n")
    _git(clone_dir, "add", "README.md")
    _git(clone_dir, "commit", "-m", "initial commit")
    _git(clone_dir, "push", "-u", "origin", "main")

    return clone_dir, origin_dir


@pytest.fixture
def repo_pair(tmp_path, _repo_pair_template):
    """A local 'origin' bare repo and a working clone, copied from the template.

    Returns (clone_dir, origin_dir) where origin_dir is a bare repo and
    clone_dir has 'origin' pointing at origin_dir. Each test gets its own copy,
    so pushes, branches, and `work2` clones (which several tests create
    alongside this pair) never touch another test's copy.
    """
    template_clone, template_origin = _repo_pair_template
    origin_dir = str(tmp_path / "origin.git")
    clone_dir = str(tmp_path / "work")
    shutil.copytree(template_origin, origin_dir)
    shutil.copytree(template_clone, clone_dir)
    # The copied clone's remote still points at the TEMPLATE's origin path;
    # repoint it at this test's own copy so pushes/fetches never reach (or
    # mutate) the session template or another test's copy.
    _git(clone_dir, "remote", "set-url", "origin", origin_dir)
    # copytree resets every file's mtime, which invalidates git's cached
    # index stat info and makes git see a false "unstaged changes" diff (e.g.
    # git rebase refuses with "You have unstaged changes"). Nothing in the
    # template is ever uncommitted, so resetting hard to HEAD is a no-op on
    # content and forces git to re-stat every file against the real index.
    _git(clone_dir, "reset", "--hard", "HEAD")
    return clone_dir, origin_dir


class TestPushGuardSafe:
    """Normal single-commit PR: push_guard exits 0 (safe)."""

    def test_single_commit_ahead(self, repo_pair):
        clone_dir, _ = repo_pair

        # Create a feature branch with one commit ahead of origin/main.
        _git(clone_dir, "checkout", "-b", "feature/my-fix")
        Path(clone_dir, "fix.py").write_text("# fix\n")
        _git(clone_dir, "add", "fix.py")
        _git(clone_dir, "commit", "-m", "fix: the bug")

        _bless(clone_dir)
        rc, stdout, stderr = _run_push_guard(clone_dir)
        assert rc == 0, f"Expected safe (0), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "SAFE TO PUSH" in stdout

    def test_max_ahead_at_threshold(self, repo_pair):
        """Exactly at --max-ahead=3 should pass."""
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/multi")
        for i in range(3):
            Path(clone_dir, f"file{i}.py").write_text(f"# {i}\n")
            _git(clone_dir, "add", f"file{i}.py")
            _git(clone_dir, "commit", "-m", f"commit {i}")

        _bless(clone_dir)
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "3"])
        assert rc == 0, f"Expected safe (0), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "SAFE TO PUSH" in stdout


class TestPushGuardRefused:
    """push_guard exits 40 (refused) when the branch is unsafe to push."""

    def test_too_many_commits_ahead(self, repo_pair):
        """Branch with 6 commits and --max-ahead=5 → refused."""
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/bloated")
        for i in range(6):
            Path(clone_dir, f"file{i}.py").write_text(f"# {i}\n")
            _git(clone_dir, "add", f"file{i}.py")
            _git(clone_dir, "commit", "-m", f"commit {i}")

        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert "6 commits ahead" in stderr

    def test_orphan_branch_no_common_history(self, repo_pair):
        """Orphan branch with no common history with origin/main → refused."""
        clone_dir, origin_dir = repo_pair

        # Advance origin/main with a new commit via a second clone.
        work2 = os.path.dirname(clone_dir) + "/work2"
        _git(os.path.dirname(clone_dir), "clone", origin_dir, "work2")
        Path(work2, "upstream.txt").write_text("upstream change\n")
        _git(work2, "add", "upstream.txt")
        _git(work2, "commit", "-m", "upstream: new feature")
        _git(work2, "push", "origin", "main")

        # In the original clone, create an orphan branch — no shared ancestry
        # with origin/main at all.
        _git(clone_dir, "checkout", "--orphan", "stale-trunk")
        Path(clone_dir, "stale.txt").write_text("stale\n")
        _git(clone_dir, "add", "stale.txt")
        _git(clone_dir, "commit", "-m", "stale trunk commit")

        # git merge-base will fail (no common ancestor) → refused.
        rc, stdout, stderr = _run_push_guard(clone_dir)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr

    def test_fetch_failure_refuses(self, tmp_path):
        """When origin doesn't have the base branch, fetch fails → refused."""
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # Set origin to a non-existent path so fetch always fails.
        _git(repo_dir, "remote", "add", "origin", "/nonexistent/repo.git")

        rc, stdout, stderr = _run_push_guard(repo_dir)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert "fetch" in stderr.lower()

    def test_stale_base_clobber_scenario(self, repo_pair):
        """Reproduce the exact clobber pattern: many commits from a local trunk
        that aren't on the remote."""
        clone_dir, origin_dir = repo_pair

        # Simulate kiki-trunk: advance local main with 10 "integration" commits
        # that never get pushed to origin.
        _git(clone_dir, "checkout", "main")
        for i in range(10):
            Path(clone_dir, f"integration{i}.py").write_text(f"# int {i}\n")
            _git(clone_dir, "add", f"integration{i}.py")
            _git(clone_dir, "commit", "-m", f"feat(integration): commit {i}")

        # Branch from the stale local main (as kiki does from kiki-trunk).
        _git(clone_dir, "checkout", "-b", "feature/pr-fix")
        Path(clone_dir, "fix.py").write_text("# fix\n")
        _git(clone_dir, "add", "fix.py")
        _git(clone_dir, "commit", "-m", "fix: the issue")

        # Now this branch is 11 commits ahead of origin/main (10 integration +
        # 1 actual fix). The push_guard MUST refuse.
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert "11 commits ahead" in stderr


class TestPushGuardEdgeCases:
    """Edge cases and error handling."""

    def test_not_a_git_repo(self, tmp_path, monkeypatch):
        """Running outside a git repo → exit 2.

        "Outside a git repo" is constructed, not assumed of ``tmp_path``: a harness
        that pins ``TMPDIR`` under the checkout gives it a real ``.git`` among its
        ancestors, and git's upward discovery would find it (exit 40, a refusal of
        THIS checkout's branch state, rather than 2). ``GIT_CEILING_DIRECTORIES``
        is git's own seam for that walk and the script inherits the environment;
        the directory is a CHILD of the ceiling because git checks its starting
        directory before consulting the ceiling.
        """
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        nowhere = tmp_path / "nowhere"
        nowhere.mkdir()
        rc, stdout, stderr = _run_push_guard(str(nowhere))
        assert rc == 2

    def test_custom_max_ahead(self, repo_pair):
        """--max-ahead=1 catches even 2 commits."""
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/small")
        for i in range(2):
            Path(clone_dir, f"f{i}.py").write_text(f"# {i}\n")
            _git(clone_dir, "add", f"f{i}.py")
            _git(clone_dir, "commit", "-m", f"commit {i}")

        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "1"])
        assert rc == 40
        assert "2 commits ahead" in stderr


class TestPushGuardStaleBaseAncestry:
    """Stale-base ancestry detection: origin/<base> must be an ancestor of HEAD.

    The is-ancestor check must not test merge-base against origin/<base>
    (true by construction, hence vacuous). It verifies instead that
    origin/<base> itself is an ancestor of HEAD — i.e. the
    branch sits on the freshly fetched base tip after a correct rebase.
    """

    def test_stale_base_novel_commits_refused(self, repo_pair):
        """origin/<base> advances after fork, novel commits <= max-ahead → exit 40.

        This is the exact scenario the vacuous check missed: the branch forked
        from an OLD origin/main commit, origin/main advanced, but the branch
        has only a few novel commits (under the count threshold). Without the
        ancestry check, the guard would pass and the subsequent squash would
        bake in reversions of the newer base changes.
        """
        clone_dir, origin_dir = repo_pair

        # Create a feature branch from the current origin/main.
        _git(clone_dir, "checkout", "-b", "feature/stale-fork")
        Path(clone_dir, "novel.py").write_text("# novel work\n")
        _git(clone_dir, "add", "novel.py")
        _git(clone_dir, "commit", "-m", "feat: novel work")

        # Advance origin/main AFTER the branch forked — simulates base movement
        # that the branch never rebased onto.
        work2 = os.path.dirname(clone_dir) + "/work2"
        _git(os.path.dirname(clone_dir), "clone", origin_dir, "work2")
        Path(work2, "upstream_new.txt").write_text("upstream advance\n")
        _git(work2, "add", "upstream_new.txt")
        _git(work2, "commit", "-m", "feat: upstream advance")
        _git(work2, "push", "origin", "main")

        # The branch has 1 novel commit (under default max-ahead=5), and there
        # are no replayed commits (so git cherry won't catch it). But
        # origin/main is NOT an ancestor of HEAD because the branch forked
        # before the upstream advance. The ancestry check MUST refuse.
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert "not based on the fresh origin/" in stderr

    def test_freshly_rebased_branch_passes(self, repo_pair):
        """Branch correctly rebased onto fresh origin/<base> → safe.

        After a proper rebase, origin/<base> IS an ancestor of HEAD, so the
        ancestry check passes and the guard allows the push.
        """
        clone_dir, origin_dir = repo_pair

        # Create a feature branch from origin/main.
        _git(clone_dir, "checkout", "-b", "feature/rebased")
        Path(clone_dir, "novel.py").write_text("# novel work\n")
        _git(clone_dir, "add", "novel.py")
        _git(clone_dir, "commit", "-m", "feat: novel work")

        # Advance origin/main.
        work2 = os.path.dirname(clone_dir) + "/work2"
        _git(os.path.dirname(clone_dir), "clone", origin_dir, "work2")
        Path(work2, "upstream_new.txt").write_text("upstream advance\n")
        _git(work2, "add", "upstream_new.txt")
        _git(work2, "commit", "-m", "feat: upstream advance")
        _git(work2, "push", "origin", "main")

        # Rebase the feature branch onto the fresh origin/main — this is the
        # correct workflow. After rebase, origin/main IS an ancestor of HEAD.
        _git(clone_dir, "fetch", "origin", "main")
        _git(clone_dir, "rebase", "origin/main")

        _bless(clone_dir)
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 0, f"Expected safe (0), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "SAFE TO PUSH" in stdout

    def test_multiple_novel_commits_on_stale_base_refused(self, repo_pair):
        """Multiple novel commits on stale base, all under max-ahead → exit 40.

        Verifies the ancestry check fires even when multiple commits are
        present (all novel, so git cherry doesn't catch them) but the base
        has advanced.
        """
        clone_dir, origin_dir = repo_pair

        # Create a feature branch with several novel commits.
        _git(clone_dir, "checkout", "-b", "feature/multi-novel-stale")
        for i in range(3):
            Path(clone_dir, f"novel{i}.py").write_text(f"# novel {i}\n")
            _git(clone_dir, "add", f"novel{i}.py")
            _git(clone_dir, "commit", "-m", f"feat: novel commit {i}")

        # Advance origin/main after the fork.
        work2 = os.path.dirname(clone_dir) + "/work2"
        _git(os.path.dirname(clone_dir), "clone", origin_dir, "work2")
        Path(work2, "upstream_new.txt").write_text("upstream advance\n")
        _git(work2, "add", "upstream_new.txt")
        _git(work2, "commit", "-m", "feat: upstream advance")
        _git(work2, "push", "origin", "main")

        # 3 novel commits (under max-ahead=5), no replayed commits, but
        # origin/main is NOT an ancestor of HEAD → ancestry check refuses.
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert "not based on the fresh origin/" in stderr


class TestPushGuardReplayedCommits:
    """Replayed-commit detection via patch-id comparison against base history.

    The patch-id replay check compares each ahead-commit's semantic diff
    against a bounded window of origin/<base> history.  An ahead-commit whose
    patch-id matches a base-history commit is a replay (e.g. a cherry-pick of
    a commit already on the base) and the guard refuses.
    """

    def test_replayed_commits_refused(self, repo_pair):
        """Branch from stale base with patch-equivalent commits → refused.

        The ancestry check fires first (origin/main is not an ancestor of HEAD
        because the branch forked before origin/main advanced), which subsumes
        the patch-id detection. The guard MUST refuse this scenario.
        """
        clone_dir, origin_dir = repo_pair

        # Create a feature branch from current main (before upstream advance).
        _git(clone_dir, "checkout", "-b", "feature/replay")

        # Make a commit with specific content on the feature branch.
        Path(clone_dir, "shared_fix.py").write_text("# shared fix\n")
        _git(clone_dir, "add", "shared_fix.py")
        _git(clone_dir, "commit", "-m", "fix: shared bugfix (local)")

        # Push the SAME patch to origin/main (different SHA, same patch-id).
        work2 = os.path.dirname(clone_dir) + "/work2"
        _git(os.path.dirname(clone_dir), "clone", origin_dir, "work2")
        Path(work2, "shared_fix.py").write_text("# shared fix\n")
        _git(work2, "add", "shared_fix.py")
        _git(work2, "commit", "-m", "fix: shared bugfix (upstream)")
        _git(work2, "push", "origin", "main")

        # Add one novel commit so we have 2 total (under --max-ahead=5).
        Path(clone_dir, "novel.py").write_text("# novel work\n")
        _git(clone_dir, "add", "novel.py")
        _git(clone_dir, "commit", "-m", "feat: novel work")

        # The guard MUST refuse: origin/main advanced past the fork point,
        # so the ancestry check fires. (If the ancestry check were absent,
        # the patch-id check would catch the replayed commit instead.)
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr

    def test_novel_commits_pass(self, repo_pair):
        """Branch with only genuinely novel commits (no upstream equivalents) → safe."""
        clone_dir, _ = repo_pair

        # Create a feature branch with novel work only.
        _git(clone_dir, "checkout", "-b", "feature/novel")
        for i in range(3):
            Path(clone_dir, f"novel{i}.py").write_text(f"# novel {i}\n")
            _git(clone_dir, "add", f"novel{i}.py")
            _git(clone_dir, "commit", "-m", f"feat: novel commit {i}")

        _bless(clone_dir)
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 0, f"Expected safe (0), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "SAFE TO PUSH" in stdout

    def test_multiple_replayed_commits_refused(self, repo_pair):
        """Multiple replayed commits on stale base → refused."""
        clone_dir, origin_dir = repo_pair

        # Create a feature branch from current main (before upstream advance).
        _git(clone_dir, "checkout", "-b", "feature/multi-replay")

        # Make two commits with specific patches on the feature branch.
        Path(clone_dir, "up1.py").write_text("# up1\n")
        _git(clone_dir, "add", "up1.py")
        _git(clone_dir, "commit", "-m", "fix: first shared (local)")
        Path(clone_dir, "up2.py").write_text("# up2\n")
        _git(clone_dir, "add", "up2.py")
        _git(clone_dir, "commit", "-m", "fix: second shared (local)")

        # Push the SAME patches to origin/main.
        work2 = os.path.dirname(clone_dir) + "/work2"
        _git(os.path.dirname(clone_dir), "clone", origin_dir, "work2")
        Path(work2, "up1.py").write_text("# up1\n")
        _git(work2, "add", "up1.py")
        _git(work2, "commit", "-m", "fix: first shared (upstream)")
        Path(work2, "up2.py").write_text("# up2\n")
        _git(work2, "add", "up2.py")
        _git(work2, "commit", "-m", "fix: second shared (upstream)")
        _git(work2, "push", "origin", "main")

        # The guard MUST refuse: origin/main advanced past the fork point,
        # ancestry check fires first.
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr

    def test_replay_on_fresh_base_revert_cherry_pick_scenario(self, repo_pair):
        """GPT's exact scenario: branch ON fresh base, revert+replay → exit 40.

        Scenario (GPT blocking finding, confirmed by local repro):
        1. Branch is ON the fresh base tip (ancestry check passes).
        2. Revert an upstream commit (C), revert another (B), cherry-pick B
           back, add a novel fix.  Count = 4 <= max-ahead.
        3. The cherry-picked B has the same patch-id as the original B on
           the base — the patch-id replay check MUST catch it and refuse.

        This is the case the dead git-cherry check could never detect (because
        with origin/<base> as an ancestor of HEAD, cherry's symmetric
        difference has an empty left side — no commit can ever be marked `-`).
        """
        clone_dir, origin_dir = repo_pair

        # Build up base history with commits B and C on origin/main.
        work2 = os.path.dirname(clone_dir) + "/work2"
        _git(os.path.dirname(clone_dir), "clone", origin_dir, "work2")

        # Commit B (a specific patch we'll replay).
        Path(work2, "feature_b.py").write_text("# feature B\n")
        _git(work2, "add", "feature_b.py")
        _git(work2, "commit", "-m", "feat: feature B")
        commit_b = _git(work2, "rev-parse", "HEAD")

        # Commit C (another commit we'll revert).
        Path(work2, "feature_c.py").write_text("# feature C\n")
        _git(work2, "add", "feature_c.py")
        _git(work2, "commit", "-m", "feat: feature C")

        _git(work2, "push", "origin", "main")

        # Fetch fresh origin/main in the working clone.
        _git(clone_dir, "fetch", "origin", "main")
        _git(clone_dir, "checkout", "-b", "feature/replay-on-fresh", "origin/main")

        # Now we're ON the fresh base tip.  Build the problematic branch:
        # 1. Revert C
        _git(clone_dir, "revert", "--no-edit", "HEAD")
        # 2. Revert B
        _git(clone_dir, "revert", "--no-edit", commit_b)
        # 3. Cherry-pick B back (this is the replay — same patch-id as B on base)
        _git(clone_dir, "cherry-pick", "--no-edit", commit_b)
        # 4. Add a novel fix
        Path(clone_dir, "fix.py").write_text("# novel fix\n")
        _git(clone_dir, "add", "fix.py")
        _git(clone_dir, "commit", "-m", "fix: the actual fix")

        # We have 4 commits ahead, max-ahead=5 allows it, ancestry passes
        # (we're ON the fresh tip). The replay check MUST catch the
        # cherry-picked B.
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 40, (
            f"Expected refused (40), got {rc}. The replay check failed to "
            f"detect the cherry-picked commit.\nstdout: {stdout}\nstderr: {stderr}"
        )
        assert "REFUSED" in stderr
        assert "patch-equivalent" in stderr

    def test_novel_commits_on_fresh_base_pass(self, repo_pair):
        """Branch ON fresh base with only novel commits → safe (no false positive).

        Ensures the patch-id replay check does not fire on genuinely novel
        commits that happen to sit on top of the fresh base.
        """
        clone_dir, origin_dir = repo_pair

        # Push some history to origin/main.
        work2 = os.path.dirname(clone_dir) + "/work2"
        _git(os.path.dirname(clone_dir), "clone", origin_dir, "work2")
        Path(work2, "upstream.py").write_text("# upstream\n")
        _git(work2, "add", "upstream.py")
        _git(work2, "commit", "-m", "feat: upstream work")
        _git(work2, "push", "origin", "main")

        # Fetch and branch from fresh tip.
        _git(clone_dir, "fetch", "origin", "main")
        _git(clone_dir, "checkout", "-b", "feature/novel-fresh", "origin/main")

        # Add genuinely novel commits (no patch equivalence to base).
        for i in range(3):
            Path(clone_dir, f"novel{i}.py").write_text(f"# novel fresh {i}\n")
            _git(clone_dir, "add", f"novel{i}.py")
            _git(clone_dir, "commit", "-m", f"feat: novel fresh commit {i}")

        _bless(clone_dir)
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 0, f"Expected safe (0), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "SAFE TO PUSH" in stdout


class TestPushGuardNarrowRefspec:
    """Regression: narrow remote.origin.fetch must not defeat the guard.

    On single-branch clones or narrow CI checkouts, remote.origin.fetch is set
    to a refspec that does NOT cover the base branch. Before the fix, a bare
    `git fetch origin <base>` would succeed into FETCH_HEAD but never update
    refs/remotes/origin/<base> — causing the guard to validate against a stale
    (or nonexistent) remote-tracking ref.  The fix uses an explicit refspec
    (+refs/heads/<base>:refs/remotes/origin/<base>) so the remote-tracking ref
    is always written regardless of the clone's configured refspec.
    """

    def test_narrow_refspec_stale_base_refused(self, repo_pair):
        """Narrow refspec clone + advanced remote base → guard MUST refuse.

        Setup: clone with remote.origin.fetch restricted to a non-base branch,
        advance origin/main, create a feature branch from the OLD main tip.
        Without the explicit-refspec fix, the guard passes because
        origin/main never updates; with the fix, it correctly refuses (exit 40).
        """
        clone_dir, origin_dir = repo_pair

        # Create a feature branch from the CURRENT origin/main (before advance).
        _git(clone_dir, "checkout", "-b", "feature/narrow-refspec")
        Path(clone_dir, "work.py").write_text("# work\n")
        _git(clone_dir, "add", "work.py")
        _git(clone_dir, "commit", "-m", "feat: work on narrow clone")

        # Advance origin/main AFTER the fork.
        work2 = os.path.dirname(clone_dir) + "/narrow_work2"
        _git(os.path.dirname(clone_dir), "clone", origin_dir, "narrow_work2")
        Path(work2, "upstream_advance.txt").write_text("advance\n")
        _git(work2, "add", "upstream_advance.txt")
        _git(work2, "commit", "-m", "feat: upstream advance")
        _git(work2, "push", "origin", "main")

        # Restrict remote.origin.fetch to a DIFFERENT branch — simulates a
        # single-branch clone or narrow CI checkout that doesn't cover 'main'.
        _git(
            clone_dir,
            "config",
            "remote.origin.fetch",
            "+refs/heads/other-branch:refs/remotes/origin/other-branch",
        )

        # Verify origin/main still points at the OLD tip (stale).
        old_origin_main = _git(clone_dir, "rev-parse", "origin/main")

        # Run the guard — with the explicit-refspec fix, origin/main gets
        # updated to the advanced tip, and the ancestry check fires (HEAD
        # is not based on the new origin/main → exit 40).
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 40, (
            f"Expected refused (40), got {rc}. The guard fail-opened on a "
            f"narrow refspec clone.\nstdout: {stdout}\nstderr: {stderr}"
        )
        assert "REFUSED" in stderr

        # Confirm origin/main was actually updated by the guard's fetch.
        new_origin_main = _git(clone_dir, "rev-parse", "origin/main")
        assert new_origin_main != old_origin_main, (
            "origin/main was NOT updated by the fetch — the explicit refspec " "did not work."
        )

    def test_narrow_refspec_require_single_on_base_refused(self, repo_pair):
        """Narrow refspec + --require-single-on-base + stale base → refused.

        Same narrow-refspec scenario but in post-squash mode. The guard must
        update origin/main via the explicit refspec and then refuse because
        HEAD~1 does not equal the (now-advanced) origin/main.
        """
        clone_dir, origin_dir = repo_pair

        # A squashed feature branch (single commit on origin/main), built by
        # the guard, so the stale base is the only thing that can refuse it.
        _git(clone_dir, "checkout", "-b", "feature/narrow-single")
        Path(clone_dir, "squashed.py").write_text("# squashed\n")
        rc, _, stderr = _run_push_guard(
            clone_dir, ["--commit", "-m", "feat: squashed commit", "--", "squashed.py"]
        )
        assert rc == 0, stderr

        # Advance origin/main AFTER the squash.
        work2 = os.path.dirname(clone_dir) + "/narrow_single_work2"
        _git(os.path.dirname(clone_dir), "clone", origin_dir, "narrow_single_work2")
        Path(work2, "upstream_advance.txt").write_text("advance\n")
        _git(work2, "add", "upstream_advance.txt")
        _git(work2, "commit", "-m", "feat: upstream advance")
        _git(work2, "push", "origin", "main")

        # Restrict remote.origin.fetch — narrow clone.
        _git(
            clone_dir,
            "config",
            "remote.origin.fetch",
            "+refs/heads/unrelated:refs/remotes/origin/unrelated",
        )

        # The guard MUST fetch with the explicit refspec, update origin/main,
        # see that HEAD~1 != (new) origin/main, and refuse.
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--require-single-on-base"])
        assert rc == 40, (
            f"Expected refused (40), got {rc}. The guard fail-opened on a "
            f"narrow refspec clone in --require-single-on-base mode.\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )
        assert "are not exactly origin/main" in stderr

    def test_narrow_refspec_fresh_rebase_passes(self, repo_pair):
        """Narrow refspec but branch correctly rebased → safe.

        Even with a narrow refspec, a properly rebased branch should pass
        because the explicit-refspec fetch updates origin/main, and the
        ancestry check sees HEAD sitting on that fresh tip.
        """
        clone_dir, origin_dir = repo_pair

        # Advance origin/main.
        work2 = os.path.dirname(clone_dir) + "/narrow_fresh_work2"
        _git(os.path.dirname(clone_dir), "clone", origin_dir, "narrow_fresh_work2")
        Path(work2, "upstream_advance.txt").write_text("advance\n")
        _git(work2, "add", "upstream_advance.txt")
        _git(work2, "commit", "-m", "feat: upstream advance")
        _git(work2, "push", "origin", "main")

        # Fetch with the full refspec first (to get the advance), then rebase.
        _git(clone_dir, "fetch", "origin", "main")
        _git(clone_dir, "checkout", "-b", "feature/narrow-fresh", "origin/main")
        Path(clone_dir, "novel.py").write_text("# novel\n")
        _git(clone_dir, "add", "novel.py")
        _git(clone_dir, "commit", "-m", "feat: novel work")

        # NOW restrict the refspec — the branch is already correctly rebased
        # onto the latest origin/main.
        _git(
            clone_dir,
            "config",
            "remote.origin.fetch",
            "+refs/heads/other:refs/remotes/origin/other",
        )

        # The guard should still pass: the explicit-refspec fetch updates
        # origin/main to the same tip we rebased onto.
        _bless(clone_dir)
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 0, (
            f"Expected safe (0), got {rc}. A correctly rebased branch on a "
            f"narrow refspec clone should pass.\nstdout: {stdout}\nstderr: {stderr}"
        )
        assert "SAFE TO PUSH" in stdout


class TestPushGuardRequireSingleOnBase:
    """Post-squash structural guard: --require-single-on-base mode."""

    def test_single_commit_on_base_passes(self, repo_pair):
        """A properly squashed branch (HEAD~1 == origin/main) → safe."""
        clone_dir, _ = repo_pair

        # A feature branch with one commit directly on origin/main, built by
        # the guard (the post-squash mode blesses only what the guard built).
        _git(clone_dir, "checkout", "-b", "feature/squashed")
        Path(clone_dir, "squashed.py").write_text("# squashed\n")
        rc, _, stderr = _run_push_guard(
            clone_dir, ["--commit", "-m", "feat: squashed commit", "--", "squashed.py"]
        )
        assert rc == 0, stderr

        # HEAD~1 should equal origin/main exactly.
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--require-single-on-base"])
        assert rc == 0, f"Expected safe (0), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "SAFE TO PUSH" in stdout

    def test_multiple_commits_refused(self, repo_pair):
        """Branch with 2+ commits (HEAD~1 != origin/main) → refused."""
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/not-squashed")
        Path(clone_dir, "a.py").write_text("# a\n")
        _git(clone_dir, "add", "a.py")
        _git(clone_dir, "commit", "-m", "first commit")
        Path(clone_dir, "b.py").write_text("# b\n")
        _git(clone_dir, "add", "b.py")
        _git(clone_dir, "commit", "-m", "second commit")

        # HEAD~1 is the first commit, not origin/main.
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--require-single-on-base"])
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert "does not sit directly" in stderr

    def test_stale_base_after_squash_refused(self, repo_pair):
        """Squashed onto a stale origin/main (before upstream advanced) → refused."""
        clone_dir, origin_dir = repo_pair

        # Create and squash a feature branch onto origin/main.
        _git(clone_dir, "checkout", "-b", "feature/stale-squash")
        Path(clone_dir, "fix.py").write_text("# fix\n")
        _git(clone_dir, "add", "fix.py")
        _git(clone_dir, "commit", "-m", "fix: the bug")

        # Now advance origin/main AFTER the squash — simulating base movement
        # between squash and push.
        work2 = os.path.dirname(clone_dir) + "/work2"
        _git(os.path.dirname(clone_dir), "clone", origin_dir, "work2")
        Path(work2, "upstream.txt").write_text("upstream advance\n")
        _git(work2, "add", "upstream.txt")
        _git(work2, "commit", "-m", "feat: upstream advance")
        _git(work2, "push", "origin", "main")

        # Now HEAD~1 points at the OLD origin/main, but a fresh fetch will
        # update origin/main → HEAD~1 != origin/main → refused.
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--require-single-on-base"])
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert "does not sit directly" in stderr

    def test_fetch_failure_refuses(self, tmp_path):
        """When fetch fails in --require-single-on-base mode → refused."""
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")
        _git(repo_dir, "remote", "add", "origin", "/nonexistent/repo.git")

        rc, stdout, stderr = _run_push_guard(repo_dir, ["--require-single-on-base"])
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert "fetch" in stderr.lower()


class TestPushGuardCredentialRedaction:
    """Regression: fetch-failure diagnostics must never expose URL credentials.

    When a git-fetch fails against a credential-bearing remote URL (e.g.
    https://user:token@host/repo), the raw stderr contains the full URL.
    The refusal message printed by push_guard must redact the userinfo so
    tokens/passwords never reach agent transcripts or logs.
    """

    @staticmethod
    def _run_fetch_failure(
        monkeypatch, tmp_path, repo_dir, *, prefix="fatal: Authentication failed for "
    ):
        """Emit raw synthetic fetch stderr through the real guard subprocess path."""
        remote = _git(repo_dir, "remote", "get-url", "origin")
        fake_cmd = TestReplayFailClosed._make_fake_git_cmd(
            tmp_path,
            "args and args[0] == 'fetch'",
            failure_message=prefix + remote,
        )
        result = TestReplayFailClosed._run_push_guard_inprocess(monkeypatch, repo_dir, fake_cmd)
        assert "error class:" in result[2], "The fetch diagnostic did not reach classification"
        return result

    def test_fetch_error_redacts_credentials(self, tmp_path, monkeypatch):
        """Fetch stderr containing https://user:token@host → refusal redacts the token."""
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # Record a synthetic remote. Only the injected fetch emits its raw URL.
        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "https://user:someSecretToken123@example.com/repo.git",
        )

        rc, stdout, stderr = self._run_fetch_failure(monkeypatch, tmp_path, repo_dir)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        # The token must NOT appear in any output — regardless of whether git
        # itself stripped it or our redaction layer did.
        assert (
            "someSecretToken123" not in stderr
        ), "Credential leaked in stderr: token was not redacted"
        assert (
            "someSecretToken123" not in stdout
        ), "Credential leaked in stdout: token was not redacted"
        assert "user:someSecretToken123" not in stderr
        assert "user:someSecretToken123" not in stdout

    def test_fetch_error_redacts_bare_token_url(self, tmp_path, monkeypatch):
        """Fetch stderr containing https://ghp_token@host → redacts the token."""
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # PAT-style URL (no colon separator, just token@host).
        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "https://ghp_aBcDeFgHiJkLmNoPqRsT@github.com/org/repo.git",
        )

        rc, stdout, stderr = self._run_fetch_failure(monkeypatch, tmp_path, repo_dir)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        # The PAT must NOT appear in any output.
        assert (
            "ghp_aBcDeFgHiJkLmNoPqRsT" not in stderr
        ), "PAT token leaked in stderr: token was not redacted"
        assert (
            "ghp_aBcDeFgHiJkLmNoPqRsT" not in stdout
        ), "PAT token leaked in stdout: token was not redacted"

    def test_fetch_error_preserves_diagnostic_without_credentials(self, tmp_path):
        """Fetch failure without credentials keeps the full diagnostic intact."""
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # Non-credential-bearing URL — diagnostic should pass through unchanged.
        _git(repo_dir, "remote", "add", "origin", "/nonexistent/repo.git")

        rc, stdout, stderr = _run_push_guard(repo_dir)
        assert rc == 40
        assert "REFUSED" in stderr
        assert "fetch" in stderr.lower()
        # No redaction needed — no credentials to strip.
        assert "<redacted>" not in stderr

    def test_fetch_error_redacts_query_string_credentials(self, tmp_path, monkeypatch):
        """Fetch stderr with query-string credentials → refusal redacts the secret.

        Regression: query-string tokens (private_token=, access_token=,
        x-access-token=) are common on self-hosted forges (GitLab, Gitea) and
        CI job tokens.  The redaction layer must strip the entire query string
        so the secret never reaches agent transcripts or logs.
        """
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # Query-string credential URL — a common self-hosted forge pattern.
        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "https://git.example.com/team/Repo?private_token=secret123",
        )

        rc, stdout, stderr = self._run_fetch_failure(monkeypatch, tmp_path, repo_dir)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        # The query-string token must NOT appear in any output.
        assert (
            "secret123" not in stderr
        ), "Query-string credential leaked in stderr: token was not redacted"
        assert (
            "secret123" not in stdout
        ), "Query-string credential leaked in stdout: token was not redacted"
        assert "private_token" not in stderr, "Query parameter name leaked in stderr"
        assert "private_token" not in stdout, "Query parameter name leaked in stdout"
        # The derived diagnostic shows only the classified error label —
        # no raw stderr (even redacted) is passed through.
        assert "error class:" in stderr, "Classified error diagnostic not found in refusal output"

    def test_fetch_error_redacts_access_token_query(self, tmp_path, monkeypatch):
        """access_token= query parameter → redacted."""
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "https://git.example.com/org/project.git?access_token=ghp_TopSecret99",
        )

        rc, stdout, stderr = self._run_fetch_failure(monkeypatch, tmp_path, repo_dir)
        assert rc == 40
        assert "REFUSED" in stderr
        assert "ghp_TopSecret99" not in stderr, "access_token value leaked"
        assert "ghp_TopSecret99" not in stdout, "access_token value leaked"

    def test_fetch_error_redacts_path_embedded_credentials(self, tmp_path, monkeypatch):
        """Fetch stderr with path-embedded token → refusal redacts the secret.

        Regression: some forges and CI proxies embed PATs or deploy tokens
        directly in the URL path (e.g. https://host/<token>/repo.git).  The
        authority-only redaction policy must strip the entire path so the token
        never reaches agent transcripts or logs — while still preserving the
        host for diagnostic value.
        """
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # Path-embedded credential URL — the token sits in a URL path segment.
        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "https://git.example.com/tok_secret123/Repo.git",
        )

        rc, stdout, stderr = self._run_fetch_failure(monkeypatch, tmp_path, repo_dir)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        # The path-embedded token must NOT appear in any output.
        assert (
            "tok_secret123" not in stderr
        ), "Path-embedded credential leaked in stderr: token was not redacted"
        assert (
            "tok_secret123" not in stdout
        ), "Path-embedded credential leaked in stdout: token was not redacted"
        # The exact redacted form: scheme://host/<redacted> (also proves
        # The derived diagnostic shows only the classified error label —
        # no raw stderr (even redacted) is passed through.
        assert "error class:" in (
            stdout + stderr
        ), "Classified error diagnostic not found in refusal output"

    def test_fetch_error_redacts_query_only_credentials(self, tmp_path, monkeypatch):
        """Fetch stderr with query-only URL (no path) → refusal redacts the secret.

        Regression: a URL like https://host?private_token=x has no path
        component; the prior regex required a literal '/' after the authority
        so the query-string credential passed through unredacted.
        """
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "https://git.example.com?private_token=qsecret1",
        )

        rc, stdout, stderr = self._run_fetch_failure(monkeypatch, tmp_path, repo_dir)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert (
            "qsecret1" not in stderr
        ), "Query-only credential leaked in stderr: token was not redacted"
        assert (
            "qsecret1" not in stdout
        ), "Query-only credential leaked in stdout: token was not redacted"
        # The derived diagnostic shows only the classified error label —
        # no raw stderr (even redacted) is passed through.
        assert "error class:" in (
            stdout + stderr
        ), "Classified error diagnostic not found in refusal output"

    def test_fetch_error_redacts_ipv6_path_credentials(self, tmp_path, monkeypatch):
        """Fetch stderr with bracketed IPv6 authority → refusal redacts the secret.

        Regression: the prior regex used [^\\s/:\"']+ for the host charset,
        which excludes ':', so a bracketed IPv6 authority ([2001:db8::7]) could
        never match — its path/query credentials passed through unredacted.
        """
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "https://[2001:db8::7]:8443/tok_v6secret/Repo.git",
        )

        rc, stdout, stderr = self._run_fetch_failure(monkeypatch, tmp_path, repo_dir)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        assert (
            "tok_v6secret" not in stderr
        ), "IPv6 path credential leaked in stderr: token was not redacted"
        assert (
            "tok_v6secret" not in stdout
        ), "IPv6 path credential leaked in stdout: token was not redacted"
        # The derived diagnostic shows only the classified error label —
        # no raw stderr (even redacted) is passed through.
        assert "error class:" in (
            stdout + stderr
        ), "Classified error diagnostic not found in refusal output"

    def test_fetch_error_redacts_credential_at_truncation_boundary(self, tmp_path, monkeypatch):
        """Token straddling the old 300-byte slice boundary must still be redacted.

        Redaction must run before truncation. Doing ``redact_credentials(fetch_err[:300])``
        means that if the credential
        URL started before byte 300 but the '@host' portion landed after it,
        the slice would break the URL into an unmatchable prefix and the token
        would print raw.  After the fix (``redact_credentials(fetch_err)[:300]``)
        the full string is redacted first, so the token never appears.
        """
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # Construct a credential-bearing URL where the token starts well before
        # byte 300 but the '@' separator lands AFTER byte 300 so that
        # fetch_err[:300] would split the URL mid-credential.
        # Use a long PAT (fine-grained token style) that pushes '@' past 300.
        # The "remote: " preamble + URL scheme + token need to exceed 300 chars
        # before the '@'.  A 260-char preamble + "https://" (8) + 40-char token
        # puts the '@' at offset ~308.
        token = "ghp_" + "A" * 36  # 40 chars total
        cred_url = "https://{}@git.example.com/org/repo.git".format(token)
        # The injected fetch emits this full raw URL after a known preamble.
        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            cred_url,
        )

        prefix = "remote: " + "X" * 252
        assert (prefix + cred_url).index(token) < 300 < (prefix + cred_url).index("@")
        rc, stdout, stderr = self._run_fetch_failure(monkeypatch, tmp_path, repo_dir, prefix=prefix)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        # The token MUST NOT appear anywhere in the output.
        combined = stdout + stderr
        assert token not in combined, (
            "Split-boundary regression: credential token '{}' leaked in "
            "output — redaction ran AFTER truncation".format(token[:10] + "...")
        )
        # The derived diagnostic shows only the error class label — raw stderr
        # (even post-redaction) is never passed through.  Verify the classified
        # diagnostic is present.
        assert "error class:" in combined, "Classified error diagnostic not found in refusal output"


class TestPushGuardScpCredentialRedaction:
    """Regression: scheme-less SCP-style remotes must not expose userinfo.

    When a git-fetch/ssh fails against a credential-bearing SCP-style remote
    (e.g. tok_secret@git.example.com:team/Repo.git), the raw stderr contains
    the userinfo verbatim. The refusal message printed by push_guard must
    redact the userinfo so tokens/secrets never reach agent transcripts.

    These tests fail against the code at e89d975b (which only scrubs
    scheme-bearing URLs with ://) and pass after the scp-style pass is added.
    """

    @pytest.fixture
    def fake_ssh(self, tmp_path):
        """Create a fake SSH (Python script) that fails echoing the remote.

        Uses a Python script instead of a shell script so the fixture works
        on Windows (where #!/bin/sh scripts set as GIT_SSH_COMMAND fail
        because Git's bundled sh cannot resolve bare Windows paths with
        backslashes).  GIT_SSH_COMMAND is parsed by sh on every platform, so
        both the interpreter and script paths use forward slashes and are
        individually quoted.
        """
        ssh_script = tmp_path / "fake_ssh.py"
        # The script extracts user@host from argv (git passes host as argv[1]
        # or user@host as part of the arguments) and prints it to stderr,
        # simulating what real ssh does on auth failure.
        ssh_script.write_text(
            "import sys, re\n"
            "args = ' '.join(sys.argv[1:])\n"
            "m = re.search(r'[^\\s]*@[^\\s]*', args)\n"
            "if m:\n"
            "    print(f'{m.group(0)}: Permission denied (publickey).', file=sys.stderr)\n"
            "else:\n"
            "    print('Permission denied (publickey).', file=sys.stderr)\n"
            "sys.exit(255)\n"
        )
        # Build a GIT_SSH_COMMAND with quoted forward-slash paths so it works
        # on all platforms (Git parses GIT_SSH_COMMAND through sh everywhere).
        interp = Path(sys.executable).as_posix()
        script = ssh_script.as_posix()
        return f'"{interp}" "{script}"'

    def _run_push_guard_with_ssh(
        self, cwd: str, fake_ssh: str, extra_args: list[str] | None = None
    ) -> tuple[int, str, str]:
        """Run push_guard.py with a fake SSH to avoid real network calls."""
        return _run_push_guard(cwd, extra_args, env_extra={"GIT_SSH_COMMAND": fake_ssh})

    def test_scp_permission_denied_redacts_userinfo(self, tmp_path, fake_ssh):
        """SSH failure echoing tok_secret123@git.example.com → userinfo redacted.

        Simulates: git fetch fails and ssh prints
        "tok_secret123@git.example.com: Permission denied (publickey)."
        The token must not appear in the refusal output; the host must.
        """
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # SCP-style remote with a credential in the userinfo position.
        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "tok_secret123@git.example.com:team/Repo.git",
        )

        rc, stdout, stderr = self._run_push_guard_with_ssh(repo_dir, fake_ssh)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        combined = stdout + stderr
        # The credential (userinfo) must NOT appear anywhere in the output.
        assert (
            "tok_secret123" not in combined
        ), "SCP userinfo credential leaked: tok_secret123 was not redacted"
        # The host must still be visible for diagnostic value, verified via
        # the exact redacted form (not a bare substring — CodeQL lesson).
        # The derived diagnostic shows only the error class — no raw/redacted
        # stderr is passed through.
        assert "error class:" in combined, "Classified error diagnostic not found in refusal output"

    def test_scp_fetch_url_redacts_userinfo(self, tmp_path, fake_ssh):
        """git fetch tok_secret123@git.example.com:team/Repo.git → userinfo redacted.

        Simulates: fetch stderr echoes the full SCP remote URL.
        The token must not appear; the host must be preserved.
        """
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # SCP-style remote — the full remote URL appears in fetch stderr.
        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "tok_secret123@git.example.com:team/Repo.git",
        )

        rc, stdout, stderr = self._run_push_guard_with_ssh(repo_dir, fake_ssh)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        combined = stdout + stderr
        assert (
            "tok_secret123" not in combined
        ), "SCP fetch URL credential leaked: tok_secret123 was not redacted"
        # Exact redacted form proves both redaction AND host preservation.
        # The derived diagnostic shows only the error class — no raw/redacted
        # stderr is passed through.
        assert "error class:" in combined, "Classified error diagnostic not found in refusal output"

    def test_scp_bare_user_at_host_redacts_userinfo(self, tmp_path, fake_ssh):
        """SCP remote without :path (bare user@host) → userinfo redacted.

        Some SSH failure messages echo just "user@host" without the trailing
        colon+path. The redaction must still catch this form.
        """
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # Use a hostname with a dot to ensure the regex recognizes it as a host.
        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "deploy_key_abc@git.example.com:org/project.git",
        )

        rc, stdout, stderr = self._run_push_guard_with_ssh(repo_dir, fake_ssh)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        combined = stdout + stderr
        assert (
            "deploy_key_abc" not in combined
        ), "SCP bare userinfo credential leaked: deploy_key_abc was not redacted"
        # The derived diagnostic shows only the error class — no raw/redacted
        # stderr is passed through.
        assert "error class:" in combined, "Classified error diagnostic not found in refusal output"

    def test_scp_dotless_host_colon_path_redacts_userinfo(self, tmp_path, fake_ssh):
        """SCP remote with dotless host + colon-path → userinfo redacted.

        Regression: at 44a9fe6a the host regex required a dot, so
        ``tok_secret123@forge:team/repo.git`` passed unredacted.
        """
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "tok_secret123@forge:team/repo.git",
        )

        rc, stdout, stderr = self._run_push_guard_with_ssh(repo_dir, fake_ssh)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        combined = stdout + stderr
        assert (
            "tok_secret123" not in combined
        ), "SCP dotless-host credential leaked: tok_secret123 was not redacted"
        # The dotless host is preserved for diagnostic value.
        # The derived diagnostic shows only the error class — no raw/redacted
        # stderr is passed through.
        assert "error class:" in combined, "Classified error diagnostic not found in refusal output"

    def test_scp_bracketed_ipv6_host_redacts_userinfo(self, tmp_path, fake_ssh):
        """SCP remote with bracketed IPv6 host → userinfo redacted.

        Regression: at 44a9fe6a the host regex could not match IPv6 addresses,
        so ``tok_secret123@[::1]:team/repo.git`` passed unredacted.
        Note: git strips brackets when calling SSH, so the actual stderr
        contains ``tok_secret123@::1:`` (un-bracketed).
        """
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "tok_secret123@[::1]:team/repo.git",
        )

        rc, stdout, stderr = self._run_push_guard_with_ssh(repo_dir, fake_ssh)
        assert rc == 40, f"Expected refused (40), got {rc}.\nstdout: {stdout}\nstderr: {stderr}"
        assert "REFUSED" in stderr
        combined = stdout + stderr
        assert (
            "tok_secret123" not in combined
        ), "SCP bracketed-IPv6 credential leaked: tok_secret123 was not redacted"
        # Git strips brackets when calling SSH, so the host appears un-bracketed
        # in SSH stderr.  The redacted form preserves the bare IPv6 address.
        # The derived diagnostic shows only the error class — no raw/redacted
        # stderr is passed through.
        assert "error class:" in combined, "Classified error diagnostic not found in refusal output"


class TestReplayFailClosed:
    """Regression: patch-id replay check must fail CLOSED on git errors.

    At b94a9448 the replay check's git-error paths printed "SAFE TO PUSH"
    and returned 0, allowing a push to proceed when a transient fetch failure
    (e.g. blobless/partial clone lazy-fetch timeout) prevented verification.
    The fix makes every git subprocess failure exit 40 with a diagnostic.

    Tests use a module-level monkeypatch on push_guard._GIT_CMD (a Python
    fake-git script) instead of the former _PUSH_GUARD_GIT_CMD environment
    variable — this eliminates the Semgrep dangerous-subprocess-use-tainted-
    env-args finding while remaining fully portable (Windows + POSIX).
    """

    @staticmethod
    def _make_fake_git_cmd(
        tmp_path: Path,
        fail_condition: str,
        *,
        failure_message: str = "fatal: bad revision/object",
    ) -> list[str]:
        """Create a cross-platform fake git that fails on a specific condition.

        Uses a Python script assigned directly to push_guard._GIT_CMD (no
        PATH manipulation, no shell launchers, no environment variables).
        This avoids all Windows compatibility issues and Semgrep tainted-env
        findings.

        Args:
            tmp_path: pytest tmp dir for writing script files.
            fail_condition: Python expression evaluated against ``args``
                (the list of git arguments) that triggers the failure.
            failure_message: Raw stderr emitted by the failing subprocess.

        Returns:
            Command list suitable for assignment to push_guard._GIT_CMD.
        """
        real_git = shutil.which("git")
        assert real_git, "git must be on PATH for these tests"

        fake_git_script = tmp_path / "fake_git.py"
        # The fake git delegates to real git for all commands except those
        # matching the fail condition.  Real git path is embedded as a
        # string literal (no env lookup) to avoid any tainted-env concerns.
        fake_git_script.write_text(
            "import subprocess, sys\n"
            "args = sys.argv[1:]\n"
            f"if {fail_condition}:\n"
            f"    print({failure_message!r}, file=sys.stderr)\n"
            "    sys.exit(128)\n"
            "real_git = {}\n".format(repr(real_git))
            + "r = subprocess.run([real_git] + args, timeout={})\n".format(_SUBPROCESS_TIMEOUT_S)
            + "sys.exit(r.returncode)\n"
        )

        return [sys.executable, str(fake_git_script)]

    @staticmethod
    def _run_push_guard_inprocess(
        monkeypatch,
        cwd: str,
        fake_git_cmd: list[str] | None = None,
        *,
        extra_args: list[str] | None = None,
        overrides: dict[str, object] | None = None,
    ) -> tuple[int, str, str]:
        """Run push_guard.main() in-process with monkeypatched _GIT_CMD.

        Args:
            monkeypatch: pytest monkeypatch fixture.
            cwd: Working directory for the guard (the git repo).
            fake_git_cmd: Command list for push_guard._GIT_CMD, or None
                to use real git.
            extra_args: Arguments after ``--base main``.
            overrides: Module attributes to set on this run's copy.

        The guard's bounds are pinned well under the suite's per-test timeout,
        so a hang fails this test by name instead of costing the worker.

        Returns:
            (exit_code, stdout, stderr) — mirrors subprocess interface.
        """
        # A private copy per run: no shared module state between tests.
        push_guard = _load_push_guard()

        # Monkeypatch the git command injection point.
        if fake_git_cmd is not None:
            monkeypatch.setattr(push_guard, "_GIT_CMD", fake_git_cmd)
        monkeypatch.setattr(push_guard, "GIT_TIMEOUT_S", _INPROCESS_TIMEOUT_S)
        monkeypatch.setattr(push_guard, "HOOK_TIMEOUT_S", _INPROCESS_TIMEOUT_S)
        monkeypatch.setattr(push_guard, "FETCH_STALL_S", _INPROCESS_TIMEOUT_S)
        for name, value in (overrides or {}).items():
            monkeypatch.setattr(push_guard, name, value)
        # Monkeypatch argv for argparse.
        monkeypatch.setattr(sys, "argv", ["push_guard.py", "--base", "main", *(extra_args or [])])

        # Change working directory to the test repo.
        monkeypatch.chdir(cwd)

        # Capture stdout and stderr.
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_buf)
        monkeypatch.setattr(sys, "stderr", stderr_buf)

        # Run main(); it returns an exit code (does not call sys.exit).
        exit_code = push_guard.main()

        return exit_code, stdout_buf.getvalue(), stderr_buf.getvalue()

    def test_revlist_ahead_failure_refuses(self, repo_pair, tmp_path, monkeypatch):
        """rev-list origin/<base>..HEAD failure → exit 40, not SAFE."""
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/revlist-fail-test")
        Path(clone_dir, "change.py").write_text("# change\n")
        _git(clone_dir, "add", "change.py")
        _git(clone_dir, "commit", "-m", "feat: test commit")

        fake_cmd = self._make_fake_git_cmd(
            tmp_path, "args[:1] == ['rev-list'] and any('..' in a for a in args)"
        )

        rc, stdout, stderr = self._run_push_guard_inprocess(monkeypatch, clone_dir, fake_cmd)
        assert rc == 40, (
            f"Expected refused (40), got {rc}. A failing "
            f"rev-list must not produce SAFE TO PUSH.\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )
        assert "SAFE TO PUSH" not in stdout
        combined = (stdout + stderr).lower()
        assert "refused" in combined, (
            "Expected a REFUSED diagnostic on rev-list failure.\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )

    def test_difftree_failure_refuses(self, repo_pair, tmp_path, monkeypatch):
        """diff-tree -p failure → exit 40, not skip-and-SAFE."""
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/difftree-fail-test")
        Path(clone_dir, "change.py").write_text("# change\n")
        _git(clone_dir, "add", "change.py")
        _git(clone_dir, "commit", "-m", "feat: test commit")

        fake_cmd = self._make_fake_git_cmd(tmp_path, "'diff-tree' in args")

        rc, stdout, stderr = self._run_push_guard_inprocess(monkeypatch, clone_dir, fake_cmd)
        assert rc == 40, (
            f"Expected refused (40), got {rc}. A failing "
            f"diff-tree must not produce SAFE TO PUSH.\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )
        assert "SAFE TO PUSH" not in stdout
        combined = (stdout + stderr).lower()
        assert "refused" in combined and "diff-tree" in combined, (
            "Expected a REFUSED diagnostic mentioning diff-tree.\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )

    def test_revlist_base_failure_refuses(self, repo_pair, tmp_path, monkeypatch):
        """rev-list --max-count base-history failure → exit 40, not SAFE."""
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/base-revlist-fail-test")
        Path(clone_dir, "change.py").write_text("# change\n")
        _git(clone_dir, "add", "change.py")
        _git(clone_dir, "commit", "-m", "feat: test commit")

        fake_cmd = self._make_fake_git_cmd(
            tmp_path,
            "any('--max-count' in a or a.startswith('--max-count=') for a in args)",
        )

        rc, stdout, stderr = self._run_push_guard_inprocess(monkeypatch, clone_dir, fake_cmd)
        assert rc == 40, (
            f"Expected refused (40), got {rc}. A failing base "
            f"rev-list must not produce SAFE TO PUSH.\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )
        assert "SAFE TO PUSH" not in stdout
        combined = (stdout + stderr).lower()
        assert "refused" in combined and "rev-list" in combined, (
            "Expected a REFUSED diagnostic mentioning rev-list.\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )

    def test_patchid_failure_refuses(self, repo_pair, tmp_path, monkeypatch):
        """patch-id --stable failure → exit 40, and it is the INJECTED git that fails.

        Both ``patch-id`` calls go through ``run()`` and so through ``_GIT_CMD``,
        which is what lets this fake reach them: a bare ``git`` spawn of their
        own would be untestable here and would cost every run of the class a
        real host ``git``. Failing the fake on ``patch-id`` and getting the refusal proves
        the calls now go through ``run()`` like every other command.
        """
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/patchid-fail-test")
        Path(clone_dir, "change.py").write_text("# change\n")
        _git(clone_dir, "add", "change.py")
        _git(clone_dir, "commit", "-m", "feat: test commit")

        fake_cmd = self._make_fake_git_cmd(tmp_path, "'patch-id' in args")

        rc, stdout, stderr = self._run_push_guard_inprocess(monkeypatch, clone_dir, fake_cmd)
        assert rc == 40, (
            f"Expected refused (40), got {rc}. A failing "
            f"patch-id must not produce SAFE TO PUSH.\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )
        assert "SAFE TO PUSH" not in stdout
        combined = (stdout + stderr).lower()
        assert "refused" in combined and "patch-id" in combined, (
            "Expected a REFUSED diagnostic mentioning patch-id.\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )

    def test_empty_ahead_set_on_success_is_still_safe(self, repo_pair):
        """rev-list SUCCEEDS with empty output (0 ahead commits) → still SAFE."""
        clone_dir, _ = repo_pair

        # Stay on main — 0 commits ahead of origin/main.
        rc, stdout, stderr = _run_push_guard(clone_dir)
        assert rc == 0, (
            f"Expected safe (0), got {rc}. No ahead commits should be "
            f"safe.\nstdout: {stdout}\nstderr: {stderr}"
        )
        assert "SAFE TO PUSH" in stdout


class TestNonUtf8Decoding:
    """Regression: non-UTF-8 tracked content must not crash the guard.


    At 8ed873cf the shared run() helper used subprocess.run(..., text=True)
    with strict decoding.  Any non-UTF-8 byte in git diff-tree output (which
    carries raw patch content for the patch-id replay check) raised an
    uncaught UnicodeDecodeError, preventing the push workflow.

    Fix: errors="replace" on all subprocess.run(..., text=True) calls.
    """

    def test_ahead_commit_with_non_utf8_content(self, repo_pair):
        """Ahead-commit adds a file with non-UTF-8 bytes → guard completes."""
        clone_dir, _ = repo_pair

        _git(clone_dir, "checkout", "-b", "feature/binary-content")
        # Write raw bytes that are invalid UTF-8.
        Path(clone_dir, "binary_data.bin").write_bytes(b"\xff\xfe latin \xe9 end")
        _git(clone_dir, "add", "binary_data.bin")
        _git(clone_dir, "commit", "-m", "feat: add binary data file")

        _bless(clone_dir)
        rc, stdout, stderr = _run_push_guard(clone_dir)
        assert rc == 0, (
            f"Expected safe (0), got {rc}. Guard must not crash on "
            f"non-UTF-8 content.\nstdout: {stdout}\nstderr: {stderr}"
        )
        assert "SAFE TO PUSH" in stdout

    def test_base_history_with_non_utf8_content(self, repo_pair):
        """Base-history commit (within replay window) has non-UTF-8 → guard completes."""
        clone_dir, origin_dir = repo_pair

        # Push a commit with non-UTF-8 bytes to origin/main (base history).
        work2 = os.path.dirname(clone_dir) + "/work2"
        _git(os.path.dirname(clone_dir), "clone", origin_dir, "work2")
        Path(work2, "upstream_binary.bin").write_bytes(b"\xff\xfe\x80\x81 raw bytes")
        _git(work2, "add", "upstream_binary.bin")
        _git(work2, "commit", "-m", "chore: add binary asset")
        _git(work2, "push", "origin", "main")

        # Fetch so origin/main is current, then branch from the fresh tip.
        _git(clone_dir, "fetch", "origin")
        _git(clone_dir, "checkout", "-b", "feature/after-binary-base", "origin/main")
        Path(clone_dir, "novel_fix.py").write_text("# novel fix\n")
        _git(clone_dir, "add", "novel_fix.py")
        _git(clone_dir, "commit", "-m", "fix: novel fix after binary base")

        _bless(clone_dir)
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "5"])
        assert rc == 0, (
            f"Expected safe (0), got {rc}. Guard must not crash on "
            f"non-UTF-8 base history.\nstdout: {stdout}\nstderr: {stderr}"
        )
        assert "SAFE TO PUSH" in stdout


class TestClassifyFetchError:
    """Regression tests for _classify_fetch_error (round-13 derived diagnostic).

    Contract: raw stderr content NEVER reaches refusal output.  Only
    hardcoded class labels from the allowlist are surfaced.  When no pattern
    matches, a generic withholding message is returned.
    """

    @staticmethod
    def _import_classify():
        return _load_push_guard()._classify_fetch_error

    def test_known_class_could_not_resolve_host(self):
        classify = self._import_classify()
        result = classify(
            "fatal: unable to access 'https://example.com/repo.git/': "
            "Could not resolve host: example.com"
        )
        assert result == "could not resolve host"

    def test_known_class_permission_denied(self):
        classify = self._import_classify()
        assert classify("Permission denied, please try again.") == "permission denied"
        # Under BatchMode an unloaded key reads the same way; name the real fix.
        assert "ssh-add" in classify("Permission denied (publickey).")

    def test_known_class_repository_not_found(self):
        classify = self._import_classify()
        result = classify("remote: Repository not found.\nfatal: ...")
        assert result == "repository not found"

    def test_known_class_not_a_git_repository(self):
        classify = self._import_classify()
        result = classify("fatal: '/tmp/notgit' does not appear to be a git repository")
        assert result == "not a git repository"

    def test_known_class_timeout(self):
        classify = self._import_classify()
        result = classify("fatal: unable to access: Failed to connect: Connection timed out")
        assert result == "connection timed out"

    def test_known_class_authentication_failed(self):
        classify = self._import_classify()
        result = classify(
            "remote: Invalid username or password.\n"
            "fatal: Authentication failed for 'https://github.com/org/repo.git/'"
        )
        assert result == "authentication failed"

    def test_known_class_remote_ref_not_found(self):
        classify = self._import_classify()
        result = classify("fatal: couldn't find remote ref refs/heads/nonexistent")
        assert result == "remote ref not found"

    def test_known_class_connection_refused(self):
        classify = self._import_classify()
        result = classify("fatal: unable to access: Failed to connect: Connection refused")
        assert result == "connection refused"

    def test_known_class_tls_error(self):
        classify = self._import_classify()
        result = classify(
            "fatal: unable to access: SSL certificate problem: "
            "unable to get local issuer certificate"
        )
        assert result == "TLS/certificate error"

    def test_bare_token_in_stderr_withheld(self):
        """A bare token (no URL shape) in stderr must NEVER reach output.

        This is the core round-13 regression: an ext:: remote helper can echo
        arbitrary command lines including bare credential strings that no
        URL-shape scrubber can redact.
        """
        classify = self._import_classify()
        stderr_with_bare_token = (
            "external helper failed: /usr/lib/git-core/git-remote-ext "
            "--run tok_secret123 --endpoint api.internal.corp"
        )
        result = classify(stderr_with_bare_token)
        # Must not contain the token
        assert "tok_secret123" not in result
        # Must be the generic withholding message
        assert "details withheld" in result

    def test_unrecognized_free_text_withheld(self):
        """Completely novel stderr text is withheld entirely."""
        classify = self._import_classify()
        result = classify("some-custom-hook: unexpected error code 42 from internal endpoint")
        assert "details withheld" in result
        assert "some-custom-hook" not in result

    def test_empty_stderr_withheld(self):
        """Empty stderr returns the withholding message."""
        classify = self._import_classify()
        result = classify("")
        assert "details withheld" in result


class TestFetchDiagnosticIntegration:
    """Integration test: fetch failure refusal output never leaks raw stderr."""

    @staticmethod
    def _import_push_guard():
        return _load_push_guard()

    def test_fetch_failure_with_bare_token_no_leak(self, tmp_path, monkeypatch):
        """When git fetch fails with stderr containing a bare token,
        the refusal output must not contain that token."""
        push_guard = self._import_push_guard()

        # Monkeypatch run() to simulate a fetch failure with bare token stderr
        original_run = push_guard.run

        def fake_run(args, **kwargs):
            if "fetch" in args:
                return (
                    128,
                    "",
                    "ext::helper --token ghp_SUPERSECRET123abc --host internal.corp",
                )
            if "symbolic-ref" in args:
                return (0, "origin/main", "")
            if "rev-parse" in args and "--is-inside-work-tree" in args:
                return (0, "true", "")
            return original_run(args, **kwargs)

        monkeypatch.setattr(push_guard, "run", fake_run)

        # Capture stderr from _fetch_base
        stderr_buf = io.StringIO()
        monkeypatch.setattr(sys, "stderr", stderr_buf)

        with pytest.raises(push_guard.Refused):
            push_guard._fetch_base("main")
        rc = 40
        stderr_output = stderr_buf.getvalue()

        # Guard must refuse (fetch failure)
        assert rc == 40
        # The bare token must NOT appear in stderr output
        assert "ghp_SUPERSECRET123abc" not in stderr_output
        # But it should mention the error class or withholding
        assert "details withheld" in stderr_output or "error class" in stderr_output

    def test_fetch_failure_known_class_shows_label(self, tmp_path, monkeypatch):
        """When fetch fails with a known error pattern, the label is shown."""
        push_guard = self._import_push_guard()

        def fake_run(args, **kwargs):
            if "fetch" in args:
                return (128, "", "fatal: Could not resolve host: github.com")
            return (0, "", "")

        monkeypatch.setattr(push_guard, "run", fake_run)

        stderr_buf = io.StringIO()
        monkeypatch.setattr(sys, "stderr", stderr_buf)

        with pytest.raises(push_guard.Refused):
            push_guard._fetch_base("main")
        stderr_output = stderr_buf.getvalue()

        assert "could not resolve host" in stderr_output
        # Raw URL should not appear
        assert "github.com" not in stderr_output


def _commit_tree(clone_dir: str, files: dict[str, str | None], message: str) -> None:
    """Write (or, for ``None``, delete) each file, stage everything, commit.

    Off ``main`` this is a commit of the branch's own, so its paths enter the
    guard's build record as ``--commit`` would leave them. A commit made BY
    HAND (the provenance tests) uses ``_git(..., "commit")`` directly.
    """
    for rel, content in files.items():
        path = Path(clone_dir, rel)
        if content is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    _git(clone_dir, "add", "-A")
    _git(clone_dir, "commit", "-q", "-m", message)
    on_main = _git(clone_dir, "symbolic-ref", "-q", "HEAD") == "refs/heads/main"
    if not on_main and _git(clone_dir, "rev-list", "--count", "HEAD") != "1":
        _bless(clone_dir, "HEAD^")


def _built_dir(clone_dir: str) -> Path:
    return Path(clone_dir, _git(clone_dir, "rev-parse", "--git-common-dir"), _PG._BUILT_DIR)


def _bless(clone_dir: str, since: str = "refs/remotes/origin/main") -> None:
    """Record every path HEAD changes against ``since`` as the guard's own.

    For tests whose subject is another check, on commits made with plain git:
    this is the record ``--commit`` would have left for HEAD.
    """
    paths = _git(clone_dir, "diff-tree", "-r", "--name-only", "--no-renames", since, "HEAD")
    git_dir = _git(clone_dir, "rev-parse", "--absolute-git-dir")
    _PG.record_built(git_dir, _rev(clone_dir, "HEAD"), {p.encode() for p in paths.split()})


def _land_on_main(clone_dir: str, files: dict[str, str | None], message: str) -> None:
    """Commit on main and push it, the way a merged upstream PR lands."""
    _git(clone_dir, "checkout", "-q", "main")
    _commit_tree(clone_dir, files, message)
    _git(clone_dir, "push", "-q", "origin", "main")


def _rev(repo: str, rev: str) -> str:
    return _git(repo, "rev-parse", "--verify", rev)


def _feature(clone_dir: str, files: dict[str, str | None] | None = None) -> str:
    """A feature branch with one commit of its own; returns its HEAD."""
    _git(clone_dir, "checkout", "-q", "-b", "feature/x")
    _commit_tree(clone_dir, files or {"mine.py": "# mine\n"}, "feat: mine")
    return _rev(clone_dir, "HEAD")


_PG = _load_push_guard()  # constants and pure helpers only; runs use a fresh copy
# The identity a guard run commits as (conftest's ``_git_identity``). Fixture
# commits are another author's (``_fixture_git_env``) unless they name this.
_GUARD_IDENT = "Test <test@example.com>"


def _message_path(clone_dir: str) -> str:
    """The checked-out branch's default squash message file."""
    return _PG.squash_message_path(
        _git(clone_dir, "rev-parse", "--absolute-git-dir"),
        _git(clone_dir, "symbolic-ref", "--short", "HEAD"),
    )


def _write_message(clone_dir: str, text: str = "feat: the squashed change\n\nDetail.\n") -> str:
    """The squash message at its default home, inside git's own directory."""
    path = _message_path(clone_dir)
    Path(path).write_text(text, encoding="utf-8")
    return path


def _spy_popen(monkeypatch, on_init=None, on_wait=None):
    """Wrap subprocess.Popen for an in-process guard run.

    ``on_init(argv, kwargs)`` runs before each spawn; ``on_wait(argv, timeout)``
    before each wait and may raise (a simulated hang).
    """
    real = subprocess.Popen

    class _Popen(real):  # type: ignore[misc, valid-type]
        def __init__(self, args, *a, **kw):
            if on_init:
                on_init(list(args), kw)
            super().__init__(args, *a, **kw)

        def wait(self, timeout=None):
            if on_wait:
                on_wait(list(self.args), timeout)
            return super().wait(timeout)

    monkeypatch.setattr(subprocess, "Popen", _Popen)


def _hang_on(monkeypatch, match):
    """Make each guard call whose argv satisfies ``match`` time out at its own bound."""

    def hang(argv, timeout):
        if timeout == _INPROCESS_TIMEOUT_S and match(argv):
            raise subprocess.TimeoutExpired(argv, timeout)

    _spy_popen(monkeypatch, on_wait=hang)


_inprocess = TestReplayFailClosed._run_push_guard_inprocess


def _staged_list(stderr: str) -> list[bytes]:
    """The entries of the list file a refusal printed (one file per refusal)."""
    path = next(line.split(": ", 1)[1] for line in stderr.splitlines() if "Full list" in line)
    return [p for p in Path(path).read_bytes().split(b"\0") if p]


def _overlay(clone_dir: str) -> str:
    """Main moves README on; an older main is checked out over this tree (the incident)."""
    old_main = _rev(clone_dir, "HEAD")
    _land_on_main(clone_dir, {"README.md": "v2\n"}, "docs: readme")
    head = _feature(clone_dir)
    _git(clone_dir, "checkout", old_main, "--", ".")
    return head


class TestPushGuardSquash:
    """``--squash`` commits the branch's TREE with ``commit-tree``, through no index.

    A scratch index of that tree exists only for the message hooks.

    A soft reset onto the base plus ``git commit`` takes whatever the shared
    index holds. A stale one (another session's ``git checkout <old ref> --
    .``) became a commit sitting exactly where a good squash sits, and the
    post-squash guard read "SAFE TO PUSH (single commit on base)" while it
    reverted 5607 files. Building from HEAD's tree has no threshold to tune.
    """

    def test_normal_pr_runs_the_whole_flow(self, repo_pair):
        """Pre-squash guard, squash, post-squash guard: the wiring end to end."""
        clone_dir, _ = repo_pair
        _land_on_main(clone_dir, {"app.py": "x = 1\n"}, "feat: app")
        _feature(clone_dir, {"app.py": "x = 2\n"})
        _commit_tree(clone_dir, {"new.py": "# new\n"}, "feat: two")
        pre_tree = _rev(clone_dir, "HEAD:")
        message = _write_message(clone_dir)

        rc, stdout, stderr = _run_push_guard(clone_dir)
        assert rc == 0, f"stdout: {stdout}\nstderr: {stderr}"
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 0, f"stdout: {stdout}\nstderr: {stderr}"
        assert "STATUS: SQUASHED" in stdout

        assert _rev(clone_dir, "HEAD:") == pre_tree
        assert _git(clone_dir, "log", "-1", "--format=%P") == _rev(
            clone_dir, "refs/remotes/origin/main"
        )
        assert _git(clone_dir, "log", "-1", "--format=%s") == "feat: the squashed change"
        assert _git(clone_dir, "symbolic-ref", "--short", "HEAD") == "feature/x"
        assert _git(clone_dir, "status", "--porcelain") == ""
        assert not Path(message).exists(), "a used message must not be reused next time"
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--require-single-on-base"])
        assert rc == 0, f"stdout: {stdout}\nstderr: {stderr}"
        assert "SAFE TO PUSH (single commit on base)" in stdout

    def test_a_stale_overlay_never_reaches_the_squash(self, repo_pair):
        """The incident order: an older tree is staged; the squash refuses, then keeps its own."""
        clone_dir, _ = repo_pair
        head = _overlay(clone_dir)
        own_tree = _rev(clone_dir, "HEAD:")
        _write_message(clone_dir)

        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 41 and "README.md" in stderr, stderr
        assert _rev(clone_dir, "HEAD") == head
        _git(clone_dir, "restore", "--source=HEAD", "--staged", "--worktree", "--", "README.md")
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 0, f"stdout: {stdout}\nstderr: {stderr}"
        assert _rev(clone_dir, "HEAD:") == own_tree
        assert _git(clone_dir, "show", "HEAD:README.md") == "v2"

    def test_a_squash_made_by_hand_is_not_blessed(self, repo_pair):
        """The incident commit itself: a soft reset plus commit of the stale index."""
        clone_dir, _ = repo_pair
        _overlay(clone_dir)
        _git(clone_dir, "reset", "-q", "--soft", "refs/remotes/origin/main")
        _git(clone_dir, "commit", "-q", "-m", "feat: squash")

        rc, _, stderr = _run_push_guard(clone_dir, ["--require-single-on-base"])
        assert rc == 40 and "push_guard.py did not commit on this branch" in stderr, stderr
        listed = stderr.split("REFUSED: HEAD")[1].split("Read each one")[0]
        # The reset is a boundary: nothing recorded before it vouches for a path.
        assert "README.md" in listed and "mine.py" in listed, stderr
        _write_message(clone_dir)
        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 40 and "README.md" in stderr, "a bare re-squash must not bless it either"

    def test_a_merge_commit_is_not_a_single_commit_on_base(self, repo_pair):
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "side")
        _commit_tree(clone_dir, {"side.py": "s\n"}, "feat: side")
        _git(clone_dir, "checkout", "-q", "-b", "feature/x", "main")
        _git(clone_dir, "merge", "-q", "--no-ff", "-m", "merge side", "side")

        rc, _, stderr = _run_push_guard(clone_dir, ["--require-single-on-base"])
        assert rc == 40 and "are not exactly origin/main" in stderr, stderr

    def test_squash_runs_the_history_checks_first(self, repo_pair):
        """No brief ordering can skip the count: --squash refuses what the guard refuses."""
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        for i in range(12):
            _commit_tree(clone_dir, {f"trunk{i}.py": "x\n"}, f"wip: trunk {i}")
        head = _rev(clone_dir, "HEAD")
        _write_message(clone_dir)

        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "2", "--squash"])
        assert rc == 40, f"stdout: {stdout}\nstderr: {stderr}"
        assert "13 commits ahead" in stderr and "--squash --max-ahead 13" in stderr
        assert _rev(clone_dir, "HEAD") == head

    @pytest.mark.parametrize(
        ("main_files", "branch_files"),
        [
            pytest.param(
                {f"evidence/s{i}.txt": f"{i}\n" for i in range(51)},
                dict.fromkeys(f"evidence/s{i}.txt" for i in range(51)),
                id="bulk-delete",
            ),
            pytest.param(
                {f"oldpkg/m{i}.py": f"import oldpkg.x{i}\n" for i in range(60)},
                {
                    **dict.fromkeys(f"oldpkg/m{i}.py" for i in range(60)),
                    **{f"newpkg/m{i}.py": f"import newpkg.y{i}\n" for i in range(60)},
                },
                id="move-and-rewrite",
            ),
            pytest.param(
                {f"src/c{i}.py": "print('x')\n" for i in range(15)},
                {f"src/c{i}.py": "print 'x'\n" for i in range(15)},
                id="codemod-undo",
            ),
        ],
    )
    def test_a_large_change_of_the_branchs_own_squashes_unchanged(
        self, repo_pair, main_files, branch_files
    ):
        """Shapes a content heuristic calls "stale": the squash just keeps the tree."""
        clone_dir, _ = repo_pair
        _land_on_main(clone_dir, main_files, "feat: base")
        _feature(clone_dir, branch_files)
        pre_tree = _rev(clone_dir, "HEAD:")
        _write_message(clone_dir)

        rc, stdout, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 0, f"stdout: {stdout}\nstderr: {stderr}"
        assert _rev(clone_dir, "HEAD:") == pre_tree

    def test_an_exact_reland_is_refused_by_the_replay_check(self, repo_pair):
        """A known residual: re-landing a reverted change verbatim reads as a replay."""
        clone_dir, _ = repo_pair
        _land_on_main(clone_dir, {"f.py": "a\n"}, "feat: base")
        _land_on_main(clone_dir, {"f.py": "b\n"}, "feat: x")
        _git(clone_dir, "revert", "--no-edit", "HEAD")
        _git(clone_dir, "push", "-q", "origin", "main")
        _feature(clone_dir, {"f.py": "b\n"})
        _write_message(clone_dir)

        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 40 and "patch-equivalent" in stderr, stderr

    def test_a_base_that_moved_since_the_rebase_is_refused(self, repo_pair):
        clone_dir, _ = repo_pair
        head = _feature(clone_dir)
        _land_on_main(clone_dir, {"upstream.txt": "new\n"}, "feat: upstream")
        _git(clone_dir, "checkout", "-q", "feature/x")
        _git(clone_dir, "update-ref", "refs/remotes/origin/main", "main~1")
        _write_message(clone_dir)

        rc, stdout, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 40, f"stdout: {stdout}\nstderr: {stderr}"
        assert "not based on the fresh origin/main" in stderr
        assert "git rebase refs/remotes/origin/main" in stderr
        assert _rev(clone_dir, "HEAD") == head

    def test_a_stray_local_branch_named_like_the_base_is_not_the_base(self, repo_pair):
        """``origin/main`` would resolve ``refs/heads/origin/main`` first."""
        clone_dir, _ = repo_pair
        stale = _rev(clone_dir, "HEAD")
        _land_on_main(clone_dir, {"up.txt": "1\n"}, "feat: upstream")
        _git(clone_dir, "branch", "origin/main", stale)
        _feature(clone_dir)
        _write_message(clone_dir)

        rc, stdout, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 0, f"stdout: {stdout}\nstderr: {stderr}"
        assert _git(clone_dir, "log", "-1", "--format=%P") == _rev(
            clone_dir, "refs/remotes/origin/main"
        )
        assert _git(clone_dir, "show", "HEAD:up.txt") == "1"

    def test_the_base_named_by_origin_head_survives_a_stray_ref(self, repo_pair, monkeypatch):
        clone_dir, _ = repo_pair
        _git(clone_dir, "push", "-q", "origin", "main:develop")
        _git(clone_dir, "fetch", "-q", "origin")
        _git(clone_dir, "remote", "set-head", "origin", "develop")
        _git(clone_dir, "branch", "origin/develop")
        monkeypatch.chdir(clone_dir)
        pg = _load_push_guard()
        monkeypatch.setattr(pg, "GIT_TIMEOUT_S", _INPROCESS_TIMEOUT_S)
        assert pg._resolve_base("") == "develop"

    def test_the_base_branch_itself_is_never_squashed(self, repo_pair):
        clone_dir, _ = repo_pair
        for i in range(3):
            _commit_tree(clone_dir, {f"local{i}.py": "x\n"}, f"wip: local {i}")
        head = _rev(clone_dir, "HEAD")
        _write_message(clone_dir)
        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 40 and "is the base (main)" in stderr, stderr
        assert _rev(clone_dir, "HEAD") == head

    def test_nothing_ahead_is_nothing_to_squash(self, repo_pair):
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        _write_message(clone_dir)
        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 40 and "nothing to squash" in stderr, stderr

    def test_a_net_empty_branch_is_refused_and_the_branch_restored(self, repo_pair):
        """git commit's own "nothing to commit" refusal still applies, and the branch goes back."""
        clone_dir, _ = repo_pair
        _feature(clone_dir, {"README.md": "changed\n"})
        _commit_tree(clone_dir, {"README.md": "initial\n"}, "revert: mine")
        head = _rev(clone_dir, "HEAD")
        _write_message(clone_dir)

        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 40 and "changes nothing against origin/main" in stderr, stderr
        assert _rev(clone_dir, "HEAD") == head

    @pytest.mark.parametrize(
        ("message", "says"),
        [
            pytest.param("\n\n  \n", "is empty", id="blank"),
            pytest.param(None, "cannot read", id="missing"),
            pytest.param("﻿feat: x\n", None, id="bom"),
        ],
    )
    def test_the_message_is_checked_before_any_change(self, repo_pair, message, says):
        clone_dir, _ = repo_pair
        head = _feature(clone_dir)
        path = _message_path(clone_dir)
        if message is not None:
            Path(path).write_text(message, encoding="utf-8")
        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"])
        if says is None:
            assert rc == 0, stderr
            assert _git(clone_dir, "log", "-1", "--format=%s") == "feat: x"
            return
        assert rc == 40 and says in stderr and path in stderr, stderr
        assert _rev(clone_dir, "HEAD") == head

    def test_stdin_is_not_a_message_file(self, repo_pair):
        clone_dir, _ = repo_pair
        rc, _, stderr = _run_push_guard(clone_dir, ["--squash", "-"])
        assert rc == _PG.EXIT_USAGE and "stdin" in stderr, stderr

    def test_another_branchs_message_is_never_used(self, repo_pair):
        """The default file is keyed to the branch, so the previous PR's cannot leak in."""
        clone_dir, _ = repo_pair
        _write_message(clone_dir, "feat: the previous PR\n")  # main's file
        _feature(clone_dir)
        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 40 and "prepare-pr-commit-msg-feature-x.txt" in stderr, stderr

    def test_a_named_message_file_is_left_to_its_owner(self, repo_pair, tmp_path):
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        mine = tmp_path / "my-message.txt"
        mine.write_text("feat: named\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--squash", str(mine)])
        assert rc == 0, stderr
        assert mine.exists()

    def test_a_relative_message_path_is_the_callers(self, repo_pair):
        """``-F`` resolves against the worktree top; the guard resolves it where you stand."""
        clone_dir, _ = repo_pair
        _feature(clone_dir, {"docs/a.md": "a\n"})
        Path(clone_dir, "msg.txt").write_text("chore: the wrong message\n")
        Path(clone_dir, "docs", "msg.txt").write_text("docs: the right message\n")

        rc, stdout, stderr = _run_push_guard(str(Path(clone_dir, "docs")), ["--squash", "msg.txt"])
        assert rc == 0, f"stdout: {stdout}\nstderr: {stderr}"
        assert _git(clone_dir, "log", "-1", "--format=%s") == "docs: the right message"

    def test_a_sparse_checkout_squashes_from_the_tree(self, repo_pair):
        """No index is involved, so a sparse checkout squashes like any other."""
        clone_dir, _ = repo_pair
        _feature(clone_dir, {"in/a.py": "a\n", "out/b.py": "b\n"})
        tree = _rev(clone_dir, "HEAD:")
        _git(clone_dir, "sparse-checkout", "set", "in")
        _write_message(clone_dir)
        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 0, stderr
        assert _rev(clone_dir, "HEAD:") == tree

    def test_a_squash_leaves_no_files_behind(self, repo_pair):
        clone_dir, _ = repo_pair
        _git(clone_dir, "config", "core.splitIndex", "true")
        _feature(clone_dir)
        git_dir = Path(_git(clone_dir, "rev-parse", "--absolute-git-dir"))
        before = set(git_dir.glob("sharedindex.*"))
        _write_message(clone_dir)
        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 0, stderr
        assert set(git_dir.glob("sharedindex.*")) == before
        assert not list(git_dir.glob("push_guard-squash-*"))
        assert not list(git_dir.glob("push_guard-message-*"))

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="hook and gpg stand-ins are sh")
    def test_hooks_and_signing_run_as_for_any_commit(self, repo_pair, tmp_path):
        clone_dir, _ = repo_pair
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "commit-msg").write_text('#!/bin/sh\nprintf "\\nChange-Id: I123\\n" >> "$1"\n')
        gpg = tmp_path / "fake-gpg"
        gpg.write_text(
            "#!/bin/sh\ncat >/dev/null\n"
            "echo '[GNUPG:] SIG_CREATED D 1 8 00 0 FP' >&2\n"
            "printf -- '-----BEGIN PGP SIGNATURE-----\\nZmFrZQ==\\n-----END PGP SIGNATURE-----\\n'\n"
        )
        for script in (hooks / "commit-msg", gpg):
            script.chmod(0o755)
        _feature(clone_dir)
        _write_message(clone_dir)
        for key, value in (
            ("core.hooksPath", str(hooks)),
            ("commit.gpgSign", "true"),
            ("user.signingKey", "test"),
            ("gpg.program", str(gpg)),
        ):
            _git(clone_dir, "config", key, value)

        rc, stdout, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 0, f"stdout: {stdout}\nstderr: {stderr}"
        raw = _git(clone_dir, "cat-file", "commit", "HEAD")
        assert "gpgsig" in raw and "Change-Id: I123" in raw

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="the hook stand-in is sh")
    def test_a_rejecting_commit_msg_hook_refuses_and_moves_nothing(self, repo_pair, tmp_path):
        clone_dir, _ = repo_pair
        head = _feature(clone_dir)
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        hook = hooks / "commit-msg"
        hook.write_text("#!/bin/sh\necho 'subject must be imperative' >&2\nexit 1\n")
        hook.chmod(0o755)
        _git(clone_dir, "config", "core.hooksPath", str(hooks))
        _write_message(clone_dir)

        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 40 and "commit-msg hook rejected" in stderr, stderr
        assert "subject must be imperative" in stderr
        assert _rev(clone_dir, "HEAD") == head

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="the hook stand-in is sh")
    def test_hooks_see_the_callers_locale_and_prompt_settings(self, repo_pair, tmp_path):
        """Only the guard's own probes run with LC_ALL=C and no prompts; user code does not."""
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        seen = tmp_path / "seen.txt"
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        hook = hooks / "commit-msg"
        hook.write_text(
            "#!/bin/sh\n"
            'echo "$LC_ALL|${GIT_TERMINAL_PROMPT-unset}" > ' + shlex.quote(str(seen)) + "\n"
        )
        hook.chmod(0o755)
        _git(clone_dir, "config", "core.hooksPath", str(hooks))
        _write_message(clone_dir, "feat: café\n")
        env = {k: v for k, v in os.environ.items() if k != "GIT_TERMINAL_PROMPT"}
        env["LC_ALL"] = "C.UTF-8"
        proc = run_bounded(
            [sys.executable, PUSH_GUARD, "--base", "main", "--squash"],
            env,
            timeout=_SUBPROCESS_TIMEOUT_S,
            cwd=clone_dir,
        )
        assert proc.returncode == 0, proc.stderr
        assert seen.read_text().strip() == "C.UTF-8|unset"

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="the hook stand-in is sh")
    def test_caller_config_from_the_environment_applies(self, repo_pair, tmp_path):
        """GIT_CONFIG_COUNT carries the caller's choices (a hooks path here); it is kept."""
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        hook = hooks / "commit-msg"
        hook.write_text(
            "#!/bin/sh\n" 'printf "\\nSigned-off-by: Env <env@example.invalid>\\n" >> "$1"\n'
        )
        hook.chmod(0o755)
        _write_message(clone_dir)
        rc, _, stderr = _run_push_guard(
            clone_dir,
            ["--squash"],
            env_extra={
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.hooksPath",
                "GIT_CONFIG_VALUE_0": str(hooks),
            },
        )
        assert rc == 0, stderr
        assert "Signed-off-by: Env" in _git(clone_dir, "log", "-1", "--format=%B")

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="needs a POSIX process group")
    @pytest.mark.parametrize("sig", ["SIGINT", "SIGTERM"])
    def test_an_interrupt_during_a_hook_leaves_the_branch_and_no_files(
        self, repo_pair, tmp_path, sig
    ):
        """The branch moves only in the final compare-and-swap, so a kill before it changes nothing."""
        clone_dir, _ = repo_pair
        head = _feature(clone_dir)
        mark = tmp_path / "hook-started"
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        hook = hooks / "commit-msg"
        hook.write_text("#!/bin/sh\ntouch " + shlex.quote(str(mark)) + "\nsleep 20\n")
        hook.chmod(0o755)
        _git(clone_dir, "config", "core.hooksPath", str(hooks))
        _write_message(clone_dir)

        proc = subprocess.Popen(
            [sys.executable, PUSH_GUARD, "--base", "main", "--squash"],
            cwd=clone_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            for _ in range(300):
                if mark.exists() or proc.poll() is not None:
                    break
                time.sleep(0.1)
            assert mark.exists(), "the hook never started"
            # SIGINT to the group is what a terminal's Ctrl-C sends.
            os.killpg(proc.pid, getattr(signal, sig))
            _, err = proc.communicate(timeout=_SUBPROCESS_TIMEOUT_S)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        assert proc.returncode == 40, err
        assert b"interrupted" in err
        assert _rev(clone_dir, "HEAD") == head
        git_dir = Path(_git(clone_dir, "rev-parse", "--absolute-git-dir"))
        assert not list(git_dir.glob("push_guard-squash-*"))

    def test_a_commit_landing_during_the_checks_is_never_squashed(self, repo_pair, monkeypatch):
        """HEAD is pinned once: commits that arrive during the replay scan refuse the squash."""
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        _write_message(clone_dir)
        landed: list[str] = []

        def land(argv, kwargs):
            if "patch-id" in argv and not landed:
                landed.append("x")
                _git(clone_dir, "commit", "-q", "--allow-empty", "-m", "feat: concurrent")

        _spy_popen(monkeypatch, on_init=land)
        rc, _, stderr = _inprocess(monkeypatch, clone_dir, extra_args=["--squash"])
        assert rc == 40 and "moved while the guard ran" in stderr, stderr

    @pytest.mark.parametrize("state", ["MERGE_HEAD", "CHERRY_PICK_HEAD"])
    def test_another_operations_state_is_never_consumed(self, repo_pair, monkeypatch, state):
        """commit-tree reads no MERGE_HEAD or CHERRY_PICK_HEAD, so another session's
        merge or cherry-pick started mid-squash neither leaks in nor is used up."""
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "other")
        _git(
            clone_dir,
            "commit",
            "-q",
            "--allow-empty",
            "--author",
            "Foreigner <foreign@example.invalid>",
            "-m",
            "foreign",
        )
        foreign = _rev(clone_dir, "HEAD")
        _git(clone_dir, "checkout", "-q", "main")
        _feature(clone_dir)
        _write_message(clone_dir)
        git_dir = _git(clone_dir, "rev-parse", "--absolute-git-dir")

        def inject(argv, kwargs):
            if "commit-tree" in argv:
                Path(git_dir, state).write_text(foreign + "\n")

        _spy_popen(monkeypatch, on_init=inject)
        rc, _, stderr = _inprocess(monkeypatch, clone_dir, extra_args=["--squash"])
        assert rc == 0, stderr
        assert _git(clone_dir, "log", "-1", "--format=%P") == _rev(
            clone_dir, "refs/remotes/origin/main"
        )
        assert "Foreigner" not in _git(clone_dir, "log", "-1", "--format=%an")
        assert Path(git_dir, state).exists(), "the other session's state was used up"

    def test_a_concurrent_branch_move_is_never_overwritten(self, repo_pair, monkeypatch):
        """The branch ref moves by compare-and-swap: a commit landing mid-squash survives."""
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        _write_message(clone_dir)
        moved: list[str] = []

        def move(argv, kwargs):
            if "push_guard: squash onto origin/main" in argv and not moved:
                _git(clone_dir, "commit", "-q", "--allow-empty", "-m", "feat: concurrent")
                moved.append(_rev(clone_dir, "HEAD"))

        _spy_popen(monkeypatch, on_init=move)
        rc, _, stderr = _inprocess(monkeypatch, clone_dir, extra_args=["--squash"])
        assert rc == 40 and "could not be moved from" in stderr, stderr
        assert _rev(clone_dir, "HEAD") == moved[0]

    def test_a_branch_switch_mid_squash_still_updates_only_the_named_branch(
        self, repo_pair, monkeypatch
    ):
        clone_dir, _ = repo_pair
        _git(clone_dir, "branch", "other")
        _feature(clone_dir)
        _write_message(clone_dir)
        other = _rev(clone_dir, "other")

        def switch(argv, kwargs):
            if "commit-tree" in argv:
                _git(clone_dir, "checkout", "-q", "other")

        _spy_popen(monkeypatch, on_init=switch)
        rc, _, stderr = _inprocess(monkeypatch, clone_dir, extra_args=["--squash"])
        assert rc == 0, stderr
        assert "HEAD moved to refs/heads/other" in stderr
        assert _rev(clone_dir, "other") == other
        assert _git(clone_dir, "log", "-1", "--format=%P", "feature/x") == _rev(
            clone_dir, "refs/remotes/origin/main"
        )

    def test_an_amend_never_blesses_what_a_hand_commit_added(self, repo_pair):
        """A path folded in by hand stays unrecorded: an --amend refuses until it is named."""
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "a.py").write_text("a\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "feat: a", "--", "a.py"])
        assert rc == 0, stderr
        Path(clone_dir, "b.py").write_text("b\n")
        _git(clone_dir, "add", "b.py")
        _git(clone_dir, "commit", "-q", "--amend", "-m", "feat: by hand")
        head = _rev(clone_dir, "HEAD")
        for argv in (["--amend"], ["--amend", "-m", "feat: reworded"], ["--amend", "--", "a.py"]):
            rc, _, stderr = _run_push_guard(clone_dir, argv)
            assert rc == 40 and "b.py" in stderr, (argv, stderr)
            assert _rev(clone_dir, "HEAD") == head, argv
        rc, _, stderr = _run_push_guard(clone_dir, ["--require-single-on-base"])
        assert rc == 40 and "b.py" in stderr and "a.py" not in stderr.split("Read each")[0], stderr
        rc, _, stderr = _run_push_guard(clone_dir, ["--amend", "--", "b.py"])
        assert rc == 0, stderr
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--require-single-on-base"])
        assert rc == 0 and "SAFE TO PUSH" in stdout, stderr

    def test_a_graft_cannot_hide_trunk_commits(self, repo_pair):
        """History is read as committed: a replace ref grafting HEAD onto the base is ignored."""
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        for i in range(6):
            _commit_tree(clone_dir, {f"trunk{i}.py": "x\n"}, f"wip: trunk {i}")
        _git(clone_dir, "replace", "--graft", "HEAD", "refs/remotes/origin/main")
        rc, _, stderr = _run_push_guard(clone_dir)
        assert rc == 40 and "7 commits ahead" in stderr, stderr


class TestPushGuardCommitByName:
    """``--commit``/``--amend`` commit exactly the named paths, whatever else is staged."""

    def test_named_paths_commit_and_a_foreign_staged_entry_refuses(self, repo_pair):
        clone_dir, _ = repo_pair
        _land_on_main(clone_dir, {"a.py": "a\n", "b.py": "b\n"}, "feat: files")
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "foreign.txt").write_text("not mine\n")
        _git(clone_dir, "add", "foreign.txt")
        _git(clone_dir, "mv", "a.py", "c.py")
        Path(clone_dir, "b.py").unlink()
        Path(clone_dir, "new.py").write_text("# new\n")
        named = ["a.py", "c.py", "b.py", "new.py"]

        rc, _, stderr = _run_push_guard(
            clone_dir, ["--commit", "-m", "refactor: move", "--", *named]
        )
        assert rc == 41 and "not among the paths you named" in stderr, stderr
        assert "foreign.txt" in stderr
        head = _rev(clone_dir, "HEAD")

        _git(clone_dir, "restore", "--staged", "--", "foreign.txt")
        rc, stdout, stderr = _run_push_guard(
            clone_dir, ["--commit", "-m", "refactor: move", "--", *named]
        )
        assert rc == 0, f"stdout: {stdout}\nstderr: {stderr}"
        assert _git(clone_dir, "rev-parse", "HEAD~1") == head
        names = _git(clone_dir, "show", "--no-renames", "--name-status", "--format=", "HEAD")
        assert sorted(names.split("\n")) == ["A\tc.py", "A\tnew.py", "D\ta.py", "D\tb.py"]

        Path(clone_dir, "new.py").write_text("# new, fixed\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--amend", "--", "new.py"])
        assert rc == 0, stderr
        assert _git(clone_dir, "log", "-1", "--format=%s") == "refactor: move"
        assert _git(clone_dir, "show", "HEAD:new.py") == "# new, fixed"

    def test_names_are_literal_never_globs(self, repo_pair):
        clone_dir, _ = repo_pair
        _land_on_main(clone_dir, {"pages/i.tsx": "i\n"}, "feat: page")
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "pages", "[id].tsx").write_text("route\n")
        Path(clone_dir, "pages", "i.tsx").write_text("another session's edit\n")
        rc, _, stderr = _run_push_guard(
            clone_dir, ["--commit", "-m", "feat: route", "--", "pages/[id].tsx"]
        )
        assert rc == 0, stderr
        assert _git(clone_dir, "show", "--name-only", "--format=", "HEAD") == "pages/[id].tsx"

    def test_a_directory_commits_its_unstaged_edits_but_not_staged_foreign_files(self, repo_pair):
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "lib").mkdir()
        Path(clone_dir, "lib", "mine.py").write_text("mine\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "feat: lib", "--", "lib"])
        assert rc == 0, stderr
        Path(clone_dir, "lib", "theirs.py").write_text("theirs\n")
        _git(clone_dir, "add", "lib/theirs.py")
        Path(clone_dir, "lib", "mine.py").write_text("mine 2\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "feat: lib 2", "--", "lib"])
        assert rc == 41 and "lib/theirs.py" in stderr, stderr

    def test_an_ignored_path_commits_when_named(self, repo_pair):
        clone_dir, _ = repo_pair
        _land_on_main(clone_dir, {".gitignore": "evidence/\n"}, "chore: ignore")
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "evidence").mkdir()
        Path(clone_dir, "evidence", "after.png").write_text("png\n")
        rc, _, stderr = _run_push_guard(
            clone_dir, ["--commit", "-m", "docs: evidence", "--", "evidence/after.png"]
        )
        assert rc == 0, stderr

    def test_unresolved_conflicts_are_never_committed(self, repo_pair):
        clone_dir, _ = repo_pair
        Path(clone_dir, "README.md").write_text("stashed\n")
        _git(clone_dir, "stash", "-q")
        _commit_tree(clone_dir, {"README.md": "committed\n"}, "docs: readme")
        subprocess.run(
            ["git", "stash", "pop", "-q"],
            cwd=clone_dir,
            capture_output=True,
            env=_fixture_git_env(),
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
        head = _rev(clone_dir, "HEAD")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "x: y", "--", "README.md"])
        assert rc == 41 and "unresolved conflicts" in stderr, stderr
        assert _rev(clone_dir, "HEAD") == head

    def test_a_failed_commit_that_moved_head_is_never_reported_as_nothing(
        self, repo_pair, monkeypatch
    ):
        """Landed means exit 0 AND HEAD is what was built; a moved HEAD with a failure is named."""
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "a.py").write_text("a\n")
        real = subprocess.Popen

        class _Popen(real):  # type: ignore[misc, valid-type]
            def wait(self, timeout=None):
                rc = super().wait(timeout)
                if "commit" in self.args and "--quiet" in self.args:
                    self.returncode = 1
                    return 1
                return rc

        monkeypatch.setattr(subprocess, "Popen", _Popen)
        rc, _, stderr = _inprocess(
            monkeypatch, clone_dir, extra_args=["--commit", "-m", "feat: a", "--", "a.py"]
        )
        head = _rev(clone_dir, "HEAD")
        assert rc == 40 and "but HEAD moved to " + head[:12] in stderr, stderr
        assert "nothing was committed" not in stderr
        assert _git(clone_dir, "log", "-1", "--format=%s") == "feat: a"

    def test_commit_needs_paths_and_a_message(self, repo_pair):
        clone_dir, _ = repo_pair
        for argv in (["--commit", "--", "x"], ["--commit", "-m", "feat: x"], ["-m", "x"]):
            rc, _, stderr = _run_push_guard(clone_dir, argv)
            assert rc == _PG.EXIT_USAGE, (argv, stderr)


class TestPushGuardIndex:
    """``--check-index`` reads what is staged in this checkout, read-only.

    A commit made by name never sweeps a staged path in, so this is a state
    report: what someone staged here, and what to do about it.
    """

    def test_a_foreign_staged_file_is_listed_with_its_remedies(self, repo_pair):
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        Path(clone_dir, "README.md").write_text("not mine\n")
        _git(clone_dir, "add", "README.md")
        Path(clone_dir, "README.md").write_text("not mine, edited\n")

        rc, _, stderr = _run_push_guard(clone_dir, ["--check-index"])
        assert rc == 41, stderr
        assert "':(top,literal)README.md'   (*)" in stderr
        assert "1 staged path(s) differ from HEAD (0 not in HEAD)" in stderr
        assert "git worktree add" in stderr
        assert _staged_list(stderr) == [b":(top,literal)README.md"]

    def test_the_whole_list_remedy_clears_it_from_a_subdirectory(self, repo_pair):
        clone_dir, _ = repo_pair
        _overlay(clone_dir)
        rc, _, stderr = _run_push_guard(clone_dir, ["--check-index"])
        assert rc == 41, stderr
        list_file = next(
            line.split(": ", 1)[1] for line in stderr.splitlines() if "Full list" in line
        )
        Path(clone_dir, "sub").mkdir()
        _git(
            str(Path(clone_dir, "sub")),
            "restore",
            "--source=HEAD",
            "--staged",
            "--worktree",
            f"--pathspec-from-file={list_file}",
            "--pathspec-file-nul",
        )
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--check-index"])
        assert rc == 0 and "INDEX MATCHES HEAD" in stdout, stderr

    def test_a_staged_deletion_with_a_copy_on_disk_is_marked(self, repo_pair):
        clone_dir, _ = repo_pair
        _git(clone_dir, "rm", "-q", "--cached", "README.md")
        rc, _, stderr = _run_push_guard(clone_dir, ["--check-index"])
        assert rc == 41 and "':(top,literal)README.md'   (*)" in stderr, stderr

    def test_a_staged_gitlink_hidden_by_gitmodules_is_refused(self, repo_pair):
        clone_dir, _ = repo_pair
        _land_on_main(
            clone_dir,
            {".gitmodules": '[submodule "sub"]\n\tpath = sub\n\turl = ./sub\n\tignore = all\n'},
            "chore: submodule config",
        )
        first = _rev(clone_dir, "HEAD")
        _git(clone_dir, "update-index", "--add", "--cacheinfo", f"160000,{first},sub")
        _git(clone_dir, "commit", "-q", "-m", "chore: add sub")
        _git(clone_dir, "update-index", "--cacheinfo", f"160000,{_rev(clone_dir, 'HEAD')},sub")

        rc, _, stderr = _run_push_guard(clone_dir, ["--check-index"])
        assert rc == 41 and ":(top,literal)sub" in stderr, stderr

    @pytest.mark.parametrize(
        "entry", [b"100664 a.txt", b"040000 dir"], ids=["blob-100664", "tree-040000"]
    )
    def test_a_non_canonical_tree_in_a_fresh_clone_matches(self, repo_pair, tmp_path, entry):
        """Legacy modes are normalised in the index; a tree-id compare refused them forever."""
        clone_dir, origin_dir = repo_pair
        blob = _git(clone_dir, "hash-object", "-w", "README.md")
        sub = _git_bytes(clone_dir, ["mktree"], f"100644 blob {blob}\tinner.txt\n".encode())
        target = sub if entry.startswith(b"040000") else blob
        tree = _git_bytes(
            clone_dir,
            ["hash-object", "-t", "tree", "-w", "--literally", "--stdin"],
            entry + b"\0" + bytes.fromhex(target),
        )
        commit = _git(clone_dir, "commit-tree", tree, "-p", "HEAD", "-m", "legacy tree")
        _git(clone_dir, "push", "-q", "origin", f"{commit}:refs/heads/main")
        _git(str(tmp_path), "clone", "-q", origin_dir, "fresh")

        rc, stdout, stderr = _run_push_guard(str(tmp_path / "fresh"), ["--check-index"])
        assert rc == 0, f"stdout: {stdout}\nstderr: {stderr}"

    def test_the_probe_writes_nothing_and_ignores_a_held_lock(self, repo_pair):
        clone_dir, _ = repo_pair
        index = Path(clone_dir, ".git", "index")
        before = index.stat()
        Path(clone_dir, ".git", "index.lock").write_text("")

        rc, stdout, stderr = _run_push_guard(clone_dir, ["--check-index"])
        assert rc == 0, f"stdout: {stdout}\nstderr: {stderr}"
        after = index.stat()
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)

    def _conflicted_rebase(self, clone_dir, resolve=True):
        _feature(clone_dir, {"README.md": "branch\n"})
        _land_on_main(clone_dir, {"README.md": "main\n"}, "feat: main side")
        _git(clone_dir, "checkout", "-q", "feature/x")
        proc = subprocess.run(
            ["git", "rebase", "-q", "main"],
            cwd=clone_dir,
            capture_output=True,
            env=_fixture_git_env(),
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
        assert proc.returncode != 0, "the fixture needs a conflict"
        if resolve:
            Path(clone_dir, "README.md").write_text("resolved\n")
            _git(clone_dir, "add", "README.md")

    def test_a_clean_rebase_stop_is_safe_to_continue(self, repo_pair):
        """--check-index before `git rebase --continue`: 0 only when the stop's own paths are staged."""
        clone_dir, _ = repo_pair
        self._conflicted_rebase(clone_dir)
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--check-index"])
        assert rc == 0 and "CLEAN (rebase in progress" in stdout, stderr
        rc, _, stderr = _run_push_guard(clone_dir)
        assert rc == 41 and "a rebase is in progress" in stderr, stderr

    def test_a_finished_rebase_leaves_no_operation_behind(self, repo_pair):
        """git keeps REBASE_HEAD after a resolved last pick; that is not a rebase."""
        clone_dir, _ = repo_pair
        self._conflicted_rebase(clone_dir)
        proc = run_bounded(
            ["git", "-c", "core.editor=true", "rebase", "--continue"],
            _fixture_git_env(),
            timeout=_SUBPROCESS_TIMEOUT_S,
            cwd=clone_dir,
        )
        assert proc.returncode == 0, proc.stderr
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--check-index"])
        assert rc == 0 and "INDEX MATCHES HEAD" in stdout, stderr

    def test_a_foreign_path_staged_mid_rebase_is_named_first(self, repo_pair):
        clone_dir, _ = repo_pair
        self._conflicted_rebase(clone_dir)
        Path(clone_dir, "foreign.txt").write_text("x\n")
        _git(clone_dir, "add", "foreign.txt")
        rc, _, stderr = _run_push_guard(clone_dir, ["--check-index"])
        assert rc == 41 and "outside the rebase in progress" in stderr, stderr
        assert "foreign.txt" in stderr and "README.md" not in stderr.split("Full list")[0]

    def test_a_run_from_rebase_exec_checks_normally(self, repo_pair):
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        _commit_tree(clone_dir, {"two.py": "2\n"}, "feat: two")
        guard = "{} {} --check-index".format(shlex.quote(sys.executable), shlex.quote(PUSH_GUARD))
        proc = run_bounded(
            ["git", "rebase", "-q", "--exec", guard, "HEAD~1"],
            _fixture_git_env(),
            timeout=_SUBPROCESS_TIMEOUT_S,
            cwd=clone_dir,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_an_unmerged_stash_pop_is_a_conflict_not_a_commit(self, repo_pair):
        clone_dir, _ = repo_pair
        Path(clone_dir, "README.md").write_text("stashed\n")
        _git(clone_dir, "stash", "-q")
        _commit_tree(clone_dir, {"README.md": "committed\n"}, "docs: readme")
        subprocess.run(
            ["git", "stash", "pop", "-q"],
            cwd=clone_dir,
            capture_output=True,
            env=_fixture_git_env(),
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
        rc, _, stderr = _run_push_guard(clone_dir, ["--check-index"])
        assert rc == 41 and "unresolved conflicts" in stderr, stderr
        assert "--commit" not in stderr

    def test_a_reftable_repository_sees_a_stopped_cherry_pick(self, tmp_path):
        repo = tmp_path / "rt"
        init = subprocess.run(
            ["git", "init", "-q", "--ref-format=reftable", str(repo)],
            capture_output=True,
            env=_fixture_git_env(),
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
        if init.returncode != 0:
            pytest.skip("this git has no reftable support")
        clone_dir = str(repo)
        _commit_tree(clone_dir, {"f.txt": "base\n"}, "init")
        _git(clone_dir, "checkout", "-q", "-b", "side")
        _commit_tree(clone_dir, {"f.txt": "side\n"}, "side")
        _git(clone_dir, "checkout", "-q", "-")
        _commit_tree(clone_dir, {"f.txt": "main\n"}, "main")
        subprocess.run(
            ["git", "cherry-pick", "side"],
            cwd=clone_dir,
            capture_output=True,
            env=_fixture_git_env(),
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
        Path(clone_dir, "f.txt").write_text("main\n")
        _git(clone_dir, "add", "f.txt")
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--check-index"])
        assert rc == 0 and "cherry-pick in progress" in stdout, stderr
        rc, _, stderr = _run_push_guard(clone_dir)
        assert rc == 41 and "a cherry-pick is in progress" in stderr, stderr

    @pytest.mark.parametrize(
        "name",
        [
            pytest.param(" leading.txt", id="leading-space"),
            pytest.param(
                "\t",
                id="tab-only",
                marks=pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="no tab in a name"),
            ),
            pytest.param(
                "a\nSTATUS: INDEX MATCHES HEAD\x1b[0m",
                id="newline-esc",
                marks=pytest.mark.skipif(
                    platform_compat.IS_WINDOWS, reason="no control character in a name"
                ),
            ),
        ],
    )
    def test_a_strange_name_is_listed_exactly_and_cannot_forge_a_line(self, repo_pair, name):
        clone_dir, _ = repo_pair
        Path(clone_dir, name).write_text("x\n")
        _git(clone_dir, "add", "--", name)

        rc, stdout, stderr = _run_push_guard(clone_dir, ["--check-index"])
        assert rc == 41, stderr
        assert "\x1b" not in stderr
        assert "STATUS: INDEX MATCHES HEAD" not in [line.strip() for line in stderr.splitlines()]
        assert "MATCHES" not in stdout
        assert _staged_list(stderr) == [b":(top,literal)" + name.encode()]
        if name.isprintable():
            assert shlex.quote(":(top,literal)" + name) in stderr
        else:
            assert "use the list file" in stderr

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs any-bytes names")
    def test_a_non_utf8_name_is_escaped_and_listed_exactly(self, repo_pair):
        clone_dir, _ = repo_pair
        with open(os.fsencode(clone_dir) + b"/bad-\xff.txt", "w") as handle:
            handle.write("x\n")
        _git(clone_dir, "add", "-A")
        rc, _, stderr = _run_push_guard(clone_dir, ["--check-index"])
        assert rc == 41 and "use the list file" in stderr, stderr
        assert _staged_list(stderr) == [b":(top,literal)bad-\xff.txt"]

    def test_an_unencodable_path_on_a_narrow_console_still_refuses_cleanly(self, repo_pair):
        """Path-bearing messages go to stderr, which Python writes with backslashreplace."""
        clone_dir, _ = repo_pair
        Path(clone_dir, "文档.md").write_text("x\n")
        _git(clone_dir, "add", "文档.md")
        rc, _, stderr = _run_push_guard(
            clone_dir, ["--check-index"], env_extra={"PYTHONIOENCODING": "ascii"}
        )
        assert rc == 41 and "Traceback" not in stderr, stderr

    def test_an_exported_git_dir_cannot_retarget_the_guard(self, repo_pair, tmp_path, monkeypatch):
        """A hook's GIT_DIR is dropped from git's env; the discovery fence is kept."""
        clone_dir, _ = repo_pair
        _git(str(tmp_path), "init", "-q", "decoy")
        Path(clone_dir, "README.md").write_text("staged here\n")
        _git(clone_dir, "add", "README.md")
        monkeypatch.setenv("GIT_DIR", str(tmp_path / "decoy" / ".git"))
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
        envs: list[dict] = []
        _spy_popen(
            monkeypatch,
            on_init=lambda argv, kw: kw.get("env") is not None and envs.append(kw["env"]),
        )
        rc, _, stderr = _inprocess(monkeypatch, clone_dir, extra_args=["--check-index"])
        assert rc == 41 and "README.md" in stderr, stderr
        assert envs and all("GIT_DIR" not in env for env in envs)
        assert all(env.get("GIT_CEILING_DIRECTORIES") == str(tmp_path.parent) for env in envs)


class TestPushGuardGitFailures:
    """A git call that fails or hangs refuses by NAME, never with a guessed cause."""

    @pytest.mark.parametrize(
        ("mode", "match", "named", "never"),
        [
            pytest.param(
                [],
                lambda a: "--is-inside-work-tree" in a,
                "--is-inside-work-tree",
                "not inside",
                id="first-probe",
            ),
            pytest.param(
                [],
                lambda a: "merge-base" in a and "--is-ancestor" not in a,
                "merge-base",
                "no common history",
                id="merge-base",
            ),
            pytest.param(
                ["--require-single-on-base"],
                lambda a: "cat-file" in a,
                "cat-file",
                "not exactly",
                id="head-parents",
            ),
            pytest.param(
                ["--check-index"],
                lambda a: "diff-index" in a,
                "diff-index",
                "staged path",
                id="index",
            ),
        ],
    )
    def test_a_hung_git_refuses_naming_the_command(
        self, repo_pair, monkeypatch, mode, match, named, never
    ):
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        _hang_on(monkeypatch, match)
        rc, stdout, stderr = _inprocess(monkeypatch, clone_dir, extra_args=mode)
        assert rc == 40, f"stdout: {stdout}\nstderr: {stderr}"
        assert "timed out" in stderr and named in stderr, stderr
        assert never not in stderr
        assert "SAFE TO PUSH" not in stdout

    @staticmethod
    def _slow_fetch_git(tmp_path: Path, chatter_s: float) -> list[str]:
        """A git whose fetch prints progress every 0.2 s for ``chatter_s`` s and for as
        long as the real fetch it runs takes (silent when 0, and then it hangs)."""
        script = tmp_path / "slow_fetch_git.py"
        script.write_text(
            "import subprocess, sys, time\n"
            "args = sys.argv[1:]\n"
            "fetch = 'fetch' in args\n"
            "if fetch and not {chatter}:\n"
            "    time.sleep({hang})\n"
            "end = time.monotonic() + {chatter}\n"
            "proc = subprocess.Popen([{git!r}] + args)\n"
            # Progress until the chatter is over AND the real fetch is done, so
            # no silence window is ever spent on spawning or fetching.
            "while fetch and (proc.poll() is None or time.monotonic() < end):\n"
            "    sys.stderr.write('Receiving objects: x\\r'); sys.stderr.flush()\n"
            "    time.sleep(0.2)\n"
            "sys.exit(proc.wait(timeout={hang}))\n".format(
                chatter=chatter_s, hang=_SUBPROCESS_TIMEOUT_S, git=shutil.which("git")
            )
        )
        return [sys.executable, str(script)]

    def test_a_stalled_fetch_is_stopped_and_names_the_manual_fetch(
        self, repo_pair, monkeypatch, tmp_path
    ):
        clone_dir, _ = repo_pair
        start = time.monotonic()
        rc, _, stderr = _inprocess(
            monkeypatch,
            clone_dir,
            self._slow_fetch_git(tmp_path, 0),
            overrides={"FETCH_STALL_S": 1},
        )
        assert rc == 40 and "made no progress for 1 s" in stderr, stderr
        assert "git fetch origin main by hand" in stderr, stderr
        assert time.monotonic() - start < _SUBPROCESS_TIMEOUT_S / 2

    def test_a_slow_fetch_that_keeps_receiving_is_never_cut_off(
        self, repo_pair, monkeypatch, tmp_path
    ):
        """The bound is on silence, not on the total: a slow link finishes."""
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        rc, stdout, stderr = _inprocess(
            monkeypatch,
            clone_dir,
            self._slow_fetch_git(tmp_path, 3),
            overrides={"FETCH_STALL_S": 2},
        )
        assert rc == 0 and "SAFE TO PUSH" in stdout, stderr

    def test_the_fetch_reads_only_the_base(self, repo_pair, monkeypatch):
        """No submodule recursion: the batch ssh command would override theirs."""
        clone_dir, _ = repo_pair
        seen: list[list[str]] = []
        _spy_popen(monkeypatch, on_init=lambda argv, kw: seen.append(argv))
        rc, _, stderr = _inprocess(monkeypatch, clone_dir)
        assert rc == 0, stderr
        fetch = next(a for a in seen if "fetch" in a)
        assert "--no-recurse-submodules" in fetch and fetch[-1].endswith(
            ":refs/remotes/origin/main"
        )

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="the hook stand-in is sh")
    def test_a_hooks_background_child_cannot_hold_a_step_open(
        self, repo_pair, tmp_path, monkeypatch
    ):
        """Output goes to files, so a finished fetch is finished even if a hook's child lingers."""
        clone_dir, _ = repo_pair
        pid_file = tmp_path / "bg.pid"
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        hook = hooks / "reference-transaction"
        hook.write_text(
            "#!/bin/sh\ncat >/dev/null\n(sleep 60 & echo $! >> "
            + shlex.quote(str(pid_file))
            + ")\n"
        )
        hook.chmod(0o755)
        _land_on_main(clone_dir, {"up.txt": "1\n"}, "feat: upstream")
        _git(clone_dir, "update-ref", "refs/remotes/origin/main", "main~1")  # so the fetch moves it
        _git(clone_dir, "config", "core.hooksPath", str(hooks))
        try:
            rc, stdout, stderr = _inprocess(monkeypatch, clone_dir)
            assert rc == 0 and "SAFE TO PUSH" in stdout, stderr
            assert pid_file.exists(), "the hook never ran"
        finally:
            for pid in pid_file.read_text().split() if pid_file.exists() else []:
                try:
                    os.kill(int(pid), signal.SIGKILL)  # only the children this test planted
                except OSError:
                    pass

    def test_an_interrupt_reaps_the_child_it_was_waiting_for(self, repo_pair, monkeypatch):
        clone_dir, _ = repo_pair
        children: list[subprocess.Popen] = []
        real = subprocess.Popen

        class _Popen(real):  # type: ignore[misc, valid-type]
            def __init__(self, args, *a, **kw):
                super().__init__(args, *a, **kw)
                children.append(self)

            def wait(self, timeout=None):
                if "fetch" in self.args and not interrupted:
                    interrupted.append(True)
                    raise KeyboardInterrupt
                return super().wait(timeout)

        interrupted: list[bool] = []
        monkeypatch.setattr(subprocess, "Popen", _Popen)
        rc, _, stderr = _inprocess(monkeypatch, clone_dir)
        assert rc == 40 and "interrupted" in stderr, stderr
        fetch = next(c for c in children if "fetch" in c.args)
        assert fetch.returncode is not None, "the interrupted fetch was left running"

    def test_a_missing_git_is_an_environment_error(self, repo_pair, monkeypatch, tmp_path):
        clone_dir, _ = repo_pair
        missing = str(tmp_path / "no-such-git")
        rc, _, stderr = _inprocess(monkeypatch, clone_dir, [missing])
        assert rc == _PG.EXIT_ENV and "git is not available" in stderr, stderr

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs any-bytes paths")
    def test_a_repository_under_a_non_utf8_path_works(self, repo_pair, tmp_path):
        clone_dir, origin_dir = repo_pair
        target = os.fsencode(str(tmp_path)) + b"/repo-\xff"
        subprocess.run(
            [b"git", b"clone", b"-q", os.fsencode(origin_dir), target],
            check=True,
            capture_output=True,
            env=_fixture_git_env(),
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
        proc = run_bounded(
            [sys.executable, PUSH_GUARD, "--check-index"],
            dict(os.environ),
            timeout=_SUBPROCESS_TIMEOUT_S,
            cwd=os.fsdecode(target),
        )
        assert proc.returncode == 0, proc.stderr

    def test_a_file_named_like_the_base_ref_does_not_break_the_guard(self, repo_pair):
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        Path(clone_dir, "origin").mkdir()
        Path(clone_dir, "origin", "main").write_text("untracked\n")
        rc, stdout, stderr = _run_push_guard(clone_dir)
        assert rc == 0, f"stdout: {stdout}\nstderr: {stderr}"


def _install_hooks(clone_dir: str, tmp_path: Path, **bodies: str) -> Path:
    """sh hooks by keyword (``commit_msg`` is ``commit-msg``), wired by core.hooksPath."""
    hooks = tmp_path / "hooks"
    hooks.mkdir(exist_ok=True)
    for name, body in bodies.items():
        hook = hooks / name.replace("_", "-")
        hook.write_text("#!/bin/sh\n" + body)
        hook.chmod(0o755)
    _git(clone_dir, "config", "core.hooksPath", str(hooks))
    return hooks


def _run_from(cwd: str, args: list[str]) -> tuple[int, str, str]:
    """push_guard.py run from any directory of the checkout; bounded."""
    proc = run_bounded(
        [sys.executable, PUSH_GUARD, "--base", "main", *args],
        dict(os.environ),
        timeout=_SUBPROCESS_TIMEOUT_S,
        cwd=cwd,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _stash_pop_conflict(clone_dir: str) -> None:
    """Unmerged README.md with no operation in progress (a conflicted git stash pop)."""
    Path(clone_dir, "README.md").write_text("stashed\n")
    _git(clone_dir, "stash", "-q")
    _commit_tree(clone_dir, {"README.md": "committed\n"}, "docs: readme")
    subprocess.run(
        ["git", "stash", "pop", "-q"],
        cwd=clone_dir,
        capture_output=True,
        env=_fixture_git_env(),
        timeout=_SUBPROCESS_TIMEOUT_S,
    )


def _commit_inprocess(monkeypatch, cwd: str, args: list[str], around_commit) -> tuple[int, str]:
    """``main()`` in-process, with ``around_commit(real)`` standing in for ``git commit``.

    The stand-in receives the real runner, so a test can act on the checkout
    in the exact window before or after git commits: deterministic, where a
    second process racing the guard would not be.
    """
    pg = _load_push_guard()
    monkeypatch.setattr(pg, "GIT_TIMEOUT_S", _INPROCESS_TIMEOUT_S)
    monkeypatch.setattr(pg, "HOOK_TIMEOUT_S", _INPROCESS_TIMEOUT_S)
    real = pg._interactive
    monkeypatch.setattr(
        pg, "_interactive", lambda argv, extra=None: around_commit(real, argv, extra)
    )
    monkeypatch.setattr(sys, "argv", ["push_guard.py", "--base", "main", *args])
    monkeypatch.chdir(cwd)
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", stderr)
    return pg.main(), stderr.getvalue()


class TestPushGuardPrivateIndex:
    """``--commit``/``--amend`` build in a private index; the shared one changes after."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["--commit", "-m", "x: y", "--", ""],
            ["--commit", "-m", "x: y", "--", "a.py", "  "],
            ["--amend", "--", ""],
            ["--squash", "--", ""],
        ],
    )
    def test_an_empty_path_names_nothing(self, repo_pair, argv):
        """An unset shell variable must not become the whole tree's pathspec."""
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        Path(clone_dir, "README.md").write_text("someone else's edit\n")
        head = _rev(clone_dir, "HEAD")
        rc, _, stderr = _run_push_guard(clone_dir, argv)
        assert rc == _PG.EXIT_USAGE and "empty path" in stderr, stderr
        assert _rev(clone_dir, "HEAD") == head

    def test_a_named_directory_never_force_adds_its_ignored_files(self, repo_pair):
        clone_dir, _ = repo_pair
        _land_on_main(clone_dir, {".gitignore": "*.log\n"}, "chore: ignore logs")
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "lib").mkdir()
        Path(clone_dir, "lib", "mine.py").write_text("mine\n")
        Path(clone_dir, "lib", "debug.log").write_text("secret-ish\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "feat: lib", "--", "lib"])
        assert rc == 0, stderr
        assert _git(clone_dir, "show", "--name-only", "--format=", "HEAD") == "lib/mine.py"

    def test_a_change_only_the_index_can_hold_is_committed(self, repo_pair):
        """git rm --cached and update-index --chmod are kept, not re-read from disk."""
        clone_dir, _ = repo_pair
        _land_on_main(clone_dir, {"keep.txt": "k\n", "tool.sh": "echo\n"}, "feat: files")
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        _git(clone_dir, "rm", "-q", "--cached", "keep.txt")
        _git(clone_dir, "update-index", "--chmod=+x", "tool.sh")
        rc, _, stderr = _run_push_guard(
            clone_dir, ["--commit", "-m", "chore: index", "--", "keep.txt", "tool.sh"]
        )
        assert rc == 0, stderr
        assert _git(clone_dir, "ls-tree", "HEAD", "keep.txt") == ""
        assert _git(clone_dir, "ls-tree", "HEAD", "tool.sh").startswith("100755 ")
        assert Path(clone_dir, "keep.txt").exists()

    def test_a_change_only_the_index_can_hold_is_committed_from_a_subdirectory(self, repo_pair):
        """ls-files names entries from this directory unless asked for full names."""
        clone_dir, _ = repo_pair
        _land_on_main(clone_dir, {"tool.sh": "echo\n"}, "feat: tool")
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        _git(clone_dir, "update-index", "--chmod=+x", "tool.sh")
        sub = Path(clone_dir, "sub")
        sub.mkdir()
        rc, _, stderr = _run_from(str(sub), ["--commit", "-m", "chore: mode", "--", "../tool.sh"])
        assert rc == 0, stderr
        assert _git(clone_dir, "ls-tree", "HEAD", "tool.sh").startswith("100755 ")
        assert _git(clone_dir, "diff", "--cached", "--name-only") == ""

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="the hook stand-in is sh")
    def test_a_hook_that_sweeps_in_unnamed_paths_is_taken_back(self, repo_pair, tmp_path):
        """The hook commits from the private index too; what it adds there is not named."""
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        pre = _rev(clone_dir, "HEAD")
        _install_hooks(clone_dir, tmp_path, pre_commit="git add -u\n")
        Path(clone_dir, "README.md").write_text("someone else's edit\n")
        Path(clone_dir, "mine.py").write_text("mine\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "feat: m", "--", "mine.py"])
        assert rc == 40 and "did not name" in stderr and "README.md" in stderr, stderr
        assert "taken back" in stderr, stderr
        assert _rev(clone_dir, "HEAD") == pre
        assert Path(clone_dir, "README.md").read_text() == "someone else's edit\n"
        assert _git(clone_dir, "diff", "--cached", "--name-only") == ""

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="the hook stand-in is sh")
    def test_a_hook_that_reformats_a_named_path_lands(self, repo_pair, tmp_path):
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        _install_hooks(
            clone_dir, tmp_path, pre_commit="printf 'formatted\\n' > mine.py\ngit add mine.py\n"
        )
        Path(clone_dir, "mine.py").write_text("mine\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "feat: m", "--", "mine.py"])
        assert rc == 0, stderr
        assert _git(clone_dir, "show", "HEAD:mine.py") == "formatted"

    def test_a_commit_landing_on_a_concurrent_one_is_taken_back(self, repo_pair, monkeypatch):
        """Another session commits between the guard reading HEAD and git committing."""
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "mine.py").write_text("mine\n")

        def concurrent_first(real, argv, extra):
            Path(clone_dir, "theirs.py").write_text("theirs\n")
            _git(clone_dir, "add", "theirs.py")
            _git(clone_dir, "commit", "-q", "-m", "feat: theirs")
            return real(argv, extra)

        rc, stderr = _commit_inprocess(
            monkeypatch, clone_dir, ["--commit", "-m", "feat: m", "--", "mine.py"], concurrent_first
        )
        assert rc == 40 and "taken back" in stderr, stderr
        assert _git(clone_dir, "log", "-1", "--format=%s") == "feat: theirs"
        assert _git(clone_dir, "show", "HEAD:theirs.py") == "theirs"
        assert Path(clone_dir, "mine.py").read_text() == "mine\n"

    def test_a_path_staged_while_it_commits_stays_staged(self, repo_pair, monkeypatch):
        """The sync after landing never overwrites an entry staged since the guard read it."""
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "mine.py").write_text("v1\n")

        def stage_after(real, argv, extra):
            res = real(argv, extra)
            Path(clone_dir, "mine.py").write_text("v2\n")
            _git(clone_dir, "add", "mine.py")
            return res

        rc, stderr = _commit_inprocess(
            monkeypatch, clone_dir, ["--commit", "-m", "feat: m", "--", "mine.py"], stage_after
        )
        assert rc == 0 and "staged again" in stderr, stderr
        assert _git(clone_dir, "show", "HEAD:mine.py") == "v1"
        assert _git(clone_dir, "show", ":mine.py") == "v2"

    @pytest.mark.parametrize("checked_out", [True, False])
    def test_a_staged_gitlink_commits_what_the_submodule_shows(self, repo_pair, checked_out):
        """A checked-out submodule's commit is its worktree copy; without one, the staged one."""
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        sub = Path(clone_dir, "sub")
        sub.mkdir()
        _git(str(sub), "init", "-q")
        commits = []
        for n in ("a", "b"):
            Path(sub, "f").write_text(n + "\n")
            _git(str(sub), "add", "f")
            _git(str(sub), "commit", "-q", "-m", n)
            commits.append(_rev(str(sub), "HEAD"))
        # Not checked out: an empty directory, as a clone without --recurse leaves it.
        name = "sub" if checked_out else "empty"
        Path(clone_dir, "empty").mkdir()
        _git(clone_dir, "update-index", "--add", "--cacheinfo", f"160000,{commits[0]},{name}")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "feat: sub", "--", name])
        assert rc == 0, stderr
        want = commits[1] if checked_out else commits[0]
        assert _git(clone_dir, "ls-tree", "HEAD", name).split()[2] == want

    def test_conflicts_are_seen_from_a_subdirectory(self, repo_pair):
        clone_dir, _ = repo_pair
        _stash_pop_conflict(clone_dir)
        sub = Path(clone_dir, "sub")
        sub.mkdir()
        head = _rev(clone_dir, "HEAD")
        rc, _, stderr = _run_from(str(sub), ["--commit", "-m", "x: y", "--", "../README.md"])
        assert rc == 41 and "unresolved conflicts" in stderr, stderr
        assert _rev(clone_dir, "HEAD") == head
        rc, _, stderr = _run_from(str(sub), ["--check-index"])
        assert rc == 41 and "unresolved conflicts" in stderr and "README.md" in stderr, stderr

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="the hook stand-in is sh")
    def test_a_rejected_commit_leaves_nothing_staged(self, repo_pair, tmp_path):
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        _install_hooks(clone_dir, tmp_path, pre_commit="echo 'lint failed' >&2\nexit 1\n")
        Path(clone_dir, "new.py").write_text("new\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "feat: n", "--", "new.py"])
        assert rc == 40 and "nothing was committed" in stderr and "lint failed" in stderr, stderr
        assert _git(clone_dir, "diff", "--cached", "--name-only") == ""
        _git(clone_dir, "config", "--unset", "core.hooksPath")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "feat: n", "--", "new.py"])
        assert rc == 0, stderr

    def test_an_edit_in_the_second_it_was_staged_is_committed(self, repo_pair):
        """The private index keeps the shared one's mtime, so git re-reads a racily clean entry."""
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        _git(clone_dir, "config", "core.trustctime", "false")
        edited = Path(clone_dir, "f.txt")
        edited.write_text("aaaa\n")
        _git(clone_dir, "add", "f.txt")
        _git(clone_dir, "commit", "-q", "-m", "feat: f")
        stamp = edited.stat().st_mtime_ns
        edited.write_text("bbbb\n")
        os.utime(edited, ns=(stamp, stamp))
        index = Path(_git(clone_dir, "rev-parse", "--absolute-git-dir"), "index")
        os.utime(index, ns=(stamp, stamp))
        Path(clone_dir, "g.txt").write_text("g\n")
        rc, _, stderr = _run_push_guard(
            clone_dir, ["--commit", "-m", "feat: second", "--", "f.txt", "g.txt"]
        )
        assert rc == 0, stderr
        assert _git(clone_dir, "show", "HEAD:f.txt") == "bbbb"
        assert _git(clone_dir, "status", "--porcelain") == ""

    def test_a_message_only_amend_leaves_the_shared_index_alone(self, repo_pair):
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "b.py").write_text("b\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "fix: b", "--", "b.py"])
        assert rc == 0, stderr
        Path(clone_dir, "c.py").write_text("c\n")
        _git(clone_dir, "add", "-N", "c.py")
        rc, _, stderr = _run_push_guard(clone_dir, ["--amend", "-m", "fix: b, better"])
        assert rc == 0, stderr
        assert _git(clone_dir, "log", "-1", "--format=%s") == "fix: b, better"
        assert _git(clone_dir, "status", "--porcelain", "--", "c.py") == "A c.py"

    def test_a_printed_name_commits_from_a_subdirectory(self, repo_pair):
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "sub").mkdir()
        Path(clone_dir, "d.py").write_text("d\n")
        _git(clone_dir, "add", "d.py")
        rc, _, stderr = _run_from(
            str(Path(clone_dir, "sub")), ["--commit", "-m", "fix: d", "--", ":(top,literal)d.py"]
        )
        assert rc == 0, stderr
        assert _git(clone_dir, "show", "--name-only", "--format=", "HEAD") == "d.py"
        rc, _, stderr = _run_push_guard(
            clone_dir, ["--commit", "-m", "x: y", "--", ":(top,literal)"]
        )
        assert rc == 64, stderr

    def test_a_message_file_keeps_the_text_off_the_command_line(self, repo_pair, tmp_path):
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "a.py").write_text("a\n")
        message = tmp_path / "msg.txt"
        message.write_text("fix(cron): refuse rm -rf / in scripts\n\nBody.\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-F", str(message), "--", "a.py"])
        assert rc == 0, stderr
        assert (
            _git(clone_dir, "log", "-1", "--format=%s") == "fix(cron): refuse rm -rf / in scripts"
        )
        argv = ["--commit", "-m", "x: y", "-F", str(message), "--", "a.py"]
        assert _run_push_guard(clone_dir, argv)[0] == _PG.EXIT_USAGE

    @pytest.mark.parametrize("mode", ["--commit", "--amend"])
    def test_a_message_from_stdin_is_a_usage_error(self, mode):
        """``-F -`` would be a file named ``-`` to git; it is refused before anything runs."""
        with pytest.raises(SystemExit) as exc:
            _PG._parse_args([mode, "-F", "-", "--", "a.py"])
        assert exc.value.code == _PG.EXIT_USAGE

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="needs a symlink")
    def test_a_path_through_a_symlinked_directory_is_the_same_path(self, repo_pair, tmp_path):
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        link = tmp_path / "link"
        link.symlink_to(clone_dir)
        Path(clone_dir, "mine.txt").write_text("mine\n")
        _git(clone_dir, "add", "mine.txt")
        rc, _, stderr = _run_push_guard(
            clone_dir, ["--commit", "-m", "feat: mine", "--", str(link / "mine.txt")]
        )
        assert rc == 0, stderr
        assert _git(clone_dir, "show", "--name-only", "--format=", "HEAD") == "mine.txt"
        outside = tmp_path / "outside.txt"
        outside.write_text("x\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "x: y", "--", str(outside)])
        assert rc == _PG.EXIT_USAGE and "not inside this repository" in stderr, stderr

    def test_a_path_on_another_drive_is_outside(self, monkeypatch):
        """ntpath.relpath raises across drives; that is a usage error, not a traceback."""

        def other_drive(path, start=None):
            raise ValueError("path is on mount 'D:', start on mount 'C:'")

        monkeypatch.setattr(_PG.os.path, "relpath", other_drive)
        assert _PG._top_relative("D:/x.txt", "C:/repo") is None

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs any-bytes argv")
    def test_a_non_utf8_message_is_committed_byte_for_byte(self, repo_pair):
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "a.py").write_text("a\n")
        subject = os.fsdecode(b"feat: caf\xe9")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", subject, "--", "a.py"])
        assert rc == 0, stderr
        raw = subprocess.run(
            ["git", "cat-file", "commit", "HEAD"],
            cwd=clone_dir,
            capture_output=True,
            check=True,
            env=_fixture_git_env(),
            timeout=_SUBPROCESS_TIMEOUT_S,
        ).stdout
        # git itself re-encodes a non-UTF-8 message (it warns); the point is
        # that the guard carries the bytes to git instead of dying on them.
        assert "feat: caf\u00e9" in raw.decode("utf-8")

    def test_an_amend_identical_to_head_has_landed(self, repo_pair):
        """Same tree, message and second: the same sha is a landed amend, not a failure."""
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "a.py").write_text("a\n")
        when = {
            "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z",
        }
        rc, _, stderr = _run_push_guard(
            clone_dir, ["--commit", "-m", "feat: a", "--", "a.py"], env_extra=when
        )
        assert rc == 0, stderr
        head = _rev(clone_dir, "HEAD")
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--amend"], env_extra=when)
        assert rc == 0 and "COMMITTED" in stdout, stderr
        assert _rev(clone_dir, "HEAD") == head

    def test_an_author_name_with_a_line_separator_commits(self, repo_pair):
        """The author check reads the header by \\n only, the way git delimits it."""
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "a.py").write_text("a\n")
        rc, _, stderr = _run_push_guard(
            clone_dir,
            ["--commit", "-m", "feat: a", "--", "a.py"],
            env_extra={"GIT_AUTHOR_NAME": "Ann\u2028Lee"},
        )
        assert rc == 0, stderr

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="the hook stand-in is sh")
    @pytest.mark.parametrize(
        "rewrite",
        [
            pytest.param("git commit -q --allow-empty --no-verify -m extra", id="a-commit-on-top"),
            pytest.param(
                "git commit -q --amend --no-edit --no-verify --author='Other <o@example.invalid>'",
                id="another-author",
            ),
        ],
    )
    def test_a_commit_a_hook_rewrote_is_not_reported_as_built(self, repo_pair, tmp_path, rewrite):
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        mark = shlex.quote(str(tmp_path / "once"))
        _install_hooks(
            clone_dir,
            tmp_path,
            post_commit="[ -e {0} ] && exit 0\ntouch {0}\n{1}\n".format(mark, rewrite),
        )
        Path(clone_dir, "a.py").write_text("a\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "feat: a", "--", "a.py"])
        assert rc == 40 and "is not the commit that was built" in stderr, stderr


class TestPushGuardStagedReport:
    """What a 41 lists, and which remedy it offers."""

    def test_every_marked_path_is_listed_and_no_clear_command_is_offered(self, repo_pair):
        clone_dir, _ = repo_pair
        for i in range(30):
            Path(clone_dir, f"f{i:02}.txt").write_text("staged\n")
        _git(clone_dir, "add", "-A")
        for i in range(5, 30):
            Path(clone_dir, f"f{i:02}.txt").write_text("edited after staging\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--check-index"])
        assert rc == 41, stderr
        for i in range(5, 30):
            assert f"':(top,literal)f{i:02}.txt'   (*)" in stderr, i
        assert "git restore --source=HEAD" not in stderr
        assert len(_staged_list(stderr)) == 30

    def test_an_unmarked_list_beyond_the_cap_offers_the_clear_command(self, repo_pair):
        clone_dir, _ = repo_pair
        for i in range(30):
            Path(clone_dir, f"f{i:02}.txt").write_text("staged\n")
        _git(clone_dir, "add", "-A")
        rc, _, stderr = _run_push_guard(clone_dir, ["--check-index"])
        assert rc == 41 and "... and 10 more, none marked (*)" in stderr, stderr
        assert "git restore --source=HEAD" in stderr

    def test_each_refusal_reads_its_own_list(self, repo_pair):
        """Another session's refusal can never rewrite the list a printed remedy reads."""
        clone_dir, _ = repo_pair
        Path(clone_dir, "a.txt").write_text("a\n")
        _git(clone_dir, "add", "a.txt")
        _, _, first = _run_push_guard(clone_dir, ["--check-index"])
        Path(clone_dir, "b.txt").write_text("b\n")
        _git(clone_dir, "add", "b.txt")
        _, _, second = _run_push_guard(clone_dir, ["--check-index"])
        assert _staged_list(first) == [b":(top,literal)a.txt"]
        assert _staged_list(second) == [b":(top,literal)a.txt", b":(top,literal)b.txt"]

    def test_the_not_yours_remedy_carries_your_commits_and_edits(self, repo_pair):
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        Path(clone_dir, "README.md").write_text("not mine\n")
        _git(clone_dir, "add", "README.md")
        rc, _, stderr = _run_push_guard(clone_dir, ["--check-index"])
        assert rc == 41, stderr
        assert "git worktree add -b <new-branch> <dir> HEAD" in stderr
        assert "git diff --binary HEAD" in stderr and "never git stash" in stderr


class TestPushGuardOperations:
    """State in the middle of a rebase, merge, cherry-pick or revert."""

    @staticmethod
    def _stop(clone_dir: str, resolve: bool = True) -> None:
        TestPushGuardIndex._conflicted_rebase(None, clone_dir, resolve=resolve)  # type: ignore[arg-type]

    def test_a_rename_upstream_is_followed_at_a_rebase_stop(self, repo_pair):
        """The replayed change to old.py lands on new.py; new.py is the stop's own path."""
        clone_dir, _ = repo_pair
        _land_on_main(clone_dir, {"old.py": "line\n" * 20, "c.txt": "base\n"}, "feat: base")
        _feature(clone_dir, {"old.py": "line\n" * 19 + "mine\n", "c.txt": "branch\n"})
        _land_on_main(
            clone_dir, {"old.py": None, "new.py": "line\n" * 20, "c.txt": "main\n"}, "refactor: mv"
        )
        _git(clone_dir, "checkout", "-q", "feature/x")
        proc = subprocess.run(
            ["git", "rebase", "-q", "main"],
            cwd=clone_dir,
            capture_output=True,
            env=_fixture_git_env(),
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
        assert proc.returncode != 0, "the fixture needs a conflict"
        assert "mine" in Path(clone_dir, "new.py").read_text(), "git did not follow the rename"
        Path(clone_dir, "c.txt").write_text("resolved\n")
        _git(clone_dir, "add", "c.txt")
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--check-index"])
        assert rc == 0 and "CLEAN (rebase in progress" in stdout, stderr

    def test_a_squash_at_a_rebase_stop_names_the_rebase(self, repo_pair):
        clone_dir, _ = repo_pair
        self._stop(clone_dir)
        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 41 and "a rebase is in progress" in stderr, stderr
        assert "detached" not in stderr

    def test_an_unresolved_stop_names_its_conflicts(self, repo_pair):
        clone_dir, _ = repo_pair
        self._stop(clone_dir, resolve=False)
        rc, _, stderr = _run_push_guard(clone_dir, ["--check-index"])
        assert rc == 41 and "stopped on conflicts in" in stderr and "README.md" in stderr, stderr
        assert "--continue" not in stderr.split("README.md")[0]

    def test_mid_operation_no_remedy_discards_the_replayed_change(self, repo_pair):
        clone_dir, _ = repo_pair
        self._stop(clone_dir)
        Path(clone_dir, "foreign.txt").write_text("x\n")
        _git(clone_dir, "add", "foreign.txt")
        rc, _, stderr = _run_push_guard(clone_dir, ["--check-index"])
        assert rc == 41, stderr
        assert "--worktree" not in stderr and "push_guard.py --commit" not in stderr
        assert "git restore --staged" in stderr and "git rebase --abort" in stderr

    def test_a_stopped_revert_sequence_is_named_a_revert(self, repo_pair):
        clone_dir, _ = repo_pair
        head = _rev(clone_dir, "HEAD")
        sequencer = Path(_git(clone_dir, "rev-parse", "--absolute-git-dir"), "sequencer")
        sequencer.mkdir()
        (sequencer / "todo").write_text("revert {} initial commit\n".format(head))
        rc, _, stderr = _run_push_guard(clone_dir, ["--check-index"])
        assert rc == 41 and "git revert --continue" in stderr, stderr

    def test_the_operation_is_decided_once_per_run(self, repo_pair, monkeypatch):
        """One reading of the state, so a run prints exactly one STATUS."""
        clone_dir, _ = repo_pair
        probes: list[list[str]] = []
        _spy_popen(monkeypatch, on_init=lambda argv, kw: probes.append(argv))
        rc, stdout, _ = _inprocess(monkeypatch, clone_dir, extra_args=["--check-index"])
        assert rc == 0 and stdout.count("STATUS:") == 1
        assert sum("rebase-merge" in argv for argv in probes) == 1


class TestPushGuardRecord:
    """What the guard committed on a branch is recorded, and every SAFE mode reads it."""

    def test_a_hand_commit_is_not_safe_to_push(self, repo_pair):
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "hand.py").write_text("h\n")
        _git(clone_dir, "add", "hand.py")
        _git(clone_dir, "commit", "-q", "-m", "feat: by hand")
        rc, stdout, stderr = _run_push_guard(clone_dir)
        assert rc == 40 and "hand.py" in stderr and "did not commit" in stderr, stderr
        assert "SAFE TO PUSH" not in stdout
        _bless(clone_dir)
        rc, stdout, stderr = _run_push_guard(clone_dir)
        assert rc == 0 and "SAFE TO PUSH" in stdout, stderr

    def test_the_record_keeps_every_branch(self, repo_pair):
        clone_dir, _ = repo_pair
        for branch, name in (("feature/a", "a.py"), ("feature/b", "b.py")):
            _git(clone_dir, "checkout", "-q", "-b", branch, "main")
            Path(clone_dir, name).write_text("x\n")
            rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "feat: x", "--", name])
            assert rc == 0, stderr
        _git(clone_dir, "checkout", "-q", "feature/a")
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--require-single-on-base"])
        assert rc == 0 and "SAFE TO PUSH" in stdout, stderr

    def test_a_resync_rebase_of_the_squash_stays_recorded(self, repo_pair):
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        _write_message(clone_dir)
        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 0, stderr
        _land_on_main(clone_dir, {"upstream.py": "u\n"}, "feat: upstream")
        _git(clone_dir, "checkout", "-q", "feature/x")
        _git(clone_dir, "rebase", "-q", "main")
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--require-single-on-base"])
        assert rc == 0 and "SAFE TO PUSH" in stdout, stderr

    def test_naming_your_paths_rebuilds_a_hand_made_branch(self, repo_pair):
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "hand.py").write_text("h\n")
        _git(clone_dir, "add", "hand.py")
        _git(clone_dir, "commit", "-q", "-m", "feat: by hand")
        _write_message(clone_dir)
        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 40 and "hand.py" in stderr, stderr
        # Naming them back commits them into the squash, and the refusal says so.
        assert "re-run this same --squash command" in stderr, stderr
        assert "commits them" in stderr and "it only reads them" not in stderr, stderr
        assert "instead" not in stderr, stderr
        rc, _, stderr = _run_push_guard(clone_dir, ["--squash", "--", "hand.py"])
        assert rc == 0, stderr
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--require-single-on-base"])
        assert rc == 0 and "SAFE TO PUSH" in stdout, stderr

    def test_the_record_is_written_before_the_branch_moves(self, repo_pair, monkeypatch):
        """An interrupt right after the move still leaves the vouched-for paths recorded."""
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "hand.py").write_text("h\n")
        _git(clone_dir, "add", "hand.py")
        _git(clone_dir, "commit", "-q", "-m", "feat: by hand")
        _write_message(clone_dir)
        real = subprocess.Popen
        interrupted: list[bool] = []

        class _Popen(real):  # type: ignore[misc, valid-type]
            def wait(self, timeout=None):
                rc = super().wait(timeout)
                if "update-ref" in self.args and not interrupted:
                    interrupted.append(True)
                    raise KeyboardInterrupt
                return rc

        monkeypatch.setattr(subprocess, "Popen", _Popen)
        rc, _, stderr = _inprocess(monkeypatch, clone_dir, extra_args=["--squash", "--", "hand.py"])
        assert rc == 40 and "interrupted" in stderr and interrupted, stderr
        monkeypatch.setattr(subprocess, "Popen", real)
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--require-single-on-base"])
        assert rc == 0 and "SAFE TO PUSH" in stdout, stderr

    def test_a_legacy_graft_file_cannot_hide_trunk_commits(self, repo_pair):
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        for i in range(6):
            _commit_tree(clone_dir, {f"trunk{i}.py": "x\n"}, f"wip: trunk {i}")
        grafts = Path(_git(clone_dir, "rev-parse", "--absolute-git-dir"), "info", "grafts")
        grafts.parent.mkdir(exist_ok=True)
        grafts.write_text(
            "{} {}\n".format(_rev(clone_dir, "HEAD"), _rev(clone_dir, "refs/remotes/origin/main"))
        )
        rc, _, stderr = _run_push_guard(clone_dir)
        assert rc == 40 and "7 commits ahead" in stderr, stderr
        assert "deprecated" not in stderr, stderr

    def test_no_child_prints_the_graft_deprecation_advice(self, repo_pair):
        """The graft file named is one that cannot exist: git reads none and says nothing."""
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "mine.py").write_text("m\n")
        rc, stdout, stderr = _run_push_guard(
            clone_dir, ["--commit", "-m", "feat: mine", "--", "mine.py"]
        )
        assert rc == 0, stderr
        rc, stdout2, stderr2 = _run_push_guard(clone_dir)
        assert rc == 0, stderr2
        assert "grafts" not in stdout + stderr + stdout2 + stderr2

    def test_no_entry_evicts_another(self, repo_pair):
        """One file per commit: however many branches write, every entry stays."""
        clone_dir, _ = repo_pair
        git_dir = _git(clone_dir, "rev-parse", "--absolute-git-dir")
        shas = ["{:040x}".format(i + 1) for i in range(80)]
        for i, sha in enumerate(shas):
            _PG.record_built(git_dir, sha, {"p{}.py".format(i).encode()})
        assert all(
            _PG._built(str(_built_dir(clone_dir) / sha)) == {"p{}.py".format(i).encode()}
            for i, sha in enumerate(shas)
        )

    def test_a_re_created_branch_starts_with_an_empty_record(self, repo_pair):
        """A deleted branch's reflog goes with it, so a new branch of that name inherits nothing."""
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "fix/z")
        Path(clone_dir, "p.py").write_text("p\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "fix: p", "--", "p.py"])
        assert rc == 0, stderr
        _git(clone_dir, "checkout", "-q", "main")
        _git(clone_dir, "branch", "-q", "-D", "fix/z")
        _git(clone_dir, "checkout", "-q", "-b", "fix/z")
        Path(clone_dir, "p.py").write_text("stale\n")
        _git(clone_dir, "add", "p.py")
        _git(clone_dir, "commit", "-q", "-m", "fix: by hand")
        rc, _, stderr = _run_push_guard(clone_dir)
        assert rc == 40 and "p.py" in stderr, stderr

    def test_a_detached_head_never_inherits_another_worktrees_commits(self, repo_pair, tmp_path):
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "--detach")
        Path(clone_dir, "p.py").write_text("p\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "fix: p", "--", "p.py"])
        assert rc == 0, stderr
        rc, stdout, stderr = _run_push_guard(clone_dir)
        assert rc == 0 and "SAFE TO PUSH" in stdout, stderr
        other = str(tmp_path / "other")
        _git(clone_dir, "worktree", "add", "-q", "--detach", other, "main")
        Path(other, "p.py").write_text("q\n")
        _git(other, "add", "p.py")
        _git(other, "commit", "-q", "-m", "fix: by hand")
        rc, _, stderr = _run_push_guard(other)
        assert rc == 40 and "p.py" in stderr, stderr

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="the hook stand-in is sh")
    def test_a_commit_a_hook_rejected_records_nothing(self, repo_pair, tmp_path):
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        _install_hooks(clone_dir, tmp_path, commit_msg="exit 1\n")
        Path(clone_dir, "p.py").write_text("p\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "fix: p", "--", "p.py"])
        assert rc == 40, stderr
        _git(clone_dir, "config", "--unset", "core.hooksPath")
        _git(clone_dir, "add", "p.py")
        _git(clone_dir, "commit", "-q", "-m", "fix: by hand")
        rc, _, stderr = _run_push_guard(clone_dir)
        assert rc == 40 and "p.py" in stderr, stderr

    def test_a_maintainers_commit_on_the_pushed_branch_is_kept(self, repo_pair, tmp_path):
        """A commit already on the pull request is published, not leaked from this index."""
        clone_dir, origin_dir = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feat")
        Path(clone_dir, "mine.py").write_text("m\n")
        rc, _, stderr = _run_push_guard(
            clone_dir, ["--commit", "-m", "feat: mine", "--", "mine.py"]
        )
        assert rc == 0, stderr
        _git(clone_dir, "push", "-q", "-u", "origin", "feat")
        _git(str(tmp_path), "clone", "-q", origin_dir, "maint")
        maint = str(tmp_path / "maint")
        _git(maint, "checkout", "-q", "feat")
        Path(maint, "maint.py").write_text("fix\n")
        _git(maint, "add", "maint.py")
        _git(maint, "commit", "-q", "-m", "fix: maintainer")
        _git(maint, "push", "-q", "origin", "feat")
        _git(clone_dir, "pull", "-q", "--rebase", "origin", "feat")
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "2"])
        assert rc == 0 and "SAFE TO PUSH" in stdout, stderr
        # A copy of that path this checkout changed is not the published one.
        Path(clone_dir, "maint.py").write_text("other\n")
        _git(clone_dir, "add", "maint.py")
        _git(clone_dir, "commit", "-q", "-m", "fix: by hand")
        rc, _, stderr = _run_push_guard(clone_dir, ["--max-ahead", "3"])
        assert rc == 40 and "maint.py" in stderr, stderr

    def test_the_carry_remedy_keeps_the_record(self, repo_pair, tmp_path):
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feat")
        Path(clone_dir, "old.py").write_text("o\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "feat: old", "--", "old.py"])
        assert rc == 0, stderr
        carried = str(tmp_path / "carried")
        _git(clone_dir, "worktree", "add", "-q", "-b", "feat2", carried, "HEAD")
        rc, stdout, stderr = _run_push_guard(carried)
        assert rc == 0 and "SAFE TO PUSH" in stdout, stderr

    def test_a_clean_rebase_across_an_upstream_rename_stays_recorded(self, repo_pair):
        clone_dir, _ = repo_pair
        body = "".join("line {}\n".format(i) for i in range(20))
        _land_on_main(clone_dir, {"old.txt": body}, "feat: old")
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "old.txt").write_text("changed\n" + body)
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "fix: old", "--", "old.txt"])
        assert rc == 0, stderr
        _land_on_main(clone_dir, {"old.txt": None, "new.txt": body}, "refactor: rename")
        _git(clone_dir, "checkout", "-q", "feature/x")
        _git(clone_dir, "rebase", "-q", "refs/remotes/origin/main")
        changed = _git(clone_dir, "diff", "--name-only", "refs/remotes/origin/main", "HEAD")
        assert changed == "new.txt"
        rc, stdout, stderr = _run_push_guard(clone_dir)
        assert rc == 0 and "SAFE TO PUSH" in stdout, stderr

    def test_naming_a_path_in_the_push_check_vouches_without_rewriting(self, repo_pair):
        """An author's commit plus a follow-up: vouching keeps both commits and authors."""
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "author.py").write_text("a\n")
        _git(clone_dir, "add", "author.py")
        _git(clone_dir, "commit", "-q", "--author", "Author <author@example.invalid>", "-m", "x")
        Path(clone_dir, "mine.py").write_text("m\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "fix: mine", "--", "mine.py"])
        assert rc == 0, stderr
        head = _rev(clone_dir, "HEAD")
        rc, _, stderr = _run_push_guard(clone_dir, ["--max-ahead", "2"])
        assert rc == 40 and "author.py" in stderr, stderr
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "2", "--", "author.py"])
        assert rc == 0 and "SAFE TO PUSH" in stdout, stderr
        assert _rev(clone_dir, "HEAD") == head
        assert _git(clone_dir, "log", "-1", "--format=%an", "HEAD^") == "Author"
        # Read-only: the vouch is for that run, never recorded.
        rc, _, stderr = _run_push_guard(clone_dir, ["--max-ahead", "2"])
        assert rc == 40 and "author.py" in stderr, stderr

    def test_a_listed_path_is_named_back_as_printed(self, repo_pair):
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "sub").mkdir()
        for name in ("b.py", "sub/c.py"):
            Path(clone_dir, name).write_text("x\n")
            _git(clone_dir, "add", name)
        _git(clone_dir, "commit", "-q", "-m", "x: by hand")
        rc, _, stderr = _run_push_guard(clone_dir)
        assert rc == 40, stderr
        printed = [
            shlex.split(line)[0]
            for line in stderr.splitlines()
            if line.startswith("    ") and ":(top,literal)" in line
        ]
        assert sorted(printed) == [":(top,literal)b.py", ":(top,literal)sub/c.py"]
        list_file = next(
            line.split(": ", 1)[1] for line in stderr.splitlines() if "Full list" in line
        )
        rc, stdout, stderr = _run_from(str(Path(clone_dir, "sub")), ["--", *printed])
        assert rc == 0 and "SAFE TO PUSH" in stdout, stderr
        rc, stdout, stderr = _run_from(
            str(Path(clone_dir, "sub")), ["--paths-from-file", list_file]
        )
        assert rc == 0 and "SAFE TO PUSH" in stdout, stderr

    def test_a_stale_copy_you_pushed_by_hand_is_never_published(self, repo_pair):
        """A hand commit of yours that reached the remote once is still read and named."""
        clone_dir, _ = repo_pair
        _land_on_main(clone_dir, {"b.txt": "v1\n"}, "feat: b")
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "a.txt").write_text("mine\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "feat: a", "--", "a.txt"])
        assert rc == 0, stderr
        _land_on_main(clone_dir, {"b.txt": "v2\n"}, "fix: b")
        _git(clone_dir, "checkout", "-q", "feature/x")
        _git(clone_dir, "rebase", "-q", "refs/remotes/origin/main")
        # Another session of the same identity overlays the old copy and pushes it.
        Path(clone_dir, "b.txt").write_text("v1\n")
        _git(clone_dir, "add", "b.txt")
        _git(clone_dir, "commit", "-q", "--author", _GUARD_IDENT, "-m", "oops")
        _git(clone_dir, "push", "-q", "origin", "feature/x")
        rc, _, stderr = _run_push_guard(clone_dir, ["--max-ahead", "2"])
        assert rc == 40 and "b.txt" in stderr, stderr
        message = _write_message(clone_dir)
        rc, _, stderr = _run_push_guard(clone_dir, ["--max-ahead", "2", "--squash", message])
        assert rc == 40 and "b.txt" in stderr, stderr

    def test_a_stale_remote_ref_of_a_reused_name_publishes_nothing(self, repo_pair):
        """origin/<branch> left by an earlier, merged branch of the name is not this branch's."""
        clone_dir, _ = repo_pair
        _land_on_main(clone_dir, {"p.txt": "v1\n"}, "feat: p")
        _git(clone_dir, "checkout", "-q", "-b", "fix/x")
        Path(clone_dir, "p.txt").write_text("v2\n")
        _git(clone_dir, "commit", "-q", "-am", "fix: p (another author)")
        _git(clone_dir, "push", "-q", "-u", "origin", "fix/x")
        _land_on_main(clone_dir, {"p.txt": "v3\n"}, "fix: p again")
        _git(clone_dir, "branch", "-q", "-D", "fix/x")
        _git(clone_dir, "checkout", "-q", "-b", "fix/x", "refs/remotes/origin/main")
        assert _rev(clone_dir, "refs/remotes/origin/fix/x")
        Path(clone_dir, "q.txt").write_text("q\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "fix: q", "--", "q.txt"])
        assert rc == 0, stderr
        Path(clone_dir, "p.txt").write_text("v2\n")
        _git(clone_dir, "commit", "-q", "-am", "stale overlay")
        rc, _, stderr = _run_push_guard(clone_dir, ["--max-ahead", "2"])
        assert rc == 40 and "p.txt" in stderr, stderr

    def test_a_maintainers_commit_stays_published_across_a_rebase(self, repo_pair, tmp_path):
        """origin/<branch> reached by a past tip of the branch (its reflog) still counts."""
        clone_dir, origin_dir = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feat")
        Path(clone_dir, "mine.py").write_text("m\n")
        rc, _, stderr = _run_push_guard(
            clone_dir, ["--commit", "-m", "feat: mine", "--", "mine.py"]
        )
        assert rc == 0, stderr
        _git(clone_dir, "push", "-q", "-u", "origin", "feat")
        _git(str(tmp_path), "clone", "-q", origin_dir, "maint")
        maint = str(tmp_path / "maint")
        _git(maint, "checkout", "-q", "feat")
        Path(maint, "maint.py").write_text("fix\n")
        _git(maint, "add", "maint.py")
        _git(maint, "commit", "-q", "-m", "fix: maintainer")
        _git(maint, "push", "-q", "origin", "feat")
        _git(clone_dir, "pull", "-q", "--rebase", "origin", "feat")
        _land_on_main(clone_dir, {"up.txt": "u\n"}, "feat: upstream")
        _git(clone_dir, "checkout", "-q", "feat")
        _git(clone_dir, "rebase", "-q", "refs/remotes/origin/main")
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--max-ahead", "2"])
        assert rc == 0 and "SAFE TO PUSH" in stdout, stderr

    @pytest.mark.parametrize(
        "reset",
        [
            ["switch", "-q", "-C", "fix/x", "refs/remotes/origin/main"],
            ["checkout", "-q", "-B", "fix/x", "refs/remotes/origin/main"],
            ["reset", "-q", "--hard", "refs/remotes/origin/main"],
        ],
        ids=["switch-C", "checkout-B", "reset-hard"],
    )
    def test_a_name_reset_in_place_inherits_nothing(self, repo_pair, reset):
        """The reflog survives a reset of the name, but the lookup stops at the reset."""
        clone_dir, _ = repo_pair
        _land_on_main(clone_dir, {"p.txt": "v1\n"}, "feat: p")
        _git(clone_dir, "checkout", "-q", "-b", "fix/x")
        Path(clone_dir, "p.txt").write_text("v2\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "fix: p", "--", "p.txt"])
        assert rc == 0, stderr
        _land_on_main(clone_dir, {"p.txt": "v3\n"}, "fix: p again")
        _git(clone_dir, "checkout", "-q", "fix/x")
        _git(clone_dir, *reset)
        Path(clone_dir, "q.txt").write_text("q\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--commit", "-m", "fix: q", "--", "q.txt"])
        assert rc == 0, stderr
        Path(clone_dir, "p.txt").write_text("v2\n")
        _git(clone_dir, "commit", "-q", "-am", "stale overlay")
        rc, _, stderr = _run_push_guard(clone_dir, ["--max-ahead", "2"])
        assert rc == 40 and "p.txt" in stderr and "q.txt" not in stderr, stderr

    def test_an_amend_after_a_rebase_across_an_upstream_rename_is_accepted(self, repo_pair):
        """--amend accounts for a path exactly as the push check does."""
        clone_dir, _ = repo_pair
        body = "".join("line {}\n".format(i) for i in range(20))
        _land_on_main(clone_dir, {"old/f.txt": body}, "feat: f")
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "old", "f.txt").write_text("changed\n" + body)
        rc, _, stderr = _run_push_guard(
            clone_dir, ["--commit", "-m", "feat: mine", "--", "old/f.txt"]
        )
        assert rc == 0, stderr
        _land_on_main(clone_dir, {"old/f.txt": None, "new/f.txt": body}, "refactor: move")
        _git(clone_dir, "checkout", "-q", "feature/x")
        _git(clone_dir, "rebase", "-q", "refs/remotes/origin/main")
        rc, stdout, stderr = _run_push_guard(clone_dir)
        assert rc == 0 and "SAFE TO PUSH" in stdout, stderr
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--amend", "-m", "feat: mine v2"])
        assert rc == 0 and "COMMITTED" in stdout, stderr

    def test_the_amend_refusal_says_naming_commits_the_worktree_copy(self, repo_pair):
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "hand.py").write_text("h\n")
        _git(clone_dir, "add", "hand.py")
        _git(clone_dir, "commit", "-q", "-m", "feat: by hand")
        rc, _, stderr = _run_push_guard(clone_dir, ["--amend", "-m", "feat: reworded"])
        assert rc == 40 and "hand.py" in stderr, stderr
        assert "commits its worktree copy" in stderr, stderr
        assert "git diff HEAD -- <path>" in stderr, stderr
        assert "it only reads them" not in stderr and "instead" not in stderr, stderr

    def test_the_push_check_alternative_keeps_the_callers_arguments(self, repo_pair):
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        Path(clone_dir, "hand.py").write_text("h\n")
        _git(clone_dir, "add", "hand.py")
        _git(clone_dir, "commit", "-q", "-m", "feat: by hand")
        rc, _, stderr = _run_push_guard(clone_dir, ["--max-ahead", "3"])
        assert rc == 40 and "it only reads them" in stderr, stderr
        assert "--max-ahead 3 --squash <message-file> -- <path>" in stderr, stderr

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="needs an unprivileged symlink")
    def test_a_listed_path_under_a_parent_now_a_symlink_is_named_back(self, repo_pair):
        """A ``:(top,literal)`` name is read as written, never through the worktree's symlinks."""
        clone_dir, _ = repo_pair
        _land_on_main(clone_dir, {"guide/a.md": "a\n", "book/a.md": "s\n"}, "feat: a")
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        _git(clone_dir, "rm", "-q", "-r", "guide")
        os.symlink("book", str(Path(clone_dir, "guide")))
        _git(clone_dir, "add", "guide")
        _git(clone_dir, "commit", "-q", "-m", "feat: link")
        rc, _, stderr = _run_push_guard(clone_dir)
        assert rc == 40, stderr
        list_file = next(
            line.split(": ", 1)[1] for line in stderr.splitlines() if "Full list" in line
        )
        rc, stdout, stderr = _run_push_guard(clone_dir, ["--paths-from-file", list_file])
        assert rc == 0 and "SAFE TO PUSH" in stdout, stderr
        rc, stdout, stderr = _run_push_guard(
            clone_dir, ["--", ":(top,literal)guide", ":(top,literal)guide/a.md"]
        )
        assert rc == 0 and "SAFE TO PUSH" in stdout, stderr

    @pytest.mark.parametrize("name", ["../outside", "a/../../outside", "/abs"])
    def test_a_top_literal_name_that_leaves_the_top_is_a_usage_error(self, repo_pair, name):
        clone_dir, _ = repo_pair
        _git(clone_dir, "checkout", "-q", "-b", "feature/x")
        rc, _, stderr = _run_push_guard(clone_dir, ["--", ":(top,literal)" + name])
        assert rc == _PG.EXIT_USAGE and "not inside this repository" in stderr, stderr


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="the hook stand-ins are sh")
class TestPushGuardSquashMessage:
    """The squash message goes through the hooks and cleanup ``git commit -F`` applies."""

    def test_a_hook_quoting_a_missing_git_command_still_rejects(self, repo_pair, tmp_path):
        clone_dir, _ = repo_pair
        head = _feature(clone_dir)
        _install_hooks(
            clone_dir,
            tmp_path,
            commit_msg="echo \"git: 'secrets' is not a git command. See 'git --help'.\" >&2\nexit 1\n",
        )
        _write_message(clone_dir)
        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 40 and "commit-msg hook rejected" in stderr, stderr
        assert _rev(clone_dir, "HEAD") == head

    def test_a_git_without_hook_run_refuses_when_hooks_exist(
        self, repo_pair, tmp_path, monkeypatch
    ):
        clone_dir, _ = repo_pair
        head = _feature(clone_dir)
        _install_hooks(clone_dir, tmp_path, commit_msg="exit 0\n")
        _write_message(clone_dir)
        rc, _, stderr = _inprocess(
            monkeypatch,
            clone_dir,
            extra_args=["--squash"],
            overrides={"_git_version": lambda: (2, 30)},
        )
        assert rc == 40 and "needs git 2.36" in stderr, stderr
        assert _rev(clone_dir, "HEAD") == head

    def test_an_old_git_squashes_when_the_hooks_path_has_no_message_hook(
        self, repo_pair, tmp_path, monkeypatch
    ):
        """A hooks path holding only other hooks (pre-commit, pre-push) is no reason to refuse."""
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        _install_hooks(clone_dir, tmp_path, pre_commit="exit 0\n")
        _write_message(clone_dir)
        rc, stdout, stderr = _inprocess(
            monkeypatch,
            clone_dir,
            extra_args=["--squash"],
            overrides={"_git_version": lambda: (2, 30)},
        )
        assert rc == 0 and "SQUASHED" in stdout, stderr

    def test_prepare_commit_msg_runs_for_a_squash(self, repo_pair, tmp_path):
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        _install_hooks(
            clone_dir,
            tmp_path,
            prepare_commit_msg='[ "$2" = message ] || exit 1\n'
            '{ printf \'[ABC-1] \'; cat "$1"; } > "$1.tmp" && mv "$1.tmp" "$1"\n',
        )
        _write_message(clone_dir)
        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 0, stderr
        assert _git(clone_dir, "log", "-1", "--format=%s") == "[ABC-1] feat: the squashed change"

    def test_a_hook_that_blanks_the_message_is_refused(self, repo_pair, tmp_path):
        clone_dir, _ = repo_pair
        head = _feature(clone_dir)
        _install_hooks(clone_dir, tmp_path, commit_msg=': > "$1"\n')
        _write_message(clone_dir)
        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 40 and "empty after its hooks" in stderr, stderr
        assert _rev(clone_dir, "HEAD") == head

    @pytest.mark.parametrize(("cleanup", "kept"), [(None, True), ("strip", False)])
    def test_commit_cleanup_decides_whether_comment_lines_stay(self, repo_pair, cleanup, kept):
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        if cleanup:
            _git(clone_dir, "config", "commit.cleanup", cleanup)
        _write_message(clone_dir, "feat: x\n\n# a note\nBody.\n")
        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 0, stderr
        assert ("# a note" in _git(clone_dir, "log", "-1", "--format=%B")) is kept

    def test_a_non_utf8_byte_a_hook_adds_is_kept(self, repo_pair, tmp_path):
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        _install_hooks(clone_dir, tmp_path, commit_msg="printf 'Tag: \\377\\n' >> \"$1\"\n")
        _write_message(clone_dir)
        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 0, stderr
        raw = subprocess.run(
            ["git", "cat-file", "commit", "HEAD"],
            cwd=clone_dir,
            capture_output=True,
            check=True,
            env=_fixture_git_env(),
            timeout=_SUBPROCESS_TIMEOUT_S,
        ).stdout
        assert "Tag: \u00ff" in raw.decode("utf-8")  # git re-encodes it; nothing died

    def test_hooks_see_an_index_of_the_squash_and_no_editor(self, repo_pair, tmp_path):
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        seen = shlex.quote(str(tmp_path / "seen.txt"))
        _install_hooks(
            clone_dir,
            tmp_path,
            commit_msg='echo "$GIT_EDITOR|$(git ls-files | tr "\\n" ,)" > ' + seen + "\n",
        )
        _write_message(clone_dir)
        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 0, stderr
        assert (tmp_path / "seen.txt").read_text().strip() == ":|README.md,mine.py,"

    def test_signing_reads_the_callers_terminal(self, repo_pair, tmp_path):
        """The signer gets the caller's stdin, so a passphrase prompt can be answered."""
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        signer = tmp_path / "fake-ssh-keygen"
        signer.write_text(
            '#!/bin/sh\nread answer\n[ "$answer" = yes ] || exit 1\n'
            "for last; do :; done\n"
            "printf -- '-----BEGIN SSH SIGNATURE-----\\nZmFrZQ==\\n"
            '-----END SSH SIGNATURE-----\\n\' > "$last.sig"\n'
        )
        signer.chmod(0o755)
        public_key = tmp_path / "signing-key.pub"
        public_key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTests\n")
        for key, value in (
            ("commit.gpgSign", "true"),
            ("gpg.format", "ssh"),
            ("gpg.ssh.program", str(signer)),
            # A key FILE, never a literal ``key::``: git writes a literal one to a
            # temporary file it never removes, which would outlive the test.
            ("user.signingKey", str(public_key)),
        ):
            _git(clone_dir, "config", key, value)
        _write_message(clone_dir)
        proc = subprocess.Popen(
            [sys.executable, PUSH_GUARD, "--base", "main", "--squash"],
            cwd=clone_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            _, err = proc.communicate(b"yes\n", timeout=_SUBPROCESS_TIMEOUT_S)
        finally:
            if proc.poll() is None:
                platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
                proc.wait()
        assert proc.returncode == 0, err
        assert "SSH SIGNATURE" in _git(clone_dir, "cat-file", "commit", "HEAD")

    def test_a_long_hook_rejection_shows_its_end(self, repo_pair, tmp_path):
        clone_dir, _ = repo_pair
        _feature(clone_dir)
        _install_hooks(
            clone_dir,
            tmp_path,
            commit_msg='i=0\nwhile [ $i -lt 100 ]; do echo "noise $i" >&2; i=$((i+1)); done\n'
            "echo REASON-AT-THE-END >&2\nexit 1\n",
        )
        _write_message(clone_dir)
        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"])
        assert rc == 40 and "REASON-AT-THE-END" in stderr, stderr


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX process groups and signals")
class TestPushGuardChildren:
    """A child is stopped with the guard, however the guard is stopped."""

    @staticmethod
    def _sleeping_fetch(tmp_path: Path) -> tuple[dict[str, str], Path]:
        """PATH whose git sleeps in fetch (writing its pid first), else is the real git."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        pid_file = tmp_path / "fetch.pid"
        fake = bin_dir / "git"
        fake.write_text(
            "#!{}\nimport os, sys, time\n"
            "if 'fetch' in sys.argv:\n"
            "    open({!r}, 'w').write(str(os.getpid()))\n"
            "    time.sleep(60)\n"
            "os.execv({!r}, [{!r}] + sys.argv[1:])\n".format(
                sys.executable, str(pid_file), shutil.which("git"), "git"
            )
        )
        fake.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        return env, pid_file

    @staticmethod
    def _gone(pid: int) -> bool:
        for _ in range(100):
            if not platform_compat.pid_exists(pid):
                return True
            time.sleep(0.1)
        return False

    @pytest.mark.parametrize("how", ["SIGKILL-the-group", "SIGHUP"])
    def test_the_child_dies_with_the_guard(self, repo_pair, tmp_path, how):
        clone_dir, _ = repo_pair
        env, pid_file = self._sleeping_fetch(tmp_path)
        proc = subprocess.Popen(
            [sys.executable, PUSH_GUARD, "--base", "main"],
            cwd=clone_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        child = 0
        try:
            for _ in range(300):
                if pid_file.exists() and pid_file.read_text():
                    break
                time.sleep(0.1)
            child = int(pid_file.read_text())
            if how == "SIGHUP":
                os.kill(proc.pid, getattr(signal, "SIGHUP"))
            else:
                os.killpg(proc.pid, getattr(signal, "SIGKILL"))
            _, err = proc.communicate(timeout=_SUBPROCESS_TIMEOUT_S)
            assert self._gone(child), "the fetch outlived the guard"
            if how == "SIGHUP":
                assert proc.returncode == 40 and b"interrupted" in err, err
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, getattr(signal, "SIGKILL"))
                proc.wait()
            if child and platform_compat.pid_exists(child):
                os.kill(child, getattr(signal, "SIGKILL"))  # only the sleeper this test planted

    def test_an_exit_code_of_124_is_not_a_timeout(self, repo_pair, tmp_path, monkeypatch):
        clone_dir, _ = repo_pair
        fake = TestReplayFailClosed._make_fake_git_cmd(
            tmp_path, "'merge-base' in args", failure_message="fatal: odd"
        )
        Path(fake[1]).write_text(
            Path(fake[1]).read_text().replace("sys.exit(128)", "sys.exit(124)")
        )
        rc, _, stderr = _inprocess(monkeypatch, clone_dir, fake)
        assert rc == 40 and "failed (exit 124)" in stderr and "timed out" not in stderr, stderr

    def test_windows_stops_politely_before_it_forces(self, monkeypatch):
        pg = _load_push_guard()
        calls: list[list[str]] = []
        monkeypatch.setattr(pg.sys, "platform", "win32")
        monkeypatch.setattr(pg, "_taskkill", lambda: "C:\\Windows\\System32\\taskkill.exe")
        monkeypatch.setattr(pg.subprocess, "run", lambda argv, **kw: calls.append(argv))

        class _Proc:
            pid = 4242
            waits = 0

            def wait(self, timeout=None):
                _Proc.waits += 1
                if _Proc.waits == 1:
                    raise subprocess.TimeoutExpired("git", timeout)
                return 1

        pg._stop(_Proc())
        assert [c[1:-2] for c in calls] == [["/T"], ["/T", "/F"]]


def _git_bytes(cwd: str, args: list[str], data: bytes) -> str:
    """Run git with bytes on stdin; return stripped stdout."""
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=data,
        capture_output=True,
        check=True,
        env=_fixture_git_env(),
        timeout=_SUBPROCESS_TIMEOUT_S,
    )
    return proc.stdout.decode().strip()


class TestPushGuardContracts:
    """Facts other files depend on, pinned against the script."""

    def test_the_retargeting_floor_covers_the_product_list(self):
        assert _GIT_LOCATION_VARS - {"GIT_CEILING_DIRECTORIES"} <= _PG._RELOCATION_VARS
        assert not {"GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"} & _PG._RELOCATION_VARS
        env = _PG.child_env()
        assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
        assert not os.path.lexists(env["GIT_GRAFT_FILE"])
        # A non-interactive credential source is wired through GIT_ASKPASS.
        assert "GIT_ASKPASS" not in _PG.NO_PROMPT_ENV and "SSH_ASKPASS" not in _PG.NO_PROMPT_ENV

    @pytest.mark.parametrize(
        ("root", "expected"),
        [
            ("D:\\Win", "D:\\Win\\System32\\taskkill.exe"),
            ("Windows", "C:\\Windows\\System32\\taskkill.exe"),
            ("", "C:\\Windows\\System32\\taskkill.exe"),
        ],
    )
    def test_taskkill_is_only_ever_an_absolute_path(self, monkeypatch, root, expected):
        """SystemRoot is honoured (Windows on another drive), but only as a drive path."""
        monkeypatch.setenv("SystemRoot", root)
        monkeypatch.setattr(_PG.os.path, "isfile", lambda path: True)
        assert _PG._taskkill() == expected

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("ssh -i key", "ssh -i key -oBatchMode=yes"),
            ("/usr/bin/ssh", "/usr/bin/ssh -oBatchMode=yes"),
            ("plink.exe -P 22", "plink.exe -P 22 -batch"),
            ("my-wrapper --x", "my-wrapper --x"),
        ],
    )
    def test_batch_mode_matches_the_transport(self, monkeypatch, command, expected):
        monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
        env = _PG.ssh_batch_env(lambda args: (0, command, ""))
        assert env == {"GIT_SSH_COMMAND": expected}

    def test_a_legacy_git_ssh_program_is_left_alone(self, monkeypatch):
        monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
        monkeypatch.setenv("GIT_SSH", "/opt/ssh-wrapper")
        assert _PG.ssh_batch_env(lambda args: (1, "", "")) == {}

    def test_the_docs_name_the_message_file_the_script_reads(self):
        skill = (PUSH_GUARD_DIR.parent / "SKILL.md").read_text(encoding="utf-8")
        brief = (
            REPO_ROOT / "src" / "kiro_crew" / "builtin_skills" / "pipeline-conductor" / "SKILL.md"
        ).read_text(encoding="utf-8")
        hint = (REPO_ROOT / ".github" / "workflows" / "code-review.yml").read_text(encoding="utf-8")
        for text in (skill, brief, hint):
            assert _PG._MESSAGE_PREFIX in text and "push_guard.py --base" in text

    def test_the_ci_squash_remedy_vouches_only_after_the_refusal_lists(self):
        """CI's readers committed by hand: the squash refuses first, and they name its list.

        Never a listing of the whole diff to vouch with: that names a stale
        tree's every path as readily as an honest change's few.
        """
        hint = (REPO_ROOT / ".github" / "workflows" / "code-review.yml").read_text(encoding="utf-8")
        line = next(text for text in hint.splitlines() if "push_guard.py --base" in text)
        assert line.rstrip().endswith('--squash <that file>"'), line
        assert not [t for t in hint.splitlines() if "echo" in t and "git diff --name-only" in t]
        assert "--paths-from-file <that list>" in hint and "Read each one's diff" in hint
        assert "stale tree: name none" in hint

    def test_an_inherited_ignore_survives_the_termination_handlers(self):
        """Under ``nohup`` SIGHUP stays ignored, for the guard and the git children it starts."""
        number = signal.SIGTERM
        before = signal.getsignal(number)
        signal.signal(number, signal.SIG_IGN)
        try:
            previous = _PG.install_termination_handlers()
            try:
                assert signal.getsignal(number) == signal.SIG_IGN
                assert number not in [n for n, _ in previous]
            finally:
                _PG.restore_handlers(previous)
        finally:
            signal.signal(number, before)

    def test_the_skill_states_every_exit_code_the_script_defines(self):
        skill = (PUSH_GUARD_DIR.parent / "SKILL.md").read_text(encoding="utf-8")
        row = next(line for line in skill.splitlines() if line.startswith("| `push_guard.py"))
        for code, _ in _PG.EXIT_CODES:
            assert re.search(rf"\b{code}\b", row.rsplit("|", 2)[-2]), code

    def test_no_line_trips_the_portability_gate(self):
        """The cross-platform job greps added lines for POSIX-only calls; these scripts pass."""
        workflow = (REPO_ROOT / ".github" / "workflows" / "cross-platform.yml").read_text(
            encoding="utf-8"
        )
        rule = r"\b(os\.(fork|kill|getuid|geteuid|setsid|getpgid|killpg)|signal\.(SIGKILL|SIGHUP|SIGUSR[12]|SIGCHLD))\b"
        assert rule in workflow, "the gate's rule moved; re-pin it here"
        # The manual '/' path-assembly rule, as the gate's grep spells it.
        assert "Manual '/' path assembly" in workflow, "the gate's rule moved; re-pin it here"
        joins = r"""\.(split|join|rsplit)\(["']/["']\)|\+\s*["']/["']\s*\+"""
        for script in ("push_guard.py", "preflight.py"):
            code = [
                line
                for line in (PUSH_GUARD_DIR / script).read_text(encoding="utf-8").splitlines()
                if not line.lstrip().startswith("#")
            ]
            assert not [line for line in code if re.search(rule, line)], script
            assert not [line for line in code if re.search(joins, line)], script

    def test_annotations_are_never_evaluated_so_python_3_9_imports_it(self):
        """``list[str] | None`` raises TypeError at import on 3.9 unless left unevaluated."""
        assert _PG.__annotations__, "the module has annotations to check"
        assert all(isinstance(v, str) for v in _PG.__annotations__.values())

    def test_no_shipped_text_carries_the_soft_reset_squash(self):
        roots = [
            REPO_ROOT / "src" / "kiro_crew" / "builtin_skills",
            REPO_ROOT / ".github" / "workflows",
            REPO_ROOT / "scripts",
        ]
        recipe = re.compile(r"reset --soft (origin/|\"?\$|refs/)")
        offenders = [
            str(path.relative_to(REPO_ROOT))
            for root in roots
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix in {".py", ".md", ".yml", ".yaml", ".sh"}
            and recipe.search(path.read_text(encoding="utf-8", errors="replace"))
        ]
        assert offenders == []


class TestGitResolution:
    """git is never taken from the checkout under guard."""

    @staticmethod
    def _plant_git(directory: Path) -> Path:
        name = "git.exe" if sys.platform == "win32" else "git"
        exe = directory / name
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
        return exe

    def test_a_relative_path_entry_is_skipped(self, tmp_path, monkeypatch):
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        tools = tmp_path / "tools"
        tools.mkdir()
        self._plant_git(checkout)
        real = self._plant_git(tools)
        monkeypatch.chdir(checkout)
        monkeypatch.setenv("PATH", os.pathsep.join([".", "", str(tools)]))
        pg = _load_push_guard()
        argv = pg._git_argv(["git", "status"])
        assert Path(argv[0]).resolve() == real.resolve() and argv[1:] == ["status"]

    def test_no_git_on_path_never_resolves_into_the_checkout(self, tmp_path, monkeypatch):
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        self._plant_git(checkout)
        monkeypatch.chdir(checkout)
        monkeypatch.setenv("PATH", ".")
        pg = _load_push_guard()
        assert pg._which_git() is None
        argv = pg._git_argv(["git", "status"])
        assert os.path.isabs(argv[0]) and not os.path.exists(argv[0])
        assert argv[1:] == ["status"]

    @pytest.mark.parametrize("launcher", ["git.cmd", "GIT.BAT"])
    def test_a_batch_launcher_never_receives_cmd_metacharacters(self, tmp_path, launcher):
        pg = _load_push_guard()
        res = pg.run_child([str(tmp_path / launcher), "fetch", "origin", "main&calc&"], None, 5)
        assert res.rc == 126 and b"metacharacters" in res.err
