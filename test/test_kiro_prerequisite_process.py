"""Kiro CLI prerequisite tests: the fallback arms of ending a probe's process tree.

``kiro_prerequisite_process`` runs every probe and auth call. Its happy paths are
exercised by the probe and supervisor tests; these pin the arms that only a failing
host reaches: a group signal that cannot be delivered, a leader that ignores its
signals, the Windows handle path, a spawn the sandbox refuses, a run that is
cancelled or raises, and a Windows descendant snapshot that fails. Every process
here is a fake, the platform primitives are recorded, and the termination grace is
zero, so nothing waits on the wall clock.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from kiro_prerequisite_helpers import _agents_dir_never_the_real_home  # noqa: F401
from kiro_prerequisite_helpers import _isolate_agent_specs  # noqa: F401

from kiro_crew import kiro_prerequisite as prerequisite_module
from kiro_crew import platform_compat
from kiro_crew.sandbox import SandboxUnavailableError
from kiro_crew.testing.ids import UNALLOCATABLE_PID


class _Stream:
    """A child pipe: EOF at once, or a read that blocks until the run is torn down."""

    def __init__(self, *, blocks: bool = False, entered: asyncio.Event | None = None) -> None:
        self._blocks = blocks
        self._entered = entered

    async def read(self, _size: int) -> bytes:
        if self._entered is not None:
            self._entered.set()
        if self._blocks:
            await asyncio.Event().wait()  # never set: only the run's teardown cancel ends this read
        return b""


class _FakeProcess:
    """An ``asyncio.subprocess.Process`` stand-in that records what was asked of it."""

    def __init__(
        self,
        *,
        exits_on_wait: bool = False,
        terminate_raises: bool = False,
        kill_raises: bool = False,
        wait_raises: BaseException | None = None,
        stdout: _Stream | None = None,
    ) -> None:
        self.pid = UNALLOCATABLE_PID
        self.returncode: int | None = None
        self.calls: list[str] = []
        self.stdout = stdout or _Stream()
        self.stderr = _Stream()
        self._exits_on_wait = exits_on_wait
        self._terminate_raises = terminate_raises
        self._kill_raises = kill_raises
        self._wait_raises = wait_raises

    def terminate(self) -> None:
        self.calls.append("terminate")
        if self._terminate_raises:
            raise ProcessLookupError

    def kill(self) -> None:
        self.calls.append("kill")
        if self._kill_raises:
            raise ProcessLookupError

    async def wait(self) -> int:
        if self._wait_raises is not None:
            # Once: the teardown that follows waits on the leader again.
            error, self._wait_raises = self._wait_raises, None
            raise error
        if self._exits_on_wait:
            self.returncode = -15
            return -15
        # Never exits on its own: the grace bounds the teardown's waits, and the run's
        # own timeout or its cancellation bounds the run's.
        await asyncio.Event().wait()
        return 0


@pytest.fixture
def posix_host(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """A POSIX host whose group signals are recorded; returns the record."""
    signalled: list[tuple[int, int]] = []

    async def kill_tree(pid: int, signal_number: int) -> None:
        signalled.append((pid, signal_number))

    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "kill_process_tree_async", kill_tree)
    monkeypatch.setattr(prerequisite_module, "_TERMINATION_GRACE_SECS", 0)
    return signalled


@pytest.fixture
def windows_host(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """A Windows host whose retained-handle terminations are recorded; returns the record."""
    terminated: list[int] = []

    def terminate_handle(handle: int) -> bool:
        terminated.append(handle)
        return True

    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "terminate_process_handle", terminate_handle)
    monkeypatch.setattr(prerequisite_module, "_TERMINATION_GRACE_SECS", 0)
    return terminated


class TestTerminateProcessFallbacks:
    @pytest.mark.asyncio
    async def test_an_undeliverable_group_signal_falls_back_to_the_leader(
        self, posix_host: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def no_group(pid: int, signal_number: int) -> None:
            raise ProcessLookupError

        monkeypatch.setattr(platform_compat, "kill_process_tree_async", no_group)
        # A grace the leader's immediate exit lands inside (a zero grace cancels the
        # wait before it runs).
        monkeypatch.setattr(prerequisite_module, "_TERMINATION_GRACE_SECS", 30)
        proc = _FakeProcess(exits_on_wait=True)

        await prerequisite_module._terminate_process(proc)

        # The leader took the plain terminate and exited inside the grace: no kill.
        assert proc.calls == ["terminate"]

    @pytest.mark.asyncio
    async def test_a_leader_that_outlives_its_signals_is_killed(
        self, posix_host: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def no_group(pid: int, signal_number: int) -> None:
            raise OSError("no such group")

        monkeypatch.setattr(platform_compat, "kill_process_tree_async", no_group)
        proc = _FakeProcess(terminate_raises=True, kill_raises=True)

        await prerequisite_module._terminate_process(proc)

        # The group was never signalled, so the leader itself gets the kill; a
        # leader already gone by then is not an error.
        assert proc.calls == ["terminate", "kill"]

    @pytest.mark.asyncio
    async def test_windows_ends_the_retained_tree_and_then_the_leader(
        self, windows_host: list[int]
    ) -> None:
        proc = _FakeProcess()

        await prerequisite_module._terminate_process(proc, {11: 101, 12: 102})

        assert proc.calls == ["terminate", "kill"]
        assert sorted(windows_host) == [101, 102]

    @pytest.mark.asyncio
    async def test_windows_tolerates_a_leader_that_is_already_gone(
        self, windows_host: list[int]
    ) -> None:
        proc = _FakeProcess(terminate_raises=True, kill_raises=True)

        await prerequisite_module._terminate_process(proc)

        assert proc.calls == ["terminate", "kill"]
        assert windows_host == []


class TestRunProcessFailureArms:
    @pytest.fixture
    def no_sandbox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def passthrough(
            argv: list[str], **_kwargs: Any
        ) -> tuple[list[str], dict[str, str], str | None]:
            return list(argv), {}, None

        monkeypatch.setattr(prerequisite_module, "_prepare_sandboxed_spawn", passthrough)
        # A POSIX runner resolves the prepended ``/usr/bin/env`` as itself; a Windows
        # runner simulating POSIX would otherwise refuse it before the arm under test.
        monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: name)

    @pytest.mark.asyncio
    async def test_a_sandbox_refusal_is_reported_structurally(
        self, no_sandbox: None, posix_host: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def refuse(argv: list[str], **_kwargs: Any) -> Any:
            raise SandboxUnavailableError(
                "sandbox unavailable", "no_backend", "unshare failed", "apparmor"
            )

        monkeypatch.setattr(prerequisite_module, "_prepare_sandboxed_spawn", refuse)

        result = await prerequisite_module._run_process(
            "/fixed/tool", ["--version"], env={}, timeout_secs=30
        )

        assert result.ok is False
        assert result.sandbox_failure == ("no_backend", "unshare failed", "apparmor")

    @pytest.mark.asyncio
    async def test_a_cancelled_run_ends_the_tree_and_propagates(
        self, no_sandbox: None, posix_host: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reading = asyncio.Event()
        proc = _FakeProcess(stdout=_Stream(blocks=True, entered=reading))

        async def spawn(*_args: str, **_kwargs: Any) -> _FakeProcess:
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        run = asyncio.create_task(
            prerequisite_module._run_process("/fixed/tool", ["whoami"], env={}, timeout_secs=30)
        )
        await asyncio.wait_for(reading.wait(), timeout=30)

        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run, timeout=30)

        assert posix_host[0] == (UNALLOCATABLE_PID, platform_compat.SIGTERM)

    @pytest.mark.asyncio
    async def test_a_run_that_raises_is_reported_not_raised(
        self, no_sandbox: None, posix_host: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _FakeProcess(wait_raises=RuntimeError("wait broke"))

        async def spawn(*_args: str, **_kwargs: Any) -> _FakeProcess:
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

        result = await prerequisite_module._run_process(
            "/fixed/tool", ["--version"], env={}, timeout_secs=30
        )

        assert result.ok is False
        assert result.error == "wait broke"
        assert result.timed_out is False
        assert posix_host[0] == (UNALLOCATABLE_PID, platform_compat.SIGTERM)


class TestWindowsDescendantSnapshotFailures:
    @pytest.fixture
    def live_handles(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform_compat, "process_handle_active", lambda handle: True)

    @pytest.mark.asyncio
    async def test_a_failed_first_snapshot_is_handed_to_the_waiting_run(
        self, live_handles: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def unreadable(*_args: Any) -> dict[int, int]:
            raise OSError("snapshot denied")

        monkeypatch.setattr(platform_compat, "descendant_termination_handles_async", unreadable)
        loop = asyncio.get_running_loop()
        first, terminal = loop.create_future(), loop.create_future()

        await prerequisite_module._track_windows_descendants(_FakeProcess(), {}, 7, first, terminal)

        assert isinstance(first.exception(), OSError)
        assert terminal.cancelled()

    @pytest.mark.asyncio
    async def test_a_failed_terminal_snapshot_is_handed_to_the_waiting_run(
        self, live_handles: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def unreadable(*_args: Any) -> dict[int, int]:
            raise OSError("snapshot denied")

        monkeypatch.setattr(platform_compat, "descendant_termination_handles_async", unreadable)
        monkeypatch.setattr(platform_compat, "process_handle_active", lambda handle: False)
        terminal = asyncio.get_running_loop().create_future()

        await prerequisite_module._track_windows_descendants(_FakeProcess(), {}, 7, None, terminal)

        assert isinstance(terminal.exception(), OSError)

    @pytest.mark.asyncio
    async def test_an_unexpected_failure_releases_both_waiters(
        self, live_handles: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def broken(*_args: Any) -> dict[int, int]:
            raise RuntimeError("tracker broke")

        monkeypatch.setattr(platform_compat, "descendant_termination_handles_async", broken)
        loop = asyncio.get_running_loop()
        first, terminal = loop.create_future(), loop.create_future()

        with pytest.raises(RuntimeError, match="tracker broke"):
            await prerequisite_module._track_windows_descendants(
                _FakeProcess(), {}, 7, first, terminal
            )

        assert first.cancelled()
        assert terminal.cancelled()
