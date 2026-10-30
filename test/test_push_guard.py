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

The guard's other families live in sibling modules: ``test_push_guard_diagnostics``
(what a refusal says when git fails), ``test_push_guard_squash`` (building the
commit), ``test_push_guard_index`` (the index, and a git call that fails or hangs)
and ``test_push_guard_record`` (the build record, the script's contracts, where git
comes from). Their shared repos, runners and fixtures are in ``push_guard_helpers``.
"""

from __future__ import annotations

import os
from pathlib import Path

import push_guard_helpers
from git_maintenance_helpers import GIT_MAINTENANCE_OFF_CONFIG
from push_guard_helpers import _bless, _git, _run_push_guard

# pytest resolves a fixture in the requesting test's module, so each shared
# fixture this module uses is bound here by name.
_no_inherited_git_c = push_guard_helpers._no_inherited_git_c
_repo_pair_template = push_guard_helpers._repo_pair_template
repo_pair = push_guard_helpers.repo_pair


def test_the_template_origin_keeps_auto_maintenance_off(_repo_pair_template) -> None:
    # A push into a copy starts receive-pack there without the fixture env, so
    # only the origin's own config keeps it from starting auto maintenance.
    _, template_origin = _repo_pair_template
    for key, value in GIT_MAINTENANCE_OFF_CONFIG:
        assert (key, _git(template_origin, "config", "--local", "--get", key)) == (key, value)


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
