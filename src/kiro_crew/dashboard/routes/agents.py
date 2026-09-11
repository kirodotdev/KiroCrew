"""Route registration for workspaces, agents, agent CRUD, edition capability agents,
the crew roster, and the shared appearance library.

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
    app.router.add_post("/api/agents/detail/{name}/fork", handlers.api_agent_fork)
    app.router.add_post("/api/agents/detail/{name}/publish", handlers.api_agent_publish)
    app.router.add_post("/api/agents/detail/{name}/reset", handlers.api_agent_reset)
    # Kiro Crew Agent CRUD
    app.router.add_get("/api/agents", handlers.api_kirocrew_agents)
    app.router.add_get("/api/agents/resolved-model", handlers.api_kirocrew_agent_resolved_model)
    app.router.add_post("/api/agents", handlers.api_kirocrew_agents_create)
    app.router.add_post("/api/agents/sync", handlers.api_kirocrew_agents_sync)
    app.router.add_put("/api/agents/{name}", handlers.api_kirocrew_agent_update)
    app.router.add_delete("/api/agents/{name}", handlers.api_kirocrew_agent_delete)
    # Per-crew uploaded avatar (the "image" tier; file under the data home,
    # served through the authenticated API so remote dashboards work)
    app.router.add_get("/api/agents/{name}/avatar", handlers.api_kirocrew_agent_avatar_get)
    app.router.add_post("/api/agents/{name}/avatar", handlers.api_kirocrew_agent_avatar_upload)
    # Edition capability agents
    app.router.add_get("/api/capability/agents", handlers.api_capability_agents_list)
    app.router.add_post("/api/capability/agents/install", handlers.api_capability_agents_install)
    app.router.add_post(
        "/api/capability/agents/uninstall", handlers.api_capability_agents_uninstall
    )
    # Edition capability plugins (agent-client integrations + drift reconcile)
    app.router.add_get("/api/capability/plugins", handlers.api_capability_plugins_list)
    app.router.add_post("/api/capability/plugins/sync", handlers.api_capability_plugins_sync)
    # Crew Members (roster + per-member pinned DM thread)
    app.router.add_get("/api/members", handlers.api_members)
    app.router.add_post("/api/members/{slug}/thread", handlers.api_member_thread)
    app.router.add_get("/api/members/{slug}/activity", handlers.api_member_activity)
    app.router.add_get("/api/members/{slug}/rules", handlers.api_member_rules_get)
    app.router.add_put("/api/members/{slug}/rules", handlers.api_member_rules_put)

    # Crew appearance library: the dashboard's own pack store, separate from
    # Crew Companion's. On the dashboard router so a crew's face renders while
    # that app is disabled or absent.
    #
    # Literals before the {id} pattern: aiohttp resolves in registration order,
    # so `/api/appearances/import` registered after `/api/appearances/{id}`
    # would be swallowed by it.
    app.router.add_get("/api/appearances", handlers.api_appearances_list)
    app.router.add_post("/api/appearances/import", handlers.api_appearances_import)
    app.router.add_post("/api/appearances/petdex/fetch", handlers.api_appearances_petdex_fetch)
    app.router.add_get("/api/appearances/{id}", handlers.api_appearance_detail)
    app.router.add_delete("/api/appearances/{id}", handlers.api_appearance_delete)
    app.router.add_get("/api/appearances/{id}/slot/{slot}", handlers.api_appearance_slot)
    app.router.add_get("/api/appearances/{id}/sound/{state}", handlers.api_appearance_sound)
