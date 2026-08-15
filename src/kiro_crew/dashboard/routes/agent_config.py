"""Route registration for agent config, MCP servers, MCP discovery and the shared gateway.

One contiguous slice of the dashboard's route table, kept in its original
order. aiohttp resolves routes in REGISTRATION order, and several routes here
rely on a literal path being registered before a pattern that would otherwise
swallow it, so neither the lines within this function nor the order in which
``server.start_dashboard`` calls the registrars may be rearranged.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard import handlers
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


def register(app: web.Application) -> None:
    """Register the agent_config routes on *app*."""
    # Agent config
    app.router.add_get("/api/agent/config", handlers.api_agent_config)
    app.router.add_put("/api/agent/config", handlers.api_agent_config)
    app.router.add_get("/api/config/default-agent", handlers.api_default_agent)
    app.router.add_put("/api/config/default-agent", handlers.api_default_agent)
    app.router.add_get("/api/config/schema", handlers.api_config_schema)
    app.router.add_get("/api/config/kirocrew", handlers.api_kirocrew_config)
    app.router.add_put("/api/config/kirocrew", handlers.api_kirocrew_config)
    app.router.add_patch("/api/config/kirocrew", handlers.api_kirocrew_config_patch)
    app.router.add_get("/api/config/theme", handlers.api_theme_config)
    app.router.add_put("/api/config/theme", handlers.api_theme_config)
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
    app.router.add_post("/api/mcp/sync", handlers.api_mcp_sync)
    app.router.add_post("/api/mcp/apply", handlers.api_mcp_apply)
    app.router.add_post("/api/mcp/toggle", handlers.api_mcp_toggle)
    app.router.add_post("/api/mcp/toggle-tool", handlers.api_mcp_toggle_tool)
    app.router.add_post("/api/mcp/toggle-all", handlers.api_mcp_toggle_all)
    app.router.add_post("/api/mcp/remove", handlers.api_mcp_remove)
    app.router.add_post("/api/mcp/oauth/relay", handlers.api_mcp_oauth_relay)
    app.router.add_post("/api/connections/mint", handlers.api_connections_mint)
    app.router.add_get("/api/connections/mint", handlers.api_connections_mint_state)
    # REST-style MCP server registration (App Kit)
    app.router.add_put("/api/mcp/servers/{name}", handlers.api_mcp_server_detail)
    app.router.add_delete("/api/mcp/servers/{name}", handlers.api_mcp_server_detail)
    # Shared MCP gateway (pool)
    app.router.add_get("/api/mcp-gateway/status", handlers.api_mcp_gateway_status)
    app.router.add_post("/api/mcp-gateway/enable", handlers.api_mcp_gateway_enable)
    app.router.add_get("/api/mcp-gateway/metrics", handlers.api_mcp_gateway_metrics)
    app.router.add_get("/api/mcp-gateway/servers", handlers.api_mcp_gateway_servers)
    app.router.add_post("/api/mcp-gateway/servers/stub", handlers.api_mcp_gateway_set_stub)
    # AIM integration
    app.router.add_get("/api/capability/mcp", handlers.api_capability_mcp_list)
    app.router.add_post("/api/capability/mcp/install", handlers.api_capability_mcp_install)
    app.router.add_post("/api/capability/mcp/uninstall", handlers.api_capability_mcp_uninstall)
    app.router.add_get("/api/capability/skills", handlers.api_capability_skills_list)
    app.router.add_post("/api/capability/skills/install", handlers.api_capability_skills_install)
    app.router.add_post(
        "/api/capability/skills/uninstall", handlers.api_capability_skills_uninstall
    )
