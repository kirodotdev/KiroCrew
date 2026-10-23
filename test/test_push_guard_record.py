"""push_guard.py's build record, the facts other files pin, and where git comes from.

What the guard committed on a branch is recorded, and every SAFE mode reads that
record; the facts other files depend on are pinned against the script; and git is
never taken from the checkout under guard.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import sys
from pathlib import Path

import push_guard_helpers
import pytest
from push_guard_helpers import (
    _PG,
    PUSH_GUARD_DIR,
    REPO_ROOT,
    _bless,
    _commit_tree,
    _feature,
    _git,
    _inprocess,
    _install_hooks,
    _land_on_main,
    _load_push_guard,
    _rev,
    _run_from,
    _run_push_guard,
    _write_message,
)

from kiro_crew import platform_compat
from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS

# pytest resolves a fixture in the requesting test's module, so each shared
# fixture this module uses is bound here by name.
_no_inherited_git_c = push_guard_helpers._no_inherited_git_c
_repo_pair_template = push_guard_helpers._repo_pair_template
repo_pair = push_guard_helpers.repo_pair


def _built_dir(clone_dir: str) -> Path:
    return Path(clone_dir, _git(clone_dir, "rev-parse", "--git-common-dir"), _PG._BUILT_DIR)


# The identity a guard run commits as (conftest's ``_git_identity``). Fixture
# commits are another author's (``_fixture_git_env``) unless they name this.
_GUARD_IDENT = "Test <test@example.com>"


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
        owners = sorted(path.name for path in PUSH_GUARD_DIR.glob("push_guard_*.py"))
        for script in ("push_guard.py", "preflight.py", *owners):
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
