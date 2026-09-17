"""AgentCore Observatory — backend routes.

Builtin-app contract: ``register_routes(app: web.Application) -> None`` registering
FULL paths directly on the gateway router. This is NOT the external-app
``AppRoute``-list contract — mixing them up produces routes that silently never
dispatch.

Every handler is wrapped in :func:`_require_enabled`: builtin routes exist from
gateway startup even while the app is disabled, so a default-disabled opt-in app
would otherwise stay callable.

**Loading is lazy, and that is a hard requirement rather than a preference.**
There are 27 resource types and each read forks an ``aws`` CLI subprocess, so an
eager overview would be 27 sequential subprocesses — tens of seconds before first
paint. ``GET /catalog`` therefore answers from the in-process table and makes NO
AWS call at all; a type is queried only when ``GET /resource/{type}`` asks for it.
The reads are not parallelised either: concurrent CLI invocations race each other
resolving the same SSO token file.

The only write is ``PUT /config``, and it writes a profile NAME and a region to
the app's own data dir — never a credential, and never anything in AWS.
"""

from __future__ import annotations

import asyncio
import logging
from functools import wraps
from typing import Any, Awaitable, Callable

from aiohttp import web

from kiro_crew.apps.builtins.agentcore_observatory.backend import agentcore, catalog
from kiro_crew.apps.builtins.agentcore_observatory.backend.config import (
    APP_NAME,
    ObservatoryConfig,
    valid_profile,
    valid_region,
)
from kiro_crew.apps.manager import is_app_enabled

logger = logging.getLogger(__name__)

_BASE = f"/api/apps/{APP_NAME}"

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

#: Cap on a submitted profile name / region. Both are already pattern-validated;
#: this refuses an oversized body before the regex ever runs on it.
_MAX_FIELD_LEN = 256


def _require_enabled(handler: Handler) -> Handler:
    """Deny every request while the app is disabled (deny-by-default)."""

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.StreamResponse:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return web.json_response(
                {"error": f"{APP_NAME} is disabled", "code": "app_disabled"}, status=403
            )
        return await handler(request)

    return _wrapped


def _bad_request(code: str, message: str) -> web.Response:
    """A 400 body, always carrying a machine-readable ``code``.

    Backend-owned strings have no i18n catalog path, so the UI localises the code
    and never renders the English prose beside it.

    There is one helper per status rather than one taking ``status``, because
    ``test_error_code_contract.py`` can only verify a response whose status is a
    LITERAL and whose body is an inline dict — a computed status lands in its
    ``dynamic_status`` ratchet and a hoisted body in ``opaque_body``, and the
    baseline file's own rule is that a number is never raised to make CI pass.
    """
    return web.json_response({"code": code, "error": message}, status=400)


def _not_found(code: str, message: str) -> web.Response:
    """A 404 body. See :func:`_bad_request` for why the status is not a parameter."""
    return web.json_response({"code": code, "error": message}, status=404)


def _conflict(code: str, message: str) -> web.Response:
    """A 409 body. See :func:`_bad_request` for why the status is not a parameter."""
    return web.json_response({"code": code, "error": message}, status=409)


async def _handle_get_config(_request: web.Request) -> web.StreamResponse:
    """The saved profile name and region. Never a credential."""
    cfg = await asyncio.to_thread(ObservatoryConfig.load)
    return web.json_response(cfg.to_dict())


async def _handle_put_config(request: web.Request) -> web.StreamResponse:
    """Save the profile name and region.

    Both are validated here AND on read: this route is not the only writer of
    that file, so the loader cannot trust it.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is a 400, not a 500
        return _bad_request("invalid_json", "request body is not JSON")
    if not isinstance(body, dict):
        return _bad_request("invalid_json", "request body is not a JSON object")

    profile = str(body.get("profile", "") or "")
    region = str(body.get("region", "") or "")
    if len(profile) > _MAX_FIELD_LEN or len(region) > _MAX_FIELD_LEN:
        return _bad_request("field_too_long", "profile or region is too long")
    # An empty profile is legitimate — it lets the CLI resolve its own default.
    if profile and not valid_profile(profile):
        return _bad_request("invalid_profile", "not a well-formed AWS profile name")
    if not valid_region(region):
        return _bad_request("invalid_region", "not a well-formed AWS region")

    cfg = ObservatoryConfig(profile=profile, region=region)
    await asyncio.to_thread(cfg.save)
    return web.json_response(cfg.to_dict())


async def _handle_profiles(_request: web.Request) -> web.StreamResponse:
    """Known profile NAMES and the region each one declares about itself.

    Sourced from the core deploy registry (``kiro_crew.deploy.profiles``), not
    from another app: a builtin importing another builtin is a coupling no app
    here has, and this list is core state rather than any one app's.

    Names and regions only — never a credential, and never an account id, which
    is a live probe's result rather than a fact the registry can assert. An
    unreadable or empty registry yields an empty list, and the UI keeps its
    free-text field, so a crew that never used the deploy surface is not blocked.
    """

    def _load() -> list[dict[str, str]]:
        try:
            from kiro_crew.deploy.profiles import load_registry

            registry = load_registry()
        except Exception:  # noqa: BLE001 - suggestions are a convenience, never a gate
            logger.debug("profile registry unavailable", exc_info=True)
            return []
        out: list[dict[str, str]] = []
        for entry in registry.get("profiles", []):
            name = str(entry.get("name", ""))
            if not name or not valid_profile(name):
                continue
            region = str(entry.get("region", ""))
            out.append({"name": name, "region": region if valid_region(region) else ""})
        return out

    return web.json_response({"profiles": await asyncio.to_thread(_load)})


async def _handle_catalog(_request: web.Request) -> web.StreamResponse:
    """The rail's skeleton: groups and their root types. Makes NO AWS call.

    This is what keeps first paint instant with 27 types on the table. The rail
    renders from here, and each item fetches itself on open.
    """
    cfg = await asyncio.to_thread(ObservatoryConfig.load)
    groups: list[dict[str, Any]] = []
    for group in catalog.GROUPS:
        types = [
            {
                "id": rt.id,
                "listable": rt.listable,
                "idField": rt.id_field,
                "children": [
                    {
                        "id": child.id,
                        "parentParams": list(child.parent_params),
                        "parentFields": list(child.parent_fields),
                    }
                    for child in catalog.children_of(rt.id)
                ],
            }
            for rt in catalog.root_types()
            if rt.group == group
        ]
        if types:
            groups.append({"id": group, "types": types})
    return web.json_response({"config": cfg.to_dict(), "groups": groups})


def _parent_ids_from_query(request: web.Request, params: tuple[str, ...]) -> dict[str, str]:
    """Read a child's parent flags from the query string.

    The query key is the flag without its leading dashes, so
    ``?gateway-identifier=gw-1`` supplies ``--gateway-identifier``. Validation of
    the VALUE stays in the query layer, which is the thing that builds the argv.
    """
    return {param: request.query.get(param.lstrip("-"), "") or "" for param in params}


async def _handle_resource(request: web.Request) -> web.StreamResponse:
    """List one resource type, on demand."""
    type_id = request.match_info.get("type_id", "")
    rt = catalog.by_id(type_id)
    if rt is None:
        return _not_found("unknown_type", f"unknown resource type {type_id!r}")

    cfg = await asyncio.to_thread(ObservatoryConfig.load)
    if not cfg.configured:
        return _conflict("not_configured", "no AWS region is configured")

    if not rt.listable:
        # A singleton has no list; serve its single object under the same route
        # so the UI needs no special case to open a rail item.
        obj = await asyncio.to_thread(agentcore.get_resource, cfg, type_id, {})
        return web.json_response({"type": type_id, "singleton": obj.to_dict()})

    parents = _parent_ids_from_query(request, rt.parent_params)
    result = await asyncio.to_thread(agentcore.list_resource, cfg, type_id, parents)
    return web.json_response({"type": type_id, "list": result.to_dict()})


async def _handle_resource_detail(request: web.Request) -> web.StreamResponse:
    """Fetch one object with its type's ``get-*`` verb.

    Identifier flags arrive as query parameters keyed by the flag without dashes,
    because the flags differ per type (``--memory-id``, ``--name``,
    ``--gateway-identifier`` + ``--target-id``) and the catalog, not this route,
    is what knows them.
    """
    type_id = request.match_info.get("type_id", "")
    rt = catalog.by_id(type_id)
    if rt is None:
        return _not_found("unknown_type", f"unknown resource type {type_id!r}")
    if not rt.get_verb:
        return _bad_request("no_detail", f"{type_id} has no get operation")

    cfg = await asyncio.to_thread(ObservatoryConfig.load)
    if not cfg.configured:
        return _conflict("not_configured", "no AWS region is configured")

    id_args = {f"--{key}": value for key, value in request.query.items() if key and value}
    result = await asyncio.to_thread(agentcore.get_resource, cfg, type_id, id_args)
    return web.json_response({"type": type_id, "detail": result.to_dict()})


def register_routes(app: web.Application) -> None:
    """Register this app's routes on the gateway's aiohttp Application."""
    router = app.router
    router.add_get(f"{_BASE}/config", _require_enabled(_handle_get_config))
    router.add_put(f"{_BASE}/config", _require_enabled(_handle_put_config))
    router.add_get(f"{_BASE}/profiles", _require_enabled(_handle_profiles))
    router.add_get(f"{_BASE}/catalog", _require_enabled(_handle_catalog))
    router.add_get(f"{_BASE}/resource/{{type_id}}", _require_enabled(_handle_resource))
    router.add_get(
        f"{_BASE}/resource/{{type_id}}/detail", _require_enabled(_handle_resource_detail)
    )
