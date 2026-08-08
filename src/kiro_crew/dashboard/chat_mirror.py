"""Channel-neutral cross-surface mirror linking.

Links a dashboard session to a NON-Slack channel conversation so a completed
turn's reply is mirrored out via the neutral ``MessagingTransport.send_message``
(delivered by the dashboard turn path — see ``chat_runner._deliver_cross_surface_reply``).

Slack keeps its dedicated ``slack-link`` endpoint (rich thread creation + the
streaming mirror); this is the generalized counterpart for proactive-capable
channels such as Telegram, built on ``SessionMap.set/clear_mirror_link``.

Auth posture matches ``slack-link``/``slack-unlink`` with no new surface: both
routes live under the ``/api/chat`` prefix (``mixed_internal_paths`` in
server.py), so they accept the internal secret on loopback and otherwise fall
back to normal dashboard-token + CSRF auth. They must NOT be added to the strict
``internal_paths`` set.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_backfill import (
    backfill_content,
    gap_summary,
    select_backfill_messages,
    session_deep_link,
)
from kiro_crew.dashboard.chat_runner import (
    _resolve_channel_target,
    _resolve_mirror_target,
    _resolve_one_target,
)
from kiro_crew.dashboard.chat_slack import list_slack_channels
from kiro_crew.dashboard.chat_utils import effective_session_key, mirror_is_paused
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.messaging.link import SLACK_NAMESPACE, ChannelLink
from kiro_crew.messaging.renderer import chunk_text
from kiro_crew.platform.context import redact_via_context
from kiro_crew.sel import sel
from kiro_crew.session_map import ConversationOwnershipConflict

logger = logging.getLogger(__name__)

# Used only when a transport reports no message-length capability. Matches
# ``TransportCapabilities.max_message_chars``' own default, which is the
# smallest common ceiling across the proactive-capable channels (Telegram and
# WhatsApp cap at 4096).
_FALLBACK_MAX_MESSAGE_CHARS = 4096

# Ceiling on how many messages the INLINE mirror backfill will deliver. Each unit
# costs a governance thread-hop plus a transport send, and a rate-limited channel
# (Telegram is roughly one message per second) makes the request duration a
# function of the unit count. Twelve covers a normal opening-turn-plus-five-turns
# preview outright, so the cap only bites on pathologically long history, and it
# keeps the request inside a browser fetch timeout.
_MAX_INLINE_BACKFILL_UNITS = 12


class _BadBody(Exception):
    """A malformed request body, carrying the 400 to return for it."""

    def __init__(self, response: web.Response) -> None:
        super().__init__("malformed body")
        self.response = response


async def _read_json_body(request: web.Request) -> dict:
    """Parse an optional JSON object body, or raise :class:`_BadBody` with a 400.

    Reads the ACTUAL payload rather than branching on ``Content-Length``: a
    chunked request carries a body with ``content_length is None``, so a
    Content-Length test treats it as empty and silently drops whatever the caller
    sent — which for these endpoints means falling back to the unnamed,
    every-binding form of an operation the caller scoped to one channel.

    An absent body is legal and yields ``{}``; only a body that is present and
    unparseable is an error, so the empty-body reconnect keeps working.
    """
    try:
        raw = (await request.text()).strip()
    except (UnicodeDecodeError, LookupError):
        # Invalid UTF-8, or an unknown charset in Content-Type: a malformed
        # request, not a server fault — 400 rather than a 500 traceback.
        raise _BadBody(
            web.json_response(
                {"error": "body must be valid UTF-8", "code": "body_not_utf8"}, status=400
            )
        )
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise _BadBody(
            web.json_response(
                {"error": "body must be valid JSON", "code": "body_not_json"}, status=400
            )
        )
    if not isinstance(body, dict):
        raise _BadBody(
            web.json_response(
                {"error": "body must be a JSON object", "code": "body_not_object"}, status=400
            )
        )
    return body


def _body_channel_type(body: dict) -> str:
    """The ``channel_type`` a scoped mirror operation names, or ``""`` for all."""
    return str(body.get("channel_type") or "")


# One lock per CONVERSATION, so connects that target the same conversation run one
# at a time while connects to different ones stay concurrent.
#
# Claiming the binding before delivering is what stops a losing racer pasting its
# transcript somewhere it does not belong, but the claim alone is not enough: the
# sequence claim → deliver → confirm spans awaits, so two confirmed takeovers can
# still interleave. The second replaces the first mid-flight and the first keeps
# streaming its history into a conversation it no longer owns — and its rollback
# would then restore bindings the second one legitimately replaced. Serialising the
# whole critical section is what makes both impossible, and it is also what makes
# eviction reversible: nothing else can touch the location while we hold this, so a
# failed takeover can put every evicted binding back exactly as it was.
_CONVERSATION_LOCKS: dict[str, asyncio.Lock] = {}


def _conversation_lock(link: ChannelLink) -> asyncio.Lock:
    """The lock guarding one conversation's binding, created on first use."""
    key = f"{link.channel_type}:{link.channel_id}:{link.thread_id or ''}"
    lock = _CONVERSATION_LOCKS.get(key)
    if lock is None:
        # The event loop is single-threaded, so there is no window between the
        # get and the set for a second coroutine to create a rival lock.
        lock = asyncio.Lock()
        _CONVERSATION_LOCKS[key] = lock
    return lock


def _release_conversation_lock(link: ChannelLink) -> None:
    """Drop a free lock so the table cannot grow without bound.

    Only when nobody holds it and nobody is queued: dropping a contended lock
    would hand the next waiter a DIFFERENT lock object and dissolve the mutual
    exclusion this exists to provide.
    """
    key = f"{link.channel_type}:{link.channel_id}:{link.thread_id or ''}"
    lock = _CONVERSATION_LOCKS.get(key)
    if lock is not None and not lock.locked() and not getattr(lock, "_waiters", None):
        _CONVERSATION_LOCKS.pop(key, None)


async def api_channel_targets(request: web.Request) -> web.Response:
    """GET /api/chat/channel-targets — list configured outbound destinations."""
    state: DashboardState = request.app["state"]
    targets: list[dict] = []
    if state.slack_client is not None and getattr(state, "owner_id", None):
        try:
            for channel in await list_slack_channels(state):
                channel_id = str(channel.get("id", "") or "")
                name = str(channel.get("name", "") or channel_id)
                if channel_id:
                    targets.append(
                        {
                            "channel_type": SLACK_NAMESPACE,
                            "target_id": channel_id,
                            "label": f"Slack · {name}",
                            "available": True,
                            "unavailable_reason": "",
                        }
                    )
        except Exception:
            logger.warning("channel-targets: failed to enumerate Slack", exc_info=True)
    for channel_type, transport in sorted(state.channel_transports.items()):
        try:
            targets.extend(
                target.to_dict(channel_type) for target in transport.configured_targets()
            )
        except Exception:
            logger.warning("channel-targets: failed to enumerate %s", channel_type, exc_info=True)
    return web.json_response(targets)


async def api_chat_slot_mirror_link(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{name}/mirror-link — mirror a session to a channel.

    Body: ``{channel_type, target_id}``. Slack is rejected with a hint to use
    ``slack-link`` (which owns Slack's rich thread + streaming mirror).
    ``target_id`` is REQUIRED and is always resolved through the transport's
    configured-target allowlist (``resolve_configured_target``): a raw
    conversation id is never accepted as a send target, so a session's transcript
    can only be anchored into a channel the user has actually configured. The
    target channel's transport must be registered at boot AND
    ``supports_proactive_send`` — Telegram qualifies; WeCom, whose replies are
    bound to an inbound token, does not.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info.get("name") or request.match_info.get("slot", "")
    slot = state.get_slot(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    # Read the ACTUAL payload rather than branching on Content-Length: a chunked
    # request carries a body with ``content_length is None``, so a Content-Length
    # test treats it as empty and falls into reminder mode below — turning a
    # malformed link attempt into an unsolicited send to the persisted channel.
    raw_body = ""
    try:
        raw_body = (await request.text()).strip()
    except (UnicodeDecodeError, LookupError):
        # Invalid UTF-8, or an unknown charset in Content-Type. That is a
        # malformed request, not a server fault — answer 400 rather than
        # letting the decode error surface as a 500 traceback.
        return web.json_response({"error": "body must be valid UTF-8"}, status=400)
    if raw_body:
        try:
            body = json.loads(raw_body)
        except ValueError:
            return web.json_response({"error": "body must be valid JSON"}, status=400)
    else:
        body = {}
    # Reminder mode keys off an EMPTY body, so a non-dict payload must be
    # rejected here rather than reaching the truthiness test below.
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)
    channel_type = str(body.get("channel_type", "") or "").strip()
    target_id = str(body.get("target_id", "") or "").strip()
    thread_id = str(body.get("thread_id", "") or "").strip() or None

    # An EMPTY body on an existing mirror mirrors Slack's "Post reminder"
    # behavior. Gate on the body being empty, NOT on channel_type/conversation_id
    # being absent: a partial payload (e.g. {"thread_id": "x"}) has neither field
    # but is a malformed link attempt, and must still hit the required-field
    # validation below instead of silently posting to the persisted channel.
    # The menu only exposes this action when the link reads live, but resolve
    # again here — through the governed async send ladder — so a disconnect or
    # governance change between render and click fails closed at the side-effect
    # boundary.
    if not body or set(body) <= {"channel_type"}:
        session_key = effective_session_key(slot)
        # A reconnect names WHICH binding to bring back: a session can hold
        # several, so an unnamed reconnect on a multi-bound session would pick an
        # arbitrary sibling. get_mirror_link returns None rather than guess.
        want = str(body.get("channel_type") or "")
        target = await asyncio.to_thread(_resolve_mirror_target, state, session_key, want)
        if target is None:
            existing = state.sessions.get_mirror_link(session_key, want)
            if existing is None:
                return web.json_response({"error": "channel_type required"}, status=400)
            return web.json_response({"error": "mirror channel is not live"}, status=503)
        link, transport = target
        # An empty body on a session that already has a link is a RECONNECT, not
        # a ping. It used to post "Session linked from dashboard — continuing
        # here." for the "Post reminder" menu item, which no longer exists.
        #
        # Muted: lift it and catch the conversation up, because the gap in it is
        # there precisely because delivery was off.
        if mirror_is_paused(state, session_key, link.channel_type):
            return await _reconnect_muted(state, slot, session_key, link, transport)
        # Already connected: a no-op, and silently so. Posting into the
        # conversation here would be a stray message explaining nothing to
        # whoever reads it.
        return web.json_response(
            {
                "ok": True,
                "already_linked": True,
                "channel_type": link.channel_type,
            }
        )

    if not channel_type:
        return web.json_response({"error": "channel_type required"}, status=400)
    if channel_type == SLACK_NAMESPACE:
        return web.json_response({"error": "use /slack-link for Slack"}, status=400)
    if not target_id:
        return web.json_response(
            {"error": "target_id required", "code": "target_id_required"}, status=400
        )
    transport = state.get_channel_transport(channel_type)
    if transport is None:
        return web.json_response(
            {"error": f"channel '{channel_type}' not connected", "code": "channel_not_connected"},
            status=503,
        )
    if not transport.capabilities.supports_proactive_send:
        return web.json_response(
            {"error": f"channel '{channel_type}' cannot mirror (no proactive send)"},
            status=400,
        )
    session_key = effective_session_key(slot)
    # Resolving an opaque configured target can itself open a remote
    # conversation (for example, Discord creates a DM channel). Re-enter the
    # shared fail-closed governance ladder before that network side effect,
    # including when a profile changed after the transport connected.
    provisional_link = ChannelLink(
        channel_type=channel_type,
        channel_id=target_id,
        thread_id=thread_id,
    )
    governed = await asyncio.to_thread(
        _resolve_channel_target, state, session_key, provisional_link
    )
    if governed is None:
        return web.json_response(
            {"error": "channel is not permitted", "code": "channel_not_permitted"}, status=403
        )
    _, transport = governed
    # target_id is required and opaque: ALWAYS resolve it through the transport's
    # configured-target allowlist. A raw conversation_id is never accepted as a
    # send target — that would let a caller anchor a session's transcript into an
    # arbitrary, non-allowlisted channel of a governance-permitted type.
    resolved = await transport.resolve_configured_target(target_id)
    # Audit the allowlist decision (allowed/denied) BEFORE branching: a
    # stale/tampered target id that the resolver rejects is an authorization
    # outcome and must land in the SEL trail, not just return a bare 409.
    sel().log_api_access(
        caller="dashboard",
        operation="chat.mirror_target_resolve",
        outcome="allowed" if resolved is not None else "denied",
        source="dashboard",
        resources=f"{slot.key} -> {channel_type}:{target_id}",
    )
    if resolved is None:
        return web.json_response(
            {
                "error": "configured target is unavailable",
                "code": "configured_target_unavailable",
            },
            status=409,
        )
    conversation_id, thread_id = resolved

    link = ChannelLink(
        channel_type=channel_type,
        channel_id=conversation_id,
        thread_id=thread_id,
    )

    # Everything from the occupancy read to the committed response is ONE critical
    # section per conversation. Two confirmed takeovers would otherwise interleave
    # across the awaited sends: the second claims while the first is mid-delivery,
    # and the first keeps streaming its transcript into a conversation it no longer
    # owns — then rolls back over bindings the second legitimately replaced.
    lock = _conversation_lock(link)
    try:
        async with lock:
            return await _claim_and_seed(
                state,
                slot,
                session_key,
                link,
                body,
                conversation_id,
                thread_id,
                transport,
            )
    finally:
        # OUTSIDE the `async with`, so the lock is already released and can be
        # judged free. Cleaning up inside would always see it held and never drop
        # it, leaving one lock per conversation alive for the process lifetime.
        _release_conversation_lock(link)


async def _reconnect_muted(
    state: "DashboardState",
    slot,
    session_key: str,
    link: ChannelLink,
    transport,
) -> web.Response:
    """Lift the mute on an existing binding and catch the conversation up.

    RECLAIM BEFORE DELIVERING, under the conversation lock — the same ordering the
    fresh connect uses, and for the same reason. Delivering first looked safer (a
    governance denial would leave the link untouched), but it meant a muted
    reconnect could stream this session's transcript into a conversation that a
    concurrent confirmed takeover had already handed to someone else. Ownership is
    cheap to undo; a posted transcript is not.

    So the mute is lifted first and RESTORED if the catch-up is denied or fails,
    which preserves the "a denial leaves the link exactly as it was" property that
    the deliver-first ordering was there to get.
    """
    lock = _conversation_lock(link)
    try:
        async with lock:
            # Snapshot the flag BEFORE the claim overwrites it. A binding can be
            # outbound-only even on a channel that resumes — an in-channel `!link`
            # creates one without `accepts_inbound` — and the claim below sets the
            # flag unconditionally for such a channel. Restoring only the mute left
            # that binding claiming inbound ownership it never had, so later replies
            # in the conversation resumed THIS session instead of starting their own.
            #
            # Third time this snapshot has been short a field (link, inbound, mute):
            # restore all three together or none of them.
            try:
                previous_inbound = bool(
                    state.sessions.mirror_accepts_inbound(session_key, link.channel_type)
                )
            except Exception:
                # Unknown means do not claim it had inbound: restoring False on a
                # binding that did have it costs a resume the user can re-establish,
                # while restoring True on one that did not silently hijacks replies.
                logger.debug("reconnect: inbound flag unreadable", exc_info=True)
                previous_inbound = False

            # accepts_inbound is re-asserted on every reconnect: a reply in that
            # conversation must resume this session, and the flag lives ON the
            # binding a rebind replaces. This is also what lifts the mute — a rebind
            # never inherits one. Offloaded like every other session-map write:
            # `set_mirror_link` calls `_save`, which serialises the whole map.
            #
            # A rival can have claimed this conversation while it sat muted, so the
            # atomic claim can refuse. Report it as the same occupied-conversation
            # 409 a fresh connect would, so the client offers the same takeover
            # confirm instead of surfacing an internal error.
            try:
                await asyncio.to_thread(
                    state.sessions.set_mirror_link,
                    session_key,
                    link,
                    accepts_inbound=_resumes_inbound(transport),
                )
            except ConversationOwnershipConflict:
                return web.json_response(
                    {
                        "error": "another session is connected to this conversation",
                        "code": "conversation_occupied",
                        "requires_confirm": True,
                        "occupied_by": 1,
                    },
                    status=409,
                )

            async def _restore_binding() -> None:
                """Put the binding back the way the user left it, all three fields.

                Link first, then the mute: `set_mirror_link` deliberately drops a
                mute on rebind, so re-applying it afterwards is the only order that
                ends muted.

                Skipped entirely once the binding is gone or has moved. An in-channel
                `!unlink` during the catch-up REMOVES it, and re-establishing it here
                would resurrect a link the user explicitly deleted — the reconnect
                failing is no reason to overrule them. Checked at the top rather than
                per write, because a half-restore (link back, mute not) is worse than
                either outcome.
                """
                current = await asyncio.to_thread(
                    state.sessions.get_mirror_link, session_key, link.channel_type
                )
                if current != link:
                    logger.info(
                        "reconnect rollback skipped: the %s binding was removed or "
                        "moved while the catch-up ran",
                        link.channel_type,
                    )
                    return
                try:
                    await asyncio.to_thread(
                        state.sessions.set_mirror_link,
                        session_key,
                        link,
                        accepts_inbound=previous_inbound,
                    )
                except Exception:
                    logger.debug(
                        "could not restore the inbound flag after a failed reconnect",
                        exc_info=True,
                    )
                try:
                    await asyncio.to_thread(
                        state.sessions.set_mirror_paused, session_key, True, link.channel_type
                    )
                except Exception:
                    logger.debug("could not restore the mute after a failed reconnect",
                                 exc_info=True)

            try:
                denial = await _deliver_catch_up(state, slot, session_key, link, transport)
            except asyncio.CancelledError:
                # CancelledError is a BaseException, so the `except Exception`
                # below never caught it: a shutdown after the claim persisted left
                # the binding half-changed. Shielded because a bare await in an
                # already-cancelled task raises at once and would abandon the
                # rollback mid-way; the cancellation still propagates after it.
                # A loop that dies before the shielded task finishes can still lose
                # it — unavoidable without blocking shutdown.
                await asyncio.shield(asyncio.ensure_future(_restore_binding()))
                raise
            except Exception:
                logger.debug("reconnect catch-up failed", exc_info=True)
                await _restore_binding()
                raise
            if denial is not None:
                await _restore_binding()
                return denial

            sel().log_api_access(
                caller="dashboard",
                operation="chat.mirror_reconnect",
                outcome="success",
                source="dashboard",
                resources=f"{slot.key} -> {link.channel_type}",
            )
            state.push_slots_update()
            return web.json_response(
                {
                    "ok": True,
                    "reconnected": True,
                    "channel_type": link.channel_type,
                    "conversation_id": link.channel_id,
                }
            )
    finally:
        # OUTSIDE the `async with`, for the reason given at the other call site.
        _release_conversation_lock(link)


async def _claim_and_seed(
    state: "DashboardState",
    slot: Any,
    session_key: str,
    link: ChannelLink,
    body: dict,
    conversation_id: str,
    thread_id: str | None,
    transport: Any,
) -> web.Response:
    """Claim a conversation for *session_key* and seed it. Caller holds the lock.

    Split out only so the lock's ``async with`` stays a thin, obviously-correct
    wrapper around the whole critical section rather than indenting it.

    ``transport`` is the one already resolved by the caller, taken here only for its
    static capabilities: the claim needs to know whether this channel resumes inbound
    BEFORE delivery resolves a live transport of its own further down.
    """
    # ONE session per conversation, and this is checked BEFORE any side effect: a
    # conversation has no threads to scope bindings to (a Discord DM cannot hold
    # them at all), so two sessions bound here would leave an inbound message
    # unroutable — the resolver refuses to pick and the message reaches nobody.
    # Taking a conversation from another session is the user's call, so it is
    # refused until they confirm rather than done silently.
    occupants = [k for k in state.sessions.find_mirror_sessions(link) if k != session_key]
    # `is True`, not truthiness: a JSON body carrying `{"confirm": "false"}` — or
    # any non-empty string, or 0/1 from a sloppy client — would otherwise read as
    # consent and evict another session's binding without the user ever seeing the
    # prompt. Consent is a boolean or it is absent.
    confirmed = body.get("confirm") is True
    if occupants and not confirmed:
        return web.json_response(
            {
                "error": "another session is connected to this conversation",
                "code": "conversation_occupied",
                "requires_confirm": True,
                "occupied_by": len(occupants),
            },
            status=409,
        )

    # ── CLAIM the conversation before delivering anything into it ──────────────
    # Ordering matters more than it looks. Delivery is not rollback-able: the link
    # notice and the whole catch-up transcript are messages a human can already
    # read. If two connects race, checking occupancy, delivering, and only THEN
    # discovering we lost means the loser has already pasted this session's
    # transcript into someone else's conversation before answering 409. So the
    # binding is claimed FIRST — under the conversation lock taken above, so no
    # rival connect can replace it while we deliver — and the claim is rolled back
    # if delivery then fails.
    #
    # Snapshot every binding we are about to evict, BEFORE clearing it, so a failed
    # takeover puts the previous owner back. Without this a confirmed takeover whose
    # delivery then failed would leave the evicted session unbound for nothing: the
    # rollback would tidy up the claimant and silently keep the eviction.
    # Snapshot THIS session's own binding first, BEFORE any eviction:
    # `clear_mirror_links_at` clears every binding at this location including this
    # session's, so reading it afterwards could see None and silently turn a failed
    # rebind into a lost binding. Both flags come with it — reading them after the
    # claim would read what the claim overwrote (inbound always True, mute cleared).
    previous = state.sessions.get_mirror_link(session_key, link.channel_type)
    previous_inbound = False
    previous_paused = False
    if previous is not None:
        try:
            previous_inbound = bool(
                state.sessions.mirror_accepts_inbound(session_key, previous.channel_type)
            )
        except Exception:
            previous_inbound = False
        try:
            previous_paused = (
                state.sessions.is_mirror_paused(session_key, previous.channel_type) is True
            )
        except Exception:
            previous_paused = False

    # A binding carries exactly three pieces of restorable state: the link itself
    # (channel type, conversation, thread), `accepts_inbound`, and `paused`. All
    # three are snapshotted, because `set_mirror_link` deliberately drops the mute
    # on a rebind — so restoring the link alone silently RECONNECTS a binding that
    # was muted, and a failed takeover would un-mute the very occupant it failed to
    # replace. That is the complete set; there is no fourth flag.
    #
    # The snapshot is taken BY the atomic claim below and handed back, not read here
    # first: eviction and replacement are one session-map mutation, so there is no
    # point at which this endpoint could read the occupant set and have it still be
    # true by the time the eviction ran.
    evicted: list[tuple[str, ChannelLink, bool, bool]] = []

    async def _release_claim() -> None:
        """Undo the claim AND the eviction it performed, in ONE mutation.

        This used to be four separate offloaded writes, on the reasoning that the
        conversation lock held them together. It does not: that lock lives in this
        module, so the Discord and Telegram in-channel `/link` handlers never take
        it. A claim landing between two of those awaits made the occupant restore
        refuse — exclusivity is checked on every claim now — and the refusal was
        swallowed per occupant, so "could not restore your binding" surfaced as the
        binding being gone. `restore_mirror_owner` does the whole compensation under
        a single `_mutate_lock` hold, which is the same guarantee
        `replace_mirror_owner` gives the takeover it reverses.
        """
        try:
            await asyncio.to_thread(
                state.sessions.restore_mirror_owner,
                session_key,
                link,
                evicted,
                (previous, previous_inbound, previous_paused)
                if previous is not None
                else None,
            )
        except Exception:
            logger.debug("mirror-link could not release its claim", exc_info=True)

    # ── The two writes that take the conversation, guarded together ────────────
    # Both are offloaded (`_save` serialises the whole map) and awaiting inside the
    # critical section is safe only because the conversation lock is held. They are
    # wrapped because the eviction PERSISTS before the claim runs: if the claim's
    # write then fails, an unguarded exception would escape with the previous owner
    # already evicted for a connect that never happened.
    try:
        if confirmed:
            # ONE session-map mutation: evict the occupants and claim the location
            # under a single lock hold. As two calls this left the conversation
            # momentarily VACANT, and the Discord picker could take that vacancy —
            # the takeover was then refused while the evicted binding stayed
            # deleted, so the user lost a link and nobody gained one.
            evicted = await asyncio.to_thread(
                state.sessions.replace_mirror_owner,
                session_key,
                link,
                accepts_inbound=_resumes_inbound(transport),
            )
        else:
            # NOT confirmed, so this connect has no licence to displace anybody.
            # `replace_mirror_owner` evicts whatever it finds at claim time, which is
            # right for a takeover and wrong here: the precheck found the
            # conversation free, so no confirm was ever shown, and a rival claiming
            # it in the meantime would be evicted without the user having agreed to
            # take anything. A plain claim refuses instead, and the refusal becomes
            # the 409 that asks for consent.
            await asyncio.to_thread(
                state.sessions.set_mirror_link,
                session_key,
                link,
                accepts_inbound=_resumes_inbound(transport),
            )
    except ConversationOwnershipConflict:
        # A rival claimed the conversation after our precheck saw it free. Reached
        # from the unconfirmed branch (a plain claim refuses rather than displacing
        # anyone) and, rarely, from a confirmed takeover that lost to a claim landing
        # after its own eviction. Either way it is the occupied-conversation case,
        # not an internal fault, so it gets the same 409 the precheck returns and the
        # user is offered the takeover confirm.
        logger.debug("mirror-link lost the claim race for this conversation")
        await _release_claim()
        return web.json_response(
            {
                "error": "another session is connected to this conversation",
                "code": "conversation_occupied",
                "requires_confirm": True,
                "occupied_by": 1,
            },
            status=409,
        )
    except Exception:
        logger.debug("mirror-link could not claim the conversation", exc_info=True)
        await _release_claim()
        return web.json_response(
            {"error": "failed to claim the conversation", "code": "claim_failed"},
            status=500,
        )

    try:
        # Recheck at the actual send boundary as well: target resolution can
        # yield while governance is updated.
        governed = await asyncio.to_thread(_resolve_channel_target, state, session_key, link)
        if governed is None:
            await _release_claim()
            return web.json_response(
                {"error": "channel is not permitted", "code": "channel_not_permitted"}, status=403
            )
        _, live_transport = governed
        await live_transport.send_message(
            conversation_id,
            "Session linked from dashboard — continuing here.",
            thread_id=thread_id,
        )
    except asyncio.CancelledError:
        # Same reason as the reconnect path: a shutdown here would leave the
        # evicted owner evicted and this session holding a conversation it never
        # delivered into. Shielded so the rollback is not cut off halfway.
        await asyncio.shield(asyncio.ensure_future(_release_claim()))
        raise
    except Exception:
        logger.debug("mirror-link initial delivery failed", exc_info=True)
        await _release_claim()
        return web.json_response(
            {"error": "failed to create channel link", "code": "channel_link_failed"}, status=502
        )

    # One shared catch-up path with the reconnect below, so a channel that has
    # seen nothing and a channel with a gap are seeded identically. It fails
    # closed on both of its failure modes: a mid-delivery governance denial comes
    # back as the 403 to return, and a failed send as a 502 — either way the claim
    # is released, so neither a denied nor an undelivered binding persists.
    try:
        denial = await _deliver_catch_up(state, slot, session_key, link, live_transport)
    except asyncio.CancelledError:
        # The initial delivery above is already shielded; the catch-up is the LONGER
        # of the two, so a shutdown landing in here is the likelier one. Without this
        # the prior owner stays evicted and this session keeps a conversation it never
        # finished delivering into — the takeover commits with nobody having received
        # anything.
        await asyncio.shield(asyncio.ensure_future(_release_claim()))
        raise
    if denial is not None:
        await _release_claim()
        return denial

    # The claim is now confirmed by successful delivery. Tell the conversation
    # whoever was evicted is gone, because whoever is reading there needs to know
    # which session they are talking to. Best-effort: a failed notice must not
    # fail a connect that has already delivered.
    if occupants:
        try:
            # `live_transport` was authorized before `_deliver_catch_up`, which is a
            # whole sequence of sends long. Ask again rather than posting into a
            # conversation the newest decision forbids. Suppressing strands nobody:
            # the connect has already delivered and the claim is confirmed, and this
            # notice was best-effort even before the recheck.
            governed = await asyncio.to_thread(
                _resolve_channel_target, state, session_key, link
            )
            if governed is None:
                logger.info(
                    "mirror-link eviction notice suppressed: %s no longer permitted",
                    link.channel_type,
                )
            else:
                _, notice_transport = governed
                await notice_transport.send_message(
                    conversation_id,
                    "🔌 A different session is connected here now.",
                    thread_id=thread_id,
                )
        except Exception:
            logger.debug("mirror-link eviction notice failed", exc_info=True)
        sel().log_api_access(
            caller="dashboard",
            operation="chat.mirror_evict",
            outcome="success",
            source="dashboard",
            resources=f"{slot.key} <- {','.join(occupants)}",
        )
        logger.info("mirror-link: evicted %s from %s", occupants, link.channel_type)

    # No second occupancy check here: the claim above IS the check. Re-reading
    # after delivery was the previous shape, and it still let the losing racer
    # deliver its transcript into the conversation before answering 409 —
    # unwinding a binding is possible, unsending messages is not. Claiming first
    # makes the loser fail on `set_mirror_link` semantics (last writer owns the
    # location) before it has posted anything, and `_release_claim` is what keeps
    # a failed delivery from leaving a binding behind.

    sel().log_api_access(
        caller="dashboard",
        operation="chat.mirror_link",
        outcome="success",
        source="dashboard",
        resources=f"{slot.key} -> {link.channel_type}",
    )
    state.push_slots_update()
    logger.info("mirror-link: %s -> %s:%s", slot.key, link.channel_type, conversation_id)
    return web.json_response(
        {
            "ok": True,
            "channel_type": link.channel_type,
            "conversation_id": conversation_id,
            # The direction the binding ACTUALLY has, so the client's optimistic row
            # matches what the next slots push will say. Reported by the server
            # rather than decided in the client: which channels resume inbound is a
            # transport capability, and duplicating that list in the frontend is how
            # the two drift into disagreeing.
            "direction": "both" if _resumes_inbound(transport) else "out",
        }
    )


def _resumes_inbound(transport: Any) -> bool:
    """Does a binding on this transport actually route replies back to the session?

    Only claim `accepts_inbound` where the transport's INBOUND path resolves the
    mirror binding. Discord's dispatcher does (`resumed_session`); Telegram and the
    rest build a session key from the route alone and never look the binding up, so
    a reply there runs in its own session with none of this one's context.

    Claiming it anyway is not a harmless over-declaration: `dashboard/state.py`
    derives the row's `direction` from this flag, so the dashboard would show a
    two-way link and the user would reasonably expect replies to come back.

    Conservative on a missing capability object: an unknown transport gets an
    outbound-only binding, which is merely less capable than it might be.
    """
    return bool(
        getattr(getattr(transport, "capabilities", None), "supports_session_resume", False)
    )


async def _deliver_catch_up(
    state: DashboardState,
    slot: Any,
    session_key: str,
    link: ChannelLink,
    transport: Any,
) -> web.Response | None:
    """Seed a channel with the history it has not seen. ``None`` on success.

    Shared by the two paths that need it and MUST behave identically in both:
    creating a link (the conversation has seen nothing) and reconnecting a muted
    one (the conversation has a gap exactly where the mute was). Returning a
    ``Response`` rather than raising keeps the fail-closed contract legible at
    both call sites: a mid-delivery governance denial is a 403 the caller must
    return WITHOUT persisting anything, and a failed send is a 502 the caller must
    answer by putting the state back (restore the mute, or release the claim).

    Every unit crosses the egress boundary as its own governed action — the gap
    marker included — so policy narrowing while the loop yields stops delivery
    instead of riding along on an earlier decision. The loop is inline and
    bounded rather than backgrounded precisely because that per-unit denial has
    to be able to fail the request closed.
    """
    # Offloaded: selection reads the on-disk transcript when the opening turn is
    # off-window, and that read parses every tab_id sibling file. On the loop
    # thread it would stall every other chat turn and the liveness heartbeat.
    selection = await asyncio.to_thread(select_backfill_messages, state, slot)
    max_chars = (
        getattr(getattr(transport, "capabilities", None), "max_message_chars", 0)
        or _FALLBACK_MAX_MESSAGE_CHARS
    )

    def _units_for(row: dict) -> list[str]:
        # redact_via_context is the canonical egress shim (a loaded companion's
        # extra credential regexes apply, not just the OSS baseline) and it never
        # truncates. chunk_text at the transport's own limit matches how a normal
        # mirrored turn is delivered in _deliver_cross_surface_reply, so a long
        # message arrives in full instead of being cut at 2,000 chars. No Slack
        # mrkdwn conversion here: this path targets Telegram/Discord/Teams.
        speaker = "You" if row.get("role") == "user" else "Kiro Crew"
        text = redact_via_context(backfill_content(row))
        return chunk_text(f"{speaker}: {text}", max_chars)

    recent_turn_units = [
        [unit for row in turn for unit in _units_for(row)] for turn in selection.recent
    ]
    head_units: list[str] = []
    for row in selection.first_turn:
        head_units.extend(_units_for(row))

    total_turns = len(recent_turn_units)

    def _fits(keep: int, with_head: bool) -> bool:
        """Would keeping the newest *keep* turns fit the budget?

        The marker costs a unit only when something is ACTUALLY skipped -- either
        selection already skipped turns, or this budget drops one. Reserving it
        unconditionally made a self-fulfilling gap: with six two-message turns
        every unit fits, but the reservation pushed the oldest turn out and then
        spent the reserved slot announcing the omission it had just caused.
        """
        tail = recent_turn_units[total_turns - keep:] if keep else []
        dropped = total_turns - keep
        marker = 1 if (selection.skipped_turns or dropped) else 0
        head = len(head_units) if with_head else 0
        return sum(len(u) for u in tail) + marker + head <= _MAX_INLINE_BACKFILL_UNITS

    # Priority order: keep as much recent history as fits WITH the opening turn;
    # only give the opening turn up if not even the newest turn fits alongside
    # it; and always keep the newest turn, which is irreducible (shrinking one
    # turn means cutting a reply mid-sentence), even if it alone overruns.
    keep_turns, include_head = 0, False
    if head_units:
        for candidate in range(total_turns, 0, -1):
            if _fits(candidate, True):
                keep_turns, include_head = candidate, True
                break
    if not keep_turns:
        for candidate in range(total_turns, 0, -1):
            if _fits(candidate, False):
                keep_turns, include_head = candidate, False
                break
    if not keep_turns and total_turns:
        keep_turns, include_head = 1, False

    kept = recent_turn_units[total_turns - keep_turns:] if keep_turns else []
    skipped_total = (
        selection.skipped_turns
        + (total_turns - keep_turns)
        + (1 if selection.first_turn and not include_head else 0)
    )

    units: list[str] = list(head_units) if include_head else []
    if skipped_total and kept:
        summary = gap_summary(skipped_total)
        deep_link = ""
        try:
            # Offloaded for the same reason as the transcript read: config load
            # is blocking file I/O and must not run on the event loop.
            cfg = await asyncio.to_thread(KiroCrewConfig.load)
            deep_link = session_deep_link(cfg.dashboard.url, slot.key)
        except Exception:
            logger.debug("catch-up: could not build session link", exc_info=True)
        units.append(f"… {summary} — {deep_link}" if deep_link else f"… {summary}")
    for turn_units in kept:
        units.extend(turn_units)

    for unit in units:
        try:
            # Historical context is a sequence of separate egress actions, so all
            # THREE questions get re-asked per unit: is the session still bound to
            # this exact conversation, is that binding muted, and does policy still
            # permit it. Asking only the last one — which is what calling
            # `_resolve_channel_target` directly does — read a binding removed by a
            # disconnect or an in-channel `!unlink` as live, and kept replaying
            # transcript history into a conversation the session had detached from.
            # Shared with the reply path so the two delivery loops cannot drift.
            governed = await _resolve_one_target(
                state, session_key, link, require_unmuted=False
            )
            if governed is None:
                # Detached or no longer permitted: fail closed. The caller must NOT
                # persist a link the latest decision denied, and must not report
                # success. A governance denial is already SEL-audited inside
                # _resolve_channel_target via vet_and_audit.
                return web.json_response(
                    {"error": "channel is not permitted", "code": "channel_not_permitted"},
                    status=403,
                )
            _, live_transport = governed
            await live_transport.send_message(
                link.channel_id,
                unit,
                thread_id=link.thread_id,
            )
        except Exception:
            # A send failure is reported, not swallowed. Continuing the loop and
            # returning success told the caller the conversation had been caught up
            # when it had not: the reconnect lifted the mute and answered 200 while
            # the missed history never arrived, so the user had no signal at all that
            # the thing they asked for did not happen.
            #
            # Returning a Response rather than raising matches this function's
            # contract, and both callers already act on it — the reconnect restores
            # the mute, the fresh connect releases the claim — so the state goes back
            # to what it was and a retry is meaningful.
            #
            # Earlier units may already have landed. That is the same partial-delivery
            # shape the governance branch above has always had, and it is the better
            # trade: a duplicate on retry is visible and recoverable, a silent
            # non-delivery is neither.
            logger.debug("catch-up delivery failed", exc_info=True)
            return web.json_response(
                {
                    "error": "could not deliver the missed history",
                    "code": "catch_up_delivery_failed",
                },
                status=502,
            )
    return None


async def api_chat_slot_mirror_pause(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{name}/mirror-pause — mute the linked channel, keep the binding.

    The channel-neutral twin of ``slack-pause``, and what the dashboard's single
    row calls to DISCONNECT. The binding survives, so inbound routing is
    untouched and the conversation still resolves to THIS session; only the
    turn's outbound mirroring stops (see ``chat_utils.mirror_is_paused`` for the
    exact scope, which is narrower than Slack's because no cron result,
    sub-agent completion or auto-nudge tick reads the mirror link).

    Resume by re-issuing ``mirror-link``, which lifts the mute and catches the
    conversation up.

    ``409`` when the session mirrors nowhere — returning ok would leave the UI
    offering to disconnect something that was never connected. Idempotent,
    reporting ``was_paused``. Nothing is posted into the conversation: the
    dashboard shows the state, and this endpoint has no copy of its own.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info.get("name") or request.match_info.get("slot", "")
    slot = state.get_slot(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    session_key = effective_session_key(slot)
    try:
        want = _body_channel_type(await _read_json_body(request))
    except _BadBody as bad:
        return bad.response
    link = state.sessions.get_mirror_link(session_key, want)
    # A Slack-only session synthesizes a Slack ChannelLink from its dedicated
    # fields, which would pass this guard and then mute nothing — `mirrors` is
    # empty, so the endpoint would answer ok/was_paused:false. Slack is muted
    # through its own endpoint; refusing here keeps the reply honest.
    if link is None or link.channel_type == SLACK_NAMESPACE:
        return web.json_response(
            {"error": "not linked", "code": "mirror_not_linked"}, status=409
        )

    # Offloaded: `set_mirror_paused` calls `_save`, which serialises the whole
    # session map, and this is a request handler on the gateway's event loop.
    was_paused = await asyncio.to_thread(
        state.sessions.set_mirror_paused, session_key, True, want
    )
    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.mirror_pause",
        outcome="noop" if was_paused else "success",
        source="dashboard",
        resources=slot.key,
    )
    logger.info("mirror-pause: %s (was_paused=%s)", slot.key, was_paused)
    return web.json_response({"ok": True, "was_paused": was_paused})


async def api_chat_slot_mirror_unlink(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{name}/mirror-unlink — stop mirroring this session.

    Clears the session's outbound mirror binding. Idempotent: unlinking a session
    with no mirror returns ``{ok, was_linked: false}``. Unlike Slack links, a
    mirror link is set on the slot's own session key — the channel key for a
    conversation that started on a channel, ``dashboard:<slot>`` otherwise — and
    is never copied onto a second spelling, so a single clear on that key
    suffices. Legacy bindings written under the pre-unification derived key are
    reached by ``SessionMap``'s own compat fallback.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info.get("name") or request.match_info.get("slot", "")
    slot = state.get_slot(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    session_key = effective_session_key(slot)
    # Scoped to the channel the caller names. The unnamed clear means EVERY
    # binding, which under single-binding was the same thing — with several it
    # would delete siblings the user never named (the chip labels one channel and
    # would silently drop the rest, losing their `accepts_inbound` with them).
    try:
        want = _body_channel_type(await _read_json_body(request))
    except _BadBody as bad:
        return bad.response
    # Offloaded: `clear_mirror_link` calls `_save`, which serialises the whole
    # session map, and this is a request handler on the gateway's event loop.
    cleared = await asyncio.to_thread(
        state.sessions.clear_mirror_link, session_key, want
    )
    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.mirror_unlink",
        outcome="success" if cleared else "noop",
        source="dashboard",
        resources=slot.key,
    )
    logger.info("mirror-unlink: %s (was_linked=%s)", slot.key, cleared)
    return web.json_response({"ok": True, "was_linked": cleared})
