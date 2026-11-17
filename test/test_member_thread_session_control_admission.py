"""What a session control mounted in a Bot thread is admitted for.

A session control runs in the dashboard document and calls the gateway through the
scoped SDK client (``website/src/app-sdk/scopedApi.ts``): same-origin ``fetch`` with
the person's cookie and a host-set ``X-Session-Key``, no app token. Such a request
carries no ``app`` claim, so the app admission chain never runs for it. Only an app
token (the app's own backend) carries the claim, and that path reaches a live Bot
thread with ``sessionApproval`` + ``memberThreads`` (pinned in
``test_member_thread_grant.py``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_mode_security_helpers import (  # noqa: F401 -- fixtures are requested by name
    _hermetic_home,
    _isolate_safety_override,
    state_fixture,
)
from members_dm_thread_helpers import CREW, _patched_config

from kiro_crew.dashboard.chat_handlers import api_chat
from kiro_crew.dashboard.slot_ownership import SlotRoutePolicy, _route_decision
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.members import DM_SLOT_MODE, member_slot_key, write_dm_binding

THREAD = member_slot_key(CREW)
_GRANT = "kiro_crew.apps.permissions.app_can_manage_session_approvals"


@web.middleware
async def _person_request(request: web.Request, handler):
    # What token_auth publishes for a dashboard cookie: no app claim.
    request["app"] = ""
    request["user"] = ""
    return await handler(request)


def _member_thread(state):
    write_dm_binding(THREAD[len("member-") :], member=CREW, slot_key=THREAD)
    return state.get_or_create_slot(THREAD, agent=CREW, mode=DM_SLOT_MODE, origin=SlotOrigin.USER)


@pytest.mark.asyncio
async def test_a_control_send_without_an_app_claim_reaches_the_bot_thread(state) -> None:
    """The control's POST /api/chat from the dashboard document: no app claim, so no
    grant read and no member-key reservation; the turn runs on the thread's own slot."""
    slot = _member_thread(state)
    app = web.Application(middlewares=[_person_request])
    app["state"] = state
    app.router.add_post("/api/chat", api_chat)
    run_chat = AsyncMock()
    grant = MagicMock(return_value=False)
    with (
        patch(_GRANT, new=grant),
        patch("kiro_crew.dashboard.chat_handlers._run_chat", new=run_chat),
        patch("kiro_crew.dashboard.chat_handlers._maybe_auto_title", new=AsyncMock()),
        _patched_config([CREW]),
    ):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={"slot": THREAD, "message": "hi"},
                headers={"X-Session-Key": f"dashboard:{THREAD}"},
            )
            body = await resp.json()
    assert resp.status == 200, body
    run_chat.assert_called_once()
    assert state._slots[THREAD] is slot
    grant.assert_not_called()


def test_slot_detail_is_owner_only_for_every_app_caller() -> None:
    """GET /api/chat/slots/{slot} is the OWNER default for an app token on ANY user
    slot, ordinary chat or Bot thread alike: not a Bot-thread-specific refusal."""
    assert _route_decision("GET", "/api/chat/slots/{slot}") == ("slot", SlotRoutePolicy.OWNER)
