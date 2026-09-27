"""The shared snapshot-commit choreography for the dashboard's vocabulary stores.

A LEAF module on purpose. Both vocabulary stores (folders and tags) need this protocol,
so it cannot live in either of them without one importing the other; and it cannot live in
``chat_persistence`` either, because the folder store importing that module closes a cycle
back through ``chat_utils``. Depending on nothing but :mod:`asyncio` is what keeps it
importable from both sides.

Only the DRAIN is shared, via ``drain_shielded``. Each caller derives its own outcome --
one re-raises the sweep's failure, one owes a confirmation, one returns the write's
result -- which is what let them share this without widening any one caller's ending.

The ordering it single-sources is specified in docs/system-specs/modules/history.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import weakref
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


_closes_in_flight: "weakref.WeakKeyDictionary[Any, dict[int, tuple[str, Any]]]" = (
    weakref.WeakKeyDictionary()
)


@contextlib.contextmanager
def close_in_flight(state: Any, name: str, slot: Any) -> Iterator[None]:
    """Publish *slot* under *name* for the span of its close, pop included.

    A vocabulary delete captures the slots it must sweep by reading ``state._slots``.
    A close that pops its slot BEFORE that capture is therefore in no view the sweep
    can see, and if the close's own save then fails it restores the same object still
    carrying the deleted id -- which the periodic flush makes durable. Publishing here
    spans the pop, so the capture reaches the slot while it is out of the map.

    Keyed by object identity, not by *name*: a same-key recreate can put a replacement
    on the name while the original is still closing, and both are then in flight.

    Partitioned per *state*, and weakly, so a delete on one dashboard state can never
    sweep a slot belonging to another one sharing the process -- which every test that
    builds a second state does -- and a discarded state's registry goes with it.
    """
    registry = _closes_in_flight.setdefault(state, {})
    token = id(slot)
    registry[token] = (name, slot)
    try:
        yield
    finally:
        registry.pop(token, None)


def slots_with_a_close_in_flight(state: Any) -> list[tuple[str, Any]]:
    """The ``(name, slot)`` pairs a sweep of *state* would otherwise miss, as a snapshot.

    A list, not the live mapping: callers iterate this alongside ``state._slots`` and a
    close finishing mid-iteration must not resize it.
    """
    return list(_closes_in_flight.get(state, {}).values())


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

    What the drain protects is the sweep's own work: the slots still naming the removed
    id, and the operation's single audit line.
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
