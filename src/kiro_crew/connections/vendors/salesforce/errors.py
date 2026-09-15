"""Salesforce error-classification skeleton.

Maps a Salesforce error signal (HTTP status, and the vendor ``errorCode``
strings its REST responses carry) onto the campaign's neutral error taxonomy --
the ``RUN-01`` family the connector DAG names: auth / scope / consent /
not_found / forbidden / quota / throttle / conflict / input / temporary /
partial / ambiguous. This is a *skeleton*: it classifies the signals L1 can
determine offline and leaves anything it cannot corroborate as
:attr:`SalesforceErrorCategory.AMBIGUOUS` rather than guessing a category.

Crucially, a timeout with no vendor response is :attr:`AMBIGUOUS`, never
``temporary``-as-safe-to-retry: the retry/idempotency layer decides whether a
retry is safe, and it must be able to tell "the call may or may not have taken
effect" apart from "the call definitely failed early". Collapsing an ambiguous
timeout into a retryable class is exactly the failure mode the idempotency
closed set exists to prevent.
"""

from __future__ import annotations

import enum
from typing import Optional


class SalesforceErrorCategory(str, enum.Enum):
    """The neutral RUN-01 error taxonomy, as it applies to Salesforce."""

    AUTH = "auth"
    SCOPE = "scope"
    CONSENT = "consent"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    QUOTA = "quota"
    THROTTLE = "throttle"
    CONFLICT = "conflict"
    INPUT = "input"
    TEMPORARY = "temporary"
    PARTIAL = "partial"
    AMBIGUOUS = "ambiguous"


#: Salesforce REST ``errorCode`` strings (search-snippet corroborated) mapped to
#: a taxonomy category. Deliberately narrow: only codes whose meaning is
#: corroborated are mapped; an unrecognized code falls through to status-based
#: classification, and then to AMBIGUOUS.
_CATEGORY_BY_VENDOR_CODE: dict[str, SalesforceErrorCategory] = {
    "INVALID_SESSION_ID": SalesforceErrorCategory.AUTH,
    "INVALID_LOGIN": SalesforceErrorCategory.AUTH,
    "INSUFFICIENT_ACCESS": SalesforceErrorCategory.FORBIDDEN,
    "INSUFFICIENT_ACCESS_OR_READONLY": SalesforceErrorCategory.FORBIDDEN,
    "API_DISABLED_FOR_ORG": SalesforceErrorCategory.SCOPE,
    "NOT_FOUND": SalesforceErrorCategory.NOT_FOUND,
    "ENTITY_IS_DELETED": SalesforceErrorCategory.NOT_FOUND,
    "REQUEST_LIMIT_EXCEEDED": SalesforceErrorCategory.QUOTA,
    "REQUEST_RUNNING_TOO_LONG": SalesforceErrorCategory.THROTTLE,
    "DUPLICATE_VALUE": SalesforceErrorCategory.CONFLICT,
    "DUPLICATES_DETECTED": SalesforceErrorCategory.CONFLICT,
    "ENTITY_IS_LOCKED": SalesforceErrorCategory.CONFLICT,
    "UNABLE_TO_LOCK_ROW": SalesforceErrorCategory.CONFLICT,
    "MALFORMED_QUERY": SalesforceErrorCategory.INPUT,
    "INVALID_FIELD": SalesforceErrorCategory.INPUT,
    "INVALID_TYPE": SalesforceErrorCategory.INPUT,
    "REQUIRED_FIELD_MISSING": SalesforceErrorCategory.INPUT,
    "SERVER_UNAVAILABLE": SalesforceErrorCategory.TEMPORARY,
}


def classify_vendor_error_code(error_code: str) -> Optional[SalesforceErrorCategory]:
    """Classify a Salesforce ``errorCode`` string, or ``None`` if unrecognized.

    ``None`` (not ``AMBIGUOUS``) is returned for an unrecognized code so a caller
    can fall through to :func:`classify_status`; the AMBIGUOUS floor is applied
    once, at the end of a full classification, not per-signal.
    """

    return _CATEGORY_BY_VENDOR_CODE.get(error_code)


def classify_status(
    http_status: Optional[int],
    *,
    timed_out: bool = False,
) -> SalesforceErrorCategory:
    """Classify an outcome from its HTTP status (and whether it timed out).

    A ``timed_out`` call with no status is :attr:`AMBIGUOUS` -- the request may
    or may not have taken effect on the server, and only the idempotency layer
    may decide whether retrying is safe. This is intentionally NOT ``temporary``.

    A ``None`` status that did not time out is also :attr:`AMBIGUOUS` (no signal
    to classify). Otherwise the status is mapped by its class; a status the map
    does not cover falls to :attr:`AMBIGUOUS` rather than a guessed category.
    """

    if timed_out:
        return SalesforceErrorCategory.AMBIGUOUS
    if http_status is None:
        return SalesforceErrorCategory.AMBIGUOUS
    if http_status == 401:
        return SalesforceErrorCategory.AUTH
    if http_status == 403:
        return SalesforceErrorCategory.FORBIDDEN
    if http_status == 404:
        return SalesforceErrorCategory.NOT_FOUND
    if http_status == 409:
        return SalesforceErrorCategory.CONFLICT
    if http_status == 429:
        return SalesforceErrorCategory.THROTTLE
    if http_status in (400, 422):
        return SalesforceErrorCategory.INPUT
    if 500 <= http_status <= 599:
        return SalesforceErrorCategory.TEMPORARY
    return SalesforceErrorCategory.AMBIGUOUS
