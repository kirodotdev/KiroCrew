"""Who wrote a mid-turn steer, and how kiro's ``_session/steer`` is told.

KAS frames a ``_session/steer`` text as "[LIVE STEERING - New message from user]"
unless the request names another ``origin``. A steer the session's person or a
peer wrote is ``STEER_ORIGIN_USER``; Crew's own automation, such as a work-ledger
wake, is ``STEER_ORIGIN_AUTOMATION``, which KAS frames as from the host
application. A KAS without the field ignores it, and every other steer transport
ignores the origin.
"""

from __future__ import annotations

from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_KAS,
    escape_user_message_markup,
    wrap_user_message,
)

STEER_ORIGIN_USER = "user"
STEER_ORIGIN_AUTOMATION = "automation"


def steer_params(session_id: str, text: str, origin: str, backend: str) -> dict[str, str]:
    """The params of a ``_session/steer`` for *text* written by *origin* on *backend*.

    A user's steer keeps the params it always had: its text ``<user_message>``-wrapped.
    Any other origin is named. On KAS its text is only escaped, because KAS puts it
    inside a ``<host_message>`` element, where a user tag would claim the host's
    words for the user, and echoes each steer on its own, so a bare echo still
    settles by equality. kiro-cli's own engine ignores the origin and echoes every
    steer it injects in one concatenation, which settlement splits on the wrapper,
    so there the text stays wrapped.
    """
    params = {"sessionId": session_id, "message": wrap_user_message(text)}
    if origin == STEER_ORIGIN_USER:
        return params
    if backend == ACP_BACKEND_KAS:
        params["message"] = escape_user_message_markup(text)
    params["origin"] = origin
    return params
