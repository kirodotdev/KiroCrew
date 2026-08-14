"""API handlers for multi-provider MCP server discovery.

Provides ``/api/mcp/discover`` (search), ``/api/mcp/discover/detail``
(preview) and ``/api/mcp/discover/install`` — the MCP-side twin of the
skill discovery handlers in ``discover.py``.

Install REUSES the mcp.json write helpers from ``handlers/mcp.py``
(``_set_kirocrew_entry`` + the file lock) so there is exactly one code
path that mutates MCP config on disk.
"""

from __future__ import annotations

import asyncio
import logging
import re

from aiohttp import web

from kiro_crew.dashboard.handlers.discover import _redact_external
from kiro_crew.dashboard.handlers.mcp import (
    _find_server_spec_anywhere,
    _get_mcp_lock,
    _is_valid_mcp_name,
    _offload_config_write,
    _set_kirocrew_entry,
)
from kiro_crew.mcp_providers.base import ProviderRegistry, ProviderUnavailableError
from kiro_crew.mcp_providers.capability import CapabilityProvider
from kiro_crew.mcp_providers.official import OfficialRegistryProvider
from kiro_crew.mcp_utils import mcp_server_alias
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# Queries shorter than this return the cheap provider-availability probe
# without calling any provider (the frontend uses it to learn availability).
_MIN_QUERY_LEN = 2

# Budget for one detail/install fetch (longer than a search hop — a single
# upstream document fetch, but still bounded).
_DETAIL_TIMEOUT_SECS = 15.0


def _build_registry() -> ProviderRegistry:
    """Build the provider registry with all available providers.

    The official registry is registered unless the composed ``discovery`` policy
    refuses it — the seam a managed deployment uses to restrict installable
    servers to its own registry. The edition capability provider is registered
    only when the CPP capability manager reports available — external installs
    never see it.
    """
    registry = ProviderRegistry()

    from kiro_crew.dashboard.handlers._shared import admits_registry

    official = OfficialRegistryProvider()
    if admits_registry("mcp", official.name, official.api_base):
        registry.register(official)

    # The edition capability manager (CPP seam) registers as a second
    # provider only when this edition actually installs one — the public
    # Default reports unavailable, so external builds only see 'official'.
    # Imported lazily from the shared handler layer (downward import, same
    # pattern as agents.py) and injected as a factory so mcp_providers
    # stays dashboard-free.
    from kiro_crew.dashboard.handlers._shared import _capability_manager

    provider = CapabilityProvider(_capability_manager)
    if provider.is_available():
        registry.register(provider)

    return registry


# Module-level singleton — cheap to build, providers are stateless.
_registry: ProviderRegistry | None = None


def _get_registry() -> ProviderRegistry:
    """Lazy-init the global provider registry."""
    global _registry
    if _registry is None:
        _registry = _build_registry()
    return _registry


def _display_name(registry: ProviderRegistry, provider_name: str) -> str:
    """Get the human-readable display name for a provider."""
    p = registry.get(provider_name)
    return p.display_name if p else provider_name


def _derive_install_name(server_id: str) -> str:
    """Sanitized short server name — the last path segment of the id.

    The result must be slash-free: slashed mcp.json keys can never be
    referenced as ``@server`` in agent tools (see ``mcp_server_alias``),
    so a fresh install must not create one.
    """
    short = server_id.rsplit("/", 1)[-1].strip()
    return re.sub(r"[^@a-zA-Z0-9_.-]+", "-", short).strip("-.")


def _configured_server_names() -> set[str]:
    """Names of all MCP servers KiroCrew currently knows about.

    Same source as the ``/api/mcp`` list endpoint so the ``installed``
    cross-ref matches what the config table shows.
    """
    # circular import: mcp_discovery defers imports of kiro_crew.agent which
    # shares state with the handler modules.
    from kiro_crew.mcp_discovery import list_servers

    try:
        return {s.name for s in list_servers()}
    except Exception:
        logger.debug("list_servers failed during discover cross-ref", exc_info=True)
        return set()


def _same_core_spec(a: dict, b: dict) -> bool:
    """True when two specs describe the same server invocation.

    Env values are deliberately ignored — a user who filled in an API key
    placeholder still has "the same server", and reinstalling it must not
    collide with their own config.
    """
    return (
        a.get("command") == b.get("command")
        and (a.get("args") or []) == (b.get("args") or [])
        and a.get("url") == b.get("url")
    )


async def api_mcp_discover(request: web.Request) -> web.Response:
    """GET /api/mcp/discover?q=<query>[&provider=<name>][&limit=N]

    Multi-provider MCP server search. A query shorter than 2 chars returns
    ``{"results": [], "providers": [...]}`` WITHOUT calling any provider —
    the cheap availability probe the frontend gates its UI on.
    """
    query = request.query.get("q", "").strip()
    provider_filter = request.query.get("provider", "").strip() or None
    try:
        limit = max(1, min(int(request.query.get("limit", "20")), 50))
    except ValueError:
        limit = 20

    registry = await asyncio.to_thread(_get_registry)
    provider_names = [p.name for p in registry.available_providers]

    if len(query) < _MIN_QUERY_LEN:
        return web.json_response({"results": [], "providers": provider_names})

    results = await registry.search(query, provider=provider_filter, limit=limit)

    # Cross-ref against KiroCrew's configured servers so the UI can badge
    # already-installed entries. list_servers() reads config files
    # synchronously — offload it.
    configured = await asyncio.to_thread(_configured_server_names)

    items = []
    for r in results:
        # A discovered server lands in config under its sanitized short name
        # (or, for legacy slashed ids, its kiro-safe alias) — check all
        # plausible keys plus the raw provider id (edition registry ids).
        candidates = {r.id, _derive_install_name(r.id), mcp_server_alias(r.id)}
        installed = r.installed or bool(candidates & configured)
        # All provider-sourced fields are attacker-controllable — redact
        # before surfacing (same posture as the skills discover endpoint).
        items.append(
            {
                "id": _redact_external(r.id),
                "name": _redact_external(r.name),
                "title": _redact_external(r.title),
                "description": _redact_external(r.description),
                "provider": r.provider,
                "display_provider": _display_name(registry, r.provider),
                "version": _redact_external(r.version),
                "repo_url": _redact_external(r.repo_url),
                "installed": installed,
                "methods": [m for m in r.methods if isinstance(m, str)],
                "deprecated": bool(r.deprecated),
            }
        )

    sel().log_tool_invocation(
        session_key=request.get("session_key", "dashboard"),
        tool_name="discover_mcp_servers",
        tool_kind="mcp_provider_search",
        outcome="success",
        metadata={
            "query": query,
            "provider_filter": provider_filter or "all",
            "result_count": str(len(items)),
        },
    )
    return web.json_response({"results": items, "providers": provider_names})


async def api_mcp_discover_detail(request: web.Request) -> web.Response:
    """GET /api/mcp/discover/detail?provider=<p>&id=<id>

    Full detail for one discovered server, including the install preview
    (what Install would write) and the env vars the user must fill.
    """
    provider_name = request.query.get("provider", "").strip()
    server_id = request.query.get("id", "").strip()
    if not provider_name or not server_id:
        return web.json_response({"error": "Both 'provider' and 'id' are required"}, status=400)

    registry = await asyncio.to_thread(_get_registry)
    provider = registry.get(provider_name)
    if provider is None:
        return web.json_response({"error": f"Unknown provider '{provider_name}'"}, status=400)
    if not provider.is_available():
        return web.json_response(
            {"error": f"Provider '{provider_name}' is not available"}, status=503
        )

    try:
        detail = await asyncio.wait_for(
            provider.fetch_detail(server_id), timeout=_DETAIL_TIMEOUT_SECS
        )
    except (ProviderUnavailableError, asyncio.TimeoutError) as exc:
        logger.warning("MCP detail fetch failed for %s:%s: %s", provider_name, server_id, exc)
        return web.json_response(
            {"error": f"Provider '{provider_name}' is not available"}, status=503
        )
    if detail is None:
        return web.json_response({"error": f"Server '{server_id}' not found"}, status=404)

    install_plan = None
    if detail.install_plan is not None:
        # The spec is returned verbatim (not redacted): it is exactly what
        # install will write, and masking it would hide from the user what
        # they are consenting to run.
        install_plan = {
            "method": detail.install_plan.method,
            "spec": detail.install_plan.spec,
        }
    return web.json_response(
        {
            "id": _redact_external(detail.id),
            "name": _redact_external(detail.name),
            "title": _redact_external(detail.title),
            "description": _redact_external(detail.description),
            "provider": detail.provider,
            "version": _redact_external(detail.version),
            "repo_url": _redact_external(detail.repo_url),
            "install_plan": install_plan,
            "required_env": [e for e in detail.required_env if isinstance(e, str)],
        }
    )


async def api_mcp_discover_install(request: web.Request) -> web.Response:
    """POST /api/mcp/discover/install — body ``{"provider": ..., "id": ...}``.

    provider=official: translate the registry entry to an mcp.json spec and
    write it into the KiroCrew scope enabled=true (reusing the mcp.py write
    helpers). provider=capability: delegate to the edition manager.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "Request body must be a JSON object"}, status=400)
    provider_name = body.get("provider")
    server_id = body.get("id")
    if not isinstance(provider_name, str) or not isinstance(server_id, str):
        return web.json_response({"error": "'provider' and 'id' must be strings"}, status=400)
    provider_name = provider_name.strip()
    server_id = server_id.strip()
    if not provider_name or not server_id:
        return web.json_response({"error": "Both 'provider' and 'id' are required"}, status=400)

    if provider_name == "capability":
        return await _install_via_capability(request, server_id)
    if provider_name == "official":
        return await _install_from_official(request, server_id)
    return web.json_response({"error": f"Unknown provider '{provider_name}'"}, status=400)


async def _install_from_official(request: web.Request, server_id: str) -> web.Response:
    """Fetch the registry entry, translate, and write into KiroCrew scope."""
    registry = await asyncio.to_thread(_get_registry)
    provider = registry.get("official")
    if provider is None or not provider.is_available():
        return web.json_response({"error": "Provider 'official' is not available"}, status=503)

    try:
        detail = await asyncio.wait_for(
            provider.fetch_detail(server_id), timeout=_DETAIL_TIMEOUT_SECS
        )
    except (ProviderUnavailableError, asyncio.TimeoutError) as exc:
        logger.warning("MCP install fetch failed for %s: %s", server_id, exc)
        return web.json_response({"error": "Provider 'official' is not available"}, status=503)
    if detail is None:
        return web.json_response({"error": f"Server '{server_id}' not found"}, status=404)
    if detail.install_plan is None:
        return web.json_response(
            {"error": f"Server '{server_id}' has no installable package"}, status=400
        )

    name = _derive_install_name(server_id)
    if not name or not _is_valid_mcp_name(name):
        return web.json_response(
            {"error": f"Cannot derive a valid server name from '{server_id}'"},
            status=400,
        )

    spec = dict(detail.install_plan.spec)
    method = detail.install_plan.method
    required_env = [e for e in detail.required_env if isinstance(e, str)]
    # Consent default: EVERY official-registry install lands disabled.
    # Registry entries are publisher-controlled on an open registry, so the
    # user's explicit enable in the servers table — after reviewing the
    # written spec (and filling any env vars) — is the consent step before
    # the process can ever spawn. Install never flips anything on.
    enable_now = False

    async with _get_mcp_lock():
        existing = _find_server_spec_anywhere(name)
        if existing is not None and not _same_core_spec(existing, spec):
            # Same name, different invocation — refuse to clobber the
            # user's existing server silently.
            sel().log_api_access(
                caller="dashboard",
                operation="mcp_discover_install",
                outcome="denied",
                source="dashboard",
                resources=f"official:{name} collision",
            )
            return web.json_response({"error": "name already in use"}, status=409)
        if existing is None:
            # Fresh install: always written disabled (consent default).
            # The store write can apply a Windows owner-only DACL via icacls —
            # a blocking subprocess kept off the event loop.
            await _offload_config_write(_set_kirocrew_entry, name, enabled=enable_now, spec=spec)
        else:
            # Identical spec already present — a reinstall is a pure no-op:
            # the user's env values AND enabled/disabled state survive.
            # (Re-enabling here would reopen the one-click path around the
            # configure-then-enable consent step.)
            enable_now = not existing.get("disabled", False)

    # Rebuild the rendered agent configs so the new server loads on the next
    # session, mirroring api_mcp_apply. Best-effort: the config write above
    # already succeeded and survives a failed rebuild.
    try:
        # circular import: kiro_crew.agent imports dashboard handlers.
        from kiro_crew.agent import rebuild_agent_config

        await asyncio.to_thread(rebuild_agent_config)
    except Exception:
        logger.warning("rebuild_agent_config failed after MCP install", exc_info=True)

    sel().log_api_access(
        caller="dashboard",
        operation="mcp_discover_install",
        outcome="ok",
        source="dashboard",
        resources=f"official:{name} method={method}",
    )
    return web.json_response(
        {
            "ok": True,
            "name": name,
            "required_env": required_env,
            "method": method,
            "enabled": enable_now,
        }
    )


async def _install_via_capability(request: web.Request, server_id: str) -> web.Response:
    """Delegate install to the edition's capability manager (CPP seam).

    Mirrors ``api_capability_mcp_install``: the manager owns the install
    mechanics and error translation; the core only applies side effects
    (agent-config sync + refresh push) and audit-logs the outcome.
    """
    # The id crosses into the edition manager verbatim — apply the same
    # allowlist the other MCP mutation endpoints use before it leaves core.
    if not _is_valid_mcp_name(server_id):
        return web.json_response({"error": f"Invalid server id '{server_id[:64]}'"}, status=400)

    registry = await asyncio.to_thread(_get_registry)
    provider = registry.get("capability")
    if provider is None or not provider.is_available():
        return web.json_response({"error": "Provider 'capability' is not available"}, status=503)

    # circular import: agents/mcp import sibling handler modules at call time.
    from kiro_crew.dashboard.handlers._shared import _capability_manager
    from kiro_crew.dashboard.handlers.agents import _get_config_lock
    from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent

    res = await _capability_manager().install_mcp(server_id)
    if not res.ok:
        sel().log_api_access(
            caller="dashboard",
            operation="mcp_discover_install",
            outcome="error",
            source="dashboard",
            resources=f"capability:{server_id}",
        )
        message = (res.message or "install failed")[:500]
        return web.json_response({"error": _redact_external(message)}, status=500)

    async with _get_config_lock():
        # Off the loop: _sync_mcp_to_agent takes bridges' synchronous _mcp_lock
        # for a full kirocrew.json RMW; a direct call would freeze the gateway if
        # app registration holds that lock. Same offload as api_capability_mcp_install.
        await asyncio.to_thread(_sync_mcp_to_agent, server_id, True)
    state = request.app["state"]
    state.push_refresh("agents")

    sel().log_api_access(
        caller="dashboard",
        operation="mcp_discover_install",
        outcome="ok",
        source="dashboard",
        resources=f"capability:{server_id}",
    )
    return web.json_response(
        {"ok": True, "name": server_id, "required_env": [], "method": "capability"}
    )
