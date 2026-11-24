"""The owner's side of local authorization: list the waiting grant requests, read one,
answer one, list the grants and revoke one.

Handlers for ``GET /v1/crew/owner/grant-requests``,
``GET /v1/crew/owner/grant-requests/{request_id}``,
``POST /v1/crew/owner/grant-requests/{request_id}/decision``,
``GET /v1/crew/owner/grants`` and ``DELETE /v1/crew/owner/grants/{grant_id}``. They are served only through
:func:`kiro_crew.dashboard.crew_api_owner.owner_route`, which supplies the Crew API
error shape and the ordinary owner gate. Every handler additionally requires the host
owner: a Shared Crew teammate may use the dashboard but cannot grant or revoke a local
credential, or read the machine's application requests.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.dashboard import local_authorization as la
from kiro_crew.dashboard.handlers.source_providers import is_host_owner_dashboard_request

logger = logging.getLogger(__name__)


def _grant_request(view: dict[str, Any]) -> dict[str, Any]:
    """A stored request in the contract's ``GrantRequest`` shape."""
    return {
        "id": view["request_id"],
        "object": "grant_request",
        "namespace": view["namespace"],
        "status": view["status"],
        "client_id": view["client_id"],
        "client_name": view["client_name"],
        # A program on this machine reported the name and id; nothing verified them.
        "verified": False,
        "redirect_uri": view["redirect_uri"],
        "user_code": view["user_code"],
        "requested_scopes": view["requested_scopes"],
        "created_at": view["created_at"],
        "expires_at": view["expires_at"],
        "decided_at": view["decided_at"],
        "decided_by": view["decided_by"],
        "grant_id": view["grant_id"],
        # Present only on an approved request: what the owner gave, which may be fewer.
        **({"granted_scopes": view["granted_scopes"]} if "granted_scopes" in view else {}),
    }


def _error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


#: The largest decision body read: a decision and at most every scope there is.
_BODY_MAX = 4 * 1024

_UNAVAILABLE = (503, "instance_unavailable", "local authorization is unavailable")

#: The contract's ``GrantStatus`` values and its ``Limit`` parameter.
_GRANT_STATUSES = ("active", "throttled", "paused", "revoked")
_LIMIT_DEFAULT = 20
_LIMIT_MAX = 100


async def _host_owner_refusal(request: web.Request, operation: str) -> web.Response | None:
    if is_host_owner_dashboard_request(request):
        return None
    caller = str(request.get("user") or "unknown")

    def audit() -> None:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources="non_owner_block",
        )

    try:
        await asyncio.to_thread(audit)
    except Exception:
        logger.warning("Local consent host-owner denial audit failed")
    return _error(403, "owner_only", "host owner authorization required")


async def list_grant_requests(request: web.Request) -> web.Response:
    """The requests waiting for the host owner's consent, oldest first."""
    if (denied := await _host_owner_refusal(request, "crew_api.grant_requests.list")) is not None:
        return denied
    try:
        waiting = await asyncio.to_thread(lambda: la.gateway_local_authorization().pending())
    except (OSError, ValueError, KeyError, TypeError):
        return _error(*_UNAVAILABLE)
    data = sorted((_grant_request(v) for v in waiting), key=lambda r: r["created_at"])
    return web.json_response(
        {
            "object": "list",
            "data": data,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None,
            "has_more": False,
        }
    )


async def get_grant_request(request: web.Request) -> web.Response:
    """One request, answered or not, for as long as the record keeps it."""
    if (denied := await _host_owner_refusal(request, "crew_api.grant_requests.get")) is not None:
        return denied
    request_id = request.match_info["request_id"]
    try:
        view = await asyncio.to_thread(lambda: la.gateway_local_authorization().request(request_id))
    except la.UnknownRequest:
        return _error(404, "not_found", "no such grant request, or it has expired")
    except (OSError, ValueError, KeyError, TypeError):
        return _error(*_UNAVAILABLE)
    return web.json_response(_grant_request(view))


def _grant(view: dict[str, Any]) -> dict[str, Any]:
    """A stored grant in the contract's ``Grant`` shape, with what this store keeps."""
    return {
        "id": view["grant_id"],
        "object": "grant",
        # Made through local authorization, by this gateway.
        "kind": "local",
        "namespace": view["namespace"],
        "client_id": view["client_id"],
        # As the app reported them; nothing verified the name or the id.
        "app": {"name": view["client_name"], "icon_url": None},
        "scopes": view["scopes"],
        "status": view["status"],
        "created_at": view["created_at"],
        "expires_at": view["expires_at"],
    }


async def list_grants(request: web.Request) -> web.Response:
    """The grants that work now, oldest first. A revoked grant is not listed."""
    if (denied := await _host_owner_refusal(request, "crew_api.grants.list")) is not None:
        return denied
    query = request.query
    wanted = query.get("status", "active")
    raw_limit = query.get("limit", str(_LIMIT_DEFAULT))
    if wanted not in _GRANT_STATUSES:
        return _error(400, "invalid_request", "status must be active, throttled, paused or revoked")
    if len(raw_limit) > len(str(_LIMIT_MAX)) or not (
        raw_limit.isascii() and raw_limit.isdigit() and 1 <= int(raw_limit) <= _LIMIT_MAX
    ):
        return _error(400, "invalid_request", f"limit must be 1 to {_LIMIT_MAX}")
    try:
        views = await asyncio.to_thread(lambda: la.gateway_local_authorization().grants())
    except (OSError, ValueError, KeyError, TypeError):
        return _error(*_UNAVAILABLE)
    # A local grant is active or gone: the other three statuses match nothing.
    rows = [_grant(v) for v in views] if wanted == "active" else []
    if "after" in query:
        ids = [r["id"] for r in rows]
        if query["after"] not in ids:
            return _error(400, "invalid_request", "after names no grant in this list")
        rows = rows[ids.index(query["after"]) + 1 :]
    limit = int(raw_limit)
    data = rows[:limit]
    return web.json_response(
        {
            "object": "list",
            "data": data,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None,
            "has_more": len(rows) > limit,
        }
    )


async def revoke_grant(request: web.Request) -> web.Response:
    """Revoke one grant and every token under it. A second call answers the same."""
    if (denied := await _host_owner_refusal(request, "crew_api.grants.revoke")) is not None:
        return denied
    grant_id = request.match_info["grant_id"]
    # Token auth set this subject; _host_owner_refusal proved it is the host owner's.
    subject = str(request["user"])
    try:
        revoked = await asyncio.to_thread(
            lambda: la.gateway_local_authorization().revoke_grant(grant_id, caller=subject)
        )
    except Exception:
        # The record cannot be read or written, or the audit row failed.
        return _error(*_UNAVAILABLE)
    if not revoked:
        return _error(404, "not_found", "no such grant, or it has expired")
    return web.json_response({"id": grant_id, "object": "grant", "deleted": True})


async def decide_grant_request(request: web.Request) -> web.Response:
    """Approve or deny one request. An approval may narrow the scopes, never widen them."""
    if (denied := await _host_owner_refusal(request, "crew_api.grant_requests.decide")) is not None:
        return denied
    if request.content_length is None or request.content_length > _BODY_MAX:
        return _error(400, "invalid_request", "the body is missing its length or too large")
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or body.get("decision") not in ("approve", "deny"):
        return _error(400, "invalid_request", "decision must be approve or deny")
    scopes = body.get("scopes")
    if scopes is not None and not (
        isinstance(scopes, list) and all(isinstance(s, str) for s in scopes)
    ):
        return _error(400, "invalid_request", "scopes must be a list of strings")
    approve = body["decision"] == "approve"
    request_id = request.match_info["request_id"]
    # Token auth set this subject; _host_owner_refusal proved it is the host owner's.
    subject = str(request["user"])
    try:
        # The store is built in the worker thread too: its path lookup can touch disk.
        view = await asyncio.to_thread(
            lambda: la.gateway_local_authorization().decide(
                request_id, approve, scopes if approve else None, caller=subject
            )
        )
    except la.UnknownRequest:
        return _error(404, "not_found", "no such grant request, or it has expired")
    except la.RequestAlreadyAnswered:
        return _error(409, "grant_request_already_resolved", "this request was already answered")
    except la.ScopesNotRequested:
        return _error(400, "invalid_scope", "scopes must be a subset of the requested scopes")
    except Exception:
        # The record cannot be read or written, or the audit row failed.
        return _error(*_UNAVAILABLE)
    return web.json_response(_grant_request(view))
