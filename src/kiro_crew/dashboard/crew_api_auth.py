"""How a Crew API request (``/v1/``) carries its credential, and who verifies it.

A Crew API request names its caller in ``Authorization: Bearer <credential>``. A
``?token=`` on these paths is refused: a URL is kept in access logs, shell history and
proxies, and a credential there would be kept with it. The dashboard's own page calls
``/v1/`` routes with its session cookie, and keeps doing so when it sends no
``Authorization`` header.

:func:`verify_bearer` is the one place a bearer credential is judged. Token auth calls
it for every ``/v1/`` request that sends the header, then runs the same pin check,
app-scope check and audit as for a cookie. A new credential kind is added here.

:func:`judge_app_credential` recognises the second kind, the access token local
authorization issues to an app, and judges it by the matched route's row in
:mod:`kiro_crew.dashboard.crew_api_scopes`. It admits the credential on the routes
:data:`APP_SERVED_ROUTES` names and refuses it on every other.

:data:`OWNER_ONLY_PUBLIC_ROUTES` names the Public routes (``/v1/models``,
``/v1/responses``) served through :func:`kiro_crew.dashboard.crew_api_owner.owner_route`.

The frozen ``/v1/chat/completions`` is outside this module: it keeps the credentials
it has always read.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final

from aiohttp import web

from kiro_crew.dashboard import crew_api_scopes, local_authorization
from kiro_crew.dashboard.crew_api_errors import is_crew_api_path
from kiro_crew.dashboard.kiro_relay.caller import published_relay_caller
from kiro_crew.dashboard.origin import is_direct_local_request, is_proxied_request
from kiro_crew.dashboard.token_auth_tokens import validate_token_with_app_off_loop
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_SCHEME = "bearer"

#: What an app credential's request carries as ``request["user"]`` and
#: ``request["app"]``, each followed by the grant id. The ``:`` keeps either from
#: being an installed app's name, which holds no ``:``.
APP_CREDENTIAL_USER_PREFIX = "app:"
APP_CREDENTIAL_APP_PREFIX = "local-grant:"
APP_CREDENTIAL_OPERATION = "crew_api.app_credential"
APP_CREDENTIAL_REFUSED = crew_api_scopes.APP_REFUSED
#: The audit word for a route whose row admits the credential and which serves no app.
NO_APP_ROUTE = "no-app-route"
_AUDIT_TEXT_MAX = 64

#: The refusal a ``?token=`` gets on a Crew API path.
QUERY_TOKEN_REFUSED = "Send the credential in the Authorization header, not in ?token="

#: Public routes served by the owner gate, mapped to their audit operations.
#: The route registers through owner_route with this operation, and the app-scope
#: refusal reads the same map.
OWNER_ONLY_PUBLIC_ROUTES = {
    "/v1/models": "crew_api.models.list",
    "/v1/responses": "crew_api.responses.create",
}

#: The routes that serve an app credential, by their ``crew_api_scopes.ROUTES`` key.
#: A route joins this set in the change that makes its handler call
#: ``crew_api_scopes.require_scope``. On every other route a credential its row
#: admits is still refused.
APP_SERVED_ROUTES: Final = frozenset({("GET", "/v1/models")})


class AppAdmitted:
    """What :func:`judge_app_credential` answers for a credential its route serves."""


ADMITTED: Final = AppAdmitted()


def app_owner_only_routes() -> frozenset[str]:
    """The Public patterns an app credential is refused on by tier.

    Every :data:`OWNER_ONLY_PUBLIC_ROUTES` pattern that no :data:`APP_SERVED_ROUTES`
    key names.
    """
    served = {pattern for _method, pattern in APP_SERVED_ROUTES}
    return frozenset(OWNER_ONLY_PUBLIC_ROUTES) - served


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


def _audit_text(value: str) -> str:
    """*value* for an audit row: printable ASCII only, at most ``_AUDIT_TEXT_MAX`` long."""
    return "".join(c for c in value if " " <= c <= "~")[:_AUDIT_TEXT_MAX]


async def _audit_app_credential(
    request: web.Request, caller: str, error: str, resources: str
) -> None:
    """Write the decision's audit row: ``allowed`` when *error* is empty."""
    outcome = "denied" if error else "allowed"

    def write() -> None:
        sel().log_api_access(
            caller=caller,
            operation=APP_CREDENTIAL_OPERATION,
            outcome=outcome,
            source="token_auth",
            resources=resources,
            error=error,
            forwarded=is_proxied_request(request),
        )

    try:
        await asyncio.to_thread(write)
    except Exception:
        logger.warning(
            "No audit row written for %s (%s, %s) by %s",
            APP_CREDENTIAL_OPERATION,
            outcome,
            error,
            caller,
        )


async def judge_app_credential(
    request: web.Request, credential: str
) -> web.Response | AppAdmitted | None:
    """The answer for a bearer that is an app credential, ``None`` when it is not one.

    An app credential is the access token local authorization issued for a grant the
    owner approved (:mod:`kiro_crew.dashboard.local_authorization`). It is verified
    there: signature, expiry, and that its grant is still active. A credential that
    fails is not an app credential, so the caller's own 401 answers it.

    A verified one is a known caller, and :func:`crew_api_scopes.refusal_for` says why it may
    not call the route it matched: the tier, the entitlement or the scope. A route whose
    row admits it answers :data:`ADMITTED` only when :data:`APP_SERVED_ROUTES` names the
    route, after the ``allowed`` audit row. On any other route it is
    refused ``403 owner_only`` with :data:`NO_APP_ROUTE` on the audit row, and the
    request never reaches a handler. It is accepted only from this machine, like the
    routes that issued it: through any forwarder this returns ``None`` as well, so the
    answer is byte for byte the 401 of a credential that does not verify.

    The caller's identity is published on the request before the answer, for the handler
    and for the audit middleware outside token auth. It is built from the gateway's own
    grant id. The ``client_id`` is the calling program's choice, so it is recorded as
    audit text only and never becomes part of an identity.
    """
    if credential.count(".") != 2:
        return None
    try:
        found = await asyncio.to_thread(
            lambda: local_authorization.gateway_local_authorization().verify(credential)
        )
    except Exception:
        logger.warning("Crew API app credential check failed")
        return None
    if found is None:
        return None
    caller = f"{APP_CREDENTIAL_USER_PREFIX}{found.grant_id}"
    resources = f"{request.path} client_id={_audit_text(found.client_id)}"
    if not is_direct_local_request(request) or published_relay_caller(request) is not None:
        # Answered as any credential that does not verify: only the audit row says why.
        await _audit_app_credential(request, caller, "not-direct-local", resources)
        return None
    request["user"] = caller
    request["app"] = f"{APP_CREDENTIAL_APP_PREFIX}{found.grant_id}"
    request["is_dashboard_user"] = False
    request[crew_api_scopes.CREDENTIAL_KEY] = found
    key = crew_api_scopes.route_key(request)
    refusal = crew_api_scopes.refusal_for(key, found.scopes, app_owner_only_routes())
    if refusal is None and key not in APP_SERVED_ROUTES:
        refusal = crew_api_scopes.Refusal("owner_only", APP_CREDENTIAL_REFUSED, NO_APP_ROUTE)
    await _audit_app_credential(
        request, caller, "" if refusal is None else refusal.audit, resources
    )
    if refusal is None:
        return ADMITTED
    # Signing in again cures no missing scope or entitlement, so only the tier
    # refusal carries the header every other token auth refusal has.
    return refusal.response(
        request, {"X-Auth-Required": "true"} if refusal.code == "owner_only" else None
    )
