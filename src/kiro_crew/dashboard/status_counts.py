from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

# One gateway-wide cache shared by all THREE status emitters — the WebSocket
# status pusher (``ws._status_frame``), ``/api/status`` (``handlers_system``),
# and the SSE ``dashboard`` event (``handlers/updates``). Each of those emits
# ``status_snapshot``, whose lesson/cron defaults compute INLINE on the event
# loop (JSONL ``stat``/``read_text`` plus a SQLite ``COUNT(*)`` under the vector
# store's shared connection lock, and a ``crons.json`` parse). Running that on
# the loop is exactly the freeze class ``no-blocking-call-on-event-loop`` guards
# against. Housing the cache here — instead of one per emitter — keeps the
# emitters from becoming INDEPENDENT contenders on that single sqlite lock: a
# per-caller cache would let each of the WS pusher, an ``/api/status`` poll, and
# every open SSE tab touch the store on its own schedule, so a busy-timeout read
# holding ``_db_lock`` for seconds would block loop-thread readers
# (``get_semantic_context`` et al.) uninstrumented. One cache, one refresh per
# TTL, however many emitters and sockets are live.
_WS_COUNTS_CACHE_TTL = 30  # seconds between refreshing lesson/cron counts
# Consecutive failed count refreshes before (a) backing off to the normal TTL
# cadence and (b) one operator-visible warning: failures retry every pusher
# tick (~5s), so 6 ≈ 30s of sustained failure — long enough to skip transient
# sqlite busy-timeouts, short enough that a store that never initializes
# surfaces the same minute it happens.
_WS_COUNTS_WARN_AFTER_FAILURES = 6
# Gateway-wide floor between count-failure warnings: the fault is global (one
# store), so N open sockets must not emit N identical warnings per streak.
# None = never warned — NOT 0.0, which time.monotonic() (time since boot) is
# still within for 10 minutes after host boot, and which would swallow the
# streak's only warning exactly when autostarted gateways hit a bad store.
# Reset to None on a successful refresh so each NEW streak warns again.
_WS_COUNTS_WARN_INTERVAL_SECS = 600.0
_last_counts_warn_monotonic: float | None = None

# Gateway-wide count cache: ONE store touch per TTL no matter how many sockets
# are open. Per-connection caches would make every dashboard tab an
# independent contender on the vector store's shared sqlite connection, whose
# _db_lock a busy-timeout read can hold for seconds while loop-thread readers
# (get_semantic_context et al.) block on it uninstrumented — N tabs polling
# independently is exactly the loop-freeze class no-blocking-call-on-event-loop
# exists to prevent. All four cells are only ever read/written from the event
# loop thread, so no lock is needed; the in-flight flag makes the refresh
# single-flight (a second socket's tick returns the stale cache immediately
# instead of piling a duplicate store read onto the executor).
_counts_cache: tuple[int | None, int | None] = (None, None)
_counts_cache_ts: float = float("-inf")
_counts_cache_failures: int = 0
_counts_refresh_inflight: bool = False


def _counts_refresh_decision(failures: int, error: str | None) -> tuple[bool, int, bool]:
    """Pure decision after one count-refresh attempt: ``(stamp_ttl, failures, warn)``.

    Success (``error is None``) re-arms the cache TTL and resets the streak. A
    failure leaves the TTL un-stamped so the next pusher tick (~5s) retries —
    fast recovery for TRANSIENT faults — but once the streak reaches
    ``_WS_COUNTS_WARN_AFTER_FAILURES`` the TTL is stamped even on failure,
    degrading a PERSISTENT fault to the normal 30s cadence instead of
    hammering the store and the shared default executor every tick. ``warn``
    is True exactly once per streak, at the threshold. Extracted as a pure
    function so the cache policy is testable without driving a WebSocket.
    """
    if error is None:
        return True, 0, False
    failures += 1
    return (
        failures >= _WS_COUNTS_WARN_AFTER_FAILURES,
        failures,
        failures == _WS_COUNTS_WARN_AFTER_FAILURES,
    )


def _warn_counts_failure(failures: int, error: str | None) -> None:
    """Log one operator-visible warning per streak, rate-limited gateway-wide.

    The per-attempt causes are logged at debug; without this line a permanent
    fault (store never initializes) would pin cached/unknown counts forever
    with no trace at default log level. Module-level latch (the event loop is
    single-threaded, so no lock) keeps repeat streaks within the interval from
    spamming; a successful refresh clears the latch so the NEXT streak warns.
    """
    global _last_counts_warn_monotonic
    now = time.monotonic()
    if (
        _last_counts_warn_monotonic is not None
        and now - _last_counts_warn_monotonic < _WS_COUNTS_WARN_INTERVAL_SECS
    ):
        return
    _last_counts_warn_monotonic = now
    logger.warning(
        "ws: status counts failed %d consecutive refreshes (%s); "
        "serving cached values, retrying at the normal cadence",
        failures,
        error,
    )


async def _load_status_counts(
    state: DashboardState, *, fallback: tuple[int | None, int | None] = (None, None)
) -> tuple[int | None, int | None, str | None]:
    """Return ``(cron_count, lesson_count, error)`` loaded OFF the event loop.

    ``DashboardState._count_lessons()`` performs blocking I/O on two stores:
    the JSONL file (``stat()`` + ``read_text()`` via ``load_all``) PLUS a
    SQLite ``COUNT(*)`` via ``VectorMemoryStore.count_lessons`` (serialized on
    the shared connection through ``_fetch_all_locked``, documented as
    executor-thread safe). The cron count comes from a direct read-only parse
    of ``crons.json`` (``count_enabled_from_disk``). The WS status pusher runs
    on the event loop, so computing these inline would stall the loop — and
    with it EVERY other WebSocket / coroutine on the gateway — for the
    duration of that disk latency (seconds on a slow/large home dir or a
    contended NFS mount). Offload both to a worker thread so the loop stays
    responsive; the pusher is a periodic background task, so the extra thread
    hop is free.

    The lesson count MUST come from ``_count_lessons`` (JSONL + vector store),
    the same total ``/api/status`` and the SSE updates path report via
    ``status_snapshot``'s default. Counting only ``lessons.load_all()`` here
    would let the pusher's cached value override the correct default with
    the JSONL-only half, so the Overview card would show 0 on hosts whose
    lessons live in the vector store.

    Each count is guarded INDEPENDENTLY: on failure that component falls back
    to its ``fallback`` half while the other keeps its fresh value — the
    vector-store read can surface ``sqlite3.OperationalError`` (busy timeout,
    disk I/O) or ``RuntimeError`` (store not initialized), and an exception
    escaping into ``_push_status``'s loop would silently end that connection's
    status frames, losing the version/liveness signal until a page reload. A
    lessons failure must not also discard a successfully-read cron count.
    ``None`` means UNKNOWN, never 0: the pusher seeds its cache with ``None``
    so a component that has never refreshed successfully is published as
    ``null`` (the dashboard renders a loading skeleton) instead of an
    authoritative-looking 0. ``error``
    joins each failed component's exception TYPE name (``None`` on full
    success) so the operator-visible warning can name the cause without
    leaking store paths (``str(OSError)`` embeds its filename); it never
    enters the WS frame. The guards catch ``Exception`` only, so
    ``asyncio.CancelledError`` (a ``BaseException``) propagates out of THIS
    helper uncaught — that is this function's contract; the pusher's own
    outer handler decides its task's teardown semantics.

    NOTE: this deliberately uses ``count_enabled_from_disk`` rather than
    ``list_jobs``. ``list_jobs`` is cache-only: it returns the in-memory
    snapshot the loop-side timer refreshes, so a count taken from it lags a
    write made by another process (the CLI or an MCP tool) by up to a timer
    tick, and it is documented as a loop-side call. ``count_enabled_from_disk``
    parses ``crons.json`` directly, never mutates loop-owned state or the timer,
    and so is the read that is both current across processes and safe from a
    worker thread.
    """
    errors: list[str] = []
    try:
        crons: int | None = await asyncio.to_thread(state.crons.count_enabled_from_disk)
    except Exception as exc:
        logger.debug("ws: cron count refresh failed; keeping cached count", exc_info=True)
        # Exception TYPE only: str()/repr() of an OSError embeds the absolute
        # store path (the operator's username) via its filename attribute, and
        # this string reaches logger.warning at default level. The full
        # traceback is already in the debug log above. Never the WS frame.
        crons = fallback[0]
        errors.append(f"crons: {type(exc).__name__}")
    try:
        lessons: int | None = await asyncio.to_thread(state._count_lessons)
    except Exception as exc:
        logger.debug("ws: lesson count refresh failed; keeping cached count", exc_info=True)
        lessons = fallback[1]
        errors.append(f"lessons: {type(exc).__name__}")
    return crons, lessons, "; ".join(errors) or None


async def _refresh_status_counts(state: DashboardState) -> tuple[int | None, int | None]:
    """Return the gateway-wide cached counts, refreshing at most once per TTL.

    Callable every pusher tick from every connection: it returns the shared
    cache immediately unless this call is the one that finds it stale (and no
    refresh is already in flight), in which case it awaits one off-loop load
    and applies ``_counts_refresh_decision``. Single-flight + shared cache =
    one store touch per TTL for the whole gateway, however many sockets are
    open, and no per-socket count divergence.
    """
    global _counts_cache, _counts_cache_ts, _counts_cache_failures
    global _counts_refresh_inflight, _last_counts_warn_monotonic
    now = time.monotonic()
    if _counts_refresh_inflight or now - _counts_cache_ts < _WS_COUNTS_CACHE_TTL:
        return _counts_cache
    _counts_refresh_inflight = True
    try:
        crons, lessons, error = await _load_status_counts(state, fallback=_counts_cache)
        _counts_cache = (crons, lessons)
        stamp, _counts_cache_failures, warn = _counts_refresh_decision(
            _counts_cache_failures, error
        )
        if stamp:
            _counts_cache_ts = now
        if error is None:
            # New streaks warn again: the rate-limit floor is for repeats
            # WITHIN one streak, not for distinct outages.
            _last_counts_warn_monotonic = None
        elif warn:
            _warn_counts_failure(_counts_cache_failures, error)
        return _counts_cache
    finally:
        _counts_refresh_inflight = False


async def cached_status_snapshot(state: DashboardState) -> dict[str, Any]:
    """Build a status snapshot whose lesson/cron counts come from the shared cache.

    The single funnel all three status emitters use, and the one place the
    update fields and the cached counts are joined — so no emitter can forget
    either. None of the emitters computes ``lesson``/``cron`` counts inline on
    the event loop: this awaits the gateway-wide ``_refresh_status_counts``
    (one off-loop store touch per TTL), reads the update fields from the shared
    ``status_update_fields`` reader itself, and passes both straight into
    ``status_snapshot``, which emits them verbatim. An unknown count is ``None``
    end to end, so it ships as ``null`` (the dashboard renders a loading
    skeleton) rather than an authoritative false 0. ``ws._status_frame`` was the
    only emitter with this shape before; now all three go through here.
    """
    # Lazy import: handlers/updates.py imports this module at module level, so
    # a module-level import here would form a cycle.
    from kiro_crew.dashboard.handlers.updates import status_update_fields

    crons, lessons = await _refresh_status_counts(state)
    # ``status_update_fields()`` is typed ``dict[str, object]``; spreading it
    # into the keyword-only ``status_snapshot`` signature is sound at runtime
    # (every key is a real parameter —
    # test_dashboard_status_snapshot.test_snapshot_accepts_every_shared_reader_field
    # pins that) but opaque to mypy.
    return state.status_snapshot(
        cron_jobs=crons,
        lessons=lessons,
        **status_update_fields(),  # type: ignore[arg-type]
    )
