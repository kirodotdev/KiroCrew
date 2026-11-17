"""A signed app token for an app the owner then disabled reaches no Bot.

Real tokens through the real ``token_auth_middleware``. The Bot gate reads the app's
switch itself (``apps.bot_grants.app_bot_scopes``) and refuses a disabled app the way
it refuses an app with no Bot scope.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from app_token_records import mark_apps_live
from chat_test_helpers import _make_state

from kiro_crew import members as members_mod
from kiro_crew.apps import bot_grants
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.dashboard import app_bot_wake, revocation_gen, token_auth
from kiro_crew.dashboard.handlers import members as m

APP = "notes-app"
SLUG = "code-reviewer"

_MANIFEST = AppManifest.from_dict(
    {
        "name": APP,
        "version": "1.0.0",
        "displayName": APP,
        "description": "disabled token fixture",
        "permissions": {"api": ["/api/members"], "bots": ["read", "wake"]},
    }
)


def _cfg():
    return SimpleNamespace(
        agents={SLUG: KiroCrewAgentConfig(kiro_agent="kirocrew", description="d", model="m")},
        default_agent=SLUG,
        memory_stores={},
        workspaces={"default": SimpleNamespace(dir="workspace")},
        default_workspace="default",
        degraded_sections=frozenset(),
    )


@pytest.fixture
def enabled(tmp_path, monkeypatch):
    """The app's switch, as ``{"on": bool}``; everything else about the app is fixed."""
    switch = {"on": True}
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"app-bot-disabled-token-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    # The token check passes; this pins the Bot gate's own read of the switch.
    mark_apps_live(monkeypatch, {APP})
    monkeypatch.setattr("kiro_crew.apps.host_exec_grants.host_exec_granted", lambda _n: True)
    manifests = {APP: _MANIFEST}
    monkeypatch.setattr("kiro_crew.apps.manager.get_app_manifest", manifests.get)
    monkeypatch.setattr(
        "kiro_crew.apps.manager.staged_app_grants",
        lambda _name, _family, read: list(read() or []),
    )
    monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda _n: switch["on"])
    monkeypatch.setattr(bot_grants, "app_granted_bots", lambda _a: (SLUG,))
    return switch


@contextlib.asynccontextmanager
async def _serve(tmp_path):
    app = web.Application(middlewares=[token_auth.token_auth_middleware()])
    state = _make_state(tmp_path)
    state.owner_id = ""
    app["state"] = state
    app.router.add_get("/api/members", m.api_members)
    app.router.add_post("/api/members/{slug}/wake", m.api_member_wake)
    async with TestClient(TestServer(app)) as client:
        yield client


def _token() -> dict[str, str]:
    return {"token": token_auth.generate_token("local-app", app=APP)}


async def _started(*_a, **_k):
    return app_bot_wake.STARTED


@pytest.mark.asyncio
async def test_a_token_minted_before_the_disable_reaches_no_bot_after_it(tmp_path, enabled):
    params = _token()
    with (
        patch.object(m.KiroCrewConfig, "load", return_value=_cfg()),
        patch.object(members_mod, "read_dm_binding", return_value=None),
        patch.object(app_bot_wake, "request_wake", _started),
    ):
        async with _serve(tmp_path) as client:
            listed = await client.get("/api/members", params=params)
            assert listed.status == 200
            assert [b["id"] for b in (await listed.json())["data"]] == [SLUG]
            woke = await client.post(f"/api/members/{SLUG}/wake", params=params, json={"ref": "r1"})
            assert woke.status == 202

            enabled["on"] = False
            listed = await client.get("/api/members", params=params)
            assert listed.status == 404
            assert (await listed.json())["code"] == "not_found"
            woke = await client.post(f"/api/members/{SLUG}/wake", params=params, json={"ref": "r2"})
            assert woke.status == 404
