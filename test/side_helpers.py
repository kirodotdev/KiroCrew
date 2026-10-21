"""Support shared by the /side handler tests in ``test_side.py`` and its confinement sibling.

It holds the side question and answer constants, a prerequisite service that always reports
ready, the ``/side`` app factory ``_make_side_app`` that installs it unless handed another,
the broadcast recorder ``_capture_broadcasts`` and the autouse ``_published_readonly_spec``
fixture (declared ``@pytest.fixture(name=...)`` on ``_published_readonly_spec_fixture``),
which stands in for the derived-spec publisher on every side turn of a file that imports
it. ``test_side.py`` and ``test_side_turn_confinement.py`` import the constants, the app
factory, the recorder and the fixture function from here.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from chat_test_helpers import stub_readonly_spec_publisher

from kiro_crew.dashboard.handlers.side import (
    api_side_close,
    api_side_open,
    api_side_state,
    api_side_stop,
    api_side_tools,
    api_side_turn,
)
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

_SIDE_QUESTION = "what is the difference between TCP and UDP?"
_SIDE_ANSWER = "TCP is connection-oriented and UDP is not."


class _ReadyKiroPrerequisiteService(KiroPrerequisiteService):
    async def session_ready(self) -> bool:
        return True


_READY_KIRO_PREREQUISITE = object.__new__(_ReadyKiroPrerequisiteService)


def _make_side_app(
    state,
    prerequisite_service: KiroPrerequisiteService | None = None,
) -> web.Application:
    app = web.Application()
    app["state"] = state
    app["kiro_prerequisite_service"] = (
        prerequisite_service if prerequisite_service is not None else _READY_KIRO_PREREQUISITE
    )
    app.router.add_post("/api/chat/slots/{slot}/side/open", api_side_open)
    app.router.add_post("/api/chat/slots/{slot}/side/turn", api_side_turn)
    app.router.add_post("/api/chat/slots/{slot}/side/stop", api_side_stop)
    app.router.add_post("/api/chat/slots/{slot}/side/close", api_side_close)
    app.router.add_get("/api/chat/side/tools", api_side_tools)
    app.router.add_get("/api/chat/slots/{slot}/side", api_side_state)
    return app


def _capture_broadcasts(state) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []

    def _record(msg_type, data):
        events.append((msg_type, data))

    # Both channels: side frames are owner-only while main chat events go to every client,
    # and these tests discriminate by event type rather than by audience.
    state.broadcast_ws = _record
    state.broadcast_ws_owners = _record
    return events


@pytest.fixture(name="_published_readonly_spec", autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture moved verbatim; no importing module calls monkeypatch.undo()
def _published_readonly_spec_fixture(monkeypatch):
    """Stand in for the derived-spec publisher on every side turn in this file;
    see ``chat_test_helpers.stub_readonly_spec_publisher``. Returns the recorded
    ``(base_name, project_dir)`` calls. The tests here pin what the turn does
    with the name (binds the session to it) and with a refusal (never runs the
    base)."""
    return stub_readonly_spec_publisher(monkeypatch)
