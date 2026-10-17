"""Coverage-raising unit tests for :mod:`kiro_crew.platform_compat`: the failure returns.

Lock release failures, the start-time identity and ppid failure returns, trusted
binaries and the process-table snapshots, PID-recycling lifetime validation, the kill
paths, the junction fallback and the Win32 per-pid RSS failure.

Part of the suite ``test_platform_compat_coverage.py`` heads, and written its way: a
platform branch the Linux CI host never reaches is simulated by flipping
``IS_WINDOWS``/``IS_POSIX`` and faking the DLL entry points, every clock the product
consults is replaced, and nothing spawns a process or writes outside ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import subprocess
import sys
import types
from typing import Any

import pytest
from platform_compat_coverage_helpers import (
    _LINUX_ONLY,
    _POSIX_ONLY,
    _const,
    _fake_windows,
    _FakeMsvcrt,
    _Fn,
    _StubbornStream,
    _toolhelp_kernel32,
)

from kiro_crew import platform_compat as pc

# The whole-module skip ``test_platform_compat_coverage.py`` applies, for the reasons given
# there.
pytestmark = [
    _POSIX_ONLY,
    # This file replaces ctypes.CDLL, find_library or a cached libc handle for a test,
    # so the shared macOS handle caches are put back after each one (see conftest.py).
    pytest.mark.usefixtures("fresh_darwin_libc_handles"),
]


# ---------------------------------------------------------------------------
# Lock release failures
#
# Every release path swallows its error on purpose: it runs from a ``finally``,
# where raising would replace the real exception with a bookkeeping one. Forcing
# the failure is the only way to execute those handlers, since a real fd never
# refuses to unlock.
# ---------------------------------------------------------------------------


_POSIX_ONLY = pytest.mark.skipif(
    pc.IS_WINDOWS, reason="drives the fcntl branch, which does not exist on Windows"
)


def _lock_fd(tmp_path: Any, name: str = "release.lock") -> int:
    """A real fd, because the lock helpers ``lseek`` before locking."""

    return os.open(str(tmp_path / name), os.O_RDWR | os.O_CREAT, 0o600)


class _UnlockRefusingMsvcrt(_FakeMsvcrt):
    """``msvcrt`` stand-in whose byte-range UNLOCK fails."""

    def locking(self, _fd: int, mode: int, _nbytes: int) -> None:
        self.calls.append(mode)
        if mode == self.LK_UNLCK:
            raise OSError(13, "unlock refused")


class TestLockReleaseFailures:
    @_POSIX_ONLY
    def test_file_lock_body_result_survives_a_failed_posix_release(self, monkeypatch, tmp_path):
        real_flock = pc.fcntl.flock

        def _flock(fd: int, operation: int) -> None:
            if operation == pc.fcntl.LOCK_UN:
                raise OSError(errno.EBADF, "bad file descriptor")
            real_flock(fd, operation)

        monkeypatch.setattr(pc.fcntl, "flock", _flock)
        fd = _lock_fd(tmp_path)
        ran = False
        try:
            with pc.file_lock(fd, exclusive=True):
                ran = True
        finally:
            os.close(fd)
        assert ran, "a failed release must not mask the body's completion"

    @_POSIX_ONLY
    def test_release_lock_swallows_a_posix_failure(self, monkeypatch, tmp_path):
        def _boom(_fd: int, _operation: int) -> None:
            raise OSError(errno.EBADF, "bad file descriptor")

        monkeypatch.setattr(pc.fcntl, "flock", _boom)
        fd = _lock_fd(tmp_path, "release2.lock")
        try:
            pc.release_lock(fd)  # must not raise
        finally:
            os.close(fd)

    def test_file_lock_swallows_a_failed_windows_release(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        fake = _UnlockRefusingMsvcrt([True])
        monkeypatch.setattr(pc, "msvcrt", fake, raising=False)
        monkeypatch.setattr(pc, "_win_acquire_blocking", lambda _fd: True)
        fd = _lock_fd(tmp_path, "release3.lock")
        try:
            with pc.file_lock(fd):
                pass
        finally:
            os.close(fd)
        assert _FakeMsvcrt.LK_UNLCK in fake.calls

    def test_release_lock_swallows_a_failed_windows_unlock(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "msvcrt", _UnlockRefusingMsvcrt([True]), raising=False)
        fd = _lock_fd(tmp_path, "release4.lock")
        try:
            pc.release_lock(fd)  # must not raise
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# Start-time identity and ppid -- the remaining failure returns
# ---------------------------------------------------------------------------


class TestProcessStartIdRemainingPaths:
    @_LINUX_ONLY
    def test_reads_a_stable_identity_for_a_live_process(self):
        # Field 22 of /proc/<pid>/stat, parsed after the LAST ')' because comm
        # may itself contain spaces and parens.
        first = pc.get_process_start_id(os.getpid())
        assert first is not None and first.isdigit()
        # Stable for the process lifetime -- the property every recycle guard
        # persists and re-compares from a different process.
        assert pc.get_process_start_id(os.getpid()) == first

    @_LINUX_ONLY
    def test_reports_unknown_for_a_pid_with_no_proc_entry(self):
        # "Unknown" must not be read as a mismatch by callers.
        assert pc.get_process_start_id(2_000_000_000) is None

    def test_macos_swallows_a_loader_error(self, monkeypatch):
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        monkeypatch.setattr(pc.ctypes.util, "find_library", lambda _n: "libproc.dylib")

        def _boom(_path: Any) -> Any:
            raise OSError("cannot load libproc")

        monkeypatch.setattr(pc.ctypes, "CDLL", _boom)
        assert pc.get_process_start_id(99) is None


class TestGetPpidWindowsLoaderFailure:
    def test_reports_failure_when_kernel32_cannot_be_reached(self, monkeypatch):
        # Introspection must never raise into a caller's kill path, however the
        # Win32 boundary fails.
        _fake_windows(monkeypatch)  # no kernel32 -> the attribute lookup raises
        monkeypatch.setattr(pc.sys, "platform", "win32")
        assert pc.get_ppid(77) == -1


# ---------------------------------------------------------------------------
# Trusted binary resolution and the process-table snapshots
# ---------------------------------------------------------------------------


class TestEnsureUtf8ConsoleRewrapFailure:
    def test_a_refused_rewrap_is_tolerated(self, monkeypatch):
        # Best-effort by design: a stream that can neither be reconfigured nor
        # re-wrapped is left as it is, because raising here would kill the
        # gateway before it binds -- the very failure this function prevents.
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(sys, "stdout", _StubbornStream())
        monkeypatch.setattr(sys, "stderr", _StubbornStream())

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise ValueError("cannot wrap this buffer")

        monkeypatch.setattr(pc.io, "TextIOWrapper", _boom)
        pc.ensure_utf8_console()  # must not raise


class TestTrustedSystemBinWindowsSuffixes:
    def test_a_bare_argv_name_resolves_with_the_loader_suffix(self, monkeypatch, tmp_path):
        # Windows argv carries a bare name (``taskkill``) while the file on disk
        # carries an extension, so the pinned lookup has to try the suffixes the
        # loader would rather than requiring callers to spell them.
        _fake_windows(monkeypatch)
        monkeypatch.setattr(pc, "_windows_system_dirs", lambda: (str(tmp_path),))
        planted = tmp_path / "taskkill.exe"
        planted.write_text("")
        planted.chmod(0o755)
        resolved = pc.trusted_system_bin("taskkill")
        assert resolved is not None and resolved.endswith("taskkill.exe")


class TestPosixParentMapParsing:
    def test_returns_empty_on_windows(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        assert pc._posix_process_parent_map() == {}

    def test_skips_malformed_rows_instead_of_aborting(self, monkeypatch):
        # ``ps`` output is not a schema: a truncated or non-numeric row must cost
        # only that row, or one oddity erases the whole tree a kill depends on.
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _name: "/usr/bin/ps")
        blob = b"  100    1\nshort\nnot a number\n  101  100\n\n"
        monkeypatch.setattr(pc.subprocess, "check_output", lambda *_a, **_k: blob)
        assert pc._posix_process_parent_map() == {100: 1, 101: 100}

    def test_degrades_to_empty_when_ps_cannot_run(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _name: "/usr/bin/ps")

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise subprocess.TimeoutExpired("ps", 5)

        monkeypatch.setattr(pc.subprocess, "check_output", _boom)
        assert pc._posix_process_parent_map() == {}


class TestWindowsParentMapWalk:
    def test_returns_empty_off_windows(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        assert pc._windows_process_parent_map() == {}

    def test_walks_the_whole_snapshot_without_the_optional_error_hooks(self, monkeypatch):
        # The kernel32 handle this code holds may not expose SetLastError /
        # GetLastError (stub loaders, Wine); the walk must still end cleanly
        # rather than mistake a missing entry point for an enumeration failure
        # and raise into a kill path.
        _fake_windows(
            monkeypatch,
            kernel32=_toolhelp_kernel32(
                [(4, 0, b"System"), (100, 4, b"gateway.exe"), (101, 100, b"kiro-cli.exe")]
            ),
        )
        assert pc._windows_process_parent_map() == {4: 0, 100: 4, 101: 100}

    def test_reads_no_more_files_through_the_real_error_hooks(self, monkeypatch):
        # The path a real Windows host takes: the error code is cleared before
        # each step and read back after, so end-of-enumeration (ERROR_NO_MORE_FILES
        # = 18) is told apart from a genuine failure mid-walk.
        kernel32 = _toolhelp_kernel32([(100, 4, b"gateway.exe")])
        kernel32.SetLastError = _const(True)
        kernel32.GetLastError = _const(18)
        _fake_windows(monkeypatch, kernel32=kernel32)
        monkeypatch.setattr(pc.ctypes, "set_last_error", lambda _code: 0, raising=False)
        assert pc._windows_process_parent_map() == {100: 4}


# ---------------------------------------------------------------------------
# PID-recycling lifetime validation
# ---------------------------------------------------------------------------


class TestWindowsLineageLifetimes:
    """Pure lifetime arithmetic over a Toolhelp snapshot -- the part that decides
    whether a numeric parent link describes the tree we actually spawned.

    Toolhelp never clears ``th32ParentProcessID`` when a parent dies and Windows
    recycles PIDs aggressively, so without this every kill could widen onto an
    unrelated subtree that merely inherited the number.
    """

    ROOT = 100

    def test_accepts_a_direct_child_created_during_the_root(self):
        identities = {self.ROOT: (self.ROOT, 10, None), 101: (101, 15, None)}
        assert (
            pc._windows_lineage_matches_lifetimes(101, self.ROOT, {101: self.ROOT}, identities)
            is True
        )

    def test_accepts_a_grandchild_through_a_validated_chain(self):
        parent_map = {101: self.ROOT, 102: 101}
        identities = {
            self.ROOT: (self.ROOT, 10, None),
            101: (101, 15, 40),  # an immediate launcher that has already exited
            102: (102, 20, None),
        }
        assert pc._windows_lineage_matches_lifetimes(102, self.ROOT, parent_map, identities) is True

    def test_the_root_is_trivially_its_own_lineage(self):
        assert pc._windows_lineage_matches_lifetimes(self.ROOT, self.ROOT, {}, {}) is True

    def test_rejects_a_broken_parent_link(self):
        identities = {self.ROOT: (self.ROOT, 10, None), 101: (101, 15, None)}
        assert pc._windows_lineage_matches_lifetimes(101, self.ROOT, {}, identities) is False

    def test_rejects_a_child_with_no_exact_handle(self):
        identities = {self.ROOT: (self.ROOT, 10, None)}
        assert (
            pc._windows_lineage_matches_lifetimes(101, self.ROOT, {101: self.ROOT}, identities)
            is False
        )

    def test_rejects_a_root_whose_handle_names_another_process(self):
        identities = {self.ROOT: (999, 10, None), 101: (101, 15, None)}
        assert (
            pc._windows_lineage_matches_lifetimes(101, self.ROOT, {101: self.ROOT}, identities)
            is False
        )

    def test_rejects_a_handle_that_names_a_different_pid(self):
        # A handle whose GetProcessId disagrees with the Toolhelp row IS the
        # recycled-PID case this check exists for.
        identities = {self.ROOT: (self.ROOT, 10, None), 101: (999, 15, None)}
        assert (
            pc._windows_lineage_matches_lifetimes(101, self.ROOT, {101: self.ROOT}, identities)
            is False
        )

    def test_rejects_a_child_older_than_its_parent(self):
        identities = {self.ROOT: (self.ROOT, 30, None), 101: (101, 10, None)}
        assert (
            pc._windows_lineage_matches_lifetimes(101, self.ROOT, {101: self.ROOT}, identities)
            is False
        )

    def test_rejects_a_child_created_after_the_parent_exited(self):
        # Same numeric parent, but the child cannot belong to a process that had
        # already exited -- the PID was reused.
        identities = {self.ROOT: (self.ROOT, 10, 20), 101: (101, 25, None)}
        assert (
            pc._windows_lineage_matches_lifetimes(101, self.ROOT, {101: self.ROOT}, identities)
            is False
        )

    def test_a_lifetime_consistent_cycle_still_terminates(self):
        # Equal creation times make every edge pass the lifetime checks, so only
        # the visited-set guard can stop the walk. Without it this hangs a kill
        # path on a snapshot that PID reuse made cyclic.
        parent_map = {101: 102, 102: 101}
        identities = {
            self.ROOT: (self.ROOT, 15, None),
            101: (101, 15, None),
            102: (102, 15, None),
        }
        assert (
            pc._windows_lineage_matches_lifetimes(101, self.ROOT, parent_map, identities) is False
        )


class TestDescendantHandleScanCleanup:
    def test_refuses_a_reserved_or_non_int_root_pid(self):
        # Nothing about this call is safe if the root is not a real PID we own:
        # pid 1 would anchor the walk at init and sweep in the whole session.
        for candidate in (1, 0, True):
            with pytest.raises(ValueError, match="reserved pid"):
                pc.descendant_termination_handles(candidate, {}, 8001)

    def test_returns_empty_off_windows(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        assert pc.descendant_termination_handles(100, {}, 8001) == {}

    def test_requires_an_exact_root_handle(self, monkeypatch):
        # A numeric PID alone cannot anchor the walk -- it is the recycle-prone
        # identifier this whole function exists to stop trusting.
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        for candidate in (None, 0, -1):
            with pytest.raises(ValueError, match="exact root handle required"):
                pc.descendant_termination_handles(100, {}, candidate)

    def test_refuses_a_root_handle_that_names_another_process(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_windows_process_handle_identity", lambda _h: (999, 1, None))
        with pytest.raises(ValueError, match="root handle identity mismatch"):
            pc.descendant_termination_handles(100, {}, 8001)

    def test_closes_every_opened_handle_when_the_scan_fails(self, monkeypatch):
        # The caller never sees these handles, so a leak here is unreachable and
        # permanent -- and it pins the exited PID's kernel object alive, which is
        # what makes a recycled PID look live.
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        maps: list[Any] = [{101: 100}, OSError("snapshot vanished")]

        def _parent_map() -> Any:
            head = maps.pop(0)
            if isinstance(head, Exception):
                raise head
            return head

        closed: list[int] = []
        monkeypatch.setattr(pc, "_windows_process_parent_map", _parent_map)
        monkeypatch.setattr(pc, "_open_process_termination_handle", lambda _pid, **_k: 9001)
        monkeypatch.setattr(
            pc,
            "_windows_process_handle_identity",
            {8001: (100, 10, None), 9001: (101, 15, None)}.get,
        )
        monkeypatch.setattr(pc, "close_process_handle", closed.append)
        with pytest.raises(OSError, match="snapshot vanished"):
            pc.descendant_termination_handles(100, {}, 8001)
        assert closed == [9001]

    @pytest.mark.parametrize("outcome", ["live", "unknown", "gone"])
    def test_unopenable_child_requires_proven_absence(self, monkeypatch, outcome):
        # OpenProcess denial is not death, even if a query-only probe says False.
        # Only a fresh complete snapshot can prove that the child disappeared.
        scans = 0

        def snapshot():
            nonlocal scans
            scans += 1
            if scans > 1:
                if outcome == "unknown":
                    raise OSError("snapshot unavailable")
                if outcome == "gone":
                    return {}
            return {101: 100}

        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_windows_process_parent_map", snapshot)
        monkeypatch.setattr(pc, "_open_process_termination_handle", lambda _pid, **_k: None)
        monkeypatch.setattr(pc, "pid_exists", lambda _pid: False)
        monkeypatch.setattr(pc, "_windows_process_handle_identity", lambda _h: (100, 10, None))
        if outcome == "gone":
            assert pc.descendant_termination_handles(100, {}, 8001) == {}
        else:
            with pytest.raises(OSError, match="unavailable"):
                pc.descendant_termination_handles(100, {}, 8001)
        assert scans == 2

    @pytest.mark.parametrize("unreadable", [True, False])
    def test_unknown_identity_refuses_but_proven_foreign_child_is_excluded(
        self, monkeypatch, unreadable
    ):
        closed: list[int] = []
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_windows_process_parent_map", lambda: {101: 100})
        monkeypatch.setattr(pc, "_open_process_termination_handle", lambda _pid, **_k: 9001)
        monkeypatch.setattr(
            pc,
            "_windows_process_handle_identity",
            {8001: (100, 10, 20), 9001: None if unreadable else (101, 21, None)}.get,
        )
        monkeypatch.setattr(pc, "close_process_handle", closed.append)
        if unreadable:
            with pytest.raises(OSError, match="identity unreadable"):
                pc.descendant_termination_handles(100, {}, 8001)
        else:
            assert pc.descendant_termination_handles(100, {}, 8001) == {}
        assert closed == [9001]


class TestCloseProcessHandleWindows:
    def test_closes_the_handle(self, monkeypatch):
        closed: list[int] = []

        def _close(handle: Any) -> bool:
            closed.append(int(handle.value))
            return True

        _fake_windows(
            monkeypatch,
            kernel32=types.SimpleNamespace(CloseHandle=_Fn(_close)),
        )
        pc.close_process_handle(4321)
        assert closed == [4321]

    def test_suppresses_a_failed_close(self, monkeypatch):
        # Called from ``finally`` blocks and from the scan's cleanup path, so it
        # must never displace the exception that got us there.
        _fake_windows(monkeypatch)  # no kernel32 -> the load raises
        pc.close_process_handle(4321)


class TestDuplicateAsyncioHandleFailure:
    def test_reports_none_when_the_duplication_call_raises(self, monkeypatch):
        popen = types.SimpleNamespace(_handle=909)
        transport = types.SimpleNamespace(get_extra_info=lambda _key: popen)
        process = types.SimpleNamespace(_transport=transport)

        def _boom(*_args: Any) -> Any:
            raise OSError("DuplicateHandle blew up")

        _fake_windows(
            monkeypatch,
            kernel32=types.SimpleNamespace(GetCurrentProcess=_const(1), DuplicateHandle=_Fn(_boom)),
        )
        assert pc.duplicate_asyncio_process_handle(process) is None


class TestDescendantHandlesAsyncWindows:
    def test_offloads_the_snapshot_walk_off_the_event_loop(self, monkeypatch):
        # Two Toolhelp snapshots plus a handle open per child is a blocking
        # burst; running it on the loop stalls chat and the heartbeat.
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        sentinel = object()
        seen_executors: list[Any] = []
        forwarded: list[Any] = []

        monkeypatch.setattr(pc, "subprocess_executor", lambda: sentinel)

        def _walk(*args: Any) -> Any:
            forwarded.append(args)
            return {101: 9001}

        monkeypatch.setattr(pc, "descendant_termination_handles", _walk)

        async def _driver() -> Any:
            loop = asyncio.get_running_loop()

            def _spy(executor: Any, func: Any, *args: Any) -> Any:
                seen_executors.append(executor)
                future: asyncio.Future = loop.create_future()
                future.set_result(func(*args))
                return future

            # setattr rather than assignment: the stdlib signature is generic
            # over the callable's parameters, which a spy cannot restate.
            setattr(loop, "run_in_executor", _spy)
            return await pc.descendant_termination_handles_async(100, {55: 7}, 8001)

        assert asyncio.run(_driver()) == {101: 9001}
        assert seen_executors == [sentinel]
        # The retained map is copied before crossing the thread boundary, so a
        # caller mutating its own dict cannot race the worker.
        assert forwarded == [(100, {55: 7}, 8001)]


# ---------------------------------------------------------------------------
# Kill paths
# ---------------------------------------------------------------------------


class TestKillProcessTreeBroadcastGuard:
    """``killpg(1, sig)`` is ``kill(-1, sig)`` in libc -- a signal to EVERY
    process this uid owns, including the ``systemd --user`` manager, the user's
    SSH session and the gateway itself. This guard is all that stands between a
    mis-derived pgid and that.
    """

    def test_refuses_a_reserved_or_non_int_pid(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", True)
        # A mocked Popen's MagicMock pid coerces to 1 via __index__, which is
        # exactly how a test double turns into a broadcast in production code.
        for candidate in (1, 0, True):
            with pytest.raises(ValueError, match="reserved pid"):
                pc.kill_process_tree(candidate, pc.SIGTERM)

    @_POSIX_ONLY
    def test_degrades_to_a_pid_scoped_kill_for_a_broadcast_pgid(self, monkeypatch, caplog):
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.os, "getpgid", lambda _pid: 1)
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(pc.os, "kill", lambda pid, sig: killed.append((pid, sig)))

        def _forbidden(*_args: Any) -> Any:
            raise AssertionError("must never signal the whole process group")

        monkeypatch.setattr(pc.os, "killpg", _forbidden)
        with caplog.at_level(logging.ERROR, logger=pc.logger.name):
            assert pc.kill_process_tree(4242, pc.SIGKILL) is True
        assert killed == [(4242, pc.SIGKILL)]
        assert any("refusing broadcast/self pgid" in r.getMessage() for r in caplog.records)

    @_POSIX_ONLY
    def test_degrades_to_a_pid_scoped_kill_for_our_own_group(self, monkeypatch):
        # Our own pgid contains the gateway, so a group signal there is suicide.
        # ``_OWN_PGID`` is captured at import precisely so the check cannot be
        # defeated by test-time ``os.getpgid`` patching -- hence the fake returns
        # that value rather than relying on the patch.
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.os, "getpgid", lambda _pid: pc._OWN_PGID)
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(pc.os, "kill", lambda pid, sig: killed.append((pid, sig)))

        def _forbidden(*_args: Any) -> Any:
            raise AssertionError("must never signal our own process group")

        monkeypatch.setattr(pc.os, "killpg", _forbidden)
        assert pc.kill_process_tree(4242, pc.SIGTERM) is True
        assert killed == [(4242, pc.SIGTERM)]


class TestWindowsKillWithoutTaskkill:
    """A quiet ``True`` would strand a live process while reporting it
    terminated, so an unresolvable ``taskkill`` must raise -- callers branch on
    the exception to escalate."""

    def test_kill_pid_raises_when_taskkill_is_unresolvable(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _name: None)

        def _forbidden(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("must not spawn a PATH-resolved taskkill")

        monkeypatch.setattr(pc.subprocess, "run", _forbidden)
        with pytest.raises(OSError, match="trusted system directories"):
            pc.kill_pid(999_999, pc.SIGKILL)

    def test_kill_process_tree_raises_when_taskkill_is_unresolvable(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _name: None)
        with pytest.raises(OSError, match="trusted system directories"):
            pc.kill_process_tree(999_999, pc.SIGKILL)

    def test_the_tree_kill_argv_carries_the_recursive_flag(self, monkeypatch):
        # /T is what makes it a TREE kill; losing it silently orphans every
        # descendant while the call still reports success.
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(
            pc, "trusted_system_bin", lambda _name: r"C:\Windows\System32\taskkill.exe"
        )
        captured: dict[str, list] = {}

        def _run(argv: Any, **_kwargs: Any) -> Any:
            captured["argv"] = list(argv)
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(pc.subprocess, "run", _run)
        assert pc.kill_process_tree(4242, pc.SIGKILL) is True
        assert captured["argv"][1:] == ["/T", "/F", "/PID", "4242"]


# ---------------------------------------------------------------------------
# Directory links -- the junction fallback
# ---------------------------------------------------------------------------


class TestSymlinkJunctionFallback:
    def test_falls_back_to_a_junction_when_the_symlink_is_refused(self, monkeypatch, tmp_path):
        # An ordinary Windows account holds no SeCreateSymbolicLinkPrivilege, so
        # os.symlink raises WinError 1314 and every feature that links a
        # directory into place breaks unless this fallback fires.
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"

        def _refused(*_args: Any, **_kwargs: Any) -> Any:
            raise OSError(1314, "A required privilege is not held by the client")

        monkeypatch.setattr(pc.os, "symlink", _refused)
        created: list[tuple[str, str]] = []
        monkeypatch.setitem(
            sys.modules,
            "_winapi",
            types.SimpleNamespace(
                CreateJunction=lambda t, ln: created.append((t, ln)),
            ),
        )
        pc.symlink_or_junction(target, link)
        assert created == [(str(target), str(link))]


# ---------------------------------------------------------------------------
# Per-pid RSS -- the Win32 failure return
# ---------------------------------------------------------------------------


class TestProcRssForPidWindowsLoaderFailure:
    def test_reports_unknown_when_the_win32_boundary_fails(self, monkeypatch):
        # "Unknown" (None) is the contract the RSS staleness probe relies on: a
        # zero would read as a healthy, tiny process.
        monkeypatch.setattr(pc.sys, "platform", "win32")
        _fake_windows(monkeypatch)  # neither kernel32 nor psapi resolves
        assert pc.proc_rss_bytes_for_pid(777) is None
