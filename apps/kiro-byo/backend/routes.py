"""KiroCrew App routes for Kiro BYO.

The app page is the preferred provider/API-key configuration surface.  Secrets
stay in the OS credential store; the routes only expose configured/missing
state.  The same App can also install the verified ACP integration overlay and
then asks KiroCrew to restart once.
"""
from __future__ import annotations

import asyncio
import hashlib
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
from ..src.kiro_byo.acp_conformance import conformance_report
from ..src.kiro_byo.routing_dashboard import build_routing_dashboard

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
async def get_presets(request: web.Request, ctx: Any) -> web.Response:
    return _ok({"presets": list_provider_presets()})


@guarded
async def get_routing_dashboard(request: web.Request, ctx: Any) -> web.Response:
    """Read-only Tiga adaptive-routing observability for the Modes page."""
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


def _cron_service(request: web.Request) -> Any:
    try:
        state = request.app["state"]
        service = getattr(state, "crons", None)
    except Exception as exc:
        raise IntegrationError("KiroCrew cron service is unavailable") from exc
    if service is None:
        raise IntegrationError("KiroCrew cron service is unavailable")
    return service


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
    """List KiroCrew schedules and the concrete BYO models they can pin."""
    home = _ensure_app_state(ctx)
    service = _cron_service(request)
    jobs = await service.list_jobs_async(include_disabled=True)
    with use_app_home(home):
        config = await asyncio.to_thread(load_config, default_credential_store())
        model_rows = [
            {"id": public_id, "name": model.name}
            for public_id, model in config.public_models
        ]
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
    background_model = "auto"
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        crew_config = await asyncio.to_thread(KiroCrewConfig.load)
        background_model = str(crew_config.agent.resolve_model("background") or "auto")
    except Exception:
        # Scheduled-task management must remain usable even if the host config
        # cannot currently be read; the update endpoint below will fail closed.
        background_model = "auto"
    return _ok({
        "tasks": payload,
        "models": model_rows,
        "background": {"model": background_model},
    })


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
    if requested.startswith("kiro-byo-type:"):
        raise ValueError("Type is not a background model; choose Auto or a concrete model")

    home = _ensure_app_state(ctx)
    with use_app_home(home):
        config = await asyncio.to_thread(load_config, default_credential_store())
        valid = {public_id for public_id, _model in config.public_models}
    if requested != "auto" and requested not in valid:
        raise ValueError(f"unknown Kiro BYO model: {requested}")

    def _write() -> str:
        from kiro_crew.config.loader import KiroCrewConfig, config_path, update_config_locked

        def mutate(doc: dict) -> dict:
            agent = doc.get("agent")
            if not isinstance(agent, dict):
                agent = {}
                doc["agent"] = agent
            role_models = agent.get("role_models")
            if not isinstance(role_models, dict):
                role_models = {}
                agent["role_models"] = role_models
            role_models["background"] = requested
            return doc

        update_config_locked(config_path(), mutate=mutate)
        effective = str(KiroCrewConfig.load().agent.resolve_model("background") or "auto")
        if effective != requested:
            raise IntegrationError(
                "KiroCrew background model is overridden by a higher-priority config: "
                f"requested {requested!r}, effective {effective!r}"
            )
        return effective

    effective = await asyncio.to_thread(_write)
    return _ok({"background": {"model": effective}})


@guarded
async def update_task_model(request: web.Request, ctx: Any) -> web.Response:
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    job_id = str(request.match_info.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    requested = str(body.get("model") or "").strip()
    if requested.startswith("kiro-byo-type:"):
        raise ValueError("Type is not a model; choose Auto or a concrete model")
    home = _ensure_app_state(ctx)
    with use_app_home(home):
        config = await asyncio.to_thread(load_config, default_credential_store())
        valid = {public_id for public_id, _model in config.public_models}
    if requested and requested not in valid:
        raise ValueError(f"unknown Kiro BYO model: {requested}")
    updated = await _cron_service(request).update_job_async(job_id, model=requested)
    if updated is None:
        raise ValueError(f"scheduled task not found: {job_id}")
    return _ok({"task": {"id": str(getattr(updated, "id", "") or ""), "model": str(getattr(updated, "model", "") or "")}})


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
    updated = await _cron_service(request).update_job_async(
        job_id, persistent_session=persistent, minimal_context=minimal
    )
    if updated is None:
        raise ValueError(f"scheduled task not found: {job_id}")
    return _ok({
        "task": {
            "id": str(getattr(updated, "id", "") or ""),
            "persistent_session": bool(getattr(updated, "persistent_session", True)),
            "minimal_context": bool(getattr(updated, "minimal_context", False)),
        }
    })


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
        "runs": runs,
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
        # Requests, their bounded route feature vectors and event diagnostics
        # form one user-visible history. Clearing it must reset all adaptive
        # evidence, not leave orphan runtime failures influencing new routes.
        removed_trace = await asyncio.to_thread(RuntimeTraceStore().clear)
    return _ok({"cleared": True, "removed": removed, "removed_trace": removed_trace})




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
    with use_app_home(home):
        store = RuntimeTraceStore()
        events = await asyncio.to_thread(store.recent, limit, session_id=session_id, turn_id=turn_id, run_id=run_id)
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
        AppRoute("PUT", "/tasks/background-model", update_background_model),
        AppRoute("PUT", "/tasks/{job_id}/model", update_task_model),
        AppRoute("PUT", "/tasks/{job_id}/execution", update_task_execution),
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
