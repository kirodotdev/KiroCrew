"""Turn wall-clock for the subagent dispatch surface (``subagent.py``).

The record builder falls back to a caller-measured ``elapsed_ms`` for
``duration_ms`` because the acp provider always reports
``TurnUsage.duration_ms == 0`` (nothing assigns it). These tests pin the
SUBAGENT surface's half of that contract: ``_run_inner`` measures its OWN turn
wall clock (``_turn_t0``, started at the subagent's own stream — never the
parent's under session sharing) and passes it as ``elapsed_ms`` to
``persist_token_record_async``.

Two tests, the second a negative control so the first cannot pass vacuously:

1. provider reports no duration  -> the row records the local wall clock (> 0).
2. provider reports a duration   -> that value WINS; the local clock is ignored.

Capture happens at ``usage._write_token_record`` so the REAL
``persist_token_record_async`` and ``_build_token_record`` run — the assertions
see the genuine ``duration_ms or elapsed_ms`` precedence, not a re-implemented
copy of it. Subagent-registry and ``KIROCREW_HOME`` isolation come from the
autouse conftest fixtures.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.types import TurnUsage
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.subagent import SubagentManager

# Implausibly large: no unit-test turn runs ~16 minutes, so a row carrying this
# value can only have come from the provider, never from the local wall clock.
_PROVIDER_DURATION_MS = 987654

# Real time forced to pass inside the turn so the local clock is measurably
# non-zero. asyncio.sleep floors the elapsed wall time at >= this many seconds.
_TURN_SLEEP_SECS = 0.03


def _text_event(text: str) -> SimpleNamespace:
    return SimpleNamespace(kind=EVENT_TEXT_CHUNK, text=text)


def _complete_event(usage: TurnUsage | None = None) -> SimpleNamespace:
    """An EVENT_COMPLETE. With no ``usage`` it mirrors the real acp stream,
    where nothing assigns ``duration_ms`` and the row must fall back to the
    caller's ``elapsed_ms``."""
    ev = SimpleNamespace(kind=EVENT_COMPLETE, stop_reason="end_turn")
    if usage is not None:
        ev.usage = usage
    return ev


def _mock_sessions(stream_factory) -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0
    provider.stream = MagicMock(side_effect=stream_factory)
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.has_session = MagicMock(return_value=True)
    sessions._provider = provider
    return sessions


def _mock_ctx_builder() -> MagicMock:
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    ctx.hooks.auto_approve_subagent_tools = False
    return ctx


def _manager(sessions: MagicMock) -> SubagentManager:
    mgr = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder())
    # Force the dedicated-process path (deterministic under MagicMock sessions).
    # The clock lives in _run_inner, which is entered once per subagent turn on
    # BOTH paths, so this exercises the same measurement the shared path uses.
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    return mgr


async def _spawn_and_capture(stream_factory) -> list[dict]:
    """Drive one subagent turn and return the token records it persisted.

    The three ``read_*`` helpers are pinned so nothing depends on the mock
    provider's internals; only ``_write_token_record`` is replaced (to capture),
    leaving the real persist + record-builder precedence under test.
    """
    captured: list[dict] = []
    mgr = _manager(_mock_sessions(stream_factory))
    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch(
            "kiro_crew.dashboard.handlers.usage._write_token_record",
            side_effect=lambda record, now: captured.append(record),
        ),
        patch(
            "kiro_crew.dashboard.handlers.usage.read_context_tokens",
            return_value=(0, 0),
        ),
        patch(
            "kiro_crew.dashboard.handlers.usage.read_effective_agent",
            return_value="kirocrew",
        ),
        patch(
            "kiro_crew.dashboard.handlers.usage.read_effective_model",
            return_value="claude-opus-5",
        ),
    ):
        info = mgr.spawn("measure my turn")
        assert info is not None
        await mgr._tasks[info.id]
    return captured


@pytest.mark.asyncio
async def test_subagent_turn_records_local_wall_clock():
    """acp reports no duration -> the subagent row records its OWN wall clock."""

    def stream_factory(msg, *a, **kw):
        async def _gen():
            yield _text_event("working ")
            # Guarantee measurable wall time between _turn_t0 and the persist.
            await asyncio.sleep(_TURN_SLEEP_SECS)
            yield _complete_event()  # no usage -> duration_ms unset (acp shape)

        return _gen()

    # Bracket the whole spawn so we have an OUTER wall-clock window that strictly
    # encloses the subagent's internal one (_turn_t0 at stream start -> persist).
    observed_start = time.monotonic()
    records = await _spawn_and_capture(stream_factory)
    observed_elapsed_ms = (time.monotonic() - observed_start) * 1000

    assert len(records) == 1
    rec = records[0]
    assert rec["surface"] == "subagent"
    # The fallback fired: a real, positive local measurement, not the literal 0
    # the provider-only read used to write into every row. Bound it as
    # 0 < duration_ms <= observed rather than with a fixed floor. The lower
    # bound (> 0) proves the clock advanced; the upper bound ties it to a real
    # measurement (a bug writing an arbitrary constant would exceed the window
    # that encloses it). Both are race-free: the internal window is a subset of
    # the outer one on any platform, unlike a fixed floor, which races Windows'
    # ~15.6 ms timer quantum when a 30 ms sleep rounds to one tick (~15 ms) (see
    # testing-conventions.md, Determinism class 2, wall-clock races).
    assert 0 < rec["duration_ms"] <= observed_elapsed_ms


@pytest.mark.asyncio
async def test_provider_reported_duration_wins_over_local_clock():
    """Negative control: a provider-reported duration WINS and the local clock
    is ignored — so the positive test cannot pass vacuously. A broken "always
    write elapsed_ms" implementation would record ~30ms here, not the provider
    value."""

    def stream_factory(msg, *a, **kw):
        async def _gen():
            yield _text_event("working ")
            # Local clock would read ~30ms here; the row must NOT use it.
            await asyncio.sleep(_TURN_SLEEP_SECS)
            yield _complete_event(TurnUsage(duration_ms=_PROVIDER_DURATION_MS))

        return _gen()

    records = await _spawn_and_capture(stream_factory)

    assert len(records) == 1
    rec = records[0]
    assert rec["surface"] == "subagent"
    # Provider value recorded verbatim — NOT the ~30ms local measurement.
    assert rec["duration_ms"] == _PROVIDER_DURATION_MS
