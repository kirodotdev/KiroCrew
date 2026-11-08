"""The relay viewer's gateway routes: the desktop app lists remote Crews and opens one.

Off unless ``tunnel.relay_viewer_enabled`` is set: both routes answer 404
``RELAY_VIEWER_OFF`` while it is off. Neither is reachable through the relay
(``kiro_relay.limits`` refuses everything under ``/api/relay-viewer``).

* ``GET /api/relay-viewer/crews``: the owner's dashboard reads the Crews this
  person can open. The answer carries no token.
* ``POST /api/relay-viewer/token``: the desktop app's main process gets an access
  token for one of them. Admitted like ``/api/token/local``: loopback (or the
  dashboard's unix socket from this user), the local secret, and a verified host
  process. A dashboard session alone is refused, so a page cannot call it.

Both calls to the control plane go through :mod:`kiro_crew.relay_viewer.cps` with
the person's Kiro sign-in from the gateway's own vault, or the test credential
``tunnel.relay_viewer_credential`` names. No token, sign-in or credential value is
logged or put in an error. Every answer is ``Cache-Control: no-store``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, TypeVar

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.core import _unix_peer_is_self
from kiro_crew.dashboard.origin import is_loopback
from kiro_crew.dashboard.token_auth import _ct_eq

if TYPE_CHECKING:
    from kiro_crew.relay_viewer import cps

# The control plane client and the relay modules are imported inside the handlers,
# after the setting check, so a gateway with the viewer off loads none of them.

logger = logging.getLogger(__name__)

OFF_CODE = "RELAY_VIEWER_OFF"
_NO_STORE = {"Cache-Control": "no-store"}
#: The client's typed codes (``relay_viewer.cps``), spelled out so this module
#: needs no import of it at load time.
_STATUS = {
    "SIGNED_OUT": 409,
    "NOT_SET_UP": 409,
    "INVALID_REQUEST": 400,
    "THROTTLED": 429,
    "SERVICE_ERROR": 502,
    "UNREACHABLE": 502,
    "BAD_ANSWER": 502,
}  # every other code (the access refusals and CREW_OFFLINE) is 403
#: A CPS 401 forces a sign-in refresh at most once per identity in this window.
FORCE_COOLDOWN_SECONDS = 300.0
_FORCED_AT: dict[str, float] = {}  # identity -> time.monotonic() of its last forced refresh
#: identity -> the lock a 401 handler holds across its re-read and forced refresh, so a
#: second 401 on the same sign-in waits for that refresh instead of reporting signed out.
_FORCE_LOCKS: dict[str, asyncio.Lock] = {}
_T = TypeVar("_T")


def _ok(body: dict[str, Any]) -> web.Response:
    return web.json_response(body, headers=_NO_STORE)


def _error(error: str, code: str, status: int) -> web.Response:
    return web.json_response({"error": error, "code": code}, status=status, headers=_NO_STORE)


def _refused(exc: cps.CpsRefused) -> web.Response:
    if exc.code == "THROTTLED":
        retry = exc.retry_after
        headers = {**_NO_STORE, **({"Retry-After": str(int(retry))} if retry is not None else {})}
        return web.json_response(
            {"error": str(exc), "code": exc.code, "retryAfter": retry}, status=429, headers=headers
        )
    return _error(str(exc), exc.code, _STATUS.get(exc.code, 403))


def _off() -> web.Response:
    return _error("The relay viewer is turned off.", OFF_CODE, 404)


def _load_tunnel() -> Any:
    from kiro_crew.config.loader import KiroCrewConfig

    return KiroCrewConfig.load().tunnel


async def _settings() -> Optional[tuple[str, str]]:
    """``(cps_url, credential_ref)`` while the viewer is on, else ``None``."""
    tunnel = await asyncio.to_thread(_load_tunnel)
    if getattr(tunnel, "relay_viewer_enabled", False) is not True:
        return None
    url = getattr(tunnel, "relay_viewer_cps_url", "")
    ref = getattr(tunnel, "relay_viewer_credential", "")
    return (
        url.strip() if isinstance(url, str) else "",
        ref.strip() if isinstance(ref, str) else "",
    )


def _may_force(identity: str) -> bool:
    """One forced refresh per sign-in identity per process every cooldown, recorded here."""
    now = time.monotonic()
    last = _FORCED_AT.get(identity)
    if last is not None and now - last < FORCE_COOLDOWN_SECONDS:
        return False
    _FORCED_AT[identity] = now
    return True


async def _bearer(ref: str, rejected: str = "") -> tuple[str, str]:
    """``(credential, token_type)`` CPS is called with. Raises :class:`cps.CpsRefused`.

    *token_type* is ``EXTERNAL_IDP`` for an external IdP sign-in, else empty (the
    test credential and every other sign-in send no ``TokenType``).

    *rejected* is a sign-in CPS just answered 401. The vault is read again, and if it
    still holds that sign-in it is refreshed, at most once per cooldown. Inside the
    cooldown the re-read is answered as is, so the caller sees the same sign-in. A
    second 401 on the same sign-in waits for a refresh in flight and gets its result.
    """
    from kiro_crew.relay_viewer import cps

    if ref:
        from kiro_crew.relay_client.lease import LeaseCredentialError, read_lease_credential

        try:
            return await asyncio.to_thread(read_lease_credential, ref), ""
        except LeaseCredentialError:
            raise cps.CpsRefused(cps.NOT_SET_UP, "the relay viewer credential can't be read")
    from kiro_crew.auth.bridge import default_token_store
    from kiro_crew.auth.provider import KasAuthProvider, NotAuthenticated
    from kiro_crew.auth.refresh import RefreshBlocked, RefreshRejected

    provider = KasAuthProvider(default_token_store(), allow_env_api_key=False)
    try:
        token = await provider.current()
        if rejected and token.access_token == rejected:
            lock = _FORCE_LOCKS.get(token.identity)
            if lock is None:
                lock = _FORCE_LOCKS[token.identity] = asyncio.Lock()
            # Held across the re-read and the refresh only, never a CPS call.
            async with lock:
                token = await provider.current()
                if token.access_token == rejected and _may_force(token.identity):
                    logger.info("relay viewer: CPS refused the sign-in; refreshing it once")
                    token = await provider.current(rejected_access_token=rejected)
    except (NotAuthenticated, RefreshRejected, RefreshBlocked):
        raise cps.CpsRefused(cps.SIGNED_OUT, "sign in to Kiro to see your remote Crews")
    except Exception as exc:  # noqa: BLE001 - a transient refresh failure; type only, no message
        logger.info("relay viewer: sign-in refresh failed (%s)", type(exc).__name__)
        raise cps.CpsRefused(cps.UNREACHABLE, "your Kiro sign-in couldn't be refreshed")
    return token.access_token, ("EXTERNAL_IDP" if token.auth_method == "external_idp" else "")


async def _with_sign_in(ref: str, call: Callable[[str, str], Awaitable[_T]]) -> _T:
    """``call(bearer, token_type)``, sent again once when CPS answers it 401.

    Only a 401 from a CPS call does this, once per request: the sign-in is read again
    (refreshed if CPS refused the stored one, see :func:`_bearer`) and *call* resent
    when it changed. An unchanged sign-in, or a second 401, stays ``SIGNED_OUT``. The
    test credential is never refreshed, so its 401 is final.
    """
    from kiro_crew.relay_viewer import cps

    bearer, token_type = await _bearer(ref)
    try:
        return await call(bearer, token_type)
    except cps.CpsRefused as exc:
        if exc.status != 401 or ref:
            raise
        refused = exc
    fresh, token_type = await _bearer(ref, rejected=bearer)
    if fresh == bearer:
        raise refused
    return await call(fresh, token_type)


async def _sign_in_marker(ref: str) -> Optional[str]:
    """A short, non-secret marker naming the Kiro sign-in, or ``None``.

    The desktop app closes its relay Crew windows when it changes (an account
    switch). Built only from the vault's identity fingerprint (allowlisted account
    claims plus the sign-in generation, no token or secret), under a fixed prefix
    so it can't be matched against that fingerprint. ``None`` with the test
    credential or an empty fingerprint. Never logged.
    """
    if ref:
        return None
    from kiro_crew.auth.bridge import vault_identity_fingerprint

    fingerprint = await asyncio.to_thread(vault_identity_fingerprint)
    if not fingerprint:
        return None
    return hashlib.sha256(b"relay-viewer-sign-in\n" + fingerprint.encode()).hexdigest()[:16]


def _entry(crew: cps.CrewEndpoint) -> dict[str, str]:
    return {
        "endpointId": crew.endpoint_id,
        "name": crew.name,
        "crewType": crew.crew_type,
        "presence": crew.presence,
        "relayConnectionStatus": crew.relay_connection_status,
        "crewUrl": crew.crew_url,
    }


async def api_relay_viewer_crews(request: web.Request) -> web.Response:
    """GET /api/relay-viewer/crews: the Crews this person can open, with no token."""
    settings = await _settings()
    if settings is None:
        return _off()
    from kiro_crew.dashboard.kiro_relay.caller import published_relay_caller
    from kiro_crew.relay_viewer import cps

    if published_relay_caller(request) is not None:
        return _error("Not available through the relay.", "relay_local_credential", 403)
    denied = await require_owner_dashboard_request(request, "relay_viewer.crews")
    if denied is not None:
        denied.headers.update(_NO_STORE)
        return denied
    cps_url, ref = settings
    try:
        crews = await _with_sign_in(
            ref,
            lambda bearer, token_type: cps.list_crew_endpoints(
                cps_url, bearer, token_type=token_type
            ),
        )
    except cps.CpsRefused as exc:
        logger.info("relay viewer: crews -> %s", exc.code)
        return _refused(exc)
    logger.info("relay viewer: crews -> %d listed", len(crews))
    return _ok({"crews": [_entry(c) for c in crews]})


def _audit(request: web.Request, outcome: str, resources: str) -> None:
    try:
        from kiro_crew.sel import sel as _sel

        _sel().log_api_access(
            caller=request.remote or "unknown",
            operation="relay_viewer.token",
            outcome=outcome,
            source="local-bootstrap",
            resources=resources,
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for relay_viewer.token failed", exc_info=True)


async def _local_main_refusal(request: web.Request) -> Optional[web.Response]:
    """The ``/api/token/local`` admission, minus app backends. ``None`` admits."""
    if not is_loopback(request.remote or "") and not _unix_peer_is_self(request):
        _audit(request, "denied", "non-loopback")
        return _error("loopback only", "loopback_only", 403)
    expected = request.app.get("local_secret", "")
    if not expected:
        return _error("not available", "not_available", 503)
    provided = request.headers.get("X-Local-Secret", "")
    if not provided or not _ct_eq(expected, provided):
        _audit(request, "denied", "invalid-secret")
        return _error("invalid secret", "invalid_secret", 403)
    from kiro_crew.member_memory_auth import local_owner_bootstrap_provenance

    provenance = await asyncio.to_thread(local_owner_bootstrap_provenance, request)
    # Only the desktop app's own main process needs this, never an app backend.
    if provenance is None or provenance.app:
        _audit(request, "denied", "unverified-owner-process")
        return _error("This process isn't the desktop app.", "member_owner_token_refused", 403)
    return None


async def api_relay_viewer_token(request: web.Request) -> web.Response:
    """POST /api/relay-viewer/token: an access token for one Crew, for main only."""
    refusal = await _local_main_refusal(request)
    if refusal is not None:
        return refusal
    settings = await _settings()
    if settings is None:
        return _off()
    from kiro_crew.relay_client.protocol import ENDPOINT_ID_RE
    from kiro_crew.relay_viewer import cps

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - any unreadable body is the same bad request
        body = None
    endpoint_id = body.get("endpointId") if isinstance(body, dict) else None
    if not isinstance(endpoint_id, str) or not ENDPOINT_ID_RE.match(endpoint_id):
        return _refused(cps.CpsRefused(cps.INVALID_REQUEST, "not a Crew endpoint ID"))
    cps_url, ref = settings

    async def _list_then_mint(
        bearer: str, token_type: str
    ) -> tuple[cps.CrewEndpoint, cps.CrewAccessToken]:
        # Listed again on each call, so the Crew must still be granted and its
        # URL is the one CPS gives now.
        crew = next(
            (
                c
                for c in await cps.list_crew_endpoints(cps_url, bearer, token_type=token_type)
                if c.endpoint_id == endpoint_id
            ),
            None,
        )
        if crew is None:
            raise cps.CpsRefused(cps.CREW_ACCESS_NOT_GRANTED, "this Crew isn't in your list")
        grant = await cps.create_crew_access_token(
            cps_url, bearer, endpoint_id, token_type=token_type
        )
        return crew, grant

    # The marker is read before and after the whole sign-in and mint (the 401
    # resend included). Every sign-in writes a new generation, so a switch at any
    # point in between changes it, and the grant, minted under an account the
    # marker can't name, is never sent.
    before = await _sign_in_marker(ref)
    try:
        crew, grant = await _with_sign_in(ref, _list_then_mint)
        marker = await _sign_in_marker(ref)
        if marker != before:
            raise cps.CpsRefused(
                cps.UNREACHABLE, "your Kiro sign-in changed while this Crew was opening, try again"
            )
    except cps.CpsRefused as exc:
        logger.info("relay viewer: mint %s -> %s", endpoint_id, exc.code)
        _audit(request, "denied", f"{endpoint_id} {exc.code}")
        return _refused(exc)
    logger.info("relay viewer: mint %s -> done", endpoint_id)
    _audit(request, "success", endpoint_id)
    return _ok(
        {
            "accessToken": grant.access_token,
            "expiresAt": grant.expires_at,
            "renewInSeconds": grant.renew_in_seconds,
            "serverDate": grant.server_date,
            "crewUrl": crew.crew_url,
            "name": crew.name,
            "signInMarker": marker,
        }
    )
