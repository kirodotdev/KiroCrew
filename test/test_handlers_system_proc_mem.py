"""The system-metrics payload must report LIVE process memory, plus the peak.

``proc_mem_mb`` is rendered on the dashboard as the gateway's current memory. It
was fed by ``ru_maxrss``, a high-water mark that never decreases, so the figure
was a monotonic peak-since-boot that no ``ps -o rss=`` reading could reproduce.
These tests pin which reader feeds which field, because the two are the same
type and the same order of magnitude — transposing them is invisible.
"""

from __future__ import annotations

from unittest.mock import patch


def _run_collect() -> dict:
    """Run ``_collect_system_metrics`` with the static/scan blocks stubbed out.

    Every other block in the collector is individually wrapped in try/except, so
    it degrades to a missing value rather than failing this test.
    """
    from kiro_crew.dashboard import handlers_system

    with patch.object(handlers_system, "_get_static_system_info", return_value={}):
        handlers_system._metrics_cache.clear()
        handlers_system._metrics_cache_ts = 0.0
        handlers_system._proc_scan_cache = {}
        handlers_system._proc_scan_cache_ts = 0.0
        return handlers_system._collect_system_metrics()


class TestProcessMemoryFields:
    def test_live_and_peak_come_from_their_own_readers(self) -> None:
        from kiro_crew.dashboard import handlers_system

        with (
            patch.object(
                handlers_system.platform_compat, "proc_rss_bytes", return_value=200 * 1024 * 1024
            ),
            patch.object(
                handlers_system.platform_compat,
                "proc_peak_rss_bytes",
                return_value=1700 * 1024 * 1024,
            ),
        ):
            data = _run_collect()

        assert data["proc_mem_mb"] == 200.0
        assert data["proc_mem_peak_mb"] == 1700.0

    def test_the_live_field_is_not_fed_by_the_peak_reader(self) -> None:
        """The reported bug in one assertion: a released spike must not persist.

        The peak reader is given a figure an order of magnitude above the live
        one, mirroring the user report of a 1.66 GB reading on a process whose
        real residency was far lower.
        """
        from kiro_crew.dashboard import handlers_system

        with (
            patch.object(
                handlers_system.platform_compat, "proc_rss_bytes", return_value=150 * 1024 * 1024
            ),
            patch.object(
                handlers_system.platform_compat,
                "proc_peak_rss_bytes",
                return_value=1660 * 1024 * 1024,
            ),
        ):
            data = _run_collect()

        assert data["proc_mem_mb"] < 200
        assert data["proc_mem_peak_mb"] > 1000

    def test_a_failing_reader_degrades_to_zero_without_dropping_the_field(self) -> None:
        # The dashboard renders whatever keys arrive, so a missing key and a 0
        # are different failures. Keep both keys present.
        from kiro_crew.dashboard import handlers_system

        with (
            patch.object(
                handlers_system.platform_compat,
                "proc_rss_bytes",
                side_effect=OSError("no reading"),
            ),
            patch.object(
                handlers_system.platform_compat,
                "proc_peak_rss_bytes",
                side_effect=OSError("no reading"),
            ),
        ):
            data = _run_collect()

        assert data["proc_mem_mb"] == 0
        assert data["proc_mem_peak_mb"] == 0
