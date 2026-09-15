"""Cost meter — the accumulator the ``--max-cost`` clean-stop reads.

The budget gate fires on ``cost_meter() > cap``, so the meter's only real contract is
that the number it reports is a finite, monotonically non-decreasing total. The
interesting behaviour is therefore what it REFUSES: a negative amount (cost only
accrues) and a non-finite one — a NaN would slip past a bare ``< 0`` check, poison the
total, and disable the budget gate silently, because ``nan > cap`` is always False.
"""

from __future__ import annotations

import math

import pytest

from kiro_crew.apps.builtins.auto_improvement.spine.cost import CostMeter, TokenRates


class TestTokenRates:
    def test_cost_is_the_sum_of_both_directions_per_1k(self) -> None:
        rates = TokenRates(input_per_1k=2.0, output_per_1k=10.0)
        assert rates.cost(input_tokens=1000, output_tokens=500) == pytest.approx(2.0 + 5.0)

    def test_the_default_rates_price_everything_at_zero(self) -> None:
        """The spine hard-codes no price list, so an unconfigured rate must be free
        rather than a guess."""
        assert TokenRates().cost(input_tokens=10_000, output_tokens=10_000) == 0.0


class TestAccumulation:
    def test_add_returns_the_running_total(self) -> None:
        meter = CostMeter()
        assert meter.add(0.25) == pytest.approx(0.25)
        assert meter.add(0.75) == pytest.approx(1.0)
        assert meter.total() == pytest.approx(1.0)

    def test_an_initial_balance_is_carried(self) -> None:
        meter = CostMeter(initial_usd=3.5)
        assert meter.total() == pytest.approx(3.5)
        assert meter.add(0.5) == pytest.approx(4.0)

    def test_the_meter_is_callable_as_the_drivers_cost_source(self) -> None:
        """The driver holds it as a plain ``Callable[[], float]``."""
        meter = CostMeter()
        meter.add(1.25)
        assert meter() == pytest.approx(1.25)
        assert meter() == meter.total()

    def test_add_tokens_converts_through_the_caller_supplied_rates(self) -> None:
        meter = CostMeter()
        rates = TokenRates(input_per_1k=1.0, output_per_1k=4.0)
        assert meter.add_tokens(input_tokens=2000, output_tokens=250, rates=rates) == pytest.approx(
            3.0
        )
        assert meter.total() == pytest.approx(3.0)

    def test_a_never_fed_meter_reads_zero_so_it_cannot_trip_the_budget(self) -> None:
        assert CostMeter()() == 0.0


class TestRejectedAmounts:
    def test_a_negative_amount_is_refused_and_leaves_the_total_untouched(self) -> None:
        meter = CostMeter(initial_usd=1.0)
        with pytest.raises(ValueError, match="non-negative"):
            meter.add(-0.01)
        assert meter.total() == pytest.approx(1.0)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
    def test_a_non_finite_amount_is_refused_so_the_budget_gate_keeps_working(
        self, bad: float
    ) -> None:
        """``nan > cap`` is always False, so a NaN total would disable ``--max-cost``."""
        meter = CostMeter()
        with pytest.raises(ValueError):
            meter.add(bad)
        assert math.isfinite(meter.total())
        assert meter.total() == 0.0

    def test_a_non_finite_token_cost_is_refused_through_add_tokens_too(self) -> None:
        meter = CostMeter()
        with pytest.raises(ValueError):
            meter.add_tokens(
                input_tokens=1000, output_tokens=0, rates=TokenRates(input_per_1k=float("inf"))
            )
        assert meter.total() == 0.0
