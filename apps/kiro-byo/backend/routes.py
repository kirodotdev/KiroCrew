"""KiroCrew App routes for Kiro BYO.

The app page is the preferred provider/API-key configuration surface.  Secrets
stay in the OS credential store; the routes only expose configured/missing
state.  The same App can also install the verified ACP integration overlay and
then asks KiroCrew to restart once.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Callable

from aiohttp import web
from kiro_crew.apps.route_registry import AppRoute

from ..src.kiro_byo.account_status import account_statuses
from ..src.kiro_byo.capabilities import capability_report
from ..src.kiro_byo.config import ConfigError
from ..src.kiro_byo.credential_store import CredentialStoreError, default_credential_store
from ..src.kiro_byo.discovery import DiscoveryError
from ..src.kiro_byo.integration_manager import (
    IntegrationError,
    install_from_app,
    integration_status,
    quick_integration_status,
    uninstall_from_app,
    migrate_legacy_data,
    running_crew,
)
from ..src.kiro_byo.native_tools import advertised_native_tool_names
from ..src.kiro_byo.overview_state import build_overview_snapshot
from ..src.kiro_byo.paths import use_app_home
from ..src.kiro_byo.provider_admin import (
    clear_provider_configuration,
    configure_provider,
    delete_provider,
    list_provider_presets,
    list_providers,
    migrate_plaintext_credentials,
    refresh_provider_models,
    mode_settings,
    set_mode_pack,
    set_model_thinking_preference,
    update_provider_credential,
)
from ..src.kiro_byo.telemetry import TurnTelemetryStore
from ..src.kiro_byo.runtime_trace import RuntimeTraceStore

APP_ROOT = Path(__file__).resolve().parent.parent


def _version() -> str:
    try:
        return (APP_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.9.13"




def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest().lower()
    except OSError:
        return ""


def _deployment_info() -> dict[str, Any]:
    """Describe the exact App bundle serving this backend/UI.

    Kiro BYO intentionally has two different installed surfaces: the KiroCrew
    App bundle (backend + Web UI) and the copied ACP runtime in Python
    site-packages.  Exposing the App root and UI fingerprint makes stale-copy
    problems diagnosable without guessing which local checkout KiroCrew loaded.
    """
    ui_entry = APP_ROOT / "ui" / "index.mjs"
    return {
        "app_root": str(APP_ROOT),
        "ui_entry": str(ui_entry),
        "ui_sha256": _sha256_file(ui_entry),
    }

def _data_dir(ctx: Any) -> Path:
    raw = getattr(ctx, "data_dir", None)
    if raw:
        return Path(raw).expanduser().resolve()
    return (APP_ROOT / "data" / "app").resolve()


def _ensure_app_state(ctx: Any) -> Path:
    home = _data_dir(ctx)
    home.mkdir(parents=True, exist_ok=True)
    try:
        _version_, _crew_package, site_packages = running_crew()
        migrate_legacy_data(home, site_packages)
    except IntegrationError:
        pass
    # If an older manual-install home was imported, sanitize/migrate it now.
    with use_app_home(home):
        try:
            migrate_plaintext_credentials(store=default_credential_store())
        except (ConfigError, CredentialStoreError):
            # Status should still load so the UI can explain/repair the failure.
            pass
    return home


def _ok(payload: Any, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status)


def _err(exc: Exception, status: int = 400) -> web.Response:
    return web.json_response({"error": str(exc)}, status=status)


def guarded(fn: Callable):
    async def wrapped(request: web.Request, ctx: Any) -> web.Response:
        try:
            return await fn(request, ctx)
        except (ConfigError, CredentialStoreError, DiscoveryError, IntegrationError, ValueError) as exc:
            return _err(exc, 400)
        except Exception as exc:  # pragma: no cover - host diagnostics path
            return _err(exc, 500)
    return wrapped


@guarded
async def get_status(request: web.Request, ctx: Any) -> web.Response:
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        store = default_credential_store()
        providers = await asyncio.to_thread(list_providers, store=store)
        telemetry = TurnTelemetryStore()
        diagnostics = await asyncio.to_thread(telemetry.summary, telemetry.max_records)
        mode = await asyncio.to_thread(mode_settings)
    # Keep the App shell fast: the full semantic integration audit scans Crew
    # source and built dashboard assets and belongs on the Integration page.
    integ = await asyncio.to_thread(quick_integration_status, APP_ROOT, home)
    runtime_capabilities = capability_report()
    native_tools = advertised_native_tool_names()
    overview = build_overview_snapshot(
        version=_version(),
        app_home=str(home),
        credential_backend=store.backend_name,
        providers=providers,
        runtime_capabilities=runtime_capabilities,
        native_tools=native_tools,
        integration=integ,
        diagnostics=diagnostics,
    )
    return _ok({
        "ready": True,
        "version": _version(),
        "target_client": "KiroCrew",
        "app_home": str(home),
        "deployment": _deployment_info(),
        "credential_backend": store.backend_name,
        "providers": providers,
        "provider_count": len(providers),
        "mode": mode,
        "runtime_capabilities": runtime_capabilities,
        "backend_card": {
            "source": "KiroCrew",
            "note": "KiroCrew Settings → Agent Harness is authoritative for backend parity.",
        },
        "native_tools": native_tools,
        "integration": integ,
        "diagnostics": diagnostics,
        "overview": overview,
    })


@guarded
async def get_presets(request: web.Request, ctx: Any) -> web.Response:
    return _ok({"presets": list_provider_presets()})


@guarded
async def get_providers(request: web.Request, ctx: Any) -> web.Response:
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        providers = await asyncio.to_thread(list_providers, store=default_credential_store())
    return _ok({"providers": providers})


@guarded
async def save_provider(request: web.Request, ctx: Any) -> web.Response:
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    home = _ensure_app_state(ctx)
    manual_raw = body.get("models") or []
    if isinstance(manual_raw, str):
        manual_models = [part.strip() for part in manual_raw.replace("\n", ",").split(",") if part.strip()]
    elif isinstance(manual_raw, list):
        manual_models = [str(x).strip() for x in manual_raw if str(x).strip()]
    else:
        raise ValueError("models must be an array or comma-separated string")
    with use_app_home(home):
        provider = await asyncio.to_thread(
            configure_provider,
            base_url=str(body.get("base_url") or ""),
            api_key=str(body.get("api_key") or ""),
            provider_name=str(body.get("name") or ""),
            preset_id=str(body.get("preset_id") or "custom"),
            manual_models=manual_models or None,
            instance_id=str(body.get("instance_id") or ""),
            create_new=bool(body.get("create_new", False)),
            store=default_credential_store(),
        )
    return _ok({"provider": provider})


def _provider_id(request: web.Request) -> str:
    value = str(request.match_info.get("provider_id") or "").strip()
    if not value:
        raise ValueError("provider_id is required")
    return value


@guarded
async def replace_credential(request: web.Request, ctx: Any) -> web.Response:
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("api_key"), str) or not body.get("api_key"):
        raise ValueError("api_key is required")
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        provider = await asyncio.to_thread(
            update_provider_credential,
            _provider_id(request),
            body["api_key"],
            store=default_credential_store(),
        )
    return _ok({"provider": provider})


@guarded
async def refresh_models(request: web.Request, ctx: Any) -> web.Response:
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        provider = await asyncio.to_thread(
            refresh_provider_models,
            _provider_id(request),
            store=default_credential_store(),
        )
    return _ok({"provider": provider})


@guarded
async def update_model_thinking(request: web.Request, ctx: Any) -> web.Response:
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    provider_id = _provider_id(request)
    model_id = str(request.match_info.get("model_id") or "").strip()
    if not model_id:
        raise ValueError("model_id is required")
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        provider = await asyncio.to_thread(
            set_model_thinking_preference,
            provider_id,
            model_id,
            str(body.get("mode") or body.get("preference") or ""),
            store=default_credential_store(),
        )
    return _ok({"provider": provider})


@guarded
async def update_mode(request: web.Request, ctx: Any) -> web.Response:
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        mode = await asyncio.to_thread(set_mode_pack, str(body.get("mode") or ""))
    return _ok({"mode": mode})


@guarded
async def remove_provider(request: web.Request, ctx: Any) -> web.Response:
    home = _ensure_app_state(ctx)
    provider_id = _provider_id(request)
    with use_app_home(home):
        await asyncio.to_thread(delete_provider, provider_id, store=default_credential_store())
    return _ok({"deleted": provider_id})


@guarded
async def get_accounts(request: web.Request, ctx: Any) -> web.Response:
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        store = default_credential_store()
        providers = await asyncio.to_thread(list_providers, store=store)
        accounts = await asyncio.to_thread(account_statuses, providers, store)
    errors = sum(1 for row in accounts if row.get("state") == "error")
    ok = sum(1 for row in accounts if row.get("state") == "ok")
    return _ok({
        "accounts": accounts,
        "summary": {
            "configured_providers": len(providers),
            "capable_providers": len(accounts),
            "ok": ok,
            "errors": errors,
        },
    })


@guarded
async def get_diagnostics(request: web.Request, ctx: Any) -> web.Response:
    home = _ensure_app_state(ctx)
    try:
        limit = max(1, min(int(request.query.get("limit", "30")), 100))
    except ValueError:
        limit = 30
    with use_app_home(home):
        store = TurnTelemetryStore()
        turns = await asyncio.to_thread(store.recent, limit)
        summary = await asyncio.to_thread(store.summary, store.max_records)
    return _ok({"summary": summary, "turns": turns, "retention_limit": store.max_records})


@guarded
async def clear_diagnostics(request: web.Request, ctx: Any) -> web.Response:
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        removed = await asyncio.to_thread(TurnTelemetryStore().clear)
    return _ok({"cleared": True, "removed": removed})




@guarded
async def get_runtime_trace(request: web.Request, ctx: Any) -> web.Response:
    home = _ensure_app_state(ctx)
    try:
        limit = max(1, min(int(request.query.get("limit", "100")), 500))
    except ValueError:
        limit = 100
    session_id = str(request.query.get("session_id", "") or "").strip()
    with use_app_home(home):
        store = RuntimeTraceStore()
        events = await asyncio.to_thread(store.recent, limit, session_id=session_id)
        status = store.status()
    return _ok({"status": status, "events": events})


@guarded
async def clear_runtime_trace(request: web.Request, ctx: Any) -> web.Response:
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        removed = await asyncio.to_thread(RuntimeTraceStore().clear)
    return _ok({"cleared": True, "removed": removed})

@guarded
async def get_integration(request: web.Request, ctx: Any) -> web.Response:
    home = _ensure_app_state(ctx)
    return _ok(await asyncio.to_thread(integration_status, APP_ROOT, home))


@guarded
async def install_integration(request: web.Request, ctx: Any) -> web.Response:
    home = _ensure_app_state(ctx)
    result = await asyncio.to_thread(install_from_app, APP_ROOT, home, version=_version())
    verified = await asyncio.to_thread(integration_status, APP_ROOT, home)
    if not verified.get("installed"):
        pending = verified.get("pending_files") or []
        detail = "; ".join(
            f"{row.get('file')}: {row.get('state')} ({row.get('detail')})"
            for row in pending[:4]
            if isinstance(row, dict)
        )
        raise IntegrationError(
            "integration write returned but post-install verification is still incomplete"
            + (f": {detail}" if detail else "")
        )
    result["verified_status"] = verified
    return _ok(result)



@guarded
async def uninstall_integration(request: web.Request, ctx: Any) -> web.Response:
    home = _ensure_app_state(ctx)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    purge_data = bool(body.get("purge_data", False))

    result = await asyncio.to_thread(uninstall_from_app, APP_ROOT, home)
    if purge_data:
        # Refuse to purge when the data home resolved to the in-bundle fallback
        # (ctx.data_dir unset): a recursive delete there would wipe bundled app
        # files rather than a dedicated per-user data directory.
        if home.resolve() == (APP_ROOT / "data" / "app").resolve():
            raise IntegrationError(
                "integration restored, but refusing to purge: the app data home "
                "resolved inside the app bundle. Configure a dedicated data "
                "directory before purging Kiro BYO data."
            )
        with use_app_home(home):
            cleared = await asyncio.to_thread(clear_provider_configuration, store=default_credential_store())
            await asyncio.to_thread(TurnTelemetryStore().clear)
        # The App's data directory is dedicated to Kiro BYO. After Crew has
        # been restored and OS credentials removed, clear all remaining local
        # snapshots/settings so uninstalling the App leaves no Kiro BYO state.
        for child in list(home.iterdir()) if home.exists() else []:
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)
            except OSError as exc:
                raise IntegrationError(f"integration restored, but failed to purge app data {child}: {exc}") from exc
        result["data_purged"] = True
        result["purged_providers"] = cleared.get("providers", 0)
        result["purged_credentials"] = cleared.get("credentials", 0)
    else:
        result["data_purged"] = False
    return _ok(result)

def register_routes(ctx: Any) -> list[AppRoute]:
    _ensure_app_state(ctx)
    return [
        AppRoute("GET", "/status", get_status),
        AppRoute("GET", "/presets", get_presets),
        AppRoute("GET", "/providers", get_providers),
        AppRoute("POST", "/providers", save_provider),
        AppRoute("PUT", "/providers/{provider_id}/credential", replace_credential),
        AppRoute("POST", "/providers/{provider_id}/refresh", refresh_models),
        AppRoute("PUT", "/providers/{provider_id}/models/{model_id}/thinking", update_model_thinking),
        AppRoute("PUT", "/mode", update_mode),
        AppRoute("DELETE", "/providers/{provider_id}", remove_provider),
        AppRoute("GET", "/accounts", get_accounts),
        AppRoute("GET", "/diagnostics", get_diagnostics),
        AppRoute("DELETE", "/diagnostics", clear_diagnostics),
        AppRoute("GET", "/runtime-trace", get_runtime_trace),
        AppRoute("DELETE", "/runtime-trace", clear_runtime_trace),
        AppRoute("GET", "/integration", get_integration),
        AppRoute("POST", "/integration/install", install_integration),
        AppRoute("POST", "/integration/uninstall", uninstall_integration),
    ]
