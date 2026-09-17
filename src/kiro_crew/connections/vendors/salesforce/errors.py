"""Salesforce error-classification skeleton.

Maps a Salesforce error signal (HTTP status, and the vendor ``errorCode``
strings its REST responses carry) onto the shared, provider-neutral error
classes the control plane owns and exports:
:data:`kiro_crew.connections.control_plane.ErrorClass` -- the ``RUN-01``
twelve-value closed set (auth / scope / consent / not_found / forbidden /
quota / throttle / conflict / input / temporary / partial / ambiguous). This
module imports and returns that type; it does NOT restate the vocabulary and
does not fork a second taxonomy. A provider stream classifies its failures
INTO the control plane's set so one governance/retry hook can switch on one
vocabulary -- the same design the sibling ``vendors/github/errors.py`` follows.

This module OWNS the Salesforce-specific reading of a failure -- which HTTP
status and which documented vendor ``errorCode`` mean which neutral class --
and nothing else: it does not define the class vocabulary, decide retry/backoff
timing, or build a result envelope.

This is a *skeleton*: it classifies the signals L1 can determine offline and
leaves anything it cannot corroborate as ``ambiguous`` rather than guessing a
class.

Crucially, a timeout with no vendor response is ``ambiguous``, never
``temporary``-as-safe-to-retry: the retry/idempotency layer decides whether a
retry is safe, and it must be able to tell "the call may or may not have taken
effect" apart from "the call definitely failed early". Collapsing an ambiguous
timeout into a retryable class is exactly the failure mode the idempotency
closed set exists to prevent.
"""

from __future__ import annotations

from typing import Optional

from kiro_crew.connections.control_plane import ERROR_CLASSES, ErrorClass

#: Salesforce REST ``errorCode`` strings (search-snippet corroborated) mapped to
#: a control-plane RUN-01 class. Deliberately narrow: only codes whose meaning
#: is corroborated are mapped; an unrecognized code falls through to
#: status-based classification, and then to ``ambiguous``. This mapping is the
#: Salesforce-specific value this module owns -- the vocabulary it maps INTO is
#: the control plane's :data:`ErrorClass`.
_CLASS_BY_VENDOR_CODE: dict[str, ErrorClass] = {
    "INVALID_SESSION_ID": "auth",
    "INVALID_LOGIN": "auth",
    "INSUFFICIENT_ACCESS": "forbidden",
    "INSUFFICIENT_ACCESS_OR_READONLY": "forbidden",
    "API_DISABLED_FOR_ORG": "scope",
    "NOT_FOUND": "not_found",
    "ENTITY_IS_DELETED": "not_found",
    "REQUEST_LIMIT_EXCEEDED": "quota",
    "REQUEST_RUNNING_TOO_LONG": "throttle",
    "DUPLICATE_VALUE": "conflict",
    "DUPLICATES_DETECTED": "conflict",
    "ENTITY_IS_LOCKED": "conflict",
    "UNABLE_TO_LOCK_ROW": "conflict",
    "MALFORMED_QUERY": "input",
    "INVALID_FIELD": "input",
    "INVALID_TYPE": "input",
    "REQUIRED_FIELD_MISSING": "input",
    "SERVER_UNAVAILABLE": "temporary",
}


def classify_vendor_error_code(error_code: str) -> Optional[ErrorClass]:
    """Classify a Salesforce ``errorCode`` string, or ``None`` if unrecognized.

    ``None`` (not ``"ambiguous"``) is returned for an unrecognized code so a
    caller can fall through to :func:`classify_status`; the ``ambiguous`` floor
    is applied once, at the end of a full classification, not per-signal. When a
    code is recognized, the returned value is always a member of the control
    plane's :data:`~kiro_crew.connections.control_plane.ERROR_CLASSES`.
    """

    return _CLASS_BY_VENDOR_CODE.get(error_code)


def classify_status(
    http_status: Optional[int],
    *,
    timed_out: bool = False,
) -> ErrorClass:
    """Classify an outcome from its HTTP status (and whether it timed out).

    A ``timed_out`` call with no status is ``"ambiguous"`` -- the request may
    or may not have taken effect on the server, and only the idempotency layer
    may decide whether retrying is safe. This is intentionally NOT
    ``"temporary"``.

    A ``None`` status that did not time out is also ``"ambiguous"`` (no signal
    to classify). Otherwise the status is mapped by its class; a status the map
    does not cover falls to ``"ambiguous"`` rather than a guessed class. The
    returned value is always a member of the control plane's
    :data:`~kiro_crew.connections.control_plane.ERROR_CLASSES`.
    """

    if timed_out:
        return "ambiguous"
    if http_status is None:
        return "ambiguous"
    if http_status == 401:
        return "auth"
    if http_status == 403:
        return "forbidden"
    if http_status == 404:
        return "not_found"
    if http_status == 409:
        return "conflict"
    if http_status == 429:
        return "throttle"
    if http_status in (400, 422):
        return "input"
    if 500 <= http_status <= 599:
        return "temporary"
    return "ambiguous"


# Re-exported for a caller that wants the control plane's closed set without a
# second import; it IS the control plane's tuple, not a copy.
__all__ = ["classify_vendor_error_code", "classify_status", "ERROR_CLASSES"]
