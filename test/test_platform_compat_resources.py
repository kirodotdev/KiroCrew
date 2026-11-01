"""Resource readings: this process's peak RSS and CPU time, and memory pressure.

``proc_peak_rss_bytes`` reporting the process's own high-water mark, the CPU
and RSS-tree readers, what each reader answers when every source fails, the
PSS of a process subtree, and the macOS memory-pressure level. A topic of the
``test_platform_compat`` family; see ``test_platform_compat.py``.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
import textwrap
import threading
import types
from pathlib import Path

import pytest

from kiro_crew import platform_compat as pc

# This file replaces ctypes.CDLL, find_library or a cached libc handle for a test,
# so the shared macOS handle caches are put back after each one (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


class TestPeakRssIsThisProcesss:
    """``proc_peak_rss_bytes`` reports the PROCESS's own high-water mark.

    On Linux ``execve`` seeds the new image's ``ru_maxrss`` with the pre-exec
    image's peak, so a gateway launched from a large parent would otherwise
    publish that parent's peak on ``proc_mem_peak_mb`` and the
    ``memory.peak_rss_bytes`` gauge for its whole life.
    """

    _STATUS = "Name:\tpython\nVmPeak:\t 1400000 kB\nVmHWM:\t   11432 kB\nVmRSS:\t   11432 kB\n"

    # These tests drive the POSIX reader (``_posix_peak_rss_bytes``) directly, with
    # ``sys.platform`` and the status-file seam pinned, so they run on every host:
    # ``proc_peak_rss_bytes`` itself dispatches on the import-time ``IS_POSIX`` and
    # on Windows answers from the Win32 counters, which have their own tests. The
    # ``resource`` module exists only on POSIX, so the ``getrusage`` stand-in is
    # installed with ``raising=False`` -- on Windows it CREATES the attribute the
    # reader would consult, which is exactly what must never happen.

    @staticmethod
    def _never_getrusage(monkeypatch):
        def inherited(*_a, **_k):
            raise AssertionError("ru_maxrss consulted on linux: that is the parent's peak")

        monkeypatch.setattr(
            pc, "resource", types.SimpleNamespace(RUSAGE_SELF=0, getrusage=inherited), raising=False
        )

    def test_linux_peak_is_its_own_vmhwm_never_getrusage(self, tmp_path, monkeypatch):
        status = tmp_path / "status"
        status.write_text(self._STATUS, encoding="utf-8")
        self._never_getrusage(monkeypatch)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(pc, "_LINUX_STATUS_PATH", status)
        monkeypatch.setattr(pc, "_LINUX_PEAK_RSS_FLOOR", 0)
        assert pc._posix_peak_rss_bytes() == 11432 * 1024
        # An unreadable /proc is None (the callers' documented 0), not the
        # inherited number and not the floor a readable earlier call left behind.
        monkeypatch.setattr(pc, "_LINUX_STATUS_PATH", tmp_path / "gone")
        assert pc._posix_peak_rss_bytes() is None

    def test_a_name_that_is_not_utf8_does_not_hide_the_peak(self, tmp_path, monkeypatch):
        """The ``Name:`` line is this process's raw comm, which a strict read raises on."""
        status = tmp_path / "status"
        status.write_bytes(b"Name:\tkc-\xff\n" + self._STATUS.encode())
        self._never_getrusage(monkeypatch)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(pc, "_LINUX_STATUS_PATH", status)
        monkeypatch.setattr(pc, "_LINUX_PEAK_RSS_FLOOR", 0)
        assert pc._posix_peak_rss_bytes() == 11432 * 1024

    @pytest.mark.skipif(
        not pc.IS_POSIX, reason="proc_rss_bytes's POSIX last resort; Windows reads Win32 counters"
    )
    def test_the_current_readings_last_resort_is_the_same_own_peak(self, tmp_path, monkeypatch):
        status = tmp_path / "status"
        status.write_text(self._STATUS, encoding="utf-8")
        self._never_getrusage(monkeypatch)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(pc, "_LINUX_STATUS_PATH", status)
        monkeypatch.setattr(pc, "_LINUX_PEAK_RSS_FLOOR", 0)
        monkeypatch.setattr(pc, "_linux_current_rss_bytes", lambda: None)
        assert pc.proc_rss_bytes() == 11432 * 1024

    @pytest.mark.parametrize(
        "status",
        [
            "Name:\tpython\nVmRSS:\t   11432 kB\n",  # no high-water field at all
            "VmHWM:\t   11432 MB\n",  # a unit the kernel never prints
            "VmHWM:\t   lots kB\n",
            "VmHWM:\n",
            "",
        ],
    )
    def test_an_unreadable_status_is_none_not_a_guess(self, status):
        assert pc._peak_rss_from_status(status) is None

    def test_the_linux_reading_never_decreases(self, tmp_path, monkeypatch):
        # The kernel folds the live RSS into hiwater_rss lazily from batched
        # per-thread counters, so consecutive VmHWM readings around an unmap
        # can dip by a few hundred KiB. The contract is a peak, so the reader
        # holds its floor -- and drops it only for an unreadable file, which
        # is None, not a stale number.
        status = tmp_path / "status"
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(pc, "_LINUX_STATUS_PATH", status)
        monkeypatch.setattr(pc, "_LINUX_PEAK_RSS_FLOOR", 0)
        readings = []
        for kib in (186_335_232 // 1024, 185_970_688 // 1024, 200_000):
            status.write_text(f"VmHWM:\t{kib} kB\n", encoding="utf-8")
            readings.append(pc._linux_peak_rss_bytes())
        assert readings == [186_335_232, 186_335_232, 200_000 * 1024]
        status.unlink()
        assert pc._linux_peak_rss_bytes() is None

    @pytest.mark.parametrize(
        ("platform", "ru_maxrss", "expected"),
        [("darwin", 123_456_789, 123_456_789), ("freebsd13", 11432, 11432 * 1024)],
    )
    def test_off_linux_getrusage_is_read_in_the_platforms_unit(
        self, monkeypatch, platform, ru_maxrss, expected
    ):
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setattr(
            pc,
            "resource",
            types.SimpleNamespace(
                RUSAGE_SELF=0, getrusage=lambda _who: types.SimpleNamespace(ru_maxrss=ru_maxrss)
            ),
            raising=False,
        )
        monkeypatch.setattr(pc, "_LINUX_STATUS_PATH", Path("/nonexistent/status"))
        assert pc._posix_peak_rss_bytes() == expected

    def test_the_two_status_parsers_agree(self):
        # pdf_extract_child keeps its own copy (it must not import this module
        # under a capped address space); the two must read the same text alike.
        from kiro_crew import pdf_extract_child

        for text in (self._STATUS, "VmHWM:\t 1 kB\n", "VmHWM:\t 1 MB\n", ""):
            assert pc._peak_rss_from_status(text) == pdf_extract_child._peak_rss_from_status(text)

    def test_a_child_of_a_bloated_parent_reports_its_own_small_peak(self, tmp_path):
        """A parent that has touched twice the bar spawns a child that reports its
        own peak under it. The planted condition is proven, not assumed: the
        parent's measured peak must exceed the bar, and on Linux the child's raw
        ``ru_maxrss`` must show the inheritance the reader exists to bypass."""
        bar = 256 * 1024 * 1024
        child = textwrap.dedent("""
            import json, sys
            from kiro_crew import platform_compat as pc
            json.dump(
                {"own_peak": pc.proc_peak_rss_bytes(),
                 "inherited": pc._ru_maxrss_bytes() if pc.IS_POSIX else None},
                sys.stdout,
            )
            """)
        parent = textwrap.dedent(f"""
            import json, subprocess, sys
            from kiro_crew import platform_compat as pc
            blob = bytearray({2 * bar})
            for i in range(0, len(blob), 4096):
                blob[i] = 1
            del blob
            report = {{"parent_peak": pc.proc_peak_rss_bytes()}}
            run = subprocess.run(
                [sys.executable, "-c", {child!r}], cwd={str(tmp_path)!r},
                capture_output=True, text=True, timeout=60, check=True,
            )
            report.update(json.loads(run.stdout))
            json.dump(report, sys.stdout)
            """)
        run = subprocess.run(
            [sys.executable, "-c", parent],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=tmp_path,
            timeout=90,
            check=True,
        )
        report = json.loads(run.stdout)
        assert report["parent_peak"] > bar, "the parent never crossed the bar"
        if sys.platform.startswith("linux"):
            # The kernel fact the Linux reader exists for: seen, or the pin is hollow.
            assert report["inherited"] > bar, "ru_maxrss did not inherit the parent's peak"
        assert 0 < report["own_peak"] < bar, report

    def test_proc_rss_tree_mb_for_pid_windows_only(self):
        # Windows-only: the lineage-validated tree walk. On POSIX it returns None
        # (callers keep their /proc or ps route), and it must never raise.
        result = pc.proc_rss_tree_mb_for_pid(os.getpid())
        if not pc.IS_WINDOWS:
            assert result is None
            return
        # This is a real-boundary smoke test only. Comparing it with a second RSS
        # sample is scheduler-dependent: Windows may trim this process's working
        # set between the tree and single-process reads. Fixed-value tests in
        # TestProcRssTree pin the root-plus-descendants sum deterministically.
        assert result is not None and result > 0

    def test_proc_rss_tree_mb_for_pid_rejects_reserved_pid(self):
        # A reserved/non-int pid must not anchor a tree walk (recycled-root risk).
        assert pc.proc_rss_tree_mb_for_pid(1) is None
        assert pc.proc_rss_tree_mb_for_pid(0) is None

    def test_proc_cpu_seconds_nonnegative(self):
        assert pc.proc_cpu_seconds() >= 0.0

    def test_proc_cpu_seconds_is_positive_for_a_running_process(self):
        # A running interpreter has always consumed some CPU. This must be > 0
        # on every supported platform: on Windows GetCurrentProcess's handle was
        # truncated without argtypes, so GetProcessTimes failed and this read 0.0.
        assert pc.proc_cpu_seconds() > 0.0

    def test_proc_cpu_nanos_for_pid_reads_a_running_process(self):
        # Linux /proc, macOS libproc, Windows GetProcessTimes: a live interpreter
        # has consumed CPU on all three. None is reserved for a platform with no
        # per-pid counter at all, where the caller keeps its prior behavior.
        ns = pc.proc_cpu_nanos_for_pid(os.getpid())
        if ns is None:
            pytest.skip("no per-pid CPU counter on this platform")
        assert ns > 0

    def test_proc_cpu_nanos_for_pid_refuses_a_reserved_pid(self):
        assert pc.proc_cpu_nanos_for_pid(0) is None
        assert pc.proc_cpu_nanos_for_pid(-1) is None

    def test_raise_nofile_soft_limit_is_safe(self):
        # No-op on Windows; best-effort raise on POSIX. Must never raise.
        pc.raise_nofile_soft_limit(4096)


class TestResourceShimFailures:
    def test_proc_rss_bytes_returns_zero_when_every_source_fails(self, monkeypatch):
        # getrusage is not the primary source for proc_rss_bytes -- it is
        # the labelled last-resort peak -- so reaching 0 needs BOTH the
        # current-RSS reader and the fallback to fail. Asserting only the
        # getrusage failure would pass on a platform whose primary reader was
        # silently removed. On Linux the fallback peak is VmHWM, not getrusage,
        # so its status file has to be unreadable too.
        if not pc.IS_POSIX:
            pytest.skip("POSIX resource.getrusage branch")

        def boom(*args, **kwargs):
            raise OSError("getrusage failed")

        monkeypatch.setattr(pc.resource, "getrusage", boom)
        monkeypatch.setattr(pc, "_LINUX_STATUS_PATH", Path("/nonexistent/status"))
        monkeypatch.setattr(pc, "_linux_current_rss_bytes", lambda: None)
        monkeypatch.setattr(pc, "_macos_current_rss_bytes", lambda: None)
        assert pc.proc_rss_bytes() == 0

    def test_proc_peak_rss_bytes_returns_zero_when_its_source_fails(self, monkeypatch):
        # The peak reading has ONE source per POSIX platform: VmHWM on Linux,
        # getrusage elsewhere. That source failing is a plain 0, and on Linux
        # the failure must not fall through to getrusage, whose ru_maxrss is
        # the parent's inherited peak (test_linux_peak_is_its_own_vmhwm).
        if not pc.IS_POSIX:
            pytest.skip("POSIX peak-RSS branch")

        def boom(*args, **kwargs):
            raise OSError("getrusage failed")

        monkeypatch.setattr(pc.resource, "getrusage", boom)
        monkeypatch.setattr(pc, "_LINUX_STATUS_PATH", Path("/nonexistent/status"))
        assert pc.proc_peak_rss_bytes() == 0

    def test_proc_cpu_seconds_returns_zero_on_getrusage_failure(self, monkeypatch):
        # The failure branch: getrusage raising OSError must yield 0.0, not raise.
        if not pc.IS_POSIX:
            pytest.skip("POSIX resource.getrusage branch")

        def boom(*args, **kwargs):
            raise OSError("getrusage failed")

        monkeypatch.setattr(pc.resource, "getrusage", boom)
        assert pc.proc_cpu_seconds() == 0.0

    def test_raise_nofile_soft_limit_executes_setrlimit(self):
        # Exercise the POSIX getrlimit/setrlimit branch with a real limit nudge,
        # then restore the original limit so no other test is affected. Lower the
        # soft limit first (never the hard limit) so the subsequent shim call
        # takes the `soft < target` setrlimit path; restore in finally.
        if not pc.IS_POSIX:
            pytest.skip("POSIX RLIMIT_NOFILE branch")
        soft, hard = pc.resource.getrlimit(pc.resource.RLIMIT_NOFILE)
        lowered = max(64, (soft if soft != pc.resource.RLIM_INFINITY else hard) // 2)
        try:
            pc.resource.setrlimit(pc.resource.RLIMIT_NOFILE, (lowered, hard))
            # target above the lowered soft limit -> setrlimit branch executes.
            pc.raise_nofile_soft_limit(lowered + 1)
            new_soft = pc.resource.getrlimit(pc.resource.RLIMIT_NOFILE)[0]
            assert new_soft >= lowered + 1
        finally:
            pc.resource.setrlimit(pc.resource.RLIMIT_NOFILE, (soft, hard))

    def test_raise_nofile_soft_limit_swallows_setrlimit_error(self, monkeypatch, caplog):
        # The except branch: if setrlimit raises (e.g. EPERM raising the soft
        # limit on a locked-down host), the shim logs at debug and never raises.
        if not pc.IS_POSIX:
            pytest.skip("POSIX RLIMIT_NOFILE branch")

        def boom(*args, **kwargs):
            raise OSError("setrlimit denied")

        # getrlimit reports a soft below the target so the setrlimit call is
        # attempted (and then fails), exercising the try-body + except.
        monkeypatch.setattr(pc.resource, "getrlimit", lambda which: (100, 1_000_000))
        monkeypatch.setattr(pc.resource, "setrlimit", boom)
        with caplog.at_level(logging.DEBUG, logger=pc.logger.name):
            pc.raise_nofile_soft_limit(500)  # must NOT raise out
        notes = [r.getMessage() for r in caplog.records if r.name == pc.logger.name]
        assert notes == ["Could not raise RLIMIT_NOFILE"]


class TestProcSubtreePss:
    """The PSS option of the subtree walker, on a synthetic tree and synthetic
    ``smaps_rollup`` files, so it runs the same on every host."""

    @staticmethod
    def _host(monkeypatch, files: dict[int, str], tree: dict[int, list[int]]) -> None:
        import builtins
        import io

        monkeypatch.setattr(pc, "IS_LINUX", True)
        monkeypatch.setattr(pc, "_proc_children", lambda pid: tree.get(pid, []))
        real_open = builtins.open
        rollups = {f"/proc/{pid}/smaps_rollup": body for pid, body in files.items()}

        def _open(path, *args, **kwargs):
            if path in rollups:
                return io.StringIO(rollups[path])
            if str(path).endswith("/smaps_rollup"):
                raise FileNotFoundError(path)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", _open)

    def test_the_subtree_pss_is_summed_from_one_walk(self, monkeypatch) -> None:
        self._host(
            monkeypatch,
            {10: "Rss: 900 kB\nPss: 100 kB\n", 11: "Pss: 20 kB\n", 12: "Pss: 3 kB\n"},
            {10: [11, 12]},
        )
        sample = pc.proc_subtree_sample(10, rss=False, jiffies=False, pss=True)
        assert sample.pss_kb == 123

    def test_an_unreadable_child_adds_nothing(self, monkeypatch) -> None:
        self._host(monkeypatch, {10: "Pss: 100 kB\n", 12: "garbage\n"}, {10: [11, 12]})
        assert pc.proc_subtree_sample(10, rss=False, jiffies=False, pss=True).pss_kb == 100

    def test_an_unreadable_root_or_a_non_linux_host_reads_as_unmeasured(self, monkeypatch) -> None:
        self._host(monkeypatch, {11: "Pss: 20 kB\n"}, {10: [11]})
        assert pc.proc_subtree_sample(10, rss=False, jiffies=False, pss=True).pss_kb == -1
        assert pc.proc_subtree_sample(None, pss=True).pss_kb == -1
        monkeypatch.setattr(pc, "IS_LINUX", False)
        assert pc.proc_subtree_sample(11, rss=False, jiffies=False, pss=True).pss_kb == -1

    def test_pss_is_read_only_when_asked(self, monkeypatch) -> None:
        self._host(monkeypatch, {10: "Pss: 100 kB\n"}, {})
        assert pc.proc_subtree_sample(10, rss=False, jiffies=False).pss_kb == -1


# ── memory_pressure_level: the macOS kernel memory-pressure sysctl ───────────
#
# Captured at import: test/conftest.py pins ``memory_pressure_level`` to None for
# every test so no case reads a macOS runner's live level, and these cases test
# the reader itself.
_REAL_MEMORY_PRESSURE_LEVEL = pc.memory_pressure_level

_SYSCTLBYNAME = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_size_t),
    ctypes.c_void_p,
    ctypes.c_size_t,
)


class _PressureLibc:
    """A libc handle whose ``sysctlbyname`` answers one pressure level.

    A real ctypes function pointer, so the reader's call through ctypes is
    exercised and not just the Python around it. The production prototype and
    the real sysctl are covered by the macOS-only test below.
    """

    def __init__(self, level: int, *, result: int = 0, size: int = 4) -> None:
        self.calls: list[tuple[bytes, object, int]] = []

        def _impl(name, oldp, oldlenp, newp, newlen):  # type: ignore[no-untyped-def]
            self.calls.append((name, newp, newlen))
            ctypes.cast(oldp, ctypes.POINTER(ctypes.c_uint32))[0] = level
            oldlenp[0] = size
            return result

        # Held on the instance: ctypes would otherwise call a freed callback.
        self.sysctlbyname = _SYSCTLBYNAME(_impl)


class TestMemoryPressureLevel:
    @pytest.fixture
    def on_macos(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(pc, "memory_pressure_level", _REAL_MEMORY_PRESSURE_LEVEL)
        monkeypatch.setattr(pc, "IS_MACOS", True)

        def _install(handle: object) -> None:
            monkeypatch.setattr(pc, "_darwin_sysctl_handle", lambda: handle)

        return _install

    @pytest.mark.parametrize(
        "level", [pc.MEMORY_PRESSURE_NORMAL, pc.MEMORY_PRESSURE_WARN, pc.MEMORY_PRESSURE_CRITICAL]
    )
    def test_reads_each_kernel_level(self, on_macos, level: int) -> None:
        libc = _PressureLibc(level)
        on_macos(libc)
        assert pc.memory_pressure_level() == level
        # A read, never a write, of the documented sysctl name.
        assert libc.calls == [(b"kern.memorystatus_vm_pressure_level", None, 0)]

    def test_none_off_macos_without_touching_libc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pc, "memory_pressure_level", _REAL_MEMORY_PRESSURE_LEVEL)
        monkeypatch.setattr(pc, "IS_MACOS", False)

        def _refuse() -> object:
            raise AssertionError("must not load libc off macOS")

        monkeypatch.setattr(pc, "_darwin_sysctl_handle", _refuse)
        assert pc.memory_pressure_level() is None

    @pytest.mark.parametrize(
        "handle",
        [
            pytest.param(None, id="no-libc"),
            pytest.param(_PressureLibc(2, result=-1), id="sysctl-fails"),
            pytest.param(_PressureLibc(2, size=8), id="wrong-size"),
            pytest.param(_PressureLibc(0), id="zero-is-not-a-level"),
            pytest.param(_PressureLibc(3), id="unknown-value"),
        ],
    )
    def test_any_failure_reads_as_unknown(self, on_macos, handle: object) -> None:
        on_macos(handle)
        assert pc.memory_pressure_level() is None

    def test_names(self) -> None:
        assert pc.memory_pressure_name(pc.MEMORY_PRESSURE_WARN) == "WARN"
        assert pc.memory_pressure_name(pc.MEMORY_PRESSURE_CRITICAL) == "CRITICAL"
        assert pc.memory_pressure_name(None) == ""
        assert pc.memory_pressure_name(3) == ""

    @pytest.mark.skipif(sys.platform != "darwin", reason="reads the real macOS sysctl")
    def test_the_real_sysctl_answers_a_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The production handle (built from the real libc) and the real
        ``kern.memorystatus_vm_pressure_level``: a wrong name or size would read
        as None here, which in production only shows as the hold quietly off."""
        monkeypatch.setattr(pc, "memory_pressure_level", _REAL_MEMORY_PRESSURE_LEVEL)
        assert pc.memory_pressure_level() in (
            pc.MEMORY_PRESSURE_NORMAL,
            pc.MEMORY_PRESSURE_WARN,
            pc.MEMORY_PRESSURE_CRITICAL,
        )

    def test_a_concurrent_first_call_waits_for_the_one_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The handle is built once, under a lock, and published with both
        prototypes declared: a second caller arriving mid-build must get the same
        handle, never None (which the pressure reader would take for "unknown")."""
        building = threading.Event()
        release = threading.Event()
        built: list[object] = []

        class _Libc:
            def __init__(self) -> None:
                self.sysctl = types.SimpleNamespace()
                self.sysctlbyname = types.SimpleNamespace()

        def _cdll(_path: str) -> _Libc:
            building.set()
            assert release.wait(5.0), "the first build was never released"
            built.append(object())
            return _Libc()

        monkeypatch.setattr(pc.ctypes.util, "find_library", lambda _name: "libc.fake")
        monkeypatch.setattr(pc.ctypes, "CDLL", _cdll)
        answers: list[object] = []
        first = threading.Thread(target=lambda: answers.append(pc._darwin_sysctl_handle()))
        first.start()
        assert building.wait(5.0), "the first call never started building"
        second = threading.Thread(target=lambda: answers.append(pc._darwin_sysctl_handle()))
        second.start()
        release.set()
        first.join(5.0)
        second.join(5.0)
        assert len(answers) == 2 and answers[0] is answers[1] is not None
        assert len(built) == 1
        assert answers[0].sysctlbyname.argtypes is not None  # type: ignore[attr-defined]
