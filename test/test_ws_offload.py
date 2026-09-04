"""Regression test for the dashboard WS status-count offload fix.

``src/kiro_crew/dashboard/ws.py`` used to call ``state.crons.list_jobs()`` and
``state.lessons.load_all()`` inline on the event loop inside the periodic WS
status pusher. Both do blocking file I/O, so on a slow/large home dir they
stalled the loop — and with it every other WebSocket/coroutine on the gateway.

The fix routes the lesson count and the cron count through
``asyncio.to_thread`` via ``_load_status_counts``. The lesson count uses
``DashboardState._count_lessons`` — the same JSONL + vector-store total that
``/api/status`` and the SSE updates path report — NOT ``lessons.load_all()``
alone, whose JSONL-only result made the Overview card show 0 on hosts whose
lessons live in the vector store (#7204). Crucially, the cron count
uses ``CronManager.count_enabled_from_disk`` — a pure read-only file parse —
rather than ``list_jobs``: ``list_jobs`` triggers ``_sync()`` → ``_load()`` →
``_arm_timer()`` → ``asyncio.create_task`` which raises ``RuntimeError`` off the
loop thread (and cancels the live cron timer first), silently killing all
scheduled jobs. These tests prove the blocking work runs OFF the event-loop
thread, that the loop stays responsive while it runs, and that the count
includes vector-store lessons.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.ws import (
    _WS_COUNTS_WARN_AFTER_FAILURES,
    _counts_refresh_decision,
    _load_status_counts,
)


@pytest.mark.asyncio
async def test_load_status_counts_runs_off_event_loop_thread():
    """FAILURE SCENARIO (pre-fix): count/load_all ran on the loop thread.

    POST-FIX: both execute in a worker thread (distinct from the loop thread),
    and the counts are returned correctly.
    """
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def blocking_count_enabled_from_disk():
        seen["jobs"] = threading.get_ident()
        time.sleep(0.05)
        return 3  # 3 enabled jobs

    def blocking_count_lessons():
        seen["lessons"] = threading.get_ident()
        time.sleep(0.05)
        return 2  # 2 lessons

    state = SimpleNamespace(
        crons=SimpleNamespace(count_enabled_from_disk=blocking_count_enabled_from_disk),
        _count_lessons=blocking_count_lessons,
    )

    crons, lessons, error = await _load_status_counts(state)  # type: ignore[arg-type]

    assert (crons, lessons, error) == (3, 2, None)
    assert seen["jobs"] != loop_thread, "cron count must run off the event loop"
    assert seen["lessons"] != loop_thread, "_count_lessons must run off the event loop"


@pytest.mark.asyncio
async def test_load_status_counts_does_not_block_the_loop():
    """While the blocking count-load is in flight, an independent coroutine
    keeps making progress — proving the loop is not stalled."""
    ticks = 0

    async def _ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    def slow_count():
        time.sleep(0.1)
        return 1

    state = SimpleNamespace(
        crons=SimpleNamespace(count_enabled_from_disk=slow_count),
        _count_lessons=lambda: 0,
    )

    ticker = asyncio.create_task(_ticker())
    crons, lessons, error = await _load_status_counts(state)  # type: ignore[arg-type]
    # Sampled the instant the load returns, BEFORE the ticker is awaited: reading
    # `ticks` after awaiting it to completion would count the ticks that ran after
    # the load, so the assertion would hold even for a load that blocked the loop
    # outright. The ticker also runs unbounded rather than a fixed 10 iterations,
    # so it cannot reach the floor on its own.
    ticks_during_load = ticks
    ticker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await ticker

    assert (crons, lessons, error) == (1, 0, None)
    # 0.1s of blocking load at a 0.01s tick interval leaves ample room; the floor is
    # low because Windows rounds sleeps up to the ~15.6ms timer tick.
    assert (
        ticks_during_load >= 2
    ), f"event loop appears stalled during blocking load (ticks={ticks_during_load})"


@pytest.mark.asyncio
async def test_ws_lesson_count_includes_vector_store_lessons():
    """FAILURE SCENARIO (pre-fix, #7204): the pusher counted only
    ``lessons.load_all()``, so on a host whose 70 lessons live in the vector
    store (``semantic_memory`` table) with no ``lessons.jsonl`` the WS frame
    reported 0, overriding the correct ``/api/status`` / SSE total in steady
    state.

    POST-FIX: ``_load_status_counts`` routes through the REAL
    ``DashboardState._count_lessons`` (driven here against fake stores), which
    sums the JSONL half (empty) and the vector-store half (70), so the WS
    status path reports the same total as the other two consumers.
    """
    from kiro_crew.dashboard.state import DashboardState

    vector_lessons = [object()] * 70  # reporter's exact repro shape
    state = SimpleNamespace(
        crons=SimpleNamespace(count_enabled_from_disk=lambda: 4),
        lessons=SimpleNamespace(load_all=lambda: []),  # no lessons.jsonl
        context_builder=SimpleNamespace(
            memory=SimpleNamespace(
                vector_store=SimpleNamespace(count_lessons=lambda: len(vector_lessons))
            )
        ),
    )
    # Bind the real counting logic, exactly as the pusher reaches it in prod.
    state._count_lessons = lambda: DashboardState._count_lessons(state)  # type: ignore[arg-type]

    crons, lessons, error = await _load_status_counts(state)  # type: ignore[arg-type]

    assert lessons == 70, "WS status count must include vector-store lessons"
    # Non-zero so a skipped cron branch cannot satisfy this via a falsy default.
    assert crons == 4
    assert error is None
    # ws.py reaches the private method by attribute access; the blanket guard
    # would turn a rename into a permanently silent wrong number, so pin the
    # coupling here to make it break loudly instead.
    assert callable(getattr(DashboardState, "_count_lessons", None))


@pytest.mark.asyncio
async def test_status_count_failure_returns_fallback_and_recovers():
    """FAILURE SCENARIO: a vector-store read raising (sqlite busy, store not
    initialized) would propagate into ``_push_status``'s loop, whose blanket
    ``except`` silently ends the task — that connection then loses ALL status
    frames (version/liveness) until a page reload.

    POST-FIX: each count is guarded independently. A lessons failure returns
    that component's ``fallback`` half WITHOUT discarding the fresh cron
    count, reports the error so the caller can decide (retry cadence,
    warning), and a later attempt recovers the real counts.
    """
    calls = {"n": 0}

    def flaky_count_lessons():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("VectorMemoryStore not initialized")
        return 70

    state = SimpleNamespace(
        crons=SimpleNamespace(count_enabled_from_disk=lambda: 4),
        _count_lessons=flaky_count_lessons,
    )

    # First attempt: lessons raises inside the worker thread. The cached
    # lesson count comes back, the FRESH cron count survives (the failure of
    # one component must not discard the other), and the error names the
    # failing component.
    crons, lessons, error = await _load_status_counts(state, fallback=(9, 55))  # type: ignore[arg-type]
    assert (crons, lessons) == (4, 55), "a failing refresh must fall back per component, not raise"
    assert error is not None and error.startswith("lessons:")

    # Second attempt succeeds: real counts replace the cache, no error.
    crons, lessons, error = await _load_status_counts(state, fallback=(crons, lessons))  # type: ignore[arg-type]
    assert (crons, lessons, error) == (4, 70, None)


@pytest.mark.asyncio
async def test_first_refresh_failure_reports_unknown_not_zero():
    """FAILURE SCENARIO (regression guard): a freshly connected socket whose
    FIRST refresh fails must not publish an authoritative-looking 0 — that is
    the exact false-zero symptom of #7204. The pusher seeds its cache with
    ``None`` (= unknown, rendered as a loading skeleton) and the default
    fallback preserves it.
    """

    def always_fail():
        raise RuntimeError("VectorMemoryStore not initialized")

    state = SimpleNamespace(
        crons=SimpleNamespace(count_enabled_from_disk=lambda: 4),
        _count_lessons=always_fail,
    )

    # Default fallback is (None, None) — the pusher's initial cache.
    crons, lessons, error = await _load_status_counts(state)  # type: ignore[arg-type]
    assert crons == 4, "the independently successful cron count must survive"
    assert lessons is None, "a never-succeeded count must stay unknown, never become 0"
    assert error is not None and error.startswith("lessons:")


def test_counts_refresh_decision_policy():
    """Pins the caller's cache policy (extracted as a pure helper so it is
    testable without driving a WebSocket): success re-arms the TTL and resets
    the streak; a failure retries on the next tick (no TTL stamp) UNTIL the
    streak reaches the threshold, at which point exactly one warning fires and
    the TTL is stamped even on failure — a persistent fault degrades to the
    normal cadence instead of hammering the shared executor every ~5s from
    every connection.
    """
    # Success: stamp, reset, no warning.
    assert _counts_refresh_decision(3, None) == (True, 0, False)

    # Failures below the threshold: fast retry (no stamp), no warning yet.
    failures = 0
    for _ in range(_WS_COUNTS_WARN_AFTER_FAILURES - 1):
        stamp, failures, warn = _counts_refresh_decision(failures, "lessons: boom")
        assert stamp is False
        assert warn is False

    # Threshold failure: back off to TTL cadence and warn exactly once.
    stamp, failures, warn = _counts_refresh_decision(failures, "lessons: boom")
    assert (stamp, warn) == (True, True)
    assert failures == _WS_COUNTS_WARN_AFTER_FAILURES

    # Beyond the threshold: keep the TTL cadence, never warn again this streak.
    stamp, failures, warn = _counts_refresh_decision(failures, "lessons: boom")
    assert (stamp, warn) == (True, False)

    # Recovery resets the streak.
    assert _counts_refresh_decision(failures, None) == (True, 0, False)


@pytest.mark.asyncio
async def test_status_count_cancellation_propagates():
    """``asyncio.CancelledError`` is a ``BaseException`` on py3.8+, so the
    ``except Exception`` guards must NOT convert a task cancellation into a
    fallback value. Nothing else pins this: a future ``except BaseException``
    (or explicitly adding ``CancelledError``) would silently break the
    pusher's shutdown path.
    """
    release = threading.Event()

    def stuck_count():
        # No timeout: the only way this returns is the test releasing it, so
        # there is no timing-dependent path where the coroutine completes
        # normally and the cancellation assertion turns into a flake.
        release.wait()
        return 1

    state = SimpleNamespace(
        crons=SimpleNamespace(count_enabled_from_disk=stuck_count),
        _count_lessons=lambda: 0,
    )

    task = asyncio.create_task(_load_status_counts(state))  # type: ignore[arg-type]
    try:
        await asyncio.sleep(0.05)  # let it enter the worker-thread wait
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        # Always unblock the worker thread so a failing assertion can never
        # wedge the test run behind a 30s executor-shutdown wait.
        release.set()


def test_warn_counts_failure_first_warning_survives_early_boot(caplog, monkeypatch):
    """FAILURE SCENARIO: with a ``0.0`` "never warned" sentinel,
    ``time.monotonic()`` (time since host boot) is itself < the 600s rate
    limit for the first 10 minutes after boot, so an autostarted gateway
    hitting a bad store would swallow the streak's ONLY warning (the decision
    helper warns at exact equality) — permanent silence at default log level.

    POST-FIX: the latch is ``None`` until the first warning, so the first
    warning always fires; a second within the interval is suppressed.
    """
    from kiro_crew.dashboard import ws as ws_module

    # Module-level global — monkeypatch restores it even on early return.
    monkeypatch.setattr(ws_module, "_last_counts_warn_monotonic", None)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.ws"):
        ws_module._warn_counts_failure(6, "lessons: RuntimeError")
        assert any(
            "6 consecutive refreshes" in r.message for r in caplog.records
        ), "the FIRST warning must fire regardless of how small monotonic time is"
        caplog.clear()
        # Within the rate-limit interval: suppressed gateway-wide.
        ws_module._warn_counts_failure(6, "lessons: RuntimeError")
        assert not caplog.records


@pytest.mark.asyncio
async def test_gateway_wide_refresh_is_single_touch_per_ttl(monkeypatch):
    """FAILURE SCENARIO: per-connection caches make every open socket an
    independent contender on the vector store's shared sqlite lock (a
    busy-timeout read holds it for seconds while loop-thread readers block).

    POST-FIX: the cache is gateway-wide — a second caller within the TTL gets
    the cached tuple back without touching the store — and a successful
    refresh resets the warn latch so the NEXT outage warns again.
    """
    from kiro_crew.dashboard import ws as ws_module

    monkeypatch.setattr(ws_module, "_counts_cache", (None, None))
    monkeypatch.setattr(ws_module, "_counts_cache_ts", float("-inf"))
    monkeypatch.setattr(ws_module, "_counts_cache_failures", 0)
    monkeypatch.setattr(ws_module, "_counts_refresh_inflight", False)
    monkeypatch.setattr(ws_module, "_last_counts_warn_monotonic", 123.0)

    touches = {"n": 0}

    def counted_lessons():
        touches["n"] += 1
        return 70

    state = SimpleNamespace(
        crons=SimpleNamespace(count_enabled_from_disk=lambda: 4),
        _count_lessons=counted_lessons,
    )

    assert await ws_module._refresh_status_counts(state) == (4, 70)  # type: ignore[arg-type]
    assert touches["n"] == 1
    # Success resets the warn latch so a future streak warns again.
    assert ws_module._last_counts_warn_monotonic is None

    # Second caller within the TTL: cache hit, store untouched.
    assert await ws_module._refresh_status_counts(state) == (4, 70)  # type: ignore[arg-type]
    assert touches["n"] == 1, "a second socket's tick must not touch the store within the TTL"


def test_status_frame_publishes_null_for_unknown_counts():
    """Pins the frame emission itself — the load-bearing half of #7204. The
    sentinel 0 passed to ``status_snapshot`` (to suppress its inline on-loop
    default) must be OVERWRITTEN by the true cached values, so an
    unknown-lessons frame carries ``None`` (rendered as a skeleton) while the
    independently known cron count ships as a real number. Deleting either
    overwrite key silently restores the authoritative false 0.
    """
    from kiro_crew.dashboard.ws import _status_frame

    seen_kwargs: dict = {}

    def fake_snapshot(**kwargs):
        seen_kwargs.update(kwargs)
        return {"cron_jobs": kwargs["cron_jobs"], "lessons": kwargs["lessons"], "sessions": 1}

    state = SimpleNamespace(status_snapshot=fake_snapshot)

    frame = _status_frame(state, crons=4, lessons=None)  # type: ignore[arg-type]

    # The sentinel suppressed the inline default...
    assert seen_kwargs["lessons"] == 0
    # ...and the overwrite published the honest unknown, not the sentinel.
    assert frame["lessons"] is None
    assert frame["cron_jobs"] == 4
    assert "version" in frame and "platform" in frame
