"""Pin the scaling half of ``conftest.assert_rejected_without_backtracking``.

The helper times a grammar on thread CPU, so these tests drive it with a fake
``time.thread_time``: the subject "costs" exactly what the test says, which makes
the verdict deterministic on any runner and instant to compute. The real grammars
are timed by their own suites; what is pinned here is that a quadratic cost curve
that stays under the absolute budget is still refused.
"""

from __future__ import annotations

import time

import pytest

from conftest import (
    REDOS_LARGE_BUDGET_SECONDS,
    REDOS_LARGE_PUMPS,
    REDOS_SCALING_FLOOR_SECONDS,
    REDOS_SCALING_RATIO,
    assert_rejected_without_backtracking,
)

_PAST_THE_RATIO = rf"past the {REDOS_SCALING_RATIO}x a linear scan stays under"


def _drive(monkeypatch, cost_of, *, tick: float = 0.0, readings: list[int] | None = None) -> None:
    """Run the helper on a subject whose CPU cost for an ``n``-unit pump is
    ``cost_of(n)``, read through a clock quantised to ``tick`` seconds.

    When ``readings`` is given, the pump length of every reading taken is
    appended to it in order, so a test can count readings after a refusal."""
    clock = [0.0]
    taken = readings if readings is not None else []

    def thread_time() -> float:
        if not tick:
            return clock[0]
        return (clock[0] // tick) * tick

    def reject(text: str) -> None:
        taken.append(len(text))
        clock[0] += cost_of(len(text))

    monkeypatch.setattr(time, "thread_time", thread_time)
    assert_rejected_without_backtracking(reject, lambda n: "x" * n)


def _quadratic_costing(seconds_at_largest: float):
    largest = REDOS_LARGE_PUMPS[-1]
    return lambda n: seconds_at_largest * (n / largest) ** 2


def test_a_quadratic_scan_under_the_absolute_budget_is_refused(monkeypatch):
    """A quadratic ``[l](<`` rescan costs ~0.9-1.8 s of CPU at 20 000 openers,
    under the 2 s budget. The ratio between the long pumps is what refuses it."""
    assert 0.9 < REDOS_LARGE_BUDGET_SECONDS
    with pytest.raises(AssertionError, match=_PAST_THE_RATIO):
        _drive(monkeypatch, _quadratic_costing(0.9))


def test_a_quadratic_scan_over_the_floor_is_refused_through_the_ratio(monkeypatch):
    """1.8 s at 20 000 against 0.018 s at 2 000: the bound is the ratio's
    25 x 0.018 = 0.45 s, above the floor, so the ratio alone refuses it. Two
    readings over that bound are the verdict, so the regression's cost is paid
    twice at the largest size, never three times."""
    largest = REDOS_LARGE_PUMPS[-1]
    assert REDOS_SCALING_RATIO * 0.018 > REDOS_SCALING_FLOOR_SECONDS
    readings: list[int] = []
    with pytest.raises(AssertionError, match=_PAST_THE_RATIO):
        _drive(monkeypatch, _quadratic_costing(1.8), readings=readings)
    assert readings.count(largest) == 2


def test_a_linear_scan_over_the_floor_passes_through_the_ratio(monkeypatch):
    """0.5 s at 20 000 against 0.05 s at 2 000 is a linear 10x. It is over the
    floor, so only the ratio bound (25 x 0.05 = 1.25 s) lets it through."""
    assert 0.5 > REDOS_SCALING_FLOOR_SECONDS
    _drive(monkeypatch, lambda n: 0.5 * n / REDOS_LARGE_PUMPS[-1])


def test_a_spiked_baseline_reading_cannot_hide_a_quadratic_scan(monkeypatch):
    """The 2 000 pump keeps all three readings even when the first is under
    every line: a collection charged to that one reading (0.2 s) would
    otherwise become the baseline and lift the bound to 5 s, past a 1.8 s
    quadratic cost at 20 000."""
    middle = REDOS_LARGE_PUMPS[-2]
    quadratic = _quadratic_costing(1.8)
    spiked = [False]

    def cost_of(n: int) -> float:
        if n == middle and not spiked[0]:
            spiked[0] = True
            return 0.2
        return quadratic(n)

    assert REDOS_SCALING_RATIO * 0.2 > REDOS_LARGE_BUDGET_SECONDS > 1.8
    readings: list[int] = []
    with pytest.raises(AssertionError, match=_PAST_THE_RATIO):
        _drive(monkeypatch, cost_of, readings=readings)
    assert spiked[0]
    assert readings.count(middle) == 3


def test_a_linear_scan_passes(monkeypatch):
    _drive(monkeypatch, lambda n: 0.15 * n / REDOS_LARGE_PUMPS[-1])


def test_a_linear_scan_on_a_coarse_clock_passes(monkeypatch):
    """On a 15.6 ms Windows tick the smaller reading rounds to zero; the floor
    keeps a linear cost under it from reading as an infinite ratio."""
    assert REDOS_SCALING_FLOOR_SECONDS > 0.1
    _drive(monkeypatch, lambda n: 0.1 * n / REDOS_LARGE_PUMPS[-1], tick=0.0156)


def test_one_inflated_reading_does_not_fail_a_linear_scan(monkeypatch):
    """A garbage collection charged to one reading of the largest pump is
    absorbed: the helper keeps the cheapest of its readings."""
    largest = REDOS_LARGE_PUMPS[-1]
    spiked = [False]

    def cost_of(n: int) -> float:
        if n == largest and not spiked[0]:
            spiked[0] = True
            return 1.5
        return 0.15 * n / largest

    _drive(monkeypatch, cost_of)
    assert spiked[0]
