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
        redact_via_context,
        render_options_as_text,
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
    # these two are deferred -- `HOST_SESSION_KEY` is a constant and is imported at
    # module scope, where the top-level-imports rule wants it.
    from kiro_crew.dashboard.chat_runner import _authorize_recipient, _resolve_channel_target

    # A cron carries its own validated session key; an out-of-band send owns no
    # session, and the host sentinel is what operators bind host-side governance
    # to (an empty key classifies as unknown and matches no profile at all).
    session_key = caller_session or HOST_SESSION_KEY
    # Vet BEFORE resolving the target: resolution is itself a visible side effect
    # on some channels (Discord opens a DM channel over REST), so a denied
    # channel must never reach it. Offloaded because the governance evaluation
    # reads profile files.
    #
    # check_recipient=False, because this link carries the configured-target
    # spelling (``user:<id>``) rather than a conversation id, and ``may_send_to``
    # is a recipient predicate over conversation ids: a transport that judges the
    # id itself (Telegram, whose private ``chat_id`` IS the peer's user id) can
    # never match the prefixed spelling against its roster of bare ids. Governance
    # and transport capability still gate the resolve here; the recipient is
    # decided below, against the resolved conversation, the same two-step the
    # mirror-link creation handler runs.
    governed = await asyncio.to_thread(
        functools.partial(
            _resolve_channel_target,
            state,
            session_key,
            ChannelLink(channel_type=channel_type, channel_id=target_id),
            check_recipient=False,
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
    # Recipient authorization on the RESOLVED conversation, through the one shared
    # decision the ladder's own recipient leg runs, so the two cannot drift. It
    # fails closed on a raising transport and SEL-audits a denial. The principal is
    # supplied rather than derived: ``session_key`` is a host sentinel naming
    # nobody, and the id came off the transport's own allow-list via
    # ``_owner_dm_target``, which is the authoritative answer for a transport that
    # judges the peer rather than the conversation (a Discord DM channel id cannot
    # be tested against a user roster). Offloaded like the ladder call above,
    # whose recipient leg this is.
    permitted = await asyncio.to_thread(
        functools.partial(
            _authorize_recipient,
            live_transport,
            channel_type,
            conversation_id,
            thread_id,
            principal=target_id.removeprefix(_DM_TARGET_PREFIX),
            session_key=session_key,
        )
    )
    if not permitted:
        logger.info(
            "send_message: outbound to %s refused - recipient not allow-listed", channel_type
        )
        return False, "channel_not_permitted", f"{channel_type} is not permitted"
    # Chunked at the transport's own declared ceiling, as a mirrored turn is:
    # Discord rejects a message over its cap outright, so an unchunked long
    # report would arrive as a delivery failure instead of a message. At least one
    # unit always comes back, because the route rejects an empty body. Unlike
    # `_deliver_to_channel`, this leg decides once for all parts: a deferred gap.
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
        # ``send_message`` takes no choices, so a proactive send builds no button
        # even where a reply would: number the ``[OPTIONS:]`` choices instead of
        # sending the raw marker. After the display pass, whose control-comment
        # strip would otherwise leave a trailing tag hiding the trailer. The two
        # legs below do the same.
        render_options_as_text(
            display_safe_for(text, live_transport.capabilities, redactor=redact_via_context),
            live_transport.capabilities,
        ),
        live_transport.capabilities,
        redactor=redact_via_context,
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
        job = _cron_job_for(state, caller_session)
        if job is None:
            return ""
        return job.session_key or ""
    return declared_session


def _cron_job_for(state: DashboardState, caller_session: str) -> Any | None:
    """The cron job a ``cron:<job_id>[:<run>]`` caller names, or ``None``.

    The one place the job id is read off the key: per-run keys carry extra
    segments (``cron:<job_id>:<run_id>``), and only the first names the job.
    """
    cron_id = caller_session.removeprefix("cron:").split(":")[0]
    jobs = state.crons.list_jobs(include_disabled=True)
    return next((j for j in jobs if j.id == cron_id), None)


async def _deliver_to_channel(
    state: DashboardState, session_key: str, text: str, *, channel_type: str = ""
) -> tuple[bool, str, str]:
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

    Authorization is re-asked before EVERY part, not once for the whole message.
    A long body is chunked, and the parts are independent egress actions however
    much they read as one message: under a single up-front decision an operator
    who revokes the binding, narrows the governance ceiling or drops the
    recipient mid-send still has the remaining parts delivered at an authority
    that no longer exists. Each part re-polls the profile store, re-walks the
    binding, re-resolves the ladder and re-decides the recipient with nothing
    awaited before the send, so a revocation stops the remainder. The parts
    already accepted cannot be recalled, which is the floor this leg reaches.

    Returns ``(sent, code, detail)``. An empty *code* is every refusal this leg
    already had, so the caller's existing answer for those is unchanged. A
    refusal that is a PERMISSION decision answers ``channel_not_permitted``,
    which the caller reports as 403 instead of the generic 502: a revocation is
    not a fault, and a caller retrying it cannot succeed. A transport fault
    keeps the empty code and stays retryable. Each outcome is audited, because a
    proactive message that reached nobody is what the caller must not read as
    success.
    """
    # Lazy: chat_runner imports this package at module scope (MAX_PROMPT_BYTES,
    # _find_prompt), so a top-level import here would close the cycle.
    from kiro_crew.dashboard.chat_runner import (
        _authorize_recipient,
        _resolve_channel_target,
        _session_principal,
    )
    from kiro_crew.platform.context import current_context
    from kiro_crew.platform.governance_profiles import (
        governance_answer_generation,
        poll_profiles_fresh,
    )

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

    def _walk_ladder() -> tuple[Any, bool]:
        """The session's current binding and whether it is the inbound one.

        Own inbound conversation first, then the outbound mirror: a channel-born
        session has the former, a dashboard session linked to a channel has the
        latter, and only one of the two is ever set. Which of the two answered is
        returned alongside it because a session that gained a mirror link after
        losing its origin link would otherwise compare as unchanged.
        """
        origin = state.sessions.get_origin_link(session_key)
        if origin is not None:
            return origin, True
        return state.sessions.get_mirror_link(session_key), False

    if not session_key or not text:
        _audit("denied", "no_session_key" if not session_key else "empty_text")
        return False, "", ""
    link, link_is_origin = _walk_ladder()
    if link is None:
        _audit("denied", "no_channel_link")
        return False, "", ""
    if channel_type and link.channel_type != channel_type:
        _audit("denied", f"link_is_{link.channel_type}")
        return False, "", ""

    def _binding_unchanged(candidate: Any, candidate_is_origin: bool) -> bool:
        """Is *candidate* still the binding this send resolved against?

        SYNCHRONOUS BY DESIGN, and that is the point of the check: an await in
        here would yield between the comparison and the send, which is exactly
        the window it exists to close. ``ChannelLink`` is a plain dataclass, so
        ``==`` compares every field and one added later is covered for free --
        a hand-written field list is the copy that passes a moved binding the
        day it forgets one. The ``is not None`` arm is NOT redundant with
        ``==``: an unlinked session walks to ``None``, and comparing that
        against a live link must answer "changed" rather than read as equal.
        """
        return candidate is not None and candidate == link and candidate_is_origin == link_is_origin

    def _generation_now() -> int | None:
        """The governance answer generation, or ``None`` when unreadable.

        ``None`` means the question could not be asked, which is never evidence
        that a permit holds.
        """
        try:
            return governance_answer_generation()
        except Exception:
            logger.warning("channel send: governance generation unreadable", exc_info=True)
            return None

    def _resolve_now() -> tuple[int | None, int | None, Any]:
        """Re-poll profiles, then resolve the ladder, bracketed by generations.

        The profile store is re-polled per call or a revocation landing mid-send
        would be answered from cache and the re-ask could not see it. The
        generation is sampled BEFORE and AFTER because the profile layer bumps
        that counter inside the authorization read itself, so a narrowing during
        the walk shows up only in the later sample.
        """
        current_context()
        poll_profiles_fresh()
        opened = _generation_now()
        resolved_target = _resolve_channel_target(state, session_key, link)
        return opened, _generation_now(), resolved_target

    def _ladder_refusal(index: int | None) -> tuple[bool, str, str]:
        """Classify the ladder's single ``None`` into a denial or a fault.

        The ladder answers ``None`` for a governance denial, an unregistered
        transport AND one that cannot send proactively, so the transport is read
        here to tell a permission decision from a fault: only the former is a
        refusal a retry cannot fix. *index* names the part being guarded, or is
        ``None`` for the up-front resolve.
        """
        suffix = "" if index is None else f"_before_part_{index + 1}"
        usable = state.get_channel_transport(link.channel_type)
        if usable is None or not getattr(usable.capabilities, "supports_proactive_send", False):
            _audit("error", f"transport_unavailable{suffix}")
            return False, "", ""
        _audit("denied", f"not_permitted{suffix}")
        return False, "channel_not_permitted", f"{link.channel_type} is not permitted"

    try:
        # Off-loop: the ladder's governance gate walks the profile directory,
        # which is unbounded on slow storage.
        _, _, target = await asyncio.to_thread(_resolve_now)
    except Exception:
        # Includes PlatformCompositionError, which _resolve_channel_target
        # re-raises. Refusing the send is the fail-closed answer either way.
        logger.warning("channel send: target resolution failed for %s", session_key, exc_info=True)
        _audit("error", "resolve_failed")
        return False, "", ""
    if target is None:
        return _ladder_refusal(None)
    resolved, transport = target
    if not resolved.channel_id:
        _audit("denied", "no_conversation_id")
        return False, "", ""
    # The gate above awaits, so the binding can have moved across it. A fresh
    # WALK, not a re-read of one row: a rebind can move the row.
    if not _binding_unchanged(*_walk_ladder()):
        _audit("denied", "link_changed_during_resolve")
        logger.info(
            "channel send: binding for %s changed while the governance gate ran; refusing",
            session_key,
        )
        return False, "channel_not_permitted", "the channel binding changed during the send"
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
        render_options_as_text(
            display_safe_for(text, transport.capabilities, redactor=redact_via_context),
            transport.capabilities,
        ),
        transport.capabilities.max_message_chars,
    )

    def _aborted(index: int, why: str) -> str:
        return (
            f"{why}; aborted before part {index + 1} of {len(parts)} "
            f"({index} already delivered)"
        )

    async def _still_permitted(index: int) -> tuple[bool, str, str]:
        """Re-ask binding, governance and transport identity before one part.

        Two attempts at most: a ceiling published while the first resolve ran
        leaves its answer stale, so the question is re-asked once rather than
        refusing a send nothing actually narrowed. Both attempts AUTHORIZED --
        a denial returns immediately -- so an unsettled token means only that
        the reading would not settle, which a retry can fix.
        """
        for attempt in (0, 1):
            try:
                opened, decided, recheck = await asyncio.to_thread(_resolve_now)
            except Exception:
                logger.warning("channel send: re-resolve failed for %s", session_key, exc_info=True)
                _audit("error", f"resolve_failed_before_part_{index + 1}")
                return False, "", ""
            if recheck is None:
                logger.warning(
                    "channel send: %s no longer resolves for %s; aborted before part %d of %d "
                    "(%d already delivered)",
                    channel_type or link.channel_type,
                    session_key,
                    index + 1,
                    len(parts),
                    index,
                )
                return _ladder_refusal(index)
            # Refuse, never retarget: `parts` was sanitised and chunked for the
            # CAPTURED transport, so a replacement cannot be handed that tail.
            if recheck[1] is not transport:
                _audit("error", f"transport_replaced_before_part_{index + 1}")
                logger.warning(
                    "channel send: the transport behind %s was replaced mid-send; aborted "
                    "before part %d of %d (%d already delivered)",
                    session_key,
                    index + 1,
                    len(parts),
                    index,
                )
                return False, "", ""
            # The re-resolve awaits, so the binding can have moved across it.
            # This walk is the binding one: nothing yields between it and the send.
            if not _binding_unchanged(*_walk_ladder()):
                _audit("denied", f"link_changed_before_part_{index + 1}")
                logger.warning(
                    "channel send: binding for %s changed while the re-resolve ran; aborted "
                    "before part %d of %d (%d already delivered)",
                    session_key,
                    index + 1,
                    len(parts),
                    index,
                )
                return (
                    False,
                    "channel_not_permitted",
                    _aborted(index, "the channel binding changed"),
                )
            # Governance was decided off-loop and neither the walk above nor the
            # recipient check re-reads it, so its generation is compared here.
            settled = _generation_now()
            readings = (opened, decided, settled)
            if all(r is not None for r in readings) and opened == decided == settled:
                return True, "", ""
            if attempt:
                unreadable = any(r is None for r in readings)
                _audit(
                    "error",
                    (
                        f"governance_unreadable_before_part_{index + 1}"
                        if unreadable
                        else f"governance_unsettled_before_part_{index + 1}"
                    ),
                )
                logger.warning(
                    "channel send: governance for %s kept moving, or could not be read, "
                    "across the re-resolve; aborted before part %d of %d (%d delivered)",
                    session_key,
                    index + 1,
                    len(parts),
                    index,
                )
                return False, "", ""
        return False, "", ""

    async def _send_part(index: int, part: str) -> tuple[bool, str, str]:
        """Re-decide the recipient, then deliver one part.

        The ladder's recipient predicate ran inside the off-loop resolve, so a
        live-config apply on the event loop can drop this recipient after it.
        Re-decided here with nothing awaited in between, through the one shared
        decision the ladder's own recipient leg runs so the two cannot drift.
        Denial-only auditing, as every per-send leg keeps: this runs per
        delivered part, and an allowed record would write a row per chunk.
        """
        if not _authorize_recipient(
            transport,
            resolved.channel_type or link.channel_type,
            resolved.channel_id,
            resolved.thread_id,
            principal=_session_principal(session_key),
            session_key=session_key,
        ):
            _audit("denied", f"recipient_revoked_before_part_{index + 1}")
            logger.warning(
                "channel send: recipient for %s is no longer allow-listed; aborted before "
                "part %d of %d (%d already delivered)",
                session_key,
                index + 1,
                len(parts),
                index,
            )
            return (
                False,
                "channel_not_permitted",
                _aborted(index, "the recipient is no longer allow-listed"),
            )
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
            return False, "", ""
        if not delivery_confirmed(transport.capabilities, delivered):
            logger.warning(
                "channel send: %s returned no message id for %s", channel_type, session_key
            )
            _audit("error", "empty_message_id")
            return False, "", ""
        return True, "", ""

    for index, part in enumerate(parts):
        permitted, code, detail = await _still_permitted(index)
        if not permitted:
            return False, code, detail
        sent, code, detail = await _send_part(index, part)
        if not sent:
            return False, code, detail
    _audit("completed", "delivered")
    return True, "", ""


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
        render_options_as_text(
            display_safe_for(text, transport.capabilities, redactor=redact_via_context),
            transport.capabilities,
        ),
        transport.capabilities,
        redactor=redact_via_context,
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
