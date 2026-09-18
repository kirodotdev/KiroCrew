"""GitHub request locator: turn an ``operation_id`` + typed params into ONE
concrete :class:`~kiro_crew.connections.control_plane.production.HttpRequest`.

WHAT THIS OWNS
==============
W01's production transport (``control_plane/production.py``) is deliberately
vendor-blind: it composes custody + wire mechanics and INJECTS the step that
shapes an operation into a concrete method/URL/headers/body, because that shape
is the vendor owner's. This module is GitHub's implementation of that injected
:data:`~kiro_crew.connections.control_plane.production.RequestLocator` -- the
same role ``vendors/microsoft/graph/locator.py`` plays for Graph.

It is pure logic and it holds NO credential: the transport reveals the secret
into an ``Authorization`` header itself (``build_production_transport`` step 4),
so a locator that touched a token would be reaching across the one boundary the
production module drew. Every request this builds carries vendor headers WITHOUT
a credential, exactly as :class:`HttpRequest` documents.

HOW IT STAYS HONEST
===================
* **No string-concatenation of caller input into a path.** Every ``{owner}`` /
  ``{repo}`` / ``{issue_number}`` placeholder in a descriptor's ``endpoint`` is
  filled from a validated, individually URL-encoded segment (:func:`_segment`),
  and a path separator inside one segment is refused (:class:`GithubLocatorError`)
  -- the same guard ``graph/locator._require_id`` applies, so a caller cannot
  inject extra path structure through an id.
* **The operation is looked up, never guessed.** The endpoint template, HTTP
  method and pagination contract come from the existing
  :func:`~kiro_crew.connections.vendors.github.descriptors.get_descriptor` table
  (this slice OWNS no second copy of them); an unknown ``operation_id`` is
  refused rather than shaped into a fabricated URL.
* **The two pagination contracts stay disjoint.** A ``REST_PAGE`` operation
  renders ``page``/``perPage`` through :class:`RestPageRequest`; a
  ``CURSOR_AFTER`` operation renders ``after`` through :class:`CursorPageRequest`.
  This module never emits ``page`` for a cursor operation -- the exact
  contract-collapse ``pagination.py`` and the campaign evidence warn against.
* **A cursor page re-sends the provider's opaque continuation verbatim.** GitHub
  REST paging returns the whole next-page URL in the ``Link`` header, and the
  decoder surfaces that URL as the ``OperationResult`` cursor; when the executor
  hands it back as ``request_args["cursor"]`` this locator sends that ABSOLUTE
  url as-is rather than re-deriving query params, because the link already
  encodes the page position (the same "re-send the opaque link" rule Graph's
  paging layer documents).

WHAT THIS DELIBERATELY DOES NOT OWN
===================================
No auth, no custody, no retry, no error classification, no transport. Those are
W01's, consumed. It also defines no vendor-error taxonomy: a shaping fault is a
:class:`GithubLocatorError` (a ``ValueError``); a vendor HTTP failure is W01's
typed boundary via ``classify_github_failure`` / the executor, never here.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any, Mapping, Optional, Tuple

from kiro_crew.connections.control_plane.production import HttpRequest
from kiro_crew.connections.vendors.github.descriptors import (
    GithubOperationDescriptor,
    Pagination,
    get_descriptor,
)
from kiro_crew.connections.vendors.github.pagination import (
    CursorPageRequest,
    RestPageRequest,
)

#: The absolute base every non-cursor GitHub REST request is built under. A
#: cursor page (an opaque ``Link``-header URL the provider returned) is already
#: absolute and is sent verbatim, so it never has this prepended. Kept as a
#: module constant, not string-interpolated per call, so one place defines the
#: host and a test can assert against it.
GITHUB_API_BASE = "https://api.github.com"

#: The request-arg key the executor/PageWalk uses to carry a continuation
#: cursor. ``execute`` injects ``{"cursor": <value>}`` for a page walk; this
#: locator reads it to advance. Its value is whatever the decoder put on
#: ``OperationResult.next_cursor`` -- for GitHub REST that is the opaque next
#: URL from the ``Link`` header, for a cursor tool the opaque ``after`` token.
CURSOR_ARG = "cursor"

#: The request-arg keys naming the caller's desired page size. A locator reads
#: a per-call size from here (defaulting per contract) and renders it through
#: the page-request dataclasses, which clamp it to GitHub's ceiling.
PER_PAGE_ARG = "per_page"


class GithubLocatorError(ValueError):
    """A GitHub request could not be shaped from an operation + its params.

    A ``ValueError`` subclass, mirroring ``graph/locator.GraphLocatorError``: a
    SHAPING fault (an unknown ``operation_id``, a missing required path
    parameter, a separator inside an id segment), NOT a vendor error. This slice
    defines no vendor-error taxonomy -- a GitHub HTTP failure is W01's typed
    boundary, classified by ``classify_github_failure`` / the executor. Do not
    grow an error hierarchy here.
    """


def _segment(value: object, what: str) -> str:
    """Validate and URL-encode ONE path segment filled into a template.

    Guards the same way ``graph/locator._require_id`` does: an empty/whitespace
    value is refused (it would assemble ``/repos//...``), and a raw path
    separator inside one segment is refused rather than silently injecting extra
    path structure the caller did not model. The returned segment is
    percent-encoded with ``safe=""`` so a legitimate but reserved character
    (a space, a ``?``) cannot break out of its slot either.
    """

    text = str(value).strip()
    if not text:
        raise GithubLocatorError(f"{what} must be a non-empty value")
    if "/" in text:
        raise GithubLocatorError(f"{what} must not contain a path separator: {value!r}")
    return urllib.parse.quote(text, safe="")


_HTTP_METHODS = frozenset({"GET", "POST", "PATCH", "PUT", "DELETE", "HEAD", "OPTIONS"})


def _path_of(endpoint: str) -> str:
    """The URL path portion of a descriptor's ``endpoint`` field.

    Descriptor endpoints are written ``"<METHOD> /path"``
    (``"GET /repos/{owner}/{repo}/pulls"``), so the leading HTTP-method token is
    stripped and the remaining first whitespace-free token is the path. An
    endpoint that is prose rather than a REST path -- the GraphQL-backed cursor
    tools carry ``"GraphQL-backed issue listing"`` -- has no shapeable path, so
    this refuses with :class:`GithubLocatorError` rather than fabricating a URL
    out of prose. A path MUST begin with ``/``.
    """

    tokens = endpoint.split()
    if not tokens:
        raise GithubLocatorError(f"empty endpoint: {endpoint!r}")
    rest = tokens[1:] if tokens[0].upper() in _HTTP_METHODS else tokens
    if not rest or not rest[0].startswith("/"):
        raise GithubLocatorError(
            f"endpoint {endpoint!r} is not a shapeable REST path (a GraphQL-backed "
            "operation has no REST endpoint template); this locator shapes REST "
            "requests only"
        )
    return rest[0]


def _fill_endpoint(endpoint: str, request_args: Mapping[str, Any]) -> str:
    """Fill every ``{name}`` placeholder in ``endpoint`` from ``request_args``.

    The template is the path portion of the descriptor's own ``endpoint``
    (``/repos/{owner}/{repo}/pulls``, after :func:`_path_of` strips the leading
    method). Each placeholder must have a matching request arg; a missing one is
    a shaping fault, refused before any URL is built. A template with no
    placeholder (a fixed path like ``/search/issues``) passes through unchanged.
    """

    path = _path_of(endpoint)
    out: list[str] = []
    i = 0
    while i < len(path):
        char = path[i]
        if char == "{":
            end = path.find("}", i)
            if end == -1:
                raise GithubLocatorError(f"malformed endpoint template: {endpoint!r}")
            name = path[i + 1 : end]
            if name not in request_args:
                raise GithubLocatorError(
                    f"endpoint {endpoint!r} requires path parameter {name!r}, "
                    "which was not supplied"
                )
            out.append(_segment(request_args[name], name))
            i = end + 1
        else:
            out.append(char)
            i += 1
    return "".join(out)


def _query_args(request_args: Mapping[str, Any]) -> Mapping[str, Any]:
    """The request args that are NOT control keys -- the caller's own filters.

    ``cursor`` and ``per_page`` are consumed by the paging machinery here, and
    every ``{name}`` a template fills is a path parameter, not a query one. The
    remaining keys (``state``, ``sort``, ``q`` ...) are the vendor query the
    caller asked for; a path-parameter key is dropped from the query because it
    already appears in the path. This keeps a page-2 request carrying the same
    filter set as page 1 (the executor's ``PageWalk`` sends ``base_args`` on
    every page), which is why the walk does not silently narrow after page 1.
    """

    reserved = {CURSOR_ARG, PER_PAGE_ARG}
    return {k: v for k, v in request_args.items() if k not in reserved}


def _endpoint_param_names(endpoint: str) -> Tuple[str, ...]:
    """The ``{name}`` placeholders a template declares, so query shaping can
    exclude them (they are path parameters, already spent in the path)."""

    path = _path_of(endpoint)
    names: list[str] = []
    i = 0
    while i < len(path):
        if path[i] == "{":
            end = path.find("}", i)
            if end == -1:
                break
            names.append(path[i + 1 : end])
            i = end + 1
        else:
            i += 1
    return tuple(names)


def _encode_query(params: Mapping[str, Any]) -> str:
    """Render a query-param map to a deterministic ``a=1&b=2`` string.

    Sorted by key so a request shape is stable (the same property Graph's
    ``QuerySpec.to_query_params`` guarantees for evidence receipts), and
    URL-encoded so a filter value cannot break out of its slot. An empty map
    renders to the empty string (no trailing ``?``).
    """

    if not params:
        return ""
    items = sorted((str(k), str(v)) for k, v in params.items())
    return urllib.parse.urlencode(items)


def _rest_page_params(request_args: Mapping[str, Any]) -> dict:
    """The ``page``/``perPage`` params for a REST_PAGE operation's FIRST page.

    Page 1 by default (a page walk over REST GitHub follows the ``Link`` header
    thereafter, so only the first request is built from a page number). The
    per-page size is clamped to GitHub's ceiling by :class:`RestPageRequest`.
    """

    per_page = int(request_args.get(PER_PAGE_ARG, 30))
    return RestPageRequest(page=1, per_page=per_page).as_query_params()


def _cursor_params(request_args: Mapping[str, Any]) -> dict:
    """The ``after``/``perPage`` params for a CURSOR_AFTER operation.

    The opaque ``after`` cursor comes from ``request_args[CURSOR_ARG]`` (``None``
    on the first page); :class:`CursorPageRequest` omits it on page 1 and never
    emits ``page`` -- keeping the cursor contract disjoint from REST paging.
    """

    per_page = int(request_args.get(PER_PAGE_ARG, 30))
    after = request_args.get(CURSOR_ARG)
    after_str = None if after is None else str(after)
    return CursorPageRequest(after=after_str, per_page=per_page).as_query_params()


def build_request(
    *,
    descriptor: GithubOperationDescriptor,
    request_args: Mapping[str, Any],
) -> HttpRequest:
    """Shape ONE :class:`HttpRequest` for ``descriptor`` from ``request_args``.

    The core of the locator, split out from :func:`locate` so it is testable
    without the transport's keyword surface. In order:

    1. **Cursor short-circuit for a REST page walk.** GitHub REST returns the
       next page's WHOLE url in its ``Link`` header, and the decoder surfaces
       that absolute url as the cursor. When the executor hands it back as
       ``request_args[CURSOR_ARG]`` for a ``REST_PAGE`` operation, that url is
       already a complete request line, so it is sent VERBATIM (method carried
       from the descriptor, no query re-derivation). Re-building ``page``/query
       from it would be re-deriving an opaque continuation the provider owns.
    2. **Otherwise fill the endpoint template** from the path parameters and
       assemble the query from the caller's filters plus the contract's paging
       params (``page``/``perPage`` for REST, ``after``/``perPage`` for cursor,
       nothing for ``NONE``). ``MIXED`` and ``UNKNOWN`` are refused: a single
       locator call cannot honestly pick one of a multi-method tool's contracts,
       so a caller must name the concrete method-scoped operation instead.

    The returned request carries the descriptor's HTTP method, an absolute
    ``https`` url, JSON ``Accept`` headers, and NO credential.
    """

    method = descriptor.http_method.upper()
    pagination = descriptor.pagination

    # (1) A REST page-walk cursor is the provider's own absolute next-page URL.
    cursor = request_args.get(CURSOR_ARG)
    if cursor is not None and pagination is Pagination.REST_PAGE:
        url = str(cursor)
        # A crafted Link header could point the "next page" at any origin; the
        # production transport then attaches the binding's bearer token to
        # whatever host this URL names. So the cursor's ORIGIN (scheme + host +
        # port) MUST equal GITHUB_API_BASE's origin -- a scheme check alone would
        # let a cross-origin URL carry the credential to an attacker. Compared as
        # parsed components (not string prefix) so a look-alike host that merely
        # starts with the base string cannot slip through.
        base = urllib.parse.urlsplit(GITHUB_API_BASE)
        got = urllib.parse.urlsplit(url)
        if (got.scheme.lower(), got.hostname, got.port) != (
            base.scheme.lower(), base.hostname, base.port
        ):
            raise GithubLocatorError(
                "REST pagination cursor must be an absolute URL on the same "
                f"origin as {GITHUB_API_BASE!r}, got {url!r}"
            )
        return HttpRequest(method=method, url=url, headers=_default_headers(), body=None)

    if pagination is Pagination.MIXED:
        raise GithubLocatorError(
            f"operation {descriptor.operation_id!r} has pagination {pagination.value!r}; "
            "a locator needs a single concrete contract -- name the method-scoped "
            "operation instead of the multi-method tool"
        )

    path = _fill_endpoint(descriptor.endpoint, request_args)

    # Query = caller filters (minus path params) + this contract's paging params.
    path_params = set(_endpoint_param_names(descriptor.endpoint))
    query: dict[str, Any] = {
        k: v for k, v in _query_args(request_args).items() if k not in path_params
    }
    if pagination is Pagination.REST_PAGE:
        query.update(_rest_page_params(request_args))
    elif pagination is Pagination.CURSOR_AFTER:
        query.update(_cursor_params(request_args))
    # Pagination.NONE: no paging params; a single-object or unpaginated fetch.

    encoded = _encode_query(query)
    url = f"{GITHUB_API_BASE}{path}"
    if encoded:
        url = f"{url}?{encoded}"

    body = _shape_body(descriptor, request_args)
    return HttpRequest(method=method, url=url, headers=_default_headers(body is not None), body=body)


def _default_headers(has_body: bool = False) -> dict:
    """The vendor headers every GitHub request carries -- WITHOUT a credential.

    ``Accept`` and ``X-GitHub-Api-Version`` are GitHub's documented REST
    convention; a JSON ``Content-Type`` is added only when there is a body. The
    transport adds ``Authorization`` itself, so it is deliberately absent here.
    """

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if has_body:
        headers["Content-Type"] = "application/json"
    return headers


def _shape_body(
    descriptor: GithubOperationDescriptor,
    request_args: Mapping[str, Any],
) -> Optional[bytes]:
    """Encode a JSON request body for a mutating operation, or ``None``.

    A read carries no body. A mutating operation (POST/PATCH/PUT) sends the
    caller's non-path, non-paging args as a JSON object, so the same
    ``request_args`` a locator receives produce the write payload. The path
    parameters and paging keys are excluded (they live in the URL), leaving the
    resource fields the write actually sets.
    """

    if descriptor.http_method.upper() in ("GET", "HEAD"):
        return None
    path_params = set(_endpoint_param_names(descriptor.endpoint))
    fields = {
        k: v
        for k, v in _query_args(request_args).items()
        if k not in path_params
    }
    if not fields:
        return None
    return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")


def locate(
    *,
    service_id: str,
    credential_mode: str,
    descriptor: Mapping[str, Any],
    request_args: Mapping[str, Any],
    request_idempotency_key: str = "",
    **_ignored: Any,
) -> HttpRequest:
    """The injected :data:`RequestLocator` GitHub hands to the transport.

    Matches the keyword surface W01's
    :func:`~kiro_crew.connections.control_plane.production.build_production_transport`
    calls a locator with (``service_id`` / ``credential_mode`` / ``descriptor``
    / ``request_args`` / ``request_idempotency_key``). It resolves the campaign
    ``operation_id`` off the control-plane descriptor to the GitHub instance
    data via :func:`get_descriptor` and delegates to :func:`build_request`.

    ``service_id`` is asserted to be ``github`` -- the transport routes on the
    trusted view's service, and a locator composed for GitHub receiving another
    service's call is a composition error, refused rather than shaped. An
    ``operation_id`` GitHub does not define is refused (no fabricated URL).
    ``credential_mode`` / ``request_idempotency_key`` are part of the contract
    but do not change the URL: the credential is the transport's to attach.
    """

    if service_id != "github":
        raise GithubLocatorError(
            f"the GitHub locator was handed a call routed at service {service_id!r}; "
            "it shapes only GitHub operations"
        )
    operation_id = descriptor["operation_id"]
    github_descriptor = get_descriptor(operation_id)
    if github_descriptor is None:
        raise GithubLocatorError(
            f"operation {operation_id!r} is not a known GitHub operation; refusing "
            "to shape a request for an operation this vendor does not define"
        )
    return build_request(descriptor=github_descriptor, request_args=request_args)


__all__ = [
    "CURSOR_ARG",
    "GITHUB_API_BASE",
    "PER_PAGE_ARG",
    "GithubLocatorError",
    "build_request",
    "locate",
]
