"""Map a Google Drive API v3 failure onto the shared neutral error classes.

Owns ONLY the Drive-specific reading of a failure -- which HTTP status + which
documented ``reason`` marker means which neutral class -- and nothing else: it
does not define the vocabulary, decide retry timing, or build a result. It
imports the same twelve-value :class:`ErrorClass` set every other provider
stream classifies into, so one governance/retry hook switches on one vocabulary.

Drive-specific facts encoded here, from Google's Drive API v3 error reference:

* A **401** is an invalid/expired/revoked credential -> ``auth``.
* A **403** is OVERLOADED and must be split by its ``errors[].reason``:
  ``userRateLimitExceeded`` / ``rateLimitExceeded`` / ``dailyLimitExceeded`` are
  throttling -> ``throttle``; ``insufficientFilePermissions`` / ``fileNotDownloadable``
  / ``appNotAuthorizedToFile`` are a genuine permission failure -> ``forbidden``.
  A 403 with no recognized reason falls back to ``forbidden`` (the conservative
  read: treat an unknown 403 as a permission problem, not a transient one, so it
  is not blindly retried).
* A **404** is ``not_found`` (a deleted file, or one the caller cannot see --
  Drive, like GitHub, may hide a no-access file as 404; the wire cannot tell
  them apart and this mapping does not pretend to).
* A **429** is ``throttle``.
* A **400** is a malformed request -> ``input``.
* A **412** precondition-failed is ``conflict`` (handled structurally upstream
  by the executor; classified here only for a direct caller).
* **5xx** is ``temporary``.

The TOKEN-INVALID signal is deliberately NOT a class of its own. Google
publishes no dedicated status/reason for an expired change page token; what is
observed is a 4xx on ``changes.list`` from a stale token. :func:`is_stale_page_token`
recognises the documented Drive SHAPE (a 400 carrying an invalid-page-token
reason) conservatively -- it is the pure, reason-level classifier and the record
of the Drive fact.

NOTE ON THE LIVE PATH: when a Drive call is executed through W01, the provider's
``reason`` text is REDACTED by the executor before a vendor sees it (a security
boundary), so this reason-level recogniser cannot run on that path. The live
resync trigger is therefore ``operations._is_stale_page_token``, keyed on W01's
neutral ``input`` error CLASS (see that function for the full documentation-gap
rationale). This function stays as the documented Drive-shape classifier and is
what a direct, non-W01 caller (or a future W01 surface that carries the status)
would use -- neither invents a status code Google never published.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from kiro_crew.connections.control_plane import ErrorClass

# 403 reason markers that mean THROTTLING (retry after backoff), not a
# permission failure. Exact Drive ``reason`` tokens from the v3 error reference.
_THROTTLE_403_REASONS = frozenset(
    {"userRateLimitExceeded", "rateLimitExceeded", "dailyLimitExceeded"}
)

# 403 reason markers that mean a genuine PERMISSION failure (do not retry).
_FORBIDDEN_403_REASONS = frozenset(
    {
        "insufficientFilePermissions",
        "fileNotDownloadable",
        "appNotAuthorizedToFile",
        "insufficientPermissions",
    }
)

# Reason markers Drive uses for an invalid/expired PAGE TOKEN on changes.list.
# Google documents no dedicated status code; the observed shapes are a 400 with
# one of these reasons. Kept conservative -- an unrecognised 4xx is NOT read as a
# stale token (that would mask a real input error as a resync).
_STALE_TOKEN_REASONS = frozenset({"invalidPageToken", "pageTokenInvalid"})


@dataclass(frozen=True)
class DriveFailure:
    """The wire-observable shape of a failed Drive API response.

    Only the fields the mapping reads are carried; a caller assembles this from a
    real transport response. ``reason`` is the first ``error.errors[].reason``
    token Drive returned (Drive nests the machine-readable reason there, distinct
    from the human ``message``). ``headers`` keys match case-insensitively.
    """

    status: int
    reason: str = ""
    message: str = ""
    headers: Optional[Mapping[str, str]] = None

    def header(self, name: str) -> Optional[str]:
        if not self.headers:
            return None
        target = name.lower()
        for key, value in self.headers.items():
            if key.lower() == target:
                return value
        return None


def classify(failure: DriveFailure) -> ErrorClass:
    """Return the neutral :class:`ErrorClass` for a Drive failure.

    Total over every status: an unmapped status >= 500 is ``temporary``,
    anything else unmapped is ``input`` (a client-side shape the caller should
    fix, not retry).
    """
    status = failure.status
    if status == 401:
        return "auth"
    if status == 403:
        reason = failure.reason
        if reason in _THROTTLE_403_REASONS:
            return "throttle"
        if reason in _FORBIDDEN_403_REASONS:
            return "forbidden"
        # Unknown 403: treat as a permission failure (conservative -- do not
        # retry a 403 we cannot prove is a rate limit).
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 409:
        return "conflict"
    if status == 412:
        return "conflict"
    if status == 429:
        return "throttle"
    if status == 400:
        return "input"
    if status >= 500:
        return "temporary"
    return "input"


def is_stale_page_token(failure: DriveFailure) -> bool:
    """True when a ``changes.list`` failure means the saved page token is stale.

    Recognised conservatively: a 400 (Drive's status for an invalid page token)
    carrying one of the documented invalid-token reasons. Returns False for every
    other failure so a real input error is not silently swallowed as a resync.
    Google publishes no TTL or dedicated status for an expired token, so this
    recogniser -- and the "re-fetch start page token, resync" recovery it gates --
    is the documented remedy, not a guessed one.

    This is the REASON-level classifier and requires the provider ``reason`` to be
    present. On the W01 execute path the reason is redacted before a vendor sees
    it, so the live trigger there is ``operations._is_stale_page_token`` (keyed on
    W01's ``input`` class); this function is for a direct caller that still holds
    the raw failure, and it records the Drive fact.
    """
    if failure.status != 400:
        return False
    return failure.reason in _STALE_TOKEN_REASONS


def retry_after_seconds(failure: DriveFailure) -> Optional[float]:
    """The server's advisory backoff for a throttled/temporary failure, or None.

    Reads the standard ``Retry-After`` header (Drive sends it on some 429/503
    responses). A malformed value is ignored (None) rather than raising -- an
    unparseable backoff is no backoff advice, not a crash.
    """
    raw = failure.header("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


__all__ = [
    "DriveFailure",
    "classify",
    "is_stale_page_token",
    "retry_after_seconds",
]
