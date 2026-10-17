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
