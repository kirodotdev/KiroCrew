"""The seam an owner of a slot-keyed fold fills to say in what order its units are read.

A slot carry replaces what a reader folds, so it must fold the slot's units in the order
that reader does. For most folds that order is the slot's succession. A fold its OWNER
serves (:data:`~kiro_crew.crew_log.projection_catalogue.OWNER_SERVED_SLOT_PROJECTION`)
is read in the order the owner recorded in, which a request that captured an old unit
before a reset can make differ from succession.

The core never names the owner. The owner registers one provider per fold name when it
loads; the carry asks :func:`owner_unit_order` and, when nothing is registered, reads in
succession like any other fold.
"""

from __future__ import annotations

from collections.abc import Callable

#: ``slot -> units``, or ``None`` when the slot is not one the owner orders (the carry
#: then reads it in succession), or ``()`` when the owner cannot give the order (the carry
#: then carries nothing and the older units stay).
UnitOrderProvider = Callable[[str], "tuple[str, ...] | None"]

_PROVIDERS: dict[str, UnitOrderProvider] = {}


def register_unit_order(fold_name: str, provider: UnitOrderProvider) -> None:
    """Set the provider that orders *fold_name*'s units; a later call replaces it."""
    _PROVIDERS[fold_name] = provider


def owner_unit_order(fold_name: str, slot: str) -> "tuple[str, ...] | None":
    """The units *fold_name* is read over for *slot* in its owner's order.

    ``None`` when no owner registered for the fold or the owner does not order this slot.
    """
    provider = _PROVIDERS.get(fold_name)
    return None if provider is None else provider(slot)
