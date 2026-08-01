"""Every dispatch surface must record a REAL turn duration.

Regression guard for the row-store half of the ``duration_ms: 0`` defect. The
OTEL histogram was fixed separately (per-instrument buckets); the row store kept
reading ``TurnUsage.duration_ms``, which the acp provider never assigns, so every
real row in ``<data home>/usage/tokens/*.jsonl`` carried a literal 0 and the
Telemetry page could not answer "how long did this turn take".

The fix threads a caller-measured ``elapsed_ms`` into the record builder as the
FALLBACK for ``duration_ms``. These tests lock in three properties:

1. the fallback fires when the provider reports nothing (the acp reality),
2. a provider that DOES report still wins (so the fallback is not a clobber),
3. every persist call site actually passes a measured value — the property that
   would silently rot as new dispatch surfaces are added.

Property 3 is enforced structurally (AST over the real source), not by mocking
each surface: a mock-per-surface suite passes happily while a NEW surface added
next quarter forgets the argument entirely.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import datetime

import pytest

from kiro_crew.acp.types import TurnUsage
from kiro_crew.dashboard.handlers.usage import _build_token_record

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


class _Ev:
    """Minimal stand-in for an AcpEvent carrying usage."""

    def __init__(self, usage: TurnUsage) -> None:
        self.usage = usage


def _record(usage: TurnUsage, **kw: object) -> dict:
    return _build_token_record("slot-1", "claude-opus-5", _Ev(usage), "acp",
                               datetime.now().astimezone(), **kw)  # type: ignore[arg-type]


# ─────────────────────── builder semantics ───────────────────────

def test_local_wall_clock_is_recorded_when_provider_reports_nothing():
    """The acp reality: usage.duration_ms is 0, so elapsed_ms must land."""
    rec = _record(TurnUsage(input_tokens=10, output_tokens=5), elapsed_ms=227589)
    assert rec["duration_ms"] == 227589


def test_provider_reported_duration_wins_over_local_clock():
    """Negative control: the fallback must not clobber a real provider value.

    Without this, `test_local_wall_clock_is_recorded...` would still pass if the
    implementation unconditionally overwrote duration_ms with elapsed_ms.
    """
    rec = _record(TurnUsage(input_tokens=10, output_tokens=5, duration_ms=999),
                  elapsed_ms=227589)
    assert rec["duration_ms"] == 999


def test_omitting_elapsed_keeps_old_behaviour():
    """Back-compat: a caller that passes nothing still gets 0, not a crash."""
    rec = _record(TurnUsage(input_tokens=10, output_tokens=5))
    assert rec["duration_ms"] == 0


@pytest.mark.parametrize("junk", ["bogus", None, object()])
def test_non_numeric_elapsed_cannot_break_serialization(junk):
    """A bad value must degrade to 0, never raise into the dispatch path.

    These records are best-effort analytics appended as JSON; a TypeError here
    would surface inside a live turn.
    """
    rec = _record(TurnUsage(input_tokens=1, output_tokens=1), elapsed_ms=junk)
    assert rec["duration_ms"] == 0


# ─────────────────── every call site is wired ───────────────────

# Files that persist a usage row, and how many call sites each owns. A new
# surface MUST be added here deliberately, which is the point: the counts make
# an unnoticed drift fail loudly instead of silently recording zeros.
EXPECTED_SITES = {
    "dashboard/chat_runner.py": 1,
    "dashboard/handlers/hooks.py": 1,
    "slack/gateway.py": 6,          # 2 cron + heartbeat + its timeout
                                    #          + monitor + its timeout
    "task_executor.py": 2,          # task step + self-review
    "subagent.py": 1,
    "workflows/agent_exec.py": 1,
}


def _persist_calls(tree: ast.AST) -> list[ast.Call]:
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name in ("persist_token_record", "persist_token_record_async"):
            out.append(node)
    return out


@pytest.mark.parametrize("rel,count", sorted(EXPECTED_SITES.items()))
def test_every_persist_site_passes_a_measured_duration(rel, count):
    """Structural: no dispatch surface may omit elapsed_ms.

    Deliberately an AST walk over the real source rather than a mock per
    surface — the failure mode being guarded is a FUTURE surface that never
    passes the argument, which no amount of mocking existing ones would catch.
    """
    path = SRC / rel
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = _persist_calls(tree)
    assert len(calls) == count, (
        f"{rel}: expected {count} persist call site(s), found {len(calls)}. "
        "If a surface was added or removed, update EXPECTED_SITES deliberately."
    )
    for call in calls:
        kwargs = {kw.arg for kw in call.keywords if kw.arg}
        assert "elapsed_ms" in kwargs, (
            f"{rel}:{call.lineno} persists a usage row without elapsed_ms, so "
            "its duration_ms will be a literal 0 on the acp provider."
        )


def test_the_structural_check_can_actually_fail():
    """Negative control for the AST walk itself.

    Without this, a bug in _persist_calls that found ZERO calls would make the
    per-file assertions vacuous everywhere.
    """
    tree = ast.parse(
        "async def f():\n"
        "    await persist_token_record_async('k', 'm', ev, surface='x')\n"
    )
    calls = _persist_calls(tree)
    assert len(calls) == 1
    assert "elapsed_ms" not in {kw.arg for kw in calls[0].keywords if kw.arg}


# ───────────────── the dead Stats block is gone ─────────────────

def test_chat_runner_no_longer_builds_the_dead_stats_object():
    """The per-turn Stats() was constructed, incremented, and never read.

    It also fed ``inc_duration_ms(usage.duration_ms)`` — the same structurally
    zero field — so it was doubly dead. Guard against it being reintroduced.
    """
    src = (SRC / "dashboard" / "chat_runner.py").read_text(encoding="utf-8")
    assert "stats = Stats()" not in src
    assert "from kiro_crew.stats import Stats" not in src
