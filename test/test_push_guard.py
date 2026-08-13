"""Tests for the prepare-pr push_guard.py stale-base detection.

Verifies that the push_guard script (src/kiro_crew/builtin_skills/kirocrew-dev/
prepare-pr/scripts/push_guard.py) correctly refuses to push when:
- The branch has no common history with origin/<base> (orphan / disconnected)
- The commit count exceeds --max-ahead (implausibly many commits for a PR)
- The fetch of origin/<base> fails (network error → fail closed)

And allows push when the branch is a normal single-commit PR (1 commit ahead
of a fresh origin/<base> with shared history).

Regression test for the 2026-07-31 clobber incident: a force-push from a
worktree branched off kiki-trunk carried 114 duplicate commits.
"""

from __future__ import annotations

import importlib
import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Resolve the push_guard.py script path relative to the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
PUSH_GUARD = str(
    REPO_ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "prepare-pr"
    / "scripts"
    / "push_guard.py"
)


def _run_push_guard(cwd: str, extra_args: list[str] | None = None) -> tuple[int, str, str]:
    """Run push_guard.py in the given directory; return (rc, stdout, stderr)."""
    args = [sys.executable, PUSH_GUARD, "--base", "main"]
    if extra_args:
        args.extend(extra_args)
    proc = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _git(cwd: str, *args: str) -> str:
    """Run a git command in cwd; raise on failure."""
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def repo_pair(tmp_path):
    """Create a local 'origin' bare repo and a working clone.

    Returns (clone_dir, origin_dir) where origin_dir is a bare repo and
    clone_dir has 'origin' pointing at origin_dir.
    """
    origin_dir = str(tmp_path / "origin.git")
    clone_dir = str(tmp_path / "work")

    # Create a bare origin with one commit on main.
    os.makedirs(origin_dir)
    _git(origin_dir, "init", "--bare")
    _git(origin_dir, "symbolic-ref", "HEAD", "refs/heads/main")

    # Clone it.
    _git(str(tmp_path), "clone", origin_dir, "work")
    _git(clone_dir, "checkout", "-b", "main")

    # Create an initial commit on main.
    Path(clone_dir, "README.md").write_text("initial\n")
    _git(clone_dir, "add", "README.md")
    _git(clone_dir, "commit", "-m", "initial commit")
    _git(clone_dir, "push", "-u", "origin", "main")

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

    def test_not_a_git_repo(self, tmp_path):
        """Running outside a git repo → exit 2."""
        rc, stdout, stderr = _run_push_guard(str(tmp_path))
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

    Regression test for the vacuous is-ancestor check that previously tested
    merge-base against origin/<base> (true by construction). The corrected
    check verifies that origin/<base> itself is an ancestor of HEAD — i.e. the
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
        HEAD~1 no longer equals the (now-advanced) origin/main.
        """
        clone_dir, origin_dir = repo_pair

        # Create a squashed feature branch (single commit on origin/main).
        _git(clone_dir, "checkout", "-b", "feature/narrow-single")
        Path(clone_dir, "squashed.py").write_text("# squashed\n")
        _git(clone_dir, "add", "squashed.py")
        _git(clone_dir, "commit", "-m", "feat: squashed commit")

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
        assert "REFUSED" in stderr

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

        # Create a feature branch with one commit directly on origin/main.
        _git(clone_dir, "checkout", "-b", "feature/squashed")
        Path(clone_dir, "squashed.py").write_text("# squashed\n")
        _git(clone_dir, "add", "squashed.py")
        _git(clone_dir, "commit", "-m", "feat: squashed commit")

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

    def test_fetch_error_redacts_credentials(self, tmp_path):
        """Fetch stderr containing https://user:token@host → refusal redacts the token."""
        repo_dir = str(tmp_path / "repo")
        os.makedirs(repo_dir)
        _git(repo_dir, "init")
        _git(repo_dir, "commit", "--allow-empty", "-m", "init")

        # Set origin to a credential-bearing URL that will fail to fetch.
        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            "https://user:someSecretToken123@example.com/repo.git",
        )

        rc, stdout, stderr = _run_push_guard(repo_dir)
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

    def test_fetch_error_redacts_bare_token_url(self, tmp_path):
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

        rc, stdout, stderr = _run_push_guard(repo_dir)
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

    def test_fetch_error_redacts_query_string_credentials(self, tmp_path):
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

        rc, stdout, stderr = _run_push_guard(repo_dir)
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

    def test_fetch_error_redacts_access_token_query(self, tmp_path):
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

        rc, stdout, stderr = _run_push_guard(repo_dir)
        assert rc == 40
        assert "REFUSED" in stderr
        assert "ghp_TopSecret99" not in stderr, "access_token value leaked"
        assert "ghp_TopSecret99" not in stdout, "access_token value leaked"

    def test_fetch_error_redacts_path_embedded_credentials(self, tmp_path):
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

        rc, stdout, stderr = _run_push_guard(repo_dir)
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

    def test_fetch_error_redacts_query_only_credentials(self, tmp_path):
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

        rc, stdout, stderr = _run_push_guard(repo_dir)
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

    def test_fetch_error_redacts_ipv6_path_credentials(self, tmp_path):
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

        rc, stdout, stderr = _run_push_guard(repo_dir)
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

    def test_fetch_error_redacts_credential_at_truncation_boundary(self, tmp_path):
        """Token straddling the old 300-byte slice boundary must still be redacted.

        Regression for the redact-before-truncate ordering fix: previously the
        code did ``redact_credentials(fetch_err[:300])`` — if the credential
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
        # Construct the origin URL so git's fetch stderr will contain the token.
        # We embed the preamble in the remote URL path so it appears in the
        # error output — but the critical credential is in the userinfo.
        # Simpler approach: use a remote URL with the token in userinfo.
        _git(
            repo_dir,
            "remote",
            "add",
            "origin",
            cred_url,
        )

        rc, stdout, stderr = _run_push_guard(repo_dir)
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
        args = [sys.executable, PUSH_GUARD, "--base", "main"]
        if extra_args:
            args.extend(extra_args)
        env = os.environ.copy()
        env["GIT_SSH_COMMAND"] = fake_ssh
        proc = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        return proc.returncode, proc.stdout, proc.stderr

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
    def _make_fake_git_cmd(tmp_path: Path, fail_condition: str) -> list[str]:
        """Create a cross-platform fake git that fails on a specific condition.

        Uses a Python script assigned directly to push_guard._GIT_CMD (no
        PATH manipulation, no shell launchers, no environment variables).
        This avoids all Windows compatibility issues and Semgrep tainted-env
        findings.

        Args:
            tmp_path: pytest tmp dir for writing script files.
            fail_condition: Python expression evaluated against ``args``
                (the list of git arguments) that triggers the failure.

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
            "    print('fatal: bad revision/object', file=sys.stderr)\n"
            "    sys.exit(128)\n"
            "real_git = {}\n".format(repr(real_git)) + "r = subprocess.run([real_git] + args)\n"
            "sys.exit(r.returncode)\n"
        )

        return [sys.executable, str(fake_git_script)]

    @staticmethod
    def _run_push_guard_inprocess(
        monkeypatch, cwd: str, fake_git_cmd: list[str] | None = None
    ) -> tuple[int, str, str]:
        """Run push_guard.main() in-process with monkeypatched _GIT_CMD.

        Args:
            monkeypatch: pytest monkeypatch fixture.
            cwd: Working directory for the guard (the git repo).
            fake_git_cmd: Command list for push_guard._GIT_CMD, or None
                to use real git.

        Returns:
            (exit_code, stdout, stderr) — mirrors subprocess interface.
        """
        # Ensure push_guard is importable from the scripts directory.
        scripts_dir = str(
            REPO_ROOT
            / "src"
            / "kiro_crew"
            / "builtin_skills"
            / "kirocrew-dev"
            / "prepare-pr"
            / "scripts"
        )
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)

        import push_guard

        # Reload to get a fresh module state (avoids cross-test contamination).
        importlib.reload(push_guard)

        # Monkeypatch the git command injection point.
        if fake_git_cmd is not None:
            monkeypatch.setattr(push_guard, "_GIT_CMD", fake_git_cmd)

        # Monkeypatch argv for argparse.
        monkeypatch.setattr(sys, "argv", ["push_guard.py", "--base", "main"])

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

        fake_cmd = self._make_fake_git_cmd(tmp_path, "any('..HEAD' in a for a in args)")

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
        scripts_dir = str(
            REPO_ROOT
            / "src"
            / "kiro_crew"
            / "builtin_skills"
            / "kirocrew-dev"
            / "prepare-pr"
            / "scripts"
        )
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from push_guard import _classify_fetch_error

        return _classify_fetch_error

    def test_known_class_could_not_resolve_host(self):
        classify = self._import_classify()
        result = classify(
            "fatal: unable to access 'https://example.com/repo.git/': "
            "Could not resolve host: example.com"
        )
        assert result == "could not resolve host"

    def test_known_class_permission_denied(self):
        classify = self._import_classify()
        result = classify("Permission denied (publickey).")
        assert result == "permission denied"

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
        scripts_dir = str(
            REPO_ROOT
            / "src"
            / "kiro_crew"
            / "builtin_skills"
            / "kirocrew-dev"
            / "prepare-pr"
            / "scripts"
        )
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import push_guard

        importlib.reload(push_guard)
        return push_guard

    def test_fetch_failure_with_bare_token_no_leak(self, tmp_path, monkeypatch):
        """When git fetch fails with stderr containing a bare token,
        the refusal output must not contain that token."""
        push_guard = self._import_push_guard()

        # Monkeypatch run() to simulate a fetch failure with bare token stderr
        original_run = push_guard.run

        def fake_run(args):
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
            return original_run(args)

        monkeypatch.setattr(push_guard, "run", fake_run)

        # Capture stderr from _fetch_base
        stderr_buf = io.StringIO()
        monkeypatch.setattr(sys, "stderr", stderr_buf)

        rc = push_guard._fetch_base("main")
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

        def fake_run(args):
            if "fetch" in args:
                return (128, "", "fatal: Could not resolve host: github.com")
            return (0, "", "")

        monkeypatch.setattr(push_guard, "run", fake_run)

        stderr_buf = io.StringIO()
        monkeypatch.setattr(sys, "stderr", stderr_buf)

        rc = push_guard._fetch_base("main")
        stderr_output = stderr_buf.getvalue()

        assert rc == 40
        assert "could not resolve host" in stderr_output
        # Raw URL should not appear
        assert "github.com" not in stderr_output
