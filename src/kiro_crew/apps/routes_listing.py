"""Reading installed apps: the list, one app, its manifest and config, and the publish providers.

``handle_list_apps``, ``handle_get_app`` and ``handle_get_manifest`` with the trust
repository each listed app is stamped with, ``handle_app_config`` (an app's own
``config.json``, written only by its owner or within an app token's scope), and
``handle_publish_providers`` over ``collect_publish_providers``.

Part of the ``kiro_crew.apps.routes`` family: callers and tests reach every name here
as ``kiro_crew.apps.routes.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

from kiro_crew.apps.backend import list_app_processes
from kiro_crew.apps.hooks_integration import get_all_hook_health
from kiro_crew.apps.manager import (
    _credential_free_source_metadata,
    apps_dir,
    get_app,
    get_app_manifest,
    list_apps,
)
from kiro_crew.apps.manifest import app_endpoint_allowed
from kiro_crew.apps.registry import _strip_git_target_userinfo, resolve_installed_trust_repository
from kiro_crew.apps.routes_lifecycle import _redact_warning
from kiro_crew.apps.update_status import attach_update, attach_updates
from kiro_crew.publish_governance import DEPLOY_WEB_PROVIDER_ID, publish_denied_reason
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.apps.routes")


def _stamp_installed_trust_repository(app: dict[str, Any]) -> dict[str, Any]:
    """Overwrite the consent target from server-resolved provenance."""
    app.pop("trustRepository", None)
    try:
        resolved, repository = resolve_installed_trust_repository(app)
    except Exception:
        # Catalog failure or corrupt registry state must not make the app list
        # fail. Omitting the proof leaves the grant handler fail-closed.
        logger.warning(
            "could not resolve trust repository for installed app %r",
            app.get("name", ""),
            exc_info=True,
        )
        resolved, repository = False, ""
    if resolved and repository:
        app["trustRepository"] = repository
    # Installed provenance and manifest metadata are also returned by the apps
    # API. Keep clone credentials server-side even when an old install record
    # persisted a credential-bearing source URL.
    source = app.get("source")
    if isinstance(source, str):
        app["source"] = _credential_free_source_metadata(source)
    for coordinate_key in ("sourceUrl", "repo", "gitUrl"):
        coordinate = app.get(coordinate_key)
        if isinstance(coordinate, str):
            app[coordinate_key] = _strip_git_target_userinfo(coordinate)
    source_registry = app.get("sourceRegistry")
    if isinstance(source_registry, str):
        app["sourceRegistry"] = _credential_free_source_metadata(source_registry)
    manifest = app.get("manifest")
    if isinstance(manifest, dict):
        manifest_repo = manifest.get("repo")
        if isinstance(manifest_repo, str):
            manifest["repo"] = _strip_git_target_userinfo(manifest_repo)
    return app


def _installed_app_detail(name: str, app: dict[str, Any]) -> dict[str, Any]:
    """One installed app's row: trust stamp, then its cached update state.

    Blocking (both halves touch disk), so async handlers offload the whole
    helper. What ``update`` means, and why it is absent rather than false when
    nothing is known: :func:`~kiro_crew.apps.update_status.attach_update`.
    """
    return attach_update(name, _stamp_installed_trust_repository(app))


def _listed_apps_with_trust() -> list[dict[str, Any]]:
    """Read installed apps and resolve their consent coordinates synchronously.

    Both halves may touch disk (and the legacy provenance resolver may consult
    registry/catalog state), so async handlers must offload this whole helper
    rather than moving only :func:`list_apps` off the event loop.
    """
    installed = list_apps()
    for app in installed:
        _stamp_installed_trust_repository(app)
    # Same ``update`` field the single-app route carries, and for a reason that
    # is not symmetry for its own sake: a side-panel tab reads its row from this
    # endpoint while a routed page reads the other, so an app's own
    # "new version" banner would appear on one surface and silently never on the
    # other. Batched, so the caches are read once rather than once per app.
    return attach_updates(installed)


async def handle_list_apps(request: web.Request) -> web.Response:
    """GET /api/apps — list all installed apps."""
    # list_apps() walks the apps dir and reads two files per installed app, and
    # this endpoint re-runs it on every dashboard refresh — so the walk goes off
    # the loop (its cost scales with installed app count).
    apps = await asyncio.to_thread(_listed_apps_with_trust)
    # Enrich with backend process status
    procs = {p["app_name"]: p for p in list_app_processes()}
    # ...and with in-process hook wiring health, which has no process to inspect:
    # an app whose route hook failed to import has no route-table entry, so the
    # dispatcher answers 404 exactly like an app that was never installed. This is
    # the only place that failure becomes visible to an operator.
    #
    # Reported under the same "hooks" envelope and the same "health_status" key the
    # enable response already uses, so one record has ONE public spelling rather
    # than a second flat name to maintain. The envelope also keeps the subsystem
    # explicit: this is hook-wiring health, not the subprocess backend_status.
    hook_health = get_all_hook_health()
    for app in apps:
        proc = procs.get(app["name"])
        if proc:
            app["backend_status"] = {
                # Both flags come from the tracking record rather than being asserted
                # from its mere presence: a tracked backend can have exited (running) or
                # stopped answering its health endpoint (healthy) since it was started.
                "running": proc["running"],
                "port": proc["port"],
                "healthy": proc["healthy"],
                "pid": proc["pid"],
            }
        health = hook_health.get(app["name"])
        if health:
            if health.get("issues"):
                health["issues"] = [_redact_warning(i) for i in health["issues"]]
            app["hooks"] = {"health_status": health}
    return web.json_response(apps)


def _provider_is_configured(app_name: str, pp: dict[str, Any]) -> bool:
    """Resolve a provider's configured-state by reading the app's persisted config.

    Core never imports app code: it reads ``<apps_dir>/<app>/data/<configFile>`` and
    checks that ``configuredField`` is non-empty. When no ``configuredField`` is
    declared, the provider is considered configured as soon as the app is enabled.
    """
    field_name = str(pp.get("configuredField", "")).strip()
    if not field_name:
        return True
    config_file = str(pp.get("configFile", "config.json")) or "config.json"
    if ".." in config_file or "/" in config_file or "\\" in config_file:
        return False  # defensive: no path traversal in the declared config filename
    cfg_path = apps_dir() / app_name / "data" / config_file
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return bool(isinstance(cfg, dict) and str(cfg.get(field_name, "")).strip())


def collect_publish_providers(
    apps: list[dict[str, Any]],
    configured_resolver: Any = None,
) -> list[dict[str, Any]]:
    """Aggregate **enabled** apps that declare a publishProvider (design §1.3, Route B).

    Pure and testable — pass ``configured_resolver(app_name, pp_dict) -> bool`` to avoid
    touching the filesystem in tests. Each returned provider carries a ``configured``
    flag so the artifact page can render the publish action when configured or a
    "set it up" link otherwise. Built-in providers (e.g. the internal registry) are registered
    on the frontend; this function contributes only the app-declared ones.

    Endpoint allowlist (§9.3 security): app-declared provider endpoints MUST match
    ``/api/apps/<that-app>/`` — an app cannot declare an endpoint that routes to
    another app's namespace or to a core API. Non-conforming endpoints are dropped
    with a warning log.
    """
    resolver = configured_resolver or _provider_is_configured
    providers: list[dict[str, Any]] = []
    for app in apps:
        if not app.get("enabled"):
            continue
        manifest = app.get("manifest") or {}
        pp = manifest.get("publishProvider") or {}
        if not isinstance(pp, dict) or not pp.get("id") or not pp.get("endpoint"):
            continue
        app_name = str(app.get("name", ""))
        endpoint = str(pp["endpoint"])
        # Endpoint allowlist: must route within the app's own namespace. The check lives
        # in `manifest.app_endpoint_allowed` because more than one contribution type
        # declares an endpoint, and two copies of one security control drift apart
        # invisibly -- the traversal and sibling-prefix guards are documented there.
        if not app_endpoint_allowed(app_name, endpoint):
            logger.warning(
                "publish provider for app %r declares non-conforming endpoint %r "
                "(must start with %r, no traversal) — dropping",
                app_name,
                endpoint,
                f"/api/apps/{app_name}/",
            )
            continue
        providers.append(
            {
                "id": str(pp["id"]),
                "label": str(pp.get("label", pp["id"])),
                "icon": str(pp.get("icon", "")),
                "endpoint": endpoint,
                "kinds": [str(k) for k in pp.get("kinds", []) if k],
                "setupRoute": str(pp.get("setupRoute", "")),
                "app": app_name,
                "origin": "app",
                "configured": bool(resolver(app_name, pp)),
            }
        )
    return providers


async def handle_publish_providers(request: web.Request) -> web.Response:
    """GET /api/publish-providers — publish destinations (app-declared + core deploy).

    Returns enabled apps' publish providers plus the core deploy provider (folded
    from the former deploy_web app), each with a ``configured`` flag. Built-in
    providers (the internal registry) are registered frontend-side and are not returned here.

    The core deploy row is omitted when EITHER control closes the public-web
    path, and the two are independent:

    * the platform's ``external_access`` policy withholds cloud deployment; or
    * the publish-governance chokepoint denies its destination id (governance
      ceiling ∩ ``publish.allowed_destinations``), so an operator who has closed
      the path never sees the button.

    Because this list is what the Publish panel renders, that single omission is
    also what makes the panel correct — a deployment that registers only an
    internal destination shows only that one, with no frontend change. Omission is
    presentation only: ``/api/deploy/deploy`` consults the same chokepoint itself,
    because a filtered list is not a control.

    On EITHER closed path, rows carrying ``DEPLOY_WEB_PROVIDER_ID`` are dropped
    from the app-declared list too — the platform withhold and the governance
    denial share one closed path for exactly this reason.
    ``collect_publish_providers`` validates
    a declared *endpoint* (it must sit under the app's own namespace) but not the
    declared *id*, so an enabled app may publish a row under the core
    destination's id — and ``PublishHub`` routes a click at
    ``selected.app.endpoint``, which this chokepoint does not cover. Serving that
    row after the operator closed the destination would hand back the path they
    closed.

    On the PERMITTED path such a row is left alone. An app shadowing this id is
    pre-existing, reachable behaviour that ``test_publish_providers`` documents
    (its fixture app declares this very id and the test asserts the APP's endpoint
    is the one resolved for it, first-match order). Whether the core provider
    should own the id outright is a behaviour change worth making on its own
    merits, not a side effect of closing a denial hole.
    """
    # Both the apps-dir walk (list_apps) and the per-provider configured-state
    # probe (_provider_is_configured reads each app's persisted config file)
    # touch disk, so the whole collection runs off the loop — same shape as the
    # deploy registry read below.
    providers = await asyncio.to_thread(lambda: collect_publish_providers(list_apps()))
    # Two independent controls can close this destination, and BOTH must land on
    # the same closed path — an early return for one of them is how a closed
    # destination stays reachable (an app-declared row carrying the core id
    # publishes at its OWN endpoint, which this chokepoint does not cover).
    # circular import: apps.routes is imported by the dashboard handler layer, so
    # reaching back into it must be a function-local downward import.
    from kiro_crew.dashboard.handlers._shared import admits_cloud_deployment

    # Gate 1 — platform: advertising a destination whose every mutating route
    # refuses is worse than not advertising it. In a worker thread with the
    # registry read below: the admission path can initialize the SEL audit log,
    # which is blocking file IO on a fresh gateway.
    admits_cloud = await asyncio.to_thread(admits_cloud_deployment, "aws")

    # Gate 2 — operator: governance ceiling ∩ publish.allowed_destinations. Only
    # consulted when gate 1 admits, because the row is dropped either way and this
    # one reads policy + config off disk. A PlatformCompositionError propagates —
    # fail-closed CPP, same as every other publish surface.
    deploy_denied = None
    if admits_cloud:
        # The core row's endpoint is ``/api/deploy/deploy``, which checks the
        # publish lists itself.
        deploy_denied = await asyncio.to_thread(
            lambda: publish_denied_reason(
                request, DEPLOY_WEB_PROVIDER_ID, lists_checked_by_caller=True
            )
        )

    if not admits_cloud or deploy_denied:
        reason = deploy_denied or "the platform withholds cloud deployment"
        logger.info(
            "publish provider %r omitted from the registry: %s",
            DEPLOY_WEB_PROVIDER_ID,
            reason,
        )
        for squatter in [p for p in providers if p.get("id") == DEPLOY_WEB_PROVIDER_ID]:
            logger.warning(
                "app %r declares the closed publish destination id %r — dropping its "
                "row too, or the operator's shutdown would be undone by an install",
                squatter.get("app", "?"),
                DEPLOY_WEB_PROVIDER_ID,
            )
        return web.json_response(
            {
                "providers": [p for p in providers if p.get("id") != DEPLOY_WEB_PROVIDER_ID],
            }
        )
    # An app row under the core id publishes at the app's own endpoint, which does
    # not check the publish lists, so it is dropped whenever they restrict.
    if any(p.get("id") == DEPLOY_WEB_PROVIDER_ID for p in providers):
        squat_denied = await asyncio.to_thread(
            lambda: publish_denied_reason(request, DEPLOY_WEB_PROVIDER_ID)
        )
        if squat_denied:
            providers = [p for p in providers if p.get("id") != DEPLOY_WEB_PROVIDER_ID]
    try:
        from kiro_crew.deploy import profiles as _deploy_profiles

        # Align with deploy/handlers.py: registry reads go through to_thread.
        reg = await asyncio.to_thread(_deploy_profiles.load_registry)
        configured = bool(reg["profiles"])
    except Exception:
        configured = False
    providers.append(
        {
            "id": DEPLOY_WEB_PROVIDER_ID,
            "label": "Publish to public web (your AWS)",
            "icon": "Globe",
            "endpoint": "/api/deploy/deploy",
            "kinds": ["widget", "html", "markdown"],
            "setupRoute": "/artifacts/deploy",
            "app": "",
            "origin": "core",
            "configured": configured,
        }
    )
    return web.json_response({"providers": providers})


async def handle_get_app(request: web.Request) -> web.Response:
    """GET /api/apps/{name} — get single app details."""
    name = request.match_info["name"]
    info = await asyncio.to_thread(get_app, name)
    if not info:
        # Compat: migrated deploy-web requests hit this generic handler before
        # the deploy module's /api/apps/deploy-web/{tail} redirect (aiohttp
        # matches in registration order). Redirect to the canonical endpoint.
        if name == "deploy-web":
            raise web.HTTPTemporaryRedirect(location="/api/deploy/list")
        return web.json_response({"error": f"app {name!r} not installed"}, status=404)
    return web.json_response(await asyncio.to_thread(_installed_app_detail, name, info))


async def handle_get_manifest(request: web.Request) -> web.Response:
    """GET /api/apps/{name}/manifest — get app manifest."""
    name = request.match_info["name"]
    manifest = await asyncio.to_thread(get_app_manifest, name)
    if not manifest:
        # Compat: migrated deploy-web — redirect to canonical endpoint.
        if name == "deploy-web":
            raise web.HTTPTemporaryRedirect(location="/api/deploy/config")
        return web.json_response({"error": f"app {name!r} not installed"}, status=404)
    return web.json_response(manifest.to_dict())


async def handle_app_config(request: web.Request) -> web.Response:
    """GET/PUT /api/apps/{name}/config — read or write app config.json.

    Reads/writes ``~/.kiro/crew/apps/{name}/data/config.json``.
    GET returns the current config (empty ``{}`` if none exists).
    PUT replaces the config with the request body. A dashboard subject must be
    the owner to PUT; an app token reaches this handler only inside the scope
    ``token_auth`` already granted it (its own app, or a manifest grant).
    """
    name = request.match_info["name"]
    info = await asyncio.to_thread(get_app, name)
    if not info:
        # Compat: migrated deploy-web — redirect to canonical deploy config endpoint.
        if name == "deploy-web":
            raise web.HTTPTemporaryRedirect(location="/api/deploy/config")
        return web.json_response({"error": f"app {name!r} not installed"}, status=404)

    from kiro_crew.apps.manager import app_data_dir
    from kiro_crew.atomic_write import atomic_write

    data_dir = app_data_dir(name)
    config_path = data_dir / "config.json"

    if request.method == "GET":
        if not await asyncio.to_thread(config_path.is_file):
            # Missing config (e.g. data dir wiped by an app update) — seed an
            # empty config so the app gets a valid response instead of hanging
            # on a perpetual "loading" state. The app repopulates it on first use.
            try:
                await asyncio.to_thread(atomic_write, config_path, "{}\n")
            except OSError:
                pass
            return web.json_response({})
        try:
            text = await asyncio.to_thread(config_path.read_text, encoding="utf-8")
            return web.json_response(json.loads(text))
        except (json.JSONDecodeError, OSError) as exc:
            return web.json_response({"error": f"failed to read config: {exc}"}, status=500)

    # PUT — write config.
    # An app token was already scoped by token_auth's _enforce_app_scope (its own
    # app via _app_owns_path, a foreign app only with a manifest grant), so only a
    # dashboard subject (an empty or missing app claim) needs the owner check.
    if not request.get("app"):
        from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

        denied = await require_owner_dashboard_request(request, "app_config_write")
        if denied is not None:
            return denied

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"error": "config must be a JSON object"}, status=400)

    try:
        content = json.dumps(body, indent=2) + "\n"
        await asyncio.to_thread(atomic_write, config_path, content)
    except OSError as exc:
        return web.json_response({"error": f"failed to write config: {exc}"}, status=500)

    sel().log_api_access(
        caller="dashboard",
        operation="app_config_write",
        outcome="completed",
        resources=name,
    )
    return web.json_response({"ok": True, "name": name})
