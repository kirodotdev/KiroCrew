"""Route registration for workspaces, agents, agent CRUD, edition capability agents.

One contiguous slice of the dashboard's route table, kept in its original
order. aiohttp resolves routes in REGISTRATION order, and several routes here
rely on a literal path being registered before a pattern that would otherwise
swallow it, so neither the lines within this function nor the order in which
``server.start_dashboard`` calls the registrars may be rearranged.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard import handlers


def register(app: web.Application) -> None:
    """Register the agents routes on *app*."""
    # Workspaces
    app.router.add_get("/api/workspaces", handlers.api_workspaces)
    app.router.add_post("/api/workspaces", handlers.api_workspaces_create)
    app.router.add_put("/api/workspaces/{name}", handlers.api_workspaces_update)
    app.router.add_delete("/api/workspaces/{name}", handlers.api_workspaces_delete)
    # Agents
    app.router.add_get("/api/agents/installed", handlers.api_agents_installed)
    app.router.add_get("/api/models", handlers.api_models)
    app.router.add_get("/api/effort-levels", handlers.api_effort_levels)
    app.router.add_get("/api/slash-commands", handlers.api_slash_commands)
    app.router.add_get("/api/agents/detail/{name}", handlers.api_agent_detail)
    app.router.add_patch("/api/agents/detail/{name}", handlers.api_agent_detail)
    app.router.add_delete("/api/agents/detail/{name}", handlers.api_agent_detail)
    # Kiro Crew Agent CRUD
    app.router.add_get("/api/agents", handlers.api_kirocrew_agents)
    app.router.add_get("/api/agents/resolved-model", handlers.api_kirocrew_agent_resolved_model)
    app.router.add_post("/api/agents", handlers.api_kirocrew_agents_create)
    app.router.add_post("/api/agents/sync", handlers.api_kirocrew_agents_sync)
    app.router.add_put("/api/agents/{name}", handlers.api_kirocrew_agent_update)
    app.router.add_delete("/api/agents/{name}", handlers.api_kirocrew_agent_delete)
    # Edition capability agents
    app.router.add_get("/api/capability/agents", handlers.api_capability_agents_list)
    app.router.add_post("/api/capability/agents/install", handlers.api_capability_agents_install)
    app.router.add_post(
        "/api/capability/agents/uninstall", handlers.api_capability_agents_uninstall
    )
    # Edition capability plugins (agent-client integrations + drift reconcile)
    app.router.add_get("/api/capability/plugins", handlers.api_capability_plugins_list)
    app.router.add_post("/api/capability/plugins/sync", handlers.api_capability_plugins_sync)
