"""Coverage-raising unit tests for :mod:`kiro_crew.platform_compat`: resource metrics.

RSS and peak RSS, open fds, the RSS of a pid and of its tree, CPU seconds, the system
metrics, ``strftime`` and the nofile soft limit.

Part of the suite ``test_platform_compat_coverage.py`` heads, and written its way: a
platform branch the Linux CI host never reaches is simulated by flipping
``IS_WINDOWS``/``IS_POSIX`` and faking the DLL entry points, every clock the product
consults is replaced, and nothing spawns a process or writes outside ``tmp_path``.
"""

from __future__ import annotations

import ctypes
import datetime
import logging
import os
import types
from typing import Any

import pytest
from platform_compat_coverage_helpers import _LINUX_ONLY, _POSIX_ONLY, _const, _fake_windows, _Fn

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

    def test_linux_fallback_scales_the_peak_from_kib_to_bytes(self, monkeypatch, tmp_path):
        # Unit handling is the trap: the kernel prints VmHWM in kB with nothing
        # in the value to say so. The Linux peak is the process's OWN VmHWM, not
        # ru_maxrss, which execve seeds with the parent's peak.
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.sys, "platform", "linux")
        _fake_resource(
            monkeypatch,
            getrusage=lambda _who: types.SimpleNamespace(ru_maxrss=999_999_999),
        )
        status = tmp_path / "status"
        status.write_text("VmHWM:\t    2048 kB\n", encoding="utf-8")
        monkeypatch.setattr(pc, "_LINUX_STATUS_PATH", status)
        monkeypatch.setattr(pc, "_LINUX_PEAK_RSS_FLOOR", 0)
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

    def test_a_total_posix_failure_is_zero(self, monkeypatch, tmp_path):
        def _boom(_who: Any) -> Any:
            raise OSError("no rusage")

        monkeypatch.setattr(pc, "IS_POSIX", True)
        _fake_resource(monkeypatch, getrusage=_boom)
        monkeypatch.setattr(pc, "_LINUX_STATUS_PATH", tmp_path / "no-proc")
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

    def test_posix_scales_kib_to_bytes_on_linux(self, monkeypatch, tmp_path):
        # Linux reads its own VmHWM (kB); ru_maxrss there is the parent's
        # inherited peak and must not be the source.
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.sys, "platform", "linux")
        _fake_resource(
            monkeypatch,
            getrusage=lambda _who: types.SimpleNamespace(ru_maxrss=999_999_999),
        )
        status = tmp_path / "status"
        status.write_text("VmHWM:\t    2048 kB\n", encoding="utf-8")
        monkeypatch.setattr(pc, "_LINUX_STATUS_PATH", status)
        monkeypatch.setattr(pc, "_LINUX_PEAK_RSS_FLOOR", 0)
        assert pc.proc_peak_rss_bytes() == 2048 * 1024

    def test_posix_reports_bytes_directly_on_macos(self, monkeypatch):
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.sys, "platform", "darwin")
        _fake_resource(
            monkeypatch,
            getrusage=lambda _who: types.SimpleNamespace(ru_maxrss=999),
        )
        assert pc.proc_peak_rss_bytes() == 999

    def test_a_getrusage_failure_is_zero(self, monkeypatch, tmp_path):
        # The single POSIX source failing (VmHWM on Linux, getrusage elsewhere)
        # is 0: neither falls through to the other.
        def _boom(_who: Any) -> Any:
            raise OSError("no rusage")

        monkeypatch.setattr(pc, "IS_POSIX", True)
        _fake_resource(monkeypatch, getrusage=_boom)
        monkeypatch.setattr(pc, "_LINUX_STATUS_PATH", tmp_path / "no-proc")
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


class TestCountOpenFds:
    """``count_open_fds`` is the ONE shared open-fd probe.

    Both the ``kirocrew.process.open_fds`` gauge and gatewayd's
    zombie-diagnostic ``fd_count`` delegate here, so these tests pin the probe
    once: the POSIX steady-state correction, the None contract, and the
    Windows handle-count route.
    """

    def test_posix_count_is_positive_and_excludes_the_probe_fd(self):
        count = pc.count_open_fds()
        if count is None:
            pytest.skip("no fd probe on this platform")
        # stdin/stdout/stderr at minimum; the enumeration fd itself excluded.
        assert count >= 3

    def test_posix_correction_never_goes_negative(self, monkeypatch, tmp_path):
        empty = tmp_path / "fd"
        empty.mkdir()
        monkeypatch.setattr(pc, "_FD_DIRS", (str(empty),))
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        assert pc.count_open_fds() == 0

    def test_second_fd_dir_is_the_fallback(self, monkeypatch, tmp_path):
        fallback = tmp_path / "fd"
        fallback.mkdir()
        for name in ("0", "1", "2", "3"):
            (fallback / name).write_text("")
        monkeypatch.setattr(pc, "_FD_DIRS", (str(tmp_path / "missing"), str(fallback)))
        # Four entries, minus the enumeration fd the listing itself opens.
        assert pc.count_open_fds() == 3

    def test_none_when_no_probe_works_on_posix(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pc, "_FD_DIRS", (str(tmp_path / "missing"),))
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        assert pc.count_open_fds() is None

    def test_windows_reads_the_kernel_handle_count_raw(self, monkeypatch, tmp_path):
        def _get_handle_count(_process: Any, out: Any) -> int:
            out._obj.value = 42
            return 1

        _fake_windows(
            monkeypatch,
            kernel32=types.SimpleNamespace(
                GetCurrentProcess=_const(1),
                GetProcessHandleCount=_Fn(_get_handle_count),
            ),
        )
        monkeypatch.setattr(pc, "_FD_DIRS", (str(tmp_path / "missing"),))
        # Raw: the handle query opens no extra handle, so no -1 correction.
        assert pc.count_open_fds() == 42

    def test_windows_api_failure_is_none(self, monkeypatch, tmp_path):
        _fake_windows(
            monkeypatch,
            kernel32=types.SimpleNamespace(
                GetCurrentProcess=_const(1),
                GetProcessHandleCount=_const(0),
            ),
        )
        monkeypatch.setattr(pc, "_FD_DIRS", (str(tmp_path / "missing"),))
        assert pc.count_open_fds() is None

    def test_windows_loader_failure_is_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.delattr(pc.ctypes, "WinDLL", raising=False)
        monkeypatch.setattr(pc, "_FD_DIRS", (str(tmp_path / "missing"),))
        assert pc.count_open_fds() is None


class TestProcRssForPid:
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
