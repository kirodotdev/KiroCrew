"""KiroCrew App routes for Kiro BYO.

The app page is the preferred provider/API-key configuration surface.  Secrets
stay in the OS credential store; the routes only expose configured/missing
state.  The same App can also install the verified ACP integration overlay and
then asks KiroCrew to restart once.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
from pathlib import Path
from typing import Any, Callable

from aiohttp import web
from kiro_crew.apps.route_registry import AppRoute

from ..src.kiro_byo.account_status import account_statuses
from ..src.kiro_byo.i18n import locale_messages, normalize_ui_language, error_code
from ..src.kiro_byo.capabilities import capability_report
from ..src.kiro_byo.config import ConfigError, load_config
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
    probe_provider_connection,
    delete_provider,
    list_provider_presets,
    list_providers,
    migrate_plaintext_credentials,
    refresh_provider_models,
    mode_settings,
    reasoning_language_settings, response_language_settings, ui_language_settings,
    set_response_language_preference, set_ui_language_preference,
    set_mode_pack,
    set_mode_selection,
    set_model_thinking_preference,
    set_reasoning_language_preference,
    update_provider_credential,
)
from ..src.kiro_byo.telemetry import TurnTelemetryStore
from ..src.kiro_byo.runtime_trace import RuntimeTraceStore
from ..src.kiro_byo.durable_runs import DurableRunStore
from ..src.kiro_byo.record_center import records_snapshot
from ..src.kiro_byo.acp_conformance import conformance_report
from ..src.kiro_byo.routing_dashboard import build_routing_dashboard
from ..src.kiro_byo.crew_linkage import (backend_id, check_member_sync, linkage_snapshot, linkage_change_plan, model_pin_allowed, role_pin, background_execution_snapshot)
from ..src.kiro_byo.mode_registry import load_mode_registry
from ..src.kiro_byo.protocol import parse_type_selection, type_selection_id

APP_ROOT = Path(__file__).resolve().parent.parent


def _version() -> str:
    try:
        return (APP_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"




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


def _harness_state() -> dict[str, Any]:
    """Report the harness KiroCrew will use for newly created sessions.

    Integration installation and harness selection are deliberately separate
    concerns in KiroCrew.  Expose both so the App cannot report "ready" while
    the model picker is still correctly serving the official Kiro catalogue.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig
        cfg = KiroCrewConfig.load()
        selected = str(getattr(cfg.agent, "acp_backend", "") or "").strip()
        return {
            "selected_backend": selected or "kiro",
            "kiro_byo_selected": selected == "kiro-byo",
            "readable": True,
        }
    except Exception as exc:  # pragma: no cover - host diagnostic path
        return {
            "selected_backend": "unknown",
            "kiro_byo_selected": False,
            "readable": False,
            "error": str(exc),
        }


def _activate_kiro_byo_harness_sync() -> dict[str, Any]:
    """Select Kiro BYO through KiroCrew's locked config writer.

    This is intentionally an explicit user action, never a side effect of
    Integration install/update.  Existing sessions keep the backend they were
    created with; the selection controls newly created sessions.
    """
    from kiro_crew.config.loader import config_path as crew_config_path, update_config_locked

    def mutate(document: dict) -> dict:
        agent = document.get("agent")
        if not isinstance(agent, dict):
            agent = {}
            document["agent"] = agent
        agent["acp_backend"] = "kiro-byo"
        return document

    update_config_locked(crew_config_path(), mutate=mutate)
    state = _harness_state()
    if not state.get("kiro_byo_selected"):
        detail = state.get("error") or state.get("selected_backend") or "unknown"
        raise IntegrationError(
            "KiroCrew config write completed but Kiro BYO is not the effective Agent Harness "
            f"({detail}); check config.local.json or another higher-priority override"
        )
    return state


def _ok(payload: Any, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status)


def _err(exc: Exception, status: int = 400) -> web.Response:
    message=str(exc)
    code=error_code(message)
    return web.json_response({"error": message, **({"code": code} if code else {})}, status=status)


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
        reasoning_language = await asyncio.to_thread(reasoning_language_settings)
        response_language = await asyncio.to_thread(response_language_settings)
        ui_language = await asyncio.to_thread(ui_language_settings)
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
        "reasoning_language": reasoning_language,
        "response_language": response_language,
        "ui_language": ui_language,
        "runtime_capabilities": runtime_capabilities,
        "backend_card": {
            "source": "KiroCrew",
            "note": "KiroCrew Settings → Agent Harness is authoritative for backend parity.",
        },
        "harness": _harness_state(),
        "native_tools": native_tools,
        "integration": integ,
        "diagnostics": diagnostics,
        "overview": overview,
    })



@guarded
async def get_mode_sync(request: web.Request, ctx: Any) -> web.Response:
    """Read-only comparison of App mode and the host model catalog's source.

    /api/models runs in KiroCrew's agents handler, while the BYO App writes
    ctx.data_dir. Different config roots or an older host patch can therefore
    make the UI show Dyna and the chat picker offer only Tiga. Report evidence
    without touching sessions, secrets, or model selections.
    """
    from ..src.kiro_byo.config import load_catalog_config
    from ..src.kiro_byo.paths import use_app_home as scoped_home

    home = _data_dir(ctx)
    app_path = home / "config" / "providers.json"
    result: dict[str, Any] = {
        "app_config_exists": app_path.is_file(),
        "app_config_sha256": _sha256_file(app_path),
        "app_config_path": str(app_path),
        "host": _harness_state(),
        "app_bundle": _deployment_info(),
        "catalogue_comparison": "unavailable",
    }
    try:
        from kiro_crew.apps.manager import app_dir
        crew_path = app_dir("kiro-byo") / "data" / "config" / "providers.json"
        result["crew_catalog_path"] = str(crew_path)
        result["crew_catalog_exists"] = crew_path.is_file()
        result["crew_catalog_sha256"] = _sha256_file(crew_path)
        result["same_config_file"] = app_path.resolve() == crew_path.resolve()
        with scoped_home(home):
            app_cfg = await asyncio.to_thread(load_catalog_config, app_path)
        with scoped_home(crew_path.parents[1]):
            crew_cfg = await asyncio.to_thread(load_catalog_config, crew_path)
        result["app_mode"] = {
            "pack": str(app_cfg.mode_pack or "").lower(),
            "type": str(app_cfg.mode_type or "").lower(),
            "models_present": bool(app_cfg.models),
        }
        result["crew_catalog_mode"] = {
            "pack": str(crew_cfg.mode_pack or "").lower(),
            "type": str(crew_cfg.mode_type or "").lower(),
            "models_present": bool(crew_cfg.models),
        }
        same = result["app_mode"] == result["crew_catalog_mode"]
        result["catalogue_comparison"] = "match" if same else "mismatch"
        # These are immutable-on-read markers, not a claim that the browser
        # actually loaded this HTML or that its React Query state is current.
        _, _, site_packages = running_crew()
        crew_package = Path(site_packages) / "kiro_crew"
        agents = crew_package / "dashboard" / "handlers" / "agents.py"
        index = crew_package / "static" / "dist" / "index.html"
        agent_source = agents.read_text(encoding="utf-8", errors="replace") if agents.is_file() else ""
        index_source = index.read_text(encoding="utf-8", errors="replace") if index.is_file() else ""
        result["installed_host_patches"] = {
            "byo_catalogue_handler": "async def _kiro_byo_models(" in agent_source,
            "mode_watch_script": "kiro-byo:mode-refresh-attempt:v3" in index_source,
            "dashboard_label_marker": "kiro-byo:crew11-type-labels:v1" in index_source,
        }
    except Exception as exc:
        result["probe_error"] = type(exc).__name__
    return _ok(result)

@guarded
async def get_presets(request: web.Request, ctx: Any) -> web.Response:
    return _ok({"presets": list_provider_presets()})


@guarded
async def get_routing_dashboard(request: web.Request, ctx: Any) -> web.Response:
    """Read-only active Mode adaptive-routing observability for the Modes page."""
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        store = default_credential_store()
        config = await asyncio.to_thread(load_config, store)
        payload = await asyncio.to_thread(
            build_routing_dashboard,
            config,
            telemetry=TurnTelemetryStore(),
            trace=RuntimeTraceStore(),
        )
    return _ok(payload)


@guarded
async def get_providers(request: web.Request, ctx: Any) -> web.Response:
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        providers = await asyncio.to_thread(list_providers, store=default_credential_store())
    return _ok({"providers": providers})

@guarded
async def test_provider(request: web.Request, ctx: Any) -> web.Response:
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        result = await asyncio.to_thread(
            probe_provider_connection,
            base_url=str(body.get("base_url") or ""),
            api_key=str(body.get("api_key") or ""),
            preset_id=str(body.get("preset_id") or "custom"),
        )
    return _ok(result)


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
async def get_locale(request: web.Request, ctx: Any) -> web.Response:
    locale = str(request.match_info.get("locale") or "")
    if locale not in ("zh-CN", "zh-TW", "en", "ja", "ko"):
        raise ValueError("unsupported UI locale")
    return _ok({"locale": locale, "messages": locale_messages(locale)})


@guarded
async def update_ui_language(request: web.Request, ctx: Any) -> web.Response:
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        settings = await asyncio.to_thread(set_ui_language_preference, str(body.get("value") or "auto"))
    return _ok(settings)


@guarded
async def update_response_language(request: web.Request, ctx: Any) -> web.Response:
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        settings = await asyncio.to_thread(set_response_language_preference, str(body.get("value") or "auto"))
    return _ok(settings)


@guarded
async def update_reasoning_language(request: web.Request, ctx: Any) -> web.Response:
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        settings = await asyncio.to_thread(
            set_reasoning_language_preference,
            str(body.get("value") or body.get("language") or "auto"),
        )
    return _ok({"reasoning_language": settings})


async def _reconcile_mode_change_with_crew(request: web.Request, pack: str = "") -> tuple[str, int]:
    """Notify cold Crew slots after a committed BYO mode change.

    A running ACP session keeps its existing routing plan. This is best-effort:
    the App may run without the patched Crew dashboard, so a missing host seam
    must never undo an already committed BYO configuration update.
    """
    try:
        state = request.app.get("state")
        if state is None:
            return "host_unavailable", 0
        from kiro_crew.dashboard import chat_handlers
        reconcile = getattr(chat_handlers, "_kiro_byo_reconcile_slot_routing", None)
        if not callable(reconcile):
            return "host_unavailable", 0
        changed = await reconcile(state)
        # Never report success when real slots still carry the previous Mode.
        # In-flight/linked sessions intentionally defer, so this is a pending
        # count, not an error and not permission to reset them unsafely.
        outstanding = 0
        if pack:
            for slot in list(getattr(state, "_slots", {}).values()):
                model = str(getattr(slot, "model", "") or "").strip().lower()
                parts = model.split(":")
                if len(parts) == 3 and parts[0] == "kiro-byo-type" and parts[1] != pack:
                    outstanding += 1
        return ("pending" if outstanding else "updated" if changed else "unchanged"), outstanding
    except Exception:
        # The host integration must remain non-authoritative over mode config,
        # but do not hide a failed cross-surface sync from diagnostics.
        logging.getLogger(__name__).warning(
            "Kiro BYO mode saved, but Crew cold-slot synchronization failed", exc_info=True
        )
        return "host_sync_unavailable", 0


@guarded
async def update_mode(request: web.Request, ctx: Any) -> web.Response:
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        pack = str(body.get("mode") or body.get("pack") or "").strip()
        policy = str(body.get("type") or body.get("policy") or "").strip()
        if policy:
            mode = await asyncio.to_thread(set_mode_selection, policy, pack)
        else:
            mode = await asyncio.to_thread(set_mode_pack, pack)
    host_sync, pending_slots = await _reconcile_mode_change_with_crew(
        request, str(mode.get("pack") or "").lower()
    )
    return _ok({"mode": mode, "host_sync": host_sync, "host_sync_pending_slots": pending_slots})


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
        "providers": [
            {
                "id": str(row.get("id") or ""),
                "name": str(row.get("name") or row.get("id") or ""),
                "base_url": str(row.get("base_url") or ""),
            }
            for row in providers
        ],
        "summary": {
            "configured_providers": len(providers),
            "capable_providers": len(accounts),
            "ok": ok,
            "errors": errors,
        },
    })


def _known_local_agent_override(config_file: Path, *keys: str) -> bool:
    """Refuse writes shadowed by Crew's documented config.local.json layer.

    This is not claimed to discover every environment/runtime override.
    """
    import json

    local_path = config_file.with_name("config.local.json")
    if not local_path.is_file():
        return False
    try:
        document = json.loads(local_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IntegrationError("Cannot safely inspect Crew config.local.json") from exc
    if not isinstance(document, dict):
        raise IntegrationError("Crew config.local.json is not an object")
    current = document.get("agent", {})
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]
    return True


def _crew_linkage_sync() -> dict[str, Any]:
    from kiro_crew.config.loader import KiroCrewConfig

    declared = None
    try:
        from kiro_crew.config.loader_document import member_acp_backend_declared
        declared = bool(member_acp_backend_declared())
    except Exception:
        # Diagnostics stay available even if Crew moves the declaration helper;
        # writes remain capability-gated below.
        declared = None
    return linkage_snapshot(KiroCrewConfig.load().agent, member_declared=declared)


def _linkage_write_supported() -> bool:
    """Whether this installed Crew can run the full Mate 2.0 kit on BYO."""
    try:
        from kiro_crew.agent_sdk.backends import (
            ACP_BACKENDS_MEMBER_DISPATCH, ACP_BACKENDS_MATE_WORKER_KIT, ACP_BACKEND_LAUNCH,
        )
        return (
            "kiro-byo" in ACP_BACKENDS_MEMBER_DISPATCH
            and "kiro-byo" in ACP_BACKENDS_MATE_WORKER_KIT
            and "kiro-byo" in ACP_BACKEND_LAUNCH
        )
    except (ImportError, AttributeError, TypeError):
        return False


def _require_linkage_write_supported() -> None:
    if not _linkage_write_supported():
        raise IntegrationError(
            "This Crew build has not verified Kiro BYO for Mate 2.0 Conductor/Worker projection; "
            "backend linkage writes are disabled."
        )


@guarded
async def get_task_linkage(request: web.Request, ctx: Any) -> web.Response:
    """Show effective defaults for future Crew work, not active-session claims."""
    _ensure_app_state(ctx)
    linkage = await asyncio.to_thread(_crew_linkage_sync)
    try:
        main, members = _registered_linkage_backends()
    except (ImportError, AttributeError, TypeError):
        main, members = set(), set()
    supported = _linkage_write_supported()
    linkage["mate_2_0"] = {
        "scope": "conductor_worker",
        "runtime_backend": linkage.get("members", {}).get("configured_backend", "unknown"),
        "byo_worker_kit": supported,
    }
    return _ok({
        "linkage": linkage,
        "available_main": sorted(main),
        "available_members": sorted(members) if supported else [],
        "linkage_write_supported": supported,
    })


@guarded
async def sync_member_backend(request: web.Request, ctx: Any) -> web.Response:
    """Explicitly align Crew's shared member backend with its main default.

    No global follow hook is installed and no existing sessions are restarted.
    Optimistic preconditions guard against edits made in the Crew UI between
    inspection and this user-confirmed action.
    """
    _ensure_app_state(ctx)
    _require_linkage_write_supported()
    body = await request.json()
    if not isinstance(body, dict) or set(body) != {"expected_main", "expected_member"}:
        raise ValueError("expected_main and expected_member are required")
    expected_main = str(body["expected_main"]).strip()
    expected_member = str(body["expected_member"]).strip()

    def _sync() -> dict[str, Any]:
        from kiro_crew.config.loader import KiroCrewConfig, config_path, update_config_locked
        from kiro_crew.agent_sdk.backends import ACP_BACKENDS_MEMBER_DISPATCH

        before = _crew_linkage_sync()
        target = check_member_sync(
            expected_main, expected_member,
            before["main"]["configured_backend"], before["members"]["configured_backend"],
        )
        if target != "kiro-byo":
            raise IntegrationError("member follow-sync is only needed when the main harness is Kiro BYO")
        if target not in ACP_BACKENDS_MEMBER_DISPATCH:
            raise IntegrationError(f"Crew has not registered {target!r} for member dispatch")
        if before["members"]["configured_backend"] == target and before["members"].get("declared") is not True:
            return {"linkage": before, "changed": False}
        if _known_local_agent_override(config_path(), "member_acp_backend"):
            raise IntegrationError("Crew config.local.json overrides member_acp_backend; edit it in Crew first")

        # Recheck inside the locked write. A higher-priority config overlay is
        # detected before writing; don't claim that writing was effective.
        def mutate(doc: dict) -> dict:
            agent = doc.get("agent")
            if not isinstance(agent, dict):
                agent = {}
                doc["agent"] = agent
            original_main = backend_id(agent.get("acp_backend"))
            if original_main != target:
                raise ValueError("Crew main backend changed; refresh before syncing")
            # Follow is represented by absence, not an explicit BYO pin.
            agent.pop("member_acp_backend", None)
            return doc

        update_config_locked(config_path(), mutate=mutate)
        after = _crew_linkage_sync()
        # A config.local/environment override may obscure the write. Surface
        # the real effective value rather than lying about a successful sync.
        return {"linkage": after, "changed": True, "effective": after["members"]["configured_backend"] == target}

    return _ok(await asyncio.to_thread(_sync))


def _registered_linkage_backends() -> tuple[set[str], set[str]]:
    """Read current Crew capabilities; full Mate backends need the worker kit."""
    from kiro_crew.agent_sdk.backends import (
        ACP_BACKENDS_KNOWN, ACP_BACKENDS_MEMBER_DISPATCH, ACP_BACKENDS_MATE_WORKER_KIT,
        ACP_BACKEND_LAUNCH,
    )

    if not isinstance(ACP_BACKEND_LAUNCH, dict):
        raise ValueError("Crew backend launch registry is unavailable")
    known = {backend_id(item) for item in ACP_BACKENDS_KNOWN}
    launched = {backend_id(item) for item in ACP_BACKEND_LAUNCH}
    main = known & (launched | {"kiro"})
    members = (
        {backend_id(item) for item in ACP_BACKENDS_MEMBER_DISPATCH}
        & {backend_id(item) for item in ACP_BACKENDS_MATE_WORKER_KIT}
        & main
    )
    return main, members


async def _linkage_plan_from_request(request: web.Request, ctx: Any, body: dict) -> dict[str, Any]:
    _ensure_app_state(ctx)
    _require_linkage_write_supported()
    allowed = {"target_main", "member_mode", "fixed_member"}
    if not isinstance(body, dict) or not set(body) <= allowed:
        raise ValueError("invalid linkage switch request")
    if "target_main" not in body or "member_mode" not in body:
        raise ValueError("target_main and member_mode are required")
    for key in body:
        if not isinstance(body[key], str):
            raise ValueError("linkage fields must be strings")
    main_backends, member_backends = _registered_linkage_backends()
    snapshot = await asyncio.to_thread(_crew_linkage_sync)
    service = _cron_service(request)
    jobs = await service.list_jobs_async(include_disabled=True)
    rows = [
        {"id": str(getattr(job, "id", "") or ""),
         "model": str(getattr(job, "model", "") or ""),
         "execution_kind": _task_execution_kind(job)}
        for job in jobs
    ]
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        config = await asyncio.to_thread(load_config, default_credential_store())
        known_models = {public_id for public_id, _ in config.public_models}
    plan = linkage_change_plan(
        snapshot, target_main=body["target_main"], member_mode=body["member_mode"],
        fixed_member=body.get("fixed_member", ""), main_backends=main_backends,
        member_backends=member_backends, cron_models=rows, byo_models=known_models,
    )
    # Snapshot binds the preview to the current actual Crew defaults and pinned
    # Cron model set, not to a rendered UI label. Never echo private job contents.
    import hashlib
    import json

    facts = {"plan": plan, "cron_models": sorted(rows, key=lambda x: (x["id"], x["model"]))}
    plan["preview_id"] = hashlib.sha256(json.dumps(facts, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()
    plan["available_main"] = sorted(main_backends)
    plan["available_members"] = sorted(member_backends)
    return plan


@guarded
async def preview_linkage_switch(request: web.Request, ctx: Any) -> web.Response:
    body = await request.json()
    return _ok({"preview": await _linkage_plan_from_request(request, ctx, body)})


@guarded
async def apply_linkage_switch(request: web.Request, ctx: Any) -> web.Response:
    """One atomic config update, with optimistic replay and rollback on mismatch.

    Only agent.acp_backend and agent.member_acp_backend are touched. No hook,
    background auto-follow daemon, job edit, session restart or model pin edit.
    """
    body = await request.json()
    if not isinstance(body, dict) or set(body) - {"target_main", "member_mode", "fixed_member", "preview_id"}:
        raise ValueError("invalid linkage switch request")
    token = body.get("preview_id")
    if not isinstance(token, str) or len(token) != 64:
        raise ValueError("preview_id is required; preview first")
    plan = await _linkage_plan_from_request(request, ctx, {k: v for k, v in body.items() if k != "preview_id"})
    if token != plan["preview_id"]:
        raise ValueError("Crew configuration or job pins changed; refresh the preview")

    def _write() -> dict[str, Any]:
        from kiro_crew.config.loader import KiroCrewConfig, config_path, update_config_locked

        path = config_path()
        if _known_local_agent_override(path, "acp_backend") or _known_local_agent_override(path, "member_acp_backend"):
            raise IntegrationError("Crew config.local.json overrides an ACP backend; edit it in Crew")
        fresh = _crew_linkage_sync()
        if (fresh["main"]["configured_backend"] != plan["before"]["main"] or
                fresh["members"]["configured_backend"] != plan["before"]["member"] or
                (plan.get("before_member_declared") is not None and
                 fresh["members"].get("declared") != plan.get("before_member_declared"))):
            raise ValueError("Crew defaults changed after preview")
        if not plan["changes"]:
            return {"changed": False, "effective": True, "linkage": fresh}
        old = {}
        to_write = {"acp_backend": "" if plan["after"]["main"] == "kiro" else plan["after"]["main"],
                    "member_acp_backend": "" if plan["after"]["member"] == "kiro" else plan["after"]["member"]}
        clear_member_override = plan.get("member_action") == "inherit"
        # Both fields are checked and written under ONE Crew config lock.
        def mutate(doc: dict) -> dict:
            agent = doc.get("agent")
            if not isinstance(agent, dict):
                agent = {}
                doc["agent"] = agent
            # Effective values were rechecked immediately before this locked
            # write. Preserve raw fields for rollback, then apply only planned
            # keys. Follow clears the member override instead of pinning BYO.
            for key, value in to_write.items():
                old[key] = (key in agent, agent.get(key))
                if key == "acp_backend" and "agent.acp_backend" not in plan["changes"]:
                    continue
                if key == "member_acp_backend" and "agent.member_acp_backend" not in plan["changes"]:
                    continue
                if key == "member_acp_backend" and clear_member_override:
                    agent.pop(key, None)
                else:
                    agent[key] = value
            return doc
        update_config_locked(path, mutate=mutate)
        result = _crew_linkage_sync()
        if result["main"]["configured_backend"] == plan["after"]["main"] and result["members"]["configured_backend"] == plan["after"]["member"]:
            return {"changed": True, "effective": True, "linkage": result}
        # A higher-priority or asynchronous change may have obscured the write.
        # Roll back only exact values we wrote; never overwrite a concurrent edit.
        def rollback(doc: dict) -> dict:
            agent = doc.get("agent")
            if not isinstance(agent, dict):
                raise ValueError("Crew config changed before safe rollback")
            for key, value in to_write.items():
                if key not in old:
                    continue
                if ((key == "acp_backend" and "agent.acp_backend" not in plan["changes"]) or
                        (key == "member_acp_backend" and "agent.member_acp_backend" not in plan["changes"])):
                    continue
                if key == "member_acp_backend" and clear_member_override:
                    if key in agent:
                        raise ValueError("Concurrent edit detected; automatic rollback refused")
                elif agent.get(key) != value:
                    raise ValueError("Concurrent edit detected; automatic rollback refused")
            for key, (present, original) in old.items():
                if ((key == "acp_backend" and "agent.acp_backend" not in plan["changes"]) or
                        (key == "member_acp_backend" and "agent.member_acp_backend" not in plan["changes"])):
                    continue
                if present:
                    agent[key] = original
                else:
                    agent.pop(key, None)
            return doc
        try:
            update_config_locked(path, mutate=rollback)
        except Exception as exc:
            raise IntegrationError("Crew effective backend mismatch; automatic rollback could not be verified; inspect Crew config") from exc
        raise IntegrationError("Crew effective backend mismatch; configuration rolled back")

    return _ok(await asyncio.to_thread(_write))


def _cron_service(request: web.Request) -> Any:
    try:
        state = request.app["state"]
        service = getattr(state, "crons", None)
    except Exception as exc:
        raise IntegrationError("KiroCrew cron service is unavailable") from exc
    if service is None:
        raise IntegrationError("KiroCrew cron service is unavailable")
    return service


def _task_execution_kind(job: Any) -> str:
    """Cron Script/Command do not run an ACP model (Crew-owned fields)."""
    if str(getattr(job, "script", "") or "").strip():
        return "script"
    if str(getattr(job, "command", "") or "").strip():
        return "command"
    return "agent"


def _require_byo_in_locked_document(document: dict, config_file: Path) -> None:
    """Recheck the main Harness inside Crew's locked config mutation.

    A preflight ``KiroCrewConfig.load`` may become stale while the request is
    validating BYO model choices.  The document passed to ``mutate`` is the
    latest locked state, so a concurrent main-backend switch must abort here.
    Config.local.json overrides are deliberately rejected: writing the base
    document while a higher-priority mode is active is never safe.
    """
    if _known_local_agent_override(config_file, "acp_backend"):
        raise IntegrationError(
            "Crew config.local.json overrides agent.acp_backend; "
            "BYO model editing is unavailable"
        )
    agent = document.get("agent")
    if not isinstance(agent, dict) or str(agent.get("acp_backend") or "").strip() != "kiro-byo":
        raise IntegrationError("Crew main Harness changed; refresh before saving a BYO model")


def _selection_rows(config: Any) -> list[dict[str, str]]:
    """Selections Crew may persist while BYO is the active Harness.

    Keep wire ids stable in Crew config, but expose friendly labels to the App.
    Enabled Mode Types and concrete provider models share one list because Crew
    stores both in the same ``model`` string fields.
    """
    rows: list[dict[str, str]] = []
    # Crew's /api/models and the ACP runtime only expose Mode Types when
    # there is at least one executable concrete model. Keep the task console
    # and its write-time whitelist consistent with that same invariant.
    public_models = list(config.public_models)
    try:
        registry = load_mode_registry()
        plugin = registry.get(str(getattr(config, "mode_pack", "") or ""))
        if public_models and plugin is not None and plugin.enabled:
            for policy in plugin.pack.policies:
                rows.append({
                    "id": type_selection_id(plugin.id, policy.id),
                    "name": f"{getattr(plugin.pack, 'short_name', '') or plugin.pack.display_name} · {policy.display_name}",
                    "kind": "type",
                })
    except Exception:
        pass
    rows.extend(
        {"id": public_id, "name": model.name, "kind": "concrete"}
        for public_id, model in public_models
    )
    return rows


def _selection_ids(config: Any) -> set[str]:
    return {row["id"] for row in _selection_rows(config)}


def _selection_label(value: str, rows: list[dict[str, str]]) -> str:
    key = str(value or "").strip()
    if not key:
        return ""
    return next((row["name"] for row in rows if row["id"] == key), key)


def _require_byo_main_backend() -> dict[str, Any]:
    selected = _crew_linkage_sync()
    if selected["main"]["configured_backend"] != "kiro-byo":
        raise IntegrationError("Kiro BYO must be Crew's main Harness before changing unified model settings")
    return selected


def _task_schedule_text(job: Any) -> str:
    schedule = getattr(job, "schedule", None)
    if schedule is None:
        return "—"
    try:
        from kiro_crew.cron_service.schedule import format_schedule

        return str(format_schedule(schedule, str(getattr(job, "timezone", "") or "")) or "—")
    except Exception:
        kind = str(getattr(schedule, "kind", "") or "")
        if kind == "cron":
            return str(getattr(schedule, "cron_expr", "") or "—")
        if kind == "every":
            secs = int(getattr(schedule, "every_secs", 0) or 0)
            return f"每 {secs} 秒" if secs else "—"
        if kind == "at":
            return "单次"
        return kind or "—"


@guarded
async def get_tasks(request: web.Request, ctx: Any) -> web.Response:
    """Read Crew's live model configuration for chat, mates, schedules and background.

    The Crew config / Cron store is the only source of truth.  This endpoint
    deliberately holds no mirrored BYO settings, so changes made in Crew are
    visible here on the next refresh and writes from this page are immediately
    visible in Crew's native editors.
    """
    home = _ensure_app_state(ctx)
    service = _cron_service(request)
    jobs = await service.list_jobs_async(include_disabled=True)
    with use_app_home(home):
        byo_config = await asyncio.to_thread(load_config, default_credential_store())
        model_rows = _selection_rows(byo_config)
    payload = []
    for job in jobs:
        try:
            from kiro_crew.dashboard.goal_reminder import is_reminder_job
            if is_reminder_job(job):
                continue
        except Exception:
            pass
        payload.append({
            "id": str(getattr(job, "id", "") or ""),
            "name": str(getattr(job, "name", "") or "未命名任务"),
            "execution_kind": _task_execution_kind(job),
            "enabled": bool(getattr(job, "enabled", True)),
            "schedule": _task_schedule_text(job),
            "timezone": str(getattr(job, "timezone", "") or ""),
            "model": str(getattr(job, "model", "") or ""),
            "persistent_session": bool(getattr(job, "persistent_session", True)),
            "minimal_context": bool(getattr(job, "minimal_context", False)),
            "last_status": str(getattr(job, "last_status", "") or ""),
            "last_error": str(getattr(job, "last_error", "") or ""),
            "last_run_ts": float(getattr(job, "last_run_ts", 0.0) or 0.0),
        })
    # Do not silently invent "Auto" when Crew's config cannot be read. Such a
    # fallback lets the UI overwrite a native choice after an unrelated error.
    from kiro_crew.config.loader import KiroCrewConfig
    try:
        crew_config = await asyncio.to_thread(KiroCrewConfig.load)
        snapshot = linkage_snapshot(crew_config.agent)
    except Exception as exc:
        raise IntegrationError("Crew model configuration could not be read; model editing is unavailable") from exc
    bg = snapshot["background"]
    background = {
        "model": bg["configured_model"],
        "resolved_model": bg["resolved_model"],
        "selection_source": bg["selection"],
        "runtime_backend": snapshot["background"]["runtime_backend"],
    }
    # The Crew config and Cron store remain the only model configuration
    # authorities. BYO's telemetry is strictly a separate execution audit.
    with use_app_home(home):
        recent = await asyncio.to_thread(TurnTelemetryStore().recent, 30)
    execution = background_execution_snapshot(recent)
    return _ok({
        "tasks": payload, "models": model_rows, "background": background,
        "execution": execution, "config_source": "kirocrew_native",
        "main_backend": snapshot["main"]["configured_backend"],
    })


@guarded
async def update_chat_model(request: web.Request, ctx: Any) -> web.Response:
    """Write Crew's native ``agent.model`` while BYO is the main Harness."""
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    requested = str(body.get("model") or "auto").strip() or "auto"
    _require_byo_main_backend()
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        config = await asyncio.to_thread(load_config, default_credential_store())
        valid = _selection_ids(config)
    if requested != "auto" and requested not in valid:
        raise ValueError(f"unknown Kiro BYO selection: {requested}")

    def _write() -> dict[str, str | bool]:
        from kiro_crew.config.loader import KiroCrewConfig, config_path, update_config_locked
        path = config_path()
        if _known_local_agent_override(path, "model"):
            raise IntegrationError("Crew config.local.json overrides agent.model; edit it in Crew first")
        def mutate(doc: dict) -> dict:
            _require_byo_in_locked_document(doc, path)
            agent = doc.get("agent")
            if not isinstance(agent, dict):
                agent = {}
                doc["agent"] = agent
            agent["model"] = requested
            return doc
        update_config_locked(path, mutate=mutate)
        stored = str(getattr(KiroCrewConfig.load().agent, "model", "auto") or "auto")
        return {"model": stored, "verified": stored == requested}
    result = await asyncio.to_thread(_write)
    if not result["verified"]:
        raise IntegrationError("Crew effective chat model differs after write; another config layer may override it")
    return _ok({"chat": result})


@guarded
async def update_member_model(request: web.Request, ctx: Any) -> web.Response:
    """Write one Crew mate's native conductor or Worker model pin."""
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    field = str(body.get("field") or "model").strip()
    if field not in {"model", "worker_model"}:
        raise ValueError("field must be model or worker_model")
    requested = str(body.get("model") or "").strip()
    _require_byo_main_backend()
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        config = await asyncio.to_thread(load_config, default_credential_store())
        valid = _selection_ids(config)
    if requested and requested != "auto" and requested not in valid:
        raise ValueError(f"unknown Kiro BYO selection: {requested}")
    if field == "model" and requested == "auto":
        # Crew's mate model uses empty for inheritance; keep its native semantics.
        requested = ""
    member = str(request.match_info.get("member") or "").strip()
    if not member:
        raise ValueError("member is required")

    def _write() -> dict[str, str | bool]:
        from kiro_crew.config.loader import KiroCrewConfig, config_path, update_config_locked
        path = config_path()
        def mutate(doc: dict) -> dict:
            _require_byo_in_locked_document(doc, path)
            agents = doc.get("agents")
            if not isinstance(agents, dict) or member not in agents or not isinstance(agents.get(member), dict):
                raise ValueError(f"Crew mate not found: {member}")
            agents[member][field] = requested
            return doc
        update_config_locked(path, mutate=mutate)
        row = (KiroCrewConfig.load().agents or {}).get(member)
        if row is None:
            raise ValueError(f"Crew mate not found after write: {member}")
        stored = str(getattr(row, field, "") or "")
        return {"member": member, "field": field, "model": stored, "verified": stored == requested}
    result = await asyncio.to_thread(_write)
    if not result["verified"]:
        raise IntegrationError("Crew effective mate model differs after write; another config layer may override it")
    return _ok({"member": result})


@guarded
async def update_background_model(request: web.Request, ctx: Any) -> web.Response:
    """Pin KiroCrew's lightweight background role to Auto or one BYO model.

    This is deliberately a host-owned setting (``agent.role_models.background``),
    not a BYO pseudo task.  Title/status-card/summary workers share that role.
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    requested = str(body.get("model") or "auto").strip() or "auto"
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        config = await asyncio.to_thread(load_config, default_credential_store())
        valid = _selection_ids(config)
    if requested != "auto" and requested not in valid:
        raise ValueError(f"unknown Kiro BYO selection: {requested}")
    selected = await asyncio.to_thread(_crew_linkage_sync)
    if selected["main"]["configured_backend"] != "kiro-byo":
        raise IntegrationError("BYO background model editing is disabled when Crew uses an official Harness")
    if not model_pin_allowed(selected_backend=selected["main"]["configured_backend"], model=requested, byo_models=valid):
        raise IntegrationError("Unknown BYO background selection")

    def _write() -> str:
        from kiro_crew.config.loader import KiroCrewConfig, config_path, update_config_locked

        path = config_path()
        def mutate(doc: dict) -> dict:
            _require_byo_in_locked_document(doc, path)
            agent = doc.get("agent")
            if not isinstance(agent, dict):
                agent = {}
                doc["agent"] = agent
            role_models = agent.get("role_models")
            if not isinstance(role_models, dict):
                role_models = {}
                agent["role_models"] = role_models
            expected = body.get("expected_model")
            current = str(role_models.get("background") or "auto").strip() or "auto"
            if expected is not None and current != str(expected or "auto"):
                raise ValueError("Crew background model changed; refresh before saving")
            role_models["background"] = requested
            return doc

        if _known_local_agent_override(path, "role_models", "background"):
            raise IntegrationError("Crew config.local.json overrides the background model; edit it in Crew first")
        update_config_locked(path, mutate=mutate)
        agent = KiroCrewConfig.load().agent
        stored = role_pin(agent, "background")
        effective = str(agent.resolve_model("background") or "auto")
        return {"model": stored, "resolved_model": effective, "verified": stored == requested}

    result = await asyncio.to_thread(_write)
    if not result["verified"]:
        raise IntegrationError("Crew background model mismatch after save; verify native config overrides")
    return _ok({"background": result})


# Only serializes this App's requests within one Gateway event loop. It cannot
# make Crew's independent scheduler/dashboard mutations atomic; a native Cron
# CAS API is required for full cross-surface conflict safety.
_CRON_APP_WRITE_LOCK = asyncio.Lock()


async def _native_cron_job(service: Any, job_id: str) -> Any:
    """Read Cron's authoritative job instead of trusting a mutation response.

    This is a post-write verification, NOT a compare-and-swap transaction:
    a concurrent native Crew update can still occur between any two awaits.
    """
    jobs = await service.list_jobs_async(include_disabled=True)
    return next((row for row in jobs if str(getattr(row, "id", "")) == job_id), None)


async def _verify_byo_after_cron_write() -> None:
    """Detect a host switch while an asynchronous Cron write was in flight.

    A failed postcondition is deliberately *not* rolled back: native Crew may
    already have applied the write, and a rollback could erase another edit.
    """
    selected = await asyncio.to_thread(_crew_linkage_sync)
    if selected["main"]["configured_backend"] != "kiro-byo":
        raise IntegrationError(
            "Crew main Harness changed during the Cron update; the change may "
            "already have been saved. Refresh and inspect the native task"
        )


@guarded
async def update_task_model(request: web.Request, ctx: Any) -> web.Response:
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    job_id = str(request.match_info.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    requested = str(body.get("model") or "").strip()
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        config = await asyncio.to_thread(load_config, default_credential_store())
        valid = _selection_ids(config)
    if requested and requested not in valid:
        raise ValueError(f"unknown Kiro BYO selection: {requested}")
    selected = await asyncio.to_thread(_crew_linkage_sync)
    if selected["main"]["configured_backend"] != "kiro-byo":
        raise IntegrationError("BYO schedule model editing is disabled when Crew uses an official Harness")
    if not model_pin_allowed(selected_backend=selected["main"]["configured_backend"], model=requested, byo_models=valid):
        raise IntegrationError("Unknown BYO scheduled-task selection")
    async with _CRON_APP_WRITE_LOCK:
        _require_byo_main_backend()
        service = _cron_service(request)
        # Read the actual Crew job before every edit, including clearing a pin.
        # Otherwise a script job or a stale BYO browser tab can be overwritten.
        job = await _native_cron_job(service, job_id)
        if job is None:
            raise ValueError(f"scheduled task not found: {job_id}")
        if _task_execution_kind(job) != "agent":
            raise ValueError("Script/Command Cron does not execute an AI model")
        expected = body.get("expected_model")
        current = str(getattr(job, "model", "") or "")
        if expected is not None and current != str(expected or ""):
            raise ValueError("Crew scheduled task model changed; refresh before saving")
        updated = await service.update_job_async(job_id, model=requested)
        if updated is None:
            raise ValueError(f"scheduled task not found: {job_id}")
        # Check Crew's stored result, not merely the object returned by the
        # update API. Never report successful synchronization for a failed or
        # lost Cron persistence operation.
        persisted = await _native_cron_job(service, job_id)
        if persisted is None:
            raise IntegrationError("Crew scheduled task vanished after update; inspect the native task")
        stored = str(getattr(persisted, "model", "") or "")
        if stored != requested:
            raise IntegrationError("Crew scheduled task model mismatch after saving; refresh the native task")
        await _verify_byo_after_cron_write()
    return _ok({"task": {"id": job_id, "model": stored, "verified": True}})


@guarded
async def update_task_execution(request: web.Request, ctx: Any) -> web.Response:
    """Update only KiroCrew-owned context controls for one scheduled task."""
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    job_id = str(request.match_info.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    persistent = body.get("persistent_session")
    minimal = body.get("minimal_context")
    if not isinstance(persistent, bool) or not isinstance(minimal, bool):
        raise ValueError("persistent_session and minimal_context must be booleans")
    # Optimistic concurrency for the BYO task console's context switches.
    # Crew owns this state; never silently replace a newer native/UI choice
    # made since the task page last refreshed. Missing expectations retain the
    # previous API contract for older clients, but the shipped UI sends both.
    expected_context = {}
    for field in ("persistent_session", "minimal_context"):
        key = "expected_" + field
        if key in body:
            if not isinstance(body[key], bool):
                raise ValueError(f"{key} must be boolean")
            expected_context[field] = body[key]

    async with _CRON_APP_WRITE_LOCK:
        _require_byo_main_backend()
        service = _cron_service(request)
        job = await _native_cron_job(service, job_id)
        if job is None:
            raise ValueError(f"scheduled task not found: {job_id}")
        # Execution-context flags belong to agent jobs, not Script/Command
        # jobs. The UI hides these controls; the API must enforce the same rule.
        if _task_execution_kind(job) != "agent":
            raise ValueError("Script/Command Cron does not execute an AI session")
        for field, previous in expected_context.items():
            if bool(getattr(job, field)) != previous:
                raise ValueError("Crew scheduled task execution context changed; refresh before saving")
        updated = await service.update_job_async(
            job_id, persistent_session=persistent, minimal_context=minimal
        )
        if updated is None:
            raise ValueError(f"scheduled task not found: {job_id}")
        persisted = await _native_cron_job(service, job_id)
        if persisted is None:
            raise IntegrationError("Crew scheduled task vanished after update; inspect the native task")
        if (bool(getattr(persisted, "persistent_session", not persistent)) != persistent or
                bool(getattr(persisted, "minimal_context", not minimal)) != minimal):
            raise IntegrationError("Crew scheduled task execution context mismatch after saving; refresh the native task")
        await _verify_byo_after_cron_write()
    return _ok({
        "task": {
            "id": job_id,
            "persistent_session": persistent,
            "minimal_context": minimal,
        }
    })


@guarded
async def get_record_center(request: web.Request, ctx: Any) -> web.Response:
    """Read-only timeline and retained counts. Never changes ledger state."""
    home = _ensure_app_state(ctx)
    try:
        limit = int(request.query.get("limit", "100"))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid records limit") from exc
    with use_app_home(home):
        result = await asyncio.to_thread(
            records_snapshot, limit=limit,
            kind=str(request.query.get("kind", "")),
            mode=str(request.query.get("mode", "")),
        )
    return _ok(result)


@guarded
async def get_diagnostics(request: web.Request, ctx: Any) -> web.Response:
    """Return only the bounded history consumed by the Records surface.

    Agent/team/workspace control stays on the live ACP process.  The management
    App deliberately does not mirror those mutable control planes into a second
    HTTP API.
    """
    home = _ensure_app_state(ctx)
    try:
        limit = max(1, min(int(request.query.get("limit", "50")), 100))
    except ValueError:
        limit = 50
    with use_app_home(home):
        store = TurnTelemetryStore()
        turns = await asyncio.to_thread(store.recent, limit)
        summary = await asyncio.to_thread(store.summary, store.max_records)
        runs = await asyncio.to_thread(DurableRunStore().list, limit=min(limit, 50))
    return _ok({
        "summary": summary,
        "turns": turns,
        "runs": [DurableRunStore.public_record(record) for record in runs],
        "retention_limit": store.max_records,
    })



@guarded
async def get_acp_conformance(request: web.Request, ctx: Any) -> web.Response:
    _ensure_app_state(ctx)
    return _ok(conformance_report())


@guarded
async def clear_diagnostics(request: web.Request, ctx: Any) -> web.Response:
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        removed = await asyncio.to_thread(TurnTelemetryStore().clear)
        # Route feature vectors are stored on completed turns, so clearing
        # telemetry resets their adaptive evidence. Runtime Trace is an
        # independent execution ledger, not a derivative of request history.
        # Keep real routing diagnostics and all Durable Runs untouched.
    return _ok({"cleared": True, "removed": removed, "removed_trace": 0})




@guarded
async def get_runtime_trace(request: web.Request, ctx: Any) -> web.Response:
    home = _ensure_app_state(ctx)
    try:
        limit = max(1, min(int(request.query.get("limit", "100")), 500))
    except ValueError:
        limit = 100
    session_id = str(request.query.get("session_id", "") or "").strip()
    turn_id = str(request.query.get("turn_id", "") or "").strip()
    run_id = str(request.query.get("run_id", "") or "").strip()
    event = str(request.query.get("event", "") or "").strip()
    if len(event) > 128:
        raise ValueError("event filter is too long")
    with use_app_home(home):
        store = RuntimeTraceStore()
        events = await asyncio.to_thread(store.recent, limit, session_id=session_id, turn_id=turn_id, run_id=run_id, event=event)
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
    payload = await asyncio.to_thread(integration_status, APP_ROOT, home)
    payload["harness"] = await asyncio.to_thread(_harness_state)
    return _ok(payload)


@guarded
async def activate_harness(request: web.Request, ctx: Any) -> web.Response:
    _ensure_app_state(ctx)
    state = await asyncio.to_thread(_activate_kiro_byo_harness_sync)
    return _ok({
        "activated": True,
        "harness": state,
        "new_session_required": True,
    })


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
    verified["harness"] = await asyncio.to_thread(_harness_state)
    result["verified_status"] = verified
    result["harness"] = verified["harness"]
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
        AppRoute("GET", "/locales/{locale}", get_locale),
        AppRoute("GET", "/presets", get_presets),
        AppRoute("GET", "/providers", get_providers),
        AppRoute("GET", "/routing", get_routing_dashboard),
        AppRoute("GET", "/mode-sync", get_mode_sync),
        AppRoute("POST", "/providers/test", test_provider),
        AppRoute("POST", "/providers", save_provider),
        AppRoute("PUT", "/providers/{provider_id}/credential", replace_credential),
        AppRoute("POST", "/providers/{provider_id}/refresh", refresh_models),
        AppRoute("PUT", "/providers/{provider_id}/models/{model_id}/thinking", update_model_thinking),
        AppRoute("PUT", "/mode", update_mode),
        AppRoute("PUT", "/preferences/reasoning-language", update_reasoning_language),
        AppRoute("PUT", "/preferences/ui-language", update_ui_language),
        AppRoute("PUT", "/preferences/response-language", update_response_language),
        AppRoute("DELETE", "/providers/{provider_id}", remove_provider),
        AppRoute("GET", "/accounts", get_accounts),
        AppRoute("GET", "/tasks", get_tasks),
        AppRoute("GET", "/tasks/linkage", get_task_linkage),
        AppRoute("PUT", "/tasks/chat-model", update_chat_model),
        AppRoute("PUT", "/tasks/members/{member}/model", update_member_model),
        AppRoute("POST", "/tasks/linkage/member/sync", sync_member_backend),
        AppRoute("POST", "/tasks/linkage/preview", preview_linkage_switch),
        AppRoute("POST", "/tasks/linkage/apply", apply_linkage_switch),
        AppRoute("PUT", "/tasks/background-model", update_background_model),
        AppRoute("PUT", "/tasks/{job_id}/model", update_task_model),
        AppRoute("PUT", "/tasks/{job_id}/execution", update_task_execution),
        AppRoute("GET", "/records", get_record_center),
        AppRoute("GET", "/diagnostics", get_diagnostics),
        AppRoute("DELETE", "/diagnostics", clear_diagnostics),
        AppRoute("GET", "/runtime-trace", get_runtime_trace),
        AppRoute("DELETE", "/runtime-trace", clear_runtime_trace),
        AppRoute("GET", "/acp-conformance", get_acp_conformance),
        AppRoute("GET", "/integration", get_integration),
        AppRoute("POST", "/harness/activate", activate_harness),
        AppRoute("POST", "/integration/install", install_integration),
        AppRoute("POST", "/integration/uninstall", uninstall_integration),
    ]
