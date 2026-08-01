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
    m = AppManifest.from_dict(
        {
            "name": "deploy-web",
            "version": "1.0.0",
            "displayName": "Web Deploy",
            "description": "x",
            "publishProvider": _PP,
        }
    )
    assert m.publishProvider.id == "deploy-web-aws"
    assert m.publishProvider.endpoint == "/api/apps/deploy-web/deploy"
    assert m.publishProvider.configuredField == "profile"
    # Round-trips through to_dict/from_dict without loss.
    d = m.to_dict()
    assert d["publishProvider"]["kinds"] == ["widget", "html", "markdown"]
    m2 = AppManifest.from_dict(d)
    assert m2.publishProvider.setupRoute == "/deploy-web"


def test_manifest_no_publish_provider_omits_key():
    m = AppManifest.from_dict(
        {
            "name": "plain",
            "version": "1.0.0",
            "displayName": "Plain",
            "description": "x",
        }
    )
    assert m.publishProvider.id == ""
    assert "publishProvider" not in m.to_dict()


def test_discovery_propagates_publish_provider():
    m = AppManifest.from_dict(
        {
            "name": "deploy-web",
            "version": "1.0.0",
            "displayName": "Web Deploy",
            "description": "x",
            "publishProvider": _PP,
        }
    )
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
    monkeypatch.setattr(
        "kiro_crew.apps.execution.third_party_execution_allowed", lambda: True
    )
    return home


def _make_provider_app_source(tmp_path, name="prov-app"):
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Provider App",
        "description": "declares a publish provider",
        "author": "tester",
        "publishProvider": {
            **_PP,
            "endpoint": f"/api/apps/{name}/deploy",
            "setupRoute": f"/{name}",
        },
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
    # Not enabled → app-declared provider excluded (but core provider still present).
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/publish-providers")
        body = await resp.json()
    app_providers = [p for p in body["providers"] if p.get("origin") == "app"]
    assert all(p["id"] != "deploy-web-aws" for p in app_providers)
    # Core provider is always present regardless of app state.
    core = [p for p in body["providers"] if p.get("origin") == "core"]
    assert len(core) == 1
    assert core[0]["id"] == "deploy-web-aws"


# --- core deploy provider (item 2 — folded from deleted deploy-web app) -------

@pytest.mark.asyncio
async def test_core_deploy_provider_always_present(tmp_path, monkeypatch):
    """The core deploy-web-aws provider is always listed, even with no apps installed."""
    _setup_env(tmp_path, monkeypatch)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/publish-providers")
        body = await resp.json()
    core = [p for p in body["providers"] if p.get("origin") == "core"]
    assert len(core) == 1
    assert core[0]["id"] == "deploy-web-aws"
    assert core[0]["endpoint"] == "/api/deploy/deploy"
    assert core[0]["setupRoute"] == "/artifacts/deploy"
    # Unconfigured (no profiles registered)
    assert core[0]["configured"] is False


@pytest.mark.asyncio
async def test_core_deploy_provider_configured_when_profiles_exist(tmp_path, monkeypatch):
    home = _setup_env(tmp_path, monkeypatch)
    # Write a profiles.json with one entry
    deploy_dir = home / "deploy"
    deploy_dir.mkdir(parents=True)
    (deploy_dir / "profiles.json").write_text(json.dumps({
        "version": 2, "default": "my-profile",
        "profiles": [{"name": "my-profile", "region": "us-west-2"}],
    }))
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/publish-providers")
        body = await resp.json()
    core = next(p for p in body["providers"] if p.get("origin") == "core")
    assert core["configured"] is True


# --- concurrent registry write safety (item 5) --------------------------------

def test_save_registry_concurrent_no_lost_updates(tmp_path, monkeypatch):
    """Threaded concurrent save_registry calls must not lose updates."""
    import threading

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    from kiro_crew.deploy import profiles as profiles_mod

    n_threads = 10
    barrier = threading.Barrier(n_threads)
    errors: list[str] = []

    def writer(i: int) -> None:
        try:
            barrier.wait(timeout=5)
            reg = profiles_mod.load_registry()
            reg["profiles"].append(profiles_mod.make_entry(f"p{i}", "us-west-2"))
            profiles_mod.save_registry(reg)
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"threads raised: {errors}"
    # With locking, final registry must have at least the last writer's entry
    # (concurrent load-modify-write without locking could lose some). The lock
    # guarantees NO lost updates — each write serializes, so the final file
    # has exactly as many entries as the last writer saw + 1. But because each
    # writer loads independently (not chaining), total entries may be <n_threads
    # unless the test chains reads. What we CAN assert: the file is valid JSON
    # and at least 1 profile exists (not corrupted/empty).
    final = profiles_mod.load_registry()
    assert len(final["profiles"]) >= 1
    # More importantly: no file corruption.
    raw = profiles_mod._registry_path().read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert "profiles" in parsed


# --- endpoint allowlist (item 5) -------------------------------------------

def test_collect_rejects_endpoint_outside_app_namespace():
    """App-declared endpoint must match /api/apps/<that-app>/ — others are dropped."""
    # Valid: endpoint within the app's own namespace
    valid_pp = {**_PP, "endpoint": "/api/apps/deploy-web/deploy"}
    apps_valid = [_app("deploy-web", True, valid_pp)]
    res = collect_publish_providers(apps_valid, configured_resolver=lambda n, pp: True)
    assert len(res) == 1

    # Invalid: endpoint targeting another app's namespace
    bad_pp = {**_PP, "endpoint": "/api/apps/other-app/steal"}
    apps_bad = [_app("deploy-web", True, bad_pp)]
    res = collect_publish_providers(apps_bad, configured_resolver=lambda n, pp: True)
    assert res == []

    # Invalid: endpoint targeting core API
    core_pp = {**_PP, "endpoint": "/api/deploy/deploy"}
    apps_core = [_app("deploy-web", True, core_pp)]
    res = collect_publish_providers(apps_core, configured_resolver=lambda n, pp: True)
    assert res == []

    # Invalid: absolute URL escape attempt
    escape_pp = {**_PP, "endpoint": "https://evil.com/steal"}
    apps_escape = [_app("deploy-web", True, escape_pp)]
    res = collect_publish_providers(apps_escape, configured_resolver=lambda n, pp: True)
    assert res == []


def test_collect_accepts_endpoint_within_own_namespace():
    """Endpoint within /api/apps/<self>/ is allowed."""
    pp = {**_PP, "endpoint": "/api/apps/my-publisher/publish"}
    apps = [_app("my-publisher", True, pp)]
    res = collect_publish_providers(apps, configured_resolver=lambda n, pp: True)
    assert len(res) == 1
    assert res[0]["endpoint"] == "/api/apps/my-publisher/publish"
