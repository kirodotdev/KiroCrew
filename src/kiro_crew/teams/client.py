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
import ipaddress
import json
import logging
import socket
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Awaitable, Callable

import aiohttp
from aiohttp import web

from kiro_crew import link_unfurl
from kiro_crew.sel import sel
from kiro_crew.teams.attachments import quoted_reply_text
from kiro_crew.teams.commands import STOP_ALIASES

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

#: Byte ceiling a bot message is sized against. The Connector's hard limit is
#: ~100 KB; Microsoft recommends staying under 80 KB, and the widely-quoted 28 KB
#: figure is the Incoming-Webhook / message-extension-card limit, not a bot
#: message. The recommended value, not the hard one: a rejected activity is a lost
#: answer, and the gap absorbs the rest of the JSON envelope (ids, serviceUrl,
#: recipient) that shares the body with the text.
TEAMS_MAX_ACTIVITY_TEXT_BYTES = 80 * 1024

#: Worst-case UTF-8 cost of ONE character on the wire: an astral codepoint (emoji,
#: rarer CJK) is 4 bytes. Only true because the outbound session serializes with
#: ``ensure_ascii=False``; with aiohttp's default the same codepoint is a 12-byte
#: surrogate-pair escape and this multiplier is a lie. The two decisions are one.
_MAX_UTF8_BYTES_PER_CHAR = 4

# Teams text cap in CHARACTERS -- the unit the shared chunker and every capability
# consumer count, neither of which can see bytes. Declared BYTE-SAFE at the
# worst-case multiplier (16000 * 4 = 64 KB, leaving 16 KB for the envelope),
# because the two consumers that trust this number fail SILENTLY when it is
# optimistic: the dashboard mirror leg chunks on it and swallows the send error,
# and a renderer chunk the Connector refuses as 413 takes that slice of the answer
# with it. Pinned by test_capability_ledger.py alongside Webex, the other
# byte-capped channel.
TEAMS_MAX_TEXT = 16000

#: Key under which the dashboard route stashes the size-capped, parsed inbound
#: activity on the request mapping. Shared so the route and ``on_activity``
#: cannot drift on the spelling.
TEAMS_ACTIVITY_REQUEST_KEY = "teams_activity"

#: Byte ceiling for an inbound activity body, matching the dashboard's shared
#: default rather than exceeding it. This is the one route in the product
#: reachable with neither cookie auth nor an Origin check, and the bounded read
#: parses on the event loop, so the cap is a loop-stall bound as much as a memory
#: one. A real Teams message activity is a few KB, leaving ample headroom.
TEAMS_MAX_ACTIVITY_BYTES = 64 * 1024

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

#: Activity types that carry a ROUTABLE address but no prompt. A personal-chat
#: ``conversationUpdate`` (membersAdded) and an ``installationUpdate`` both arrive
#: under the same JWT attestation as a message, so the conversation id + serviceUrl
#: on them are as trustworthy as any other -- and learning them is what makes a
#: freshly-installed app reachable before the user says anything.
_ROUTE_ONLY_ACTIVITY_TYPES = frozenset({"conversationUpdate", "installationUpdate"})

#: The ONE ``activity.channelId`` this channel serves. Every other Azure Bot
#: channel (Web Chat, Direct Line, Email, SMS) reaches the same endpoint with the
#: same credential and would otherwise be indistinguishable from Teams.
_TEAMS_CHANNEL_ID = "msteams"

#: Authority a MULTI-tenant bot exchanges its credential against; a single-tenant
#: bot substitutes its own tenant id. Public because the settings handler verifies
#: a pasted credential with the same exchange, and a second copy of this literal
#: would let the save-time check and the boot path disagree about which authority
#: a blank tenant means.
TEAMS_MULTITENANT_AUTHORITY = "botframework.com"
# Refresh the outbound token this many seconds before it actually expires.
_TOKEN_REFRESH_SKEW = 60.0
_CONNECTOR_API_TIMEOUT = 15.0

# Clock-skew tolerance for inbound token exp/nbf. The Bot Framework
# authentication guidance specifies the industry-standard 5 minutes; without it
# a bot host whose clock trails the Connector's rejects otherwise-valid tokens
# and the channel goes silent with a 401 that looks like a bad credential.
_JWT_LEEWAY_SECS = 300

#: Floor between two kid-miss-driven JWKS refetches. Microsoft rotates Bot
#: Framework signing keys on the order of days and PyJWKClient caches the set for
#: five minutes, so a legitimate new kid is served within one interval; anything
#: faster than this is an unauthenticated caller spending our egress. Matches the
#: reference implementation's once-per-hour cap.
_JWKS_REFRESH_MIN_INTERVAL_SECS = 3600.0

# Connector statuses worth retrying. Teams' rate-limit guidance is explicitly
# WIDER than the usual 429-only rule: 412, 502 and 504 must be retried too, so
# a transient Connector hiccup does not surface as a lost answer.
_RETRY_STATUSES = frozenset({412, 429, 502, 504})
# Total attempts per outbound activity (1 initial + 2 retries). Small on
# purpose: a turn's reply is latency-sensitive and the caller surfaces failure.
_SEND_ATTEMPTS = 3
# Back-off ceiling for a Retry-After the Connector asks for.
_RETRY_AFTER_CAP_SECS = 10.0

# Replay window for inbound activity ids. The Connector legitimately redelivers
# an activity when the bot does not ack within its timeout, so a duplicate is
# expected traffic and must be dropped idempotently rather than refused. Bounded
# in BOTH directions -- count and age -- so a long-lived gateway cannot grow the
# set without limit.
_SEEN_ACTIVITY_MAX = 2048
_SEEN_ACTIVITY_TTL_SECS = 600.0

# Ceiling on concurrently-running inbound turn tasks. A valid-token burst would
# otherwise create an unbounded number of background turns, each holding a
# session semaphore and a provider process.
_MAX_INFLIGHT_TURNS = 32

#: Byte ceiling for ONE inbound attachment download, enforced on bytes actually
#: READ rather than on ``Content-Length`` -- a missing or understated header must
#: not let an unbounded body through. Matches the largest per-class cap in
#: ``messaging.attachments.IngestLimits`` (the document cap), so this bound never
#: refuses something the neutral pipeline would have accepted; the per-class cap
#: then applies to the file on disk.
TEAMS_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

#: Read granularity for a bounded attachment download. Large enough that a normal
#: file arrives in a handful of iterations, small enough that the over-cap check
#: fires long before an oversized body is written whole.
_DOWNLOAD_CHUNK_BYTES = 64 * 1024
_DOWNLOAD_TIMEOUT_SECS = 30.0
#: Redirect hops followed for an attachment fetch. Followed MANUALLY, one hop at a
#: time, so every hop is re-validated and the credential decision is retaken for
#: the host that actually serves the bytes.
_DOWNLOAD_MAX_REDIRECTS = 3

#: Hosts the bot's Connector bearer token may be sent to. The token is
#: credential-equivalent, and an inbound ``contentUrl`` is data from an activity
#: rather than a value we chose, so the allow-list is the whole control:
#: everything else is fetched ANONYMOUSLY, which can fail visibly but cannot leak
#: a secret. Exact host or a dot-anchored suffix -- never a bare substring, which
#: ``botframework.com.evil.example`` would satisfy.
#:
#: Microsoft does not document which host serves an inline image's ``contentUrl``,
#: so this is the set of Bot Framework / Teams media hosts a Connector token is
#: meaningfully scoped to. Widening it is a data change here; narrowing it costs
#: at most one failed authenticated attempt, because the anonymous fetch is tried
#: either way.
_TOKEN_HOSTS_EXACT = frozenset({"smba.trafficmanager.net"})
_TOKEN_HOST_SUFFIXES = (
    ".botframework.com",
    ".smba.trafficmanager.net",
    ".asm.skype.com",
    ".teams.microsoft.com",
)

#: Host names an attachment fetch refuses outright. An inbound URL that resolves
#: to the loopback interface or a link-local name would turn the gateway into an
#: SSRF proxy for whoever can get an activity past the allow-list.
_BLOCKED_DOWNLOAD_HOSTS = frozenset({"localhost", "localhost.localdomain"})
_BLOCKED_DOWNLOAD_SUFFIXES = (".localhost", ".local", ".internal")


def connector_host_allowed(service_url: str) -> bool:
    """Whether *service_url* is an https Bot Framework Connector endpoint.

    The single predicate behind BOTH decisions that hand the Connector token to a
    host: the outbound send (``_post_activity``) and the durable route store's
    load/record path. One function because the question is identical -- "may this
    host see our credential?" -- and two copies would drift.

    Deny-by-default, dot-anchored so ``botframework.com.evil.example`` cannot pass,
    and the port is pinned to 443 because a Connector endpoint has no other.
    """
    try:
        parsed = urllib.parse.urlsplit(service_url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or port not in (None, 443):
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return bool(host) and (host in _TOKEN_HOSTS_EXACT or host.endswith(_TOKEN_HOST_SUFFIXES))


async def resolve_addresses(host: str, port: int = 443) -> list[str]:
    """Resolve *host* to its IP strings, off the event loop.

    A single seam so the SSRF vet has exactly one place to be verified against, and
    a test can supply a record without touching the network. ``getaddrinfo`` blocks,
    which is why this goes through the loop's threaded resolver rather than the bare
    ``socket`` call.
    """
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    # Strip an IPv6 scope id ("fe80::1%eth0") -- ip_address rejects it.
    return [str(info[4][0]).split("%", 1)[0] for info in infos]


#: Vetted answers the download resolver will hand aiohttp, bounded so a long-lived
#: gateway that fetched from many hosts cannot grow it without limit. Every entry is an
#: address that already passed the vet, so an evicted one only costs a fresh lookup.
_PINNED_HOSTS_MAX = 64


class _VettedResolver(aiohttp.abc.AbstractResolver):
    """Hands aiohttp ONLY addresses the SSRF vet already approved.

    aiohttp resolves a URL's host itself, so vetting a name and then letting the client
    look it up again leaves a DNS-rebinding window: the first answer is public, the
    second -- inside the client, microseconds before the socket connects -- is
    ``169.254.169.254``. Pre-vetting narrows that window; it does not close it.

    This closes it by removing the second lookup. ``pin`` records what the vet resolved
    and approved, and ``resolve`` serves that and nothing else, so the address the socket
    dials IS the address that was vetted. An unpinned host is refused rather than
    resolved, which keeps the failure closed: a code path that reaches this session
    without going through the vet cannot fetch at all.

    aiohttp still sees the original URL, so TLS SNI and the ``Host`` header carry the real
    hostname -- certificate validation is unaffected, which connecting by IP would break.
    """

    def __init__(self) -> None:
        self._pinned: dict[str, list[str]] = {}

    def pin(self, host: str, addresses: list[str]) -> None:
        """Record the vetted addresses for *host*, replacing any earlier answer."""
        key = host.rstrip(".").lower()
        if key not in self._pinned and len(self._pinned) >= _PINNED_HOSTS_MAX:
            # Oldest first: dict preserves insertion order, and every entry is
            # equally valid, so which one goes is a cost question, not a safety one.
            self._pinned.pop(next(iter(self._pinned)))
        self._pinned[key] = list(addresses)

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[Any]:
        addresses = self._pinned.get(host.rstrip(".").lower())
        if not addresses:
            # OSError, not ValueError: aiohttp's connector expects a resolution
            # failure here and reports it as a connection error rather than a crash.
            raise OSError(f"refusing to resolve an unvetted host: {host!r}")
        results: list[Any] = []
        for address in addresses:
            parsed = ipaddress.ip_address(address)
            results.append(
                {
                    "hostname": host,
                    "host": address,
                    "port": port,
                    "family": socket.AF_INET6 if parsed.version == 6 else socket.AF_INET,
                    "proto": socket.IPPROTO_TCP,
                    "flags": 0,
                }
            )
        return results

    async def close(self) -> None:
        self._pinned.clear()


class TeamsAuthError(Exception):
    """Raised when an inbound Bot Framework JWT fails validation."""


class _AttachmentUnauthorized(Exception):
    """An attachment fetch was refused for want of (or because of) a credential.

    Private and narrow: it exists so the ONE place that knows which credential
    modes are worth trying can distinguish "this host rejected me" from a genuine
    transport failure, without treating every error as retryable.
    """


class TeamsSendError(Exception):
    """Raised when an outbound Connector activity could not be delivered.

    Outbound failure MUST be loud. The renderer and every proactive leg treat a
    returned id as proof of delivery, so a swallowed error makes the gateway
    record a message the user never received.
    """

    def __init__(self, detail: str, *, status: int = 0) -> None:
        super().__init__(detail)
        #: HTTP status when the Connector answered, else 0. Carried so a caller can
        #: tell "this conversation is gone" from "the network hiccuped" -- the first
        #: means the persisted route should be dropped, the second must not.
        self.status = status

    @property
    def conversation_is_gone(self) -> bool:
        """Whether the Connector says this conversation can never be delivered to.

        403 is what Teams answers once the user blocked the bot or removed the app;
        404 is a conversation id that no longer resolves. Both are permanent, and a
        route kept after either one turns every later cron result and mirror leg into
        a red badge with nothing to clear it. Deliberately NOT 401 (our credential)
        or 429/5xx (transient).
        """
        return self.status in (403, 404)


@dataclass
class TeamsInbound:
    """Normalized inbound message from a Bot Framework ``message`` activity."""

    conversation_id: str
    conversation_type: str  # "personal" | "channel" | "groupChat"
    service_url: str
    text: str
    user_email: str = ""
    aad_object_id: str = ""
    resolved_identity: str = ""
    """Which of the two identity forms the ALLOW-LIST authorized, set by the transport.

    Teams may send a UPN, an AAD object id, or both, and ``teams.allowed_emails`` accepts
    either form -- so the two are not interchangeable and the choice cannot be re-derived
    downstream from a fixed preference order. Deriving it twice is how the authorization
    gate and the session key came to disagree: a user allow-listed by object id was
    admitted on the object id and then keyed on the UPN, so their turns persisted under a
    session nobody had authorized and owner-only ``/sessions`` refused them.

    Empty on an inbound built outside :meth:`TeamsTransport.receive` (tests, and the
    route-only activities that never reach a turn); consumers fall back to
    email-then-object-id, which is the same answer whenever only one form is present.
    """
    activity_id: str = ""
    attachments: list[Any] = field(default_factory=list)
    """Raw ``activity.attachments`` entries, untouched.

    Carried raw because the two file-bearing shapes Teams uses need OPPOSITE
    fetch auth and the mapping that decides which is which lives in
    :mod:`kiro_crew.teams.attachments`, downstream of the transport's
    authorization gates. Nothing here is fetched, parsed as a path, or trusted:
    a message with attachments is still dropped unless it clears the personal-scope
    and allow-list gates first.
    """
    reply_to_id: str = ""
    """``activity.replyToId`` -- the id of the activity this one REPLIES to.

    For a card submit that is the id of the CARD, which is not ``activity_id`` (the
    submit has its own). Anything that has to address the card a press came from --
    replacing a picker with its outcome, or matching a press against the posting it
    belongs to -- must use this, or it addresses the press instead and the lookup
    always misses.
    """
    card_value: dict[str, Any] | None = None
    """An Adaptive Card ``Action.Submit`` payload, when this activity is a click.

    Teams delivers a card submit as an ordinary ``message`` activity carrying
    ``value`` and, normally, NO text -- so an inbound path that requires text
    silently discards every button press. Untrusted client input: it is only ever
    a lookup key into state this process already holds.
    """

    @property
    def is_card_action(self) -> bool:
        """Whether this activity is a card click rather than typed text."""
        return self.card_value is not None


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
        # Monotonic timestamp of the last kid-miss-driven JWKS refetch. -inf so the
        # first miss is served, not rate-limited.
        self._last_jwks_refresh: float = float("-inf")

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
        client = self._jwk_client
        try:
            kid = _jwt.get_unverified_header(token).get("kid") or ""
        except Exception as exc:
            raise TeamsAuthError(f"unreadable token header: {type(exc).__name__}") from exc
        # The kid lookup is done HERE rather than through
        # ``get_signing_key_from_jwt`` because that helper answers a MISS with an
        # unconditional ``refresh=True`` fetch and has no rate limit of its own: each
        # unauthenticated POST carrying a bogus kid would buy one outbound HTTPS GET
        # to the Bot Framework JWKS endpoint. The webhook route's request throttle
        # cannot absorb that -- it is skipped whenever an ``X-Forwarded-*`` header is
        # present, which is normal in two of the three documented topologies -- so
        # the damper has to sit next to the refetch it bounds. The reference
        # implementation caps the same refresh at once per hour.
        #
        # The non-refresh call below is already bounded by PyJWKClient's own JWK-set
        # cache lifespan, so a matching kid costs no network at all.
        try:
            match = client.match_kid(client.get_signing_keys(), kid)
        except Exception as exc:
            raise TeamsAuthError(f"JWKS unavailable: {type(exc).__name__}") from exc
        if match is not None:
            return match.key
        now = time.monotonic()
        if now - self._last_jwks_refresh < _JWKS_REFRESH_MIN_INTERVAL_SECS:
            raise TeamsAuthError("unknown signing key (JWKS refresh rate-limited)")
        self._last_jwks_refresh = now
        try:
            match = client.match_kid(client.get_signing_keys(refresh=True), kid)
        except Exception as exc:
            raise TeamsAuthError(f"JWKS refresh failed: {type(exc).__name__}") from exc
        if match is None:
            raise TeamsAuthError("no signing key matches the token's kid")
        return match.key

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
                leeway=_JWT_LEEWAY_SECS,
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
        self._tenant_id = tenant_id or TEAMS_MULTITENANT_AUTHORITY
        self._on_message = on_message
        self._validator = validator or JwtValidator(app_id)
        self._session: aiohttp.ClientSession | None = None
        # Attachment fetches get their OWN session so its connector can refuse to
        # resolve anything the SSRF vet did not approve; see _ensure_download_session.
        self._download_session: aiohttp.ClientSession | None = None
        self._resolver = _VettedResolver()
        self._session_lock = asyncio.Lock()
        # Outbound app-credential token cache.
        self._token: str = ""
        self._token_expiry: float = 0.0
        self._token_lock = asyncio.Lock()
        self._closed = False
        self.last_error: str = ""
        self.on_state_change: Callable[[bool, str], None] | None = None
        #: Set by the gateway to the transport's route recorder, so an install or a
        #: join can be learned without the client owning the durable store. A plain
        #: attribute for the same reason ``on_message`` is one: the transport is built
        #: after the client, and a constructor argument would need a cycle.
        self.on_route: Callable[[str, str, str, str], Awaitable[None]] | None = None
        # Live turn tasks -- prevent GC of in-flight background handlers, and
        # bound how many a burst may start (see _MAX_INFLIGHT_TURNS).
        self._handler_tasks: set[asyncio.Task[None]] = set()
        # activity id -> monotonic timestamp of first delivery, for replay drop.
        self._seen_activities: dict[str, float] = {}
        # Last published health, so _notify_state only reports transitions.
        # None (not True) initially: the first outcome is always worth publishing.
        self._healthy: bool | None = None
        self._healthy_reason: str = ""

    # ── Wiring / lifecycle ──

    def set_message_handler(self, on_message: Callable[[TeamsInbound], Awaitable[None]]) -> None:
        self._on_message = on_message

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    # ensure_ascii=False so the body on the wire is real UTF-8.
                    # aiohttp's default serializer escapes every non-ASCII
                    # codepoint to \\uXXXX -- 6 bytes each, so CJK doubles and an
                    # astral emoji (a surrogate PAIR, 12 bytes) triples. That is
                    # what turns a reply sitting inside TEAMS_MAX_TEXT into an
                    # activity over the Connector's byte ceiling, rejected as 413
                    # rather than chunked.
                    self._session = aiohttp.ClientSession(
                        json_serialize=partial(json.dumps, ensure_ascii=False)
                    )
        return self._session

    async def _ensure_download_session(self) -> aiohttp.ClientSession:
        """The session attachment fetches use, and only they.

        Separate from the Connector session on purpose: its connector resolves through
        :class:`_VettedResolver`, which refuses any host the SSRF vet did not approve. The
        Connector session must not share that -- its hosts are gated by
        :func:`connector_host_allowed`, a different and stricter rule, and routing them
        through a pin map would make an outbound activity depend on a download's state.
        """
        if self._download_session is None or self._download_session.closed:
            async with self._session_lock:
                if self._download_session is None or self._download_session.closed:
                    self._download_session = aiohttp.ClientSession(
                        connector=aiohttp.TCPConnector(resolver=self._resolver)
                    )
        return self._download_session

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
        handler_tasks = list(self._handler_tasks)
        for task in handler_tasks:
            task.cancel()
        if handler_tasks:
            await asyncio.gather(*handler_tasks, return_exceptions=True)
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
        if self._download_session is not None and not self._download_session.closed:
            # Closes the connector, which closes the resolver, which drops the pins.
            await self._download_session.close()
            self._download_session = None

    def _notify_state(self, connected: bool, error: str) -> None:
        """Publish a health transition to the dashboard badge.

        Deduped on the transition: a healthy channel sending hundreds of messages
        must not re-publish "connected" per send, and a channel failing repeatedly
        must not overwrite the FIRST reason with an identical later one. The first
        call always publishes, because the initial state is unknown rather than
        healthy.
        """
        if self._healthy is connected and error == self._healthy_reason:
            return
        self._healthy = connected
        self._healthy_reason = error
        if self.on_state_change is not None:
            try:
                self.on_state_change(connected, error)
            except Exception:
                logger.debug("Teams on_state_change observer raised", exc_info=True)

    def _is_replay(self, activity_id: str) -> bool:
        """Whether ``activity_id`` has already been delivered recently.

        Records unseen ids as a side effect. Evicts by age first, then by count
        (oldest first) so the map is bounded even if every id stays fresh.
        """
        if not activity_id:
            # An activity with no id cannot be deduped; treat it as new rather
            # than collapsing every id-less activity onto one another.
            return False
        now = time.monotonic()
        expired = [
            key
            for key, seen_at in self._seen_activities.items()
            if now - seen_at > _SEEN_ACTIVITY_TTL_SECS
        ]
        for key in expired:
            self._seen_activities.pop(key, None)
        if activity_id in self._seen_activities:
            return True
        self._seen_activities[activity_id] = now
        while len(self._seen_activities) > _SEEN_ACTIVITY_MAX:
            oldest = min(self._seen_activities, key=self._seen_activities.__getitem__)
            self._seen_activities.pop(oldest, None)
        return False

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
                logger.warning("Teams: failed to audit inbound auth denial (%s)", outcome)

        async def _audit_denied_off_loop(outcome: str) -> None:
            """Audit a refusal WITHOUT putting SEL's I/O on the gateway loop.

            This is the one audit an anonymous internet caller can drive at will, and
            SEL's first call initializes its log (a mkdir plus a file open) -- so the
            synchronous form stalls every other conversation and heartbeat on the loop
            for the duration, once per process and then once per write.
            """
            await asyncio.to_thread(_audit_denied, outcome)

        try:
            claims = await asyncio.to_thread(self._validator.verify, token)
        except TeamsAuthError:
            await _audit_denied_off_loop("denied_invalid_token")
            return web.Response(status=401, text="invalid bearer token")
        except Exception:
            logger.exception("Teams: unexpected error validating inbound token")
            await _audit_denied_off_loop("denied_token_validation_error")
            return web.Response(status=401, text="token validation error")

        # The dashboard route reads the body under a size cap and stashes the
        # parsed dict under TEAMS_ACTIVITY_REQUEST_KEY (aiohttp's Request is a
        # MutableMapping). Prefer it so the cap is enforced before any parse;
        # fall back to parsing here for callers that hand us a bare request.
        activity: Any = None
        try:
            activity = request.get(TEAMS_ACTIVITY_REQUEST_KEY)
        except Exception:
            activity = None
        if activity is None:
            try:
                activity = await request.json()
            except Exception:
                return web.Response(status=400, text="malformed activity payload")
        if not isinstance(activity, dict):
            return web.Response(status=400, text="malformed activity payload")

        # Shed rather than queue once too many turns are already in flight. Each
        # inbound turn holds a session semaphore and a provider process, so an
        # authenticated burst is a resource-exhaustion path even though every
        # message passed the JWT gate. 200 (not 429): the Connector would retry a
        # 429 and re-enter the same saturated state.
        #
        # Two shapes are EXEMPT, and must be, because both are how a saturated
        # gateway gets UNstuck -- shedding them means the only ways out are the first
        # casualties of the condition they resolve:
        #
        # * A card click. Teams delivers an Action.Submit as an ordinary `message`
        #   activity, so an approval press would otherwise be dropped, deadlocking
        #   every waiting prompt until each times out.
        # * `/stop`. It cancels a running turn, freeing a slot.
        #
        # Neither starts a turn, so neither costs a semaphore or a provider process.
        if not self._is_relief_activity(activity) and (
            len(self._handler_tasks) >= _MAX_INFLIGHT_TURNS
        ):
            _audit_denied("denied_inflight_limit")
            logger.warning(
                "Teams: %d turns already in flight; shedding inbound activity",
                len(self._handler_tasks),
            )
            return web.Response(status=200)

        task = asyncio.create_task(self._dispatch_activity(activity, claims))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)
        return web.Response(status=200)

    @staticmethod
    def _is_relief_activity(activity: dict[str, Any]) -> bool:
        """Whether this activity RELIEVES saturation rather than adding to it.

        Deliberately a shape test on the raw activity, not a call into the command
        parser: this runs on the ingress path before anything is normalized, and the
        set is small and closed. The stop aliases are the ones
        ``teams.commands.COMMAND_SPEC`` lists for the ``stop`` row.
        """
        if isinstance(activity.get("value"), dict):
            return True
        raw_attachments = activity.get("attachments")
        # The same quote-reply unwrapping the dispatch path does. A quote-replied
        # ``/stop`` has the QUOTED message on the front of ``activity.text``, so
        # reading that field alone would shed the one message that frees a slot.
        text = quoted_reply_text(raw_attachments if isinstance(raw_attachments, list) else [])
        if not text:
            raw_text = activity.get("text")
            text = raw_text if isinstance(raw_text, str) else ""
        first = text.strip().lower().split(maxsplit=1)
        return bool(first) and first[0] in STOP_ALIASES

    async def _note_route(
        self, conversation_id: str, conversation_type: str, service_url: str, identity: str
    ) -> None:
        """Hand an attested, promptless address to the transport's route store."""
        if self.on_route is None or not conversation_id or not service_url:
            return
        await self.on_route(conversation_id, conversation_type, service_url, identity)

    async def _dispatch_activity(self, activity: dict[str, Any], claims: dict[str, Any]) -> None:
        try:
            atype = activity.get("type")
            if atype not in ("message", *_ROUTE_ONLY_ACTIVITY_TYPES):
                # Only a message activity drives a turn; the two install/join types
                # below carry a routable address but no prompt. typing,
                # messageReaction and invoke carry neither.
                return
            from_obj = activity.get("from") or {}
            conversation = activity.get("conversation") or {}
            text = (activity.get("text") or "").strip()
            service_url = str(activity.get("serviceUrl", ""))
            aad_object_id = str(from_obj.get("aadObjectId", ""))
            activity_id = str(activity.get("id", ""))

            def _deny(outcome: str) -> None:
                sel().log_api_access(
                    caller=aad_object_id or "unknown",
                    operation="teams_client.dispatch",
                    outcome=outcome,
                    source="teams",
                )

            # POSITIVE channel check, not "not some other channel". An Azure Bot
            # resource has Web Chat enabled by DEFAULT and can carry Direct Line;
            # either produces a token this validator accepts as fully trusted (same
            # issuer, same audience, matching serviceurl claim) carrying an activity
            # that defaults to conversationType "personal". On Direct Line the
            # CLIENT composes the ``from`` object, so ``aadObjectId`` is not
            # channel-attested the way it is on msteams -- which would leave
            # ``teams.allowed_emails`` matching an identity the sender chose. The
            # reference implementation binds channelId the same way; PyJWKClient
            # gives us no access to the endorsements that would do it for us.
            channel_id = str(activity.get("channelId", "")).lower()
            if channel_id != _TEAMS_CHANNEL_ID:
                _deny("denied_foreign_channel")
                return
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
                _deny("denied_serviceurl_mismatch")
                return
            # Replay drop, AFTER attestation so an unattested activity can never
            # consume a dedupe slot. The Connector redelivers when the bot misses
            # its ack window, so a duplicate must be dropped idempotently -- but
            # audited, so a replay is attributable rather than indistinguishable
            # from an ordinary duplicate.
            if self._is_replay(activity_id):
                _deny("denied_replayed_activity")
                return
            if atype in _ROUTE_ONLY_ACTIVITY_TYPES:
                # An install or a personal-chat join carries the whole routable tuple
                # -- conversation id, serviceUrl, aadObjectId -- under exactly the
                # attestation checked above, and no prompt. Learning it here is what
                # makes a freshly-installed app a proactive target BEFORE the user
                # first types; the alternative (Connector conversation creation) needs
                # a round trip and its own permissions. The store still applies its
                # own allow-list gate, so this records nothing it would not have
                # recorded from the first message.
                await self._note_route(
                    str(conversation.get("id", "")),
                    _activity_conversation_type(conversation),
                    service_url,
                    _activity_user_email(from_obj) or aad_object_id,
                )
                return
            raw_value = activity.get("value")
            raw_attachments = activity.get("attachments")
            attachments = list(raw_attachments) if isinstance(raw_attachments, list) else []
            # Right-click -> Reply in a 1:1 chat prepends the QUOTED message to
            # ``activity.text``, so the field everything downstream reads is the
            # previous message with the user's own words tacked on the end. Prefer the
            # clean text when the body attachment carries a Reply blockquote; the
            # helper returns "" for any shape it does not recognise, so an unfamiliar
            # client degrades to ``activity.text`` rather than losing the message.
            text = quoted_reply_text(attachments) or text
            inbound = TeamsInbound(
                conversation_id=str(conversation.get("id", "")),
                conversation_type=_activity_conversation_type(conversation),
                service_url=service_url,
                text=text,
                user_email=_activity_user_email(from_obj),
                aad_object_id=aad_object_id,
                activity_id=activity_id,
                reply_to_id=str(activity.get("replyToId", "")),
                attachments=attachments,
                card_value=raw_value if isinstance(raw_value, dict) else None,
            )
            # An Adaptive Card submit normally carries a payload and NO text, so
            # requiring text here discards every button press -- and a file upload
            # carries attachments and no text, so requiring text discards those
            # too. Drop only when the activity says nothing at all.
            if not inbound.text and not inbound.is_card_action and not inbound.attachments:
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
        """Send a text message to a conversation; return the activity id.

        Raises :class:`TeamsSendError` when delivery failed. Returning ``None``
        means the message was accepted but Teams reported no id -- which happens
        when one activity carries both text and an attachment, since Teams splits
        it and withholds the id. Callers that need an id to edit later must treat
        ``None`` as "delivered, not editable", never as failure.
        """
        result = await self._post_activity(
            conversation_id, service_url, self._message_activity(content)
        )
        activity_id = result.get("id") if isinstance(result, dict) else None
        return str(activity_id) if activity_id else None

    async def update_message(
        self, conversation_id: str, activity_id: str, content: str, service_url: str
    ) -> bool:
        """Rewrite a previously-sent bot activity in place.

        Teams supports ``PUT .../activities/{activityId}`` for the bot's OWN
        activities only (a user's message can never be updated), which is what
        makes an edited progress message and an in-place queue receipt possible
        here where WeCom and Weixin cannot have them.

        Returns ``False`` when the edit could not be applied, so a caller can
        degrade to posting a fresh message. An edit is cosmetic -- a failed one
        must not abort the turn -- so this reports rather than raises.
        """
        if not activity_id:
            return False
        try:
            await self._post_activity(
                conversation_id,
                service_url,
                self._message_activity(content),
                activity_id=activity_id,
                method="PUT",
            )
            return True
        except TeamsSendError:
            logger.debug("Teams: activity update failed; caller may repost", exc_info=True)
            return False

    async def send_typing(self, conversation_id: str, service_url: str) -> None:
        """Post a typing indicator (best-effort).

        Deliberately swallows failure: the indicator is a progress hint, and a
        turn whose answer lands fine must not fail because a cosmetic typing
        activity was throttled.
        """
        try:
            await self._post_activity(conversation_id, service_url, {"type": "typing"})
        except TeamsSendError:
            logger.debug("Teams: typing indicator failed", exc_info=True)

    async def send_card(
        self, conversation_id: str, card: dict[str, Any], service_url: str
    ) -> str | None:
        """Post an Adaptive Card attachment; return its activity id.

        No ``text`` is set alongside the attachment on purpose: Teams SPLITS an
        activity that carries both, and withholds the resulting id -- which would
        leave the card unaddressable for the update that settles it.
        """
        result = await self._post_activity(
            conversation_id, service_url, {"type": "message", "attachments": [card]}
        )
        activity_id = result.get("id") if isinstance(result, dict) else None
        return str(activity_id) if activity_id else None

    async def update_card(
        self, conversation_id: str, activity_id: str, card: dict[str, Any], service_url: str
    ) -> bool:
        """Replace a previously-posted card in place. False when not applied."""
        if not activity_id:
            return False
        try:
            await self._post_activity(
                conversation_id,
                service_url,
                {"type": "message", "attachments": [card]},
                activity_id=activity_id,
                method="PUT",
            )
            return True
        except TeamsSendError:
            logger.debug("Teams: card update failed", exc_info=True)
            return False

    async def send_inline_image(
        self, conversation_id: str, attachment: dict[str, Any], service_url: str
    ) -> str | None:
        """Post ONE inline image as its own activity; return its activity id.

        Teams renders an image with no hosting and no consent round trip when the
        ``Attachment``'s ``contentUrl`` is a ``data:image/...;base64,`` URI, which
        is what makes an agent-produced chart deliverable here at all. Built by
        :func:`kiro_crew.teams.attachments.inline_image_attachment`.

        No ``text`` rides along, for the same reason ``send_card`` sends none:
        Teams SPLITS an activity carrying both text and an attachment and withholds
        the resulting id, so a combined send would land as two messages the caller
        can no longer address. Raises :class:`TeamsSendError` on failure -- the
        caller must be able to tell the user the picture did not arrive.
        """
        result = await self._post_activity(
            conversation_id, service_url, {"type": "message", "attachments": [attachment]}
        )
        activity_id = result.get("id") if isinstance(result, dict) else None
        return str(activity_id) if activity_id else None

    # ── Inbound attachment fetch ──

    @staticmethod
    def _token_allowed_for(host: str) -> bool:
        """Whether the bot's Connector token may be sent to *host*.

        Dot-anchored suffix match, never a substring: ``botframework.com.evil.test``
        must not pass. Deny-by-default -- an unknown host is fetched anonymously.
        """
        return host in _TOKEN_HOSTS_EXACT or host.endswith(_TOKEN_HOST_SUFFIXES)

    @staticmethod
    def _vet_download_url(url: str) -> str:
        """Return *url*'s host, or raise when its NAME already disqualifies it.

        Applied to EVERY redirect hop, so host validation stays true for the bytes
        finally written. Refuses a non-https scheme (an attachment fetch must not
        be downgradeable), a non-443 port, an IP literal, and the loopback /
        link-local name space -- an inbound URL is activity data, and following it
        blindly would make the gateway an SSRF proxy.

        Name-only. What a name RESOLVES to is checked by
        :meth:`_vet_resolved_address`, because that needs the event loop.
        """
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid Teams attachment URL") from exc
        # Strip the FQDN root dot before every comparison below. "localhost." is
        # the same host as "localhost" to every resolver, so a blocklist that
        # compares the raw name refuses one spelling and admits the other.
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host or port not in (None, 443):
            raise ValueError("refusing non-https Teams attachment URL")
        if host in _BLOCKED_DOWNLOAD_HOSTS or host.endswith(_BLOCKED_DOWNLOAD_SUFFIXES):
            raise ValueError("refusing local-network Teams attachment URL")
        # An IP literal never names a Microsoft-operated host and is the shape an
        # SSRF probe takes; ``ip_address`` accepts a bracket-stripped IPv6 too.
        try:
            ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            return host
        raise ValueError("refusing IP-literal Teams attachment URL")

    @staticmethod
    async def _vet_resolved_address(host: str, *, port: int = 443) -> list[str]:
        """Refuse a host that resolves into a private or otherwise reserved range.

        The name blocklist above cannot see this: ``metadata.google.internal`` is
        caught by its suffix, but any public name an attacker controls can be
        pointed at ``127.0.0.1`` or ``169.254.169.254``, and a nip.io-style wildcard
        needs no control at all. Because a fetched body is written to a temp file and
        a text/document body is injected into the prompt, this is a READ primitive,
        not a blind one -- the model would summarize an internal endpoint back into
        the chat.

        Refuses when ANY resolved address is disallowed, not merely the one that
        would be connected to: stricter is the safe direction, and the ordering
        aiohttp picks is not ours to predict. Resolution failure also refuses -- the
        fetch could not have succeeded anyway.

        Returns the approved addresses so the caller can PIN them: aiohttp resolves a
        name again when it connects, and a record that changes in between is DNS
        rebinding, which a check on the name alone cannot see. The download session's
        :class:`_VettedResolver` serves exactly this list, so the socket dials what was
        vetted and there is no second lookup to poison.
        """
        try:
            resolved_addresses = await resolve_addresses(host, port)
        except OSError as exc:
            raise ValueError("refusing unresolvable Teams attachment host") from exc
        for resolved in resolved_addresses:
            # `link_unfurl`'s vet, not a local flag list. The category flags this
            # used to enumerate approved two ranges that are plainly not public:
            # `100.64.0.0/10` (RFC 6598 shared space -- what a Tailscale tailnet
            # and most carrier NAT hand out, which CPython's `is_private` table
            # omits and only `is_global` rejects) and `fec0::/10` (deprecated IPv6
            # site-local, which reports `is_global=True`). It also evaluated the
            # ipv4-mapped and 6to4 encodings as written, so `::ffff:127.0.0.1`
            # passed a check whose whole purpose was to refuse loopback. That
            # module already owns this decision for link unfurling and for the
            # meetings calendar fetch, and its
            # `test_vet_rejects_every_special_purpose_range` pins the refusal set
            # against a table of IANA special-purpose prefixes -- so the next gap
            # is found by the suite instead of by a reviewer, which a second
            # implementation here would not inherit.
            #
            # It also subsumes the unparseable-address guard: it fails CLOSED on a
            # literal it cannot read, which is the same refusal this reached the
            # long way round via `ipaddress.ip_address`.
            if link_unfurl.address_is_not_public(resolved):
                raise ValueError("refusing local-network Teams attachment URL")
        return resolved_addresses

    async def download_inbound_file(
        self,
        url: str,
        dest: str,
        *,
        authenticated: bool = False,
        max_bytes: int = TEAMS_MAX_DOWNLOAD_BYTES,
    ) -> None:
        """Stream one inbound attachment to *dest*, bounded and off-loop.

        ``authenticated`` is the caller's assertion that this URL is a Teams-hosted
        inline-image ``contentUrl``, as opposed to the ``content.downloadUrl`` of a
        personal-chat file upload, which Microsoft documents as fetchable with a
        plain GET and which MUST never see the bot's token: that token is
        credential-equivalent, and the host is not guaranteed to be one Microsoft
        operates.

        Microsoft's guidance for the inline-image case is self-contradictory -- the
        current page says the SDK handles authentication and its samples send no
        header, while the previous revision of the same page and the shipped sample
        attach the Connector bearer token -- and the host is not documented at all.
        So the credential decision fails closed on the host: the token is offered
        only to a recognized Bot Framework host, and the anonymous fetch is tried
        as well, in that order. Both orders are documented as correct somewhere, and
        the cost of trying both is one extra request on a 401.

        The cap is enforced on bytes actually READ, so a lying or absent
        ``Content-Length`` cannot smuggle an unbounded body through. Every write
        goes through a worker thread because ``TMPDIR`` is not guaranteed to be
        local disk, and a stall there would freeze the gateway's single loop.
        """
        # Vet before deciding anything: an unfetchable URL must be refused whether
        # or not a credential was in play.
        host = self._vet_download_url(url)
        offer_token = authenticated and self._token_allowed_for(host)
        last: Exception | None = None
        for use_token in (True, False) if offer_token else (False,):
            try:
                await self._stream_attachment(url, dest, use_token=use_token, max_bytes=max_bytes)
                return
            except _AttachmentUnauthorized as exc:
                last = exc
        raise ValueError(f"Teams refused the attachment fetch ({last})")

    async def _stream_attachment(
        self, url: str, dest: str, *, use_token: bool, max_bytes: int
    ) -> None:
        """Fetch *url* to *dest*, following redirects with per-hop revalidation.

        Each hop is re-vetted and the credential decision is RETAKEN for the host
        that will actually serve the bytes, so a redirect off a Bot Framework host
        cannot carry the token with it.
        """
        session = await self._ensure_download_session()
        current = url
        for _hop in range(_DOWNLOAD_MAX_REDIRECTS + 1):
            host = self._vet_download_url(current)
            # Pin what the vet approved, so the socket dials that and aiohttp performs no
            # second lookup a rebinding record could answer. Re-done per hop: a redirect
            # is a new host with its own vet and its own pin.
            self._resolver.pin(host, await self._vet_resolved_address(host))
            headers: dict[str, str] = {}
            if use_token and self._token_allowed_for(host):
                headers["Authorization"] = f"Bearer {await self._get_app_token()}"
            async with session.get(
                current,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT_SECS),
                allow_redirects=False,
            ) as resp:
                if 300 <= resp.status < 400:
                    location = resp.headers.get("Location", "")
                    if not location:
                        raise ValueError("Teams attachment redirect without a location")
                    current = urllib.parse.urljoin(current, location)
                    continue
                if resp.status in (401, 403):
                    raise _AttachmentUnauthorized(f"HTTP {resp.status}")
                resp.raise_for_status()
                written = 0
                fh = await asyncio.to_thread(open, dest, "wb")
                try:
                    async for chunk in resp.content.iter_chunked(_DOWNLOAD_CHUNK_BYTES):
                        written += len(chunk)
                        if written > max_bytes:
                            raise ValueError(f"Teams attachment exceeds {max_bytes} bytes")
                        await asyncio.to_thread(fh.write, chunk)
                finally:
                    await asyncio.to_thread(fh.close)
                return
        raise ValueError("too many redirects for a Teams attachment URL")

    @staticmethod
    def _message_activity(content: str) -> dict[str, Any]:
        """Build a message activity carrying Teams-flavored markdown text."""
        return {"type": "message", "text": content or "…", "textFormat": "markdown"}

    async def _post_activity(
        self,
        conversation_id: str,
        service_url: str,
        activity: dict[str, Any],
        *,
        activity_id: str = "",
        method: str = "POST",
    ) -> dict[str, Any] | None:
        """Send/replace one activity, retrying the Connector's transient set.

        Raises :class:`TeamsSendError` on any failure. Every caller treats a
        successful return as proof of delivery -- the renderer records the answer
        as sent, and a proactive leg reports it delivered -- so swallowing the
        error here is what makes the gateway lie about a message the user never
        saw. Callers that legitimately tolerate failure (typing, an in-place
        edit) catch it explicitly at their own call site.
        """
        if not service_url or not conversation_id:
            raise TeamsSendError("missing service_url or conversation_id")
        if not connector_host_allowed(service_url):
            # The ONE chokepoint where the app bearer token is attached, so this is
            # where the destination has to be proven -- not at whichever layer
            # happened to supply the URL.
            #
            # https alone is NOT enough. The inbound path binds serviceUrl to the
            # JWT's own `serviceurl` claim precisely so a replayed activity cannot
            # redirect this credential, but that attestation does not survive
            # PERSISTENCE: the durable route store is a file under the data home,
            # and an injected agent with write access could put
            # `https://attacker.example` in it, wait for a restart, and collect the
            # Connector token from the first proactive send. A real Teams serviceUrl
            # is always a Microsoft-operated Connector host, so requiring one costs
            # nothing legitimate and closes every supplier of a bad URL at once --
            # the store, a replayed activity, a hand-edited config.
            raise TeamsSendError("refusing a serviceUrl outside the Connector hosts")
        base = service_url.rstrip("/")
        url = f"{base}/v3/conversations/{conversation_id}/activities"
        if activity_id:
            url = f"{url}/{activity_id}"
        last_detail = ""
        try:
            session = await self._ensure_session()
            for attempt in range(_SEND_ATTEMPTS):
                # Re-read the token each attempt: a retry may cross the refresh
                # boundary, and a 401 on attempt two would otherwise be permanent.
                token = await self._get_app_token()
                async with session.request(
                    method,
                    url,
                    json=activity,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=_CONNECTOR_API_TIMEOUT),
                ) as resp:
                    if resp.status in _RETRY_STATUSES and attempt < _SEND_ATTEMPTS - 1:
                        await asyncio.sleep(
                            self._retry_delay(resp.headers.get("Retry-After"), attempt)
                        )
                        last_detail = f"HTTP {resp.status}"
                        continue
                    if resp.status >= 400:
                        # Status only. The Connector's body echoes request content
                        # and correlation ids, and this message reaches the
                        # dashboard verbatim via last_error; the full body goes to
                        # the debug log instead.
                        logger.debug(
                            "Teams: connector rejected %s %s with HTTP %s: %s",
                            method,
                            "activity",
                            resp.status,
                            (await resp.text())[:500],
                        )
                        raise TeamsSendError(f"HTTP {resp.status}", status=resp.status)
                    # A delivered activity proves the credential and the route are
                    # good, so it clears a stale failure badge. Without this edge
                    # the dashboard stays red for the rest of the process after
                    # one transient send error.
                    self._record_send_success()
                    try:
                        payload = await resp.json()
                    except Exception:
                        # A 2xx with a non-JSON body is a successful delivery we
                        # simply cannot read an id out of.
                        return {}
                    return payload if isinstance(payload, dict) else {}
            raise TeamsSendError(f"retries exhausted ({last_detail or 'unknown'})")
        except TeamsSendError as exc:
            self._record_send_failure(exc)
            raise
        except Exception as exc:
            err = TeamsSendError(f"{type(exc).__name__}")
            self._record_send_failure(err)
            raise err from exc

    @staticmethod
    def _retry_delay(retry_after: str | None, attempt: int) -> float:
        """Seconds to wait before the next attempt.

        Honors the Connector's ``Retry-After`` when it sends one -- Teams'
        rate-limit guidance specifies it and the value is authoritative -- and
        otherwise backs off exponentially. Deliberately unjittered: the retry
        budget is 2 attempts against a per-conversation limit, so there is no
        thundering herd to spread, and a deterministic delay keeps the wait
        assertable in tests.
        """
        if retry_after:
            try:
                return min(max(float(retry_after), 0.5), _RETRY_AFTER_CAP_SECS)
            except (TypeError, ValueError):
                pass
        return min(0.5 * (2**attempt), _RETRY_AFTER_CAP_SECS)

    def _record_send_success(self) -> None:
        """Clear a stale outbound-failure badge after a delivered activity."""
        self.last_error = ""
        self._notify_state(True, "")

    def _record_send_failure(self, exc: "TeamsSendError") -> None:
        """Record an outbound failure on the status badge.

        Only the short reason reaches ``last_error`` -- a status line, a refusal
        cause, or an exception class name. ``_post_activity`` deliberately keeps
        the Connector's response body out of the exception for this reason: the
        dashboard surfaces this string verbatim.
        """
        self.last_error = f"send failed: {exc}"[:200]
        logger.warning("Teams: outbound send failed: %s", exc)
        self._notify_state(False, self.last_error)
