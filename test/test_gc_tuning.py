"""``kiro_crew.gc_tuning``: off by default; on, one threshold change and one freeze after warm-up."""

from __future__ import annotations

import asyncio
import gc

import pytest

from kiro_crew import gc_tuning
from kiro_crew.testing.wait import await_until, finished


@pytest.fixture(autouse=True)
def _isolated_collector(_floor_monkeypatch):
    """Spy on ``gc.freeze`` and restore the thresholds, so no test changes this worker's GC."""
    freezes: list[int] = []
    # A spy, not the real freeze: freezing would park this worker's whole heap.
    _floor_monkeypatch.setattr(gc, "freeze", lambda: freezes.append(1))
    _floor_monkeypatch.delenv(gc_tuning.SWITCH_ENV, raising=False)
    before = gc.get_threshold()
    gc_tuning.reset_for_tests()
    try:
        yield freezes
    finally:
        gc.set_threshold(*before)
        gc_tuning.reset_for_tests()


class TestSwitchOff:
    def test_unset_is_off(self) -> None:
        assert gc_tuning.enabled() is False

    @pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "2"])
    def test_non_truthy_values_are_off(self, monkeypatch, value: str) -> None:
        monkeypatch.setenv(gc_tuning.SWITCH_ENV, value)
        assert gc_tuning.enabled() is False

    def test_thresholds_untouched(self) -> None:
        before = gc.get_threshold()
        assert gc_tuning.apply_threshold() is False
        assert gc.get_threshold() == before

    @pytest.mark.asyncio
    async def test_no_freeze_after_warm_up(self, _isolated_collector) -> None:
        warm = asyncio.Event()
        warm.set()
        await gc_tuning.freeze_when_warm(warm)
        assert gc_tuning.freeze_after_warm_up() is False
        assert _isolated_collector == []


class TestSwitchOn:
    @pytest.fixture(autouse=True)
    def _on(self, _floor_monkeypatch) -> None:
        _floor_monkeypatch.setenv(gc_tuning.SWITCH_ENV, "1")

    @pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
    def test_truthy_values_are_on(self, monkeypatch, value: str) -> None:
        monkeypatch.setenv(gc_tuning.SWITCH_ENV, value)
        assert gc_tuning.enabled() is True

    def test_gen2_threshold_applied_and_younger_kept(self) -> None:
        gc.set_threshold(700, 10, 10)
        assert gc_tuning.apply_threshold() is True
        assert gc.get_threshold() == (700, 10, gc_tuning.GEN2_THRESHOLD)

    @pytest.mark.asyncio
    async def test_freezes_only_after_warm_up_and_only_once(self, _isolated_collector) -> None:
        warm = asyncio.Event()
        waiter = asyncio.create_task(gc_tuning.freeze_when_warm(warm))
        await asyncio.sleep(0)
        assert _isolated_collector == [], "must not freeze before warm-up finishes"

        warm.set()
        await await_until(waiter, what="the warm-up freeze")
        assert _isolated_collector == [1]

        # A second warm-up signal (the event is set again on later tab rebuilds).
        warm.clear()
        warm.set()
        await finished(gc_tuning.freeze_when_warm(warm), "the second warm-up wait")
        assert gc_tuning.freeze_after_warm_up() is False
        assert _isolated_collector == [1]
