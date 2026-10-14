"""PUT /api/agents — body-form update for crew names that need percent-encoding.

The crew editor's Save button calls ``PUT /api/agents/{name}``. When the
agent's config key contains a space (created before slug keys were introduced)
and the dashboard is behind a reverse proxy that normalises percent-encoded
paths, the proxy decodes ``My%20Agent`` to a literal space in the HTTP
request-line, producing a 502 Bad Gateway.

The body-form route ``PUT /api/agents`` accepts the crew name in the JSON body
under the key ``name`` instead of the URL path. The frontend uses it
automatically when ``encodeURIComponent(name) !== name``, so a space-containing
key is never placed in a URL path segment.

These tests verify that:

* the body-form route saves settings for a crew whose key contains a space;
* the existing path-form route ``PUT /api/agents/{name}`` is unchanged;
* sending to the body-form route with a missing or empty ``name`` returns 400;
* both routes enforce the same owner-only auth gate.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _make_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

    app = web.Application()
    # Body-form route registered first, matching the real route registration order.
    app.router.add_put("/api/agents", api_kirocrew_agent_update)
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    return app


def _seed(name: str) -> None:
    """Write a minimal config with one crew and a default agent."""
    cfg = KiroCrewConfig.load()
    cfg.agents[name] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", workspace="default", model="claude-sonnet-4"
    )
    cfg.default_agent = name
    cfg.save()


class TestBodyFormRoute:
    @pytest.mark.asyncio
    async def test_space_key_saves_through_body_form(self):
        """A crew with a space in its config key is saved via PUT /api/agents."""
        _seed("My Agent")

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/agents",
                json={
                    "name": "My Agent",
                    "kiro_agent": "kirocrew",
                    "workspace": "default",
                    "model": "claude-sonnet-4",
                    "description": "updated",
                    "triggers": "",
                    "reasoning_effort": "",
                    "session_color": "",
                    "avatar": {},
                    "memory_store": "default",
                },
            )
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            assert body.get("ok") is True

        saved = KiroCrewConfig.load()
        assert "My Agent" in saved.agents
        assert saved.agents["My Agent"].description == "updated"

    @pytest.mark.asyncio
    async def test_body_form_missing_name_returns_400(self):
        """Body-form without a name field returns 400 name_required."""
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/agents",
                json={"kiro_agent": "kirocrew"},
            )
            assert resp.status == 400
            body = await resp.json()
            assert body.get("code") == "name_required"

    @pytest.mark.asyncio
    async def test_path_form_still_works_for_slug_keys(self):
        """The existing PUT /api/agents/{name} route is unchanged for slug keys."""
        _seed("my-agent")

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/agents/my-agent",
                json={
                    "kiro_agent": "kirocrew",
                    "workspace": "default",
                    "model": "claude-sonnet-4",
                    "description": "path form",
                    "triggers": "",
                    "reasoning_effort": "",
                    "session_color": "",
                    "avatar": {},
                    "memory_store": "default",
                },
            )
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            assert body.get("ok") is True

        saved = KiroCrewConfig.load()
        assert saved.agents["my-agent"].description == "path form"


class TestBodyFormAuthParity:
    """Body-form PUT /api/agents is gated by the same owner check as the path-form.

    Both routes share one handler (``api_kirocrew_agent_update``); ``_require_owner``
    fires before the name is resolved from either source. The tests here pin
    this explicitly so a future refactor that separates the handlers does not
    accidentally omit the gate on the body-form route.

    The comprehensive route-walk coverage lives in
    ``test_agents_endpoints_owner_auth.py``; these tests verify the behaviour
    directly for the body-form route.
    """

    @pytest.mark.asyncio
    async def test_body_form_rejects_non_owner(self, monkeypatch):
        """Non-owner caller gets 403 owner_only on PUT /api/agents."""
        from kiro_crew.dashboard.handlers import source_providers

        monkeypatch.setattr(
            source_providers,
            "is_owner_dashboard_request",
            lambda request: False,
        )
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put("/api/agents", json={"name": "any-crew"})
            assert resp.status == 403
            body = await resp.json()
            assert body.get("code") == "owner_only"

    @pytest.mark.asyncio
    async def test_body_form_same_gate_as_path_form(self, monkeypatch):
        """Body-form and path-form both return 403 owner_only for a non-owner."""
        from kiro_crew.dashboard.handlers import source_providers

        monkeypatch.setattr(
            source_providers,
            "is_owner_dashboard_request",
            lambda request: False,
        )
        async with TestClient(TestServer(_make_app())) as client:
            body_resp = await client.put("/api/agents", json={"name": "my-agent"})
            path_resp = await client.put("/api/agents/my-agent", json={})
            body_status, body_body = body_resp.status, await body_resp.json()
            path_status, path_body = path_resp.status, await path_resp.json()

        assert body_status == path_status == 403
        assert body_body.get("code") == "owner_only"
        assert path_body.get("code") == "owner_only"
