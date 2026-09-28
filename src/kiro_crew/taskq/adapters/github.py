"""GitHub adapter: REST/GraphQL responses and ``gh`` CLI stderr -> one signal.

GitHub reports a primary rate limit as ``403`` (REST) or ``200`` with a
``RATE_LIMITED`` GraphQL error, and a secondary/abuse limit as ``403``/``429``
with ``Retry-After``; every one carries ``X-RateLimit-Reset`` (epoch seconds)
when the primary limit is the cause. The ``gh`` CLI folds the same answers
into stderr prose (``HTTP 403: API rate limit exceeded for installation``,
``HTTP 429``, ``abuse detection``). This module reads all of those, and
:func:`parse_gh_stderr` is the ONE classifier the pull-request and
workflow-run monitors call instead of each carrying its own copy.

Scopes: the primary limit is shared per token across the whole API, so the
default scope is ``github:api``; a GraphQL error is ``github:graphql`` and a
secondary limit is ``github:secondary`` because those pools are separate.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from ..dependency import (
    KIND_AUTH_FAILED,
    KIND_DEPENDENCY_UNAVAILABLE,
    KIND_PERMANENT_PARAM_ERROR,
    KIND_RATE_LIMITED,
    DependencySignal,
)
from . import http as _http

SOURCE = "github"
SCOPE_API = "github:api"
SCOPE_GRAPHQL = "github:graphql"
SCOPE_SECONDARY = "github:secondary"

#: Provider-error categories in the monitors' vocabulary. Kept as strings so
#: this module does not import ``monitoring.models``; the monitors map them.
CATEGORY_RATE_LIMITED = "rate_limited"
CATEGORY_AUTHENTICATION = "authentication"
CATEGORY_AUTHORIZATION = "authorization"
CATEGORY_NOT_FOUND = "not_found"
CATEGORY_TRANSIENT = "transient"

_RATE_LIMIT_MARKERS = ("rate limit", "abuse detection", "too many requests", "secondary rate")
_SECONDARY_MARKERS = ("secondary rate", "abuse detection")
_AUTHENTICATION_MARKERS = (
    "bad credentials",
    "authentication",
    "not logged into",
    "gh auth login",
    "requires authentication",
)
_NOT_FOUND_MARKERS = (
    "not found",
    "could not resolve to a",
    "no runs found",
)
_AUTHORIZATION_MARKERS = ("forbidden", "permission", "resource not accessible", "saml")
_TRANSIENT_MARKERS = ("could not resolve host",)

_HTTP_STATUS_RE = re.compile(r"\bhttp\s+(\d{3})\b", re.IGNORECASE)
_GH_HOST_RE = re.compile(r"\bhttps?://([a-z0-9.-]*github(?:usercontent)?\.com)\b", re.IGNORECASE)


@dataclass(frozen=True)
class GhFailure:
    """What ``gh`` stderr said, in the monitors' category vocabulary."""

    category: str
    status: int | None
    retry_at: float | None
    secondary: bool


def parse_gh_stderr(raw: str, *, now: float | None = None) -> GhFailure:
    """Classify ``gh`` CLI stderr.

    Rate-limit wording wins over any status (a ``403`` that says "rate limit"
    is a throttle, not a permission problem). The first ``HTTP <ddd>`` token
    is then read; a ``429`` behind an earlier status survives to a substring
    check, exactly as the two monitor copies did. Falls back to wording for
    authentication, not-found and authorization, and to ``transient`` --
    the retry-safe default -- for anything else.
    """
    lowered = raw.lower()
    status: int | None = None
    match = _HTTP_STATUS_RE.search(raw)
    if match is not None:
        status = int(match.group(1), 10)
    clock = time.time() if now is None else now
    retry_at = _http.parse_retry_after(_retry_after_in_text(raw), clock)
    if retry_at is None:
        retry_at = _http.parse_reset_epoch(_reset_in_text(raw))
    secondary = any(marker in lowered for marker in _SECONDARY_MARKERS)
    if any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
        return GhFailure(CATEGORY_RATE_LIMITED, status, retry_at, secondary)
    if status is not None:
        if status == 429:
            return GhFailure(CATEGORY_RATE_LIMITED, status, retry_at, secondary)
        if status == 401:
            return GhFailure(CATEGORY_AUTHENTICATION, status, None, False)
        if status == 403:
            return GhFailure(CATEGORY_AUTHORIZATION, status, None, False)
        if status == 404:
            return GhFailure(CATEGORY_NOT_FOUND, status, None, False)
        if status >= 500:
            return GhFailure(CATEGORY_TRANSIENT, status, retry_at, False)
    if any(marker in lowered for marker in _TRANSIENT_MARKERS):
        return GhFailure(CATEGORY_TRANSIENT, status, None, False)
    if "http 429" in lowered:
        return GhFailure(CATEGORY_RATE_LIMITED, 429, retry_at, secondary)
    if any(marker in lowered for marker in _AUTHENTICATION_MARKERS):
        return GhFailure(CATEGORY_AUTHENTICATION, status, None, False)
    if any(marker in lowered for marker in _NOT_FOUND_MARKERS):
        return GhFailure(CATEGORY_NOT_FOUND, status, None, False)
    if any(marker in lowered for marker in _AUTHORIZATION_MARKERS):
        return GhFailure(CATEGORY_AUTHORIZATION, status, None, False)
    return GhFailure(CATEGORY_TRANSIENT, status, retry_at, False)


_RE_RETRY_AFTER_TEXT = re.compile(r"\bretry-?after[:\s=]+(\d+)\b", re.IGNORECASE)
_RE_RESET_TEXT = re.compile(r"\bx-ratelimit-reset[:\s=]+(\d{9,13})\b", re.IGNORECASE)


def _retry_after_in_text(raw: str) -> str | None:
    match = _RE_RETRY_AFTER_TEXT.search(raw)
    return match.group(1) if match else None


def _reset_in_text(raw: str) -> str | None:
    match = _RE_RESET_TEXT.search(raw)
    return match.group(1) if match else None


def signal_from_failure(
    failure: GhFailure, *, scope: str = "", detail: str = ""
) -> DependencySignal | None:
    """Monitor category -> signal. ``transient`` with no status is unknown, not a dependency error."""
    if failure.category == CATEGORY_RATE_LIMITED:
        default_scope = SCOPE_SECONDARY if failure.secondary else SCOPE_API
        return DependencySignal(
            kind=KIND_RATE_LIMITED,
            dependency_scope=scope or default_scope,
            source=SOURCE,
            retry_at=failure.retry_at,
            detail=detail,
        )
    if failure.category in (CATEGORY_AUTHENTICATION, CATEGORY_AUTHORIZATION):
        return DependencySignal(
            kind=KIND_AUTH_FAILED, dependency_scope=scope or SCOPE_API, source=SOURCE, detail=detail
        )
    if failure.category == CATEGORY_NOT_FOUND:
        return DependencySignal(
            kind=KIND_PERMANENT_PARAM_ERROR,
            dependency_scope=scope or SCOPE_API,
            source=SOURCE,
            detail=detail,
        )
    if (
        failure.category == CATEGORY_TRANSIENT
        and failure.status is not None
        and failure.status >= 500
    ):
        return DependencySignal(
            kind=KIND_DEPENDENCY_UNAVAILABLE,
            dependency_scope=scope or SCOPE_API,
            source=SOURCE,
            retry_at=failure.retry_at,
            detail=detail,
        )
    return None


def classify_stderr(
    raw: str, *, scope: str = "", now: float | None = None
) -> DependencySignal | None:
    """``gh`` stderr -> signal (``None`` when the text names no dependency error)."""
    return signal_from_failure(parse_gh_stderr(raw, now=now), scope=scope, detail=raw[:200])


def classify_response(
    status: int, headers: Any, *, body: str = "", scope: str = "", now: float | None = None
) -> DependencySignal | None:
    """A raw GitHub REST response (status + headers, optional body text) -> signal."""
    clock = time.time() if now is None else now
    retry_at = _http.parse_retry_after(_http.header(headers, "Retry-After"), clock)
    reset = _http.parse_reset_epoch(_http.header(headers, "X-RateLimit-Reset"))
    remaining = _http.header(headers, "X-RateLimit-Remaining")
    lowered = body.lower()
    secondary = any(marker in lowered for marker in _SECONDARY_MARKERS) or (
        status in (403, 429) and retry_at is not None and remaining not in (None, "0")
    )
    rate_limited = (
        status == 429
        or any(marker in lowered for marker in _RATE_LIMIT_MARKERS)
        or (status == 403 and remaining == "0")
    )
    if rate_limited:
        return DependencySignal(
            kind=KIND_RATE_LIMITED,
            dependency_scope=scope or (SCOPE_SECONDARY if secondary else SCOPE_API),
            source=SOURCE,
            retry_at=retry_at if retry_at is not None else reset,
            detail=f"HTTP {status}: {body[:200]}",
        )
    return _http.signal_for_status(
        status,
        scope=scope or SCOPE_API,
        source=SOURCE,
        retry_at=retry_at if retry_at is not None else reset,
        detail=f"HTTP {status}: {body[:200]}",
    )


def classify_graphql_errors(
    errors: list[dict[str, Any]], *, scope: str = "", now: float | None = None
) -> DependencySignal | None:
    """GraphQL ``errors[]`` (a ``200`` envelope) -> signal by ``type``/``message``."""
    for err in errors:
        if not isinstance(err, dict):
            continue
        etype = str(err.get("type", "")).upper()
        message = str(err.get("message", ""))
        lowered = message.lower()
        if etype == "RATE_LIMITED" or "rate limit" in lowered:
            return DependencySignal(
                kind=KIND_RATE_LIMITED,
                dependency_scope=scope or SCOPE_GRAPHQL,
                source=SOURCE,
                retry_at=None,
                detail=message[:200],
            )
        if etype in ("FORBIDDEN", "INSUFFICIENT_SCOPES") or "authentication" in lowered:
            return DependencySignal(
                kind=KIND_AUTH_FAILED,
                dependency_scope=scope or SCOPE_GRAPHQL,
                source=SOURCE,
                detail=message[:200],
            )
        if etype == "NOT_FOUND" or "could not resolve to a" in lowered:
            return DependencySignal(
                kind=KIND_PERMANENT_PARAM_ERROR,
                dependency_scope=scope or SCOPE_GRAPHQL,
                source=SOURCE,
                detail=message[:200],
            )
    return None


def is_github_error(exc: BaseException) -> bool:
    host = _http.host_of(exc)
    if host is not None:
        return host.endswith("github.com") or host.endswith("githubusercontent.com")
    text = str(exc)
    return bool(_GH_HOST_RE.search(text)) or "github" in text.lower()


def classify(exc: BaseException, scope: str = "") -> DependencySignal | None:
    """Adapter entry: GitHub HTTP errors, GraphQL envelopes carried on the exception, ``gh`` text."""
    if not is_github_error(exc) and not scope.startswith("github"):
        return None
    now = _http.observed_now(exc)
    errors = getattr(exc, "graphql_errors", None)
    if isinstance(errors, list):
        signal = classify_graphql_errors(errors, scope=scope, now=now)
        if signal is not None:
            return signal
    status = _http.status_of(exc)
    if status is not None:
        return classify_response(status, _http.headers_of(exc), body=str(exc), scope=scope, now=now)
    return classify_stderr(str(exc), scope=scope, now=now)


__all__ = [
    "CATEGORY_AUTHENTICATION",
    "CATEGORY_AUTHORIZATION",
    "CATEGORY_NOT_FOUND",
    "CATEGORY_RATE_LIMITED",
    "CATEGORY_TRANSIENT",
    "GhFailure",
    "SCOPE_API",
    "SCOPE_GRAPHQL",
    "SCOPE_SECONDARY",
    "SOURCE",
    "classify",
    "classify_graphql_errors",
    "classify_response",
    "classify_stderr",
    "is_github_error",
    "parse_gh_stderr",
    "signal_from_failure",
]
