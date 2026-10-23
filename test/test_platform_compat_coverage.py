"""Coverage-raising unit tests for :mod:`kiro_crew.platform_compat`.

Companion to ``test_platform_compat.py``, which covers the POSIX-native surface
and the process-session contracts that need a real process. This file targets
the branches that the host platform alone never reaches on the Linux CI fleet:

* the Windows ``ctypes``/``msvcrt`` paths, exercised by flipping ``IS_WINDOWS``
  and standing in for the DLL entry points (the same technique
  ``test_platform_compat_process_tree.py`` uses for the Toolhelp snapshot),
* the macOS ``libproc`` paths, exercised by faking ``ctypes.CDLL``,
* the macOS TCC walk-pruning rules,
* the failure/degradation paths of the ``/proc`` readers.

No test here spawns a process, touches the network, or writes outside
``tmp_path``; every clock the product consults is replaced with a fake, so
nothing depends on wall time or on execution order.

The suite is split by topic. This file keeps the TCC walk, console encoding, locking,
process identity and handles, and the ``/proc`` readers;
``test_platform_compat_coverage_filesystem.py`` holds the filesystem helpers, Windows
SIDs and executable discovery, ``test_platform_compat_coverage_metrics.py`` the resource
and system metrics, and ``test_platform_compat_coverage_failure_paths.py`` the remaining
failure returns. The fakes they share live in ``platform_compat_coverage_helpers.py``.
"""

from __future__ import annotations

import ctypes
import io
import logging
import os
import struct
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

# Applied to the WHOLE module, deliberately.
#
# This suite's method is to simulate BOTH platform branches from one host: its
# files flip ``IS_POSIX`` / ``IS_WINDOWS`` and monkeypatch ``os.getuid``,
# ``os.getpgid``, ``os.killpg``, ``os.fchmod`` and ``/proc/locks`` reads.
# On real Windows those attributes do not exist
# (so ``monkeypatch.setattr`` raises before any assertion) and the faked
# branches diverge from what the OS actually does -- which produced 13 failures
# across two CI rounds in TestFlockOwnerPid, TestRestrictToOwner,
# TestChmodHelpers and TestCurrentUserSid. Fixing them one at a time was
# whack-a-mole; the property under test is platform-independent logic, so the
# honest fix is to run the simulation only from the host it is written for.
#
# This costs ZERO coverage: ci.yml measures coverage on the ubuntu 3.12 shards
# only (the Windows lane runs --no-cov), so every line here is still counted on
# the lane that reports the number.
pytestmark = [
    _POSIX_ONLY,
    # This file replaces ctypes.CDLL, find_library or a cached libc handle for a test,
    # so the shared macOS handle caches are put back after each one (see conftest.py).
    pytest.mark.usefixtures("fresh_darwin_libc_handles"),
]

# ---------------------------------------------------------------------------
# Windows / macOS fakes
# ---------------------------------------------------------------------------


def _fake_clock(monkeypatch: pytest.MonkeyPatch, ticks: list[float]) -> list[float]:
    """Replace ``platform_compat``'s ``time`` module with a scripted clock.

    Only the module-level reference in ``platform_compat`` is swapped, so the
    real :mod:`time` is untouched for everything else in the process. Returns
    the list that records each faked sleep, so a test can assert the product
    slept rather than busy-spun without ever sleeping for real.
    """

    slept: list[float] = []
    reads = iter(ticks)
    last: list[float] = [ticks[-1] if ticks else 0.0]

    def _monotonic() -> float:
        last[0] = next(reads, last[0])
        return last[0]

    monkeypatch.setattr(
        pc,
        "time",
        types.SimpleNamespace(monotonic=_monotonic, sleep=slept.append),
    )
    return slept


# ---------------------------------------------------------------------------
# macOS TCC walk pruning
# ---------------------------------------------------------------------------


class TestTccWalkPruning:
    """The home-rooted walk must prune TCC-gated folders, and only those."""

    @staticmethod
    def _as_macos(monkeypatch: pytest.MonkeyPatch, home: str) -> None:
        monkeypatch.setattr(pc, "IS_MACOS", True)
        monkeypatch.setattr(pc.os.path, "expanduser", lambda _p: home)

    def test_no_pruning_off_macos(self, monkeypatch, tmp_path):
        # Linux/Windows have no TCC, so the set is empty and a walk is untouched.
        monkeypatch.setattr(pc, "IS_MACOS", False)
        assert pc.tcc_protected_dirs_for_walk(str(tmp_path)) == frozenset()
        names = ["Downloads", "src"]
        assert pc.tcc_prune_walk_dirs(str(tmp_path), str(tmp_path), names) == names

    def test_home_root_reports_the_gated_names(self, monkeypatch, tmp_path):
        self._as_macos(monkeypatch, str(tmp_path))
        assert pc.tcc_protected_dirs_for_walk(str(tmp_path)) == pc.TCC_PROTECTED_HOME_DIRS

    def test_explicitly_scoped_root_is_never_pruned(self, monkeypatch, tmp_path):
        # A walk the user rooted at ~/Downloads must still see its contents.
        home = tmp_path / "home"
        scoped = home / "Downloads"
        scoped.mkdir(parents=True)
        self._as_macos(monkeypatch, str(home))
        assert pc.tcc_protected_dirs_for_walk(str(scoped)) == frozenset()

    def test_unresolvable_root_prunes_nothing(self, monkeypatch):
        # realpath raises ValueError on a NUL byte (not an OSError subclass) —
        # it must be swallowed, not escape and 500 the file-search request.
        self._as_macos(monkeypatch, "/home/u")

        def _boom(_path: Any) -> str:
            raise ValueError("embedded null byte")

        monkeypatch.setattr(pc.os.path, "realpath", _boom)
        assert pc.tcc_protected_dirs_for_walk("/home/u") == frozenset()

    def test_root_position_drops_gated_folders_but_keeps_library(self, monkeypatch, tmp_path):
        self._as_macos(monkeypatch, str(tmp_path))
        root = str(tmp_path)
        kept = pc.tcc_prune_walk_dirs(root, root, ["Downloads", "Desktop", "Library", "code"])
        # Library survives so the walk can descend to the cloud mounts below it.
        assert kept == ["Library", "code"]

    def test_library_position_keeps_only_the_cloud_mounts(self, monkeypatch, tmp_path):
        self._as_macos(monkeypatch, str(tmp_path))
        root = str(tmp_path)
        library = os.path.join(root, "Library")
        kept = pc.tcc_prune_walk_dirs(
            root, library, ["Mail", "Safari", "CloudStorage", "Mobile Documents"]
        )
        assert sorted(kept) == ["CloudStorage", "Mobile Documents"]

    def test_deeper_positions_are_untouched(self, monkeypatch, tmp_path):
        # A project's own Documents/ deeper in the tree is not TCC-gated.
        self._as_macos(monkeypatch, str(tmp_path))
        root = str(tmp_path)
        deep = os.path.join(root, "code", "proj")
        names = ["Documents", "Downloads"]
        assert pc.tcc_prune_walk_dirs(root, deep, names) == names

    def test_non_home_root_at_root_position_prunes_nothing(self, monkeypatch, tmp_path):
        self._as_macos(monkeypatch, str(tmp_path / "elsewhere"))
        root = str(tmp_path)
        names = ["Downloads", "code"]
        assert pc.tcc_prune_walk_dirs(root, root, names) == names


# ---------------------------------------------------------------------------
# Windows console encoding
# ---------------------------------------------------------------------------


class _ReconfigurableStream:
    def __init__(self, encoding: str, *, reconfigure_to: str | None = None) -> None:
        self.encoding = encoding
        self._to = reconfigure_to
        self.buffer = io.BytesIO()
        self.reconfigured: list[tuple[str, str]] = []

    def reconfigure(self, *, encoding: str, errors: str) -> None:
        self.reconfigured.append((encoding, errors))
        if self._to is not None:
            self.encoding = self._to


class TestEnsureUtf8Console:
    def test_off_windows_only_publishes_environment(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setenv("PYTHONUTF8", "0")
        monkeypatch.setenv("PYTHONIOENCODING", "ascii")
        before = sys.stdout
        pc.ensure_utf8_console()
        assert sys.stdout is before
        assert os.environ["PYTHONUTF8"] == "1"
        assert os.environ["PYTHONIOENCODING"] == "utf-8:backslashreplace"

    def test_reconfigure_in_place_is_preferred(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        out = _ReconfigurableStream("cp1252", reconfigure_to="utf-8")
        err = _ReconfigurableStream("cp1252", reconfigure_to="UTF-8")
        monkeypatch.setattr(pc.sys, "stdout", out)
        monkeypatch.setattr(pc.sys, "stderr", err)

        pc.ensure_utf8_console()

        assert out.reconfigured == [("utf-8", "backslashreplace")]
        # Reconfigure reported UTF-8, so the stream object is kept as-is.
        assert sys.stdout is out
        assert sys.stderr is err

    def test_falls_back_to_wrapping_the_binary_buffer(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        out = _StubbornStream()
        monkeypatch.setattr(pc.sys, "stdout", out)
        monkeypatch.setattr(pc.sys, "stderr", _StubbornStream(with_buffer=False))

        pc.ensure_utf8_console()

        assert isinstance(sys.stdout, io.TextIOWrapper)
        assert (sys.stdout.encoding or "").lower() == "utf-8"
        # No buffer to wrap on stderr, so it is left untouched rather than lost.
        assert sys.stderr is not None

    def test_reconfigure_that_does_not_take_still_wraps(self, monkeypatch):
        # reconfigure() succeeds but the encoding stays cp1252 — the wrap must
        # still run, otherwise the first emoji print kills the gateway.
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        out = _ReconfigurableStream("cp1252")
        monkeypatch.setattr(pc.sys, "stdout", out)
        monkeypatch.setattr(pc.sys, "stderr", out)

        pc.ensure_utf8_console()

        assert isinstance(sys.stdout, io.TextIOWrapper)

    def test_absent_stream_is_skipped(self, monkeypatch):
        # pythonw / fully detached: sys.stdout is None and must not AttributeError.
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc.sys, "stdout", None)
        monkeypatch.setattr(pc.sys, "stderr", None)
        pc.ensure_utf8_console()


# ---------------------------------------------------------------------------
# Windows locking
# ---------------------------------------------------------------------------


class TestWindowsLocking:
    def test_acquire_takes_a_free_lock_on_the_first_try(self, monkeypatch, tmp_path):
        fake = _FakeMsvcrt([True])
        _fake_windows(monkeypatch)
        monkeypatch.setattr(pc, "msvcrt", fake, raising=False)
        _fake_clock(monkeypatch, [0.0])
        lock = tmp_path / "a.lock"
        lock.write_text("")
        with open(lock, "r+") as handle:
            assert pc._win_acquire_blocking(handle.fileno()) is True
        assert fake.calls == [_FakeMsvcrt.LK_NBLCK]

    def test_acquire_polls_until_the_holder_releases(self, monkeypatch, tmp_path):
        # A contended fd must be waited out, not raced — and the wait must be a
        # sleep, not a spin. The clock is fake, so nothing sleeps for real.
        fake = _FakeMsvcrt([False, False, True])
        _fake_windows(monkeypatch)
        monkeypatch.setattr(pc, "msvcrt", fake, raising=False)
        slept = _fake_clock(monkeypatch, [0.0, 0.1, 0.2, 0.3])
        lock = tmp_path / "b.lock"
        lock.write_text("")
        with open(lock, "r+") as handle:
            assert pc._win_acquire_blocking(handle.fileno(), timeout=100.0) is True
        assert slept == [pc._WIN_LOCK_POLL_SECS, pc._WIN_LOCK_POLL_SECS]

    def test_acquire_gives_up_at_the_ceiling(self, monkeypatch, tmp_path):
        fake = _FakeMsvcrt([False] * 5)
        _fake_windows(monkeypatch)
        monkeypatch.setattr(pc, "msvcrt", fake, raising=False)
        _fake_clock(monkeypatch, [0.0, 999.0])
        lock = tmp_path / "c.lock"
        lock.write_text("")
        with open(lock, "r+") as handle:
            assert pc._win_acquire_blocking(handle.fileno(), timeout=1.0) is False

    @pytest.mark.asyncio
    async def test_acquire_is_single_shot_on_the_event_loop(self, monkeypatch, tmp_path):
        # A spin-sleep on the loop thread would freeze chat/heartbeat, so the
        # on-loop acquire must try exactly once and fail closed.
        fake = _FakeMsvcrt([False])
        _fake_windows(monkeypatch)
        monkeypatch.setattr(pc, "msvcrt", fake, raising=False)
        slept = _fake_clock(monkeypatch, [0.0])
        lock = tmp_path / "d.lock"
        lock.write_text("")
        with open(lock, "r+") as handle:
            assert pc._win_acquire_blocking(handle.fileno()) is False
        assert slept == []
        assert fake.calls == [_FakeMsvcrt.LK_NBLCK]

    def test_file_lock_round_trips_and_unlocks(self, monkeypatch, tmp_path):
        fake = _FakeMsvcrt([True])
        _fake_windows(monkeypatch)
        monkeypatch.setattr(pc, "msvcrt", fake, raising=False)
        _fake_clock(monkeypatch, [0.0])
        lock = tmp_path / "e.lock"
        lock.write_text("")
        ran = False
        with open(lock, "r+") as handle:
            with pc.file_lock(handle.fileno()):
                ran = True
        assert ran
        assert fake.calls == [_FakeMsvcrt.LK_NBLCK, _FakeMsvcrt.LK_UNLCK]

    def test_file_lock_fails_closed_when_the_lock_is_unreachable(self, monkeypatch, tmp_path):
        # Entering the critical section unserialized is the fail-open that loses
        # writes, so an unacquirable lock must raise instead.
        _fake_windows(monkeypatch)
        monkeypatch.setattr(pc, "_win_acquire_blocking", lambda *_a, **_k: False)
        lock = tmp_path / "f.lock"
        lock.write_text("")
        with open(lock, "r+") as handle:
            with pytest.raises(OSError, match="refusing to proceed unserialized"):
                with pc.file_lock(handle.fileno()):
                    pytest.fail("body must not run without the lock")

    def test_file_lock_not_waiting_reports_a_held_lock_as_blocking(self, monkeypatch, tmp_path):
        """``wait=False`` must raise BlockingIOError, not the stuck-holder OSError.

        The two refusals mean different things to a caller — "someone holds it
        right now, come back later" versus "a holder is stuck past the ceiling" —
        and only the first is safe to retry, so collapsing them would turn a
        normal contended write into a reported fault.
        """
        _fake_windows(monkeypatch)
        monkeypatch.setattr(pc, "_win_acquire_blocking", lambda *_a, **_k: False)
        lock = tmp_path / "nowait.lock"
        lock.write_text("")
        with open(lock, "r+") as handle:
            with pytest.raises(BlockingIOError, match="not waiting for it"):
                with pc.file_lock(handle.fileno(), wait=False):
                    pytest.fail("body must not run without the lock")

    def test_file_lock_names_the_callers_own_timeout_in_the_refusal(self, monkeypatch, tmp_path):
        """An explicit ``timeout`` is the ceiling the refusal must name.

        The message is the only evidence of WHY the critical section was declined,
        so reporting the 300s default when the caller waited 2.5s would send a
        reader hunting for a stall that never happened.
        """
        _fake_windows(monkeypatch)
        monkeypatch.setattr(pc, "_win_acquire_blocking", lambda *_a, **_k: False)
        lock = tmp_path / "timeout.lock"
        lock.write_text("")
        with open(lock, "r+") as handle:
            with pytest.raises(OSError, match=r"limit 2\.5s"):
                with pc.file_lock(handle.fileno(), timeout=2.5):
                    pytest.fail("body must not run without the lock")

    def test_acquire_lock_fails_closed(self, monkeypatch, tmp_path):
        _fake_windows(monkeypatch)
        monkeypatch.setattr(pc, "_win_acquire_blocking", lambda *_a, **_k: False)
        monkeypatch.setattr(pc, "msvcrt", _FakeMsvcrt([]), raising=False)
        lock = tmp_path / "g.lock"
        lock.write_text("")
        with open(lock, "r+") as handle:
            with pytest.raises(OSError, match="refusing to proceed unserialized"):
                pc.acquire_lock(handle.fileno())

    def test_acquire_lock_succeeds_then_releases(self, monkeypatch, tmp_path):
        fake = _FakeMsvcrt([True])
        _fake_windows(monkeypatch)
        monkeypatch.setattr(pc, "msvcrt", fake, raising=False)
        _fake_clock(monkeypatch, [0.0])
        lock = tmp_path / "h.lock"
        lock.write_text("")
        with open(lock, "r+") as handle:
            pc.acquire_lock(handle.fileno())
            pc.release_lock(handle.fileno())
        assert fake.calls == [_FakeMsvcrt.LK_NBLCK, _FakeMsvcrt.LK_UNLCK]

    def test_release_lock_swallows_an_unlock_error(self, monkeypatch, tmp_path):
        class _Failing(_FakeMsvcrt):
            def locking(self, _fd: int, mode: int, _nbytes: int) -> None:
                raise OSError(13, "not locked")

        _fake_windows(monkeypatch)
        monkeypatch.setattr(pc, "msvcrt", _Failing([]), raising=False)
        lock = tmp_path / "i.lock"
        lock.write_text("")
        with open(lock, "r+") as handle:
            pc.release_lock(handle.fileno())  # must not raise

    def test_try_acquire_lock_reports_both_outcomes(self, monkeypatch, tmp_path):
        fake = _FakeMsvcrt([True, False])
        _fake_windows(monkeypatch)
        monkeypatch.setattr(pc, "msvcrt", fake, raising=False)
        lock = tmp_path / "j.lock"
        lock.write_text("")
        with open(lock, "r+") as handle:
            assert pc.try_acquire_lock(handle.fileno()) is True
            assert pc.try_acquire_lock(handle.fileno()) is False


# ---------------------------------------------------------------------------
# get_ppid / get_process_start_id — macOS libproc and Windows Toolhelp
# ---------------------------------------------------------------------------


def _bsdinfo(ppid: int = 0, sec: int = 0, usec: int = 0) -> bytes:
    """A synthetic ``struct proc_bsdinfo`` with the three fields we read."""

    buf = bytearray(pc._DARWIN_BSDINFO_SIZE)
    buf[pc._DARWIN_PBI_PPID_OFFSET : pc._DARWIN_PBI_PPID_OFFSET + 4] = ppid.to_bytes(4, "little")
    buf[pc._DARWIN_OFF_START_TVSEC : pc._DARWIN_OFF_START_TVSEC + 8] = sec.to_bytes(8, "little")
    buf[pc._DARWIN_OFF_START_TVUSEC : pc._DARWIN_OFF_START_TVUSEC + 8] = usec.to_bytes(8, "little")
    return bytes(buf)


def _fake_libproc(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: bytes | None,
    ret: int,
    library: str | None = "libproc.dylib",
) -> None:
    """Pretend to be macOS with a ``libproc`` that answers ``proc_pidinfo``.

    The handle cache must start unbuilt, which the module's
    ``fresh_darwin_libc_handles`` request gives every test here.
    """

    monkeypatch.setattr(pc.sys, "platform", "darwin")
    monkeypatch.setattr(pc.ctypes.util, "find_library", lambda _n: library)

    def _proc_pidinfo(_pid: Any, _flavor: Any, _arg: Any, buf: Any, _size: Any) -> int:
        if payload is not None:
            ctypes.memmove(buf, payload, len(payload))
        return ret

    lib = types.SimpleNamespace(proc_pidinfo=_Fn(_proc_pidinfo))
    monkeypatch.setattr(pc.ctypes, "CDLL", lambda _p: lib)


class TestGetPpid:
    def test_macos_returns_the_ppid_from_libproc(self, monkeypatch):
        _fake_libproc(monkeypatch, payload=_bsdinfo(ppid=4321), ret=136)
        assert pc.get_ppid(99) == 4321

    def test_macos_without_libproc_reports_failure(self, monkeypatch):
        _fake_libproc(monkeypatch, payload=None, ret=136, library=None)
        assert pc.get_ppid(99) == -1

    def test_macos_reports_failure_when_the_call_returns_nothing(self, monkeypatch):
        _fake_libproc(monkeypatch, payload=None, ret=0)
        assert pc.get_ppid(99) == -1

    def test_macos_swallows_a_loader_error(self, monkeypatch):
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        monkeypatch.setattr(pc.ctypes.util, "find_library", lambda _n: "libproc.dylib")

        def _boom(_path: Any) -> Any:
            raise OSError("cannot load")

        monkeypatch.setattr(pc.ctypes, "CDLL", _boom)
        assert pc.get_ppid(99) == -1

    def test_unknown_platform_reports_failure(self, monkeypatch):
        monkeypatch.setattr(pc.sys, "platform", "aix")
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        assert pc.get_ppid(99) == -1

    def test_windows_walks_the_snapshot_to_the_matching_pid(self, monkeypatch):
        kernel32 = _toolhelp_kernel32([(1, 0, b"a.exe"), (77, 42, b"b.exe")])
        _fake_windows(monkeypatch, kernel32=kernel32)
        monkeypatch.setattr(pc.sys, "platform", "win32")
        assert pc.get_ppid(77) == 42

    def test_windows_reports_failure_when_the_pid_is_absent(self, monkeypatch):
        kernel32 = _toolhelp_kernel32([(1, 0, b"a.exe")])
        _fake_windows(monkeypatch, kernel32=kernel32)
        monkeypatch.setattr(pc.sys, "platform", "win32")
        assert pc.get_ppid(999) == -1

    def test_windows_reports_failure_on_an_invalid_snapshot(self, monkeypatch):
        invalid = pc.wintypes.HANDLE(-1).value
        kernel32 = _toolhelp_kernel32([], snapshot=invalid)
        _fake_windows(monkeypatch, kernel32=kernel32)
        monkeypatch.setattr(pc.sys, "platform", "win32")
        assert pc.get_ppid(1) == -1

    def test_windows_reports_failure_when_enumeration_cannot_start(self, monkeypatch):
        kernel32 = _toolhelp_kernel32([])
        _fake_windows(monkeypatch, kernel32=kernel32)
        monkeypatch.setattr(pc.sys, "platform", "win32")
        assert pc.get_ppid(1) == -1


class TestGetProcessStartId:
    def test_macos_reads_start_id_and_ppid_atomically(self, monkeypatch):
        _fake_libproc(
            monkeypatch,
            payload=_bsdinfo(ppid=4321, sec=1700000000, usec=42),
            ret=136,
        )

        assert pc.get_process_start_identity(5) == pc.ProcessStartIdentity(
            "1700000000.000042", 4321
        )

    def test_macos_formats_seconds_and_microseconds(self, monkeypatch):
        _fake_libproc(monkeypatch, payload=_bsdinfo(sec=1700000000, usec=42), ret=136)
        # Microsecond resolution is the point: two processes started in the same
        # second must not alias onto one identity.
        assert pc.get_process_start_id(5) == "1700000000.000042"

    def test_macos_treats_a_zero_start_time_as_unknown(self, monkeypatch):
        _fake_libproc(monkeypatch, payload=_bsdinfo(sec=0, usec=7), ret=136)
        assert pc.get_process_start_id(5) is None

    def test_macos_without_libproc_is_unknown(self, monkeypatch):
        _fake_libproc(monkeypatch, payload=None, ret=136, library=None)
        assert pc.get_process_start_id(5) is None

    def test_macos_failed_call_is_unknown(self, monkeypatch):
        _fake_libproc(monkeypatch, payload=None, ret=-1)
        assert pc.get_process_start_id(5) is None

    def test_windows_uses_query_only_creation_identity(self, monkeypatch):
        monkeypatch.setattr(pc.sys, "platform", "win32")
        monkeypatch.setattr(
            pc, "process_start_time", lambda pid: "133000123456789" if pid == 5 else None
        )
        assert pc.get_process_start_id(5) == "133000123456789"
        assert pc.get_process_start_id(6) is None

    def test_windows_unreadable_identity_is_unknown(self, monkeypatch):
        monkeypatch.setattr(pc.sys, "platform", "win32")
        monkeypatch.setattr(pc, "process_start_time", lambda pid: None)
        assert pc.get_process_start_id(5) is None

    def test_a_nonpositive_pid_is_unknown_without_a_platform_read(self, monkeypatch):
        # pid <= 0 names no process, so the guard answers None before dispatch --
        # the win32 producer (and every other platform's) is never consulted.
        monkeypatch.setattr(pc.sys, "platform", "win32")

        def _refuse(_pid):
            raise AssertionError("get_process_start_id read a non-positive pid")

        monkeypatch.setattr(pc, "process_start_time", _refuse)
        assert pc.get_process_start_id(0) is None
        assert pc.get_process_start_id(-1) is None

    def test_identity_never_contains_a_colon(self, monkeypatch):
        # Callers embed the value in colon-delimited records.
        _fake_libproc(monkeypatch, payload=_bsdinfo(sec=17, usec=1), ret=136)
        value = pc.get_process_start_id(5)
        assert value is not None and ":" not in value


@pytest.mark.parametrize(
    "scenario", ["unique", "duplicate", "closed", "truncated", "denied", "oversize"]
)
def test_windows_tcp_peer_table_refuses_uncertain_identity(monkeypatch, scenario):
    monkeypatch.setattr(pc, "IS_WINDOWS", True)
    row = struct.pack(
        "<I4sI4sII",
        1 if scenario == "closed" else 5,
        b"\x7f\x00\x00\x01",
        0xD007,
        b"\x7f\x00\x00\x01",
        0xE803,
        2468,
    )
    count = 2 if scenario in {"duplicate", "truncated"} else 1
    raw = struct.pack("<I", count) + row * (2 if scenario == "duplicate" else 1)

    def query(buffer, size_pointer, *_args):
        size = ctypes.cast(size_pointer, ctypes.POINTER(pc.wintypes.DWORD))
        if scenario == "denied":
            return 5
        size.contents.value = 16 * 1024 * 1024 if scenario == "oversize" else len(raw)
        if buffer is None:
            return 122
        ctypes.memmove(buffer, raw, len(raw))
        return 0

    monkeypatch.setattr(
        pc.ctypes,
        "WinDLL",
        lambda *a, **kw: types.SimpleNamespace(GetExtendedTcpTable=_Fn(query)),
        raising=False,
    )
    result = pc.get_tcp_peer_pid(("127.0.0.1", 1000), ("127.0.0.1", 2000))
    assert result == (2468 if scenario == "unique" else None)


# ---------------------------------------------------------------------------
# Trusted system-binary resolution
# ---------------------------------------------------------------------------


class TestWindowsSystemDirs:
    def test_api_directory_leads_and_powershell_is_appended_per_root(self, monkeypatch):
        def _get_system_directory(buf: Any, _size: Any) -> int:
            buf.value = r"C:\Windows\system32"
            return len(buf.value)

        kernel32 = types.SimpleNamespace(GetSystemDirectoryW=_get_system_directory)
        _fake_windows(monkeypatch, kernel32=kernel32)
        monkeypatch.setitem(os.environ, "SystemRoot", r"C:\Windows")

        dirs = pc._windows_system_dirs()

        assert dirs[0] == r"C:\Windows\system32"
        # The conventionally-cased hardcoded fallback names the same directory as
        # the API answer and must be deduped case-insensitively, not probed twice.
        assert r"C:\Windows\System32" not in dirs
        assert any(d.endswith(os.path.join("WindowsPowerShell", "v1.0")) for d in dirs)

    def test_falls_back_when_the_api_call_fails(self, monkeypatch):
        def _boom(_buf: Any, _size: Any) -> int:
            raise OSError("no such entry point")

        kernel32 = types.SimpleNamespace(GetSystemDirectoryW=_boom)
        _fake_windows(monkeypatch, kernel32=kernel32)
        monkeypatch.setitem(os.environ, "SystemRoot", r"D:\Win")

        dirs = pc._windows_system_dirs()

        assert dirs[0] == os.path.join(r"D:\Win", "System32")
        assert r"C:\Windows\System32" in dirs

    def test_api_overflow_is_ignored(self, monkeypatch):
        # A written length at or past the buffer size means truncation; the
        # value must not be trusted.
        kernel32 = types.SimpleNamespace(GetSystemDirectoryW=_const(10_000))
        _fake_windows(monkeypatch, kernel32=kernel32)
        dirs = pc._windows_system_dirs()
        assert all("system32" not in d for d in dirs)


class TestUnpinnedToolDiagnostic:
    def test_silent_when_the_tool_is_not_installed_anywhere(self, monkeypatch, caplog):
        monkeypatch.setattr(pc, "_UNPINNED_TOOL_PROBED", set())
        monkeypatch.setattr(pc.shutil, "which", lambda _n: None)
        with caplog.at_level(logging.WARNING, logger=pc.logger.name):
            pc._log_tool_outside_trusted_dirs("lsof", ("/usr/bin",))
        assert caplog.records == []

    def test_warns_once_when_the_pin_is_what_hid_a_present_tool(self, monkeypatch, caplog):
        monkeypatch.setattr(pc, "_UNPINNED_TOOL_PROBED", set())
        monkeypatch.setattr(pc.shutil, "which", lambda _n: "/run/current-system/sw/bin/lsof")
        with caplog.at_level(logging.WARNING, logger=pc.logger.name):
            pc._log_tool_outside_trusted_dirs("lsof", ("/usr/bin",))
            pc._log_tool_outside_trusted_dirs("lsof", ("/usr/bin",))
        assert len(caplog.records) == 1
        assert "/run/current-system/sw/bin/lsof" in caplog.records[0].getMessage()

    def test_an_unresolvable_tool_is_none_and_diagnosed(self, monkeypatch):
        # The pin decides; the miss is reported so the answer can be explained.
        monkeypatch.setattr(pc, "_UNPINNED_TOOL_PROBED", set())
        monkeypatch.setattr(pc.shutil, "which", lambda _n: None)
        assert pc.trusted_system_bin("kirocrew-no-such-tool") is None

    def test_tool_outside_trusted_dirs_is_none_when_the_pin_resolved(self, monkeypatch):
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: "/usr/bin/lsof")
        assert pc.tool_outside_trusted_dirs("lsof") is None

    def test_tool_outside_trusted_dirs_reports_the_path_lookup(self, monkeypatch):
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: None)
        monkeypatch.setattr(pc.shutil, "which", lambda _n: "/opt/brew/bin/lsof")
        assert pc.tool_outside_trusted_dirs("lsof") == "/opt/brew/bin/lsof"


# ---------------------------------------------------------------------------
# Windows process handles
# ---------------------------------------------------------------------------


class TestWindowsHandleAcquisition:
    """Opening, duplicating and closing identity-stable process handles."""

    def test_open_handle_is_none_off_windows(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        assert pc._open_process_termination_handle(500) is None

    def test_open_handle_is_none_when_the_open_fails(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=types.SimpleNamespace(OpenProcess=_const(0)))
        assert pc._open_process_termination_handle(500) is None

    def test_open_handle_is_none_on_a_loader_failure(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.delattr(pc.ctypes, "WinDLL", raising=False)
        assert pc._open_process_termination_handle(500) is None

    def test_last_error_is_zero_without_the_windows_only_getter(self, monkeypatch):
        # ctypes.get_last_error does not exist on POSIX; the helper must not
        # assume it does.
        monkeypatch.delattr(pc.ctypes, "get_last_error", raising=False)
        assert pc._windows_last_error() == 0

    def test_last_error_reads_the_thread_local_code(self, monkeypatch):
        monkeypatch.setattr(pc.ctypes, "get_last_error", lambda: 5, raising=False)
        assert pc._windows_last_error() == 5

    @staticmethod
    def _asyncio_process(handle: Any) -> Any:
        popen = types.SimpleNamespace(_handle=handle)
        transport = types.SimpleNamespace(get_extra_info=lambda _key: popen)
        return types.SimpleNamespace(_transport=transport)

    @staticmethod
    def _duplicating_kernel32(*, ok: bool = True, out: int = 8899) -> Any:
        def _duplicate(
            _owner: Any,
            _source: Any,
            _target: Any,
            duplicate: Any,
            _options: Any,
            _inherit: Any,
            _access: Any,
        ) -> int:
            duplicate._obj.value = out
            return 1 if ok else 0

        return types.SimpleNamespace(
            GetCurrentProcess=_const(1),
            DuplicateHandle=_Fn(_duplicate),
        )

    def test_duplicate_is_none_off_windows(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        assert pc.duplicate_asyncio_process_handle(self._asyncio_process(1234)) is None

    def test_duplicate_returns_the_new_handle(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=self._duplicating_kernel32())
        assert pc.duplicate_asyncio_process_handle(self._asyncio_process(1234)) == 8899

    def test_duplicate_is_none_without_an_underlying_popen_handle(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=self._duplicating_kernel32())
        # A transport that exposes no subprocess yields no source handle.
        bare = types.SimpleNamespace(_transport=None)
        assert pc.duplicate_asyncio_process_handle(bare) is None
        assert pc.duplicate_asyncio_process_handle(self._asyncio_process(0)) is None

    def test_duplicate_is_none_when_the_call_fails(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=self._duplicating_kernel32(ok=False))
        assert pc.duplicate_asyncio_process_handle(self._asyncio_process(1234)) is None

    def test_duplicate_is_none_when_the_result_is_empty(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=self._duplicating_kernel32(out=0))
        assert pc.duplicate_asyncio_process_handle(self._asyncio_process(1234)) is None


def _identity_kernel32(
    *,
    pid: int = 4242,
    creation: int = 100,
    exit_times: list[int] | None = None,
    exit_code: int = 259,
    times_ok: list[bool] | None = None,
    exit_code_ok: bool = True,
    wait: int | None = None,
) -> Any:
    """kernel32 fake for ``_windows_process_handle_identity``.

    ``exit_times`` is replayed one entry per ``GetProcessTimes`` call, so a test
    can script the exited-but-exit-FILETIME-unpublished window; ``times_ok``
    scripts per-call success of the same function.

    ``wait`` is what ``WaitForSingleObject`` answers, which is what decides
    liveness: a signalled process object has terminated. It defaults to agreeing
    with ``exit_code`` -- WAIT_TIMEOUT for a live process, WAIT_OBJECT_0 for an
    exited one -- so a test scripts liveness in one place. Pass it explicitly to
    script a handle that cannot be waited on (WAIT_FAILED).
    """

    times = list(exit_times or [0])
    oks = list(times_ok or [])
    waited = (0x00000102 if exit_code == 259 else 0x00000000) if wait is None else wait

    def _get_times(_handle: Any, creation_out: Any, exit_out: Any, _k: Any, _u: Any) -> int:
        if oks and not oks.pop(0):
            return 0
        value = times.pop(0) if times else (exit_times or [0])[-1]
        creation_out._obj.dwHighDateTime = creation >> 32
        creation_out._obj.dwLowDateTime = creation & 0xFFFFFFFF
        exit_out._obj.dwHighDateTime = value >> 32
        exit_out._obj.dwLowDateTime = value & 0xFFFFFFFF
        return 1

    def _get_exit_code(_handle: Any, out: Any) -> int:
        out._obj.value = exit_code
        return 1 if exit_code_ok else 0

    return types.SimpleNamespace(
        GetProcessId=_const(pid),
        GetProcessTimes=_Fn(_get_times),
        GetExitCodeProcess=_Fn(_get_exit_code),
        WaitForSingleObject=_Fn(lambda _handle, _millis: waited),
    )


class TestWindowsHandleIdentity:
    """The PID-recycling guard: (pid, creation_time, exit_time) or nothing."""

    def test_none_off_windows_or_for_an_invalid_handle(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        assert pc._windows_process_handle_identity(5) is None
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        assert pc._windows_process_handle_identity(0) is None
        assert pc._windows_process_handle_identity(-1) is None

    def test_a_live_process_has_no_exit_bound(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=_identity_kernel32())
        assert pc._windows_process_handle_identity(5) == (4242, 100, None)

    def test_an_exit_status_of_259_is_read_as_exited_when_the_wait_says_so(self, monkeypatch):
        # 259 is both STILL_ACTIVE and an ordinary exit code, so the exit code
        # alone cannot decide liveness: a child that picks it would read back as
        # running for as long as a handle is held, and a drain waiting for its
        # exit would never finish. The signalled process object is authoritative.
        _fake_windows(
            monkeypatch,
            kernel32=_identity_kernel32(exit_code=259, wait=0x00000000, exit_times=[777, 777]),
        )
        assert pc._windows_process_handle_identity(5) == (4242, 100, 777)

    def test_a_handle_that_cannot_be_waited_on_falls_back_to_the_exit_code(self, monkeypatch):
        # A query-only handle carries no SYNCHRONIZE right, so the wait fails.
        # Those callers read identity without draining anything, so they keep the
        # exit-code answer rather than losing the identity altogether.
        _fake_windows(
            monkeypatch,
            kernel32=_identity_kernel32(exit_code=259, wait=0xFFFFFFFF),
        )
        assert pc._windows_process_handle_identity(5) == (4242, 100, None)

    def test_an_exited_process_reports_its_exit_filetime(self, monkeypatch):
        _fake_windows(
            monkeypatch,
            kernel32=_identity_kernel32(exit_code=0, exit_times=[777, 777]),
        )
        assert pc._windows_process_handle_identity(5) == (4242, 100, 777)

    def test_an_implausible_pid_is_refused(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=_identity_kernel32(pid=1))
        assert pc._windows_process_handle_identity(5) is None

    def test_unreadable_times_are_refused(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=_identity_kernel32(times_ok=[False]))
        assert pc._windows_process_handle_identity(5) is None

    def test_an_unreadable_exit_code_is_refused(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=_identity_kernel32(exit_code_ok=False))
        assert pc._windows_process_handle_identity(5) is None

    def test_a_zero_creation_time_is_refused(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=_identity_kernel32(creation=0))
        assert pc._windows_process_handle_identity(5) is None

    def test_a_failed_reread_after_exit_is_refused(self, monkeypatch):
        # The second read exists so the exit bound belongs to the terminated
        # object; losing it must refuse rather than report a stale bound.
        _fake_windows(
            monkeypatch,
            kernel32=_identity_kernel32(exit_code=0, times_ok=[True, False]),
        )
        assert pc._windows_process_handle_identity(5) is None

    def test_it_waits_out_the_unpublished_exit_filetime_window(self, monkeypatch):
        # GetExitCodeProcess reports the exit before the kernel publishes the
        # exit FILETIME; refusing inside that window rejected healthy handles.
        _fake_windows(
            monkeypatch,
            kernel32=_identity_kernel32(exit_code=0, exit_times=[0, 0, 555]),
        )
        slept = _fake_clock(monkeypatch, [0.0, 0.0, 0.0])
        assert pc._windows_process_handle_identity(5) == (4242, 100, 555)
        assert slept == [pc._WINDOWS_EXIT_FILETIME_POLL_SECS]

    def test_it_gives_up_when_the_exit_filetime_never_publishes(self, monkeypatch):
        # The recycling guard must not weaken into "assume it is fine".
        _fake_windows(monkeypatch, kernel32=_identity_kernel32(exit_code=0, exit_times=[0]))
        _fake_clock(monkeypatch, [0.0, 0.0, 99.0])
        assert pc._windows_process_handle_identity(5) is None

    def test_a_failed_read_inside_the_window_is_refused(self, monkeypatch):
        _fake_windows(
            monkeypatch,
            kernel32=_identity_kernel32(
                exit_code=0, exit_times=[0, 0], times_ok=[True, True, False]
            ),
        )
        _fake_clock(monkeypatch, [0.0, 0.0, 0.0])
        assert pc._windows_process_handle_identity(5) is None

    def test_a_loader_failure_is_refused(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.delattr(pc.ctypes, "WinDLL", raising=False)
        assert pc._windows_process_handle_identity(5) is None


def _exit_code_kernel32(code: int, *, ok: bool = True, terminated: bool = True) -> Any:
    def _get_exit_code(_handle: Any, out: Any) -> int:
        out._obj.value = code
        return 1 if ok else 0

    return types.SimpleNamespace(
        GetExitCodeProcess=_Fn(_get_exit_code),
        TerminateProcess=_const(terminated),
        # WAIT_TIMEOUT: the process object is not signalled, so a refused
        # terminate is read as a refusal of a live process.
        WaitForSingleObject=_const(0x102),
        CloseHandle=_const(True),
    )


class TestProcessHandles:
    def test_terminate_rejects_an_invalid_handle(self):
        with pytest.raises(ValueError):
            pc.terminate_process_handle(0)
        with pytest.raises(ValueError):
            pc.process_handle_active(-1)

    def test_handle_helpers_are_inert_off_windows(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        assert pc.terminate_process_handle(5) is False
        assert pc.process_handle_active(5) is False
        pc.close_process_handle(5)  # no-op, must not raise

    def test_terminate_kills_a_still_active_process(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=_exit_code_kernel32(259))
        assert pc.terminate_process_handle(9) is True

    def test_terminate_reports_false_for_an_already_exited_process(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=_exit_code_kernel32(0))
        assert pc.terminate_process_handle(9) is False

    def test_terminate_raises_when_the_exit_code_cannot_be_read(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=_exit_code_kernel32(259, ok=False))
        with pytest.raises(OSError, match="GetExitCodeProcess"):
            pc.terminate_process_handle(9)

    def test_terminate_raises_when_the_kill_itself_fails(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=_exit_code_kernel32(259, terminated=False))
        with pytest.raises(OSError, match="TerminateProcess"):
            pc.terminate_process_handle(9)

    def test_active_is_true_only_for_still_active(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=_exit_code_kernel32(259))
        assert pc.process_handle_active(9) is True
        _fake_windows(monkeypatch, kernel32=_exit_code_kernel32(1))
        assert pc.process_handle_active(9) is False

    def test_active_degrades_to_false_on_a_loader_failure(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.delattr(pc.ctypes, "WinDLL", raising=False)
        assert pc.process_handle_active(9) is False

    def test_close_handle_swallows_a_failure(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.delattr(pc.ctypes, "WinDLL", raising=False)
        pc.close_process_handle(9)  # suppressed, must not raise


class TestWindowsImageNameMatching:
    def test_image_name_is_read_from_the_snapshot(self, monkeypatch):
        kernel32 = _toolhelp_kernel32([(1, 0, b"init.exe"), (55, 1, b"kiro-cli.exe")])
        _fake_windows(monkeypatch, kernel32=kernel32)
        assert pc._win_process_image_name(55) == "kiro-cli.exe"

    def test_image_name_is_none_when_the_pid_is_absent(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=_toolhelp_kernel32([(1, 0, b"init.exe")]))
        assert pc._win_process_image_name(999) is None

    def test_image_name_is_none_on_an_invalid_snapshot(self, monkeypatch):
        invalid = pc.wintypes.HANDLE(-1).value
        _fake_windows(monkeypatch, kernel32=_toolhelp_kernel32([], snapshot=invalid))
        assert pc._win_process_image_name(1) is None

    def test_image_name_is_none_when_enumeration_cannot_start(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=_toolhelp_kernel32([]))
        assert pc._win_process_image_name(1) is None

    def test_image_name_is_none_on_a_loader_failure(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc.ctypes, "windll", None, raising=False)
        assert pc._win_process_image_name(1) is None

    def test_process_matches_is_case_insensitive_on_windows(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc.sys, "platform", "win32")
        monkeypatch.setattr(pc, "_win_process_image_name", lambda _pid: "Kiro-CLI.exe")
        assert pc.process_matches(5, ("kiro-cli",)) is True
        assert pc.process_matches(5, ("claude",)) is False

    def test_process_matches_is_false_when_the_image_is_unknown(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc.sys, "platform", "win32")
        monkeypatch.setattr(pc, "_win_process_image_name", lambda _pid: None)
        assert pc.process_matches(5, ("kiro-cli",)) is False

    def test_process_matches_uses_ps_on_macos(self, monkeypatch):
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: "/bin/ps")
        monkeypatch.setattr(
            pc.subprocess, "check_output", lambda *_a, **_k: b"/opt/kiro-cli --serve"
        )
        assert pc.process_matches(5, ("kiro-cli",)) is True

    def test_process_matches_is_false_without_a_trusted_ps(self, monkeypatch):
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: None)
        assert pc.process_matches(5, ("kiro-cli",)) is False

    def test_process_matches_is_false_on_an_unknown_platform(self, monkeypatch):
        monkeypatch.setattr(pc.sys, "platform", "aix")
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        assert pc.process_matches(5, ("kiro-cli",)) is False


# ---------------------------------------------------------------------------
# Command line / owner / liveness
# ---------------------------------------------------------------------------


class TestProcessCommandLine:
    def test_macos_uses_ps(self, monkeypatch):
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: "/bin/ps")
        monkeypatch.setattr(pc.subprocess, "check_output", lambda *_a, **_k: " kirocrew \n")
        assert pc.process_command_line(5) == "kirocrew"

    def test_macos_without_ps_is_empty(self, monkeypatch):
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: None)
        assert pc.process_command_line(5) == ""

    def test_windows_queries_wmi_through_powershell(self, monkeypatch):
        seen: dict[str, Any] = {}

        def _check_output(argv: Any, **kwargs: Any) -> str:
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return "python.exe -m kiro_crew gateway\n"

        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc.sys, "platform", "win32")
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: r"C:\ps.exe")
        monkeypatch.setattr(pc.subprocess, "check_output", _check_output)

        assert pc.process_command_line(7) == "python.exe -m kiro_crew gateway"
        assert "-NoProfile" in seen["argv"]
        assert "ProcessId=7" in " ".join(seen["argv"])
        # A console-less parent must not flash a window per poll.
        assert seen["kwargs"]["creationflags"] == pc._SUBPROCESS_NO_WINDOW

    def test_windows_without_powershell_is_empty(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc.sys, "platform", "win32")
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: None)
        assert pc.process_command_line(7) == ""

    def test_a_spawn_failure_degrades_to_empty(self, monkeypatch):
        def _boom(*_a: Any, **_k: Any) -> Any:
            raise subprocess.SubprocessError("timeout")

        monkeypatch.setattr(pc.sys, "platform", "darwin")
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: "/bin/ps")
        monkeypatch.setattr(pc.subprocess, "check_output", _boom)
        assert pc.process_command_line(5) == ""

    def test_unknown_platform_is_empty(self, monkeypatch):
        monkeypatch.setattr(pc.sys, "platform", "aix")
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        assert pc.process_command_line(5) == ""


class TestProcessOwnerUid:
    def test_macos_parses_the_uid_from_ps(self, monkeypatch):
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: "/bin/ps")
        monkeypatch.setattr(pc.subprocess, "check_output", lambda *_a, **_k: " 501 \n")
        assert pc.process_owner_uid(5) == 501

    def test_macos_non_numeric_output_is_unproven(self, monkeypatch):
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: "/bin/ps")
        monkeypatch.setattr(pc.subprocess, "check_output", lambda *_a, **_k: "\n")
        assert pc.process_owner_uid(5) is None

    def test_macos_without_ps_is_unproven(self, monkeypatch):
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: None)
        assert pc.process_owner_uid(5) is None

    def test_windows_has_no_uid_concept(self, monkeypatch):
        monkeypatch.setattr(pc.sys, "platform", "win32")
        assert pc.process_owner_uid(5) is None


class TestPidExistsWindows:
    @staticmethod
    def _kernel32(handle: int, code: int, *, got: bool = True) -> Any:
        def _get_exit_code(_handle: Any, out: Any) -> int:
            out._obj.value = code
            return 1 if got else 0

        return types.SimpleNamespace(
            OpenProcess=_const(handle),
            GetExitCodeProcess=_Fn(_get_exit_code),
            CloseHandle=_const(True),
        )

    def test_still_active_exists(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=self._kernel32(42, 259))
        assert pc.pid_exists(7) is True

    def test_a_defunct_handle_to_a_dead_pid_does_not_exist(self, monkeypatch):
        # OpenProcess succeeds while asyncio's Proactor transport still holds a
        # duplicated handle; without the exit-code confirmation every recycle
        # logged a false "PID survived kill".
        _fake_windows(monkeypatch, kernel32=self._kernel32(42, 0))
        assert pc.pid_exists(7) is False

    def test_an_unreadable_exit_code_is_treated_as_alive(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=self._kernel32(42, 0, got=False))
        assert pc.pid_exists(7) is True

    def test_a_failed_open_reports_absent_on_linux_ctypes(self, monkeypatch):
        # ctypes.get_last_error is Windows-only, so the ERROR_ACCESS_DENIED
        # probe degrades to "absent" rather than raising.
        _fake_windows(monkeypatch, kernel32=self._kernel32(0, 0))
        monkeypatch.delattr(pc.ctypes, "get_last_error", raising=False)
        assert pc.pid_exists(7) is False

    def test_a_loader_failure_reports_absent(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.delattr(pc.ctypes, "WinDLL", raising=False)
        assert pc.pid_exists(7) is False

    def test_liveness_maps_windows_onto_two_states(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "pid_exists", lambda _pid: True)
        assert pc.pid_liveness(7) == pc.PID_ALIVE
        monkeypatch.setattr(pc, "pid_exists", lambda _pid: False)
        assert pc.pid_liveness(7) == pc.PID_DEAD

    def test_liveness_treats_an_unknown_errno_as_unsignalable(self, monkeypatch):
        # Conservative by design: never prune or kill a PID we merely failed to
        # probe for an unrecognised reason.
        def _boom(_pid: int, _sig: int) -> None:
            raise OSError(9999, "who knows")

        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.os, "kill", _boom)
        assert pc.pid_liveness(7) == pc.PID_UNSIGNALABLE


# ---------------------------------------------------------------------------
# /proc readers
# ---------------------------------------------------------------------------


class TestProcReaders:
    def test_thread_count_is_unknown_off_linux(self, monkeypatch):
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        assert pc.process_thread_count(os.getpid()) is None

    @_LINUX_ONLY
    def test_thread_count_of_this_process_is_positive(self):
        count = pc.process_thread_count(os.getpid())
        assert count is not None and count >= 1

    @_LINUX_ONLY
    def test_thread_count_of_a_vanished_pid_is_unknown(self):
        # A pid far above the default pid_max has no /proc entry.
        assert pc.process_thread_count(2**30) is None

    def test_parent_pid_is_unknown_off_linux(self, monkeypatch):
        monkeypatch.setattr(pc.sys, "platform", "win32")
        assert pc.parent_pid(os.getpid()) is None

    @_LINUX_ONLY
    def test_parent_pid_matches_the_real_ppid(self):
        assert pc.parent_pid(os.getpid()) == os.getppid()

    @_LINUX_ONLY
    def test_parent_pid_of_a_vanished_pid_is_unknown(self):
        assert pc.parent_pid(2**30) is None

    def test_parent_pid_is_unknown_for_unparseable_stat(self, monkeypatch, tmp_path):
        # No ')' at all means the comm field cannot be located.
        stat_file = tmp_path / "stat"
        stat_file.write_text("garbage without a paren")
        monkeypatch.setattr(pc.sys, "platform", "linux")
        monkeypatch.setattr(pc, "Path", lambda _p: stat_file)
        assert pc.parent_pid(1) is None
        assert pc.get_process_start_id(1) is None

    def test_pids_holding_file_is_none_off_linux(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        assert pc.pids_holding_file(tmp_path) is None

    @_LINUX_ONLY
    def test_pids_holding_file_is_none_for_a_missing_path(self, tmp_path):
        assert pc.pids_holding_file(tmp_path / "nope") is None

    @_LINUX_ONLY
    def test_pids_holding_file_finds_this_process(self, tmp_path):
        target = tmp_path / "held"
        target.write_text("x")
        with open(target, "r"):
            holders = pc.pids_holding_file(target)
        assert holders is not None and os.getpid() in holders

    @_LINUX_ONLY
    def test_pids_holding_file_is_none_when_proc_is_unreadable(self, monkeypatch, tmp_path):
        target = tmp_path / "held"
        target.write_text("x")

        def _boom(_path: Any) -> Any:
            raise OSError("no /proc")

        monkeypatch.setattr(pc.os, "listdir", _boom)
        assert pc.pids_holding_file(target) is None


def _locks_row(path: Any, pid: int) -> str:
    """A ``/proc/locks`` FLOCK row whose device/inode triple matches *path*."""

    info = os.stat(path)
    return (
        f"1: FLOCK ADVISORY WRITE {pid} "
        f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino} 0 EOF\n"
    )


def _fake_proc_locks(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Serve *body* for ``/proc/locks`` and defer to the real ``open`` otherwise."""

    real_open = open

    def _open(path: Any, *args: Any, **kwargs: Any) -> Any:
        if str(path) == "/proc/locks":
            return io.StringIO(body)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(pc.sys, "platform", "linux")
    monkeypatch.setattr(pc, "open", _open, raising=False)


class TestFlockOwnerPid:
    def test_none_off_linux(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        assert pc.flock_owner_pid(tmp_path) is None

    def test_none_for_an_unstattable_path(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pc.sys, "platform", "linux")
        assert pc.flock_owner_pid(tmp_path / "missing") is None

    def test_matching_triple_names_the_acquirer(self, monkeypatch, tmp_path):
        lock = tmp_path / "gateway.lock"
        lock.write_text("")
        _fake_proc_locks(monkeypatch, _locks_row(lock, 39542))
        assert pc.flock_owner_pid(lock) == 39542

    def test_blocked_waiters_and_other_lock_types_are_skipped(self, monkeypatch, tmp_path):
        lock = tmp_path / "gateway.lock"
        lock.write_text("")
        info = os.stat(lock)
        triple = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
        body = (
            f"1: -> FLOCK ADVISORY WRITE 111 {triple} 0 EOF\n"
            f"2: POSIX ADVISORY WRITE 222 {triple} 0 EOF\n"
            "3: short row\n"
            f"4: FLOCK ADVISORY WRITE notanint {triple} 0 EOF\n" + _locks_row(lock, 333)
        )
        _fake_proc_locks(monkeypatch, body)
        assert pc.flock_owner_pid(lock) == 333

    def test_a_same_inode_lock_on_another_device_is_not_accepted(self, monkeypatch, tmp_path):
        # Inode numbers are only unique within a filesystem; accepting a bare
        # inode match would name a completely unrelated process.
        lock = tmp_path / "gateway.lock"
        lock.write_text("")
        info = os.stat(lock)
        body = f"1: FLOCK ADVISORY WRITE 777 fe:ff:{info.st_ino} 0 EOF\n"
        _fake_proc_locks(monkeypatch, body)
        assert pc.flock_owner_pid(lock) is None

    def test_unreadable_proc_locks_is_none(self, monkeypatch, tmp_path):
        lock = tmp_path / "gateway.lock"
        lock.write_text("")

        def _open(_path: Any, *_a: Any, **_k: Any) -> Any:
            raise OSError("permission denied")

        monkeypatch.setattr(pc.sys, "platform", "linux")
        monkeypatch.setattr(pc, "open", _open, raising=False)
        assert pc.flock_owner_pid(lock) is None

    def test_fdinfo_rows_are_read_like_proc_locks_rows(self, monkeypatch, tmp_path):
        lock = tmp_path / "gateway.lock"
        lock.write_text("")
        info = os.stat(lock)
        triple = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
        fdinfo = {
            # The descriptor the kernel reports first is a waiter on the inode.
            "/proc/41/fdinfo/3": f"pos:\t0\nflags:\t02\nlock:\t1: -> FLOCK ADVISORY WRITE 41 {triple} 0 EOF\n",
            # A POSIX lock on the same inode is a different lock type.
            "/proc/42/fdinfo/5": f"lock:\t1: POSIX ADVISORY WRITE 42 {triple} 0 EOF\n",
            "/proc/43/fdinfo/7": f"pos:\t0\nlock:\t1: FLOCK ADVISORY WRITE 43 {triple} 0 EOF\n",
        }
        _fake_fdinfo(monkeypatch, [(41, "3"), (42, "5"), (43, "7")], fdinfo)
        assert pc.flock_owner_pid(lock) == 43

    def test_an_fdinfo_lock_with_the_same_inode_number_on_another_device_is_not_accepted(
        self, monkeypatch, tmp_path
    ):
        # The descriptor can be closed and reused between the stat that selected
        # it and the fdinfo read, so the row's own triple still decides.
        lock = tmp_path / "gateway.lock"
        lock.write_text("")
        info = os.stat(lock)
        fdinfo = {
            "/proc/43/fdinfo/7": f"lock:\t1: FLOCK ADVISORY WRITE 43 fe:ff:{info.st_ino} 0 EOF\n"
        }
        _fake_fdinfo(monkeypatch, [(43, "7")], fdinfo)
        assert pc.flock_owner_pid(lock) is None

    def test_an_fdinfo_that_vanishes_under_the_read_is_skipped(self, monkeypatch, tmp_path):
        lock = tmp_path / "gateway.lock"
        lock.write_text("")
        info = os.stat(lock)
        triple = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
        fdinfo = {"/proc/44/fdinfo/9": f"lock:\t1: FLOCK ADVISORY WRITE 44 {triple} 0 EOF\n"}
        _fake_fdinfo(monkeypatch, [(43, "7"), (44, "9")], fdinfo)  # 43 raises: gone
        assert pc.flock_owner_pid(lock) == 44

    def test_an_unlistable_proc_after_a_scan_miss_is_none(self, monkeypatch, tmp_path):
        lock = tmp_path / "gateway.lock"
        lock.write_text("")
        _fake_proc_locks(monkeypatch, "")

        def _boom(_path: Any) -> Any:
            raise OSError("no /proc")

        monkeypatch.setattr(pc.os, "listdir", _boom)
        assert pc.flock_owner_pid(lock) is None


def _fake_fdinfo(
    monkeypatch: pytest.MonkeyPatch, descriptors: list[tuple[int, str]], files: dict[str, str]
) -> None:
    """An empty ``/proc/locks``, *descriptors* open on every inode, and *files* as ``fdinfo``.

    A descriptor whose ``fdinfo`` is not in *files* reads as a process that is
    gone (``OSError``), the way a real one does.
    """

    def _open(path: Any, *args: Any, **kwargs: Any) -> Any:
        if str(path) == "/proc/locks":
            return io.StringIO("")
        if str(path).startswith("/proc/"):
            if str(path) in files:
                return io.StringIO(files[str(path)])
            raise FileNotFoundError(str(path))
        return open(path, *args, **kwargs)

    monkeypatch.setattr(pc.sys, "platform", "linux")
    monkeypatch.setattr(pc, "open", _open, raising=False)
    monkeypatch.setattr(pc, "_descriptors_on", lambda _target: iter(descriptors))
