"""The two decisions on whether a slot's trust passes to what it starts."""

from __future__ import annotations

from typing import Any

#: The ``_trust_scope`` prefix of a hub conductor's Trust carried to its bound worker
#: on this crew (rfc-conductor-remote-dispatch §4). It stands in for Trust, so the
#: ceiling's ``trust`` verdict masks it wherever it is honoured.
REMOTE_TRUST_SCOPE_PREFIX = "remote-dispatch:"


def remote_scope_denied(scope: str) -> bool:
    """Whether *scope* is a remote Trust grant the ceiling's ``trust`` verdict denies.

    The one mask every reader of such a grant applies: the approval path, the
    slots poll's Trust label and the hub's ``trust_active``. A memory read.
    """
    if not scope.startswith(REMOTE_TRUST_SCOPE_PREFIX):
        return False
    from kiro_crew.approval_mode_policy import session_mode_permitted

    return not session_mode_permitted("trust")


def trust_reaches_subagents(slot: Any) -> bool:
    """Whether *slot*'s ``_trust`` covers the subagents it spawns.

    Any ``_trust`` does, a Crew Mode card trust ("Trust this crew member",
    ``_trust_card_scoped``) included: trusting a worker trusts the subagents it
    spawns, so its ``spawn_run`` and their tools are not asked on the conductor's
    card. Every writer of a slot's stored ``approval_policy`` (what the spawn gate
    and each subagent read) goes through this; the Slack/background parent-trust
    gate counts the card trust through ``_slot_is_trusted`` like any slot trust. A
    revoke clears ``_trust`` and writes ``""``, so a child spawned after it asks
    again, and one already running loses its ``parent_policy=auto`` grant on its next
    tool request (``subagent_manager.turn_session.parent_trust_revoked``).
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
