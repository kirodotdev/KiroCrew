"""Kiro calls: the gateway calls an approved Kiro BFF operation for an app, as the person.

An app names the operations it needs in ``permissions.kiroOperations``
(``apps/kiro_operations.py``) and the owner approves them as a staged grant. Two
callers reach :func:`call_kiro_operation`:

* ``POST /api/apps/{name}/kiro/{operation}`` (``apps/routes_kiro.py``): the app's
  backend with its own app token, or the host owner's dashboard session;
* ``await ctx.kiro_call(operation, input)`` (``apps/context.py``): the app's
  in-gateway hooks.

The order is fixed, and a refusal at any step sends nothing upstream:

1. ``operation in app_kiro_operations(app)``: exact set membership;
2. the input is a JSON object within :data:`MAX_REQUEST_BYTES` and
   :data:`MAX_INPUT_DEPTH`;
3. at most :data:`MAX_IN_FLIGHT` calls of one app run at once;
4. the upstream signs the call (credential and fixed endpoint);
5. encode, send with no redirect and a :data:`TIMEOUT_S` bound, decode at most
   :data:`MAX_RESPONSE_BYTES`, answer.

**Upstream seam.** Which credential and which fixed endpoint a call uses is one
:class:`Upstream` (:data:`KIRO_UPSTREAM`). It answers a :class:`SignedCall`: the
base URL and the headers that carry the credential. Nothing outside the upstream
reads the vault or picks a host, so another provider would be one more upstream,
not a change to the call path.

**Kiro.** The credential is the gateway's own Kiro sign-in
(``KasAuthProvider(default_token_store(), allow_env_api_key=False)``), renewed
inside its refresh margin, and renewed once more when the BFF answers 401 or 403.
The endpoint comes from the signed-in profile's region through :data:`KIRO_ORIGINS`;
a region not in the table is refused, never sent to another region's host. The app
chooses no host, path prefix or header.

The token never leaves this process: it is in no log line, no error, no answer and
no process environment. :class:`SignedCall` hides its headers from ``repr``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import aiohttp

from kiro_crew.apps.kiro_operations import app_kiro_operations, declared_kiro_operations
from kiro_crew.apps.rpc_cbor import cbor_decode, cbor_encode
from kiro_crew.http_proxy import resolve_proxy

logger = logging.getLogger(__name__)

#: The BFF's Smithy service: the one fixed part of the path.
KIRO_SERVICE = "KiroWebBearerService"
#: The BFF origin for each Kiro profile region. A region not here is refused.
KIRO_ORIGINS: dict[str, str] = {
    "us-east-1": "https://app.kiro.dev",
    "eu-central-1": "https://eu-central-1.app.kiro.dev",
}
#: The region of a sign-in that names no profile ARN, as the engine assumes.
KIRO_DEFAULT_REGION = "us-east-1"
#: The ``X-Kiro-Idp`` value the BFF accepts for each stored sign-in provider.
_KIRO_IDP_HEADER = {
    "Google": "Google",
    "Github": "Github",
    "BuilderId": "BuilderId",
    "Enterprise": "AWSIdC",
    "Internal": "AWSIdC",
    "ExternalIdp": "ExternalOIDC",
}
#: The ``TokenType`` header a stored auth method declares.
_KIRO_TOKEN_TYPE = {"external_idp": "EXTERNAL_IDP", "IdC": "SSO_OIDC"}

#: Largest JSON input read from a caller.
MAX_REQUEST_BYTES = 1024 * 1024
#: Deepest nesting of a JSON input.
MAX_INPUT_DEPTH = 32
#: Largest upstream answer read.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
#: Seconds one call may take, connect included.
TIMEOUT_S = 30.0
#: Calls of one app that may run at once.
MAX_IN_FLIGHT = 8
#: Longest upstream error message passed back to the app.
MAX_ERROR_MESSAGE = 300
SIGN_IN_HINT = "Sign in to Kiro in Settings."

_PROTOCOL = "rpc-v2-cbor"
_CBOR = "application/cbor"
_CHUNK = 64 * 1024


class KiroCallError(Exception):
    """A call that did not succeed. ``code`` and ``status`` are the route's error body.

    ``kiro`` carries the upstream's own status, error type and message when the
    upstream answered an error. Nothing here holds a credential.
    """

    def __init__(
        self, status: int, code: str, message: str, *, kiro: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.kiro = kiro

    def body(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.kiro is not None:
            error["kiro"] = self.kiro
        return {"error": error}


@dataclass(frozen=True)
class SignedCall:
    """Where one call goes and the headers that carry its credential. ``repr`` hides them."""

    base_url: str
    headers: dict[str, str] = field(repr=False)
    #: Handed back to :meth:`Upstream.sign` to renew a refused credential.
    rejected: str = field(default="", repr=False)


class Upstream(Protocol):
    """One credential and its fixed endpoint: the only part that differs per provider."""

    #: The Smithy service in the path, fixed by the upstream.
    service: str

    async def sign(self, *, rejected: SignedCall | None = None) -> SignedCall:
        """The signed call, renewing the credential *rejected* carried. Raises KiroCallError."""
        ...


def _sign_in_error(status: int, code: str, what: str) -> KiroCallError:
    return KiroCallError(status, code, f"{what} {SIGN_IN_HINT}")


def _default_provider() -> Any:
    from kiro_crew.auth.bridge import default_token_store  # noqa: PLC0415
    from kiro_crew.auth.provider import KasAuthProvider  # noqa: PLC0415

    return KasAuthProvider(default_token_store(), allow_env_api_key=False)


class KiroSignIn:
    """The gateway's own Kiro sign-in, sent to the BFF of the signed-in profile's region."""

    service = KIRO_SERVICE

    def __init__(
        self,
        provider: Callable[[], Any] = _default_provider,
        origins: dict[str, str] | None = None,
    ) -> None:
        self._provider = provider
        self._origins = KIRO_ORIGINS if origins is None else origins

    async def sign(self, *, rejected: SignedCall | None = None) -> SignedCall:
        from kiro_crew.auth.login.control_plane import (  # noqa: PLC0415
            ControlPlaneError,
            profile_region,
        )
        from kiro_crew.auth.provider import NotAuthenticated, kas_profile_arn  # noqa: PLC0415

        try:
            token = await self._provider().current(
                rejected_access_token=rejected.rejected if rejected else None
            )
        except NotAuthenticated:
            raise _sign_in_error(409, "kiro_sign_in_required", "Not signed in to Kiro.") from None
        except Exception as exc:  # noqa: BLE001 - a refresh error can quote response bytes
            raise _sign_in_error(
                503,
                "kiro_sign_in_unavailable",
                f"The Kiro sign-in could not be read or renewed ({type(exc).__name__}).",
            ) from None
        access = getattr(token, "access_token", None)
        if not isinstance(access, str) or not access:
            raise _sign_in_error(409, "kiro_sign_in_required", "Not signed in to Kiro.")
        idp = _KIRO_IDP_HEADER.get(getattr(token, "provider", None) or "")
        if idp is None:
            raise _sign_in_error(
                503, "kiro_sign_in_unavailable", "The Kiro sign-in's provider is not accepted."
            )
        arn = kas_profile_arn(token)
        try:
            region = profile_region(arn) if arn else KIRO_DEFAULT_REGION
        except ControlPlaneError:
            raise _sign_in_error(
                503, "kiro_sign_in_unavailable", "The Kiro sign-in names no Kiro profile."
            ) from None
        origin = self._origins.get(region)
        if origin is None:
            raise KiroCallError(
                503,
                "kiro_region_unsupported",
                f"Kiro calls are not available for the profile region {region!r}.",
            )
        headers = {"Authorization": f"Bearer {access}", "X-Kiro-Idp": idp}
        if arn:
            headers["X-Kiro-Profile-Arn"] = arn
        token_type = _KIRO_TOKEN_TYPE.get(getattr(token, "auth_method", None) or "")
        if token_type:
            headers["TokenType"] = token_type
        return SignedCall(base_url=origin, headers=headers, rejected=access)


#: The one upstream the Kiro call path uses.
KIRO_UPSTREAM: Upstream = KiroSignIn()


# ---------------------------------------------------------------------------
# Input and output
# ---------------------------------------------------------------------------


def _depth_ok(value: Any, depth: int = 0) -> bool:
    if depth > MAX_INPUT_DEPTH:
        return False
    if isinstance(value, dict):
        return all(_depth_ok(v, depth + 1) for v in value.values())
    if isinstance(value, list):
        return all(_depth_ok(v, depth + 1) for v in value)
    return True


def check_input(payload: Any) -> dict[str, Any]:
    """*payload* when it is a JSON object within the size and depth bounds; else KiroCallError."""
    if not isinstance(payload, dict):
        raise KiroCallError(400, "invalid_json", "The input must be a JSON object.")
    if not _depth_ok(payload):
        raise KiroCallError(413, "body_too_large", "The input is nested too deep.")
    try:
        size = len(json.dumps(payload, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError):
        raise KiroCallError(400, "invalid_json", "The input must be plain JSON.") from None
    if size > MAX_REQUEST_BYTES:
        raise KiroCallError(413, "body_too_large", "The input is too large.")
    return payload


def parse_json_body(raw: bytes) -> dict[str, Any]:
    """A request body as the operation's input: an empty body is ``{}``. Raises KiroCallError."""
    if len(raw) > MAX_REQUEST_BYTES:
        raise KiroCallError(413, "body_too_large", "The input is too large.")
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        raise KiroCallError(400, "invalid_json", "The input must be a JSON object.") from None
    return check_input(payload)


def jsonable(value: Any) -> Any:
    """An upstream output as JSON: a byte string is base64 text, a timestamp ISO-8601."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and value != value:  # NaN has no JSON form
        return None
    return value


def _short(text: Any, sent: tuple[str, ...]) -> str:
    """Upstream text cut short, with every credential this call sent and any known secret removed."""
    from kiro_crew.security.text_redaction import redact  # noqa: PLC0415

    if not isinstance(text, str):
        return ""
    # The exact values sent are removed first: a bare token matches no secret pattern.
    for value in sent:
        if value:
            text = text.replace(value, "[REDACTED]")
    return redact(text[:MAX_ERROR_MESSAGE])


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Reply:
    status: int
    protocol: str
    body: bytes


_in_flight: dict[str, int] = {}


async def _post(url: str, body: bytes, signed: SignedCall) -> _Reply:
    headers = {
        **signed.headers,
        "content-type": _CBOR,
        "accept": _CBOR,
        "smithy-protocol": _PROTOCOL,
    }
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_S, connect=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url, data=body, headers=headers, allow_redirects=False, proxy=resolve_proxy(url)
            ) as resp:
                declared = resp.content_length
                if declared is not None and declared > MAX_RESPONSE_BYTES:
                    raise KiroCallError(502, "kiro_bad_reply", "The Kiro reply is too large.")
                raw = bytearray()
                async for chunk in resp.content.iter_chunked(_CHUNK):
                    raw += chunk
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise KiroCallError(502, "kiro_bad_reply", "The Kiro reply is too large.")
                return _Reply(resp.status, resp.headers.get("smithy-protocol", ""), bytes(raw))
    except asyncio.TimeoutError:
        raise KiroCallError(504, "kiro_unreachable", "Kiro did not answer in time.") from None
    except aiohttp.ClientError as exc:
        # The type only: the message can carry the URL or a header.
        raise KiroCallError(
            502, "kiro_unreachable", f"Kiro could not be reached ({type(exc).__name__})."
        ) from None
    except ValueError as exc:
        # A header value with a newline is refused; the value is the credential.
        raise KiroCallError(
            502, "kiro_unreachable", f"The Kiro request could not be built ({type(exc).__name__})."
        ) from None


def _output(reply: _Reply, sent: tuple[str, ...]) -> dict[str, Any]:
    if reply.protocol.lower() != _PROTOCOL:
        raise KiroCallError(502, "kiro_bad_reply", "The Kiro reply is not rpc-v2-cbor.")
    try:
        decoded = cbor_decode(reply.body, timestamps=True) if reply.body else {}
    except (ValueError, UnicodeDecodeError):
        raise KiroCallError(502, "kiro_bad_reply", "The Kiro reply is not valid CBOR.") from None
    if not isinstance(decoded, dict):
        raise KiroCallError(502, "kiro_bad_reply", "The Kiro reply is not a map.")
    if 200 <= reply.status < 300:
        return decoded
    error_type = decoded.get("__type")
    error_type = error_type.rsplit("#", 1)[-1] if isinstance(error_type, str) else ""
    message = _short(decoded.get("message", decoded.get("Message")), sent)
    if reply.status in (401, 403):
        raise KiroCallError(
            409,
            "kiro_sign_in_refused",
            f"Kiro refused the sign-in. {SIGN_IN_HINT}",
            kiro={"status": reply.status, "type": _short(error_type, sent), "message": message},
        )
    raise KiroCallError(
        502,
        "kiro_operation_failed",
        message or f"Kiro answered HTTP {reply.status}.",
        kiro={"status": reply.status, "type": _short(error_type, sent), "message": message},
    )


def _audit(app: str, operation: str, caller: str, outcome: str, error: str = "") -> None:
    try:
        from kiro_crew.sel import sel  # noqa: PLC0415

        sel().log_api_access(
            caller=f"app:{app}",
            operation="kiro_call",
            outcome=outcome,
            source="apps",
            resources=f"caller={caller} operation={operation} grant=kiroOperations:{operation}",
            error=error,
        )
    except Exception:  # noqa: BLE001 - the audit never changes the outcome
        logger.debug("kiro call audit failed", exc_info=True)


async def call_kiro_operation(
    app: str,
    operation: str,
    payload: Any,
    *,
    caller: str,
    upstream: Upstream | None = None,
) -> dict[str, Any]:
    """Call *operation* for *app* with *payload*; the upstream's output map, as JSON.

    *caller* names who asked (``backend``, ``owner`` or ``hook``) for the audit row.
    Raises :class:`KiroCallError`; nothing reaches the upstream unless every check
    before the send passed.
    """
    approved = await asyncio.to_thread(app_kiro_operations, app)
    if operation not in approved:
        declared = await asyncio.to_thread(declared_kiro_operations, app)
        _audit(app, operation, caller, "denied", "not approved")
        if operation in declared:
            raise KiroCallError(
                403,
                "operation_not_approved",
                f"{operation!r} is not approved for this app, or the app is not enabled.",
            )
        raise KiroCallError(404, "unknown_operation", "This app declares no such Kiro operation.")
    body = cbor_encode(check_input(payload))
    if _in_flight.get(app, 0) >= MAX_IN_FLIGHT:
        _audit(app, operation, caller, "denied", "too many calls")
        raise KiroCallError(429, "too_many_calls", "Too many Kiro calls are running for this app.")
    _in_flight[app] = _in_flight.get(app, 0) + 1
    started = time.monotonic()
    status: int | str = "-"
    try:
        target = upstream if upstream is not None else KIRO_UPSTREAM
        signed = await target.sign()
        url = f"{signed.base_url}/service/{target.service}/operation/{operation}"
        reply = await _post(url, body, signed)
        sent: tuple[str, ...] = (signed.rejected,)
        if reply.status in (401, 403):
            signed = await target.sign(rejected=signed)
            reply = await _post(url, body, signed)
            sent += (signed.rejected,)
        status = reply.status
        output = jsonable(_output(reply, sent))
    except KiroCallError as exc:
        _audit(app, operation, caller, "error", exc.code)
        raise
    finally:
        left = _in_flight.get(app, 1) - 1
        if left > 0:
            _in_flight[app] = left
        else:
            _in_flight.pop(app, None)
        logger.info(
            "kiro call: app=%s operation=%s status=%s ms=%d in=%d",
            app,
            operation,
            status,
            int((time.monotonic() - started) * 1000),
            len(body),
        )
    _audit(app, operation, caller, "success")
    return output


__all__ = [
    "KIRO_DEFAULT_REGION",
    "KIRO_ORIGINS",
    "KIRO_SERVICE",
    "KIRO_UPSTREAM",
    "MAX_INPUT_DEPTH",
    "MAX_IN_FLIGHT",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "SIGN_IN_HINT",
    "TIMEOUT_S",
    "KiroCallError",
    "KiroSignIn",
    "SignedCall",
    "Upstream",
    "call_kiro_operation",
    "check_input",
    "jsonable",
    "parse_json_body",
]
