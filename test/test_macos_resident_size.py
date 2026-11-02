"""macOS per-pid resident size read through ``proc_pidinfo``.

Every libproc call is faked, so these run on any host.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kiro_crew import cli_doctor
from kiro_crew import platform_compat as pc

_MIB = 1024 * 1024


def _taskinfo_lib(resident: int, *, filled: int | None = None, calls: list | None = None):
    """A fake ``libproc`` whose ``proc_pidinfo`` fills a ``proc_taskinfo``.

    ``pti_resident_size`` is written at its pinned offset; ``filled`` overrides
    the returned fill size to simulate a layout mismatch.
    """

    class _FakeLib:
        @staticmethod
        def proc_pidinfo(pid, flavor, arg, buf, size):
            if calls is not None:
                calls.append((pid, flavor, arg, size))
            raw = bytearray(size)
            off = pc._DARWIN_PTI_RESIDENT_SIZE_OFFSET
            raw[off : off + 8] = resident.to_bytes(8, "little")
            buf.raw = bytes(raw)
            return size if filled is None else filled

    return _FakeLib()


class TestDarwinResidentBytes:
    def test_reads_pti_resident_size(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list = []
        monkeypatch.setattr(pc, "IS_MACOS", True)
        monkeypatch.setattr(
            pc, "_darwin_libproc_handle", lambda: _taskinfo_lib(412 * _MIB, calls=calls)
        )
        assert pc.darwin_resident_bytes_for_pid(4242) == 412 * _MIB
        assert calls == [(4242, pc._DARWIN_PROC_PIDTASKINFO, 0, pc._DARWIN_PROC_TASKINFO_SIZE)]

    def test_a_short_fill_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pc, "IS_MACOS", True)
        monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: _taskinfo_lib(4096, filled=0))
        assert pc.darwin_resident_bytes_for_pid(4242) is None

    def test_without_libproc_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pc, "IS_MACOS", True)
        monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: None)
        assert pc.darwin_resident_bytes_for_pid(4242) is None

    def test_is_macos_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pc, "IS_MACOS", False)
        monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: _taskinfo_lib(4096))
        assert pc.darwin_resident_bytes_for_pid(4242) is None


class TestDoctorGatewayRss:
    def test_macos_reads_the_gateway_rss_without_ps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A sandboxed doctor whose setuid ``ps`` cannot run still reports the
        gateway RSS on macOS."""

        def _no_ps(*_a, **_k):
            raise AssertionError("ps must not run when libproc answers")

        cfg = MagicMock()
        cfg.session.watchdog_rss_max_mb = 1536
        monkeypatch.setattr(cli_doctor.KiroCrewConfig, "load", lambda: cfg)
        monkeypatch.setattr(cli_doctor, "_read_gateway_pid", lambda: 4242)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "IS_MACOS", True)
        monkeypatch.setattr(pc, "proc_rss_bytes_for_pid", lambda pid: None)
        monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: _taskinfo_lib(412 * _MIB))
        monkeypatch.setattr(pc, "trusted_system_bin", _no_ps)
        monkeypatch.setattr(cli_doctor.subprocess, "check_output", _no_ps)
        _ceiling, rss = cli_doctor._gateway_memory_lines()
        assert "412 MiB" in rss and "4242" in rss
        assert "unreadable" not in rss

    def test_ps_stays_the_last_resort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When libproc does not answer, the doctor still asks ``ps`` (KiB)."""
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "IS_MACOS", True)
        monkeypatch.setattr(pc, "proc_rss_bytes_for_pid", lambda pid: None)
        monkeypatch.setattr(pc, "_darwin_libproc_handle", lambda: None)
        monkeypatch.setattr(pc, "trusted_system_bin", lambda name: "/bin/ps")
        monkeypatch.setattr(cli_doctor.subprocess, "check_output", lambda argv, timeout: b" 1024\n")
        assert cli_doctor._gateway_rss_bytes(4242) == 1024 * 1024
