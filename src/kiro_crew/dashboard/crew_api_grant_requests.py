"""The owner's side of local authorization: list the waiting grant requests, answer one.

Handlers for ``GET /v1/crew/owner/grant-requests`` and
``POST /v1/crew/owner/grant-requests/{request_id}/decision``. They are served only through
:func:`kiro_crew.dashboard.crew_api_owner.owner_route`, which refuses every caller that
is not the owner, an app token included, before a handler runs. That gate is what keeps
an app from answering its own request: the handlers here check no credential themselves.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from kiro_crew.dashboard import local_authorization as la


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
    }


def _error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


_UNAVAILABLE = (503, "instance_unavailable", "local authorization is unavailable")


async def list_grant_requests(request: web.Request) -> web.Response:
    """The requests waiting for the owner's consent, oldest first."""
    try:
        waiting = await asyncio.to_thread(la.gateway_local_authorization().pending)
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


async def decide_grant_request(request: web.Request) -> web.Response:
    """Approve or deny one request. An approval may narrow the scopes, never widen them."""
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
    store = la.gateway_local_authorization()
    try:
        view = await asyncio.to_thread(
            store.decide, request.match_info["request_id"], approve, scopes if approve else None
        )
    except KeyError:
        return _error(404, "not_found", "no such grant request, or it has expired")
    except RuntimeError:
        return _error(409, "grant_request_already_resolved", "this request was already answered")
    except ValueError:
        return _error(400, "invalid_scope", "scopes must be a subset of the requested scopes")
    except (OSError, TypeError):
        return _error(*_UNAVAILABLE)
    return web.json_response(_grant_request(view))
