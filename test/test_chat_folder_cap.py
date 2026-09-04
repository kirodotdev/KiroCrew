"""The bounds on chat-folder creation: a global ceiling and a per-caller rate.

Folder creation was the one create path in the dashboard with no bound at all, so an
automated caller looping on it could grow durable on-disk state without limit. Two
guards now: ``MAX_CHAT_FOLDERS`` bounds how many can exist, tested where the count is
authoritative (under the folder lock), and a per-caller rate bounds how fast one
caller may make them. The rate applies to INTERNAL callers only -- this endpoint also
serves the browser's own control, and throttling a person organizing their chats
would be a regression with no security value.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard.chat_folders import MAX_CHAT_FOLDERS, api_chat_folder_create
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def _folders(count: int) -> list[dict[str, Any]]:
    return [
        {"id": f"fldr{i:04d}", "name": f"F{i}", "parent_id": None, "order": i} for i in range(count)
    ]


def _state(folders: list[dict[str, Any]], *, on_mutate: Any = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._folders = folders
    slot = _ChatSlot("chat-1-100")
    state._slots = {slot.key: slot}
    state.push_slots_update = MagicMock()
    state.conversation_log = None

    async def _mutate(fn: Any) -> Any:
        # The real store runs the callback while holding the lock and hands back
        # its second element. `on_mutate` stands in for a concurrent creator that
        # won the lock first, so the callback sees a list that grew after the
        # request was admitted.
        if on_mutate is not None:
            on_mutate(state._folders)
        _changed, value = fn(state._folders)
        return value

    state.mutate_folders = _mutate
    return state


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_app(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        return await handler(request)

    app.middlewares.append(_publish_app)
    app.router.add_post("/api/chat/folders", api_chat_folder_create)
    return app


async def _create(state: DashboardState, name: str = "New") -> tuple[int, dict[str, Any]]:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat/folders",
            json={"name": name},
            headers={"X-Session-Key": "dashboard:chat-1-100"},
        )
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_creation_is_refused_at_the_ceiling() -> None:
    """Mutation guard: remove the check and this returns 201 forever."""
    status, body = await _create(_state(_folders(MAX_CHAT_FOLDERS)))

    assert status == 429, "a cap breach is a 429, not a 500 and not a silent success"
    assert body["code"] == "folder_cap_reached"
    assert str(MAX_CHAT_FOLDERS) in body["error"], "the refusal should name the ceiling"


@pytest.mark.asyncio
async def test_the_last_folder_under_the_ceiling_is_still_allowed() -> None:
    """The boundary, so an off-by-one cannot cost the user their final folder."""
    folders = _folders(MAX_CHAT_FOLDERS - 1)
    status, _body = await _create(_state(folders))

    assert status == 201
    assert len(folders) == MAX_CHAT_FOLDERS


@pytest.mark.asyncio
async def test_an_internal_caller_is_rate_limited_at_the_endpoint() -> None:
    """The MCP path is throttled, which is what bounds an auto-approved loop.

    Mutation guard: drop the `allow_create` call and the burst all returns 201.
    """
    create_rate_limit.reset_for_tests()
    try:
        state = _state(_folders(0))
        headers = {
            "X-Session-Key": "dashboard:chat-1-100",
            "X-Internal-Secret": "s3cret",
            "X-Internal-Caller": "kirocrew-dashboard",
        }
        async with TestClient(TestServer(_make_app(state))) as client:
            allowed = 0
            for i in range(create_rate_limit.MAX_FOLDER_CREATES_PER_WINDOW + 3):
                resp = await client.post(
                    "/api/chat/folders", json={"name": f"F{i}"}, headers=headers
                )
                if resp.status == 201:
                    allowed += 1
                else:
                    body = await resp.json()
                    assert resp.status == 429
                    assert body["code"] == "create_rate_limited"
        assert allowed == create_rate_limit.MAX_FOLDER_CREATES_PER_WINDOW
    finally:
        create_rate_limit.reset_for_tests()


@pytest.mark.asyncio
async def test_the_browser_is_not_rate_limited() -> None:
    """The person's own "new folder" control posts to this same endpoint, and someone
    organizing their chats can legitimately create more than the agent budget in one
    sitting. Throttling them would be a regression with no security value.

    Mutation guard: apply the limiter unconditionally and this starts 429ing.
    """
    create_rate_limit.reset_for_tests()
    try:
        state = _state(_folders(0))
        async with TestClient(TestServer(_make_app(state))) as client:
            for i in range(create_rate_limit.MAX_FOLDER_CREATES_PER_WINDOW + 3):
                resp = await client.post(
                    "/api/chat/folders",
                    json={"name": f"F{i}"},
                    headers={"X-Session-Key": "dashboard:chat-1-100"},
                )
                assert resp.status == 201, "a browser create must never be throttled"
    finally:
        create_rate_limit.reset_for_tests()

    """`len(folders)` is only authoritative while the lock is held.

    A pre-lock test lets two concurrent creators each pass a cap that one of them
    has already filled -- the same pre-lock/post-lock gap the parent re-check and
    the `order` recount exist to close.

    Mutation guard: hoist the check above `mutate_folders` and this returns 201,
    landing folder 501.
    """

    def _fill(folders: list[dict[str, Any]]) -> None:
        folders.extend(_folders(MAX_CHAT_FOLDERS)[len(folders) :])

    # Admitted while the tree was empty; the lock winner filled it meanwhile.
    status, body = await _create(_state([], on_mutate=_fill))

    assert status == 429
    assert body["code"] == "folder_cap_reached"
