"""The lease client: the host token source that holds this Crew's lease.

:class:`LeaseHostTokenSource` calls the lease service's ``RenewCrewLease`` (relay
contract section 10.1) when the relay client first asks for a host token, and
again each time an answer's ``renewInSeconds`` runs out. Each new ``hostToken`` an
answer carries goes to the relay client, which sends it down the host connection
in a ``token`` frame (section 4.3). Each call reports ``relay.connected`` from the
relay client itself, so it is true only from ``ready`` until that connection ends
(section 4.2).

Timing follows section 11.1. The next call is timed from when the answer arrived,
on a monotonic clock, never by comparing a time in the answer with this machine's
clock. A host token is used for its own lifetime, ``exp`` minus ``iat``, counted
from when it arrived. A spent one is never handed over: the source asks for a new
one (``hostTokenRequested``) instead. A call that cannot reach the service, or
gets an answer it cannot use, is retried on the reconnect steps with full jitter
(section 11.2). The typed refusals are handled as the contract's refusals fixture
says: some stop the source until Remote Control restarts, ``RATE_LIMITED`` and
``IDENTITY_IN_USE`` wait out ``retryAfterSeconds``, ``ENDPOINT_MISMATCH`` calls
again at once without the endpoint, and ``SIGN_IN_EXPIRED`` reads the credential
again and retries once before it stops.

The credential is the Crew's own sign-in to the lease service. It is read from a
file or from the Crew's secrets vault for each call, never held in the config,
and never logged or put in a ``repr``. It goes only to the configured lease
service: the session reads no netrc and follows no redirects. Logs carry refusal
codes, HTTP statuses and delays, never a token, the credential or the answer's
text.
"""

from __future__ import annotations

import asyncio
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
from pathlib import Path
from typing import Any, Awaitable, Callable, NoReturn, Optional, Tuple, TypeGuard
from urllib.parse import urlsplit

import aiohttp

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
    HostCredential,
    HostTokenRetryLater,
    HostTokenUnavailable,
    NewTokenCallback,
    RelayConnected,
    token_claims,
)

logger = logging.getLogger(__name__)

#: The operation's path under the lease service's base URL.
RENEW_PATH = "/RenewCrewLease"
#: How long one call may take, connecting included.
CALL_TIMEOUT_SECONDS = 15.0
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
#: ``retryAfterSeconds`` is stretched by up to this fraction, and never shortened.
RETRY_AFTER_JITTER = 0.2
#: The shortest and longest wait a ``retryAfterSeconds`` can impose.
RETRY_AFTER_MIN_SECONDS = 1.0
RETRY_AFTER_MAX_SECONDS = 900.0
DISPLAY_NAME_MAX_CHARS = 128
CREW_VERSION_MAX_CHARS = 32
DEFAULT_DISPLAY_NAME = "Kiro Crew"
#: Where the Crew keeps the instance UUID it made once, under its data home.
INSTANCE_ID_FILE = Path("relay") / "crew-instance-id"
SECRET_URI_PREFIX = "secret://"

#: Refusals that stop renewals until Remote Control restarts (refusals fixture).
_STOP_REFUSALS = {
    "REMOTE_ACCESS_DISABLED": "the organization does not allow remote access",
    "HOSTING_NOT_ALLOWED": "this sign-in cannot host a Crew",
    "CREW_IDENTITY_DISABLED": "the Crew's identity is disabled, deleted or rotated",
    "VERSION_UNSUPPORTED": "this Crew's relay protocol version is too old; upgrade Crew",
}
_SIGN_IN_EXPIRED = "SIGN_IN_EXPIRED"
_ENDPOINT_MISMATCH = "ENDPOINT_MISMATCH"
#: Refusals that name how long to wait before the next call.
_WAIT_REFUSALS = frozenset({"RATE_LIMITED", "IDENTITY_IN_USE"})
_CODE_RE = re.compile(r"\A[A-Z][A-Z0-9_]{0,63}\Z", re.ASCII)


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


def read_lease_credential(ref: str, *, home: Optional[Path] = None) -> str:
    """The credential *ref* names, read now. Blocking: call it in a thread."""
    problem = credential_reference_problem(ref)
    if problem is not None:
        raise LeaseCredentialError(problem)
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
                raw = handle.read(CREDENTIAL_MAX_BYTES + 1)
        except OSError as exc:
            raise LeaseCredentialError(
                f"the credential file could not be read ({type(exc).__name__})"
            ) from None
        if len(raw) > CREDENTIAL_MAX_BYTES:
            raise LeaseCredentialError("the credential file is too large")
        try:
            value = raw.decode("ascii")
        except UnicodeDecodeError:
            raise LeaseCredentialError("the credential is not ASCII") from None
    value = value.strip()
    if not value or not all(33 <= ord(ch) < 127 for ch in value):
        raise LeaseCredentialError("the credential must be one line of printable ASCII")
    return value


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

        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write(path, value + "\n", restrict_to_owner=True)
    except Exception:  # noqa: BLE001 - an unsaved ID still serves this run
        logger.warning("the relay's crew instance ID could not be saved; it lasts this run only")
    return value


def default_display_name() -> str:
    """The name the Crew list shows: this machine's short host name."""
    try:
        name = socket.gethostname().split(".", 1)[0]
    except OSError:
        name = ""
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    return name[:DISPLAY_NAME_MAX_CHARS] or DEFAULT_DISPLAY_NAME


# ── the answer ──


class _Unusable(Exception):
    """An answer field this client cannot use; the message names the field only."""


@dataclass(frozen=True)
class LeaseAnswer:
    """A ``RenewCrewLease`` answer, checked."""

    endpoint_id: str
    host_url: str
    crew_url: str
    lease_expires_at: str
    renew_in: float
    host_token: Optional[str] = field(default=None, repr=False)


def _is_number(value: Any) -> TypeGuard[float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_utc_time(value: str) -> bool:
    if len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def parse_answer(body: Any) -> LeaseAnswer:
    """Check a ``RenewCrewLease`` answer; raise :class:`_Unusable` naming the bad field."""
    if not isinstance(body, dict):
        raise _Unusable("body")
    endpoint = body.get("endpointId")
    if not isinstance(endpoint, str) or not ENDPOINT_ID_RE.match(endpoint):
        raise _Unusable("endpointId")
    host_url = body.get("hostUrl")
    if not isinstance(host_url, str) or host_url_problem(host_url) is not None:
        raise _Unusable("hostUrl")
    crew_url = body.get("crewUrl")
    if not isinstance(crew_url, str) or crew_url_problem(crew_url) is not None:
        raise _Unusable("crewUrl")
    expires = body.get("leaseExpiresAt")
    if not isinstance(expires, str) or not _is_utc_time(expires):
        raise _Unusable("leaseExpiresAt")
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
        lease_expires_at=expires,
        renew_in=renew_in,
        host_token=token,
    )


def refusal_of(body: Any) -> Tuple[Optional[str], Optional[float]]:
    """A refusal body's ``error.code`` and ``error.retryAfterSeconds``, each or ``None``."""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None, None
    code = error.get("code")
    if not isinstance(code, str) or not _CODE_RE.match(code):
        code = None
    retry_after = error.get("retryAfterSeconds")
    if _is_number(retry_after) and retry_after >= 0:
        wait: Optional[float] = min(float(retry_after), RETRY_AFTER_MAX_SECONDS)
    else:
        wait = None
    return code, wait


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


def _never_connected() -> bool:
    return False


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
    ) -> None:
        self._renew_url = lease_url.rstrip("/") + RENEW_PATH
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
        self._instance_id: Optional[str] = None
        self._endpoint_id: Optional[str] = None
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
    def endpoint_id(self) -> Optional[str]:
        """The endpoint the last answer named, which the next call reports."""
        return self._endpoint_id

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

    def track_relay(self, connected: Optional[RelayConnected]) -> None:
        self._connected = connected or _never_connected

    async def aclose(self) -> None:
        """End the renewals and forget the lease state, so a later start begins anew."""
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        session, self._session = self._session, None
        if session is not None:
            await session.close()
        self._callback = None
        self._connected = _never_connected
        self._held = None
        self._held_until = self._next_at = self._hold_until = 0.0
        self._hold_reason = self._stopped = ""
        self._backoff.reset()

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
        self, host_token_requested: bool, *, endpoint_retry: bool = True, sign_in_retry: bool = True
    ) -> None:
        """One ``RenewCrewLease`` call, with its outcome applied. The lock is held."""
        if self._stopped:
            raise _Stop(self._stopped)
        now = self._monotonic()
        if now < self._hold_until:
            raise _Later(f"{self._hold_reason}; next try in {self._hold_until - now:.0f}s")
        try:
            credential = await asyncio.to_thread(self._read_credential, self._credential_ref)
        except LeaseCredentialError as exc:
            self._stop(f"the lease credential could not be read: {exc}")
        if self._instance_id is None:
            self._instance_id = await asyncio.to_thread(
                crew_instance_id, self._home if self._home is not None else _data_home()
            )
        body = self._request_body(host_token_requested)
        try:
            status, payload = await self._post(credential, body)
        except _Unreachable as exc:
            self._back_off(str(exc))
        arrived = self._monotonic()
        if status == 200:
            try:
                answer = parse_answer(payload)
            except _Unusable as exc:
                self._back_off(f"the lease service's answer has no usable {exc}")
            self._apply(answer, arrived)
            return
        code, retry_after = refusal_of(payload)
        if code in _STOP_REFUSALS:
            self._stop(f"the lease service refused this Crew ({code}): {_STOP_REFUSALS[code]}")
        if code == _SIGN_IN_EXPIRED:
            if sign_in_retry:
                logger.warning(
                    "the lease service refused the Crew's sign-in (%s); reading it "
                    "again and retrying once",
                    code,
                )
                await self._call(
                    host_token_requested, endpoint_retry=endpoint_retry, sign_in_retry=False
                )
                return
            self._stop(
                f"the lease service refused the Crew's credential ({code}): update "
                "tunnel.relay_lease_credential's credential and restart Remote Control"
            )
        if code == _ENDPOINT_MISMATCH and endpoint_retry:
            logger.warning("the lease service refused the endpoint (%s); renewing without it", code)
            self._endpoint_id = None
            await self._call(
                host_token_requested, endpoint_retry=False, sign_in_retry=sign_in_retry
            )
            return
        if code in _WAIT_REFUSALS and retry_after is not None:
            wait = max(retry_after, RETRY_AFTER_MIN_SECONDS) * self._rng.uniform(
                1.0, 1.0 + RETRY_AFTER_JITTER
            )
            self._hold(arrived + wait, f"the lease service asked the Crew to wait ({code})")
            logger.warning("the lease service refused the renewal (%s); waiting %.0fs", code, wait)
            raise _Later(self._hold_reason)
        self._back_off(f"the lease service refused the renewal ({code or f'HTTP {status}'})")

    def _request_body(self, host_token_requested: bool) -> dict[str, Any]:
        relay: dict[str, Any] = {"connected": self._relay_connected()}
        if self._endpoint_id:
            relay["endpointId"] = self._endpoint_id
        body: dict[str, Any] = {
            "crewInstanceId": self._instance_id,
            "displayName": self._display_name or default_display_name(),
            "crewVersion": self._crew_version,
            "protocolVersion": PROTOCOL_VERSION,
            "hostKind": "crew",
            "relay": relay,
            # Known gap: the people connected now are not tracked yet.
            "connectedUsers": [],
        }
        if host_token_requested:
            body["hostTokenRequested"] = True
        return body

    def _relay_connected(self) -> bool:
        try:
            return self._connected() is True
        except Exception:  # noqa: BLE001 - a broken reader reports not connected
            logger.warning("reading the relay connection state failed", exc_info=True)
            return False

    def _client_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # trust_env=False: the call carries the Crew's credential, so the session
            # never reads netrc or any other credential from the environment. A proxy
            # set in the environment is still used: each call resolves it itself.
            self._session = aiohttp.ClientSession(trust_env=False)
        return self._session

    async def _post(self, credential: str, body: dict) -> Tuple[int, Any]:
        try:
            async with self._client_session().post(
                self._renew_url,
                json=body,
                headers={"Authorization": f"Bearer {credential}", "Accept": "application/json"},
                allow_redirects=False,
                proxy=env_proxy_for(self._renew_url),
                timeout=aiohttp.ClientTimeout(total=CALL_TIMEOUT_SECONDS),
            ) as response:
                status = response.status
                raw = await _read_capped(response.content, ANSWER_MAX_BYTES)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise _Unreachable(
                f"the lease service could not be reached ({type(exc).__name__})"
            ) from None
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        return status, payload

    def _apply(self, answer: LeaseAnswer, arrived: float) -> None:
        self._hold_until = 0.0
        self._hold_reason = ""
        self._backoff.reset()
        self._endpoint_id = answer.endpoint_id
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
