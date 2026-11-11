"""The lease client: the host token source that holds this Crew's lease.

:class:`LeaseHostTokenSource` calls the lease service's ``RenewCrewLease`` (relay
contract section 10.1) when the relay client first asks for a host token, and
again each time an answer's ``renewInSeconds`` runs out. The call is the Kiro
control plane's bearer service, spoken as awsJson 1.0: ``POST /`` on the service's
base URL, the operation named in ``X-Amz-Target``. Each new ``hostToken`` an
answer carries goes to the relay client, which sends it down the host connection
in a ``token`` frame (section 4.3). Each call reports ``relay.connectionStatus``
from the relay client itself, so it is ``CONNECTED`` only from ``ready`` until
that connection ends (section 4.2), and ``connectedCount``, the people the relay
client sees connected now. The count is display-only, so one that cannot be read
is sent as 0 and never holds up the renewal.

Timing follows section 11.1. The next call is timed from when the answer arrived,
on a monotonic clock, never by comparing a time in the answer with this machine's
clock. A host token is used for its own lifetime, ``exp`` minus ``iat``, counted
from when it arrived. A spent one is never handed over: the source asks for a new
one instead, by leaving out the ``leaseVersion`` it would otherwise send back,
as it does on the first call after a start. A call that cannot reach the
service, or gets an answer it cannot use, is retried on the reconnect steps with
full jitter (section 11.2). A refusal is read from its error type (``__type``)
and ``reason``: an access refusal is tried again after 15 minutes; an unknown
Crew, or a request the service calls invalid, stops the source until Remote
Control restarts, once a stored endpoint has been dropped and tried without. An
invalid refusal of a call that left out the ``leaseVersion`` is first retried
with the last one the service returned, which is kept across restarts. A
credential that cannot be read is read again on the reconnect steps, and only a
setting that names no credential stops the source. ``IdentityInUseException`` waits
until ``takeoverAfter``, measured against the same answer's ``Date`` header; a
refused sign-in (any HTTP 401, whatever its body) reads the credential again and
retries once before it stops; anything else backs off.

When Remote Control stops, the source makes one ``ReleaseCrewInstanceLease`` call
(section 10.2) carrying only ``crewInstanceId``, so the lease ends at once instead
of lapsing up to five minutes later. It is bounded by ``RELEASE_TIMEOUT_SECONDS``
and any failure is only logged: the lease still lapses on its own.

The credential is the Crew's own sign-in to the lease service. It is read from a
file or from the Crew's secrets vault for each call, never held in the config,
and never logged or put in a ``repr``. It goes only to the configured lease
service: the session reads no netrc and follows no redirects. Logs carry refusal
codes, HTTP statuses and delays, never a token, the credential or the answer's
text.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import logging
import math
import os
import random
import re
import socket
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, NoReturn, Optional, Tuple, TypeGuard
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from kiro_crew.bounded_read import bounded_read
from kiro_crew.relay_client.backoff import Backoff
from kiro_crew.relay_client.connection import env_proxy_for
from kiro_crew.relay_client.protocol import (
    ENDPOINT_ID_RE,
    PROTOCOL_VERSION,
    crew_url_problem,
    host_url_problem,
    is_loopback_host,
)
from kiro_crew.relay_client.token_source import (
    ConnectedPeople,
    HostCredential,
    HostTokenRetryLater,
    HostTokenUnavailable,
    NewTokenCallback,
    RelayConnected,
    token_claims,
)

logger = logging.getLogger(__name__)

#: The awsJson 1.0 envelope: every operation is ``POST /``, named in ``X-Amz-Target``.
SERVICE_NAME = "KiroControlPlaneBearerService"
RENEW_TARGET = f"{SERVICE_NAME}.RenewCrewLease"
RELEASE_TARGET = f"{SERVICE_NAME}.ReleaseCrewInstanceLease"
AWS_JSON_CONTENT_TYPE = "application/x-amz-json-1.0"
#: The ``TokenType`` header values this client may send. IAM Identity Center,
#: Builder ID and Kiro social sign-ins send none; a token straight from an outside
#: identity provider (Okta, Entra ID) sends ``EXTERNAL_IDP``. A team Crew on a
#: Kiro-issued crew API key sends ``API_KEY``. The lease service refuses any other
#: type with a crew key, so the client accepts no other. It never guesses the type
#: from the credential: ``tunnel.relay_lease_token_type`` names it.
TOKEN_TYPE_EXTERNAL_IDP = "EXTERNAL_IDP"
TOKEN_TYPE_API_KEY = "API_KEY"
TOKEN_TYPES = frozenset({TOKEN_TYPE_EXTERNAL_IDP, TOKEN_TYPE_API_KEY})
#: How long one call may take, connecting included.
CALL_TIMEOUT_SECONDS = 15.0
#: How long the release at a stop may take, credential read included. It runs
#: after the relay client's ``STOP_BUDGET_SECONDS``, and both must fit in the
#: gateway's tunnel stop budget.
RELEASE_TIMEOUT_SECONDS = 1.5
#: The largest answer read; a lease answer is well under a kilobyte.
ANSWER_MAX_BYTES = 64 * 1024
#: The largest credential file read.
CREDENTIAL_MAX_BYTES = 16 * 1024
#: Bounds on an answer's ``renewInSeconds``: never a tight loop, and never long
#: enough for the 5-minute lease to lapse while the Crew is up.
RENEW_MIN_SECONDS = 5.0
RENEW_MAX_SECONDS = 240.0
#: When an answer names no ``renewInSeconds`` (a provisional field name): the
#: contract's "every minute".
RENEW_DEFAULT_SECONDS = 60.0
#: A host token is not handed over within this many seconds of its end.
TOKEN_EXPIRY_MARGIN_SECONDS = 5.0
#: The identity-in-use wait is stretched by up to this fraction before it is clamped.
TAKEOVER_WAIT_JITTER = 0.2
#: The identity-in-use wait is clamped to these bounds. The top is the lease's
#: takeover time (contract sections 10.1 and 11.1), and the wait when the answer has
#: no usable Date or takeoverAfter. The floor keeps a refusal from turning into a
#: tight loop of calls.
TAKEOVER_WAIT_MIN_SECONDS = 1.0
TAKEOVER_WAIT_MAX_SECONDS = 120.0
#: How long an access refusal holds the next call: at most every 15 minutes.
RETRY_AFTER_MAX_SECONDS = 900.0
#: The model's ``CrewName``: 1 to 64 characters, starting with a letter or digit.
DISPLAY_NAME_MAX_CHARS = 64
DISPLAY_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9 ._-]*\Z", re.ASCII)
CREW_VERSION_MAX_CHARS = 32
DEFAULT_DISPLAY_NAME = "Kiro Crew"
#: Where the Crew keeps the instance UUID it made once, under its data home.
INSTANCE_ID_FILE = Path("relay") / "crew-instance-id"
#: The last leaseVersion the service returned, with the instance ID it was for.
LEASE_VERSION_FILE = Path("relay") / "crew-lease-version"
SECRET_URI_PREFIX = "secret://"

#: Error types the service refuses this Crew with, and why. An access refusal
#: waits ``RETRY_AFTER_MAX_SECONDS``; the others stop until Remote Control restarts.
_ACCESS_DENIED = "AccessDeniedException"
_ACCESS_DENIED_REASONS = {
    "RemoteAccessDisabled": "the organization does not allow remote access",
    "CrewAccessNotGranted": "this sign-in has no access to the Crew, or its identity is not active",
}
_ACCESS_DENIED_DEFAULT = "this sign-in cannot host this Crew now"
_NOT_FOUND = "ResourceNotFoundException"
_NOT_FOUND_WHY = "the Crew's identity is disabled or deleted"
_VALIDATION = "ValidationException"
_VALIDATION_WHY = "the request does not fit the service's model; upgrade Crew"
_IDENTITY_IN_USE = "IdentityInUseException"
_ERROR_TYPE_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9]{0,127}\Z", re.ASCII)
_REASON_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_]{0,63}\Z", re.ASCII)


# ── settings ──


def lease_url_problem(url: str) -> Optional[str]:
    """Why the Crew must not send its credential to *url*, or ``None`` when it may.

    The credential rides in the call's ``Authorization`` header, so the call is
    ``https://`` unless it names this machine's loopback, which only a local
    port-forward to a test lease service does.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # raises ValueError for a port that is not a number
    except ValueError:
        return "the lease service URL does not parse"
    if not host or parts.username is not None or parts.query or parts.fragment:
        return "the lease service URL must be a base URL with no user info, query or fragment"
    if parts.scheme == "https":
        return None
    if parts.scheme == "http" and is_loopback_host(host):
        return None
    return "the lease service URL must be https:// (http:// only for a loopback port-forward)"


def credential_reference_problem(ref: str) -> Optional[str]:
    """Why *ref* cannot name the lease credential, or ``None`` when it can.

    The setting names where the credential is, ``secret://NAME`` in the Crew's
    secrets vault or the absolute path of a file, and never holds the credential
    itself. No message here repeats *ref*, which could be a credential pasted in
    by mistake.
    """
    if not ref:
        return "tunnel.relay_lease_credential is not set"
    if ref.startswith(SECRET_URI_PREFIX):
        name = ref[len(SECRET_URI_PREFIX) :]
        if not name or name != name.strip():
            return "tunnel.relay_lease_credential is a malformed secret:// reference"
        return None
    if os.path.isabs(os.path.expanduser(ref)):
        return None
    return (
        "tunnel.relay_lease_credential must be secret://NAME or the absolute path of a "
        "file holding the credential, never the credential itself"
    )


class LeaseCredentialError(Exception):
    """The lease credential could not be read; the message never contains it."""


class LeaseCredentialReferenceError(LeaseCredentialError):
    """The setting cannot name a credential at all; reading again cannot help."""


def read_lease_credential(ref: str, *, home: Optional[Path] = None) -> str:
    """The credential *ref* names, read now. Blocking: call it in a thread.

    A malformed reference raises :class:`LeaseCredentialReferenceError`. Any other
    :class:`LeaseCredentialError` may pass on its own, such as a file that is
    missing or half written while it is rotated.
    """
    problem = credential_reference_problem(ref)
    if problem is not None:
        raise LeaseCredentialReferenceError(problem)
    if ref.startswith(SECRET_URI_PREFIX):
        from kiro_crew.secrets import SecretVault

        try:
            secret = SecretVault(home if home is not None else _data_home()).get(
                ref[len(SECRET_URI_PREFIX) :]
            )
        except Exception:  # noqa: BLE001 - any vault fault fails closed
            raise LeaseCredentialError("the secrets vault could not be read") from None
        if secret is None:
            raise LeaseCredentialError("the secrets vault holds no secret by that name")
        value = secret.reveal()
    else:
        try:
            with open(os.path.expanduser(ref), "rb") as handle:
                raw = bounded_read(handle, CREDENTIAL_MAX_BYTES + 1)
        except OSError as exc:
            raise LeaseCredentialError(
                f"the credential file could not be read ({type(exc).__name__})"
            ) from None
        if len(raw) > CREDENTIAL_MAX_BYTES:
            raise LeaseCredentialError("the credential file is too large")
        try:
            value = _decode_credential_file(raw)
        except UnicodeDecodeError:
            raise LeaseCredentialError("the credential is not ASCII") from None
    value = value.strip()
    if not value or not all(33 <= ord(ch) < 127 for ch in value):
        raise LeaseCredentialError("the credential must be one line of printable ASCII")
    return value


def _decode_credential_file(raw: bytes) -> str:
    """*raw* as ASCII text, after the byte-order mark a Windows editor may write.

    Notepad can save UTF-8 with a mark, and PowerShell's ``echo x > file`` writes
    UTF-16 with one. The credential itself must still be ASCII.
    """
    if raw.startswith(codecs.BOM_UTF8):
        raw = raw[len(codecs.BOM_UTF8) :]
    elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        raw = raw.decode("utf-16").encode("utf-8")
    return raw.decode("ascii")


def _data_home() -> Path:
    from kiro_crew.config.paths import data_home

    return data_home()


def crew_instance_id(home: Path) -> str:
    """The UUID this Crew made once for its lease, made and kept on first use.

    Kept across restarts, because a team Crew whose instance changes waits for the
    old lease to free (``IDENTITY_IN_USE``). Blocking: call it in a thread.
    """
    path = home / INSTANCE_ID_FILE
    try:
        return str(uuid.UUID(path.read_text(encoding="ascii").strip()))
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        logger.warning("the relay's crew instance ID file is unreadable; making a new one")
    value = str(uuid.uuid4())
    try:
        from kiro_crew.atomic_write import atomic_write
        from kiro_crew.owner_only_files import mkdirs_owner_only

        mkdirs_owner_only(path.parent)
        atomic_write(path, value + "\n", restrict_to_owner=True)
    except Exception:  # noqa: BLE001 - an unsaved ID still serves this run
        logger.warning("the relay's crew instance ID could not be saved; it lasts this run only")
    return value


def stored_lease_version(home: Path, instance_id: str) -> Optional[int]:
    """The leaseVersion last saved for *instance_id*, or None. Blocking: call it in a thread."""
    try:
        text = (home / LEASE_VERSION_FILE).read_text(encoding="ascii")
        saved_for, _, version = text.partition(" ")
        return int(version) if saved_for == instance_id and int(version) >= 0 else None
    except (OSError, ValueError):
        return None


def save_lease_version(home: Path, instance_id: str, version: int) -> None:
    """Keep *version* for *instance_id* across restarts. Blocking: call it in a thread."""
    try:
        from kiro_crew.atomic_write import atomic_write
        from kiro_crew.owner_only_files import mkdirs_owner_only

        path = home / LEASE_VERSION_FILE
        mkdirs_owner_only(path.parent)
        atomic_write(path, f"{instance_id} {version}\n", restrict_to_owner=True)
    except Exception:  # noqa: BLE001 - an unsaved version only loses the restart fallback
        logger.warning("the relay's lease version could not be saved; a restart may not renew")


def crew_name(name: Optional[str]) -> str:
    """*name* made to fit the model's ``CrewName``, or the default when nothing fits.

    Characters the name cannot hold are dropped, as are leading ones a name
    cannot start with, and the rest is cut to the length limit.
    """
    kept = "".join(ch for ch in (name or "") if ch.isascii() and (ch.isalnum() or ch in " ._-"))
    while kept and not kept[0].isalnum():
        kept = kept[1:]
    kept = kept[:DISPLAY_NAME_MAX_CHARS].rstrip()
    return kept if DISPLAY_NAME_RE.match(kept) else DEFAULT_DISPLAY_NAME


def default_display_name() -> str:
    """The name the Crew list shows: this machine's short host name."""
    try:
        name = socket.gethostname().split(".", 1)[0]
    except OSError:
        name = ""
    return crew_name(name)


# ── the answer ──


class _Unusable(Exception):
    """An answer field this client cannot use; the message names the field only."""


@dataclass(frozen=True)
class LeaseAnswer:
    """A ``RenewCrewLease`` answer, checked."""

    endpoint_id: str
    host_url: str
    crew_url: str
    lease_version: int
    renew_in: float
    host_token: Optional[str] = field(default=None, repr=False)


def _is_number(value: Any) -> TypeGuard[float]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        # A JSON integer too large for a float is not a time or a count of seconds.
        return False


def parse_time(value: Any) -> Optional[float]:
    """A model ``Timestamp`` as seconds since the epoch, or ``None`` when it is not one.

    awsJson 1.0 sends epoch seconds; an ISO 8601 time with a zone is accepted too.
    """
    if _is_number(value):
        return float(value) if value >= 0 else None
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00", 1))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def lease_url_is_loopback(url: str) -> bool:
    """Whether the lease service URL names this machine's loopback (a local test service)."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    return bool(host) and is_loopback_host(host or "")


def dial_url(url: str, *, local_test: bool = False) -> Optional[str]:
    """The URL the host leg dials for a lease answer's ``hostUrl``, or ``None``.

    The model types ``hostUrl`` as an ``https://`` URL; it is dialed as ``wss://``
    with the same host, port and path. A ``wss://`` URL is dialed as it is. Any
    other scheme is refused. Only when *local_test* is set, because the lease
    service itself is on this machine's loopback, a loopback ``http://`` or
    ``ws://`` test relay is dialed too: a real service's answer never sends the
    host token to a plaintext port.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme == "https":
        url = urlunsplit(("wss",) + tuple(parts[1:]))  # type: ignore[arg-type]
    elif parts.scheme in ("http", "ws"):
        if not (local_test and host and is_loopback_host(host)):
            return None
        url = urlunsplit(("ws",) + tuple(parts[1:]))  # type: ignore[arg-type]
    elif parts.scheme != "wss":
        return None
    return url if host_url_problem(url) is None else None


def parse_answer(body: Any, *, local_test: bool = False) -> LeaseAnswer:
    """Check a ``RenewCrewLease`` answer; raise :class:`_Unusable` naming the bad field.

    *local_test* is whether the lease service is on loopback (see :func:`dial_url`).
    """
    if not isinstance(body, dict):
        raise _Unusable("body")
    endpoint = body.get("endpointId")
    if not isinstance(endpoint, str) or not ENDPOINT_ID_RE.match(endpoint):
        raise _Unusable("endpointId")
    raw_host_url = body.get("hostUrl")
    host_url = (
        dial_url(raw_host_url, local_test=local_test) if isinstance(raw_host_url, str) else None
    )
    if host_url is None:
        raise _Unusable("hostUrl")
    crew_url = body.get("crewUrl")
    if not isinstance(crew_url, str) or crew_url_problem(crew_url) is not None:
        raise _Unusable("crewUrl")
    # Required, but nothing compares it with this clock.
    if parse_time(body.get("leaseExpiresAt")) is None:
        raise _Unusable("leaseExpiresAt")
    version = body.get("leaseVersion")
    if not isinstance(version, int) or isinstance(version, bool):
        raise _Unusable("leaseVersion")
    renew = body.get("renewInSeconds")
    if renew is None:
        renew_in = RENEW_DEFAULT_SECONDS
    elif _is_number(renew) and renew > 0:
        renew_in = min(max(float(renew), RENEW_MIN_SECONDS), RENEW_MAX_SECONDS)
    else:
        raise _Unusable("renewInSeconds")
    token = body.get("hostToken")
    if token is not None and (not isinstance(token, str) or not token):
        raise _Unusable("hostToken")
    return LeaseAnswer(
        endpoint_id=endpoint,
        host_url=host_url,
        crew_url=crew_url,
        lease_version=version,
        renew_in=renew_in,
        host_token=token,
    )


def error_type(value: Any) -> Optional[str]:
    """The exception name in an awsJson ``__type`` or ``X-Amzn-ErrorType``, or ``None``.

    The value can carry a namespace before ``#`` and a URL after ``:``; both go.
    """
    if not isinstance(value, str):
        return None
    name = value.split(":", 1)[0].rsplit("#", 1)[-1].strip()
    return name if _ERROR_TYPE_RE.match(name) else None


def refusal_of(body: Any, header_type: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """A refusal's error type and ``reason``, each or ``None``.

    The type comes from the body's ``__type``, or else the ``X-Amzn-ErrorType``
    header. Neither the ``message`` nor any other text of the body is kept.
    """
    fields = body if isinstance(body, dict) else {}
    kind = error_type(fields.get("__type")) or error_type(header_type)
    reason = fields.get("reason")
    if not isinstance(reason, str) or not _REASON_RE.match(reason):
        reason = None
    return kind, reason


def takeover_wait(body: Any, date_header: Optional[str], jitter: float = 1.0) -> float:
    """How long an ``IdentityInUseException`` asks the Crew to wait, in seconds.

    ``takeoverAfter`` minus the same answer's ``Date``, so both times are the
    service's own and this machine's clock is never read. *jitter* stretches it
    first, then it is clamped to the takeover-wait bounds, so it never passes the
    takeover time. Without a usable ``takeoverAfter`` or ``Date`` it is the whole
    takeover time, so the Crew never takes the lease over early.
    """
    takeover = parse_time(body.get("takeoverAfter")) if isinstance(body, dict) else None
    sent: Optional[float] = None
    if date_header:
        try:
            parsed = parsedate_to_datetime(date_header)
        except (TypeError, ValueError, IndexError):
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            sent = parsed.timestamp()
    if takeover is None or sent is None:
        return TAKEOVER_WAIT_MAX_SECONDS
    wait = (takeover - sent) * jitter
    return min(max(wait, TAKEOVER_WAIT_MIN_SECONDS), TAKEOVER_WAIT_MAX_SECONDS)


def token_lifetime(token: str) -> Optional[float]:
    """``exp`` minus ``iat`` from the host token's claims, or ``None`` without both."""
    claims = token_claims(token)
    issued, expires = claims.get("iat"), claims.get("exp")
    if not (_is_number(issued) and _is_number(expires)):
        return None
    lifetime = float(expires) - float(issued)
    return lifetime if lifetime > 0 else None


# ── the source ──


class _Stop(Exception):
    """Renewals are over until Remote Control restarts."""


class _Later(Exception):
    """No answer this time; the schedule says when to try again."""


class _Unreachable(Exception):
    """The call did not get an answer it could read."""


@dataclass(frozen=True)
class _Answered:
    """One call's HTTP status, parsed body, and the headers a refusal is read from."""

    status: int
    payload: Any = field(repr=False)
    date: Optional[str] = None
    error_type: Optional[str] = None


def _never_connected() -> bool:
    return False


def _nobody() -> int:
    return 0


Sleep = Callable[[float], Awaitable[Any]]


class LeaseHostTokenSource:
    """Holds this Crew's lease and hands each new host token to the relay client."""

    def __init__(
        self,
        *,
        lease_url: str,
        credential: str,
        crew_version: str,
        home: Optional[Path] = None,
        display_name: Optional[str] = None,
        read_credential: Optional[Callable[[str], str]] = None,
        rng: Optional[random.Random] = None,
        sleep: Sleep = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        token_type: Optional[str] = None,
    ) -> None:
        if token_type is not None and token_type not in TOKEN_TYPES:
            raise ValueError("the lease credential's token type is not one this client sends")
        # awsJson 1.0: every operation is POST / on the service's base URL.
        self._renew_url = lease_url.rstrip("/") + "/"
        # Only a lease service on loopback may name a plaintext loopback test relay.
        self._local_test = lease_url_is_loopback(lease_url)
        # The TokenType header, or None for a sign-in that sends none.
        self._token_type = token_type
        # A reference to the credential (secret://NAME or a path), not the credential.
        self._credential_ref = credential
        self._crew_version = crew_version[:CREW_VERSION_MAX_CHARS]
        self._home = home
        self._display_name = display_name
        self._read_credential = read_credential or self._read_from_reference
        self._rng = rng or random.Random()
        self._sleep = sleep
        self._monotonic = monotonic
        self._backoff = Backoff(rng=self._rng)
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._session: Optional[aiohttp.ClientSession] = None
        self._timer: Optional[asyncio.Task[None]] = None
        self._callback: Optional[NewTokenCallback] = None
        self._connected: RelayConnected = _never_connected
        self._people: ConnectedPeople = _nobody
        self._instance_id: Optional[str] = None
        self._endpoint_id: Optional[str] = None
        #: The last answer's leaseVersion (or the one saved before a restart), sent
        #: back unless a fresh host token is wanted.
        self._lease_version: Optional[int] = None
        #: Set once the service refused a call with no leaseVersion and accepted the
        #: retry that sent it. From then until Remote Control stops, every call sends
        #: it: a fresh host token does not clear it, because this service would refuse
        #: the next version-less call from this instance just the same.
        self._send_version = False
        #: Saves of the leaseVersion run one at a time, in order, in a thread; the
        #: last one is kept so aclose() can wait for it.
        self._save_lock = asyncio.Lock()
        self._saving: Optional[asyncio.Future[None]] = None
        self._held: Optional[HostCredential] = None
        self._held_until = 0.0
        #: When the next renewal is due, after a good answer.
        self._next_at = 0.0
        #: No call before this: a backoff after a failure, or the service's own wait.
        self._hold_until = 0.0
        self._hold_reason = ""
        #: Why renewals stopped for good, once a refusal or a credential fault did it.
        self._stopped = ""

    def __repr__(self) -> str:
        return f"LeaseHostTokenSource(url={self._renew_url!r})"

    @property
    def stopped_reason(self) -> str:
        return self._stopped

    # ── HostTokenSource ──

    async def current(self) -> HostCredential:
        held = self._usable()
        if held is not None:
            return held
        return await self._renew_for_client(host_token_requested=True, reuse_held=True)

    async def renew(self, *, host_token_requested: bool) -> HostCredential:
        return await self._renew_for_client(
            host_token_requested=host_token_requested, reuse_held=False
        )

    def on_new_token(self, callback: Optional[NewTokenCallback]) -> None:
        self._callback = callback

    def track_relay(
        self,
        connected: Optional[RelayConnected],
        people: Optional[ConnectedPeople] = None,
    ) -> None:
        self._connected = connected or _never_connected
        self._people = people or _nobody

    async def aclose(self) -> None:
        """End the renewals and the lease, and forget its state, so a later start begins anew."""
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        # A save the cancel left running finishes first, so none outlives the stop
        # and a later start reads the newest version.
        saving, self._saving = self._saving, None
        if saving is not None:
            await asyncio.gather(saving, return_exceptions=True)
        instance_id = self._instance_id
        self._callback = None
        self._connected = _never_connected
        self._people = _nobody
        self._held = None
        # A later start reads the instance ID and its saved leaseVersion again.
        self._instance_id = self._lease_version = None
        self._send_version = False
        self._held_until = self._next_at = self._hold_until = 0.0
        self._hold_reason = self._stopped = ""
        self._backoff.reset()
        # Last, so a stop cancelled during the release has already forgotten the
        # lease, and its session still closes.
        try:
            # An instance ID is read only by a renewal, so with none there is no lease.
            if instance_id is not None:
                await self._release(instance_id)
        finally:
            session, self._session = self._session, None
            if session is not None:
                await session.close()

    # ── calls ──

    def _usable(self) -> Optional[HostCredential]:
        if self._held is not None and self._monotonic() < self._held_until:
            return self._held
        return None

    async def _renew_for_client(
        self, *, host_token_requested: bool, reuse_held: bool
    ) -> HostCredential:
        async with self._lock:
            if reuse_held:
                held = self._usable()
                if held is not None:
                    return held
            elif host_token_requested:
                # The client holds no usable token (the relay refused it or let it
                # expire), so the one held here is no use to it either.
                self._held = None
            try:
                await self._call(host_token_requested or self._usable() is None)
            except _Stop as exc:
                raise HostTokenUnavailable(str(exc)) from None
            except _Later as exc:
                raise HostTokenRetryLater(str(exc)) from None
            finally:
                self._ensure_timer()
            held = self._usable()
        if held is None:
            raise HostTokenRetryLater("the lease service has not issued a host token yet")
        return held

    async def _call(
        self,
        host_token_requested: bool,
        *,
        endpoint_retry: bool = True,
        sign_in_retry: bool = True,
        with_version: bool = False,
    ) -> None:
        """One ``RenewCrewLease`` call, with its outcome applied. The lock is held."""
        if self._stopped:
            raise _Stop(self._stopped)
        now = self._monotonic()
        if now < self._hold_until:
            raise _Later(f"{self._hold_reason}; next try in {self._hold_until - now:.0f}s")
        try:
            credential = await asyncio.to_thread(self._read_credential, self._credential_ref)
        except LeaseCredentialReferenceError as exc:
            self._stop(f"tunnel.relay_lease_credential names no credential: {exc}")
        except LeaseCredentialError as exc:
            # A file briefly missing or half written during a rotation: read again
            # on the backoff steps rather than stop until a restart.
            self._back_off(
                f"the credential tunnel.relay_lease_credential names could not be read: {exc}"
            )
        home = self._home if self._home is not None else _data_home()
        if self._instance_id is None:
            self._instance_id = await asyncio.to_thread(crew_instance_id, home)
            self._lease_version = await asyncio.to_thread(
                stored_lease_version, home, self._instance_id
            )
        body = self._request_body(host_token_requested, with_version or self._send_version)
        try:
            answered = await self._post(credential, body, RENEW_TARGET)
        except _Unreachable as exc:
            self._back_off(str(exc))
        status, payload = answered.status, answered.payload
        arrived = self._monotonic()
        if status == 200:
            try:
                answer = parse_answer(payload, local_test=self._local_test)
            except _Unusable as exc:
                self._back_off(f"the lease service's answer has no usable {exc}")
            if with_version:
                # The stale-version fallback worked: this service needs the version.
                self._send_version = True
            saved = self._lease_version
            self._apply(answer, arrived)
            if answer.lease_version != saved:
                # Shielded: a stop that cancels this call leaves the save to finish,
                # and aclose() waits for it.
                self._saving = asyncio.ensure_future(
                    self._save_version(home, self._instance_id, answer.lease_version)
                )
                await asyncio.shield(self._saving)
            if self._send_version and self._usable() is None:
                # A service that needs the version mints when a token is due, not on
                # request, so asking again before the next renewal only repeats this.
                self._hold(self._next_at, "the lease service has not issued a host token yet")
            return
        if status == 401:
            # The one status read before the exception name: a sign-in failure from
            # the bearer check has no modeled name, and until its shape is confirmed
            # any HTTP 401 is one, whatever its body (contract section 10.9).
            if sign_in_retry:
                logger.warning(
                    "the lease service refused the Crew's sign-in (HTTP 401); reading it "
                    "again and retrying once"
                )
                await self._call(
                    host_token_requested,
                    endpoint_retry=endpoint_retry,
                    sign_in_retry=False,
                    with_version=with_version,
                )
                return
            self._stop(
                "the lease service refused the Crew's credential (HTTP 401): check "
                "tunnel.relay_lease_credential's credential and that "
                "tunnel.relay_lease_token_type matches it (a team Crew on a crew API key "
                "needs API_KEY), then restart Remote Control"
            )
        kind, reason = refusal_of(payload, answered.error_type)
        named = f"{kind} {reason}" if kind and reason else (kind or f"HTTP {status}")
        if kind == _ACCESS_DENIED:
            # Access can be granted again with no change on this machine, so the
            # Crew tries again on its own, at most every 15 minutes, as the refusals
            # fixture says.
            why = _ACCESS_DENIED_REASONS.get(reason or "", _ACCESS_DENIED_DEFAULT)
            self._hold(
                arrived + RETRY_AFTER_MAX_SECONDS,
                f"the lease service refused this Crew ({named}): {why}",
            )
            logger.warning(
                "the lease service refused this Crew (%s); trying again in %.0fs",
                named,
                RETRY_AFTER_MAX_SECONDS,
            )
            raise _Later(self._hold_reason)
        if kind == _NOT_FOUND:
            self._stop(f"the lease service refused this Crew ({named}): {_NOT_FOUND_WHY}")
        if kind == _VALIDATION:
            # Once per renewal: the endpoint retry below (endpoint_retry False) never
            # comes back here.
            stored = self._lease_version
            if endpoint_retry and "leaseVersion" not in body and stored is not None:
                # A service that fences a team lease by version refuses a call that
                # leaves it out (it reads as version 0) while this instance holds the
                # lease, e.g. after a restart or a 4401. Renew once with the stored
                # version, and take a host token when the service next issues one.
                logger.warning(
                    "the lease service refused a renewal with no lease version (%s); "
                    "renewing once with the stored one (stale-version fallback)",
                    named,
                )
                await self._call(
                    host_token_requested,
                    endpoint_retry=endpoint_retry,
                    sign_in_retry=sign_in_retry,
                    with_version=True,
                )
                return
            if endpoint_retry and "endpointId" in body["relay"]:
                # The model has no endpoint-mismatch reason: a stored endpoint that
                # is not the one the service derives for this Crew is refused as invalid.
                logger.warning(
                    "the lease service refused the renewal (%s); renewing once without "
                    "the stored endpoint",
                    named,
                )
                self._endpoint_id = None
                await self._call(
                    host_token_requested, endpoint_retry=False, sign_in_retry=sign_in_retry
                )
                return
            self._stop(f"the lease service refused the renewal ({named}): {_VALIDATION_WHY}")
        if kind == _IDENTITY_IN_USE:
            wait = takeover_wait(
                payload, answered.date, self._rng.uniform(1.0, 1.0 + TAKEOVER_WAIT_JITTER)
            )
            self._hold(arrived + wait, f"the lease service asked the Crew to wait ({named})")
            logger.warning("the lease service refused the renewal (%s); waiting %.0fs", named, wait)
            raise _Later(self._hold_reason)
        self._back_off(f"the lease service refused the renewal ({named})")

    async def _release(self, instance_id: str) -> None:
        """One best-effort ``ReleaseCrewInstanceLease``; a failure leaves the lease to lapse."""

        async def call() -> int:
            credential = await asyncio.to_thread(self._read_credential, self._credential_ref)
            body = {"crewInstanceId": instance_id}
            return (await self._post(credential, body, RELEASE_TARGET)).status

        try:
            status = await asyncio.wait_for(call(), RELEASE_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - a release never fails the stop
            logger.info("ending the lease failed (%s); it lapses on its own", type(exc).__name__)
            return
        if status != 200:
            logger.info("the lease service did not end the lease (HTTP %d)", status)

    async def _save_version(self, home: Path, instance_id: str, version: int) -> None:
        # One at a time, so an older version never lands after a newer one.
        async with self._save_lock:
            await asyncio.to_thread(save_lease_version, home, instance_id, version)

    def _request_body(
        self, host_token_requested: bool, with_version: bool = False
    ) -> dict[str, Any]:
        relay: dict[str, Any] = {
            "connectionStatus": "CONNECTED" if self._relay_connected() else "NOT_CONNECTED"
        }
        if self._endpoint_id:
            relay["endpointId"] = self._endpoint_id
        body: dict[str, Any] = {
            "crewInstanceId": self._instance_id,
            "name": crew_name(self._display_name) if self._display_name else default_display_name(),
            "crewVersion": self._crew_version,
            "protocolVersion": str(PROTOCOL_VERSION),
            "hostKind": "CREW",
            "relay": relay,
            "connectedCount": self._connected_count(),
        }
        # Leaving out leaseVersion asks for a fresh host token: on the first call
        # after a start, and whenever the client holds no usable one (after a 4401
        # or 4403 close, or once the held one ran out). A service that refused that
        # gets the version on every call instead (the stale-version fallback).
        if (with_version or not host_token_requested) and self._lease_version is not None:
            body["leaseVersion"] = self._lease_version
        return body

    def _relay_connected(self) -> bool:
        try:
            return self._connected() is True
        except Exception:  # noqa: BLE001 - a broken reader reports not connected
            logger.warning("reading the relay connection state failed", exc_info=True)
            return False

    def _connected_count(self) -> int:
        """The people connected through the relay now; 0 when that cannot be read.

        The service shows it and decides nothing on it, so a broken count never
        holds up a renewal.
        """
        try:
            count = self._people()
        except Exception:  # noqa: BLE001 - a broken count reports nobody
            logger.warning("counting the people connected through the relay failed", exc_info=True)
            return 0
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return 0
        return count

    def _client_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # trust_env=False: the call carries the Crew's credential, so the session
            # never reads netrc or any other credential from the environment. A proxy
            # set in the environment is still used: each call resolves it itself.
            self._session = aiohttp.ClientSession(trust_env=False)
        return self._session

    def _headers(self, credential: str, target: str) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {credential}",
            "Content-Type": AWS_JSON_CONTENT_TYPE,
            "X-Amz-Target": target,
        }
        if self._token_type is not None:
            headers["TokenType"] = self._token_type
        return headers

    async def _post(self, credential: str, body: dict, target: str) -> "_Answered":
        try:
            async with self._client_session().post(
                self._renew_url,
                data=json.dumps(body).encode("utf-8"),
                headers=self._headers(credential, target),
                allow_redirects=False,
                proxy=env_proxy_for(self._renew_url),
                timeout=aiohttp.ClientTimeout(total=CALL_TIMEOUT_SECONDS),
            ) as response:
                status = response.status
                date = response.headers.get("Date")
                header_type = response.headers.get("X-Amzn-ErrorType")
                raw = await _read_capped(response.content, ANSWER_MAX_BYTES)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise _Unreachable(
                f"the lease service could not be reached ({type(exc).__name__})"
            ) from None
        try:
            payload = json.loads(raw) if raw else None
        except (ValueError, RecursionError):
            payload = None
        return _Answered(status=status, payload=payload, date=date, error_type=header_type)

    def _apply(self, answer: LeaseAnswer, arrived: float) -> None:
        self._hold_until = 0.0
        self._hold_reason = ""
        self._backoff.reset()
        self._endpoint_id = answer.endpoint_id
        self._lease_version = answer.lease_version
        self._next_at = arrived + answer.renew_in
        fresh = False
        if answer.host_token is not None:
            lifetime = token_lifetime(answer.host_token)
            if lifetime is None or lifetime <= TOKEN_EXPIRY_MARGIN_SECONDS:
                logger.warning(
                    "the lease service issued a host token with no lifetime left; it is not used"
                )
            else:
                try:
                    credential = HostCredential(
                        token=answer.host_token, host_url=answer.host_url, crew_url=answer.crew_url
                    )
                except ValueError:
                    logger.warning(
                        "the lease service issued a malformed host token; it is not used"
                    )
                else:
                    self._held = credential
                    self._held_until = arrived + lifetime - TOKEN_EXPIRY_MARGIN_SECONDS
                    fresh = True
        if (
            not fresh
            and self._held is not None
            and (self._held.host_url != answer.host_url or self._held.crew_url != answer.crew_url)
        ):
            self._held = replace(self._held, host_url=answer.host_url, crew_url=answer.crew_url)
        logger.info(
            "lease renewed (%s); next renewal in %.0fs",
            "new host token" if fresh else "same host token",
            answer.renew_in,
        )

    def _hold(self, until: float, reason: str) -> None:
        self._hold_until = until
        self._hold_reason = reason
        self._next_at = until

    def _back_off(self, problem: str) -> NoReturn:
        """Schedule the next try on the reconnect steps, and end this call."""
        delay = self._backoff.next_delay()
        self._hold(self._monotonic() + delay, problem)
        logger.warning("lease renewal failed (%s); next try in %.1fs", problem, delay)
        raise _Later(problem)

    def _stop(self, reason: str) -> NoReturn:
        """Stop renewals until Remote Control restarts, and end this call."""
        self._stopped = reason
        logger.warning("lease renewals stopped: %s", reason)
        raise _Stop(reason)

    def _read_from_reference(self, ref: str) -> str:
        return read_lease_credential(ref, home=self._home)

    # ── the schedule ──

    def _ensure_timer(self) -> None:
        self._wake.set()
        if self._stopped or (self._timer is not None and not self._timer.done()):
            return
        self._timer = asyncio.create_task(self._run_timer(), name="relay-lease-renewal")

    async def _run_timer(self) -> None:
        while not self._stopped:
            self._wake.clear()
            delay = max(self._next_at, self._hold_until) - self._monotonic()
            if delay > 0:
                await self._sleep_or_wake(delay)
                continue
            fresh: Optional[HostCredential] = None
            async with self._lock:
                if self._stopped or max(self._next_at, self._hold_until) > self._monotonic():
                    continue
                before = self._held
                try:
                    await self._call(self._usable() is None)
                except (_Stop, _Later):
                    continue
                except Exception:  # noqa: BLE001 - keep renewing after a fault of this code
                    logger.exception("a lease renewal failed")
                    self._hold(self._monotonic() + self._backoff.next_delay(), "a renewal failed")
                    continue
                if self._held is not before:
                    fresh = self._usable()
            if fresh is not None and self._callback is not None:
                try:
                    await self._callback(fresh)
                except Exception:  # noqa: BLE001 - the next renewal tries again
                    logger.warning(
                        "handing the relay client its new host token failed", exc_info=True
                    )

    async def _sleep_or_wake(self, delay: float) -> None:
        sleeper = asyncio.ensure_future(self._sleep(delay))
        waker = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            waker.cancel()
            await asyncio.gather(sleeper, waker, return_exceptions=True)


async def _read_capped(stream: aiohttp.StreamReader, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in stream.iter_chunked(8192):
        total += len(chunk)
        if total > limit:
            raise _Unreachable("the lease service's answer is too large")
        chunks.append(chunk)
    return b"".join(chunks)
