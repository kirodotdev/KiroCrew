"""``GET /api/chat/slots`` for a delegated (``via_app``) token is a bare list.

A delegated token passes the owner gate (it is the owner's token), so without a
projection it would read each chat's last message, its working directory and project, its
source links and its work-item fields. The handler answers it key, title, running and
last-activity time only; the person keeps the full row.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.handlers import source_providers
from kiro_crew.dashboard.source_providers import chip_refresh

SECRET_MESSAGE = "the last words of a private chat"
SECRET_PATH = "/home/owner/private-project"
ROW = {
    "key": "chat-1-1",
    "title": "Plan the launch",
    "running": True,
    "last_activity_ts": "2026-11-22T10:00:00Z",
    "last_message": SECRET_MESSAGE,
    "prompt_preview": SECRET_MESSAGE,
    "workspace": SECRET_PATH,
    "project": SECRET_PATH,
    "turn_project": SECRET_PATH,
    "source_links": [{"kind": "change", "url": "https://example.test/o/r/pull/1"}],
    "source_links_total": 1,
    "todo": [{"text": "ship"}],
    "options": ["yes", "no"],
    "pending_approval_info": {"tool": "run", "tool_input": SECRET_PATH},
    "model": "m",
    "agent": "default",
    "welcome_message": "hello from the agent",
    "ci": "failing",
    "state": "open",
}
#: The keyword arguments each ``serialize_slots`` call received.
CALLS: list[dict] = []


@pytest.fixture
def slots_app(monkeypatch: pytest.MonkeyPatch) -> web.Application:
    async def _noop() -> None:
        return None

    monkeypatch.setattr(source_providers, "ensure_gitlab_hosts_loaded", _noop)
    # A delegated token passes the owner gate as the owner.
    monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda _r: True)
    monkeypatch.setattr(source_providers, "schedule_visibility_refresh", lambda *_a, **_k: None)
    monkeypatch.setattr(chip_refresh, "schedule_tiered_check_refresh", lambda *_a, **_k: None)
    state = SimpleNamespace(
        _slots={},
        push_slots_update=lambda *_a, **_k: None,
        idle_source_link_urls=lambda: set(),
        slot_list_held_keys=lambda: [],
        slot_list_hold_channels=lambda: False,
        serialize_slots=lambda **k: (CALLS.append(k), [dict(ROW)])[1],
    )
    CALLS.clear()
    app = web.Application()
    app["state"] = state
    return app


def _request(app: web.Application, *, via_app: str):
    req = make_mocked_request("GET", "/api/chat/slots", app=app)
    req["app"] = ""
    req["is_dashboard_user"] = True
    if via_app:
        req["via_app"] = via_app
    return req


@pytest.mark.asyncio
async def test_a_delegated_token_gets_only_key_title_running_and_time(
    slots_app: web.Application,
) -> None:
    resp = await chat_handlers.api_chat_slots(_request(slots_app, via_app="some-app"))
    rows = json.loads(resp.text)
    assert rows == [
        {
            "key": "chat-1-1",
            "title": "Plan the launch",
            "running": True,
            "last_activity_ts": "2026-11-22T10:00:00Z",
        }
    ]
    assert SECRET_MESSAGE not in resp.text
    assert SECRET_PATH not in resp.text
    for field in ("pending_approval_info", "welcome_message", "source_links", "ci", "state"):
        assert field not in resp.text, field
    # A delegated token never asks for the owner-only provider check status.
    assert [call.get("include_check_status") for call in CALLS] == [False]


@pytest.mark.asyncio
async def test_the_person_keeps_the_full_row(slots_app: web.Application) -> None:
    resp = await chat_handlers.api_chat_slots(_request(slots_app, via_app=""))
    row = json.loads(resp.text)[0]
    assert row["last_message"] == SECRET_MESSAGE
    assert row["workspace"] == SECRET_PATH
    assert [call.get("include_check_status") for call in CALLS] == [True]
