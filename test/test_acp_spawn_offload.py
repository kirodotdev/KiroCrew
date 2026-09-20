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
import json
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.acp.client as client_mod
import kiro_crew.acp.runtime as runtime_mod
from kiro_crew.acp.client import AcpClient, _resolve_spawn_env
from kiro_crew.acp.runtime import AcpRuntime


@pytest.fixture(autouse=True)
def _fake_process_admission(monkeypatch):
    # This file tests IO placement and lifecycle with fake subprocesses. Native
    # pin/admission contracts are exercised by test_windows_cleanup_capacity.
    async def create(factory):
        return await factory()

    monkeypatch.setattr(client_mod.platform_compat, "create_windows_cleanup_owned_process", create)


@pytest.fixture(autouse=True)
def _native_projection_for_fake_processes(monkeypatch):
    from kiro_crew.acp import skill_projection

    loop_thread = threading.current_thread()

    def prepare(work_dir):
        assert threading.current_thread() is not loop_thread
        return skill_projection.NativeSkillProjection({"kirocrew": "kirocrew-skill-view-test"})

    # Launch lifecycle tests own no native agent tree. Keep the projection hop
    # real and off-loop; its file and mapping behavior has separate coverage.
    monkeypatch.setattr(skill_projection, "prepare_native_skill_projection", prepare)


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

        # ``patch("pathlib.Path.mkdir")`` installs a plain MagicMock, which is not
        # a descriptor, so the recorder never receives ``self`` and cannot create
        # anything. autospec hands it the Path; calling through keeps the
        # directories the spawn prelude promises to create.
        real_mkdir = Path.mkdir

        def _rec_mkdir(self, *a, **kw):
            t = threading.current_thread()
            mkdir_threads.append(t)
            if t is loop_thread:
                mkdir_stacks.append("".join(traceback.format_stack()))
            return real_mkdir(self, *a, **kw)

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
            patch.object(
                Path,
                "mkdir",
                autospec=True,
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
                side_effect=lambda pid: session_track_threads.append(threading.current_thread()),
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

        # See the note in TestClientSpawnOffLoop: the recorder must receive
        # ``self`` and call through, or the work dir it claims to observe is
        # never created and the macOS-only spawn guard stats a missing path.
        real_mkdir = Path.mkdir

        def _rec_mkdir(self, *a, **kw):
            mkdir_threads.append(threading.current_thread())
            return real_mkdir(self, *a, **kw)

        monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", resolve_bin)
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
        monkeypatch.setattr(runtime_mod, "wrap_argv", lambda argv, mode, **kw: (list(argv), None))
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
            patch.object(
                Path,
                "mkdir",
                autospec=True,
                side_effect=_rec_mkdir,
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
    async def test_client_spawn_cancel_unlinks_sandbox_file(self, tmp_path, raise_in) -> None:
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

        monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", resolve_bin)
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
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
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=1.0),
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
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=1.0),
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


class TestRuntimeSpawnCarriesItsInstance:
    """The per-spawn incarnation travels in the child's environment.

    It is what a teardown reads back out of /proc/<pid>/environ to prove a
    process is THIS spawn's descendant once the root is gone -- so it must be in
    the env the process was created with, and it must be the value the runtime
    keeps as its own process_instance.
    """

    @pytest.mark.asyncio
    async def test_env_instance_matches_process_instance(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.constants import KIROCREW_SPAWN_INSTANCE_ENV, KIROCREW_SPAWNED_ENV

        class _StopSpawn(Exception):
            pass

        mock_proc = MagicMock()
        mock_proc.pid = 5152
        mock_proc.returncode = None
        mock_proc.stderr = None
        mock_proc.stdout = None
        TestRuntimeShieldSurvivesAFailedAppend._patch_prelude(monkeypatch, tmp_path, mock_proc)
        seen_env: dict[str, str] = {}

        async def fake_spawn(*_a, **kw):
            seen_env.update(kw.get("env") or {})
            return mock_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
        monkeypatch.setattr(runtime_mod, "register_protected_pid", lambda pid: None)
        monkeypatch.setattr(runtime_mod, "_track_pid", lambda pid: None)
        monkeypatch.setattr(runtime_mod, "_track_session_pid", lambda pid: None)

        runtime = AcpRuntime(work_dir=tmp_path / "workspace")

        async def _no_reader(_self) -> None:
            return None

        monkeypatch.setattr(AcpRuntime, "_reader_loop", _no_reader, raising=True)
        monkeypatch.setattr(
            AcpRuntime, "_send_and_await", AsyncMock(side_effect=_StopSpawn()), raising=True
        )
        # The failed-handshake cleanup would clear _process_instance; hold it.
        monkeypatch.setattr(AcpRuntime, "kill", AsyncMock(), raising=True)

        with pytest.raises(_StopSpawn):
            await runtime.spawn()

        assert seen_env.get(KIROCREW_SPAWNED_ENV) == "1"
        instance = seen_env.get(KIROCREW_SPAWN_INSTANCE_ENV)
        assert instance, "the child env carries no spawn instance"
        assert runtime._process_instance == instance


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

        monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", resolve_bin)
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
        monkeypatch.setattr(runtime_mod, "wrap_argv", lambda argv, mode, **kw: (list(argv), None))
        monkeypatch.setattr(runtime_mod, "cgroup_scope_argv", lambda argv: list(argv))
        monkeypatch.setattr(runtime_mod, "resolve_krb5_ccname", lambda env: None)
        monkeypatch.setattr(runtime_mod, "inject_xdist_auto_cap", lambda env: None)
        monkeypatch.setattr("kiro_crew.platform_compat.get_process_start_id", lambda pid: 1.0)

        async def fake_spawn(*_a, **_kw):
            return mock_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
        # Fake processes must never reach native Windows resume/job operations,
        # scratch owner probes, or browser socket discovery.
        monkeypatch.setattr(runtime_mod, "finish_suspended_spawn", lambda *a, **kw: None)
        monkeypatch.setattr(
            runtime_mod, "agent_scratch", SimpleNamespace(allocate_scratch=lambda _: None)
        )
        monkeypatch.setattr(runtime_mod, "browser_session_env", lambda env: {})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("windows", [False, True], ids=["posix", "windows"])
    @pytest.mark.parametrize("raise_in", ["finish_suspended_spawn", "initialize", "cancel"])
    async def test_failed_spawn_unwinds_live_tree_and_tracking(
        self, tmp_path, monkeypatch, raise_in, windows
    ) -> None:
        """Rejected spawns own the whole tree, even before PID tracking exists.

        Keep spawn, JSON-RPC demux, kill, registration and file writes real.
        Only the subprocess and kernel boundary are simulated. In particular,
        killing just the root must leave the MCP children visible to assertions.
        """
        from kiro_crew import platform_compat, session_pid

        root = 6100
        # Windows tree teardown follows parentage, not POSIX group membership.
        # The grandchild has its own process group on that branch.
        groups = {root: root, root + 1: root, root + 2: root + 2 if windows else root, 6200: 6200}
        parents = {root: 6000, root + 1: root, root + 2: root + 1, 6200: 6000}
        initialized = asyncio.Event()
        requests = []
        spawns = []

        class ReadPipe(asyncio.StreamReader):
            def __init__(self):
                super().__init__()
                self.reading = asyncio.Event()

            async def readuntil(self, separator=b"\n"):
                self.reading.set()
                return await super().readuntil(separator)

        class WritePipe:
            def write(self, data):
                request = json.loads(data)
                assert request["method"] == "initialize"
                requests.append(request)
                initialized.set()

            async def drain(self):
                pass

        class Process:
            pid = root
            returncode = None

            def __init__(self):
                self.stdin = WritePipe()
                self.stdout = ReadPipe()
                self.stderr = ReadPipe()

            async def wait(self):
                assert root not in groups, "process wait cannot reap a live root"
                self.returncode = -platform_compat.SIGTERM
                return self.returncode

        process = Process()
        self._patch_prelude(monkeypatch, tmp_path, process)

        async def fake_spawn(*args, **kwargs):
            spawns.append(kwargs)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

        def kill_tree(pid, sig):
            assert pid == root, "cleanup targeted another runtime"
            assert sig in (platform_compat.SIGTERM, platform_compat.SIGKILL)
            victims = {member for member, group in groups.items() if group == pid}
            if windows:
                victims = {pid}
                while (
                    children := {child for child, parent in parents.items() if parent in victims}
                    - victims
                ):
                    victims.update(children)
            for member in victims:
                groups.pop(member, None)

        # A closed port, not a proxy: no fabricated PID can fall back to host
        # liveness, native signals, process enumeration or Windows job handles.
        async def terminate_windows_tree(proc):
            assert proc is process
            kill_tree(proc.pid, platform_compat.SIGTERM)
            await proc.wait()
            session_pid.retire_windows_tree_tracking(proc.pid)
            return True

        async def with_fake_process(factory):
            return await factory()

        backend = SimpleNamespace(
            create_windows_cleanup_owned_process=with_fake_process,
            finish_windows_cleanup_owned_spawn=with_fake_process,
            IS_POSIX=not windows,
            IS_WINDOWS=windows,
            terminate_windows_asyncio_tree=terminate_windows_tree,
            CREATE_NEW_PROCESS_GROUP=0x200 if windows else 0,
            _SUBPROCESS_NO_WINDOW=0x08000000 if windows else 0,
            CREATE_SUSPENDED=0x4 if windows else 0,
            SIGTERM=platform_compat.SIGTERM,
            SIGKILL=platform_compat.SIGKILL,
            get_process_start_id=lambda pid: f"start-{pid}" if pid in groups else None,
            pid_exists=lambda pid: pid in groups,
            kill_process_tree=kill_tree,
            # The teardown pins the root's identity across the terminate, so the
            # stand-in answers the pinned call the same way it answers the plain
            # one once the recorded identity matches. Its assertions are unchanged:
            # the live tree must still come down and other runtimes must be spared.
            kill_process_tree_pinned=lambda pid, start, sig: (
                (kill_tree(pid, sig) is None or True) if start == f"start-{pid}" else False
            ),
            file_lock=platform_compat.file_lock,
            open_lock_file=platform_compat.open_lock_file,
        )
        monkeypatch.setattr(runtime_mod, "platform_compat", backend)
        monkeypatch.setattr(session_pid, "platform_compat", backend)
        monkeypatch.setattr(session_pid, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(session_pid, "os", SimpleNamespace(getpid=lambda: 6000))
        monkeypatch.setattr(session_pid, "_PROTECTED_PIDS", set())
        paths = [tmp_path / "kiro_pids.txt", tmp_path / "kiro_session_pids.txt"]
        boom = OSError("resume rejected after process start")

        def reject_resume(*args, **kwargs):
            assert root in groups
            assert not any(path.exists() for path in paths)
            raise boom

        if raise_in == "finish_suspended_spawn":
            monkeypatch.setattr(runtime_mod, "finish_suspended_spawn", reject_resume)

        runtime = AcpRuntime(work_dir=tmp_path / "workspace", expect_mcp_reports=False)
        # Join the worker while its kernel and filesystem pins still hold.
        with ThreadPoolExecutor(max_workers=1) as pool:
            monkeypatch.setattr(runtime_mod, "subprocess_executor", lambda: pool)
            task = asyncio.create_task(runtime.spawn())
            readers = []
            try:
                if raise_in != "finish_suspended_spawn":
                    await asyncio.wait_for(initialized.wait(), 5)
                    readers = [runtime._reader_task, runtime._stderr_task]
                    await asyncio.wait_for(
                        asyncio.gather(
                            process.stdout.reading.wait(), process.stderr.reading.wait()
                        ),
                        5,
                    )
                    assert all(reader is not None and not reader.done() for reader in readers)
                    assert root in session_pid._PROTECTED_PIDS
                    assert [path.read_text(encoding="utf-8") for path in paths] == [
                        f"{root}\n",
                        f"6000:{root}:start-{root}\n",
                    ], "the handshake must begin with both real PID records present"
                    assert not task.done()
                    if raise_in == "cancel":
                        task.cancel("cancel pending initialize")
                    else:
                        process.stdout.feed_data(
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": requests[0]["id"],
                                    "error": {"code": -32603, "message": "initialize rejected"},
                                }
                            ).encode()
                            + b"\n"
                        )
                if raise_in == "cancel":
                    with pytest.raises(asyncio.CancelledError, match="cancel pending initialize"):
                        await asyncio.wait_for(task, 5)
                elif raise_in == "initialize":
                    with pytest.raises(runtime_mod.AcpRuntimeError, match="initialize rejected"):
                        await asyncio.wait_for(task, 5)
                else:
                    with pytest.raises(OSError) as caught:
                        await asyncio.wait_for(task, 5)
                    assert caught.value is boom
                    assert runtime._reader_task is None and runtime._stderr_task is None

                assert len(spawns) == 1
                assert spawns[0]["start_new_session"] is (not windows)
                assert spawns[0]["creationflags"] == (
                    backend.CREATE_NEW_PROCESS_GROUP
                    | backend._SUBPROCESS_NO_WINDOW
                    | backend.CREATE_SUSPENDED
                )
                assert root not in groups, "failed spawn leaked its live root"
                assert (
                    root + 1 not in groups and root + 2 not in groups
                ), "failed spawn leaked MCP descendants"
                assert groups == {6200: 6200}, "cleanup must preserve unrelated runtimes"
                assert process.returncode is not None, "failed spawn never reaped its process"
                assert runtime._process is None and runtime._dead
                assert all(reader.done() for reader in readers), "spawn leaked reader/stderr tasks"
                assert root not in session_pid._PROTECTED_PIDS, "spawn leaked its sweep shield"
                assert not runtime._pending_requests, "spawn leaked an initialize waiter"
                assert all(
                    not path.exists() or path.read_text(encoding="utf-8") == "" for path in paths
                )
            finally:
                # Also safe under a negative-control mutation of production kill:
                # settle tasks without invoking that potentially broken cleanup.
                owned = [task, runtime._reader_task, runtime._stderr_task]
                for pending in owned:
                    if pending is not None and not pending.done():
                        pending.cancel()
                await asyncio.wait_for(
                    asyncio.gather(*(t for t in owned if t is not None), return_exceptions=True), 5
                )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raise_in", ["finish_suspended_spawn", "get_start_time"])
    async def test_runtime_spawn_reaps_the_process_when_the_window_raises(
        self, tmp_path, monkeypatch, raise_in
    ) -> None:
        """The sibling of the client-side window. ``finish_suspended_spawn``
        documents its own resume failure as FATAL and ``_get_start_time`` can
        raise, and every ``runtime.spawn()`` caller catches only
        ``AcpRuntimeError`` / ``AcpRuntimeDead`` -- so an ``OSError`` here would
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
                "kiro_crew.platform_compat.get_process_start_id",
                lambda pid: (_ for _ in ()).throw(boom),
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
