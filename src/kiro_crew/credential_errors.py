"""Auth-error classification shared across the agent-SDK boundary.

``acp/client.py`` owns the auth-error family (``_RE_AUTH``, ``_RE_INVALID_BEARER``,
``_is_session_expired``), and every member of it is terminal — except one. This
module is the home for that exception, because it has consumers on BOTH sides of
the boundary ``scripts/check_agent_sdk_boundary.py`` enforces: the ACP client
itself, and ``llm_helpers``' string-fallback classifier, which is application code
and may not import the ACP layer. Homing the predicate here gives the two one
verdict without a new boundary edge and without a second regex spelling — the same
argument ``credential_patterns.py`` makes for the redaction spellings.

Stdlib-only (``re``), so neither consumer pays an import cost for it.
"""

from __future__ import annotations

import re

# The ONE auth-shaped rejection that is TRANSIENT. A credential minted moments
# ago is refused until IAM has propagated it: Bedrock answers "The security token
# included in the request is invalid", normally wrapped in
# UnrecognizedClientException and carrying a 403, and the IDENTICAL credential is
# accepted seconds later. Every other signal in that family is terminal, so
# without a carve-out a burst of concurrent cold starts (several Slack messages
# at once, each minting its own STS token) surfaces a raw 403 and tells the
# operator to sign in again — which fixes nothing, because the credential is
# already valid. Measured ~60% turn failure on a 5-message burst, to 0 once the
# existing 2s/4s/8s ladder is allowed to absorb it.
#
# Scoped to the "is invalid" WORDING, never to the 403 status, so the bare-401/403
# session-expiry classification is untouched. The "... is expired" sibling stays
# terminal because no retry revives an expired token, and the lookahead keeps a
# combined "invalid or expired" rejection terminal for the same reason. Fenced to
# one sentence and one line, like ``_RE_INVALID_BEARER`` in ``acp/client.py``, so
# it cannot span unrelated errors in a combined haystack.
#
# The wording is NOT unique to the race: AWS returns the same sentence — as
# UnrecognizedClientException or InvalidClientTokenId — for an access key that is
# permanently invalid (deleted, rotated away, or mistyped). A never-valid key
# therefore classifies transient here, and it is the retry budget, not the
# pattern, that bounds the mistake: ``llm_helpers._TRANSIENT_RETRIES`` = 3 on the
# 2/4/8s curve (~15s), after which the formatted message's closing sentence is
# the terminal "refresh your AWS credentials" exit. Deliberately no occurrence
# counting — that would put per-credential state into an otherwise stateless
# predicate for a cost the ladder already caps.
_RE_TOKEN_NOT_YET_VALID = re.compile(
    r"\bsecurity\s+token\b[^.\n]{0,80}?\bis\s+invalid\b(?!\s*(?:or|and|/)\s*expired)",
    re.IGNORECASE,
)


def is_credential_propagation_delay(haystack: str) -> bool:
    """True when an auth-shaped rejection is really an IAM-propagation race.

    Single source of truth for the one RETRYABLE member of ``acp/client.py``'s auth
    family (see ``_RE_TOKEN_NOT_YET_VALID`` above). Read ahead of that module's
    ``_RE_AUTH`` and session-expiry branches by ``_is_transient_raw_error`` and
    ``_format_acp_error`` — either would otherwise claim the frame and return a
    terminal verdict — by ``is_auth_failure_output``, so the runtime does not latch
    it into non-retryable ``AcpAuthRequired`` and skip the ladder entirely, and by
    ``llm_helpers._is_transient_acp_error`` for the string fallback. One predicate
    rather than four spellings, for the same anti-drift reason that module states: a
    second vocabulary is what creates the gaps.
    """
    return bool(_RE_TOKEN_NOT_YET_VALID.search(haystack))


# Credential rejection: a bearer token that is invalid or has been revoked.
# Moved here from ``acp/transport_errors._RE_INVALID_BEARER`` so both
# ``acp/transport_errors._is_session_expired`` and
# ``credential_errors.is_auth_error_message`` share one definition rather
# than keeping separate copies that can drift. ``transport_errors`` imports
# this back.
# Switching the active Kiro account invalidates the credential a long-lived
# kiro-cli child still holds, and the upstream rejection reports only that the
# bearer token is invalid: no status code and no expiry wording. Grouped with
# session expiry because the remedy is identical: sign in again.
_RE_INVALID_BEARER = re.compile(
    r"\b(?:bearer\s+token\b[^.\n]{0,80}?\binvalid|invalid\s+bearer\s+token)\b",
    re.IGNORECASE,
)

# Session expiry, by wording. Complements the status match for backends that
# describe the expiry in prose without a machine-readable code. Deliberately
# excludes Bedrock's named exceptions, which ``transport_errors._RE_AUTH`` owns.
# Moved here from ``acp/transport_errors``, which imports it back.
_RE_SESSION_EXPIRED = re.compile(
    r"\b(?:session\s+(?:has\s+)?expired|session\s+timed?\s*out"
    r"|login\s+(?:has\s+)?expired|authentication\s+(?:has\s+)?expired"
    r"|not\s+logged\s+in|not\s+authenticated|not\s+signed\s+in"
    r"|re-?authenticate|login\s+required|auth(?:entication)?\s+required)\b",
    re.IGNORECASE,
)

# Expiry-only vocabulary for the cron auto-pause carve-out. Deliberately
# narrower than ``acp/transport_errors._RE_AUTH``, which also covers
# ``AccessDeniedException`` and ``UnauthorizedException`` — permission denials
# that are NOT self-healing and must still count toward auto-pause. Only the
# AWS ``ExpiredToken`` exception family (whose name makes the expiry explicit),
# the Bedrock raw-error phrase for an expired STS token, the Kiro
# session-expiry message produced by ``_format_acp_error``, and the Kiro Crew
# signed-out messages from ``host_auth.signed_out_message`` are included,
# because those are the ones that recover on their own once the user
# re-authenticates.
#
# ``AcpAuthRequired`` sign-out messages ("kiro-cli is not logged in",
# "Not signed in to Kiro") are covered by the gateway handler using
# ``isinstance(exc, AcpAuthRequired)`` rather than text matching, so those
# broad phrases are not added here. Text-based matching is scoped to the
# AWS/Bedrock vocabulary that is specific enough to avoid false positives
# from third-party tool output.
_RE_CREDENTIAL_EXPIRED = re.compile(
    r"\b(?:ExpiredToken(?:Exception)?"
    r"|security\s+token\b[^.\n]{0,80}?\bis\s+expired\b"
    r"|session\s+has\s+expired)\b",
    re.IGNORECASE,
)


def is_auth_error_message(haystack: str) -> bool:
    """True when *haystack* is a self-healing credential/auth error message.

    Recognises the expiry vocabulary that appears in a cron job's ``last_error``
    when Kiro Crew's own session credential is expired, a bearer token has been
    revoked, or the kiro-cli process is not authenticated (``AcpAuthRequired``):
    the job fails for an environmental reason and recovers on its own once the
    user re-authenticates.

    Deliberately excludes ``AccessDeniedException``, ``UnauthorizedException``,
    and ``InvalidSignatureException`` — those name permanent permission denials
    that do NOT self-heal and must still count toward auto-pause.

    The IAM-propagation race (:func:`is_credential_propagation_delay`) is also
    excluded: that signal is retried by the ACP ladder before it reaches
    ``record_failure``; if retries are exhausted the credential may be
    permanently broken rather than merely racing, so it counts toward auto-pause.

    Returns False for an empty or falsy haystack.
    """
    if not haystack:
        return False
    # Exclude the transient IAM-propagation race. Its retries are bounded; once
    # exhausted the credential may be permanently invalid.
    if is_credential_propagation_delay(haystack):
        return False
    return bool(_RE_CREDENTIAL_EXPIRED.search(haystack) or _RE_INVALID_BEARER.search(haystack))


def is_sign_out_wording(haystack: str) -> bool:
    """True when *haystack* contains self-healing sign-out or expiry wording.

    Narrower than ``transport_errors._is_session_expired``: uses only prose patterns
    (``_RE_SESSION_EXPIRED``, ``_RE_INVALID_BEARER``, ``_RE_CREDENTIAL_EXPIRED``)
    and deliberately excludes the HTTP-status match (``_RE_AUTH_STATUS``) that
    ``_is_session_expired`` includes.  An HTTP 403 can accompany a permanent
    ``AccessDeniedException`` that re-auth cannot fix; the prose patterns here
    specifically describe self-healing failures (credential expiry, sign-out,
    bearer rejection).
    """
    return (
        bool(_RE_SESSION_EXPIRED.search(haystack))
        or bool(_RE_INVALID_BEARER.search(haystack))
        or bool(_RE_CREDENTIAL_EXPIRED.search(haystack))
    )
