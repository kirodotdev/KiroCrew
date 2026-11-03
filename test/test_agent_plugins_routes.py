"""The owner-only ``/api/agent-plugins`` routes: list, and the on/off switch.

Turning a plugin on lets its stdio servers start outside the PreToolUse gate, so
only the dashboard owner may do it: an app token, an allowed messaging user and
an unauthenticated caller are refused, and a refused POST never reads its body
or writes the record.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from agent_plugins_helpers import install

from kiro_crew.agent_plugins import consent
from kiro_crew.dashboard.handlers import agent_plugins_consent as handler

OWNER = "owner-1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    powers = tmp_path / "powers"
    crew = tmp_path / "crew"
    crew.mkdir()
    monkeypatch.setenv("KIRO_POWERS_HOME", str(powers))
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    root = install(powers, "demo")
    return powers, root


def _req(*, app: str = "", user: str = OWNER, body: dict | None = None):
    req = MagicMock()
    req.path = "/api/agent-plugins"
    store = {"app": app, "user": user}
    req.get = lambda key, default=None: store.get(key, default)
    req.__contains__ = lambda _self, key: key in store
    req.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = OWNER
    req.app = {"state": state}
    req.json = AsyncMock(return_value=body or {})
    return req


_REFUSED = [
    pytest.param({"app": "notes"}, id="app-token"),
    pytest.param({"user": "slack-guest"}, id="non-owner-messaging-user"),
    pytest.param({"user": ""}, id="unauthenticated"),
]


@pytest.mark.parametrize("who", _REFUSED)
def test_get_refuses_anyone_but_the_owner(env, who):
    resp = asyncio.run(handler.api_agent_plugins_get(_req(**who)))
    assert resp.status == 403
    assert json.loads(resp.text)["code"] == "dashboard_owner_required"


@pytest.mark.parametrize("who", _REFUSED)
def test_post_refuses_anyone_but_the_owner_and_writes_nothing(env, who):
    req = _req(**who, body={"name": "demo", "enabled": True})
    resp = asyncio.run(handler.api_agent_plugins_post(req))
    assert resp.status == 403
    assert req.json.await_count == 0
    assert not consent.consent_path().exists()


def test_the_owner_lists_turns_on_and_turns_off(env):
    _powers, _root = env
    listed = json.loads(asyncio.run(handler.api_agent_plugins_get(_req())).text)
    assert [(p["name"], p["state"]) for p in listed["plugins"]] == [("demo", "off")]

    on = asyncio.run(handler.api_agent_plugins_post(_req(body={"name": "demo", "enabled": True})))
    assert on.status == 200 and json.loads(on.text)["state"] == "on"
    assert consent.enabled() == {"demo"}

    off = asyncio.run(handler.api_agent_plugins_post(_req(body={"name": "demo", "enabled": False})))
    assert off.status == 200 and json.loads(off.text)["state"] == "off"
    assert consent.enabled() == frozenset()


def test_the_record_stores_the_name_only(env):
    body = {"name": "demo", "enabled": True, "extra": "ignored"}
    asyncio.run(handler.api_agent_plugins_post(_req(body=body)))
    stored = json.loads(consent.consent_path().read_text(encoding="utf-8"))
    assert stored == {"version": 1, "enabled": {"demo": {}}}


def test_an_on_plugin_stays_on_after_an_edit(env):
    _powers, root = env
    asyncio.run(handler.api_agent_plugins_post(_req(body={"name": "demo", "enabled": True})))
    (root / "extra.txt").write_text("v2", encoding="utf-8")
    listed = json.loads(asyncio.run(handler.api_agent_plugins_get(_req())).text)
    assert [(p["name"], p["state"]) for p in listed["plugins"]] == [("demo", "on")]


def test_an_unknown_or_invalid_plugin_cannot_be_turned_on(env):
    powers, _root = env
    install(powers, "broken", manifest="{not json")
    missing = asyncio.run(
        handler.api_agent_plugins_post(_req(body={"name": "absent", "enabled": True}))
    )
    assert missing.status == 404
    broken = asyncio.run(
        handler.api_agent_plugins_post(_req(body={"name": "broken", "enabled": True}))
    )
    assert broken.status == 409
    assert consent.enabled() == frozenset()


@pytest.mark.parametrize("body", [{}, {"name": "demo"}, {"name": "demo", "enabled": "yes"}])
def test_a_malformed_body_is_refused(env, body):
    resp = asyncio.run(handler.api_agent_plugins_post(_req(body=body)))
    assert resp.status == 400


def test_the_routes_are_registered_on_the_dashboard():
    from aiohttp import web

    from kiro_crew.dashboard.routes import system

    app = web.Application()
    system.register(app)
    routes = {
        (r.method, r.resource.canonical)
        for r in app.router.routes()
        if r.resource is not None and r.resource.canonical == "/api/agent-plugins"
    }
    assert routes == {
        ("GET", "/api/agent-plugins"),
        ("POST", "/api/agent-plugins"),
        ("HEAD", "/api/agent-plugins"),
    }
