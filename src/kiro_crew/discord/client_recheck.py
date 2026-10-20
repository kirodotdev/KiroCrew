"""The mid-send destination re-check: may a waited send still reach where it goes?

A wait inside the REST ladder is time in which an operator can withdraw a destination,
so the ladder asks again after every wait it serves (``_wait_then_revalidate``): the
operator's ``channels`` ceiling for Discord first, then the transport's live rosters
through ``still_permitted``, failing closed. :class:`SendPermission` carries each answer
with its ground, ``_guarded_destination`` names the channel a route discloses to, and
every decision is recorded in the SEL. Each function here whose first parameter is
``self`` is bound as the ``DiscordClient`` attribute of the same name in
:mod:`kiro_crew.discord.client`, which keeps the client's state, so ``self.<name>(...)``
calls and instance and class patches reach it as they reach a method defined in the
class body. They read ``_evicted_dm_channels`` and ``still_permitted``. New re-check
work goes here.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kiro_crew.discord.client_rest import _ID_SEGMENT_RE, _ROUTE_SEP
from kiro_crew.messaging.identity import channel_outbound_permitted
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.discord.client import DiscordClient

logger = logging.getLogger("kiro_crew.discord.client")


#: ``DiscordApiResult.detail`` when authorization ended while the ladder was
#: suspended in one of its own waits. Distinct from every other blocked reason so
#: a caller, and an operator reading the log, can tell a withdrawn destination
#: from a tripped breaker.
_REVOKED_DETAIL = "destination authorization withdrawn mid-send"

#: ``DiscordApiResult.detail`` when nothing available here can ATTRIBUTE the
#: destination: no current roster places it and no DM pairing names its peer. The
#: send still stops, because at an egress boundary "cannot tell" reads as no, but
#: the two grounds are different events and an operator debugging a dropped
#: notification is owed the one that happened. Saying a destination cannot be
#: placed is not the claim that someone withdrew it.
_UNATTRIBUTABLE_DETAIL = "destination unattributable mid-send: no roster entry, no DM pairing"

#: ``DiscordApiResult.detail`` when the re-check ITSELF failed: the installed
#: predicate raised, so no authority answered about this destination at all. The
#: send still stops, and that is the whole point of failing closed, but this is a
#: third event again -- a roster that refused, a destination nothing could place,
#: and a check that could not run are three different things to go and fix.
_UNANSWERABLE_DETAIL = "destination unanswerable mid-send: the authorization check failed"


@dataclass(frozen=True)
class SendPermission:
    """One mid-send answer about one destination, carrying its own ground.

    The transport's rosters are the only thing that knows WHY a destination is
    refused, and the ladder is the only thing that reports it, so the reason
    travels with the decision rather than being guessed at the other end. A
    refusal's ``detail`` reaches the caller as ``DiscordApiResult.detail``.

    ``permitted`` is the whole decision; ``detail`` is empty when permitted. The
    four named constructors are the only grounds a destination re-check has, and
    they exist so the refusal wording lives here once instead of in every module
    that answers.
    """

    permitted: bool
    detail: str = ""

    @classmethod
    def allow(cls) -> SendPermission:
        """This destination may still be written to."""
        return cls(True)

    @classmethod
    def revoked(cls) -> SendPermission:
        """An authority that CAN place this destination refuses to admit it."""
        return cls(False, _REVOKED_DETAIL)

    @classmethod
    def unattributable(cls) -> SendPermission:
        """Nothing available can place this destination, so it cannot be judged."""
        return cls(False, _UNATTRIBUTABLE_DETAIL)

    @classmethod
    def unanswerable(cls) -> SendPermission:
        """The check itself failed, so no authority answered about this at all."""
        return cls(False, _UNANSWERABLE_DETAIL)


def _guarded_destination(path: str) -> str:
    """The channel id whose authorization a re-check must re-read, or ``""``.

    A route is guarded when it names a channel as its major parameter, because
    that channel IS the disclosure boundary: a message, an edit, an upload, a
    reaction and a thread creation all become visible to whoever can read it.
    The id is the segment after ``channels``, matched with the same rule the
    rate-limit route key uses so the two cannot drift.

    Two families answer ``""``, because the PATH names no channel. That is not the
    same as being unguarded:

    * ``/interactions/`` and ``/webhooks/`` -- a button press's own reply. Its
      destination rides inside an opaque token, so the path cannot yield one, but
      the dispatcher holds it and passes it down, and the mid-send re-check then
      reads BOTH authorities against it: the ``channels`` ceiling first, then the
      live rosters. Two of those replies pass no destination on purpose -- the ones
      that announce a refusal, which must not be gated by the authority they are
      announcing -- and those are the only genuinely unguarded interaction routes;
    * every route that names no channel at all (the gateway URL, ``/users/@me``
      and its DM-channel creation) and whose caller supplies none either. Creating
      a DM channel discloses nothing to anybody; the send INTO it is a
      ``/channels/`` route and is guarded.
    """
    segments = path.split(_ROUTE_SEP)
    for index, segment in enumerate(segments):
        if index and segments[index - 1] == "channels" and _ID_SEGMENT_RE.match(segment):
            return segment
    return ""


async def _destination_still_permitted(
    self: DiscordClient, path: str, destination_hint: str = ""
) -> SendPermission:
    """May this route still reach its destination RIGHT NOW? Fails closed.

    Asked again after each of the ladder's own waits, because a wait is time
    during which an operator can withdraw the destination: the allow-list edit
    and the ``channels`` governance flip both take effect on the next read, and
    without this the next read is after the message has already landed.

    Two authorities, both consulted, neither sufficient alone:

    * the operator's ``channels`` ceiling for Discord, which the messaging seam
      owns for every channel, read through the OUTBOUND entry point so the row
      it leaves names the direction it decided. Global to the channel, so it is
      answerable from the client alone and is read even with no predicate wired;
    * the transport's live rosters via :attr:`still_permitted`, which is where
      the per-destination decision lives. The client holds no roster of its
      own on purpose -- duplicating one here is how outbound and inbound drift
      apart.

    The route is classified FIRST, but only to settle WHICH id names the
    destination: a route naming a channel in its path uses that, a route naming
    none uses the ``destination_hint`` its caller supplied, and a route naming
    neither reads nothing. Both authorities then apply to whichever id was
    settled on, in that order, with the ROSTER LAST -- the governance read is an
    await, so a roster reading taken before it describes a destination that may
    already have been withdrawn.

    Neither authority is read on the happy path either: this runs solely after a
    wait was served, so a send that never waits pays nothing for it.

    Every outcome is audited, allow as well as refusal. This is a network egress
    decision taken on an already-composed, user-visible message, and the ladder's
    own result carries it no further than the caller, so the SEL row is the record
    it leaves. Only a route that actually waited reaches here, so the rows are
    paced by rate limits and back-offs rather than by traffic.
    """
    # The path names the destination when it can; otherwise the caller does.
    # An interaction or webhook reply carries its destination inside an opaque
    # token, so its path cannot yield one while the dispatcher holds it.
    destination = _guarded_destination(path) or destination_hint
    if not destination:
        return SendPermission.allow()
    if not await channel_outbound_permitted("discord"):
        self._audit_mid_send_decision(destination, "channels_ceiling", "denied")
        return SendPermission.revoked()
    if self.still_permitted is None:
        # The ceiling is the whole decision when no roster is installed, so it
        # is the allow that gets recorded.
        self._audit_mid_send_decision(destination, "channels_ceiling", "allowed")
        return SendPermission.allow()
    # The governance read above is itself an await, so any roster reading taken
    # before it describes a destination that could have been withdrawn since.
    # This read is the LAST thing that happens before the caller writes, which
    # is the whole contract of this predicate: a cheaper ordering that answers
    # from a reading taken before a suspension reintroduces the defect one
    # layer up.
    return self._roster_still_permits(destination)


def _roster_still_permits(self: DiscordClient, destination: str) -> SendPermission:
    """Ask the transport's live rosters about one destination. Fails closed.

    Synchronous and in-memory by contract, so it is the half of the re-check a
    deadline-bound route can afford. A predicate that cannot answer is read as
    a refusal: this is a network egress boundary.

    The predicate's own answer carries the refusal's ground, because the rosters
    are the only thing that can tell a destination an operator withdrew from one
    that nothing there can place.
    """
    if self.still_permitted is None:
        return SendPermission.allow()
    try:
        permission = self.still_permitted(destination)
        if permission.permitted:
            self._audit_mid_send_decision(destination, "roster", "allowed")
            return permission
        # A destination whose pairing THIS process dropped to honour the cap is
        # refused for want of a peer, which is not the same event as an operator
        # withdrawing it. Reading them as one sends whoever is debugging it after
        # a policy change that never happened.
        authority = "pairing_truncated" if destination in self._evicted_dm_channels else "roster"
        self._audit_mid_send_decision(destination, authority, "denied")
        return permission
    except Exception:
        logger.warning(
            "Discord REST: the destination predicate raised while re-checking a "
            "route after a wait; refusing the send, and reporting that the check "
            "itself could not answer",
            exc_info=True,
        )
        self._audit_mid_send_decision(destination, "predicate_raised", "denied")
        return SendPermission.unanswerable()


def _audit_mid_send_decision(
    self: DiscordClient, destination: str, authority: str, outcome: str
) -> None:
    """Record one mid-send egress decision, naming which authority decided.

    Best-effort on purpose: the decision itself already stands, and letting an
    unwritable audit store raise here would turn every throttled send on a
    governed install into an exception instead of a clean answer.
    """
    try:
        sel().log_api_access(
            caller=destination,
            operation=f"discord_client.mid_send_revocation.{authority}",
            outcome=outcome,
            source="discord",
        )
    except Exception:
        logger.warning(
            "Discord REST: could not record the mid-send decision audit event",
            exc_info=True,
        )


async def _wait_then_revalidate(
    self: DiscordClient, path: str, delay: float, destination_hint: str = ""
) -> SendPermission:
    """Serve one of the ladder's back-offs, then re-ask authorization.

    The two belong together: a back-off served without the re-check after it
    is exactly the window this guards, so pairing them in one helper keeps a
    new wait site from being added with only half of the pair.
    """
    await asyncio.sleep(delay)
    return await self._destination_still_permitted(path, destination_hint)
