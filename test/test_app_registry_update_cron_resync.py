"""A registry update carries a changed ``app.json`` cron into the RUNNING scheduler.

``register_app_crons_with_service`` re-applies a changed manifest entry to the job it
already registered, but only where it is called. The local-source update calls it; the
three ways a registry app is updated in place did not, so a cron field the new version
changed kept its old value until the next gateway start:

* ``POST /api/apps/{name}/update`` on a registry-sourced app;
* ``POST /api/apps/registry/install`` over an app already installed;
* ``POST /api/apps/registry/install-stream`` over an app already installed (the
  detail page's Update button).

Each scenario runs a real ``CronService`` and a real ``update_app`` file swap; only the
clone and the backend are stubbed. A fresh registry install leaves the app disabled, and
enabling it registers the crons, so the install routes promote nothing there.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import APP, _install, _make_app, _setup_env

import kiro_crew.apps.routes as routes_mod
from kiro_crew.apps.bridges import register_app_crons_with_service
from kiro_crew.apps.manager import enable_app, get_app, update_app
from kiro_crew.cron import CronService

CRON = "digest"
JOB = f"{APP}/{CRON}"


def _manifest_extra(message: str) -> dict[str, Any]:
    return {
        "permissions": {"cron": True},
        "crons": [{"name": CRON, "every": 600, "message": message}],
    }


def _new_version_source(tmp_path: Path, message: str) -> Path:
    """The tree a registry clone would hand ``update_app``: same app, new cron message."""
    src = tmp_path / "clone" / APP
    src.mkdir(parents=True)
    manifest = {
        "name": APP,
        "version": "2.0.0",
        "displayName": "Coverage Test App",
        "description": "App for routes coverage testing",
        "author": "tester",
        **_manifest_extra(message),
    }
    (src / "app.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return src


def _job_message(svc: CronService) -> str:
    jobs = [j for j in svc.list_jobs(include_disabled=True) if j.name == JOB]
    assert len(jobs) == 1, [j.name for j in svc.list_jobs(include_disabled=True)]
    return jobs[0].message


def _stub_registry_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the clone: swap the installed tree with the real ``update_app``."""

    async def _reinstall(name: str, **kwargs: Any) -> dict[str, Any]:
        result = update_app(_new_version_source(tmp_path, "v2 words"), expected_name=name)
        assert result.ok, result.error
        return {"ok": True, "name": name}

    monkeypatch.setattr(routes_mod, "install_from_registry", _reinstall)
    monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
    monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
    monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
    monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)


_UPDATE_ROUTES = [
    pytest.param(f"/api/apps/{APP}/update", {}, id="update"),
    pytest.param("/api/apps/registry/install", {"name": APP}, id="install"),
    pytest.param("/api/apps/registry/install-stream", {"name": APP}, id="install-stream"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "body"), _UPDATE_ROUTES)
async def test_registry_update_reaches_the_running_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, body: dict[str, Any]
) -> None:
    home = _setup_env(tmp_path, monkeypatch)
    _install(tmp_path, **_manifest_extra("v1 words"))
    enable_app(APP)
    _stub_registry_update(tmp_path, monkeypatch)

    svc = CronService(base_dir=home / "crons")
    await svc.start()
    try:
        await routes_mod._register_app_off_loop(APP)
        await register_app_crons_with_service(APP, svc)
        assert _job_message(svc) == "v1 words"

        app = _make_app(dashboard_user="owner")
        app["state"].crons = svc
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(path, json=body)
            assert resp.status in (200, 201), await resp.text()
            await resp.read()

        assert get_app(APP)["version"] == "2.0.0"
        assert get_app(APP)["enabled"] is True
        assert _job_message(svc) == "v2 words"
    finally:
        await svc.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/api/apps/registry/install", "/api/apps/registry/install-stream"]
)
async def test_a_fresh_registry_install_promotes_no_crons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """A fresh install is disabled: its crons wait for the enable that registers them."""
    _setup_env(tmp_path, monkeypatch)

    async def _fresh(name: str, **kwargs: Any) -> dict[str, Any]:
        _install(tmp_path, **_manifest_extra("v1 words"))
        return {"ok": True, "name": APP}

    monkeypatch.setattr(routes_mod, "install_from_registry", _fresh)
    monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
    resync = AsyncMock(return_value=[])
    monkeypatch.setattr(routes_mod, "register_app_crons_with_service", resync)

    app = _make_app(dashboard_user="owner")
    app["state"].crons = object()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(path, json={"name": APP})
        assert resp.status in (200, 201), await resp.text()
        await resp.read()

    assert get_app(APP)["enabled"] is False
    resync.assert_not_awaited()
