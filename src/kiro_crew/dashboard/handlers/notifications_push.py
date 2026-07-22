"""App notification producer endpoint (RFC local notification bus, Phase 2).

``POST /api/notifications/push`` lets an authenticated app backend push a
schema-v2 notification through the bus. Security posture (RFC "Security
considerations"):

- The producer identity comes from the verified app token (``request["app"]``,
  set by the token-auth middleware after HMAC validation) — never from the
  request body, so an app cannot impersonate another source.
- The channel must be declared in the app's manifest (``notifications.channels``)
  and the app must currently be enabled; deny-by-default otherwise.
- Pushes are rate limited per app (token bucket); violations are SEL-logged
  and rejected with 429.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

from kiro_crew.apps.manager import get_app_manifest, is_app_enabled
from kiro_crew.notifications.bus import (
    NotificationPayload,
    NotificationValidationError,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 64 * 1024  # generous; payload fields have their own caps


def _resolve_app_channels(app_name: str) -> dict[str, str] | None:
    """Return {channel_id: default_priority} for an installed, enabled app.

    None when the app is unknown or disabled — callers treat that as deny.
    Read-only by design: runs in a worker thread (asyncio.to_thread), so it
    must never write app metadata (``get_app`` has a version-sync write side
    effect that would race loop-side writers of ``installed.json``).
    """
    if not is_app_enabled(app_name):
        return None
    manifest = get_app_manifest(app_name)
    if manifest is None:
        return None
    return {ch.id: ch.defaultPriority for ch in manifest.notifications.channels}


async def api_push_notification(request: web.Request) -> web.Response:
    """POST /api/notifications/push — app backends push notifications via the bus."""
    app_name = request.get("app", "")
    if not app_name:
        # Dashboard-user tokens have no app identity; this endpoint is for
        # app producers only (deny-by-default).
        sel().log_api_access(
            caller="unknown",
            operation="notification_push",
            outcome="denied",
            source="notifications_api",
            error="app token required",
        )
        return web.json_response(
            {"error": "app token required"}, status=403
        )

    if request.content_length and request.content_length > _MAX_BODY_BYTES:
        return web.json_response({"error": "payload too large"}, status=413)

    # Enforce the cap while streaming: chunked transfer-encoding carries no
    # Content-Length header, and buffering the whole body first
    # (request.read()) would allocate up to the server-wide client_max_size
    # before any check runs. Reading incrementally bounds the allocation to
    # _MAX_BODY_BYTES + one chunk.
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.content.iter_chunked(8192):
        received += len(chunk)
        if received > _MAX_BODY_BYTES:
            return web.json_response({"error": "payload too large"}, status=413)
        chunks.append(chunk)
    try:
        body: dict[str, Any] = json.loads(b"".join(chunks))
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)

    # get_app/get_app_manifest read installed.json + app.json from disk (and
    # may even write a version sync) -- keep that off the event loop. The
    # await window this opens between the enablement check and bus.push()
    # means an app disabled mid-request can still land one final
    # notification; that is accepted (harmless) rather than locked against.
    channels = await asyncio.to_thread(_resolve_app_channels, app_name)
    if channels is None:
        sel().log_api_access(
            caller=app_name,
            operation="notification_push",
            outcome="denied",
            source="notifications_api",
            error="app not installed or not enabled",
        )
        return web.json_response({"error": "app not installed or not enabled"}, status=403)

    channel_id = str(body.get("channel", ""))
    if channel_id not in channels:
        sel().log_api_access(
            caller=app_name,
            operation="notification_push",
            outcome="denied",
            source="notifications_api",
            error=f"undeclared channel: {channel_id!r}",
        )
        return web.json_response(
            {"error": f"channel not declared in app manifest: {channel_id!r}"}, status=400
        )

    state = request.app["state"]
    bus = state.notification_bus
    full_channel = f"{app_name}.{channel_id}"

    priority_raw = body.get("priority")
    # ``is not None`` (not truthiness): falsy-but-valid values like
    # ``group_key: 0`` must survive, and an explicit ``url: ""`` should fail
    # validation loudly (400) rather than be silently dropped. Single lookup
    # per key -- the checked value is the consumed value.
    group_key_raw = body.get("group_key")
    url_raw = body.get("url")
    icon_raw = body.get("icon")
    actions_raw = body.get("actions")
    ttl_raw = body.get("ttl")
    meta_raw = body.get("meta")
    meta: dict[str, Any] = meta_raw if isinstance(meta_raw, dict) else {}
    payload = NotificationPayload(
        source=f"app:{app_name}",  # server-resolved; body cannot override
        channel=full_channel,
        title=str(body.get("title", "")),
        body=str(body.get("body", "")),
        priority=str(priority_raw) if priority_raw is not None else None,
        group_key=str(group_key_raw) if group_key_raw is not None else None,
        actions=actions_raw if isinstance(actions_raw, list) else None,
        url=str(url_raw) if url_raw is not None else None,
        icon=str(icon_raw) if icon_raw is not None else None,
        ttl=ttl_raw if isinstance(ttl_raw, int) else None,
        meta=meta,
    )
    try:
        # Validate BEFORE consuming a rate-limit token: the limiter's purpose
        # is capping delivered notifications (RFC "Rate limiting"), and
        # invalid payloads deliver nothing -- they must not drain the budget
        # and then 429-block legitimate retries.
        payload.validate()
        # Register once per channel, also before the limiter: registration is
        # cheap and idempotent, and its only failure mode (corrupt on-disk
        # manifest defaultPriority) is a 400 that likewise delivers nothing.
        # Re-registering on every push would stomp any later runtime priority
        # override (RFC Phase 3 per-channel settings); manifest priority
        # changes take effect on gateway restart.
        if not bus.is_registered(full_channel):
            bus.register_channel(full_channel, channels[channel_id])
    except NotificationValidationError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    if not state.notification_rate_limiter.allow(app_name):
        sel().log_api_access(
            caller=app_name,
            operation="notification_push",
            outcome="denied",
            source="notifications_api",
            error="rate limit exceeded",
        )
        return web.json_response({"error": "rate limit exceeded"}, status=429)

    try:
        # bus.push delivers via the state sink: redact, in-memory append,
        # SSE broadcast, and a persist job queued on the notification I/O
        # executor (the sink stashes its future on the state).
        note = bus.push(payload)
    except NotificationValidationError as exc:
        # Unreachable today (payload validated and channel registered above
        # with no await between); kept as a safety net so a future refactor
        # cannot turn this into an unhandled 500. Refund the token: nothing
        # was delivered, and the budget caps delivered notifications only.
        state.notification_rate_limiter.refund(app_name)
        return web.json_response({"error": str(exc)}, status=400)
    except Exception:
        # Sink failure during delivery. Delivery may have PARTIALLY happened
        # (broadcast precedes persist), so the token is deliberately NOT
        # refunded -- and throttling a producer while the gateway is failing
        # is protective, not punitive.
        logger.exception("notification push delivery failed for %s", full_channel)
        sel().log_api_access(
            caller=app_name,
            operation="notification_push",
            outcome="error",
            source="notifications_api",
            error="delivery failed",
        )
        return web.json_response({"error": "notification delivery failed"}, status=500)

    # Await durability before acknowledging: an accepted app push must not
    # be silently lost to a disk failure (legacy system producers remain
    # best-effort fire-and-forget). No await ran between bus.push and here,
    # so the stashed future belongs to this note. The note WAS broadcast, so
    # the token stays spent on failure (same rationale as the sink except).
    persist_fut = state.last_notification_persist
    if persist_fut is not None and not await persist_fut:
        logger.error("notification persistence failed for %s", full_channel)
        sel().log_api_access(
            caller=app_name,
            operation="notification_push",
            outcome="error",
            source="notifications_api",
            error="persistence failed",
        )
        return web.json_response({"error": "notification persistence failed"}, status=500)

    sel().log_api_access(
        caller=app_name,
        operation="notification_push",
        outcome="granted",
        source="notifications_api",
        resources=full_channel,
    )
    return web.json_response({"ok": True, "note": note})
