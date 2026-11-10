"""Bounded re-attempt of an MCP server that failed to start for want of credentials.

A stdio MCP server whose signer resolves credentials lazily (``mcp-proxy-for-aws``
is the reported case) fails its first ``initialize`` when the session starts
before any credential exists, then works once a credential is vended later in
the session. The engine marks such a server ``failed`` and never initializes it
again on its own, so its tools stay missing for the whole session.

On a host that takes ``_kiro/mcp/resetServer`` (KAS), Crew asks the engine to
connect the server again at a turn start, at most :data:`MAX_ATTEMPTS` times per
server per session. :func:`is_recoverable_auth_failure` decides which failures
qualify: an error that reads as a missing or rejected credential, the one
failure a later credential can cure. Any other failure (a bad command, a crash,
a protocol error) is left alone: re-running it costs a process spawn and cannot
succeed. A kiro-cli session is not re-attempted: that engine exposes no client
request that re-initializes one server, so the session's MCP report names the
server for a restart instead.

A managed server that only ran out of time starting is the other failure a second
attempt can cure: :func:`is_start_timeout_failure` reads it, and the KAS readiness
barrier (``session_handle_mcp.wait_mcp_ready``) re-attempts such a server once,
while the session start is still waiting for it.
"""

from __future__ import annotations

import re

from kiro_crew.acp.transport_errors import is_auth_failure_output
from kiro_crew.mcp_gateway.backend_handshake import INITIALIZE_DEADLINE_REASON

#: Re-attempts per server per session. Each is one connect attempt of a server
#: that is already failed, so a server that never recovers costs at most this
#: many spawns over the session's life.
MAX_ATTEMPTS = 3

# Wording of a credential that is absent, the case a later vend cures. Word-
# bounded so that an error naming an "author" or a port "4013" is not read as
# one. The word "credential" alone does not qualify: a start failure that merely
# mentions one ("loaded credentials from profile default", then a crash) is not
# cured by a later credential, so it counts only next to wording that says the
# credential is absent or bad. A refused or expired login is not spelled here:
# it is the shared auth vocabulary, ``transport_errors.is_auth_failure_output``.
_CREDENTIAL_ABSENT = re.compile(
    r"\b(?:"
    r"(?:no|missing|invalid|expired|bad|partial)\s+(?:[\w-]+\s+){0,2}credentials?"
    r"|(?:unable|failed|could\s+not|cannot|can't)\s+(?:to\s+)?"
    r"(?:locate|load|find|resolve|obtain|get|refresh|retrieve)\s+"
    r"(?:[\w-]+\s+){0,2}credentials?"
    r"|credentials?\b[^\n]{0,40}?\b(?:not\s+(?:found|available|provided|set)"
    r"|missing|expired|invalid|rejected|unavailable|required)"
    r"|(?:security|session)\s+token\b[^.\n]{0,60}?\bexpired"
    r"|nocredentials\w*"
    r")\b",
    re.IGNORECASE,
)


def is_recoverable_auth_failure(error_message: object) -> bool:
    """Whether a failed server's error reads as a credential it lacked."""
    if not isinstance(error_message, str) or not error_message:
        return False
    if _CREDENTIAL_ABSENT.search(error_message) is not None:
        return True
    return is_auth_failure_output(error_message)


# A host's own connect timeout ("connection timed out after 60000ms" from KAS).
# Word-bounded so a config error that merely names a "timeout" does not qualify.
_TIMED_OUT = re.compile(r"\btimed\s+out\b", re.IGNORECASE)


def is_start_timeout_failure(error_message: object) -> bool:
    """Whether a failed server's error reads as a start that ran out of time.

    True for the gateway's first-``initialize`` deadline
    (:data:`~kiro_crew.mcp_gateway.backend_handshake.INITIALIZE_DEADLINE_REASON`)
    and for a host connect timeout. A bad command, a crash or a protocol error
    is not one: a second spawn of it fails the same way.
    """
    if not isinstance(error_message, str) or not error_message:
        return False
    if INITIALIZE_DEADLINE_REASON in error_message:
        return True
    return _TIMED_OUT.search(error_message) is not None
