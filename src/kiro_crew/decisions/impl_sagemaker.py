"""System One questions sent to an Amazon SageMaker endpoint instead of over HTTP.

A System One model can be hosted on a SageMaker real-time endpoint whose container
answers the same ``/v1/systemone`` JSON body ``impl_jev`` sends. This module is that
client. It is off unless ``decisions.provider.endpoint`` names a SageMaker endpoint::

    sagemaker://<endpoint-name>?region=<aws-region>[&profile=<aws-profile>]

The request is signed with SigV4 by boto3 from the machine's own AWS credentials
(``profile`` picks a named profile; without it the default credential chain is
used). No API key is read or sent: SageMaker's ``InvokeEndpoint`` is the only call
made, always to ``runtime.sagemaker.<region>.amazonaws.com``. Consent is recorded
for the endpoint string exactly as written, so changing the endpoint name, the
region or the profile withdraws it, the same as changing an HTTP endpoint does.

``timeout_ms`` is the budget for the whole decision, as it is for the HTTP client.
A throttled or not-yet-ready answer is retried, at most :data:`MAX_ATTEMPTS`
attempts in total, and only while the budget has time left: every attempt's read
timeout is what remains of it, so no attempt outlives the gate's own wait. A
timeout is never retried.

The body and the answer are the ``/v1/systemone`` JSON. Images travel as
``assets`` with ``data_base64`` (a SageMaker request carries one body, so there is
no multipart form), and a request that carries images or a Box question sets the
custom attribute ``TypeSafe-Danger-Zone-Images=true``, which the container reads
in place of the HTTP opt-in header of the same name. :func:`invoke` sends such a
body and :func:`box_answers` reads the Box answers back; the gate's text
decisions go through :class:`SageMakerOracle`.

Nothing here logs a request, a response, an image or a credential. Errors carry a fixed
message and, for a container error, its status code only.

``boto3`` is imported only when a SageMaker endpoint is configured, and it is an
optional dependency: without it a request fails with :class:`SageMakerUnavailable`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlsplit

from kiro_crew.config.sections import DECISION_PROVIDER_MODEL_DEFAULT
from kiro_crew.constants import AWS_PROFILE_NAME_RE
from kiro_crew.decisions.types import Answers, Question

SCHEME = "sagemaker"

#: SageMaker endpoint names: letters, digits and single or repeated hyphens, up to
#: 63 characters, starting and ending with a letter or digit.
_ENDPOINT_NAME_RE = re.compile(r"\A[A-Za-z0-9](?:-*[A-Za-z0-9]){0,62}\Z")
#: An AWS region code such as ``us-west-2`` or ``us-gov-west-1``.
_REGION_RE = re.compile(r"\A[a-z]{2}(?:-[a-z]+)+-\d{1,2}\Z")
#: Region prefixes of the isolated partitions, whose hosts this module does not name.
_UNSUPPORTED_REGION_PREFIXES = ("us-iso", "eu-isoe")

#: The custom attribute that stands in for the ``TypeSafe-Danger-Zone-Images``
#: header, which ``InvokeEndpoint`` cannot pass through.
IMAGES_ATTRIBUTE = "TypeSafe-Danger-Zone-Images=true"
#: Image formats the ``/v1/systemone`` asset contract accepts.
IMAGE_FORMATS = frozenset({"png", "jpg", "jpeg"})


#: Largest answer read back. A System One answer is a few kilobytes.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
#: Largest request sent. SageMaker real-time inference refuses a body over 25 MB.
MAX_REQUEST_BYTES = 25 * 1000 * 1000

#: Attempts per decision, the first one included.
MAX_ATTEMPTS = 3
#: Wait before the second attempt; doubled before each later one.
_BACKOFF_SECS = 0.1
#: An attempt is not started with less than this left of the budget.
_MIN_ATTEMPT_SECS = 0.05
#: Read timeouts are rounded down to this step, so a retry reuses a cached client.
_TIMEOUT_STEP_SECS = 0.05
#: Connection setup never waits longer than this, whatever the budget is.
_CONNECT_TIMEOUT_CAP_SECS = 5.0
_MIN_TIMEOUT_SECS = 0.001

_DEFAULT_MODEL = DECISION_PROVIDER_MODEL_DEFAULT

_CREDENTIAL_CODES = frozenset(
    {
        "AccessDeniedException",
        "ExpiredTokenException",
        "InvalidClientTokenId",
        "InvalidSignatureException",
        "SignatureDoesNotMatch",
        "UnrecognizedClientException",
    }
)
_THROTTLE_CODES = frozenset({"ThrottlingException", "TooManyRequestsException"})
_UNAVAILABLE_CODES = frozenset(
    {
        "InternalDependencyException",
        "InternalFailure",
        "ModelNotReadyException",
        "ServiceUnavailable",
    }
)


class SageMakerError(RuntimeError):
    """A SageMaker request did not produce a usable answer."""


class SageMakerConfigError(SageMakerError):
    """The configured endpoint is not a valid ``sagemaker://`` address."""


class SageMakerUnavailable(SageMakerError):
    """boto3 is not installed, or the endpoint is not ready to answer."""


class SageMakerCredentialsError(SageMakerError):
    """No AWS credentials were found, or AWS refused the ones that were used."""


class SageMakerEndpointNotFound(SageMakerError):
    """No endpoint by that name exists in that region for these credentials."""


class SageMakerTimeout(SageMakerError):
    """The endpoint did not answer within the budget."""


class SageMakerThrottled(SageMakerError):
    """The endpoint was still throttling when the attempts or the budget ran out."""


class SageMakerModelError(SageMakerError):
    """The model container answered with an error status."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SageMakerProtocolError(SageMakerError):
    """The endpoint answered, but not with a ``/v1/systemone`` body."""


#: Errors worth another attempt: the endpoint answered quickly that it cannot answer yet.
_RETRYABLE = (SageMakerThrottled, SageMakerUnavailable)


@dataclass(frozen=True)
class SageMakerTarget:
    """Where one request goes: an endpoint name, its region and an optional profile."""

    endpoint_name: str
    region: str
    profile: str = ""


def is_sagemaker_endpoint(endpoint: object) -> bool:
    """Whether *endpoint* uses the ``sagemaker://`` scheme, valid or not."""
    return isinstance(endpoint, str) and endpoint.strip().lower().startswith(f"{SCHEME}://")


def parse_endpoint(endpoint: object) -> SageMakerTarget:
    """The target *endpoint* names, or raise :class:`SageMakerConfigError`.

    Strict on purpose: ``config.json`` is agent-writable, so only the three known
    parts are read, each against its own pattern, and anything else in the address
    (a path, user info, a port, another query key, a repeated key) is refused
    rather than ignored.
    """
    if not is_sagemaker_endpoint(endpoint):
        raise SageMakerConfigError("endpoint is not a sagemaker:// address")
    parts = urlsplit(str(endpoint).strip())
    if parts.path not in ("", "/") or parts.fragment or "@" in parts.netloc or ":" in parts.netloc:
        raise SageMakerConfigError("sagemaker:// address has parts other than name and query")
    name = parts.netloc
    if not _ENDPOINT_NAME_RE.match(name):
        raise SageMakerConfigError("sagemaker:// endpoint name is not valid")
    query: dict[str, str] = {}
    # Split by hand: ``parse_qsl`` reads ``+`` as a space, and ``+`` is legal in a
    # profile name.
    for pair in parts.query.split("&") if parts.query else ():
        key, _, value = pair.partition("=")
        key, value = unquote(key), unquote(value)
        if key not in ("region", "profile") or key in query:
            raise SageMakerConfigError("sagemaker:// address has an unknown or repeated key")
        query[key] = value
    region = query.get("region", "")
    if not _REGION_RE.match(region) or region.startswith(_UNSUPPORTED_REGION_PREFIXES):
        raise SageMakerConfigError("sagemaker:// address needs a valid region=")
    profile = query.get("profile", "")
    if profile and not AWS_PROFILE_NAME_RE.match(profile):
        raise SageMakerConfigError("sagemaker:// profile name is not valid")
    return SageMakerTarget(endpoint_name=name, region=region, profile=profile)


def runtime_url(target: SageMakerTarget) -> str:
    """The SageMaker runtime URL a request to *target* is sent to.

    The client is pinned to this URL, so an ``AWS_ENDPOINT_URL`` variable or an
    ``endpoint_url`` in the AWS config cannot send it anywhere else, and the
    governance ``endpoints`` list is asked about this host.
    """
    suffix = "amazonaws.com.cn" if target.region.startswith("cn-") else "amazonaws.com"
    return f"https://runtime.sagemaker.{target.region}.{suffix}"


def policy_endpoint(endpoint: object) -> str:
    """The URL governance checks for a ``sagemaker://`` *endpoint*. Raises if invalid."""
    target = parse_endpoint(endpoint)
    return f"{runtime_url(target)}/endpoints/{target.endpoint_name}/invocations"


def image_asset(data: bytes, fmt: str) -> dict[str, str]:
    """One ``assets`` value carrying *data* inline, as the asset contract spells it."""
    fmt = fmt.lower()
    if fmt not in IMAGE_FORMATS:
        raise SageMakerConfigError("image format must be png, jpg or jpeg")
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise SageMakerConfigError("image data must be non-empty bytes")
    return {
        "type": "image",
        "data_base64": base64.b64encode(bytes(data)).decode("ascii"),
        "format": fmt,
    }


def needs_images_opt_in(body: Mapping[str, Any]) -> bool:
    """Whether *body* carries images or a Box question, so it needs the opt-in."""
    if body.get("assets"):
        return True
    questions = body.get("questions")
    if isinstance(questions, dict):
        return any(isinstance(q, dict) and q.get("type") == "box" for q in questions.values())
    return False


@dataclass(frozen=True)
class BoxAnswer:
    """A Box answer: the four edges as fractions of the image, and a confidence."""

    top: float
    bottom: float
    left: float
    right: float
    confidence: float | None = None


def _finite_unit(raw: Any) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    return value if math.isfinite(value) and 0.0 <= value <= 1.0 else None


def parse_box(raw: object) -> BoxAnswer:
    """Read one ``{"type": "box", "box": {...}, "confidence"}`` answer, or raise.

    Each edge must be a finite number in 0..1 with ``left < right`` and
    ``top < bottom``; a confidence, when present, must be in 0..1 too.
    """
    if not isinstance(raw, dict) or raw.get("type") != "box":
        raise SageMakerProtocolError("answer is not a box answer")
    box = raw.get("box")
    if not isinstance(box, dict):
        raise SageMakerProtocolError("box answer has no 'box' object")
    edges = {side: _finite_unit(box.get(side)) for side in ("top", "bottom", "left", "right")}
    top, bottom, left, right = edges["top"], edges["bottom"], edges["left"], edges["right"]
    if top is None or bottom is None or left is None or right is None:
        raise SageMakerProtocolError("box edge is not a number in 0..1")
    if not (left < right and top < bottom):
        raise SageMakerProtocolError("box edges are out of order")
    confidence = None
    if raw.get("confidence") is not None:
        confidence = _finite_unit(raw.get("confidence"))
        if confidence is None:
            raise SageMakerProtocolError("box confidence is not a number in 0..1")
    return BoxAnswer(top=top, bottom=bottom, left=left, right=right, confidence=confidence)


def box_answers(response: Mapping[str, Any]) -> dict[str, BoxAnswer]:
    """Every Box answer in a ``/v1/systemone`` *response*, keyed by question id."""
    answers = response.get("answers")
    if not isinstance(answers, dict):
        raise SageMakerProtocolError("response has no 'answers' object")
    return {
        qid: parse_box(raw)
        for qid, raw in answers.items()
        if isinstance(raw, dict) and raw.get("type") == "box"
    }


def _budget_secs(timeout_ms: object) -> float:
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, (int, float)):
        return 1.0
    value = float(timeout_ms)
    if not math.isfinite(value):
        return 1.0
    return max(_MIN_TIMEOUT_SECS, value / 1000.0)


#: One client per (region, profile, read timeout), so a decision does not build a
#: fresh client, credential chain and connection pool every time. Bounded, oldest first.
_CLIENTS_MAX = 8
_clients: "OrderedDict[tuple[str, str, float], Any]" = OrderedDict()
_clients_lock = threading.Lock()


def _make_client(target: SageMakerTarget, read_timeout_secs: float) -> Any:
    """A ``sagemaker-runtime`` client for *target*. Raises a :class:`SageMakerError`.

    botocore's own retries are off: :func:`invoke_sync` retries within the budget.
    """
    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ProfileNotFound
    except ImportError:
        raise SageMakerUnavailable(
            "boto3 is not installed; install it to use a sagemaker:// decision endpoint"
        ) from None
    config = Config(
        connect_timeout=min(_CONNECT_TIMEOUT_CAP_SECS, read_timeout_secs),
        read_timeout=read_timeout_secs,
        retries={"total_max_attempts": 1, "mode": "standard"},
    )
    try:
        session = boto3.session.Session(
            profile_name=target.profile or None, region_name=target.region
        )
        return session.client("sagemaker-runtime", config=config, endpoint_url=runtime_url(target))
    except ProfileNotFound:
        raise SageMakerCredentialsError("the configured AWS profile does not exist") from None
    except BotoCoreError as exc:
        raise SageMakerUnavailable(
            f"could not create the SageMaker client ({type(exc).__name__})"
        ) from None


def _client_for(target: SageMakerTarget, read_timeout_secs: float) -> Any:
    key = (target.region, target.profile, read_timeout_secs)
    with _clients_lock:
        client = _clients.get(key)
        if client is not None:
            _clients.move_to_end(key)
            return client
    client = _make_client(target, read_timeout_secs)
    with _clients_lock:
        _clients[key] = client
        while len(_clients) > _CLIENTS_MAX:
            _clients.popitem(last=False)
    return client


def _clear_clients() -> None:
    """Drop every cached client; a later request builds a new one."""
    with _clients_lock:
        _clients.clear()


def _client_error(exc: Any) -> SageMakerError:
    """Map a botocore ``ClientError`` onto one of this module's errors."""
    error = (getattr(exc, "response", None) or {}).get("Error", {}) or {}
    code = str(error.get("Code", "") or "")
    message = str(error.get("Message", "") or "")
    if code in _CREDENTIAL_CODES:
        return SageMakerCredentialsError(f"AWS refused the credentials ({code})")
    if code in _THROTTLE_CODES:
        return SageMakerThrottled("endpoint is throttling requests")
    if code in ("ValidationError", "ValidationException") and "not found" in message.lower():
        return SageMakerEndpointNotFound("no SageMaker endpoint by that name in that region")
    if code == "ModelError":
        raw_status = (getattr(exc, "response", None) or {}).get("OriginalStatusCode")
        status = raw_status if isinstance(raw_status, int) else None
        return SageMakerModelError(
            f"model container answered with status {status if status is not None else 'unknown'}",
            status=status,
        )
    if code in _UNAVAILABLE_CODES:
        return SageMakerUnavailable(f"endpoint is not ready to answer ({code})")
    return SageMakerError(f"SageMaker request failed ({code or 'unknown error'})")


def _read_body(stream: Any) -> bytes:
    data = stream.read(MAX_RESPONSE_BYTES + 1) if stream is not None else b""
    if not isinstance(data, (bytes, bytearray)):
        raise SageMakerProtocolError("response body is not bytes")
    if len(data) > MAX_RESPONSE_BYTES:
        raise SageMakerProtocolError(f"response exceeded {MAX_RESPONSE_BYTES} bytes")
    return bytes(data)


def _invoke_once(
    target: SageMakerTarget, payload: bytes, read_timeout_secs: float, images: bool = False
) -> Any:
    """One ``InvokeEndpoint`` call. Raises a :class:`SageMakerError` subclass."""
    from botocore.exceptions import (
        ClientError,
        ConnectTimeoutError,
        EndpointConnectionError,
        NoCredentialsError,
        PartialCredentialsError,
        ReadTimeoutError,
    )

    client = _client_for(target, read_timeout_secs)
    kwargs: dict[str, Any] = {
        "EndpointName": target.endpoint_name,
        "Body": payload,
        "ContentType": "application/json",
        "Accept": "application/json",
    }
    if images:
        kwargs["CustomAttributes"] = IMAGES_ATTRIBUTE
    try:
        return client.invoke_endpoint(**kwargs)
    except (NoCredentialsError, PartialCredentialsError):
        raise SageMakerCredentialsError("no AWS credentials found for the SageMaker call") from None
    except (ReadTimeoutError, ConnectTimeoutError):
        raise SageMakerTimeout("no answer within the decision budget") from None
    except EndpointConnectionError:
        raise SageMakerUnavailable("could not reach SageMaker in that region") from None
    except ClientError as exc:
        raise _client_error(exc) from None


def invoke_sync(
    target: SageMakerTarget,
    body: Mapping[str, Any],
    timeout_ms: object,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict, str | None]:
    """Send *body* to *target* and return ``(parsed answer, request id)``. Blocking.

    Raises a :class:`SageMakerError` subclass for every failure; none of their
    messages quote the request or the response.
    """
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_REQUEST_BYTES:
        raise SageMakerConfigError(f"request exceeds {MAX_REQUEST_BYTES} bytes")
    images = needs_images_opt_in(body)
    budget = _budget_secs(timeout_ms)
    deadline = clock() + budget
    attempt = 0
    last_error: SageMakerError | None = None
    while True:
        attempt += 1
        # A retry reads for what is left of the budget, rounded down to a step so
        # retries reuse a cached client.
        read_timeout = budget
        if attempt > 1:
            steps = math.floor((deadline - clock()) / _TIMEOUT_STEP_SECS)
            read_timeout = steps * _TIMEOUT_STEP_SECS
        if read_timeout < _MIN_ATTEMPT_SECS:
            # The backoff overran the budget, so the gate has stopped waiting.
            raise last_error or SageMakerTimeout("no answer within the decision budget")
        try:
            resp = _invoke_once(target, payload, read_timeout, images)
            break
        except _RETRYABLE as exc:
            last_error = exc
            backoff = _BACKOFF_SECS * 2 ** (attempt - 1)
            if attempt >= MAX_ATTEMPTS or deadline - clock() - backoff < _MIN_ATTEMPT_SECS:
                raise
            sleep(backoff)
    raw = _read_body(resp.get("Body"))
    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        raise SageMakerProtocolError("response is not JSON") from None
    if not isinstance(parsed, dict):
        raise SageMakerProtocolError("response is not an object")
    request_id = (resp.get("ResponseMetadata") or {}).get("RequestId")
    return parsed, request_id if isinstance(request_id, str) else None


async def invoke(
    endpoint: str, body: Mapping[str, Any], timeout_ms: object = 1000
) -> dict[str, Any]:
    """Send a full ``/v1/systemone`` *body* (``assets`` and Box questions allowed).

    Returns the parsed answer as the container wrote it; read Box answers from it
    with :func:`box_answers`. Raises a :class:`SageMakerError` subclass.
    """
    target = parse_endpoint(endpoint)
    parsed, _ = await asyncio.to_thread(invoke_sync, target, body, timeout_ms)
    return parsed


class SageMakerOracle:
    """The gate's Jev lane, answered by a SageMaker endpoint instead of HTTP.

    Same request and answer shape as :class:`~kiro_crew.decisions.impl_jev.JevOracle`;
    only the transport and the credential differ.
    """

    def __init__(self, provider: Any) -> None:
        self._endpoint = str(getattr(provider, "endpoint", "") or "")
        self._model = str(getattr(provider, "model", "") or _DEFAULT_MODEL)
        self._timeout_ms = getattr(provider, "timeout_ms", 1000)
        self.last_response_meta: dict[str, Any] | None = None

    async def ask(self, state: dict | str, questions: list[Question]) -> Answers:
        """Send one request carrying every question. Raises on any failure."""
        from kiro_crew.decisions.impl_systemone import _from_wire, _response_meta, _to_wire

        if not questions:
            raise SageMakerProtocolError("no questions to ask")
        target = parse_endpoint(self._endpoint)
        body = _to_wire(state, self._model, questions)
        parsed, request_id = await asyncio.to_thread(invoke_sync, target, body, self._timeout_ms)
        answers = _from_wire(parsed, questions)
        self.last_response_meta = _response_meta(parsed, request_id)
        return answers
