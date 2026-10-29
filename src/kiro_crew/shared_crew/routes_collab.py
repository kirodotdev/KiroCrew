"""HTTP reads for collaboration state a reconnecting tab reconciles against.

``GET /api/shared-crew/presence?session_key=`` answers the same whole-value shape
the ``shared_crew.presence`` frame carries, and
``GET /api/shared-crew/decisions?session_key=`` the session's shared decisions,
and ``GET /api/shared-crew/send-state?session_key=`` the session's send revision
(the ``shared_crew.send_state`` frame body), which a send carries as
``base_revision``, and ``GET /api/shared-crew/typing-lease?session_key=`` who
holds the session's composer typing lease (the ``shared_crew.typing_lease``
frame body).
Every one is a read of in-memory state for dashboard users; the existing auth
middleware has already admitted the caller, and an app token is refused.
"""

from __future__ import annotations

from typing import Any, Callable

from aiohttp import web

from kiro_crew.shared_crew.codec import SCHEMA_VERSION
from kiro_crew.shared_crew.collab import presence_frame
from kiro_crew.shared_crew.decisions import decisions_for
from kiro_crew.shared_crew.presence import MAX_SESSION_KEY_LEN, presence_for
from kiro_crew.shared_crew.send_race import send_race_for
from kiro_crew.shared_crew.typing_lease import typing_lease_for


def _session_read(body: Callable[[Any, str], dict[str, Any]]) -> Callable[[web.Request], Any]:
    """A GET handler that refuses a non-dashboard caller, reads ``session_key``, then answers *body*."""

    async def handler(request: web.Request) -> web.Response:
        if not request.get("is_dashboard_user", False):
            return web.json_response(
                {"error": "dashboard users only", "code": "forbidden"}, status=403
            )
        session_key = request.query.get("session_key", "")
        if not session_key or len(session_key) > MAX_SESSION_KEY_LEN:
            return web.json_response(
                {"error": "session_key is required", "code": "invalid_session_key"}, status=400
            )
        return web.json_response(body(request.app["state"], session_key))

    return handler


api_shared_crew_presence = _session_read(
    lambda state, key: presence_frame(key, presence_for(state).entries(key))
)
api_shared_crew_decisions = _session_read(
    lambda state, key: {
        "schema_version": SCHEMA_VERSION,
        "items": [d.to_public_dict() for d in decisions_for(state).for_session(key)],
    }
)
api_shared_crew_send_state = _session_read(lambda state, key: send_race_for(state).state(key))
#: Who holds the session's typing lease now (the ``shared_crew.typing_lease`` body).
api_shared_crew_typing_lease = _session_read(lambda state, key: typing_lease_for(state).state(key))


def register(app: web.Application) -> None:
    """Mount the collaboration reads. F adds this to ``routes._REGISTRARS``."""
    app.router.add_get("/api/shared-crew/presence", api_shared_crew_presence)
    app.router.add_get("/api/shared-crew/decisions", api_shared_crew_decisions)
    app.router.add_get("/api/shared-crew/send-state", api_shared_crew_send_state)
    app.router.add_get("/api/shared-crew/typing-lease", api_shared_crew_typing_lease)
