"""``GET /v1/crew/grant``: an app reads the grant its own credential was issued for.

The grant is named by nothing in the request. It is the ``grant_id`` of the credential
token auth verified (:data:`crew_api_scopes.CREDENTIAL_KEY`), so no path, query, header
or body value chooses which grant is read, and one app cannot read another's.

A first-party session has no grant of its own and is answered 404 ``not_found``: the
machine owner, and a person the owner let into this Kiro Crew alike. The owner lists
every grant with ``GET /v1/crew/owner/grants``.

The answer holds the fields of the contract's ``GrantSelf`` and nothing else: no token,
no secret, and none of the fields the owner's view adds. Local authorization knows no
pause, throttle, entitlement or quota, so ``status`` is ``active``, ``pause`` is null,
``entitlements`` is empty and ``quota`` is left out. A grant the owner revoked is never
read here: its token does not verify, and token auth answers that request.

A caller ``require_scope`` refuses gets its audit row here. An app credential's own
decision row is token auth's.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

from aiohttp import web

from kiro_crew.dashboard import local_authorization
from kiro_crew.dashboard.crew_api_auth import app_owner_only_routes
from kiro_crew.dashboard.crew_api_errors import (
    CREW_API_VERSION,
    REQUEST_ID_HEADER,
    VERSION_HEADER,
    contract_error,
    request_id_for,
)
from kiro_crew.dashboard.crew_api_scopes import CREDENTIAL_KEY, require_scope
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

ROUTE_KEY: Final = ("GET", "/v1/crew/grant")
#: The route's action name in ``route_access`` and on its audit rows.
OPERATION: Final = "crew_api.grant.read"


def grant_self(view: dict[str, Any]) -> dict[str, Any]:
    """A stored grant in the contract's ``GrantSelf`` shape."""
    return {
        "id": view["grant_id"],
        "object": "grant",
        "client_id": view["client_id"],
        # Made through local authorization, by this gateway.
        "kind": "local",
        "scopes": view["scopes"],
        "entitlements": [],
        "bots": view["bots"],
        "status": view["status"],
        "pause": None,
        "created_at": view["created_at"],
    }


def _audit_refusal(request: web.Request) -> None:
    """Write the audit row of a caller ``require_scope`` refused.

    An app credential's decision is audited by token auth; this row is for a caller
    that reached the handler another way (an installed app's token whose manifest
    names the path). The audit log is warmed at startup, so this only enqueues.
    """
    try:
        sel().log_api_access(
            caller=str(request.get("user") or "unknown"),
            operation=OPERATION,
            outcome="denied",
            source="crew-api",
            resources=ROUTE_KEY[1],
            error="owner_only",
        )
    except Exception:  # the audit row must never change the outcome
        logger.warning("No audit row written for a refused %s", OPERATION)


async def _answer(request: web.Request, request_id: str) -> web.Response:
    # First, with this route's own key: an app credential is judged by its row, a
    # first-party session passes, and any other caller is refused.
    if (denied := require_scope(request, ROUTE_KEY, app_owner_only_routes())) is not None:
        _audit_refusal(request)
        return denied
    credential = request.get(CREDENTIAL_KEY)
    if credential is None:
        return contract_error(404, "not_found", "this credential has no grant", request_id)
    grant_id = credential.grant_id
    try:
        view = await asyncio.to_thread(
            lambda: local_authorization.gateway_local_authorization().grant(grant_id)
        )
    except Exception:
        # The record's text is not logged: only that the read failed.
        logger.warning("Crew API grant read failed for %s", grant_id)
        return contract_error(
            503, "instance_unavailable", "the grant record is unavailable", request_id
        )
    if view is None:
        # Revoked or run out between token auth's check and this read.
        return contract_error(404, "not_found", "this credential has no grant", request_id)
    return web.json_response(grant_self(view))


async def api_grant_self(request: web.Request) -> web.Response:
    """The caller's own grant; 404 for a caller that has none."""
    request_id = request_id_for(request)
    response = await _answer(request, request_id)
    response.headers[REQUEST_ID_HEADER] = request_id
    response.headers[VERSION_HEADER] = CREW_API_VERSION
    return response
