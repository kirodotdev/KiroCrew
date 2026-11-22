"""System One questions sent to an HTTPS endpoint that authenticates with AWS SigV4.

Some System One services sit behind a URL of their own (a custom domain, an API
gateway, a service front door) and authenticate callers with AWS Signature Version 4
rather than an API key. This module is that client. It is off unless
``decisions.provider.endpoint`` names such an endpoint::

    aws-https://<host>[:<port>]/<path>?service=<signing-service>&region=<aws-region>[&profile=<aws-profile>]

The request is ``POST https://<host>[:<port>]/<path>`` with the same JSON body
``impl_systemone`` sends, and the answer is read by that module's own reader. The
body is signed with SigV4 for *service* and *region* from the machine's own AWS
credentials (``profile`` picks a named profile; without it the default credential
chain is used). No API key is read or sent. Consent is recorded for the endpoint
string as written, so changing the host, path, service, region or profile withdraws
it.

The address is held to a strict shape because ``config.json`` is agent-writable: an
``https`` host name (no user info, no IP literal), an optional port, a plain path,
and exactly the three known query keys. The governance ``endpoints`` list is asked
about the ``https://`` URL the request reaches (:func:`policy_endpoint`).

Text questions only: this provider declares no image support, so the gate never
hands it an image or a Box question.

``botocore`` is imported only when such an endpoint is configured, and it is an
optional dependency: without it a request fails with :class:`SignedHttpsUnavailable`.
Nothing here logs a request, a response or a credential.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from kiro_crew.config.sections import DECISION_PROVIDER_MODEL_DEFAULT
from kiro_crew.constants import AWS_PROFILE_NAME_RE
from kiro_crew.decisions.types import Answers, Question

SCHEME = "aws-https"

#: One DNS label: letters, digits and hyphens, 1-63 characters, no hyphen at either end.
_LABEL_RE = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
#: A path made of unreserved characters and slashes only, so nothing in it needs
#: escaping and what is signed is exactly what is sent.
_PATH_RE = re.compile(r"\A(?:/[A-Za-z0-9._~-]*)*\Z")
#: An AWS SigV4 signing service name, such as ``execute-api`` or ``lambda``.
_SERVICE_RE = re.compile(r"\A[a-z0-9](?:[a-z0-9-]{0,62})\Z")
#: An AWS region code such as ``us-west-2`` or ``us-gov-west-1``.
_REGION_RE = re.compile(r"\A(?=.{1,63}\Z)[a-z]{2}(?:-[a-z]+)+-\d{1,2}\Z")
_MAX_HOST_CHARS = 253
_MAX_PATH_CHARS = 512

#: The response header carrying the service's request id.
REQUEST_ID_HEADER = "x-amzn-requestid"

_DEFAULT_MODEL = DECISION_PROVIDER_MODEL_DEFAULT


class SignedHttpsError(RuntimeError):
    """A request to a SigV4 HTTPS endpoint did not produce a usable answer."""


class SignedHttpsConfigError(SignedHttpsError):
    """The configured endpoint is not a valid ``aws-https://`` address."""


class SignedHttpsAddressFieldError(SignedHttpsConfigError):
    """One part :func:`build_endpoint` was given is not valid; ``field`` names it."""

    def __init__(self, field: str) -> None:
        super().__init__(f"aws-https:// {field} is not valid")
        self.field = field


class SignedHttpsUnavailable(SignedHttpsError):
    """botocore is not installed, or the endpoint could not be reached."""


class SignedHttpsCredentialsError(SignedHttpsError):
    """No AWS credentials were found, or the endpoint refused the signed request."""


class SignedHttpsHttpError(SignedHttpsError):
    """The endpoint answered with a non-2xx status."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


@dataclass(frozen=True)
class SignedHttpsTarget:
    """Where one request goes and how it is signed."""

    #: ``host`` or ``host:port``, lower-cased.
    authority: str
    #: The request path, starting with ``/``.
    path: str
    service: str
    region: str
    profile: str = ""

    @property
    def url(self) -> str:
        """The ``https://`` URL the request is sent to."""
        return f"https://{self.authority}{self.path}"


def is_signed_https_endpoint(endpoint: object) -> bool:
    """Whether *endpoint* uses the ``aws-https://`` scheme, valid or not."""
    return isinstance(endpoint, str) and endpoint.strip().lower().startswith(f"{SCHEME}://")


def _authority(netloc: str) -> str:
    """*netloc* as a checked ``host[:port]``, lower-cased. Raises ``ValueError``.

    A host name only: user info, an IPv6 literal and an all-numeric last label (an
    IPv4 literal) are refused, so the address always names a host by name.
    """
    if not netloc or "@" in netloc or "[" in netloc or "]" in netloc:
        raise ValueError("authority")
    host, sep, port = netloc.partition(":")
    if sep:
        if not port.isdigit() or not 1 <= int(port) <= 65535 or port != str(int(port)):
            raise ValueError("port")
    host = host.lower()
    if len(host) > _MAX_HOST_CHARS:
        raise ValueError("host")
    labels = host.split(".")
    if not all(_LABEL_RE.match(label) for label in labels) or labels[-1].isdigit():
        raise ValueError("host")
    return f"{host}:{port}" if sep else host


def _path(raw: str) -> str:
    """*raw* as a checked request path; an empty path is ``/``. Raises ``ValueError``."""
    path = raw or "/"
    if (
        len(path) > _MAX_PATH_CHARS
        or not _PATH_RE.match(path)
        or "//" in path
        or any(part in (".", "..") for part in path.split("/"))
    ):
        raise ValueError("path")
    return path


def parse_endpoint(endpoint: object) -> SignedHttpsTarget:
    """The target *endpoint* names, or raise :class:`SignedHttpsConfigError`.

    Strict on purpose: only the known parts are read, each against its own pattern,
    and anything else (user info, a fragment, another or a repeated query key) is
    refused rather than ignored.
    """
    if not is_signed_https_endpoint(endpoint):
        raise SignedHttpsConfigError("endpoint is not an aws-https:// address")
    try:
        parts = urlsplit(str(endpoint).strip())
        if parts.fragment:
            raise ValueError("fragment")
        authority = _authority(parts.netloc)
        path = _path(parts.path)
    except ValueError:
        raise SignedHttpsConfigError(
            "aws-https:// address does not name a valid host and path"
        ) from None
    query: dict[str, str] = {}
    # Split by hand: ``parse_qsl`` reads ``+`` as a space, and ``+`` is legal in a
    # profile name.
    for pair in parts.query.split("&") if parts.query else ():
        key, _, value = pair.partition("=")
        key, value = unquote(key), unquote(value)
        if key not in ("service", "region", "profile") or key in query:
            raise SignedHttpsConfigError("aws-https:// address has an unknown or repeated key")
        query[key] = value
    service = query.get("service", "")
    if not _SERVICE_RE.match(service):
        raise SignedHttpsConfigError("aws-https:// address needs a valid service=")
    region = query.get("region", "")
    if not _REGION_RE.match(region):
        raise SignedHttpsConfigError("aws-https:// address needs a valid region=")
    profile = query.get("profile", "")
    if profile and not AWS_PROFILE_NAME_RE.match(profile):
        raise SignedHttpsConfigError("aws-https:// profile name is not valid")
    return SignedHttpsTarget(
        authority=authority, path=path, service=service, region=region, profile=profile
    )


def build_endpoint(url: object, service: object, region: object, profile: object = "") -> str:
    """The ``aws-https://`` address for these parts, or raise :class:`SignedHttpsAddressFieldError`.

    *url* is the ``https://`` URL the owner typed: ``https`` only, a host name, an
    optional port and a plain path, with no user info, query or fragment. Each part
    is held to the pattern :func:`parse_endpoint` reads it with, and the address is
    parsed back before it is returned, so it names these parts and nothing else.
    """
    if not isinstance(url, str):
        raise SignedHttpsAddressFieldError("url")
    try:
        parts = urlsplit(url.strip())
        if parts.scheme.lower() != "https" or parts.query or parts.fragment or "?" in url:
            raise ValueError("url")
        authority = _authority(parts.netloc)
        path = _path(parts.path)
    except ValueError:
        raise SignedHttpsAddressFieldError("url") from None
    if not isinstance(service, str) or not _SERVICE_RE.match(service):
        raise SignedHttpsAddressFieldError("service")
    if not isinstance(region, str) or not _REGION_RE.match(region):
        raise SignedHttpsAddressFieldError("region")
    if not isinstance(profile, str) or (profile and not AWS_PROFILE_NAME_RE.match(profile)):
        raise SignedHttpsAddressFieldError("profile")
    address = f"{SCHEME}://{authority}{path}?service={service}&region={region}"
    if profile:
        address += f"&profile={profile}"
    expected = SignedHttpsTarget(authority, path, service, region, profile)
    if parse_endpoint(address) != expected:
        raise SignedHttpsAddressFieldError("url")
    return address


def policy_endpoint(endpoint: object) -> str:
    """The ``https://`` URL governance checks for an ``aws-https://`` *endpoint*. Raises if invalid."""
    return parse_endpoint(endpoint).url


def botocore_installed() -> bool:
    """Whether ``botocore`` can be imported, without importing it."""
    import importlib.util

    return importlib.util.find_spec("botocore") is not None


#: One botocore session per profile, so a decision reuses the resolved (and
#: refreshable) credentials instead of walking the credential chain every time.
_SESSIONS_MAX = 8
_sessions: dict[str, Any] = {}
_sessions_lock = threading.Lock()


def _clear_sessions() -> None:
    """Drop every cached botocore session; a later request builds a new one."""
    with _sessions_lock:
        _sessions.clear()


def _frozen_credentials(profile: str) -> Any:
    """Frozen AWS credentials for *profile* (default chain when empty). Blocking.

    Raises :class:`SignedHttpsUnavailable` without botocore and
    :class:`SignedHttpsCredentialsError` when none can be loaded. The error names the
    botocore class only: its text can name a profile, a role ARN or a token path.
    """
    try:
        import botocore.session
    except ImportError:
        raise SignedHttpsUnavailable(
            "botocore is not installed; install boto3 to use an aws-https:// decision endpoint"
        ) from None
    try:
        with _sessions_lock:
            session = _sessions.get(profile)
            if session is None:
                session = botocore.session.Session(profile=profile or None)
                if len(_sessions) >= _SESSIONS_MAX:
                    _sessions.clear()
                _sessions[profile] = session
        credentials = session.get_credentials()
        frozen = credentials.get_frozen_credentials() if credentials is not None else None
    except Exception as exc:
        raise SignedHttpsCredentialsError(
            f"AWS credentials could not be loaded ({type(exc).__name__})"
        ) from None
    if frozen is None or not frozen.access_key or not frozen.secret_key:
        raise SignedHttpsCredentialsError("no AWS credentials were found")
    return frozen


def signed_headers(target: SignedHttpsTarget, payload: bytes, credentials: Any) -> dict[str, str]:
    """The headers of a SigV4-signed ``POST`` of *payload* to *target*.

    Signs the exact URL and body that are sent. The caller sends these headers
    unchanged; anything its HTTP client adds afterwards is outside the signature.
    """
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    request = AWSRequest(
        method="POST",
        url=target.url,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    SigV4Auth(credentials, target.service, target.region).add_auth(request)
    return dict(request.headers.items())


class SignedHttpsProvider:
    """The gate's endpoint lane, answered by a SigV4-signed HTTPS endpoint.

    Same request and answer shape as
    :class:`~kiro_crew.decisions.impl_systemone.SystemOneProvider`; only the
    credential differs.
    """

    def __init__(self, provider: Any) -> None:
        self._endpoint = str(getattr(provider, "endpoint", "") or "")
        self._model = str(getattr(provider, "model", "") or _DEFAULT_MODEL)
        self._timeout_ms = getattr(provider, "timeout_ms", 1000)
        self.last_response_meta: dict[str, Any] | None = None

    async def ask(self, state: dict | str, questions: list[Question]) -> Answers:
        """POST one signed request carrying every question. Raises on any failure.

        Error text never quotes the request, the response or a credential.
        """
        import httpx

        from kiro_crew.decisions import impl_systemone as s1

        if not questions:
            raise s1.ProviderProtocolError("no questions to ask")
        target = parse_endpoint(self._endpoint)
        body = s1._to_wire(state, self._model, questions)
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        credentials = await asyncio.to_thread(_frozen_credentials, target.profile)
        headers = signed_headers(target, payload, credentials)
        timeout_ms = s1._as_float_or_none(self._timeout_ms)
        seconds = max(0.001, (timeout_ms or 0.0) / 1000.0)
        session = s1._session_for_running_loop()
        # The shared client never follows a redirect: consent is bound to THIS
        # endpoint, and a 3xx is refused below as a non-2xx.
        try:
            async with asyncio.timeout(seconds):
                async with session.stream(
                    "POST",
                    target.url,
                    content=payload,
                    headers=headers,
                    timeout=httpx.Timeout(seconds),
                ) as resp:
                    if resp.status_code < 200 or resp.status_code >= 300:
                        if resp.status_code in (401, 403):
                            raise SignedHttpsCredentialsError(
                                f"the endpoint refused the signed request (HTTP {resp.status_code})"
                            )
                        raise SignedHttpsHttpError(resp.status_code)
                    request_id = resp.headers.get(REQUEST_ID_HEADER)
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes(s1._RESPONSE_CHUNK_BYTES):
                        total += len(chunk)
                        if total > s1._MAX_RESPONSE_BYTES:
                            raise s1.ProviderProtocolError(
                                f"response exceeded {s1._MAX_RESPONSE_BYTES} bytes"
                            )
                        chunks.append(chunk)
        except httpx.TimeoutException:
            # The gate reads a timeout by its class; keep it one.
            raise asyncio.TimeoutError from None
        except httpx.TransportError as exc:
            raise SignedHttpsUnavailable(
                f"could not reach the endpoint ({type(exc).__name__})"
            ) from None
        try:
            parsed = json.loads(b"".join(chunks).decode("utf-8", errors="replace"))
        except ValueError:
            raise s1.ProviderProtocolError("response is not JSON") from None
        answers = s1._from_wire(parsed, questions)
        self.last_response_meta = s1._response_meta(parsed, request_id)
        return answers
