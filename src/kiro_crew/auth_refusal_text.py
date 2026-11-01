"""What the gateway's auth layer means by the codes it sends with a bare ``Forbidden``.

``token_auth`` refuses an internal-API request with ``{"error": "Forbidden",
"code": <code>}``. The ``error`` reads as a permission decision about whatever the
request asked for, which sends the reader after the wrong bug: most of these codes
name an instance or credential mix-up, not a permission. Every client that shows a
gateway refusal to a person or a model looks the code up here, so the MCP tools and
the CLI say the same thing for the same refusal.

Keyed on the CODE, never the wording: a genuine permission denial carries the same
``Forbidden`` with no code, and must not be given any of these explanations.

A leaf that imports nothing, so the cheapest CLI path can use it.
"""

from __future__ import annotations

#: Each auth-layer refusal code, with the sentence that explains it.
AUTH_REFUSAL_TEXT: dict[str, str] = {
    "internal_auth_mismatch": (
        "this client authenticated against the wrong Kiro Crew instance. "
        "The credential it read does not match the gateway now serving that "
        "port, usually because a second gateway started on this machine and "
        "replaced the shared credential file. Restart the gateway (or target "
        "the instance you meant) and retry; the gateway's security event log "
        "records both credential fingerprints for the mismatch."
    ),
    "internal_secret_missing": (
        "the gateway received no internal credential from this client. A "
        "sandbox or harness mask that withholds it, or a desynchronized "
        "credential listener, are possible causes. This is not a "
        "second-gateway mix-up. The gateway's security event log marks "
        "these denials as received=absent."
    ),
    "caller_record_missing": (
        "this session's cron or subagent record no longer exists; start a new "
        "session before retrying the tool."
    ),
    "unix_peer_unverified": (
        "the gateway could not verify this Unix-socket caller as the same local "
        "user. Restart the gateway and start a new session; if the error persists, "
        "make sure the client and gateway run as the same OS user."
    ),
    "peer_session_mismatch": (
        "this Unix-socket caller is bound to a different session than the "
        "X-Session-Key it declared. Restart the affected session, or start a new "
        "one, before retrying the tool."
    ),
}


def auth_refusal_text(code: object) -> str | None:
    """The explanation for an auth-layer refusal *code*, or ``None`` for any other."""
    if not isinstance(code, str):
        return None
    return AUTH_REFUSAL_TEXT.get(code)
