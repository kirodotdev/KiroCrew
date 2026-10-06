"""Bounded waits on a signal, for tests: rules D1, D7 and D8 of the Determinism contract.

``docs/system-specs/common/testing-conventions.md`` § Determinism contract asks
a test to wait on the state it is about to assert, under a deadline that RAISES
quoting the last state it read. Every helper here does that and never returns
``False``: a barrier that returns on a state that never happened hands the next
assertion a consequence to fail on, and the failure then names the wrong thing.

The deadline is a lost-run bound, not the barrier. By default it is half of the
running test's timeout (:func:`default_timeout`), so a wait that never succeeds
fails by name before pytest-timeout kills the worker and costs the whole run.

The helpers read the REAL clock: they capture ``time.monotonic`` and the sleeps
at import, so a test that installs a fake clock on its subject does not reach
the deadline that bounds its own wait. Import this module at module level: a
test that patches the stdlib clock and then imports it for the first time
captures the fake (the rootdir conftest puts the session's originals back at
each test's setup, which covers this repository's suites). Besides the standard library the module
imports only ``kiro_crew.loop_lock``; it never imports pytest.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from typing import Any, Awaitable, Callable, TypeVar

from kiro_crew.loop_lock import LoopBoundLock

__all__ = [
    "DEFAULT_INTERVAL_SECS",
    "DEFAULT_PROGRESS_WINDOW_SECS",
    "FALLBACK_TIMEOUT_SECS",
    "async_wait_until",
    "default_timeout",
    "drained_progress",
    "runner_timeout_secs",
    "until_parked",
    "wait_until",
]

#: Half the suite's per-test ``--timeout=120``: the deadline used when no runner
#: has published a timeout for the running test.
FALLBACK_TIMEOUT_SECS = 60.0

#: How often a condition is re-read: the ``_await_parked`` poll in
#: ``test/test_runloop_integration.py``.
DEFAULT_INTERVAL_SECS = 0.01

#: How long :func:`drained_progress` lets a counter stand still before it calls
#: the producer stuck: ``_NO_PROGRESS_SECONDS`` in
#: ``test/test_crew_log_edge_concurrency.py``.
DEFAULT_PROGRESS_WINDOW_SECS = 10.0

#: Share of a wait's timeout that one evaluation still gets once the deadline has
#: passed, and that ``describe()`` gets off the loop. The read at the deadline must
#: be a real read, not one cut to the poll interval; a predicate that never returns
#: still fails by name, at most half the timeout late.
_EVALUATION_FLOOR_SHARE = 0.25

#: The running test's timeout in seconds, published by the runner's glue (the
#: rootdir ``conftest.py`` sets it at each test's setup and clears it after
#: teardown). ``None`` means no timeout is known. A downstream suite without
#: that glue may set it through ``monkeypatch``.
runner_timeout_secs: float | None = None

_monotonic = time.monotonic
_sleep = time.sleep
_asyncio_sleep = asyncio.sleep

_T = TypeVar("_T")


def default_timeout() -> float:
    """Half the running test's timeout, else :data:`FALLBACK_TIMEOUT_SECS`.

    Half, so the wait fails as a readable assertion with time left for teardown
    and the message, rather than at the runner's kill.
    """
    budget = runner_timeout_secs
    if budget is None or not (isinstance(budget, (int, float)) and math.isfinite(budget)):
        return FALLBACK_TIMEOUT_SECS
    if budget <= 0:
        return FALLBACK_TIMEOUT_SECS
    return budget / 2


def _resolve_timeout(timeout: float | None) -> float:
    if timeout is None:
        return default_timeout()
    _check_seconds(timeout, "timeout")
    return float(timeout)


def _check_seconds(value: float, what: str, *, positive: bool = False) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{what} must be a number of seconds, not {type(value).__name__}")
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        bound = "positive" if positive else "non-negative"
        raise ValueError(f"{what} must be {bound} and finite, got {value!r}")


def _check_ignoring(ignoring: tuple[type[BaseException], ...]) -> None:
    for kind in ignoring:
        if not (isinstance(kind, type) and issubclass(kind, Exception)):
            raise TypeError(
                f"ignoring takes Exception subclasses only, got {kind!r}: a cancellation "
                "or a runner interrupt must always propagate"
            )


def _refuse_running_loop(helper: str) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        f"{helper}() blocks, and it was called on a thread running an event loop, so "
        "the producer it waits for could never run. Await async_wait_until() instead."
    )


def _name(predicate: Callable[..., Any]) -> str:
    return getattr(predicate, "__qualname__", None) or repr(predicate)


def _read_describe(describe: Callable[[], object]) -> str:
    try:
        return str(describe())
    except Exception as exc:  # the timeout is the failure; never mask it
        return f"describe() raised {exc!r}"


def _refuse_awaitable(value: object, predicate: Callable[..., Any], helper: str) -> None:
    """A blocking helper handed something that returns an awaitable would pass at once."""
    if inspect.isawaitable(value):
        if inspect.iscoroutine(value):
            value.close()
        raise TypeError(
            f"{_name(predicate)} returned an awaitable, which {helper}() would read as true "
            "at once; await async_wait_until() instead"
        )


def _failure(
    helper: str,
    predicate: Callable[..., Any],
    elapsed: float,
    timeout: float,
    polls: int,
    state: str,
) -> AssertionError:
    message = (
        f"{helper}({_name(predicate)}) still false after {elapsed:.2f}s "
        f"(timeout {timeout:.2f}s, {polls} polls)"
    )
    if state:
        message += f"; last state: {state}"
    return AssertionError(message)


def wait_until(
    predicate: Callable[[], _T],
    *,
    timeout: float | None = None,
    interval: float = DEFAULT_INTERVAL_SECS,
    describe: Callable[[], object] = lambda: "",
    ignoring: tuple[type[Exception], ...] = (),
) -> _T:
    """Poll ``predicate`` until it returns a truthy value, and return that value.

    The predicate is read at least once, and once more at or after the
    deadline, so a condition that becomes true exactly at the deadline still
    passes. On timeout it raises ``AssertionError`` naming the predicate, the
    elapsed time, the number of polls and ``describe()`` -- the state the test
    needs to see. An exception in ``ignoring`` counts as "not yet"; the last one
    becomes the error's ``__cause__``. Anything else propagates at once.

    A falsy-but-valid result (``0``, ``""``) keeps it waiting: wrap such a
    predicate as ``lambda: f() is not None``.

    Refused on a thread running an event loop (``RuntimeError``), where it
    would block the very producer it waits for, and for a predicate that returns
    an awaitable (``TypeError``), which would read as true at once. A predicate
    that blocks is not interrupted: the deadline is checked between reads, so
    keep each read short (D8).
    """
    _refuse_running_loop("wait_until")
    if inspect.iscoroutinefunction(predicate):
        raise TypeError(f"{_name(predicate)} is a coroutine function; await async_wait_until()")
    budget = _resolve_timeout(timeout)
    _check_seconds(interval, "interval", positive=True)
    _check_ignoring(ignoring)
    start = _monotonic()
    deadline = start + budget
    polls = 0
    cause: Exception | None = None
    while True:
        polls += 1
        try:
            value = predicate()
        except ignoring as exc:
            cause = exc
        else:
            _refuse_awaitable(value, predicate, "wait_until")
            if value:
                return value
        now = _monotonic()
        if now >= deadline:
            break
        _sleep(min(interval, deadline - now))
    error = _failure(
        "wait_until", predicate, _monotonic() - start, budget, polls, _read_describe(describe)
    )
    raise error from cause


async def async_wait_until(
    predicate: Callable[[], Any],
    *,
    timeout: float | None = None,
    interval: float = DEFAULT_INTERVAL_SECS,
    describe: Callable[[], object] = lambda: "",
    ignoring: tuple[type[Exception], ...] = (),
    off_loop: bool = False,
) -> Any:
    """The awaitable form of :func:`wait_until`, with the same contract.

    A predicate that returns an awaitable is awaited. ``off_loop=True`` runs the
    predicate and ``describe`` through ``asyncio.to_thread``: use it for a
    predicate that touches a store, a file or a lock, which must never run on
    the loop, and pass a plain function (one that returns an awaitable is a
    ``TypeError``). Leave it ``False`` for a predicate over loop-confined state
    (asyncio primitives are not thread-safe). Every evaluation is bounded by
    the remaining deadline, or by ``max(interval, timeout / 4)`` for the read at
    the deadline, and one still running then fails the test by name; the worker
    thread itself is not killed. So the worst case is ``timeout + 2 *
    max(interval, timeout / 4)`` (the deadline, the read at it, and
    ``describe()`` off the loop): 1.5 times the timeout whenever the interval is
    at most a quarter of it, which the default timeout keeps at three quarters
    of the test's timeout. A predicate that blocks ON the loop cannot be
    bounded, so never pass one (D8).
    """
    budget = _resolve_timeout(timeout)
    _check_seconds(interval, "interval", positive=True)
    _check_ignoring(ignoring)
    if off_loop and inspect.iscoroutinefunction(predicate):
        raise TypeError("off_loop=True runs the predicate on a thread; pass a plain function")
    start = _monotonic()
    deadline = start + budget
    floor = max(interval, budget * _EVALUATION_FLOOR_SHARE)
    polls = 0
    cause: Exception | None = None
    still_running = False
    while True:
        polls += 1
        bound = max(deadline - _monotonic(), floor)
        try:
            value = await _evaluate(predicate, off_loop, bound)
        except _StillRunning:
            still_running = True
            break
        except ignoring as exc:
            cause = exc
        else:
            if value:
                return value
        now = _monotonic()
        if now >= deadline:
            break
        await _asyncio_sleep(min(interval, deadline - now))
    if off_loop:
        try:
            state = await _bounded(asyncio.to_thread(_read_describe, describe), floor)
        except _StillRunning:
            state = f"describe() was still running after {floor:.2f}s"
    else:
        state = _read_describe(describe)
    elapsed = _monotonic() - start
    if still_running:
        raise AssertionError(
            f"async_wait_until({_name(predicate)}): an evaluation was still running after "
            f"{elapsed:.2f}s (timeout {budget:.2f}s, {polls} polls); a wait's own reads must "
            f"return so it can fail by name; last state: {state}"
        )
    error = _failure("async_wait_until", predicate, elapsed, budget, polls, state)
    raise error from cause


async def _evaluate(predicate: Callable[[], Any], off_loop: bool, bound: float) -> Any:
    if off_loop:
        value = await _bounded(asyncio.to_thread(predicate), bound, predicate)
        if inspect.isawaitable(value):
            if inspect.iscoroutine(value):
                value.close()
            raise TypeError(
                f"{_name(predicate)} returned an awaitable on a worker thread; with "
                "off_loop=True pass a plain function that returns the condition"
            )
        return value
    value = predicate()
    if inspect.isawaitable(value):
        return await _bounded(value, bound, predicate)
    return value


class _StillRunning(Exception):
    """An evaluation outlived its bound; the caller reports it with the wait's state."""


async def _bounded(
    awaitable: Awaitable[_T], bound: float, predicate: Callable[..., Any] | None = None
) -> _T:
    """Await ``awaitable`` for at most ``bound`` seconds, raising ``_StillRunning`` past it.

    Waits on the task rather than through ``wait_for``, so a ``TimeoutError``
    the predicate itself raises reaches the caller (and its ``ignoring``) as
    the predicate's own error, never as this bound expiring.
    """
    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait({task}, timeout=bound)
    if task in done:
        return task.result()
    task.cancel()
    raise _StillRunning()


def _waiters_of(primitive: object, loop: asyncio.AbstractEventLoop) -> int:
    """How many waiters are parked on ``primitive`` right now, not counting woken ones."""
    if isinstance(primitive, asyncio.Barrier):
        return int(primitive.n_waiting)
    if isinstance(primitive, (asyncio.Lock, asyncio.Semaphore, asyncio.Event, asyncio.Condition)):
        if not hasattr(primitive, "_waiters"):
            raise TypeError(
                f"{type(primitive).__name__} has no _waiters on this Python, so its parked "
                "waiters cannot be read; until_parked needs updating for this version"
            )
        return _pending(getattr(primitive, "_waiters"))
    if isinstance(primitive, LoopBoundLock):
        # Read the loop's lock without creating one: _bound() would create it
        # and can warn on a second live loop.
        with primitive._guard:
            bound_lock = primitive._locks.get(loop)
        if bound_lock is None:
            return 0
        return _pending(bound_lock._waiters)
    raise TypeError(
        "until_parked() reads asyncio.Lock, Semaphore, BoundedSemaphore, Event, Condition, "
        f"Barrier and kiro_crew.loop_lock.LoopBoundLock, not {type(primitive).__name__}"
    )


def _pending(waiters: Any) -> int:
    # A woken or cancelled waiter stays in the deque until its own ``finally``
    # removes it, so only a future that is not done is still parked.
    return sum(1 for fut in (waiters or ()) if not fut.done())


async def until_parked(
    primitive: object,
    *,
    count: int = 1,
    timeout: float | None = None,
    interval: float = DEFAULT_INTERVAL_SECS,
    describe: Callable[[], object] = lambda: "",
) -> int:
    """Wait until at least ``count`` tasks are parked on ``primitive``; return how many.

    This is the barrier before an absence assertion ("the second caller is
    still waiting"): the waiter is observed parked first, so the negative
    cannot pass because the racer never got there. Reads ``asyncio.Lock``,
    ``Semaphore`` / ``BoundedSemaphore``, ``Event``, ``Condition`` (tasks inside
    ``wait()``; a task still acquiring the condition's lock is not counted),
    ``Barrier`` and ``kiro_crew.loop_lock.LoopBoundLock``. Anything else is a
    ``TypeError``.

    Await it on the loop that owns the primitive (``RuntimeError`` otherwise).
    For a loop on another thread, run it there with
    ``asyncio.run_coroutine_threadsafe(until_parked(p), loop).result(timeout)``.
    """
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError(f"count must be a positive int, got {count!r}")
    loop = asyncio.get_running_loop()
    owner = getattr(primitive, "_loop", None)
    if owner is not None and owner is not loop:
        raise RuntimeError(
            "until_parked() was awaited on a different loop than the one the primitive is "
            "bound to; run it on the owning loop"
        )
    _waiters_of(primitive, loop)  # an unsupported primitive fails before any wait
    seen = 0

    def parked() -> bool:
        nonlocal seen
        seen = _waiters_of(primitive, loop)
        return seen >= count

    def state() -> str:
        extra = _read_describe(describe)
        return f"{seen} waiter(s) parked on {type(primitive).__name__}, want {count}" + (
            f"; {extra}" if extra else ""
        )

    await async_wait_until(parked, timeout=timeout, interval=interval, describe=state)
    return seen


def drained_progress(
    read_size: Callable[[], int],
    *,
    done: Callable[[float], object],
    window: float = DEFAULT_PROGRESS_WINDOW_SECS,
    cap: float | None = None,
    interval: float = DEFAULT_INTERVAL_SECS,
    describe: Callable[[], object] = lambda: "",
) -> int:
    """Wait for a producer to finish, failing only when it stops making progress.

    For a batch of work whose speed the host owns (an ``fsync`` per append),
    where a fixed ceiling would assert a rate. ``done(budget)`` reports whether
    the work is finished and may block for up to ``budget`` seconds (as
    ``emit.flush(timeout=budget)`` does); ``budget`` never exceeds the rest of
    the current window or of the cap. ``read_size()`` is a counter that only
    grows, such as the bytes on disk.

    Raises ``AssertionError`` when ``read_size()`` did not grow for a whole
    ``window`` -- measured from before the first ``done()`` call, so a producer
    wedged from the start is reported one window in -- or when the producer is
    still going at ``cap`` (by default :func:`default_timeout`). Returns the
    final ``read_size()``.
    """
    _refuse_running_loop("drained_progress")
    if inspect.iscoroutinefunction(done):
        raise TypeError(f"{_name(done)} is a coroutine function; drained_progress() blocks")
    _check_seconds(window, "window", positive=True)
    limit = _resolve_timeout(cap)
    _check_seconds(interval, "interval", positive=True)
    start = _monotonic()
    cap_at = start + limit
    window_end = start + window
    landed = read_size()
    while True:
        now = _monotonic()
        finished = done(max(0.0, min(window_end, cap_at) - now))
        _refuse_awaitable(finished, done, "drained_progress")
        if finished:
            return read_size()
        now = _monotonic()
        if now >= cap_at:
            raise AssertionError(
                f"drained_progress: still not done at the {limit:.2f}s cap "
                f"(size {read_size()}); last state: {_read_describe(describe)}"
            )
        if now >= window_end:
            size = read_size()
            if size <= landed:
                raise AssertionError(
                    f"drained_progress: no progress for {window:.2f}s (size stayed {size}); "
                    f"last state: {_read_describe(describe)}"
                )
            landed = size
            window_end = now + window
            continue
        _sleep(min(interval, window_end - now))
