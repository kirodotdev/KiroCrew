"""The spine driver's ``--max-hours`` and ``--max-cost`` budgets must be finite.

The run loop stops when ``hours_used > max_hours`` or ``cost > max_cost_usd``.
Every comparison with NaN is False and nothing is greater than infinity, so a
NaN or infinite cap would never stop the run. argparse refuses such a value
with its usage error (exit 2) before any run starts. A finite value, including
the defaults, reaches the caps unchanged.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.builtins.auto_improvement.spine import driver as drv


@pytest.mark.parametrize("flag", ["--max-hours", "--max-cost"])
@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "Infinity", "1e999"])
def test_a_non_finite_budget_is_refused_before_the_run(
    flag: str, value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        drv.main([flag, value])
    assert exc.value.code == 2
    assert flag in capsys.readouterr().err


def test_finite_budgets_reach_the_caps_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    assert drv.main(["--max-hours", "0.5", "--max-cost", "12.25"]) == 0
    assert "/ 0.5h / $12.25" in capsys.readouterr().out


def test_the_default_budgets_are_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    assert drv.main([]) == 0
    assert "/ 10.0h / $50.0" in capsys.readouterr().out
