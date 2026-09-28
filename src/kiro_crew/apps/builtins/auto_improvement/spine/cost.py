"""Cost meter — the injectable cost source the ``--max-cost`` budget reads (spine).

The cost budget is one of the four clean-stop conditions (04_improvement_loop_perf.md
§5.1 "Cost budget | ``--max-cost USD`` | stop when token/compute cost cap reached";
08_safety_isolation_and_guardrails.md §7). The driver reads a cost SOURCE each cycle and
stops the run when the metered cost exceeds the cap.

The driver's ``cost_meter`` is a plain ``Callable[[], float]`` — the current cumulative
USD spend. That keeps the spine target-agnostic: it never names a model, a provider, or a
token price; it only reads a number. This module supplies the concrete, INJECTABLE meter
the agent-runner updates so the ``--max-cost`` check at the top of ``Driver.run`` can
actually fire (it defaults to ``lambda: 0.0`` — safe, never trips — until a real source is
wired).

Two shapes, both target-agnostic:

  - :class:`CostMeter` — a simple accumulator the agent-runner ``add()``s to per candidate
    (e.g. each proposer/measure agent invocation reports its incurred USD). The driver
    holds the meter via ``cost_meter=meter`` (the meter is callable: ``meter()`` returns
    the running total).
  - :meth:`CostMeter.add_tokens` — a tokens x rate accumulator: the runner reports input/
    output token counts + per-1K rates and the meter converts to USD. The RATES are
    supplied by the caller (the profile/run config), never hard-coded in the spine — so the
    spine stays free of any provider's price list.

Docs: 04_improvement_loop_perf.md §5.1 (cost budget stop), 08_safety §7; driver.py
``--max-cost`` gate (the cost check fires when ``cost_meter() > max_cost_usd``).
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass


@dataclass
class TokenRates:
    """Per-1K-token USD rates (caller/profile-supplied; the spine hard-codes none).

    Kept as a tiny value object so a run config can declare a target's model rates
    without the spine ever embedding a provider price list (target-agnostic)."""

    input_per_1k: float = 0.0
    output_per_1k: float = 0.0

    def cost(self, *, input_tokens: int, output_tokens: int) -> float:
        """USD for one (input, output) token pair at these rates."""
        return (input_tokens / 1000.0) * self.input_per_1k + (
            output_tokens / 1000.0
        ) * self.output_per_1k


class CostMeter:
    """A thread-safe cumulative USD accumulator the agent-runner updates per candidate.

    The realized cost SOURCE the driver reads for the ``--max-cost`` budget. The
    agent-runner (proposer/measure/keep agents) ``add()``s the USD each metered step
    incurred — or ``add_tokens()`` with token counts + rates — and the driver, which
    holds ``cost_meter=meter``, calls ``meter()`` each cycle to get the running total and
    stops the run if it exceeds the cap. Default total is ``0.0`` (safe — a meter that is
    never fed never trips the budget), so wiring it is purely additive over the
    ``lambda: 0.0`` default.

    It is intentionally trivial and target-agnostic: it knows nothing about WHAT incurred
    the cost — only the USD numbers reported to it. ``__call__`` makes an instance directly
    usable as the driver's ``cost_meter`` callable."""

    def __init__(self, *, initial_usd: float = 0.0) -> None:
        self._total = float(initial_usd)
        self._lock = threading.Lock()

    def add(self, usd: float) -> float:
        """Add an incurred USD amount (per candidate / per agent step). Returns the new
        running total. Negative amounts are rejected (cost only accrues)."""
        # Reject NaN/inf as well: ``float('nan') < 0`` is False, so a NaN would slip
        # past a bare negative check, poison ``_total`` (NaN + x == NaN), and silently
        # disable the ``--max-cost`` budget gate (``nan > cap`` is always False).
        if not math.isfinite(usd) or usd < 0:
            raise ValueError(f"cost must be non-negative; got {usd}")
        with self._lock:
            self._total += float(usd)
            return self._total

    def add_tokens(self, *, input_tokens: int, output_tokens: int, rates: TokenRates) -> float:
        """Accumulate a tokens x rate cost (the agent-runner's per-call token usage).

        ``rates`` is caller-supplied (the run config's model rates) so the spine never
        hard-codes a price. Returns the new running total."""
        return self.add(rates.cost(input_tokens=input_tokens, output_tokens=output_tokens))

    def total(self) -> float:
        """The current cumulative USD spend."""
        with self._lock:
            return self._total

    def __call__(self) -> float:
        """Callable form — usable directly as ``Driver(cost_meter=meter)``."""
        return self.total()
