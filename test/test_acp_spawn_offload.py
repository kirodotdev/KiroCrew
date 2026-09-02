"""Tests pinning the off-loop offload of the ACP spawn filesystem work.

The spawn prelude performs synchronous filesystem syscalls whose latency
scales with the ``/tmp`` entry count (``_resolve_ssh_auth_sock`` globs
``/tmp/ssh-*/agent.*`` + stats each match; ``resolve_krb5_ccname`` lstat/stats
``/tmp/krb5cc_<uid>``) plus a ``mkdir`` of the work dir. None of these may run
on the asyncio event loop: a blocking call there stalls every other task,
including the watchdog heartbeat. These tests pin four contracts:

1. ``AcpClient._spawn`` runs both env resolvers and the work-dir mkdir on a
   non-loop thread (one bundled thread hop for the resolvers).
2. ``AcpClient.ensure_ready`` — which runs before EVERY prompt — performs at
   most ONE mkdir, on the first call per instance and off-loop; every later
   call performs none. (``_spawn`` also creates the dir, and ``_reset_state``
   clears the process and session id together, so every session-init path
   re-enters ``_spawn`` first.)
3. ``AcpRuntime.spawn`` runs its mkdir and ``resolve_krb5_ccname`` on a
   non-loop thread.
4. ``AcpClient._spawn`` runs the PID-file tracking writes (``_track_pid``,
   ``_track_session_pid``) on a non-loop thread — each takes an exclusive
   file lock and writes under it, so an on-loop call serializes concurrent
   spawns behind the lock with the waiter holding the loop.
"""

from __future__ import annotations

import asyncio
import threading
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.acp.client as client_mod
import kiro_crew.acp.runtime as runtime_mod
from kiro_crew.acp.client import AcpClient, _resolve_spawn_env
from kiro_crew.acp.runtime import AcpRuntime


async def _stop_stderr_drain(client: AcpClient) -> None:
    """Cancel and await the stderr-drain task a mocked _spawn started.

    A mock process has a truthy stderr, so _spawn starts _drain_stderr over it;
    left alive, its exception surfaces against an unrelated test at collection.
    """
    task = client._stderr_task
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    client._stderr_task = None


class TestResolveSpawnEnv:
    def test_bundles_both_resolvers_and_returns_env(self) -> None:
        env = {"PATH": "/usr/bin"}
        with (
            patch.object(client_mod, "_resolve_ssh_auth_sock") as ssh,
            patch.object(client_mod, "resolve_krb5_ccname") as krb,
        ):
            result = _resolve_spawn_env(env)
        ssh.assert_called_once_with(env)
        krb.assert_called_once_with(env)
        assert result is env


class TestClientSpawnOffLoop:
    @pytest.mark.asyncio
    async def test_spawn_env_resolution_and_mkdir_run_off_loop(self, tmp_path) -> None:
        loop_thread = threading.current_thread()
        ssh_threads: list[threading.Thread] = []
        krb_threads: list[threading.Thread] = []
        cgroup_threads: list[threading.Thread] = []
        xdist_threads: list[threading.Thread] = []
        mkdir_threads: list[threading.Thread] = []

        # On-loop mkdir failures capture the offending stack: the spawn path
        # has several lazy, cache-cold callees (config reads, probes), so the
        # thread identity alone does not name the regressing call site.
        mkdir_stacks: list[str] = []

        def _rec_mkdir(*a, **kw):
            t = threading.current_thread()
            mkdir_threads.append(t)
            if t is loop_thread:
                mkdir_stacks.append("".join(traceback.format_stack()))

        client = AcpClient(work_dir=tmp_path / "workspace", session_key="k")

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.returncode = None

        with (
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch.object(client_mod, "ensure_agent_materialized"),
            patch(
                "kiro_crew.acp.client.wrap_argv",
                return_value=(["/usr/bin/kiro-cli", "acp"], None),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=mock_proc,
            ),
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
            # PID 12345 may be a real host process; without this, the early
            # descendant scan can find its children and _track_child_pids then
            # writes tracking state (mkdir included) on the loop thread —
            # host-dependent noise this test must not observe.
            patch.object(client_mod, "_get_child_pids", return_value=[]),
            patch.object(
                client_mod,
                "_resolve_ssh_auth_sock",
                side_effect=lambda env: ssh_threads.append(threading.current_thread()),
            ),
            patch.object(
                client_mod,
                "resolve_krb5_ccname",
                side_effect=lambda env: krb_threads.append(threading.current_thread()),
            ),
            # cgroup_scope_argv's first call probes /proc + /sys and reads the
            # config (mkdir + file IO) — record its thread directly so the
            # assertion does not depend on this host's cgroup delegation.
            patch.object(
                client_mod,
                "cgroup_scope_argv",
                side_effect=lambda argv: (
                    cgroup_threads.append(threading.current_thread()),
                    argv,
                )[1],
            ),
            # inject_xdist_auto_cap resolves its cap from the raw config, and
            # that read enters config_dir() (mkdir + file IO) — record its
            # thread directly so the assertion does not depend on whether the
            # host env already carries PYTEST_XDIST_AUTO_NUM_WORKERS (which
            # would short-circuit the config read).
            patch.object(
                client_mod,
                "inject_xdist_auto_cap",
                side_effect=lambda env: xdist_threads.append(threading.current_thread()),
            ),
            patch(
                "pathlib.Path.mkdir",
                side_effect=_rec_mkdir,
            ),
        ):
            await client._spawn()

        await _stop_stderr_drain(client)

        assert ssh_threads, "_resolve_ssh_auth_sock must run during _spawn"
        assert krb_threads, "resolve_krb5_ccname must run during _spawn"
        assert cgroup_threads, "cgroup_scope_argv must run during _spawn"
        assert xdist_threads, "inject_xdist_auto_cap must run during _spawn"
        assert mkdir_threads, "the work-dir mkdir must run during _spawn"
        for t in ssh_threads:
            assert t is not loop_thread, "ssh resolver ran on the loop thread"
        for t in krb_threads:
            assert t is not loop_thread, "krb5 resolver ran on the loop thread"
        for t in cgroup_threads:
            assert t is not loop_thread, "cgroup_scope_argv ran on the loop thread"
        for t in xdist_threads:
            assert t is not loop_thread, "inject_xdist_auto_cap ran on the loop thread"
        for t in mkdir_threads:
            assert t is not loop_thread, "mkdir ran on the loop thread:\n" + "\n".join(mkdir_stacks)


class TestClientSpawnPidTrackingOffLoop:
    @pytest.mark.asyncio
    async def test_pid_tracking_runs_off_loop(self, tmp_path) -> None:
        """_track_pid / _track_session_pid take an exclusive file lock and
        write under it; ensure_ready awaits _spawn from the loop, so an
        on-loop tracker blocks every task while the lock is contended."""
        loop_thread = threading.current_thread()
        track_threads: list[threading.Thread] = []
        session_track_threads: list[threading.Thread] = []

        client = AcpClient(work_dir=tmp_path / "workspace", session_key="k")

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.returncode = None

        with (
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch.object(client_mod, "ensure_agent_materialized"),
            patch(
                "kiro_crew.acp.client.wrap_argv",
                return_value=(["/usr/bin/kiro-cli", "acp"], None),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=mock_proc,
            ),
            patch(
                "kiro_crew.session._track_pid",
                side_effect=lambda pid: track_threads.append(threading.current_thread()),
            ),
            patch(
                "kiro_crew.session._track_session_pid",
                side_effect=lambda pid: session_track_threads.append(
                    threading.current_thread()
                ),
            ),
            # PID 12345 may be a real host process; an empty scan keeps the
            # early-descendant branch (and its own tracking write) out of
            # this test's observations.
            patch.object(client_mod, "_get_child_pids", return_value=[]),
        ):
            await client._spawn()

        await _stop_stderr_drain(client)

        assert track_threads, "_track_pid must run during _spawn"
        assert session_track_threads, "_track_session_pid must run during _spawn"
        for t in track_threads:
            assert t is not loop_thread, "_track_pid ran on the loop thread"
        for t in session_track_threads:
            assert t is not loop_thread, "_track_session_pid ran on the loop thread"


class TestEnsureReadyWorkDir:
    @pytest.mark.asyncio
    async def test_work_dir_created_once_off_loop_then_never_again(self, tmp_path) -> None:
        """ensure_ready runs before EVERY prompt; the work-dir check must pay
        one off-loop mkdir on the FIRST call and no syscall afterwards —
        restoring a per-prompt mkdir fails this test."""
        loop_thread = threading.current_thread()
        client = AcpClient(work_dir=tmp_path / "workspace", session_key="k")
        proc = MagicMock()
        proc.returncode = None
        client._process = proc
        client._session_id = "sess-1"

        mkdir_threads: list[threading.Thread] = []
        with patch(
            "pathlib.Path.mkdir",
            side_effect=lambda *a, **kw: mkdir_threads.append(threading.current_thread()),
        ):
            await client.ensure_ready()
            assert len(mkdir_threads) == 1, "first ensure_ready must create the work dir"
            assert mkdir_threads[0] is not loop_thread, "work-dir mkdir ran on the loop thread"

            for _ in range(3):
                await client.ensure_ready()
        assert len(mkdir_threads) == 1, "warm ensure_ready must perform no mkdir"


class TestRuntimeSpawnOffLoop:
    @pytest.mark.asyncio
    async def test_spawn_mkdir_and_krb5_run_off_loop(self, tmp_path, monkeypatch) -> None:
        loop_thread = threading.current_thread()
        krb_threads: list[threading.Thread] = []
        cgroup_threads: list[threading.Thread] = []
        xdist_threads: list[threading.Thread] = []
        mkdir_threads: list[threading.Thread] = []

        class _StopSpawn(Exception):
            pass

        async def resolve_bin(*, environ=None, home=None) -> str:
            return "/usr/bin/kiro-cli"

        async def stop_spawn(*args, **kwargs):
            raise _StopSpawn()

        def _rec_cgroup(argv):
            cgroup_threads.append(threading.current_thread())
            return argv

        monkeypatch.setattr(runtime_mod, "_resolve_kiro_bin_for_spawn", resolve_bin)
        monkeypatch.setattr(runtime_mod, "ensure_agent_materialized", lambda agent: None)
        monkeypatch.setattr(
            runtime_mod, "wrap_argv", lambda argv, mode, **kw: (list(argv), None)
        )
        monkeypatch.setattr(runtime_mod, "cgroup_scope_argv", _rec_cgroup)
        monkeypatch.setattr(
            runtime_mod,
            "inject_xdist_auto_cap",
            lambda env: xdist_threads.append(threading.current_thread()),
        )
        monkeypatch.setattr(
            runtime_mod,
            "resolve_krb5_ccname",
            lambda env: krb_threads.append(threading.current_thread()),
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", stop_spawn)

        runtime = AcpRuntime(work_dir=tmp_path / "workspace")
        with (
            patch(
                "pathlib.Path.mkdir",
                side_effect=lambda *a, **kw: mkdir_threads.append(threading.current_thread()),
            ),
            pytest.raises(_StopSpawn),
        ):
            await runtime.spawn()

        assert krb_threads, "resolve_krb5_ccname must run during spawn"
        assert cgroup_threads, "cgroup_scope_argv must run during spawn"
        assert xdist_threads, "inject_xdist_auto_cap must run during spawn"
        assert mkdir_threads, "the work-dir mkdir must run during spawn"
        for t in krb_threads + cgroup_threads + xdist_threads + mkdir_threads:
            assert t is not loop_thread, "blocking spawn-prelude syscall ran on the loop thread"


class TestSpawnCancellationSandboxCleanup:
    """A cancellation landing in one of the offload hops AFTER ``wrap_argv``
    allocated the sandbox temp file must not orphan that file: nothing else
    unlinks it on the cancel path, and the next spawn reassigns
    ``_sandbox_cleanup``, leaking one file per cancelled attempt."""

    @staticmethod
    def _sandbox_file(tmp_path) -> str:
        f = tmp_path / "sandbox-profile.sb"
        f.write_text("(profile)")
        return str(f)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raise_in", ["cgroup", "env"])
    async def test_client_spawn_cancel_unlinks_sandbox_file(
        self, tmp_path, raise_in
    ) -> None:
        sandbox_file = self._sandbox_file(tmp_path)
        client = AcpClient(work_dir=tmp_path / "workspace", session_key="k")

        def _cgroup(argv):
            if raise_in == "cgroup":
                raise asyncio.CancelledError()
            return argv

        def _env(env, **_kwargs):
            raise asyncio.CancelledError()

        with (
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch.object(client_mod, "ensure_agent_materialized"),
            patch(
                "kiro_crew.acp.client.wrap_argv",
                return_value=(["/usr/bin/kiro-cli", "acp"], sandbox_file),
            ),
            patch.object(client_mod, "cgroup_scope_argv", side_effect=_cgroup),
            patch.object(client_mod, "_resolve_spawn_env", side_effect=_env),
            pytest.raises(asyncio.CancelledError),
        ):
            await client._spawn()

        assert not Path(sandbox_file).exists(), "cancelled spawn orphaned the sandbox file"
        assert client._sandbox_cleanup is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raise_in", ["cgroup", "krb5"])
    async def test_runtime_spawn_cancel_unlinks_sandbox_file(
        self, tmp_path, monkeypatch, raise_in
    ) -> None:
        sandbox_file = self._sandbox_file(tmp_path)

        async def resolve_bin(*, environ=None, home=None) -> str:
            return "/usr/bin/kiro-cli"

        def _cgroup(argv):
            if raise_in == "cgroup":
                raise asyncio.CancelledError()
            return argv

        def _krb5(env):
            raise asyncio.CancelledError()

        monkeypatch.setattr(runtime_mod, "_resolve_kiro_bin_for_spawn", resolve_bin)
        monkeypatch.setattr(runtime_mod, "ensure_agent_materialized", lambda agent: None)
        monkeypatch.setattr(
            runtime_mod,
            "wrap_argv",
            lambda argv, mode, **kw: (list(argv), sandbox_file),
        )
        monkeypatch.setattr(runtime_mod, "cgroup_scope_argv", _cgroup)
        monkeypatch.setattr(runtime_mod, "resolve_krb5_ccname", _krb5)

        runtime = AcpRuntime(work_dir=tmp_path / "workspace")
        with pytest.raises(asyncio.CancelledError):
            await runtime.spawn()

        assert not Path(sandbox_file).exists(), "cancelled spawn orphaned the sandbox file"
        assert runtime._sandbox_cleanup is None


class TestSpawnFailureCannotLeaveAnUntrackedProcess:
    """A LIVE subprocess that is absent from both PID files is unreachable by
    every agent-runtime reaper — ``cleanup_orphaned_sessions``,
    ``_periodic_pid_sweep`` and ``cleanup_orphaned_session_roots`` all read
    those files, and the ``/proc`` orphan scan declines managed agent runtimes
    on purpose (``_MANAGED_AGENT_MARKERS`` is a negative gate). So it holds its
    hundreds of MB until the host reboots.

    ``AcpClient._spawn`` reaches that state whenever anything in the window
    between the subprocess existing and the tracking appends completing raises:
    the exception unwinds out of ``_spawn`` with the process still running.
    ``ensure_ready``'s retry loop does not save it — that only catches
    ``AcpTimeoutError`` / ``AcpError``, and an ``OSError`` from one of these
    executor hops is neither.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raise_in",
        ["finish_suspended_spawn", "track_pid", "track_session_pid", "child_scan"],
    )
    async def test_client_spawn_kills_the_process_when_the_window_raises(
        self, tmp_path, raise_in
    ) -> None:
        client = AcpClient(work_dir=tmp_path / "workspace", session_key="k")

        mock_proc = MagicMock()
        mock_proc.pid = 4242
        mock_proc.returncode = None
        mock_proc.stderr = None

        boom = OSError("no space left on device")

        def _raise_if(name):
            def _inner(*_a, **_kw):
                if raise_in == name:
                    raise boom

            return _inner

        killed = AsyncMock()

        with (
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch.object(client_mod, "ensure_agent_materialized"),
            patch(
                "kiro_crew.acp.client.wrap_argv",
                return_value=(["/usr/bin/kiro-cli", "acp"], None),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=mock_proc,
            ),
            patch.object(
                client_mod,
                "finish_suspended_spawn",
                side_effect=_raise_if("finish_suspended_spawn"),
            ),
            patch.object(client_mod, "_get_start_time", return_value=1.0),
            patch("kiro_crew.session._track_pid", side_effect=_raise_if("track_pid")),
            patch(
                "kiro_crew.session._track_session_pid",
                side_effect=_raise_if("track_session_pid"),
            ),
            patch.object(client_mod, "_get_child_pids", side_effect=_raise_if("child_scan")),
            patch.object(client, "_kill_process", killed),
            pytest.raises(OSError),
        ):
            await client._spawn()

        await _stop_stderr_drain(client)

        # The process was live when the failure landed, so the ONLY correct
        # outcome is that _spawn reaps it before re-raising. Anything else is a
        # permanent leak with no log line and no reaper that can reach it.
        killed.assert_awaited_once_with(force=True)

    @pytest.mark.asyncio
    async def test_client_spawn_reports_the_failure_at_error(self, tmp_path, caplog) -> None:
        """The kill is silent to the user otherwise: nothing downstream sees a
        spawn that died after the process existed."""
        client = AcpClient(work_dir=tmp_path / "workspace", session_key="k")

        mock_proc = MagicMock()
        mock_proc.pid = 4243
        mock_proc.returncode = None
        mock_proc.stderr = None

        with (
            caplog.at_level("ERROR"),
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch.object(client_mod, "ensure_agent_materialized"),
            patch(
                "kiro_crew.acp.client.wrap_argv",
                return_value=(["/usr/bin/kiro-cli", "acp"], None),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=mock_proc,
            ),
            patch.object(client_mod, "finish_suspended_spawn"),
            patch.object(client_mod, "_get_start_time", return_value=1.0),
            patch("kiro_crew.session._track_pid"),
            patch(
                "kiro_crew.session._track_session_pid",
                side_effect=OSError("no space left on device"),
            ),
            patch.object(client_mod, "_get_child_pids", return_value=[]),
            patch.object(client, "_kill_process", AsyncMock()),
            pytest.raises(OSError),
        ):
            await client._spawn()

        await _stop_stderr_drain(client)
        assert any(
            r.levelname == "ERROR" and "4243" in r.getMessage() for r in caplog.records
        ), "a spawn that failed with a live process must say so at ERROR"


class TestRuntimeShieldSurvivesAFailedAppend:
    """``AcpRuntime.spawn`` shields its PID from the periodic orphan sweep with
    ``register_protected_pid`` — an in-memory set insert with no IO. Behind the
    two PID-file appends it was reachable only when BOTH succeeded, so a single
    failed append (ENOSPC, a wedged file lock) escalated into a LIVE runtime
    losing its shield and being SIGKILLed mid-use by the sweep the call exists
    to hide it from.

    The shield must therefore be registered BEFORE the appends, and a failed
    append must be reported at ERROR rather than swallowed at debug — that log
    line is the only signal the resulting leak will ever produce.
    """

    @staticmethod
    def _patch_prelude(monkeypatch, tmp_path, mock_proc):
        async def resolve_bin(*, environ=None, home=None) -> str:
            return "/usr/bin/kiro-cli"

        monkeypatch.setattr(runtime_mod, "_resolve_kiro_bin_for_spawn", resolve_bin)
        monkeypatch.setattr(runtime_mod, "ensure_agent_materialized", lambda agent: None)
        monkeypatch.setattr(runtime_mod, "wrap_argv", lambda argv, mode, **kw: (list(argv), None))
        monkeypatch.setattr(runtime_mod, "cgroup_scope_argv", lambda argv: list(argv))
        monkeypatch.setattr(runtime_mod, "resolve_krb5_ccname", lambda env: None)
        monkeypatch.setattr(runtime_mod, "inject_xdist_auto_cap", lambda env: None)
        monkeypatch.setattr(runtime_mod, "_get_start_time", lambda pid: 1.0)

        async def fake_spawn(*_a, **_kw):
            return mock_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raise_in", ["finish_suspended_spawn", "get_start_time"])
    async def test_runtime_spawn_reaps_the_process_when_the_window_raises(
        self, tmp_path, monkeypatch, raise_in
    ) -> None:
        """The sibling of the client-side window. ``finish_suspended_spawn``
        documents its own resume failure as FATAL and ``_get_start_time`` can
        raise, and every ``runtime.spawn()`` caller catches only
        ``AcpRuntimeError`` / ``AcpRuntimeDead`` -- so an ``OSError`` here used to
        propagate with a live, unrecorded process behind it."""
        mock_proc = MagicMock()
        mock_proc.pid = 5151
        mock_proc.returncode = None
        mock_proc.stderr = None
        mock_proc.stdout = None
        self._patch_prelude(monkeypatch, tmp_path, mock_proc)

        boom = OSError("resume failed")

        if raise_in == "finish_suspended_spawn":
            monkeypatch.setattr(
                runtime_mod,
                "finish_suspended_spawn",
                lambda *a, **kw: (_ for _ in ()).throw(boom),
            )
        else:
            monkeypatch.setattr(
                runtime_mod, "_get_start_time", lambda pid: (_ for _ in ()).throw(boom)
            )

        monkeypatch.setattr(runtime_mod, "register_protected_pid", lambda pid: None)
        monkeypatch.setattr(runtime_mod, "_track_pid", lambda pid: None)
        monkeypatch.setattr(runtime_mod, "_track_session_pid", lambda pid: None)

        reaped = AsyncMock()
        monkeypatch.setattr(AcpRuntime, "kill", reaped, raising=True)

        runtime = AcpRuntime(work_dir=tmp_path / "workspace")
        with pytest.raises(OSError):
            await runtime.spawn()

        reaped.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shield_is_registered_even_when_the_append_fails(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        class _StopSpawn(Exception):
            pass

        mock_proc = MagicMock()
        mock_proc.pid = 5150
        mock_proc.returncode = None
        mock_proc.stderr = None
        mock_proc.stdout = None
        self._patch_prelude(monkeypatch, tmp_path, mock_proc)

        protected: list[int] = []
        monkeypatch.setattr(runtime_mod, "register_protected_pid", protected.append)
        monkeypatch.setattr(
            runtime_mod,
            "_track_pid",
            lambda pid: (_ for _ in ()).throw(OSError("no space left on device")),
        )
        monkeypatch.setattr(runtime_mod, "_track_session_pid", lambda pid: None)

        runtime = AcpRuntime(work_dir=tmp_path / "workspace")

        # The reader loop asserts on a real stdout; this test is not about it,
        # and an unretrieved task exception would surface against an unrelated
        # test at collection.
        async def _no_reader(_self) -> None:
            return None

        monkeypatch.setattr(AcpRuntime, "_reader_loop", _no_reader, raising=True)
        # Stop at the handshake: everything under test has already run by then.
        monkeypatch.setattr(
            AcpRuntime, "_send_and_await", AsyncMock(side_effect=_StopSpawn()), raising=True
        )
        monkeypatch.setattr(AcpRuntime, "kill", AsyncMock(), raising=True)

        with caplog.at_level("ERROR"), pytest.raises(_StopSpawn):
            await runtime.spawn()

        assert protected == [5150], (
            "a failed PID-file append must not cost the live runtime its "
            "sweep shield — register_protected_pid has to run first"
        )
        assert any(
            r.levelname == "ERROR" and "5150" in r.getMessage() for r in caplog.records
        ), "a runtime that could not be recorded leaks silently unless this is an ERROR"
