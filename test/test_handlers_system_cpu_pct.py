"""Tests for system cpu_pct via /proc/stat delta in handlers_system.py."""

from __future__ import annotations

from unittest.mock import mock_open, patch

from kiro_crew.dashboard import handlers_system as hs


def _reset() -> None:
    hs._prev_sys_cpu["busy"] = 0.0
    hs._prev_sys_cpu["total"] = 0.0
    hs._sys_cpu_pct = 0.0


def _stat(line: str):
    return patch("builtins.open", mock_open(read_data=line + "\n"))


class TestSystemCpuPctFromProcStat:
    def test_first_sample_returns_none(self) -> None:
        _reset()
        # cpu user nice system idle iowait irq softirq steal
        with _stat("cpu 100 0 50 800 30 0 10 10"):
            assert hs._system_cpu_pct_from_proc_stat() is None
        assert hs._prev_sys_cpu["total"] == 1000.0
        assert hs._prev_sys_cpu["busy"] == 200.0

    def test_second_sample_computes_delta(self) -> None:
        _reset()
        with _stat("cpu 100 0 50 800 30 0 10 10"):
            hs._system_cpu_pct_from_proc_stat()  # prime: total=1000, busy=200
        # total +800, busy +200 -> 200/800 = 25.0%
        with _stat("cpu 200 0 100 1400 40 0 30 30"):
            assert hs._system_cpu_pct_from_proc_stat() == 25.0

    def test_no_tick_advance_reuses_last(self) -> None:
        _reset()
        with _stat("cpu 100 0 50 800 30 0 10 10"):
            hs._system_cpu_pct_from_proc_stat()
        with _stat("cpu 200 0 100 1400 40 0 30 30"):
            hs._system_cpu_pct_from_proc_stat()  # -> 25.0
        # identical totals: no delta, return the cached value
        with _stat("cpu 200 0 100 1400 40 0 30 30"):
            assert hs._system_cpu_pct_from_proc_stat() == 25.0

    def test_iowait_and_steal_count_as_busy(self) -> None:
        _reset()
        with _stat("cpu 0 0 0 1000 0 0 0 0"):
            hs._system_cpu_pct_from_proc_stat()  # prime: total=1000, busy=0
        # next interval is entirely iowait(50)+steal(50), idle unchanged
        with _stat("cpu 0 0 0 1000 50 0 0 50"):
            assert hs._system_cpu_pct_from_proc_stat() == 100.0

    def test_non_linux_returns_none(self) -> None:
        _reset()
        with patch("builtins.open", side_effect=OSError):
            assert hs._system_cpu_pct_from_proc_stat() is None

    def test_malformed_line_returns_none(self) -> None:
        _reset()
        with _stat("cpu 1 2"):  # fewer than 5 fields
            assert hs._system_cpu_pct_from_proc_stat() is None
        with _stat("intr 12345 0 0"):  # not the cpu aggregate line
            assert hs._system_cpu_pct_from_proc_stat() is None

    def test_garbage_values_return_none(self) -> None:
        _reset()
        # cpu-prefixed with enough fields but a non-numeric value -> None, not a raise
        with _stat("cpu 100 0 bad 800 30 0 10 10"):
            assert hs._system_cpu_pct_from_proc_stat() is None

    def test_guest_columns_excluded(self) -> None:
        _reset()
        # guest(col 9)/guest_nice(col 10) are already in user/nice; they must
        # not be summed again. Prime with 8 cols, then advance only guest/
        # guest_nice -> total must not move, so the cached value is reused.
        with _stat("cpu 100 0 50 800 30 0 10 10"):
            hs._system_cpu_pct_from_proc_stat()  # prime: total=1000 (cols 1-8)
        with _stat("cpu 200 0 100 1400 40 0 30 30 500 250"):
            hs._system_cpu_pct_from_proc_stat()  # -> 25.0 from cols 1-8 only
        # only guest/guest_nice change; cols 1-8 identical -> no real delta
        with _stat("cpu 200 0 100 1400 40 0 30 30 999 999"):
            assert hs._system_cpu_pct_from_proc_stat() == 25.0
        assert hs._prev_sys_cpu["total"] == 1800.0  # cols 1-8 sum, guest excluded


class TestCollectSystemMetricsCpuFallback:
    def test_ps_fallback_when_proc_stat_returns_none(self) -> None:
        """When /proc/stat yields None (non-Linux/first sample), the handler falls
        back to the ps lifetime-average to populate cpu_pct."""
        _reset()
        with (
            patch.object(hs, "_system_cpu_pct_from_proc_stat", return_value=None),
            patch.object(hs.subprocess, "check_output", return_value=b"%CPU\n10.0\n5.0\n"),
            patch.object(hs.os, "cpu_count", return_value=1),
        ):
            data = hs._collect_system_metrics()
        assert data["cpu_pct"] == 15.0
