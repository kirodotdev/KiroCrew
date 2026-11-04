"""``SessionAllocationService.claim_existing``: claim the live session for a key, never start one.

``get_or_create`` promises a session. It holds a caller at a fenced key until the fence
lifts, evicts a dead entry, joins an eager start and cold-starts when nothing is
registered, so its caller always ends up holding something. A caller that may only
CONTINUE a conversation needs the opposite promise: a cron sub-agent's completion is
one, because a ``cron:`` key is stateless, so a session started for it carries none of
the orchestration it reports to and would act on the report as a new, unattended
agent.

``claim_existing`` takes the permit of the entry registered under the key when it is
called, waiting out a busy turn, and hands that entry's provider back only when, with
the permit held, the same entry is still registered, its process is alive and no
ending fence rose meanwhile. Every other outcome is ``None`` with no permit held and
nothing evicted, started or retried.

The member is a ``SessionAllocationService`` method, bound by name as the other owners'
members are; this module never imports :mod:`kiro_crew.session_allocation` at run time.

Instance fields written: ``_allocation_reservations``.

Instance fields read: ``_closing``, ``_deps``, ``_lock``, ``_owner``, ``_sessions``, ``state``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kiro_crew.session_allocation_parts.errors import SessionEndingError
from kiro_crew.session_lifecycle import adopt_parked_queue

if TYPE_CHECKING:
    from kiro_crew.session_allocation import LLMProvider, SessionAllocationService


async def claim_existing(
    self: SessionAllocationService, key: str, *, wait_if_busy: bool = True
) -> tuple[LLMProvider, bool, bool] | None:
    """Claim the session registered for *key* as ``(provider, is_new, resumed)``, or None.

    A tuple means this task holds the entry's permit and must ``release`` it, exactly
    as after ``get_or_create``. None means no permit is held: nothing is registered
    under the key, the manager is closing, the key is fenced for ending or its entry is
    being recycled, or, after the permit wait, the manager began closing, the entry
    was replaced or popped, its process died, or an ending fence rose (and possibly
    lifted) during the wait. A busy
    entry is waited for unless ``wait_if_busy`` is false, which raises
    ``SessionBusyError`` as ``get_or_create`` does.

    The process check is the allocator's own (``provider_effectively_alive``), asked
    when the claim is made and again, under the lock, when the permit wait ends.
    """
    await self._owner.await_replay_gap(key)
    token = object()
    async with self._lock:
        if self._closing:
            return None
        folded = self._owner._fold_key(key)
        session = self._sessions.get(folded)
        if (
            session is None
            or folded in self.state.ending_keys
            or self._owner._recycling.get(folded) is session
            or not self._deps.provider_effectively_alive(session.provider)
        ):
            return None
        # A reservation, as ``get_or_create`` takes: an ending fence that rises
        # while this claim waits for the permit invalidates it, so the check after
        # the wait refuses even when the fence has already lifted again.
        self._allocation_reservations.setdefault(folded, set()).add(token)
        self.advance_ownership_generation(folded)
    try:
        try:
            claimed = await self._owner._reacquire_and_validate(
                folded, session, wait_if_busy=wait_if_busy, reservation=token
            )
        except SessionEndingError:
            # The permit was given back before the refusal was raised.
            claimed = False
        # Asked again with the permit in hand: the manager may have begun closing
        # while the claim waited out a turn. The wait itself re-asked whether the
        # process is alive, under the lock and with no await since.
        if claimed and self._closing:
            self._release_reacquired(folded, session)
            claimed = False
    except BaseException:
        await self._remove_reservation_cancellation_drained(folded, token)
        raise
    self._remove_reservation_now(folded, token)
    if not claimed:
        return None
    # From here on, as at the end of ``get_or_create``: the release this task makes
    # is for THIS permit, and the claim consumes the entry's first-turn observation.
    self._owner.adopt_turn(folded)
    first_turn = session.first_turn
    session.first_turn = self._deps.first_turn_nothing_armed
    if adopt_parked_queue(session, folded):
        session.adopted_parked = True
    return session.provider, first_turn.is_new, first_turn.resumed
