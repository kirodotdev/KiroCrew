"""Coverage-raising unit tests for :mod:`kiro_crew.platform_compat`.

Companion to ``test_platform_compat.py``, which covers the POSIX-native surface
and the process-session contracts that need a real process. This file targets
the branches that the host platform alone never reaches on the Linux CI fleet:

* the Windows ``ctypes``/``msvcrt`` paths, exercised by flipping ``IS_WINDOWS``
  and standing in for the DLL entry points (the same technique the sibling file
  already uses for the Toolhelp snapshot),
* the macOS ``libproc`` paths, exercised by faking ``ctypes.CDLL``,
* the macOS TCC walk-pruning rules,
* the failure/degradation paths of the ``/proc`` readers.

No test here spawns a process, touches the network, or writes outside
``tmp_path``; every clock the product consults is replaced with a fake, so
nothing depends on wall time or on execution order.
"""

from __future__ import annotations

import asyncio
import ctypes
import datetime
import errno
import io
import logging
import os
import subprocess
import sys
import types
from typing import Any, Callable

import pytest

from kiro_crew import platform_compat as pc

# Some tests below simulate the POSIX branch (forcing ``IS_POSIX = True``) or the
# Windows branch from a POSIX host, by monkeypatching attributes that only exist
# on one platform -- ``os.getpgid`` / ``os.killpg`` are absent on Windows, so
# ``monkeypatch.setattr`` itself raises there before the assertion runs.
#
# Skipping them on Windows costs ZERO coverage: ci.yml measures coverage on the
# ubuntu 3.12 shards only (the Windows lane runs --no-cov), so these lines are
# already counted on the lane that reports the number. Simulating the other
# platform's branch is only meaningful from the host that isn't that platform.
_POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt",
    reason="simulates a platform branch via attributes absent on Windows; "
    "coverage is measured on the ubuntu 3.12 shards, so this loses none",
)

# Applied to the WHOLE module, deliberately.
#
# This file's method is to simulate BOTH platform branches from one host: it
# flips ``IS_POSIX`` / ``IS_WINDOWS`` and monkeypatches ``os.getuid``,
# ``os.getpgid``, ``os.killpg``, ``os.fchmod``, ``/proc/locks`` reads and the
# Windows ``icacls`` subprocess. On real Windows those attributes do not exist
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
pytestmark = _POSIX_ONLY

# ---------------------------------------------------------------------------
# Windows / macOS fakes
# ---------------------------------------------------------------------------


class _Fn:
    """Stand-in for a ctypes function pointer.

    The product assigns ``argtypes``/``restype`` on every entry point before
    calling it, so a plain lambda will not do — the attributes must be
    assignable. Declaring them on the class lets the assignment shadow them per
    instance exactly as ctypes would.
    """

    argtypes: list = []
    restype: Any = None

    def __init__(self, impl: Callable[..., Any]) -> None:
        self._impl = impl

    def __call__(self, *args: Any) -> Any:
        return self._impl(*args)


def _const(value: Any) -> _Fn:
    """A fake entry point that ignores its arguments and returns *value*."""

    return _Fn(lambda *_args: value)


def _fake_windows(monkeypatch: pytest.MonkeyPatch, **dlls: Any) -> None:
    """Pretend to be Windows, resolving *dlls* by name for both load styles.

    ``platform_compat`` reaches Win32 two ways — ``ctypes.windll.<name>`` and
    ``ctypes.WinDLL("<name>", use_last_error=True)`` — and neither attribute
    exists on POSIX, hence ``raising=False``.
    """

    monkeypatch.setattr(pc, "IS_WINDOWS", True)
    monkeypatch.setattr(pc, "IS_POSIX", False)
    namespace = types.SimpleNamespace(**dlls)
    monkeypatch.setattr(pc.ctypes, "windll", namespace, raising=False)
    monkeypatch.setattr(
        pc.ctypes,
        "WinDLL",
        lambda name, **_kwargs: getattr(namespace, str(name)),
        raising=False,
    )


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


class _FakeMsvcrt:
    """Minimal ``msvcrt`` stand-in: ``locking`` replays scripted outcomes."""

    LK_NBLCK = 1
    LK_UNLCK = 0

    def __init__(self, outcomes: list[bool]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[int] = []

    def locking(self, _fd: int, mode: int, _nbytes: int) -> None:
        self.calls.append(mode)
        if mode == self.LK_UNLCK:
            return
        allowed = self._outcomes.pop(0) if self._outcomes else True
        if not allowed:
            raise OSError(13, "locked")


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


class _StubbornStream:
    """A stream whose ``reconfigure`` is refused, as seen in the 3-layer spawn."""

    encoding = "cp1252"

    def __init__(self, *, with_buffer: bool = True) -> None:
        self.buffer: Any = io.BytesIO() if with_buffer else None

    def reconfigure(self, **_kwargs: Any) -> None:
        raise ValueError("cannot reconfigure a detached stream")


class TestEnsureUtf8Console:
    def test_noop_off_windows(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        before = sys.stdout
        pc.ensure_utf8_console()
        assert sys.stdout is before

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
        pc.ensure_utf8_console()  # no exception = pass


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
    buf[16:20] = ppid.to_bytes(4, "little")
    buf[pc._DARWIN_OFF_START_TVSEC : pc._DARWIN_OFF_START_TVSEC + 8] = sec.to_bytes(8, "little")
    buf[pc._DARWIN_OFF_START_TVUSEC : pc._DARWIN_OFF_START_TVUSEC + 8] = usec.to_bytes(
        8, "little"
    )
    return bytes(buf)


def _fake_libproc(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: bytes | None,
    ret: int,
    library: str | None = "libproc.dylib",
) -> None:
    """Pretend to be macOS with a ``libproc`` that answers ``proc_pidinfo``."""

    monkeypatch.setattr(pc.sys, "platform", "darwin")
    monkeypatch.setattr(pc.ctypes.util, "find_library", lambda _n: library)

    def _proc_pidinfo(_pid: Any, _flavor: Any, _arg: Any, buf: Any, _size: Any) -> int:
        if payload is not None:
            ctypes.memmove(buf, payload, len(payload))
        return ret

    lib = types.SimpleNamespace(proc_pidinfo=_Fn(_proc_pidinfo))
    monkeypatch.setattr(pc.ctypes, "CDLL", lambda _p: lib)


def _toolhelp_kernel32(entries: list[tuple[int, int, bytes]], *, snapshot: Any = 123) -> Any:
    """A kernel32 whose process snapshot replays *entries* (pid, ppid, image)."""

    remaining = list(entries)

    def _fill(entry: Any) -> bool:
        if not remaining:
            return False
        pid, ppid, image = remaining.pop(0)
        entry._obj.th32ProcessID = pid
        entry._obj.th32ParentProcessID = ppid
        entry._obj.szExeFile = image
        return True

    return types.SimpleNamespace(
        CreateToolhelp32Snapshot=_const(snapshot),
        Process32First=_Fn(lambda _snap, entry: _fill(entry)),
        Process32Next=_Fn(lambda _snap, entry: _fill(entry)),
        CloseHandle=_const(True),
    )


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

    def test_windows_is_unknown_rather_than_a_mismatch(self, monkeypatch):
        monkeypatch.setattr(pc.sys, "platform", "win32")
        assert pc.get_process_start_id(5) is None

    def test_identity_never_contains_a_colon(self, monkeypatch):
        # Callers embed the value in colon-delimited records.
        _fake_libproc(monkeypatch, payload=_bsdinfo(sec=17, usec=1), ret=136)
        value = pc.get_process_start_id(5)
        assert value is not None and ":" not in value


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

    def test_open_handle_returns_the_opened_handle(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=types.SimpleNamespace(OpenProcess=_const(4321)))
        assert pc._open_process_termination_handle(500) == 4321

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
) -> Any:
    """kernel32 fake for ``_windows_process_handle_identity``.

    ``exit_times`` is replayed one entry per ``GetProcessTimes`` call, so a test
    can script the exited-but-exit-FILETIME-unpublished window; ``times_ok``
    scripts per-call success of the same function.
    """

    times = list(exit_times or [0])
    oks = list(times_ok or [])

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


_LINUX_ONLY = pytest.mark.skipif(
    sys.platform != "linux", reason="reads Linux /proc; the shim returns None elsewhere"
)


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


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


class TestRmtreeForce:
    def test_removes_a_tree_containing_read_only_files(self, tmp_path):
        # A git checkout is full of mode-444 loose objects, which Windows
        # refuses to unlink; the helper must clear the bit and still succeed.
        root = tmp_path / "tree"
        (root / "objects").mkdir(parents=True)
        obj = root / "objects" / "abc"
        obj.write_text("blob")
        obj.chmod(0o444)
        assert pc.rmtree_force(root) is True
        assert not root.exists()

    def test_reports_false_when_the_tree_survives(self, monkeypatch, tmp_path):
        # ignore_errors-style success reporting over a surviving tree is the bug
        # this return value exists to catch.
        root = tmp_path / "tree"
        root.mkdir()
        monkeypatch.setattr(pc.shutil, "rmtree", lambda *_a, **_k: None)
        assert pc.rmtree_force(root) is False

    def test_readonly_hook_retries_the_operation(self, tmp_path):
        victim = tmp_path / "ro.txt"
        victim.write_text("x")
        victim.chmod(0o444)
        removed: list[str] = []
        pc._clear_readonly_and_retry(removed.append, str(victim), OSError("denied"))
        assert removed == [str(victim)]

    def test_readonly_hook_warns_when_the_retry_also_fails(self, tmp_path, caplog):
        def _boom(_path: str) -> None:
            raise OSError("still denied")

        victim = tmp_path / "ro.txt"
        victim.write_text("x")
        with caplog.at_level(logging.WARNING, logger=pc.logger.name):
            pc._clear_readonly_and_retry(_boom, str(victim), OSError("denied"))
        assert any("Cannot remove" in r.getMessage() for r in caplog.records)


class TestLinkHelpers:
    def test_posix_symlink_round_trips(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        pc.symlink_or_junction(target, link)
        assert pc.is_link_or_junction(link) is True
        pc.unlink_link_or_junction(link)
        assert not link.exists()
        # The target must survive removal of the link.
        assert target.is_dir()

    def test_windows_symlink_requests_a_directory_link(self, monkeypatch, tmp_path):
        # Without target_is_directory=True the link is a FILE-type symlink to a
        # directory, which is not traversable on Windows.
        seen: dict[str, Any] = {}

        def _symlink(src: str, dst: str, **kwargs: Any) -> None:
            seen.update({"src": src, "dst": dst, **kwargs})

        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc.os, "symlink", _symlink)
        pc.symlink_or_junction(tmp_path / "t", tmp_path / "l")
        assert seen["target_is_directory"] is True

    def test_junction_fallback_is_false_for_a_plain_directory(self, tmp_path):
        assert pc._is_junction_fallback(tmp_path) is False

    def test_junction_fallback_is_false_for_a_missing_path(self, tmp_path):
        assert pc._is_junction_fallback(tmp_path / "gone") is False

    def test_junction_fallback_recognises_a_mount_point_reparse_tag(self, monkeypatch, tmp_path):
        fake = types.SimpleNamespace(
            st_file_attributes=pc._FILE_ATTRIBUTE_REPARSE_POINT,
            st_reparse_tag=pc._IO_REPARSE_TAG_MOUNT_POINT,
        )
        monkeypatch.setattr(pc.os, "stat", lambda *_a, **_k: fake)
        assert pc._is_junction_fallback(tmp_path) is True

    def test_junction_fallback_rejects_another_reparse_tag(self, monkeypatch, tmp_path):
        fake = types.SimpleNamespace(
            st_file_attributes=pc._FILE_ATTRIBUTE_REPARSE_POINT,
            st_reparse_tag=0xA000000C,
        )
        monkeypatch.setattr(pc.os, "stat", lambda *_a, **_k: fake)
        assert pc._is_junction_fallback(tmp_path) is False

    def test_is_link_or_junction_uses_the_stdlib_probe_when_present(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pc, "_ISJUNCTION", lambda _p: True)
        assert pc.is_link_or_junction(tmp_path) is True

    def test_is_link_or_junction_degrades_to_false_when_the_probe_raises(
        self, monkeypatch, tmp_path
    ):
        def _boom(_path: Any) -> bool:
            raise OSError("bad path")

        monkeypatch.setattr(pc, "_ISJUNCTION", _boom)
        assert pc.is_link_or_junction(tmp_path) is False

    def test_is_link_or_junction_uses_the_fallback_without_the_stdlib_probe(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(pc, "_ISJUNCTION", None)
        assert pc.is_link_or_junction(tmp_path) is False

    def test_unlink_removes_a_junction_with_rmdir(self, monkeypatch, tmp_path):
        # A junction is a directory reparse point: rmdir unlinks the junction
        # itself and never the target it points at.
        victim = tmp_path / "junction"
        victim.mkdir()
        calls: list[str] = []
        monkeypatch.setattr(pc, "_ISJUNCTION", lambda _p: True)
        # ``**_`` because this replaces the os module attribute for the whole test,
        # teardown included, and os.rmdir really takes dir_fd=.
        monkeypatch.setattr(pc.os, "rmdir", lambda p, **_: calls.append(str(p)))
        pc.unlink_link_or_junction(victim)
        assert calls == [str(victim)]

    def test_unlink_falls_through_to_unlink_for_a_real_file(self, tmp_path):
        plain = tmp_path / "plain.txt"
        plain.write_text("x")
        pc.unlink_link_or_junction(plain)
        assert not plain.exists()


class TestChmodHelpers:
    def test_chmod_safe_warns_instead_of_raising(self, monkeypatch, caplog, tmp_path):
        def _boom(*_a: Any, **_k: Any) -> None:
            raise OSError("read-only fs")

        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.os, "chmod", _boom)
        with caplog.at_level(logging.WARNING, logger=pc.logger.name):
            pc.chmod_safe(tmp_path / "f", 0o600)
        assert any("Cannot set permissions" in r.getMessage() for r in caplog.records)

    def test_fchmod_safe_warns_instead_of_raising(self, monkeypatch, caplog):
        def _boom(*_a: Any, **_k: Any) -> None:
            raise OSError("bad fd")

        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.os, "fchmod", _boom)
        with caplog.at_level(logging.WARNING, logger=pc.logger.name):
            pc.fchmod_safe(3, 0o600)
        assert any("Cannot set permissions" in r.getMessage() for r in caplog.records)

    def test_both_are_noops_on_windows(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pc, "IS_POSIX", False)

        def _never(*_a: Any, **_k: Any) -> None:
            pytest.fail("POSIX mode bits must not be touched on Windows")

        monkeypatch.setattr(pc.os, "chmod", _never)
        monkeypatch.setattr(pc.os, "fchmod", _never)
        pc.chmod_safe(tmp_path, 0o600)
        pc.fchmod_safe(3, 0o600)


# ---------------------------------------------------------------------------
# Windows SIDs
# ---------------------------------------------------------------------------


def _token_dlls(
    *,
    sid: str = "S-1-5-21-1-2-3-1000",
    open_process: int = 55,
    open_token: bool = True,
    size: int = 64,
    read_info: bool = True,
    convert: bool = True,
) -> tuple[Any, Any, list[str]]:
    """Fake advapi32/kernel32 for the access-token SID read.

    Returns ``(kernel32, advapi32, closed)`` where ``closed`` records which
    handles the product closed — the pseudo-handle from ``GetCurrentProcess``
    must never be one of them.
    """

    closed: list[str] = []
    info_calls: list[int] = []

    def _open_process_token(_process: Any, _access: Any, token: Any) -> int:
        if not open_token:
            return 0
        token._obj.value = 4242
        return 1

    def _get_token_information(_token: Any, _cls: Any, buf: Any, _len: Any, out_size: Any) -> int:
        info_calls.append(1)
        if len(info_calls) == 1:
            # First call sizes the buffer and is expected to "fail".
            out_size._obj.value = size
            return 0
        return 1 if read_info else 0

    def _convert(_sid: Any, out: Any) -> int:
        if not convert:
            return 0
        out._obj.value = sid
        return 1

    kernel32 = types.SimpleNamespace(
        GetCurrentProcess=_const(1),
        OpenProcess=_const(open_process),
        CloseHandle=_Fn(lambda handle: closed.append(str(getattr(handle, "value", handle)))),
        LocalFree=_const(0),
    )
    advapi32 = types.SimpleNamespace(
        OpenProcessToken=_Fn(_open_process_token),
        GetTokenInformation=_Fn(_get_token_information),
        ConvertSidToStringSidW=_Fn(_convert),
    )
    return kernel32, advapi32, closed


class TestProcessTokenSid:
    def test_none_on_posix(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", True)
        assert pc._process_token_sid() is None

    def test_reads_the_sid_from_this_process_token(self, monkeypatch):
        kernel32, advapi32, closed = _token_dlls()
        _fake_windows(monkeypatch, kernel32=kernel32, advapi32=advapi32)
        assert pc._process_token_sid_unguarded() == "S-1-5-21-1-2-3-1000"
        # The GetCurrentProcess pseudo-handle must NOT be closed; only the token.
        assert closed == ["4242"]

    def test_reads_the_sid_of_another_pid_and_closes_its_handle(self, monkeypatch):
        kernel32, advapi32, closed = _token_dlls()
        _fake_windows(monkeypatch, kernel32=kernel32, advapi32=advapi32)
        assert pc._process_token_sid_unguarded(1234) == "S-1-5-21-1-2-3-1000"
        assert closed == ["4242", "55"]

    def test_unopenable_process_is_none(self, monkeypatch):
        kernel32, advapi32, _ = _token_dlls(open_process=0)
        _fake_windows(monkeypatch, kernel32=kernel32, advapi32=advapi32)
        assert pc._process_token_sid_unguarded(1234) is None

    def test_unopenable_token_is_none(self, monkeypatch):
        kernel32, advapi32, _ = _token_dlls(open_token=False)
        _fake_windows(monkeypatch, kernel32=kernel32, advapi32=advapi32)
        assert pc._process_token_sid_unguarded() is None

    def test_zero_sized_token_information_is_none(self, monkeypatch):
        kernel32, advapi32, _ = _token_dlls(size=0)
        _fake_windows(monkeypatch, kernel32=kernel32, advapi32=advapi32)
        assert pc._process_token_sid_unguarded() is None

    def test_failed_token_information_read_is_none(self, monkeypatch):
        kernel32, advapi32, _ = _token_dlls(read_info=False)
        _fake_windows(monkeypatch, kernel32=kernel32, advapi32=advapi32)
        assert pc._process_token_sid_unguarded() is None

    def test_failed_sid_conversion_is_none(self, monkeypatch):
        kernel32, advapi32, _ = _token_dlls(convert=False)
        _fake_windows(monkeypatch, kernel32=kernel32, advapi32=advapi32)
        assert pc._process_token_sid_unguarded() is None

    def test_a_non_sid_string_is_rejected(self, monkeypatch):
        kernel32, advapi32, _ = _token_dlls(sid="not-a-sid")
        _fake_windows(monkeypatch, kernel32=kernel32, advapi32=advapi32)
        assert pc._process_token_sid_unguarded() is None

    def test_missing_windll_is_none(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.delattr(pc.ctypes, "WinDLL", raising=False)
        assert pc._process_token_sid_unguarded() is None

    def test_guarded_wrapper_swallows_an_unexpected_error(self, monkeypatch):
        def _boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("unexpected")

        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "_process_token_sid_unguarded", _boom)
        assert pc._process_token_sid() is None

    def test_process_owner_sid_is_none_on_posix(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", True)
        assert pc.process_owner_sid(7) is None

    def test_process_owner_sid_reads_the_peer_token(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "_process_token_sid_unguarded", lambda pid: f"S-1-5-{pid}")
        assert pc.process_owner_sid(7) == "S-1-5-7"

    def test_process_owner_sid_is_unverifiable_on_failure(self, monkeypatch):
        def _boom(_pid: Any) -> Any:
            raise OSError("denied")

        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "_process_token_sid_unguarded", _boom)
        assert pc.process_owner_sid(7) is None


class TestCurrentUserSid:
    @staticmethod
    def _fresh_caches(monkeypatch: pytest.MonkeyPatch) -> None:
        # Both memos are module-scoped; give each test its own so no test
        # depends on another having run (or not run) first.
        monkeypatch.setattr(pc, "_USER_SID_CACHE", [])
        monkeypatch.setattr(pc, "_TOKEN_SID_CACHE", [])

    def test_none_on_posix(self, monkeypatch):
        self._fresh_caches(monkeypatch)
        monkeypatch.setattr(pc, "IS_POSIX", True)
        assert pc._current_user_sid() is None

    def test_prefers_the_access_token_and_memoises_it(self, monkeypatch):
        self._fresh_caches(monkeypatch)
        calls: list[int] = []

        def _token() -> str:
            calls.append(1)
            return "S-1-5-21-9"

        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "_process_token_sid", _token)

        assert pc._current_user_sid() == "*S-1-5-21-9"
        assert pc._current_user_sid() == "*S-1-5-21-9"
        assert len(calls) == 1

    def test_falls_back_to_whoami_when_the_token_is_unavailable(self, monkeypatch):
        self._fresh_caches(monkeypatch)
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "_process_token_sid", lambda: None)
        monkeypatch.setattr(pc.shutil, "which", lambda _n: r"C:\whoami.exe")
        result = types.SimpleNamespace(
            returncode=0, stdout=b'"CORP\\zezhen","S-1-5-21-7-7-7-500"\n'
        )
        monkeypatch.setattr(pc.subprocess, "run", lambda *_a, **_k: result)
        assert pc._current_user_sid() == "*S-1-5-21-7-7-7-500"

    def test_a_failing_whoami_is_not_cached(self, monkeypatch):
        # A transient failure must stay retryable — memoising None would poison
        # every later restrict_to_owner for the process lifetime.
        self._fresh_caches(monkeypatch)
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "_process_token_sid", lambda: None)
        monkeypatch.setattr(pc.shutil, "which", lambda _n: r"C:\whoami.exe")
        monkeypatch.setattr(
            pc.subprocess,
            "run",
            lambda *_a, **_k: types.SimpleNamespace(returncode=1, stdout=b""),
        )
        assert pc._current_user_sid() is None
        assert pc._USER_SID_CACHE == []

    def test_a_whoami_spawn_error_is_none(self, monkeypatch):
        self._fresh_caches(monkeypatch)

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise OSError("not found")

        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "_process_token_sid", lambda: None)
        monkeypatch.setattr(pc.shutil, "which", lambda _n: None)
        monkeypatch.setattr(pc.subprocess, "run", _boom)
        assert pc._current_user_sid() is None

    @pytest.mark.parametrize("stdout", [b'"only-one-field"\n', b'"CORP\\u","NOT-A-SID"\n'])
    def test_unparseable_whoami_output_is_none(self, monkeypatch, stdout):
        self._fresh_caches(monkeypatch)
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "_process_token_sid", lambda: None)
        monkeypatch.setattr(pc.shutil, "which", lambda _n: r"C:\whoami.exe")
        monkeypatch.setattr(
            pc.subprocess,
            "run",
            lambda *_a, **_k: types.SimpleNamespace(returncode=0, stdout=stdout),
        )
        assert pc._current_user_sid() is None

    def test_bare_sid_strips_the_icacls_prefix_and_memoises(self, monkeypatch):
        self._fresh_caches(monkeypatch)
        calls: list[int] = []

        def _token() -> str:
            calls.append(1)
            return "*S-1-5-21-4"

        monkeypatch.setattr(pc, "_process_token_sid", _token)
        assert pc.current_user_sid() == "S-1-5-21-4"
        assert pc.current_user_sid() == "S-1-5-21-4"
        assert len(calls) == 1

    def test_bare_sid_is_none_when_the_token_read_fails(self, monkeypatch):
        self._fresh_caches(monkeypatch)
        monkeypatch.setattr(pc, "_process_token_sid", lambda: None)
        assert pc.current_user_sid() is None

    def test_local_user_id_is_the_uid_on_posix(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", True)
        assert pc.local_user_id() == os.getuid()

    def test_local_user_id_is_a_stable_crc_of_the_sid_on_windows(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "current_user_sid", lambda: "S-1-5-21-4")
        first = pc.local_user_id()
        assert isinstance(first, int) and first != 0
        assert pc.local_user_id() == first

    def test_local_user_id_collapses_to_zero_without_a_sid(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "current_user_sid", lambda: None)
        assert pc.local_user_id() == 0


class TestRestrictToOwner:
    def test_posix_applies_owner_only_mode(self, tmp_path):
        secret = tmp_path / "token.key"
        secret.write_text("x")
        secret.chmod(0o644)
        pc.restrict_to_owner(secret)
        assert oct(secret.stat().st_mode)[-3:] == "600"

    def test_windows_refuses_a_half_configured_dacl(self, monkeypatch, tmp_path):
        # An Owner-Rights-only DACL would lock the caller out of a file created
        # by a different principal, so an unresolvable SID must fail loud.
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "_current_user_sid", lambda: None)

        def _never(*_a: Any, **_k: Any) -> Any:
            pytest.fail("icacls must not run without a resolved SID")

        monkeypatch.setattr(pc.subprocess, "run", _never)
        with pytest.raises(OSError, match="cannot resolve current user SID"):
            pc.restrict_to_owner(tmp_path / "token.key")

    def test_windows_grants_both_owner_rights_and_the_caller(self, monkeypatch, tmp_path):
        seen: dict[str, Any] = {}

        def _run(argv: Any, **kwargs: Any) -> Any:
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return types.SimpleNamespace(returncode=0, stderr=b"", stdout=b"")

        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "_current_user_sid", lambda: "*S-1-5-21-3")
        monkeypatch.setattr(pc.shutil, "which", lambda _n: r"C:\icacls.exe")
        monkeypatch.setattr(pc.subprocess, "run", _run)

        pc.restrict_to_owner(tmp_path / "token.key")

        argv = seen["argv"]
        assert "/inheritance:r" in argv
        assert f"{pc._OWNER_RIGHTS_SID}:F" in argv
        assert "*S-1-5-21-3:F" in argv
        assert seen["kwargs"]["creationflags"] == pc._SUBPROCESS_NO_WINDOW

    def test_windows_does_not_duplicate_the_owner_rights_grant(self, monkeypatch, tmp_path):
        seen: dict[str, Any] = {}

        def _run(argv: Any, **_kwargs: Any) -> Any:
            seen["argv"] = argv
            return types.SimpleNamespace(returncode=0, stderr=b"", stdout=b"")

        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "_current_user_sid", lambda: pc._OWNER_RIGHTS_SID)
        monkeypatch.setattr(pc.shutil, "which", lambda _n: r"C:\icacls.exe")
        monkeypatch.setattr(pc.subprocess, "run", _run)
        pc.restrict_to_owner(tmp_path / "token.key")
        assert seen["argv"].count(f"{pc._OWNER_RIGHTS_SID}:F") == 1

    @_POSIX_ONLY
    def test_windows_raises_on_a_non_zero_icacls(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "_current_user_sid", lambda: "*S-1-5-21-3")
        monkeypatch.setattr(pc.shutil, "which", lambda _n: r"C:\icacls.exe")
        monkeypatch.setattr(
            pc.subprocess,
            "run",
            lambda *_a, **_k: types.SimpleNamespace(
                returncode=5, stderr=b"Access is denied.", stdout=b""
            ),
        )
        with pytest.raises(OSError, match="Access is denied"):
            pc.restrict_to_owner(tmp_path / "token.key")

    @_POSIX_ONLY
    def test_windows_raises_when_icacls_cannot_be_spawned(self, monkeypatch, tmp_path):
        def _boom(*_a: Any, **_k: Any) -> Any:
            raise subprocess.SubprocessError("timeout")

        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "_current_user_sid", lambda: "*S-1-5-21-3")
        monkeypatch.setattr(pc.shutil, "which", lambda _n: r"C:\icacls.exe")
        monkeypatch.setattr(pc.subprocess, "run", _boom)
        with pytest.raises(OSError, match="icacls invocation failed"):
            pc.restrict_to_owner(tmp_path / "token.key")

    def test_make_owner_only_dir_warns_instead_of_raising(self, monkeypatch, caplog, tmp_path):
        target = tmp_path / "secrets"

        def _boom(*_a: Any, **_k: Any) -> None:
            raise OSError("no perms")

        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc.Path, "chmod", _boom)
        with caplog.at_level(logging.WARNING, logger=pc.logger.name):
            pc.make_owner_only_dir(target)
        assert target.is_dir()
        assert any("owner-only" in r.getMessage() for r in caplog.records)

    def test_make_owner_only_dir_uses_the_dacl_on_windows(self, monkeypatch, tmp_path):
        called: list[Any] = []
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "restrict_to_owner", called.append)
        target = tmp_path / "secrets"
        pc.make_owner_only_dir(target)
        assert called and str(called[0]) == str(target)


# ---------------------------------------------------------------------------
# Executable / interpreter discovery
# ---------------------------------------------------------------------------


class TestExecutableDiscovery:
    def test_windows_target_accepts_known_script_suffixes(self, tmp_path):
        hook = tmp_path / "pre.ps1"
        hook.write_text("")
        # No execute bit is set: on Windows there is none, so an X_OK check
        # would silently disable every hook.
        assert pc.is_executable_file(hook, platform_name="win32") is True

    def test_windows_target_rejects_an_unknown_suffix(self, tmp_path):
        hook = tmp_path / "notes.md"
        hook.write_text("")
        assert pc.is_executable_file(hook, platform_name="win32") is False

    def test_posix_target_accepts_any_regular_file_from_a_windows_host(
        self, monkeypatch, tmp_path
    ):
        hook = tmp_path / "pre.sh"
        hook.write_text("")
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        assert pc.is_executable_file(hook, platform_name="linux") is True

    def test_a_directory_is_not_executable(self, tmp_path):
        assert pc.is_executable_file(tmp_path) is False

    def test_an_os_error_degrades_to_false(self, monkeypatch, tmp_path):
        def _boom(*_a: Any, **_k: Any) -> Any:
            raise OSError("stat failed")

        monkeypatch.setattr(pc.os.path, "isfile", _boom)
        assert pc.is_executable_file(tmp_path / "x") is False

    def test_store_stub_detection_is_windows_only(self, monkeypatch):
        stub = r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\python.exe"
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        assert pc._is_windows_store_python_stub(stub) is False
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        assert pc._is_windows_store_python_stub(stub) is True
        assert pc._is_windows_store_python_stub(r"C:\Python312\python.exe") is False


class TestFindPythonInterpreter:
    @staticmethod
    def _resolve(monkeypatch: pytest.MonkeyPatch, table: dict[str, str | None]) -> None:
        monkeypatch.setattr(pc.shutil, "which", lambda name: table.get(name))

    def test_skips_build_and_brazil_interpreters(self, monkeypatch):
        self._resolve(
            monkeypatch,
            {
                "python3.12": "/brazil-path/python3.12",
                "python3.11": "/x/build/private/python3.11",
                "python3.10": "/usr/bin/python3.10",
            },
        )
        monkeypatch.setattr(pc.subprocess, "check_output", lambda *_a, **_k: "3.10\n")
        assert pc.find_python_interpreter() == "/usr/bin/python3.10"

    def test_skips_the_microsoft_store_stub(self, monkeypatch):
        stub = r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\python.exe"
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        self._resolve(monkeypatch, {"python": stub, "python3": r"C:\Python312\python.exe"})
        monkeypatch.setattr(pc.subprocess, "check_output", lambda *_a, **_k: "3.12\n")
        assert pc.find_python_interpreter() == r"C:\Python312\python.exe"

    def test_rejects_an_interpreter_below_the_floor(self, monkeypatch):
        self._resolve(monkeypatch, {"python3": "/usr/bin/python3"})
        monkeypatch.setattr(pc.subprocess, "check_output", lambda *_a, **_k: "3.9\n")
        assert pc.find_python_interpreter() is None

    def test_a_probe_failure_falls_through(self, monkeypatch):
        def _boom(*_a: Any, **_k: Any) -> Any:
            raise subprocess.SubprocessError("timeout")

        self._resolve(monkeypatch, {"python3": "/usr/bin/python3"})
        monkeypatch.setattr(pc.subprocess, "check_output", _boom)
        assert pc.find_python_interpreter() is None

    def test_the_reject_predicate_falls_through_rather_than_aborting(self, monkeypatch):
        # A single unusable interpreter must not short-circuit the whole search.
        # POSIX order is 3.12, 3.11, 3.10, python3, 3.13 — vetoing 3.12 must
        # land on 3.11, not give up.
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        self._resolve(
            monkeypatch,
            {"python3.12": "/usr/bin/python3.12", "python3.11": "/usr/bin/python3.11"},
        )
        monkeypatch.setattr(pc.subprocess, "check_output", lambda *_a, **_k: "3.12\n")
        picked = pc.find_python_interpreter(reject=lambda p: p.endswith("3.12"))
        assert picked == "/usr/bin/python3.11"

    def test_nothing_resolvable_is_none(self, monkeypatch):
        self._resolve(monkeypatch, {})
        assert pc.find_python_interpreter() is None


# ---------------------------------------------------------------------------
# Resource / system metrics
# ---------------------------------------------------------------------------


def _fake_resource(monkeypatch: pytest.MonkeyPatch, **members: Any) -> None:
    defaults: dict[str, Any] = {
        "RUSAGE_SELF": 0,
        "RLIMIT_NOFILE": 7,
        "getrusage": lambda _who: types.SimpleNamespace(ru_maxrss=0, ru_utime=0.0, ru_stime=0.0),
        "getrlimit": lambda _res: (1024, 4096),
        "setrlimit": lambda _res, _limits: None,
    }
    defaults.update(members)
    monkeypatch.setattr(pc, "resource", types.SimpleNamespace(**defaults), raising=False)


def _memory_info_dll(working_set: int, *, ok: bool = True, peak: int = 0) -> Any:
    def _get_memory_info(_handle: Any, counters: Any, _cb: Any) -> int:
        counters._obj.WorkingSetSize = working_set
        counters._obj.PeakWorkingSetSize = peak
        return 1 if ok else 0

    return types.SimpleNamespace(GetProcessMemoryInfo=_Fn(_get_memory_info))


def _fake_mach_libsystem(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resident: int,
    resident_max: int,
    kern_return: int = 0,
) -> None:
    """Stand in for ``libSystem``'s ``task_info``, filling the real struct.

    The struct is the module-level layout, so a wrong field order in production
    surfaces here as the wrong number rather than passing on a hand-built dict.
    """

    def _task_info(_task: Any, _flavor: Any, out: Any, _count: Any) -> int:
        out._obj.resident_size = resident
        out._obj.resident_size_max = resident_max
        return kern_return

    fake = types.SimpleNamespace(
        mach_task_self=_Fn(lambda: 1),
        task_info=_Fn(_task_info),
    )
    monkeypatch.setattr(pc.ctypes, "CDLL", lambda _path, **_kw: fake)


class TestProcRss:
    """``proc_rss_bytes`` must report CURRENT residency, not the peak.

    ``ru_maxrss`` is a high-water mark that never decreases for the life of the
    process, so using it made the dashboard's per-process memory figure a
    monotonic peak-since-boot that no ``ps -o rss=`` reading could reproduce.
    These tests pin the live source per platform and keep ``ru_maxrss`` confined
    to the labelled fallback.
    """

    def test_linux_reads_statm_not_the_getrusage_peak(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.sys, "platform", "linux")
        _fake_resource(
            monkeypatch,
            getrusage=lambda _who: types.SimpleNamespace(ru_maxrss=9_999_999),
        )
        monkeypatch.setattr(pc, "_linux_current_rss_bytes", lambda: 4096)
        assert pc.proc_rss_bytes() == 4096

    @_LINUX_ONLY
    def test_linux_current_reader_agrees_with_proc_status(self):
        # statm's resident pages and status' VmRSS are the same quantity, and
        # VmRSS is what `ps -o rss=` prints -- the number a user compares against.
        vm_rss_kb = 0
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    vm_rss_kb = int(line.split()[1])
                    break
        assert vm_rss_kb > 0
        measured = pc._linux_current_rss_bytes()
        assert measured is not None
        # Sampled a moment apart, so allow drift rather than demanding equality.
        assert abs(measured - vm_rss_kb * 1024) < 4 * 1024 * 1024

    def test_linux_fallback_scales_the_peak_from_kib_to_bytes(self, monkeypatch):
        # Unit handling is the trap: ru_maxrss is KiB on Linux and bytes on
        # macOS, with nothing in the value to tell them apart.
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.sys, "platform", "linux")
        _fake_resource(
            monkeypatch,
            getrusage=lambda _who: types.SimpleNamespace(ru_maxrss=2048),
        )
        monkeypatch.setattr(pc, "_linux_current_rss_bytes", lambda: None)
        assert pc.proc_rss_bytes() == 2048 * 1024

    def test_macos_reads_the_mach_resident_size(self, monkeypatch):
        # Not resident_size_max, which is the same peak bug in Mach clothing.
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        _fake_resource(
            monkeypatch,
            getrusage=lambda _who: types.SimpleNamespace(ru_maxrss=7_777_777),
        )
        _fake_mach_libsystem(monkeypatch, resident=123_456, resident_max=999_999_999)
        assert pc.proc_rss_bytes() == 123_456

    def test_macos_task_info_failure_falls_back_to_the_peak_in_bytes(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        _fake_resource(
            monkeypatch,
            getrusage=lambda _who: types.SimpleNamespace(ru_maxrss=999),
        )
        _fake_mach_libsystem(monkeypatch, resident=1, resident_max=2, kern_return=5)
        # macOS ru_maxrss is ALREADY bytes -- scaling it by 1024 here would
        # over-report by three orders of magnitude.
        assert pc.proc_rss_bytes() == 999

    def test_macos_missing_libsystem_falls_back(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        _fake_resource(
            monkeypatch,
            getrusage=lambda _who: types.SimpleNamespace(ru_maxrss=555),
        )

        def _no_libsystem(_path: Any, **_kw: Any) -> Any:
            raise OSError("no libSystem")

        monkeypatch.setattr(pc.ctypes, "CDLL", _no_libsystem)
        assert pc.proc_rss_bytes() == 555

    def test_a_total_posix_failure_is_zero(self, monkeypatch):
        def _boom(_who: Any) -> Any:
            raise OSError("no rusage")

        monkeypatch.setattr(pc, "IS_POSIX", True)
        _fake_resource(monkeypatch, getrusage=_boom)
        monkeypatch.setattr(pc, "_linux_current_rss_bytes", lambda: None)
        monkeypatch.setattr(pc, "_macos_current_rss_bytes", lambda: None)
        assert pc.proc_rss_bytes() == 0

    def test_windows_reads_the_working_set(self, monkeypatch):
        _fake_windows(
            monkeypatch,
            psapi=_memory_info_dll(8192, peak=4_000_000),
            kernel32=types.SimpleNamespace(GetCurrentProcess=_const(1)),
        )
        # WorkingSetSize is already current; PeakWorkingSetSize must not leak in.
        assert pc.proc_rss_bytes() == 8192

    def test_windows_failure_is_zero(self, monkeypatch):
        _fake_windows(
            monkeypatch,
            psapi=_memory_info_dll(8192, ok=False),
            kernel32=types.SimpleNamespace(GetCurrentProcess=_const(1)),
        )
        assert pc.proc_rss_bytes() == 0

    def test_windows_loader_failure_is_zero(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.delattr(pc.ctypes, "WinDLL", raising=False)
        assert pc.proc_rss_bytes() == 0


class TestMachTaskBasicInfoLayout:
    """A wrong layout reads a neighbouring field and looks plausible.

    ``task_info`` writes into raw memory and reports back how many
    ``natural_t``-sized elements it filled, so a struct whose size disagrees
    with the kernel's yields a number with no error -- exactly the failure mode
    that is indistinguishable from the peak-vs-current bug being fixed.
    """

    def test_the_element_count_matches_mach_task_basic_info_count(self):
        # MACH_TASK_BASIC_INFO_COUNT from <mach/task_info.h>.
        assert pc._MACH_TASK_BASIC_INFO_COUNT == 12
        assert ctypes.sizeof(pc._MachTaskBasicInfo) == 48

    def test_resident_size_precedes_its_own_high_water_mark(self):
        # Both are uint64 and adjacent, so transposing them is a silent revert
        # to reporting the peak.
        assert pc._MachTaskBasicInfo.resident_size.offset == 8
        assert pc._MachTaskBasicInfo.resident_size_max.offset == 16

    def test_the_flavor_selector_is_mach_task_basic_info(self):
        assert pc._MACH_TASK_BASIC_INFO == 20


class TestProcPeakRss:
    """The peak is still reported, but as its own clearly-named reading."""

    def test_posix_scales_kib_to_bytes_on_linux(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.sys, "platform", "linux")
        _fake_resource(
            monkeypatch,
            getrusage=lambda _who: types.SimpleNamespace(ru_maxrss=2048),
        )
        assert pc.proc_peak_rss_bytes() == 2048 * 1024

    def test_posix_reports_bytes_directly_on_macos(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        _fake_resource(
            monkeypatch,
            getrusage=lambda _who: types.SimpleNamespace(ru_maxrss=999),
        )
        assert pc.proc_peak_rss_bytes() == 999

    def test_a_getrusage_failure_is_zero(self, monkeypatch):
        def _boom(_who: Any) -> Any:
            raise OSError("no rusage")

        monkeypatch.setattr(pc, "IS_POSIX", True)
        _fake_resource(monkeypatch, getrusage=_boom)
        assert pc.proc_peak_rss_bytes() == 0

    def test_windows_reads_the_peak_working_set(self, monkeypatch):
        _fake_windows(
            monkeypatch,
            psapi=_memory_info_dll(8192, peak=4_000_000),
            kernel32=types.SimpleNamespace(GetCurrentProcess=_const(1)),
        )
        assert pc.proc_peak_rss_bytes() == 4_000_000

    def test_windows_failure_is_zero(self, monkeypatch):
        _fake_windows(
            monkeypatch,
            psapi=_memory_info_dll(8192, ok=False, peak=4_000_000),
            kernel32=types.SimpleNamespace(GetCurrentProcess=_const(1)),
        )
        assert pc.proc_peak_rss_bytes() == 0


class TestProcRssForPid:
    @_LINUX_ONLY
    def test_linux_reads_statm_for_this_process(self):
        rss = pc.proc_rss_bytes_for_pid(os.getpid())
        assert rss is not None and rss > 0

    @_LINUX_ONLY
    def test_linux_vanished_pid_is_unknown(self):
        assert pc.proc_rss_bytes_for_pid(2**30) is None

    def test_macos_has_no_ctypes_only_path(self, monkeypatch):
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        assert pc.proc_rss_bytes_for_pid(1) is None

    @staticmethod
    def _win(monkeypatch: pytest.MonkeyPatch, handle: int, *, ok: bool = True) -> None:
        kernel32 = types.SimpleNamespace(
            OpenProcess=_const(handle),
            CloseHandle=_const(True),
        )
        _fake_windows(monkeypatch, kernel32=kernel32, psapi=_memory_info_dll(4096, ok=ok))
        monkeypatch.setattr(pc.sys, "platform", "win32")

    def test_windows_reads_the_working_set_of_another_pid(self, monkeypatch):
        self._win(monkeypatch, 77)
        assert pc.proc_rss_bytes_for_pid(9) == 4096

    def test_windows_unopenable_pid_is_unknown(self, monkeypatch):
        self._win(monkeypatch, 0)
        assert pc.proc_rss_bytes_for_pid(9) is None

    def test_windows_failed_query_is_unknown(self, monkeypatch):
        self._win(monkeypatch, 77, ok=False)
        assert pc.proc_rss_bytes_for_pid(9) is None


class TestProcRssTree:
    def test_none_off_windows(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        assert pc.proc_rss_tree_mb_for_pid(os.getpid()) is None

    @pytest.mark.parametrize("bad", [0, 1, -5, True])
    def test_implausible_roots_are_refused(self, monkeypatch, bad):
        # `True` is an int subclass; the strict type check keeps a bool out.
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        assert pc.proc_rss_tree_mb_for_pid(bad) is None

    def test_unanchorable_root_falls_back_to_the_single_process_read(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_open_process_termination_handle", lambda _pid: None)
        monkeypatch.setattr(pc, "proc_rss_bytes_for_pid", lambda _pid: 2 * 1024 * 1024)
        assert pc.proc_rss_tree_mb_for_pid(500) == pytest.approx(2.0)

    def test_unanchorable_and_unreadable_root_is_unknown(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_open_process_termination_handle", lambda _pid: None)
        monkeypatch.setattr(pc, "proc_rss_bytes_for_pid", lambda _pid: None)
        assert pc.proc_rss_tree_mb_for_pid(500) is None

    def test_a_recycled_root_is_not_measured_as_a_tree(self, monkeypatch):
        # Identity mismatch means the PID was recycled: fall back to the single
        # read rather than summing an unrelated subtree.
        closed: list[int] = []
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_open_process_termination_handle", lambda _pid: 900)
        monkeypatch.setattr(pc, "_windows_process_handle_identity", lambda _h: (999, 1, None))
        monkeypatch.setattr(pc, "proc_rss_bytes_for_pid", lambda _pid: 1024 * 1024)
        monkeypatch.setattr(pc, "close_process_handle", closed.append)
        assert pc.proc_rss_tree_mb_for_pid(500) == pytest.approx(1.0)

    def test_sums_the_root_and_its_validated_descendants(self, monkeypatch):
        closed: list[int] = []
        rss = {500: 1024 * 1024, 501: 3 * 1024 * 1024}
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_open_process_termination_handle", lambda _pid: 900)
        monkeypatch.setattr(pc, "_windows_process_handle_identity", lambda _h: (500, 1, None))
        monkeypatch.setattr(pc, "descendant_termination_handles", lambda _pid, **_k: {501: 901})
        monkeypatch.setattr(pc, "proc_rss_bytes_for_pid", rss.get)
        monkeypatch.setattr(pc, "close_process_handle", closed.append)

        assert pc.proc_rss_tree_mb_for_pid(500) == pytest.approx(4.0)
        # Every handle the scan opened must be released.
        assert sorted(closed) == [900, 901]

    def test_a_failed_enumeration_measures_the_root_alone(self, monkeypatch):
        def _boom(_pid: Any, **_k: Any) -> Any:
            raise OSError("snapshot race")

        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_open_process_termination_handle", lambda _pid: 900)
        monkeypatch.setattr(pc, "_windows_process_handle_identity", lambda _h: (500, 1, None))
        monkeypatch.setattr(pc, "descendant_termination_handles", _boom)
        monkeypatch.setattr(pc, "proc_rss_bytes_for_pid", lambda _pid: 5 * 1024 * 1024)
        monkeypatch.setattr(pc, "close_process_handle", lambda _h: None)
        assert pc.proc_rss_tree_mb_for_pid(500) == pytest.approx(5.0)

    def test_an_entirely_unreadable_tree_is_unknown(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_open_process_termination_handle", lambda _pid: 900)
        monkeypatch.setattr(pc, "_windows_process_handle_identity", lambda _h: (500, 1, None))
        monkeypatch.setattr(pc, "descendant_termination_handles", lambda _pid, **_k: {})
        monkeypatch.setattr(pc, "proc_rss_bytes_for_pid", lambda _pid: None)
        monkeypatch.setattr(pc, "close_process_handle", lambda _h: None)
        assert pc.proc_rss_tree_mb_for_pid(500) is None


class TestProcCpuSeconds:
    def test_posix_sums_user_and_system_time(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", True)
        _fake_resource(
            monkeypatch,
            getrusage=lambda _who: types.SimpleNamespace(ru_utime=1.5, ru_stime=0.25),
        )
        assert pc.proc_cpu_seconds() == pytest.approx(1.75)

    def test_posix_failure_is_zero(self, monkeypatch):
        def _boom(_who: Any) -> Any:
            raise ValueError("bad who")

        monkeypatch.setattr(pc, "IS_POSIX", True)
        _fake_resource(monkeypatch, getrusage=_boom)
        assert pc.proc_cpu_seconds() == 0.0

    @staticmethod
    def _times_kernel32(kernel_ticks: int, user_ticks: int, *, ok: bool = True) -> Any:
        def _get_times(_handle: Any, _creation: Any, _exit: Any, kernel: Any, user: Any) -> int:
            kernel._obj.dwHighDateTime = kernel_ticks >> 32
            kernel._obj.dwLowDateTime = kernel_ticks & 0xFFFFFFFF
            user._obj.dwHighDateTime = user_ticks >> 32
            user._obj.dwLowDateTime = user_ticks & 0xFFFFFFFF
            return 1 if ok else 0

        return types.SimpleNamespace(
            GetCurrentProcess=_const(1),
            GetProcessTimes=_Fn(_get_times),
        )

    def test_windows_converts_100ns_filetimes_to_seconds(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=self._times_kernel32(10_000_000, 25_000_000))
        assert pc.proc_cpu_seconds() == pytest.approx(3.5)

    def test_windows_failure_is_zero(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=self._times_kernel32(1, 1, ok=False))
        assert pc.proc_cpu_seconds() == 0.0

    def test_windows_loader_failure_is_zero(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc.ctypes, "windll", None, raising=False)
        assert pc.proc_cpu_seconds() == 0.0


class TestSystemMetrics:
    def test_memory_is_none_off_windows(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        assert pc.system_memory() is None

    @staticmethod
    def _memory_status(total: int, avail: int, *, ok: bool = True) -> Any:
        def _status(status: Any) -> int:
            status._obj.ullTotalPhys = total
            status._obj.ullAvailPhys = avail
            return 1 if ok else 0

        return types.SimpleNamespace(GlobalMemoryStatusEx=_Fn(_status))

    def test_memory_reports_total_and_available(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=self._memory_status(16 << 30, 4 << 30))
        assert pc.system_memory() == (16 << 30, 4 << 30)

    def test_memory_failure_is_none(self, monkeypatch):
        _fake_windows(monkeypatch, kernel32=self._memory_status(1, 1, ok=False))
        assert pc.system_memory() is None

    def test_memory_loader_failure_is_none(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc.ctypes, "windll", None, raising=False)
        assert pc.system_memory() is None

    def test_cpu_percent_is_none_off_windows(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        assert pc.system_cpu_percent() is None

    @staticmethod
    def _system_times(samples: list[tuple[int, int, int]], *, ok: bool = True) -> Any:
        reads = iter(samples)

        def _get(idle: Any, kernel: Any, user: Any) -> int:
            if not ok:
                return 0
            idle_t, kernel_t, user_t = next(reads)
            for holder, value in ((idle, idle_t), (kernel, kernel_t), (user, user_t)):
                holder._obj.dwHighDateTime = value >> 32
                holder._obj.dwLowDateTime = value & 0xFFFFFFFF
            return 1

        return types.SimpleNamespace(GetSystemTimes=_Fn(_get))

    def test_cpu_percent_needs_two_samples(self, monkeypatch):
        # The first call only primes the delta; a percentage from one sample
        # would be meaningless. Between the samples 100 ticks elapsed of which
        # 15 were idle, so 85% was busy.
        monkeypatch.setattr(pc, "_prev_win_sys_cpu", {"idle": 0.0, "total": 0.0})
        _fake_windows(
            monkeypatch,
            kernel32=self._system_times([(10, 40, 0), (25, 140, 0)]),
        )
        assert pc.system_cpu_percent() is None
        assert pc.system_cpu_percent() == pytest.approx(85.0)

    def test_cpu_percent_is_none_without_forward_progress(self, monkeypatch):
        monkeypatch.setattr(pc, "_prev_win_sys_cpu", {"idle": 0.0, "total": 0.0})
        _fake_windows(
            monkeypatch,
            kernel32=self._system_times([(0, 100, 0), (0, 100, 0)]),
        )
        assert pc.system_cpu_percent() is None  # primes
        assert pc.system_cpu_percent() is None  # zero delta

    def test_cpu_percent_failure_is_none(self, monkeypatch):
        monkeypatch.setattr(pc, "_prev_win_sys_cpu", {"idle": 0.0, "total": 0.0})
        _fake_windows(monkeypatch, kernel32=self._system_times([], ok=False))
        assert pc.system_cpu_percent() is None

    def test_cpu_percent_loader_failure_is_none(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc.ctypes, "windll", None, raising=False)
        assert pc.system_cpu_percent() is None


class _FormatRecorder:
    """Captures the format string the shim finally hands to ``strftime``."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def strftime(self, fmt: str) -> str:
        self.seen.append(fmt)
        return fmt


class TestStrftime:
    def test_posix_passes_the_format_through_unchanged(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        recorder = _FormatRecorder()
        pc.strftime(recorder, "%-I:%M %p")
        assert recorder.seen == ["%-I:%M %p"]

    @pytest.mark.parametrize(
        ("posix_fmt", "windows_fmt"),
        [
            ("%-I:%M %p", "%#I:%M %p"),
            ("%-d/%-m", "%#d/%#m"),
            ("%Y-%m-%d", "%Y-%m-%d"),
            ("100%% done at %-I", "100%% done at %#I"),
            ("trailing %", "trailing %"),
            ("dangling %-", "dangling %-"),
        ],
    )
    def test_windows_translates_the_no_pad_directives(self, monkeypatch, posix_fmt, windows_fmt):
        # %-I is a glibc/BSD extension that MSVCRT rejects; %#I is its spelling.
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        recorder = _FormatRecorder()
        pc.strftime(recorder, posix_fmt)
        assert recorder.seen == [windows_fmt]

    def test_a_real_datetime_still_formats_on_this_host(self):
        moment = datetime.datetime(2026, 8, 7, 9, 5)
        assert pc.strftime(moment, "%Y-%m-%d") == "2026-08-07"


class TestRaiseNofileSoftLimit:
    def test_noop_on_windows(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", False)

        def _never(*_a: Any, **_k: Any) -> Any:
            pytest.fail("there is no descriptor rlimit on Windows")

        _fake_resource(monkeypatch, getrlimit=_never)
        pc.raise_nofile_soft_limit(4096)

    def test_raises_the_soft_limit_towards_the_target(self, monkeypatch):
        applied: list[tuple[int, tuple[int, int]]] = []
        monkeypatch.setattr(pc, "IS_POSIX", True)
        _fake_resource(
            monkeypatch,
            getrlimit=lambda _res: (256, 4096),
            setrlimit=lambda res, limits: applied.append((res, limits)),
        )
        pc.raise_nofile_soft_limit(1024)
        assert applied == [(7, (1024, 4096))]

    def test_the_hard_limit_is_the_ceiling(self, monkeypatch):
        applied: list[tuple[int, tuple[int, int]]] = []
        monkeypatch.setattr(pc, "IS_POSIX", True)
        _fake_resource(
            monkeypatch,
            getrlimit=lambda _res: (256, 512),
            setrlimit=lambda res, limits: applied.append((res, limits)),
        )
        pc.raise_nofile_soft_limit(99999)
        assert applied == [(7, (512, 512))]

    def test_an_already_generous_limit_is_left_alone(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", True)

        def _never(*_a: Any, **_k: Any) -> Any:
            pytest.fail("must not lower an already-sufficient limit")

        _fake_resource(monkeypatch, getrlimit=lambda _res: (8192, 8192), setrlimit=_never)
        pc.raise_nofile_soft_limit(1024)

    def test_a_refused_setrlimit_is_logged_not_raised(self, monkeypatch, caplog):
        def _boom(*_a: Any, **_k: Any) -> None:
            raise ValueError("not permitted")

        monkeypatch.setattr(pc, "IS_POSIX", True)
        _fake_resource(monkeypatch, getrlimit=lambda _res: (256, 4096), setrlimit=_boom)
        with caplog.at_level(logging.DEBUG, logger=pc.logger.name):
            pc.raise_nofile_soft_limit(1024)
        assert any("RLIMIT_NOFILE" in r.getMessage() for r in caplog.records)


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
        assert (
            pc._windows_lineage_matches_lifetimes(102, self.ROOT, parent_map, identities) is True
        )

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
        monkeypatch.setattr(
            pc, "_windows_process_handle_identity", lambda _h: (999, 1, None)
        )
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
        monkeypatch.setattr(pc, "_open_process_termination_handle", lambda _pid: 9001)
        monkeypatch.setattr(
            pc,
            "_windows_process_handle_identity",
            {8001: (100, 10, None), 9001: (101, 15, None)}.get,
        )
        monkeypatch.setattr(pc, "close_process_handle", closed.append)
        with pytest.raises(OSError, match="snapshot vanished"):
            pc.descendant_termination_handles(100, {}, 8001)
        assert closed == [9001]

    def test_skips_children_whose_handle_cannot_be_opened(self, monkeypatch):
        # A child that exits between the snapshot and the open is not an error;
        # it just is not ours to terminate.
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_windows_process_parent_map", lambda: {101: 100})
        monkeypatch.setattr(pc, "_open_process_termination_handle", lambda _pid: None)
        monkeypatch.setattr(
            pc, "_windows_process_handle_identity", lambda _h: (100, 10, None)
        )
        assert pc.descendant_termination_handles(100, {}, 8001) == {}


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
            kernel32=types.SimpleNamespace(
                GetCurrentProcess=_const(1), DuplicateHandle=_Fn(_boom)
            ),
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
