"""Backoff policy and run schedule for the Kiro Space market client.

Pure functions with an injected rng, so every number here is deterministic.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.market_backoff import (
    CAP_S,
    MAX_TRIES,
    BackoffPolicy,
    first_run_offset,
    next_run_delay,
)


class _Rng:
    """uniform(a, b) returns a point `frac` of the way from a to b."""

    def __init__(self, frac: float) -> None:
        self.frac = frac

    def uniform(self, a: float, b: float) -> float:
        return a + (b - a) * self.frac


def test_full_jitter_spans_zero_to_the_exponential_ceiling() -> None:
    p = BackoffPolicy()
    assert p.next_delay(1, None, _Rng(0.0)) == 0.0
    top = [p.next_delay(n, None, _Rng(1.0)) for n in (1, 2, 3)]
    assert top == [p.base_s, p.base_s * 2, p.base_s * 4]


def test_delay_is_capped_at_one_hour() -> None:
    p = BackoffPolicy(base_s=1000.0, max_tries=20)
    assert CAP_S == 3600.0
    assert p.next_delay(10, None, _Rng(1.0)) == 3600.0


def test_delay_is_never_shorter_than_retry_after() -> None:
    p = BackoffPolicy()
    assert p.next_delay(1, 120, _Rng(0.0)) == 120
    # Retry-After longer than the cap still wins: the server asked for it.
    assert p.next_delay(1, 7200, _Rng(1.0)) == 7200


def test_gives_up_after_a_small_fixed_number_of_tries() -> None:
    p = BackoffPolicy()
    assert MAX_TRIES == p.max_tries == 5
    assert p.next_delay(MAX_TRIES - 1, None, _Rng(0.5)) is not None
    assert p.next_delay(MAX_TRIES, None, _Rng(0.5)) is None
    assert p.next_delay(MAX_TRIES + 3, 10, _Rng(0.5)) is None


def test_attempt_below_one_is_refused() -> None:
    with pytest.raises(ValueError):
        BackoffPolicy().next_delay(0, None, _Rng(0.5))


def test_first_run_offset_is_stable_and_inside_the_interval() -> None:
    a = first_run_offset("client-a", 3600.0)
    assert a == first_run_offset("client-a", 3600.0)
    assert 0 <= a < 3600.0
    offsets = {first_run_offset(f"client-{i}", 3600.0) for i in range(200)}
    assert len(offsets) > 150  # the fleet is spread, not stacked on one second


def test_first_run_offset_refuses_a_non_positive_interval() -> None:
    with pytest.raises(ValueError):
        first_run_offset("c", 0)


def test_next_run_adds_plus_minus_fifteen_percent() -> None:
    assert next_run_delay(1000.0, _Rng(0.0)) == pytest.approx(850.0)
    assert next_run_delay(1000.0, _Rng(0.5)) == pytest.approx(1000.0)
    assert next_run_delay(1000.0, _Rng(1.0)) == pytest.approx(1150.0)
