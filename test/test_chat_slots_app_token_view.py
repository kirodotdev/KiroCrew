"""``GET /api/chat/slots`` shows an app token only the slots its socket shows it.

A ``permissions.api`` entry is a prefix, so an app granted ``/api/chat`` (to send
and to drive its own chat slot) also reaches the slot list. The handler filters
that list for an app-token caller with the WebSocket connect snapshot's filter
(``ws_event_scope.filter_slots_for_app``) and the app's declared ``slots:*``
scopes; the person and an internal-secret caller keep the full list.

Route-level: the handler is called over rows ``serialize_slots`` would stamp and
the real slot-visibility rule, with only the manifest read replaced. Dropping the
filter from ``chat_api/slot_detail.py`` reddens the first test.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard import chat_handlers, ws_event_scope
from kiro_crew.dashboard.handlers import source_providers
from kiro_crew.dashboard.state import SlotOrigin

APP = "design-tweak"
ROWS = [
    {"key": "design-tweak-1", "title": "own", "last_message": "mine"},
    {"key": "chat-1-1", "title": "person", "last_message": "private"},
    {"key": "other-app-1", "title": "other app", "last_message": "theirs"},
]


def _slot(app: str, origin: str) -> SimpleNamespace:
    return SimpleNamespace(_app=app, _origin=origin)


@pytest.fixture
def declared(monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[bool, frozenset[str]]]:
    """The manifest read, per app: ``(enabled, declared events)``."""
    answers: dict[str, tuple[bool, frozenset[str]]] = {APP: (True, frozenset())}
    monkeypatch.setattr(ws_event_scope, "_declared_cache", {})
    monkeypatch.setattr(ws_event_scope, "_read_declared_events", lambda app: answers[app])
    return answers


@pytest.fixture
def slots_app(monkeypatch: pytest.MonkeyPatch, declared) -> web.Application:
    async def _noop() -> None:
        return None

    monkeypatch.setattr(source_providers, "ensure_gitlab_hosts_loaded", _noop)
    monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda _r: False)
    state = SimpleNamespace(
        _slots={
            "design-tweak-1": _slot(APP, SlotOrigin.APP),
            "chat-1-1": _slot("", SlotOrigin.USER),
            "other-app-1": _slot("other-app", SlotOrigin.APP),
        },
        slot_list_held_keys=lambda: [],
        slot_list_hold_channels=lambda: False,
        serialize_slots=lambda **_k: [dict(r) for r in ROWS],
    )
    app = web.Application()
    app["state"] = state
    return app


def _request(app: web.Application, *, caller: str, dashboard: bool, internal: bool = False):
    req = make_mocked_request("GET", "/api/chat/slots", app=app)
    req["app"] = caller
    req["is_dashboard_user"] = dashboard
    if internal:
        req["internal_auth"] = True
    return req


async def _keys(req: web.Request) -> list[str]:
    resp = await chat_handlers.api_chat_slots(req)
    return [row["key"] for row in json.loads(resp.text)]


@pytest.mark.asyncio
async def test_an_app_token_lists_only_its_own_slots(slots_app: web.Application) -> None:
    req = _request(slots_app, caller=APP, dashboard=False)
    assert await _keys(req) == ["design-tweak-1"]


@pytest.mark.asyncio
async def test_a_declared_slots_scope_widens_the_list_as_on_the_socket(
    slots_app: web.Application, declared
) -> None:
    declared[APP] = (True, frozenset({"slots:user"}))
    req = _request(slots_app, caller=APP, dashboard=False)
    assert await _keys(req) == ["design-tweak-1", "chat-1-1"]


@pytest.mark.asyncio
async def test_a_disabled_app_lists_nothing(slots_app: web.Application, declared) -> None:
    declared[APP] = (False, frozenset({"slots:all"}))
    req = _request(slots_app, caller=APP, dashboard=False)
    assert await _keys(req) == []


@pytest.mark.asyncio
async def test_an_unreadable_manifest_keeps_only_the_apps_own_slots(
    slots_app: web.Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _broken(_app: str) -> tuple[bool, frozenset[str]]:
        raise OSError("unreadable")

    monkeypatch.setattr(ws_event_scope, "load_declared_events_for_connect", _broken)
    req = _request(slots_app, caller=APP, dashboard=False)
    assert await _keys(req) == ["design-tweak-1"]


@pytest.mark.asyncio
async def test_the_person_keeps_the_full_list(slots_app: web.Application) -> None:
    req = _request(slots_app, caller="", dashboard=True)
    assert await _keys(req) == ["design-tweak-1", "chat-1-1", "other-app-1"]


@pytest.mark.asyncio
async def test_an_internal_caller_with_a_derived_app_keeps_the_full_list(
    slots_app: web.Application,
) -> None:
    # The dashboard MCP tools read this list over the internal secret and narrow
    # it by the caller's app themselves (``mcp_dashboard_caller_scope``).
    req = _request(slots_app, caller=APP, dashboard=False, internal=True)
    assert await _keys(req) == ["design-tweak-1", "chat-1-1", "other-app-1"]
