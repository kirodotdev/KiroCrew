"""OAuth 2.0 authorization-code + PKCE, for the Google and Microsoft 365 calendars.

Nothing in the product implemented OAuth before this, so this is a client written
from scratch rather than a wiring job — the only prior credential handling is the
Notes builtin's plain token on disk, which is a store and not a flow.

The shape is RFC 6749's authorization-code grant with RFC 7636 PKCE, in the
RFC 8252 "native app" arrangement:

1. :func:`begin` mints a code verifier and an opaque ``state``, remembers them in
   memory, and returns the provider's consent URL.
2. The user consents in a browser. The provider redirects to a **loopback**
   redirect URI served by the dashboard itself
   (``/api/apps/meetings/calendar/oauth/callback``), so no public callback host
   is involved and the code never leaves the machine.
3. :func:`complete` checks ``state``, exchanges the code plus the verifier for
   tokens, and writes them to the credential store.
4. :func:`access_token` hands out a live access token, refreshing it when it has
   expired.

Why PKCE is mandatory here rather than optional: a desktop client cannot keep a
client secret, so the authorization code is the only thing standing between an
attacker and the user's calendar. PKCE binds the code to a verifier that never
left this process, so a code intercepted at the redirect — another local process
racing the loopback listener is the realistic case — cannot be redeemed.

Two things this module deliberately does NOT do:

* It does not open a browser. The caller decides how the URL reaches the user,
  which keeps this module testable and leaves the "headless host" case to a
  layer that can see the UI.
* It does not persist the pending state. A verifier is useful for seconds and
  writing it down would put a second live secret on disk to protect; an
  interrupted flow is restarted instead.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import credentials
from kiro_crew.apps.builtins.meetings.backend.providers.calendar import (
    CalendarError,
    fetch_vetted,
)


@dataclass(frozen=True)
class OAuthClient:
    """Everything provider-specific about one OAuth deployment.

    ``client_secret`` is optional because a native client is a PUBLIC client: for
    Google's "Desktop app" type the secret is shipped to the user and is not a
    secret in any meaningful sense, and for Microsoft's public-client
    registration there is none at all. It is sent when present because some
    registrations still require it, and PKCE — not the secret — is what actually
    protects the exchange.
    """

    provider_id: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    #: Extra parameters the provider needs on the authorization request.
    #: Google needs ``access_type=offline`` + ``prompt=consent`` or it returns no
    #: refresh token on a repeat authorization, which is the difference between a
    #: calendar that keeps working and one that stops in an hour.
    extra_authorize_params: dict[str, str] = field(default_factory=dict)


@dataclass
class _Pending:
    """A flow in progress. Held in memory only — see the module docstring."""

    state: str
    verifier: str
    redirect_uri: str
    created_at: float


#: At most one pending flow per provider. Starting a second authorization
#: replaces the first, which is what a user retrying after a mistake expects.
_pending: dict[str, _Pending] = {}


def _now() -> float:
    return time.monotonic()


def new_verifier() -> str:
    """A PKCE code verifier: 43-128 unreserved characters (RFC 7636 §4.1).

    ``token_urlsafe(64)`` yields ~86 characters from a CSPRNG, comfortably inside
    the range and well past the 256 bits of entropy the RFC asks for.
    """
    return secrets.token_urlsafe(64)


def challenge_for(verifier: str) -> str:
    """The S256 code challenge for *verifier*.

    S256 only. RFC 7636 also allows ``plain``, which offers no protection at all
    against an intercepted authorization request — the verifier IS the challenge
    there — so it is not implemented rather than being offered and discouraged.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


async def begin(client: OAuthClient, *, redirect_uri: str) -> str:
    """Start a flow and return the URL the user must open to consent.

    The ``state`` is a fresh CSPRNG value, not derived from anything: it is the
    only thing tying the callback the dashboard receives back to a flow this
    process actually started, so a forged callback — a link a user is tricked
    into opening — has nothing to present.

    ``async`` because reading the registered client id touches the disk, and this
    runs on the gateway's single event loop (AUTOSDE
    ``no-blocking-call-on-event-loop``).
    """
    client_id = await _client_id_for(client.provider_id)
    if not client_id:
        raise CalendarError(
            "No OAuth client id is configured for this calendar. Add one in "
            "Settings -> Calendar."
        )
    verifier = new_verifier()
    state = secrets.token_urlsafe(32)
    _pending[client.provider_id] = _Pending(
        state=state,
        verifier=verifier,
        redirect_uri=redirect_uri,
        created_at=_now(),
    )
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(client.scopes),
        "state": state,
        "code_challenge": challenge_for(verifier),
        "code_challenge_method": "S256",
        **client.extra_authorize_params,
    }
    return f"{client.authorize_url}?{urlencode(params)}"


async def _client_id_for(provider_id: str) -> str:
    """The registered client id, from the credential store.

    A client id is not a secret, but it lives beside the tokens because it is
    per-installation configuration the user pastes in once and because keeping
    the pair together means one place can answer "is this provider set up".
    """
    stored = await credentials.read_for(provider_id)
    return stored.get("client_id", "")


def forget_pending(provider_id: str) -> None:
    """Drop any flow in progress for *provider_id*."""
    _pending.pop(provider_id, None)


async def complete(client: OAuthClient, *, code: str, state: str) -> None:
    """Finish a flow: verify ``state``, exchange *code*, store the tokens.

    The pending flow is consumed BEFORE the exchange, whatever the outcome. A
    verifier that survived a failed attempt could be replayed, and leaving one
    behind on success would let a captured code be redeemed twice.
    """
    pending = _pending.pop(client.provider_id, None)
    if pending is None:
        raise CalendarError("no calendar authorization is in progress; start again")
    if _now() - pending.created_at > k.OAUTH_FLOW_TTL_SECS:
        raise CalendarError("the calendar authorization expired; start again")
    # Constant time, because a comparison that returns early leaks a prefix and
    # `state` is the anti-forgery value.
    if not secrets.compare_digest(pending.state, state or ""):
        raise CalendarError("the calendar authorization did not match; start again")
    if not code:
        raise CalendarError("the calendar authorization returned no code")

    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending.redirect_uri,
        "client_id": await _client_id_for(client.provider_id),
        "code_verifier": pending.verifier,
    }
    tokens = await _post_token(client, payload)
    await _store_tokens(client.provider_id, tokens)


async def access_token(client: OAuthClient) -> str:
    """A usable access token, refreshed if the stored one has expired.

    Refreshes slightly EARLY (:data:`k.OAUTH_REFRESH_SKEW_SECS`). A token that
    passes the check and then expires in flight surfaces as a 401 halfway through
    a sync, which is a worse failure than one extra refresh.
    """
    stored = await credentials.read_for(client.provider_id)
    refresh = stored.get("refresh_token", "")
    if not refresh:
        raise CalendarError(
            "This calendar is not connected. Connect it in Settings -> Calendar."
        )
    token = stored.get("access_token", "")
    expires_at = _as_float(stored.get("expires_at", "0"))
    if token and expires_at - time.time() > k.OAUTH_REFRESH_SKEW_SECS:
        return token

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": await _client_id_for(client.provider_id),
    }
    tokens = await _post_token(client, payload)
    await _store_tokens(client.provider_id, tokens)
    fresh = (await credentials.read_for(client.provider_id)).get("access_token", "")
    if not fresh:
        raise CalendarError("the calendar provider returned no access token")
    return fresh


def _as_float(value: object) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


async def _post_token(client: OAuthClient, payload: dict[str, str]) -> dict[str, Any]:
    """POST to the token endpoint through the shared, gated fetch.

    Routed through :func:`fetch_vetted` rather than a bare aiohttp call so the
    token endpoint gets the same treatment as a calendar URL: https only, DNS
    resolved once and pinned, redirects re-validated per hop, and a size cap. A
    token endpoint is a well-known host, but "well-known" is a property of the
    configuration, and the configuration is editable.

    The secret, when the registration has one, goes in the request BODY rather
    than a Basic header. Both are allowed by RFC 6749 §2.3.1; the body form keeps
    it out of ``auth_headers`` semantics that do not apply here, and Google and
    Microsoft both accept it.
    """
    stored = await credentials.read_for(client.provider_id)
    secret = stored.get("client_secret", "")
    body = dict(payload)
    if secret:
        body["client_secret"] = secret
    raw = await fetch_vetted(
        client.token_url,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        body=urlencode(body).encode("utf-8"),
        timeout_secs=k.OAUTH_TOKEN_TIMEOUT_SECS,
        max_bytes=k.MAX_OAUTH_RESPONSE_BYTES,
    )
    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise CalendarError("the calendar provider's token response was not JSON") from exc
    if not isinstance(parsed, dict):
        raise CalendarError("the calendar provider's token response was not an object")
    if parsed.get("error"):
        # `error_description` is the provider's own prose and is safe to show; the
        # request body it describes is never echoed, so no secret rides along.
        detail = str(parsed.get("error_description") or parsed.get("error"))
        raise CalendarError(f"the calendar provider refused the authorization: {detail}")
    return parsed


async def _store_tokens(provider_id: str, tokens: dict[str, Any]) -> None:
    """Persist an access token, its expiry, and a refresh token when given one.

    A refresh response often omits ``refresh_token`` — the existing one stays
    valid. The store MERGES, so an omitted field is left alone rather than
    cleared; writing an empty string here would delete the only thing that keeps
    the connection alive.
    """
    values: dict[str, str] = {}
    access = str(tokens.get("access_token") or "")
    if access:
        values["access_token"] = access
        expires_in = _as_float(tokens.get("expires_in", 0))
        # Absolute, not relative: a stored duration would have to be interpreted
        # against a write time nobody recorded.
        values["expires_at"] = str(time.time() + expires_in) if expires_in > 0 else "0"
    refresh = str(tokens.get("refresh_token") or "")
    if refresh:
        values["refresh_token"] = refresh
    if values:
        await credentials.write_for(provider_id, values)
