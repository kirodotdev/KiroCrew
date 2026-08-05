"""Workflow surface: per-turn usage row survives, and carries a real duration.

Guards two independent fixes in ``kiro_crew.workflows.agent_exec.build_agent_fn``:

1. **The swallowed-row bug (regression).** The usage-row block used to wrap the
   function-local import, the context-token read, AND the persist in one wide
   ``try/except Exception: logger.debug(...)``. A failure in the *enrichment*
   step (the context read) therefore jumped past the persist and the workflow
   surface wrote **zero** rows. :func:`test_context_read_failure_still_writes_row`
   injects exactly that failure; it FAILS on the old wide try (no row) and passes
   now that the context read is guarded on its own with a ``(0, 0)`` fallback.
2. **The turn wall clock.** acp never assigns ``TurnUsage.duration_ms`` (only the
   removed claude_code provider did), so the row's ``duration_ms`` was a literal
   0. ``agent_fn`` now measures a local monotonic clock and passes it as
   ``elapsed_ms``; the record builder uses it only as a FALLBACK — a
   provider-reported duration still wins.

These tests never touch disk: they patch ``usage._write_token_record`` to
capture the exact record ``_build_token_record`` produced, so the real
duration/precedence logic is exercised with zero shard pollution.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import kiro_crew.dashboard.handlers.usage as usage_mod
import kiro_crew.workflows.agent_exec as agent_exec
from kiro_crew.acp.types import TurnUsage
from kiro_crew.workflows.agent_exec import build_agent_fn

pytestmark = pytest.mark.asyncio


class FakeProvider:
    """Bare provider double — no context/agent accessors, so the real
    ``read_context_tokens`` yields ``(0, 0)`` and ``read_effective_agent`` yields
    ``""`` (falls back to the step's agent)."""

    def __init__(self, key: str = "k") -> None:
        self.key = key


class FakeSessions:
    """Minimal SessionManager double recording releases."""

    def __init__(self) -> None:
        self.released: list[str] = []

    async def get_or_create(self, key, *, agent=None, model=None, cwd=None, **kw):
        return FakeProvider(key), True, False

    def release(self, key, *, cleanup=False):
        self.released.append(key)


class _FakeTime:
    """Stand-in for the ``time`` module in agent_exec (only ``monotonic`` used).

    Patched onto ``agent_exec.time`` alone so agent_fn's turn clock is
    deterministic WITHOUT disturbing asyncio's real event-loop clock.
    """

    def __init__(self, *values: float) -> None:
        self._values = list(values)
        self._i = 0

    def monotonic(self) -> float:
        v = self._values[min(self._i, len(self._values) - 1)]
        self._i += 1
        return v


@pytest.fixture(autouse=True)
def _fast_stream(monkeypatch):
    """stream_and_collect → echo the prompt (no real model / kiro-cli)."""

    async def fake_stream(provider, message, **kw):
        return f"reply:{message}"

    monkeypatch.setattr(agent_exec, "stream_and_collect", fake_stream)


@pytest.fixture
def captured_rows(monkeypatch):
    """Capture every record handed to the shard writer, without disk I/O.

    ``persist_token_record_async`` resolves ``_write_token_record`` as a
    usage-module global at call time (then runs it via ``asyncio.to_thread``), so
    patching the module attribute captures the REAL record ``_build_token_record``
    built — exercising the actual duration precedence — with zero file writes.
    """
    rows: list[dict] = []
    monkeypatch.setattr(
        usage_mod, "_write_token_record", lambda record, now: rows.append(record)
    )
    return rows


async def test_context_read_failure_still_writes_row(captured_rows, monkeypatch):
    """REGRESSION: a failing context-token read must NOT take the whole row down.

    Old wide try: ``read_context_tokens`` shared one ``except Exception`` with the
    persist, so this RuntimeError skipped the persist and the workflow surface
    wrote zero rows. This asserts a row is STILL written (context degraded to
    0/0) — it fails on the old behavior, passes on the narrowed guards.
    """
    boom = MagicMock(side_effect=RuntimeError("context read boom"))
    monkeypatch.setattr(usage_mod, "read_context_tokens", boom)

    sessions = FakeSessions()
    fn = build_agent_fn(
        sessions, run_id="wf_reg", default_agent="researcher", default_model="m1"
    )

    out = await fn("do work", {})

    assert out == "reply:do work"          # the workflow run completed normally
    assert boom.called                     # the failing read path WAS exercised
    assert len(captured_rows) == 1         # ...and a usage row was STILL written
    row = captured_rows[0]
    assert row["surface"] == "workflow"
    assert row["agent"] == "researcher"    # fell back from empty read_effective_agent
    assert row["context_used"] == 0        # degraded to the (0, 0) fallback
    assert row["context_window"] == 0


async def test_row_records_local_wall_clock_when_provider_reports_none(
    captured_rows, monkeypatch
):
    """duration_ms is the locally measured wall clock when the provider reports
    none (the acp case: TurnUsage.duration_ms == 0)."""
    # Deterministic turn clock: t0=1000.0, persist-time=1000.250 → 250 ms.
    monkeypatch.setattr(agent_exec, "time", _FakeTime(1000.0, 1000.25))
    monkeypatch.setattr(
        agent_exec, "provider_last_turn_usage", lambda p: TurnUsage(duration_ms=0)
    )

    sessions = FakeSessions()
    fn = build_agent_fn(sessions, run_id="wf_dur", default_model="m1")

    await fn("hi", {})

    assert len(captured_rows) == 1
    assert captured_rows[0]["duration_ms"] == 250


async def test_provider_reported_duration_wins_over_local_clock(
    captured_rows, monkeypatch
):
    """Negative control: a provider-reported duration WINS — the local clock is a
    fallback, never an override."""
    # The local clock would compute 250 ms; the provider reports 999999.
    monkeypatch.setattr(agent_exec, "time", _FakeTime(1000.0, 1000.25))
    monkeypatch.setattr(
        agent_exec,
        "provider_last_turn_usage",
        lambda p: TurnUsage(duration_ms=999999),
    )

    sessions = FakeSessions()
    fn = build_agent_fn(sessions, run_id="wf_neg", default_model="m1")

    await fn("hi", {})

    assert len(captured_rows) == 1
    assert captured_rows[0]["duration_ms"] == 999999  # provider value, not 250


async def test_persist_failure_does_not_break_workflow_run(monkeypatch):
    """A persist write failure is best-effort: it must never propagate out and
    break the workflow run, and the ephemeral session is still torn down."""

    async def boom(*a, **kw):
        raise RuntimeError("shard write exploded")

    monkeypatch.setattr(usage_mod, "persist_token_record_async", boom)

    sessions = FakeSessions()
    fn = build_agent_fn(sessions, run_id="wf_pf", default_model="m1")

    out = await fn("still works", {})

    assert out == "reply:still works"        # run completed despite the raise
    assert sessions.released == ["wf:wf_pf:0"]  # ephemeral session cleaned up
