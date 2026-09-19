"""Serve task-runner ``/api/projects`` through ``api_task_*`` handlers and Projects
``/api/project-bundles`` through bundle-named handlers, never a shared symbol.
Task-runner handlers move to their own module when the task runner next changes;
the routes stay.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.history import is_incognito_transcript
from kiro_crew.project_git import (
    GitProjectStore,
    ProjectCheckoutDivergedError,
    ProjectGitError,
    ProjectSandboxUnavailableError,
)
from kiro_crew.project_manifest import (
    PROJECT_MANIFEST_NAME,
    ProjectManifestError,
    create_project_manifest,
    load_project_manifest,
)
from kiro_crew.project_registry import (
    ProjectRegistry,
    ProjectRegistryError,
    RegisteredProject,
)
from kiro_crew.project_review import (
    compute_review_digest,
    review_preview,
    stale_review_paths,
    unreviewable_files,
)
from kiro_crew.project_sessions import (
    ProjectSessionError,
    describe_project_sources,
    pending_project_sources,
    resolve_primary_checkout,
    review_stale_files,
)
from kiro_crew.security import (
    redact_and_truncate,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.taskrunner import WorkflowInitializing

_VISIBLE_SOURCES = {"text", "spec", "file", "chat", "dashboard", "mcp"}
logger = logging.getLogger(__name__)


class _ProjectServices:
    """Lazily construct and retain the install-scoped Project registry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._registry: ProjectRegistry | None = None

    def get(self) -> ProjectRegistry:
        with self._lock:
            if self._registry is None:
                self._registry = ProjectRegistry()
            return self._registry


PROJECT_SERVICES_KEY = web.AppKey("project_services", _ProjectServices)


def install_project_services(app: web.Application) -> None:
    """Install a lazy holder without touching Project storage before socket bind."""
    app[PROJECT_SERVICES_KEY] = _ProjectServices()


def _runner(request):
    return request.app["state"].task_runner


def _registry(request: web.Request) -> ProjectRegistry:
    return request.app[PROJECT_SERVICES_KEY].get()


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
    try:
        if not tr or not await tr.delete_run(pid):
            raise web.HTTPNotFound(text=f"Project {pid} not found")
    except WorkflowInitializing as exc:
        return web.json_response({"error": str(exc), "code": exc.code}, status=503)
    return web.json_response({"ok": True})


async def api_activities_list(request):
    return web.json_response([])


async def api_comment_add(request):
    return web.json_response({"ok": True}, status=201)


async def api_comments_list(request):
    return web.json_response([])


async def api_comment_delete(request):
    return web.json_response({"ok": True})


def _project_payload(
    project: RegisteredProject,
    sessions: list[dict[str, Any]] | None = None,
    registry: ProjectRegistry | None = None,
    fetch_failures: tuple[str, ...] = (),
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
            "registrations": registrations,
            "health": {"status": "unavailable", "code": code},
            "sessions": sessions or [],
        }

    try:
        manifest = load_project_manifest(primary.path)
    except (OSError, ProjectManifestError):
        return _unavailable("project_manifest_unavailable")
    # A readable Project can still need review because executable surfaces moved.
    # A pull with fetch failures also reports those changes alongside its source
    # health, so neither outcome hides the other.
    health: dict[str, Any] = {"status": "healthy", "code": "project_healthy"}
    try:
        bundle_dir, checkout_dir, unavailable_sources = describe_project_sources(
            project, registry=registry
        )
        stale_files = review_stale_files(project, bundle_dir, checkout_dir)
    except (OSError, ProjectManifestError, ProjectSessionError, ProjectRegistryError):
        stale_files = ()
        checkout_dir = None
        unavailable_sources = ()
    pending_sources = pending_project_sources(project, primary.path, manifest)
    unavailable_sources = tuple(
        sorted((set(unavailable_sources) | set(fetch_failures)) - set(pending_sources))
    )
    if fetch_failures:
        # A cached tree may still resolve even though fetching it failed. Report
        # that outcome without accepting any changed discovery surfaces.
        health = {
            "status": "sources_unavailable",
            "code": "project_sources_unavailable",
        }
        if stale_files:
            health["stale_files"] = [_redact(path) for path in stale_files]
    elif pending_sources:
        # A declared source is a definition the owner has not seen, so the
        # Project is review-stale on the manifest that names it -- not
        # "sources unavailable", which would read as a broken URL.
        health = {
            "status": "review_stale",
            "code": "project_review_stale",
            "stale_files": [_redact(path) for path in (stale_files or (PROJECT_MANIFEST_NAME,))],
        }
    elif checkout_dir is None:
        # No primary checkout means nothing can start AND the reviewed surfaces
        # cannot be read, so this outranks a stale digest: the digest computed
        # against a missing tree would name files it never actually compared.
        health = {
            "status": "sources_unavailable",
            "code": "project_sources_unavailable",
        }
    elif stale_files:
        health = {
            "status": "review_stale",
            "code": "project_review_stale",
            "stale_files": [_redact(path) for path in stale_files],
        }
    elif unavailable_sources:
        health = {
            "status": "sources_unavailable",
            "code": "project_sources_unavailable",
        }
    if unavailable_sources:
        # Reported alongside whichever state won, so a missing secondary source
        # is never hidden by a stale digest and a stale digest is never hidden by
        # a missing secondary source.
        health["unavailable_sources"] = [_redact(source_id) for source_id in unavailable_sources]
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
                "status": (
                    "pending"
                    if source.id in pending_sources
                    else "unavailable" if source.id in unavailable_sources else "healthy"
                ),
            }
            for source in manifest.sources
        ],
        "registrations": registrations,
        "health": health,
        "sessions": sessions or [],
    }


def _record_review(registry: ProjectRegistry, project: RegisteredProject) -> RegisteredProject:
    """Record an add-time baseline only when there is no discovery content.

    Present discovery surfaces require an explicit preview and digest-bound
    acceptance. Re-adding a registered Project never ratifies changed content.
    """
    if project.reviewed_digest or load_project_manifest(project.registrations[-1].path).sources:
        return project
    bundle_dir, checkout_dir = resolve_primary_checkout(project, registry=registry)
    digest, hashes = compute_review_digest(bundle_dir, checkout_dir)
    if stale_review_paths("", {}, hashes):
        return project
    return registry.record_review(project.id, digest, hashes)


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
    state = request.app["state"]
    live_slots = _live_slots_snapshot(state)
    session_index = await asyncio.to_thread(_project_sessions_by_id, state, live_slots)

    def _read() -> list[dict[str, Any]]:
        return [
            _project_payload(project, session_index.get(project.id, []), registry)
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
        await asyncio.to_thread(_project_payload, project, sessions, _registry(request))
    )


def _project_sandbox_error(exc: ProjectSandboxUnavailableError) -> web.Response:
    return web.json_response({"error": _redact(str(exc)), "code": exc.code}, status=503)


def project_manifest_error(project_id: str, exc: ValueError) -> web.Response:
    """Expose parser diagnostics without leaking credentials from manifest text."""
    return web.json_response(
        {
            "error": "project_manifest_invalid",
            "code": "project_manifest_invalid",
            "project_id": project_id,
            "detail": _redact(str(exc)),
        },
        status=409,
    )


def _checkout_diverged_error(exc: ProjectCheckoutDivergedError) -> web.Response:
    """Report every checkout the sync advanced, and every one that could not.

    A Project syncs N independent Git repositories, so the answer is a pair of
    lists rather than one tree: ``advanced`` names what did fast-forward and stays
    fast-forwarded, ``diverged`` names each tree that refused with its own
    ``detail``. ``error`` carries the code rather than prose because the remedy
    differs per detail (push or rebase local commits, settle a dirty tree, re-add
    a Project whose remote was replaced), so the dashboard renders the sentence
    and the backend names the state.
    """
    advanced = [_redact(checkout) for checkout in exc.advanced]
    diverged = [
        {"checkout": _redact(str(entry["checkout"])), "detail": str(entry["detail"])}
        for entry in exc.diverged
    ]
    if exc.failures:
        return web.json_response(
            {
                "error": exc.code,
                "code": exc.code,
                "project_id": exc.project_id,
                "advanced": advanced,
                "diverged": diverged,
                "unavailable_sources": [_redact(sid) for sid in sorted(exc.failures)],
            },
            status=409,
        )
    return web.json_response(
        {
            "error": exc.code,
            "code": exc.code,
            "project_id": exc.project_id,
            "advanced": advanced,
            "diverged": diverged,
        },
        status=409,
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
        return _record_review(registry, registry.add_local(bundle))

    try:
        project = await asyncio.to_thread(_create)
    except ProjectSandboxUnavailableError as exc:
        return _project_sandbox_error(exc)
    except (OSError, RuntimeError, ProjectManifestError, ProjectRegistryError) as exc:
        return web.json_response(
            {"error": _redact(str(exc)), "code": "project_create_failed"}, status=400
        )
    return web.json_response(
        await asyncio.to_thread(_project_payload, project, None, registry), status=201
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

    def _add() -> RegisteredProject:
        local = Path(source).expanduser()
        if local.exists():
            return _record_review(registry, registry.add_local(local))
        return _record_review(registry, GitProjectStore(registry).add(source))

    try:
        project = await asyncio.to_thread(_add)
    except ProjectSandboxUnavailableError as exc:
        return _project_sandbox_error(exc)
    except (
        OSError,
        ProjectGitError,
        ProjectManifestError,
        ProjectRegistryError,
    ) as exc:
        return web.json_response(
            {"error": _redact(str(exc)), "code": "project_add_failed"}, status=400
        )
    return web.json_response(
        await asyncio.to_thread(_project_payload, project, None, registry), status=201
    )


async def api_project_sync(request: web.Request) -> web.Response:
    """Fast-forward the managed Git materialization for one Project."""

    denied = await _owner_only(request, "project_sync")
    if denied is not None:
        return denied
    await _warm_project_services(request)
    registry = _registry(request)
    try:
        project, result = await asyncio.to_thread(
            GitProjectStore(registry).sync, request.match_info["id"]
        )
    except ProjectManifestError as exc:
        return project_manifest_error(request.match_info["id"], exc)
    except ProjectSandboxUnavailableError as exc:
        return _project_sandbox_error(exc)
    except ProjectCheckoutDivergedError as exc:
        return _checkout_diverged_error(exc)
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
    # Deliberately NOT re-recording the review: the digest is recomputed by the
    # payload against the record the owner set, so a fast-forward that landed a
    # new MCP or agent definition comes back as review_stale here instead of
    # being ratified by the very pull that introduced it.
    response = await asyncio.to_thread(
        _project_payload, project, None, registry, tuple(result.failures)
    )
    if result.failures:
        response["unavailable_sources"] = [_redact(sid) for sid in sorted(result.failures)]
    return web.json_response(response)


def _review_snapshot(registry: ProjectRegistry, project: RegisteredProject) -> tuple[dict, dict]:
    bundle, checkout = resolve_primary_checkout(project, registry=registry)
    return review_preview(
        bundle,
        checkout,
        project.reviewed_digest,
        project.reviewed_files,
        # A Project declaring sources must show the manifest itself: accepting it
        # is what authorizes cloning the hosts those sources name.
        require_manifest_review=bool(load_project_manifest(bundle).sources),
    )


async def api_project_review_preview(request: web.Request) -> web.Response:
    """Show precisely the stale files and the digest required for acceptance."""
    denied = await _owner_only(request, "project_review_preview")
    if denied is not None:
        return denied
    await _warm_project_services(request)
    registry = _registry(request)
    try:
        project = await asyncio.to_thread(registry.get, request.match_info["id"])
        preview, _hashes = await asyncio.to_thread(_review_snapshot, registry, project)
    except ProjectRegistryError:
        return web.json_response(
            {"error": "project not found", "code": "project_not_found"}, status=404
        )
    except (ProjectManifestError, ProjectSessionError) as exc:
        if isinstance(exc, ProjectManifestError) or exc.code == "project_manifest_invalid":
            return project_manifest_error(request.match_info["id"], exc)
        return web.json_response(
            {"error": _redact(str(exc)), "code": "project_review_failed"}, status=409
        )
    except (OSError, ProjectGitError) as exc:
        return web.json_response(
            {"error": _redact(str(exc)), "code": "project_review_failed"}, status=409
        )
    return web.json_response(_redact_json_value(preview))


async def api_project_review(request: web.Request) -> web.Response:
    """Accept only the digest the owner previewed; never ratify unreadable state."""
    denied = await _owner_only(request, "project_review")
    if denied is not None:
        return denied
    await _warm_project_services(request)
    payload = await _json_object(request)
    expected = payload.get("digest") if payload else None
    if not isinstance(expected, str) or not expected:
        return web.json_response(
            {"error": "digest must be a non-empty string", "code": "project_invalid_request"},
            status=400,
        )
    registry = _registry(request)
    project_id = request.match_info["id"]

    def _review() -> tuple[RegisteredProject, dict, bool]:
        project = registry.get(project_id)
        store = GitProjectStore(registry)
        # Retry only unavailable sources. Materialization reuses matching trees;
        # it does not fetch or silently accept content that arrives during retry.
        _bundle, _checkout, unavailable = describe_project_sources(project, registry=registry)
        if unavailable:
            store.materialize_registered_sources(project)
        preview, hashes = _review_snapshot(registry, project)
        if preview["digest"] != expected:
            return project, preview, False
        if not unreviewable_files(hashes):
            # Audit the trust transition before writing its authority record.
            _sel().log_governance_decision(
                session_key="dashboard:projects",
                tool_name="project_review",
                scope="project_review",
                item=project_id,
                outcome="allowed",
                rule="operator_reviewed_project",
                reason="operator accepted the previewed Project digest",
                critical=True,
            )
            project = registry.record_review(project_id, preview["digest"], hashes)
            # Stage one accepted the manifest, so the sources it declares may now
            # be cloned. Their trees carry their own `.kiro/`, which enters the
            # digest and puts the Project back to review-stale for stage two --
            # the owner accepts the checkout's surfaces having already accepted
            # the definition that produced them. An unreachable URL is reported
            # through health, exactly as it is on sync.
            try:
                store.materialize_registered_sources(project)
            except (ProjectSandboxUnavailableError, ProjectManifestError):
                raise
            except (ProjectGitError, OSError, RuntimeError):
                logger.warning(
                    "Project source materialization failed for %s", project_id, exc_info=True
                )
        return project, preview, True

    try:
        project, preview, matches = await asyncio.to_thread(_review)
    except ProjectSandboxUnavailableError as exc:
        return _project_sandbox_error(exc)
    except ProjectRegistryError:
        return web.json_response(
            {"error": "project not found", "code": "project_not_found"}, status=404
        )
    except (ProjectManifestError, ProjectSessionError) as exc:
        if isinstance(exc, ProjectManifestError) or exc.code == "project_manifest_invalid":
            return project_manifest_error(request.match_info["id"], exc)
        return web.json_response(
            {"error": _redact(str(exc)), "code": "project_review_failed"}, status=409
        )
    except (OSError, ProjectGitError) as exc:
        return web.json_response(
            {"error": _redact(str(exc)), "code": "project_review_failed"}, status=409
        )
    if not matches:
        return web.json_response(
            {
                "error": "Project files changed; review the fresh preview",
                "code": "project_review_moved",
                "preview": _redact_json_value(preview),
            },
            status=409,
        )
    if any(entry["status"] == "unreadable" for entry in preview["files"]):
        return web.json_response(
            {
                "error": "Project files cannot be fully displayed; replace unreadable files and review again",
                "code": "project_review_unreviewable",
                "preview": _redact_json_value(preview),
            },
            status=409,
        )
    return web.json_response(await asyncio.to_thread(_project_payload, project, None, registry))


async def api_project_remove(request: web.Request) -> web.Response:
    """Forget one Project's registration, then delete the two roots Crew owns.

    Removal is TWO operations, not one, and the response says which of them
    happened. Forgetting the record (``registry.unregister``) is instant and is
    what a session resolves through; deleting the on-disk roots
    (``projects/state/<id>/`` and ``projects/managed/<id>/``) is slow and can
    partially fail. Post-conditions, each one tested:

    * audit unwritable -> ``409 project_remove_failed`` ``detail:
      audit-unwritable``; the registration is intact and NOTHING is removed, so
      the owner can retry once SEL is writable.
    * unregister raises -> the registration is unchanged and the error
      propagates, because no file has been touched yet.
    * unregister succeeded, cleanup did not -> ``200`` with ``cleanup_pending``
      naming what remains under ``projects/``. The registration IS gone: that is
      the safe end state, the leftover trees are regenerable, and a completed
      removal is never reported as a failure.

    A local bundle registered by path is never deleted -- the owner chose that
    directory and it is theirs.
    """

    denied = await _owner_only(request, "project_remove")
    if denied is not None:
        return denied
    await _warm_project_services(request)
    project_id = request.match_info["id"]
    registry = _registry(request)

    def _audit() -> None:
        _sel().log_governance_decision(
            session_key="dashboard:projects",
            tool_name="project_remove",
            scope="project_registry",
            item=project_id,
            outcome="allowed",
            rule="operator_removed_project",
            reason="operator removed Project registration",
            critical=True,
        )

    def _remove() -> list[str]:
        # Unregister first: a Project a session cannot resolve is the safe end
        # state, and the derived checkouts are regenerable, so a failure to clean
        # them up never leaves a live registration pointing at removed source
        # data.
        registry.unregister(project_id)
        return GitProjectStore(registry).remove_derived_state(project_id)

    try:
        # Existence decides the 404, and it decides it BEFORE the audit, so an
        # unknown id leaves no governance record claiming a removal that never
        # had anything to remove.
        await asyncio.to_thread(registry.get, project_id)
    except ProjectRegistryError:
        return web.json_response(
            {"error": "project not found", "code": "project_not_found"}, status=404
        )
    try:
        # Audited BEFORE the removal, not after: the record is the authority for
        # an irreversible operator action, so an unwritable audit must cost the
        # removal rather than the record. Auditing afterwards could only report a
        # removal that had already happened as a failure.
        await asyncio.to_thread(_audit)
    except Exception:
        logger.error("SEL audit for Project remove failed", exc_info=True)
        return web.json_response(
            {
                "error": "Project removal audit is unavailable",
                "code": "project_remove_failed",
                "detail": "audit-unwritable",
            },
            status=409,
        )
    try:
        cleanup_pending = await asyncio.to_thread(_remove)
    except ProjectRegistryError:
        return web.json_response(
            {"error": "project not found", "code": "project_not_found"}, status=404
        )
    except (OSError, ProjectGitError) as exc:
        # Reachable only BEFORE the registration is forgotten -- cleanup itself
        # reports what it could not delete rather than raising -- so the record is
        # still there and the owner can retry.
        return web.json_response(
            {"error": _redact(str(exc)), "code": "project_remove_failed"}, status=409
        )
    if cleanup_pending:
        # The record is gone, so this is a success with a warning, not a failure:
        # reporting 409 here would tell the owner the Project survived when it did
        # not, which is the contradiction that kept this response wrong.
        return web.json_response(
            {"ok": True, "id": project_id, "cleanup_pending": [_redact(p) for p in cleanup_pending]}
        )
    return web.json_response({"ok": True, "id": project_id})
