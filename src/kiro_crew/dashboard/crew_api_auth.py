"""How a Crew API request (``/v1/``) carries its credential, and who verifies it.

A Crew API request names its caller in ``Authorization: Bearer <credential>``. A
``?token=`` on these paths is refused: a URL is kept in access logs, shell history and
proxies, and a credential there would be kept with it. The dashboard's own page calls
``/v1/`` routes with its session cookie, and keeps doing so when it sends no
``Authorization`` header.

:func:`verify_bearer` is the one place a bearer credential is judged. Token auth calls
it for every ``/v1/`` request that sends the header, then runs the same pin check,
app-scope check and audit as for a cookie. A new credential kind is added here.

:data:`OWNER_ONLY_PUBLIC_ROUTES` names the Public routes (``/v1/models``) that serve
the owner alone, through :func:`kiro_crew.dashboard.crew_api_owner.owner_route`.

The frozen ``/v1/chat/completions`` is outside this module: it keeps the credentials
it has always read.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard.crew_api_errors import is_crew_api_path
from kiro_crew.dashboard.token_auth_tokens import validate_token_with_app_off_loop

_SCHEME = "bearer"

#: The refusal a ``?token=`` gets on a Crew API path.
QUERY_TOKEN_REFUSED = "Send the credential in the Authorization header, not in ?token="

#: Public routes served by the owner gate, mapped to their audit operations.
#: The route registers through owner_route with this operation, and the app-scope
#: refusal reads the same map.
OWNER_ONLY_PUBLIC_ROUTES = {"/v1/models": "crew_api.models.list"}


def bearer_credential(request: web.Request) -> str | None:
    """The credential in the ``Authorization`` header.

    ``None`` when the request sends no ``Bearer`` header, so the caller may read the
    session cookie: another scheme (``Basic``, set by a proxy in front of the
    dashboard) is not a credential for this gateway. ``""`` for a ``Bearer`` header
    with nothing after it: the request chose this header, so it is refused on it.
    """
    scheme, _, credential = request.headers.get("Authorization", "").strip().partition(" ")
    if scheme.lower() != _SCHEME:
        return None
    return credential.strip()


def crew_api_bearer(request: web.Request) -> str | None:
    """The :func:`bearer_credential` of a Crew API request, ``None`` on any other path.

    Every token auth branch that asks whether a request carries a credential, or which
    kind, reads the bearer through here, so a bearer meets each check a cookie meets.
    """
    return bearer_credential(request) if is_crew_api_path(request.path) else None


async def verify_bearer(credential: str) -> tuple[bool, str, str, str]:
    """Judge a bearer credential: ``(valid, user_id, reason, app_name)``.

    Today the one accepted credential is a session token this gateway signed, the
    same one the session cookie carries: its signature (compared in constant time),
    its expiry, the revocation counter and the logout list are all checked, and a
    one-time link is refused as not being a session. Anything else, an empty
    credential included, is invalid.
    """
    if not credential:
        return False, "", "Token required", ""
    return await validate_token_with_app_off_loop(credential, use_session_exp=True)
