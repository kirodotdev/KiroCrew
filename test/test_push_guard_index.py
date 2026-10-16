"""push_guard.py and the index: ``--check-index``, the private index, git failures.

``--check-index`` reads what is staged in this checkout, read-only; a git call
that fails or hangs refuses by name, never with a guessed cause; ``--commit`` and
``--amend`` build in a private index and change the shared one after; a refusal
lists every path it saw and offers its remedy; and state in the middle of a
rebase, merge, cherry-pick or revert is handled. ``TestPushGuardOperations`` calls
``TestPushGuardIndex._conflicted_rebase`` by class name, so the two share this module.
"""

from __future__ import annotations

import io
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import push_guard_helpers
import pytest
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
    _overlay,
    _rev,
    _run_from,
    _run_push_guard,
    _spy_popen,
)

from kiro_crew import platform_compat

# pytest resolves a fixture in the requesting test's module, so each shared
# fixture this module uses is bound here by name.
_no_inherited_git_c = push_guard_helpers._no_inherited_git_c
_repo_pair_template = push_guard_helpers._repo_pair_template
repo_pair = push_guard_helpers.repo_pair


def _hang_on(monkeypatch, match):
    """Make each guard call whose argv satisfies ``match`` time out at its own bound."""

    def hang(argv, timeout):
        if timeout == _INPROCESS_TIMEOUT_S and match(argv):
            raise subprocess.TimeoutExpired(argv, timeout)

    _spy_popen(monkeypatch, on_wait=hang)


def _staged_list(stderr: str) -> list[bytes]:
    """The entries of the list file a refusal printed (one file per refusal)."""
    path = next(line.split(": ", 1)[1] for line in stderr.splitlines() if "Full list" in line)
    return [p for p in Path(path).read_bytes().split(b"\0") if p]


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
            # flake-ok: the fake git's silent hang is the stall under test, cut at FETCH_STALL_S
            "    time.sleep({hang})\n"
            # flake-ok: the fake git's own clock times its progress chatter; no test asserts on it
            "end = time.monotonic() + {chatter}\n"
            "proc = subprocess.Popen([{git!r}] + args)\n"
            # Progress until the chatter is over AND the real fetch is done, so
            # no silence window is ever spent on spawning or fetching.
            # flake-ok: the fake git chatters until its own clock passes the end; moved verbatim
            "while fetch and (proc.poll() is None or time.monotonic() < end):\n"
            "    sys.stderr.write('Receiving objects: x\\r'); sys.stderr.flush()\n"
            # flake-ok: the fake git's 0.2 s progress cadence is its output, not a test barrier
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
        # flake-ok: real-clock start of a loose ceiling, half the 30 s bound, on a 1 s stall
        start = time.monotonic()
        rc, _, stderr = _inprocess(
            monkeypatch,
            clone_dir,
            self._slow_fetch_git(tmp_path, 0),
            overrides={"FETCH_STALL_S": 1},
        )
        assert rc == 40 and "made no progress for 1 s" in stderr, stderr
        assert "git fetch origin main by hand" in stderr, stderr
        # flake-ok: loose real-time ceiling, half the 30 s bound, on a stall the guard cuts at 1 s
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
            # flake-ok: the hook's background 60 s sleeper is the lingering child under test; the test kills it
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
