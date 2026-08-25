"""The shared snapshot-commit choreography for the dashboard's vocabulary stores.

A LEAF module on purpose. Both vocabulary stores (folders and tags) need this protocol,
so it cannot live in either of them without one importing the other; and it cannot live in
``chat_persistence`` either, because the folder store importing that module closes a cycle
back through ``chat_utils``. Depending on nothing but :mod:`asyncio` is what keeps it
importable from both sides.

A THIRD shield-drain-reraise loop already exists in the tree, at
``chat_utils.run_config_write``, and it is named here because the loop is subtle enough
that a future correction to it must be applied to every copy. That one is NOT folded in:
the two here add publish-on-both-exits and failure-supersedes-cancellation, and it guards
a config write rather than a vocabulary commit, so unifying them would either widen those
semantics onto a caller that does not want them or drag base code into this change. If a
drain-loop defect is found, fix all three: the two below and that one.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine


async def sweep_to_completion_despite_cancellation(
    sweep: "Coroutine[Any, Any, None]",
) -> None:
    """Run a post-commit sweep to completion even when the handler is cancelled.

    The COMPANION to :func:`commit_snapshot_while_holding_the_lock`, needed because that
    function succeeds at its own job: it shields the write, so a cancelled delete still
    LANDS the vocabulary removal, then re-raises. The caller's sweep runs after that await,
    so cancellation in the gap leaves the row gone and slot metadata still naming it.

    Shielding the commit alone cannot help -- the gap is BETWEEN the halves, so the atomic
    unit is commit-and-sweep. A sweep failure supersedes the cancellation, as in the
    sibling, because we reach the drain having never seen it.

    The full protocol, and why the restore-time fail-safe makes this load-bearing, is in
    ``docs/system-specs/modules/history.md``.
    """
    task = asyncio.ensure_future(sweep)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # Loops because an already-cancelled task gets a fresh cancellation per await;
        # ``task.done()`` terminates, so this cannot spin past the sweep finishing.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            failure = task.exception()
            if failure is not None:
                raise failure
        raise


async def commit_snapshot_while_holding_the_lock(
    write: "asyncio.Future[Any]",
    publish: Callable[[], None],
) -> None:
    """Await a shielded snapshot write, publish it, and unwind without losing the lock.

    ONE definition of the whole choreography, shared by both vocabulary stores: a
    hand-synced cancellation protocol per store would drift toward silent data loss, so
    everything the two sites do differently is a parameter rather than a copy.

    ``asyncio.to_thread`` hands the body to a worker that cannot be interrupted, so a
    cancelled handler still lands the bytes. Three consequences, each handled here:

    * Awaiting bare would lose the PUBLICATION -- disk moves on while the committed set
      keeps the old value -- so publication is an explicit statement on BOTH exits,
      never a done-callback. A callback is scheduled with ``call_soon``, so a
      cancellation racing the write's completion unwinds through the caller while the
      callback is still queued, and the caller's sweep decision reads the pre-removal
      committed set.
    * ``shield`` re-raises immediately, so returning would release the caller's lock
      with the worker still writing and let a later mutation be overwritten by this
      older worker finishing last. Hence the drain.
    * ``CancelledError`` is not an ``Exception``, so a caller's ``except Exception``
      never sees it. When the drain reports the write FAILED, the write's own error is
      raised INSTEAD of the cancellation -- which is what puts a plain
      ``except Exception`` at the call site back in reach of the failure.

    ROLLBACK IS THE CALLER'S, deliberately: both call sites already hold the
    pre-mutation copy and both recover in an ``except Exception`` of their own, so a
    ``rollback`` parameter here would be a second spelling of a decision the caller is
    better placed to make. A WRITE FAILURE SUPERSEDES THE CANCELLATION: when the drain
    reports the write failed, that error is raised in the cancellation's place, so the
    caller's plain ``except Exception`` is reached exactly when a restore is owed.
    Otherwise -- the shielded write SUCCEEDED -- the cancellation is re-raised as-is and
    ``except Exception`` does not see it, which is correct: the bytes landed, so there is
    nothing to restore.

    A genuine write failure on the NON-cancelled path needs no arm of its own here: it
    propagates from the ``await`` untouched, and the caller restores. Only the cancelled
    path is special-cased, because that is the one where the failure would otherwise go
    unseen.
    """

    try:
        await asyncio.shield(write)
    except asyncio.CancelledError:
        # DRAIN WITHOUT RELEASING THE LOCK, inline because this is the only place it
        # happens. ``shield`` re-raised the cancellation while the shielded task keeps
        # running, so returning here would let the caller's ``async with lock`` release
        # the store lock with the worker still writing -- and the next mutation would
        # then acquire it, write, and be OVERWRITTEN by this older worker finishing
        # last. That lost update is worse than the publication staleness the shield
        # fixed, and ``asyncio.to_thread`` cannot interrupt the worker, so the only
        # correct move is to outlive it.
        #
        # Loops because a task that is already cancelled can have a fresh cancellation
        # delivered on each await. ``write.done()`` is the termination condition and is
        # reached as soon as the worker returns, so this cannot spin on a write that
        # completes.
        while not write.done():
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                # Re-cancellation while draining. Keep waiting: the lock must outlive
                # the worker, and each await yields so the write can make progress.
                continue
            except Exception:
                # The write itself failed. Stop waiting; the outcome is derived once,
                # below, so there is exactly one place it comes from.
                break
        if write.cancelled():  # pragma: no cover - shielded, needs an outside cancel
            failure: BaseException | None = asyncio.CancelledError()
        else:
            failure = write.exception()
        # The write's failure is NOT swallowed: we arrived here from a cancellation and
        # have therefore not seen it, so discarding it would leave the caller's
        # ``except Exception`` unreached and memory holding a value that never reached
        # disk.
        if failure is not None:
            raise failure
        # Publication is owed here and cannot wait for a callback: the caller decides
        # whether the sweep is owed while this cancellation unwinds through it.
        publish()
        raise
    else:
        publish()
