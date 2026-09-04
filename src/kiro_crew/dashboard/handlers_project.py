"""Portable Project bundle API plus compatibility aliases for task runs."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.history import is_incognito_transcript
from kiro_crew.project_capabilities import (
    ProjectCapabilityError,
    ProjectCapabilityManager,
    ProjectCapabilityStatus,
)
from kiro_crew.project_git import GitProjectStore, ProjectGitError
from kiro_crew.project_manifest import (
    ProjectManifestError,
    create_project_manifest,
    load_project_manifest,
)
from kiro_crew.project_registry import (
    ProjectRegistry,
    ProjectRegistryError,
    RegisteredProject,
)
from kiro_crew.security import (
    redact_and_truncate,
    redact_credentials,
    redact_exfiltration_urls,
)

_VISIBLE_SOURCES = {"text", "spec", "file", "chat", "dashboard", "mcp"}
logger = logging.getLogger(__name__)


class _ProjectServices:
    """Lazily construct and retain the install-scoped Project services."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._registry: ProjectRegistry | None = None
        self._capability_manager: ProjectCapabilityManager | None = None

    def get(self) -> tuple[ProjectRegistry, ProjectCapabilityManager]:
        with self._lock:
            if self._registry is None:
                self._registry = ProjectRegistry()
                self._capability_manager = ProjectCapabilityManager(self._registry)
            assert self._capability_manager is not None
            return self._registry, self._capability_manager


PROJECT_SERVICES_KEY = web.AppKey("project_services", _ProjectServices)


def install_project_services(app: web.Application) -> None:
    """Install a lazy holder without touching Project storage before socket bind."""
    app[PROJECT_SERVICES_KEY] = _ProjectServices()


def _runner(request):
    return request.app["state"].task_runner


def _registry(request: web.Request) -> ProjectRegistry:
    return request.app[PROJECT_SERVICES_KEY].get()[0]


def _capabilities(request: web.Request) -> ProjectCapabilityManager:
    return request.app[PROJECT_SERVICES_KEY].get()[1]


async def _warm_project_services(request: web.Request) -> None:
    """Run first-use path and keystone setup away from the aiohttp event loop."""
    await asyncio.to_thread(request.app[PROJECT_SERVICES_KEY].get)


async def project_registry_for_request(request: web.Request) -> ProjectRegistry:
    """Return the shared registry after safe lazy initialization."""
    await _warm_project_services(request)
    return _registry(request)


def _sel():
    import kiro_crew.dashboard.handlers as handlers

    return handlers.sel()


async def _owner_only(request: web.Request, operation: str) -> web.Response | None:
    authorized = is_owner_dashboard_request(request)
    caller = str(request.get("user") or "unknown")
    try:
        await asyncio.to_thread(
            lambda: _sel().log_api_access(
                caller=caller,
                operation=operation,
                outcome="allowed" if authorized else "denied",
                source="dashboard",
                resources="owner_dashboard" if authorized else "non_owner_block",
                critical=authorized,
            )
        )
    except Exception:
        logger.error("SEL audit for Project %s failed", operation, exc_info=True)
        if authorized:
            return web.json_response(
                {
                    "error": "Project permission audit is unavailable",
                    "code": "project_audit_unavailable",
                },
                status=503,
            )
    if authorized:
        return None
    return web.json_response(
        {"error": "owner authorization required", "code": "owner_only"}, status=403
    )


def _is_hidden(run) -> bool:
    return run.source not in _VISIBLE_SOURCES


def _redact(text: str) -> str:
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _redact_json_value(value: Any) -> Any:
    """Defensively redact every string before provider config reaches dashboard JSON."""
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    if isinstance(value, dict):
        return {_redact(str(key)): _redact_json_value(item) for key, item in value.items()}
    return value


def _redact_and_truncate(text: str, max_chars: int) -> str:
    """Redact over the FULL text, then truncate (never ``_redact(x[:n])``).

    Truncating first can cut a credential in half at the boundary, leaving a
    fragment the redaction regexes no longer match. Delegates to the canonical
    helper so redaction always precedes the slice.
    """
    return redact_and_truncate(text, max_chars)


def _run_to_project(run) -> dict:
    desc = getattr(run, "description", None) or run.spec_content or run.original_input or ""
    return {
        "id": run.task_id,
        "name": _redact(run.name or run.task_id),
        "description": _redact_and_truncate(desc, 4000),
        "status": run.status,
        "created_at": run.started_at or 0,
        "updated_at": getattr(run, "updated_at", run.started_at) or 0,
    }


async def api_task_projects_list(request):
    tr = _runner(request)
    if not tr:
        return web.json_response([])
    runs = sorted(
        (r for r in tr._runs.values() if r.source in _VISIBLE_SOURCES),
        key=lambda r: r.started_at or 0,
        reverse=True,
    )
    return web.json_response([_run_to_project(r) for r in runs])


async def api_task_project_get(request):
    tr = _runner(request)
    pid = request.match_info["id"]
    run = tr._runs.get(pid) if tr else None
    if not run or _is_hidden(run):
        raise web.HTTPNotFound(text=f"Project {pid} not found")
    return web.json_response(_run_to_project(run))


async def api_task_project_create(request):
    return web.json_response({"error": "Use 'task run <spec>' to create projects"}, status=400)


async def api_task_project_update(request):
    tr = _runner(request)
    pid = request.match_info["id"]
    data = await request.json()
    run = tr._runs.get(pid) if tr else None
    if not run or _is_hidden(run):
        raise web.HTTPNotFound(text=f"Project {pid} not found")
    if "name" in data:
        run.name = data["name"]
        await tr._apersist_runs()
    return web.json_response(_run_to_project(run))


async def api_task_project_delete(request):
    tr = _runner(request)
    pid = request.match_info["id"]
    if not tr or not await tr.delete_run(pid):
        raise web.HTTPNotFound(text=f"Project {pid} not found")
    return web.json_response({"ok": True})


async def api_activities_list(request):
    return web.json_response([])


async def api_comment_add(request):
    return web.json_response({"ok": True}, status=201)


async def api_comments_list(request):
    return web.json_response([])


async def api_comment_delete(request):
    return web.json_response({"ok": True})


def _capability_payload(status: ProjectCapabilityStatus) -> dict[str, Any]:
    return {
        "active": status.active,
        "review_key": status.review_key,
        "agents": status.inventory.agents,
        "skills": status.inventory.skills,
        "mcp_servers": status.inventory.mcp_servers,
        "repos": status.inventory.repos,
        "repositories": [
            {"source_id": _redact(source_id), "path": _redact(path)}
            for source_id, path in status.repositories
        ],
        "agent_names": [_redact(name) for name in status.agent_names],
        "mcp_server_details": [
            {
                "name": _redact(name),
                **_redact_json_value(spec),
            }
            for name, spec in status.mcp_servers
        ],
    }


def _project_payload(
    project: RegisteredProject,
    capability_manager: ProjectCapabilityManager | None = None,
    sessions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    registrations = [
        {
            "origin": registration.origin,
            "path": _redact(str(registration.path)),
            "syncable": registration.origin == "managed_git",
        }
        for registration in project.registrations
    ]
    primary = project.registrations[-1]

    def _unavailable(code: str) -> dict[str, Any]:
        return {
            "id": project.id,
            "name": _redact(project.name),
            "description": "",
            "workspace_source": "",
            "sources": [],
            "context": {"agents": [], "skills": [], "mcp": ""},
            "registrations": registrations,
            "health": {"status": "unavailable", "code": code},
            "sessions": sessions or [],
            "capabilities": {
                "active": False,
                "review_key": "",
                "agents": 0,
                "skills": 0,
                "mcp_servers": 0,
                "repos": 0,
                "repositories": [],
                "agent_names": [],
                "mcp_server_details": [],
            },
        }

    try:
        manifest = load_project_manifest(primary.path)
    except (OSError, ProjectManifestError):
        return _unavailable("project_manifest_unavailable")
    if manifest.id != project.id:
        return _unavailable("project_manifest_identity_mismatch")
    capabilities = capability_manager or ProjectCapabilityManager()
    try:
        capability_status = _capability_payload(capabilities.status(project.id))
    except (OSError, ProjectCapabilityError, ProjectRegistryError):
        capability_status = {
            "active": False,
            "review_key": "",
            "agents": 0,
            "skills": 0,
            "mcp_servers": 0,
            "repos": sum(source.type == "repo" for source in manifest.sources),
            "repositories": [],
            "agent_names": [],
            "mcp_server_details": [],
        }
    return {
        "id": project.id,
        "name": _redact(manifest.name),
        "description": _redact(manifest.description),
        "workspace_source": _redact(manifest.workspace_source),
        "sources": [
            {
                "id": _redact(source.id),
                "type": _redact(source.type),
                **_redact_json_value(source.config),
            }
            for source in manifest.sources
        ],
        "context": {
            "agents": [_redact(item) for item in manifest.context.agents],
            "skills": [_redact(item) for item in manifest.context.skills],
            "mcp": _redact(manifest.context.mcp),
        },
        "registrations": registrations,
        "health": {"status": "healthy", "code": "project_healthy"},
        "sessions": sessions or [],
        "capabilities": capability_status,
    }


def _session_key_from_history(raw: str) -> str:
    return raw.removeprefix("dashboard_")


def _live_slots_snapshot(state: Any) -> tuple[Any, ...]:
    slots = getattr(state, "_slots", {})
    return tuple(slots.values()) if isinstance(slots, dict) else ()


def _project_sessions_by_id(
    state: Any, live_slots: tuple[Any, ...]
) -> dict[str, list[dict[str, Any]]]:
    by_project: dict[str, dict[str, dict[str, Any]]] = {}
    conversation_log = getattr(state, "conversation_log", None)
    if conversation_log is not None:
        for session in conversation_log.list_sessions():
            if is_incognito_transcript(session.get("memory_mode")):
                continue
            project_id = session.get("project_id")
            if not isinstance(project_id, str) or not project_id:
                continue
            key = _session_key_from_history(str(session.get("key") or ""))
            if not key:
                continue
            by_project.setdefault(project_id, {})[key] = {
                "key": key,
                "title": _redact(str(session.get("title") or key)),
                "messages": int(session.get("messages") or 0),
                "running": False,
                "live": False,
            }
    for slot in live_slots:
        if is_incognito_transcript(getattr(slot, "memory_mode", "")):
            continue
        project_id = getattr(slot, "project_id", "")
        if not project_id:
            continue
        by_project.setdefault(project_id, {})[slot.key] = {
            "key": slot.key,
            "title": _redact(slot.display_title),
            "messages": len(slot.messages),
            "running": slot.running,
            "live": True,
        }
    return {
        project_id: sorted(sessions.values(), key=lambda item: (not item["live"], item["key"]))
        for project_id, sessions in by_project.items()
    }


async def api_projects_list(request: web.Request) -> web.Response:
    """List the portable Project bundles registered on this install."""

    denied = await _owner_only(request, "project_list")
    if denied is not None:
        return denied
    await _warm_project_services(request)

    registry = _registry(request)
    capability_manager = _capabilities(request)
    state = request.app["state"]
    live_slots = _live_slots_snapshot(state)
    session_index = await asyncio.to_thread(_project_sessions_by_id, state, live_slots)

    def _read() -> list[dict[str, Any]]:
        return [
            _project_payload(project, capability_manager, session_index.get(project.id, []))
            for project in registry.list_projects()
        ]

    try:
        projects = await asyncio.to_thread(_read)
    except ProjectRegistryError as exc:
        return web.json_response(
            {"error": _redact(str(exc)), "code": "project_registry_invalid"}, status=500
        )
    return web.json_response({"projects": projects})


async def api_project_get(request: web.Request) -> web.Response:
    """Return one portable Project bundle by stable id."""

    denied = await _owner_only(request, "project_get")
    if denied is not None:
        return denied
    await _warm_project_services(request)

    try:
        project = await asyncio.to_thread(_registry(request).get, request.match_info["id"])
    except ProjectRegistryError:
        return web.json_response(
            {"error": "project not found", "code": "project_not_found"}, status=404
        )
    state = request.app["state"]
    session_index = await asyncio.to_thread(
        _project_sessions_by_id, state, _live_slots_snapshot(state)
    )
    sessions = session_index.get(project.id, [])
    return web.json_response(
        await asyncio.to_thread(_project_payload, project, _capabilities(request), sessions)
    )


async def _json_object(request: web.Request) -> dict[str, Any] | None:
    try:
        payload = await request.json()
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


async def api_project_create(request: web.Request) -> web.Response:
    """Create and register a local Project bundle."""

    denied = await _owner_only(request, "project_create")
    if denied is not None:
        return denied
    await _warm_project_services(request)
    payload = await _json_object(request)
    name = payload.get("name") if payload else None
    path = payload.get("path") if payload else None
    if (
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(path, str)
        or not path.strip()
    ):
        return web.json_response(
            {
                "error": "name and path must be non-empty strings",
                "code": "project_invalid_request",
            },
            status=400,
        )

    registry = _registry(request)

    def _create() -> RegisteredProject:
        # The dashboard owner deliberately chooses this local Project root. The
        # creator resolves it off-loop and rejects every Crew-sensitive location.
        bundle = Path(path).expanduser().resolve()  # lgtm[py/path-injection]
        create_project_manifest(bundle, name=name)
        return registry.add_local(bundle)

    try:
        project = await asyncio.to_thread(_create)
    except (OSError, RuntimeError, ProjectManifestError, ProjectRegistryError) as exc:
        return web.json_response(
            {"error": _redact(str(exc)), "code": "project_create_failed"}, status=400
        )
    return web.json_response(
        await asyncio.to_thread(_project_payload, project, _capabilities(request)), status=201
    )


async def api_project_add(request: web.Request) -> web.Response:
    """Register an existing local bundle or clone and register a Git bundle."""

    denied = await _owner_only(request, "project_add")
    if denied is not None:
        return denied
    await _warm_project_services(request)
    payload = await _json_object(request)
    source = payload.get("source") if payload else None
    if not isinstance(source, str) or not source.strip():
        return web.json_response(
            {"error": "source must be a non-empty string", "code": "project_invalid_request"},
            status=400,
        )
    source = source.strip()
    registry = _registry(request)
    capability_manager = _capabilities(request)

    def _add() -> RegisteredProject:
        local = Path(source).expanduser()
        if local.exists():
            return capability_manager.register_local(local)
        return GitProjectStore(registry).add(
            source,
            before_primary_change=capability_manager.guard_primary_change,
        )

    try:
        project = await asyncio.to_thread(_add)
    except (
        OSError,
        ProjectCapabilityError,
        ProjectGitError,
        ProjectManifestError,
        ProjectRegistryError,
    ) as exc:
        return web.json_response(
            {"error": _redact(str(exc)), "code": "project_add_failed"}, status=400
        )
    return web.json_response(
        await asyncio.to_thread(_project_payload, project, capability_manager), status=201
    )


async def api_project_sync(request: web.Request) -> web.Response:
    """Fast-forward the managed Git materialization for one Project."""

    denied = await _owner_only(request, "project_sync")
    if denied is not None:
        return denied
    await _warm_project_services(request)
    registry = _registry(request)
    try:
        project = await asyncio.to_thread(GitProjectStore(registry).sync, request.match_info["id"])
        capability_status = await asyncio.to_thread(
            _capabilities(request).refresh_if_active, project.id
        )
    except ProjectRegistryError:
        return web.json_response(
            {"error": "project not found", "code": "project_not_found"}, status=404
        )
    except ProjectGitError as exc:
        message = str(exc)
        if "has no managed Git clone" in message:
            return web.json_response(
                {"error": _redact(message), "code": "project_not_syncable"}, status=409
            )
        return web.json_response(
            {"error": _redact(message), "code": "project_sync_failed"}, status=400
        )
    except (OSError, ProjectCapabilityError) as exc:
        return web.json_response(
            {"error": _redact(str(exc)), "code": "project_refresh_failed"}, status=409
        )
    if capability_status.active:
        await _rebuild_agent_config()
    try:
        await asyncio.to_thread(
            lambda: _sel().log_api_access(
                caller=str(request.get("user") or "dashboard"),
                operation="project_sync",
                outcome="allowed",
                source="dashboard",
                resources=f"project={project.id}",
            )
        )
    except Exception:
        logger.debug("SEL audit for Project sync failed", exc_info=True)
    return web.json_response(
        await asyncio.to_thread(_project_payload, project, _capabilities(request))
    )


async def _rebuild_agent_config() -> None:
    try:
        from kiro_crew.agent import rebuild_agent_config

        await asyncio.to_thread(rebuild_agent_config)
    except Exception:
        logger.warning("Agent config rebuild failed after Project activation change", exc_info=True)


async def api_project_activate(request: web.Request) -> web.Response:
    """Trust and materialize a Project's declared install capabilities."""

    denied = await _owner_only(request, "project_activate")
    if denied is not None:
        return denied
    await _warm_project_services(request)
    payload = await _json_object(request)
    expected_key = payload.get("expected_key") if payload else None
    project_id = request.match_info["id"]

    def _audit() -> None:
        _sel().log_governance_decision(
            session_key="dashboard:projects",
            tool_name="project_activate",
            scope="project_capabilities",
            item=project_id,
            outcome="allowed",
            rule="operator_activated_project",
            reason="operator trusted and activated Project-bundled capabilities",
            critical=True,
        )

    try:
        status = await asyncio.to_thread(
            _capabilities(request).activate,
            project_id,
            expected_key=expected_key,
            on_authorized=_audit,
        )
    except ProjectRegistryError:
        return web.json_response(
            {"error": "project not found", "code": "project_not_found"}, status=404
        )
    except (OSError, ProjectCapabilityError) as exc:
        return web.json_response(
            {"error": _redact(str(exc)), "code": "project_activation_failed"}, status=400
        )
    await _rebuild_agent_config()
    return web.json_response(_capability_payload(status))


async def api_project_deactivate(request: web.Request) -> web.Response:
    """Withdraw Project capability trust and remove its materialized entries."""

    denied = await _owner_only(request, "project_deactivate")
    if denied is not None:
        return denied
    await _warm_project_services(request)
    project_id = request.match_info["id"]

    def _audit() -> None:
        _sel().log_governance_decision(
            session_key="dashboard:projects",
            tool_name="project_deactivate",
            scope="project_capabilities",
            item=project_id,
            outcome="denied",
            rule="operator_deactivated_project",
            reason="operator deactivated Project-bundled capabilities",
            critical=True,
        )

    try:
        status = await asyncio.to_thread(
            _capabilities(request).deactivate,
            project_id,
            on_authorized=_audit,
        )
    except ProjectRegistryError:
        return web.json_response(
            {"error": "project not found", "code": "project_not_found"}, status=404
        )
    except (OSError, ProjectCapabilityError) as exc:
        return web.json_response(
            {"error": _redact(str(exc)), "code": "project_deactivation_failed"}, status=409
        )
    await _rebuild_agent_config()
    return web.json_response(_capability_payload(status))


async def api_project_remove(request: web.Request) -> web.Response:
    """Unregister a Project after withdrawing its activated capabilities."""

    denied = await _owner_only(request, "project_remove")
    if denied is not None:
        return denied
    await _warm_project_services(request)
    project_id = request.match_info["id"]

    def _audit() -> None:
        _sel().log_governance_decision(
            session_key="dashboard:projects",
            tool_name="project_remove",
            scope="project_capabilities",
            item=project_id,
            outcome="denied",
            rule="operator_removed_project",
            reason="operator removed Project registration and bundled capabilities",
            critical=True,
        )

    try:
        await asyncio.to_thread(
            _capabilities(request).unregister,
            project_id,
            on_authorized=_audit,
        )
    except ProjectRegistryError:
        return web.json_response(
            {"error": "project not found", "code": "project_not_found"}, status=404
        )
    except (OSError, ProjectCapabilityError, ProjectGitError) as exc:
        return web.json_response(
            {"error": _redact(str(exc)), "code": "project_remove_failed"}, status=409
        )
    await _rebuild_agent_config()
    return web.json_response({"ok": True, "id": project_id})
