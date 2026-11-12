"""Crew API Owner tier: ``/v1/crew/owner/*`` routes over the dashboard's own handlers.

Each Owner route serves the SAME handler as its ``/api/*`` twin, so a success body is
the twin's body byte for byte. What :func:`owner_route` adds is the Owner tier's
contract:

* Owner only. Every caller, an app token included, must pass
  :func:`require_owner_dashboard_request`, the gate the ``/api/*`` handlers use, which
  refuses an app token ``403 owner_only`` and writes the denial to the SEL trail. An
  Owner route is therefore never wider than its twin.
* A request id. The caller's ``X-Request-Id`` is kept when it is ``req_``-shaped;
  otherwise the gateway makes one. It is returned in the ``X-Request-Id`` header and in
  every error body.
* The ``Crew-API-Version`` response header.
* The contract error body ``{"error": {"type", "code", "message", "param",
  "request_id"}}`` in place of the dashboard's flat ``{"error": "...", "code": "..."}``.

The error body, request id and version header come from :mod:`crew_api_errors`, which
token auth also uses for a ``/v1/crew/`` refusal. A refusal another middleware answers (the
Bot prune gate) never reaches the route, so it keeps the dashboard's own shape.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Awaitable, Callable

from aiohttp import web

from kiro_crew.dashboard.crew_api_errors import (
    CREW_API_VERSION,
    OWNER_PATH_PREFIX,
    REQUEST_ID_HEADER,
    VERSION_HEADER,
    contract_error,
)
from kiro_crew.dashboard.crew_api_errors import default_code as _default_code
from kiro_crew.dashboard.crew_api_errors import (
    request_id_for,
)
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

__all__ = ["OWNER_PATH_PREFIX", "contract_error", "owner_route", "request_id_for"]

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

# Headers of the handler's error response that the contract body replaces.
_BODY_HEADERS = frozenset({"content-type", "content-length", "content-encoding"})


def _as_contract_error(response: web.StreamResponse, request_id: str) -> web.Response:
    """Rewrite a handler's flat error response into the contract shape."""
    raw = getattr(response, "body", None)
    parsed: object = None
    if isinstance(raw, (bytes, bytearray)):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
    fields = parsed if isinstance(parsed, dict) else {}
    code = fields.get("code")
    message = fields.get("error")
    code = code if isinstance(code, str) else ""
    message = message if isinstance(message, str) else ""
    status = response.status
    kept = {k: v for k, v in response.headers.items() if k.lower() not in _BODY_HEADERS}
    return contract_error(
        status,
        code or _default_code(status),
        message or (response.reason or ""),
        request_id,
        headers=kept,
    )


def owner_route(handler: Handler, operation: str) -> Handler:
    """Serve *handler* as an Owner route (see the module docstring)."""

    @functools.wraps(handler)
    async def _owner(request: web.Request) -> web.StreamResponse:
        request_id = request_id_for(request)
        response: web.StreamResponse
        denied = await require_owner_dashboard_request(request, operation)
        if denied is not None:
            response = _as_contract_error(denied, request_id)
        else:
            try:
                response = await handler(request)
            except web.HTTPException as exc:
                response = contract_error(
                    exc.status, _default_code(exc.status), exc.reason, request_id
                )
            if response.status >= 400:
                response = _as_contract_error(response, request_id)
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[VERSION_HEADER] = CREW_API_VERSION
        return response

    return _owner
