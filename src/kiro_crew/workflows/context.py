"""Run-scoped execution context for dynamic workflows (GATES A4, B6, B3-runtime, B7).

This is the ``context`` layer (``dsl`` → ``context`` → ``runner``; GATE F1). It
provides the pieces the runner needs to execute a workflow script safely and
within ceilings, WITHOUT importing the runner or any heavy gateway code:

* ``Budget`` — token accounting with a HARD ceiling (GATE A4). ``charge()`` past
  ``total`` raises ``BudgetExceeded``; ``remaining()`` is ``math.inf`` when
  ``total is None``. Satisfies the frozen ``Budget`` Protocol (conformance F2).
* ``AgentCounter`` — per-run lifetime agent-call cap (GATE B6 backstop).
* ``build_safe_globals()`` — the restricted ``globals`` the runner ``exec``s a
  script in: only ``validate.SAFE_BUILTINS`` plus the injected ``ctx`` are
  reachable, so ``time``/``random``/``uuid``/``open``/``__import__`` are absent at
  run time (B3 runtime half) and there is no filesystem/socket capability (B7).

The concrete ``WorkflowContext`` that wires ``dsl.parallel/pipeline``, agents,
and the event stream is assembled by ``runner.py`` (next unit) using these parts;
keeping them here lets the runner stay thin and keeps this layer dependency-light.

Spec: ``docs/system-specs/modules/workflows.md``.
"""

from __future__ import annotations

import builtins
import math
from typing import Any, Optional

from . import BudgetExceeded
from .validate import SAFE_BUILTINS

# Lifetime agent-call backstop per run (matches ``_MAX_AGENTS_PER_RUN`` in spec).
DEFAULT_MAX_AGENTS_PER_RUN = 1000


class Budget:
    """Token budget with a hard ceiling (GATE A4).

    ``total`` is the ceiling (None = unbounded). ``charge(cost)`` accumulates
    spend and raises ``BudgetExceeded`` the moment cumulative spend would meet or
    exceed ``total`` — the runner calls it before/around each ``agent()`` so a
    script cannot spend past the target. Satisfies the frozen ``Budget`` Protocol.
    """

    def __init__(self, total: Optional[int] = None) -> None:
        self.total = total
        self._spent = 0

    def spent(self) -> int:
        return self._spent

    def remaining(self) -> float:
        if self.total is None:
            return math.inf
        return float(max(0, self.total - self._spent))

    def would_exceed(self, cost: int = 0) -> bool:
        """True if spending ``cost`` more would reach/exceed the ceiling."""
        if self.total is None:
            return False
        return self._spent + cost >= self.total

    def charge(self, cost: int) -> None:
        """Record ``cost`` tokens; raise ``BudgetExceeded`` if at/over the ceiling.

        The ceiling is hard: once cumulative spend reaches ``total`` no further
        charge is allowed (the check is ``>=`` so a run cannot exceed the target).
        """
        new_spent = self._spent + cost
        if self.total is not None and new_spent >= self.total:
            # Report the *attempted* spend (new_spent) before clamping; clamp the
            # stored counter to the ceiling so it never reads above total.
            self._spent = min(self.total, new_spent)
            raise BudgetExceeded(
                f"budget exhausted: would spend {new_spent} of {self.total}"
            )
        self._spent = new_spent


class AgentCounter:
    """Lifetime agent-call cap per run (GATE B6 runaway backstop)."""

    def __init__(self, limit: int = DEFAULT_MAX_AGENTS_PER_RUN) -> None:
        self.limit = limit
        self._count = 0

    @property
    def count(self) -> int:
        return self._count

    def increment(self) -> None:
        """Count one agent call; raise once the per-run cap is exceeded."""
        if self._count >= self.limit:
            raise BudgetExceeded(f"agent-count cap reached: {self.limit} agents per run")
        self._count += 1


# Builtins a workflow script may use — resolved from the names ``validate`` allows,
# so the allow-list has a single source of truth (and B-group tests gate it).
def _safe_builtins_namespace() -> dict[str, Any]:
    ns: dict[str, Any] = {}
    for name in SAFE_BUILTINS:
        obj = getattr(builtins, name, None)
        if obj is not None:
            ns[name] = obj
    # ``__import__`` is deliberately absent → ``import`` statements fail at run time
    # even if one slipped past static validation (defense in depth, B3/B7).
    return ns


def build_safe_globals(ctx: Any) -> dict[str, Any]:
    """Return the restricted ``globals`` dict the runner execs a script in.

    Only ``ctx`` and the safe builtins are reachable; ``__builtins__`` is the
    curated set, so ``open``/``eval``/``exec``/``__import__``/``time``/``random``/
    ``uuid`` are unavailable at run time regardless of static validation
    (B3 runtime half, B7 no egress).
    """
    return {
        "__builtins__": _safe_builtins_namespace(),
        "ctx": ctx,
    }
