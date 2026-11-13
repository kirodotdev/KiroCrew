"""The control plane calls a relay viewer makes: list the Crews, mint an access token.

``ListCrewEndpoints`` lists the Crews this person can open through the relay, and
``CreateCrewAccessToken`` mints the short-lived token that opens one of them. Both
are awsJson 1.0 calls to the bearer service, the same shape as the host's lease
call (:mod:`kiro_crew.relay_client.lease`), and both carry the person's sign-in.

So each call is made the way the lease call is: no credential is read from the
environment (``trust_env=False``), a redirect is never followed, a call takes at
most 15 seconds and an answer is read to 64 KiB at most. Host names resolve on
:func:`~kiro_crew.executors.relay_viewer_executor`, not the default executor. Nothing logged here
carries the sign-in, the access token, a body, or the service's message text.

Every failure is a :class:`CpsRefused` with one of the typed codes below, so
callers show a plain reason without reading the service's own words.
"""

from __future__ import annotations

import asyncio
import json
import logging
import string
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from urllib.parse import urlsplit

import aiohttp

from kiro_crew.auth.login.control_plane import cps_headers
from kiro_crew.executor_resolver import executor_connector
from kiro_crew.executors import relay_viewer_executor
from kiro_crew.relay_client.connection import env_proxy_for
from kiro_crew.relay_client.lease import (
    ANSWER_MAX_BYTES,
    CALL_TIMEOUT_SECONDS,
    SERVICE_NAME,
    lease_url_problem,
    parse_time,
    refusal_of,
)
from kiro_crew.relay_client.protocol import ENDPOINT_ID_RE, crew_url_problem, is_display_name

logger = logging.getLogger(__name__)

LIST_TARGET = f"{SERVICE_NAME}.ListCrewEndpoints"
MINT_TARGET = f"{SERVICE_NAME}.CreateCrewAccessToken"
#: Pages followed at most. The service grants at most 500 team Crews at 50 a page.
MAX_PAGES = 20
NEXT_TOKEN_MAX_CHARS = 4096
RETRY_AFTER_MIN_SECONDS = 1.0
RETRY_AFTER_MAX_SECONDS = 60.0
#: The earliest ``lastSeenAt`` kept, in epoch seconds. A value further ahead of this clock than
#: LAST_SEEN_MAX_AHEAD_SECONDS is dropped too.
LAST_SEEN_EARLIEST = 1_577_836_800.0
LAST_SEEN_MAX_AHEAD_SECONDS = 300.0
#: CPS's ``x-amzn-RequestId`` is kept only when it is this short and made of these
#: characters, so a log line or the dashboard never shows anything else from it.
REQUEST_ID_MAX_CHARS = 64
_REQUEST_ID_CHARS = frozenset(string.ascii_letters + string.digits + "-")

# ── typed refusal codes ──
SIGNED_OUT = "SIGNED_OUT"
NOT_SET_UP = "NOT_SET_UP"
CREW_ACCESS_NOT_GRANTED = "CREW_ACCESS_NOT_GRANTED"
REMOTE_ACCESS_DISABLED = "REMOTE_ACCESS_DISABLED"
ACCESS_DENIED = "ACCESS_DENIED"
CREW_OFFLINE = "CREW_OFFLINE"
THROTTLED = "THROTTLED"
INVALID_REQUEST = "INVALID_REQUEST"
SERVICE_ERROR = "SERVICE_ERROR"
UNREACHABLE = "UNREACHABLE"
BAD_ANSWER = "BAD_ANSWER"

#: An ``AccessDeniedException`` reason, with case and underscores dropped, to its code.
_REASONS = {
    "crewaccessnotgranted": CREW_ACCESS_NOT_GRANTED,
    "remoteaccessdisabled": REMOTE_ACCESS_DISABLED,
}
_TYPES = {
    "CrewOfflineException": CREW_OFFLINE,
    "ThrottlingException": THROTTLED,
    "ValidationException": INVALID_REQUEST,
    "InternalServerException": SERVICE_ERROR,
}
CREW_TYPES = frozenset({"PERSONAL", "TEAM"})
PRESENCES = frozenset({"CONNECTED", "HEARTBEAT_LATE", "NOT_CONNECTED", "NEVER_CONNECTED"})
RELAY_STATUSES = frozenset({"CONNECTED", "NOT_CONNECTED"})
UNKNOWN = "UNKNOWN"


class CpsRefused(Exception):
    """A call did not give a usable answer. The message never holds a credential."""

    def __init__(
        self,
        code: str,
        why: str,
        *,
        status: Optional[int] = None,
        retry_after: Optional[float] = None,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(why)
        self.code = code
        self.status = status
        self.retry_after = retry_after
        #: CPS's request ID for the answer that was refused, when it sent a usable
        #: one. Not a secret: it names the request so a failure can be reported.
        self.request_id = request_id


@dataclass(frozen=True)
class CrewEndpoint:
    endpoint_id: str
    crew_url: str
    name: str
    crew_type: str
    presence: str
    relay_connection_status: str
    #: The Crew's last lease renewal in epoch seconds, or ``None`` when absent or not a sane time.
    last_seen_at: Optional[float] = None


@dataclass(frozen=True)
class CrewAccessToken:
    access_token: str = field(repr=False)
    expires_at: float
    renew_in_seconds: Optional[float]
    server_date: Optional[float]


@dataclass(frozen=True)
class _Answer:
    status: int
    payload: Any
    date: Optional[float]
    error_type: Optional[str]
    retry_after: Optional[str]
    request_id: Optional[str] = None


async def list_crew_endpoints(
    cps_url: str, bearer: str, *, crew_domain: str = "", token_type: str = ""
) -> tuple[CrewEndpoint, ...]:
    """Every Crew this person can open, in the service's order, checked entry by entry.

    *crew_domain* is the relay's domain, lower case. When set, an entry is kept
    only when its host is exactly ``<endpointId>.<crew_domain>`` with no port.
    Empty keeps the host check that has no domain. *token_type* is
    ``EXTERNAL_IDP`` for an external IdP sign-in, else empty.
    """
    crews: dict[str, CrewEndpoint] = {}
    dropped = 0
    body: dict[str, Any] = {}
    for _ in range(MAX_PAGES):
        answer = await _call(cps_url, bearer, LIST_TARGET, body, token_type)
        payload = answer.payload
        if not isinstance(payload, dict) or not isinstance(payload.get("crewEndpoints", []), list):
            raise CpsRefused(
                BAD_ANSWER,
                "the Crew list answer is not a list",
                status=200,
                request_id=answer.request_id,
            )
        if payload.get("remoteAccessAllowed") is False:
            raise CpsRefused(
                REMOTE_ACCESS_DISABLED,
                "remote access is turned off",
                status=200,
                request_id=answer.request_id,
            )
        for raw in payload.get("crewEndpoints", []):
            crew = _crew_endpoint(raw, crew_domain)
            if crew is None or crew.endpoint_id in crews:
                dropped += 1
            else:
                crews[crew.endpoint_id] = crew
        token = payload.get("nextToken")
        if not (isinstance(token, str) and 0 < len(token) <= NEXT_TOKEN_MAX_CHARS):
            break
        body = {"nextToken": token}
    else:
        logger.warning(
            "relay viewer: the Crew list has more than %d pages; showing those", MAX_PAGES
        )
    if dropped:
        logger.warning(
            "relay viewer: left %d unusable Crew list entries out%s",
            dropped,
            (
                "; tunnel.relay_viewer_relay_domain is set, so only addresses under it are kept"
                if crew_domain
                else ""
            ),
        )
    return tuple(crews.values())


async def create_crew_access_token(
    cps_url: str, bearer: str, endpoint_id: str, *, token_type: str = ""
) -> CrewAccessToken:
    """A fresh access token for the Crew *endpoint_id*.

    *token_type* is ``EXTERNAL_IDP`` for an external IdP sign-in, else empty.
    """
    if not isinstance(endpoint_id, str) or not ENDPOINT_ID_RE.match(endpoint_id):
        raise CpsRefused(INVALID_REQUEST, "not a Crew endpoint ID")
    answer = await _call(cps_url, bearer, MINT_TARGET, {"endpointId": endpoint_id}, token_type)
    payload = answer.payload if isinstance(answer.payload, dict) else {}
    token = payload.get("accessToken")
    expires = parse_time(payload.get("expiresAt"))
    renew = payload.get("renewInSeconds")
    renew_ok = renew is None or (
        isinstance(renew, (int, float)) and not isinstance(renew, bool) and renew > 0
    )
    if not (isinstance(token, str) and token) or expires is None or not renew_ok:
        raise CpsRefused(
            BAD_ANSWER,
            "the access token answer is missing a field",
            status=200,
            request_id=answer.request_id,
        )
    logger.info("relay viewer: minted access for %s", endpoint_id)
    return CrewAccessToken(
        access_token=token,
        expires_at=expires,
        renew_in_seconds=None if renew is None else float(renew),
        server_date=answer.date,
    )


def _crew_endpoint(raw: Any, crew_domain: str) -> Optional[CrewEndpoint]:
    if not isinstance(raw, dict):
        return None
    endpoint, url, name = raw.get("endpointId"), raw.get("crewUrl"), raw.get("name")
    if not (isinstance(endpoint, str) and ENDPOINT_ID_RE.match(endpoint)):
        return None
    if not isinstance(url, str) or crew_url_problem(url) is not None:
        return None
    # The token goes only to this origin, so it must be this endpoint's own name.
    # With the relay's domain set, it must be exactly that name under the domain,
    # on the default port.
    # shortcut: one relay domain, change to a list when the viewer must reach a second relay region.
    parts = urlsplit(url)
    if crew_domain:
        if parts.port is not None or parts.hostname != f"{endpoint}.{crew_domain}":
            return None
    elif not (parts.hostname or "").startswith(f"{endpoint}."):
        return None
    crew_type = raw.get("crewType")
    if not is_display_name(name) or not isinstance(crew_type, str) or crew_type not in CREW_TYPES:
        return None
    # A list or dict is unhashable, so only a string is looked up.
    presence, relay = (
        value if isinstance(value, str) else None
        for value in (raw.get("presence"), raw.get("relayConnectionStatus"))
    )
    return CrewEndpoint(
        endpoint_id=endpoint,
        crew_url=url,
        name=name,
        crew_type=crew_type,
        presence=presence if presence in PRESENCES else UNKNOWN,
        relay_connection_status=relay if relay in RELAY_STATUSES else UNKNOWN,
        last_seen_at=_last_seen(raw.get("lastSeenAt")),
    )


def _last_seen(value: Any) -> Optional[float]:
    """``lastSeenAt`` as epoch seconds, or ``None`` when it is missing or not a sane time.

    A bad value only loses the "last seen" words, so it never drops the entry.
    """
    seen = parse_time(value)
    if seen is None or not (
        LAST_SEEN_EARLIEST <= seen <= time.time() + LAST_SEEN_MAX_AHEAD_SECONDS
    ):
        return None
    return seen


async def _call(
    cps_url: str, bearer: str, target: str, body: dict, token_type: str = ""
) -> _Answer:
    """One awsJson call; a 200 answer, or :class:`CpsRefused`."""
    operation = target.rsplit(".", 1)[-1]
    if not bearer:
        raise CpsRefused(SIGNED_OUT, "not signed in to Kiro")
    if not cps_url or lease_url_problem(cps_url) is not None:
        raise CpsRefused(NOT_SET_UP, "the control plane URL is not set up")
    answer = await _post(cps_url, bearer, target, body, token_type)
    if answer.status == 200:
        return answer
    code, retry_after = _refusal_code(answer)
    logger.warning(
        "relay viewer: %s refused (HTTP %d, %s, request %s)",
        operation,
        answer.status,
        code,
        answer.request_id or "-",
    )
    raise CpsRefused(
        code,
        f"{operation} refused ({code})",
        status=answer.status,
        retry_after=retry_after,
        request_id=answer.request_id,
    )


def _refusal_code(answer: _Answer) -> tuple[str, Optional[float]]:
    status = answer.status
    kind, reason = refusal_of(answer.payload, answer.error_type)
    if status == 401:
        return SIGNED_OUT, None
    if 300 <= status < 400:
        return UNREACHABLE, None
    if kind == "AccessDeniedException" or (kind is None and status == 403):
        return _REASONS.get((reason or "").replace("_", "").lower(), ACCESS_DENIED), None
    code = _TYPES.get(kind or "") or (THROTTLED if status == 429 else SERVICE_ERROR)
    if code != THROTTLED:
        return code, None
    try:
        wait = float(answer.retry_after or "")
    except ValueError:
        return code, None
    if wait != wait:  # NaN
        return code, None
    return code, min(max(wait, RETRY_AFTER_MIN_SECONDS), RETRY_AFTER_MAX_SECONDS)


async def _post(url: str, bearer: str, target: str, body: dict, token_type: str) -> _Answer:
    headers = cps_headers(target, bearer, token_type=token_type)
    try:
        # trust_env=False: no netrc or other credential from the environment. A
        # proxy the environment names is still used, as for the lease call.
        connector = executor_connector(relay_viewer_executor())
        async with aiohttp.ClientSession(trust_env=False, connector=connector) as session:
            async with session.post(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                allow_redirects=False,
                proxy=env_proxy_for(url),
                timeout=aiohttp.ClientTimeout(total=CALL_TIMEOUT_SECONDS),
            ) as response:
                status = response.status
                date = _http_date(response.headers.get("Date"))
                header_type = response.headers.get("X-Amzn-ErrorType")
                retry_after = response.headers.get("Retry-After")
                request_id = _request_id(response.headers.get("x-amzn-RequestId"))
                raw = await _read_capped(response.content, status, request_id)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        raise CpsRefused(
            UNREACHABLE, f"the control plane could not be reached ({type(exc).__name__})"
        ) from None
    try:
        payload = json.loads(raw) if raw else None
    except (ValueError, RecursionError):
        payload = None
    if status == 200 and payload is None:
        raise CpsRefused(
            BAD_ANSWER,
            "the control plane's answer is not JSON",
            status=status,
            request_id=request_id,
        )
    return _Answer(status, payload, date, header_type, retry_after, request_id)


def _request_id(value: Optional[str]) -> Optional[str]:
    """*value* when it is 1 to 64 ASCII letters, digits or ``-``, else ``None``.

    The header is the service's, so anything else (a control character, a long
    string) is dropped rather than logged or shown.
    """
    if not value or len(value) > REQUEST_ID_MAX_CHARS:
        return None
    return value if all(ch in _REQUEST_ID_CHARS for ch in value) else None


async def _read_capped(
    stream: aiohttp.StreamReader, status: int, request_id: Optional[str] = None
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in stream.iter_chunked(8192):
        total += len(chunk)
        if total > ANSWER_MAX_BYTES:
            raise CpsRefused(
                BAD_ANSWER,
                "the control plane's answer is too large",
                status=status,
                request_id=request_id,
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _http_date(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return parsed.timestamp() if parsed.tzinfo is not None else None
