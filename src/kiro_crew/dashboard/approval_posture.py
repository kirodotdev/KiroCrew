"""The one decision on whether a slot's trust passes to what it starts."""

from __future__ import annotations

from typing import Any


def trust_carries(slot: Any) -> bool:
    """Whether *slot*'s ``_trust`` passes to its spawns, subagents, new sessions and forks.

    It does for a trust a person set on the slot itself. It does not for a Crew Mode
    card trust ("Trust this crew member", ``_trust_card_scoped``): that covers the
    crew member's own tools only, so its children keep asking on the conductor's card.
    Every writer of a slot's stored ``approval_policy`` (what the spawn gate and each
    subagent read) and every creator posture goes through this.
    """
    return bool(getattr(slot, "_trust", False)) and (
        getattr(slot, "_trust_card_scoped", False) is not True
    )
