"""Owner routes for sharing this Crew over AWS Systems Manager (``shared_crew.ssm``).

Every route is owner-only, with the gate the Tailscale invite routes use: a
person who came in through an SSM invite is a collaborator and can neither
invite nor revoke anyone. The invite credential is returned once, by the create
route, and is never stored or readable again.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from kiro_crew.shared_crew import access_set, ssm, ssm_invites
from kiro_crew.shared_crew.identity import PROVIDER_SSM
from kiro_crew.shared_crew.preview import preview_only
from kiro_crew.shared_crew.routes_invites import WS_ACTOR_KEY, _actor, _error, _json, _owner_refusal

logger = logging.getLogger(__name__)

STATUS_PATH = "/api/shared-crew/ssm"
INVITES_PATH = "/api/shared-crew/ssm/invites"
INVITE_PATH = "/api/shared-crew/ssm/invites/{invite_id}"

CODE_NOT_ACTIVE = "ssm_not_active"
CODE_LIMIT = "ssm_invite_limit"


def _audit(operation: str, outcome: str, actor: str, invite_id: str = "") -> None:
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=actor or "crew",
            operation=f"shared_crew.ssm_invite.{operation}",
            outcome=outcome,
            source="shared-crew-ssm",
            resources=f"invite:{invite_id}" if invite_id else "",
        )
    except Exception:  # noqa: BLE001 -- the audit never breaks the action
        logger.debug("ssm invite audit write failed", exc_info=True)


def _share(answer: ssm.Listener) -> dict[str, Any]:
    """What a teammate needs, from the running listener. Empty when it is not active."""
    if not answer.active or answer.target is None:
        return {"command": "", "local_url": ""}
    return {
        "command": ssm.start_session_command(answer.target, answer.port),
        "local_url": ssm.local_url(answer.port),
    }


def _status_body() -> dict[str, Any]:
    answer = ssm.listener()
    target = answer.target
    return {
        "state": answer.state,
        "reason": answer.reason,
        "port": answer.port,
        "target": (
            {"instance_id": target.instance_id, "region": target.region}
            if target is not None
            else None
        ),
        **_share(answer),
        "invites": ssm_invites.public_rows(),
    }


def governed(
    handler: Callable[[web.Request], Awaitable[Any]],
) -> Callable[[web.Request], Awaitable[Any]]:
    """404 while an enterprise origin policy keeps the SSM listener off, as if the route did not exist."""

    @functools.wraps(handler)
    async def gated(request: web.Request) -> Any:
        withdrawn = getattr(request.app.get("ssm_gate_ref"), "withdrawn", False)
        if withdrawn or ssm.listener().reason == ssm.REASON_GOVERNED:
            raise web.HTTPNotFound()
        return await handler(request)

    return gated


@preview_only
@governed
async def api_status(request: web.Request) -> web.Response:
    refused = await _owner_refusal(request)
    if refused is not None:
        return refused
    return web.json_response(await asyncio.to_thread(_status_body))


def _created_body(answer: ssm.Listener, invite_id: str, credential: str) -> dict[str, Any]:
    row = next((r for r in ssm_invites.public_rows() if r["id"] == invite_id), {})
    return {
        "invite": row,
        "url": ssm.invite_url(answer.port, credential),
        **_share(answer),
    }


@preview_only
@governed
async def api_create(request: web.Request) -> web.Response:
    refused = await _owner_refusal(request)
    if refused is not None:
        return refused
    answer = ssm.listener()
    if not answer.active or answer.target is None:
        return _error(409, CODE_NOT_ACTIVE)
    body = await _json(request)
    if body is None:
        return _error(400, "bad_request")
    try:
        # The first share needs the owner to have seen what teammates will see.
        await asyncio.to_thread(access_set.require_first_share_ack)
    except access_set.AccessError as exc:
        return _error(409, exc.code)
    actor = _actor(request)
    try:
        invite_id, credential = await asyncio.to_thread(ssm_invites.create, body.get("alias"))
    except ssm_invites.InviteLimitError:
        await asyncio.to_thread(_audit, "create", "denied", actor)
        return _error(409, CODE_LIMIT)
    await asyncio.to_thread(_audit, "create", "ok", actor, invite_id)
    # The command resolves the local AWS CLI (a PATH walk and a trust check), and the rows
    # read the store: neither belongs on the event loop.
    body = await asyncio.to_thread(_created_body, answer, invite_id, credential)
    return web.json_response(body)


def _revoked_socket(ws: Any, invite_id: str) -> bool:
    principal = getattr(ws.get(WS_ACTOR_KEY), "principal", None)
    return (
        principal is not None
        and principal.provider == PROVIDER_SSM
        and principal.subject == invite_id
    )


async def close_revoked_sockets(app: web.Application, invite_id: str) -> int:
    """Close the open dashboard sockets of the person a revoke removed. Never raises."""
    from aiohttp import WSCloseCode

    closed = 0
    state = app.get("state")
    for ws in list(getattr(state, "_ws_clients", None) or ()):
        try:
            if not ws.closed and _revoked_socket(ws, invite_id):
                await ws.close(code=WSCloseCode.POLICY_VIOLATION, message=b"access revoked")
                closed += 1
        except Exception:  # noqa: BLE001 -- one socket never blocks the rest
            logger.debug("closing a revoked person's socket failed", exc_info=True)
    return closed


@preview_only
@governed
async def api_revoke(request: web.Request) -> web.Response:
    refused = await _owner_refusal(request)
    if refused is not None:
        return refused
    invite_id = request.match_info["invite_id"]
    removed = await asyncio.to_thread(ssm_invites.revoke, invite_id)
    if not removed:
        return _error(404, "not_found")
    await asyncio.to_thread(_audit, "revoke", "ok", _actor(request), invite_id)
    closed = await close_revoked_sockets(request.app, invite_id)
    return web.json_response({"revoked": True, "closed_connections": closed})


def register(app: web.Application) -> None:
    """Register the SSM routes on *app*."""
    app.router.add_get(STATUS_PATH, api_status)
    app.router.add_post(INVITES_PATH, api_create)
    app.router.add_delete(INVITE_PATH, api_revoke)
