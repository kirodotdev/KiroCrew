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

        async def resolve_bin() -> str:
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

        async def resolve_bin() -> str:
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
