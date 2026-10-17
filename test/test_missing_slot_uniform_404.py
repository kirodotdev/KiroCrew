"""An unknown slot gets the shared ``slot_not_found()`` 404 from these handlers.

``slot_ownership.slot_not_found()`` is the 404 the app-isolation refusals answer
with. The handlers below answer a slot that does not exist through the same
helper, so their response is pinned to the helper's rather than to a literal: a
handler whose 404 differs from the helper's, or that calls a name its module does
not bind, fails here.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Awaitable, Callable

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard import chat_folders, chat_slack
from kiro_crew.dashboard.handlers import side
from kiro_crew.dashboard.slot_ownership import slot_not_found

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _request(method: str, path: str) -> web.Request:
    app = web.Application()
    app["state"] = SimpleNamespace(_slots={}, get_slot=lambda _name: None)
    return make_mocked_request(method, path, match_info={"slot": "nope"}, app=app)


def _shape(resp: Any) -> tuple[int, bytes, str]:
    return resp.status, resp.body, resp.content_type


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "method", "path"),
    [
        (chat_folders.api_chat_slot_muted, "PATCH", "/api/chat/slots/nope/muted"),
        (chat_folders.api_chat_slot_mutes_opened, "PATCH", "/api/chat/slots/nope/mutes-opened"),
        (chat_slack.api_chat_slot_slack_pause, "POST", "/api/chat/slots/nope/slack-pause"),
        (side.api_side_stop, "POST", "/api/chat/slots/nope/side/stop"),
    ],
    ids=["muted", "mutes-opened", "slack-pause", "side-stop"],
)
async def test_an_unknown_slot_gets_the_shared_not_found(
    handler: Handler, method: str, path: str
) -> None:
    resp = await handler(_request(method, path))
    assert _shape(resp) == _shape(slot_not_found())
