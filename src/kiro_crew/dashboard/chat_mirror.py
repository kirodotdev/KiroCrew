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

import json
import logging

from aiohttp import web

from kiro_crew.dashboard.chat_runner import _resolve_mirror_target
from kiro_crew.dashboard.chat_utils import _history_key_for
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.messaging.link import SLACK_NAMESPACE, ChannelLink
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


async def api_chat_slot_mirror_link(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{name}/mirror-link — mirror a session to a channel.

    Body: ``{channel_type, conversation_id, thread_id?}``. Slack is rejected with
    a hint to use ``slack-link`` (which owns Slack's rich thread + streaming
    mirror). The target channel's transport must be registered at boot AND
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
    conversation_id = str(body.get("conversation_id", "") or "").strip()
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
        session_key = _history_key_for(name)
        target = _resolve_mirror_target(state, session_key)
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
    if not conversation_id:
        return web.json_response({"error": "conversation_id required"}, status=400)

    transport = state.get_channel_transport(channel_type)
    if transport is None:
        return web.json_response(
            {"error": f"channel '{channel_type}' not connected"}, status=503
        )
    if not transport.capabilities.supports_proactive_send:
        return web.json_response(
            {"error": f"channel '{channel_type}' cannot mirror (no proactive send)"},
            status=400,
        )

    session_key = _history_key_for(name)
    state.sessions.set_mirror_link(
        session_key,
        ChannelLink(channel_type=channel_type, channel_id=conversation_id, thread_id=thread_id),
    )
    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.mirror_link",
        outcome="success",
        source="dashboard",
        resources=f"{slot.key} -> {channel_type}",
    )
    logger.info(
        "mirror-link: %s -> %s:%s", slot.key, channel_type, conversation_id
    )
    return web.json_response(
        {"ok": True, "channel_type": channel_type, "conversation_id": conversation_id}
    )


async def api_chat_slot_mirror_unlink(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{name}/mirror-unlink — stop mirroring this session.

    Clears the session's outbound mirror binding. Idempotent: unlinking a session
    with no mirror returns ``{ok, was_linked: false}``. Unlike Slack links, a
    mirror link is only ever set on the history (``dashboard:``-prefixed) key by
    ``mirror-link`` — it is never copied onto the bare key — so a single clear on
    that key suffices.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info.get("name") or request.match_info.get("slot", "")
    slot = state.get_slot(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    session_key = _history_key_for(name)
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
