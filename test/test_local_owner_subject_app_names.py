"""No app may carry the local owner's own token subject as its name.

An app token's subject is the app's name, and with no owner id configured the owner
check (``is_host_owner_dashboard_request``) admits the subjects ``local-app`` and
``local-startup`` alone. So both names are reserved at every door that admits an app,
and the two app-token mints refuse them for an app that reached disk before the
reservation. The owner's own ``local-app`` session keeps working: the fallback itself
is unchanged.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.apps import plugin_import
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    _read_installed,
    install_app,
    register_external_app,
)
from kiro_crew.apps.manifest import RESERVED_APP_NAME_CODE, app_name_error, is_reserved_app_name
from kiro_crew.dashboard import revocation_gen, token_auth, token_auth_tokens
from kiro_crew.dashboard.handlers import core as core_mod
from kiro_crew.dashboard.handlers.source_providers import is_host_owner_dashboard_request
from kiro_crew.shared_crew.owner import LOCAL_BOOTSTRAP_SUBJECTS

OWNER_SUBJECTS = ("local-app", "local-startup")
SECRET = "s3cret"


def test_every_owner_subject_is_a_reserved_app_name() -> None:
    """``manifest_names`` keeps its own copy of the subjects; this pins it to the owner's."""
    assert set(OWNER_SUBJECTS) == set(LOCAL_BOOTSTRAP_SUBJECTS)
    for subject in LOCAL_BOOTSTRAP_SUBJECTS:
        assert is_reserved_app_name(subject) is True


# -- admission doors ------------------------------------------------------------------


@pytest.fixture()
def app_home(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    return home


def _app_source(tmp_path, name):
    src = tmp_path / "source" / f"{name}-src"
    src.mkdir(parents=True)
    (src / APP_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "displayName": "Test App",
                "description": "A test app",
                "author": "tester",
            }
        ),
        encoding="utf-8",
    )
    return src


@pytest.mark.parametrize("name", OWNER_SUBJECTS)
def test_the_name_contract_refuses_it(name) -> None:
    error = app_name_error(name) or ""
    # The refusal names the real reason, not the notification-channel one ``system`` gets.
    assert "reserved" in error and "sign-in identity" in error
    assert "notification" not in error
    assert is_reserved_app_name(name) is True


@pytest.mark.parametrize("name", OWNER_SUBJECTS)
def test_install_and_self_registration_refuse_it(name, tmp_path, app_home) -> None:
    installed = install_app(_app_source(tmp_path, name))
    assert not installed.ok
    assert installed.error_code == RESERVED_APP_NAME_CODE
    assert _read_installed(name) is None

    registered = register_external_app(name, "1.0.0", "Test App")
    assert not registered.ok
    assert registered.error_code == RESERVED_APP_NAME_CODE


@pytest.mark.parametrize("name", OWNER_SUBJECTS)
def test_a_plugin_import_cannot_derive_it(name) -> None:
    with pytest.raises(plugin_import.PluginImportError) as refused:
        plugin_import.normalize_app_name(name)
    assert refused.value.code == "reserved_app_name"


# -- token mints, for an app already on disk under the name ---------------------------


@pytest.fixture()
def enabled_apps(tmp_path, monkeypatch):
    """Every app is owner-enabled and holds ``SECRET``, so only the name decides."""
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"local-owner-subject-app-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    monkeypatch.setattr("kiro_crew.apps.host_exec_grants.host_exec_granted", lambda _n: True)
    monkeypatch.setattr(token_auth_tokens, "read_app_secret", lambda _n: SECRET)
    monkeypatch.setattr(token_auth, "_sel_fn", lambda: MagicMock())
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: MagicMock())


@pytest.mark.parametrize("name", OWNER_SUBJECTS)
def test_no_app_token_is_minted_for_it(name, enabled_apps) -> None:
    assert token_auth_tokens.validate_app_secret(name, SECRET) is False
    assert token_auth_tokens.issue_in_process_app_token(name) == ""
    # The same harness mints for an ordinary name, so the refusal is the name's.
    assert token_auth_tokens.validate_app_secret("notes-app", SECRET) is True
    assert token_auth_tokens.issue_in_process_app_token("notes-app") != ""


async def _probe(request: web.Request) -> web.Response:
    return web.json_response({"owner": is_host_owner_dashboard_request(request)})


@contextlib.asynccontextmanager
async def _serve(tmp_path):
    app = web.Application(middlewares=[token_auth.token_auth_middleware()])
    state = _make_state(tmp_path)
    state.owner_id = ""
    app["state"] = state
    app.router.add_post("/api/apps/{name}/token", core_mod.api_app_token)
    app.router.add_get("/api/probe-owner", _probe)
    async with TestClient(TestServer(app)) as client:
        yield client


@pytest.mark.asyncio
@pytest.mark.parametrize("name", OWNER_SUBJECTS)
async def test_the_exchange_route_refuses_it_and_the_owner_keeps_the_fallback(
    name, tmp_path, enabled_apps
) -> None:
    """Real middleware, real exchange handler, over a loopback socket."""
    async with _serve(tmp_path) as client:
        refused = await client.post(f"/api/apps/{name}/token", headers={"X-App-Secret": SECRET})
        assert refused.status == 403
        assert "token" not in await refused.json()

        granted = await client.post("/api/apps/notes-app/token", headers={"X-App-Secret": SECRET})
        assert granted.status == 200

        # The owner's own bootstrap session, minted as the gateway mints it with no
        # owner id, is still the owner.
        owner = await client.get(
            "/api/probe-owner", params={"token": token_auth.generate_token(name)}
        )
        assert owner.status == 200
        assert (await owner.json())["owner"] is True
