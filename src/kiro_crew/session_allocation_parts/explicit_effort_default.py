"""A cold start's read and save of the session map's one-shot explicit effort Default."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.session_allocation import SessionAllocationService


async def explicit_default_for_start(
    service: SessionAllocationService,
    key: str,
    reservation: object | None,
    start_priority: Any,
    level_override: object,
) -> bool | asyncio.Event:
    """Whether this start applies the key's pending explicit Default, or the Event to wait on.

    A pending Default needs a cold start: a warm process read cli.json at its own
    spawn, and Default cannot be pushed to a live session. Read once, here: the
    caller's reservation is marked a cold start (``cold_start_reservations``) from
    before this read until the session is registered, and the effort handler
    refuses a pick that would change the flag while it is
    (``SessionManager.effort_basis_locked``), so what this start reads is the basis
    the session it publishes runs on. A pick that passed that check before the mark
    may still be saving, so its write settles first, and the value read is one a
    save kept (or the one a failed save put back). The count can only fall while
    this waits, because every later pick is refused or has nothing to write. A
    cancellation here propagates; nothing was read or written.

    A start on a level its caller chose (*level_override*) runs that level and
    leaves the flag for the next start that takes no override. The flag is pending
    because the key's latest effort action was a Default pick, so this override is
    not a newer one: a slot's level read before that pick landed, an alias slot's
    own level, or a caller's pin. That start keeps the pool, because the override is
    pushed to the claimed process.

    Otherwise the Default is reserved before the pool decision and any provider
    exists, so a second start that read the same pending Default never runs unarmed
    beside it. That second start gets the holder's Event, which the outer loop waits
    on before it retries.
    """
    owner = service._owner
    await owner.wait_for_effort_intent_writes(key)
    if not owner.explicit_effort_default_pending(key) or level_override:
        return False
    if reservation is None:  # pragma: no cover - private implementation contract
        raise RuntimeError("explicit Default allocation has no reservation token")
    if service._reserve_explicit_effort_default(key, reservation, start_priority):
        return True
    current = service.state.explicit_effort_default_reservations.get(
        service._deps.canonical_key(key)
    )
    if current is not None:
        return current[1]
    return False


async def save_applied_explicit_effort_default_clear(
    service: SessionAllocationService, key: str
) -> None:
    """Clear and save an explicit effort Default the started provider just applied.

    A failed save never fails the start: ``aflush`` restores the dirty mark, so
    the map's next successful save retries this already-applied Default's clear.
    """
    owner = service._owner
    owner.set_explicit_effort_default(key, False)
    try:
        await owner.aflush()
    except Exception:
        service._deps.logger.warning(
            "Could not save the explicit effort Default clear for %s; "
            "the Default already ran and the map's next save retries its clear",
            key,
            exc_info=True,
        )
