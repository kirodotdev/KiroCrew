"""The shared snapshot-commit choreography for the dashboard's vocabulary stores.

A LEAF module on purpose. Both vocabulary stores (folders and tags) need this protocol,
so it cannot live in either of them without one importing the other; and it cannot live in
``chat_persistence`` either, because the folder store importing that module closes a cycle
back through ``chat_utils``. Depending on nothing but :mod:`asyncio` is what keeps it
importable from both sides.

It holds no drain loop of its own: both store commits and the post-commit sweep run
through the shared ``chat_utils_awaits.run_to_completion``.

The ordering it single-sources is specified in docs/system-specs/modules/history.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import weakref
from typing import Any, Iterator


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
_close_registrations = itertools.count()


@contextlib.contextmanager
def close_in_flight(state: Any, name: str, slot: Any) -> Iterator[None]:
    """Publish *slot* under *name* for the span of its close, pop included.

    A vocabulary delete captures the slots it must sweep by reading ``state._slots``.
    A close that pops its slot BEFORE that capture is therefore in no view the sweep
    can see, and if the close's own save then fails it restores the same object still
    carrying the deleted id -- which the periodic flush makes durable. Publishing here
    spans the pop, so the capture reaches the slot while it is out of the map.

    Keyed per REGISTRATION, not per object and not by *name*: a same-key recreate can put
    a replacement on the name while the original is still closing, and two closes can
    overlap on ONE object (a duplicate DELETE, or one racing a close from elsewhere). A
    per-object key would let whichever exits first unregister the other, returning a
    still-closing slot to the invisibility this exists to end. A slot a sweep reaches
    twice is harmless -- the first visit removes the id, so the second takes its
    ``continue`` -- so distinct entries are the safe direction.

    Partitioned per *state*, and weakly, so a delete on one dashboard state can never
    sweep a slot belonging to another one sharing the process -- which every test that
    builds a second state does -- and a discarded state's registry goes with it.
    """
    registry = _closes_in_flight.setdefault(state, {})
    token = next(_close_registrations)
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
