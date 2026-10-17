"""Map a GitHub API failure (HTTP status + response body) onto the shared,
provider-neutral error classes the control plane owns.

This module OWNS the GitHub-specific reading of a failure -- which HTTP status
and which documented body markers mean which neutral class -- and NOTHING
else. It does not define the class vocabulary, it does not decide retry/backoff
timing, and it does not build a result envelope. It answers exactly one
question: given what GitHub returned on the wire, which neutral class is this?

The neutral vocabulary is the ``RUN-01`` typed error taxonomy the shared
control plane owns and exports: :data:`kiro_crew.connections.control_plane.ErrorClass`
(a twelve-value closed set -- ``auth`` / ``scope`` / ``consent`` / ``not_found``
/ ``forbidden`` / ``quota`` / ``throttle`` / ``conflict`` / ``input`` /
``temporary`` / ``partial`` / ``ambiguous``). This module imports and returns
that type; it does not restate the vocabulary and does not fork a second
taxonomy. A provider stream classifies its failures INTO the control plane's
set so one governance/retry hook can switch on one vocabulary.

GitHub-specific facts this mapping encodes, each traceable to the vendor's own
documentation captured for the campaign:

* GitHub returns **404, not 403**, when a caller is not authorized to see a
  private resource, to avoid confirming the resource exists. So a 404 is
  ``not_found`` and is NOT re-read as ``forbidden`` -- the hidden-private case
  is indistinguishable on the wire from a truly missing resource, by design,
  and this mapping must not pretend to distinguish them.
* A **403 or 429 carrying a secondary-rate-limit signal** is ``throttle``, not
  ``forbidden``: the body carries a secondary-limit message and/or a
  ``retry-after`` header. A primary-rate-limit 403 (``x-ratelimit-remaining:
  0``) is likewise ``throttle``. A 403 with neither signal is a genuine
  permission failure -> ``forbidden``.
* A **409** on a write is a base-SHA / head-moved ``conflict``.
* A **422** is GitHub's validation-failure status -> ``input`` (a bad base
  branch, a malformed ref, a duplicate name).
* A **401** is an invalid/expired/revoked credential -> ``auth``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from kiro_crew.connections.control_plane import ERROR_CLASSES, ErrorClass


@dataclass(frozen=True)
class GithubFailure:
    """The wire-observable shape of a failed GitHub API response.

    Only the fields the mapping actually reads are carried; a caller assembles
    this from a real transport response in a later round. ``headers`` keys are
    matched case-insensitively (HTTP header names are case-insensitive, and
    GitHub emits lowercase ``x-ratelimit-*``).
    """

    status: int
    body_message: str = ""
    headers: Optional[Mapping[str, str]] = None

    def header(self, name: str) -> Optional[str]:
        """Return a header value by case-insensitive name, or ``None``."""
        if not self.headers:
            return None
        target = name.lower()
        for key, value in self.headers.items():
            if key.lower() == target:
                return value
        return None


def _looks_like_secondary_limit(failure: GithubFailure) -> bool:
    """True when a 403/429 carries a secondary-rate-limit signal.

    GitHub documents two independent signals, either of which marks a
    secondary limit: a ``retry-after`` header, or a body phrase naming the
    secondary limit ("secondary rate limit" / "abuse detection"). There is no
    proactive way to inspect the secondary-limit budget, so the failure itself
    is the only evidence -- read both signals, not just one.
    """
    if failure.header("retry-after") is not None:
        return True
    message = (failure.body_message or "").lower()
    return "secondary rate limit" in message or "abuse detection" in message


def _looks_like_primary_limit(failure: GithubFailure) -> bool:
    """True when a 403 is a primary-rate-limit exhaustion.

    The documented primary-limit signal is ``x-ratelimit-remaining: 0``; the
    body also typically names the primary rate limit. Either is sufficient.
    """
    remaining = failure.header("x-ratelimit-remaining")
    if remaining is not None and remaining.strip() == "0":
        return True
    return "api rate limit exceeded" in (failure.body_message or "").lower()


def classify_github_failure(failure: GithubFailure) -> ErrorClass:
    """Return the control plane's RUN-01 class for a GitHub failure response.

    Pure mapping: same input always yields the same class, no I/O, no timing
    decision. The returned value is always a member of the control plane's
    :data:`~kiro_crew.connections.control_plane.ERROR_CLASSES`.
    """
    status = failure.status

    if status == 401:
        # Invalid, expired, or revoked credential. Distinct from 403: 403 is a
        # recognized identity that is insufficiently permissioned, 401 is an
        # identity the server does not accept at all.
        return "auth"

    if status == 429:
        # 429 is always a rate-limit signal; GitHub uses it for the secondary
        # limit in particular. It is throttling regardless of which limit.
        return "throttle"

    if status == 403:
        # 403 is overloaded on GitHub: it is the status for both a genuine
        # permission failure AND a rate-limit rejection. Disambiguate on the
        # documented signals; a bare 403 with no rate-limit signal is a real
        # permission denial.
        if _looks_like_secondary_limit(failure) or _looks_like_primary_limit(failure):
            return "throttle"
        return "forbidden"

    if status == 404:
        # A private resource hidden from an unauthorized caller and a truly
        # missing resource are the SAME 404 on the wire, by GitHub's own
        # design (it will not confirm a private resource exists). This mapping
        # returns not_found for both and does not fabricate a distinction the
        # wire does not carry.
        return "not_found"

    if status == 409:
        # Optimistic-concurrency clash: a stale base SHA / a head that moved
        # under a write. The base-SHA-guard idempotency class recovers by
        # re-reading the current SHA and retrying.
        return "conflict"

    if status == 405:
        # A protected-branch merge refusal: GitHub documents 405 Method Not
        # Allowed with the unmet requirement in the body (a required check
        # pending, an approval missing, the head moved under an
        # expectedHeadSha). It is a resource-STATE clash, not a malformed
        # request -- so it is conflict, not input. Placed before the generic
        # 4xx fallback, which would otherwise misread it as input.
        return "conflict"

    if status == 422:
        # GitHub's validation-failure status: a bad base branch, a malformed
        # ref, a duplicate name in a namespace. The request shape or its
        # arguments were rejected.
        return "input"

    if 500 <= status <= 599:
        # Transient server-side failures; safe to retry per the control
        # plane's backoff policy (which this module does not implement).
        return "temporary"

    # Any other 4xx that is not one of the specifically-handled codes is a
    # client-side input problem rather than a server or auth failure.
    if 400 <= status <= 499:
        return "input"

    # A non-error status reaching a failure classifier is itself ambiguous:
    # the caller handed a response the failure path should never have seen.
    return "ambiguous"


# Re-exported for a caller that wants the control plane's closed set without a
# second import; it IS the control plane's tuple, not a copy.
__all__ = ["GithubFailure", "classify_github_failure", "ERROR_CLASSES"]
