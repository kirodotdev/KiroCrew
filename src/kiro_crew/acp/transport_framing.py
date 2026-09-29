"""Stdio JSON-RPC framing shared by both ACP transports.

``AcpClient`` (one harness process per session) and ``AcpRuntime`` (one process
multiplexed across sessions) exchange newline-delimited JSON-RPC frames over the
harness's stdio. This module owns the parts of that framing both transports share:
discarding one oversize stdout line while keeping the stream on a frame boundary, and
bounding a response or notification write by the reader's progress rather than by
elapsed time. Each transport keeps its own write lock, pending-request table and the
death handling a failed write triggers.

``kiro_crew.acp.client`` re-exports every name defined here.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

# Subprocess stdout buffer — kiro-cli can send large JSON-RPC lines (tool outputs)
_STDOUT_BUFFER_LIMIT = 10 * 1024 * 1024  # 10MB
# Ceiling on the bytes discarded while draining ONE oversize line. Per drain call
# and expressed in BYTES: each call provably ends ON a frame boundary, so a replay
# of many legitimately-oversize-but-terminated frames each gets its own budget and
# stays survivable. A count of oversize FRAMES would kill the runtime on exactly
# that replay. Only a single blob that never terminates can exhaust this.
_OVERSIZE_DRAIN_MAX_BYTES = 16 * _STDOUT_BUFFER_LIMIT  # 160MB


class OversizeLineUnrecoverable(Exception):
    """An oversize stdout line exceeded the drain budget without terminating."""


# Named by the path callers import it from, as the ACP error classes are: an error
# chain renders a raised class as ``module.qualname``.
OversizeLineUnrecoverable.__module__ = "kiro_crew.acp.client"


async def _drain_oversize_line(reader: asyncio.StreamReader, exc: asyncio.LimitOverrunError) -> int:
    """Discard one oversize line ENTIRELY, leaving the stream on a frame boundary.

    Called after ``readuntil(b"\\n")`` raised ``LimitOverrunError``, which consumes
    nothing. ``exc.consumed`` is the already-buffered prefix that provably holds no
    separator, so consuming it cannot cross into the next frame; retrying
    ``readuntil`` then either returns the remainder of the line or raises for
    another step. Same consume-prefix-and-retry drain as
    ``mcp_gateway/backend.py::run_stdout_pump``; a plain ``read(n)`` would instead
    eat into the NEXT frame.

    The recovered remainder is **discarded, never parsed**. It is a byte-slice of
    the line cut at an arbitrary offset, so it can split a multibyte UTF-8
    character — and ``json.loads`` on that raises ``UnicodeDecodeError``, which is
    NOT a ``json.JSONDecodeError`` and would escape the caller's non-JSON guard
    into its crash handler, killing every multiplexed session over one oversize
    frame.

    Returns the bytes discarded. Raises ``OversizeLineUnrecoverable`` past
    ``_OVERSIZE_DRAIN_MAX_BYTES`` (the stream is garbage, not merely verbose) and
    propagates ``IncompleteReadError`` on EOF mid-drain so the caller can use its
    normal end-of-stream path.
    """
    discarded = 0
    while True:
        if exc.consumed <= 0:
            # Unreachable via CPython, whose consumed always exceeds the reader's
            # limit; guarded because a zero would make this loop spin without
            # awaiting and starve the event loop.
            raise OversizeLineUnrecoverable(
                f"stream reported a {exc.consumed}-byte oversize prefix"
            )
        discarded += len(await reader.readexactly(exc.consumed))
        if discarded > _OVERSIZE_DRAIN_MAX_BYTES:
            raise OversizeLineUnrecoverable(
                f"discarded {discarded} bytes with no frame boundary "
                f"(limit {_OVERSIZE_DRAIN_MAX_BYTES})"
            )
        try:
            return discarded + len(await reader.readuntil(b"\n"))
        except asyncio.LimitOverrunError as again:
            exc = again


# No-progress bound on delivering a response or error frame to the backend's
# stdin (permission answers, unknown-method errors). ``StreamWriter.drain()``
# returns at once while the pipe has room; it only parks when the writer is
# flow-control paused -- the kernel pipe buffer is full and the transport's
# own buffer is past its high-water mark. That is what a backend that has
# stopped reading stdin looks like once frames have backed up behind it, but
# it is ALSO what a healthy backend looks like while it consumes a multi-MB
# prompt frame queued ahead of the response on the same writer (the shared
# runtime multiplexes sessions on one stdin). The two are told apart by
# progress, not by elapsed time: the bound is the longest the transport's
# write buffer may go without shrinking. A reader that is consuming, however
# slowly, keeps the wait alive; a reader that consumed nothing for this long
# is gone, and the write is raised as the process death it is (AcpProcessDied
# / AcpRuntimeDead) so the caller takes the same session-reset + bounded-
# requeue recovery the broken-pipe case already uses. The bound cannot observe
# delivery of a frame the pipe accepted (the protocol gives no ack for a
# response); it bounds the wait on a paused writer. Sized like chat_runner's
# _STEER_NOTICE_BOUND_SECS: far below the turn deadline.
_RESPONSE_WRITE_BOUND_SECS = 5.0
# The least the write-buffer level must DROP within one window for the drop
# to count as the reader consuming. Without a floor, a reader that takes one
# byte per window extends the wait forever; with a flat elapsed cap instead, a
# healthy-but-slow reader draining a co-tenant's multi-MB frame would be
# killed by the cap -- the very failure the progress bound exists to avoid.
# 4 KiB per 5s window is under 1 KB/s: a reader that slow would need hours
# for a single image frame, which is dead for every practical purpose.
_RESPONSE_WRITE_MIN_PROGRESS_BYTES = 4096
# The wait is bounded in TOTAL by construction, not by a flat figure: no fixed
# ceiling can be right when the largest frame is unbounded (a prompt may carry
# any number of 5 MiB image blocks) -- a fixed one either kills a live reader
# on a valid large frame or lets a trickling one run long. Instead: the write
# buffer's level is finite and non-negative, and every window that continues
# the wait removed at least the floor from it, so a wait on a backlog of B
# bytes lasts at most B / _RESPONSE_WRITE_MIN_PROGRESS_BYTES + 1 windows (plus
# the same for any frame a sibling writer appends meanwhile). That is the
# derived ceiling -- backlog over the minimum accepted rate -- and it needs no
# constant of its own.
# Window used when the transport cannot show progress at all. Windows's
# proactor pipe transport reports ``get_write_buffer_size()`` as the whole
# in-flight overlapped write until that write completes, so its level is flat
# for the entire time a healthy reader consumes a large frame -- flatness is
# not a stall there, and the only signal left is elapsed time. With no backlog
# to derive from, this is the one place a fixed figure remains, sized for the
# largest frame a healthy local reader plausibly drains: ~30 MiB at ~40 KiB/s
# (test-pinned). Platform-limited, like the Windows watchdog: a dead reader is
# detected in 15 minutes there instead of 5 seconds.
_RESPONSE_WRITE_UNOBSERVABLE_BOUND_SECS = 900.0


def _is_proactor_loop(loop: asyncio.AbstractEventLoop) -> bool:
    """Whether ``loop`` is Windows's proactor loop (the only loop that drives
    subprocess pipes through overlapped I/O). Keyed to the public
    ``asyncio.ProactorEventLoop`` class the platform owns, which exists only
    on Windows, rather than to a private transport class name."""
    proactor = getattr(asyncio, "ProactorEventLoop", None)
    return proactor is not None and isinstance(loop, proactor)


def _level_is_progress_signal(transport: object) -> bool:
    """Whether a transport's write-buffer level moves as the reader consumes.

    True under the selector loops (the pipe transport trims its buffer per
    readiness callback). False under the proactor loop Windows uses for
    subprocess pipes: the level is the whole in-flight overlapped write until
    it completes, so it is flat while a live reader consumes a large frame.
    Decided from the running loop, not the transport's class name.
    """
    return not _is_proactor_loop(asyncio.get_running_loop())


def _pending_write_bytes(stdin: asyncio.StreamWriter) -> int | None:
    """Bytes the writer still holds for the pipe, or ``None`` when that level
    is not a progress signal.

    ``None`` -- a transport without ``get_write_buffer_size`` (a test double),
    or one whose level does not move mid-frame (``_level_is_progress_signal``)
    -- means progress cannot be observed; the bounded wait then falls back to
    the platform-limited elapsed window, ``_RESPONSE_WRITE_UNOBSERVABLE_BOUND_SECS``.
    """
    transport = getattr(stdin, "transport", None)
    size = getattr(transport, "get_write_buffer_size", None)
    if not callable(size) or not _level_is_progress_signal(transport):
        return None
    value = size()
    return value if isinstance(value, int) else None


async def await_under_no_progress_bound(
    aw: Awaitable[Any],
    stdin: asyncio.StreamWriter,
    *,
    bound_secs: float,
) -> bool:
    """Await ``aw`` while the writer behind ``stdin`` keeps showing activity.

    Returns ``True`` when ``aw`` completed (its exception, if any, propagates).
    Returns ``False`` -- after cancelling ``aw`` -- when the transport's write
    buffer showed no activity for ``bound_secs``. The total wait is bounded by
    construction -- each continued window removed at least the floor from a
    finite, non-negative level, so a backlog of B bytes is waited on for at
    most B / floor + 1 windows (plus the same for any frame a sibling writer
    appends meanwhile). Activity at a window's end
    is either a DROP of at least ``_RESPONSE_WRITE_MIN_PROGRESS_BYTES`` (the reader consumed a
    real amount; a large frame ahead of this one is being drained) or a RISE
    (a frame from a writer the lock woke ahead of this caller landed on the
    pipe); both continue the wait, measured again from the new level. A level
    that held still, or dropped by less than the floor, is a reader that is
    gone -- the floor is what keeps a byte-per-window trickle from extending
    the wait forever without capping a genuinely draining frame.

    When the level is not a progress signal at all (``_pending_write_bytes``
    returns ``None``: a proactor transport, or a test double without one) the
    single window is ``_RESPONSE_WRITE_UNOBSERVABLE_BOUND_SECS`` and elapsed
    time alone decides
    -- platform-limited, never extended, since nothing can be observed to
    extend it on.

    ``aw`` is shielded from the caller's cancellation so a window closing does
    not abort it; an awaitable found complete when a window closes counts as
    completed, never as a stall. On a stall verdict or a cancellation it is
    cancelled if still pending. An awaitable that completed in the meantime is
    NOT undone here -- a caller whose awaitable acquires something must release
    it on those paths (see ``write_response_frame_bounded`` /
    ``_release_if_acquired``).

    The level measurement needs one writer in flight at a time to mean
    anything, which is why every stdin write on a transport goes through that
    transport's write lock. Without it, concurrent appends interleave with the
    reader's consumption and a level that merely looks flat could hide both.
    """
    task = asyncio.ensure_future(aw)
    last = _pending_write_bytes(stdin)
    window = _RESPONSE_WRITE_UNOBSERVABLE_BOUND_SECS if last is None else bound_secs
    try:
        while True:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=window)
            except asyncio.TimeoutError:
                if task.done():
                    task.result()  # completed as the window closed; re-raise its error
                    return True
                now = _pending_write_bytes(stdin)
                if now is not None and last is not None:
                    if now > last or last - now >= _RESPONSE_WRITE_MIN_PROGRESS_BYTES:
                        last = now  # the level moved: activity, measure again from here
                        continue
                task.cancel()
                return False
            return True
    finally:
        if not task.done():
            task.cancel()


def _release_if_acquired(lock: asyncio.Lock, acquire: "asyncio.Future[bool]") -> None:
    """Undo a ``lock.acquire()`` whose caller has given up on it.

    ``await_under_no_progress_bound`` shields the acquire task, so an outer
    cancellation (or a stall verdict) can land AFTER the acquire completed and
    the task holds the lock with nobody left to release it -- and this lock is
    the one every stdin write on the transport waits for. If the acquire is
    done and succeeded, release now; otherwise cancel it and release from its
    done callback should it still complete with the lock.
    """

    def _done(task: "asyncio.Future[bool]") -> None:
        if not task.cancelled() and task.exception() is None and task.result():
            lock.release()

    if acquire.done():
        _done(acquire)
    else:
        acquire.cancel()
        acquire.add_done_callback(_done)


async def write_response_frame_bounded(
    stdin: asyncio.StreamWriter,
    lock: asyncio.Lock,
    data: bytes,
    *,
    bound_secs: float,
    before_write: Callable[[], None] | None = None,
) -> bool:
    """Write one response/error frame under the transport's write lock and a
    no-progress bound on BOTH waits -- for the lock and for the drain.

    ``before_write`` runs under the lock, just before the frame is written, and
    may raise to abort the write -- the shared runtime uses it to re-check that
    it was not marked dead while this caller waited for the lock, so no frame
    is written into a pipe whose owner has already been torn down.

    Waiting for the lock is waiting for the previous frame's drain: a
    flow-control-paused writer holding a multi-MB prompt is a live reader as
    long as its buffer level moves, and a dead one when it does not. Returns
    ``False`` on a stall in either phase without writing (lock phase) or after
    cancelling the drain (drain phase); the caller maps that to its own
    process-death exception. Pipe errors from ``drain()`` propagate. The lock
    is never left held: a stall verdict or a cancellation that lands after the
    shielded acquire completed releases it (``_release_if_acquired``).
    """
    acquire: asyncio.Future[bool] = asyncio.ensure_future(lock.acquire())
    try:
        acquired = await await_under_no_progress_bound(acquire, stdin, bound_secs=bound_secs)
    except BaseException:
        _release_if_acquired(lock, acquire)
        raise
    if not acquired:
        _release_if_acquired(lock, acquire)
        return False
    try:
        if before_write is not None:
            before_write()
        stdin.write(data)
        return await await_under_no_progress_bound(stdin.drain(), stdin, bound_secs=bound_secs)
    finally:
        lock.release()


def response_write_window_secs(stdin: asyncio.StreamWriter, bound_secs: float) -> float:
    """The no-progress window ``await_under_no_progress_bound`` applies to this
    writer -- ``bound_secs`` when its level is a progress signal, the
    platform-limited window when it is not -- so a stall log line states the
    window that was actually measured."""
    return (
        bound_secs
        if _pending_write_bytes(stdin) is not None
        else _RESPONSE_WRITE_UNOBSERVABLE_BOUND_SECS
    )


async def write_notification_best_effort(
    stdin: asyncio.StreamWriter,
    lock: asyncio.Lock,
    data: bytes,
    *,
    bound_secs: float,
    before_write: Callable[[], None] | None = None,
) -> str:
    """Write a fire-and-forget notification (``session/cancel``) without letting
    the write lock swallow it.

    A cancel is the one cooperative signal that can end a wedged turn, so it
    must not queue forever behind a holder parked on a reader that stopped. Wait
    for the lock under the no-progress bound; if the lock does not come, append
    the frame UNLOCKED (no drain) so the transport enqueues it the moment the
    pipe has room -- a single extra append the lock-holder's measurement reads
    as activity for one window, which is the price of delivering the cancel.
    Under the lock the drain is bounded the same way. Returns what happened, for
    the caller's log line: ``"drained"`` (written and drained under the lock),
    ``"appended_unlocked"`` (the lock did not come; the frame sits in the
    transport's buffer with no drain observed), or ``"stalled"`` (the locked
    drain made no progress). Pipe errors propagate.
    """
    acquire: asyncio.Future[bool] = asyncio.ensure_future(lock.acquire())
    try:
        acquired = await await_under_no_progress_bound(acquire, stdin, bound_secs=bound_secs)
    except BaseException:
        _release_if_acquired(lock, acquire)
        raise
    if not acquired:
        _release_if_acquired(lock, acquire)
        if before_write is not None:
            before_write()
        stdin.write(data)
        return "appended_unlocked"
    try:
        if before_write is not None:
            before_write()
        stdin.write(data)
        drained = await await_under_no_progress_bound(stdin.drain(), stdin, bound_secs=bound_secs)
        return "drained" if drained else "stalled"
    finally:
        lock.release()
