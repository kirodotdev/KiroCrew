"""push_guard.py builds the commit: ``--squash``, ``--commit``/``--amend``, the squash message.

``--squash`` commits the branch's tree with ``commit-tree``, through no index;
``--commit`` and ``--amend`` commit exactly the named paths, whatever else is
staged; and the squash message goes through the hooks and the cleanup that
``git commit -F`` applies.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path

import push_guard_helpers
import pytest
from git_maintenance_helpers import git_maintenance_off_entries
from installer_test_helpers import run_bounded
from push_guard_helpers import (
    _INPROCESS_TIMEOUT_S,
    _PG,
    _SUBPROCESS_TIMEOUT_S,
    PUSH_GUARD,
    _commit_tree,
    _feature,
    _fixture_git_env,
    _git,
    _inprocess,
    _install_hooks,
    _land_on_main,
    _load_push_guard,
    _message_path,
    _overlay,
    _rev,
    _run_push_guard,
    _spy_popen,
    _write_message,
)

from kiro_crew import platform_compat
from kiro_crew.testing.wait import wait_until

# pytest resolves a fixture in the requesting test's module, so each shared
# fixture this module uses is bound here by name.
_no_inherited_git_c = push_guard_helpers._no_inherited_git_c
_repo_pair_template = push_guard_helpers._repo_pair_template
repo_pair = push_guard_helpers.repo_pair


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
        hooks_path = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": str(hooks),
        }
        # The count replaces the suite's entries, so the maintenance ones go after it.
        hooks_path.update(git_maintenance_off_entries(hooks_path))
        rc, _, stderr = _run_push_guard(clone_dir, ["--squash"], env_extra=hooks_path)
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
        # The hook execs one Python process that writes the mark and then waits.
        # Nothing is forked after the mark appears, so the group signal cannot
        # race a fork: macOS can miss a child forked while killpg runs, and a
        # `sleep` that misses it holds the stderr pipe past the wait below.
        waiter = (
            "import os, signal, sys, time; "
            # Always install SIG_DFL for SIGINT unconditionally: git sets SIGINT to
            # SIG_IGN before running hooks so that git itself is not interrupted while
            # waiting for them.  The hook inherits that SIG_IGN, and Python keeps an
            # inherited SIG_IGN rather than overriding it with default_int_handler.
            # A conditional getsignal check therefore never restores SIG_DFL, leaving
            # SIGINT ignored; the hook survives the killpg and holds the stderr pipe
            # open past the communicate() timeout.  Installing SIG_DFL here is always
            # correct for a test hook: the process should die on SIGINT so the group
            # signal terminates every member deterministically.
            "signal.signal(signal.SIGINT, signal.SIG_DFL); "
            "open(sys.argv[1] + '.tmp', 'w').close(); "
            "os.replace(sys.argv[1] + '.tmp', sys.argv[1]); "
            # flake-ok: the hook's own 60 s wait keeps it running until the test interrupts it
            "time.sleep(60)"
        )
        hook.write_text(
            "#!/bin/sh\nexec "
            + " ".join(shlex.quote(a) for a in (sys.executable, "-c", waiter, str(mark)))
            + "\n"
        )
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
            wait_until(
                lambda: mark.exists() or proc.poll() is not None,
                describe=lambda: f"hook mark {mark} absent, hook exit {proc.poll()}",
            )
            assert mark.exists(), "the hook never started"
            # SIGINT to the group is what a terminal's Ctrl-C sends.
            os.killpg(proc.pid, getattr(signal, sig))
            _, err = proc.communicate(timeout=_SUBPROCESS_TIMEOUT_S)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                # flake-ok: reaps the guard this finally block just SIGKILLed; moved verbatim
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
        public_key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5" "AAAAIFakeKeyForTests\n")
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
                # flake-ok: reaps the guard this finally block just SIGKILLed; moved verbatim
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
