"""On macOS the runtime memory ceilings read the footprint, not ``ps`` RSS.

The background runtime's 500 MiB ceiling (``AcpRuntime._is_stale``) judged the
macOS tree by ``ps -Ao pid=,ppid=,rss=``. ``ps`` RSS counts only pages resident
right now; macOS compresses and swaps idle pages aggressively, and those pages
are exactly what an idle runtime that has grown is made of. jetsam and Activity
Monitor's "Memory" column read ``phys_footprint`` instead, which includes them.
Measured on an operator Mac: a gateway at 124 MB ``ps`` RSS against 1983 MB
footprint (16x), and two idle runtimes holding 462 MB of footprint for hours
while the ceiling never fired.

These pin the footprint reader (``proc_pid_rusage(RUSAGE_INFO_V2)``, no
subprocess), that the macOS tree sum uses it per pid with ``ps`` RSS as the
per-pid fallback, and that the ceiling therefore fires on a tree whose RSS is
small and whose footprint is not.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew.acp import runtime as rt

# This file replaces ctypes.CDLL, find_library or a cached libc handle for a test,
# so the shared macOS handle caches are put back after each one (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

# --------------------------------------------------------------------------- #
# The reader
# --------------------------------------------------------------------------- #


def _rusage_lib(footprint: int, rc: int = 0):
    class _FakeLib:
        calls: list[tuple[int, int]] = []

        @classmethod
        def proc_pid_rusage(cls, pid, flavor, buf):
            cls.calls.append((pid, flavor))
            if rc == 0:
                raw = bytearray(pc._DARWIN_RUSAGE_INFO_V2_SIZE)
                off = pc._DARWIN_RI_PHYS_FOOTPRINT_OFFSET
                raw[off : off + 8] = footprint.to_bytes(8, "little")
                # resident_size, the field ps reports, sits just before it and
                # must NOT be what the reader returns.
                raw[off - 8 : off] = (1 << 20).to_bytes(8, "little")
                buf.raw = bytes(raw)
            return rc

    return _FakeLib


class TestFootprintReader:
    def test_reads_phys_footprint_from_rusage_info_v2(self, monkeypatch):
        lib = _rusage_lib(2 * 1024**3)
        monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: lib)

        assert pc._darwin_process_phys_footprint_bytes(4242) == 2 * 1024**3
        assert lib.calls == [(4242, pc._DARWIN_RUSAGE_INFO_V2)]

    def test_a_failed_call_is_none(self, monkeypatch):
        monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: _rusage_lib(5, rc=-1))
        assert pc._darwin_process_phys_footprint_bytes(4242) is None

    def test_no_libproc_is_none(self, monkeypatch):
        monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: None)
        assert pc._darwin_process_phys_footprint_bytes(4242) is None

    def test_a_libproc_without_rusage_still_serves_the_other_probes(self, monkeypatch):
        """Only the footprint goes dark; the shared handle must still load."""
        import types

        lib = types.SimpleNamespace(proc_pidinfo=lambda *a: 0)
        monkeypatch.setattr(pc.ctypes.util, "find_library", lambda _n: "libproc.dylib")
        monkeypatch.setattr(pc.ctypes, "CDLL", lambda _p: lib)

        assert pc._darwin_libproc_handle() is lib
        assert pc._darwin_process_phys_footprint_bytes(4242) is None

    def test_the_public_reader_is_macos_only(self, monkeypatch):
        monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: _rusage_lib(7))
        monkeypatch.setattr(pc, "IS_MACOS", False)
        assert pc.proc_phys_footprint_bytes_for_pid(4242) is None
        monkeypatch.setattr(pc, "IS_MACOS", True)
        assert pc.proc_phys_footprint_bytes_for_pid(4242) == 7

    def test_the_constants_restate_the_headers_field_order(self):
        """Pins the field ORDER as transcribed from ``resource.h``, nothing more.

        This repeats the arithmetic that defines the constants, so it can only
        catch an edit that changes one side of the transcription — it cannot
        tell whether the transcription matches the real struct. The ABI itself
        is checked against a live ``libproc`` by ``TestTheRealDarwinAbi``.
        """
        # 16-byte uuid, then 18 uint64 fields; ri_phys_footprint is the 8th.
        assert pc._DARWIN_RUSAGE_INFO_V2_SIZE == 16 + 18 * 8
        assert pc._DARWIN_RI_PHYS_FOOTPRINT_OFFSET == 16 + 7 * 8


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="exercises the real libproc ABI, which only a Mac has",
)
class TestTheRealDarwinAbi:
    """The live canary: the reader against the real ``rusage_info_v2``.

    Every other test here hands the reader a buffer built from the SAME
    offset constants it reads with, so none of them can notice the constants
    disagreeing with the kernel's struct. These call the real
    ``proc_pid_rusage`` on our own pid — no monkeypatching — so a wrong size
    or offset shows up as a nonsensical reading on the macOS CI lane.
    """

    def test_our_own_footprint_is_a_positive_int(self):
        footprint = pc.proc_phys_footprint_bytes_for_pid(os.getpid())
        assert isinstance(footprint, int)
        assert footprint > 0

    def test_dirtying_256_mib_grows_the_footprint_by_at_least_192_mib(self):
        """Distinguishes ``ri_phys_footprint`` from its neighbouring fields.

        A positive read alone could come from any counter in the struct. Only
        the footprint grows by hundreds of MiB when we dirty 256 MiB of
        anonymous memory: the wakeup counters and pageins next to it move by
        at most a handful, and a mis-sliced offset straddling two fields reads
        garbage or zero. The margin is 192 MiB, not 256: freshly dirtied pages
        can start being compressed, but the compressor still charges them to
        the footprint (compressed pages are what the footprint exists to
        count), so the slack only needs to absorb unrelated interpreter noise.

        Deliberately NOT asserted: footprint >= RSS. The footprint excludes
        clean shared pages that RSS includes, so it can sit below RSS.
        """
        pid = os.getpid()
        before = pc.proc_phys_footprint_bytes_for_pid(pid)
        assert before is not None

        blob = bytearray(256 * 1024 * 1024)
        # A large zeroed allocation is zero-fill-on-demand: untouched pages
        # cost nothing. Write one byte per 4 KiB page so every page is dirty
        # and therefore charged to the footprint.
        for i in range(0, len(blob), 4096):
            blob[i] = 1

        after = pc.proc_phys_footprint_bytes_for_pid(pid)
        grew = None if after is None else after - before
        # Only now may the allocation be released: freeing it before the
        # second read would hand the pages back and erase the signal.
        del blob

        assert grew is not None
        assert grew >= 192 * 1024 * 1024

    def test_a_pid_that_does_not_exist_reads_none(self):
        # Far above macOS's pid range, so proc_pid_rusage fails and the
        # reader must answer None rather than the buffer's stale zeros.
        assert pc.proc_phys_footprint_bytes_for_pid(2_000_000_000) is None


# --------------------------------------------------------------------------- #
# The macOS tree sum
# --------------------------------------------------------------------------- #

#: pid ppid rss(KiB): runtime 100 (120 MiB resident) with child 101 (8 MiB).
_PS_OUTPUT = b"\n".join(
    [
        b"  1     0   1024",
        b"100     1 122880",
        b"101   100   8192",
    ]
)

_MIB = 1024 * 1024


@pytest.fixture
def _darwin(monkeypatch):
    monkeypatch.setattr(rt.sys, "platform", "darwin")
    monkeypatch.setattr(rt.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(rt.platform_compat, "trusted_system_bin", lambda name: f"/bin/{name}")
    monkeypatch.setattr(rt.subprocess, "check_output", lambda *a, **kw: _PS_OUTPUT)
    rt._reset_ps_table_cache()
    yield
    rt._reset_ps_table_cache()


@pytest.mark.usefixtures("_darwin")
class TestTheMacosTreeReadsTheFootprint:
    def test_the_tree_sums_footprint_not_resident_size(self, monkeypatch):
        footprints = {100: 2000 * _MIB, 101: 48 * _MIB}
        monkeypatch.setattr(rt.platform_compat, "proc_phys_footprint_bytes_for_pid", footprints.get)

        assert rt._get_rss_tree_mb(100) == pytest.approx(2048.0)

    def test_an_unreadable_footprint_falls_back_to_that_pids_rss(self, monkeypatch):
        footprints = {100: 2000 * _MIB}  # 101 answers None -> its ps RSS (8 MiB)
        monkeypatch.setattr(rt.platform_compat, "proc_phys_footprint_bytes_for_pid", footprints.get)

        assert rt._get_rss_tree_mb(100) == pytest.approx(2008.0)

    def test_no_footprint_at_all_is_the_old_rss_sum(self, monkeypatch):
        monkeypatch.setattr(
            rt.platform_compat, "proc_phys_footprint_bytes_for_pid", lambda pid: None
        )

        assert rt._get_rss_tree_mb(100) == pytest.approx(128.0)

    def test_the_depth_bound_still_applies(self, monkeypatch):
        footprints = {100: 2000 * _MIB, 101: 48 * _MIB}
        monkeypatch.setattr(rt.platform_compat, "proc_phys_footprint_bytes_for_pid", footprints.get)

        assert rt._get_rss_tree_mb(100, max_depth=0) == pytest.approx(2000.0)

    def test_a_single_pid_read_prefers_the_footprint(self, monkeypatch):
        monkeypatch.setattr(
            rt.platform_compat, "proc_phys_footprint_bytes_for_pid", lambda pid: 900 * _MIB
        )

        assert rt._get_rss_mb(100) == pytest.approx(900.0)


@pytest.mark.usefixtures("_darwin")
class TestTheCeilingFiresOnFootprint:
    """The reported state: a small RSS, a large footprint, a ceiling that never fired."""

    def _old_runtime(self) -> rt.AcpRuntime:
        runtime = rt.AcpRuntime(work_dir="/tmp")
        runtime._pid = 100
        runtime._spawn_monotonic = time.monotonic() - 3600
        return runtime

    def test_a_large_footprint_behind_a_small_rss_is_stale(self, monkeypatch):
        footprints = {100: 1983 * _MIB, 101: 8 * _MIB}
        monkeypatch.setattr(rt.platform_compat, "proc_phys_footprint_bytes_for_pid", footprints.get)

        assert asyncio.run(self._old_runtime()._is_stale()) == "rss"

    def test_a_small_footprint_is_not(self, monkeypatch):
        footprints = {100: 120 * _MIB, 101: 8 * _MIB}
        monkeypatch.setattr(rt.platform_compat, "proc_phys_footprint_bytes_for_pid", footprints.get)

        assert asyncio.run(self._old_runtime()._is_stale()) is None
