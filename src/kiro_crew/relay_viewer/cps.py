"""The control plane calls a relay viewer makes: list the Crews, mint an access token.

``ListCrewEndpoints`` lists the Crews this person can open through the relay, and
``CreateCrewAccessToken`` mints the short-lived token that opens one of them. Both
are awsJson 1.0 calls to the bearer service, the same shape as the host's lease
call (:mod:`kiro_crew.relay_client.lease`), and both carry the person's sign-in.

So each call is made the way the lease call is: no credential is read from the
environment (``trust_env=False``), a redirect is never followed, a call takes at
most 15 seconds and an answer is read to 64 KiB at most. Nothing logged here
carries the sign-in, the access token, a body, or the service's message text.

Every failure is a :class:`CpsRefused` with one of the typed codes below, so
callers show a plain reason without reading the service's own words.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from urllib.parse import urlsplit

import aiohttp

from kiro_crew.relay_client.connection import env_proxy_for
from kiro_crew.relay_client.lease import (
    ANSWER_MAX_BYTES,
    AWS_JSON_CONTENT_TYPE,
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
    ) -> None:
        super().__init__(why)
        self.code = code
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class CrewEndpoint:
    endpoint_id: str
    crew_url: str
    name: str
    crew_type: str
    presence: str
    relay_connection_status: str


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


async def list_crew_endpoints(cps_url: str, bearer: str) -> tuple[CrewEndpoint, ...]:
    """Every Crew this person can open, in the service's order, checked entry by entry."""
    crews: dict[str, CrewEndpoint] = {}
    dropped = 0
    body: dict[str, Any] = {}
    for _ in range(MAX_PAGES):
        payload = (await _call(cps_url, bearer, LIST_TARGET, body)).payload
        if not isinstance(payload, dict) or not isinstance(payload.get("crewEndpoints", []), list):
            raise CpsRefused(BAD_ANSWER, "the Crew list answer is not a list", status=200)
        if payload.get("remoteAccessAllowed") is False:
            raise CpsRefused(REMOTE_ACCESS_DISABLED, "remote access is turned off", status=200)
        for raw in payload.get("crewEndpoints", []):
            crew = _crew_endpoint(raw)
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
        logger.warning("relay viewer: left %d unusable Crew list entries out", dropped)
    return tuple(crews.values())


async def create_crew_access_token(cps_url: str, bearer: str, endpoint_id: str) -> CrewAccessToken:
    """A fresh access token for the Crew *endpoint_id*."""
    if not isinstance(endpoint_id, str) or not ENDPOINT_ID_RE.match(endpoint_id):
        raise CpsRefused(INVALID_REQUEST, "not a Crew endpoint ID")
    answer = await _call(cps_url, bearer, MINT_TARGET, {"endpointId": endpoint_id})
    payload = answer.payload if isinstance(answer.payload, dict) else {}
    token = payload.get("accessToken")
    expires = parse_time(payload.get("expiresAt"))
    renew = payload.get("renewInSeconds")
    renew_ok = renew is None or (
        isinstance(renew, (int, float)) and not isinstance(renew, bool) and renew > 0
    )
    if not (isinstance(token, str) and token) or expires is None or not renew_ok:
        raise CpsRefused(BAD_ANSWER, "the access token answer is missing a field", status=200)
    logger.info("relay viewer: minted access for %s", endpoint_id)
    return CrewAccessToken(
        access_token=token,
        expires_at=expires,
        renew_in_seconds=None if renew is None else float(renew),
        server_date=answer.date,
    )


def _crew_endpoint(raw: Any) -> Optional[CrewEndpoint]:
    if not isinstance(raw, dict):
        return None
    endpoint, url, name = raw.get("endpointId"), raw.get("crewUrl"), raw.get("name")
    if not (isinstance(endpoint, str) and ENDPOINT_ID_RE.match(endpoint)):
        return None
    if not isinstance(url, str) or crew_url_problem(url) is not None:
        return None
    # The token goes only to this origin, so it must be this endpoint's own name.
    if not (urlsplit(url).hostname or "").startswith(f"{endpoint}."):
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
    )


async def _call(cps_url: str, bearer: str, target: str, body: dict) -> _Answer:
    """One awsJson call; a 200 answer, or :class:`CpsRefused`."""
    operation = target.rsplit(".", 1)[-1]
    if not bearer:
        raise CpsRefused(SIGNED_OUT, "not signed in to Kiro")
    if not cps_url or lease_url_problem(cps_url) is not None:
        raise CpsRefused(NOT_SET_UP, "the control plane URL is not set up")
    answer = await _post(cps_url, bearer, target, body)
    if answer.status == 200:
        return answer
    code, retry_after = _refusal_code(answer)
    logger.warning("relay viewer: %s refused (HTTP %d, %s)", operation, answer.status, code)
    raise CpsRefused(
        code, f"{operation} refused ({code})", status=answer.status, retry_after=retry_after
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


async def _post(url: str, bearer: str, target: str, body: dict) -> _Answer:
    headers = {
        "Authorization": f"Bearer {bearer}",
        "Content-Type": AWS_JSON_CONTENT_TYPE,
        "X-Amz-Target": target,
    }
    try:
        # trust_env=False: no netrc or other credential from the environment. A
        # proxy the environment names is still used, as for the lease call.
        async with aiohttp.ClientSession(trust_env=False) as session:
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
                raw = await _read_capped(response.content, status)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        raise CpsRefused(
            UNREACHABLE, f"the control plane could not be reached ({type(exc).__name__})"
        ) from None
    try:
        payload = json.loads(raw) if raw else None
    except (ValueError, RecursionError):
        payload = None
    if status == 200 and payload is None:
        raise CpsRefused(BAD_ANSWER, "the control plane's answer is not JSON", status=status)
    return _Answer(status, payload, date, header_type, retry_after)


async def _read_capped(stream: aiohttp.StreamReader, status: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in stream.iter_chunked(8192):
        total += len(chunk)
        if total > ANSWER_MAX_BYTES:
            raise CpsRefused(BAD_ANSWER, "the control plane's answer is too large", status=status)
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
