"""The app-name gate bounds the name's length.

An app name becomes part of several file names the gateway creates. A name the
filesystem cannot hold in one component must be refused at the gate with a
message, not admitted and then fail at install with an ``OSError`` that the
install route answers as a 500.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps import manifest_names
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME
from kiro_crew.apps.manager_records import _record_lock_path, _update_retired_tree_prefix
from kiro_crew.apps.manifest import app_name_error
from kiro_crew.apps.routes import register_app_routes

# The documented cap on an app name's length.
APP_NAME_MAX_LENGTH = 128
# The per-component limit of NTFS, APFS, ext4 and the other common filesystems.
_COMPONENT_LIMIT = 255
# A Windows process id is a 32-bit value: at most 10 decimal digits.
_LONGEST_PID = "9" * 10
_HEX8 = "f" * 8


def _derived_components(name: str) -> list[str]:
    """Every file-name component the gateway builds from an app name."""
    return [
        name,
        _record_lock_path(name).name,
        f".{name}-data-tmp",
        f".{name}-secret-tmp",
        f".{name}-update-inflight",
        f"{_update_retired_tree_prefix(name)}{_LONGEST_PID}-{_HEX8}",
        f"{name}.partial-{_HEX8}",
        f".{name}-deps-doomed.kirocrew-deps-prior",
        f".{name}-deps-doomed.kirocrew-deps-staging-{_LONGEST_PID}-{_HEX8}",
    ]


class TestAppNameLengthGate:
    def test_the_gate_publishes_the_documented_cap(self):
        assert getattr(manifest_names, "APP_NAME_MAX_LENGTH", None) == APP_NAME_MAX_LENGTH

    def test_the_longest_name_is_admitted(self):
        assert app_name_error("a" * APP_NAME_MAX_LENGTH) is None

    def test_one_past_the_limit_is_refused_with_the_reason(self):
        error = app_name_error("a" * (APP_NAME_MAX_LENGTH + 1))
        assert error is not None
        assert str(APP_NAME_MAX_LENGTH) in error

    def test_a_name_that_overflows_the_lock_file_is_refused(self):
        assert app_name_error("a" * 251) is not None

    def test_a_long_kebab_name_with_hyphens_is_refused(self):
        name = "-".join(["abcdefghi"] * 26)
        assert len(name) > APP_NAME_MAX_LENGTH
        assert app_name_error(name) is not None

    def test_grammar_errors_keep_their_message(self):
        error = app_name_error("A" * 300)
        assert error is not None
        assert "kebab-case" in error

    def test_every_derived_component_fits_at_the_limit(self):
        name = "a" * APP_NAME_MAX_LENGTH
        for component in _derived_components(name):
            assert len(component.encode("utf-8")) <= _COMPONENT_LIMIT, component


# ---------------------------------------------------------------------------
# Install route
# ---------------------------------------------------------------------------


def _make_app_source(tmp_path, name):
    src = tmp_path / "source" / "long-name-src"
    src.mkdir(parents=True)
    (src / APP_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "displayName": "Long Name App",
                "description": "App whose name is near the length limit",
                "author": "tester",
            }
        ),
        encoding="utf-8",
    )
    return src


@pytest.fixture()
def app_home(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    import kiro_crew.apps.backend as bmod
    import kiro_crew.apps.bridges as bridges_mod

    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    bmod._processes.clear()
    bmod._allocated_ports.clear()
    return home


def _make_web_app():
    # Stands in for token_auth_middleware authenticating the dashboard owner.
    @web.middleware
    async def _owner(request, handler):
        request["app"] = ""
        request["user"] = "owner"
        return await handler(request)

    app = web.Application(middlewares=[_owner])
    app["state"] = SimpleNamespace(owner_id="owner")
    register_app_routes(app)
    return app


def _installed_entries(home):
    apps = home / "apps"
    return sorted(p.name for p in apps.iterdir()) if apps.is_dir() else []


@pytest.mark.asyncio
async def test_install_route_refuses_an_over_long_name_with_400(tmp_path, app_home):
    src = _make_app_source(tmp_path, "a" * 251)
    async with TestClient(TestServer(_make_web_app())) as client:
        resp = await client.post("/api/apps/install", json={"source": str(src)})
        assert resp.status == 400
        data = await resp.json()
        assert str(APP_NAME_MAX_LENGTH) in data["error"]
    assert _installed_entries(app_home) == []


@pytest.mark.asyncio
async def test_install_route_installs_a_name_at_the_limit(tmp_path, app_home):
    name = "a" * APP_NAME_MAX_LENGTH
    src = _make_app_source(tmp_path, name)
    async with TestClient(TestServer(_make_web_app())) as client:
        resp = await client.post("/api/apps/install", json={"source": str(src)})
        assert resp.status == 201
        assert (await resp.json())["ok"] is True
    assert name in _installed_entries(app_home)
