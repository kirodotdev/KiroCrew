"""The session RSS ceiling measures on macOS instead of reading every tree as 0.

macOS has no ``/proc``. ``get_session_rss_mb`` and the sweep's
``_rss_mb_from_tree`` sum each process's
``phys_footprint`` over libproc's live child lists. These tests replace only the
libproc handle (the ctypes seam) with a fake process table, so the real reader,
child walk and MiB conversion all run; the expectation is derived from the same
footprint bytes the fake hands out. A host that still cannot measure warns once.
"""

from __future__ import annotations

import logging
import os
import struct
import sys
import types

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew import session_pid

_MIB = 1024 * 1024

# pid -> footprint bytes, pid -> direct children. 300's subtree is the one the
# exclusion test prunes; 999 has no footprint (unreadable, adds nothing).
_FOOTPRINT = {100: 100 * _MIB + 7, 200: 50 * _MIB, 300: 300 * _MIB, 400: 25 * _MIB, 500: 9 * _MIB}
_CHILDREN = {100: [200, 300, 999], 200: [400], 300: [500]}


def _fake_libproc() -> types.SimpleNamespace:
    def proc_pid_rusage(pid, flavor, buf):
        if pid not in _FOOTPRINT or flavor != pc._DARWIN_RUSAGE_INFO_V2:
            return -1
        struct.pack_into("<Q", buf, pc._DARWIN_RI_PHYS_FOOTPRINT_OFFSET, _FOOTPRINT[pid])
        return 0

    def proc_listchildpids(ppid, buf, size):
        kids = _CHILDREN.get(ppid, [])
        struct.pack_into(f"<{len(kids)}i", buf, 0, *kids)
        return len(kids)

    def proc_pidpath(pid, buf, size):
        return 0

    def proc_pidinfo(*_args):
        return 0

    return types.SimpleNamespace(
        proc_pid_rusage=proc_pid_rusage,
        proc_listchildpids=proc_listchildpids,
        proc_pidpath=proc_pidpath,
        proc_pidinfo=proc_pidinfo,
    )


def _expected_mb(pids) -> int:
    return int(sum(_FOOTPRINT[p] for p in pids) / _MIB)


@pytest.fixture
def mac(monkeypatch: pytest.MonkeyPatch) -> None:
    lib = _fake_libproc()
    monkeypatch.setattr(session_pid.sys, "platform", "darwin")
    monkeypatch.setattr(pc, "IS_MACOS", True)
    monkeypatch.setattr(pc, "IS_WINDOWS", False)
    monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: lib)
    monkeypatch.setattr(pc, "_darwin_libproc_tree_bound", False)
    monkeypatch.setattr(session_pid, "_rss_ceiling_inert_warned", False, raising=False)


@pytest.mark.usefixtures("mac")
class TestDarwinTreeMeasurement:
    def test_single_tree_sums_root_and_every_descendant(self) -> None:
        assert session_pid.get_session_rss_mb(100) == _expected_mb([100, 200, 300, 400, 500])

    def test_sweep_route_measures_without_a_proc_map(self) -> None:
        """The recycle sweep calls ``_rss_mb_from_tree`` with the (empty) map."""
        child_map = session_pid._build_child_map()
        got = session_pid._rss_mb_from_tree(100, child_map)
        assert got == _expected_mb([100, 200, 300, 400, 500])
        assert got > 0

    def test_exclude_pids_prunes_the_whole_subtree(self) -> None:
        got = session_pid.get_session_rss_mb(100, exclude_pids={300})
        assert got == _expected_mb([100, 200, 400])

    def test_an_excluded_root_measures_nothing(self) -> None:
        assert session_pid.get_session_rss_mb(100, exclude_pids={100}) == 0

    def test_an_unreadable_root_is_zero_not_a_guess(self) -> None:
        assert session_pid.get_session_rss_mb(999) == 0

    def test_measuring_logs_no_inert_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=session_pid.logger.name):
            session_pid.get_session_rss_mb(100)
        assert "cannot measure" not in caplog.text


class TestInertCeilingWarnsOnce:
    @pytest.fixture(autouse=True)
    def _reset(self, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
        _floor_monkeypatch.setattr(pc, "IS_WINDOWS", False)
        _floor_monkeypatch.setattr(session_pid, "_rss_ceiling_inert_warned", False, raising=False)

    def _warnings(self, caplog: pytest.LogCaptureFixture) -> list[str]:
        return [r.getMessage() for r in caplog.records if "cannot measure" in r.getMessage()]

    def test_a_mac_without_libproc_warns_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(session_pid.sys, "platform", "darwin")
        monkeypatch.setattr(pc, "IS_MACOS", True)
        monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: None)
        with caplog.at_level(logging.WARNING, logger=session_pid.logger.name):
            assert session_pid.get_session_rss_mb(100) == 0
            assert session_pid._rss_mb_from_tree(100, {}) == 0
        assert len(self._warnings(caplog)) == 1
        assert "libproc unavailable" in self._warnings(caplog)[0]

    def test_an_unsupported_platform_warns_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(session_pid.sys, "platform", "freebsd14")
        with caplog.at_level(logging.WARNING, logger=session_pid.logger.name):
            assert session_pid.get_session_rss_mb(100) == 0
            assert session_pid.get_session_rss_mb(100) == 0
        assert len(self._warnings(caplog)) == 1
        assert "freebsd14" in self._warnings(caplog)[0]


@pytest.mark.skipif(sys.platform != "darwin", reason="reads the real libproc, which only a Mac has")
def test_real_mac_measures_our_own_process() -> None:
    """Live canary: our own pytest process has a non-zero footprint."""
    assert session_pid.get_session_rss_mb(os.getpid()) > 0
