"""``GET /api/chat/slots`` keeps a Crew Mode root's crew members, and a mate's
sessions unless its Bot shows them in the list, out of a person's list.

Route-level: the handler is called with the rows ``serialize_slots`` would stamp,
so the one server-side filter (``crew_mode.list_view`` in
``chat_api/slot_detail.py``) is what each test exercises. Returning the rows
unfiltered from ``list_view`` reddens the first test.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import crew_mode
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.handlers import source_providers

ROOT = {"key": "chat-1-1", crew_mode.ROOT_FIELD: True}
WORKER = {"key": "chat-2-2", crew_mode.WORKER_FIELD: "chat-1-1"}
PLAIN = {"key": "chat-3-3"}


@pytest.fixture
def slots_app(monkeypatch: pytest.MonkeyPatch) -> web.Application:
    async def _noop() -> None:
        return None

    monkeypatch.setattr(source_providers, "ensure_gitlab_hosts_loaded", _noop)
    monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda _r: False)
    state = SimpleNamespace(
        _slots={},
        slot_list_held_keys=lambda: [],
        slot_list_hold_channels=lambda: False,
        serialize_slots=lambda **_k: [dict(ROOT), dict(WORKER), dict(PLAIN)],
    )
    app = web.Application()
    app["state"] = state
    return app


async def _keys(req: web.Request) -> set[str]:
    resp = await chat_handlers.api_chat_slots(req)
    return {row["key"] for row in json.loads(resp.text)}


@pytest.mark.asyncio
async def test_a_persons_default_read_drops_crew_members(slots_app: web.Application) -> None:
    req = make_mocked_request("GET", "/api/chat/slots", app=slots_app)
    assert await _keys(req) == {"chat-1-1", "chat-3-3"}


@pytest.mark.asyncio
async def test_crew_all_keeps_the_crew_members(slots_app: web.Application) -> None:
    req = make_mocked_request("GET", "/api/chat/slots?crew=all", app=slots_app)
    assert await _keys(req) == {"chat-1-1", "chat-2-2", "chat-3-3"}


@pytest.mark.asyncio
async def test_an_agents_read_keeps_the_crew_members(slots_app: web.Application) -> None:
    req = make_mocked_request("GET", "/api/chat/slots", app=slots_app)
    req["internal_auth"] = True
    assert await _keys(req) == {"chat-1-1", "chat-2-2", "chat-3-3"}


MATE = {"key": "dm-ada", "mode": "member", "agent": "ada"}
MATE_CHILD = {"key": "chat-4-4", crew_mode.MATE_ROOT_FIELD: "dm-ada"}


def _mate_app(monkeypatch: pytest.MonkeyPatch, listed: object) -> web.Application:
    """The route over a mate thread, its session and a plain row, stamped for real.

    *listed* is Bot ``ada``'s ``show_sessions_in_list`` in the live config.
    """

    async def _noop() -> None:
        return None

    monkeypatch.setattr(source_providers, "ensure_gitlab_hosts_loaded", _noop)
    monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda _r: False)
    cfg = SimpleNamespace(agents={"ada": SimpleNamespace(show_sessions_in_list=listed)})
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: cfg)
    monkeypatch.setattr(crew_mode, "_known_mate_roots", set())
    monkeypatch.setattr(crew_mode, "_known_roots", set())

    def _serialize(**_k: object) -> list[dict[str, object]]:
        rows = [dict(MATE), {"key": "chat-4-4", "created_by": "dm-ada"}, dict(PLAIN)]
        crew_mode.stamp_rows(rows, parents={}, tree_slots=set())
        return rows

    state = SimpleNamespace(
        _slots={},
        slot_list_held_keys=lambda: [],
        slot_list_hold_channels=lambda: False,
        serialize_slots=_serialize,
    )
    app = web.Application()
    app["state"] = state
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("listed", [False, None, "yes"])
async def test_a_persons_read_drops_a_mates_sessions_and_keeps_the_mate_thread(
    monkeypatch: pytest.MonkeyPatch, listed: object
) -> None:
    app = _mate_app(monkeypatch, listed)
    req = make_mocked_request("GET", "/api/chat/slots", app=app)
    assert await _keys(req) == {"dm-ada", "chat-3-3"}


@pytest.mark.asyncio
async def test_a_bot_that_shows_its_sessions_keeps_them_in_a_persons_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _mate_app(monkeypatch, True)
    req = make_mocked_request("GET", "/api/chat/slots", app=app)
    assert await _keys(req) == {"dm-ada", "chat-4-4", "chat-3-3"}


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", [False, True])
async def test_crew_all_and_an_agents_read_keep_a_mates_sessions(
    monkeypatch: pytest.MonkeyPatch, agent: bool
) -> None:
    app = _mate_app(monkeypatch, False)
    path = "/api/chat/slots" if agent else "/api/chat/slots?crew=all"
    req = make_mocked_request("GET", path, app=app)
    if agent:
        req["internal_auth"] = True
    assert await _keys(req) == {"dm-ada", "chat-4-4", "chat-3-3"}


def test_a_session_whose_mate_thread_is_closed_stays_in_a_persons_list() -> None:
    rows = [{"key": "chat-4-4", crew_mode.MATE_ROOT_FIELD: "dm-gone"}, dict(PLAIN)]
    assert crew_mode.list_view(rows, "", agent=False) == rows
