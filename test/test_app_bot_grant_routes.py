"""The owner's Bot-grant route and the gate reads behind it, on a real install record.

``GET/PUT /api/apps/{name}/bot-grants`` reads and replaces the ticked list in the
gateway-only store; a bad body, an app that is not installed and an unreadable
record each get their own answer, and every read that cannot be trusted grants
nothing.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from chat_test_helpers import _make_state

from kiro_crew.apps import bot_grant_store, bot_grants, manager_records
from kiro_crew.apps.manager_records import InstalledApp
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.routes_bot_grants import handle_bot_grants

APP = "notes-app"
SLUG = "code-reviewer"
OWNER = "U_OWNER"


def _install(installed_at="2026-11-08T00:00:00Z"):
    manager_records.app_dir(APP).mkdir(parents=True, exist_ok=True)
    manager_records._write_installed(
        APP, InstalledApp(name=APP, version="1.0.0", enabled=True, installedAt=installed_at)
    )


def _owner_request(tmp_path, method, body=None):
    state = _make_state(tmp_path)
    state.owner_id = OWNER
    app = web.Application()
    app["state"] = state
    req = make_mocked_request(
        method, f"/api/apps/{APP}/bot-grants", app=app, match_info={"name": APP}
    )
    req["app"] = ""
    req["user"] = OWNER

    async def _json():
        if isinstance(body, Exception):
            raise body
        return body

    req.json = _json  # type: ignore[method-assign]
    return req


async def _call(req):
    resp = await handle_bot_grants(req)
    return resp.status, json.loads(resp.body)


@pytest.mark.asyncio
async def test_the_owner_reads_and_replaces_the_ticked_list(tmp_path):
    _install()
    status, body = await _call(_owner_request(tmp_path, "PUT", {"bots": [SLUG, "Bad"]}))
    assert status == 200 and body["bots"] == [SLUG]
    status, body = await _call(_owner_request(tmp_path, "GET"))
    assert status == 200
    assert body == {"bots": [SLUG], "declaredScopes": [], "scopes": []}
    status, body = await _call(_owner_request(tmp_path, "PUT", {"bots": []}))
    assert (status, body["bots"]) == (200, [])
    assert not bot_grant_store.grants_path(APP).exists()


@pytest.mark.asyncio
async def test_unticking_every_bot_keeps_the_approved_scopes_and_a_disabled_app_shows_them(
    tmp_path, monkeypatch
):
    _install()
    manifest = AppManifest.from_dict(
        {"name": APP, "version": "1.0.0", "permissions": {"bots": ["read", "wake"]}}
    )
    monkeypatch.setattr("kiro_crew.apps.manager.get_app_manifest", lambda _n: manifest)
    manager_records._write_approved_grants(APP, {"bots": ["read"]})
    await _call(_owner_request(tmp_path, "PUT", {"bots": [SLUG]}))
    status, body = await _call(_owner_request(tmp_path, "PUT", {"bots": []}))
    assert status == 200 and body["bots"] == []
    assert manager_records._read_approved_grants(APP)["bots"] == ["read"]
    # The card reads the owner's approvals whether or not the app is enabled.
    monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda _n: False)
    status, body = await _call(_owner_request(tmp_path, "GET"))
    assert (status, body["scopes"], body["declaredScopes"]) == (200, ["read"], ["read", "wake"])


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_an_app_that_is_not_installed_is_not_found(tmp_path, method):
    status, body = await _call(_owner_request(tmp_path, method, {"bots": [SLUG]}))
    assert (status, body["code"]) == (404, "app_not_installed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        (ValueError("not json"), "invalid_json"),
        ({"bots": SLUG}, "invalid_bot_grants"),
        ({"bots": [SLUG, 7]}, "invalid_bot_grants"),
        ({"bots": ["b"] * (bot_grant_store.MAX_BOT_GRANTS + 1)}, "invalid_bot_grants"),
        ([SLUG], "invalid_bot_grants"),
    ],
)
async def test_a_bad_body_is_refused_and_writes_nothing(tmp_path, body, code):
    _install()
    status, resp = await _call(_owner_request(tmp_path, "PUT", body))
    assert (status, resp["code"]) == (400, code)
    assert not bot_grant_store.grants_path(APP).exists()


def test_reads_that_cannot_be_trusted_grant_nothing():
    assert bot_grants.app_bot_scopes("") == frozenset()
    assert bot_grants.app_granted_bots("") == ()
    # Not installed.
    assert bot_grants.app_granted_bots(APP) == ()
    assert bot_grants.set_app_granted_bots(APP, [SLUG]) is None
    # The enabled check itself fails.
    with patch("kiro_crew.apps.manager.is_app_enabled", side_effect=OSError("unreadable")):
        assert bot_grants.app_bot_scopes(APP) == frozenset()
    # A record that is not an object.
    _install()
    path = bot_grant_store.grants_path(APP)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]", encoding="utf-8")
    assert bot_grants.app_granted_bots(APP) == ()


def test_a_name_that_could_leave_the_records_directory_is_refused():
    for name in ("", "../x", "a/b", ".hidden", "Upper"):
        with pytest.raises(ValueError):
            bot_grant_store.grants_path(name)
