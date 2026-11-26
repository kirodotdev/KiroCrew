"""Nesting reservation steps for :class:`WorkflowService`, as functions of the service.

Like ``update_from_run``, this module never imports ``service.py``.
``WorkflowService`` has a delegating method for each function, and callers use
those methods.
"""

from __future__ import annotations

import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from .registry import ACTIVE_STATUSES


@dataclass
class NestedSlot:
    """A start's nesting slot: its refusal, or the result and run id the start stores."""

    refusal: Optional[dict]
    reserved: str = ""
    started: Any = ""
    run_id: str = ""


@contextmanager
def admitted_slot(service: Any, session_key: str, run_id: str) -> Iterator[NestedSlot]:
    """Admit a start whose run id is known, and settle its slot when the block exits.

    The block returns ``slot.refusal`` when it is set and otherwise stores the
    start's result in ``slot.started``. A refused start holds no slot to settle.
    """
    slot = NestedSlot(
        service.nesting.admit(
            session_key, run_id, is_active=lambda active_id: run_active(service, active_id)
        )
    )
    try:
        yield slot
    finally:
        if slot.refusal is None:
            service.nesting.settle(run_id, started=bool(slot.started))


@contextmanager
def reserved_slot(service: Any, session_key: str) -> Iterator[NestedSlot]:
    """Reserve a slot for a start whose run id another component allocates.

    The block returns ``slot.refusal`` when it is set, passes ``slot.reserved``
    on as ``nesting_reservation`` and stores the produced run id in
    ``slot.run_id``. On exit the placeholder moves onto that run or is dropped.
    """
    reserved, refusal = reserve_nested_start(service, session_key)
    slot = NestedSlot(refusal, reserved=reserved)
    try:
        yield slot
    finally:
        if slot.refusal is None:
            settle_nested_start(service, reserved, slot.run_id)


def reserve_nested_start(service: Any, session_key: str) -> tuple[str, Optional[dict]]:
    """Check a start whose run id another component allocates; reserve its slot.

    Returns the placeholder the reservation is held under and ``None``, or the
    refusal. Hand the placeholder to ``begin_host_run`` as ``nesting_reservation``
    so the host run takes over its slot, and pair it with ``settle_nested_start``.
    """
    if not service.nesting.parent_run_of(session_key):
        # A start outside every run is depth 1 and fills no run's slots.
        return "", None
    reserved = f"pending:{secrets.token_hex(8)}"
    return reserved, service.nesting.admit(
        session_key, reserved, is_active=lambda run_id: run_active(service, run_id)
    )


def settle_nested_start(service: Any, reserved: str, run_id: str) -> None:
    """Move a placeholder onto the run it produced, or drop it when there is none."""
    if not reserved:
        return
    ledger = service.nesting
    # A host run that took the placeholder over is the run this start produced.
    moved_to = ledger.release_placeholder(reserved)
    run_id = run_id or moved_to
    if run_id:
        ledger.rename(reserved, run_id)
    ledger.settle(run_id or reserved, started=bool(run_id))


def run_active(service: Any, run_id: str) -> bool:
    handle = service.registry.get(run_id)
    return handle is not None and handle.status in ACTIVE_STATUSES
