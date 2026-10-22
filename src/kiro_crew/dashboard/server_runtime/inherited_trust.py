"""Tearing down the trust a safety override handed to the sessions it covered.

The pair the safety override calls back: dropping every inherited grant when the
override expires or is revoked, and blanking the inherited slot policies before a new
ceiling publishes (with the restore that puts them back if the ceiling still permits).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        DashboardState,
        effective_session_key,
        logger,
    )


def _clear_override_derived_trust(state: "DashboardState", source: str) -> None:
    """Drop every INHERITED grant of the expiring override. State only, no loop.

    Module-level (not a ``start_dashboard`` closure), like the Slack notifier, so the
    seam is directly testable against a real ``DashboardState``.

    Split out of the notifier because the two halves have different
    deadlines. ``subagent_manager.admission.parent_trusted`` reads a session's
    ``approval_policy == "auto"`` DIRECTLY -- it consults no flag in
    ``safety_override`` -- so until this has run a spawn is auto-approved against
    a ceiling that already denies, and an already-launched subagent is not
    un-spawned by anything later. That makes this the half a policy revocation has
    to complete synchronously, on whichever thread installed the ceiling, while
    the broadcasts and DMs below can be scheduled onto the loop.

    Safe off the event loop: it touches the slot dict and the session store and
    nothing loop-affine. Idempotent, so the notifier re-running it costs nothing.
    """
    # Slots carrying STANDING trust keep their policy: that is a separate,
    # longer-lived decision than the expiring override, and it is also what must
    # survive the channel-trust revoke below.
    standing_trust: set[str] = set()
    if state.sessions is not None:
        # Snapshot the slots before iterating. This runs on whatever thread
        # installed the denying ceiling, and the loop keeps creating and removing
        # slots -- so iterating the live dict raises "dictionary changed size
        # during iteration" and ABORTS the teardown partway, leaving the slots it
        # had not reached yet at ``approval_policy="auto"`` with nothing to come
        # back for them. A partial revocation is the failure this whole path
        # exists to prevent, so the iteration cannot be the thing that breaks it.
        for slot in list(state._slots.values()):
            if slot._trust or slot._trust_reads:
                # Excluded from the channel-trust revoke below, via the SAME
                # derivation the reset uses: a channel-born slot's turns run on
                # the channel's own session key, so a `dashboard:<slot>` spelling
                # names a key nothing on that path reads.
                standing_trust.add(effective_session_key(slot))
            else:
                # The SAME derivation the grant used. A channel-born slot's
                # turns run on the channel's own session key, which is what
                # `linked_session_key` holds, so clearing `dashboard:<slot>`
                # here cleared a key nothing on the channel path ever reads:
                # the TTL could not expire the grant it had handed out, which
                # is worse than a missing off-switch because the operator was
                # told it was time-bounded.
                state.sessions.set_approval_policy(effective_session_key(slot), "")
    # Slack cleanup — isolated so failures don't block dashboard operations
    try:
        # From `messaging`, not `slack.handler`: the grant is channel-neutral.
        # This revokes the approval_policy half as well as the mapping, which is
        # what a CHANNEL session needs -- the loop just above resets only the
        # dashboard's own slots, and a subagent reads the policy rather than the
        # mapping, so policy left at "auto" outlives the override it belonged to.
        # ``keep_policy`` is what stops this from undoing the preservation above:
        # a Trust press can file a ``dashboard:`` key in the shared grant, and
        # resetting its policy here would revoke standing trust nobody expired.
        from kiro_crew.messaging.session_trust import clear_trusted_sessions

        clear_trusted_sessions(keep_policy=standing_trust)
    except Exception:
        logger.debug("Could not clear trusted sessions", exc_info=True)


def _suspend_override_derived_trust(state: "DashboardState") -> Callable[[], None] | None:
    """Blank the grant's inherited slot policies BEFORE a new ceiling publishes.

    Returns the restore. ``_clear_override_derived_trust`` runs once a deny
    has been RESOLVED against the new ceiling, but resolving is a governance read
    and the ceiling is already published while it runs -- and
    ``admission.parent_trusted`` reads the slot's approval policy directly, not
    ``is_active()``, so for that whole window a spawn from a slot carrying the
    override's inherited ``"auto"`` was auto-approved against a ceiling that may
    deny. This is the pre-publication half that closes it.

    Only the override's OWN inherited trust is suspended: slots with standing
    ``_trust`` / ``_trust_reads`` are left alone (a Trust press is a separate,
    longer-lived decision no yolo ceiling touches), and only slots currently at
    ``"auto"`` are recorded, so the restore puts back exactly what was taken. The
    shared channel-trust mapping is NOT suspended: ``is_session_trusted`` gates a
    tool in an already-running turn (recoverable, audited), while this guards
    spawn admission (unrecoverable) -- the same asymmetry the revoke ordering rests
    on. Same thread contract as the clear: slot dict + session store only.
    """
    if state.sessions is None:
        return None
    suspended: list[tuple[str, str]] = []
    for slot in list(state._slots.values()):
        if slot._trust or slot._trust_reads:
            continue
        key = effective_session_key(slot)
        try:
            if state.sessions.get_approval_policy(key) != "auto":
                continue
            state.sessions.set_approval_policy(key, "")
        except Exception:
            logger.debug("could not suspend inherited trust on %s", key, exc_info=True)
            continue
        suspended.append((slot.key, key))
    if not suspended:
        return None

    def _restore() -> None:
        # Restore is CONDITIONAL, per slot, on the slot still being in the state it
        # was suspended from. The governance read in between is long enough for
        # the operator to have changed a slot's mode -- picked ``trust_reads`` or
        # ``trust``, or ``normal`` -- and each of those writes this same policy.
        # Writing ``"auto"`` over a ``trust_reads`` slot would upgrade read-only
        # trust to full auto-approve on the read ``parent_trusted`` makes; over a
        # ``normal`` slot it would undo an explicit revoke. So a slot gets its
        # ``"auto"`` back only if it still exists, still carries no standing trust
        # flag, and its policy is still the empty string this suspension left.
        for slot_key, key in suspended:
            slot = state._slots.get(slot_key)
            if slot is None or slot._trust or slot._trust_reads:
                continue
            try:
                if state.sessions.get_approval_policy(key) != "":
                    continue
                state.sessions.set_approval_policy(key, "auto")
            except Exception:
                logger.debug("could not restore inherited trust on %s", key, exc_info=True)

    return _restore
