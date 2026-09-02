"""GATES A4 + B6 + B3-runtime + B7 — budget ceiling, agent cap, restricted namespace.

* A4 — ``Budget`` is a HARD ceiling: ``charge`` past ``total`` raises
  ``BudgetExceeded``; ``remaining()``/``spent()`` track correctly; unbounded when
  ``total is None``. (Also confirms it still satisfies the frozen Protocol.)
* B6 — ``AgentCounter`` caps lifetime agent calls per run.
* B3 (runtime half) + B7 — ``build_safe_globals`` exposes only SAFE_BUILTINS + ctx;
  ``time``/``random``/``uuid``/``open``/``__import__``/``eval`` are unreachable, so
  even a script that slipped past static validation cannot import or open at run
  time. Proven by actually ``exec``-ing hostile snippets in the namespace.

See ``docs/system-specs/modules/workflows.md`` and GATES.md (A4, B6, B3, B7).
"""

from __future__ import annotations

import math

import pytest

import kiro_crew.workflows as wf
from kiro_crew.workflows.context import (
    DEFAULT_MAX_AGENTS_PER_RUN,
    AgentCounter,
    Budget,
    build_safe_globals,
)
from kiro_crew.workflows.validate import SAFE_BUILTINS

# --------------------------------------------------------------------------- #
# A4 — Budget hard ceiling
# --------------------------------------------------------------------------- #


def test_budget_unbounded_when_total_none() -> None:
    b = Budget(total=None)
    assert b.remaining() == math.inf
    b.charge(10_000_000)  # never raises
    assert b.spent() == 10_000_000
    assert b.remaining() == math.inf


def test_budget_tracks_spent_and_remaining() -> None:
    b = Budget(total=1000)
    b.charge(300)
    assert b.spent() == 300
    assert b.remaining() == 700


def test_budget_charge_under_ceiling_ok() -> None:
    b = Budget(total=1000)
    b.charge(999)  # 999 < 1000, allowed
    assert b.spent() == 999


def test_budget_hard_ceiling_raises_at_total() -> None:
    b = Budget(total=1000)
    with pytest.raises(wf.BudgetExceeded):
        b.charge(1000)  # reaches the ceiling exactly → hard stop


def test_budget_hard_ceiling_raises_over_total() -> None:
    b = Budget(total=1000)
    b.charge(900)
    with pytest.raises(wf.BudgetExceeded):
        b.charge(200)  # 900 + 200 >= 1000


def test_budget_would_exceed_predicate() -> None:
    b = Budget(total=100)
    b.charge(60)
    assert b.would_exceed(40) is True  # 60 + 40 >= 100
    assert b.would_exceed(30) is False  # 60 + 30 < 100
    assert Budget(total=None).would_exceed(10**9) is False


def test_budget_satisfies_frozen_protocol() -> None:
    """A4 impl must remain a structural match for the frozen Budget Protocol (F2)."""
    assert isinstance(Budget(total=10), wf.Budget)


# --------------------------------------------------------------------------- #
# B6 — agent-count cap
# --------------------------------------------------------------------------- #


def test_agent_counter_allows_up_to_limit() -> None:
    c = AgentCounter(limit=3)
    c.increment()
    c.increment()
    c.increment()
    assert c.count == 3


def test_agent_counter_raises_past_limit() -> None:
    c = AgentCounter(limit=2)
    c.increment()
    c.increment()
    with pytest.raises(wf.BudgetExceeded):
        c.increment()


def test_agent_counter_default_limit() -> None:
    assert AgentCounter().limit == DEFAULT_MAX_AGENTS_PER_RUN


# --------------------------------------------------------------------------- #
# B3 (runtime) + B7 — restricted namespace
# --------------------------------------------------------------------------- #


def test_safe_globals_only_exposes_ctx_and_safe_builtins() -> None:
    sentinel = object()
    g = build_safe_globals(sentinel)
    assert g["ctx"] is sentinel
    bi = g["__builtins__"]
    # every declared safe builtin is present...
    for name in SAFE_BUILTINS:
        assert name in bi
    # ...and dangerous names are absent.
    for danger in ("open", "eval", "exec", "compile", "__import__", "input", "getattr"):
        assert danger not in bi


@pytest.mark.parametrize(
    "snippet",
    [
        "open('/etc/passwd')",  # B7 no filesystem
        "__import__('os')",  # B3/B7 no import at runtime
        "eval('1+1')",
        "exec('x=1')",
    ],
)
def test_hostile_snippet_fails_at_runtime_in_safe_globals(snippet: str) -> None:
    """Even if static validation were bypassed, the namespace blocks it at run time."""
    g = build_safe_globals(ctx=object())
    with pytest.raises((NameError, KeyError)):
        exec(snippet, g)  # noqa: S102 (intentionally exec'ing hostile code in the SANDBOX)


def test_safe_builtins_actually_usable() -> None:
    """Positive: a benign script using safe builtins runs in the namespace."""
    g = build_safe_globals(ctx=object())
    exec("result = sorted([3, 1, 2])[:len([1, 2])]", g)  # noqa: S102
    assert g["result"] == [1, 2]


def test_import_statement_fails_in_safe_globals() -> None:
    """An ``import`` that slipped past static checks dies at run time (no __import__)."""
    g = build_safe_globals(ctx=object())
    with pytest.raises((ImportError, NameError, KeyError)):
        exec("import time", g)  # noqa: S102
