"""The channel legs of a proactive send: the channel owner's DM, a configured
channel target, and the conversation the calling session belongs to.
"""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.messaging import (
        _DM_TARGET_PREFIX,
        HOST_SESSION_KEY,
        ChannelLink,
        DashboardState,
        _sel,
        chunk_for_transport,
        chunk_text,
        delivery_confirmed,
        display_safe_for,
        logger,
        sole_direct_target,
    )


def _owner_dm_target(transport: Any) -> str:
    """The configured owner-DM target id on *transport*, or ``""``.

    ``configured_targets()`` is the channel-neutral allowlist the dashboard's own
    target picker reads, so a proactive DM can only be addressed at something the
    user configured for that channel. Two entries are skipped: a target the
    transport marks unavailable (WeCom may only reply to an inbound message), and
    a thread or room target, which is a wider audience than a DM.

    No channel carries a separate "owner" field the way Slack's ``owner_id`` does,
    so an owner can only be inferred, and this REFUSES to infer one from an
    ambiguous list: a target is returned only when the channel advertises exactly
    ONE available direct target. Picking the first of several would send private
    agent output to whichever allow-listed person happened to sort first, which is
    the wrong human rather than a smaller audience, and fanning out to all of them
    would deliver a message the agent decided to send once N times. With no single
    answer the caller degrades to the dashboard notification, which reaches the
    operator without guessing who they are.

    The inference itself is :func:`~kiro_crew.messaging.transport.sole_direct_target`,
    shared with session control's owner-DM audience predicate so the two surfaces
    name the same human as the owner; this wrapper adds only the enumeration guard
    and the send-path logging.
    """
    try:
        targets = list(transport.configured_targets())
    except Exception:
        logger.warning("send_message: could not enumerate channel targets", exc_info=True)
        return ""
    target = sole_direct_target(targets)
    if target:
        return target
    direct_count = sum(
        1
        for candidate in targets
        if str(getattr(candidate, "target_id", "") or "").startswith(_DM_TARGET_PREFIX)
        and getattr(candidate, "available", False)
    )
    if direct_count:
        logger.info(
            "send_message: %d configured DM targets and no owner field, so no single "
            "recipient can be inferred; degrading to the dashboard notification",
            direct_count,
        )
    return ""


async def _deliver_channel_dm(
    state: DashboardState, channel_type: str, text: str, *, caller_session: str
) -> tuple[bool, str, str]:
    """Deliver *text* as a DM on *channel_type*. Returns ``(sent, code, detail)``.

    Channel-neutral by construction: the transport is looked up by name, the
    destination comes from its own configured-target allowlist, and delivery runs
    through the shared cross-surface send ladder
    (``chat_runner._resolve_channel_target``), the same fail-closed, SEL-audited
    ``channels`` governance gate the mirror-link endpoint passes. A channel added
    later needs no code here.

    An empty *code* with ``sent=False`` is the SOFT miss: the channel is not
    connected, or the user configured no reachable DM target. It degrades to the
    dashboard notification the caller already got, exactly as an absent Slack
    client does. A non-empty *code* is a refusal or a failed delivery, and the
    caller must answer non-2xx: reporting success for a message the user will
    never see is the failure this contract exists to prevent.
    """
    transport = state.get_channel_transport(channel_type)
    if transport is None:
        logger.info("send_message: channel %s is not connected", channel_type)
        return False, "", ""
    target_id = _owner_dm_target(transport)
    if not target_id:
        logger.info("send_message: channel %s advertises no DM target", channel_type)
        return False, "", ""
    # Imported here, not at module scope: chat_runner imports from
    # kiro_crew.dashboard.handlers, so a top-level import closes that cycle. Only
    # this one is deferred -- `HOST_SESSION_KEY` is a constant and is imported at
    # module scope, where the top-level-imports rule wants it.
    from kiro_crew.dashboard.chat_runner import _resolve_channel_target

    # A cron carries its own validated session key; an out-of-band send owns no
    # session, and the host sentinel is what operators bind host-side governance
    # to (an empty key classifies as unknown and matches no profile at all).
    session_key = caller_session or HOST_SESSION_KEY
    # Vet BEFORE resolving the target: resolution is itself a visible side effect
    # on some channels (Discord opens a DM channel over REST), so a denied
    # channel must never reach it. Offloaded because the governance evaluation
    # reads profile files.
    # The principal is supplied rather than derived: this addresses a
    # ``configured_targets()`` entry, so the link carries a ``user:<id>`` target id
    # and ``session_key`` is a host sentinel naming nobody. The id came off the
    # transport's own allow-list via ``_owner_dm_target``, which is the authoritative
    # answer the recipient check would otherwise be unable to reach.
    governed = await asyncio.to_thread(
        functools.partial(
            _resolve_channel_target,
            state,
            session_key,
            ChannelLink(channel_type=channel_type, channel_id=target_id),
            principal=target_id.removeprefix(_DM_TARGET_PREFIX),
        )
    )
    if governed is None:
        return False, "channel_not_permitted", f"{channel_type} is not permitted"
    _, live_transport = governed
    try:
        resolved = await live_transport.resolve_configured_target(target_id)
    except Exception as exc:
        logger.exception("send_message: %s target resolution failed", channel_type)
        return False, "channel_delivery_failed", str(exc)
    if resolved is None:
        # The advertised target stopped resolving between enumeration and use
        # (revoked from the allow-list, or a thread that stopped being private). Refuse
        # rather than fall back to a wider audience.
        return False, "channel_delivery_failed", f"{channel_type} DM target is unavailable"
    conversation_id, thread_id = resolved
    # Chunked at the transport's own declared ceiling, as a mirrored turn is:
    # Discord rejects a message over its cap outright, so an unchunked long
    # report would arrive as a delivery failure instead of a message. At least one
    # unit always comes back, because the route rejects an empty body. One
    # governance decision covers the whole send -- this is a single message the
    # transport happens to split, not the sequence of independent egress actions
    # the mirror backfill re-vets per unit.
    #
    # ``chunk_for_transport``, not ``chunk_text``: a byte-capped channel (Webex)
    # is reachable here, and its char declaration is only the 4x-pessimistic floor
    # a caller that can measure bytes does not need. The same helper the two
    # cross-surface mirror legs use, so one channel cannot be chunked against a
    # unit it does not have.
    #
    # ``display_safe`` is the display-form floor at the egress rather than only at
    # the caller that happens to exist today: this leg passes no renderer, and a
    # renderer is where a turn gets that floor. `api_send_message` already applies
    # it, so on that path this is a second, idempotent application; what it buys is
    # that a future caller of this helper cannot reach a channel without it. The
    # neutral sink rather than a bare redactor pair, because the leg is
    # channel-NEUTRAL and Slack/Discord both parse broadcast-mention grammars.
    # Offloaded: the display sink threads the credential-aware splitter's whole-text
    # budget search (up to 128 dense probes plus span-repair passes) onto the call,
    # and this leg runs on the gateway's single loop thread with model-authored,
    # length-unchecked text -- a large body would stall the loop past the watchdog's
    # 25s dump-then-exit alarm. Offloaded like the renderer's own send legs.
    units = await asyncio.to_thread(
        chunk_for_transport,
        display_safe_for(text, live_transport.capabilities),
        live_transport.capabilities,
    )
    try:
        for unit in units:
            # Fail on the first UNCONFIRMED unit rather than pressing on: the
            # remaining chunks of a message whose head never landed would arrive as
            # an orphaned fragment. `delivery_confirmed` owns which of the two id
            # conventions this transport follows.
            sent = await live_transport.send_message(conversation_id, unit, thread_id=thread_id)
            if not delivery_confirmed(live_transport.capabilities, sent):
                logger.warning(
                    "send_message: %s returned no message id; treating as undelivered",
                    channel_type,
                )
                return False, "channel_delivery_failed", "the channel returned no message id"
    except Exception as exc:
        logger.exception("send_message: %s delivery failed", channel_type)
        return False, "channel_delivery_failed", str(exc)
    return True, "", ""


def _channel_delivery_key(state: DashboardState, caller_session: str, declared_session: str) -> str:
    """The session whose channel conversation a proactive send should reach.

    Two sources, in order, and the request BODY's own idea of who it is talking to
    is not one of them:

    * a **cron** caller (``caller_session`` has already matched
      ``CRON_SESSION_RE``) names its job, and the job's stored ``session_key`` is
      gateway-owned state — so the conversation is chosen by the scheduler rather
      than by whoever posted the request.
    * any other caller is identified by the ``X-Session-Key`` header, which
      ``token_auth._verify_unix_peer`` kernel-attests against the peer's own
      process ancestry on the AF_UNIX socket and denies on mismatch. A body field
      carries no such check, so naming another session's key there would post
      into a conversation the caller does not own.

    Unlike :func:`_resolve_session_target` this returns the job's session key
    VERBATIM. That function wants a dashboard slot name and strips the
    ``dashboard:`` prefix to get one; channel links are keyed by the full session
    key, so stripping it here would lose a dashboard session's outbound mirror.

    Returns ``""`` when neither source answers, which fails the send closed.
    """
    if caller_session.startswith("cron:"):
        cron_id = caller_session.removeprefix("cron:").split(":")[0]
        jobs = state.crons.list_jobs(include_disabled=True)
        job = next((j for j in jobs if j.id == cron_id), None)
        if job is None:
            return ""
        return job.session_key or ""
    return declared_session


async def _deliver_to_channel(
    state: DashboardState, session_key: str, text: str, *, channel_type: str = ""
) -> bool:
    """Governed proactive send to the channel conversation behind *session_key*.

    Rides the same cross-surface ladder as the auto-compact notice and the
    inbound-unbind notice (``chat_runner._resolve_channel_target``) rather than
    reaching for a transport directly, so the send is capability-checked,
    governance-vetted under the ``channels`` scope and SEL-audited exactly like
    every other outbound notice. Slack is not reachable through it by design —
    that transport is not registered in ``state.channel_transports``.

    *channel_type*, when given, is the transport the caller NAMED. The resolved
    link must match it: a session can only have one channel link, so a mismatch
    means the caller asked for a conversation this session does not have, and
    posting to the link it does have would deliver to an audience nobody asked
    for. Empty accepts whatever the link names.

    Fails closed and returns ``False`` — never falls through to another
    destination — for every reason a send can be refused: no link, a link on
    another transport, a governance denial, an unregistered transport, one that
    cannot send proactively, or a transport error. Each is audited, because a
    proactive message that reached nobody is exactly what the caller must not
    read as success.
    """
    # Lazy: chat_runner imports this package at module scope (MAX_PROMPT_BYTES,
    # _find_prompt), so a top-level import here would close the cycle.
    from kiro_crew.dashboard.chat_runner import _resolve_channel_target

    def _audit(outcome: str, reason: str) -> None:
        try:
            _sel().log_tool_invocation(
                session_key=session_key or "dashboard",
                tool_name="send_message",
                outcome=outcome,
                downstream_service=channel_type or "channel",
                resources=f"channel_type={channel_type} reason={reason}",
            )
        except Exception:
            logger.warning("SEL logging failed for channel send", exc_info=True)

    if not session_key or not text:
        _audit("denied", "no_session_key" if not session_key else "empty_text")
        return False
    # Own inbound conversation first, then the outbound mirror: a channel-born
    # session has the former, a dashboard session linked to a channel has the
    # latter, and only one of the two is ever set for a given session.
    link = state.sessions.get_origin_link(session_key) or state.sessions.get_mirror_link(
        session_key
    )
    if link is None:
        _audit("denied", "no_channel_link")
        return False
    if channel_type and link.channel_type != channel_type:
        _audit("denied", f"link_is_{link.channel_type}")
        return False
    try:
        # Off-loop: the ladder's governance gate walks the profile directory,
        # which is unbounded on slow storage.
        target = await asyncio.to_thread(_resolve_channel_target, state, session_key, link)
    except Exception:
        # Includes PlatformCompositionError, which _resolve_channel_target
        # re-raises. Refusing the send is the fail-closed answer either way, and
        # the audit line is what keeps a broken ceiling from reading as a
        # routine skip.
        logger.warning("channel send: target resolution failed for %s", session_key, exc_info=True)
        _audit("error", "resolve_failed")
        return False
    if target is None:
        # Governance denial, no registered transport, or one that cannot send
        # proactively. The ladder logs which; all three are a refusal here.
        _audit("denied", "not_permitted_or_unregistered")
        return False
    resolved, transport = target
    if not resolved.channel_id:
        _audit("denied", "no_conversation_id")
        return False
    # ``display_safe_for`` is the SHARED outbound display sink (redact against the
    # rendered form, then defang mentions ONLY where the platform parses one).
    # Routing through it rather than re-running the two byte-level scanners is what
    # keeps this from becoming a second, differently-sanitised copy of the same
    # egress boundary — and the capability-aware variant rather than the flat
    # ``display_safe`` because this leg is channel-NEUTRAL: Webex reaches it, has no
    # broadcast grammar, and its allow-list IS email addresses, so a blanket defang
    # would insert a ZWSP into every address the agent prints.
    #
    # CHUNKED against the transport's own cap, like the sibling leg above. A
    # transport caps by SLICING (Telegram's `_cap_text` at 4096), so handing it a
    # longer message loses the tail and still answers with a message id -- a
    # delivery this function would then audit as complete. Chunking is what makes
    # the confirmation mean the whole message. The two legs keep separate loops on
    # purpose: this one splits plain text, while the gateway's splits markdown with
    # fence sealing, and collapsing them would silently retune one of the two.
    parts = chunk_text(
        display_safe_for(text, transport.capabilities), transport.capabilities.max_message_chars
    )
    for part in parts:
        try:
            # "No exception" is not delivery on its own, and auditing it as such
            # would report a success for a message the user never saw -- the one
            # outcome this helper's contract exists to prevent.
            # `delivery_confirmed` owns which id convention this transport follows.
            delivered = await transport.send_message(
                resolved.channel_id,
                part,
                thread_id=resolved.thread_id,
            )
        except Exception:
            logger.warning("channel send: delivery failed for %s", session_key, exc_info=True)
            _audit("error", "transport_error")
            return False
        if not delivery_confirmed(transport.capabilities, delivered):
            logger.warning(
                "channel send: %s returned no message id for %s", channel_type, session_key
            )
            _audit("error", "empty_message_id")
            return False
    _audit("completed", "delivered")
    return True


async def _send_to_channel_target(
    state: DashboardState,
    channel_type: str,
    target_id: str,
    text: str,
    *,
    caller_session: str = "",
) -> web.Response:  # noqa: C901
    """Deliver *text* to an opaque configured target on a registered transport.

    Four gates, all fail-closed, in the order their evidence is cheapest:

    1. **A registered transport.** Expressed as membership in the registry, never
       as ``channel_type != "slack"``: a negation hands every channel added later
       whatever this path grants, in the permissive direction.
    2. **``supports_proactive_send``.** A channel whose reply is bound to an
       inbound token (WeCom) cannot originate a message at all, and saying so is
       better than a confusing platform error.
    3. **Governance.** The same ``channels``-scope chokepoint the mirror leg uses,
       fail-closed, so a profile that narrows after startup stops sends too.
    4. **The transport's own allow-list**, re-applied by
       ``resolve_configured_target``. The opaque id travelled through the browser
       or the model, and the config may have narrowed since it was minted.

    Every non-2xx body carries a machine-readable ``code``: backend strings have
    no i18n catalog path, so the caller needs something stable to branch on.
    """
    transports = getattr(state, "channel_transports", None) or {}
    transport = transports.get(channel_type)
    if transport is None:
        return web.json_response(
            {"error": f"channel {channel_type} is not connected", "code": "channel_not_connected"},
            status=404,
        )
    if not getattr(transport.capabilities, "supports_proactive_send", False):
        return web.json_response(
            {
                "error": f"channel {channel_type} cannot start a conversation",
                "code": "channel_no_proactive_send",
            },
            status=400,
        )
    # Vet under the CALLER's identity, not the destination's. The ``channels``
    # scope resolves against the surface that ORIGINATED the send, so a cron
    # profile permitting only Slack must deny a Webex target; a key synthesized
    # from ``channel_type`` would resolve the DESTINATION channel's own profile
    # instead, making every per-surface operator binding inert on this leg while
    # the sibling ``_deliver_channel_dm`` honours it. Empty ``caller_session``
    # is a non-cron caller (a browser operator, or a direct call): the host
    # sentinel is what operators bind host-side governance to.
    session_key = caller_session or HOST_SESSION_KEY
    # Offloaded: the governance evaluation stats and reads the profile files (and
    # writes a SEL record either way), which is filesystem latency on the shared
    # gateway loop. The sibling ``_deliver_channel_dm`` already runs its own vet
    # through ``asyncio.to_thread`` for exactly this reason.
    gov = await asyncio.to_thread(_vet_channel_send, channel_type, session_key)
    if gov:
        return web.json_response({"error": gov, "code": "channel_denied"}, status=403)
    resolved = await transport.resolve_configured_target(target_id)
    if resolved is None:
        # Audited like the governance denial above: this is a permission decision
        # on an egress chokepoint, and the caller may be the model. A refusal that
        # leaves no record is the one an operator cannot review — someone probing
        # target ids would look identical to normal traffic.
        _sel().log_api_access(
            caller=session_key,
            operation="channel.send_message",
            outcome="denied",
            source="dashboard",
            resources=f"channel={channel_type} reason=target_not_configured",
        )
        return web.json_response(
            {"error": "target is not configured for this channel", "code": "target_not_allowed"},
            status=403,
        )
    conversation_id, thread_id = resolved
    # The DISPLAY sink, not a byte-level redactor pair. This text can come from
    # the model, and a credential split by markdown delimiters
    # (``AKIA**IOSF**ODNN7EXAMPLE``) survives a byte scan and is reassembled whole
    # by the platform's own renderer. ``display_safe`` canonicalizes to the
    # displayed form before scanning, and defangs broadcast-mention grammars —
    # correct here because this leg is channel-NEUTRAL and Slack/Discord do have
    # them.
    # Offloaded for the same reason as the live-transport leg above: the display
    # sink runs the credential-aware splitter's whole-text budget search on the
    # gateway's single loop thread with model-authored, length-unchecked text, and
    # a large body would stall the loop past the watchdog's 25s dump-then-exit alarm.
    parts = await asyncio.to_thread(
        chunk_for_transport,
        display_safe_for(text, transport.capabilities),
        transport.capabilities,
    )
    try:
        for index, part in enumerate(parts):
            # Most transports report a failed send by RETURNING a falsy id rather
            # than raising, so reading only exceptions would answer 200 "ok" for a
            # message that never arrived — worse than an error, because the caller
            # (including the LLM, which cannot see the room) records it as delivered
            # and moves on. But some transports carry no reliable id (WeCom's
            # proactive command, Feishu's reply, iMessage's best-effort GUID) and
            # raise on failure instead, so there the
            # empty string is the SUCCESS value. ``delivery_confirmed`` owns which
            # convention each transport follows, from its own declared
            # ``returns_message_id`` — the alternative is this leg reporting every
            # delivered message on those as lost.
            sent = await transport.send_message(conversation_id, part, thread_id=thread_id)
            if not delivery_confirmed(transport.capabilities, sent):
                raise _ChannelSendFailed(f"part {index + 1} of {len(parts)} was not accepted")
    except Exception as exc:
        logger.warning("channel send failed for %s: %s", channel_type, exc, exc_info=True)
        _sel().log_api_access(
            caller=session_key,
            operation="channel.send_message",
            outcome="error",
            source="dashboard",
            resources=f"channel={channel_type} parts={len(parts)}",
        )
        return web.json_response(
            {"error": "delivery failed", "code": "channel_delivery_failed"}, status=502
        )
    _sel().log_api_access(
        caller=session_key,
        operation="channel.send_message",
        outcome="allowed",
        source="dashboard",
        resources=f"channel={channel_type} parts={len(parts)}",
    )
    return web.json_response({"ok": True, "delivered_to": channel_type, "parts": len(parts)})


class _ChannelSendFailed(Exception):
    """A transport declined a part of a channel-addressed send.

    Raised so the falsy-return path and the raising path converge on one handler:
    the endpoint must not answer 200 for a message the channel never accepted.
    """


def _vet_channel_send(channel_type: str, caller_session: str) -> str:
    """Governance for a channel-addressed send; ``""`` when permitted.

    Fail-closed, and audited by ``vet_and_audit`` on both grant and denial: this
    is an egress chokepoint on a network surface, so a degraded governance
    evaluation must DENY rather than degrade to permit.
    """
    try:
        from kiro_crew.platform.governance_profiles import vet_and_audit

        decision = vet_and_audit(
            "channels",
            channel_type,
            session_key=caller_session,
            tool_name="channel.send_message",
            fail_closed=True,
        )
        if not getattr(decision, "permitted", False):
            return f"channel {channel_type} is denied by the active governance profile"
    except Exception:
        logger.warning("channel send governance check failed", exc_info=True)
        return "governance evaluation unavailable"
    return ""
