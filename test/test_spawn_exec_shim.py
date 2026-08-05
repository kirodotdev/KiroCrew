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
