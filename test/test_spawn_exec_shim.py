"""Post-exec spawn shim: resource limits without forking the threaded gateway.

The defect these guard against (issue #935): passing ``preexec_fn`` makes CPython
``fork()`` the multi-GB, ~118-thread gateway and run Python bytecode in the child
before ``exec``. A lock another thread held at fork time is unreleasable there, so
the child can wedge before exec -- and then

* ``Popen._execute_child`` blocks in an unbounded ``os.read(errpipe_read, ...)``
  waiting for that exec, on the event loop thread, past any per-command timeout;
* ``_close_open_fds()`` has not run yet (``child_exec()`` closes fds AFTER
  ``preexec_fn``), so the wedged child pins every inherited fd, the dashboard's
  listening socket included.

The fix moves the limits after ``exec``, where the process is single-threaded, and
leaves ``preexec_fn`` unset so the fork child runs only async-signal-safe C.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import threading
from unittest.mock import AsyncMock, patch

import pytest
from spawn_test_helpers import strip_spawn_shim

from kiro_crew import _spawn_exec_shim as shim
from kiro_crew import sandbox
from kiro_crew.sandbox import (
    RLIMIT_PROFILE_BUILD,
    RLIMIT_PROFILE_NONE,
    RLIMIT_PROFILE_SESSION_HOST,
    RLIMIT_PROFILE_TOOL,
    create_subprocess_limited,
    popen_limited,
    run_limited,
    spawn_shim_argv,
)

# The shim exists to deliver POSIX rlimits, and `spawn_shim_argv` returns an empty
# prefix on Windows, so there is nothing here to exercise there. Skipping at import
# keeps `import resource` from failing collection on the Windows shards. Windows
# still runs test_spawn_preexec_guard.py, which is pure AST and platform-neutral.
resource = pytest.importorskip("resource", reason="POSIX resource limits only")

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX resource limits only")


@pytest.fixture(autouse=True)
def _clear_shim_cache():
    """The argv prefix is cached per (profile, ctty); tests mutate the inputs."""
    sandbox._SHIM_ARGV_CACHE.clear()
    yield
    sandbox._SHIM_ARGV_CACHE.clear()


# --------------------------------------------------------------------------
# The shim's own argv contract
# --------------------------------------------------------------------------


class TestShimSpecParsing:
    def test_numeric_and_hard_tokens_resolve(self):
        parsed = shim._parse_rlimits("RLIMIT_NOFILE:1024,RLIMIT_CPU:hard")
        assert (resource.RLIMIT_NOFILE, 1024) in parsed
        assert (resource.RLIMIT_CPU, None) in parsed

    def test_unknown_name_and_junk_value_are_skipped_not_fatal(self):
        # Mirrors security.apply_resource_limits: an rlimit this platform lacks
        # must never block the spawn.
        assert shim._parse_rlimits("RLIMIT_NOPE:1,RLIMIT_NOFILE:abc") == []
        assert shim._parse_rlimits("") == []

    def test_clamps_down_to_the_inherited_hard_limit(self):
        _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard == resource.RLIM_INFINITY:
            pytest.skip("no finite hard limit to clamp against")
        applied: list[tuple[int, tuple[int, int]]] = []
        with patch.object(shim._resource, "setrlimit", lambda i, v: applied.append((i, v))):
            shim._apply_rlimits([(resource.RLIMIT_NOFILE, hard + 4096)])
        assert applied == [(resource.RLIMIT_NOFILE, (hard, hard))]

    def test_hard_token_raises_soft_without_lowering_the_ceiling(self):
        applied: list[tuple[int, tuple[int, int]]] = []
        with (
            patch.object(shim._resource, "getrlimit", lambda _i: (64, 4096)),
            patch.object(shim._resource, "setrlimit", lambda i, v: applied.append((i, v))),
        ):
            shim._apply_rlimits([(resource.RLIMIT_NOFILE, None)])
        assert applied == [(resource.RLIMIT_NOFILE, (4096, 4096))]


class TestShimArgvContract:
    def test_missing_separator_is_refused(self, capsys):
        assert shim.main(["--rlimits=RLIMIT_NOFILE:1024"]) == 127
        assert "argv separator" in capsys.readouterr().err

    def test_unknown_option_is_refused_rather_than_guessed(self, capsys):
        # Guessing where the command starts could exec the wrong thing.
        assert shim.main(["--surprise", "--", "/bin/true"]) == 127
        assert "unknown option" in capsys.readouterr().err

    def test_empty_command_is_refused(self, capsys):
        assert shim.main(["--"]) == 127
        assert "no command" in capsys.readouterr().err

    def test_exec_failure_reports_127_not_a_traceback(self, capsys):
        assert shim.main(["--", "/nonexistent/binary"]) == 127
        assert "cannot execute" in capsys.readouterr().err

    def test_separator_inside_the_command_is_not_consumed(self):
        calls: list[list[bytes]] = []

        def fake_execv(_path, argv):
            calls.append(argv)
            raise OSError(2, "stop here")

        with patch.object(shim.os, "execv", fake_execv):
            shim.main(["--", "/bin/echo", "--", "--rlimits=bogus"])
        assert calls == [[b"/bin/echo", b"--", b"--rlimits=bogus"]]

    def test_limits_are_applied_before_exec(self):
        """Ordering matters: a limit applied after exec would not bind the child."""
        order: list[str] = []
        with (
            patch.object(shim, "_apply_rlimits", lambda _p: order.append("limits")),
            patch.object(shim, "_bias_oom_score", lambda: order.append("oom")),
            patch.object(
                shim.os,
                "execv",
                lambda *_a: order.append("exec") or (_ for _ in ()).throw(OSError(2, "x")),
            ),
        ):
            shim.main(["--rlimits=RLIMIT_NOFILE:1024", "--oom-bias", "--", "/bin/true"])
        assert order == ["limits", "oom", "exec"]

    def test_a_malformed_chdir_fd_is_refused_rather_than_ignored(self, capsys):
        # Ignoring it would exec in whatever cwd was inherited -- a directory the
        # caller never authorized -- which is exactly what the pin exists to stop.
        assert shim.main(["--chdir-fd=not-a-number", "--", "/bin/true"]) == 127
        assert "bad --chdir-fd=" in capsys.readouterr().err
        assert shim.main(["--chdir-fd=-1", "--", "/bin/true"]) == 127
        assert "bad --chdir-fd=" in capsys.readouterr().err

    def test_an_unenterable_chdir_fd_never_reaches_exec(self, capsys):
        """Fail the spawn rather than run the command in the wrong directory."""
        not_a_directory = os.open(os.devnull, os.O_RDONLY)
        execs: list[tuple] = []
        try:
            with patch.object(shim.os, "execv", lambda *args: execs.append(args)):
                rc = shim.main([f"--chdir-fd={not_a_directory}", "--", "/bin/true"])
        finally:
            os.close(not_a_directory)
        assert rc == 127
        assert execs == []
        assert "cannot enter bound directory" in capsys.readouterr().err

    def test_the_bound_directory_is_entered_before_the_limits(self):
        """A tight RLIMIT_AS must not be able to break the fchdir's error path."""
        order: list[str] = []
        with (
            patch.object(shim, "_enter_bound_directory", lambda _fd: order.append("chdir") or True),
            patch.object(shim, "_apply_rlimits", lambda _p: order.append("limits")),
            patch.object(
                shim.os,
                "execv",
                lambda *_a: order.append("exec") or (_ for _ in ()).throw(OSError(2, "x")),
            ),
        ):
            shim.main(["--rlimits=RLIMIT_NOFILE:1024", "--chdir-fd=9", "--", "/bin/true"])
        assert order == ["chdir", "limits", "exec"]

    def test_entering_a_bound_directory_closes_the_descriptor(self, tmp_path, monkeypatch):
        """The command must not inherit a handle that outlives the parent's check."""
        # monkeypatch.chdir, not a bare os.chdir with a manual restore: the cwd is
        # process-wide state shared with every other test on this worker, and only
        # the fixture guarantees it reverts when the assertion below fails.
        monkeypatch.chdir(tmp_path)
        descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        closed: list[int] = []
        try:
            with patch.object(shim.os, "close", closed.append):
                assert shim._enter_bound_directory(descriptor) is True
            assert os.path.realpath(os.getcwd()) == os.path.realpath(str(tmp_path))
            assert closed == [descriptor]
        finally:
            os.close(descriptor)

    def test_oom_bias_only_when_requested(self):
        biased: list[bool] = []
        with (
            patch.object(shim, "_bias_oom_score", lambda: biased.append(True)),
            patch.object(shim.os, "execv", lambda *_a: (_ for _ in ()).throw(OSError(2, "x"))),
        ):
            shim.main(["--", "/bin/true"])
            assert biased == []
            shim.main(["--oom-bias", "--", "/bin/true"])
            assert biased == [True]


# --------------------------------------------------------------------------
# Parent side: the argv prefix and the profiles
# --------------------------------------------------------------------------


@posix_only
class TestSpawnShimArgv:
    def test_prefix_is_an_isolated_interpreter_running_captured_source(self):
        prefix = spawn_shim_argv()
        assert prefix[0] == sys.executable
        # -I keeps env/user-site out; -S additionally skips site, so a
        # sitecustomize dropped into site-packages cannot run ahead of the shim.
        assert prefix[1:4] == ("-I", "-S", "-c")
        assert "def main(" in prefix[4]
        assert prefix[-1] == "--"

    def test_tool_profile_carries_limits_and_the_oom_bias(self):
        prefix = spawn_shim_argv(RLIMIT_PROFILE_TOOL)
        assert any(a.startswith("--rlimits=RLIMIT_NOFILE:") for a in prefix)
        assert "--oom-bias" in prefix

    def test_session_host_raises_nofile_and_is_not_an_oom_target(self):
        # Faithful port of session_host_preexec: NOFILE only, no OOM bias.
        prefix = spawn_shim_argv(RLIMIT_PROFILE_SESSION_HOST)
        assert "--rlimits=RLIMIT_NOFILE:hard" in prefix
        assert "--oom-bias" not in prefix

    def test_build_profile_raises_the_descriptor_ceiling(self):
        tool = [a for a in spawn_shim_argv(RLIMIT_PROFILE_TOOL) if a.startswith("--rlimits=")]
        build = [a for a in spawn_shim_argv(RLIMIT_PROFILE_BUILD) if a.startswith("--rlimits=")]
        assert tool != build
        assert f"RLIMIT_NOFILE:{sandbox._BUILD_NOFILE_CEILING}" in build[0]

    def test_policy_free_profile_skips_the_interpreter_hop(self):
        # Nothing to do post-exec: no reason to pay an exec + startup.
        assert spawn_shim_argv(RLIMIT_PROFILE_NONE) == ()


# --------------------------------------------------------------------------
# Parent side: the wrapper
# --------------------------------------------------------------------------


@posix_only
class TestCreateSubprocessLimited:
    @pytest.mark.asyncio
    async def test_never_passes_a_fork_child_callable(self):
        """The regression guard: a callable here forks the threaded gateway."""
        spawn = AsyncMock()
        with patch("asyncio.create_subprocess_exec", spawn):
            await create_subprocess_limited("/bin/true")
        assert spawn.await_args.kwargs["preexec_fn"] is None
        assert strip_spawn_shim(spawn.await_args.args) == ("/bin/true",)

    @pytest.mark.asyncio
    async def test_refuses_a_caller_supplied_preexec_fn(self):
        with pytest.raises(TypeError, match="owns preexec_fn"):
            await create_subprocess_limited("/bin/true", preexec_fn=lambda: None)

    @pytest.mark.asyncio
    async def test_requires_a_command(self):
        with pytest.raises(ValueError):
            await create_subprocess_limited()

    @pytest.mark.asyncio
    async def test_bare_name_is_resolved_against_the_child_path(self):
        # Discover where `true` actually lives instead of assuming /bin/true:
        # this is the one test in the class that performs a REAL PATH lookup
        # (the others mock the spawn, so their path never has to exist), and
        # `true` is /usr/bin/true on macOS — the hardcoded /bin/true made this
        # fail there with FileNotFoundError.
        true_path = shutil.which("true")
        if not true_path:
            pytest.skip("no `true` binary on PATH")
        spawn = AsyncMock()
        with patch("asyncio.create_subprocess_exec", spawn):
            await create_subprocess_limited("true", env={"PATH": os.path.dirname(true_path)})
        # The shim execs without a PATH search, so the parent must hand it a path.
        assert strip_spawn_shim(spawn.await_args.args)[0].endswith("/true")

    @pytest.mark.asyncio
    async def test_missing_command_still_raises_filenotfound_at_the_spawn(self):
        # Callers branch on this (git_coord treats it as "no git on this host"),
        # so the shim must not turn it into an exit status.
        with patch("asyncio.create_subprocess_exec", AsyncMock()):
            with pytest.raises(FileNotFoundError):
                await create_subprocess_limited(
                    "kirocrew-no-such-command", env={"PATH": "/nonexistent"}
                )

    @pytest.mark.asyncio
    async def test_path_search_runs_off_the_event_loop(self):
        """A stalled NFS/autofs PATH entry must not freeze the gateway.

        The search this replaces ran in the forked child, so it never touched this
        process; doing it inline here would be new blocking I/O on the loop.
        """
        loop_thread = threading.get_ident()
        ran_on: list[int] = []

        def spy(argv, env, cwd=None):
            ran_on.append(threading.get_ident())
            return "/bin/true"

        with (
            patch("asyncio.create_subprocess_exec", AsyncMock()),
            patch.object(sandbox, "_resolve_spawn_target", spy),
        ):
            await create_subprocess_limited("true")
        assert ran_on and ran_on[0] != loop_thread

    @pytest.mark.asyncio
    async def test_explicit_path_takes_no_thread_hop_and_no_resolution(self):
        """Nothing to resolve, so no worker thread and no filesystem access."""
        spawn = AsyncMock()
        with (
            patch("asyncio.create_subprocess_exec", spawn),
            patch.object(
                sandbox, "_resolve_spawn_target", side_effect=AssertionError("resolved a path")
            ),
            patch.object(sandbox.os.path, "isfile", side_effect=AssertionError("stat on loop")),
        ):
            await create_subprocess_limited("/nonexistent/tool", cwd="/tmp")
        assert strip_spawn_shim(spawn.await_args.args) == ("/nonexistent/tool",)

    def test_relative_path_entries_resolve_against_the_child_cwd(self, tmp_path):
        """``execvpe`` searched from the child's cwd, not the gateway's."""
        tools = tmp_path / "tools"
        tools.mkdir()
        exe = tools / "mytool"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        resolved = sandbox._resolve_spawn_target(["mytool"], {"PATH": "tools"}, cwd=str(tmp_path))
        assert resolved == str(exe)
        with pytest.raises(FileNotFoundError):
            sandbox._resolve_spawn_target(["mytool"], {"PATH": "tools"}, cwd=None)

    @pytest.mark.asyncio
    async def test_falls_back_to_preexec_rather_than_dropping_the_limits(self):
        """A truncated install must not silently spawn children uncapped."""
        spawn = AsyncMock()
        with (
            patch.object(sandbox, "_SPAWN_SHIM_CODE", ""),
            patch("asyncio.create_subprocess_exec", spawn),
        ):
            await create_subprocess_limited("/bin/true")
        assert callable(spawn.await_args.kwargs["preexec_fn"])
        assert spawn.await_args.args == ("/bin/true",)

    @pytest.mark.asyncio
    async def test_chdir_fd_rides_the_shim_and_is_inherited_by_it(self):
        spawn = AsyncMock()
        with patch("asyncio.create_subprocess_exec", spawn):
            await create_subprocess_limited("/bin/true", cwd="/tmp", chdir_fd=9, pass_fds=(7,))
        args = spawn.await_args.args
        # Ahead of the separator, so the shim reads it as its own option.
        assert args.index("--chdir-fd=9") < args.index("--")
        assert strip_spawn_shim(args) == ("/bin/true",)
        # The shim can only fchdir a descriptor the child actually holds, and
        # pass_fds is what carries it past _close_open_fds.
        assert spawn.await_args.kwargs["pass_fds"] == (7, 9)
        # cwd is DROPPED from the spawn: Popen would chdir it in the fork child
        # before the shim runs, resolving the very name the descriptor bypasses.
        assert "cwd" not in spawn.await_args.kwargs

    @pytest.mark.asyncio
    async def test_chdir_fd_never_resolves_a_command_inside_the_pinned_directory(self, tmp_path):
        """A bare name is the NORMAL shape here, so the search is narrowed, not refused.

        wrap_argv hands back "env" on macOS and cgroup_scope_argv hands back
        "systemd-run", so a pinned spawn does search PATH. ``execvpe`` resolved a
        relative entry against the child's cwd -- the pinned workspace -- so a planted
        copy there must lose to the absolute entry.
        """
        planted = tmp_path / "tools"
        planted.mkdir()
        (planted / "mytool").write_text("#!/bin/sh\nexit 9\n")
        (planted / "mytool").chmod(0o755)
        real_dir = tmp_path / "real-bin"
        real_dir.mkdir()
        real = real_dir / "mytool"
        real.write_text("#!/bin/sh\n")
        real.chmod(0o755)

        spawn = AsyncMock()
        with patch("asyncio.create_subprocess_exec", spawn):
            await create_subprocess_limited(
                "mytool",
                cwd=str(tmp_path),
                chdir_fd=9,
                env={"PATH": os.pathsep.join(["tools", "", ".", str(real_dir)])},
            )
        assert strip_spawn_shim(spawn.await_args.args) == (str(real),)
        assert "cwd" not in spawn.await_args.kwargs
        # The CHILD gets the same absolute-only PATH. Resolving argv[0] here is not
        # the last lookup: the wrapper this spawns looks its own target up on PATH
        # after the shim has entered the pinned workspace, so a relative entry left
        # in the child's environment would exec out of that directory one level down.
        assert spawn.await_args.kwargs["env"]["PATH"] == str(real_dir)

    @pytest.mark.asyncio
    async def test_an_unpinned_spawn_keeps_its_env_untouched(self):
        """The PATH narrowing is scoped to a pinned spawn, environment included."""
        spawn = AsyncMock()
        with patch("asyncio.create_subprocess_exec", spawn):
            await create_subprocess_limited("/bin/true", env={"PATH": "tools:/usr/bin"})
        assert spawn.await_args.kwargs["env"]["PATH"] == "tools:/usr/bin"

    @pytest.mark.asyncio
    async def test_chdir_fd_narrows_the_child_path_even_with_no_env_passed(self, monkeypatch):
        """A caller that passes no env still must not hand the child a relative entry."""
        monkeypatch.setenv("PATH", os.pathsep.join([".", "/usr/bin"]))
        spawn = AsyncMock()
        with patch("asyncio.create_subprocess_exec", spawn):
            await create_subprocess_limited("/bin/true", chdir_fd=9)
        assert spawn.await_args.kwargs["env"]["PATH"] == "/usr/bin"

    @pytest.mark.asyncio
    async def test_chdir_fd_with_only_relative_path_entries_resolves_nothing(self, tmp_path):
        """Dropping every entry is the intended fail-closed outcome, not a fallback."""
        planted = tmp_path / "tools"
        planted.mkdir()
        (planted / "mytool").write_text("#!/bin/sh\n")
        (planted / "mytool").chmod(0o755)
        with patch("asyncio.create_subprocess_exec", AsyncMock()):
            with pytest.raises(FileNotFoundError):
                await create_subprocess_limited(
                    "mytool", cwd=str(tmp_path), chdir_fd=9, env={"PATH": "tools:.:"}
                )

    @staticmethod
    def _planted_tool(directory, body="#!/bin/sh\n"):
        directory.mkdir(parents=True, exist_ok=True)
        tool = directory / "mytool"
        tool.write_text(body)
        tool.chmod(0o755)
        return tool

    @pytest.mark.asyncio
    async def test_chdir_fd_drops_an_absolute_path_entry_inside_the_pinned_directory(
        self, tmp_path
    ):
        """The lexical screen alone kept this: absolute, yet inside the workspace.

        ``PATH=<workspace>/bin:...`` reaches the same planted binary a relative
        entry did, by a different spelling, so the identity screen must drop it
        from the search AND from the child's environment (clause (c)). The
        surviving entry's pathname deliberately EXTENDS the workspace's own
        (``pinned-outside`` startswith ``pinned``): only an identity comparison
        keeps it, so this also guards against a string-prefix reimplementation.
        """
        workspace = tmp_path / "pinned"
        self._planted_tool(workspace / "bin", body="#!/bin/sh\nexit 9\n")
        real_dir = tmp_path / "pinned-outside"
        self._planted_tool(real_dir)
        canonical_dir = os.path.realpath(real_dir)
        descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
        spawn = AsyncMock()
        try:
            with patch("asyncio.create_subprocess_exec", spawn):
                await create_subprocess_limited(
                    "mytool",
                    chdir_fd=descriptor,
                    env={"PATH": os.pathsep.join([str(workspace / "bin"), str(real_dir)])},
                )
        finally:
            os.close(descriptor)
        assert strip_spawn_shim(spawn.await_args.args) == (os.path.join(canonical_dir, "mytool"),)
        assert spawn.await_args.kwargs["env"]["PATH"] == canonical_dir

    @pytest.mark.asyncio
    async def test_chdir_fd_drops_the_pinned_directory_itself_as_a_path_entry(self, tmp_path):
        """The E-is-the-bound-directory case, distinct from the descendant case."""
        workspace = tmp_path / "pinned"
        self._planted_tool(workspace, body="#!/bin/sh\nexit 9\n")
        real_dir = tmp_path / "real-bin"
        self._planted_tool(real_dir)
        canonical_dir = os.path.realpath(real_dir)
        descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
        spawn = AsyncMock()
        try:
            with patch("asyncio.create_subprocess_exec", spawn):
                await create_subprocess_limited(
                    "mytool",
                    chdir_fd=descriptor,
                    env={"PATH": os.pathsep.join([str(workspace), str(real_dir)])},
                )
        finally:
            os.close(descriptor)
        assert strip_spawn_shim(spawn.await_args.args) == (os.path.join(canonical_dir, "mytool"),)
        assert spawn.await_args.kwargs["env"]["PATH"] == canonical_dir

    @pytest.mark.asyncio
    async def test_chdir_fd_drops_a_symlink_alias_of_the_pinned_directory(
        self, tmp_path, monkeypatch
    ):
        """The screen is an identity check, not a pathname or realpath check.

        The alias string shares no prefix with the workspace's own path, and
        ``os.path.realpath`` is broken on purpose: a screen that compared
        resolved pathname STRINGS would keep this entry, while descriptor
        identity sees the same ``(st_dev, st_ino)`` regardless of spelling.
        """
        workspace = tmp_path / "pinned"
        self._planted_tool(workspace, body="#!/bin/sh\nexit 9\n")
        alias = tmp_path / "alias"
        os.symlink(workspace, alias)
        real_dir = tmp_path / "real-bin"
        self._planted_tool(real_dir)
        canonical_dir = os.path.realpath(real_dir)  # before realpath is broken below
        monkeypatch.setattr(sandbox.os.path, "realpath", lambda path, **_kwargs: os.fspath(path))
        descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
        spawn = AsyncMock()
        try:
            with patch("asyncio.create_subprocess_exec", spawn):
                await create_subprocess_limited(
                    "mytool",
                    chdir_fd=descriptor,
                    env={"PATH": os.pathsep.join([str(alias), str(real_dir)])},
                )
        finally:
            os.close(descriptor)
        assert strip_spawn_shim(spawn.await_args.args) == (os.path.join(canonical_dir, "mytool"),)
        assert spawn.await_args.kwargs["env"]["PATH"] == canonical_dir

    @pytest.mark.asyncio
    async def test_chdir_fd_with_only_workspace_path_entries_resolves_nothing(self, tmp_path):
        """The identity screen emptying PATH fails closed, exactly like the lexical one."""
        workspace = tmp_path / "pinned"
        self._planted_tool(workspace / "bin")
        descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with patch("asyncio.create_subprocess_exec", AsyncMock()):
                with pytest.raises(FileNotFoundError):
                    await create_subprocess_limited(
                        "mytool",
                        chdir_fd=descriptor,
                        env={"PATH": os.pathsep.join([str(workspace), str(workspace / "bin")])},
                    )
        finally:
            os.close(descriptor)

    @pytest.mark.asyncio
    async def test_chdir_fd_drops_a_path_entry_that_cannot_be_opened(self, tmp_path):
        """An unopenable entry is dropped, never kept.

        It cannot contribute a resolvable binary today, and dropping is the
        direction that cannot be gamed by making a directory un-``stat``-able.
        Covers both a missing entry (ENOENT) and a file where a directory
        should be (ENOTDIR).
        """
        workspace = tmp_path / "pinned"
        workspace.mkdir()
        missing = tmp_path / "missing"
        not_a_dir = tmp_path / "not-a-dir"
        not_a_dir.write_text("")
        real_dir = tmp_path / "real-bin"
        self._planted_tool(real_dir)
        canonical_dir = os.path.realpath(real_dir)
        descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
        spawn = AsyncMock()
        try:
            with patch("asyncio.create_subprocess_exec", spawn):
                await create_subprocess_limited(
                    "mytool",
                    chdir_fd=descriptor,
                    env={"PATH": os.pathsep.join([str(missing), str(not_a_dir), str(real_dir)])},
                )
        finally:
            os.close(descriptor)
        assert strip_spawn_shim(spawn.await_args.args) == (os.path.join(canonical_dir, "mytool"),)
        assert spawn.await_args.kwargs["env"]["PATH"] == canonical_dir

    @pytest.mark.asyncio
    async def test_chdir_fd_respells_a_kept_entry_from_the_verified_descriptor(self, tmp_path):
        """A kept entry is the OPENED descriptor's canonical path, not the spelling.

        The child re-resolves its ``PATH`` strings at its own lookup, so a kept
        spelling that traverses a symlink could be retargeted between this
        screen and that lookup -- pointing the child at a directory the screen
        never verified. Emitting the descriptor-derived path pins the child to
        the identity that passed the screen.
        """
        workspace = tmp_path / "pinned"
        workspace.mkdir()
        real_dir = tmp_path / "real-bin"
        self._planted_tool(real_dir)
        canonical_dir = os.path.realpath(real_dir)
        link = tmp_path / "mutable-link"
        os.symlink(real_dir, link)
        descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
        spawn = AsyncMock()
        try:
            with patch("asyncio.create_subprocess_exec", spawn):
                await create_subprocess_limited(
                    "mytool", chdir_fd=descriptor, env={"PATH": str(link)}
                )
        finally:
            os.close(descriptor)
        # Both consumers see the canonical directory; the mutable spelling is gone.
        assert spawn.await_args.kwargs["env"]["PATH"] == canonical_dir
        assert strip_spawn_shim(spawn.await_args.args) == (os.path.join(canonical_dir, "mytool"),)

    @pytest.mark.asyncio
    async def test_chdir_fd_drops_an_entry_whose_canonical_path_is_unreadable(self, tmp_path):
        """Unresolvable canonical path => the entry is dropped, never the raw spelling.

        Falling back to the caller's spelling would reopen the retarget window
        the re-spelling exists to close, so the degrade direction is DROP --
        here that empties ``PATH`` and the resolve fails closed.
        """
        workspace = tmp_path / "pinned"
        workspace.mkdir()
        real_dir = tmp_path / "real-bin"
        self._planted_tool(real_dir)
        descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with (
                patch("asyncio.create_subprocess_exec", AsyncMock()),
                patch("kiro_crew.hooks._fd_real_path", return_value=None),
            ):
                with pytest.raises(FileNotFoundError):
                    await create_subprocess_limited(
                        "mytool", chdir_fd=descriptor, env={"PATH": str(real_dir)}
                    )
        finally:
            os.close(descriptor)

    @pytest.mark.asyncio
    async def test_an_unreadable_bound_descriptor_degrades_to_the_lexical_screen(self, tmp_path):
        """No bound identity to compare against => the absolute-only screen stands.

        This is the deliberate degrade that keeps the placeholder-descriptor
        tests above meaningful (a mocked spawn never opens fd 9), and it must
        not silently become fail-open: relative entries still drop, absolute
        entries are kept without an identity walk. A descriptor number at the
        NOFILE soft limit can never be open, so the ``fstat`` failure is
        deterministic -- and in production such a descriptor is one the shim's
        own ``fchdir`` rejects before any command runs.
        """
        never_open = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        real_dir = tmp_path / "real-bin"
        real = self._planted_tool(real_dir)
        spawn = AsyncMock()
        with patch("asyncio.create_subprocess_exec", spawn):
            await create_subprocess_limited(
                "mytool",
                chdir_fd=never_open,
                env={"PATH": os.pathsep.join(["tools", ".", "", str(real_dir)])},
            )
        assert strip_spawn_shim(spawn.await_args.args) == (str(real),)
        assert spawn.await_args.kwargs["env"]["PATH"] == str(real_dir)

    @pytest.mark.asyncio
    async def test_an_unpinned_spawn_env_is_byte_identical(self):
        """No screen of any kind runs without a pin -- the whole env passes through."""
        env = {"PATH": "tools:.:/usr/bin", "HOME": "/home/someone"}
        spawn = AsyncMock()
        with patch("asyncio.create_subprocess_exec", spawn):
            await create_subprocess_limited("/bin/true", env=dict(env))
        assert spawn.await_args.kwargs["env"] == env

    @pytest.mark.asyncio
    async def test_pinned_screen_and_resolve_run_off_the_event_loop(self):
        """The identity walk opens PATH entries, so it must not run on the loop.

        Same hazard as the resolve's own stats: one stalled NFS/autofs entry
        would freeze the gateway. The screen and the resolve share a single
        worker-thread hop, which also pins that a pinned EXPLICIT-path spawn
        takes the hop -- its child env still needs screening (clause (c)).
        """
        loop_thread = threading.get_ident()
        ran_on: list[int] = []

        def spy(env, *, chdir_fd=None):
            ran_on.append(threading.get_ident())
            return {"PATH": "/usr/bin"}

        with (
            patch("asyncio.create_subprocess_exec", AsyncMock()),
            patch.object(sandbox, "_pinned_spawn_path", spy),
        ):
            await create_subprocess_limited("/bin/true", chdir_fd=9)
        assert ran_on and ran_on[0] != loop_thread

    @pytest.mark.asyncio
    async def test_an_unpinned_spawn_still_resolves_a_relative_path_entry(self, tmp_path):
        """The refusal is scoped to a pinned spawn; ordinary callers are unchanged."""
        tools = tmp_path / "tools"
        tools.mkdir()
        tool = tools / "mytool"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
        spawn = AsyncMock()
        with patch("asyncio.create_subprocess_exec", spawn):
            await create_subprocess_limited("mytool", cwd=str(tmp_path), env={"PATH": "tools"})
        assert strip_spawn_shim(spawn.await_args.args) == (str(tool),)
        assert spawn.await_args.kwargs["cwd"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_chdir_fd_is_refused_when_no_shim_can_carry_it(self):
        """Downgrading to cwd's pathname would reopen the window the pin closes."""
        with (
            patch.object(sandbox, "_SPAWN_SHIM_CODE", ""),
            patch("asyncio.create_subprocess_exec", AsyncMock()),
        ):
            with pytest.raises(RuntimeError, match="descriptor-pinned"):
                await create_subprocess_limited("/bin/true", chdir_fd=9)

    @pytest.mark.asyncio
    async def test_chdir_fd_does_not_leak_into_the_cached_profile_prefix(self):
        """The prefix is cached per profile; a descriptor belongs to one spawn."""
        spawn = AsyncMock()
        with patch("asyncio.create_subprocess_exec", spawn):
            await create_subprocess_limited("/bin/true", chdir_fd=9)
            first = spawn.await_args.args
            await create_subprocess_limited("/bin/true")
            second = spawn.await_args.args
        assert "--chdir-fd=9" in first
        assert not any(item.startswith("--chdir-fd=") for item in second)

    @pytest.mark.asyncio
    async def test_forwards_every_other_keyword_untouched(self):
        spawn = AsyncMock()
        with patch("asyncio.create_subprocess_exec", spawn):
            await create_subprocess_limited(
                "/bin/true", cwd="/tmp", env={"A": "1"}, start_new_session=True
            )
        kwargs = spawn.await_args.kwargs
        assert kwargs["cwd"] == "/tmp"
        assert kwargs["env"] == {"A": "1"}
        assert kwargs["start_new_session"] is True


# --------------------------------------------------------------------------
# Descriptor-pinned working directory (macOS workspace binding)
# --------------------------------------------------------------------------


@posix_only
class TestDescriptorPinnedWorkingDirectory:
    """The child lands in the directory the parent VERIFIED, not in a name.

    The regression this pins reached a packaged build: the verified descriptor was
    handed to the spawn as ``cwd="/dev/fd/<n>"``. Linux publishes those entries as
    symlinks to the target, so it happened to work there; macOS -- the only
    platform that binds a workspace at all -- fails ``chdir()`` on them with
    EACCES, so every agent spawn died with
    ``PermissionError: [Errno 13] Permission denied: '/dev/fd/25'``.
    """

    @pytest.mark.asyncio
    async def test_child_enters_the_pinned_directory_even_after_a_retarget(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        decoy = tmp_path / "decoy"
        decoy.mkdir()
        name = tmp_path / "workspace"
        name.symlink_to(real)

        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            # The same-UID retarget the binding exists to survive: it lands
            # between the parent's check and the child's own resolution.
            name.unlink()
            name.symlink_to(decoy)
            proc = await create_subprocess_limited(
                sys.executable,
                "-I",
                "-S",
                "-c",
                "import os; print(os.getcwd())",
                stdout=asyncio.subprocess.PIPE,
                cwd=str(name),
                chdir_fd=descriptor,
                profile=RLIMIT_PROFILE_SESSION_HOST,
            )
            stdout, _ = await proc.communicate()
        finally:
            os.close(descriptor)

        assert proc.returncode == 0
        assert stdout.decode().strip() == os.path.realpath(str(real))

    @pytest.mark.asyncio
    async def test_child_still_starts_when_the_name_is_gone_after_the_bind(self, tmp_path):
        """The pinned descriptor must be the ONLY thing that decides the cwd.

        Popen chdirs to ``cwd`` before exec'ing the shim, so a pathname that stopped
        naming a directory after the bind used to fail the spawn outright -- the
        descriptor was never reached.
        """
        real = tmp_path / "real"
        real.mkdir()
        name = tmp_path / "workspace"
        name.symlink_to(real)

        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            # Not merely retargeted: gone. A pathname-based chdir has nothing left
            # to resolve, while the descriptor still names the same directory.
            name.unlink()
            proc = await create_subprocess_limited(
                sys.executable,
                "-I",
                "-S",
                "-c",
                "import os; print(os.getcwd())",
                stdout=asyncio.subprocess.PIPE,
                cwd=str(name),
                chdir_fd=descriptor,
                profile=RLIMIT_PROFILE_SESSION_HOST,
            )
            stdout, _ = await proc.communicate()
        finally:
            os.close(descriptor)

        assert proc.returncode == 0
        assert stdout.decode().strip() == os.path.realpath(str(real))

    @pytest.mark.asyncio
    async def test_the_pinned_descriptor_is_not_inherited_by_the_command(self, tmp_path):
        """The shim closes it once it stands in the directory."""
        descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        probe = (
            "import os\n"
            "try:\n"
            f"    os.fstat({descriptor})\n"
            "except OSError:\n"
            "    print('closed')\n"
            "else:\n"
            "    print('inherited')\n"
        )
        try:
            proc = await create_subprocess_limited(
                sys.executable,
                "-I",
                "-S",
                "-c",
                probe,
                stdout=asyncio.subprocess.PIPE,
                chdir_fd=descriptor,
                profile=RLIMIT_PROFILE_SESSION_HOST,
            )
            stdout, _ = await proc.communicate()
        finally:
            os.close(descriptor)

        assert stdout.decode().strip() == "closed"


# --------------------------------------------------------------------------
# The synchronous siblings
# --------------------------------------------------------------------------


@posix_only
class TestSyncLimitedSpawns:
    def test_never_passes_a_fork_child_callable(self):
        """The regression guard: a callable here forks the threaded gateway."""
        with patch("subprocess.run") as spawn:
            run_limited(["/bin/true"])
        assert spawn.call_args.kwargs["preexec_fn"] is None
        assert strip_spawn_shim(spawn.call_args.args[0]) == ("/bin/true",)

        with patch("subprocess.Popen") as spawn:
            popen_limited(["/bin/true"])
        assert spawn.call_args.kwargs["preexec_fn"] is None
        assert strip_spawn_shim(spawn.call_args.args[0]) == ("/bin/true",)

    def test_the_shim_prefix_is_prepended_when_one_is_available(self):
        """The whole point: the command is reached through the shim, not directly."""
        prefix = spawn_shim_argv()
        assert prefix, "no shim prefix on this host — the rest of this test is vacuous"
        with patch("subprocess.run") as spawn:
            run_limited(["/bin/true", "arg"])
        argv = spawn.call_args.args[0]
        assert tuple(argv[: len(prefix)]) == prefix
        assert strip_spawn_shim(argv) == ("/bin/true", "arg")

    @pytest.mark.parametrize("call", [run_limited, popen_limited])
    def test_refuses_a_caller_supplied_preexec_fn(self, call):
        with pytest.raises(TypeError, match="owns preexec_fn"):
            call(["/bin/true"], preexec_fn=lambda: None)

    @pytest.mark.parametrize("call", [run_limited, popen_limited])
    def test_refuses_shell_true(self, call):
        """A shell command is one string, so there is nowhere to put the prefix."""
        with pytest.raises(TypeError, match="shell=True"):
            call("true", shell=True)

    @pytest.mark.parametrize("call", [run_limited, popen_limited])
    def test_requires_a_command(self, call):
        with pytest.raises(ValueError):
            call([])

    def test_bare_name_is_resolved_against_the_child_path(self):
        true_path = shutil.which("true")
        if not true_path:
            pytest.skip("no `true` binary on PATH")
        with patch("subprocess.run") as spawn:
            run_limited(["true"], env={"PATH": os.path.dirname(true_path)})
        # The shim execs without a PATH search, so the parent must hand it a path.
        assert strip_spawn_shim(spawn.call_args.args[0])[0].endswith("/true")

    def test_missing_command_still_raises_filenotfound_at_the_spawn(self):
        with patch("subprocess.run"):
            with pytest.raises(FileNotFoundError):
                run_limited(["kirocrew-no-such-command"], env={"PATH": "/nonexistent"})

    def test_path_search_runs_inline_not_on_a_worker_thread(self):
        """A sync caller is already off the event loop, so a thread hop buys nothing."""
        caller = threading.get_ident()
        ran_on: list[int] = []

        def spy(argv, env, cwd=None):
            ran_on.append(threading.get_ident())
            return "/bin/true"

        with (
            patch("subprocess.run"),
            patch.object(sandbox, "_resolve_spawn_target", spy),
        ):
            run_limited(["true"])
        assert ran_on == [caller]

    def test_explicit_path_takes_no_resolution(self):
        with (
            patch("subprocess.run") as spawn,
            patch.object(
                sandbox, "_resolve_spawn_target", side_effect=AssertionError("resolved a path")
            ),
        ):
            run_limited(["/nonexistent/tool"], cwd="/tmp")
        assert strip_spawn_shim(spawn.call_args.args[0]) == ("/nonexistent/tool",)

    @pytest.mark.parametrize(
        ("call", "target"), [(run_limited, "subprocess.run"), (popen_limited, "subprocess.Popen")]
    )
    def test_falls_back_to_preexec_rather_than_dropping_the_limits(self, call, target):
        """A truncated install must not silently spawn children uncapped."""
        with (
            patch.object(sandbox, "_SPAWN_SHIM_CODE", ""),
            patch(target) as spawn,
        ):
            call(["/bin/true"])
        assert callable(spawn.call_args.kwargs["preexec_fn"])
        assert spawn.call_args.args[0] == ["/bin/true"]

    def test_a_policy_free_profile_drops_both_the_shim_and_the_callable(self):
        """The negative control for the fallback: nothing to apply, nothing applied."""
        with patch("subprocess.run") as spawn:
            run_limited(["/bin/true"], profile=RLIMIT_PROFILE_NONE)
        assert spawn.call_args.kwargs["preexec_fn"] is None
        assert spawn.call_args.args[0] == ["/bin/true"]

    def test_forwards_every_other_keyword_untouched(self):
        with patch("subprocess.run") as spawn:
            run_limited(["/bin/true"], cwd="/tmp", env={"A": "1"}, timeout=5, check=True)
        kwargs = spawn.call_args.kwargs
        assert kwargs["cwd"] == "/tmp"
        assert kwargs["env"] == {"A": "1"}
        assert kwargs["timeout"] == 5
        assert kwargs["check"] is True


@posix_only
class TestSyncReportedArgv:
    """The shim source rides in argv as a ~8 KB ``-c`` string.

    ``CompletedProcess.args`` and both failure exceptions render that argv into
    their message, so reporting the spawned form would put the whole shim into
    every failure log line. Each test here pins BOTH halves -- that the shim IS
    in the argv actually spawned, and that it is NOT in what gets reported -- so
    none of them can pass by the shim quietly ceasing to be prepended.
    """

    # The regression these guards pin was an 8815-char message that was nearly
    # all shim source. A generous cap still catches that leak while a clean,
    # shim-free message can never trip it just because the interpreter path is
    # long (a deep CI worktree or venv pushes a bare ``sys.executable`` message
    # past a tight 200-char bound with zero code signal).
    _MAX_REPORTED_MESSAGE_LEN = 1000

    def _shim_is_prepended(self, spawned: "list[str]") -> bool:
        return len(strip_spawn_shim(spawned)) < len(spawned)

    def test_completed_process_reports_the_command_not_the_shim(self):
        result = run_limited(["/bin/echo", "hi"], capture_output=True, text=True)
        assert result.args == ["/bin/echo", "hi"]
        assert not any("--rlimits=" in a for a in result.args)

    def test_called_process_error_names_the_command_not_the_shim(self):
        # A real exit(1), not `/bin/false`: that path is Linux-only (macOS ships
        # `false`/`true` under /usr/bin, not /bin), so a hardcoded `/bin/false`
        # made execv fail with ENOENT on macOS -- caught by the shim's own
        # `except OSError`, which reports EXEC_FAILED (127), not the command's
        # own exit status. sys.executable is the portable equivalent already
        # used throughout this file for a real spawned child.
        cmd = [sys.executable, "-c", "import sys;sys.exit(1)", "x"]
        with pytest.raises(subprocess.CalledProcessError) as caught:
            run_limited(cmd, check=True, capture_output=True)
        assert caught.value.cmd == cmd
        assert caught.value.returncode == 1
        assert len(str(caught.value)) < self._MAX_REPORTED_MESSAGE_LEN

    def test_timeout_expired_names_the_command_not_the_shim(self):
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            run_limited([sys.executable, "-c", "import time;time.sleep(30)"], timeout=0.3)
        assert caught.value.cmd == [sys.executable, "-c", "import time;time.sleep(30)"]
        assert len(str(caught.value)) < self._MAX_REPORTED_MESSAGE_LEN

    def test_popen_communicate_timeout_names_the_command_not_the_shim(self):
        """``communicate(timeout=...)`` builds TimeoutExpired from ``Popen.args``."""
        proc = popen_limited(
            [sys.executable, "-c", "import time;time.sleep(30)"], stdout=subprocess.PIPE
        )
        try:
            assert proc.args == [sys.executable, "-c", "import time;time.sleep(30)"]
            with pytest.raises(subprocess.TimeoutExpired) as caught:
                proc.communicate(timeout=0.3)
            assert len(str(caught.value)) < self._MAX_REPORTED_MESSAGE_LEN
        finally:
            proc.kill()
            proc.wait()

    def test_the_shim_is_still_in_the_argv_that_was_spawned(self):
        """The other half: rewriting what is REPORTED must not stop the wrapping."""
        with patch("subprocess.run") as spawn:
            run_limited(["/bin/true"])
        assert self._shim_is_prepended(list(spawn.call_args.args[0]))
        with patch("subprocess.Popen") as spawn:
            popen_limited(["/bin/true"])
        assert self._shim_is_prepended(list(spawn.call_args.args[0]))


@posix_only
class TestSyncRealChild:
    def test_child_is_capped_and_is_its_own_process(self):
        probe = (
            "import os,resource,sys;"
            "print(resource.getrlimit(resource.RLIMIT_NOFILE)[0], os.getpid(),"
            " os.environ.get('KC_PROBE',''), *sys.argv[1:])"
        )
        proc = popen_limited(
            [sys.executable, "-c", probe, "tail-arg"],
            stdout=subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", ""), "KC_PROBE": "kept"},
        )
        out, _ = proc.communicate()
        soft, pid, marker, tail = out.decode().split()
        # The limit really bound the exec'd image...
        assert int(soft) <= 65536
        # ...the shim exec'd in place, so the PID the caller holds is the child's own...
        assert int(pid) == proc.pid
        # ...and the environment and trailing argv passed through untouched.
        assert marker == "kept"
        assert tail == "tail-arg"

    def test_the_cap_is_lower_than_an_unwrapped_spawn(self):
        """Negative control: without the wrapper the child inherits the gateway's soft limit."""
        # The tool profile's cap is a fixed target (`_RLIMIT_DEFAULTS
        # ["max_open_files"]` in security.py == 1024), not "whatever is lower
        # than inherited". This negative control only proves anything when the
        # inherited soft limit starts out ABOVE that cap -- and macOS's own
        # per-process default can already sit AT or BELOW 1024 (the classic
        # 256 login default), in which case an unwrapped child reports <=1024
        # too and the assertion fails for a reason that has nothing to do with
        # the shim. Same host-variance handling as
        # test_session_host_child_gets_headroom_not_the_tool_cap: read the real
        # limits and, if needed, raise this process's own soft limit above the
        # cap first so the comparison is meaningful on any host.
        default_cap = 1024
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard != resource.RLIM_INFINITY and hard <= default_cap:
            pytest.skip(f"host hard NOFILE limit ({hard}) is at or below the tool cap")
        raised = soft <= default_cap
        if raised:
            resource.setrlimit(resource.RLIMIT_NOFILE, (default_cap + 1, hard))
        try:
            probe = "import resource;print(resource.getrlimit(resource.RLIMIT_NOFILE)[0])"
            wrapped = run_limited(
                [sys.executable, "-c", probe], capture_output=True, text=True
            ).stdout.strip()
            bare = subprocess.run(
                [sys.executable, "-c", probe], capture_output=True, text=True
            ).stdout.strip()
            inherited = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
            assert int(bare) == inherited
            assert int(wrapped) < int(
                bare
            ), f"wrapped child got {wrapped}, unwrapped got {bare} — the cap did nothing"
        finally:
            if raised:
                resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))

    def test_exit_status_belongs_to_the_command(self):
        assert run_limited([sys.executable, "-c", "raise SystemExit(42)"]).returncode == 42


# --------------------------------------------------------------------------
# End to end against a real child
# --------------------------------------------------------------------------


@posix_only
class TestRealChild:
    @pytest.mark.asyncio
    async def test_child_is_capped_and_is_its_own_process(self):
        probe = (
            "import os,resource,sys;"
            "print(resource.getrlimit(resource.RLIMIT_NOFILE)[0], os.getpid(),"
            " os.environ.get('KC_PROBE',''), *sys.argv[1:])"
        )
        proc = await create_subprocess_limited(
            sys.executable,
            "-c",
            probe,
            "tail-arg",
            stdout=asyncio.subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", ""), "KC_PROBE": "kept"},
        )
        out, _ = await proc.communicate()
        soft, pid, marker, tail = out.decode().split()
        # The limit really bound the exec'd image...
        assert int(soft) <= 65536
        # ...the shim exec'd in place, so the PID the caller holds is the child's
        # own (kill_process_tree and signal delivery still work)...
        assert int(pid) == proc.pid
        # ...and the environment and trailing argv passed through untouched.
        assert marker == "kept"
        assert tail == "tail-arg"

    @pytest.mark.asyncio
    async def test_exit_status_and_signals_belong_to_the_command(self):
        proc = await create_subprocess_limited(sys.executable, "-c", "raise SystemExit(42)")
        assert await proc.wait() == 42

        proc = await create_subprocess_limited(sys.executable, "-c", "import time;time.sleep(30)")
        await asyncio.sleep(0.5)
        proc.kill()
        assert await proc.wait() == -9

    @pytest.mark.asyncio
    async def test_session_host_child_gets_headroom_not_the_tool_cap(self):
        probe = "import resource;print(resource.getrlimit(resource.RLIMIT_NOFILE)[0])"
        proc = await create_subprocess_limited(
            sys.executable,
            "-c",
            probe,
            profile=RLIMIT_PROFILE_SESSION_HOST,
            stdout=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        gateway_soft, gateway_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if gateway_hard == resource.RLIM_INFINITY:
            # The shim raises the soft limit to the 65536 floor but must never
            # LOWER an already-higher inherited soft limit (`max(soft, floor)`),
            # so on a host whose soft limit already exceeds the floor the child
            # keeps that value. Asserting a flat 65536 here failed on exactly
            # such a host (macOS with `ulimit -Sn 1048576`) even though the shim
            # behaved correctly.
            expected = max(gateway_soft, 65536)
        else:
            expected = gateway_hard
        assert int(out.decode().strip()) == expected
