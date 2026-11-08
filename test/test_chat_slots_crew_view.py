"""``GET /api/chat/slots`` keeps a Crew Mode root's crew members, and with the
mate sessions preview on a mate's sessions, out of a person's list.

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


MATE = {"key": "dm-ada", "mode": "member"}
MATE_CHILD = {"key": "chat-4-4", crew_mode.MATE_ROOT_FIELD: "dm-ada"}


def _mate_app(monkeypatch: pytest.MonkeyPatch, previews: dict[str, bool]) -> web.Application:
    """The route over a mate thread, its session and a plain row, with *previews* live."""

    async def _noop() -> None:
        return None

    monkeypatch.setattr(source_providers, "ensure_gitlab_hosts_loaded", _noop)
    monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda _r: False)
    cfg = SimpleNamespace(dashboard=SimpleNamespace(feature_previews=previews))
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: cfg)
    state = SimpleNamespace(
        _slots={}, serialize_slots=lambda **_k: [dict(MATE), dict(MATE_CHILD), dict(PLAIN)]
    )
    app = web.Application()
    app["state"] = state
    return app


@pytest.mark.asyncio
async def test_mate_sessions_stay_in_a_persons_read_with_the_preview_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _mate_app(monkeypatch, {})
    req = make_mocked_request("GET", "/api/chat/slots", app=app)
    assert await _keys(req) == {"dm-ada", "chat-4-4", "chat-3-3"}


@pytest.mark.asyncio
async def test_the_preview_drops_a_mates_sessions_and_keeps_the_mate_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _mate_app(monkeypatch, {crew_mode.MATE_SESSIONS_PREVIEW: True})
    req = make_mocked_request("GET", "/api/chat/slots", app=app)
    assert await _keys(req) == {"dm-ada", "chat-3-3"}


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", [False, True])
async def test_crew_all_and_an_agents_read_keep_a_mates_sessions_with_the_preview_on(
    monkeypatch: pytest.MonkeyPatch, agent: bool
) -> None:
    app = _mate_app(monkeypatch, {crew_mode.MATE_SESSIONS_PREVIEW: True})
    path = "/api/chat/slots" if agent else "/api/chat/slots?crew=all"
    req = make_mocked_request("GET", path, app=app)
    if agent:
        req["internal_auth"] = True
    assert await _keys(req) == {"dm-ada", "chat-4-4", "chat-3-3"}


def test_mate_sessions_hidden_reads_only_a_true_flag() -> None:
    def cfg(previews: object) -> SimpleNamespace:
        return SimpleNamespace(dashboard=SimpleNamespace(feature_previews=previews))

    assert crew_mode.mate_sessions_hidden(cfg({crew_mode.MATE_SESSIONS_PREVIEW: True}))
    assert not crew_mode.mate_sessions_hidden(cfg({crew_mode.MATE_SESSIONS_PREVIEW: "yes"}))
    assert not crew_mode.mate_sessions_hidden(cfg(None))
    assert not crew_mode.mate_sessions_hidden(SimpleNamespace())
