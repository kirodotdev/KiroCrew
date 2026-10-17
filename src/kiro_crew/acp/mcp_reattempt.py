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
"""

from __future__ import annotations

import re

from kiro_crew.acp.transport_errors import is_auth_failure_output

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
