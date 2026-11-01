"""The two decisions on whether a slot's trust passes to what it starts."""

from __future__ import annotations

from typing import Any


def trust_reaches_subagents(slot: Any) -> bool:
    """Whether *slot*'s ``_trust`` covers the subagents it spawns.

    Any ``_trust`` does, a Crew Mode card trust ("Trust this crew member",
    ``_trust_card_scoped``) included: trusting a worker trusts the subagents it
    spawns, so its ``spawn_run`` and their tools are not asked on the conductor's
    card. Every writer of a slot's stored ``approval_policy`` (what the spawn gate
    and each subagent read) goes through this; the Slack/background parent-trust
    gate counts the card trust through ``_slot_is_trusted`` like any slot trust. A
    revoke clears ``_trust`` and writes ``""``, so a child spawned after it asks
    again; one already running keeps the policy it was given at spawn.
    """
    return bool(getattr(slot, "_trust", False))


def trust_carries(slot: Any) -> bool:
    """Whether *slot*'s ``_trust`` passes to the new sessions, forks and workflows it starts.

    It does for a trust a person set on the slot itself. It does not for a Crew Mode
    card trust (``_trust_card_scoped``): that covers the crew member and the subagents
    it spawns (:func:`trust_reaches_subagents`), not other sessions it opens, which
    are born asking on the conductor's card. Every creator posture goes through this.
    """
    return trust_reaches_subagents(slot) and (
        getattr(slot, "_trust_card_scoped", False) is not True
    )
