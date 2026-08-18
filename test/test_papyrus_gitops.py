"""Tests for Papyrus's git surface (``gitops.py``).

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer`` — a test under
``src/kiro_crew/apps/builtins/...`` is never collected by CI.

Every ``git`` invocation is mocked at the ``_git`` chokepoint, so no repository is
created and no network is touched.

Coverage targets:

  * the clone-URL scheme allowlist, and that the URL is passed after ``--`` so an
    option-shaped value cannot be smuggled into argv;
  * the pull autostash flow, including the case that matters most — when the stash
    pop conflicts, the stash is KEPT rather than the user's work being discarded to
    let the operation "succeed";
  * push authentication detection, which is what lets the UI say "log in" instead
    of "something broke";
  * that a spawn routes through the sandbox chokepoint with a resource ceiling, and
    that a timeout kills the process tree.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew import sandbox
from kiro_crew.apps.builtins.papyrus.backend import gitops


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A directory that looks like a git repo to ``is_git_repo``."""
    project = tmp_path / "paper"
    (project / ".git").mkdir(parents=True)
    return project


@pytest.fixture()
def plain(tmp_path: Path) -> Path:
    """A project directory that is NOT a git repo."""
    project = tmp_path / "plain"
    project.mkdir()
    return project


class _GitScript:
    """Replays a scripted sequence of git results, keyed by the first argument."""

    def __init__(self, results: dict[str, tuple[int, str, str]] | None = None) -> None:
        self.results = results or {}
        self.calls: list[list[str]] = []

    async def __call__(self, args, *, cwd, timeout=None):  # noqa: ANN001
        self.calls.append(list(args))
        code, out, err = self.results.get(args[0], (0, "", ""))
        # A real successful `clone` CREATES its destination, and `clone` now targets a
        # staging sibling it renames into place — so the fake has to create it too or
        # the rename fails and every clone test becomes a false failure.
        if args[0] == "clone" and code == 0:
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
        return code, out, err

    @property
    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls]

    def argv_for(self, verb: str) -> list[str]:
        return next(c for c in self.calls if c[0] == verb)


#: Every transport the URL gate accepts. Shared by the accept test and the
#: trailing-newline regression test so a new transport is automatically covered
#: by both.
_ACCEPTED_URLS = (
    "https://example.com/group/paper.git",
    "http://example.com/paper",
    "ssh://git@example.com/paper.git",
    "git://example.com/paper.git",
    "git@example.com:group/paper.git",
)


class TestUrlValidation:
    @pytest.mark.parametrize("url", _ACCEPTED_URLS)
    def test_accepts_known_transports(self, url: str) -> None:
        assert gitops.GIT_URL_RE.match(url)

    @pytest.mark.parametrize("url", _ACCEPTED_URLS)
    def test_rejects_trailing_newline_on_accepted_url(self, url: str) -> None:
        """Python's ``$`` matches before a trailing newline; the ``\\Z`` anchor
        must not, or ``.match`` on a client URL hands the newline to git argv."""
        assert gitops.GIT_URL_RE.match(url + "\n") is None

    @pytest.mark.parametrize(
        "url",
        [
            "--upload-pack=/bin/sh",   # argument smuggling
            "-oProxyCommand=x",        # ssh option smuggling
            "file:///etc",             # local-path transport
            "ext::sh -c whoami",       # git's ext:: transport runs a command
            "",
            "not a url",
            "https://example.com/a b",  # whitespace
        ],
    )
    def test_rejects_everything_else(self, url: str) -> None:
        assert gitops.GIT_URL_RE.match(url) is None

    def test_derive_project_name_strips_dot_git_and_lowercases(self) -> None:
        assert gitops.derive_project_name("https://example.com/Group/My-Paper.git") == "my-paper"
        assert gitops.derive_project_name("https://example.com/group/paper/") == "paper"


@pytest.mark.asyncio
class TestClone:
    async def test_refuses_an_unrecognized_url_without_spawning(self, tmp_path: Path) -> None:
        script = _GitScript()
        with mock.patch.object(gitops, "_git", script):
            with pytest.raises(gitops.GitError):
                await gitops.clone("--upload-pack=/bin/sh", tmp_path / "dest")
        assert script.calls == []

    async def test_passes_the_url_after_a_double_dash(self, tmp_path: Path) -> None:
        """So a URL that begins with a dash can never be read as an option."""
        script = _GitScript()
        with mock.patch.object(gitops, "_git", script):
            await gitops.clone("https://example.com/g/p.git", tmp_path / "dest")
        argv = script.argv_for("clone")
        assert argv[argv.index("--") + 1] == "https://example.com/g/p.git"

    async def test_clones_shallow(self, tmp_path: Path) -> None:
        script = _GitScript()
        with mock.patch.object(gitops, "_git", script):
            await gitops.clone("https://example.com/g/p.git", tmp_path / "dest")
        assert "--depth" in script.argv_for("clone")

    async def test_removes_only_its_own_staging_dir_on_failure(self, tmp_path: Path) -> None:
        """A leftover directory would block the retry with "already exists"."""
        dest = tmp_path / "dest"
        script = _GitScript({"clone": (128, "", "fatal: repository not found")})
        with mock.patch.object(gitops, "_git", script):
            with pytest.raises(gitops.GitError) as excinfo:
                await gitops.clone("https://example.com/g/p.git", dest)
        assert "not found" in excinfo.value.output
        assert not dest.exists()
        # And nothing is left behind in the parent either.
        assert list(tmp_path.iterdir()) == []

    async def test_a_failed_clone_never_deletes_an_existing_project(
        self, tmp_path: Path
    ) -> None:
        """The destructive race, and the reason for the staging dir.

        Two concurrent clones of the same project name both proceed; the loser gets
        git's "destination path already exists" error, and its cleanup used to delete
        the WINNER's freshly-cloned checkout — turning a duplicate-request 500 into
        data loss for the request that succeeded.
        """
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "main.tex").write_text("the winner's work", encoding="utf-8")

        script = _GitScript({"clone": (128, "", "fatal: destination path already exists")})
        with mock.patch.object(gitops, "_git", script):
            with pytest.raises(gitops.GitError):
                await gitops.clone("https://example.com/g/p.git", dest)

        assert (dest / "main.tex").read_text(encoding="utf-8") == "the winner's work"

    async def test_a_clone_that_wins_the_race_lands_at_the_destination(
        self, tmp_path: Path
    ) -> None:
        dest = tmp_path / "dest"
        script = _GitScript()
        with mock.patch.object(gitops, "_git", script):
            await gitops.clone("https://example.com/g/p.git", dest)
        assert dest.is_dir()

    async def test_a_clone_that_loses_the_rename_reports_a_conflict(
        self, tmp_path: Path
    ) -> None:
        """`os.rename` refuses to clobber a non-empty directory, which is what makes
        the loser fail rather than overwrite."""
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "existing.tex").write_text("mine", encoding="utf-8")
        script = _GitScript()
        with mock.patch.object(gitops, "_git", script):
            with pytest.raises(gitops.GitError) as excinfo:
                await gitops.clone("https://example.com/g/p.git", dest)
        assert "already exists" in str(excinfo.value)
        assert (dest / "existing.tex").read_text(encoding="utf-8") == "mine"


@pytest.mark.asyncio
class TestStatus:
    async def test_reports_not_a_repo(self, plain: Path) -> None:
        result = await gitops.status(plain)
        assert result.is_git is False
        assert result.to_dict() == {"is_git": False}

    async def test_collects_branch_dirtiness_and_remote(self, repo: Path) -> None:
        script = _GitScript({
            "status": (0, " M main.tex\n?? new.tex\n", ""),
            "branch": (0, "main\n", ""),
            "log": (0, "abc123 first\n", ""),
            "remote": (0, "origin\n", ""),
            "rev-list": (0, "2\t1\n", ""),
        })
        with mock.patch.object(gitops, "_git", script):
            result = await gitops.status(repo)
        assert result.is_git is True
        assert result.branch == "main"
        assert result.dirty is True
        assert result.has_remote is True
        assert (result.ahead, result.behind) == (2, 1)
        assert len(result.changes) == 2
        assert result.recent_commits == ["abc123 first"]

    async def test_clean_tree_is_not_dirty(self, repo: Path) -> None:
        script = _GitScript({"status": (0, "\n", ""), "branch": (0, "main\n", "")})
        with mock.patch.object(gitops, "_git", script):
            result = await gitops.status(repo)
        assert result.dirty is False

    async def test_no_upstream_leaves_the_counts_at_zero(self, repo: Path) -> None:
        """A branch with no upstream makes `rev-list @{upstream}` fail — not an error."""
        script = _GitScript({"rev-list": (128, "", "fatal: no upstream")})
        with mock.patch.object(gitops, "_git", script):
            result = await gitops.status(repo)
        assert (result.ahead, result.behind) == (0, 0)

    async def test_malformed_counts_are_ignored(self, repo: Path) -> None:
        script = _GitScript({"rev-list": (0, "garbage\n", "")})
        with mock.patch.object(gitops, "_git", script):
            result = await gitops.status(repo)
        assert (result.ahead, result.behind) == (0, 0)

    async def test_changes_are_bounded(self, repo: Path) -> None:
        many = "\n".join(f" M f{i}.tex" for i in range(500))
        script = _GitScript({"status": (0, many, "")})
        with mock.patch.object(gitops, "_git", script):
            result = await gitops.status(repo)
        assert len(result.changes) == 200


@pytest.mark.asyncio
class TestCommit:
    async def test_refuses_a_non_repo(self, plain: Path) -> None:
        with pytest.raises(gitops.GitError):
            await gitops.commit(plain, "msg")

    async def test_stages_everything_then_commits(self, repo: Path) -> None:
        script = _GitScript({"commit": (0, "[main abc] msg\n", "")})
        with mock.patch.object(gitops, "_git", script):
            output = await gitops.commit(repo, "msg")
        assert script.verbs == ["add", "commit"]
        assert "abc" in output

    async def test_nothing_to_commit_is_a_success(self, repo: Path) -> None:
        """Pressing Push with no edits is not an error the user should see."""
        script = _GitScript({"commit": (1, "nothing to commit, working tree clean\n", "")})
        with mock.patch.object(gitops, "_git", script):
            assert await gitops.commit(repo, "msg") == "nothing to commit"

    async def test_a_real_failure_raises(self, repo: Path) -> None:
        script = _GitScript({"commit": (1, "", "error: gpg failed to sign the data\n")})
        with mock.patch.object(gitops, "_git", script):
            with pytest.raises(gitops.GitError) as excinfo:
                await gitops.commit(repo, "msg")
        assert "gpg" in excinfo.value.output

    async def test_uses_the_default_message_when_none_is_given(self, repo: Path) -> None:
        script = _GitScript()
        with mock.patch.object(gitops, "_git", script):
            await gitops.commit(repo, "")
        assert script.argv_for("commit")[-1] == gitops.DEFAULT_COMMIT_MESSAGE


@pytest.mark.asyncio
class TestPush:
    async def test_refuses_a_non_repo(self, plain: Path) -> None:
        with pytest.raises(gitops.GitError):
            await gitops.push(plain)

    async def test_success_returns_the_output(self, repo: Path) -> None:
        script = _GitScript({"push": (0, "", "To example.com\n   abc..def  main -> main\n")})
        with mock.patch.object(gitops, "_git", script):
            assert "main -> main" in await gitops.push(repo)

    @pytest.mark.parametrize(
        "message",
        [
            "fatal: Authentication failed for 'https://example.com/g/p.git'",
            "fatal: could not read Username for 'https://example.com'",
            "git@example.com: Permission denied (publickey).",
            "remote: 403 Forbidden",
            "fatal: could not read Username: terminal prompts disabled",
        ],
    )
    async def test_detects_an_auth_failure_across_transports(self, repo: Path, message: str) -> None:
        """The wording varies by remote type, so the UI needs the classification."""
        script = _GitScript({"push": (128, "", message)})
        with mock.patch.object(gitops, "_git", script):
            with pytest.raises(gitops.GitError) as excinfo:
                await gitops.push(repo)
        assert excinfo.value.auth is True

    async def test_a_non_auth_failure_is_not_flagged_as_auth(self, repo: Path) -> None:
        script = _GitScript({"push": (1, "", "! [rejected] main -> main (non-fast-forward)\n")})
        with mock.patch.object(gitops, "_git", script):
            with pytest.raises(gitops.GitError) as excinfo:
                await gitops.push(repo)
        assert excinfo.value.auth is False


@pytest.mark.asyncio
class TestPull:
    async def test_refuses_a_non_repo(self, plain: Path) -> None:
        with pytest.raises(gitops.GitError):
            await gitops.pull(plain)

    async def test_clean_tree_pulls_without_stashing(self, repo: Path) -> None:
        script = _GitScript({"status": (0, "", ""), "pull": (0, "Already up to date.\n", "")})
        with mock.patch.object(gitops, "_git", script):
            output, stashed = await gitops.pull(repo)
        assert stashed is False
        assert "stash" not in script.verbs
        assert "up to date" in output

    async def test_dirty_tree_is_autostashed_and_popped(self, repo: Path) -> None:
        """Compiler artifacts not in .gitignore would otherwise refuse the rebase."""
        script = _GitScript({
            "status": (0, " M main.tex\n", ""),
            "stash": (0, "Saved working directory\n", ""),
            "pull": (0, "Fast-forward\n", ""),
        })
        with mock.patch.object(gitops, "_git", script):
            _output, stashed = await gitops.pull(repo)
        assert stashed is True
        assert script.verbs.count("stash") == 2  # push then pop

    async def test_a_conflict_aborts_the_rebase_and_restores_the_stash(self, repo: Path) -> None:
        """The tree must come back exactly as it was before the pull."""
        script = _GitScript({
            "status": (0, " M main.tex\n", ""),
            "stash": (0, "Saved working directory\n", ""),
            "pull": (1, "CONFLICT (content): Merge conflict in main.tex\n", ""),
        })
        with mock.patch.object(gitops, "_git", script):
            with pytest.raises(gitops.GitConflict):
                await gitops.pull(repo)
        assert "rebase" in script.verbs
        assert script.argv_for("rebase")[1] == "--abort"
        assert script.verbs.count("stash") == 2

    async def test_a_failed_pop_keeps_the_stash(self, repo: Path) -> None:
        """Discarding the user's edits to make the operation "succeed" is the worse
        outcome, so the stash is deliberately LEFT and the conflict is reported."""
        calls: list[list[str]] = []

        async def scripted(args, *, cwd, timeout=None):  # noqa: ANN001
            calls.append(list(args))
            if args[0] == "status":
                return 0, " M main.tex\n", ""
            if args == ["stash", "pop"]:
                return 1, "", "CONFLICT (content): Merge conflict in main.tex\n"
            if args[0] == "stash":
                return 0, "Saved working directory\n", ""
            return 0, "Fast-forward\n", ""

        with mock.patch.object(gitops, "_git", scripted):
            with pytest.raises(gitops.GitConflict) as excinfo:
                await gitops.pull(repo)
        assert "stash was kept" in str(excinfo.value)
        # Exactly one pop attempt — no second, destructive recovery.
        assert calls.count(["stash", "pop"]) == 1

    async def test_a_non_conflict_failure_restores_the_stash_and_raises(self, repo: Path) -> None:
        script = _GitScript({
            "status": (0, " M main.tex\n", ""),
            "stash": (0, "Saved working directory\n", ""),
            "pull": (1, "", "fatal: unable to access remote\n"),
        })
        with mock.patch.object(gitops, "_git", script):
            with pytest.raises(gitops.GitError) as excinfo:
                await gitops.pull(repo)
        assert not isinstance(excinfo.value, gitops.GitConflict)
        assert script.verbs.count("stash") == 2

    async def test_pull_rebases(self, repo: Path) -> None:
        script = _GitScript({"status": (0, "", "")})
        with mock.patch.object(gitops, "_git", script):
            await gitops.pull(repo)
        assert script.argv_for("pull") == ["pull", "--rebase"]

    async def test_a_raising_pull_still_restores_the_stash(self, repo: Path) -> None:
        """A pull that never returns an exit code must not strand the autostash.

        `_git` raises rather than returning on a network timeout or a missing git
        binary, so the `code != 0` branches never see it. Left unhandled, the user
        gets an apparently-clean tree with their work parked in a stash nobody told
        them about — indistinguishable from "my edits vanished".
        """
        calls: list[list[str]] = []

        async def scripted(args, *, cwd, timeout=None):  # noqa: ANN001
            calls.append(list(args))
            if args[0] == "status":
                return 0, " M main.tex\n", ""
            if args[0] == "pull":
                raise gitops.GitError("git pull timed out")
            return 0, "Saved working directory\n", ""

        with mock.patch.object(gitops, "_git", scripted):
            with pytest.raises(gitops.GitError) as excinfo:
                await gitops.pull(repo)
        # The original cause survives — the recovery must not mask it.
        assert "timed out" in str(excinfo.value)
        assert calls.count(["stash", "pop"]) == 1

    async def test_a_raising_pull_on_a_clean_tree_pops_nothing(self, repo: Path) -> None:
        """Nothing was stashed, so there is nothing to restore."""
        calls: list[list[str]] = []

        async def scripted(args, *, cwd, timeout=None):  # noqa: ANN001
            calls.append(list(args))
            if args[0] == "status":
                return 0, "", ""
            raise gitops.GitError("git pull timed out")

        with mock.patch.object(gitops, "_git", scripted):
            with pytest.raises(gitops.GitError):
                await gitops.pull(repo)
        assert "stash" not in [c[0] for c in calls]

    async def test_a_failed_recovery_pop_does_not_mask_the_pull_error(
        self, repo: Path
    ) -> None:
        """If the restoring pop ALSO fails, the user still sees why the pull died."""
        async def scripted(args, *, cwd, timeout=None):  # noqa: ANN001
            if args[0] == "status":
                return 0, " M main.tex\n", ""
            if args == ["stash", "pop"]:
                raise gitops.GitError("stash pop timed out")
            if args[0] == "pull":
                raise gitops.GitError("git pull timed out")
            return 0, "Saved working directory\n", ""

        with mock.patch.object(gitops, "_git", scripted):
            with pytest.raises(gitops.GitError) as excinfo:
                await gitops.pull(repo)
        assert "pull timed out" in str(excinfo.value)


@pytest.mark.asyncio
class TestGitSpawn:
    async def test_routes_through_the_sandbox_chokepoint(self, repo: Path) -> None:
        """A repository's own hooks and config can execute code."""
        proc = mock.AsyncMock()
        proc.communicate = mock.AsyncMock(return_value=(b"out", b""))
        proc.returncode = 0
        with mock.patch.object(
            gitops, "sandboxed_spawn_argv", return_value=(["/bin/true"], {}, None)
        ) as wrap, mock.patch(
            "asyncio.create_subprocess_exec", mock.AsyncMock(return_value=proc)
        ):
            code, out, _err = await gitops._git(["status"], cwd=repo)
        assert wrap.called
        assert (code, out) == (0, "out")
        # STANDARD here, unlike the LaTeX compiler's strict mode (see
        # `test_hides_credential_dirs_from_the_compiler`). Pushing over SSH is the
        # one place the key material is the point, so hiding `~/.ssh` would break
        # the feature rather than harden it. Pinned so the two modes cannot be
        # "made consistent" in the wrong direction.
        assert wrap.call_args.kwargs.get("mode", "standard") == "standard"

    async def test_applies_a_resource_ceiling(self, repo: Path) -> None:
        """Via ``create_subprocess_limited`` (limits applied post-exec).

        A post-fork ``preexec_fn`` would fork the threaded gateway and run
        Python in the child before exec — see ``test_spawn_preexec_guard``.
        """
        proc = mock.AsyncMock()
        proc.communicate = mock.AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        spawn = mock.AsyncMock(return_value=proc)
        with mock.patch.object(
            gitops, "sandboxed_spawn_argv", return_value=(["/bin/true"], {}, None)
        ), mock.patch.object(gitops, "create_subprocess_limited", spawn):
            await gitops._git(["status"], cwd=repo)
        assert spawn.await_args is not None
        assert "preexec_fn" not in spawn.await_args.kwargs

    async def test_disables_the_interactive_credential_prompt(self, repo: Path) -> None:
        """The gateway has no terminal, so a prompt would hang until the timeout."""
        captured: dict[str, str] = {}
        proc = mock.AsyncMock()
        proc.communicate = mock.AsyncMock(return_value=(b"", b""))
        proc.returncode = 0

        async def spawn(*_args, **kwargs):  # noqa: ANN001
            captured.update(kwargs["env"])
            return proc

        with mock.patch.object(
            gitops, "sandboxed_spawn_argv", return_value=(["/bin/true"], {}, None)
        ), mock.patch("asyncio.create_subprocess_exec", spawn):
            await gitops._git(["push"], cwd=repo)
        assert captured["GIT_TERMINAL_PROMPT"] == "0"

    async def test_a_timeout_kills_the_process_tree_and_raises(self, repo: Path) -> None:
        proc = mock.AsyncMock()
        proc.communicate = mock.AsyncMock(side_effect=asyncio.TimeoutError)
        proc.wait = mock.AsyncMock(return_value=0)
        proc.returncode = None
        proc.pid = 9876
        with mock.patch.object(
            gitops, "sandboxed_spawn_argv", return_value=(["/bin/true"], {}, None)
        ), mock.patch(
            "asyncio.create_subprocess_exec", mock.AsyncMock(return_value=proc)
        ), mock.patch.object(
            gitops.platform_compat, "kill_process_tree_async", mock.AsyncMock(return_value=True)
        ) as kill:
            with pytest.raises(gitops.GitError):
                await gitops._git(["push"], cwd=repo, timeout=0.01)
        assert kill.await_args is not None
        assert kill.await_args.args[0] == 9876

    async def test_a_missing_git_binary_is_a_clear_error(self, repo: Path) -> None:
        with mock.patch.object(
            gitops, "sandboxed_spawn_argv", return_value=(["/bin/true"], {}, None)
        ), mock.patch(
            "asyncio.create_subprocess_exec", mock.AsyncMock(side_effect=FileNotFoundError)
        ):
            with pytest.raises(gitops.GitError) as excinfo:
                await gitops._git(["status"], cwd=repo)
        assert "not installed" in str(excinfo.value)


class TestErrorShape:
    def test_output_is_bounded(self) -> None:
        error = gitops.GitError("boom", output="x" * 99999)
        assert len(error.output) == gitops.MAX_OUTPUT_CHARS

    def test_conflict_is_a_git_error(self) -> None:
        """So a caller that only catches GitError still handles a conflict."""
        assert issubclass(gitops.GitConflict, gitops.GitError)


@pytest.mark.asyncio
class TestGitOutputIsBounded:
    """`git` relays sideband progress from a REMOTE, so a hostile server decides how
    much arrives — and with a 120s timeout that is a long window to write to memory at
    pipe speed, inside the gateway's own process.

    `MAX_OUTPUT_CHARS` bounds what is DISPLAYED, not what is held. Same shape as the
    compiler path in `latex`, and the same shared helper now fixes both.
    """

    async def test_run_drains_through_the_capped_reader(self) -> None:
        """AST: the unit tests for `read_capped` live beside `latex`, so this pins the
        WIRING — a bare `communicate()` here would pass every one of them."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(gitops))
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]:
            body = ast.dump(fn)
            if "attr='communicate'" in body:
                assert "read_capped" in body, (
                    f"{fn.name} calls communicate() directly — that is the unbounded read"
                )

    async def test_the_helper_is_shared_rather_than_duplicated(self) -> None:
        """One implementation, so a fix to the drain logic cannot land in one caller
        and miss the other — which is exactly how this bug survived in `gitops` after
        being fixed in `latex`."""
        from kiro_crew.apps.builtins.papyrus.backend import latex, procio

        assert gitops.procio.read_capped is procio.read_capped
        assert latex.MAX_CAPTURED_OUTPUT_BYTES == procio.MAX_CAPTURED_OUTPUT_BYTES


@pytest.mark.asyncio
class TestRepoConfigCannotExecuteCommands:
    """A cloned repository — or the co-author agent, which can write into the project —
    controls `.git/config`, and several git settings are COMMANDS git executes:
    `core.sshCommand`, `core.pager`, `core.editor`, `credential.helper`,
    `diff.external`, and the `ext::` transport.

    Push runs in `standard` sandbox mode on purpose (an SSH push needs the key), so such
    a command would run WITH access to `~/.ssh` — arbitrary execution plus the very
    credential the mode exists to permit. Verified against real git: with
    `core.sshCommand` set in a repo, an unoverridden `ls-remote` executes it; with the
    override, real `ssh` runs instead.
    """

    async def test_executable_config_keys_are_pinned_inert(self, repo: Path) -> None:
        script = _GitScript()
        with mock.patch.object(gitops, "_git", script):
            pass
        # Inspect the argv the real `_git` builds, not the fake's.
        import inspect

        source = inspect.getsource(gitops._git)
        for key in (
            "core.sshCommand=ssh",
            "core.pager=cat",
            "core.editor=true",
            "core.hooksPath=/dev/null",
            "credential.helper=",
            "diff.external=",
        ):
            assert key in source, f"{key} is not overridden — repo config can run a command"

    async def test_the_ext_transport_is_refused(self, repo: Path) -> None:
        """`ext::sh -c …` is a shell escape by design, so the transport allow-list has to
        exclude it while keeping the ones a user legitimately pushes over."""
        import inspect

        source = inspect.getsource(gitops._git)
        assert "protocol.ext.allow=never" in source
        assert "protocol.allow=user" in source

    async def test_the_overrides_precede_the_subcommand(self) -> None:
        """`-c` is only accepted BEFORE the subcommand, and only there does it beat
        `.git/config`. An override placed after would be silently inert."""
        captured: list[list[str]] = []

        async def _fake_exec(*wrapped, **kwargs):  # noqa: ANN001, ANN202
            captured.append(list(wrapped))
            raise OSError("stop here")

        with mock.patch.object(
            gitops, "sandboxed_spawn_argv", side_effect=lambda argv, *a, **k: (argv, {}, None)
        ), mock.patch.object(gitops, "create_subprocess_limited", _fake_exec):
            with pytest.raises(OSError):
                await gitops._git(["status", "--porcelain"], cwd=Path("/tmp"))

        argv = captured[0]
        assert argv[0] == "git"
        # Every `-c` sits before the subcommand.
        assert argv.index("status") > max(i for i, a in enumerate(argv) if a == "-c")


@pytest.mark.asyncio
class TestCommitChecksTheStage:
    """A failed `git add` must not go on to commit.

    The result was discarded, so if the index already held content — a previous partial
    stage, or a concurrent operation — that STALE index was committed and pushed while
    the response reported success. The user's edits were not in the commit and nothing
    said so; the next pull would then present their own missing work as a remote change.
    """

    async def test_a_failed_add_aborts_the_commit(self, repo: Path) -> None:
        script = _GitScript({"add": (128, "", "fatal: unable to read files")})
        with mock.patch.object(gitops, "_git", script):
            with pytest.raises(gitops.GitError) as excinfo:
                await gitops.commit(repo, "msg")
        assert "add" in str(excinfo.value)
        # And the commit was never attempted.
        assert "commit" not in script.verbs

    async def test_a_successful_add_still_commits(self, repo: Path) -> None:
        script = _GitScript({"commit": (0, "abc123 done", "")})
        with mock.patch.object(gitops, "_git", script):
            assert await gitops.commit(repo, "msg") == "abc123 done"
        assert "add" in script.verbs and "commit" in script.verbs


@pytest.mark.asyncio
class TestFsmonitorAndOtherHooksAreNeutralized:
    """`core.fsmonitor` holds the PATHNAME OF A HOOK that `git status`/`add` run on every
    invocation — the same class as `core.sshCommand`, and the one the first version of
    this list missed.

    Demonstrated against real git: with `core.fsmonitor=$PWD/mon.sh` set in a repo, a
    plain `git status` executes it; with `-c core.fsmonitor=false` it does not.
    """

    async def test_fsmonitor_is_disabled(self) -> None:
        import inspect

        source = inspect.getsource(gitops._git)
        # `false` is the documented "no monitor" value. An EMPTY string would be read as
        # a path, which is why this one entry differs in shape from `diff.external=`.
        assert "core.fsmonitor=false" in source

    async def test_every_hook_reachable_from_our_commands_is_pinned(self) -> None:
        """The list is deliberately broader than the keys this app's own argv touches.

        There is no "ignore repo config" switch — `GIT_CONFIG_GLOBAL`/`SYSTEM` suppress
        the other two scopes but a repo's own config is always read (verified) — so this
        is necessarily an enumeration. It therefore covers every execution hook reachable
        from `status`/`add`/`commit`/`pull`/`push`/`ls-remote`, which is this module's
        whole surface.
        """
        import inspect

        source = inspect.getsource(gitops._git)
        for key in (
            "core.sshCommand=ssh",
            "core.fsmonitor=false",
            "core.hooksPath=/dev/null",
            "core.alternateRefsCommand=",
            "gc.recentObjectsHook=",
            "credential.helper=",
            "core.askPass=",
            "gpg.program=false",
            "core.pager=cat",
            "core.editor=true",
            "sequence.editor=true",
            "diff.external=",
            "interactive.diffFilter=",
            "protocol.ext.allow=never",
            "protocol.allow=user",
            "remote.origin.uploadpack=git-upload-pack",
            "remote.origin.receivepack=git-receive-pack",
        ):
            assert key in source, f"{key} is not pinned — repo config can still run it"


@pytest.mark.asyncio
class TestPackProgramsArePinnedForEveryRemote:
    """`remote.<name>.uploadpack` / `.receivepack` name a COMMAND, and the subsection is
    ATTACKER-CHOSEN — the same defect as `filter.<name>.clean`.

    Pinning `remote.origin.*` with `-c` covers only the remote this app creates. An
    agent-written second remote, selected via `remote.pushDefault` (push) or
    `branch.<b>.remote` (pull), executed its own program straight past those pins.
    Verified against real git in BOTH directions below.
    """

    @pytest.mark.parametrize(
        ("subcommand", "flag"),
        [
            ("push", "--receive-pack=git-receive-pack"),
            ("pull", "--upload-pack=git-upload-pack"),
            ("fetch", "--upload-pack=git-upload-pack"),
            ("ls-remote", "--upload-pack=git-upload-pack"),
            ("clone", "--upload-pack=git-upload-pack"),
        ],
    )
    def test_every_remote_speaking_subcommand_is_pinned(
        self, subcommand: str, flag: str
    ) -> None:
        out = gitops._pack_program_args([subcommand, "extra"])
        assert out[0] == subcommand, "the subcommand must stay first"
        assert out[1] == flag
        # The subcommand's own arguments keep their order — `clone` takes positionals.
        assert out[2:] == ["extra"]

    @pytest.mark.parametrize("subcommand", ["status", "add", "commit", "stash", "branch"])
    def test_local_subcommands_are_left_alone(self, subcommand: str) -> None:
        """They REJECT the flag as an unknown option, so adding it unconditionally would
        turn every local call into an error."""
        assert gitops._pack_program_args([subcommand, "-x"]) == [subcommand, "-x"]

    async def test_the_pin_reaches_the_built_argv(self) -> None:
        captured: list[list[str]] = []

        async def _fake_exec(*wrapped, **kwargs):  # noqa: ANN001, ANN202
            captured.append(list(wrapped))
            raise OSError("stop here")

        with mock.patch.object(
            gitops, "_pin_attributes_sync", lambda cwd: None
        ), mock.patch.object(
            gitops, "sandboxed_spawn_argv", side_effect=lambda argv, *a, **k: (argv, {}, None)
        ), mock.patch.object(gitops, "create_subprocess_limited", _fake_exec):
            with pytest.raises(OSError):
                await gitops._git(["push"], cwd=Path("/tmp"))

        argv = captured[0]
        assert "--receive-pack=git-receive-pack" in argv
        # Directly after the subcommand, which is where a subcommand's own flag belongs.
        assert argv[argv.index("push") + 1] == "--receive-pack=git-receive-pack"

    async def test_against_real_git_a_selected_remote_cannot_run_its_receivepack(
        self, tmp_path: Path
    ) -> None:
        """The push direction, demonstrated. Without the flag this body writes the
        marker even WITH `-c remote.origin.receivepack=git-receive-pack` set — that is
        how the finding was confirmed."""
        git = shutil.which("git")
        if not git:  # pragma: no cover - git is present on CI
            pytest.skip("git is not installed")
        upstream, work = tmp_path / "up", tmp_path / "work"
        marker = tmp_path / "RECV_RAN"
        hostile = tmp_path / "recv.sh"
        hostile.write_text(f"#!/bin/sh\necho ran > {marker}\nexit 1\n", encoding="utf-8")
        hostile.chmod(0o755)

        def _run(*args: str, cwd: Path = work) -> None:
            subprocess.run(
                [git, *args], cwd=cwd, capture_output=True, text=True, timeout=60
            )

        subprocess.run(
            [git, "init", "-q", "--bare", "-b", "main", str(upstream)],
            capture_output=True, timeout=60,
        )
        subprocess.run(
            [git, "clone", "-q", str(upstream), str(work)], capture_output=True, timeout=60
        )
        _run("config", "user.email", "t@example.invalid")
        _run("config", "user.name", "t")
        (work / "f.txt").write_text("x\n", encoding="utf-8")
        _run("add", "-A")
        _run("commit", "-qm", "one")
        # The attacker's half: a second remote whose receivepack is a script, made the
        # default for pushes. `remote.origin.*` says nothing about `evil`.
        _run("remote", "add", "evil", str(upstream))
        _run("config", "remote.evil.receivepack", str(hostile))
        _run("config", "remote.pushDefault", "evil")

        _run("-c", "remote.origin.receivepack=git-receive-pack",
             *gitops._pack_program_args(["push"]))
        assert not marker.exists(), "a selected remote's receivepack executed"

    async def test_against_real_git_a_selected_remote_cannot_run_its_uploadpack(
        self, tmp_path: Path
    ) -> None:
        """The PULL direction. Both halves matter: the first fix attempt covered push
        only, and `branch.<b>.remote` reaches the same execution on a pull."""
        git = shutil.which("git")
        if not git:  # pragma: no cover - git is present on CI
            pytest.skip("git is not installed")
        upstream, work = tmp_path / "up", tmp_path / "work"
        marker = tmp_path / "UP_RAN"
        hostile = tmp_path / "up.sh"
        hostile.write_text(f"#!/bin/sh\necho ran > {marker}\nexit 1\n", encoding="utf-8")
        hostile.chmod(0o755)

        def _run(*args: str) -> None:
            subprocess.run(
                [git, *args], cwd=work, capture_output=True, text=True, timeout=60
            )

        subprocess.run(
            [git, "init", "-q", "--bare", "-b", "main", str(upstream)],
            capture_output=True, timeout=60,
        )
        subprocess.run(
            [git, "clone", "-q", str(upstream), str(work)], capture_output=True, timeout=60
        )
        _run("config", "user.email", "t@example.invalid")
        _run("config", "user.name", "t")
        (work / "f.txt").write_text("x\n", encoding="utf-8")
        _run("add", "-A")
        _run("commit", "-qm", "one")
        _run("remote", "add", "evil", str(upstream))
        _run("config", "remote.evil.uploadpack", str(hostile))
        _run("config", "branch.main.remote", "evil")

        _run("-c", "remote.origin.uploadpack=git-upload-pack",
             *gitops._pack_program_args(["pull"]))
        assert not marker.exists(), "a selected remote's uploadpack executed"


@pytest.mark.asyncio
class TestGitProxyCannotExecuteACommand:
    """`core.gitProxy` names an EXECUTABLE git runs for `git://` remotes, so an
    agent-written `core.gitProxy=/path/to/script` plus a `git://` remote is arbitrary
    execution. Confirmed against real git — the script ran and wrote its marker.

    It is NOT reachable-by-accident: the existing `protocol.allow=user` override still
    permits `git://` (verified), so the vector was live.
    """

    async def test_the_proxy_is_disabled_in_the_env_not_by_a_c_override(self) -> None:
        """`-c` cannot fix this, which is the whole point of the env var.

        `core.gitProxy` is MULTI-VALUED: `-c` APPENDS rather than replaces, git uses the
        first match, and the repo's value is read first. Tested against real git —
        `-c core.gitProxy=none` (the obvious remedy), `=` and `=true` ALL still executed
        the script. `git -c core.gitProxy=none config --get-all core.gitProxy` prints the
        repo's value *and* ours, which is why.
        """
        import inspect

        source = inspect.getsource(gitops._git)
        assert 'env["GIT_PROXY_COMMAND"]' in source, "the proxy is not neutralized"

        # And specifically NOT as a `-c` override, which would be silently inert. Asserted
        # against the built ARGV rather than the source text: the source also EXPLAINS why
        # `-c core.gitProxy=none` does not work, so a substring check on it matches the
        # comment and can never fail.
        captured: list[list[str]] = []

        async def _fake_exec(*wrapped, **kwargs):  # noqa: ANN001, ANN202
            captured.append(list(wrapped))
            raise OSError("stop here")

        with mock.patch.object(
            gitops, "_pin_attributes_sync", lambda cwd: None
        ), mock.patch.object(
            gitops, "sandboxed_spawn_argv", side_effect=lambda argv, *a, **k: (argv, {}, None)
        ), mock.patch.object(gitops, "create_subprocess_limited", _fake_exec):
            with pytest.raises(OSError):
                await gitops._git(["status"], cwd=Path("/tmp"))

        assert not any(
            a.startswith("core.gitProxy") for a in captured[0]
        ), "core.gitProxy is passed as a -c override, which git appends rather than replaces"

    async def test_the_env_value_is_set_on_the_child(self) -> None:
        captured: list[dict[str, str]] = []

        async def _fake_exec(*wrapped, **kwargs):  # noqa: ANN001, ANN202
            captured.append(dict(kwargs.get("env") or {}))
            raise OSError("stop here")

        with mock.patch.object(
            gitops, "_pin_attributes_sync", lambda cwd: None
        ), mock.patch.object(
            gitops, "sandboxed_spawn_argv", side_effect=lambda argv, *a, **k: (argv, {}, None)
        ), mock.patch.object(gitops, "create_subprocess_limited", _fake_exec):
            with pytest.raises(OSError):
                await gitops._git(["status"], cwd=Path("/tmp"))

        # `true` is a real no-op binary: an EMPTY value is read as a path.
        assert captured[0]["GIT_PROXY_COMMAND"] == "true"

    async def test_against_real_git_the_proxy_does_not_run(self, tmp_path: Path) -> None:
        """The demonstration. Without `GIT_PROXY_COMMAND` this same body writes the
        marker — that is how the finding was confirmed."""
        git = shutil.which("git")
        if not git:  # pragma: no cover - git is present on CI
            pytest.skip("git is not installed")
        project = tmp_path / "hostile"
        project.mkdir()
        marker = tmp_path / "PROXY_RAN"
        proxy = project / "proxy.sh"
        proxy.write_text(f"#!/bin/sh\necho ran > {marker}\nexit 1\n", encoding="utf-8")
        proxy.chmod(0o755)

        def _run(*args: str, env: dict[str, str] | None = None) -> None:
            subprocess.run(
                [git, *args],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=60,
                env={**os.environ, **(env or {})},
            )

        _run("init", "-q", ".")
        _run("config", "core.gitProxy", str(proxy))
        # `example.invalid` never resolves, so this cannot reach the network; the proxy
        # would be exec'd BEFORE any connection is attempted, which is the point.
        _run("ls-remote", "git://example.invalid/x", env={"GIT_PROXY_COMMAND": "true"})
        assert not marker.exists(), "core.gitProxy executed despite GIT_PROXY_COMMAND"


@pytest.mark.asyncio
class TestGitattributesCannotNameAProgram:
    """`.gitattributes` selects per-path DRIVERS whose config keys hold commands:
    `filter` (clean/smudge/process), `diff` (textconv/external), `merge` (driver).

    `git add` runs a clean filter, so a cloned repo shipping `* filter=x` plus an
    agent-written `filter.x.clean` in `.git/config` executed that command — reached
    during `push`, i.e. in `standard` sandbox mode with `~/.ssh` readable. Verified
    against real git in both directions (see the real-git test below).

    `-c` cannot close this: the subsection name is ATTACKER-CHOSEN and git accepts no
    glob there — `-c 'filter.*.clean='` was tested and the filter still ran.
    """

    async def test_the_pin_is_written_before_every_command(self, repo: Path) -> None:
        """Ensured on EVERY call, not once at clone time: the same agent write that
        plants `filter.x.clean` can delete this file, so a one-shot pin is removable by
        the actor it defends against."""
        script = _GitScript()
        with mock.patch.object(gitops, "_git", script):
            pass
        pinned = repo / ".git" / "info" / "attributes"
        assert not pinned.exists()
        await asyncio.to_thread(gitops._pin_attributes_sync, repo)
        assert pinned.read_text(encoding="utf-8") == gitops._ATTRIBUTES_PIN
        # Deleting it (the agent's move) and running again restores it.
        pinned.unlink()
        await asyncio.to_thread(gitops._pin_attributes_sync, repo)
        assert pinned.exists(), "the pin is not re-established, so it can be removed once"

    async def test_a_users_own_attributes_are_not_destroyed(self, repo: Path) -> None:
        """The pin APPENDS. The first version wrote over the file, which silently
        replaced a user's own `.git/info/attributes` — their `text eol=lf` and `binary`
        rules gone on the next status poll, with nothing said. This file is
        checkout-local, so git never restores it: the loss is permanent and invisible.

        Appending keeps the guarantee because git resolves attributes per NAME with the
        LAST match winning, not per line — so a user rule for `eol`/`text` survives while
        `filter`/`diff`/`merge` still end up ours (asserted against real git below).
        """
        pinned = repo / ".git" / "info" / "attributes"
        pinned.parent.mkdir(parents=True, exist_ok=True)
        theirs = "data/*.csv text eol=lf\n*.png binary\n"
        pinned.write_text(theirs, encoding="utf-8")

        await asyncio.to_thread(gitops._pin_attributes_sync, repo)

        after = pinned.read_text(encoding="utf-8")
        assert theirs in after, "the user's own attribute rules were destroyed"
        assert after.endswith(gitops._ATTRIBUTES_PIN), "the pin must come LAST to win"

    async def test_the_pin_does_not_accumulate(self, repo: Path) -> None:
        """It is re-established on every git call, so appending must be idempotent or the
        file grows without bound over a session."""
        for _ in range(5):
            await asyncio.to_thread(gitops._pin_attributes_sync, repo)
        body = (repo / ".git" / "info" / "attributes").read_text(encoding="utf-8")
        assert body.count(gitops._ATTRIBUTES_PIN.strip()) == 1

    async def test_a_file_without_a_trailing_newline_is_not_spliced(
        self, repo: Path
    ) -> None:
        """Appending to `*.png binary` with no trailing newline would produce
        `*.png binary* -filter ...` — one corrupt rule instead of two valid ones."""
        pinned = repo / ".git" / "info" / "attributes"
        pinned.parent.mkdir(parents=True, exist_ok=True)
        pinned.write_text("*.png binary", encoding="utf-8")
        await asyncio.to_thread(gitops._pin_attributes_sync, repo)
        lines = pinned.read_text(encoding="utf-8").splitlines()
        assert lines == ["*.png binary", gitops._ATTRIBUTES_PIN.strip()]

    async def test_against_real_git_user_rules_survive_and_filters_stay_dead(
        self, tmp_path: Path
    ) -> None:
        """Both properties at once, against real git: the append must not have traded
        the security guarantee for the data-loss fix."""
        git = shutil.which("git")
        if not git:  # pragma: no cover - git is present on CI
            pytest.skip("git is not installed")
        project = tmp_path / "both"
        project.mkdir()
        marker = tmp_path / "FILTER_RAN"

        def _run(*args: str) -> str:
            return subprocess.run(
                [git, *args], cwd=project, capture_output=True, text=True, timeout=60
            ).stdout

        _run("init", "-q", ".")
        _run("config", "user.email", "t@example.invalid")
        _run("config", "user.name", "t")
        _run("config", "filter.pwn.clean", f"sh -c 'echo ran > {marker}; cat'")
        (project / ".gitattributes").write_text("* filter=pwn\n", encoding="utf-8")
        (project / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (project / ".git" / "info" / "attributes").write_text(
            "data/*.csv text eol=lf\n", encoding="utf-8"
        )
        (project / "data").mkdir()
        (project / "data" / "x.csv").write_text("a\n", encoding="utf-8")

        gitops._pin_attributes_sync(project)
        _run("add", "-A")

        assert not marker.exists(), "the clean filter executed despite the pin"
        assert "eol: lf" in _run("check-attr", "eol", "--", "data/x.csv"), (
            "the user's own eol rule stopped applying"
        )

    async def test_git_is_never_spawned_without_the_pin(self) -> None:
        """The pin must be a precondition of the spawn, not a separate step a future
        caller can forget — an unpinned spawn is the whole vulnerability."""
        order: list[str] = []

        async def _fake_exec(*wrapped, **kwargs):  # noqa: ANN001, ANN202
            order.append("spawn")
            raise OSError("stop here")

        with mock.patch.object(
            gitops, "_pin_attributes_sync", side_effect=lambda cwd: order.append("pin")
        ), mock.patch.object(
            gitops, "sandboxed_spawn_argv", side_effect=lambda argv, *a, **k: (argv, {}, None)
        ), mock.patch.object(gitops, "create_subprocess_limited", _fake_exec):
            with pytest.raises(OSError):
                await gitops._git(["status"], cwd=Path("/tmp"))

        assert order == ["pin", "spawn"], f"pin did not precede the spawn: {order}"

    async def test_filter_and_diff_are_unset_but_merge_keeps_the_builtin(self) -> None:
        """`merge=text`, NOT `-merge`. Unsetting `merge` makes git treat the file as
        BINARY and declare a conflict instead of merging, which silently broke `pull` — a
        clean 3-way merge became "could not apply" (verified against real git). `text`
        pins the built-in 3-way driver, so a normal pull still merges while a custom
        driver stays unreachable.
        """
        pin = gitops._ATTRIBUTES_PIN
        assert pin.startswith("* "), "the pin must apply to every path"
        assert "-filter" in pin and "-diff" in pin
        assert "merge=text" in pin
        assert "-merge" not in pin, "unsetting merge breaks ordinary pulls"

    async def test_a_git_file_rather_than_a_dir_is_refused(self, tmp_path: Path) -> None:
        """A `.git` FILE is a worktree/submodule pointing elsewhere, so the pin cannot be
        placed where git reads it. Papyrus never creates that shape, and running git with
        attribute drivers live is worse than refusing."""
        project = tmp_path / "wt"
        project.mkdir()
        (project / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
        with pytest.raises(gitops.GitError):
            await asyncio.to_thread(gitops._pin_attributes_sync, project)

    async def test_a_non_repo_is_a_no_op(self, plain: Path) -> None:
        """`clone`'s cwd is the PARENT directory, which is not a repo. Nothing to pin, and
        the clone is safe regardless: filter config lives in `.git/config`, which is
        created locally and never transferred from the remote."""
        await asyncio.to_thread(gitops._pin_attributes_sync, plain)
        assert not (plain / ".git").exists()

    async def test_against_real_git_a_clean_filter_does_not_run(self, tmp_path: Path) -> None:
        """The demonstration, not a restatement of the fix.

        Runs REAL git: configure a clean filter the way a hostile repo would, then assert
        the pin stops `git add` from executing it. Without the pin this same body writes
        the marker file (that is how the finding was confirmed).
        """
        git = shutil.which("git")
        if not git:  # pragma: no cover - git is present on CI
            pytest.skip("git is not installed")
        project = tmp_path / "hostile"
        project.mkdir()
        marker = tmp_path / "FILTER_RAN"

        def _run(*args: str) -> int:
            return subprocess.run(
                [git, *args], cwd=project, capture_output=True, text=True, timeout=60
            ).returncode

        assert _run("init", "-q", ".") == 0
        _run("config", "user.email", "t@example.invalid")
        _run("config", "user.name", "t")
        # A hostile repo supplies BOTH halves: the driver command and the path mapping.
        _run("config", "filter.pwn.clean", f"sh -c 'echo ran > {marker}; cat'")
        (project / ".gitattributes").write_text("* filter=pwn\n", encoding="utf-8")
        (project / "f.txt").write_text("hello\n", encoding="utf-8")

        gitops._pin_attributes_sync(project)
        assert _run("add", "-A") == 0, "the pin broke `git add`"
        assert not marker.exists(), "the clean filter executed despite the pin"


class TestSandboxRefusalIsReportedNotSwallowed:
    """No sandbox backend => a typed, actionable error, not an unhandled 500.

    ``push`` deliberately runs in ``standard`` mode because an SSH push needs the
    key, so the OS sandbox is doing real work on this path — dropping the wrap to
    make Windows "work" would hand an agent-writable repo config a shell with
    ``~/.ssh`` in reach. The refusal is therefore surfaced, never bypassed.

    Reachable on every Windows host: user namespaces are Linux-only and
    ``sandbox-exec`` is macOS-only, so ``detect_backend()`` there is ``"none"``.
    """

    @pytest.mark.asyncio
    async def test_it_raises_the_typed_subclass(self, repo: Path) -> None:
        boom = mock.Mock(
            side_effect=sandbox.SandboxUnavailableError(
                "no backend", "no_backend", "simulated"
            )
        )
        with mock.patch.object(gitops, "sandboxed_spawn_argv", boom):
            with pytest.raises(gitops.GitSandboxUnavailable) as caught:
                await gitops.commit(repo, "msg")
        # A `GitError` subclass, so existing handlers still catch it — but its own
        # type, so the route layer can answer with a code whose remedy is a config
        # change rather than something about the repository.
        assert isinstance(caught.value, gitops.GitError)
        assert "no backend" in str(caught.value)


class TestTheAttributesPinCannotBeNeutralized:
    """The pin must END UP LAST, and must never be written through a symlink.

    Two ways the original implementation could be defeated, both verified against
    real git before the fix and pinned here:

    1. **Pre-seed the pin line.** The idempotence check returned early when the pin
       was already *present*, so an attacker wrote the pin line themselves and put
       `* filter=x` after it. Git resolves attributes per name with the LAST match
       winning, so the attacker's rule won while the early return kept the real pin
       from being re-appended. "Already present" is not the same property as
       "last", and only the second one is protective.
    2. **A symlink at the name.** `read_text`/`write_text` both follow one, so
       `attributes -> /dev/null` made the pin unobservable AND unwritable (silently
       inert forever), and `attributes -> <any file>` turned a `GET /git` status
       poll into an arbitrary-file append plus a read of that file's contents.

    Both are reachable by the co-author agent, which can write into the project, on
    a path that runs in `standard` sandbox mode with `~/.ssh` readable.
    """

    def _repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "paper"
        (repo / ".git" / "info").mkdir(parents=True)
        return repo

    def test_a_pre_seeded_pin_is_moved_back_to_last(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        attrs = repo / ".git" / "info" / "attributes"
        pin = gitops._ATTRIBUTES_PIN.strip()
        attrs.write_text(f"{pin}\n* filter=x\n", encoding="utf-8")

        gitops._pin_attributes_sync(repo)

        lines = attrs.read_text(encoding="utf-8").splitlines()
        # The attacker's rule survives (we do not delete a user's rules), but the
        # pin is now AFTER it, which is what decides the outcome.
        assert "* filter=x" in lines
        assert lines[-1] == pin, f"pin must be last, got {lines!r}"

    def test_the_pin_still_does_not_accumulate(self, tmp_path: Path) -> None:
        """Idempotence is preserved — repeat calls converge, they do not append."""
        repo = self._repo(tmp_path)
        attrs = repo / ".git" / "info" / "attributes"
        pin = gitops._ATTRIBUTES_PIN.strip()

        gitops._pin_attributes_sync(repo)
        once = attrs.read_text(encoding="utf-8")
        gitops._pin_attributes_sync(repo)
        gitops._pin_attributes_sync(repo)
        thrice = attrs.read_text(encoding="utf-8")

        assert once == thrice
        assert thrice.count(pin) == 1

    def test_a_users_own_rules_are_preserved_below_the_pin(self, tmp_path: Path) -> None:
        """The append-not-overwrite property this file documents must still hold."""
        repo = self._repo(tmp_path)
        attrs = repo / ".git" / "info" / "attributes"
        attrs.write_text("*.tex text eol=lf\n*.pdf binary\n", encoding="utf-8")

        gitops._pin_attributes_sync(repo)

        content = attrs.read_text(encoding="utf-8")
        assert "*.tex text eol=lf" in content
        assert "*.pdf binary" in content
        assert content.splitlines()[-1] == gitops._ATTRIBUTES_PIN.strip()

    def test_a_symlinked_attributes_file_is_refused(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        victim = tmp_path / "victim.txt"
        victim.write_text("original\n", encoding="utf-8")
        (repo / ".git" / "info" / "attributes").symlink_to(victim)

        with pytest.raises(gitops.GitError, match="symlink"):
            gitops._pin_attributes_sync(repo)
        # The write did NOT follow the link.
        assert victim.read_text(encoding="utf-8") == "original\n"

    def test_a_symlinked_info_directory_is_refused(self, tmp_path: Path) -> None:
        """`mkdir(exist_ok=True)` is a no-op on a link, so the dir needs its own check."""
        repo = tmp_path / "paper"
        (repo / ".git").mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (repo / ".git" / "info").symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(gitops.GitError, match="symlink"):
            gitops._pin_attributes_sync(repo)
        assert not (elsewhere / "attributes").exists()

    def test_the_rewrite_is_atomic(self, tmp_path: Path) -> None:
        """Temp file + rename, not truncate-then-write.

        This is a read-modify-write on a file two concurrent requests touch (a
        toolbar status poll and a push). `write_text` truncates first, so an
        overlapping reader could observe the empty window, keep nothing, and rename
        pin-only content over the user's rules — a permanent loss, since the file is
        checkout-local and git never restores it.

        Asserted on the source rather than by racing threads: the property is "which
        write primitive is used", and a timing test for it would be inherently flaky.
        """
        import inspect

        src = inspect.getsource(gitops._pin_attributes_sync)
        assert "atomic_write(" in src
        assert "target.write_text(" not in src

    def test_no_temp_residue_is_left_behind(self, tmp_path: Path) -> None:
        repo = tmp_path / "paper"
        (repo / ".git" / "info").mkdir(parents=True)
        gitops._pin_attributes_sync(repo)
        gitops._pin_attributes_sync(repo)
        names = sorted(p.name for p in (repo / ".git" / "info").iterdir())
        assert names == ["attributes"], f"temp file survived: {names!r}"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
    def test_the_rewrite_preserves_the_existing_mode(self, tmp_path: Path) -> None:
        """A guard must not silently loosen permissions on the file it protects.

        `atomic_write` renames a fresh temp file into place, so without carrying the
        mode across, a user who had tightened `.git/info/attributes` to 0600 would
        find it 0644 after any status poll.
        """
        repo = tmp_path / "paper"
        (repo / ".git" / "info").mkdir(parents=True)
        attrs = repo / ".git" / "info" / "attributes"
        attrs.write_text("*.tex text\n", encoding="utf-8")
        os.chmod(attrs, 0o600)

        gitops._pin_attributes_sync(repo)

        assert stat.S_IMODE(attrs.stat().st_mode) == 0o600
        assert "*.tex text" in attrs.read_text(encoding="utf-8")

    def test_a_first_write_needs_no_prior_mode(self, tmp_path: Path) -> None:
        """Nothing to preserve is the normal case on a fresh clone, not an error."""
        repo = tmp_path / "paper"
        (repo / ".git" / "info").mkdir(parents=True)
        gitops._pin_attributes_sync(repo)
        assert (repo / ".git" / "info" / "attributes").read_text(
            encoding="utf-8"
        ) == gitops._ATTRIBUTES_PIN

    def test_junctions_are_refused_like_symlinks(self, tmp_path: Path) -> None:
        """A Windows junction is a reparse point `is_symlink()` does not report.

        It is also the link type a Windows user can create without elevation, so a
        symlink-only guard was bypassable on exactly the platform this PR adds.
        Asserted through the shared helper so the two guards cannot drift on which
        link types they cover.
        """
        repo = tmp_path / "paper"
        (repo / ".git" / "info").mkdir(parents=True)
        target = repo / ".git" / "info" / "attributes"
        target.write_text("", encoding="utf-8")
        with mock.patch.object(gitops.store, "is_reparse_link", return_value=True):
            with pytest.raises(gitops.GitError, match="symlink"):
                gitops._pin_attributes_sync(repo)

    @pytest.mark.skipif(
        sys.platform == "win32", reason="symlink creation needs privilege on Windows"
    )
    def test_a_linked_git_dir_is_refused(self, tmp_path: Path) -> None:
        """`.git` itself is the OUTERMOST name that must not be a link.

        Guarding only `info` and `attributes` left the chain rooted on an unverified
        link: point `.git` at ANOTHER repository and both inner names are legitimate
        non-links *inside that repo*, so both checks pass and a `GET /git` status
        poll rewrites a different repository's attributes — outside this project
        entirely. The rule has to hold for every segment traversed by name.
        """
        project = tmp_path / "paper"
        project.mkdir()
        victim = tmp_path / "victim-repo"
        (victim / "info").mkdir(parents=True)
        original = "*.tex text eol=lf\n"
        (victim / "info" / "attributes").write_text(original, encoding="utf-8")
        (project / ".git").symlink_to(victim, target_is_directory=True)

        with pytest.raises(gitops.GitError, match="symlink"):
            gitops._pin_attributes_sync(project)
        # The other repository was not touched.
        assert (victim / "info" / "attributes").read_text(encoding="utf-8") == original

    @pytest.mark.skipif(
        sys.platform == "win32", reason="symlink creation needs privilege on Windows"
    )
    def test_the_refusal_is_sel_audited(self, tmp_path: Path) -> None:
        """A security decision that leaves no record is indistinguishable from a
        no-op afterwards, and AUTOSDE requires a SEL event for every permission
        decision. Both refusals on this path emit one."""
        project = tmp_path / "paper"
        project.mkdir()
        victim = tmp_path / "victim"
        (victim / "info").mkdir(parents=True)
        (project / ".git").symlink_to(victim, target_is_directory=True)

        with mock.patch.object(gitops, "_audit") as audit:
            with pytest.raises(gitops.GitError):
                gitops._pin_attributes_sync(project)
        assert audit.call_count == 1
        assert audit.call_args.args[2] == "denied"

    def test_the_non_directory_refusal_is_audited_too(self, tmp_path: Path) -> None:
        """The sibling refusal (a `.git` FILE — worktree/submodule) had the same gap."""
        project = tmp_path / "paper"
        project.mkdir()
        (project / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")

        with mock.patch.object(gitops, "_audit") as audit:
            with pytest.raises(gitops.GitError, match="not a directory"):
                gitops._pin_attributes_sync(project)
        assert audit.call_count == 1
        assert audit.call_args.args[2] == "denied"
