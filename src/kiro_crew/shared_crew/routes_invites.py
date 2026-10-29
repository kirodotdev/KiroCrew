"""Owner routes for in-Crew Tailscale invites (``tailscale_invites``).

Every route but the avatar read is owner-only: a dashboard user (no app token),
not a restricted session, the configured owner (``is_owner_dashboard_request``),
and reaching the Crew as the local operator or as this machine's own tailnet
login. A teammate who accepted an invite therefore cannot invite or revoke
anyone. The API key goes in once and never comes back out: no response carries
it, and the status read reports only whether one is connected.

An upstream Tailscale failure answers 502 with a ``tailscale_*`` code rather
than 401, so the dashboard never reads the key's refusal as its own sign-out.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from typing import Any

from aiohttp import web

from kiro_crew.shared_crew import access_set
from kiro_crew.shared_crew import tailscale_invite_store as store
from kiro_crew.shared_crew import tailscale_invites as invites
from kiro_crew.shared_crew.contracts import Provenance
from kiro_crew.shared_crew.identity import PROVIDER_TAILSCALE, viewer_principal
from kiro_crew.shared_crew.tailscale_api import CODE_RATE_LIMITED, TailscaleApiError

logger = logging.getLogger(__name__)

BASE = "/api/shared-crew/tailscale"
STATUS_PATH = f"{BASE}/invites"
CREDENTIAL_PATH = f"{BASE}/credential"
INVITE_PATH = f"{BASE}/invites/{{invite_id}}"
RESEND_PATH = f"{BASE}/invites/{{invite_id}}/resend"
AVATARS_PATH = f"{BASE}/avatars"
AVATAR_PATH = f"{BASE}/avatars/{{avatar_id}}"
#: Where ``dashboard/ws.py`` keeps a socket's verified actor; a revoke closes
#: the sockets whose actor is the revoked person.
WS_ACTOR_KEY = "_shared_crew_actor"
_MAX_JSON_BYTES = 16 * 1024
_POLLER_KEY: web.AppKey[Any] = web.AppKey("shared_crew_invite_poller", object)
_TAILNET_PROVENANCE = frozenset(
    {
        Provenance.TAILSCALE_SERVE.value,
        Provenance.TAILSCALE_DIRECT.value,
        Provenance.TAILSCALE_LISTENER.value,
    }
)


def _error(status: int, code: str, *, retry_after: float = 0.0) -> web.Response:
    headers = {"Retry-After": str(int(retry_after) or 1)} if retry_after else None
    return web.json_response({"error": code, "code": code}, status=status, headers=headers)


def _dashboard_user(request: web.Request) -> bool:
    return request.get("is_dashboard_user") is True and not request.get("app")


async def _owner_refusal(request: web.Request) -> web.Response | None:
    from kiro_crew.dashboard.handlers._shared import _is_restricted_session
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    if not _dashboard_user(request) or request.get("app") != "":
        return _error(403, "forbidden")
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        return _error(403, "restricted_session")
    if not is_owner_dashboard_request(request):
        return _error(403, "owner_only")
    viewer = viewer_principal(request)
    if viewer.provenance is Provenance.LOCAL_OPERATOR:
        return None
    from kiro_crew.dashboard.tailnet_owner import OWNER_WHOIS_REQUEST_KEY

    if request.get(OWNER_WHOIS_REQUEST_KEY) is True:
        # The direct listener's whois proved this is this machine's own user.
        return None
    if viewer.provider == PROVIDER_TAILSCALE:
        me = await asyncio.to_thread(invites.daemon_self_reader)
        if me is not None and me.login and me.login.strip().lower() == viewer.subject:
            return None
    return _error(403, "owner_only")


def _actor(request: web.Request) -> str:
    return viewer_principal(request).id or "unknown"


def _failure(exc: Exception) -> web.Response:
    if isinstance(exc, invites.InviteError):
        status = 409 if exc.code in (invites.CODE_NOT_CONNECTED, invites.CODE_BACKING_OFF) else 400
        if exc.code == CODE_RATE_LIMITED:
            return _error(429, "tailscale_rate_limited", retry_after=exc.retry_after)
        if exc.code in (invites.CODE_DEVICE_NOT_FOUND, invites.CODE_DAEMON):
            status = 409
        return _error(status, exc.code, retry_after=exc.retry_after)
    if isinstance(exc, TailscaleApiError):
        if exc.code == CODE_RATE_LIMITED:
            return _error(429, "tailscale_rate_limited", retry_after=exc.retry_after or 60)
        return _error(502, f"tailscale_{exc.code}")
    raise exc


async def _json(request: web.Request) -> dict[str, Any] | None:
    if request.content_length is not None and request.content_length > _MAX_JSON_BYTES:
        return None
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- any unreadable body is a bad request
        return None
    return body if isinstance(body, dict) else None


async def api_status(request: web.Request) -> web.Response:
    refused = await _owner_refusal(request)
    if refused is not None:
        return refused
    view = await invites.status_view()
    sync: dict[str, Any] = {}
    due = store.now() - float(view.get("last_sync_at") or 0.0) >= invites.SYNC_MIN_INTERVAL_SECS
    # Due reads sync even with nothing pending here: an invite made in the
    # admin console has no local row until a sync finds it.
    if view["connected"] and due:
        try:
            sync = await invites.sync(actor=_actor(request))
        except (invites.InviteError, TailscaleApiError) as exc:
            sync = {"error": getattr(exc, "code", "error")}
        view = await invites.status_view()
    view["sync"] = sync
    return web.json_response(view)


async def api_connect(request: web.Request) -> web.Response:
    refused = await _owner_refusal(request)
    if refused is not None:
        return refused
    body = await _json(request)
    key = body.get("key") if body else None
    if not isinstance(key, str) or not key.strip():
        return _error(400, invites.CODE_BAD_KEY)
    try:
        view = await invites.connect(key, _actor(request))
    except (invites.InviteError, TailscaleApiError) as exc:
        return _failure(exc)
    return web.json_response(view)


async def api_disconnect(request: web.Request) -> web.Response:
    refused = await _owner_refusal(request)
    if refused is not None:
        return refused
    await invites.disconnect(_actor(request))
    return web.json_response(await invites.status_view())


async def api_create(request: web.Request) -> web.Response:
    refused = await _owner_refusal(request)
    if refused is not None:
        return refused
    body = await _json(request)
    if body is None:
        return _error(400, "bad_request")
    try:
        # The first share needs the owner to have seen what teammates will see.
        if await asyncio.to_thread(store.credential_present):
            await asyncio.to_thread(access_set.require_first_share_ack)
    except access_set.AccessError as exc:
        return _error(409, exc.code)
    try:
        emails = invites.clean_emails(body.get("emails", []))
        made = await invites.create_invites(
            emails,
            multi_use=body.get("multi_use") is True,
            alias=str(body.get("alias") or ""),
            avatar_id=str(body.get("avatar_id") or ""),
            actor=_actor(request),
        )
    except (invites.InviteError, TailscaleApiError) as exc:
        return _failure(exc)
    return web.json_response({"invites": made})


async def api_resend(request: web.Request) -> web.Response:
    refused = await _owner_refusal(request)
    if refused is not None:
        return refused
    try:
        await invites.resend(request.match_info["invite_id"], _actor(request))
    except (invites.InviteError, TailscaleApiError) as exc:
        return _failure(exc)
    return web.json_response({"resent": True})


async def api_revoke(request: web.Request) -> web.Response:
    refused = await _owner_refusal(request)
    if refused is not None:
        return refused
    try:
        result = await invites.revoke(request.match_info["invite_id"], _actor(request))
    except (invites.InviteError, TailscaleApiError) as exc:
        return _failure(exc)
    except access_set.AccessError as exc:
        # The share is gone at Tailscale, but the seed could not be cleared.
        return _error(503, exc.code)
    return web.json_response(result)


async def api_upload_avatar(request: web.Request) -> web.Response:
    refused = await _owner_refusal(request)
    if refused is not None:
        return refused
    # JSON ``{"data_base64": ...}``, so the browser uses the same JSON transport
    # (session header, auth recovery) as every other dashboard write.
    cap = (store.MAX_AVATAR_BYTES * 4) // 3 + 1024
    if request.content_length is not None and request.content_length > cap:
        return _error(413, "avatar_too_large")
    # One ``read(n)`` returns as soon as any bytes are buffered, so a body that
    # arrives in pieces is read in a loop until EOF or past the cap.
    chunks: list[bytes] = []
    total = 0
    while total <= cap:
        chunk = await request.content.read(cap + 1 - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    raw = b"".join(chunks)
    if len(raw) > cap:
        return _error(413, "avatar_too_large")
    try:
        encoded = json.loads(raw).get("data_base64")
        data = base64.b64decode(encoded, validate=True) if isinstance(encoded, str) else b""
    except (ValueError, AttributeError, binascii.Error):
        return _error(400, "bad_request")
    try:
        avatar_id = await asyncio.to_thread(store.save_avatar, data)
    except ValueError as exc:
        return _error(413 if str(exc) == "avatar_too_large" else 400, str(exc))
    return web.json_response({"avatar_id": avatar_id})


async def api_avatar(request: web.Request) -> web.Response:
    """An avatar image, to any dashboard user: People rows show it to everyone."""
    if not _dashboard_user(request):
        return _error(403, "forbidden")
    found = await asyncio.to_thread(store.read_avatar, request.match_info["avatar_id"])
    if found is None:
        return _error(404, "not_found")
    data, ctype = found
    return web.Response(
        body=data,
        content_type=ctype,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, max-age=86400, immutable",
        },
    )


def decorate_people(body: dict[str, Any]) -> dict[str, Any]:
    """Attach the alias and avatar bound to each tailnet row's verified login.

    Only a row whose provenance is a daemon-verified tailnet peer is matched, so a
    session creator read back from stored attribution never borrows an alias.
    """
    members = invites.members_by_login()
    for row in body.get("people") or []:
        if row.get("provenance") not in _TAILNET_PROVENANCE:
            continue
        principal = row.get("principal") or {}
        login = str((principal.get("display") or {}).get("login") or "").strip().lower()
        member = members.get(login)
        if member:
            row["member"] = {
                "alias": member["alias"],
                "avatar_id": member["avatar_id"],
                "profile_pic_url": member["profile_pic_url"],
            }
    return body


def _closer_for(app: web.Application) -> Any:
    """Close a revoked login's sockets: by login, and by each user id it came in under."""

    async def close(login: str, user_ids: frozenset[str] = frozenset()) -> int:
        from kiro_crew.shared_crew.access_set import principal_ids_for
        from kiro_crew.shared_crew.routes_access import close_sockets

        return await close_sockets(app, principal_ids_for(login, user_ids), login)

    return close


async def _start_poller(app: web.Application) -> None:
    invites.connection_closer = _closer_for(app)
    app[_POLLER_KEY] = asyncio.create_task(invites.poll_forever())


async def _stop_poller(app: web.Application) -> None:
    task = app.get(_POLLER_KEY)
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


def install_poller(app: web.Application) -> None:
    app.on_startup.append(_start_poller)
    app.on_cleanup.append(_stop_poller)


def register(app: web.Application) -> None:
    """Register the invite routes and the accept poller on *app*."""
    app.router.add_get(STATUS_PATH, api_status)
    app.router.add_post(STATUS_PATH, api_create)
    app.router.add_delete(INVITE_PATH, api_revoke)
    app.router.add_post(RESEND_PATH, api_resend)
    app.router.add_post(CREDENTIAL_PATH, api_connect)
    app.router.add_delete(CREDENTIAL_PATH, api_disconnect)
    app.router.add_post(AVATARS_PATH, api_upload_avatar)
    app.router.add_get(AVATAR_PATH, api_avatar)
    install_poller(app)
