"""The memory provider preview switch: off by default, and off means no part of the feature.

Off: the resolver answers Crew's own memory whatever ``memory.provider`` says, every
``/api/memory/provider`` route is a 404, and ``memoryAdmin`` is neither granted nor
honoured. On (the environment override, or the owner's stored choice read at start):
each of them works again. The switch's own route is owner-only and registered either way.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from memory_provider_helpers import FIXTURE_ID, set_provider

from kiro_crew.apps import point_grants
from kiro_crew.config import loader
from kiro_crew.dashboard.handlers import memory_provider as handlers
from kiro_crew.dashboard.routes import memory as memory_routes
from kiro_crew.memory_provider import preview, resolver, routes_preview

pytestmark = pytest.mark.xdist_group("memory_provider")


@pytest.fixture(autouse=True)
def _off(_floor_monkeypatch):
    _floor_monkeypatch.delenv(preview.PREVIEW_ENV, raising=False)
    preview.reset_for_tests()
    resolver.reset_snapshot_cache()
    yield
    preview.reset_for_tests()
    resolver.reset_snapshot_cache()


def _turn_on(how: str, monkeypatch) -> None:
    if how == "env":
        monkeypatch.setenv(preview.PREVIEW_ENV, "1")
    else:
        preview.write_stored(True)
        preview.reset_for_tests()


def _memory_admin_app(monkeypatch) -> None:
    from kiro_crew.apps import manager

    manifest = SimpleNamespace(permissions=SimpleNamespace(memoryAdmin=True))
    monkeypatch.setattr(manager, "get_app_manifest", lambda app: manifest)
    monkeypatch.setattr(manager, "is_app_enabled", lambda app: True)
    monkeypatch.setattr(manager, "staged_app_grants", lambda app, family, declared: declared())


async def _status(path: str) -> int:
    app = web.Application()
    memory_routes.register(app)
    async with TestClient(TestServer(app)) as client:
        return (await client.get(path)).status


@pytest.fixture
def stranger(monkeypatch):
    """Every request comes from neither the owner nor an app."""
    monkeypatch.setattr(handlers, "_is_owner", lambda request: False)


def test_off_is_the_default_and_reads_every_seam_as_off(monkeypatch):
    set_provider("replace", FIXTURE_ID)
    _memory_admin_app(monkeypatch)
    assert preview.preview_enabled() is False
    assert resolver.route_for("default") is resolver.BUILTIN_ROUTE
    assert resolver.route_for("default", session_key="chat:1") is resolver.BUILTIN_ROUTE
    assert resolver.provider_configured() is False
    assert point_grants.grant_source({"permissions": {"memoryAdmin": True}})["api"] == []
    assert handlers._app_holds_memory_admin("memory-app") is False


def test_a_saved_replace_mode_does_not_pin_harness_memory_off_while_off(monkeypatch):
    from kiro_crew.memory_provider.harness_pin import harness_memory_settings

    set_provider("replace", FIXTURE_ID)
    assert harness_memory_settings() == {}
    _turn_on("env", monkeypatch)
    assert harness_memory_settings() == {"userMemoryOptIn": {"enabled": False}}


@pytest.mark.parametrize("how", ["env", "stored"])
def test_on_routes_to_the_configured_provider_and_grants_memory_admin(how, monkeypatch):
    set_provider("replace", FIXTURE_ID)
    _turn_on(how, monkeypatch)
    assert preview.preview_enabled() is True
    route = resolver.route_for("default")
    assert (route.mode, route.provider_id) == ("replace", FIXTURE_ID)
    _memory_admin_app(monkeypatch)
    assert point_grants.grant_source({"permissions": {"memoryAdmin": True}})["api"] == [
        "/api/memory/provider"
    ]
    assert handlers._app_holds_memory_admin("memory-app") is True


@pytest.mark.asyncio
async def test_provider_routes_are_a_404_while_off_and_reachable_when_on(monkeypatch, stranger):
    for path in ("/api/memory/provider", "/api/memory/provider/migrations"):
        assert await _status(path) == 404, path
    _turn_on("env", monkeypatch)
    for path in ("/api/memory/provider", "/api/memory/provider/migrations"):
        # Reached the handler: an unauthenticated caller is refused by its owner gate.
        assert await _status(path) == 403, path


def test_a_stored_choice_waits_for_the_next_start():
    assert preview.snapshot_at_start() is False
    preview.write_stored(True)
    setting = preview.current_setting().to_dict()
    assert setting == {
        "enabled": True,
        "active": False,
        "env_override": False,
        "restart_required": True,
    }
    assert json.loads(loader.memory_provider_preview_path().read_text()) == {"enabled": True}


@pytest.mark.asyncio
async def test_the_switch_route_is_owner_only_and_registered_while_off(monkeypatch, stranger):
    request = make_mocked_request("GET", routes_preview.PREVIEW_PATH)
    request["app"] = "memory-app"
    with pytest.raises(web.HTTPForbidden) as refused:
        await routes_preview.api_get_preview(request)
    assert json.loads(refused.value.text)["code"] == "owner_only"
    assert await _status(routes_preview.PREVIEW_PATH) == 403
    assert preview.snapshot_at_start() is False  # the gateway reads it at start
    monkeypatch.setattr(handlers, "_is_owner", lambda request: True)
    audits: list[tuple] = []
    monkeypatch.setattr(routes_preview, "_audit", lambda *a: audits.append(a[1:]))
    app = web.Application()
    app.router.add_put(routes_preview.PREVIEW_PATH, routes_preview.api_put_preview)
    async with TestClient(TestServer(app)) as client:
        response = await client.put(routes_preview.PREVIEW_PATH, json={"enabled": True})
        assert response.status == 200
        assert (await response.json())["restart_required"] is True
        bad = await client.put(routes_preview.PREVIEW_PATH, json={"enabled": "yes"})
        assert bad.status == 400
    assert audits == [(True, "enabled")]
    assert preview.read_stored() is True
    assert preview.preview_enabled() is False
