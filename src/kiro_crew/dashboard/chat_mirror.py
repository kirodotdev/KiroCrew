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

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_backfill import (
    backfill_content,
    gap_summary,
    select_backfill_messages,
    session_deep_link,
)
from kiro_crew.dashboard.chat_runner import _resolve_channel_target, _resolve_mirror_target
from kiro_crew.dashboard.chat_slack import list_slack_channels
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.messaging.link import SLACK_NAMESPACE, ChannelLink
from kiro_crew.messaging.renderer import chunk_text
from kiro_crew.platform.context import redact_via_context
from kiro_crew.sel import sel

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
    if not body:
        session_key = effective_session_key(slot)
        target = await asyncio.to_thread(_resolve_mirror_target, state, session_key)
        if target is None:
            existing = state.sessions.get_mirror_link(session_key)
            if existing is None:
                return web.json_response({"error": "channel_type required"}, status=400)
            return web.json_response({"error": "mirror channel is not live"}, status=503)
        link, transport = target
        try:
            await transport.send_message(
                link.channel_id,
                "🔗 Session linked from dashboard — continuing here.",
                thread_id=link.thread_id,
            )
        except Exception:
            logger.debug("mirror-link reminder delivery failed", exc_info=True)
            return web.json_response({"error": "failed to post reminder"}, status=502)
        sel().log_api_access(
            caller="dashboard",
            operation="chat.mirror_reminder",
            outcome="success",
            source="dashboard",
            resources=f"{slot.key} -> {link.channel_type}",
        )
        return web.json_response(
            {"ok": True, "already_linked": True, "channel_type": link.channel_type}
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

    try:
        # Recheck at the actual send boundary as well: target resolution can
        # yield while governance is updated.
        governed = await asyncio.to_thread(_resolve_channel_target, state, session_key, link)
        if governed is None:
            return web.json_response(
                {"error": "channel is not permitted", "code": "channel_not_permitted"}, status=403
            )
        _, live_transport = governed
        await live_transport.send_message(
            conversation_id,
            "Session linked from dashboard — continuing here.",
            thread_id=thread_id,
        )
    except Exception:
        logger.debug("mirror-link initial delivery failed", exc_info=True)
        return web.json_response(
            {"error": "failed to create channel link", "code": "channel_link_failed"}, status=502
        )

    # Build the ordered delivery units BEFORE the loop. Each unit is one Slack-
    # free chunk of text, and the gap marker is a unit like any other, so every
    # single thing that crosses the egress boundary gets its own governance
    # re-check below rather than riding along on a message's decision.
    #
    # Offloaded: selection reads the on-disk transcript when the opening turn is
    # off-window, and that read parses every tab_id sibling file. On the loop
    # thread it would stall every other chat turn and the liveness heartbeat.
    selection = await asyncio.to_thread(select_backfill_messages, state, slot)
    max_chars = (
        getattr(getattr(live_transport, "capabilities", None), "max_message_chars", 0)
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

    # Bound the INLINE delivery. Unlike the Slack drain this cannot be
    # backgrounded -- the per-unit governance re-check below has to be able to
    # fail the request closed with 403 -- so an unbounded loop would reintroduce
    # the very defect backgrounding fixed on the Slack side: a rate-limited
    # transport (Telegram is roughly one message per second) would hold the HTTP
    # request open past the browser's fetch timeout and the user would see a
    # failed link that had actually persisted.
    #
    # The budget is spent on WHOLE turns in priority order, and every turn it
    # cannot afford is folded into the gap marker's count. Trimming composed
    # units instead would cut a reply mid-sentence and could drop the marker
    # itself -- the one line telling the reader history is missing.
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
            logger.debug("mirror-link: could not build session link", exc_info=True)
        units.append(f"… {summary} — {deep_link}" if deep_link else f"… {summary}")
    for turn_units in kept:
        units.extend(turn_units)

    for unit in units:
        try:
            # Historical context is a sequence of separate egress actions.
            # Stop immediately if policy narrows while the loop is yielding.
            governed = await asyncio.to_thread(_resolve_channel_target, state, session_key, link)
            if governed is None:
                # Policy narrowed mid-delivery: fail closed. Do NOT fall
                # through to set_mirror_link (which would persist a link the
                # latest governance decision denied) and do NOT report
                # success. The denial is already SEL-audited inside
                # _resolve_channel_target via vet_and_audit.
                return web.json_response(
                    {"error": "channel is not permitted", "code": "channel_not_permitted"},
                    status=403,
                )
            _, live_transport = governed
            await live_transport.send_message(
                conversation_id,
                unit,
                thread_id=thread_id,
            )
        except Exception:
            logger.debug("mirror-link context delivery failed", exc_info=True)

    state.sessions.set_mirror_link(
        session_key,
        link,
    )
    sel().log_api_access(
        caller="dashboard",
        operation="chat.mirror_link",
        outcome="success",
        source="dashboard",
        resources=f"{slot.key} -> {channel_type}",
    )
    state.push_slots_update()
    logger.info("mirror-link: %s -> %s:%s", slot.key, channel_type, conversation_id)
    return web.json_response(
        {"ok": True, "channel_type": channel_type, "conversation_id": conversation_id}
    )


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
        return web.json_response({"error": "not found"}, status=404)

    session_key = effective_session_key(slot)
    cleared = state.sessions.clear_mirror_link(session_key)
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
