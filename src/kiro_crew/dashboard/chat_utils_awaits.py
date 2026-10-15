"""Cancellation-safe awaits: a cancellation never abandons the work a handler started.

Served through :mod:`kiro_crew.dashboard.chat_utils`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import asyncio


async def run_config_write(fn, /, *args, **kwargs):
    """Run a blocking ``config.json`` writer under BOTH config locks.

    Every ``config.json`` read-modify-write must serialize against two writer
    generations at once: the sidecar advisory flock that ``update_config_locked``
    takes (covering CLI / boot-refresh / other-process writers), and the
    loop-side :func:`_get_config_lock` asyncio lock that the dashboard's legacy
    handlers still rely on *alone* (bare ``read_config_for_update`` +
    ``write_config_atomically``). A writer that holds only one of the two can
    interleave with the other family and silently revert its settings from a
    stale snapshot. The memory-settings PUT was such a writer and is not one
    any more: ``handlers/memory.py`` routes every config mutation through this
    helper.

    This helper is the one async entry point that holds both: the asyncio lock
    is acquired on the event loop, then ``fn`` (a sync callable that itself
    routes through ``update_config_locked``) runs in a worker thread so the
    flock wait never blocks the loop. Mirrors the boot-time meta-stamp refresh
    in ``server.py``, which established the pattern.
    """
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # lazy: import cycle

    async with _get_config_lock():
        # The worker is SHIELDED and drained before the lock is released. A
        # thread cannot be cancelled, so cancelling this coroutine -- a gateway
        # shutdown cancelling the boot migration, a client disconnecting mid-PUT
        # -- would otherwise unwind the `async with` while ``fn`` is still inside
        # its read-modify-write. The next writer would then enter the critical
        # section against a file the previous one is still rewriting, and land a
        # config derived from a snapshot taken before it: the earlier caller's
        # settings silently revert.
        #
        # The invariant is NOT "drain once after a cancellation": it is that once
        # a cancellation has been observed, the lock cannot leave this block until
        # the worker is done. A single drain does not give that -- awaiting the
        # drain is itself a suspension point, so a SECOND cancel (a graceful
        # shutdown escalating after its timeout, which is exactly when a config
        # write is most likely to be in flight) unwinds the `async with` with the
        # thread still writing. Hence the loop: every cancellation is absorbed,
        # and only a finished future ends it.
        #
        # Draining cannot change WHETHER the write happens -- the thread runs to
        # completion either way -- so this only decides whether the lock outlives
        # it. The cancellation is re-raised once, never swallowed.
        fut = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(fut)
            except asyncio.CancelledError:
                cancelled = True
                continue
            break
        if cancelled:
            raise asyncio.CancelledError
        return result


async def run_to_completion(aw):
    """Await *aw* so that a cancellation cannot abandon it part-way.

    The awaitable runs as its own task behind ``asyncio.shield``; a
    ``CancelledError`` delivered to the CALLER is remembered and the caller
    keeps waiting until the task finishes, then the cancellation is re-raised.
    The loop, not a single re-await, is what makes that hold under repeated
    cancellation (a graceful shutdown escalating after its timeout): each
    re-shield absorbs one more cancel, and only a finished task ends it.

    For a multi-phase write -- a channel saver's config.json commit followed by
    its ``.env`` credential write, the MCP gateway toggle's persist followed by
    its live apply -- a cancellation between the phases would leave the stored
    state and the effective state disagreeing; wrapping the whole transaction
    here is what keeps the pair consistent. Cancellation is deferred, never
    swallowed: the caller still unwinds with ``CancelledError`` afterwards, and
    an exception from the task propagates as usual.
    """
    task = asyncio.ensure_future(aw)
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            # OUR await was cancelled, not the task: remember it, keep waiting
            # for the still-running work.
            cancelled = exc
    if cancelled is not None:
        raise cancelled
    return result


async def drained_to_thread(fn, /, *args):
    """``asyncio.to_thread`` that a cancellation cannot abandon mid-mutation.

    A plain ``await to_thread(...)`` raises ``CancelledError`` at the await
    while the worker THREAD keeps running — a handler that then performs
    cleanup (releasing a lock, removing a staging directory) races its own
    still-running worker. :func:`run_to_completion` keeps the await alive until
    the worker actually finishes, then re-raises the cancellation, so control
    only ever returns with no mutation in flight. Shared by the agents handler's
    config writers and the files handler's workspace-copy staging.
    """
    return await run_to_completion(asyncio.to_thread(fn, *args))
