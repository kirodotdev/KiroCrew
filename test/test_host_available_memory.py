"""Tests for ``platform_compat.host_available_mib`` on all three platforms.

This reading is what stops a full test run from swapping the developer's machine:
the xdist worker budget divides it by a per-worker reservation to decide how many
workers to spawn. Its failure mode is silent and one-directional -- ``0`` means
"unknown", an unknown reading is SKIPPED rather than treated as zero memory, and
the run then proceeds at the unbounded ceiling with nothing in the log. So a
platform whose branch is missing does not report a conservative number; it reports
no number, and the budget it feeds becomes an identity function.

That is exactly what happened while the reading was ``/proc/meminfo`` only, and
no test could catch it because one asserted the zero. The class that matters most
here is therefore :class:`TestEveryPlatformProducesAReading`, which fails if any
platform's branch stops answering.

macOS and Windows cannot execute on the Linux fleet, so both are driven through
their own C entry point: a fake ``libSystem`` injected over ``ctypes.CDLL`` for
Mach, and a stubbed ``system_memory`` for ``GlobalMemoryStatusEx``.
"""

from __future__ import annotations

import ctypes

import pytest

from kiro_crew import platform_compat as pc

_SENTINEL_PORT = 0x1111
_SENTINEL_TASK = 0x2222

#: 4 KiB pages keep the arithmetic in the tests obvious: 256 pages == 1 MiB.
_PAGE = 4096
_PAGES_PER_MIB = 1024 * 1024 // _PAGE


class _FakeFn:
    """A ctypes function-pointer stand-in that tolerates ``.restype`` assignment."""

    def __init__(self, fn):
        self._fn = fn
        self.restype = None
        self.argtypes = None

    def __call__(self, *args):
        return self._fn(*args)


class _FakeLibSystem:
    """Enough of ``libSystem`` to drive ``macos_vm_statistics`` on Linux.

    ``fields`` are written into the caller's struct through the ``byref`` target.
    ``filled`` is what the kernel reports having written, which is how the reading
    decides whether the later-revision fields hold anything.
    """

    def __init__(self, *, fields: dict, kern_return: int = 0, filled: int | None = None):
        self.fields = fields
        self.kern_return = kern_return
        self.filled = filled
        self.dealloc_calls: list[tuple[int, int]] = []
        self.mach_host_self = _FakeFn(lambda: _SENTINEL_PORT)
        self.mach_task_self = _FakeFn(lambda: _SENTINEL_TASK)
        self.mach_port_deallocate = _FakeFn(self._deallocate)
        self.host_statistics64 = _FakeFn(self._host_statistics64)

    def _deallocate(self, task, port):
        self.dealloc_calls.append((task, port))
        return 0

    def _host_statistics64(self, host_port, flavor, stats_ref, count_ref):
        obj = getattr(stats_ref, "_obj", None)
        if obj is not None:
            for name, value in self.fields.items():
                setattr(obj, name, value)
        if self.filled is not None:
            count_obj = getattr(count_ref, "_obj", None)
            if count_obj is not None:
                count_obj.value = self.filled
        return self.kern_return


@pytest.fixture
def as_macos(monkeypatch: pytest.MonkeyPatch):
    """Present as macOS and inject a fake ``libSystem``.

    ``page_size`` defaults to 4 KiB because that keeps the arithmetic in most tests
    obvious, but it is a parameter: Apple silicon reports 16 KiB, and this reading
    multiplies a PAGE COUNT by it.
    """

    def _install(page_size: int = _PAGE, **kwargs) -> _FakeLibSystem:
        fake = _FakeLibSystem(**kwargs)
        monkeypatch.setattr(pc, "IS_LINUX", False)
        monkeypatch.setattr(pc, "IS_MACOS", True)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: fake)
        # raising=False: os.sysconf does not exist on Windows, and this fixture has
        # to install a page size there too -- the macOS branch under test reads it,
        # and a bare setattr would fail the whole class on the Windows shard.
        monkeypatch.setattr(pc.os, "sysconf", lambda name: page_size, raising=False)
        return fake

    return _install


class TestEveryPlatformProducesAReading:
    """The regression this file exists for.

    A platform with no branch returns 0, the budget skips an unknown reading, and
    the run is unbounded. So every platform we ship on must answer -- and the
    assertion has to be that the reading is POSITIVE, because 0 is precisely the
    value that fails open.
    """

    def test_linux_reads_meminfo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pc, "IS_LINUX", True)
        monkeypatch.setattr(pc, "IS_MACOS", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        real_open = open

        def _meminfo(path, *args, **kwargs):
            if str(path) == "/proc/meminfo":
                import io

                return io.StringIO("MemTotal: 1000 kB\nMemAvailable:  2097152 kB\n")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _meminfo)

        assert pc.host_available_mib() == 2048

    def test_macos_reads_mach_vm_statistics(self, as_macos) -> None:
        as_macos(fields={"free_count": 512 * _PAGES_PER_MIB})

        assert pc.host_available_mib() == 512

    def test_windows_reads_global_memory_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pc, "IS_LINUX", False)
        monkeypatch.setattr(pc, "IS_MACOS", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "system_memory", lambda: (16 * 1024**3, 3 * 1024**3))

        assert pc.host_available_mib() == 3072

    def test_an_unknown_platform_is_zero_not_a_guess(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail OPEN on a platform we have no reading for.

        The opposite of the rest of this class, and deliberately: a fabricated
        number would clamp a host nobody has measured, and 0 keeps its
        parallelism. The cost of being wrong here is a slow suite, not a wrong one.
        """
        monkeypatch.setattr(pc, "IS_LINUX", False)
        monkeypatch.setattr(pc, "IS_MACOS", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)

        assert pc.host_available_mib() == 0


class TestTheMacosCompositionIsConservative:
    """Which page classes count as available, and why each choice is deliberate."""

    def test_speculative_pages_are_not_added_on_top_of_free(self, as_macos) -> None:
        """``free_count`` already CONTAINS ``speculative_count``.

        Darwin's own ``vm_stat`` prints ``free_count - speculative_count`` as its
        "Pages free" line, which is the proof. Adding speculative again inflates
        the reading -- and it inflates it on a busy machine, where speculative is
        large, which is the one place the budget must not be optimistic.

        This is the only test that stops the sum being "corrected" back to
        ``free + inactive + speculative + purgeable``.
        """
        without = as_macos(fields={"free_count": 400 * _PAGES_PER_MIB})
        baseline = pc.host_available_mib()
        assert without is not None

        with_speculative = as_macos(
            fields={
                "free_count": 400 * _PAGES_PER_MIB,
                "speculative_count": 300 * _PAGES_PER_MIB,
            }
        )
        assert with_speculative is not None

        assert pc.host_available_mib() == baseline == 400

    @pytest.mark.parametrize("page_size", [4096, 16384])
    def test_the_page_size_comes_from_the_kernel_not_a_constant(
        self, as_macos, page_size: int
    ) -> None:
        """Apple silicon reports 16 KiB pages, and this reading multiplies by it.

        A hardcoded 4096 would under-report available memory by 4x on exactly the
        machines this budget exists to protect -- and under-reporting clamps the run
        to one worker, which reads as a hang rather than as a bug. Parametrized over
        both sizes so the same page COUNT must produce four times the MiB.
        """
        pages = 100 * 1024 * 1024 // page_size  # 100 MiB worth, whatever the size
        as_macos(page_size=page_size, fields={"free_count": pages})

        assert pc.host_available_mib() == 100

    def test_purgeable_pages_count_as_available(self, as_macos) -> None:
        """The kernel can drop them outright, with no I/O."""
        as_macos(
            fields={"free_count": 100 * _PAGES_PER_MIB, "purgeable_count": 50 * _PAGES_PER_MIB}
        )

        assert pc.host_available_mib() == 150

    def test_inactive_is_bounded_by_the_file_backed_page_count(self, as_macos) -> None:
        """Inactive mixes clean file pages with DIRTY ANONYMOUS pages.

        Only the file-backed share can be handed over without compressing or
        swapping, and ``HOST_VM_INFO64`` publishes no inactive-AND-file counter --
        so ``min(inactive, external_page_count)`` is the tightest computable bound.
        Without it a browser holding gigabytes of inactive anonymous memory reads
        as free, and the budget grants workers against memory that is not there.
        """
        as_macos(
            fields={
                "free_count": 100 * _PAGES_PER_MIB,
                "inactive_count": 900 * _PAGES_PER_MIB,
                "external_page_count": 200 * _PAGES_PER_MIB,
            },
            filled=pc._EXTERNAL_PAGE_COUNT_ELEMENTS,
        )

        assert pc.host_available_mib() == 300  # 100 free + min(900, 200)

    def test_a_kernel_too_old_for_the_field_keeps_the_looser_bound(self, as_macos) -> None:
        """``external_page_count`` reads 0 on a kernel that predates it.

        ``min(inactive, 0)`` would then be 0, collapsing a HEALTHY Mac to one
        worker for a reason no log would explain. The count the kernel writes back
        is what distinguishes "no file-backed pages" from "field not written".
        """
        as_macos(
            fields={"free_count": 100 * _PAGES_PER_MIB, "inactive_count": 900 * _PAGES_PER_MIB},
            filled=pc._EXTERNAL_PAGE_COUNT_ELEMENTS - 1,
        )

        assert pc.host_available_mib() == 1000  # inactive taken whole

    def test_compressed_pages_are_not_available(self, as_macos) -> None:
        """Compressor pages are occupied; only their contents were shrunk."""
        as_macos(
            fields={
                "free_count": 100 * _PAGES_PER_MIB,
                "compressor_page_count": 800 * _PAGES_PER_MIB,
                "total_uncompressed_pages_in_compressor": 2000 * _PAGES_PER_MIB,
            }
        )

        assert pc.host_available_mib() == 100

    def test_wired_and_active_pages_are_not_available(self, as_macos) -> None:
        as_macos(
            fields={
                "free_count": 100 * _PAGES_PER_MIB,
                "wire_count": 4000 * _PAGES_PER_MIB,
                "active_count": 4000 * _PAGES_PER_MIB,
            }
        )

        assert pc.host_available_mib() == 100


class TestTheMacosReadingFailsSafely:
    def test_a_nonzero_kern_return_is_unknown(self, as_macos) -> None:
        as_macos(fields={"free_count": 512 * _PAGES_PER_MIB}, kern_return=1)

        assert pc.host_available_mib() == 0

    def test_an_all_zero_struct_is_unknown_not_zero_memory(self, as_macos) -> None:
        """A successful read of an impossible host is a failed read.

        No running Mac has zero free, purgeable and file-backed pages at once, so
        the plausible cause is a struct that was never filled. Reporting 0 =
        unknown keeps the run going; reporting a real 0 would clamp it to one
        worker forever.
        """
        as_macos(fields={})

        assert pc.host_available_mib() == 0

    def test_a_missing_libsystem_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pc, "IS_LINUX", False)
        monkeypatch.setattr(pc, "IS_MACOS", True)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)

        def _no_libsystem(*a, **k):
            raise OSError("libSystem not here")

        monkeypatch.setattr(ctypes, "CDLL", _no_libsystem)

        assert pc.host_available_mib() == 0

    def test_an_unreadable_page_size_is_unknown(self, as_macos) -> None:
        """A page count without a page size is not a byte count."""
        as_macos(fields={"free_count": 512 * _PAGES_PER_MIB})

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pc.os, "sysconf", lambda name: 0, raising=False)
            assert pc.host_available_mib() == 0

    @pytest.mark.parametrize("kern_return", [0, 1])
    def test_the_mach_send_right_is_released_on_every_path(
        self, as_macos, kern_return: int
    ) -> None:
        """``mach_host_self`` returns a send right that leaks if not deallocated.

        Parametrized over success AND failure because the failure path is the one
        that gets forgotten, and this probe runs repeatedly.
        """
        fake = as_macos(fields={"free_count": 512 * _PAGES_PER_MIB}, kern_return=kern_return)

        pc.host_available_mib()

        assert fake.dealloc_calls == [(_SENTINEL_TASK, _SENTINEL_PORT)]


class TestTheWindowsReadingFailsSafely:
    def test_an_unavailable_reading_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pc, "IS_LINUX", False)
        monkeypatch.setattr(pc, "IS_MACOS", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "system_memory", lambda: None)

        assert pc.host_available_mib() == 0

    def test_a_sub_mib_reading_truncates_to_zero_and_is_treated_as_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Documents the one place the unit choice still bites.

        ``GlobalMemoryStatusEx`` reports bytes, so under 1 MiB available the
        integer division yields 0 = unknown and the bound is skipped. A Windows
        host with under 1 MiB free has already lost, so the direction is
        acceptable -- but it is asserted rather than left to be discovered.
        """
        monkeypatch.setattr(pc, "IS_LINUX", False)
        monkeypatch.setattr(pc, "IS_MACOS", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "system_memory", lambda: (16 * 1024**3, 1000))

        assert pc.host_available_mib() == 0


class TestTheLinuxReadingIsUnchanged:
    """``MemAvailable``, not ``MemFree``, and the difference is large."""

    def test_memfree_is_not_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``MemFree`` omits reclaimable page cache and understates badly.

        Measured on an idle host: ``MemFree`` 43,574 MiB against
        ``MemAvailable`` 74,768 MiB -- a 42% understatement that would halve
        parallelism on a machine with plenty of headroom.
        """
        monkeypatch.setattr(pc, "IS_LINUX", True)
        monkeypatch.setattr(pc, "IS_MACOS", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        real_open = open

        def _meminfo(path, *args, **kwargs):
            if str(path) == "/proc/meminfo":
                import io

                return io.StringIO(
                    "MemTotal:      131072000 kB\n"
                    "MemFree:        1048576 kB\n"
                    "MemAvailable:   4194304 kB\n"
                )
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _meminfo)

        assert pc.host_available_mib() == 4096

    def test_a_missing_meminfo_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pc, "IS_LINUX", True)
        monkeypatch.setattr(pc, "IS_MACOS", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        real_open = open

        def _no_meminfo(path, *args, **kwargs):
            if str(path) == "/proc/meminfo":
                raise FileNotFoundError(path)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _no_meminfo)

        assert pc.host_available_mib() == 0

    def test_a_garbled_meminfo_line_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pc, "IS_LINUX", True)
        monkeypatch.setattr(pc, "IS_MACOS", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        real_open = open

        def _meminfo(path, *args, **kwargs):
            if str(path) == "/proc/meminfo":
                import io

                return io.StringIO("MemAvailable:\n")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _meminfo)

        assert pc.host_available_mib() == 0


class TestTheStructLayoutMatchesTheKernel:
    def test_the_element_count_threshold_is_derived_not_hardcoded(self) -> None:
        """It must track the layout, or adding a field silently breaks the check."""
        expected = (
            pc._VMStatistics64.external_page_count.offset
            + pc._VMStatistics64.external_page_count.size
        ) // ctypes.sizeof(ctypes.c_int)

        assert pc._EXTERNAL_PAGE_COUNT_ELEMENTS == expected

    def test_natural_t_is_32_bit(self) -> None:
        """``natural_t`` is 32-bit on macOS including Apple silicon; a 64-bit
        guess would shift every field after the first and misread all of them."""
        assert ctypes.sizeof(pc._NATURAL_T) == 4

    def test_the_declared_struct_covers_the_fields_the_reading_uses(self) -> None:
        names = {name for name, _type in pc._VMStatistics64._fields_}

        assert {
            "free_count",
            "inactive_count",
            "purgeable_count",
            "speculative_count",
            "external_page_count",
        } <= names
