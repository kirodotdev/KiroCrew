"""The token a Slack tool-approval button carries, and how it is read back.

Split out of ``slack/renderer.py``, which re-exports every name here, so a
caller importing them from the renderer keeps working.
"""

from __future__ import annotations


def _approval_registry_key(session_key: str, request_id: str | int) -> str:
    """Namespace a kiro-cli request id by session for the approval registry.

    kiro-cli's JSON-RPC request id counter restarts at ``1`` for every session
    (``acp/client.py``), so two concurrent Slack threads both produce
    ``request_id == 1``. Keying the process-global registry (and the button
    ``action_id``/``value``) by ``session_key:request_id`` keeps each thread's
    approval isolated — a click resolves ONLY its own turn's pending tool.
    Falls back to the bare id when no session is supplied (e.g. unit tests).
    """
    rid = str(request_id)
    return f"{session_key}:{rid}" if session_key else rid


#: Separates the registry key from the per-prompt nonce inside a button's token.
#: ``|`` because a session key may contain ``:`` and the nonce is
#: ``token_urlsafe``, whose alphabet is ``[A-Za-z0-9_-]`` -- so splitting on this
#: from the right recovers both halves whatever either contains.
_APPROVAL_TOKEN_SEP = "|"


def build_approval_token(session_key: str, request_id: str | int, nonce: str) -> str:
    """The value a button carries: the registry key plus this prompt's nonce.

    The key alone identifies WHICH pending request a click is for; the nonce is
    what says the click came from the buttons currently live for it. Both travel
    together because the interaction handler has nothing else to go on.
    """
    key = _approval_registry_key(session_key, request_id)
    return f"{key}{_APPROVAL_TOKEN_SEP}{nonce}" if nonce else key


def split_approval_token(token: str) -> tuple[str, str]:
    """Split a button's token back into ``(registry_key, nonce)``.

    A token with no separator yields an EMPTY nonce rather than guessing, which
    the decider then refuses -- so a button minted before this prompt carried a
    nonce cannot resolve anything.
    """
    key, sep, nonce = str(token).rpartition(_APPROVAL_TOKEN_SEP)
    if not sep:
        return str(token), ""
    return key, nonce
