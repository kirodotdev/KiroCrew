"""Generic HTTP adapter: status codes and ``Retry-After`` on any HTTP error.

Recognises the exception shapes the codebase actually raises -- ``urllib``'s
``HTTPError`` (``.code``, ``.headers``), ``aiohttp``'s ``ClientResponseError``
(``.status``, ``.headers``, ``.request_info.url``), ``httpx``'s
``HTTPStatusError`` (``.response.status_code`` / ``.headers`` / ``.url``) --
by duck typing, so none of those libraries is imported here. A plain
exception whose text carries ``HTTP 429`` / ``status 503`` is read too, which
is how ``gh``/``curl`` stderr reaches this adapter.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

from ..dependency import (
    KIND_AUTH_FAILED,
    KIND_DEPENDENCY_UNAVAILABLE,
    KIND_PERMANENT_PARAM_ERROR,
    KIND_RATE_LIMITED,
    DependencySignal,
)

SOURCE = "http"

#: 4xx that mean "your request", not "our load": retrying is pointless.
PERMANENT_STATUSES: frozenset[int] = frozenset({400, 404, 405, 406, 410, 411, 413, 414, 415, 422})
AUTH_STATUSES: frozenset[int] = frozenset({401, 407})
#: 5xx and transport-ish statuses the dependency will recover from.
UNAVAILABLE_STATUSES: frozenset[int] = frozenset({500, 502, 503, 504, 507, 508, 522, 523, 524, 529})

_RE_STATUS = re.compile(r"\b(?:HTTP|status(?:\s+code)?)[:\s]+(\d{3})\b", re.IGNORECASE)
_RE_RETRY_AFTER_TEXT = re.compile(r"\bretry-?after[:\s=]+(\d+)\b", re.IGNORECASE)


def header(headers: Any, name: str) -> str | None:
    """Case-insensitive header lookup over a Mapping, an ``HTTPMessage`` or a list of pairs."""
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        for candidate in (name, name.lower(), name.title(), name.upper()):
            try:
                value = getter(candidate)
            except Exception:  # noqa: BLE001 - odd header containers
                value = None
            if value is not None:
                return str(value)
    if isinstance(headers, Mapping):
        lowered = name.lower()
        for key, value in headers.items():
            if str(key).lower() == lowered:
                return str(value)
        return None
    try:
        for key, value in headers:  # list of pairs
            if str(key).lower() == name.lower():
                return str(value)
    except (TypeError, ValueError):
        return None
    return None


def parse_retry_after(value: str | None, now: float) -> float | None:
    """``Retry-After`` -> absolute epoch seconds; delay-seconds or an HTTP date."""
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.isdigit():
        return now + float(int(raw))
    try:
        return parsedate_to_datetime(raw).timestamp()
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def parse_reset_epoch(value: str | None) -> float | None:
    """``X-RateLimit-Reset`` (GitHub, many APIs) -> absolute epoch seconds."""
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        reset = float(raw)
    except ValueError:
        return None
    # Some APIs send milliseconds; anything past year 5138 is not seconds.
    if reset > 1e11:
        reset /= 1000.0
    return reset


def status_of(exc: BaseException) -> int | None:
    """HTTP status from the common client exception shapes, else from the text."""
    for attr in ("status", "status_code", "code", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("status_code", "status"):
            value = getattr(response, attr, None)
            if isinstance(value, int) and 100 <= value <= 599:
                return value
    match = _RE_STATUS.search(str(exc))
    if match is not None:
        return int(match.group(1))
    return None


def headers_of(exc: BaseException) -> Any:
    for attr in ("headers", "hdrs"):
        value = getattr(exc, attr, None)
        if value is not None:
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "headers", None)
        if value is not None:
            return value
    return None


def host_of(exc: BaseException) -> str | None:
    """The request host, when the exception carries its URL."""
    candidates: list[Any] = [getattr(exc, "url", None), getattr(exc, "filename", None)]
    request_info = getattr(exc, "request_info", None)
    if request_info is not None:
        candidates.append(getattr(request_info, "url", None))
    for owner in (getattr(exc, "request", None), getattr(exc, "response", None)):
        if owner is not None:
            candidates.append(getattr(owner, "url", None))
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            host = urlsplit(str(candidate)).hostname
        except ValueError:
            continue
        if host:
            return host.lower()
    return None


def retry_at_from(exc: BaseException, now: float) -> float | None:
    """``Retry-After`` / ``X-RateLimit-Reset`` from headers, else from the text."""
    hdrs = headers_of(exc)
    retry_at = parse_retry_after(header(hdrs, "Retry-After"), now)
    if retry_at is None:
        retry_at = parse_reset_epoch(header(hdrs, "X-RateLimit-Reset"))
    if retry_at is None:
        explicit = getattr(exc, "retry_after", None)
        if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
            retry_at = now + float(explicit)
    if retry_at is None:
        match = _RE_RETRY_AFTER_TEXT.search(str(exc))
        if match is not None:
            retry_at = now + float(int(match.group(1)))
    return retry_at


def kind_for_status(status: int) -> str | None:
    if status == 429:
        return KIND_RATE_LIMITED
    if status in AUTH_STATUSES:
        return KIND_AUTH_FAILED
    if status in PERMANENT_STATUSES:
        return KIND_PERMANENT_PARAM_ERROR
    if status in UNAVAILABLE_STATUSES or status == 408:
        return KIND_DEPENDENCY_UNAVAILABLE
    return None


def signal_for_status(
    status: int,
    *,
    scope: str,
    source: str,
    retry_at: float | None,
    detail: str,
    forbidden_is_auth: bool = True,
) -> DependencySignal | None:
    """Build the signal for a status; ``403`` is auth unless the caller says otherwise."""
    kind = kind_for_status(status)
    if kind is None and status == 403:
        kind = KIND_AUTH_FAILED if forbidden_is_auth else KIND_PERMANENT_PARAM_ERROR
    if kind is None:
        return None
    return DependencySignal(
        kind=kind,
        dependency_scope=scope,
        source=source,
        retry_at=(
            retry_at if kind == KIND_RATE_LIMITED or kind == KIND_DEPENDENCY_UNAVAILABLE else None
        ),
        detail=detail,
    )


def classify(exc: BaseException, scope: str = "") -> DependencySignal | None:
    """Generic HTTP classification; the scope is ``http:<host>`` when the host is known."""
    status = status_of(exc)
    if status is None:
        return None
    host = host_of(exc)
    effective_scope = scope or (f"http:{host}" if host else "http")
    now = observed_now(exc)
    return signal_for_status(
        status,
        scope=effective_scope,
        source=SOURCE,
        retry_at=retry_at_from(exc, now),
        detail=f"HTTP {status}: {str(exc)[:200]}",
    )


def observed_now(exc: BaseException) -> float:
    """``Retry-After`` is relative: adapters read the caller's clock off the exception
    when a seam attached one (``exc.observed_at``), else the wall clock."""
    observed = getattr(exc, "observed_at", None)
    if isinstance(observed, (int, float)) and not isinstance(observed, bool):
        return float(observed)
    return time.time()


__all__ = [
    "AUTH_STATUSES",
    "PERMANENT_STATUSES",
    "SOURCE",
    "UNAVAILABLE_STATUSES",
    "classify",
    "header",
    "headers_of",
    "host_of",
    "kind_for_status",
    "observed_now",
    "parse_reset_epoch",
    "parse_retry_after",
    "retry_at_from",
    "signal_for_status",
    "status_of",
]
