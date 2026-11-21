"""Who wrote a mid-turn steer, and how kiro's ``_session/steer`` is told.

KAS frames a ``_session/steer`` text as "[LIVE STEERING - New message from user]"
unless the request names another ``origin``. A steer the session's person or a
peer wrote is ``STEER_ORIGIN_USER``; Crew's own automation, such as a work-ledger
wake, is ``STEER_ORIGIN_AUTOMATION``, which KAS frames as from the host
application. A KAS without the field ignores it, and every other steer transport
ignores the origin.
"""

from __future__ import annotations

STEER_ORIGIN_USER = "user"
STEER_ORIGIN_AUTOMATION = "automation"


def steer_params(session_id: str, wrapped: str, origin: str) -> dict[str, str]:
    """The params of a kiro ``_session/steer`` for *wrapped* text written by *origin*.

    A user's steer keeps the params it always had; any other origin is named.
    """
    params = {"sessionId": session_id, "message": wrapped}
    if origin != STEER_ORIGIN_USER:
        params["origin"] = origin
    return params
