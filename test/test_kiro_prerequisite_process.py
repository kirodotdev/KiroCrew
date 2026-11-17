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
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


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


class TestRequireCrewSandbox:
    """A caller may refuse a spawn the chokepoint did not put in Kiro Crew's own sandbox."""

    @pytest.fixture
    def spawns(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        """Every argv handed to the spawn, which then fails: no child is needed here."""
        seen: list[list[str]] = []

        async def spawn(*argv: str, **_kwargs: Any) -> Any:
            seen.append(list(argv))
            raise OSError("spawn reached")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: name)
        return seen

    @staticmethod
    def _wrap(monkeypatch: pytest.MonkeyPatch, prefix: list[str], cleanup: str | None) -> None:
        async def prepare(
            argv: list[str], **_kwargs: Any
        ) -> tuple[list[str], dict[str, str], str | None]:
            return [*prefix, *argv], {}, cleanup

        monkeypatch.setattr(prerequisite_module, "_prepare_sandboxed_spawn", prepare)

    @pytest.mark.asyncio
    async def test_an_unconfined_wrap_is_refused_before_any_spawn(
        self,
        spawns: list[list[str]],
        posix_host: list[tuple[int, int]],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
    ) -> None:
        leftover = tmp_path / "launcher"
        leftover.write_text("")
        self._wrap(monkeypatch, [], str(leftover))

        result = await prerequisite_module._run_process(
            "/planted/kiro-cli",
            ["--version"],
            env={},
            timeout_secs=FALLBACK_TIMEOUT_SECS,
            require_crew_sandbox=True,
        )

        assert spawns == []
        assert result.ok is False and result.returncode is None
        assert result.error == prerequisite_module._NOT_IN_CREW_SANDBOX_ERROR
        assert result.sandbox_failure is None and result.timed_out is False
        assert not leftover.exists(), "the wrap's launcher is removed"

    @pytest.mark.asyncio
    async def test_the_default_still_runs_that_spawn(
        self,
        spawns: list[list[str]],
        posix_host: list[tuple[int, int]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Every caller that does not ask keeps the behaviour it had."""
        self._wrap(monkeypatch, [], None)

        result = await prerequisite_module._run_process(
            "/fixed/kiro-cli", ["--version"], env={}, timeout_secs=FALLBACK_TIMEOUT_SECS
        )

        assert len(spawns) == 1 and spawns[0][-3:] == [
            "/usr/bin/env",
            "/fixed/kiro-cli",
            "--version",
        ]
        assert result.error == "spawn reached"

    @pytest.mark.asyncio
    async def test_a_spawn_inside_crew_sandbox_runs(
        self,
        spawns: list[list[str]],
        posix_host: list[tuple[int, int]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._wrap(monkeypatch, ["/bin/sh", "/run/kirocrew_sandbox_1.sh"], None)

        result = await prerequisite_module._run_process(
            "/planted/kiro-cli",
            ["--version"],
            env={},
            timeout_secs=FALLBACK_TIMEOUT_SECS,
            require_crew_sandbox=True,
        )

        assert len(spawns) == 1 and "/run/kirocrew_sandbox_1.sh" in spawns[0]
        assert result.error == "spawn reached"

    @pytest.mark.asyncio
    async def test_windows_which_skips_the_wrap_is_refused(
        self,
        spawns: list[list[str]],
        windows_host: list[int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def never(*_a: Any, **_k: Any) -> Any:
            pytest.fail("Windows does not wrap the spawn")

        monkeypatch.setattr(prerequisite_module, "_prepare_sandboxed_spawn", never)

        result = await prerequisite_module._run_process(
            "C:/planted/kiro-cli.exe",
            ["--version"],
            env={},
            timeout_secs=FALLBACK_TIMEOUT_SECS,
            require_crew_sandbox=True,
            _on_private_loop=True,
        )

        assert spawns == []
        assert result.error == prerequisite_module._NOT_IN_CREW_SANDBOX_ERROR


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
    async def test_an_unexpected_failure_reaches_the_waiting_run_as_its_own_error(
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

        # The run awaits the first snapshot before the terminal one, so the error goes
        # to the first. A cancelled waiter would read as a cancellation of the run.
        assert isinstance(first.exception(), RuntimeError)
        assert terminal.cancelled()

    @pytest.mark.asyncio
    async def test_a_failed_later_snapshot_reaches_the_waiting_run_as_its_own_error(
        self, live_handles: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The root is still alive on the second scan, so no terminal snapshot is due."""
        scans = 0

        async def incomplete_on_the_second_scan(*_args: Any) -> dict[int, int]:
            nonlocal scans
            scans += 1
            if scans > 1:
                raise OSError("Windows descendant ancestry incomplete: 12")
            return {}

        monkeypatch.setattr(
            platform_compat, "descendant_termination_handles_async", incomplete_on_the_second_scan
        )
        monkeypatch.setattr(prerequisite_module, "_WINDOWS_DESCENDANT_POLL_SECS", 0)
        loop = asyncio.get_running_loop()
        first, terminal = loop.create_future(), loop.create_future()

        with pytest.raises(OSError, match="ancestry incomplete"):
            await asyncio.wait_for(
                prerequisite_module._track_windows_descendants(
                    _FakeProcess(), {}, 7, first, terminal
                ),
                timeout=30,
            )

        assert first.result() is None
        assert isinstance(terminal.exception(), OSError)

    @pytest.mark.asyncio
    async def test_a_cancelled_tracker_still_cancels_its_waiters(
        self, live_handles: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scanning = asyncio.Event()

        async def never_returns(*_args: Any) -> dict[int, int]:
            scanning.set()
            await asyncio.Event().wait()  # never set: only the cancel below ends this scan
            return {}

        monkeypatch.setattr(platform_compat, "descendant_termination_handles_async", never_returns)
        loop = asyncio.get_running_loop()
        first, terminal = loop.create_future(), loop.create_future()
        tracker = asyncio.create_task(
            prerequisite_module._track_windows_descendants(_FakeProcess(), {}, 7, first, terminal)
        )
        await asyncio.wait_for(scanning.wait(), timeout=30)

        tracker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(tracker, timeout=30)

        assert first.cancelled()
        assert terminal.cancelled()


_ROOT_HANDLE = 7
_CHILD_PID = 12
_CHILD_HANDLE = 102
_PREREQUISITE_LOGGER = "kiro_crew.kiro_prerequisite"


class _TrackedTree:
    """The Windows tree primitives ``_run_process`` drives, recorded.

    The first scan finds one child, so the run retains a handle the failure paths
    must terminate and close. ``fail_from`` is the scan that starts raising.
    """

    def __init__(self, terminated: list[int]) -> None:
        self.terminated = terminated
        self.closed: list[int] = []
        self.scans = 0
        self.fail_from = 0
        self.error_type: type[Exception] = OSError
        self.second_scan = asyncio.Event()

    async def scan(self, _root_pid: int, _tracked: Any, _root_handle: Any) -> dict[int, int]:
        self.scans += 1
        if self.scans >= 2:
            self.second_scan.set()
        if self.fail_from and self.scans >= self.fail_from:
            raise self.error_type(f"Windows descendant ancestry incomplete: {_CHILD_PID}")
        return {_CHILD_PID: _CHILD_HANDLE} if self.scans == 1 else {}


@pytest.fixture
def tracked_tree(windows_host: list[int], monkeypatch: pytest.MonkeyPatch) -> _TrackedTree:
    """A Windows host whose root and child stay alive until the run ends them."""
    tree = _TrackedTree(windows_host)
    monkeypatch.setattr(
        platform_compat, "duplicate_asyncio_process_handle", lambda proc: _ROOT_HANDLE
    )
    monkeypatch.setattr(platform_compat, "process_handle_active", lambda handle: True)
    monkeypatch.setattr(platform_compat, "close_process_handle", tree.closed.append)
    monkeypatch.setattr(platform_compat, "descendant_termination_handles_async", tree.scan)
    monkeypatch.setattr(prerequisite_module, "_WINDOWS_DESCENDANT_POLL_SECS", 0)
    return tree


def _spawns(monkeypatch: pytest.MonkeyPatch, proc: _FakeProcess) -> None:
    async def spawn(*_args: str, **_kwargs: Any) -> _FakeProcess:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)


async def _run_on_this_loop(timeout_secs: float = 30) -> Any:
    """The body of a Windows run, on the test's own loop (the hop is pinned elsewhere)."""
    return await prerequisite_module._run_process(
        r"C:\fixed\kiro-cli.exe",
        ["--version"],
        env={},
        timeout_secs=timeout_secs,
        _on_private_loop=True,
    )


def _cut_short() -> Any:
    """What a run returns when it ended its tree unverified: a zero exit, partial help."""
    return prerequisite_module.ProcessResult(
        ok=False,
        output="Usage: kiro-cli acp [OPTIONS]\n",
        returncode=0,
        error=f"Windows descendant ancestry incomplete: {_CHILD_PID}",
        tree_unverified=True,
    )


def _answers(monkeypatch: pytest.MonkeyPatch, result: Any) -> list[list[str]]:
    """Replace the probe runner with one that records each argv and returns *result*."""
    runs: list[list[str]] = []

    async def answer(command: str, args: list[str], **_kwargs: Any) -> Any:
        runs.append([command, *args])
        return result

    monkeypatch.setattr(prerequisite_module, "_run_process", answer)
    return runs


def _tracker_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == _PREREQUISITE_LOGGER and rec.levelname == "WARNING"
    ]


class TestWindowsTrackerFailureDuringARun:
    """A tracker that cannot certify the tree fails the run; it does not cancel it."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("fail_from", "error_type"),
        [(2, OSError), (1, RuntimeError), (2, RuntimeError), (1, OSError)],
        ids=["later-incomplete", "first-unexpected", "later-unexpected", "first-incomplete"],
    )
    async def test_the_run_ends_the_tree_and_returns_a_failed_result(
        self,
        tracked_tree: _TrackedTree,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        fail_from: int,
        error_type: type[Exception],
    ) -> None:
        tracked_tree.fail_from, tracked_tree.error_type = fail_from, error_type
        proc = _FakeProcess()
        _spawns(monkeypatch, proc)

        with caplog.at_level("WARNING", logger=_PREREQUISITE_LOGGER):
            # Bounded here so a run that never returns fails by name at this line.
            result = await asyncio.wait_for(_run_on_this_loop(), timeout=30)

        assert result.ok is False
        assert result.tree_unverified is True
        assert result.timed_out is False
        assert result.error == f"Windows descendant ancestry incomplete: {_CHILD_PID}"
        # Fail-closed: the leader and every retained child are ended, and no handle leaks.
        assert proc.calls == ["terminate", "kill"]
        retained = [_CHILD_HANDLE] if fail_from > 1 else []
        assert tracked_tree.terminated == retained
        assert sorted(tracked_tree.closed) == sorted([_ROOT_HANDLE, *retained])
        reasons = _tracker_warnings(caplog)
        assert len(reasons) == 1, reasons
        assert error_type.__name__ in reasons[0]
        assert f"ancestry incomplete: {_CHILD_PID}" in reasons[0]

    @pytest.mark.asyncio
    async def test_a_cancel_from_outside_still_cancels_the_run(
        self, tracked_tree: _TrackedTree, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _FakeProcess()
        _spawns(monkeypatch, proc)
        run = asyncio.create_task(_run_on_this_loop())
        # Past the first snapshot: the run is waiting on the terminal one, the same
        # await a failing tracker interrupts.
        await asyncio.wait_for(tracked_tree.second_scan.wait(), timeout=30)

        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run, timeout=30)

        assert proc.calls == ["terminate", "kill"]
        assert tracked_tree.terminated == [_CHILD_HANDLE]
        assert sorted(tracked_tree.closed) == [_ROOT_HANDLE, _CHILD_HANDLE]

    @pytest.mark.asyncio
    async def test_a_run_that_times_out_is_still_a_timeout(
        self, tracked_tree: _TrackedTree, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _FakeProcess()
        _spawns(monkeypatch, proc)

        result = await asyncio.wait_for(_run_on_this_loop(timeout_secs=0), timeout=30)

        assert result.timed_out is True
        assert result.tree_unverified is False
        assert proc.calls == ["terminate", "kill"]

    @pytest.mark.asyncio
    async def test_a_tracker_that_fails_under_a_timeout_is_still_logged(
        self,
        tracked_tree: _TrackedTree,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The run never reaches the tracker's error itself: the deadline ends it first."""
        tracked_tree.fail_from, tracked_tree.error_type = 1, RuntimeError
        _spawns(monkeypatch, _FakeProcess())

        with caplog.at_level("WARNING", logger=_PREREQUISITE_LOGGER):
            result = await asyncio.wait_for(_run_on_this_loop(timeout_secs=0), timeout=30)

        assert result.timed_out is True
        assert _tracker_warnings(caplog) == [
            "The Windows descendant tracker failed during a kiro-cli run (RuntimeError: "
            f"Windows descendant ancestry incomplete: {_CHILD_PID}); the run ended its "
            "retained process tree without certifying it"
        ]

    @pytest.mark.asyncio
    async def test_a_scan_that_fails_after_the_leader_exited_is_reported_too(
        self,
        tracked_tree: _TrackedTree,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Both snapshots have settled, so the error reaches the run from the tracker task."""
        monkeypatch.setattr(
            platform_compat, "process_handle_active", lambda handle: handle != _ROOT_HANDLE
        )
        tracked_tree.fail_from = 2
        proc = _FakeProcess(exits_on_wait=True)
        _spawns(monkeypatch, proc)

        with caplog.at_level("WARNING", logger=_PREREQUISITE_LOGGER):
            result = await asyncio.wait_for(_run_on_this_loop(), timeout=30)

        assert result.ok is False
        assert result.tree_unverified is True
        # The leader had already exited, so only the retained child is ended.
        assert proc.calls == []
        assert tracked_tree.terminated == [_CHILD_HANDLE]
        assert len(_tracker_warnings(caplog)) == 1


class TestTheBootProbeSurvivesATrackerFailure:
    """The gateway reads the kiro-cli version while it boots, through this same run."""

    @pytest.fixture
    def pinned(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> str:
        """An executable stand-in that is the pinned install, on the relay route."""
        from kiro_crew import kiro_cli
        from kiro_crew import sandbox as sandbox_mod
        from kiro_crew.agent_sdk import backend_install

        binary = tmp_path / "kiro-cli"
        binary.write_bytes(b"")
        binary.chmod(0o755)
        monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (str(binary), False))
        for cache in (
            "_version_cache",
            "_kas_start_cache",
            "_chat_engines_cache",
            "_unverified_probe_runs",
        ):
            monkeypatch.setattr(kiro_cli, cache, {})
        monkeypatch.setattr(sandbox_mod, "configured_sandbox_mode", lambda: "standard")
        monkeypatch.setattr(backend_install, "_kas_bundle_route", lambda: False)
        return str(binary)

    def test_the_version_probe_fails_and_the_spec_gate_still_answers(
        self, tracked_tree: _TrackedTree, monkeypatch: pytest.MonkeyPatch, pinned: str
    ) -> None:
        from kiro_crew import kiro_cli
        from kiro_crew.agent_materialization import auto_approve

        tracked_tree.fail_from = 2
        _spawns(monkeypatch, _FakeProcess())

        # No running loop, as at boot: the probe runs the real hop onto a private loop.
        result = kiro_cli._sandboxed_probe(pinned, ["--version"], 30)

        assert result is not None
        assert result.ok is False
        assert result.tree_unverified is True
        # The gate every generated spec writer asks at boot answers "unknown version".
        assert auto_approve._spec_permissions_accepted() is False

    def test_a_run_that_was_cut_short_is_not_read_as_the_childs_answer(
        self, monkeypatch: pytest.MonkeyPatch, pinned: str
    ) -> None:
        """Even with a zero exit and help text captured before the tree was ended."""
        from kiro_crew import kiro_cli

        cut_short = _cut_short()
        _answers(monkeypatch, cut_short)

        assert kiro_cli._probe_ran(cut_short) is False
        # Help text missing the engine selector would otherwise read as "no KAS here".
        assert kiro_cli.kas_relay_help(pinned) is None
        assert kiro_cli.kas_relay_start_output(pinned, ("acp",)) is None
        assert kiro_cli.kiro_cli_version_at(pinned) is None

    def test_the_next_read_probes_again_and_gets_the_real_version(
        self, tracked_tree: _TrackedTree, monkeypatch: pytest.MonkeyPatch, pinned: str
    ) -> None:
        """One failed tree scan is not kept as "unknown" for the gateway's life."""
        from kiro_crew import kiro_cli
        from kiro_crew.agent_materialization import auto_approve

        tracked_tree.fail_from = 2
        _spawns(monkeypatch, _FakeProcess())
        assert kiro_cli.kiro_cli_version_at(pinned) is None

        accepted = kiro_cli.SPEC_PERMISSIONS_MIN_VERSION
        runs = _answers(
            monkeypatch,
            prerequisite_module.ProcessResult(
                ok=True, output="kiro-cli {}.{}.{}\n".format(*accepted), returncode=0
            ),
        )

        assert kiro_cli.kiro_cli_version_at(pinned) == accepted
        assert auto_approve._spec_permissions_accepted() is True
        # The real answer is kept: the gate's own read did not spawn again.
        assert len(runs) == 1

    @pytest.mark.parametrize("probe", ["kiro_cli_version_at", "chat_agent_engines_at"])
    def test_a_tree_that_never_verifies_is_probed_a_bounded_number_of_times(
        self, monkeypatch: pytest.MonkeyPatch, pinned: str, probe: str
    ) -> None:
        """Each retry spawns kiro-cli, so a failure that repeats ends as a timeout does."""
        from kiro_crew import kiro_cli

        runs = _answers(monkeypatch, _cut_short())

        for _ in range(kiro_cli._UNVERIFIED_PROBE_ATTEMPTS + 2):
            assert getattr(kiro_cli, probe)(pinned) is None

        assert len(runs) == kiro_cli._UNVERIFIED_PROBE_ATTEMPTS
