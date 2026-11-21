"""``POST /api/lessons`` re-vets the calling app's ``capabilities.memory_writes`` grant.

The ``learn_add`` tool vets the grant in ``kirocrew-core``, a process that knows no
app, so a per-app profile that forbids durable memory writes only bites when the
gateway asks again with the app it attested. Each case drives the real route
through a real governance ceiling and a real app-bound profile, so a refusal comes
from the resolver, not from a stubbed decision.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from governance_chokepoints_helpers import _isolate  # noqa: F401
from governance_chokepoints_helpers import _install

from kiro_crew.dashboard.handlers import cron
from kiro_crew.history import ConversationLog
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.vector_memory import LessonWriteOutcome, LessonWriteResult

APP = "file-explorer"


@web.middleware
async def _stamp_app(request: web.Request, handler: Any) -> web.StreamResponse:
    """Stand in for ``token_auth_middleware``: publish the app claim it attests."""
    claimed = request.headers.get("X-Test-App", "")
    if claimed:
        request["app"] = claimed
    return await handler(request)


@pytest.fixture(autouse=True)
def _app_forbids_memory_writes(_isolate: None) -> None:  # noqa: F811
    """The surface permits memory writes; a profile bound to APP forbids them.

    Requests ``_isolate`` by name so the profiles directory exists before it is written.
    """
    _install(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "capabilities": {"memory_writes": {"enabled": True}},
        }
    )
    (gp._PROFILES_DIR / "no-memory.json").write_text(
        json.dumps(
            {
                "name": "no-memory",
                "bind": {"type": "app", "id": APP},
                "capabilities": {"memory_writes": {"enabled": False}},
            }
        ),
        encoding="utf-8",
    )
    gp.reset_store()


def _state() -> MagicMock:
    state = MagicMock()
    # An empty slot registry, so the app comes from the stamped claim alone and a
    # MagicMock slot cannot be read as some other app owning the caller.
    state._slots = {}
    state.conversation_log = ConversationLog()
    state._background_tasks = set()
    return state


async def _post(headers: dict[str, str]) -> tuple[int, dict, MagicMock]:
    """POST one lesson through the real route; return status, body and the store."""
    state = _state()
    app = web.Application(middlewares=[_stamp_app])
    app.router.add_post("/api/lessons", cron.api_lessons_create)
    app["state"] = state
    store = MagicMock()
    store.embed_lesson.return_value = [0.1] * 4
    store.find_contradiction_candidates.return_value = []
    store.write_lesson.return_value = LessonWriteResult(LessonWriteOutcome.INSERTED, "")
    with (
        patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=store)),
        patch.object(cron, "_is_restricted_session", return_value=False),
        patch.object(cron, "_sel"),
        patch.object(cron, "_resolve_and_supersede", new=AsyncMock()),
    ):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/lessons",
                json={"rule": "prefer ruff over flake8", "category": "tool"},
                headers={"X-Session-Key": "dashboard:ui", **headers},
            )
            status, body = resp.status, await resp.json()
    return status, body, store


@pytest.mark.asyncio
async def test_an_app_whose_profile_forbids_memory_writes_saves_nothing() -> None:
    status, body, store = await _post({"X-Test-App": APP})
    assert status == 403
    assert body["code"] == "memory_writes_denied"
    assert body["error"].startswith("Lesson was NOT saved:")
    store.write_lesson.assert_not_called()


@pytest.mark.asyncio
async def test_an_app_the_profile_does_not_bind_still_saves() -> None:
    status, body, store = await _post({"X-Test-App": "another-app"})
    assert status == 200, body
    store.write_lesson.assert_called_once()


@pytest.mark.asyncio
async def test_the_persons_lesson_is_not_re_vetted() -> None:
    """No app claim is the person, whose surface profile the tool already vets."""
    with patch.object(gp, "vet_and_audit", wraps=gp.vet_and_audit) as vetted:
        status, body, store = await _post({})
    assert status == 200, body
    store.write_lesson.assert_called_once()
    vetted.assert_not_called()


@pytest.mark.asyncio
async def test_the_vet_is_audited_under_learn_add() -> None:
    with patch.object(gp, "vet_and_audit", wraps=gp.vet_and_audit) as vetted:
        status, _body, _store = await _post({"X-Test-App": APP})
    assert status == 403
    vetted.assert_called_once()
    args, kwargs = vetted.call_args
    assert args[0] == "capabilities.memory_writes"
    assert kwargs["tool_name"] == "learn_add"
    assert kwargs["app"] == APP


@pytest.mark.asyncio
async def test_an_evaluation_error_keeps_the_tools_degrade_posture() -> None:
    """The tool's own vet does not refuse on an evaluation error; neither does this."""

    def _unreadable(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("profile store unreadable")

    with patch.object(gp, "resolve_active_scope", side_effect=_unreadable):
        status, body, store = await _post({"X-Test-App": APP})
    assert status == 200, body
    store.write_lesson.assert_called_once()
