"""Tests for the Route B publish-provider registry (design §1.3).

Covers: manifest `publishProvider` parse/round-trip, discovery propagation,
the pure aggregation core (`collect_publish_providers`), the filesystem-backed
configured-check (`_provider_is_configured`), and the live
`GET /api/publish-providers` endpoint.
"""
from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.discovery import _manifest_to_builtin_dict
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, apps_dir, enable_app, install_app
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.routes import (
    _provider_is_configured,
    collect_publish_providers,
    register_app_routes,
)

_PP = {
    "id": "deploy-web-aws",
    "label": "Publish to public web (your AWS)",
    "icon": "Globe",
    "endpoint": "/api/apps/deploy-web/deploy",
    "kinds": ["widget", "html", "markdown"],
    "setupRoute": "/deploy-web",
    "configuredField": "profile",
}


# --- manifest parse / round-trip / propagation -----------------------------

def test_manifest_publish_provider_round_trip():
    m = AppManifest.from_dict({
        "name": "deploy-web", "version": "1.0.0", "displayName": "Web Deploy",
        "description": "x", "publishProvider": _PP,
    })
    assert m.publishProvider.id == "deploy-web-aws"
    assert m.publishProvider.endpoint == "/api/apps/deploy-web/deploy"
    assert m.publishProvider.configuredField == "profile"
    # Round-trips through to_dict/from_dict without loss.
    d = m.to_dict()
    assert d["publishProvider"]["kinds"] == ["widget", "html", "markdown"]
    m2 = AppManifest.from_dict(d)
    assert m2.publishProvider.setupRoute == "/deploy-web"


def test_manifest_no_publish_provider_omits_key():
    m = AppManifest.from_dict({
        "name": "plain", "version": "1.0.0", "displayName": "Plain", "description": "x",
    })
    assert m.publishProvider.id == ""
    assert "publishProvider" not in m.to_dict()


def test_discovery_propagates_publish_provider():
    m = AppManifest.from_dict({
        "name": "deploy-web", "version": "1.0.0", "displayName": "Web Deploy",
        "description": "x", "publishProvider": _PP,
    })
    d = _manifest_to_builtin_dict(m)
    assert d["publishProvider"]["id"] == "deploy-web-aws"


# --- pure aggregation -------------------------------------------------------

def _app(name, enabled, pp):
    return {"name": name, "enabled": enabled, "manifest": ({"publishProvider": pp} if pp else {})}


def test_collect_only_enabled_with_provider():
    apps = [
        _app("deploy-web", True, _PP),
        _app("no-provider", True, None),
        _app("disabled", False, _PP),
    ]
    res = collect_publish_providers(apps, configured_resolver=lambda n, pp: True)
    assert [p["id"] for p in res] == ["deploy-web-aws"]
    assert res[0]["app"] == "deploy-web" and res[0]["origin"] == "app"
    assert res[0]["configured"] is True


def test_collect_carries_configured_flag():
    apps = [_app("deploy-web", True, _PP)]
    res = collect_publish_providers(apps, configured_resolver=lambda n, pp: False)
    assert res[0]["configured"] is False
    assert res[0]["setupRoute"] == "/deploy-web"


def test_collect_skips_provider_without_id_or_endpoint():
    bad = {"label": "x"}  # no id, no endpoint
    res = collect_publish_providers([_app("x", True, bad)], configured_resolver=lambda n, pp: True)
    assert res == []


# --- filesystem configured-check -------------------------------------------

def test_provider_is_configured_reads_app_config(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    data_dir = apps_dir() / "deploy-web" / "data"
    data_dir.mkdir(parents=True)
    # No config yet → not configured.
    assert _provider_is_configured("deploy-web", _PP) is False
    # Empty profile → not configured.
    (data_dir / "config.json").write_text(json.dumps({"profile": "", "region": "us-west-2"}))
    assert _provider_is_configured("deploy-web", _PP) is False
    # Non-empty profile → configured.
    (data_dir / "config.json").write_text(json.dumps({"profile": "my-sso", "region": "us-west-2"}))
    assert _provider_is_configured("deploy-web", _PP) is True


def test_provider_is_configured_no_field_means_always(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    pp = {**_PP, "configuredField": ""}
    assert _provider_is_configured("deploy-web", pp) is True


def test_provider_is_configured_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    pp = {**_PP, "configFile": "../../../etc/passwd"}
    assert _provider_is_configured("deploy-web", pp) is False


# --- live endpoint ----------------------------------------------------------

def _setup_env(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    import kiro_crew.apps.bridges as bridges_mod
    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    import kiro_crew.apps.backend as bmod
    bmod._processes.clear()
    bmod._allocated_ports.clear()
    return home


def _make_provider_app_source(tmp_path, name="prov-app"):
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    manifest = {
        "name": name, "version": "1.0.0", "displayName": "Provider App",
        "description": "declares a publish provider", "author": "tester",
        "publishProvider": {**_PP, "endpoint": f"/api/apps/{name}/deploy", "setupRoute": f"/{name}"},
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    return src


def _make_app():
    app = web.Application()
    register_app_routes(app)
    return app


@pytest.mark.asyncio
async def test_endpoint_lists_enabled_configured_provider(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    src = _make_provider_app_source(tmp_path)
    install_app(str(src))
    enable_app("prov-app")
    # Mark it configured by writing the app's config field.
    data_dir = apps_dir() / "prov-app" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.json").write_text(json.dumps({"profile": "my-sso"}))

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/publish-providers")
        assert resp.status == 200
        body = await resp.json()
    ids = [p["id"] for p in body["providers"]]
    assert "deploy-web-aws" in ids
    prov = next(p for p in body["providers"] if p["id"] == "deploy-web-aws")
    assert prov["configured"] is True
    assert prov["endpoint"] == "/api/apps/prov-app/deploy"


@pytest.mark.asyncio
async def test_endpoint_unconfigured_provider_flagged(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    src = _make_provider_app_source(tmp_path)
    install_app(str(src))
    enable_app("prov-app")
    # No config written → configured=False (but still listed, so the UI can
    # render a "set it up" link).
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/publish-providers")
        body = await resp.json()
    prov = next(p for p in body["providers"] if p["id"] == "deploy-web-aws")
    assert prov["configured"] is False


@pytest.mark.asyncio
async def test_endpoint_excludes_disabled_app(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    src = _make_provider_app_source(tmp_path)
    install_app(str(src))
    # Not enabled → excluded entirely.
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/publish-providers")
        body = await resp.json()
    assert all(p["id"] != "deploy-web-aws" for p in body["providers"])
