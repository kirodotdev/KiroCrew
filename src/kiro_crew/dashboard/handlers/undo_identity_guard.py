"""Request-scoped identity guard for a change card's crewmate Undo.

A card's Undo of ``crewmate.create`` (DELETE) or ``crewmate.update`` (PUT) is
addressed by name, so on its own it would hit a crewmate re-made under that name
since the apply. The card records the immutable ``member_id``, the parent puts it
on the request under the key below, and the route compares it with the live one
under the lock it writes under. This module holds only the key and the
comparison; an absent key leaves the route unchanged.
"""

from __future__ import annotations

from typing import Any

#: The error code and client message the armed Undo route returns on a mismatch,
#: identical to the schedule-create Undo guard so the dashboard renders one
#: outcome.
UNDO_IDENTITY_CHANGED_CODE = "changed_since_apply"
UNDO_IDENTITY_CHANGED_MESSAGE = "this changed after the card was applied; undo refused"

#: ``request[...]`` key the parent sets before dispatching the destructive
#: crewmate-create Undo step. Its value is the immutable ``member_id`` the card's
#: own ``POST /api/agents`` create response returned (the card's
#: ``after["member_id"]``). ``crew_removal.api_kirocrew_agent_delete`` re-reads
#: the live crew's ``member_id`` under the config deletion lock and refuses the
#: Undo delete when it differs, so an owner who deleted and recreated the same
#: name between apply and Undo keeps the replacement crewmate and its crew log.
REQ_CARD_UNDO_CREWMATE_EXPECT = "card_undo_crewmate_expect"


def identity_mismatch(expected: Any, current: Any) -> bool:
    """Whether a recorded identity and the live one disagree enough to refuse.

    Returns ``False`` — proceed with the delete — only when the two are a
    confident, exact match. ``None`` on either side is "no evidence", which must
    NOT clear the guard: a member_id the card never recorded, or one a locked
    read could not find now, cannot prove the on-disk crew is still the card's,
    so a mismatch is reported and the destructive step is refused rather than
    guessing.

    Lists compare element-wise after coercing each element to its own value, so a
    value carried through JSON (where it arrives as a list) matches a tuple read
    back live without the container type mattering.
    """
    if expected is None or current is None:
        return True
    if isinstance(expected, (list, tuple)) and isinstance(current, (list, tuple)):
        return list(expected) != list(current)
    return expected != current
