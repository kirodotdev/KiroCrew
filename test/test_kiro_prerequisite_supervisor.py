"""Kiro CLI prerequisite tests: the process-group supervisor around a probe.

Timeout escalation while the supervisor anchors the group, the POSIX supervisor's argv
guard, and Windows descendant tracking and termination.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from typing import Any
from unittest.mock import AsyncMock

import pytest
from kiro_prerequisite_helpers import _agents_dir_never_the_real_home  # noqa: F401
from kiro_prerequisite_helpers import _isolate_agent_specs  # noqa: F401

from kiro_crew import kiro_prerequisite as prerequisite_module
from kiro_crew import platform_compat
from kiro_crew.kiro_prerequisite import _run_process
from kiro_crew.testing.wait import await_until


class TestKiroPrerequisiteWorkflowSupervisor:
    @pytest.mark.asyncio
    async def test_process_timeout_escalates_while_supervisor_anchors_group(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tree_kills: list[tuple[int, int]] = []

        class _HeldOpenStream:
            async def read(self, _size: int) -> bytes:
                await asyncio.Event().wait()
                return b""

        class _AnchoredParent:
            pid = 9876
            returncode: int | None = None
            stdout = _HeldOpenStream()
            stderr = _HeldOpenStream()

            async def wait(self) -> int:
                await asyncio.Event().wait()
                return 1

        async def spawn(*_args: str, **_kwargs: Any) -> _AnchoredParent:
            return _AnchoredParent()

        async def kill_tree(pid: int, signal_number: int) -> None:
            tree_kills.append((pid, signal_number))

        async def passthrough_sandbox(
            argv: list[str],
            **_kwargs: Any,
        ) -> tuple[list[str], dict[str, str], str | None]:
            return list(argv), {}, None

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(platform_compat, "kill_process_tree_async", kill_tree)
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        # The simulated POSIX host must also RESOLVE the ``/usr/bin/env`` wrapper
        # the spawn path prepends: on a Windows runner the host's ``os.path.isabs``
        # (ntpath, from Python 3.13) does not consider that path absolute, so the
        # code falls back to ``trusted_system_bin`` -- which cannot find a POSIX
        # path on this host and would refuse the spawn before the timeout under
        # test is ever reached. Resolving the name as itself is what the real
        # POSIX host does.
        monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: name)
        # This case is about the timeout escalation, not about sandbox building,
        # and every spawn is sandboxed now — so stub the builder rather than let
        # host sandbox availability decide the outcome.
        monkeypatch.setattr(
            "kiro_crew.kiro_prerequisite._prepare_sandboxed_spawn",
            passthrough_sandbox,
        )
        monkeypatch.setattr(
            "kiro_crew.kiro_prerequisite._TERMINATION_GRACE_SECS",
            0.001,
        )

        result = await _run_process(
            "/fixed/tool",
            ["--version"],
            env={},
            timeout_secs=0.01,
        )

        assert result.timed_out is True
        assert tree_kills == [
            (9876, platform_compat.SIGTERM),
            (9876, platform_compat.SIGKILL),
        ]

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX,
        reason="POSIX process-group supervisor",
    )
    def test_posix_supervisor_rejects_relative_executable(self) -> None:
        # The supervisor is the last line of defence against resolving a bare
        # program name through an agent-writable PATH. Exercised directly rather
        # than through _run_process: every spawn is sandboxed now, and that path
        # hands the supervisor an absolute /usr/bin/env, so the guard cannot be
        # reached from there.
        completed = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-I",
                "-c",
                prerequisite_module._PROCESS_GROUP_SUPERVISOR_CODE,
                "kiro-cli",
                "--version",
            ],
            env={"PATH": "/tmp/agent-writable"},
            capture_output=True,
            timeout=30,
            check=False,
        )

        assert completed.returncode == 127

    @pytest.mark.asyncio
    async def test_windows_timeout_terminates_retained_descendant_handle(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        terminated_handles: list[int] = []
        closed_handles: list[int] = []

        class _HeldOpenStream:
            async def read(self, _size: int) -> bytes:
                await asyncio.Event().wait()
                return b""

        class _ExitedParent:
            pid = 4321
            returncode: int | None = None
            stdout = _HeldOpenStream()
            stderr = _HeldOpenStream()
            stdin = None

            async def wait(self) -> int:
                self.returncode = 0
                return 0

        async def spawn(*_args: str, **_kwargs: Any) -> _ExitedParent:
            return _ExitedParent()

        async def descendants(
            _pid: int,
            _retained_handles: dict[int, int] | None = None,
            _root_handle: int | None = None,
        ) -> dict[int, int]:
            return {4322: 9001}

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(
            platform_compat,
            "descendant_termination_handles_async",
            descendants,
        )
        monkeypatch.setattr(
            platform_compat,
            "duplicate_asyncio_process_handle",
            lambda _proc: 8001,
        )
        monkeypatch.setattr(
            platform_compat,
            "terminate_process_handle",
            lambda handle: terminated_handles.append(handle) or True,
        )
        monkeypatch.setattr(
            platform_compat,
            "close_process_handle",
            lambda handle: closed_handles.append(handle),
        )
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(
            "kiro_crew.kiro_prerequisite._TERMINATION_GRACE_SECS",
            0.01,
        )

        result = await _run_process(
            r"C:\fixed\tool.exe",
            ["--version"],
            env={},
            timeout_secs=0.01,
        )

        assert result.timed_out is True
        assert terminated_handles == [9001]
        assert closed_handles == [9001, 8001]

    @pytest.mark.asyncio
    async def test_windows_immediate_exit_still_takes_anchored_initial_snapshot(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        snapshot_roots: list[int] = []
        closed_handles: list[int] = []

        class _ClosedStream:
            async def read(self, _size: int) -> bytes:
                return b""

        class _ExitedParent:
            pid = 4321
            returncode = 0
            stdout = _ClosedStream()
            stderr = _ClosedStream()
            stdin = None

            async def wait(self) -> int:
                return 0

        async def spawn(*_args: str, **_kwargs: Any) -> _ExitedParent:
            return _ExitedParent()

        async def descendants(
            root_pid: int,
            _retained_handles: dict[int, int] | None = None,
            _root_handle: int | None = None,
        ) -> dict[int, int]:
            snapshot_roots.append(root_pid)
            await asyncio.sleep(0)
            return {4322: 9001} if root_pid == 4321 else {}

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(
            platform_compat,
            "duplicate_asyncio_process_handle",
            lambda _proc: 8001,
        )
        monkeypatch.setattr(
            platform_compat,
            "descendant_termination_handles_async",
            descendants,
        )
        monkeypatch.setattr(platform_compat, "process_handle_active", lambda _handle: False)
        monkeypatch.setattr(
            platform_compat,
            "close_process_handle",
            closed_handles.append,
        )
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        result = await _run_process(
            r"C:\fixed\tool.exe",
            ["--version"],
            env={},
            timeout_secs=1,
        )

        assert result.ok is True
        assert snapshot_roots == [4321, 4322]
        assert closed_handles == [9001, 8001]

    @pytest.mark.asyncio
    async def test_windows_success_waits_for_live_launcher_descendant(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        active_handles = {9001}
        child_observed = asyncio.Event()
        closed_handles: list[int] = []

        class _ClosedStream:
            async def read(self, _size: int) -> bytes:
                return b""

        class _ExitedParent:
            pid = 4321
            returncode = 0
            stdout = _ClosedStream()
            stderr = _ClosedStream()
            stdin = None

            async def wait(self) -> int:
                return 0

        async def spawn(*_args: str, **_kwargs: Any) -> _ExitedParent:
            return _ExitedParent()

        async def descendants(
            root_pid: int,
            _retained_handles: dict[int, int] | None = None,
            root_handle: int | None = None,
        ) -> dict[int, int]:
            if root_pid == 4321:
                assert root_handle == 8001
                child_observed.set()
                return {4322: 9001}
            assert root_pid == 4322
            assert root_handle == 9001
            return {}

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(
            platform_compat,
            "duplicate_asyncio_process_handle",
            lambda _proc: 8001,
        )
        monkeypatch.setattr(
            platform_compat,
            "descendant_termination_handles_async",
            descendants,
        )
        monkeypatch.setattr(
            platform_compat,
            "process_handle_active",
            lambda handle: handle in active_handles,
        )
        monkeypatch.setattr(
            platform_compat,
            "close_process_handle",
            closed_handles.append,
        )
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(
            prerequisite_module,
            "_WINDOWS_DESCENDANT_POLL_SECS",
            0.001,
        )

        process_task = asyncio.create_task(
            _run_process(
                r"C:\fixed\launcher.exe",
                ["install"],
                env={},
                timeout_secs=1,
                # Drive the BODY on this test's own loop. What is under test here
                # is the descendant tracker's step-by-step behaviour, and the
                # test observes it through ``child_observed`` -- an
                # ``asyncio.Event`` bound to this loop, which the fake
                # ``descendants`` coroutine sets. Letting the Windows hop run
                # would put that fake on the private loop, where setting a
                # foreign loop's Event never wakes this one. The hop itself is
                # covered by test_kiro_probe_spawn_off_loop.py.
                _on_private_loop=True,
            )
        )
        await await_until(child_observed, what="the supervisor observing the child")
        await asyncio.sleep(0)

        assert process_task.done() is False

        active_handles.clear()
        result = await await_until(process_task, what="the supervised process finishing")

        assert result.ok is True
        assert closed_handles == [9001, 8001]

    @pytest.mark.asyncio
    async def test_windows_tracker_discovers_from_live_child_after_root_exit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _ExitedRoot:
            pid = 4321
            returncode = 0

        tracked = {4322: 9001}
        active_handles = {9001}
        child_root_scans = 0

        async def descendants(
            root_pid: int,
            _retained_handles: dict[int, int] | None = None,
            root_handle: int | None = None,
        ) -> dict[int, int]:
            nonlocal child_root_scans
            if root_pid == 4322:
                assert root_handle == 9001
                child_root_scans += 1
                return {4323: 9002} if child_root_scans == 1 else {}
            assert root_pid == 4323
            assert root_handle == 9002
            return {}

        async def one_poll(_delay: float) -> None:
            active_handles.clear()

        monkeypatch.setattr(
            platform_compat,
            "process_handle_active",
            lambda handle: handle in active_handles,
        )
        monkeypatch.setattr(
            platform_compat,
            "descendant_termination_handles_async",
            descendants,
        )
        monkeypatch.setattr(asyncio, "sleep", one_poll)

        await prerequisite_module._track_windows_descendants(_ExitedRoot(), tracked)  # type: ignore[arg-type]

        assert tracked[4323] == 9002
        assert child_root_scans == 2

    @pytest.mark.asyncio
    async def test_windows_tracker_accepts_validated_discovery_when_anchor_exits(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _ExitedRoot:
            pid = 4321
            returncode = 0

        tracked = {4322: 9001}
        active_handles = {9001}
        child_root_scans = 0

        async def descendants(
            root_pid: int,
            _retained_handles: dict[int, int] | None = None,
            root_handle: int | None = None,
        ) -> dict[int, int]:
            nonlocal child_root_scans
            if root_pid == 4322:
                assert root_handle == 9001
                child_root_scans += 1
                if child_root_scans == 1:
                    # The parent exits after this scan. The child appears only
                    # in the required post-exit terminal snapshot.
                    active_handles.clear()
                    return {}
                return {9876: 9002}
            assert root_pid == 9876
            assert root_handle == 9002
            return {}

        monkeypatch.setattr(
            platform_compat,
            "process_handle_active",
            lambda handle: handle in active_handles,
        )
        monkeypatch.setattr(
            platform_compat,
            "descendant_termination_handles_async",
            descendants,
        )
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())

        await prerequisite_module._track_windows_descendants(  # type: ignore[arg-type]
            _ExitedRoot(),
            tracked,
        )

        assert tracked[9876] == 9002
        assert child_root_scans == 2

    @pytest.mark.asyncio
    async def test_windows_tracker_scans_each_inactive_child_root_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _ExitedRoot:
            pid = 4321
            returncode = 0

        tracked = {4322: 9001}
        snapshot_roots: list[int] = []

        async def descendants(
            root_pid: int,
            _retained_handles: dict[int, int] | None = None,
            root_handle: int | None = None,
        ) -> dict[int, int]:
            snapshot_roots.append(root_pid)
            if root_pid == 4322:
                assert root_handle == 9001
                return {4323: 9002}
            assert root_pid == 4323
            assert root_handle == 9002
            return {}

        monkeypatch.setattr(
            platform_compat,
            "process_handle_active",
            lambda _handle: False,
        )
        monkeypatch.setattr(
            platform_compat,
            "descendant_termination_handles_async",
            descendants,
        )
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())

        await prerequisite_module._track_windows_descendants(  # type: ignore[arg-type]
            _ExitedRoot(),
            tracked,
        )

        assert tracked[4323] == 9002
        assert snapshot_roots == [4322, 4323]

    @pytest.mark.asyncio
    async def test_windows_tracker_fails_closed_on_later_snapshot_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _RunningRoot:
            pid = 4321
            returncode = None

        snapshot_calls = 0

        async def descendants(
            root_pid: int,
            _retained_handles: dict[int, int] | None = None,
            root_handle: int | None = None,
        ) -> dict[int, int]:
            nonlocal snapshot_calls
            assert root_pid == 4321
            assert root_handle == 8001
            snapshot_calls += 1
            if snapshot_calls == 1:
                return {}
            raise OSError("Toolhelp unavailable")

        monkeypatch.setattr(
            platform_compat,
            "descendant_termination_handles_async",
            descendants,
        )
        monkeypatch.setattr(
            platform_compat,
            "process_handle_active",
            lambda _handle: True,
        )
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())

        with pytest.raises(OSError, match="Toolhelp unavailable"):
            await prerequisite_module._track_windows_descendants(  # type: ignore[arg-type]
                _RunningRoot(),
                {},
                8001,
            )

        assert snapshot_calls == 2

    @pytest.mark.asyncio
    async def test_windows_tracker_accepts_validated_discovery_when_primary_exits(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _PrimaryRoot:
            pid = 4321
            returncode: int | None = None

        root = _PrimaryRoot()
        tracked: dict[int, int] = {}

        async def descendants(
            root_pid: int,
            _retained_handles: dict[int, int] | None = None,
            root_handle: int | None = None,
        ) -> dict[int, int]:
            if root_pid == root.pid:
                assert root_handle is None
                root.returncode = 0
                return {9876: 9002}
            assert root_pid == 9876
            assert root_handle == 9002
            return {}

        monkeypatch.setattr(
            platform_compat,
            "descendant_termination_handles_async",
            descendants,
        )
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())

        await prerequisite_module._track_windows_descendants(  # type: ignore[arg-type]
            root,
            tracked,
        )

        assert tracked == {9876: 9002}
