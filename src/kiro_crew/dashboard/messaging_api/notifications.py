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
        logger,
    )


async def api_notifications(request: web.Request) -> web.Response:
    state: DashboardState = request.app["state"]
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
        return web.json_response({"error": "invalid JSON"}, status=400)
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
        return web.json_response({"error": "invalid JSON"}, status=400)
    ts = body.get("ts", "")
    if not ts:
        return web.json_response({"error": "ts is required"}, status=400)
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
    Also returns ``bridge_transports``: the transport ids a routing rule may
    name, each flagged with whether it can receive right now.
    """
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
    from kiro_crew.dashboard.state import bridge_sink_implemented
    from kiro_crew.notifications.bridge import KNOWN_BRIDGE_TRANSPORTS
    from kiro_crew.notifications.settings import DELIVERY_SETTING_KEYS, PROTECTED_CHANNELS

    state: DashboardState = request.app["state"]
    registered = state.notification_bus.channels()
    stored = state.notification_channel_settings.all_settings()
    # Reading the OWNER's routing choice takes the owner, and "not an app" is a
    # weaker thing: an allow-listed messaging user running ``!dashboard`` holds an
    # ordinary dashboard session whose ``app`` claim is the empty string, so a
    # check on that claim admits them to the two fields that say which chat
    # surfaces the owner's notifications reach. Ownership is the property those
    # fields carry, so it is the property asked for, through the gate the sibling
    # owner-only routes in this package already use. Withholding the fields rather
    # than refusing the route keeps the pre-existing mute and priority contract for
    # every caller that already reads channel settings.
    hide_delivery = not is_owner_dashboard_request(request)
    channels = []
    for channel in sorted(set(registered) | set(stored)):
        source = channel.split(".", 1)[0]
        settings = stored.get(channel, {})
        if hide_delivery and settings:
            settings = {k: v for k, v in settings.items() if k not in DELIVERY_SETTING_KEYS}
        channels.append(
            {
                "channel": channel,
                "source": source,
                "registered": channel in registered,
                "default_priority": registered.get(channel),
                "protected": channel in PROTECTED_CHANNELS,
                "settings": settings,
            }
        )
    # The transport ids a route may name, each with two SEPARATE facts about it.
    # Separate because they answer different questions and a single "available"
    # would have to lie about one of them: `connected` is whether the transport
    # itself is up, and `bridgeable` is whether a bridge sink exists for it at
    # all. Only slack is bridgeable today, so a connected-but-not-bridgeable
    # transport routes to an audited skip -- which the picker must be able to
    # see, or it would offer a row that can never deliver. Validation still uses
    # the KNOWN set, so a transport that is merely down keeps its saved route.
    connected = state.channel_status()
    return web.json_response(
        {
            "channels": channels,
            "bridge_transports": [
                {
                    "transport": transport,
                    "connected": bool(connected.get(transport, {}).get("connected")),
                    "bridgeable": bridge_sink_implemented(transport),
                }
                for transport in KNOWN_BRIDGE_TRANSPORTS
            ],
        }
    )


async def api_notification_channel_settings(request: web.Request) -> web.Response:
    """PUT /api/notifications/channels/settings — update one channel's settings.

    Body: ``{"channel": str, "muted"?: bool, "priority"?: str|null,
    "deliver_to"?: [str]|null, "deliver_min_priority"?: str|null}`` —
    ``priority: null`` clears the override, ``deliver_to: null`` clears the
    bridge route, ``deliver_to: []`` disarms it (what the Settings UI sends
    when the multi-select is emptied). Protected channels reject mute and
    priority-lowering with 400; an unknown transport id or floor is 400 too.
    """
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
    from kiro_crew.notifications.settings import (
        DELIVERY_SETTING_KEYS,
        ChannelSettingsAuthError,
        ChannelSettingsError,
    )
    from kiro_crew.sel import sel

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
    muted = body.get("muted")
    if muted is not None and not isinstance(muted, bool):
        return web.json_response({"error": "muted must be a boolean"}, status=400)
    has_priority = "priority" in body
    priority = body.get("priority")
    if has_priority and priority is not None and not isinstance(priority, str):
        return web.json_response({"error": "priority must be a string or null"}, status=400)
    # Bridge routing. Shape is checked here so a malformed request is a 400
    # rather than reaching the writer; the transport ids and the floor are
    # checked by ChannelSettings.update against the KNOWN transport set, which
    # keeps one validator for the HTTP path and the hand-edited-file path.
    has_deliver_to = "deliver_to" in body
    deliver_to = body.get("deliver_to")
    if has_deliver_to and deliver_to is not None and not isinstance(deliver_to, list):
        return web.json_response(
            {
                "error": "deliver_to must be a list of transport ids or null",
                "code": "invalid_deliver_to",
            },
            status=400,
        )
    has_floor = "deliver_min_priority" in body
    deliver_min_priority = body.get("deliver_min_priority")
    if has_floor and deliver_min_priority is not None and not isinstance(deliver_min_priority, str):
        return web.json_response(
            {
                "error": "deliver_min_priority must be a string or null",
                "code": "invalid_deliver_min_priority",
            },
            status=400,
        )
    # The two routing fields decide whether the OWNER's notifications leave the
    # machine as chat DMs, which is the owner's call. So the check asks for
    # ownership rather than enumerating the principals that lack it: an app token
    # is one of them, and an allow-listed messaging user running ``!dashboard`` is
    # another, holding an ordinary dashboard session whose ``app`` claim is the
    # empty string. A condition on that claim reads as a fence and admits the
    # second one.
    #
    # The check lives here because the transport layer's exclusion is declarative
    # and defeatable: `app_token_path_allowed` deliberately grants only
    # `/api/notifications/push` and its comment says app tokens must not reach
    # `/api/notifications`, but its final clause still honours the app's own
    # `permissions.api`, and `_api_pattern_matches` treats a bare
    # `/api/notifications` prefix as covering every child path. So an installed
    # app that declares that prefix is granted this route, and an owner
    # installing such an app is an ordinary supported flow rather than an attack.
    # Guarding the FIELDS rather than the route keeps the pre-existing
    # mute/priority contract for any app already using it, and refuses exactly
    # the authority the bridge introduced.
    app_name = request.get("app", "")
    is_owner = is_owner_dashboard_request(request)
    if DELIVERY_SETTING_KEYS & set(body) and not is_owner:
        # Audit the denial, but never let an audit failure change the RESPONSE: a bare
        # ``sel()`` construction can raise, and warm_sel_singleton documents SEL
        # degradation as survivable. Unguarded, that raise would abort the handler before
        # the 403 below and surface an unhandled 500 -- a quieter but worse outcome than a
        # logged, audited refusal. The denial itself does not depend on the audit.
        try:
            sel().log_api_access(
                caller=f"app:{app_name}" if app_name else str(request.get("user") or "unknown"),
                operation="notification_channel_delivery_settings",
                outcome="denied",
                source="notifications_api",
                error="caller is not the dashboard owner: delivery routing is owner-only",
            )
        except Exception:
            logger.warning(
                "notifications_api: delivery-routing denial audit failed; the 403 "
                "refusal still holds",
                exc_info=True,
            )
        return web.json_response(
            {
                "error": "notification delivery routing is owner-only",
                "code": "delivery_routing_owner_only",
            },
            status=403,
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
            deliver_to=deliver_to if has_deliver_to and deliver_to is not None else None,
            deliver_min_priority=(
                deliver_min_priority if has_floor and deliver_min_priority is not None else None
            ),
            clear_delivery=has_deliver_to and deliver_to is None,
            # Names the caller only when it lacks routing authority, which is what the
            # armed-channel display fence gates on. An app is one such caller and a
            # non-owner dashboard session is another, and passing the empty string for
            # the second would skip the fence and let it mute a channel that routes.
            app_caller=app_name or ("" if is_owner else str(request.get("user") or "non-owner")),
        )
    except ChannelSettingsAuthError as exc:
        # BEFORE the ChannelSettingsError branch below, which it subclasses. The
        # subclassing is what makes a missed except fail closed rather than open;
        # this branch is only what turns the refusal into the right status.
        try:
            sel().log_api_access(
                caller=f"app:{app_name}" if app_name else str(request.get("user") or "unknown"),
                operation="notification_channel_display_settings",
                outcome="denied",
                source="notifications_api",
                error="caller without routing authority cannot mute or re-rank a routing channel",
            )
        except Exception:
            logger.warning(
                "notifications_api: display-settings denial audit failed; the 403 "
                "refusal still holds",
                exc_info=True,
            )
        return web.json_response(
            {"error": str(exc), "code": "delivery_routing_owner_only"},
            status=403,
        )
    except ChannelSettingsError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    # `entry` is the WHOLE stored row, so it carries the owner's routing whenever
    # a route is armed. Withhold the delivery fields on the way out for a non-owner
    # rather than refusing: the strip is response shaping, and refusing every such
    # request would break the pre-existing contract on the unrouted channels where
    # the display pair is still an app's to set.
    state.broadcast_ws("notification_channel_settings", {"channel": channel, "settings": entry})
    visible = entry
    if not is_owner:
        visible = {k: v for k, v in entry.items() if k not in DELIVERY_SETTING_KEYS}
    return web.json_response({"ok": True, "channel": channel, "settings": visible})
