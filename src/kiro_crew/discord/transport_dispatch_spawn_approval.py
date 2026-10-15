"""Spawn-approval delivery: post a spawn's Approve/Deny prompt into the Discord
conversation that asked for it, and wait for the press.

``deliver_spawn_approval`` is the hook the channel-neutral
``messaging.spawn_approval_delivery`` seam calls; ``_spawn_chat_target`` turns the parent
session key back into a conversation. When the prompt is posted, the governance ceiling
is re-read on the direct route after its DM channel opens
(``_spawn_prompt_channel_permitted``) and the rosters are re-read on every route
(``_spawn_prompt_destination_permitted``); when an unpressed wait ends,
``_spawn_approval_actionable`` re-reads the rosters, and the ceiling there is the seam's
to read. The press resolves through ``on_interaction``
(``transport_dispatch_interactions``), like a tool approval.

Each function here whose first parameter is ``self`` is a ``DiscordDispatcher``
method: the class binds it by name, so it reads and writes the dispatcher's own
state on the instance, and a patch of the method on the class or on an instance
reaches every caller.

Instance fields read (a call on one included): ``_allowed``, ``_allowed_threads``,
``client``, ``transport``.

Instance fields assigned, or whose mapping is changed: none.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING

from kiro_crew.constants import DENY_CAUSE_APPROVAL_TIMEOUT
from kiro_crew.discord.renderer import _STYLE_DANGER, _STYLE_SUCCESS, DiscordApprovalDecider
from kiro_crew.discord.transport_dispatch_origin import _CHANNEL, _CHAT_TYPE_THREAD
from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.identity import channel_inbound_permitted
from kiro_crew.messaging.link import CHAT_TYPE_DIRECT, parse_session_key
from kiro_crew.messaging.spawn_approval_delivery import unpressed_wait_answer
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.discord.transport_dispatch import DiscordDispatcher

#: The dispatcher's one logger, named for the facade module operators filter on.
logger = logging.getLogger("kiro_crew.discord.transport_dispatch")


async def deliver_spawn_approval(
    self: DiscordDispatcher, request_id: str, description: str, parent_session_key: str
) -> bool | None:
    """Post a spawn-approval prompt to the ORIGINATING Discord conversation.

    Registered into the channel-neutral
    :mod:`~kiro_crew.messaging.spawn_approval_delivery` seam so the single
    host spawn gate can reach the same Approve/Deny buttons the main-agent
    tool ladder already uses here. Returns the user's decision
    (``True``/``False``), or ``None`` to tell the gate "not surfaced here,
    fall through to Slack/dashboard": for a key this dispatcher cannot turn
    back into a conversation (a ``unified`` dm_scope drops the peer, a
    non-``discord`` key, an unparseable one), when the client is not up, when
    the channels governance profile denies this channel, when the
    destination's authorization has since been withdrawn, or when the post
    fails.

    The wait is the SAME deny-by-default one a tool prompt uses
    (:class:`DiscordApprovalDecider`, ``APPROVAL_TIMEOUT_S``): the press
    resolves through the ``on_interaction`` ``a:`` branch exactly as a tool
    approval does, so a spawn id (``spawn:<agent_id>``) cannot collide with an
    opaque tool id in a registry keyed by ``session_key:request_id``.

    The prompt is armed under ``parent_session_key`` VERBATIM (its ``:genN``
    suffix included), while a press recomputes the key from the LIVE
    conversation (``_inbound_session_key``). Anything that moves that key
    between the spawn and the press — a generation rotation from ``!new``, an
    idle or daily reset, or a resumed session taking the channel over — means
    the recomputed key does not match the armed one, the press resolves
    nothing, and the prompt deny-by-defaults at its timeout (the user sees
    "already expired"). This mirrors how a mid-run tool prompt behaves across a
    rotation, and it stays a DENY: the prompt WAS surfaced, so ``False`` is a
    real decision and the gate refuses the spawn on it rather than re-offering
    it on Slack/dashboard. An elapsed wait falls through in one case only, when
    AUTHORIZATION ended during it and no press could have answered: the
    destination rosters are re-read here (``_spawn_prompt_destination_permitted``,
    the pair that gated the post), and the channels ceiling is read by the seam
    (``unpressed_wait_answer``), which owns that reading for every channel.
    """
    client = self.client
    if client is None:
        return None
    target = self._spawn_chat_target(parent_session_key)
    if target is None:
        # A key this channel does not own or cannot address (unified DM
        # bucket, non-discord key, malformed). Let the gate fall through.
        return None
    channel_id, thread_id, user_id, session_key = target
    if not channel_id:
        # A direct route names its PEER, not a channel, so the DM channel has
        # to be opened before anything can be posted into it. Resolved here,
        # ahead of the authorization check below, so that check has no
        # suspension point between it and the send it guards.
        try:
            channel_id = await client.create_dm_channel(user_id)
        except Exception:
            logger.warning(
                "Discord: could not open a DM channel for the spawn-approval prompt for %s",
                request_id,
                exc_info=True,
            )
            return None
        # The seam reads the operator's ceiling once, before it invokes any
        # hook, which is the authority for entering here at all. This open is
        # a full round trip INSIDE the hook, so the seam's answer can go stale
        # across it and the seam cannot see that happen. Re-read on this route
        # only: everything from here to the send is synchronous, which makes
        # this the latest point a read can speak for, and a thread route
        # arrives with its channel already resolved and never suspends.
        if not await self._spawn_prompt_channel_permitted(request_id):
            return None
    if not channel_id:
        return None

    rid = str(request_id)
    key = DiscordApprovalDecider.key(session_key, rid)
    # Detached: the gate awaits this in its own task and the agent is told to
    # end its turn, so the arming turn's sweep must not close the window while
    # the user is still looking at the prompt.
    nonce = DiscordApprovalDecider.register_nonce(key, detached=True)
    components = [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": _STYLE_SUCCESS,
                    "label": "✅ Approve",
                    "custom_id": f"a:{rid}:{nonce}:1",
                },
                {
                    "type": 2,
                    "style": _STYLE_DANGER,
                    "label": "🚫 Deny",
                    "custom_id": f"a:{rid}:{nonce}:0",
                },
            ],
        }
    ]
    # ``description`` is the gate's own ``spawn_run(<task-preview>)`` string.
    # The upstream credential pass scans the literal text, so a secret split by
    # zero-width format characters survives it and Discord reassembles it on
    # display: clear the preview in display form over both outbound redactors,
    # the same pass embed alt text and renderer-borne text go through. It is
    # synchronous, so it adds no suspension point between the destination check
    # below and the send. Two markdown concerns ride along, because Discord
    # renders the message as markdown and the task text is agent-authored:
    # collapse whitespace so a multi-line preview stays one block, and drop
    # backticks so the preview cannot close the fence it sits in and style the
    # rest of the message.
    detail, _ = redact_for_display(
        " ".join((description or "spawn_run").split()).replace("`", "'"),
        lambda s: redact_credentials(redact_exfiltration_urls(s)[0])[0],
    )
    if not self._spawn_prompt_destination_permitted(channel_id, thread_id, user_id):
        # Authorization for this destination was withdrawn between the turn that
        # asked for the spawn and this delivery. Retire the armed nonce and fall
        # through, so the spawn is still answerable on Slack/dashboard.
        DiscordApprovalDecider.retire(key)
        logger.info(
            "Discord: not posting the spawn-approval prompt for %s; the "
            "originating conversation is not an authorized destination",
            rid,
        )
        return None
    try:
        posted = await client.send_message(
            channel_id,
            f"🔐 Approve sub-agent spawn?\n```\n{detail}\n```",
            components=components,
        )
    except Exception:
        # Could not surface it: retire the armed nonce and fall through so the
        # spawn can still be answered on Slack/dashboard rather than denied by a
        # timeout nobody could see.
        DiscordApprovalDecider.retire(key)
        logger.warning(
            "Discord: failed to post the spawn-approval prompt for %s",
            rid,
            exc_info=True,
        )
        return None
    if not posted:
        # This client reports a failed send by RETURNING no message id rather
        # than by raising (a revoked token, a dead network, a channel it cannot
        # write to), so the ``except`` above does not cover it. Same conclusion:
        # nothing was surfaced, so fall through instead of waiting out the
        # decision window on a prompt nobody can see and calling that a denial.
        DiscordApprovalDecider.retire(key)
        logger.warning(
            "Discord: the spawn-approval prompt for %s was not accepted by the "
            "channel; falling through",
            rid,
        )
        return None

    decider = DiscordApprovalDecider(session_key=session_key)
    approved = bool(await decider(SimpleNamespace(request_id=rid)))
    if not approved and decider.last_deny_cause == DENY_CAUSE_APPROVAL_TIMEOUT:
        # Nobody pressed. The checks above spoke for the moment of the post;
        # the wait outlives them by minutes, and ``on_interaction`` drops
        # every press once this peer (or thread) leaves its roster, and every
        # press but an explicit reject once the channels ceiling closes. A
        # wait that elapsed after either could not have been answered, so
        # ``False`` would refuse the spawn in the operator's name. Re-read the
        # rosters here -- the same check that gated the post -- and leave the
        # ceiling's reading to the seam, which owns it for every channel. A
        # press, reject included, returns its own answer below.
        if not await self._spawn_approval_actionable(channel_id, thread_id, user_id):
            logger.info(
                "Discord: the spawn-approval prompt for %s went unanswered and "
                "its destination is no longer authorized, so no press could have "
                "resolved it; falling through to the Slack/dashboard path",
                rid,
            )
            return None
        return await unpressed_wait_answer(_CHANNEL, rid)
    return approved


async def _spawn_prompt_channel_permitted(self: DiscordDispatcher, request_id: str) -> bool:
    """Is the operator's channels ceiling open for this channel RIGHT NOW?

    The delivery seam reads this once before it invokes any hook, so entering
    this dispatcher at all is already gated and this is not that authority
    again. It answers a narrower question the seam cannot: the peer's DM
    channel is opened INSIDE the hook, that open is a full round trip, and the
    ceiling can close across it.

    Closing matters because a denied channel drops the Approve press that
    would answer a prompt -- only an explicit reject is exempt there -- so a
    prompt posted under a deny can never be answered, its wait
    deny-by-defaults at the timeout, and the gate reads that elapsed wait as a
    decision nobody made. Answering False makes the delivery fall through
    instead, leaving the spawn answerable on Slack and the dashboard.
    """
    if await channel_inbound_permitted("discord"):
        return True
    logger.info(
        "Discord: not posting the spawn-approval prompt for %s; the channel is "
        "denied by channels governance policy",
        request_id,
    )
    return False


def _spawn_prompt_destination_permitted(
    self: DiscordDispatcher, channel_id: str, thread_id: str, user_id: str
) -> bool:
    """May a spawn-approval prompt be posted here RIGHT NOW? Fails closed.

    The gate can hold a spawn for as long as its approval takes, so the
    authorization that admitted the originating turn is not evidence about this
    instant: an operator can drop the peer from ``discord.allowed_user_ids``, or
    a thread from the thread roster, while the prompt is still being prepared.
    The prompt carries a task preview, so it is a send that must be re-decided
    against the LIVE rosters rather than the one the turn started under.

    Called SYNCHRONOUSLY with no suspension point between it and the send it
    gates — an await in between would reopen the window it closes.

    Two authorities, both consulted, neither sufficient alone:

    * this dispatcher's own live rosters, which are exactly the ones a PRESS is
      judged by in ``on_interaction`` (``_authorized`` for the peer of a direct
      route; ``_allowed_threads`` for a thread route), so a prompt is never
      posted where its own button could not be honored;
    * ``transport.may_send_to``, the transport's revocation-at-egress decision,
      when a transport is wired. Absent (no transport, as in a unit harness) the
      rosters above stand alone; a raise is read as a denial.
    """
    if thread_id:
        if not self._allowed or thread_id not in self._allowed_threads:
            return False
    elif not self._authorized(user_id):
        return False
    gate = getattr(self.transport, "may_send_to", None)
    if gate is None:
        return True
    try:
        # A thread route is recognised by its CONVERSATION id, which for a
        # Discord thread is the thread's own snowflake; a direct route carries
        # no usable conversation id for the roster, so it is judged by its
        # principal. This is the split ``may_send_to`` itself documents.
        return bool(gate(channel_id, thread_id or None, principal=user_id))
    except Exception:
        logger.warning(
            "Discord: may_send_to raised for the spawn-approval destination; "
            "treating it as revoked",
            exc_info=True,
        )
        return False


async def _spawn_approval_actionable(
    self: DiscordDispatcher, channel_id: str, thread_id: str, user_id: str
) -> bool:
    """Whether some authorized user can still answer this spawn prompt.

    Rosters and egress only — NOT the operator's channels ceiling. The
    ceiling is the seam's authority: ``unpressed_wait_answer`` reads it on the
    timeout path, and the pre-post caller reads it once before the send. This
    helper is deliberately ceiling-free so neither reading is duplicated here.

    ``_interaction_actionable`` awaits ``is_thread_channel`` (an uncached REST
    GET on a cold cache); the SYNCHRONOUS roster/egress check runs LAST, after
    that await, so a peer or thread dropped during classification is still
    caught by a check with no suspension point before the caller acts on it.
    """
    principal = user_id
    if thread_id:
        # A thread key intentionally carries no user id. Actionability is
        # existential there: at least one live roster member must remain who
        # could pass the same interaction gate as a real button press.
        principal = next(iter(self._allowed), "")
    if not await self._interaction_actionable(principal, thread_id):
        return False
    return self._spawn_prompt_destination_permitted(channel_id, thread_id, user_id)


def _spawn_chat_target(
    self: DiscordDispatcher, parent_session_key: str
) -> tuple[str, str, str, str] | None:
    """``(channel_id, thread_id, user_id, session_key)`` for a Discord spawn parent.

    ``None`` for anything this channel cannot address. Reconstructs the
    conversation from the parent session key's grammar
    (``discord:{agent}:{chat_type}:{scope}``): a thread route's scope is the
    thread's own snowflake, which IS the channel to post into; a direct route's
    scope is the peer's user id, whose DM channel the caller opens, so
    ``channel_id`` comes back empty and ``user_id`` carries the peer. A
    ``unified`` DM bucket (``unified:{agent}``) parses as a non-discord surface
    and returns ``None`` — it names no single conversation to post into, the
    same reason the origin mirror declines it. ``session_key`` is returned so
    the caller arms the decider under the exact key ``on_interaction``
    recomputes for a press in that conversation.
    """
    parsed = parse_session_key(parent_session_key)
    if parsed is None or parsed.surface != _CHANNEL or len(parsed.scope) != 1:
        return None
    scope = parsed.scope[0]
    if parsed.chat_type == _CHAT_TYPE_THREAD:
        return scope, scope, "", parent_session_key
    if parsed.chat_type == CHAT_TYPE_DIRECT:
        return "", "", scope, parent_session_key
    return None
