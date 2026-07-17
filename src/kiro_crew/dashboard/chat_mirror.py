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

import logging

from aiohttp import web

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

    body = await request.json() if request.content_length else {}
    channel_type = str(body.get("channel_type", "") or "").strip()
    conversation_id = str(body.get("conversation_id", "") or "").strip()
    thread_id = str(body.get("thread_id", "") or "").strip() or None

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
    sel().log_api_access(
        caller="dashboard",
        operation="chat.mirror_unlink",
        outcome="success" if cleared else "noop",
        source="dashboard",
        resources=slot.key,
    )
    logger.info("mirror-unlink: %s (was_linked=%s)", slot.key, cleared)
    return web.json_response({"ok": True, "was_linked": cleared})
