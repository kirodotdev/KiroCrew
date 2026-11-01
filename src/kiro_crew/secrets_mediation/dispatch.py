"""Trusted dispatch boundary for a mediated secret request.

This is the ONLY place the plaintext of a Custom secret is read for an outbound
API call, and it never leaves this module: the value is resolved from the vault
at the moment of dispatch, injected into the request per the owner's placement
policy, and dropped when the response returns. The value is never placed in the
tool result, an error message, a log line, telemetry, or the SEL audit record --
callers receive only a :class:`SanitizedResponse`.

Order of operations (all fail closed):

1. Load the owner authorization for the secret name. No authorization -> refuse
   BEFORE any vault read or network I/O.
2. Normalize the request URL's origin and require it to equal the authorized
   origin EXACTLY. Mismatch -> refuse before resolving the secret.
3. Vet the URL and pin its addresses with the product's one fetch vet,
   :func:`kiro_crew.link_unfurl.vet_unfurl_url` + :func:`~kiro_crew.link_unfurl.
   pinned_connector` (the same pair link previews, the remote MCP probe and the
   calendar fetch use), so this path cannot drift from the others.
4. Resolve the secret (:class:`~kiro_crew.secrets.SecretVault`) and inject it per
   placement -- only now, only in memory, only for this send.
5. Send with automatic redirects DISABLED through the pinned connector. A 3xx is
   handled manually: the target is required to keep the SAME authorized origin
   and is vetted and pinned again before another hop, so a redirect can never
   move the credential-bearing request to a different or internal origin.
6. Return the fixed withheld result, padded to the request deadline.

Everything from step 3 on runs as one task under ``asyncio.wait_for`` bounded by
the normalized request deadline. Cancelling the task at the deadline closes its
connection inside the task, so no byte of the request can be written after the
deadline and no worker thread outlives it.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import aiohttp
from cryptography.exceptions import InvalidTag

from kiro_crew.link_unfurl import UnfurlRejected, VettedUrl, pinned_connector, vet_unfurl_url
from kiro_crew.secrets import SecretVault
from kiro_crew.secrets_mediation.policy import (
    CredentialPlacement,
    PolicyError,
    SecretAuthorization,
    load_authorization,
    normalize_origin,
)
from kiro_crew.security.exfil import redact_exfiltration_urls
from kiro_crew.security.redaction import redact_credentials

#: Methods a mediated request may use. Writes only: the origin's response is always
#: withheld, so a safe method (GET, HEAD) would send the credential and show nothing.
ALLOWED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Bounds. A credential-bearing request is short and bounded by construction.
_DEFAULT_TIMEOUT_S = 20.0
_MAX_TIMEOUT_S = 60.0
_MAX_REDIRECTS = 3
_MAX_REQUEST_BODY_BYTES = 256_000
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

#: The status returned on every mediated call. A FIXED value, never the upstream
#: status -- the status code is origin-controlled, so echoing it would leave a
#: covert channel (an origin could encode the injected credential a few bits per
#: call across the status). 0 is not a real HTTP status, so a consumer cannot
#: mistake it for one.
_WITHHELD_STATUS = 0

#: Resolver handed to the vet. ``None`` is the vet's own system resolver; tests
#: substitute a fake so no real DNS is touched.
_resolve: Optional[Callable[[str, int], Sequence[str]]] = None


class MediationError(Exception):
    """A mediated request failed. The message is safe (never the secret)."""


@dataclass
class SanitizedResponse:
    status: int
    headers: dict[str, str]
    body: str
    truncated: bool = False
    #: The authorized origin, for the agent's situational awareness. Only that
    #: origin can ever appear here (a cross-origin redirect fails).
    final_url_origin: str = ""


@dataclass
class MediatedRequest:
    """The non-secret request intent supplied by the agent."""

    secret_name: str
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    json_body: Optional[Any] = None
    timeout_s: float = _DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class _Prepared:
    """Everything the pre-send checks settled, before any network I/O."""

    method: str
    timeout: float
    authorization: SecretAuthorization
    caller_headers: dict[str, str]


def _inject(placement: CredentialPlacement, secret_value: str, headers: dict[str, str]) -> None:
    """Inject the resolved secret into *headers* per *placement*. Mutates in place."""
    if placement.type == "bearer":
        headers["Authorization"] = f"Bearer {secret_value}"
    elif placement.type == "header" and placement.header:
        headers[placement.header] = secret_value
    else:  # pragma: no cover - policy validation already rejects other shapes
        raise MediationError("Unsupported credential placement.")


def _withheld_response(final_origin: str) -> SanitizedResponse:
    """The fixed result of EVERY mediated call once control reaches the padded region.

    Success, a vet refusal, a redirect refusal, a transport error, a timeout, an
    authorized-but-unstored secret and an unreadable store all return this, so
    nothing an owner-authorized origin controls (a body, a header, a status, or
    even error-vs-success) can encode a bit of the credential back to the agent.
    Because it stands for refusals that never sent anything as well as for a call
    that did, the body does NOT assert the request was sent or completed.
    """
    return SanitizedResponse(
        status=_WITHHELD_STATUS,
        headers={},
        body=(
            "[The response is withheld. This tool lets you USE the secret against "
            "the owner-authorized origin without revealing anything the origin "
            "controls (body, headers, status, or length) and without telling you "
            "whether the request was sent, so a reflected credential has no channel "
            "to ride back out.]"
        ),
        truncated=False,
        final_url_origin=final_origin,
    )


def _iter_outbound_strings(value: Any) -> Any:
    """Yield every string leaf reachable in an agent-supplied outbound value
    (a JSON body, a headers map, or a query map), recursing through dicts and
    lists so a credential nested inside the request body is scanned too."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str):
                yield k
            yield from _iter_outbound_strings(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_outbound_strings(item)


def _refuse_if_outbound_redacted(url: Any, json_body: Any, headers: Any, query: Any) -> None:
    """Refuse the request if EITHER required redactor would change any
    agent-supplied outbound string.

    ``redact_credentials`` and ``redact_exfiltration_urls`` are the two mandatory
    scans for content that leaves the machine. Individual leaves are scanned, as
    are the serialized JSON body, joined header lines, and the URL with its query
    map assembled, so credential markers that depend on their key stay visible to
    the redactors. A match fails the whole request closed, before the vault read.
    The injected secret is added by the dispatcher at the owner's placement, never
    by the agent, so a clean payload is unaffected.
    """
    contextual_strings: list[str] = []
    if json_body is not None:
        contextual_strings.append(json.dumps(json_body))
    if isinstance(headers, dict):
        contextual_strings.extend(
            f"{name}: {value}"
            for name, value in headers.items()
            if isinstance(name, str) and isinstance(value, str)
        )
    if isinstance(url, str) and query:
        encoded_query = urllib.parse.urlencode(query)
        if encoded_query:
            parts = urllib.parse.urlsplit(url)
            assembled_query = "&".join(part for part in (parts.query, encoded_query) if part)
            contextual_strings.append(
                urllib.parse.urlunsplit(parts._replace(query=assembled_query))
            )

    for source in (url, json_body, headers, query, contextual_strings):
        for text in _iter_outbound_strings(source):
            if not text:
                continue
            redacted_cred, _ = redact_credentials(text)
            if redacted_cred != text:
                raise MediationError(
                    "The request carries a credential in an agent-supplied value; "
                    "refused before sending."
                )
            redacted_url, _ = redact_exfiltration_urls(text)
            if redacted_url != text:
                raise MediationError(
                    "The request carries a disallowed URL in an agent-supplied "
                    "value; refused before sending."
                )


def _filter_caller_headers(
    headers: dict[str, str], placement: CredentialPlacement
) -> dict[str, str]:
    forbidden = {"host", "content-length", "connection", "transfer-encoding", "authorization"}
    if placement.type == "header" and placement.header:
        forbidden.add(placement.header.lower())
    out: dict[str, str] = {}
    for k, v in (headers or {}).items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if k.lower() in forbidden:
            continue
        if any(ord(c) < 0x20 or c in "\r\n" for c in k + v):
            continue
        out[k] = v
    return out


def _reject_confusable_url(url: str) -> None:
    """Refuse a URL whose authority a parser and the transport could read apart.

    ``userinfo@host`` and a backslash are the two classic origin-confusion
    vectors: different parsers split them differently, so the origin
    ``normalize_origin`` validates might not be the one the request is sent to.
    Refusing both, plus any control character, removes the divergence.
    """
    if not isinstance(url, str) or not url:
        raise MediationError("The request URL is missing.")
    if "\\" in url:
        raise MediationError("The request URL must not contain a backslash.")
    if any(ord(c) < 0x20 for c in url):
        raise MediationError("The request URL must not contain control characters.")
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        # A malformed authority (e.g. an unmatched IPv6 bracket) makes urlsplit
        # itself raise; exit as a mediation refusal, never an uncaught 500.
        raise MediationError("The request URL is malformed.") from None
    if "@" in parts.netloc:
        raise MediationError(
            "The request URL must not contain userinfo (a '@' in the host); refused."
        )


def prepare_mediated_request(req: MediatedRequest, config_dir: str | Path) -> _Prepared:
    """Run every PRE-send check. Blocking (reads the policy file): call off the loop.

    Raises :class:`MediationError` / :class:`PolicyError`. These refusals happen
    before the secret is read, so there is nothing to leak and they answer a
    distinct error rather than the padded constant.
    """
    method = (req.method or "").upper()
    if method not in ALLOWED_METHODS:
        raise MediationError(f"HTTP method {req.method!r} is not allowed.")
    raw_timeout = float(req.timeout_s or _DEFAULT_TIMEOUT_S)
    if not math.isfinite(raw_timeout):
        # NaN defeats min/max (every comparison is False) and would propagate into
        # the deadline, so refuse a non-finite budget outright.
        raise MediationError("Request timeout must be a finite number.")
    timeout = min(max(raw_timeout, 0.1), _MAX_TIMEOUT_S)

    authorization = load_authorization(config_dir, req.secret_name)
    _reject_confusable_url(req.url)
    request_origin = normalize_origin(req.url)
    if request_origin != authorization.origin:
        raise PolicyError(
            f"Secret {req.secret_name!r} is authorized only for {authorization.origin}, "
            f"not {request_origin}. The request was refused before the secret was read."
        )
    caller_headers = _filter_caller_headers(req.headers, authorization.placement)
    if req.json_body is not None:
        encoded = json.dumps(req.json_body).encode("utf-8")
        if len(encoded) > _MAX_REQUEST_BODY_BYTES:
            raise MediationError("Request body is too large.")
    _refuse_if_outbound_redacted(req.url, req.json_body, caller_headers, dict(req.query or {}))
    return _Prepared(method, timeout, authorization, caller_headers)


def _read_secret(config_dir: str | Path, secret_name: str) -> str:
    """The plaintext of *secret_name*. Blocking (vault read + decrypt)."""
    try:
        secret = SecretVault(config_dir).get(secret_name)
    except (ValueError, InvalidTag, OSError):
        # A corrupt store, a mismatched key or an unreadable file: collapse to the
        # fixed refusal with no store internals, never a raw 500 that would strand
        # the already-consumed capability.
        raise MediationError("The secret store could not be read; the request was refused.")
    if secret is None:
        raise MediationError("The secret is authorized but not stored in the vault; refused.")
    return secret.reveal()


async def _vet(url: str) -> VettedUrl:
    """Vet *url* with the shared fetch vet, off the loop (it resolves DNS once).

    ``allow_any_port`` because the origin is one the owner authorized exactly,
    port included; the address rules (no private, loopback, link-local or
    metadata target, every resolved address checked) are unchanged.
    """
    if urllib.parse.urlsplit(url).scheme.lower() != "https":
        raise PolicyError("The mediated request must use https.")
    return await asyncio.to_thread(vet_unfurl_url, url, resolve=_resolve, allow_any_port=True)


async def _send(
    *,
    prepared: _Prepared,
    url: str,
    query: dict[str, str],
    json_body: Optional[Any],
    config_dir: str | Path,
    secret_name: str,
) -> None:
    """Vet, read the secret, and send, following same-origin redirects by hand."""
    authorized_origin = prepared.authorization.origin
    vetted = await _vet(url)
    # Read only after the destination passed the vet, so a refused destination
    # never resolves the credential.
    secret_value = await asyncio.to_thread(_read_secret, config_dir, secret_name)
    method = prepared.method
    try:
        for _hop in range(_MAX_REDIRECTS + 1):
            _reject_confusable_url(url)
            headers = dict(prepared.caller_headers)
            _inject(prepared.authorization.placement, secret_value, headers)
            async with aiohttp.ClientSession(
                connector=pinned_connector(vetted),
                # No cookie is ever kept: the result carries none.
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
                auto_decompress=False,
            ) as session:
                async with session.request(
                    method,
                    url,
                    params=query or None,
                    headers=headers,
                    json=json_body,
                    allow_redirects=False,
                ) as resp:
                    status = resp.status
                    location = resp.headers.get("Location")
                    # The body is never read: the result never derives from it, and
                    # leaving the block closes the connection without draining.
            if status not in _REDIRECT_CODES or not location:
                return
            try:
                url = urllib.parse.urljoin(url, location)
                next_origin = normalize_origin(url)
            except (ValueError, PolicyError):
                raise PolicyError("The response redirected to an invalid destination; refused.")
            if next_origin != authorized_origin:
                raise PolicyError(
                    "The response tried to redirect the credential-bearing request to a "
                    "different origin; refused."
                )
            vetted = await _vet(url)
            query = {}  # already applied to the first hop
            # A redirect never replays a write: 303, and 301/302 after any unsafe
            # method, continue as a bodyless GET. 307/308 keep method and body.
            if status in (301, 302, 303):
                method = "GET"
                json_body = None
        raise MediationError("The mediated request exceeded the redirect limit.")
    finally:
        # Drop our binding so no frame that outlives the call (a traceback holding
        # locals) keeps the plaintext reachable.
        secret_value = ""  # noqa: F841


async def perform_mediated_request(
    req: MediatedRequest, config_dir: str | Path
) -> SanitizedResponse:
    """Execute *req*, injecting the named secret only inside this call.

    Raises :class:`MediationError` / :class:`PolicyError` for a PRE-send refusal
    (safe messages, never the secret). Every outcome after that collapses to the
    fixed withheld result, returned at the request deadline.
    """
    prepared = await asyncio.to_thread(prepare_mediated_request, req, config_dir)
    # One absolute deadline starts BEFORE the vet. The origin controls its own DNS
    # and response timing, so every outcome from here -- a vet refusal, a transport
    # error, a redirect refusal, a timeout or a success -- returns the same
    # constant at the same moment: neither error-vs-success nor latency carries a
    # bit of a credential the origin may already have.
    deadline = time.monotonic() + prepared.timeout
    try:
        await asyncio.wait_for(
            _send(
                prepared=prepared,
                url=req.url,
                query=dict(req.query or {}),
                json_body=req.json_body,
                config_dir=config_dir,
                secret_name=req.secret_name,
            ),
            timeout=prepared.timeout,
        )
    except (
        MediationError,
        PolicyError,
        UnfurlRejected,
        aiohttp.ClientError,
        asyncio.TimeoutError,
        OSError,
        ValueError,
    ):
        # Includes UnicodeError (a ValueError) from a header value that is not
        # latin-1 encodable. Never chained or logged: the cause can carry the
        # offending bytes. The post-send outcome is not audited apart from a
        # success either, since the audit log is readable in the sandbox.
        pass
    await _wait_for_withheld_deadline(deadline)
    return _withheld_response(prepared.authorization.origin)


async def _wait_for_withheld_deadline(deadline: float) -> None:
    """Pad every mediated completion to its fixed, request-bounded deadline."""
    remaining = deadline - time.monotonic()
    if remaining > 0:
        await asyncio.sleep(remaining)
