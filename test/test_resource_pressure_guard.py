"""The memory guard's trigger and fast sampling in the resource-pressure notifier.

rfc-host-memory-guard.md §8 tests 7 and 8: critical fires; tight with PSI
``full avg10`` >= 10 fires; PSI alone on ample does not; a missing PSI file fails
open; ``resource_critical_gb = 0`` disables both arms; sampling drops to the
heartbeat's 5 s on tight or critical and returns to 30 s on ample.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.notifications import resource_pressure
from kiro_crew.notifications.bus import NotificationBus
from kiro_crew.notifications.resource_pressure import (
    FAST_SAMPLE_INTERVAL_SECS,
    SAMPLE_INTERVAL_SECS,
    ResourcePressureNotifier,
)
from kiro_crew.platform_compat_metrics import linux_memory_psi_full_avg10
from kiro_crew.resource_status import (
    POSTURE_AMPLE,
    POSTURE_CRITICAL,
    POSTURE_TIGHT,
    ResourceStatus,
)


def _status(posture: str, gb: float = 1.5, critical_gb: float = 2.0) -> ResourceStatus:
    return ResourceStatus(
        available_gb=gb,
        cpu_count=8,
        load_per_cpu=0.5,
        posture=posture,
        pressure_gb=4.0,
        critical_gb=critical_gb,
    )


@pytest.fixture(autouse=True)
def _no_slice_io(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(resource_pressure, "check_agents_slice_pressure", lambda: None)
    _floor_monkeypatch.setattr(resource_pressure, "reclaim_agents_slice_page_cache", lambda: None)


def _run(
    readings: list[tuple[ResourceStatus, float | None]], clock: list[float]
) -> tuple[ResourcePressureNotifier, list[tuple[str, float | None]], list[float]]:
    """Sample each reading at the clock values the caller sets; record guard calls."""
    calls: list[tuple[str, float | None]] = []
    probed: list[float] = []
    queue = list(readings)
    current: list[tuple[ResourceStatus, float | None]] = []

    def probe() -> ResourceStatus:
        current[:] = [queue.pop(0)]
        probed.append(clock[0])
        return current[0][0]

    notifier = ResourcePressureNotifier(
        NotificationBus(sink=lambda note: None),
        probe_fn=probe,
        clock=lambda: clock[0],
        psi_fn=lambda: current[0][1],
    )
    notifier.memory_guard = lambda status, psi, trigger: calls.append((trigger, psi))
    return notifier, calls, probed


class TestTrigger:
    @pytest.mark.parametrize(
        ("posture", "psi", "expected"),
        [
            (POSTURE_CRITICAL, None, "critical"),
            (POSTURE_CRITICAL, 50.0, "critical"),
            (POSTURE_TIGHT, 10.0, "psi"),
            (POSTURE_TIGHT, 9.99, None),
            (POSTURE_TIGHT, None, None),  # a missing PSI file fails open
            (POSTURE_AMPLE, 80.0, None),  # PSI alone on an ample host stops nothing
        ],
    )
    def test_trigger_per_reading(self, posture: str, psi: float | None, expected: Any) -> None:
        clock = [0.0]
        notifier, calls, _ = _run([(_status(posture), psi)], clock)
        asyncio.run(notifier.maybe_sample())
        assert calls == ([] if expected is None else [(expected, psi)])

    @pytest.mark.parametrize("posture", [POSTURE_CRITICAL, POSTURE_TIGHT])
    def test_critical_gb_zero_disables_both_arms(self, posture: str) -> None:
        clock = [0.0]
        notifier, calls, _ = _run([(_status(posture, critical_gb=0.0), 90.0)], clock)
        asyncio.run(notifier.maybe_sample())
        assert calls == []

    def test_a_guard_that_raises_does_not_break_sampling(self) -> None:
        clock = [0.0]
        notifier, _, probed = _run(
            [(_status(POSTURE_CRITICAL), None), (_status(POSTURE_CRITICAL), None)], clock
        )

        def boom(*_a: Any) -> None:
            raise RuntimeError("guard down")

        notifier.memory_guard = boom

        async def scenario() -> None:
            await notifier.maybe_sample()
            clock[0] = FAST_SAMPLE_INTERVAL_SECS
            await notifier.maybe_sample()

        asyncio.run(scenario())
        assert probed == [0.0, FAST_SAMPLE_INTERVAL_SECS]


class TestFastSampling:
    def test_tight_and_critical_sample_every_beat_then_ample_returns_to_30s(self) -> None:
        clock = [0.0]
        notifier, _, probed = _run(
            [
                (_status(POSTURE_TIGHT, gb=3.0), None),
                (_status(POSTURE_CRITICAL), None),
                (_status(POSTURE_AMPLE, gb=20.0), None),
                (_status(POSTURE_AMPLE, gb=20.0), None),
            ],
            clock,
        )

        async def scenario() -> None:
            for at in (0.0, 5.0, 10.0, 15.0, 20.0, 40.0):
                clock[0] = at
                await notifier.maybe_sample()

        asyncio.run(scenario())
        # tight at 0 -> next at 5 (critical) -> next at 10 (ample) -> gated to 40.
        assert FAST_SAMPLE_INTERVAL_SECS == 5.0
        assert probed == [0.0, 5.0, 10.0, 10.0 + SAMPLE_INTERVAL_SECS]


class TestPsiReader:
    def test_reads_full_avg10(self, tmp_path: Path) -> None:
        path = tmp_path / "memory"
        path.write_text(
            "some avg10=31.20 avg60=10.00 avg300=2.00 total=1\n"
            "full avg10=12.50 avg60=4.00 avg300=1.00 total=2\n"
        )
        assert linux_memory_psi_full_avg10(str(path)) == 12.5

    def test_missing_or_malformed_file_reads_none(self, tmp_path: Path) -> None:
        assert linux_memory_psi_full_avg10(str(tmp_path / "absent")) is None
        bad = tmp_path / "bad"
        bad.write_text("full avg10=nope\n")
        assert linux_memory_psi_full_avg10(str(bad)) is None
        some_only = tmp_path / "some"
        some_only.write_text("some avg10=5.00 avg60=1.00 avg300=0.00 total=1\n")
        assert linux_memory_psi_full_avg10(str(some_only)) is None
