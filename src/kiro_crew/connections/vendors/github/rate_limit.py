"""Read GitHub's rate-limit signals off a response's headers and body.

GitHub carries primary-rate-limit state in ``x-ratelimit-*`` response headers
on ordinary calls, and signals a secondary (abuse) limit with a ``retry-after``
header and/or a body phrase. The vendor's own guidance is to READ THE HEADERS
on ordinary responses rather than polling ``GET /rate_limit`` (which itself
draws against the secondary limit), and there is NO proactive way to inspect
the secondary-limit budget -- it is only observable once a request is rejected.

This module OWNS only the GitHub-specific reading of those signals into a small
neutral snapshot. It does NOT implement a backoff/retry strategy (that is the
shared control plane's ``RUN`` family) and performs no waiting or I/O; a caller
feeds the snapshot to whatever backoff policy the control plane provides.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

# Primary-limit headers GitHub sets on ordinary responses.
HEADER_LIMIT = "x-ratelimit-limit"
HEADER_REMAINING = "x-ratelimit-remaining"
HEADER_RESET = "x-ratelimit-reset"
HEADER_USED = "x-ratelimit-used"
HEADER_RESOURCE = "x-ratelimit-resource"

# Secondary-limit / generic retry signal.
HEADER_RETRY_AFTER = "retry-after"


def _get(headers: Optional[Mapping[str, str]], name: str) -> Optional[str]:
    """Case-insensitive header lookup (HTTP header names are case-insensitive)."""
    if not headers:
        return None
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _as_int(value: Optional[str]) -> Optional[int]:
    """Parse an integer header value, or ``None`` if absent/malformed.

    A malformed numeric header is treated as absent rather than raising: a
    rate-limit reader must not turn a stray header into an exception on an
    otherwise-usable response.
    """
    if value is None:
        return None
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return None


@dataclass(frozen=True)
class RateLimitSnapshot:
    """A neutral read of GitHub's rate-limit signals on one response.

    Every field is optional because a given response may carry only some of
    them (e.g. a ``retry-after`` on a secondary-limit rejection that omits the
    primary ``x-ratelimit-*`` set, or the reverse on a healthy read).
    """

    limit: Optional[int] = None
    remaining: Optional[int] = None
    reset_epoch: Optional[int] = None
    used: Optional[int] = None
    resource: Optional[str] = None
    retry_after_seconds: Optional[int] = None
    secondary_limit_signaled: bool = False

    @property
    def primary_exhausted(self) -> bool:
        """True when the primary limit is known to be spent (remaining == 0).

        ``None`` remaining (header absent) is NOT treated as exhausted -- an
        unknown budget is unknown, not empty.
        """
        return self.remaining == 0

    @property
    def should_back_off(self) -> bool:
        """True when either limit tells the caller to stop sending now.

        A convenience for a backoff policy; this module still does not DO the
        backing off, it only reports that the signals warrant it.
        """
        return (
            self.secondary_limit_signaled
            or self.retry_after_seconds is not None
            or self.primary_exhausted
        )


def read_rate_limit(
    headers: Optional[Mapping[str, str]],
    body_message: str = "",
) -> RateLimitSnapshot:
    """Read a :class:`RateLimitSnapshot` from response headers and body text.

    Pure: same inputs yield the same snapshot, no I/O. The secondary-limit flag
    is set when EITHER a ``retry-after`` header is present OR the body names the
    secondary limit -- both are documented signals and either alone is
    sufficient, so a reader that checked only one would miss the other's case.
    """
    message = (body_message or "").lower()
    retry_after = _as_int(_get(headers, HEADER_RETRY_AFTER))
    secondary = (
        retry_after is not None or "secondary rate limit" in message or "abuse detection" in message
    )
    return RateLimitSnapshot(
        limit=_as_int(_get(headers, HEADER_LIMIT)),
        remaining=_as_int(_get(headers, HEADER_REMAINING)),
        reset_epoch=_as_int(_get(headers, HEADER_RESET)),
        used=_as_int(_get(headers, HEADER_USED)),
        resource=_get(headers, HEADER_RESOURCE),
        retry_after_seconds=retry_after,
        secondary_limit_signaled=secondary,
    )
