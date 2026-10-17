"""Post a sub-agent spawn-approval prompt into the Telegram conversation that asked for it.

Telegram's half of the channel-neutral ``messaging/spawn_approval_delivery.py`` seam:
the Approve / Deny / Trust keyboard a tool approval uses, armed under the parent
session key, with every path that cannot surface the prompt falling through to the
Slack and dashboard surfaces instead of denying by a timeout nobody could see.
"""

from __future__ import annotations

import asyncio
import html
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING

from kiro_crew.constants import DENY_CAUSE_APPROVAL_TIMEOUT
from kiro_crew.messaging.link import (
    CHAT_TYPE_DIRECT,
    CHAT_TYPE_FORUM,
    CHAT_TYPE_PRIVATE_TOPIC,
    parse_session_key,
)
from kiro_crew.messaging.renderer import new_approval_nonce
from kiro_crew.telegram.renderer import _display_safe
from kiro_crew.telegram.transport import forum_gate_outcome

if TYPE_CHECKING:
    from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

#: The dispatcher's one logger, named for the facade module operators filter on.
logger = logging.getLogger("kiro_crew.telegram.transport_dispatch")


async def deliver_spawn_approval(
    self: TelegramDispatcher, request_id: str, description: str, parent_session_key: str
) -> bool | None:
    """Post a spawn-approval prompt to the ORIGINATING Telegram conversation.

    Registered into the channel-neutral
    :mod:`~kiro_crew.messaging.spawn_approval_delivery` seam so the single
    host spawn gate can reach the same Approve/Deny/Trust keyboard the
    main-agent tool ladder already uses here. Returns the user's decision
    (``True``/``False``), or ``None`` to tell the gate "not surfaced here,
    fall through to Slack/dashboard" — for a key this dispatcher cannot turn
    back into a chat (``unified`` dm_scope drops the peer, a non-``telegram``
    key, an unparseable one), when the client is not up, when the
    destination's authorization has since been withdrawn, or when the post
    fails. The operator's ``channels`` governance ceiling is read TWICE for
    one prompt, and both reads belong to the seam: before it invokes any hook,
    so a denied channel is never prompted at all, and again through
    ``unpressed_wait_answer`` when a wait elapses unpressed, because a deny
    can land inside that wait. This method owns neither reading; it asks for
    the second one.

    The wait is the SAME deny-by-default one a tool prompt uses
    (:class:`TelegramApprovalDecider`, ``APPROVAL_TIMEOUT_S``): the press
    resolves through the ``on_callback`` ``a:`` branch exactly as a tool
    approval does, so Trust still runs ``add_trusted_session`` and a spawn id
    (``spawn:<agent_id>``) cannot collide with an opaque tool id in the
    registry keyed by ``session_key:request_id``.

    The prompt is armed under ``parent_session_key`` VERBATIM (its ``:genN``
    suffix included), but a press recomputes the key from the LIVE
    conversation (``_callback_session_key``). A generation rotation between the
    spawn and the press — ``/new``, an idle reset, a daily rotation — bumps the
    generation, so the recomputed key does not match the armed one, the press
    resolves nothing, and the prompt deny-by-defaults at the timeout (the user
    sees "already expired"). This mirrors how a mid-run tool prompt behaves
    across a rotation, and it stays a DENY: a rotation is the conversation
    moving on, not a withdrawal of the right to answer. An elapsed wait is a
    fall-through in one case only, when AUTHORIZATION ended during it -- the
    prompt was surfaced, so otherwise ``False`` is a real decision and the gate
    refuses the spawn on it. Two authorities can end authorization mid-wait and
    both are re-read when the wait elapses: this conversation's own
    authorization, which ``on_callback`` checks first for every press with no
    exemption (``_spawn_prompt_destination_permitted``, the same pair consulted
    before posting), and the operator's ``channels`` ceiling, whose reading
    belongs to the seam (``unpressed_wait_answer``) for every channel.
    """
    from kiro_crew.telegram import transport_dispatch as facade

    client = self.client
    if client is None:
        return None
    target = self._spawn_chat_target(parent_session_key)
    if target is None:
        # A key this channel does not own or cannot address (unified DM
        # bucket, non-telegram key, malformed). Let the gate fall through.
        return None
    chat_id, thread_id, session_key = target

    rid = str(request_id)
    nonce = new_approval_nonce()
    key = facade.TelegramApprovalDecider.key(session_key, rid)
    # The wait below runs in this gate's OWN task, not in the turn that asked
    # for the spawn: that turn returns as soon as the spawn is admitted, so its
    # end-of-turn sweep can land while this coroutine is still in the send.
    # Claiming the window here keeps the sweep off a prompt the operator is
    # looking at; every exit path below releases the claim.
    facade.TelegramApprovalDecider.arm(key, nonce, detached=True)
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"a:{rid}:{nonce}:1"},
                {"text": "🚫 Deny", "callback_data": f"a:{rid}:{nonce}:0"},
            ],
            [
                {
                    "text": "🤝 Trust this conversation",
                    "callback_data": f"a:{rid}:{nonce}:t",
                }
            ],
        ]
    }
    # The upstream literal scan cannot see credentials split by invisible
    # format characters. Redact the rendered form before HTML escaping, which
    # preserves those characters. Keep the destination check after this pass.
    detail = _display_safe(" ".join((description or "spawn_run").split()))
    body = f"🔐 Approve sub-agent spawn?\n<pre>{html.escape(detail)}</pre>"
    if not self._spawn_prompt_destination_permitted(chat_id, thread_id):
        # Authorization for this destination was withdrawn between the turn that
        # asked for the spawn and this delivery. Retire the armed nonce and fall
        # through, so the spawn is still answerable on Slack/dashboard.
        facade.TelegramApprovalDecider.retire(key)
        logger.info(
            "Telegram: not posting the spawn-approval prompt for %s; the "
            "originating conversation is no longer authorized",
            rid,
        )
        return None
    try:
        posted = await client.send_message(
            chat_id,
            body,
            parse_mode="HTML",
            reply_markup=keyboard,
            message_thread_id=thread_id,
        )
    except asyncio.CancelledError:
        # The only suspension point between the arm and the wait, so a cancel
        # here is the one exit that would otherwise leave the window claimed
        # with no wait coming to release it. Close it, then let the cancel run.
        facade.TelegramApprovalDecider.retire(key)
        raise
    except Exception:
        # Could not surface it: retire the armed nonce and fall through so the
        # spawn can still be answered on Slack/dashboard rather than deny by a
        # timeout nobody could see.
        facade.TelegramApprovalDecider.retire(key)
        logger.warning("Telegram: failed to post spawn-approval prompt for %s", rid, exc_info=True)
        return None
    if not posted:
        # This client reports a failed send by RETURNING no message id rather
        # than by raising (a revoked token, a deleted forum Topic, a chat it
        # cannot write to, a 5xx past its own retries), so the ``except`` above
        # does not cover it. Same conclusion: nothing is on screen, so fall
        # through instead of waiting out the whole decision window on a prompt
        # nobody can press and handing that silence back as a denial.
        facade.TelegramApprovalDecider.retire(key)
        logger.warning(
            "Telegram: the spawn-approval prompt for %s was not accepted by the "
            "chat; falling through",
            rid,
        )
        return None

    decider = facade.TelegramApprovalDecider(session_key=session_key)
    event = SimpleNamespace(request_id=rid)
    approved = bool(await decider(event))
    if not approved and decider.last_deny_cause == DENY_CAUSE_APPROVAL_TIMEOUT:
        # Nobody pressed. An elapsed wait is a deny-by-default except when
        # AUTHORIZATION ended during it; reporting ``False`` then would refuse
        # the spawn in the operator's name. A generation rotation is not in
        # that set: it moves the conversation on rather than withdrawing the
        # right to answer, and stays a deny like a mid-run tool prompt. Two
        # authorities can end authorization, and both are asked:
        #
        # * this conversation's own authorization, which ``on_callback`` checks
        #   FIRST for every press with no exemption: the peer roster gates
        #   every press, and a Topic passes the shared ``forum_gate_outcome``
        #   as well. A peer dropped from the roster, or a Topic dropped from
        #   the allow-list, therefore silences even a reject.
        #   ``_spawn_prompt_destination_permitted`` is the same pair this
        #   method already consults before posting, read here as "could a
        #   press still have been honored";
        # * the operator's ``channels`` ceiling, which the seam owns for every
        #   channel (``unpressed_wait_answer``).
        #
        # A press — approve, trust, or the explicit reject the channels drop
        # exempts — is the operator's own decision and is returned verbatim
        # below, so a real refusal never becomes a fall-through.
        if not self._spawn_prompt_destination_permitted(chat_id, thread_id):
            logger.info(
                "Telegram: the spawn-approval prompt for %s went unanswered and "
                "its conversation is not authorized, so no press could have "
                "resolved it; falling through to the Slack/dashboard path",
                rid,
            )
            return None
        return await facade.unpressed_wait_answer("telegram", rid)
    return approved


def _spawn_prompt_destination_permitted(
    self: TelegramDispatcher, chat_id: int, thread_id: int | None
) -> bool:
    """May a spawn-approval prompt be posted into this chat RIGHT NOW? Fails closed.

    The gate can hold a spawn for as long as its approval takes, so the
    authorization that admitted the originating turn is not evidence about this
    instant: an operator can drop the peer from ``telegram.allowed_user_ids``, or
    a Topic from the forum allow-list, while the prompt is still being prepared.
    The prompt carries a task preview, so it is a send that must be re-decided
    against the LIVE roster rather than the one the turn started under.

    Called SYNCHRONOUSLY with no suspension point between it and the send it
    gates — an await in between would reopen the window it closes.

    Two authorities, both consulted, neither sufficient alone:

    * the dispatcher's own live gates, which are exactly the ones a PRESS is
      judged by in ``on_callback``: the peer roster gates EVERY press, and a
      Topic passes the shared ``forum_gate_outcome`` predicate as well. A DM's
      chat id IS the peer's user id, so ``_authorized`` answers it directly; a
      Topic names no single peer, so the roster is asked whether it admits
      anybody. Either way a prompt is never posted where its own button could
      not be honored;
    * ``transport.may_send_to``, the transport's revocation-at-egress decision,
      when a transport is wired. Absent (no transport, as in a unit harness) the
      dispatcher's gates above stand alone; a raise is read as a denial.
    """
    if thread_id is None:
        # Private chat: its id IS the peer's user id, so the roster answers.
        if not self._authorized(chat_id):
            return False
    else:
        # A private-chat forum Topic carries a thread but its chat_id IS the
        # peer's user id, so the conversation attests its own peer -- the SAME
        # ``direct_peer_of`` discriminator ``may_send_to`` uses (no hand-rolled
        # ``chat_id > 0`` copy), so this gate cannot admit a destination the
        # egress check would refuse. A supergroup Topic carries a group chat_id
        # that attests no peer (``direct_peer_of`` -> ""), so it goes through
        # BOTH gates: the roster (keyed by the PRESSING peer, and a group Topic
        # names none of them -- any authorized peer in it may press -- so what
        # the roster can answer here is whether it admits anybody at all; an
        # empty roster denies every press, reject included) and then the shared
        # forum predicate. Absent a transport (unit harness), there is no
        # attester, so there is no attestation and the destination is treated as
        # a group Topic -- fail-closed, matching the egress step below.
        peer = self.transport.direct_peer_of(str(chat_id)) if self.transport is not None else ""
        is_private_topic = bool(peer)
        if is_private_topic and self._authorized(chat_id):
            pass
        elif not self._allowed:
            return False
        else:
            forum_cfg = self._live_cfg().telegram
            if (
                forum_gate_outcome(
                    "supergroup",
                    chat_id,
                    thread_id,
                    allow_forum=bool(forum_cfg.allow_forum),
                    allowed_forum_chat_ids=forum_cfg.allowed_forum_chat_ids,
                )
                is not None
            ):
                return False
    gate = getattr(self.transport, "may_send_to", None)
    if gate is None:
        return True
    try:
        return bool(gate(str(chat_id), str(thread_id) if thread_id is not None else None))
    except Exception:
        logger.warning(
            "Telegram: may_send_to raised for the spawn-approval destination; "
            "treating it as revoked",
            exc_info=True,
        )
        return False


def _spawn_chat_target(
    self: TelegramDispatcher, parent_session_key: str
) -> tuple[int, int | None, str] | None:
    """``(chat_id, thread_id, session_key)`` for a Telegram spawn parent, else None.

    Reconstructs the conversation from the parent session key's grammar
    (``telegram:{agent}:{chat_type}:{scope…}``): a direct DM's scope is the
    peer's user id, and a Telegram private chat's id EQUALS that user id; a
    forum route's scope is ``{chat_id}:{thread}`` -- for a group forum Topic
    (``direct_topic`` carries the same ``{chat_id}:{thread}`` shape, where the
    chat_id is the peer's private-chat id). A ``unified`` DM bucket
    (``unified:{agent}``) parses as a non-telegram surface and returns None —
    it names no single conversation to post into, which is the same reason the
    origin mirror declines it. ``session_key`` is returned so the caller arms
    the decider under the exact key ``on_callback`` recomputes for a press in
    that chat.
    """
    parsed = parse_session_key(parent_session_key)
    if parsed is None or parsed.surface != "telegram":
        return None
    try:
        if (
            parsed.chat_type in (CHAT_TYPE_FORUM, CHAT_TYPE_PRIVATE_TOPIC)
            and len(parsed.scope) >= 2
        ):
            # Group forum Topic (scope ``{chat_id}:{thread}``) OR private-chat
            # forum Topic (same scope shape; chat_id == the peer's user id).
            chat_id = int(parsed.scope[0])
            thread_id: int | None = int(parsed.scope[1])
        elif parsed.chat_type == CHAT_TYPE_DIRECT and len(parsed.scope) == 1:
            chat_id = int(parsed.scope[0])
            thread_id = None
        else:
            return None
    except (TypeError, ValueError):
        return None
    # The key was minted with a generation suffix; the press recomputes the
    # same key from the live conversation, so key the decider by the exact
    # value the gate handed us.
    return chat_id, thread_id, parent_session_key
