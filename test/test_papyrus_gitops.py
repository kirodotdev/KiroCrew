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
from pathlib import Path
from unittest import mock

import pytest

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
        return self.results.get(args[0], (0, "", ""))

    @property
    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls]

    def argv_for(self, verb: str) -> list[str]:
        return next(c for c in self.calls if c[0] == verb)


class TestUrlValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/group/paper.git",
            "http://example.com/paper",
            "ssh://git@example.com/paper.git",
            "git://example.com/paper.git",
            "git@example.com:group/paper.git",
        ],
    )
    def test_accepts_known_transports(self, url: str) -> None:
        assert gitops.GIT_URL_RE.match(url)

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

    async def test_removes_the_partial_clone_on_failure(self, tmp_path: Path) -> None:
        """A leftover directory would block the retry with "already exists"."""
        dest = tmp_path / "dest"
        dest.mkdir()
        script = _GitScript({"clone": (128, "", "fatal: repository not found")})
        with mock.patch.object(gitops, "_git", script):
            with pytest.raises(gitops.GitError) as excinfo:
                await gitops.clone("https://example.com/g/p.git", dest)
        assert "not found" in excinfo.value.output
        assert not dest.exists()


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
