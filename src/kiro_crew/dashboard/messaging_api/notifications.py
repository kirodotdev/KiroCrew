"""The notification feed (list, delete, clear, ack, unack, ack-all), the
per-channel notification settings and the gateway's Do Not Disturb.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
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
    hidden = _withheld_from(request)
    if hidden is not None:
        # Owner-only notes are withheld from anyone but the machine owner, as the
        # live WebSocket and SSE feeds withhold them. Unread is counted over the
        # notes this view returns.
        notes = [note for note in state._notification_log if not hidden(note)]
        if len(notes) != len(state._notification_log):
            _audit_withheld("notification")
        return web.json_response({"notifications": notes, "unread": unread_count(notes)})
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
    refused = _refuse_owner_only_note(request, state, ts, "notifications.delete")
    if refused is not None:
        return refused
    ok = await state.delete_notification(ts)
    return web.json_response({"ok": ok})


def _withheld_from(request: web.Request) -> Callable[[dict], bool] | None:
    """Which notes this caller is withheld, or ``None`` for the machine owner.

    Everyone else, a Shared Crew teammate who passes the owner gate included, is
    withheld owner-only notes, which name the machine owner's own accounts.
    """
    from kiro_crew.dashboard.handlers.source_providers import is_host_owner_dashboard_request
    from kiro_crew.dashboard.ws_event_scope import owner_only_note

    if is_host_owner_dashboard_request(request):
        return None
    return owner_only_note


def _audit_withheld(event_type: str) -> None:
    from kiro_crew.dashboard.ws_event_scope import audit_owner_only_withheld

    audit_owner_only_withheld(event_type)


def _persist_failed() -> web.Response:
    """The answer when a filtered clear or ack-all could not be written."""
    return web.json_response(
        {"error": "Couldn't save the notification change.", "code": "persist_failed"},
        status=500,
    )


def _refuse_owner_only_note(
    request: web.Request, state: DashboardState, ts: str, operation: str
) -> web.Response | None:
    """The refusal for one notification withheld from this caller, else ``None``.

    The list never shows the caller a note withheld from them, so one they name by
    its timestamp is refused rather than changed on the owner's behalf.
    """
    hidden = _withheld_from(request)
    if hidden is None:
        return None
    if not any(note.get("ts") == ts and hidden(note) for note in state._notification_log):
        return None
    _audit_withheld(operation)
    return web.json_response(
        {"error": "Only the owner can change this notification.", "code": "owner_only"},
        status=403,
    )


async def api_notifications_clear(request: web.Request) -> web.Response:
    """POST /api/notifications/clear — clear all notifications."""
    state: DashboardState = request.app["state"]
    withheld = _withheld_from(request)
    if withheld is not None and any(withheld(note) for note in state._notification_log):
        # Clear what the caller sees and keep what they are withheld: no whole-feed
        # clear, and no clear broadcast that would empty the owner's. Each cleared
        # note is withdrawn by name once the file holds the result.
        persisted, _cleared = await state.clear_visible_notifications(withheld)
        if not persisted:
            return _persist_failed()
        _audit_withheld("notifications_clear")
        return web.json_response({"ok": True})
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
    refused = _refuse_owner_only_note(request, state, ts, "notifications.ack")
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
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object
    ts = body.get("ts", "")
    if not ts:
        return web.json_response({"error": "ts is required"}, status=400)
    refused = _refuse_owner_only_note(request, state, ts, "notifications.unack")
    if refused is not None:
        return refused
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
    hidden = _withheld_from(request)
    if hidden is not None:
        # Acknowledge what the caller sees: a note withheld from them keeps its read
        # state, so the ack goes out per entry rather than as "*", once written.
        withheld = any(hidden(note) for note in state._notification_log)
        if not await state.acknowledge_visible_notifications(hidden):
            return _persist_failed()
        if withheld:
            _audit_withheld("notification_ack")
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


async def api_notification_dnd_get(request: web.Request) -> web.Response:
    """GET /api/notifications/dnd — ``{"until": epoch seconds | null}``.

    ``null`` when Do Not Disturb is off, including once its end time has passed.
    """
    state: DashboardState = request.app["state"]
    return web.json_response({"until": state.notification_dnd.until()})


async def api_notification_dnd_put(request: web.Request) -> web.Response:
    """PUT /api/notifications/dnd — start or end the gateway's Do Not Disturb.

    Body: ``{"until": epoch seconds | null}``; ``null`` ends it. An end time that
    is not a number, is not in the future, or is too far ahead is refused with 400.
    """
    from kiro_crew.notifications.dnd import DndError

    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict) or "until" not in body:
        return web.json_response(
            {"error": "body must be an object with until", "code": "invalid_until"}, status=400
        )
    try:
        # set() persists via atomic_write (blocking file I/O): keep it off the loop.
        until = await asyncio.to_thread(state.notification_dnd.set, body["until"])
    except DndError as exc:
        return web.json_response({"error": str(exc), "code": "invalid_until"}, status=400)
    state.broadcast_ws("notification_dnd", {"until": until})
    return web.json_response({"until": until})
