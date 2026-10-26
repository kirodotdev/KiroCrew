"""The notification feed (list, delete, clear, ack, unack, ack-all) and the
per-channel notification settings.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.messaging import (
        APPS_OFF_FOR_TEAMMATES,
        CronStoreBusy,
        CronStoreUnreadable,
        DashboardState,
        audit_teammate_withheld,
        body_not_object_error,
        is_app_channel,
        is_relay_teammate,
        logger,
        notification_source_app,
        refuse_relayed,
    )


async def api_notifications(request: web.Request) -> web.Response:
    state: DashboardState = request.app["state"]
    if is_relay_teammate(request):
        # A relayed teammate has no app surface, so an app's push notifications are
        # withheld, as the live WebSocket and SSE feeds withhold them. Unread is the
        # delivery counter capped at what this view still counts.
        notes = [note for note in state._notification_log if not _is_app_note(note)]
        if len(notes) != len(state._notification_log):
            audit_teammate_withheld("notification")
        counted = sum(1 for note in notes if note.get("priority") != "passive")
        unread = min(state._unread_count, counted)
        return web.json_response({"notifications": notes, "unread": unread})
    return web.json_response(
        {"notifications": state._notification_log, "unread": state._unread_count}
    )


async def api_notification_delete(request: web.Request) -> web.Response:
    """DELETE /api/notifications — delete a single notification by timestamp."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object
    ts = body.get("ts", "")
    if not ts:
        return web.json_response({"error": "ts is required"}, status=400)
    refused = _refuse_hidden_note(request, state, ts, "notifications.delete")
    if refused is not None:
        return refused
    ok = await state.delete_notification(ts)
    return web.json_response({"ok": ok})


def _is_app_note(note: dict) -> bool:
    """Whether *note* is an app's push notification, which a relayed teammate never sees."""
    return bool(notification_source_app(str(note.get("source", ""))))


def _refuse_hidden_note(
    request: web.Request, state: DashboardState, ts: str, operation: str
) -> web.Response | None:
    """A relayed teammate's refusal for one notification withheld from them, else ``None``.

    The list never shows a teammate an app's notification, so one they name by its
    timestamp is refused rather than changed on the owner's behalf.
    """
    if not is_relay_teammate(request):
        return None
    if any(note.get("ts") == ts and _is_app_note(note) for note in state._notification_log):
        return refuse_relayed(request, APPS_OFF_FOR_TEAMMATES, operation=operation)
    return None


async def api_notifications_clear(request: web.Request) -> web.Response:
    """POST /api/notifications/clear — clear all notifications."""
    state: DashboardState = request.app["state"]
    if is_relay_teammate(request):
        hidden = [note for note in state._notification_log if _is_app_note(note)]
        if hidden:
            # Clear what the teammate sees and keep what they are withheld: no
            # whole-feed clear, and no clear broadcast that would empty the owner's.
            state._notification_log = hidden
            still_counted = sum(1 for note in hidden if note.get("priority") != "passive")
            state._unread_count = min(state._unread_count, still_counted)
            await state._rewrite_notifications_async()
            audit_teammate_withheld("notifications_clear")
            return web.json_response({"ok": True})
    await state.clear_notifications()
    return web.json_response({"ok": True})


async def api_notification_ack(request: web.Request) -> web.Response:
    """POST /api/notifications/ack — mark a single notification as read."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object
    ts = body.get("ts", "")
    if not ts:
        return web.json_response({"error": "ts is required"}, status=400)
    refused = _refuse_hidden_note(request, state, ts, "notifications.ack")
    if refused is not None:
        return refused
    ok = await state.ack_notification(ts)
    return web.json_response({"ok": ok})


async def api_notification_unack(request: web.Request) -> web.Response:
    """POST /api/notifications/unack — mark a single notification as unread."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object
    ts = body.get("ts", "")
    if not ts:
        return web.json_response({"error": "ts is required"}, status=400)
    refused = _refuse_hidden_note(request, state, ts, "notifications.unack")
    if refused is not None:
        return refused
    # If this is a cron notification, also remove the last acked item from the job
    for n in state._notification_log:
        if n.get("ts") == ts and n.get("kind") == "cron" and n.get("job_id"):
            try:
                await state.crons.unack_job_async(n["job_id"])
            except (CronStoreBusy, CronStoreUnreadable) as exc:
                # Store transiently contended, or refusing writes outright — the
                # notification-level unack below still succeeds; the acked-item
                # trim is best-effort. Unreadable degrades here rather than
                # surfacing a 409: the person unacking a notification asked
                # nothing of the cron store, so failing their request for a
                # bookkeeping trim they did not request would be the wrong
                # trade. Escaping instead becomes a 500 on a request that does
                # not depend on the store at all.
                logger.warning("unack_job skipped: %s (job %s)", type(exc).__name__, n["job_id"])
            break
    ok = await state.unack_notification(ts)
    return web.json_response({"ok": ok})


async def api_notifications_ack_all(request: web.Request) -> web.Response:
    """POST /api/notifications/ack-all — mark all notifications as read."""
    state: DashboardState = request.app["state"]
    if is_relay_teammate(request):
        # Acknowledge what the teammate sees: an app notification withheld from them
        # keeps its read state, so the ack goes out per entry rather than as "*".
        acknowledged = []
        withheld = False
        for note in state._notification_log:
            if _is_app_note(note):
                withheld = True
            elif not note.get("acked"):
                note["acked"] = True
                acknowledged.append(note.get("ts"))
        await state._rewrite_notifications_async()
        for ts in acknowledged:
            state.broadcast_ws("notification_ack", {"ts": ts})
        if withheld:
            audit_teammate_withheld("notification_ack")
        return web.json_response({"ok": True})
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
    """
    from kiro_crew.notifications.settings import PROTECTED_CHANNELS

    state: DashboardState = request.app["state"]
    registered = state.notification_bus.channels()
    stored = state.notification_channel_settings.all_settings()
    # A relayed teammate has no app surface, so an app's channels are left out.
    teammate = is_relay_teammate(request)
    withheld = False
    channels = []
    for channel in sorted(set(registered) | set(stored)):
        if teammate and is_app_channel(channel):
            withheld = True
            continue
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
    if withheld:
        audit_teammate_withheld("notification_channel_settings")
    return web.json_response({"channels": channels})


async def api_notification_channel_settings(request: web.Request) -> web.Response:
    """PUT /api/notifications/channels/settings — update one channel's settings.

    Body: ``{"channel": str, "muted"?: bool, "priority"?: str|null}`` —
    ``priority: null`` clears the override. Protected channels reject mute
    and priority-lowering with 400.
    """
    from kiro_crew.notifications.settings import ChannelSettingsError

    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        # Valid-but-non-object JSON ([], null, "str") would AttributeError on
        # body.get below -- an unintended 500 instead of a validation 400.
        return web.json_response({"error": "body must be a JSON object"}, status=400)
    channel = body.get("channel")
    if not isinstance(channel, str) or not channel.strip():
        return web.json_response({"error": "channel is required"}, status=400)
    channel = channel.strip()
    if len(channel) > 256:
        return web.json_response({"error": "channel name too long"}, status=400)
    if is_relay_teammate(request) and is_app_channel(channel):
        # The same refusal, and SEL row, the remote limits give a teammate on an app
        # route: the channel is named in the body, so the ingress cannot decide it.
        return refuse_relayed(
            request, APPS_OFF_FOR_TEAMMATES, operation="notifications.channel_settings"
        )
    muted = body.get("muted")
    if muted is not None and not isinstance(muted, bool):
        return web.json_response({"error": "muted must be a boolean"}, status=400)
    has_priority = "priority" in body
    priority = body.get("priority")
    if has_priority and priority is not None and not isinstance(priority, str):
        return web.json_response({"error": "priority must be a string or null"}, status=400)
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
        )
    except ChannelSettingsError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    state.broadcast_ws("notification_channel_settings", {"channel": channel, "settings": entry})
    return web.json_response({"ok": True, "channel": channel, "settings": entry})
