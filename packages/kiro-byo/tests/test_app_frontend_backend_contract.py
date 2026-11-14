"""The embedded App UI and its real aiohttp routes must agree on model policy.

This is deliberately a local HTTP contract test, not a claim of live Crew UI
E2E coverage.  It verifies the same wire path/payload used by the UI, the
persisted setting and the data returned to the frontend after a reload.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession, web
from kiro_byo.credential_store import MemoryCredentialStore, ProviderSecrets, encode_provider_secrets
from kiro_byo.paths import use_app_home

ROOT = Path(__file__).resolve().parents[1]


def _load_app_routes():
    name = "_byo_app_wire_contract"
    package = types.ModuleType(name)
    package.__path__ = [str(ROOT)]
    backend = types.ModuleType(f"{name}.backend")
    backend.__path__ = [str(ROOT / "backend")]
    registry = types.ModuleType("kiro_crew.apps.route_registry")
    registry.AppRoute = lambda *args: args
    modules = {
        name: package, f"{name}.backend": backend,
        "kiro_crew": types.ModuleType("kiro_crew"),
        "kiro_crew.apps": types.ModuleType("kiro_crew.apps"),
        "kiro_crew.apps.route_registry": registry,
    }
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(f"{name}.backend.routes", ROOT / "backend" / "routes.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
            yield module
        finally:
            sys.modules.pop(spec.name, None)


def _write_fixture(home: Path) -> None:
    (home / "config").mkdir()
    (home / "config" / "providers.json").write_text(json.dumps({
        "mode": {"id": "tiga", "type": "multi"},
        "providers": [{
            "id": "demo", "name": "Demo", "type": "openai-compatible",
            "preset_id": "custom", "base_url": "https://example.invalid/v1",
            "credential_ref": "provider:demo",
            "models": [
                {"id": "openai/gpt-test", "name": "Toggle Model", "reasoning": {
                    "mode": "toggle", "supported": True, "can_disable": True,
                    "default_enabled": True, "enable_value": "enabled", "disable_value": "disabled"}},
                {"id": "fixed", "name": "Fixed Model", "reasoning": {
                    "mode": "fixed", "supported": True, "can_disable": False,
                    "default_enabled": True}},
            ],
        }],
    }), encoding="utf-8")


def test_embedded_frontend_thinking_round_trip_uses_backend_wire_contract(tmp_path, monkeypatch):
    _write_fixture(tmp_path)
    store = MemoryCredentialStore()
    store.set("provider:demo", encode_provider_secrets(ProviderSecrets(api_key="secret-never-expose")))
    ui = (ROOT / "ui/index.mjs").read_text(encoding="utf-8")
    assert "const API=`/api/apps/${APP}`" in ui
    assert "encodeURIComponent(row.model.id)}/thinking" in ui
    assert "body:JSON.stringify({mode})" in ui
    assert "await onReload()" in ui

    for routes in _load_app_routes():
        monkeypatch.setattr(routes, "_ensure_app_state", lambda ctx: tmp_path)
        monkeypatch.setattr(routes, "default_credential_store", lambda: store)
        monkeypatch.setattr(routes, "quick_integration_status", lambda app, home: {"installed": False})
        monkeypatch.setattr(routes, "_harness_state", lambda: {"selected_backend": "kiro-byo", "kiro_byo_selected": True})
        declared = {(route[0], route[1]) for route in routes.register_routes(types.SimpleNamespace(data_dir=tmp_path))}
        assert ("GET", "/status") in declared
        assert ("GET", "/providers") in declared
        assert ("PUT", "/providers/{provider_id}/models/{model_id}/thinking") in declared
        app = web.Application()
        ctx = types.SimpleNamespace(data_dir=tmp_path)
        app.router.add_get("/api/apps/kiro-byo/status", lambda req: routes.get_status(req, ctx))
        app.router.add_get("/api/apps/kiro-byo/providers", lambda req: routes.get_providers(req, ctx))
        app.router.add_put(
            "/api/apps/kiro-byo/providers/{provider_id}/models/{model_id}/thinking",
            lambda req: routes.update_model_thinking(req, ctx),
        )

        async def check():
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            try:
                port = site._server.sockets[0].getsockname()[1]
                url = f"http://127.0.0.1:{port}/api/apps/kiro-byo/providers"
                async with ClientSession() as session:
                    # Percent-encoded slash is important for many relay model IDs.
                    target = url + "/demo/models/openai%2Fgpt-test/thinking"
                    for preference in ("off", "on", "auto"):
                        async with session.put(target, json={"mode": preference}) as response:
                            assert response.status == 200, await response.text()
                            saved = (await response.json())["provider"]
                            assert saved["models"][0]["thinking_preference"] == preference
                            assert saved["models"][0]["capabilities"]["reasoning"]["thinking"]["can_disable"] is True
                            assert "secret-never-expose" not in json.dumps(saved)
                        async with session.get(url) as response:
                            assert response.status == 200
                            loaded = (await response.json())["providers"][0]["models"][0]
                            assert loaded["thinking_preference"] == preference
                        async with session.get(url.replace("/providers", "/status")) as response:
                            assert response.status == 200, await response.text()
                            body = await response.text()
                            status = json.loads(body)
                            assert status["providers"][0]["models"][0]["thinking_preference"] == preference
                            assert status["mode"]["pack"] == "tiga"
                            assert "secret-never-expose" not in body
                    # Invalid policies or unswitchable models must fail without a write.
                    for target_suffix, mode in (("fixed", "off"), ("openai%2Fgpt-test", "surprise")):
                        async with session.put(url + f"/demo/models/{target_suffix}/thinking", json={"mode": mode}) as response:
                            assert response.status == 400, await response.text()
                    # Malformed JSON is a client error, not an internal server crash.
                    async with session.put(target, data="{", headers={"Content-Type": "application/json"}) as response:
                        assert response.status == 400, await response.text()
                    # Unexpected exceptions cannot leak backend exception text
                    # through the JSON response consumed by the App UI.
                    def internal_failure(_ctx):
                        raise RuntimeError("secret-never-expose upstream failure")
                    original = routes._ensure_app_state
                    routes._ensure_app_state = internal_failure
                    try:
                        async with session.get(url) as response:
                            assert response.status == 500
                            assert "secret-never-expose" not in await response.text()
                    finally:
                        routes._ensure_app_state = original
            finally:
                await runner.cleanup()

        asyncio.run(check())
        with use_app_home(tmp_path):
            document = json.loads((tmp_path / "config/providers.json").read_text(encoding="utf-8"))
        assert document["providers"][0]["models"][0]["thinking_preference"] == "auto"
        assert "secret-never-expose" not in json.dumps(document)


def test_custom_local_connections_use_existing_provider_form_contract():
    """Relays and local servers share the custom preset; no new engine or key mandate."""
    from kiro_byo.provider_catalog import provider_preset

    preset = provider_preset("custom")
    assert preset is not None
    assert preset.category == "custom"
    assert preset.transport == "openai-compatible"
    assert preset.requires_api_key is False
    assert "本地模型" in preset.display_name
    ui = (ROOT / "ui/index.mjs").read_text(encoding="utf-8")
    assert "const isCustom=preset.id==='custom'" in ui
    assert "isCustom?h('label',{className:'ka-field'}" in ui
    assert "http://127.0.0.1:11434/v1" in ui
    assert "providers.custom_connection_hint" in ui
    assert "(isCustom&&!currentBase)" in ui
    for locale in ("zh-CN", "zh-TW", "ja", "ko", "en"):
        messages = json.loads((ROOT / "src/kiro_byo/locales" / f"{locale}.json").read_text(encoding="utf-8"))
        assert messages["providers.custom_connection_hint"]
