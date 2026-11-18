"""Route registration for agent config, MCP servers, MCP discovery and the shared gateway.

One contiguous slice of the dashboard's route table, kept in its original
order. aiohttp resolves routes in REGISTRATION order, and several routes here
rely on a literal path being registered before a pattern that would otherwise
swallow it, so neither the lines within this function nor the order in which
``server.start_dashboard`` calls the registrars may be rearranged.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard import handlers
from kiro_crew.dashboard.handlers.acp_backend_status import (
    api_acp_backend_recheck,
    api_acp_backend_status,
)
from kiro_crew.dashboard.handlers.agent_spec_pins import api_agent_spec_pin_write
from kiro_crew.dashboard.handlers.gateway_footprint import api_mcp_gateway_footprint
from kiro_crew.dashboard.handlers.mcp_custom import (
    api_mcp_custom_add,
    api_mcp_custom_get,
    api_mcp_custom_update,
)
from kiro_crew.dashboard.handlers.mcp_discover import (
    api_mcp_discover,
    api_mcp_discover_detail,
    api_mcp_discover_install,
)

logger = logging.getLogger(__name__)


def tool_tester_enabled() -> bool:
    """Whether ``mcp.tool_tester`` is on; a config that cannot be read reads as off. Blocking."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        return KiroCrewConfig.load().mcp.tool_tester is True
    except Exception:
        logger.debug("mcp.tool_tester: config unreadable; the route stays off", exc_info=True)
        return False


async def api_mcp_try_tool(request: web.Request) -> web.StreamResponse:
    """``POST /api/mcp/try-tool``, the owner's Try a tool panel, while ``mcp.tool_tester`` is on.

    Registered at every start but loads nothing until a request
    (no-new-work-on-gateway-boot-path): the owner gate answers first, as on every
    mutating route here; with the setting off the path then answers as an unknown
    route does. The setting is read per request, off the loop, and the tester
    module is imported only once it is on, so turning it on needs no restart.
    """
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "mcp_try_tool")
    if denied is not None:
        return denied
    if not await asyncio.to_thread(tool_tester_enabled):
        raise web.HTTPNotFound()
    from kiro_crew.dashboard.handlers import mcp_try_tool

    return await mcp_try_tool.api_mcp_try_tool(request)


def register(app: web.Application) -> None:
    """Register the agent_config routes on *app*."""
    # Agent config
    app.router.add_get("/api/agent/config", handlers.api_agent_config)
    app.router.add_put("/api/agent/config", handlers.api_agent_config)
    app.router.add_get("/api/config/default-agent", handlers.api_default_agent)
    app.router.add_put("/api/config/default-agent", handlers.api_default_agent)
    # Which spec file a contested agent name runs (agent.spec_pins); owner-only.
    app.router.add_put("/api/agent-spec-pins/{agent}", api_agent_spec_pin_write)
    app.router.add_get("/api/config/schema", handlers.api_config_schema)
    # Per-backend selectability + whether THIS machine has the harness installed.
    # Beside the schema route because the dashboard's backend switch reads both:
    # the schema says which options this build/policy allows, this says which of
    # them would actually start.
    app.router.add_get("/api/acp-backends", api_acp_backend_status)
    # The same facts, re-taken for ONE backend with this process's cached absence
    # dropped first. A POST because it mutates spawn-path state: the GET above may
    # only report the divergence, which is what ``restart_required`` is for.
    app.router.add_post("/api/acp-backends/recheck", api_acp_backend_recheck)
    app.router.add_get("/api/config/kirocrew", handlers.api_kirocrew_config)
    app.router.add_put("/api/config/kirocrew", handlers.api_kirocrew_config)
    app.router.add_patch("/api/config/kirocrew", handlers.api_kirocrew_config_patch)
    app.router.add_get("/api/config/theme", handlers.api_theme_config)
    app.router.add_put("/api/config/theme", handlers.api_theme_config)
    # Host-side backup of the renderer's own settings (localStorage), so an
    # origin or userData change does not read as "the upgrade ate my settings".
    # Deliberately NOT under /api/config: these keys are client-owned and never
    # enter config.json. See kiro_crew/ui_prefs.py.
    app.router.add_get("/api/ui-prefs", handlers.api_ui_prefs)
    app.router.add_put("/api/ui-prefs", handlers.api_ui_prefs)
    app.router.add_get(
        "/api/onboarding/import/scan",
        handlers.api_onboarding_import_scan,
    )
    app.router.add_post(
        "/api/onboarding/import/apply",
        handlers.api_onboarding_import_apply,
    )
    app.router.add_put(
        "/api/onboarding/import/state",
        handlers.api_onboarding_import_state,
    )
    app.router.add_get("/api/dashboard/config", handlers.api_dashboard_config)
    app.router.add_put("/api/dashboard/config", handlers.api_dashboard_config)

    # MCP servers
    app.router.add_get("/api/mcp", handlers.api_mcp_servers)
    app.router.add_get("/api/mcp/scopes", handlers.api_mcp_global_scopes)
    app.router.add_get("/api/mcp/unreadable", handlers.api_mcp_unreadable_files)
    app.router.add_get("/api/mcp/active", handlers.api_mcp_active)
    # Multi-provider MCP discovery (official registry + optional edition capability provider)
    app.router.add_get("/api/mcp/discover", api_mcp_discover)
    app.router.add_get("/api/mcp/discover/detail", api_mcp_discover_detail)
    app.router.add_post("/api/mcp/discover/install", api_mcp_discover_install)
    # Manual MCP server management (Add Custom modal + per-server JSON edit)
    app.router.add_post("/api/mcp/custom", api_mcp_custom_add)
    app.router.add_get("/api/mcp/custom/{name}", api_mcp_custom_get)
    app.router.add_put("/api/mcp/custom/{name}", api_mcp_custom_update)
    app.router.add_post("/api/mcp/probe", handlers.api_mcp_probe)
    app.router.add_get("/api/mcp/probe", handlers.api_mcp_probe_cached)
    # The owner's Try a tool panel; while mcp.tool_tester is off it answers as an
    # unknown route does.
    app.router.add_post("/api/mcp/try-tool", api_mcp_try_tool)
    app.router.add_post("/api/mcp/quarantine/clear", handlers.api_mcp_quarantine_clear)
    app.router.add_post("/api/mcp/measure", handlers.api_mcp_measure_start)
    app.router.add_get("/api/mcp/measure", handlers.api_mcp_measure_progress)
    app.router.add_post("/api/mcp/sync", handlers.api_mcp_sync)
    app.router.add_post("/api/mcp/apply", handlers.api_mcp_apply)
    app.router.add_post("/api/mcp/toggle", handlers.api_mcp_toggle)
    app.router.add_post("/api/mcp/toggle-tool", handlers.api_mcp_toggle_tool)
    app.router.add_post("/api/mcp/toggle-all", handlers.api_mcp_toggle_all)
    app.router.add_post("/api/mcp/remove", handlers.api_mcp_remove)
    app.router.add_post("/api/mcp/oauth/relay", handlers.api_mcp_oauth_relay)
    app.router.add_post("/api/connections/mint", handlers.api_connections_mint)
    app.router.add_get("/api/connections/mint", handlers.api_connections_mint_state)
    app.router.add_post("/api/connections/premint", handlers.api_connections_premint)
    app.router.add_get("/api/connections/status", handlers.api_connections_status)
    app.router.add_post("/api/connections/test", handlers.api_connections_test)
    app.router.add_post("/api/connections/cancel", handlers.api_connections_cancel)
    app.router.add_post("/api/connections/disconnect", handlers.api_connections_disconnect)
    # GET only (no HEAD): token auth lets just GET through; ``state`` is the gate.
    app.router.add_get(
        "/api/connections/oauth/callback",
        handlers.api_connections_oauth_callback,
        allow_head=False,
    )
    app.router.add_get("/api/connections/oauth-clients", handlers.api_connections_oauth_clients)
    app.router.add_put(
        "/api/connections/oauth-clients/{slug}", handlers.api_connections_oauth_client_put
    )
    app.router.add_delete(
        "/api/connections/oauth-clients/{slug}", handlers.api_connections_oauth_client_delete
    )
    # REST-style MCP server registration (App Kit)
    app.router.add_put("/api/mcp/servers/{name}", handlers.api_mcp_server_detail)
    app.router.add_delete("/api/mcp/servers/{name}", handlers.api_mcp_server_detail)
    # Shared MCP gateway (pool)
    app.router.add_get("/api/mcp-gateway/status", handlers.api_mcp_gateway_status)
    app.router.add_post("/api/mcp-gateway/enable", handlers.api_mcp_gateway_enable)
    app.router.add_get("/api/mcp-gateway/metrics", handlers.api_mcp_gateway_metrics)
    app.router.add_get("/api/mcp-gateway/footprint", api_mcp_gateway_footprint)
    app.router.add_get("/api/mcp-gateway/servers", handlers.api_mcp_gateway_servers)
    app.router.add_get("/api/mcp-gateway/servers/launch", handlers.api_mcp_gateway_server_launch)
    app.router.add_post("/api/mcp-gateway/servers/launch", handlers.api_mcp_gateway_server_launches)
    app.router.add_post("/api/mcp-gateway/servers/stub", handlers.api_mcp_gateway_set_stub)
    app.router.add_post("/api/mcp-gateway/resolve-refresh", handlers.api_mcp_resolve_refresh)
    # AIM integration
    app.router.add_get("/api/capability/mcp", handlers.api_capability_mcp_list)
    app.router.add_post("/api/capability/mcp/install", handlers.api_capability_mcp_install)
    app.router.add_post("/api/capability/mcp/uninstall", handlers.api_capability_mcp_uninstall)
    app.router.add_get("/api/capability/skills", handlers.api_capability_skills_list)
    app.router.add_post("/api/capability/skills/install", handlers.api_capability_skills_install)
    app.router.add_post(
        "/api/capability/skills/uninstall", handlers.api_capability_skills_uninstall
    )
