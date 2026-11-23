"""How a Crew API request (``/v1/``) carries its credential, and who verifies it.

A Crew API request names its caller in ``Authorization: Bearer <credential>``. A
``?token=`` on these paths is refused: a URL is kept in access logs, shell history and
proxies, and a credential there would be kept with it. The dashboard's own page calls
``/v1/`` routes with its session cookie, and keeps doing so when it sends no
``Authorization`` header.

:func:`verify_bearer` is the one place a bearer credential is judged. Token auth calls
it for every ``/v1/`` request that sends the header, then runs the same pin check,
app-scope check and audit as for a cookie. A new credential kind is added here.

:func:`refuse_app_credential` recognises the second kind, the access token local
authorization issues to an app, and refuses it by tier: no route serves an app yet.

:data:`OWNER_ONLY_PUBLIC_ROUTES` names the Public routes (``/v1/models``) that serve
the owner alone, through :func:`kiro_crew.dashboard.crew_api_owner.owner_route`.

The frozen ``/v1/chat/completions`` is outside this module: it keeps the credentials
it has always read.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard import local_authorization
from kiro_crew.dashboard.crew_api_errors import contract_error, is_crew_api_path, request_id_for
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
APP_CREDENTIAL_REFUSED = "an app credential may not call this endpoint"
_AUDIT_TEXT_MAX = 64

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


def _audit_text(value: str) -> str:
    """*value* for an audit row: printable ASCII only, at most ``_AUDIT_TEXT_MAX`` long."""
    return "".join(c for c in value if " " <= c <= "~")[:_AUDIT_TEXT_MAX]


async def _audit_app_credential(
    request: web.Request, caller: str, error: str, resources: str
) -> None:
    def write() -> None:
        sel().log_api_access(
            caller=caller,
            operation=APP_CREDENTIAL_OPERATION,
            outcome="denied",
            source="token_auth",
            resources=resources,
            error=error,
            forwarded=is_proxied_request(request),
        )

    try:
        await asyncio.to_thread(write)
    except Exception:
        logger.warning(
            "No audit row written for %s (denied, %s) by %s",
            APP_CREDENTIAL_OPERATION,
            error,
            caller,
        )


async def refuse_app_credential(request: web.Request, credential: str) -> web.Response | None:
    """The refusal for a bearer that is an app credential, ``None`` when it is not one.

    An app credential is the access token local authorization issued for a grant the
    owner approved (:mod:`kiro_crew.dashboard.local_authorization`). It is verified
    there: signature, expiry, and that its grant is still active. A credential that
    fails is not an app credential, so the caller's own 401 answers it.

    A verified one is a known caller, and no Crew API route serves that caller yet. So
    it is refused by tier, ``403 owner_only``, on every Crew API path, and the request
    never reaches a handler. It is accepted only from this machine, like the routes
    that issued it: through any forwarder this returns ``None`` as well, so the answer
    is byte for byte the 401 of a credential that does not verify.

    The caller's identity is published on the request before the refusal, for the audit
    middleware outside token auth. It is built from the gateway's own grant id. The
    ``client_id`` is the calling program's choice, so it is recorded as audit text
    only and never becomes part of an identity.
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
    await _audit_app_credential(request, caller, "owner_only", resources)
    return contract_error(
        403,
        "owner_only",
        APP_CREDENTIAL_REFUSED,
        request_id_for(request),
        {"X-Auth-Required": "true"},
    )
