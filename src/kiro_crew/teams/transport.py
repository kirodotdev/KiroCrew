"""Microsoft Teams as a concrete ``MessagingTransport``.

Wraps the low-level :class:`TeamsClient` (Bot Framework webhook inbound + REST
outbound) in the channel-neutral transport contract, so the Teams channel rides
the shared ``TurnDriver`` (credential/exfil redaction + tool-approval ladder +
SEL audit) instead of a hand-rolled turn loop.

Dependency direction is ``teams -> messaging`` (allowed); the neutral
``messaging`` package never imports ``teams``.

Teams is DM-only and fail-closed:

* Direct/personal rooms only: in a channel or group chat the bot's reply would
  land in front of non-authorized members, exposing tool output. Webex and
  Telegram admit a non-DM room only because each pairs a per-room session with an
  explicit room allow-list; Teams has neither, so non-personal scopes are denied
  and audited BEFORE authorization.
* No streaming: the renderer posts a typing indicator, keeps it alive, and
  delivers the final answer in one shot (``streaming=False``). Buttons ARE real
  here -- an Adaptive Card ``Action.Submit`` round-trips as a message activity --
  so ``[OPTIONS:]`` trailers become chips up to ``max_buttons``.

Attachment ingest lives in the DISPATCHER, not here, for the same reason it does
in Discord and Telegram: a message that arrives mid-turn is queued, and the turn
that eventually reads its files runs minutes later. Downloading in this frame
would either delete the temp files before that turn opens them or leak them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any, Awaitable, Callable, Iterable

from kiro_crew.messaging.tables import TABLE_POLICY_AUTO
from kiro_crew.messaging.transport import (
    ConfiguredChannelTarget,
    InboundMessage,
    MessagingTransport,
    TransportCapabilities,
)
from kiro_crew.sel import sel
from kiro_crew.teams.client import TEAMS_MAX_TEXT, TeamsClient, TeamsInbound, TeamsSendError
from kiro_crew.teams.service_urls import ServiceUrlStore

logger = logging.getLogger(__name__)

DispatchFn = Callable[[TeamsInbound], Awaitable[None]]

# Teams capabilities. Per the HONESTY CONTRACT on TransportCapabilities these
# describe what the CODE does today, not the platform ceiling:
#
# * ``edit=True`` -- the renderer really does rewrite its own activities
#   (``TeamsClient.update_message`` -> ``PUT .../activities/{id}``), which is what
#   carries the progress message and the mid-turn queue receipt.
# * ``streaming=False`` -- Teams' native token streaming exists but is 1:1 only,
#   throttled to one request per second, and dies at a hard two-minute ceiling
#   that an agentic turn routinely exceeds. A stream that dies mid-answer is worse
#   than one complete message, so the renderer buffers instead.
# * ``reactions=False`` -- the Connector exposes no operation for a bot to ADD a
#   reaction; ``messageReaction`` activities are inbound-only. This is why a Teams
#   steer is acknowledged with a message where Telegram and Discord use an emoji.
# * ``threads=False`` -- a Teams personal chat has no thread concept.
# * ``rich_blocks=True`` and ``max_buttons`` -- Adaptive Card ``Action.Submit``
#   round-trips as an ordinary message activity carrying ``value``, which is what
#   makes tool approval and [OPTIONS:] chips real here. The cap is BELOW Slack's
#   10: a card can hold more actions, but Adaptive Card actions render as a row of
#   full-width buttons in the mobile client, and overflow degrades to a numbered
#   text list through the shared ``apply_options_cap`` rather than being dropped.
# * ``files_inbound=True`` -- the dispatcher really does download the files on an
#   activity and hand their content to the turn, AFTER the personal-scope and
#   allow-list gates this module applies. Both Teams shapes are covered: a
#   personal-chat upload (pre-authorized ``downloadUrl``, fetched with no
#   credential) and an inline image (Teams-hosted ``contentUrl``, fetched with the
#   bot token only on a recognized Microsoft host). The Teams app MANIFEST must
#   also declare ``supportsFiles: true`` or the platform never sends the
#   ``file.download.info`` attachment at all -- see ``docs/teams-integration.md``.
# * ``files_outbound=True`` -- and deliberately narrower than the platform. The
#   renderer really does deliver a local raster the reply referenced, as an inline
#   ``data:`` URI attachment, which needs no hosting and no round trip. A
#   NON-image file would need the ``FileConsentCard`` flow: consent card, user
#   accept, an ``invoke`` activity carrying an upload URL, then a PUT of the bytes.
#   This channel's ingress fast-acks a message activity and dispatches a turn; it
#   handles no ``invoke``, so that flow is out of scope here and a non-inlinable
#   reference is refused visibly instead (see ``teams/attachments.py``).
TEAMS_CAPABILITIES = TransportCapabilities(
    streaming=False,
    edit=True,
    reactions=False,
    files_inbound=True,
    files_outbound=True,
    rich_blocks=True,
    threads=False,
    # Teams renders pipe tables literally.
    table_mode=TABLE_POLICY_AUTO,
    max_message_chars=TEAMS_MAX_TEXT,
    max_buttons=5,
    supports_proactive_send=True,
)


class TeamsTransport(MessagingTransport):
    """Concrete Teams transport over the low-level ``TeamsClient``."""

    channel_type = "teams"

    def __init__(
        self,
        client: TeamsClient,
        *,
        allowed_emails: Iterable[str] = (),
        dispatch: DispatchFn | None = None,
    ) -> None:
        self._client = client
        # Deny-by-default: freeze the (lowercased) allow-list so it can't
        # mutate under an in-flight decision.
        self._allowed: frozenset[str] = frozenset(e.lower() for e in allowed_emails if e)
        self._dispatch = dispatch
        # conversation_id -> serviceUrl, learned from inbound activities so
        # proactive/outbound sends can reach a known conversation. Backed by a
        # durable store: the Bot Framework offers no way to look a serviceUrl up,
        # so losing it on restart leaves every proactive path with nowhere to send
        # until the user speaks again.
        self._store = ServiceUrlStore()
        # Holds the warm-up task started in ``connect`` so it is not garbage
        # collected mid-flight; nothing awaits its result.
        self._store_warm: asyncio.Task[None] | None = None
        self.capabilities = TEAMS_CAPABILITIES

    @property
    def client(self) -> TeamsClient:
        """The underlying Teams client (held + exposed, not hidden)."""
        return self._client

    def service_url_for(self, conversation_id: str) -> str:
        """Return the last-seen serviceUrl for a conversation (or empty)."""
        return self._store.get(conversation_id)

    async def note_route(
        self, conversation_id: str, conversation_type: str, service_url: str, identity: str
    ) -> None:
        """Learn a routable address from a promptless activity (install / join).

        Wired onto ``TeamsClient.on_route``. The activity was already attested by the
        client, so what this adds is the SAME authorization ``receive`` applies before
        recording anything: personal scope only, and an allow-listed identity. A
        conversationUpdate from a channel or from a stranger records nothing, so this
        can never make an unauthorized destination advertise as reachable.
        """
        if conversation_type != "personal" or not identity:
            return
        if identity.lower() not in self._allowed:
            sel().log_api_access(
                caller=identity,
                operation="teams_transport.note_route",
                outcome="denied_not_allowed",
                source="teams",
            )
            return
        await self._store.ensure_loaded()
        if self._store.remember(conversation_id, service_url, identity=identity):
            await self._store.flush()
            logger.info("Teams: learned a proactive route from an install/join activity")

    # -- Tier-1 core --------------------------------------------------------
    async def send_message(
        self, conversation_id: str, content: str, thread_id: str | None = None
    ) -> str:
        # A proactive send can be the first thing this process does for a
        # conversation, so consult the persisted map before giving up.
        await self._store.ensure_loaded()
        service_url = self._store.get(conversation_id)
        try:
            mid = await self._client.send_message(conversation_id, content, service_url)
        except TeamsSendError as exc:
            # A PERMANENTLY undeliverable conversation loses its route. Without
            # this, a user who blocks the bot or removes the app leaves a row that
            # keeps advertising as reachable, so every later cron result and mirror
            # leg 403s into a red badge with nothing able to clear it. Only on a
            # permanent status -- a transient failure must not cost the route.
            if exc.conversation_is_gone and self._store.forget(conversation_id):
                logger.info("Teams: conversation is no longer reachable; dropping its route")
                await self._store.flush()
            raise
        return mid or ""

    async def resolve_conversation(self, user_id: str) -> str:
        # DM sessions are keyed on the Teams conversation id, which the inbound
        # activity supplies directly; there is no separate open-DM step.
        return user_id

    async def fetch_history(
        self, conversation_id: str, thread_id: str | None = None
    ) -> list[InboundMessage]:
        # Sessions persist via conversation_log instead.
        return []

    def configured_targets(self) -> list[ConfiguredChannelTarget]:
        targets: list[ConfiguredChannelTarget] = []
        for identity in sorted(self._allowed):
            available = bool(self._reachable_conversation(identity))
            targets.append(
                ConfiguredChannelTarget(
                    f"user:{identity}",
                    f"Teams DM · {identity}",
                    available=available,
                    unavailable_reason=(
                        "" if available else "Send a message to Kiro Crew in Teams first"
                    ),
                )
            )
        return targets

    async def resolve_configured_target(self, target_id: str) -> tuple[str, str | None] | None:
        kind, separator, value = target_id.partition(":")
        identity = value.lower()
        if kind != "user" or not separator or identity not in self._allowed:
            return None
        await self._store.ensure_loaded()
        conversation_id = self._reachable_conversation(identity)
        return (conversation_id, None) if conversation_id else None

    def _reachable_conversation(self, identity: str) -> str:
        """This identity's conversation, but only if it can actually be reached.

        ONE predicate behind both ``configured_targets`` (which advertises a
        destination) and ``resolve_configured_target`` (which sends to it).
        Splitting them let a conversation known without a serviceUrl advertise as
        available and then refuse to resolve, so the dashboard offered a target
        that could not receive anything.
        """
        conversation_id = self._store.conversation_for(identity)
        return conversation_id if conversation_id and self._store.get(conversation_id) else ""

    # -- Outbound authorization --------------------------------------------
    def may_send_to(
        self, conversation_id: str, thread_id: str | None = None, *, principal: str = ""
    ) -> bool:
        """Authorize a proactive send by reverse-mapping the conversation.

        Teams links persist a conversation id, not a principal, so the roster is
        reached through ``_reachable_conversation`` -- the same predicate
        ``resolve_configured_target`` and ``configured_targets`` answer from, which
        is what keeps "may I send here" and "where do I send" from drifting. A
        conversation is authorized only while some CURRENTLY allow-listed identity
        still resolves onto it, so dropping an identity from the allow-list stops
        its proactive traffic.

        Reads the ``ServiceUrlStore`` synchronously, as this seam requires, which
        means it has to tell "not in the store" from "the store is not read yet".
        Those look identical through the accessors and mean opposite things: the
        store is PERSISTED and ``send_message`` awaits its own ``ensure_loaded``, so
        a route sitting on disk is deliverable even on the first send of a process.
        The transport is also registered BEFORE ``connect``, and ``connect`` only
        starts the warm-up rather than awaiting it, so an unloaded store is a real
        window rather than a corner case.

        An unloaded store is a REFUSAL rather than a permit, which is safe only
        because the gateway awaits :meth:`warm_routes` before registering this
        transport: nothing can reach this check with the store unread. Permitting
        instead would leave a startup window in which a recipient already removed
        from the allow-list is still reachable, because ``send_message`` awaits its
        own ``ensure_loaded`` and would reload the persisted route and deliver.
        """
        if not conversation_id:
            return False
        if not self._store.loaded:
            # Unreachable while warm_routes precedes registration; kept as the
            # fail-closed floor if that ordering is ever changed.
            return False
        return any(
            self._reachable_conversation(identity) == conversation_id for identity in self._allowed
        )

    async def warm_routes(self) -> None:
        """Read the persisted route store, before anything can consult it.

        Awaited by the gateway BEFORE the transport is registered, which is what
        lets :meth:`may_send_to` treat an unloaded store as a refusal: the
        authorization seam is synchronous and cannot load it, so a window where the
        store is empty is a window where either a revoked recipient is reachable or
        a deliverable send is refused. Closing the window removes the choice.

        Does not block the loop despite the disk read: ``ensure_loaded`` does its
        own ``asyncio.to_thread``. Idempotent, so the warm-up task ``connect``
        starts remains harmless.
        """
        await self._store.ensure_loaded()

    # -- Lifecycle ----------------------------------------------------------
    async def connect(self) -> None:
        await self._client.connect()
        # Warm the routing store WITHOUT awaiting it. ``configured_targets`` is a
        # sync accessor the dashboard calls to list link destinations, so it cannot
        # load the store itself -- and until something does, a restart reports every
        # Teams target as "send a message first" even though the route is persisted,
        # which is the exact outcome persisting it exists to prevent. A task rather
        # than an await because ``connect`` is reached from the gateway boot path,
        # where no new step may block the socket bind.
        self._store_warm = asyncio.create_task(self._store.ensure_loaded())

    async def disconnect(self) -> None:
        await self._client.close()

    # -- Inbound adapter ----------------------------------------------------
    def _resolve_identity(self, inbound: "TeamsInbound") -> str:
        """The sender identity to authorize by and key sessions on.

        Teams may supply a UPN, an AAD object id, or BOTH, and the allow-list may hold
        either form -- so this picks the form the list actually authorizes rather than a
        fixed preference order. Preferring the email unconditionally refuses a user whose
        OBJECT ID is allow-listed whenever Teams also sent an email, which is the ordinary
        shape for a guest account and for any tenant that allow-lists by object id: the
        entry is right there in the list and the sender is denied anyway.

        Order still matters when both match: the email wins, so an install that lists
        both forms keeps the human-readable session key it already had.

        An UNAUTHORIZED sender falls back to email-then-object-id, so the deny audit
        names them the way an operator would recognise.
        """
        email = inbound.user_email
        object_id = inbound.aad_object_id
        for candidate in (email, object_id):
            if candidate and candidate.lower() in self._allowed:
                return candidate
        return email or object_id

    def authorize(self, msg: InboundMessage) -> bool:
        """Allow-list (email or AAD object id), deny-by-default.

        ``msg.user_id`` is the resolved sender identity (email when Teams
        supplies it, else the AAD object id). Empty allow-list authorizes
        nobody.
        """
        allowed = bool(msg.user_id) and msg.user_id.lower() in self._allowed
        if not allowed:
            sel().log_api_access(
                caller=msg.user_id or "unknown",
                operation="teams_transport.authorize",
                outcome="denied",
                source="teams",
            )
        return allowed

    async def receive(self, raw_envelope: Any) -> None:
        """Normalize -> scope gate -> authorize -> dispatch.

        The low-level client hydrates activities into ``TeamsInbound``; this
        adapter enforces direct-rooms-only + deny-by-default auth, maps onto the
        neutral ``InboundMessage``, and hands the richer ``TeamsInbound``
        (carrying ``service_url``) to the turn dispatcher.
        """
        if not isinstance(raw_envelope, TeamsInbound):
            return
        inbound = raw_envelope
        # A card click carries a payload instead of text, and a file upload carries
        # attachments and no text; both must survive this guard or every Approve/Deny
        # press and every sent file is discarded. Neither skips anything: both still
        # pass every gate below -- scope, identity, allow-list -- and attachments are
        # not even LOOKED at until those have passed.
        if not inbound.text and not inbound.is_card_action and not inbound.attachments:
            return
        await self._store.ensure_loaded()
        # Direct/personal rooms only, fail closed: a reply in a channel/group
        # would expose tool output to non-authorized members. Deny + audit.
        if inbound.conversation_type != "personal":
            sel().log_api_access(
                caller=self._resolve_identity(inbound) or "unknown",
                operation="teams_transport.receive",
                outcome="denied_non_personal_scope",
                source="teams",
            )
            return
        # Resolve a stable sender identity. Fail closed if neither form is present.
        identity = self._resolve_identity(inbound)
        if not identity:
            sel().log_api_access(
                caller="unknown",
                operation="teams_transport.receive",
                outcome="denied_unresolved_identity",
                source="teams",
            )
            return
        msg = InboundMessage(
            channel_type="teams",
            user_id=identity,
            conversation_id=inbound.conversation_id,
            text=inbound.text,
            thread_id=None,
        )
        if not self.authorize(msg):
            return
        # Record the route only AFTER every gate has passed, so a denied group
        # conversation or an unauthorized sender leaves no durable trace and
        # cannot drive eviction of a legitimate row. Persist only when something
        # actually changed: a conversation's serviceUrl is stable for its
        # lifetime, so the common inbound message writes nothing.
        if self._store.remember(inbound.conversation_id, inbound.service_url, identity=identity):
            await self._store.flush()
        if self._dispatch is None:
            return
        # Hand the dispatcher the identity the ALLOW-LIST authorized, rather than letting
        # it re-derive one: the two forms are not interchangeable, so a second derivation
        # keyed the session on the UPN for a user admitted on their object id.
        inbound = replace(inbound, resolved_identity=identity)
        # Raw attachment descriptors ride along untouched. The DISPATCHER downloads
        # them, and only for a message it is about to turn into a prompt: a
        # mid-turn arrival is queued with its descriptors and re-ingested when the
        # drained turn runs, so nothing is fetched for a message that waits and
        # nothing is unlinked before its reader opens it.
        await self._dispatch(inbound)
