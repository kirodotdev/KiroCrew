"""Reading a request body or an upstream response within a byte budget.

``read_bounded_json`` decodes a JSON request body only after its declared type and
length pass, and answers a refusal a route can return as it is;
``read_capped_response`` reads an upstream response up to a cap.

Served through :mod:`kiro_crew.dashboard.handlers._shared`, which callers and tests
import and patch; this module never imports it."""

from __future__ import annotations

import json
from typing import Any

import aiohttp
from aiohttp import web

# Shared body cap for the small JSON-object endpoints that must bound the
# request BEFORE decoding (the strict-internal notification routes). Kept
# module-level and in one place so the security-relevant cap cannot drift
# between the two call sites. 64 KB is generous — payload fields
# have their own caps.
_MAX_BODY_BYTES = 64 * 1024


def _declares_json(content_type: str) -> bool:
    """Is *content_type* a media type whose payload is a JSON document?

    ``application/json`` and the ``+json`` structured-suffix family
    (``application/merge-patch+json``, ``application/ld+json``) only. The suffix
    family is in because the app SDK already lets a caller send one -- a scoped
    API test pins ``application/merge-patch+json`` reaching the gateway -- so
    refusing it would break a shape the product ships.

    Everything else is out, including ``text/plain`` and an ABSENT header, which
    aiohttp reports as ``application/octet-stream``. Absent and ``text/plain``
    are the two spellings that matter: both are CORS *simple* request types, so a
    cross-origin page sends them with no preflight, and a plain HTML form can
    emit either. ``application/json`` is not simple, so requiring it means the
    browser must ask permission before the body is ever delivered.
    """
    ct = content_type.strip().lower()
    return ct == "application/json" or (ct.startswith("application/") and ct.endswith("+json"))


def _json_content_type_error(request: web.Request) -> web.Response | None:
    """415 unless a request that HAS a body declares a JSON content type.

    Checked only when a body is actually present. An empty body carries no JSON
    document to be misread, so gating it would change the status of a bodiless
    POST (today a 400 ``invalid_json``, or ``{}`` under ``allow_absent``) while
    removing no attack primitive.

    415 rather than 400: the body may be perfectly well-formed JSON, and what is
    refused is the media type the client declared for it.

    Runs AFTER the capped path's Content-Length precheck, so a caller who
    declared too many bytes still gets 413 rather than being told about its
    header instead. Both refuse before anything is read, so the order is about
    which fact the client is told, not about work done.
    """
    if not request.can_read_body:
        return None
    if _declares_json(request.content_type or ""):
        return None
    return web.json_response(
        {
            "error": "JSON body requires Content-Type: application/json",
            "code": "unsupported_media_type",
        },
        status=415,
    )


async def read_bounded_json(
    request: web.Request,
    max_bytes: int | None = _MAX_BODY_BYTES,
    *,
    allow_absent: bool = False,
    require_json_content_type: bool = True,
) -> tuple[dict[str, Any] | None, web.Response | None]:
    """Read and parse a JSON *object* request body, capped at *max_bytes*.

    Returns ``(body, None)`` on success, or ``(None, error_response)`` when the
    caller should return early. This owns the parse-and-shape guard for the
    endpoints routed through it: ``await request.json()`` happily returns a
    list, string, or number for a body that is valid JSON but not an object, and
    a handler that then calls ``.get()`` on the result turns a client mistake
    into a 500.

    A request that HAS a body must also DECLARE a JSON content type, or it is
    refused 415 ``unsupported_media_type`` before the body is read -- see
    :func:`_json_content_type_error`. Parsing a ``text/plain`` or
    content-type-less body as JSON is what lets a cross-origin page deliver a
    JSON command to a local endpoint with no CORS preflight; requiring the
    header puts the preflight back. In-tree callers are unaffected: every client
    that sends a body already sets ``application/json`` (the frontend
    ``post``/``put``/``patch``/``del`` helpers, ``mcp_core``, ``cron_script``,
    ``cli_*``, ``pod.runtime``), and the bodiless requests
    (``app_lifecycle_client``, ``cron_trigger``) are not checked.

    ONE caller opts OUT, and must: ``POST /api/messaging/teams``, where
    Microsoft's Bot Framework Connector posts activities, passes
    ``require_json_content_type=False``. Two reasons, and the second is the
    load-bearing one:

    * Refusing a media type there is a change to an EXTERNAL contract. The route
      is the single entry in ``token_auth.CSRF_EXEMPT_EXACT_METHODS``, so the
      Origin barrier does not stand in front of it, and a media-type refusal
      would be the one refusal that could silently stop an external channel.
    * The gate cannot be enforced there ANYWAY, and enforcing it costs the byte
      cap. ``handlers/messaging.py`` forwards this helper's error only when it is
      a 413, because a verdict derived from body CONTENT must not precede the JWT
      check. A 415 is therefore dropped, the parsed body is never stashed, and
      ``teams/client.py``'s fallback re-parses with a bare ``request.json()`` --
      on a stream this helper returned from WITHOUT reading, so the re-parse is
      bounded only by the app-wide ``client_max_size`` instead of
      ``TEAMS_MAX_ACTIVITY_BYTES`` (64 KiB). Opting out keeps the capped read,
      and with it the stash, so the body is parsed exactly once and never past
      the cap.

    Which generalizes to the rule any future caller must satisfy: because the
    415 and the declared-oversize 413 return BEFORE a single byte is read, a
    caller that does not return every error this helper hands back must pass
    ``require_json_content_type=False``, or it inherits an unread stream that
    nothing bounds. Every other caller in the tree answers every error it gets
    (``chat_pins``, ``session_storage`` and ``artifacts`` rewrite a 400 into
    their own shape; ``hooks`` defaults an ``invalid_json`` 400, which comes
    back only after the whole body was read), so the Teams route is the only
    opt-out.

    Which also means: this gate is a SECOND barrier, not the first one. Every
    other unsafe-method ``/api/`` route is already refused cross-origin by
    ``csrf_middleware``, so on those routes the 415 removes no primitive that
    was reachable -- it holds if a route is ever added to the CSRF-exempt list,
    or the Origin barrier is ever mis-scoped, without waiting for that mistake
    to be exploitable. Extending the refusal to the Teams route is a separate
    decision about an external contract, not part of this one.

    The other self-authenticating webhook, ``POST /api/hooks/agent``, reads raw
    bytes through ``handlers/hooks_webhook_intake.py::_read_hook_body`` (it must HMAC
    the exact bytes) and is not a caller, so this gate does not reach it.

    The four module-local JSON body guards read through this helper and keep
    only their module's answer shape:
    ``handlers_channel._json_object`` (raises, via
    :func:`bounded_json_error_as_exception`), ``handlers/hooks_webhook_intake.py::_json_object``
    (``default_empty`` defaults an absent or ``invalid_json`` body, never a 415
    or 413), ``handlers/session_storage.py::_json_body`` (absent is ``{}``,
    malformed is its own ``invalid_body`` 400), and
    ``handlers/artifacts_parts/route_plumbing.py::_read_json_body``
    (``ArtifactValidationError`` for the 400s, the 415 raised). So a handler
    that picks any of them gets the media-type gate.

    What this does NOT cover: handlers that still call ``await request.json()``
    directly, and the builtin apps' own body helpers. Those parse whatever media
    type is declared, behind ``csrf_middleware``'s Origin barrier only. A
    multipart or raw-bytes route (uploads, the session import) is correctly not
    a caller.

    The cap is enforced BEFORE decoding: a Content-Length precheck rejects an
    oversized declared body, and the stream is then read incrementally so a
    chunked body (which carries no Content-Length) cannot buffer past
    ``max_bytes + one chunk`` on the event-loop thread. That bound is the point
    of the helper for the strict-internal notification routes.

    ``max_bytes=None`` reads the body whole with no pre-decode ceiling, for the
    endpoints that have no principled byte limit today (a knowledge bundle
    import has no defensible maximum size). It is deliberately explicit rather
    than the default: an endpoint opting out of the cap should say so at the
    call site, and giving one of those endpoints a real ceiling later is then a
    one-argument change here instead of a re-plumb.

    Which one a converting caller wants is a real choice, not a default to
    inherit: take the cap when the body is a fixed set of control fields (an
    identifier, a flag, a number), and ``None`` only when the body legitimately
    carries user content of unbounded size (file contents, an export, a fetched
    document). Note that switching a site TO the cap also moves it off
    ``request.json()`` onto the streaming read, so that handler's unit tests
    must feed ``content``/``content_length`` rather than mocking ``json``.

    *allow_absent* treats a request with no readable body as an empty object,
    for endpoints whose fields all have defaults. A body that is *present but
    malformed* is still a 400 -- "the client sent nothing" and "the client sent
    garbage" are different facts, and only the first one can be defaulted.

    Decoding matches ``request.json()`` on both paths -- ``decode(charset or
    utf-8)`` then ``loads`` -- so the two differ only in whether the read is
    bounded, and the declared ``charset=`` is honoured either way. The uncapped
    path calls ``request.json()`` itself rather than reimplementing it, which is
    what makes converting a ``try: await request.json()`` site a drop-in: no
    handler and no test harness sees a different read.

    The catch is narrowed to the three client-input failures -- ``ValueError``
    (which covers ``json.JSONDecodeError`` and ``UnicodeDecodeError``),
    ``LookupError`` (an unknown ``charset=`` codec), and ``RecursionError`` (a
    deeply nested document blowing the parser's stack). Transport failures (a
    disconnect mid-body, a read timeout) deliberately propagate: they are not a
    client JSON mistake and keep their 500 status class.
    """
    if allow_absent and not request.can_read_body:
        return {}, None
    # A DECLARED oversize keeps its 413, which is the actionable answer for a
    # caller sending too much and was this helper's answer before the media-type
    # gate existed. The gate runs next, still before a single byte is read.
    if max_bytes is not None and request.content_length and request.content_length > max_bytes:
        return None, web.json_response(
            {"error": "payload too large", "code": "payload_too_large"}, status=413
        )
    ct_error = _json_content_type_error(request) if require_json_content_type else None
    if ct_error is not None:
        return None, ct_error
    if max_bytes is None:
        try:
            body = await request.json()
        except (LookupError, RecursionError, ValueError):
            return None, web.json_response(
                {"error": "invalid JSON", "code": "invalid_json"}, status=400
            )
    else:
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.content.iter_chunked(8192):
            received += len(chunk)
            if received > max_bytes:
                return None, web.json_response(
                    {"error": "payload too large", "code": "payload_too_large"}, status=413
                )
            chunks.append(chunk)
        try:
            body = json.loads(b"".join(chunks).decode(request.charset or "utf-8"))
        except (LookupError, RecursionError, ValueError):
            return None, web.json_response(
                {"error": "invalid JSON", "code": "invalid_json"}, status=400
            )
    if not isinstance(body, dict):
        return None, web.json_response(
            {"error": "body must be a JSON object", "code": "body_not_object"}, status=400
        )
    return body, None


def bounded_json_error_code(error: web.Response) -> str | None:
    """The ``code`` of a :func:`read_bounded_json` refusal *error*.

    For the callers that answer one refusal their own way (a defaulted
    ``invalid_json``, a module-specific message) while passing the rest through.
    Telling ``invalid_json`` from ``body_not_object`` needs the code: both are 400.
    """
    try:
        code = json.loads(error.text or "").get("code")
    except (ValueError, AttributeError):
        return None
    return code if isinstance(code, str) else None


def bounded_json_error_as_exception(error: web.Response) -> web.HTTPException:
    """The :func:`read_bounded_json` refusal *error*, as an exception to raise.

    For the callers whose contract is to RAISE rather than return a response
    (``handlers_channel._json_object``, the 415 from
    ``handlers/artifacts_parts/route_plumbing.py::_read_json_body``). aiohttp
    answers a raised ``HTTPException`` with its own status and body, so the
    client sees the same status, ``code`` and JSON the returning callers send.
    Keeping the mapping here means a raising caller cannot drop the 415 by
    translating it into the wrong status.
    """
    kwargs: dict[str, Any] = {"text": error.text, "content_type": "application/json"}
    if error.status == 415:
        return web.HTTPUnsupportedMediaType(**kwargs)
    if error.status == 413:
        return web.HTTPRequestEntityTooLarge(max_size=0, actual_size=0, **kwargs)
    return web.HTTPBadRequest(**kwargs)


# Chunk size for draining an OUTBOUND HTTP response to EOF. Matches the
# bounded-read shape in ``mcp_providers.official._fetch_json``: large enough
# that a typical body arrives in a handful of iterations, small enough that
# the over-cap check fires long before an oversized body is buffered whole.
_RESPONSE_READ_CHUNK_BYTES = 64 * 1024


async def read_capped_response(resp: "aiohttp.ClientResponse", cap: int) -> bytes:
    """Read *resp*'s body to EOF, returning at most ``cap + 1`` bytes.

    A single ``StreamReader.read(n)`` resolves as soon as ANY bytes are
    buffered -- on a chunked response (no Content-Length) that is the first
    buffered chunk, so the caller silently works on a truncated body. This
    drains ``iter_chunked`` chunks until EOF, enforcing the cap against the
    ACCUMULATED total: reading stops as soon as the total exceeds *cap*, so a
    hostile oversized body is refused mid-stream rather than buffered whole.
    The return is clamped to ``cap + 1`` bytes so callers keep the established
    over-cap sentinel (``len(body) > cap`` means "exceeded the cap"), while a
    body of exactly *cap* bytes is still delivered complete.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.content.iter_chunked(_RESPONSE_READ_CHUNK_BYTES):
        chunks.append(chunk)
        total += len(chunk)
        if total > cap:
            break
    return b"".join(chunks)[: cap + 1]
