"""Ending processes: a pid, a tree, the pinned and async variants, and the reap.

``kill_pid`` and ``kill_process_tree`` on a real POSIX child and group, the
Windows ``taskkill`` return-code mapping, ``kill_pid_async`` and
``kill_process_tree_async``, the pinned kills that act only while the target's
identity handle is held, and ``kill_and_reap`` / ``terminate_and_reap_sync`` for
children this process spawned. A topic of the ``test_platform_compat`` family;
see ``test_platform_compat.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import types

import pytest
from platform_compat_helpers import _fake_windows_bins

from kiro_crew import platform_compat as pc
from kiro_crew.testing.wait import wait_until

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_platform_compat family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


#: The REAL same-group probe, bound at module import so this file can test it.
#: The rootdir conftest pins ``pc._shares_own_process_group`` for every test
#: (see ``_pin_kill_and_reap_group_probe``), and that pin lands after this
#: import -- so reaching for the module attribute inside a test would exercise
#: the stub instead of the function.
_real_shares_own_process_group = pc._shares_own_process_group


class TestKillSubprocessPosix:
    @pytest.mark.skipif(pc.IS_WINDOWS, reason="POSIX os.kill path; Windows uses taskkill")
    def test_kill_pid_terminates_real_child_posix(self):
        # POSIX kill_pid success path (os.kill + return True): spawn a real
        # long-lived child, confirm it is alive, SIGKILL it via the shim, then
        # reap it so its PID leaves the table and pid_exists() flips to False.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            assert pc.pid_exists(child.pid) is True
            assert pc.kill_pid(child.pid, pc.SIGKILL) is True
            # Reap the killed child so it is not left a zombie occupying the
            # PID; otherwise os.kill(pid, 0) would still report it as existing.
            child.wait(timeout=5)
            wait_until(lambda: not pc.pid_exists(child.pid), timeout=2.0)
            assert pc.pid_exists(child.pid) is False
        finally:
            if child.poll() is None:
                child.kill()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    @pytest.mark.skipif(pc.IS_WINDOWS, reason="POSIX killpg path; Windows uses taskkill /T")
    def test_kill_process_tree_kills_group_posix(self):
        # POSIX kill_process_tree success path (os.getpgid + os.killpg + return
        # True): spawn the child in its OWN session/process group so its pgid
        # equals its pid, then tree-kill the group and confirm it is gone.
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        try:
            assert os.getpgid(child.pid) == child.pid
            assert pc.pid_exists(child.pid) is True
            assert pc.kill_process_tree(child.pid, pc.SIGKILL) is True
            child.wait(timeout=5)
            wait_until(lambda: not pc.pid_exists(child.pid), timeout=2.0)
            assert pc.pid_exists(child.pid) is False
        finally:
            if child.poll() is None:
                child.kill()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


class TestTaskkillErrorMapping:
    """Regression guards for the Windows taskkill rc -> exception mapping.

    Ensures the shim raises the same exception TYPES the POSIX branch raises
    so callers' ``except (ProcessLookupError, PermissionError, OSError)``
    guards fire uniformly on both platforms. Runs on POSIX by monkeypatching
    IS_WINDOWS + subprocess.run — the mapping is platform-independent code,
    and doing so keeps the Windows security branches regression-guarded on
    the Linux CI fleet.
    """

    @staticmethod
    def _fake_run(rc: int, stderr: bytes = b""):
        def _run(*_a, **_kw):
            r = types.SimpleNamespace(returncode=rc, stdout=b"", stderr=stderr)
            return r

        return _run

    def test_taskkill_rc128_maps_to_process_lookup(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "run", self._fake_run(128, b"process not found"))
        with pytest.raises(ProcessLookupError):
            pc.kill_pid(99999, pc.SIGKILL)
        with pytest.raises(ProcessLookupError):
            pc.kill_process_tree(99999, pc.SIGKILL)

    def test_taskkill_rc5_maps_to_permission_error(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "run", self._fake_run(5, b"access denied"))
        with pytest.raises(PermissionError):
            pc.kill_pid(99999, pc.SIGKILL)
        with pytest.raises(PermissionError):
            pc.kill_process_tree(99999, pc.SIGKILL)

    def test_taskkill_generic_rc_maps_to_oserror(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "run", self._fake_run(42, b"weird error"))
        with pytest.raises(OSError) as ei:
            pc.kill_pid(99999, pc.SIGKILL)
        # not one of the more specific subclasses
        assert not isinstance(ei.value, (ProcessLookupError, PermissionError))

    def test_taskkill_success_returns_true_on_windows(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(pc.subprocess, "run", self._fake_run(0))
        assert pc.kill_pid(99999, pc.SIGKILL) is True
        assert pc.kill_process_tree(99999, pc.SIGKILL) is True

    def test_taskkill_subprocess_error_wraps_as_oserror(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)

        def _boom(*_a, **_kw):
            raise FileNotFoundError(2, "taskkill.exe not found")

        monkeypatch.setattr(pc.subprocess, "run", _boom)
        with pytest.raises(OSError):
            pc.kill_pid(99999, pc.SIGKILL)
        with pytest.raises(OSError):
            pc.kill_process_tree(99999, pc.SIGKILL)


class TestKillAsyncVariants:
    """Regression guards for the async ``kill_pid_async`` / ``kill_process_tree_async``
    variants.

    The async wrappers exist so async call sites can offload the blocking
    Windows ``taskkill`` spawn to :func:`kiro_crew.executors.subprocess_executor`
    without stalling the event loop. The POSIX branch dispatches inline to the
    sync ``kill_pid`` / ``kill_process_tree`` (``os.kill`` / ``os.killpg`` are
    non-blocking, and preserving the same callable keeps existing tests that
    patch the sync entrypoints working). Windows offload is exercised via
    monkeypatching IS_WINDOWS + subprocess.run so the branch is covered on
    the Linux CI fleet.
    """

    def test_posix_kill_pid_async_dispatches_inline_to_kill_pid(self, monkeypatch):
        """POSIX branch: kill_pid_async calls kill_pid synchronously so tests
        that patch platform_compat.kill_pid observe the call unchanged."""
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        seen: list[tuple[int, int]] = []

        def fake_kill_pid(pid: int, sig: int) -> bool:
            seen.append((pid, sig))
            return True

        monkeypatch.setattr(pc, "kill_pid", fake_kill_pid)
        import asyncio as _asyncio

        result = _asyncio.new_event_loop().run_until_complete(pc.kill_pid_async(4242, pc.SIGKILL))
        assert result is True
        assert seen == [(4242, pc.SIGKILL)]

    def test_posix_kill_process_tree_async_dispatches_inline(self, monkeypatch):
        """POSIX branch: kill_process_tree_async calls kill_process_tree inline
        (same-callable dispatch keeps existing patch-based tests working)."""
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        seen: list[tuple[int, int]] = []

        def fake_kill_tree(pid: int, sig: int) -> bool:
            seen.append((pid, sig))
            return True

        monkeypatch.setattr(pc, "kill_process_tree", fake_kill_tree)
        import asyncio as _asyncio

        result = _asyncio.new_event_loop().run_until_complete(
            pc.kill_process_tree_async(9999, pc.SIGTERM)
        )
        assert result is True
        assert seen == [(9999, pc.SIGTERM)]

    def test_posix_kill_pid_async_propagates_process_lookup_error(self, monkeypatch):
        """POSIX branch propagates ProcessLookupError from kill_pid — callers'
        ``except (ProcessLookupError, OSError)`` guards must still fire."""
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)

        def raiser(*_a, **_kw):
            raise ProcessLookupError("gone")

        monkeypatch.setattr(pc, "kill_pid", raiser)
        import asyncio as _asyncio

        loop = _asyncio.new_event_loop()
        with pytest.raises(ProcessLookupError):
            loop.run_until_complete(pc.kill_pid_async(1, pc.SIGKILL))

    def test_windows_kill_pid_async_offloads_via_subprocess_executor(self, monkeypatch):
        """Windows branch: kill_pid_async submits the taskkill spawn to
        subprocess_executor() (so the event loop never blocks on taskkill.exe).

        Monkeypatched on Linux by flipping IS_WINDOWS and stubbing the executor
        to a synchronous callable-runner; asserts the run_in_executor path was
        taken by observing the executor sentinel captured at call time.
        """
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)

        # Fake subprocess_executor sentinel — anything hashable-and-truthy.
        sentinel = object()
        seen_executors: list[object] = []

        # Stub subprocess.run so kill_pid returns success without spawning.
        def fake_run(*_a, **_kw):
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(pc.subprocess, "run", fake_run)

        # Patch the `subprocess_executor` name bound in the platform_compat
        # module namespace (top-level `from kiro_crew.executors import ...`)
        # to return our sentinel.
        monkeypatch.setattr(pc, "subprocess_executor", lambda: sentinel)

        # Intercept the loop's run_in_executor to record which executor is used.
        import asyncio as _asyncio

        real_loop = _asyncio.new_event_loop()

        async def _driver() -> bool:
            loop = _asyncio.get_running_loop()
            orig_rie = loop.run_in_executor

            def spy(executor, func, *args):
                seen_executors.append(executor)
                # Run the callable inline in a completed future so we don't
                # actually need the sentinel to be a real Executor.
                fut: _asyncio.Future[bool] = loop.create_future()
                try:
                    fut.set_result(func(*args))
                except BaseException as exc:  # pragma: no cover — defensive
                    fut.set_exception(exc)
                return fut

            loop.run_in_executor = spy  # type: ignore[method-assign]
            try:
                return await pc.kill_pid_async(1234, pc.SIGKILL)
            finally:
                loop.run_in_executor = orig_rie  # type: ignore[method-assign]

        result = real_loop.run_until_complete(_driver())
        assert result is True
        assert seen_executors == [
            sentinel
        ], f"expected the subprocess_executor sentinel, got {seen_executors!r}"

    def test_windows_kill_process_tree_async_offloads_via_subprocess_executor(self, monkeypatch):
        """Same offload contract as kill_pid_async but for the /T variant."""
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)

        sentinel = object()
        seen_executors: list[object] = []

        monkeypatch.setattr(
            pc.subprocess,
            "run",
            lambda *_a, **_kw: types.SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
        )
        monkeypatch.setattr(pc, "subprocess_executor", lambda: sentinel)

        import asyncio as _asyncio

        real_loop = _asyncio.new_event_loop()

        async def _driver() -> bool:
            loop = _asyncio.get_running_loop()

            def spy(executor, func, *args):
                seen_executors.append(executor)
                fut: _asyncio.Future[bool] = loop.create_future()
                fut.set_result(func(*args))
                return fut

            loop.run_in_executor = spy  # type: ignore[method-assign]
            return await pc.kill_process_tree_async(5678, pc.SIGTERM)

        assert real_loop.run_until_complete(_driver()) is True
        assert seen_executors == [sentinel]

    def test_windows_kill_pid_async_propagates_taskkill_rc128(self, monkeypatch):
        """Windows offload preserves the taskkill rc→exception mapping:
        rc=128 must still surface as ProcessLookupError so the callers'
        ``except (ProcessLookupError, OSError)`` guards fire.
        """
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        _fake_windows_bins(monkeypatch)
        monkeypatch.setattr(
            pc.subprocess,
            "run",
            lambda *_a, **_kw: types.SimpleNamespace(
                returncode=128, stdout=b"", stderr=b"not found"
            ),
        )
        monkeypatch.setattr(pc, "subprocess_executor", lambda: object())

        import asyncio as _asyncio

        async def _driver() -> None:
            loop = _asyncio.get_running_loop()

            def spy(_executor, func, *args):
                fut: _asyncio.Future = loop.create_future()
                try:
                    fut.set_result(func(*args))
                except BaseException as exc:
                    fut.set_exception(exc)
                return fut

            loop.run_in_executor = spy  # type: ignore[method-assign]
            await pc.kill_pid_async(99999, pc.SIGKILL)

        loop = _asyncio.new_event_loop()
        with pytest.raises(ProcessLookupError):
            loop.run_until_complete(_driver())


class TestKillProcessTreePinned:
    """The verified identity must stay PINNED for the whole terminate.

    ``kill_process_tree`` addresses the target by PID, and on Windows it does so
    from a separate ``taskkill`` process. A caller that only read the start time
    first has released every handle by then, so the process can exit and Windows
    can recycle the PID onto an unrelated one in between -- which
    ``taskkill /T /F /PID`` would then tear down with its whole tree. Windows
    keeps a process ID reserved while ANY handle to the process object is open,
    so holding the query handle that verified the identity across the terminate
    is what makes the PID still mean the same process when taskkill resolves it.

    Driven through the module seams with ``IS_WINDOWS`` patched, so every case
    runs on every platform: the invariant is about handle LIFETIME, not about
    which OS the test host happens to be.
    """

    HANDLE = 4242

    # flake-ok: no test in this class calls monkeypatch.undo(), so the shared monkeypatch cannot lift this pin
    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this class calls monkeypatch.undo()
    def _isolate_pending_registry(self, monkeypatch):
        # kill_process_tree_pinned now transfers the pinned root into the
        # process-owned pending registry. Give each case its own so no fake
        # pending state leaks between tests or into an unrelated one.
        monkeypatch.setattr(pc, "_PENDING_WINDOWS_TREE_CLEANUPS", {})
        monkeypatch.setattr(pc, "_WINDOWS_TREE_ADMISSIONS", set())

    def _wire(self, monkeypatch, *, handle=HANDLE, identity=(4321, 777, None)):
        """Patch the seams; return (opened, closed, killed) recorders."""
        opened: list[int] = []
        closed: list[int] = []
        killed: list[tuple[int, int]] = []

        monkeypatch.setattr(pc, "IS_WINDOWS", True)

        def _open(pid):
            opened.append(pid)
            return handle

        def _identity(h):
            assert h == handle, "the identity must be read from the handle just opened"
            return identity

        def _close(h):
            closed.append(h)

        def _kill(handle_arg):
            # terminate_windows_process_tree_owned OWNS the handle it is given:
            # on success it closes it once, on refusal it retains it. Model both
            # against the same handle production passed, so the closure/retention
            # assertions still observe close_process_handle.
            killed.append((handle_arg, pc.SIGTERM))
            _close(handle_arg)
            return True

        monkeypatch.setattr(pc, "_open_process_termination_handle", _open)
        monkeypatch.setattr(pc, "_windows_process_handle_identity", _identity)
        monkeypatch.setattr(pc, "close_process_handle", _close)
        monkeypatch.setattr(
            pc,
            "_advance_owned_windows_tree",
            lambda state: (_kill(state.handles[state.root_pid]), True),
        )
        return opened, closed, killed

    def test_a_matching_identity_kills_and_then_releases_the_handle(self, monkeypatch):
        opened, closed, killed = self._wire(monkeypatch)

        assert pc.kill_process_tree_pinned(4321, "777", pc.SIGTERM) is True

        assert opened == [4321]
        assert killed == [(self.HANDLE, pc.SIGTERM)]
        assert closed == [self.HANDLE]

    def test_a_mismatched_identity_never_invokes_the_kill(self, monkeypatch):
        """The pid was recycled: refuse, and do not spawn taskkill at all.

        Asserting on "no kill" rather than on the return value is the point --
        a terminate that ran and then failed would still have torn down whatever
        now owns the pid.
        """
        _, closed, killed = self._wire(monkeypatch, identity=(4321, 999, None))

        assert pc.kill_process_tree_pinned(4321, "777", pc.SIGKILL) is False

        assert killed == []
        assert closed == [self.HANDLE], "the handle must still be released"

    def test_an_unopenable_process_never_invokes_the_kill(self, monkeypatch):
        """No handle means no pin, and an unpinned pid must not be killed."""
        _, closed, killed = self._wire(monkeypatch, handle=None)

        assert pc.kill_process_tree_pinned(4321, "777", pc.SIGTERM) is False

        assert killed == []
        assert closed == [], "nothing was opened, so nothing may be closed"

    def test_an_unreadable_identity_never_invokes_the_kill(self, monkeypatch):
        """A handle that cannot answer WHO it is confirms nothing."""
        _, closed, killed = self._wire(monkeypatch, identity=None)

        assert pc.kill_process_tree_pinned(4321, "777", pc.SIGTERM) is False

        assert killed == []
        assert closed == [self.HANDLE]

    def test_the_handle_is_still_open_while_the_kill_is_in_flight(self, monkeypatch):
        """The invariant itself, observed rather than inferred.

        The fake terminate is gated on an event, so the assertion runs at a
        moment that is CAUSALLY inside the kill rather than at a moment chosen by
        a sleep. The two ``wait`` calls are bounded hang guards; nothing asserts
        on elapsed time.
        """
        closed: list[int] = []
        entered = threading.Event()
        release = threading.Event()
        seen_closed_during_kill: list[list[int]] = []

        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_open_process_termination_handle", lambda pid: self.HANDLE)
        monkeypatch.setattr(pc, "_windows_process_handle_identity", lambda h: (4321, 777, None))
        monkeypatch.setattr(pc, "close_process_handle", closed.append)

        def _gated_kill(handle):
            assert handle == self.HANDLE
            seen_closed_during_kill.append(list(closed))
            entered.set()
            assert release.wait(10), "the gate was never released"
            # The owned drain OWNS the handle and closes it once, on return.
            closed.append(handle)
            return True

        monkeypatch.setattr(
            pc,
            "_advance_owned_windows_tree",
            lambda state: (_gated_kill(state.handles[state.root_pid]), True),
        )

        result: list[bool] = []
        worker = threading.Thread(
            target=lambda: result.append(pc.kill_process_tree_pinned(4321, "777"))
        )
        worker.start()
        try:
            assert entered.wait(10), "the kill never started"
            assert closed == [], (
                "the handle was released while taskkill was still in flight -- "
                "the pid is unpinned for exactly the window this exists to close"
            )
        finally:
            release.set()
            worker.join(10)

        assert not worker.is_alive()
        assert seen_closed_during_kill == [[]]
        assert result == [True]
        assert closed == [self.HANDLE], "released once the kill returned"

    def test_the_handle_is_retained_for_retry_when_the_kill_raises(self, monkeypatch):
        """A failing drain must RETAIN the pinned handle, not leak or drop it.

        Under the owned model the pinned root is transferred into the process
        pending registry; a raised drain leaves it there with its handle open so
        the incarnation stays pinned and the next maintenance tick can finish it.
        Closing on the raise would unpin the pid mid-failure — the very reuse
        window this change closes — so the contract is deliberate retention, and
        a later successful drain is what releases the handle, exactly once.
        """
        closed: list[int] = []
        first = [True]
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_open_process_termination_handle", lambda pid: self.HANDLE)
        monkeypatch.setattr(
            pc,
            "_windows_process_handle_identity",
            lambda h: (4321, 777, None if first[0] else 888),
        )
        monkeypatch.setattr(pc, "close_process_handle", closed.append)

        def _descendants(pid, retained, root_handle):
            if first[0]:
                raise ProcessLookupError("gone between the pin and the signal")
            return {}

        monkeypatch.setattr(pc, "descendant_termination_handles", _descendants)

        # First attempt: the drain raises, so the handle is RETAINED, not closed.
        with pytest.raises(ProcessLookupError):
            pc.kill_process_tree_pinned(4321, "777")
        assert closed == [], "a failed drain must not close (unpin) the handle"
        assert len(pc._PENDING_WINDOWS_TREE_CLEANUPS) == 1

        # A later maintenance retry completes and releases the handle exactly once.
        first[0] = False
        assert pc.retry_pending_windows_process_trees() == (4321,)
        assert closed == [self.HANDLE]
        assert pc._PENDING_WINDOWS_TREE_CLEANUPS == {}

    def test_posix_delegates_straight_through(self, monkeypatch):
        """POSIX is unchanged: no handle exists to hold, so none is sought.

        ``os.killpg`` is issued in-process by the same interpreter that did the
        check. Introducing a Windows-shaped pin here would change a path this
        finding is not about.
        """
        killed: list[tuple[int, int]] = []
        opened: list[int] = []

        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "_open_process_query_handle", opened.append)
        monkeypatch.setattr(
            pc, "kill_process_tree", lambda pid, sig: killed.append((pid, sig)) or True
        )

        assert pc.kill_process_tree_pinned(4321, "anything", pc.SIGTERM) is True

        assert killed == [(4321, pc.SIGTERM)]
        assert opened == [], "no handle work on POSIX"

    def test_the_pinned_identity_is_the_same_half_process_start_time_returns(self, monkeypatch):
        """Both sides must read the CREATION half, or the comparison is nonsense.

        ``process_start_time`` records ``str(identity[1])``; if the pin compared
        a different element the guard would refuse every legitimate reap while
        reporting itself as working.
        """
        # ``process_start_time`` checks ``sys.platform == "linux"`` BEFORE
        # ``IS_WINDOWS``, so on a Linux runner the /proc arm answers None for a
        # pid that does not exist and the patched Windows arm is never reached.
        # Steering the platform too is what keeps this case host-independent --
        # the same technique test_app_backend_stale_reap uses to model a
        # ps-less host.
        monkeypatch.setattr(pc.sys, "platform", "win32")
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_open_process_query_handle", lambda pid: self.HANDLE)
        monkeypatch.setattr(pc, "_open_process_termination_handle", lambda pid: self.HANDLE)
        monkeypatch.setattr(pc, "_close_process_handle", lambda h: None)
        monkeypatch.setattr(pc, "close_process_handle", lambda h: None)
        monkeypatch.setattr(
            pc,
            "_windows_process_handle_identity",
            lambda h, **_mode: (4321, 777, 888),
        )
        monkeypatch.setattr(pc, "_advance_owned_windows_tree", lambda state: (True, True))

        recorded = pc.process_start_time(4321)

        assert recorded == "777"
        assert pc.kill_process_tree_pinned(4321, recorded) is True
        # The exit half moves as the process dies and must never be the identity.
        assert pc.kill_process_tree_pinned(4321, "888") is False


class TestKillPidPinned:
    """Single-process variant of the pinned kill: same handle-lifetime
    invariant as :class:`TestKillProcessTreePinned`, delegating to ``kill_pid``
    instead of the tree teardown. Driven through the module seams with
    ``IS_WINDOWS`` patched so every case runs on every platform."""

    HANDLE = 4242

    def _wire(self, monkeypatch, *, handle=HANDLE, identity=(4321, 777, None)):
        opened: list[int] = []
        closed: list[int] = []
        killed: list[tuple[int, int]] = []

        monkeypatch.setattr(pc, "IS_WINDOWS", True)

        def _open(pid):
            opened.append(pid)
            return handle

        def _identity(h):
            assert h == handle, "the identity must be read from the handle just opened"
            return identity

        def _close(h):
            closed.append(h)

        def _kill(pid, sig):
            killed.append((pid, sig))
            return True

        monkeypatch.setattr(pc, "_open_process_query_handle", _open)
        monkeypatch.setattr(pc, "_windows_process_handle_identity", _identity)
        monkeypatch.setattr(pc, "_close_process_handle", _close)
        monkeypatch.setattr(pc, "kill_pid", _kill)
        return opened, closed, killed

    def test_a_matching_identity_kills_and_then_releases_the_handle(self, monkeypatch):
        opened, closed, killed = self._wire(monkeypatch)

        assert pc.kill_pid_pinned(4321, "777", pc.SIGTERM) is True

        assert opened == [4321]
        assert killed == [(4321, pc.SIGTERM)]
        assert closed == [self.HANDLE]

    def test_a_mismatched_identity_never_invokes_the_kill(self, monkeypatch):
        _, closed, killed = self._wire(monkeypatch, identity=(4321, 999, None))

        assert pc.kill_pid_pinned(4321, "777", pc.SIGKILL) is False

        assert killed == []
        assert closed == [self.HANDLE], "the handle must still be released"

    def test_an_unopenable_process_never_invokes_the_kill(self, monkeypatch):
        _, closed, killed = self._wire(monkeypatch, handle=None)

        assert pc.kill_pid_pinned(4321, "777", pc.SIGTERM) is False

        assert killed == []
        assert closed == [], "nothing was opened, so nothing may be closed"

    def test_an_unreadable_identity_never_invokes_the_kill(self, monkeypatch):
        _, closed, killed = self._wire(monkeypatch, identity=None)

        assert pc.kill_pid_pinned(4321, "777", pc.SIGTERM) is False

        assert killed == []
        assert closed == [self.HANDLE]

    def test_posix_delegates_straight_through(self, monkeypatch):
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "kill_pid", lambda pid, sig: killed.append((pid, sig)) or True)

        assert pc.kill_pid_pinned(4321, "777", pc.SIGTERM) is True
        assert killed == [(4321, pc.SIGTERM)]


class TestKillAndReap:
    """The shared kill-the-tree + bounded-pipe-draining-reap helper."""

    @staticmethod
    def _proc(pid: int = 4242):
        from unittest import mock

        proc = mock.MagicMock()
        proc.pid = pid
        # Default to a still-running child (asyncio has not recorded an exit),
        # which is the case the group kill fires for. A reaped child
        # (``returncode is not None``) is covered by its own test.
        proc.returncode = None
        proc.kill = mock.MagicMock()
        proc.communicate = mock.AsyncMock(return_value=(b"", b""))
        proc.wait = mock.AsyncMock()
        return proc

    @pytest.mark.asyncio
    async def test_kills_the_whole_tree_then_the_pid(self) -> None:
        """A spawned command is often a shell line, so the whole group must be
        signalled; the pid-scoped kill backs up a group signal that missed."""
        from unittest import mock

        proc = self._proc(pid=4242)
        with mock.patch.object(pc, "kill_process_tree_async", mock.AsyncMock()) as tree:
            await pc.kill_and_reap(proc)
        tree.assert_awaited_once()
        assert tree.await_args.args == (4242, pc.SIGKILL)
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_the_group_kill_for_a_same_group_child(self) -> None:
        """A child sharing OUR group leads no tree, so the group signal is
        skipped and the pid-scoped kill covers it -- otherwise every routine
        timeout would trip ``kill_process_tree``'s broadcast refusal.

        Also the escape hatch for the rootdir conftest's autouse pin of this
        probe: a test that wants the skip patches the seam itself and wins.
        """
        from unittest import mock

        proc = self._proc()
        with (
            mock.patch.object(pc, "_shares_own_process_group", lambda _pid: True),
            mock.patch.object(pc, "kill_process_tree_async", mock.AsyncMock()) as tree,
        ):
            await pc.kill_and_reap(proc)
        tree.assert_not_awaited()
        proc.kill.assert_called_once()
        proc.communicate.assert_awaited_once()

    @pytest.mark.skipif(not pc.IS_POSIX, reason="POSIX only")
    def test_group_probe_reports_our_own_group(self) -> None:
        """Our own pid is in our own group by construction."""
        assert _real_shares_own_process_group(os.getpid()) is True

    @pytest.mark.skipif(not pc.IS_POSIX, reason="POSIX only")
    def test_group_probe_fails_closed_for_an_unreadable_pid(self, monkeypatch) -> None:
        """Fail-closed, so a vanished or unreadable pid still gets its tree
        signalled rather than silently skipping the kill."""
        monkeypatch.setattr(
            pc.os,
            "getpgid",
            lambda _pid: (_ for _ in ()).throw(ProcessLookupError()),
        )
        assert _real_shares_own_process_group(4242) is False

    def test_group_probe_is_posix_only(self, monkeypatch) -> None:
        """Windows has no process groups to compare, so nothing is ever skipped
        there -- and the probe must not reach a missing ``os.getpgid``.

        This case runs on EVERY platform on purpose -- the non-POSIX branch is
        what it covers -- so the tripwire is installed with ``raising=False``:
        ``os.getpgid`` is Unix-only, and a strict ``setattr`` raises
        ``AttributeError`` during the test's own arrangement on Windows, which
        is where the assertion matters most. With ``raising=False`` the sentinel
        is created where the attribute is absent, never called (that is the
        assertion), and removed at teardown.
        """
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(
            pc.os,
            "getpgid",
            lambda _pid: (_ for _ in ()).throw(AssertionError("probed on Windows")),
            raising=False,
        )
        assert _real_shares_own_process_group(os.getpid()) is False

    @pytest.mark.asyncio
    async def test_reaps_via_communicate_never_wait(self) -> None:
        """The reap must drain the pipes: a killed child blocked writing into
        a full pipe makes a bare ``wait()`` hang the calling task forever."""
        from unittest import mock

        proc = self._proc()
        with mock.patch.object(pc, "kill_process_tree_async", mock.AsyncMock()):
            await pc.kill_and_reap(proc)
        proc.communicate.assert_awaited_once()
        proc.wait.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bounds_the_reap(self) -> None:
        """A descendant that ignores SIGKILL's effects on the pipe (e.g. an
        inherited fd held open) must not turn cleanup into a hang."""
        import asyncio
        from unittest import mock

        assert 0 < pc.REAP_TIMEOUT_SECS <= 30
        proc = self._proc(pid=1)

        async def _never_returns():
            # flake-ok: stands in for a communicate that never returns; the reap's own timeout cancels it
            # nosemgrep: kirocrew.test-sleep-as-barrier -- stands in for a communicate that never returns; the reap's own timeout cancels it
            await asyncio.sleep(3600)

        proc.communicate = _never_returns
        with mock.patch.object(pc, "kill_process_tree_async", mock.AsyncMock()):
            # Outer bound is a hang detector only; the helper's own bound
            # (passed explicitly) is what must return first.
            await asyncio.wait_for(pc.kill_and_reap(proc, timeout=0.01), timeout=30)

    @pytest.mark.asyncio
    async def test_tolerates_dead_child_and_mock_pids(self) -> None:
        """Best-effort throughout: an already-exited child (or a non-int test
        pid refused by the broadcast guard) must not mask the caller's own
        timeout or cancellation handling."""
        from unittest import mock

        proc = mock.MagicMock()  # pid is a MagicMock -> tree kill refuses it
        proc.kill = mock.MagicMock(side_effect=ProcessLookupError())
        proc.communicate = mock.AsyncMock(side_effect=RuntimeError("already reaped"))
        await pc.kill_and_reap(proc)

    @pytest.mark.asyncio
    async def test_repeat_cancellation_does_not_abandon_cleanup(self) -> None:
        """A second Task.cancel() landing mid-cleanup is a BaseException that
        escapes ``suppress(Exception)``: without the shield it aborts the
        cleanup before the reap, leaving the killed child un-drained. The
        helper must finish the kill + reap, then re-deliver the cancellation
        exactly once."""
        import asyncio
        from unittest import mock

        started = asyncio.Event()
        release = asyncio.Event()
        events: list[str] = []

        class Proc:
            pid = 4242
            returncode = None  # a live child: the group kill fires

            def kill(self):
                events.append("killed")

            async def communicate(self):
                events.append("reap-started")
                started.set()
                # flake-ok: the stub parks on an event the test sets after its two cancellations
                await release.wait()
                events.append("reaped")
                return b"", b""

        async def _caller():
            with mock.patch.object(pc, "kill_process_tree_async", mock.AsyncMock()):
                await pc.kill_and_reap(Proc())

        task = asyncio.ensure_future(_caller())
        # flake-ok: the reap stub sets this event the moment the task reaches it
        await started.wait()
        task.cancel()
        # flake-ok: a zero-delay yield lets the first cancellation reach the reap
        await asyncio.sleep(0)
        task.cancel()  # a repeat cancellation must not abort the reap
        # flake-ok: a zero-delay yield lets the repeat cancellation reach the reap
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            # flake-ok: awaits the task that release.set() above unblocked, so it is finishing
            await task
        assert events == ["killed", "reap-started", "reaped"]

    @pytest.mark.asyncio
    async def test_skips_the_tree_kill_for_a_reaped_child(self) -> None:
        """The recycled-pid guard: once asyncio has recorded the child's
        exit (``returncode is not None``) the OS may hand that pid to an
        unrelated process -- even another of our own gateway children, whose
        parent pid would also be ours -- so a pid-addressed group SIGKILL could
        reach a stranger's tree. The group kill must be skipped; only the
        pid-scoped ``kill()`` (harmless on a dead handle) runs. The same-group
        probe is pinned to ``False`` (its routine value) so the returncode
        guard alone gates the branch."""
        from unittest import mock

        proc = self._proc(pid=4242)
        proc.returncode = 0  # asyncio has already reaped this child
        with (
            mock.patch.object(pc, "_shares_own_process_group", lambda _pid: False),
            mock.patch.object(pc, "kill_process_tree_async", mock.AsyncMock()) as tree,
        ):
            await pc.kill_and_reap(proc)
        tree.assert_not_awaited()
        proc.kill.assert_called_once()
        proc.communicate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fires_the_tree_kill_for_a_live_child(self) -> None:
        """The complement: a child asyncio has NOT reaped (``returncode is
        None``) still owns its pid, so the group kill fires exactly as before.
        This is the branch the recycled-pid guard must not disturb."""
        from unittest import mock

        proc = self._proc(pid=4242)
        assert proc.returncode is None  # a live child
        with (
            mock.patch.object(pc, "_shares_own_process_group", lambda _pid: False),
            mock.patch.object(pc, "kill_process_tree_async", mock.AsyncMock()) as tree,
        ):
            await pc.kill_and_reap(proc)
        tree.assert_awaited_once()
        assert tree.await_args.args == (4242, pc.SIGKILL)
        proc.kill.assert_called_once()


class TestTerminateAndReapSyncReapedChild:
    """The blocking twin of the reaped-child guard in :func:`kill_and_reap`."""

    @pytest.mark.parametrize("live", [True, False])
    def test_the_tree_kill_addresses_only_a_child_that_is_not_reaped(self, live) -> None:
        """``terminate_and_reap_sync``'s fallback (Windows, a reaped child, or
        one outside its own group) kills by pid. Once ``poll()`` has reaped the
        child its pid may name another process, so only an unreaped child's
        tree is killed. The group and same-group probes are pinned so the reap
        check alone decides."""
        from unittest import mock

        proc = mock.MagicMock()
        proc.pid = 99_999_999_999  # above every pid_max: never a live process
        proc.poll.return_value = None if live else 0
        with (
            mock.patch.object(pc, "_isolated_group_of_live_child", lambda *a, **k: None),
            mock.patch.object(pc, "_shares_own_process_group", lambda _pid: False),
            mock.patch.object(pc, "kill_process_tree") as tree,
        ):
            pc.terminate_and_reap_sync(proc, grace=0.1, reap_timeout=0.1)
        if live:
            tree.assert_called_once_with(99_999_999_999, pc.SIGKILL)
        else:
            tree.assert_not_called()
