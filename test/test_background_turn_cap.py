"""The memory-sized background-turn ceiling and the resizable limiter behind it.

Pure arithmetic is checked against worked host sizes; the limiter is driven on
the real event loop with no sleeps longer than its own short poll.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.dashboard import background_turn_cap as btc
from kiro_crew.dashboard.background_turn_cap import (
    ABSOLUTE_CEIL,
    LEGACY_CEIL,
    TurnLimiter,
    extra_permit_fits,
    reserve_gb,
    size_cap,
)


class TestSizing:
    def test_reserve_is_the_larger_of_the_share_and_the_floor(self) -> None:
        assert reserve_gb(128.0, 10, 4.0) == pytest.approx(12.8)
        assert reserve_gb(16.0, 10, 4.0) == 4.0, "a small host keeps the GB floor"
        assert reserve_gb(None, 10, 4.0) == 4.0

    @pytest.mark.parametrize(
        ("total_gb", "slice_gb", "expected_term", "expected_ceiling"),
        [
            # 128 GB workstation, no agents slice: (128 - 12.8) / 2 = 57.
            (128.0, None, 57, 57),
            # The same host under kirocrew-agents.slice (memory.high = 75%): 96 / 2 = 48.
            (128.0, 96.0, 48, 48),
            # 256 GB: (256 - 25.6) / 2 = 115, held at the absolute ceiling.
            (256.0, None, 115, ABSOLUTE_CEIL),
            # 16 GB laptop: (16 - 4) / 2 = 6, held at the floor of 16.
            (16.0, None, 6, LEGACY_CEIL),
        ],
    )
    def test_ceiling_from_host_memory(
        self, total_gb, slice_gb, expected_term, expected_ceiling
    ) -> None:
        sizing = size_cap({"max_background_turns": 0}, total_gb=total_gb, slice_high_gb=slice_gb)
        assert sizing.memory_term == expected_term
        assert sizing.ceiling == expected_ceiling
        assert sizing.effective == expected_ceiling, "0 is auto: the ceiling itself"
        assert sizing.auto

    def test_a_number_is_kept_and_capped_at_the_ceiling(self) -> None:
        assert (
            size_cap({"max_background_turns": 9}, total_gb=128.0, slice_high_gb=None).effective == 9
        )
        assert (
            size_cap({"max_background_turns": 500}, total_gb=128.0, slice_high_gb=None).effective
            == 57
        )

    def test_default_count_is_unchanged(self) -> None:
        assert size_cap({}, total_gb=128.0, slice_high_gb=None).effective == btc.DEFAULT_TURNS

    def test_unreadable_memory_keeps_the_old_ceiling(self) -> None:
        sizing = size_cap({"max_background_turns": 0}, total_gb=None, slice_high_gb=None)
        assert sizing.memory_term is None
        assert sizing.ceiling == LEGACY_CEIL
        assert sizing.effective == LEGACY_CEIL

    def test_sizing_inputs_are_live_tunables(self) -> None:
        lighter = size_cap(
            {"max_background_turns": 0, "background_turn_mem_gb": 1.5},
            total_gb=128.0,
            slice_high_gb=None,
        )
        assert lighter.effective == 64  # floor(115.2 / 1.5) = 76, held at 64
        bigger_reserve = size_cap(
            {"max_background_turns": 0, "background_turn_os_reserve_pct": 25},
            total_gb=128.0,
            slice_high_gb=None,
        )
        assert bigger_reserve.effective == 48  # (128 - 32) / 2

    @pytest.mark.parametrize(
        "raw",
        [
            {"background_turn_mem_gb": "x"},
            {"background_turn_mem_gb": True},
            {"background_turn_mem_gb": float("nan")},
        ],
    )
    def test_garbage_inputs_read_as_defaults(self, raw) -> None:
        sizing = size_cap({"max_background_turns": 0, **raw}, total_gb=128.0, slice_high_gb=None)
        assert sizing.turn_mem_gb == btc.DEFAULT_TURN_MEM_GB

    def test_out_of_range_inputs_are_clamped(self) -> None:
        sizing = size_cap(
            {
                "max_background_turns": 0,
                "background_turn_mem_gb": 0.0,
                "background_turn_os_reserve_pct": 1,
            },
            total_gb=128.0,
            slice_high_gb=None,
        )
        assert sizing.turn_mem_gb == btc.TURN_MEM_GB_MIN
        assert sizing.reserve_gb == pytest.approx(128.0 * btc.OS_RESERVE_PCT_MIN / 100)


class TestLiveGate:
    def _sizing(self):
        return size_cap({"max_background_turns": 0}, total_gb=128.0, slice_high_gb=None)

    def test_fits_while_the_reserve_and_one_turn_are_free(self) -> None:
        assert extra_permit_fits(self._sizing(), host_available_gb=40.0, tightest_available_gb=30.0)

    def test_denies_when_the_turn_would_eat_the_os_reserve(self) -> None:
        # reserve 12.8 + turn 2.0 = 14.8 needed on the host
        assert not extra_permit_fits(
            self._sizing(), host_available_gb=14.0, tightest_available_gb=14.0
        )

    def test_denies_when_the_agents_slice_is_full(self) -> None:
        assert not extra_permit_fits(
            self._sizing(), host_available_gb=60.0, tightest_available_gb=1.0
        )

    def test_unreadable_memory_denies(self) -> None:
        assert not extra_permit_fits(
            self._sizing(), host_available_gb=None, tightest_available_gb=None
        )


class TestTurnLimiter:
    @pytest.mark.asyncio
    async def test_raising_the_cap_admits_queued_turns_while_others_run(self) -> None:
        lim = TurnLimiter(1, poll_secs=0.05)
        await lim.acquire()
        waiter = asyncio.ensure_future(lim.acquire())
        await asyncio.sleep(0)
        assert not waiter.done() and lim.waiting == 1
        lim.resize(2)
        await asyncio.wait_for(waiter, 1)
        assert lim.held == 2

    @pytest.mark.asyncio
    async def test_cutting_the_cap_lets_running_turns_finish_and_holds_new_ones(self) -> None:
        lim = TurnLimiter(3, poll_secs=0.05)
        for _ in range(3):
            await lim.acquire()
        lim.resize(1)
        waiter = asyncio.ensure_future(lim.acquire())
        lim.release()
        lim.release()
        await asyncio.sleep(0.1)
        assert not waiter.done(), "admitted above the new cap"
        lim.release()
        await asyncio.wait_for(waiter, 1)
        assert lim.held == 1

    @pytest.mark.asyncio
    async def test_waiters_are_served_in_arrival_order_and_newcomers_queue(self) -> None:
        lim = TurnLimiter(1, poll_secs=0.05)
        await lim.acquire()
        order: list[int] = []

        async def take(i: int) -> None:
            await lim.acquire()
            order.append(i)

        tasks = [asyncio.ensure_future(take(i)) for i in range(3)]
        await asyncio.sleep(0)
        assert lim.locked()
        for _ in range(3):
            lim.release()
            await asyncio.sleep(0)
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        assert order == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_a_refresh_reaches_a_queue_no_release_wakes(self) -> None:
        box = {"cap": 1}
        lim: TurnLimiter

        def refresh() -> None:
            lim.resize(box["cap"])

        lim = TurnLimiter(1, refresh=refresh, poll_secs=0.05)
        await lim.acquire()
        waiter = asyncio.ensure_future(lim.acquire())
        await asyncio.sleep(0.1)
        assert not waiter.done()
        box["cap"] = 2  # a config edit, with nothing released
        await asyncio.wait_for(waiter, 1)

    @pytest.mark.asyncio
    async def test_permits_above_the_old_ceiling_wait_for_memory(self) -> None:
        box = {"fits": False}
        lim = TurnLimiter(LEGACY_CEIL + 2, admit_extra=lambda: box["fits"], poll_secs=0.05)
        for _ in range(LEGACY_CEIL):
            await lim.acquire()
        waiter = asyncio.ensure_future(lim.acquire())
        await asyncio.sleep(0.1)
        assert not waiter.done(), "an extra permit was granted with no memory for it"
        box["fits"] = True
        await asyncio.wait_for(waiter, 1)
        assert lim.held == LEGACY_CEIL + 1

    @pytest.mark.asyncio
    async def test_a_cancelled_waiter_gives_its_place_to_the_next(self) -> None:
        lim = TurnLimiter(1, poll_secs=0.05)
        await lim.acquire()
        first = asyncio.ensure_future(lim.acquire())
        second = asyncio.ensure_future(lim.acquire())
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        lim.release()
        await asyncio.wait_for(second, 1)
        assert lim.held == 1

    def test_over_release_raises(self) -> None:
        with pytest.raises(ValueError):
            TurnLimiter(1).release()
