"""The notification feed (list, delete, clear, ack, unack, ack-all) and the
per-channel notification settings.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.messaging import (
        CronStoreBusy,
        CronStoreUnreadable,
        DashboardState,
        body_not_object_error,
        clear_cron_item_recorded,
        cron_note_transition,
        logger,
        recorded_cron_item,
        unread_count,
    )


async def api_notifications(request: web.Request) -> web.Response:
    state: DashboardState = request.app["state"]
    return web.json_response(
        {
            "notifications": state._notification_log,
            "unread": unread_count(state._notification_log),
        }
    )


async def api_notification_delete(request: web.Request) -> web.Response:
    """DELETE /api/notifications — delete a single notification by timestamp."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object
    ts = body.get("ts", "")
    if not ts:
        return web.json_response({"error": "ts is required"}, status=400)
    ok = await state.delete_notification(ts)
    return web.json_response({"ok": ok})


async def api_notifications_clear(request: web.Request) -> web.Response:
    """POST /api/notifications/clear — clear all notifications."""
    state: DashboardState = request.app["state"]
    await state.clear_notifications()
    return web.json_response({"ok": True})


async def api_notification_ack(request: web.Request) -> web.Response:
    """POST /api/notifications/ack — mark a single notification as read."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object
    ts = body.get("ts", "")
    if not ts:
        return web.json_response({"error": "ts is required"}, status=400)
    ok = await state.ack_notification(ts)
    return web.json_response({"ok": ok})


async def api_notification_unack(request: web.Request) -> web.Response:
    """POST /api/notifications/unack — mark a single notification as unread."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object
    ts = body.get("ts", "")
    if not ts:
        return web.json_response({"error": "ts is required"}, status=400)
    # Take back the exact entry this note's cron ack added, and nothing for a
    # note read any other way (see dashboard.cron_note_ack). One step per
    # note, so it cannot interleave with that note's ack.
    async with cron_note_transition(state, ts):
        recorded = recorded_cron_item(state, ts)
        if recorded is not None:
            job_id, ack_id = recorded
            try:
                await state.crons.unack_job_async(job_id, ack_id)
                # Removed now, or already gone from the job: either way the
                # record has nothing left to take back.
                clear_cron_item_recorded(state, ts)
            except (CronStoreBusy, CronStoreUnreadable) as exc:
                # Store transiently contended, or refusing writes outright —
                # the notification-level unack below still succeeds; the
                # acked-item trim is best-effort. Unreadable degrades here
                # rather than surfacing a 409: the person unacking a
                # notification asked nothing of the cron store, so failing
                # their request for a bookkeeping trim they did not request
                # would be the wrong trade. Escaping instead becomes a 500 on
                # a request that does not depend on the store at all.
                logger.warning("unack_job skipped: %s (job %s)", type(exc).__name__, job_id)
    ok = await state.unack_notification(ts)
    return web.json_response({"ok": ok})


async def api_notifications_ack_all(request: web.Request) -> web.Response:
    """POST /api/notifications/ack-all — mark all notifications as read."""
    state: DashboardState = request.app["state"]
    for n in state._notification_log:
        n["acked"] = True
    # Same ordered executor as every other notification-file mutation: a
    # rewrite submitted after a queued delivery append can never be
    # overtaken by it, and durability is awaited before responding.
    await state._rewrite_notifications_async()
    state.broadcast_ws("notification_ack", {"ts": "*"})
    return web.json_response({"ok": True})


async def api_notification_channels(request: web.Request) -> web.Response:
    """GET /api/notifications/channels — registered channels + user settings.

    Returns every channel the bus knows about, grouped by source (``system``
    or the owning app name), each with its default priority, the user's
    stored settings, and whether it is protected (approval cannot be muted).
    Channels with stored settings but no live registration (e.g. app
    currently disabled) are included so mutes remain visible and editable.
    ``bridge_transports`` lists the transports the notification bridge has a
    sink for, which is what the Settings route control may offer.
    """
    from kiro_crew.notifications.settings import PROTECTED_CHANNELS

    state: DashboardState = request.app["state"]
    registered = state.notification_bus.channels()
    stored = state.notification_channel_settings.all_settings()
    channels = []
    for channel in sorted(set(registered) | set(stored)):
        source = channel.split(".", 1)[0]
        channels.append(
            {
                "channel": channel,
                "source": source,
                "registered": channel in registered,
                "default_priority": registered.get(channel),
                "protected": channel in PROTECTED_CHANNELS,
                "settings": stored.get(channel, {}),
            }
        )
    # The transports a bridge route can actually deliver to (those with a
    # registered sink), so Settings offers no destination that would be skipped.
    bridge = getattr(state, "notification_bridge", None)
    bridge_transports = list(getattr(bridge, "transports", ()) or ())
    return web.json_response({"channels": channels, "bridge_transports": bridge_transports})


async def api_notification_channel_settings(request: web.Request) -> web.Response:
    """PUT /api/notifications/channels/settings — update one channel's settings.

    Body: ``{"channel": str, "muted"?: bool, "priority"?: str|null,
    "deliver_to"?: list[str]|null, "deliver_min_priority"?: str}`` —
    ``priority: null`` clears the override; ``deliver_to: null`` or ``[]`` clears
    the bridge route. Protected channels reject mute and priority-lowering, and
    an unknown transport or floor is refused, with 400.
    """
    from kiro_crew.notifications.settings import ChannelSettingsError

    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        # Valid-but-non-object JSON ([], null, "str") would AttributeError on
        # body.get below -- an unintended 500 instead of a validation 400.
        return web.json_response(
            {"error": "body must be a JSON object", "code": "body_not_object"}, status=400
        )
    channel = body.get("channel")
    if not isinstance(channel, str) or not channel.strip():
        return web.json_response({"error": "channel is required"}, status=400)
    channel = channel.strip()
    if len(channel) > 256:
        return web.json_response({"error": "channel name too long"}, status=400)
    muted = body.get("muted")
    if muted is not None and not isinstance(muted, bool):
        return web.json_response({"error": "muted must be a boolean"}, status=400)
    has_priority = "priority" in body
    priority = body.get("priority")
    if has_priority and priority is not None and not isinstance(priority, str):
        return web.json_response({"error": "priority must be a string or null"}, status=400)
    # The bridge route: deliver_to null clears it like [] does; the values are
    # validated in update(), which refuses unknown transports and floors.
    deliver_to = body.get("deliver_to")
    if "deliver_to" in body and deliver_to is None:
        deliver_to = []
    if deliver_to is not None and not isinstance(deliver_to, list):
        return web.json_response(
            {"error": "deliver_to must be a list or null", "code": "invalid_deliver_to"}, status=400
        )
    deliver_min_priority = body.get("deliver_min_priority")
    if deliver_min_priority is not None and not isinstance(deliver_min_priority, str):
        return web.json_response(
            {
                "error": "deliver_min_priority must be a string",
                "code": "invalid_deliver_min_priority",
            },
            status=400,
        )
    try:
        # update() persists via atomic_write (blocking file I/O) -- keep it
        # off the event loop. ChannelSettings serializes internally with its
        # own lock, so concurrent updates from worker threads are safe.
        entry = await asyncio.to_thread(
            state.notification_channel_settings.update,
            channel,
            muted=muted,
            priority=priority if has_priority and priority is not None else None,
            clear_priority=has_priority and priority is None,
            deliver_to=deliver_to,
            deliver_min_priority=deliver_min_priority,
        )
    except ChannelSettingsError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    state.broadcast_ws("notification_channel_settings", {"channel": channel, "settings": entry})
    return web.json_response({"ok": True, "channel": channel, "settings": entry})
