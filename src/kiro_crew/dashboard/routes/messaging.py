"""Route registration for Slack and Teams settings, script hooks, inbound webhook management.

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
    """Register the messaging routes on *app*."""
    # Slack settings (dashboard-only, NOT in _register_mcp_routes: that set is
    # also mounted on the token-less API-only server, and these endpoints
    # write credentials / expose config state, so they must sit behind the
    # dashboard's token auth in addition to the direct-local write gate).
    app.router.add_get("/api/slack/config", handlers.api_slack_config_get)
    app.router.add_put("/api/slack/config", handlers.api_slack_config_save)
    app.router.add_get("/api/slack/manifest", handlers.api_slack_manifest)
    app.router.add_get("/api/discord/config", handlers.api_discord_config_get)
    app.router.add_put("/api/discord/config", handlers.api_discord_config_save)
    app.router.add_get("/api/telegram/config", handlers.api_telegram_config_get)
    app.router.add_put("/api/telegram/config", handlers.api_telegram_config_save)
    app.router.add_get("/api/webex/config", handlers.api_webex_config_get)
    app.router.add_put("/api/webex/config", handlers.api_webex_config_save)
    app.router.add_get("/api/wecom/config", handlers.api_wecom_config_get)
    app.router.add_put("/api/wecom/config", handlers.api_wecom_config_save)
    # Microsoft Teams: inbound Bot Framework webhook (self-authenticating via
    # JWT; exempt from the cookie gate) + read-only status for the settings UI.
    app.router.add_post("/api/messaging/teams", handlers.api_teams_activity)
    app.router.add_get("/api/teams/config", handlers.api_teams_config_get)
    app.router.add_put("/api/teams/config", handlers.api_teams_config_save)

    # Script Hooks
    app.router.add_get("/api/hooks", handlers.api_hooks)
    app.router.add_get("/api/kiro-hooks", handlers.api_kiro_hooks)
    app.router.add_post("/api/hooks", handlers.api_hooks_create)
    app.router.add_put("/api/hooks/{hook_id}", handlers.api_hook_detail)
    app.router.add_delete("/api/hooks/{hook_id}", handlers.api_hook_detail)
    app.router.add_post("/api/hooks/{hook_id}/toggle", handlers.api_hook_toggle)
    app.router.add_post("/api/hooks/{hook_id}/test", handlers.api_hook_test)

    # Inbound webhook management (dashboard-authed — the webhook token itself
    # only ever authenticates POST /api/hooks/agent, never these).
    app.router.add_get("/api/webhooks", handlers.api_webhooks)
    app.router.add_post("/api/webhooks/tokens", handlers.api_webhook_token_create)
    app.router.add_delete("/api/webhooks/tokens/{token_id}", handlers.api_webhook_token_delete)
    app.router.add_delete("/api/webhooks/contexts/{hook_id}", handlers.api_webhook_context_delete)
    app.router.add_post("/api/webhooks/test", handlers.api_webhook_test)
    app.router.add_post("/api/webhooks/switch", handlers.api_webhooks_switch)
