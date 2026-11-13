"""Loop creation from the goal form is fenced with the draft version the form showed.

Device A loads goal A and leaves the form open; device B saves goal B for the same
slot. A Start from A names the version A showed, so the create route answers the
draft PUT's own 409 instead of arming goal A. Identical content is never a
conflict, and a create that names no version is not checked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.autonudge import NudgeLoop
from kiro_crew.dashboard.handlers import autonudge as handlers
from kiro_crew.goal_drafts import GoalDraftSnapshot, GoalDraftStore

_SLOT = "chat-1-111"


def _request(body: dict[str, Any]) -> web.Request:
    app = web.Application()
    state = MagicMock()
    state.owner_id = ""
    app["state"] = state
    request = make_mocked_request("POST", "/api/autonudge", app=app)
    request["user"] = "local-app"
    request["app"] = ""
    request.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return request


def _body(response: web.StreamResponse) -> dict[str, Any]:
    assert isinstance(response, web.Response)
    assert isinstance(response.body, bytes)
    return json.loads(response.body.decode("utf-8"))


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GoalDraftStore:
    goal_store = GoalDraftStore(tmp_path)
    monkeypatch.setattr(handlers, "get_goal_draft_store", lambda: goal_store)
    monkeypatch.setattr(handlers, "_autonudge_get", lambda: MagicMock())
    monkeypatch.setattr(handlers, "sel", lambda: MagicMock())
    return goal_store


@pytest.fixture
def authorize(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    loop = NudgeLoop(id="lp-new", slot_key=_SLOT, message="goal A", idle_secs=60)
    mock = AsyncMock(return_value=(loop, None, 200))
    monkeypatch.setattr(handlers, "authorize_and_add_nudge", mock)
    return mock


def _goal_a_then_goal_b(store: GoalDraftStore) -> tuple[GoalDraftSnapshot, GoalDraftSnapshot]:
    shown = store.put(_SLOT, message="goal A", idle_secs=60, max_cycles=0, base_updated_at=0)
    newer = store.put(
        _SLOT,
        message="goal B",
        idle_secs=60,
        max_cycles=0,
        base_updated_at=shown.updated_at,
    )
    assert newer.updated_at > shown.updated_at
    return shown, newer


def _start(version: int | None, message: str = "goal A") -> dict[str, Any]:
    body: dict[str, Any] = {"slot_key": _SLOT, "message": message, "idle_secs": 60, "max_cycles": 0}
    if version is not None:
        body["draft_updated_at"] = version
    return body


@pytest.mark.asyncio
async def test_start_over_a_newer_different_draft_answers_the_draft_conflict(
    store: GoalDraftStore, authorize: AsyncMock
) -> None:
    shown, newer = _goal_a_then_goal_b(store)

    response = await handlers.api_autonudge_start(_request(_start(shown.updated_at)))

    assert response.status == 409
    assert _body(response) == {
        "error": "goal draft changed since it was loaded",
        "code": "goal_draft_conflict",
        "current": newer.to_public_dict(),
    }
    authorize.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_over_a_newer_identical_draft_arms_the_loop(
    store: GoalDraftStore, authorize: AsyncMock
) -> None:
    shown, _newer = _goal_a_then_goal_b(store)

    response = await handlers.api_autonudge_start(_request(_start(shown.updated_at, "goal B")))

    assert response.status == 200
    assert authorize.await_args.kwargs["message"] == "goal B"


@pytest.mark.asyncio
async def test_start_on_the_current_version_arms_the_loop(
    store: GoalDraftStore, authorize: AsyncMock
) -> None:
    _shown, newer = _goal_a_then_goal_b(store)

    response = await handlers.api_autonudge_start(_request(_start(newer.updated_at, "goal C")))

    assert response.status == 200
    authorize.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_without_a_version_is_not_checked(
    store: GoalDraftStore, authorize: AsyncMock
) -> None:
    _goal_a_then_goal_b(store)

    response = await handlers.api_autonudge_start(_request(_start(None)))

    assert response.status == 200
    assert authorize.await_args.kwargs["message"] == "goal A"


@pytest.mark.asyncio
async def test_start_refuses_a_non_integer_version(
    store: GoalDraftStore, authorize: AsyncMock
) -> None:
    response = await handlers.api_autonudge_start(
        _request({**_start(None), "draft_updated_at": "1"})
    )

    assert response.status == 400
    assert _body(response)["code"] == "invalid_goal_draft"
    authorize.assert_not_awaited()
