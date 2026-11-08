"""Which session an internal request comes from, proven rather than claimed.

A runtime's MCP server reaches the gateway on the internal transport and names
its calling session in ``X-Session-Key``. The internal secret proves the call
came from a runtime this gateway started; it does not prove which session, so
fair admission reads the session only once :func:`attest_internal_session`
proved it (``admission_gate.internal_initiator``).
"""

from __future__ import annotations

import asyncio
from typing import Any

#: Header an internal caller (a runtime's MCP server) names its own session with.
CALLER_SESSION_HEADER = "X-Session-Key"
#: Header carrying the per-session token the gateway signed for that session.
CALLER_TOKEN_HEADER = "X-Session-Token"
#: Request key holding the calling session :func:`attest_internal_session` proved.
ATTESTED_SESSION_KEY = "shared_crew_attested_session"
_MAX_SESSION_KEY = 512


def _declared_session(request: Any) -> str:
    headers = getattr(request, "headers", None)
    raw = headers.get(CALLER_SESSION_HEADER, "") if headers is not None else ""
    session = raw.strip() if isinstance(raw, str) else ""
    return session if 0 < len(session) <= _MAX_SESSION_KEY else ""


def _attested(request: Any, declared: str) -> bool:
    """The transport's one answer for a declared key. Blocking: may read a file."""
    from kiro_crew.member_memory_auth import session_key_is_attested

    return session_key_is_attested(request, declared)


def _verify_token(token: str) -> str:
    """The session the gateway signed *token* for, or ``""``. Blocking: reads a file."""
    from kiro_crew.session_token_sig import verify_session_token

    return verify_session_token(token)


async def attest_internal_session(request: Any) -> str:
    """Prove which session an internal request comes from; ``""`` when nothing proves it.

    The header alone is the caller's word. A declared key counts only when
    ``member_memory_auth.session_key_is_attested`` (the transport's one check)
    attests it; with no declared key, a per-session token the gateway signed
    (``session_token_sig``) names the session. The answer is kept on the
    request for :func:`internal_initiator`.
    """
    try:
        cached = request.get(ATTESTED_SESSION_KEY)
    except Exception:  # noqa: BLE001 -- a request that cannot hold keys proves nothing
        return ""
    if isinstance(cached, str):
        return cached
    declared = _declared_session(request)
    proved = ""
    headers = getattr(request, "headers", None)
    if declared:
        if headers is not None and await asyncio.to_thread(_attested, request, declared):
            proved = declared
    else:
        token = headers.get(CALLER_TOKEN_HEADER, "") if headers is not None else ""
        if isinstance(token, str) and token:
            # No key is declared, so there is nothing for the shared check to
            # match: the session the gateway signed the token for is the answer.
            proved = await asyncio.to_thread(_verify_token, token)
    try:
        request[ATTESTED_SESSION_KEY] = proved
    except Exception:  # noqa: BLE001 -- not cached; the answer still stands
        pass
    return proved


__all__ = [
    "ATTESTED_SESSION_KEY",
    "CALLER_SESSION_HEADER",
    "CALLER_TOKEN_HEADER",
    "attest_internal_session",
]
