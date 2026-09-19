"""The shared snapshot-commit choreography for the dashboard's vocabulary stores.

A LEAF module on purpose. Both vocabulary stores (folders and tags) need this protocol,
so it cannot live in either of them without one importing the other; and it cannot live in
``chat_persistence`` either, because the folder store importing that module closes a cycle
back through ``chat_utils``. Depending on nothing but :mod:`asyncio` is what keeps it
importable from both sides.

Only the DRAIN is shared, via ``drain_shielded``. Each caller derives its own outcome,
because they differ -- one re-raises the sweep's failure, one also owes a publication, one
returns the write's result -- which is what let the other spellings fold in without
widening publish-on-both-exits onto a caller that does not want it.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Callable, Coroutine, Iterator


class VocabularyDeleteCancellations:
    """The capture-then-ordered-re-raise ledger both vocabulary delete handlers share.

    ONE definition of the ORDER, because the order is the part a third vocabulary would
    re-derive wrong. A delete CAPTURES cancellations instead of propagating them so the
    work owed after a durable mutation still runs -- the rollback decision, the slots
    push, and the single audit emission -- and only then re-raises.

    COMMIT BEFORE SWEEP: a commit cancellation can have arrived before any write, so it
    is the one carrying the caller's rollback decision, while a sweep cancellation always
    follows a mutation that already landed.

    THE FIRST SWEEP CANCELLATION WINS: a handler may run several sweeps after one commit,
    so the earliest describes when it stopped and a later one adds nothing.
    """

    def __init__(self) -> None:
        self.commit: BaseException | None = None
        self.sweep: BaseException | None = None

    @contextlib.contextmanager
    def capturing_commit(self) -> Iterator[None]:
        """Hold a commit cancellation. Every other exception reaches the caller."""
        try:
            yield
        except asyncio.CancelledError as exc:
            self.commit = exc

    @contextlib.contextmanager
    def capturing_sweep(self) -> Iterator[None]:
        """Hold the FIRST sweep cancellation. Every other exception reaches the caller."""
        try:
            yield
        except asyncio.CancelledError as exc:
            if self.sweep is None:
                self.sweep = exc

    def reraise_in_order(self) -> None:
        """Re-raise what was captured, commit before sweep."""
        if self.commit is not None:
            raise self.commit
        if self.sweep is not None:
            raise self.sweep


async def drain_shielded(task: "asyncio.Future[Any]") -> "asyncio.CancelledError | None":
    """Outlive *task*, absorbing every cancellation delivered while it finishes.

    ONE definition, because every caller needs exactly this and this is where the
    subtlety lives: a task that is ALREADY cancelled gets a fresh cancellation delivered on
    each await, so a single drain is not enough -- awaiting the drain is itself a suspension
    point. ``task.done()`` is the termination condition and is reached as soon as the worker
    returns, so this cannot spin on a task that completes.

    Returns on a task that finished EITHER way, handing back the FIRST cancellation it
    absorbed (or ``None``). Deriving the outcome stays the caller's: they differ -- one
    re-raises the sweep's failure, one also owes a publication, one re-raises the absorbed
    cancellation and returns the worker's value -- which is why this stops at the drain
    rather than folding their endings in too. Callers that do not care simply ignore it.
    """
    absorbed: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if absorbed is None:
                absorbed = exc
            continue
        except Exception:
            # The task itself failed. Stop waiting; each caller derives the outcome once.
            break
    return absorbed


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
        await drain_shielded(task)
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
    hand-synced cancellation protocol per store would drift toward silent data loss.
    Rollback stays the caller's, since both call sites hold the pre-mutation copy.

    ``asyncio.to_thread`` hands the body to a worker that cannot be interrupted, so a
    cancelled handler still lands the bytes. Three consequences, each handled here:

    * Awaiting bare would lose the PUBLICATION, so it is an explicit statement on BOTH
      exits -- a ``call_soon`` done-callback stays queued while the cancellation unwinds.
    * ``shield`` re-raises at once, so returning would release the caller's lock with the
      worker still writing and let a later mutation lose to this older write.
    * ``CancelledError`` is not an ``Exception``. When the drain reports the write FAILED,
      that error is raised instead, putting the caller's handler back in reach of it.
    """

    try:
        await asyncio.shield(write)
    except asyncio.CancelledError:
        # Returning here would release the caller's store lock with the worker still
        # writing, and the next mutation would then be overwritten by this older write.
        await drain_shielded(write)
        if write.cancelled():  # pragma: no cover - shielded, needs an outside cancel
            failure: BaseException | None = asyncio.CancelledError()
        else:
            failure = write.exception()
        # Not swallowed: we arrived from a cancellation and have not seen this failure, so
        # discarding it leaves memory holding a value that never reached disk.
        if failure is not None:
            raise failure
        # Publication is owed here and cannot wait for a callback: the caller decides
        # whether the sweep is owed while this cancellation unwinds through it.
        publish()
        raise
    else:
        publish()
