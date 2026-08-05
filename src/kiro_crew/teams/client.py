"""Microsoft Teams / Bot Framework client transport layer.

Inbound: the Bot Framework POSTs Activity payloads to a single HTTPS webhook
route (registered on the gateway's aiohttp app). Each request carries an
``Authorization: Bearer <jwt>`` that MUST be validated (signature via the Bot
Framework JWKS, issuer in the documented set, audience == the bot's App ID,
expiry) before the activity is processed. The handler fast-acks with HTTP 200
and runs the agent turn in a background task so a long turn never times out the
inbound POST.

Outbound: plain REST -- ``POST {serviceUrl}/v3/conversations/{id}/activities``
authenticated with an app-credential (client-credentials) bearer token that is
cached and refreshed before expiry. A ``typing`` activity is posted at turn
start for immediate feedback.

No Bot Framework SDK dependency -- pure ``aiohttp`` for HTTP plus ``PyJWT`` for
inbound token verification (an optional ``kirocrew[teams]`` extra). This keeps
the module lightweight, OSS-clean, and easy to audit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp
from aiohttp import web

from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# PyJWT is an optional dependency (kirocrew[teams]); the channel refuses to
# start without it (see teams.gateway.maybe_start_teams) rather than crashing
# on import.
try:  # pragma: no cover - import guard
    import jwt as _jwt
    from jwt import PyJWKClient as _PyJWKClient

    HAS_JWT = True
except Exception:  # pragma: no cover - exercised only when PyJWT absent
    _jwt = None  # type: ignore[assignment]
    _PyJWKClient = None  # type: ignore[assignment,misc]
    HAS_JWT = False

# Teams text cap (characters). The platform allows large messages; keep a safe
# ceiling and let the renderer chunk beyond it.
TEAMS_MAX_TEXT = 20000

# Bot Framework OpenID metadata (public channel) -> jwks_uri for signature keys.
_OPENID_METADATA_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
# Accepted token issuers for activities originating from the Bot Connector.
_BOT_FRAMEWORK_ISSUERS = frozenset(
    {
        "https://api.botframework.com",
    }
)
# Client-credentials token endpoint (multi-tenant uses the botframework.com
# authority; single-tenant substitutes the configured tenant id).
_TOKEN_URL_TMPL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_TOKEN_SCOPE = "https://api.botframework.com/.default"
# Refresh the outbound token this many seconds before it actually expires.
_TOKEN_REFRESH_SKEW = 60.0
_CONNECTOR_API_TIMEOUT = 15.0


class TeamsAuthError(Exception):
    """Raised when an inbound Bot Framework JWT fails validation."""


@dataclass
class TeamsInbound:
    """Normalized inbound message from a Bot Framework ``message`` activity."""

    conversation_id: str
    conversation_type: str  # "personal" | "channel" | "groupChat"
    service_url: str
    text: str
    user_email: str = ""
    aad_object_id: str = ""
    activity_id: str = ""


def _activity_user_email(from_obj: dict[str, Any]) -> str:
    """Best-effort UPN/email extraction from an activity's ``from`` identity.

    Teams does not always populate an email on the bare activity. We check the
    documented locations in order; when none is present the caller treats the
    message as unauthorized (fail closed).
    """
    for key in ("userPrincipalName", "email", "upn"):
        val = from_obj.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _activity_conversation_type(conversation: dict[str, Any]) -> str:
    """Resolve the conversation scope, defaulting to ``personal`` for a 1:1."""
    ctype = conversation.get("conversationType")
    if isinstance(ctype, str) and ctype:
        return ctype
    # Older payloads use ``isGroup``; absent/false => a 1:1 personal chat.
    return "groupChat" if conversation.get("isGroup") else "personal"


class JwtValidator:
    """Validates inbound Bot Framework bearer tokens.

    The signing-key lookup + verify are synchronous (``PyJWT`` + a JWKS fetch),
    so callers run :meth:`verify` in a thread. Tests inject
    ``signing_key_getter`` to avoid any network access.
    """

    def __init__(
        self,
        app_id: str,
        *,
        issuers: frozenset[str] = _BOT_FRAMEWORK_ISSUERS,
        metadata_url: str = _OPENID_METADATA_URL,
        signing_key_getter: Callable[[str], Any] | None = None,
    ) -> None:
        self._app_id = app_id
        self._issuers = issuers
        self._metadata_url = metadata_url
        self._signing_key_getter = signing_key_getter
        self._jwk_client: Any = None
        self._jwks_uri: str = ""

    @staticmethod
    def _require_https(url: str, what: str) -> str:
        """Reject any non-HTTPS URL before a network fetch.

        ``urllib`` (and ``PyJWKClient``, which fetches the JWKS) honor
        ``file://`` and other schemes, so a non-https value could read a local
        file. The metadata URL is a fixed Bot Framework constant, but we pin
        the scheme defensively regardless, closing the arbitrary-file-read
        vector for both the metadata document and the resolved ``jwks_uri``.
        """
        if not url.lower().startswith("https://"):
            raise TeamsAuthError(f"{what} must be an https URL")
        return url

    def _resolve_jwks_uri(self) -> str:
        if self._jwks_uri:
            return self._jwks_uri
        metadata_url = self._require_https(self._metadata_url, "OpenID metadata URL")
        # URL scheme pinned to https above; the metadata URL is the fixed Bot
        # Framework OpenID constant, not user-controlled.
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(metadata_url, timeout=10) as resp:  # noqa: S310
            meta = json.loads(resp.read().decode("utf-8"))
        uri = meta.get("jwks_uri")
        if not isinstance(uri, str) or not uri:
            raise TeamsAuthError("Bot Framework OpenID metadata missing jwks_uri")
        self._jwks_uri = self._require_https(uri, "jwks_uri")
        return self._jwks_uri

    def _get_signing_key(self, token: str) -> Any:
        if self._signing_key_getter is not None:
            return self._signing_key_getter(token)
        if _PyJWKClient is None:  # pragma: no cover - guarded before use
            raise TeamsAuthError("PyJWT is not installed")
        if self._jwk_client is None:
            self._jwk_client = _PyJWKClient(self._resolve_jwks_uri())
        return self._jwk_client.get_signing_key_from_jwt(token).key

    def verify(self, token: str) -> dict[str, Any]:
        """Verify ``token`` and return its claims, or raise ``TeamsAuthError``.

        Checks RS256 signature against the matching JWKS key, audience == the
        bot's App ID, expiry/nbf, and issuer membership in the accepted set.
        """
        if not token:
            raise TeamsAuthError("missing bearer token")
        if _jwt is None:  # pragma: no cover - guarded before use
            raise TeamsAuthError("PyJWT is not installed")
        try:
            key = self._get_signing_key(token)
            claims = _jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self._app_id,
                options={"require": ["exp", "iss", "aud"]},
            )
        except TeamsAuthError:
            raise
        except Exception as exc:  # PyJWT raises many subclasses of PyJWTError
            raise TeamsAuthError(f"token validation failed: {type(exc).__name__}") from exc
        iss = claims.get("iss")
        if iss not in self._issuers:
            raise TeamsAuthError(f"untrusted issuer: {iss!r}")
        return claims


class TeamsClient:
    """Bot Framework client: inbound webhook + outbound Connector REST.

    Constructed with the Azure Bot ``app_id`` / ``app_password`` (+ optional
    ``tenant_id``). ``set_message_handler`` wires the transport's ``receive``
    after construction (avoids a client<->transport cycle). ``on_state_change``
    lets the gateway keep the dashboard status badge truthful.
    """

    def __init__(
        self,
        *,
        app_id: str,
        app_password: str,
        tenant_id: str = "",
        on_message: Callable[[TeamsInbound], Awaitable[None]] | None = None,
        validator: JwtValidator | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_password = app_password
        self._tenant_id = tenant_id or "botframework.com"
        self._on_message = on_message
        self._validator = validator or JwtValidator(app_id)
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        # Outbound app-credential token cache.
        self._token: str = ""
        self._token_expiry: float = 0.0
        self._token_lock = asyncio.Lock()
        self._closed = False
        self.last_error: str = ""
        self.on_state_change: Callable[[bool, str], None] | None = None
        # Live turn tasks -- prevent GC of in-flight background handlers.
        self._handler_tasks: set[asyncio.Task[None]] = set()

    # ── Wiring / lifecycle ──

    def set_message_handler(self, on_message: Callable[[TeamsInbound], Awaitable[None]]) -> None:
        self._on_message = on_message

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession()
        return self._session

    async def connect(self) -> None:
        """Prime the outbound token to validate credentials up front.

        A failure here is surfaced via ``on_state_change`` but never raised --
        the webhook route is still registered so the badge reports the reason.
        """
        self._closed = False
        try:
            await self._get_app_token(force=True)
            self._notify_state(True, "")
        except Exception as exc:
            self.last_error = f"credential check failed: {type(exc).__name__}"
            self._notify_state(False, self.last_error)

    async def close(self) -> None:
        self._closed = True
        for task in list(self._handler_tasks):
            task.cancel()
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    def _notify_state(self, connected: bool, error: str) -> None:
        if self.on_state_change is not None:
            try:
                self.on_state_change(connected, error)
            except Exception:
                logger.debug("Teams on_state_change observer raised", exc_info=True)

    # ── Inbound webhook ──

    async def on_activity(self, request: web.Request) -> web.Response:
        """Handle a Bot Framework activity POST.

        Validates the bearer token (401 on failure) BEFORE processing, parses
        the Activity (400 on malformed), fast-acks with 200, and runs the turn
        in a background task so a long turn never times out the POST.
        """
        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        # request.remote is the peer IP when present (real aiohttp request); the
        # bearer failed before any Activity is parsed, so it is the only caller
        # correlator available. getattr keeps this safe for lightweight test
        # doubles that omit .remote.
        peer = getattr(request, "remote", "") or "unknown"

        def _audit_denied(outcome: str) -> None:
            # Detective-control audit for a failed inbound authentication
            # (CWE-778), mirroring the serviceUrl-mismatch deny below. Best-effort
            # by design: the 401 denial is the security decision and MUST stand
            # even if the audit sink is unavailable (e.g. a corrupt SEL key), so a
            # sink failure is swallowed to a warning rather than surfacing as a
            # 500 that masks the denial.
            try:
                sel().log_api_access(
                    caller=peer,
                    operation="teams_client.on_activity",
                    outcome=outcome,
                    source="teams",
                )
            except Exception:
                logger.warning(
                    "Teams: failed to audit inbound auth denial (%s)", outcome
                )

        try:
            claims = await asyncio.to_thread(self._validator.verify, token)
        except TeamsAuthError:
            _audit_denied("denied_invalid_token")
            return web.Response(status=401, text="invalid bearer token")
        except Exception:
            logger.exception("Teams: unexpected error validating inbound token")
            _audit_denied("denied_token_validation_error")
            return web.Response(status=401, text="token validation error")

        try:
            activity = await request.json()
            if not isinstance(activity, dict):
                raise ValueError("activity is not a JSON object")
        except Exception:
            return web.Response(status=400, text="malformed activity payload")

        task = asyncio.create_task(self._dispatch_activity(activity, claims))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)
        return web.Response(status=200)

    async def _dispatch_activity(
        self, activity: dict[str, Any], claims: dict[str, Any]
    ) -> None:
        try:
            atype = activity.get("type")
            if atype != "message":
                # conversationUpdate / typing / etc. -> no turn (MVP).
                return
            from_obj = activity.get("from") or {}
            conversation = activity.get("conversation") or {}
            text = (activity.get("text") or "").strip()
            service_url = str(activity.get("serviceUrl", ""))
            # Bind the outbound target to the JWT-attested serviceUrl. The reply
            # carries an app-credential bearer token, so a replayed activity that
            # points serviceUrl at an attacker-controlled host must NOT receive
            # it. Require https and an exact match (trailing slash normalized)
            # against the token's 'serviceurl' claim; deny + audit otherwise.
            attested = str(claims.get("serviceurl", "")) if isinstance(claims, dict) else ""
            if (
                not service_url.lower().startswith("https://")
                or not attested
                or attested.rstrip("/").lower() != service_url.rstrip("/").lower()
            ):
                sel().log_api_access(
                    caller=str(from_obj.get("aadObjectId", "")) or "unknown",
                    operation="teams_client.dispatch",
                    outcome="denied_serviceurl_mismatch",
                    source="teams",
                )
                return
            inbound = TeamsInbound(
                conversation_id=str(conversation.get("id", "")),
                conversation_type=_activity_conversation_type(conversation),
                service_url=service_url,
                text=text,
                user_email=_activity_user_email(from_obj),
                aad_object_id=str(from_obj.get("aadObjectId", "")),
                activity_id=str(activity.get("id", "")),
            )
            if not inbound.text:
                return
            if self._on_message is not None:
                await self._on_message(inbound)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception:
            logger.exception("Teams: error dispatching inbound activity")

    # ── Outbound app-credential token ──

    async def _get_app_token(self, *, force: bool = False) -> str:
        now = time.monotonic()
        if not force and self._token and now < self._token_expiry:
            return self._token
        async with self._token_lock:
            now = time.monotonic()
            if not force and self._token and now < self._token_expiry:
                return self._token
            session = await self._ensure_session()
            url = _TOKEN_URL_TMPL.format(tenant=self._tenant_id)
            data = {
                "grant_type": "client_credentials",
                "client_id": self._app_id,
                "client_secret": self._app_password,
                "scope": _TOKEN_SCOPE,
            }
            async with session.post(
                url, data=data, timeout=aiohttp.ClientTimeout(total=_CONNECTOR_API_TIMEOUT)
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"token endpoint returned HTTP {resp.status}")
                body = await resp.json()
            token = body.get("access_token")
            if not isinstance(token, str) or not token:
                raise RuntimeError("token endpoint returned no access_token")
            expires_in = float(body.get("expires_in", 3600) or 3600)
            self._token = token
            self._token_expiry = time.monotonic() + max(0.0, expires_in - _TOKEN_REFRESH_SKEW)
            return token

    # ── Outbound Connector REST ──

    async def send_message(
        self, conversation_id: str, content: str, service_url: str
    ) -> str | None:
        """Send a text message to a conversation; return the activity id."""
        result = await self._post_activity(
            conversation_id, service_url, {"type": "message", "text": content or "…"}
        )
        return result.get("id") if isinstance(result, dict) else None

    async def send_typing(self, conversation_id: str, service_url: str) -> None:
        """Post a typing indicator (best-effort)."""
        await self._post_activity(conversation_id, service_url, {"type": "typing"})

    async def _post_activity(
        self, conversation_id: str, service_url: str, activity: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not service_url or not conversation_id:
            logger.warning("Teams: missing service_url/conversation_id; dropping outbound send")
            return None
        if not service_url.lower().startswith("https://"):
            # Never send the app bearer token over a non-https serviceUrl.
            logger.warning("Teams: refusing non-https serviceUrl for outbound send")
            return None
        base = service_url.rstrip("/")
        url = f"{base}/v3/conversations/{conversation_id}/activities"
        try:
            token = await self._get_app_token()
            session = await self._ensure_session()
            async with session.post(
                url,
                json=activity,
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=_CONNECTOR_API_TIMEOUT),
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
                try:
                    return await resp.json()
                except Exception:
                    return {}
        except Exception as exc:
            self.last_error = f"send failed: {type(exc).__name__}"
            logger.warning("Teams: outbound send failed: %s", type(exc).__name__)
            self._notify_state(False, self.last_error)
            return None
